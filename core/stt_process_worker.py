from __future__ import annotations

import multiprocessing
import queue
import threading
import time
from collections import deque
from dataclasses import replace
from typing import Any, Iterable

from core.events import CaptionEvent
from core.stt_worker import SttJob, SttResult, enqueue_confirmed_by_priority


class _EchoEngine:
    name = "echo"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def on_audio_segment(
        self,
        audio: object,
        sample_rate: int,
        audio_offset_sec: float,
    ) -> Iterable[CaptionEvent]:
        if isinstance(audio, dict):
            delay_sec = float(audio.get("sleep_sec", 0.0))
            if delay_sec > 0:
                time.sleep(delay_sec)
            if audio.get("raise_error"):
                raise RuntimeError("synthetic engine error")
            sample_count = int(audio.get("sample_count", 0))
        else:
            try:
                sample_count = len(audio)  # type: ignore[arg-type]
            except TypeError:
                sample_count = 0
        yield CaptionEvent(
            event_type="caption_confirmed",
            text=f"samples={sample_count}",
            confidence=1.0,
            audio_offset_sec=audio_offset_sec,
            engine=self.name,
            metadata={"sample_rate": sample_rate},
        )

    def close(self) -> None:
        return


def _create_engine(config: dict[str, Any], engine_factory: str) -> Any:
    if engine_factory == "echo":
        return _EchoEngine(config)
    if engine_factory != "baseline":
        raise ValueError(f"Unsupported process engine factory: {engine_factory}")

    from engines.baseline.engine import BaselineEngine

    return BaselineEngine(config)


def _execute_job(engine: Any, job: SttJob) -> list[CaptionEvent]:
    events: list[CaptionEvent] = []
    try:
        for event in engine.on_audio_segment(
            job.audio,
            job.sample_rate,
            job.audio_offset_sec,
        ):
            events.append(
                replace(
                    event,
                    metadata={**job.metadata, **event.metadata},
                )
            )
    except Exception as exc:
        events.append(
            CaptionEvent(
                event_type="engine_error",
                text=str(exc),
                audio_offset_sec=job.audio_offset_sec,
                engine=getattr(engine, "name", "unknown"),
                metadata=job.metadata,
            )
        )
    return events


