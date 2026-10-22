"""Support bundles for bug reports.

Two flavours:
  * full    (make_support_bundle)  - everything, including caption text; for sending
                                     privately to a helper you trust.
  * privacy (make_issue_bundle)    - what a public GitHub issue needs and nothing that
                                     repeats what was said on stream. Caption text,
                                     summaries, slang lists and prompts are replaced
                                     by length markers so timing and error structure
                                     stay intact.
"""
from __future__ import annotations

import argparse
import copy
import json
import platform
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.archive_maintenance import recover_unclean_sessions
from core.config import PROJECT_ROOT, USER_DATA_ROOT, load_config
from core.transcript_archive import TranscriptArchive
from tools.broadcast_validation import write_validation_report

# JSON keys whose string values can contain spoken words (captions, summaries, hints).
TEXT_KEYS = {
    "text", "ko", "en", "caption", "captions", "pending_text", "confirmed_text", "raw_text",
    "source_text", "translation", "summary", "summaries", "title", "topic", "topics",
    "highlights", "keywords", "bullets", "lines", "note", "notes", "annotation",
    "initial_prompt", "last_caption", "sample", "samples", "preview", "recap", "content",
    "sentences", "segments_text", "transcript", "query", "label", "last_error_text",
}
# Log-folder files that are pure text transcripts; never copied into the privacy bundle.
TRANSCRIPT_FILES = {"captions.txt", "latest_captions.txt", "broadcast_report.md", "validation_report.md"}


def latest_files(pattern: str, limit: int = 8, root: Path | None = None) -> list[Path]:
    base = root or PROJECT_ROOT
    files = [path for path in base.glob(pattern) if path.is_file()]
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


