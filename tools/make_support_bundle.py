from __future__ import annotations

import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.archive_maintenance import recover_unclean_sessions
from core.config import PROJECT_ROOT, load_config
from core.transcript_archive import TranscriptArchive
from tools.broadcast_validation import write_validation_report


def latest_files(pattern: str, limit: int = 8) -> list[Path]:
    files = [path for path in PROJECT_ROOT.glob(pattern) if path.is_file()]
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
        error_path.write_text(
            f"support preflight failed: {type(exc).__name__}: {exc}\n",
            encoding="utf-8",
        )
        return error_path


def make_support_bundle() -> Path:
    out_dir = PROJECT_ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"streamer_caption_v2_support_{time.strftime('%Y-%m-%d_%H%M%S')}.zip"
    log_dirs = latest_log_dirs(limit=3)
    preflight_error = prepare_latest_validation(
        log_dirs[0] if log_dirs else None
    )
    include = [
        PROJECT_ROOT / "config.json",
        PROJECT_ROOT / "data" / "slang_dict.json",
        PROJECT_ROOT / "data" / "whitelist.txt",
        PROJECT_ROOT / "data" / "blocklist.txt",
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
                zf.write(path, path.relative_to(PROJECT_ROOT))
    return out


def main() -> int:
    try:
        out = make_support_bundle()
    except Exception as exc:
        print(exc)
        return 1
    print(f"support_bundle: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