def _engine_child_main(
    config: dict[str, Any],
    request_queue: Any,
    response_queue: Any,
    generation: int,
    engine_factory: str,
) -> None:
    engine: Any | None = None
    heartbeat_stop = threading.Event()
    heartbeat_interval = max(
        0.25,
        float(
            config.get("streaming", {}).get(
                "stt_heartbeat_interval_sec",
                2.0,
            )
        ),
    )

    def heartbeat_loop() -> None:
        while not heartbeat_stop.wait(heartbeat_interval):
            try:
                response_queue.put_nowait(
                    {
                        "type": "heartbeat",
                        "generation": generation,
                    }
                )
            except Exception:
                return

    heartbeat_thread = threading.Thread(
        target=heartbeat_loop,
        name=f"stt-heartbeat-{generation}",
        daemon=True,
    )
    try:
        engine = _create_engine(config, engine_factory)
        response_queue.put(
            {
                "type": "ready",
                "generation": generation,
                "engine": getattr(engine, "name", engine_factory),
            }
        )
        heartbeat_thread.start()
        while True:
            message = request_queue.get()
            if message is None:
                return
            if message.get("type") != "job":
                continue
            job = message["job"]
            started = time.monotonic()
            events = _execute_job(engine, job)
            response_queue.put(
                {
                    "type": "result",
                    "generation": generation,
                    "job_id": job.job_id,
                    "events": events,
                    "elapsed_sec": time.monotonic() - started,
                }
            )
    except (KeyboardInterrupt, EOFError):
        return
    except BaseException as exc:
        try:
            response_queue.put(
                {
                    "type": "child_error",
                    "generation": generation,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        except Exception:
            pass
        raise
    finally:
        heartbeat_stop.set()
        if heartbeat_thread.is_alive():
            heartbeat_thread.join(timeout=0.5)
        if engine is not None:
            try:
                engine.close()
            except Exception:
                pass

class ProcessSttWorker:
    """Single-model STT child process with confirmed-job recovery.

    Audio capture, VAD, queues, and the ring buffer remain in the parent. A
    stalled or crashed child can therefore be terminated without losing queued
    confirmed segments.
    """

    def __init__(
        self,
        config: dict[str, Any],
        max_queue: int = 2,
        engine_factory: str = "baseline",
        max_process_restarts: int | None = None,
    ) -> None:
        self.config = config
        self.max_queue = max(1, int(max_queue))
        self.engine_factory = engine_factory
        recovery = config.get("streaming", {}).get("error_recovery", {})
        self.max_process_restarts = (
            int(max_process_restarts)
            if max_process_restarts is not None
            else int(recovery.get("max_retry", 3))
        )

        self._ctx = multiprocessing.get_context("spawn")
        self._confirmed_jobs: deque[SttJob] = deque()
        self._pending_jobs: deque[SttJob] = deque()
        self._results: queue.Queue[SttResult] = queue.Queue()
        self._condition = threading.Condition()
        self._controller: threading.Thread | None = None
        self._process: Any | None = None
        self._request_queue: Any | None = None
        self._response_queue: Any | None = None
        self._next_job_id = 0
        self._active_job: SttJob | None = None
        self._active_started_at: float | None = None
        self._process_started_at: float | None = None
        self._last_child_heartbeat_at: float | None = None
        self._heartbeat_count = 0
        self._process_state = "stopped"
        self._generation = 0
        self._restart_requested = ""
        self._stopping = False
        self._fatal_error = ""
        self._completed_jobs = 0

        self.dropped_jobs = 0
        self.superseded_pending_jobs = 0
        self.skipped_pending_jobs = 0
        self.process_restarts = 0
        self.recovered_confirmed_jobs = 0
        self.failed_recoveries = 0

    def start(self) -> None:
        with self._condition:
            if self._controller is not None:
                return
            self._stopping = False
            self._fatal_error = ""
            self._start_child_locked()
            self._controller = threading.Thread(
                target=self._control_loop,
                name="stt-process-controller",
                daemon=True,
            )
            self._controller.start()

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
                self._process_state == "ready"
                and self._active_job is None
                and not self._confirmed_jobs
                and not self._pending_jobs
            )

    def mark_pending_skipped(self) -> None:
        with self._condition:
            self.skipped_pending_jobs += 1

    def restart_stalled_job(self, reason: str) -> bool:
        with self._condition:
            if (
                self._stopping
                or self._fatal_error
                or self._process is None
                or self._restart_requested
            ):
                return False
            self._restart_requested = reason
            self._process_state = "restart_requested"
            self._condition.notify_all()
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            now = time.monotonic()
            active_elapsed = (
                now - self._active_started_at
                if self._active_started_at is not None
                else 0.0
            )
            process_start_elapsed = (
                now - self._process_started_at
                if self._process_started_at is not None
                else 0.0
            )
            process_pid = (
                self._process.pid if self._process is not None else None
            )
            process_alive = (
                self._process is not None
                and self._process.exitcode is None
            )
            controller_alive = (
                self._controller is not None
                and self._controller.is_alive()
            )
            heartbeat_age = (
                now - self._last_child_heartbeat_at
                if self._last_child_heartbeat_at is not None
                else None
            )
            recovery_attempt = 0
            if self._active_job is not None:
                recovery_attempt = int(
                    self._active_job.metadata.get("recovery_attempt", 0)
                )
            return {
                "worker_mode": "process",
                "active_job_type": (
                    self._active_job.job_type
                    if self._active_job is not None
                    else ""
                ),
                "active_job_id": (
                    self._active_job.job_id
                    if self._active_job is not None
                    else None
                ),
                "active_elapsed_sec": active_elapsed,
                "active_cold_start": (
                    self._active_job is not None
                    and self._completed_jobs == 0
                ),
                "active_recovery_attempt": recovery_attempt,
                "confirmed_queue": len(self._confirmed_jobs),
                "pending_queue": len(self._pending_jobs),
                "superseded_pending_jobs": self.superseded_pending_jobs,
                "skipped_pending_jobs": self.skipped_pending_jobs,
                "process_state": self._process_state,
                "process_pid": process_pid,
                "process_alive": process_alive,
                "controller_alive": controller_alive,
                "process_start_elapsed_sec": process_start_elapsed,
                "child_heartbeat_age_sec": heartbeat_age,
                "child_heartbeat_count": self._heartbeat_count,
                "process_restarts": self.process_restarts,
                "recovered_confirmed_jobs": self.recovered_confirmed_jobs,
                "failed_recoveries": self.failed_recoveries,
                "fatal_error": self._fatal_error,
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
            remaining = (
                None
                if deadline is None
                else max(0.0, deadline - time.monotonic())
            )
            if remaining == 0.0:
                for item in deferred:
                    self._results.put(item)
                raise TimeoutError(f"STT process job timed out: {job_id}")
            result = self._results.get(timeout=remaining)
            if result.job.job_id == job_id:
                for item in deferred:
                    self._results.put(item)
                return result
            deferred.append(result)

    def wait_until_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while (
                self._confirmed_jobs
                or self._pending_jobs
                or self._active_job is not None
            ):
                remaining = (
                    None if deadline is None else deadline - time.monotonic()
                )
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def stop(self, timeout: float = 30.0) -> None:
        with self._condition:
            controller = self._controller
            if controller is None:
                self._shutdown_child(graceful=True)
                return
            self._stopping = True
            self._condition.notify_all()
        controller.join(timeout=timeout)
        if controller.is_alive():
            self._shutdown_child(graceful=False)
        with self._condition:
            if not controller.is_alive():
                self._controller = None
            self._process_state = "stopped"

    def _start_child_locked(self) -> None:
        self._generation += 1
        generation = self._generation
        self._request_queue = self._ctx.Queue(maxsize=1)
        self._response_queue = self._ctx.Queue()
        self._process = self._ctx.Process(
            target=_engine_child_main,
            args=(
                self.config,
                self._request_queue,
                self._response_queue,
                generation,
                self.engine_factory,
            ),
            name=f"stt-engine-{generation}",
            daemon=True,
        )
        self._process.start()
        self._process_started_at = time.monotonic()
        self._last_child_heartbeat_at = self._process_started_at
        self._process_state = "starting"

    def _control_loop(self) -> None:
        try:
            while True:
                self._drain_messages()
                with self._condition:
                    if self._stopping:
                        return
                    restart_reason = self._restart_requested
                    process = self._process
                    process_dead = (
                        process is not None
                        and process.exitcode is not None
                    )
                    exit_code = (
                        process.exitcode if process is not None else None
                    )
                if restart_reason:
                    self._restart_child(restart_reason)
                    continue
                if process_dead:
                    self._restart_child(
                        f"STT child exited with code {exit_code}"
                    )
                    continue
                self._dispatch_next()
                with self._condition:
                    self._condition.wait(timeout=0.05)
        finally:
            self._shutdown_child(graceful=True)

    def _drain_messages(self) -> None:
        while True:
            with self._condition:
                response_queue = self._response_queue
            if response_queue is None:
                return
            try:
                message = response_queue.get_nowait()
            except queue.Empty:
                return
            except (EOFError, OSError):
                return
            self._handle_message(message)

    def _handle_message(self, message: dict[str, Any]) -> None:
        with self._condition:
            if int(message.get("generation", -1)) != self._generation:
                return
            self._last_child_heartbeat_at = time.monotonic()
            message_type = message.get("type")
            if message_type == "heartbeat":
                self._heartbeat_count += 1
                self._condition.notify_all()
                return
            if message_type == "ready":
                self._process_state = "ready"
                self._condition.notify_all()
                return
            if message_type == "child_error":
                self._restart_requested = str(
                    message.get("error", "STT child error")
                )
                self._process_state = "restart_requested"
                self._condition.notify_all()
                return
            if message_type != "result":
                return

            job = self._active_job
            if job is None or job.job_id != int(message["job_id"]):
                return
            events = list(message.get("events", []))
            engine_errors = [
                event
                for event in events
                if event.event_type == "engine_error"
            ]
            if engine_errors:
                self._restart_requested = (
                    f"STT job {job.job_id} returned engine_error: "
                    f"{engine_errors[0].text}"
                )
                self._process_state = "restart_requested"
                self._condition.notify_all()
                return

            self._results.put(
                SttResult(
                    job=job,
                    events=events,
                    elapsed_sec=float(message.get("elapsed_sec", 0.0)),
                )
            )
            self._active_job = None
            self._active_started_at = None
            self._restart_requested = ""
            self._process_state = "ready"
            self._completed_jobs += 1
            self._condition.notify_all()

    def _dispatch_next(self) -> None:
        with self._condition:
            if (
                self._process_state != "ready"
                or self._active_job is not None
                or self._request_queue is None
            ):
                return
            if self._confirmed_jobs:
                job = self._confirmed_jobs.popleft()
            elif self._pending_jobs:
                job = self._pending_jobs.popleft()
            else:
                return
            request_queue = self._request_queue
            self._active_job = job
            self._active_started_at = time.monotonic()
            self._process_state = "busy"

        try:
            request_queue.put_nowait({"type": "job", "job": job})
        except Exception as exc:
            with self._condition:
                if self._active_job is job:
                    if job.job_type == "confirmed":
                        self._confirmed_jobs.appendleft(job)
                    else:
                        self._pending_jobs.appendleft(job)
                    self._active_job = None
                    self._active_started_at = None
                self._restart_requested = (
                    f"failed to dispatch STT job: {exc}"
                )
                self._process_state = "restart_requested"
                self._condition.notify_all()

    def _restart_child(self, reason: str) -> None:
        with self._condition:
            if self._stopping:
                return
            active = self._active_job
            if self.process_restarts >= self.max_process_restarts:
                self.failed_recoveries += 1
                self._fatal_error = (
                    f"STT process recovery limit reached: {reason}"
                )
                self._process_state = "failed"
                self._restart_requested = ""
                jobs: list[SttJob] = []
                if active is not None:
                    jobs.append(active)
                jobs.extend(self._confirmed_jobs)
                jobs.extend(self._pending_jobs)
                self._active_job = None
                self._active_started_at = None
                self._confirmed_jobs.clear()
                self._pending_jobs.clear()
                for job in jobs:
                    self._results.put(
                        SttResult(
                            job=job,
                            events=[
                                CaptionEvent(
                                    event_type="engine_error",
                                    text=self._fatal_error,
                                    audio_offset_sec=job.audio_offset_sec,
                                    engine=self.engine_factory,
                                    metadata={
                                        **job.metadata,
                                        "recovery_failed": True,
                                        "recovery_reason": reason,
                                    },
                                )
                            ],
                            elapsed_sec=0.0,
                        )
                    )
                resources = self._detach_child_locked()
                self._condition.notify_all()
                should_restart = False
            else:
                self.process_restarts += 1
                if active is not None:
                    if active.job_type == "confirmed":
                        recovered = replace(
                            active,
                            metadata={
                                **active.metadata,
                                "recovered": True,
                                "recovery_source": "active_confirmed_job",
                                "recovery_reason": reason,
                                "recovery_attempt": (
                                    int(
                                        active.metadata.get(
                                            "recovery_attempt",
                                            0,
                                        )
                                    )
                                    + 1
                                ),
                            },
                        )
                        self._confirmed_jobs.appendleft(recovered)
                        self.recovered_confirmed_jobs += 1
                    else:
                        self.dropped_jobs += 1
                        self.superseded_pending_jobs += 1
                self._active_job = None
                self._active_started_at = None
                self._restart_requested = ""
                self._process_state = "restarting"
                resources = self._detach_child_locked()
                should_restart = True
                self._condition.notify_all()

        self._close_resources(resources, graceful=False)
        if should_restart:
            with self._condition:
                if not self._stopping:
                    self._start_child_locked()
                    self._condition.notify_all()

    def _detach_child_locked(self) -> tuple[Any, Any, Any]:
        resources = (
            self._process,
            self._request_queue,
            self._response_queue,
        )
        self._process = None
        self._request_queue = None
        self._response_queue = None
        self._process_started_at = None
        return resources

    def _shutdown_child(self, graceful: bool) -> None:
        with self._condition:
            resources = self._detach_child_locked()
        self._close_resources(resources, graceful=graceful)

    @staticmethod
    def _close_resources(
        resources: tuple[Any, Any, Any],
        graceful: bool,
    ) -> None:
        process, request_queue, response_queue = resources
        if process is not None and process.is_alive():
            if graceful and request_queue is not None:
                try:
                    request_queue.put_nowait(None)
                except Exception:
                    pass
                process.join(timeout=2.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5.0)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(timeout=2.0)
        for child_queue in (request_queue, response_queue):
            if child_queue is None:
                continue
            try:
                child_queue.cancel_join_thread()
            except Exception:
                pass
            try:
                child_queue.close()
            except Exception:
                pass