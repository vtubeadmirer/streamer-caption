"""Support bundles for bug reports.

Two flavours:
  * full    (make_support_bundle)  - everything, including caption text; for sending
                                     privately to a helper you trust.
  * privacy (make_issue_bundle)    - what a public GitHub issue needs and nothing that
                                     repeats what was said on stream. A string survives
                                     only under an allow-listed key (event types, times,
                                     engine/model/device names, error messages); every
                                     other string becomes a length marker, so timing and
                                     error structure stay intact. Paths under the user
                                     profile appear as %USERPROFILE%.
"""
from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import os
import platform
import re
import sys
import time
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.archive_maintenance import recover_unclean_sessions
from core.config import PROJECT_ROOT, USER_DATA_ROOT, load_config, user_path
from core.transcript_archive import TranscriptArchive
from tools.broadcast_validation import write_validation_report


def latest_files(pattern: str, limit: int = 8, root: Path | None = None) -> list[Path]:
    base = root or PROJECT_ROOT
    files = [path for path in base.glob(pattern) if path.is_file()]
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


def latest_log_dirs(
    log_root: Path | None = None,
    *,
    limit: int = 3,
) -> list[Path]:
    root = Path(log_root) if log_root else user_path(load_config()["log"]["dir"])
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
    entries = [(path, _bundle_name(path)) for path in include]
    # Named explicitly: a configured log folder can sit outside both roots.
    for log_dir in log_dirs:
        entries.extend(
            (path, f"logs/{log_dir.name}/{path.name}")
            for path in log_dir.glob("*")
            if path.is_file()
        )
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        seen: set[Path] = set()
        for path, name in entries:
            if path.exists() and path not in seen:
                seen.add(path)
                zf.write(path, name)
    return out


# --------------------------------------------------------------------------- privacy bundle
#
# Allow-list, not deny-list: new pipeline keys keep carrying spoken words (corrected_text,
# summary headlines, keyword terms, recap text, speaker labels), so a string survives only
# under a key known to hold something code or hardware chose. Numbers, booleans and null
# always pass; list items inherit the list's key.

# Enums, ids, timestamps, versions and device/model names.
STRUCTURAL_KEYS = frozenset({
    "ts", "created_at", "generated_at", "started_at", "ended_at", "last_caption_at",
    "event_type", "type", "kind", "verdict", "status", "state", "reason", "reasons",
    "session_id", "id", "version", "python", "platform", "os", "profile", "mode",
    "engine", "method", "methods", "analysis_method", "window_mode", "provider", "model",
    "device", "device_label", "compute_type", "language", "task", "vad_engine",
    "worker_mode", "worker_fallback", "runtime_vad_override", "process_state", "job_type",
    "active_job_type", "stt_job_type", "recovery_source", "audio_source", "audio_mode",
    "source_priority", "duplicate_source", "defense_reason", "defense_signals",
    "capture_state",
    # caption engine choice and fallback state (engines/simulstreaming/route.py)
    "requested_engine", "active_engine", "caption_engine", "model_setting", "engine_model",
})
# File-system locations; the user-profile part is masked.
PATH_KEYS = frozenset({"path", "log_path", "session_jsonl", "registered_dll_dirs"})
# Exception and warning text: kept for debugging, masked and cut to ERROR_TEXT_LIMIT.
ERROR_KEYS = frozenset({
    "error", "last_error", "fatal_error", "degraded_reason", "source_last_error",
    "recovery_reason", "fallback_reason", "pipeline_warning",
})
ERROR_TEXT_LIMIT = 300
# Keys that are structural only inside one of these containers (a GPU row, a check result).
SCOPED_KEYS = {
    "name": frozenset({"gpu", "gpus", "packages", "devices", "checks"}),
    "detail": frozenset({"checks"}),
    "args": frozenset({"checks"}),
}
# Error texts that quote raw engine output (possibly a caption line) after a fixed prefix.
RAW_OUTPUT_PREFIXES = ("Invalid SimulStreaming JSON:",)
# A work OneDrive folder is named after the organisation: "OneDrive - <Org>".
_ONEDRIVE_ORG = re.compile(r"(OneDrive - )[^\\/\r\n\"']+")


