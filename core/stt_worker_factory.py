from __future__ import annotations

from typing import Any

from core.stt_process_worker import ProcessSttWorker
from core.stt_worker import SttWorker


def create_stt_worker(config: dict[str, Any], engine: Any) -> Any:
    streaming = config["streaming"]
    max_queue = int(streaming.get("pending_queue_max", 2))
    worker_mode = str(streaming.get("worker_mode", "process")).lower()
    if worker_mode == "process":
        return ProcessSttWorker(
            config,
            max_queue=max_queue,
            engine_factory="baseline",
        )
    if worker_mode == "thread":
        return SttWorker(engine, max_queue=max_queue)
    raise ValueError(f"Unsupported streaming.worker_mode: {worker_mode}")

def create_and_start_stt_worker(
    config: dict[str, Any],
    engine: Any,
) -> tuple[Any, str]:
    worker = create_stt_worker(config, engine)
    try:
        worker.start()
        return worker, ""
    except Exception as exc:
        if not isinstance(worker, ProcessSttWorker):
            raise
        max_queue = int(
            config["streaming"].get("pending_queue_max", 2)
        )
        fallback = SttWorker(engine, max_queue=max_queue)
        fallback.start()
        warning = (
            "STT process worker could not start; "
            f"using thread compatibility mode for this run: {exc}"
        )
        return fallback, warning