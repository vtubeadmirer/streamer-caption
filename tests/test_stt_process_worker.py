from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.stt_process_worker import ProcessSttWorker


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise TimeoutError("condition was not reached")


def main() -> int:
    config = load_config()
    config["streaming"]["stt_heartbeat_interval_sec"] = 0.25
    config["streaming"]["stt_heartbeat_timeout_sec"] = 2.0
    worker = ProcessSttWorker(
        config,
        max_queue=1,
        engine_factory="echo",
        max_process_restarts=2,
    )
    worker.start()
    try:
        wait_for(
            lambda: worker.snapshot()["process_state"] == "ready",
            timeout=10.0,
        )
        wait_for(
            lambda: worker.snapshot()["child_heartbeat_count"] >= 1,
            timeout=3.0,
        )
        heartbeat_snapshot = worker.snapshot()
        assert heartbeat_snapshot["process_alive"]
        assert heartbeat_snapshot["controller_alive"]
        assert heartbeat_snapshot["child_heartbeat_age_sec"] < 1.0
        job = worker.submit(
            "confirmed",
            {"sleep_sec": 0.8, "sample_count": 3200},
            16000,
            1.0,
            metadata={
                "segment_start_sample": 16000,
                "segment_end_sample": 19200,
            },
        )
        wait_for(
            lambda: worker.snapshot()["active_job_id"] == job.job_id,
            timeout=5.0,
        )
        assert worker.restart_stalled_job("synthetic stall")
        result = worker.wait_for(job.job_id, timeout=15.0)
        assert result.events[0].text == "samples=3200"
        assert result.job.metadata["recovered"] is True
        assert result.job.metadata["recovery_attempt"] == 1
        assert result.job.metadata["segment_start_sample"] == 16000

        snapshot = worker.snapshot()
        assert snapshot["process_restarts"] == 1, snapshot
        assert snapshot["recovered_confirmed_jobs"] == 1, snapshot
        assert snapshot["fatal_error"] == "", snapshot
        assert worker.wait_until_idle(timeout=2.0)
    finally:
        worker.stop(timeout=5.0)

    failing = ProcessSttWorker(
        config,
        max_queue=1,
        engine_factory="echo",
        max_process_restarts=0,
    )
    failing.start()
    try:
        wait_for(
            lambda: failing.snapshot()["process_state"] == "ready",
            timeout=10.0,
        )
        failed_job = failing.submit(
            "confirmed",
            {"raise_error": True, "sample_count": 100},
            16000,
            0.0,
        )
        failed_result = failing.wait_for(
            failed_job.job_id,
            timeout=10.0,
        )
        assert failed_result.events[0].event_type == "engine_error"
        assert failed_result.events[0].metadata["recovery_failed"]
        assert failing.snapshot()["failed_recoveries"] == 1
    finally:
        failing.stop(timeout=5.0)
    baseline = ProcessSttWorker(
        config,
        max_queue=1,
        engine_factory="baseline",
        max_process_restarts=0,
    )
    baseline.start()
    try:
        wait_for(
            lambda: baseline.snapshot()["process_state"] == "ready",
            timeout=10.0,
        )
        baseline_snapshot = baseline.snapshot()
        assert baseline_snapshot["process_pid"] is not None
        assert baseline_snapshot["fatal_error"] == ""
    finally:
        baseline.stop(timeout=5.0)

    print("stt_process_worker_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())