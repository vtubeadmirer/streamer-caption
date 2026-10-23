from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.summarize_session import summarize


def main() -> int:
    path = ROOT / "reports" / "_test_session.jsonl"
    rows = [
        {
            "event_type": "caption_pending",
            "text": "pending",
            "extra": {"stt_elapsed_sec": 0.2},
        },
        {
            "event_type": "caption_confirmed",
            "text": "confirmed",
            "extra": {
                "stt_elapsed_sec": 0.5,
                "stt_job_id": 7,
                "recovered": True,
                "recovery_attempt": 1,
                "recovery_reason": "synthetic stall",
            },
        },
        {"event_type": "engine_error", "text": "oops"},
        {
            "event_type": "engine_status",
            "text": "v2_light session summary",
            "metadata": {
                "process_restarts": 1,
                "superseded_pending_jobs": 4,
                "skipped_pending_jobs": 3,
                "child_heartbeat_count": 12,
                "recovered_confirmed_jobs": 1,
                "failed_recoveries": 0,
                "committed_confirmed_jobs": 1,
                "duplicate_confirmed_results": 0,
                "audio_source_restarts": 2,
            },
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)  # reports/ is gitignored: absent on a fresh checkout (CI)
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(
                json.dumps(row, ensure_ascii=False) + chr(10)
            )
    summary = summarize(path)
    assert summary["rows"] == 4
    assert summary["counts"]["caption_confirmed"] == 1
    assert summary["last_caption"] == "confirmed"
    assert summary["stt_elapsed_max_sec"] == 0.5
    assert summary["process_restarts"] == 1
    assert summary["superseded_pending_jobs"] == 4
    assert summary["skipped_pending_jobs"] == 3
    assert summary["child_heartbeat_count"] == 12
    assert summary["recovered_confirmed_jobs"] == 1
    assert summary["max_recovery_attempt"] == 1
    assert summary["recovery_reasons"]["synthetic stall"] == 1
    assert summary["audio_source_restarts"] == 2
    path.unlink(missing_ok=True)
    print("summarize_session_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())