def latest_log_dirs(
    log_root: Path | None = None,
    *,
    limit: int = 3,
) -> list[Path]:
    root = Path(log_root or PROJECT_ROOT / "logs")
    dirs = [path for path in root.glob("*") if path.is_dir()]
    session_dirs = [
        path for path in dirs if (path / "session.jsonl").is_file()
    ]
    candidates = session_dirs or dirs
    return sorted(
        candidates,
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[: max(1, int(limit))]


def latest_log_dir(log_root: Path | None = None) -> Path | None:
    dirs = latest_log_dirs(log_root, limit=1)
    return dirs[0] if dirs else None


def prepare_latest_validation(log_dir: Path | None) -> Path | None:
    if log_dir is None:
        return None
    session_jsonl = log_dir / "session.jsonl"
    if not session_jsonl.exists():
        return None
    try:
        config = load_config()
        archive = TranscriptArchive.from_config(config)
        recover_unclean_sessions(archive, config)
        write_validation_report(session_jsonl, archive, config)
        return None
    except Exception as exc:
        error_path = PROJECT_ROOT / "reports" / "support_preflight_error.txt"
        error_path.parent.mkdir(parents=True, exist_ok=True)
        error_path.write_text(
            f"support preflight failed: {type(exc).__name__}: {exc}\n",
            encoding="utf-8",
        )
        return error_path


def _bundle_name(path: Path) -> str:
    for base in (PROJECT_ROOT, USER_DATA_ROOT):
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.name


def make_support_bundle() -> Path:
    out_dir = PROJECT_ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"streamer_caption_v2_support_{time.strftime('%Y-%m-%d_%H%M%S')}.zip"
    log_dirs = latest_log_dirs(limit=3)
    preflight_error = prepare_latest_validation(
        log_dirs[0] if log_dirs else None
    )
    include = [
        USER_DATA_ROOT / "config.json",
        USER_DATA_ROOT / "data" / "slang_dict.json",
        USER_DATA_ROOT / "data" / "whitelist.txt",
        USER_DATA_ROOT / "data" / "blocklist.txt",
    ]
    if preflight_error is not None:
        include.append(preflight_error)
    include.extend(latest_files("reports/*.json", limit=12))
    for log_dir in log_dirs:
        include.extend(
            path for path in log_dir.glob("*") if path.is_file()
        )
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        seen: set[Path] = set()
        for path in include:
            if path.exists() and path not in seen:
                seen.add(path)
                zf.write(path, _bundle_name(path))
    return out


# --------------------------------------------------------------------------- privacy bundle


def scrub_value(value: Any, *, key: str = "") -> Any:
    """Replace spoken-word strings by length markers; keep numbers, flags and structure."""
    if isinstance(value, dict):
        return {str(k): scrub_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_value(item, key=key) for item in value]
    if isinstance(value, str) and key in TEXT_KEYS:
        return f"[redacted {len(value)} chars]" if value else ""
    return value


def scrub_jsonl(text: str) -> str:
    lines: list[str] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            lines.append('{"unparsable_line": true}')
            continue
        lines.append(json.dumps(scrub_value(record), ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")


def scrub_json_text(text: str) -> str:
    try:
        value = json.loads(text)
    except ValueError:
        return '{"unparsable": true}\n'
    return json.dumps(scrub_value(value), ensure_ascii=False, indent=2) + "\n"


def scrub_config(config: dict[str, Any]) -> dict[str, Any]:
    cleaned = copy.deepcopy(config)
    whisper = cleaned.get("whisper")
    if isinstance(whisper, dict) and whisper.get("initial_prompt"):
        whisper["initial_prompt"] = f"[redacted {len(str(whisper['initial_prompt']))} chars]"
    caption = cleaned.get("caption")
    if isinstance(caption, dict):
        for key in ("microphone_label", "system_label"):
            if caption.get(key):
                caption[key] = f"[redacted {len(str(caption[key]))} chars]"
    return cleaned


def environment_summary(config: dict[str, Any]) -> dict[str, Any]:
    from core.app_info import describe  # local import keeps this tool light for the CLI path

    info = describe()
    gpu: dict[str, Any] = {}
    try:
        from tools.collect_diagnostics import run_nvidia_smi

        gpu = run_nvidia_smi()
    except Exception as exc:  # diagnostics are best effort
        gpu = {"error": f"{type(exc).__name__}: {exc}"}
    # Package metadata only: importing torch/ctranslate2 inside the admin HTTP thread can
    # crash the process (native DLL init), and the version is all a bug report needs.
    from importlib import metadata

    versions: dict[str, str] = {}
    for module, dist in (("faster_whisper", "faster-whisper"), ("ctranslate2", "ctranslate2"), ("torch", "torch"),
                         ("numpy", "numpy"), ("sounddevice", "sounddevice"), ("websockets", "websockets")):
        try:
            versions[module] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            versions[module] = "missing"
    return {
        "app": info,
        "gpu": gpu,
        "packages": versions,
        "os": platform.platform(),
        "profile": config.get("app", {}).get("profile"),
        "whisper": {k: config.get("whisper", {}).get(k) for k in ("model", "device", "compute_type", "language")},
        "audio": {k: config.get("audio", {}).get(k) for k in ("source", "device", "system_device")},
        "vad": {k: config.get("vad", {}).get(k) for k in ("engine", "device", "speech_threshold")},
    }


def environment_text(environment: dict[str, Any]) -> str:
    """Short plain-text block for the GitHub issue form."""
    app = environment.get("app", {})
    gpu = environment.get("gpu", {})
    gpu_name = gpu.get("name") or gpu.get("gpu") or gpu.get("error") or "unknown"
    if isinstance(gpu.get("gpus"), list) and gpu["gpus"]:
        first = gpu["gpus"][0]
        gpu_name = first.get("name", gpu_name) if isinstance(first, dict) else str(first)
    whisper = environment.get("whisper", {})
    audio = environment.get("audio", {})
    packages = environment.get("packages", {})
    lines = [
        f"version: {app.get('version')} ({'portable' if app.get('portable') else 'installed' if app.get('managed') else 'source'})",
        f"os: {environment.get('os')}",
        f"python: {app.get('python')}",
        f"gpu: {gpu_name}",
        f"model: {whisper.get('model')} / {whisper.get('device')} / {whisper.get('compute_type')}",
        f"audio: source={audio.get('source')} vad={environment.get('vad', {}).get('engine')}",
        f"packages: faster_whisper={packages.get('faster_whisper')} ctranslate2={packages.get('ctranslate2')} torch={packages.get('torch')}",
        f"profile: {environment.get('profile')}",
    ]
    return "\n".join(lines)


def _log_folder_entries(log_dir: Path) -> Iterable[tuple[str, bytes]]:
    for path in sorted(log_dir.glob("*")):
        if not path.is_file() or path.name in TRANSCRIPT_FILES:
            continue
        name = f"logs/{log_dir.name}/{path.name}"
        if path.suffix == ".jsonl":
            yield name, scrub_jsonl(path.read_text(encoding="utf-8", errors="replace")).encode("utf-8")
        elif path.suffix == ".json":
            yield name, scrub_json_text(path.read_text(encoding="utf-8", errors="replace")).encode("utf-8")
        elif path.suffix in {".csv", ".log"}:
            yield name, path.read_bytes()
        # anything else (.md, .txt, .wav) stays out of the privacy bundle


def make_issue_bundle(
    *,
    status: dict[str, Any] | None = None,
    note: str = "",
    log_root: Path | None = None,
    out_dir: Path | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bundle that is safe to attach to a public issue. Returns path + summary."""
    config = config or load_config()
    target_dir = out_dir or (PROJECT_ROOT / "reports")
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / f"streamer_caption_bugreport_{time.strftime('%Y-%m-%d_%H%M%S')}.zip"
    environment = environment_summary(config)
    files: list[str] = []
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        def add(name: str, data: bytes) -> None:
            zf.writestr(name, data)
            files.append(name)

        add("README.txt", (
            "Streamer Caption bug report bundle\n"
            "Caption text, summaries, prompts and word lists were removed before packing.\n"
            "Strings that carried spoken words appear as [redacted N chars].\n"
        ).encode("utf-8"))
        add("environment.json", json.dumps(environment, ensure_ascii=False, indent=2).encode("utf-8"))
        add("config.json", json.dumps(scrub_config(config), ensure_ascii=False, indent=2).encode("utf-8"))
        if status is not None:
            add("status.json", json.dumps(scrub_value(status), ensure_ascii=False, indent=2).encode("utf-8"))
        if note.strip():
            add("note.txt", note.strip().encode("utf-8") + b"\n")
        for log_dir in latest_log_dirs(log_root, limit=2):
            for name, data in _log_folder_entries(log_dir):
                add(name, data)
        for report in latest_files("reports/final_check_*.json", limit=1):
            add(f"reports/{report.name}", scrub_json_text(report.read_text(encoding="utf-8", errors="replace")).encode("utf-8"))
    return {
        "path": str(out),
        "name": out.name,
        "size": out.stat().st_size,
        "files": files,
        "environment": environment,
        "environment_text": environment_text(environment),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Streamer Caption support bundle")
    parser.add_argument("--privacy", action="store_true", help="issue bundle without caption text")
    parser.add_argument("--note", default="", help="free text to include (privacy bundle)")
    args = parser.parse_args()
    try:
        if args.privacy:
            result = make_issue_bundle(note=args.note)
            print(f"issue_bundle: {result['path']} ({len(result['files'])} files, no caption text)")
            return 0
        out = make_support_bundle()
    except Exception as exc:
        print(exc)
        return 1
    print(f"support_bundle: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
