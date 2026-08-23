from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.stt_process_worker import ProcessSttWorker
from core.stt_worker import SttWorker
from core.stt_worker_factory import (
    create_and_start_stt_worker,
    create_stt_worker,
)


class FakeEngine:
    name = "fake"


def main() -> int:
    config = load_config()
    worker = create_stt_worker(config, FakeEngine())
    assert isinstance(worker, ProcessSttWorker)

    thread_config = copy.deepcopy(config)
    thread_config["streaming"]["worker_mode"] = "thread"
    fallback = create_stt_worker(thread_config, FakeEngine())
    assert isinstance(fallback, SttWorker)

    original_start = ProcessSttWorker.start

    def fail_start(self):
        raise OSError("synthetic spawn failure")

    ProcessSttWorker.start = fail_start
    try:
        started, warning = create_and_start_stt_worker(
            config,
            FakeEngine(),
        )
    finally:
        ProcessSttWorker.start = original_start
    try:
        assert isinstance(started, SttWorker)
        assert "thread compatibility mode" in warning
    finally:
        started.stop(timeout=2.0)

    print("stt_worker_factory_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())