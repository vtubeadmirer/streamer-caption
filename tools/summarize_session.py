from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config, user_path


def latest_session_jsonl() -> Path | None:
    log_dir = user_path(load_config()["log"]["dir"])
    paths = sorted(log_dir.glob("*/session.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def summarize(path: Path) -> dict[str, Any]:
    counts: dict[str, int] = {}
    last_caption = ""
    last_error = ""
    stt_elapsed: list[float] = []
    dropped_jobs = 0
    superseded_pending_jobs = 0
    skipped_pending_jobs = 0
    child_heartbeat_count = 0
    process_restarts = 0
    recovered_confirmed_jobs = 0
    failed_recoveries = 0
    committed_confirmed_jobs = 0
    duplicate_confirmed_results = 0
    audio_source_restarts = 0
    max_recovery_attempt = 0
    recovery_reasons: dict[str, int] = {}
    recovered_job_ids: set[int] = set()
    rows = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rows += 1
            record = json.loads(line)
            event_type = str(record.get("event_type", "unknown"))
            counts[event_type] = counts.get(event_type, 0) + 1
            text = str(record.get("text", ""))
            if event_type == "caption_confirmed":
                last_caption = text
            if event_type == "engine_error":
                last_error = text
            metadata = record.get("metadata") or {}
            extra = record.get("extra") or {}
            elapsed = extra.get("stt_elapsed_sec") or metadata.get("stt_elapsed_sec")
            if isinstance(elapsed, (int, float)):
                stt_elapsed.append(float(elapsed))
            combined = {**metadata, **extra}
            dropped = combined.get("dropped_jobs")
            if isinstance(dropped, int):
                dropped_jobs = max(dropped_jobs, dropped)
            superseded_pending_jobs = max(
                superseded_pending_jobs,
                int(combined.get("superseded_pending_jobs", 0) or 0),
            )
            skipped_pending_jobs = max(
                skipped_pending_jobs,
                int(combined.get("skipped_pending_jobs", 0) or 0),
            )
            child_heartbeat_count = max(
                child_heartbeat_count,
                int(combined.get("child_heartbeat_count", 0) or 0),
            )
            process_restarts = max(
                process_restarts,
                int(combined.get("process_restarts", 0) or 0),
            )
            recovered_confirmed_jobs = max(
                recovered_confirmed_jobs,
                int(
                    combined.get("recovered_confirmed_jobs", 0)
                    or 0
                ),
            )
            failed_recoveries = max(
                failed_recoveries,
                int(combined.get("failed_recoveries", 0) or 0),
            )
            committed_confirmed_jobs = max(
                committed_confirmed_jobs,
                int(
                    combined.get("committed_confirmed_jobs", 0)
                    or 0
                ),
            )
            duplicate_confirmed_results = max(
                duplicate_confirmed_results,
                int(
                    combined.get("duplicate_confirmed_results", 0)
                    or 0
                ),
            )
            audio_source_restarts = max(
                audio_source_restarts,
                int(combined.get("audio_source_restarts", 0) or 0),
            )
            attempt = int(combined.get("recovery_attempt", 0) or 0)
            max_recovery_attempt = max(max_recovery_attempt, attempt)
            if combined.get("recovered"):
                job_id = combined.get("stt_job_id")
                if isinstance(job_id, int):
                    recovered_job_ids.add(job_id)
                reason = str(
                    combined.get("recovery_reason", "unknown")
                )
                recovery_reasons[reason] = (
                    recovery_reasons.get(reason, 0) + 1
                )
    return {
        "path": str(path),
        "rows": rows,
        "counts": counts,
        "last_caption": last_caption,
        "last_error": last_error,
        "stt_elapsed_avg_sec": sum(stt_elapsed) / len(stt_elapsed) if stt_elapsed else None,
        "stt_elapsed_max_sec": max(stt_elapsed) if stt_elapsed else None,
        "dropped_jobs": dropped_jobs,
        "superseded_pending_jobs": superseded_pending_jobs,
        "skipped_pending_jobs": skipped_pending_jobs,
        "child_heartbeat_count": child_heartbeat_count,
        "process_restarts": process_restarts,
        "recovered_confirmed_jobs": max(
            recovered_confirmed_jobs,
            len(recovered_job_ids),
        ),
        "failed_recoveries": failed_recoveries,
        "committed_confirmed_jobs": committed_confirmed_jobs,
        "duplicate_confirmed_results": duplicate_confirmed_results,
        "audio_source_restarts": audio_source_restarts,
        "max_recovery_attempt": max_recovery_attempt,
        "recovery_reasons": recovery_reasons,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize session.jsonl caption logs.")
    parser.add_argument("path", nargs="?")
    parser.add_argument("--latest", action="store_true")
    args = parser.parse_args()

    path = Path(args.path) if args.path else latest_session_jsonl()
    if path is None:
        print("No session.jsonl found. Run the app first.")
        return 1
    print(json.dumps(summarize(path), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
