from __future__ import annotations

import ctypes
import json
import os
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.config import user_path
from core.transcript_archive import TranscriptArchive
from features.broadcast_report import write_session_report


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def process_is_alive(process_id: int) -> bool:
    pid = int(process_id)
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def recover_unclean_sessions(
    archive: TranscriptArchive,
    config: dict[str, Any],
    *,
    current_session_id: str | None = None,
    stale_after_sec: float = 30.0,
    generate_reports: bool = True,
) -> list[dict[str, Any]]:
    now = _now()
    recovered: list[dict[str, Any]] = []
    log_root = user_path(config.get("log", {}).get("dir", "logs")).resolve()
    for session in archive.list_open_sessions(limit=1000):
        session_id = str(session["id"])
        if session_id == current_session_id:
            continue
        metadata = dict(session.get("metadata", {}))
        raw_process_id = metadata.get("process_id")
        try:
            process_id = int(raw_process_id) if raw_process_id is not None else None
        except (TypeError, ValueError):
            process_id = None
        if process_id is not None and process_is_alive(process_id):
            continue
        started_at = _parse_datetime(session.get("started_at"))
        if process_id is None and started_at is not None:
            age = (now - started_at).total_seconds()
            if age < max(0.0, float(stale_after_sec)):
                continue
        ended_at = session.get("last_caption_at") or now.isoformat(
            timespec="milliseconds"
        )
        archive.close_session(
            session_id,
            ended_at=str(ended_at),
            metadata={
                "unclean_shutdown_recovered": True,
                "recovered_at": now.isoformat(timespec="milliseconds"),
                "original_process_id": process_id,
            },
        )
        report_paths: dict[str, str] = {}
        session_dir = (log_root / session_id).resolve()
        if (
            generate_reports
            and session_dir.parent == log_root
            and session_dir.exists()
        ):
            try:
                report_paths = write_session_report(
                    archive,
                    session_id,
                    session_dir,
                    config,
                )
            except Exception as exc:
                report_paths = {"error": str(exc)}
        recovered.append(
            {
                "session_id": session_id,
                "ended_at": ended_at,
                "report_paths": report_paths,
            }
        )
    return recovered


def create_archive_backup(
    archive: TranscriptArchive,
    output_dir: str | Path | None = None,
) -> Path:
    destination_dir = user_path(output_dir or "reports/archive_backups").resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now().strftime("%Y-%m-%d_%H%M%S_%f")[:-3]
    sqlite_path = destination_dir / f"transcripts_{stamp}.sqlite3"
    zip_path = destination_dir / f"transcript_archive_{stamp}.zip"
    archive.backup_to(sqlite_path)
    manifest = {
        "created_at": _now().isoformat(timespec="seconds"),
        "archive": archive.archive_info(),
        "database_file": sqlite_path.name,
    }
    try:
        with zipfile.ZipFile(
            zip_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as bundle:
            bundle.write(sqlite_path, f"data/{sqlite_path.name}")
            bundle.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
    finally:
        sqlite_path.unlink(missing_ok=True)
    return zip_path


def prune_archive(
    archive: TranscriptArchive,
    retention_days: int,
) -> dict[str, Any]:
    days = int(retention_days)
    if not 1 <= days <= 3650:
        raise ValueError("retention_days must be between 1 and 3650")
    cutoff = _now() - timedelta(days=days)
    deleted = archive.prune_sessions_before(
        cutoff.isoformat(timespec="milliseconds")
    )
    return {
        "retention_days": days,
        "cutoff": cutoff.isoformat(timespec="milliseconds"),
        "deleted_session_ids": deleted,
        "deleted_count": len(deleted),
    }
