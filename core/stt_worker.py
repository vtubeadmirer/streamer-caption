from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from core.engine_contract import CaptionEngine
from core.events import CaptionEvent


@dataclass(frozen=True)
class SttJob:
    job_id: int
    job_type: str
    audio: Any
    sample_rate: int
    audio_offset_sec: float
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class SttResult:
    job: SttJob
    events: list[CaptionEvent]
    elapsed_sec: float


def enqueue_confirmed_by_priority(
    jobs: deque[SttJob],
    job: SttJob,
) -> None:
    priority = int(job.metadata.get("source_priority", 100))
    for index, queued in enumerate(jobs):
        queued_priority = int(queued.metadata.get("source_priority", 100))
        if priority < queued_priority:
            jobs.insert(index, job)
            return
    jobs.append(job)


class SttWorker:
    """Single-model STT worker with confirmed-job priority.

    GPU transcription remains single-threaded, while microphone capture can keep
    running. Pending jobs are replaceable; confirmed jobs are never discarded.
    """

    def __init__(self, engine: CaptionEngine, max_queue: int = 2) -> None:
        self.engine = engine
        self.max_queue = max(1, int(max_queue))
        self._confirmed_jobs: deque[SttJob] = deque()
        self._pending_jobs: deque[SttJob] = deque()
        self._results: queue.Queue[SttResult] = queue.Queue()
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._next_job_id = 0
        self._active_jobs = 0
        self._active_job: SttJob | None = None
        self._active_started_at: float | None = None
        self._stopping = False
        self.dropped_jobs = 0
        self.superseded_pending_jobs = 0
        self.skipped_pending_jobs = 0

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                return
            self._stopping = False
            self._thread = threading.Thread(target=self._run, name="stt-worker", daemon=True)
            self._thread.start()

    def submit(
        self,
        job_type: str,
        audio: Any,
        sample_rate: int,
        audio_offset_sec: float,
        metadata: dict[str, Any] | None = None,
        drop_oldest: bool = False,
    ) -> SttJob:
        with self._condition:
            job = SttJob(
                job_id=self._next_job_id,
                job_type=job_type,
                audio=audio,
                sample_rate=sample_rate,
                audio_offset_sec=audio_offset_sec,
                metadata=metadata or {},
            )
            self._next_job_id += 1

            if job_type == "confirmed":
                # Queued previews are obsolete once their final segment exists.
                superseded = len(self._pending_jobs)
                self.dropped_jobs += superseded
                self.superseded_pending_jobs += superseded
                self._pending_jobs.clear()
                enqueue_confirmed_by_priority(self._confirmed_jobs, job)
            else:
                if len(self._pending_jobs) >= self.max_queue:
                    if not drop_oldest:
                        raise queue.Full
                    self._pending_jobs.popleft()
                    self.dropped_jobs += 1
                    self.superseded_pending_jobs += 1
                self._pending_jobs.append(job)

            self._condition.notify_all()
            return job

    def submit_latest(
        self,
        job_type: str,
        audio: Any,
        sample_rate: int,
        audio_offset_sec: float,
        metadata: dict[str, Any] | None = None,
    ) -> SttJob:
        return self.submit(
            job_type=job_type,
            audio=audio,
            sample_rate=sample_rate,
            audio_offset_sec=audio_offset_sec,
            metadata=metadata,
            drop_oldest=True,
        )

    def can_accept_pending(self) -> bool:
        with self._condition:
            return (
                self._active_job is None
                and not self._confirmed_jobs
                and not self._pending_jobs
            )

    def mark_pending_skipped(self) -> None:
        with self._condition:
            self.skipped_pending_jobs += 1

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            active_elapsed = (
                time.monotonic() - self._active_started_at
                if self._active_started_at is not None
                else 0.0
            )
            return {
                "worker_mode": "thread",
                "active_job_type": (
                    self._active_job.job_type if self._active_job is not None else ""
                ),
                "active_job_id": (
                    self._active_job.job_id if self._active_job is not None else None
                ),
                "active_elapsed_sec": active_elapsed,
                "confirmed_queue": len(self._confirmed_jobs),
                "pending_queue": len(self._pending_jobs),
                "superseded_pending_jobs": self.superseded_pending_jobs,
                "skipped_pending_jobs": self.skipped_pending_jobs,
                "process_state": "",
                "process_restarts": 0,
                "recovered_confirmed_jobs": 0,
                "failed_recoveries": 0,
                "fatal_error": "",
            }

    def get_results(self, max_items: int = 10) -> list[SttResult]:
        results = []
        for _ in range(max_items):
            try:
                results.append(self._results.get_nowait())
            except queue.Empty:
                break
        return results

    def wait_for(self, job_id: int, timeout: float | None = None) -> SttResult:
        deadline = None if timeout is None else time.monotonic() + timeout
        deferred: list[SttResult] = []
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining == 0.0:
                for item in deferred:
                    self._results.put(item)
                raise TimeoutError(f"STT job timed out: {job_id}")
            result = self._results.get(timeout=remaining)
            if result.job.job_id == job_id:
                for item in deferred:
                    self._results.put(item)
                return result
            deferred.append(result)

    def wait_until_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._confirmed_jobs or self._pending_jobs or self._active_jobs:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def stop(self, timeout: float = 30.0) -> None:
        with self._condition:
            if self._thread is None:
                return
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        thread.join(timeout=timeout)
        with self._condition:
            if not thread.is_alive():
                self._thread = None

    def _run(self) -> None:
        while True:
            with self._condition:
                while (
                    not self._confirmed_jobs
                    and not self._pending_jobs
                    and not self._stopping
                ):
                    self._condition.wait()
                if (
                    self._stopping
                    and not self._confirmed_jobs
                    and not self._pending_jobs
                ):
                    return
                if self._confirmed_jobs:
                    job = self._confirmed_jobs.popleft()
                else:
                    job = self._pending_jobs.popleft()
                self._active_jobs += 1
                self._active_job = job
                self._active_started_at = time.monotonic()

            started = time.monotonic()
            events = list(self._run_job(job))
            self._results.put(
                SttResult(
                    job=job,
                    events=events,
                    elapsed_sec=time.monotonic() - started,
                )
            )

            with self._condition:
                self._active_jobs -= 1
                self._active_job = None
                self._active_started_at = None
                self._condition.notify_all()

    def _run_job(self, job: SttJob) -> Iterable[CaptionEvent]:
        try:
            for event in self.engine.on_audio_segment(
                job.audio,
                job.sample_rate,
                job.audio_offset_sec,
            ):
                yield replace(
                    event,
                    metadata={**job.metadata, **event.metadata},
                )
        except Exception as exc:
            yield CaptionEvent(
                event_type="engine_error",
                text=str(exc),
                audio_offset_sec=job.audio_offset_sec,
                engine=getattr(self.engine, "name", "unknown"),
                metadata=job.metadata,
            )