def _redacted(value: str) -> str:
    return f"[redacted {len(value)} chars]" if value else ""


@lru_cache(maxsize=8)
def _profile_pattern(*roots: str) -> re.Pattern[str] | None:
    alternatives = []
    for root in roots:
        parts = [part for part in re.split(r"[\\/]+", root) if part]
        if not parts or (len(parts) == 1 and parts[0].endswith(":")):
            continue  # never mask a bare drive or "/"
        lead = r"[\\/]+" if root.startswith(("\\", "/")) else ""
        alternatives.append(lead + r"[\\/]+".join(re.escape(part) for part in parts))
    if not alternatives:
        return None
    alternatives.sort(key=len, reverse=True)
    # Either separator and any case. No word boundary on purpose: "C:\Users\kim.old" also
    # names the person, so masking it as "%USERPROFILE%.old" is the right mistake to make.
    return re.compile("(?:" + "|".join(alternatives) + ")", re.IGNORECASE)


def mask_private_paths(text: str) -> str:
    """Show paths under the user profile as %USERPROFILE%\\... and hide the organisation
    in a work OneDrive folder; both name the person."""
    if "\\" not in text and "/" not in text:
        return text  # not a path (most enums and timestamps); skip the regex work
    pattern = _profile_pattern(str(Path.home()), os.environ.get("USERPROFILE", ""))
    if pattern is not None:
        text = pattern.sub("%USERPROFILE%", text)
    return _ONEDRIVE_ORG.sub(r"\1[redacted]", text)


def _mask_tree(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _mask_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_tree(item) for item in value]
    if isinstance(value, str):
        return mask_private_paths(value)
    return value


def _error_text(value: str) -> str:
    for prefix in RAW_OUTPUT_PREFIXES:
        head, found, tail = value.partition(prefix)
        if found:
            value = f"{head}{prefix} {_redacted(tail.strip())}"
    text = mask_private_paths(value).strip()
    return text if len(text) <= ERROR_TEXT_LIMIT else text[: ERROR_TEXT_LIMIT - 1] + "…"


def _scrub_string(value: str, key: str, container: str) -> str:
    if key in ERROR_KEYS:
        return _error_text(value)
    if key in STRUCTURAL_KEYS or key in PATH_KEYS or container in SCOPED_KEYS.get(key, ()):
        return mask_private_paths(value)
    return _redacted(value)


def scrub_value(value: Any, *, key: str = "", container: str = "") -> Any:
    """Keep numbers, flags and structure; keep a string only under an allow-listed key.
    `container` is the key of the enclosing object; it scopes keys such as "name"."""
    if isinstance(value, dict):
        return {mask_private_paths(str(k)): scrub_value(v, key=str(k), container=key) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_value(item, key=key, container=container) for item in value]
    if isinstance(value, str):
        return _scrub_string(value, key, container)
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


