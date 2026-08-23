from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from core.events import CaptionEvent
from core.transcript_archive import TranscriptArchive
from features.summarizer import summarize_captions
from features.topic_detector import detect_topic_transitions


class LiveAnalysisWorker:
    """Latest-only CPU analysis worker for live summary and topic events."""

    def __init__(
        self,
        config: dict[str, Any],
        session_id: str,
        callback: Callable[[CaptionEvent], None],
    ) -> None:
        self.config = config
        self.session_id = session_id
        self.callback = callback
        self.store = TranscriptArchive.from_config(config)
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._pending: tuple[float, int] | None = None
        self._stopping = False
        self._submitted_count = 0
        self._last_offset = 0.0
        self._last_scheduled_at = 0.0
        self._last_summary_signature: tuple[Any, ...] | None = None
        self._last_topic_offset = -1.0
        self.events_emitted = 0
        self.error_count = 0
        self.last_error = ""

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run,
                name="live-caption-analysis",
                daemon=True,
            )
            self._thread.start()

    def submit(self, latest_offset_sec: float) -> None:
        archive = self.config.get("archive", {})
        if not bool(archive.get("live_analysis_enabled", True)):
            return
        with self._condition:
            self._submitted_count += 1
            self._last_offset = max(
                self._last_offset,
                float(latest_offset_sec),
            )
            every = max(
                1,
                int(archive.get("live_analysis_every_captions", 8)),
            )
            minimum_interval = max(
                0.0,
                float(
                    archive.get(
                        "live_analysis_min_interval_sec",
                        15.0,
                    )
                ),
            )
            now = time.monotonic()
            due = (
                self._submitted_count == 1
                or self._submitted_count % every == 0
                or now - self._last_scheduled_at >= minimum_interval
            )
            if not due:
                return
            self._pending = (
                self._last_offset,
                self._submitted_count,
            )
            self._last_scheduled_at = now
            self._condition.notify_all()

    def force_submit(self) -> None:
        with self._condition:
            if self._submitted_count <= 0:
                return
            self._pending = (
                self._last_offset,
                self._submitted_count,
            )
            self._condition.notify_all()

    def stop(self, timeout: float = 5.0) -> bool:
        with self._condition:
            thread = self._thread
            if thread is None:
                return True
            self._stopping = True
            self._condition.notify_all()
        thread.join(timeout=timeout)
        with self._condition:
            stopped = not thread.is_alive()
            if stopped:
                self._thread = None
            return stopped

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {
                "submitted_captions": self._submitted_count,
                "pending": self._pending is not None,
                "events_emitted": self.events_emitted,
                "error_count": self.error_count,
                "last_error": self.last_error,
                "running": (
                    self._thread is not None
                    and self._thread.is_alive()
                ),
            }

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stopping:
                    self._condition.wait()
                if self._pending is None and self._stopping:
                    return
                job = self._pending
                self._pending = None
            if job is not None:
                try:
                    self._analyze(job[0], job[1])
                except Exception as exc:
                    with self._condition:
                        self.error_count += 1
                        self.last_error = str(exc)
            with self._condition:
                if self._stopping and self._pending is None:
                    return

    def _analyze(
        self,
        latest_offset_sec: float,
        caption_count: int,
    ) -> None:
        archive = dict(self.config.get("archive", {}))
        window_start = max(
            0.0,
            latest_offset_sec - self.store.bucket_seconds,
        )
        captions = self.store.get_captions(
            self.session_id,
            start_offset_sec=window_start,
            end_offset_sec=latest_offset_sec,
            limit=None,
        )
        if not captions:
            return

        bucket_index = int(
            latest_offset_sec // self.store.bucket_seconds
        )
        summary = summarize_captions(
            captions,
            bucket_index=bucket_index,
            bucket_minutes=self.store.bucket_minutes,
            max_bullets=int(
                archive.get("summary_max_bullets", 5)
            ),
        )
        summary["window_start_sec"] = window_start
        summary["window_end_sec"] = latest_offset_sec
        summary["window_mode"] = "live"
        summary_signature = (
            summary.get("headline", ""),
            tuple(
                item.get("text", "")
                for item in summary.get("bullets", [])
            ),
        )
        if summary_signature != self._last_summary_signature:
            self._last_summary_signature = summary_signature
            self._emit(
                CaptionEvent(
                    event_type="summary_updated",
                    text=str(
                        summary.get("headline", "최근 방송 요약")
                    ),
                    confidence=1.0,
                    audio_offset_sec=latest_offset_sec,
                    engine="local_analysis",
                    metadata={
                        "session_id": self.session_id,
                        "analysis_version": 1,
                        "caption_count": caption_count,
                        "summary": summary,
                    },
                )
            )

        topics = detect_topic_transitions(
            captions,
            window_captions=int(
                archive.get("topic_window_captions", 6)
            ),
            similarity_threshold=float(
                archive.get(
                    "topic_similarity_threshold",
                    0.22,
                )
            ),
            cooldown_sec=float(
                archive.get("topic_cooldown_sec", 30.0)
            ),
        )
        changed = [
            item
            for item in topics
            if item.get("reason") != "session_start"
            and float(item.get("offset_sec", 0.0))
            > self._last_topic_offset
        ]
        if changed:
            topic = changed[-1]
            topic_offset = float(topic.get("offset_sec", 0.0))
            self._last_topic_offset = topic_offset
            similarity = float(topic.get("similarity", 1.0))
            self._emit(
                CaptionEvent(
                    event_type="topic_changed",
                    text=str(topic.get("label", "주제 전환")),
                    confidence=max(0.0, min(1.0, 1.0 - similarity)),
                    audio_offset_sec=topic_offset,
                    engine="local_analysis",
                    metadata={
                        "session_id": self.session_id,
                        "analysis_version": 1,
                        "topic": topic,
                    },
                )
            )

    def _emit(self, event: CaptionEvent) -> None:
        self.callback(event)
        with self._condition:
            self.events_emitted += 1