_COLUMN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def scrub_csv(text: str) -> str:
    """Column names in the header row and numeric cells survive; any other cell is redacted."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    try:
        for index, row in enumerate(csv.reader(io.StringIO(text))):
            writer.writerow([
                cell if not cell.strip() or _is_number(cell) or (index == 0 and _COLUMN_NAME.fullmatch(cell))
                else _redacted(cell)
                for cell in row
            ])
    except csv.Error:
        return "unparsable_csv\n"
    return out.getvalue()


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
    return _mask_tree(cleaned)


def environment_summary(config: dict[str, Any]) -> dict[str, Any]:
    """Fixed fields only, so it needs no allow-list; profile paths are masked."""
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
    return _mask_tree({
        "app": info,
        "gpu": gpu,
        "packages": versions,
        "os": platform.platform(),
        "profile": config.get("app", {}).get("profile"),
        "whisper": {k: config.get("whisper", {}).get(k) for k in ("model", "device", "compute_type", "language")},
        "audio": {k: config.get("audio", {}).get(k) for k in ("source", "device", "system_device")},
        "vad": {k: config.get("vad", {}).get(k) for k in ("engine", "device", "speech_threshold")},
    })


# Backslash, "C:/", a "/" that starts a word, or an already masked %USERPROFILE% path.
_PATH_HINT = re.compile(r"\\|[A-Za-z]:/|(?:^|\s)/[^\s/]|%[A-Za-z]+%")


def _issue_field(value: Any) -> str:
    text = " ".join(str(value).split())
    return "(local path)" if _PATH_HINT.search(text) else text


def environment_text(environment: dict[str, Any]) -> str:
    """Short plain-text block for the GitHub issue form; it ends up in a public URL, so a
    value that holds a path (a local model folder, an error naming a file) is left out."""
    app = environment.get("app", {})
    gpu = environment.get("gpu", {})
    gpu_name = gpu.get("name") or gpu.get("gpu") or gpu.get("error") or "unknown"
    if isinstance(gpu.get("gpus"), list) and gpu["gpus"]:
        first = gpu["gpus"][0]
        gpu_name = first.get("name", gpu_name) if isinstance(first, dict) else str(first)
    whisper = environment.get("whisper", {})
    audio = environment.get("audio", {})
    packages = environment.get("packages", {})
    field = _issue_field
    lines = [
        f"version: {field(app.get('version'))} ({'portable' if app.get('portable') else 'installed' if app.get('managed') else 'source'})",
        f"os: {field(environment.get('os'))}",
        f"python: {field(app.get('python'))}",
        f"gpu: {field(gpu_name)}",
        f"model: {field(whisper.get('model'))} / {field(whisper.get('device'))} / {field(whisper.get('compute_type'))}",
        f"audio: source={field(audio.get('source'))} vad={field(environment.get('vad', {}).get('engine'))}",
        f"packages: faster_whisper={field(packages.get('faster_whisper'))} ctranslate2={field(packages.get('ctranslate2'))} torch={field(packages.get('torch'))}",
        f"profile: {field(environment.get('profile'))}",
    ]
    return "\n".join(lines)


# Only structured files go in, each through the allow-list. Transcripts (.txt, .md), audio
# and free-form logs stay out: nothing in them says which words are structure.
_LOG_FILE_SCRUBBERS: dict[str, Callable[[str], str]] = {
    ".jsonl": scrub_jsonl,
    ".json": scrub_json_text,
    ".csv": scrub_csv,
}


def _log_folder_entries(log_dir: Path) -> Iterable[tuple[str, bytes]]:
    for path in sorted(log_dir.glob("*")):
        scrub = _LOG_FILE_SCRUBBERS.get(path.suffix.lower())
        if scrub is None or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        yield f"logs/{log_dir.name}/{path.name}", scrub(text).encode("utf-8")


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
            "Strings were kept only under known structural keys (event types, times,\n"
            "engine/model/device names, error messages). Captions, summaries, keywords,\n"
            "recaps, speaker labels, prompts and word lists appear as [redacted N chars].\n"
            "Paths under the user profile appear as %USERPROFILE%.\n"
        ).encode("utf-8"))
        add("environment.json", json.dumps(environment, ensure_ascii=False, indent=2).encode("utf-8"))
        add("config.json", json.dumps(scrub_config(config), ensure_ascii=False, indent=2).encode("utf-8"))
        if status is not None:
            add("status.json", json.dumps(scrub_value(status), ensure_ascii=False, indent=2).encode("utf-8"))
        if note.strip():
            add("note.txt", note.strip().encode("utf-8") + b"\n")
        for log_dir in latest_log_dirs(log_root or user_path(config["log"]["dir"]), limit=2):
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
