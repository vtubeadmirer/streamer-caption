from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.events import CaptionEvent
from core.log_writer import JsonlLogWriter
from core.transcript_archive import TranscriptArchive


class SessionArchiveRecorder:
    """Best-effort bridge from live caption events to the transcript archive."""

    def __init__(
        self,
        config: dict[str, Any],
        writer: JsonlLogWriter,
        *,
        source: str,
        engine: str,
        mode: str,
        analysis_sink: Callable[[CaptionEvent], Any] | None = None,
    ) -> None:
        archive_config = config.get("archive", {})
        self.enabled = bool(archive_config.get("enabled", True))
        self.auto_report = bool(archive_config.get("auto_report_on_close", True))
        self.config = config
        self.writer = writer
        self.session_id = writer.session_dir.name
        self.store: TranscriptArchive | None = None
        self.last_error = ""
        self.error_count = 0
        self.recovered_sessions: list[dict[str, Any]] = []
        self.analysis_sink = analysis_sink
        self.live_analysis: Any | None = None
        self.recap_worker: Any | None = None
        self.last_confirmed_offset = 0.0
        if not self.enabled:
            return
        try:
            self.store = TranscriptArchive.from_config(config)
            try:
                from core.archive_maintenance import recover_unclean_sessions

                self.recovered_sessions = recover_unclean_sessions(
                    self.store,
                    config,
                    current_session_id=self.session_id,
                )
                if self.recovered_sessions:
                    print(
                        "recovered unclean transcript sessions: "
                        f"{len(self.recovered_sessions)}"
                    )
            except Exception as exc:
                self._record_error(exc)
            self.store.create_session(
                self.session_id,
                source=source,
                engine=engine,
                metadata={
                    "mode": mode,
                    "profile": config.get("app", {}).get("profile", "custom"),
                    "log_path": str(writer.path),
                    "process_id": os.getpid(),
                },
            )
            if bool(
                archive_config.get("live_analysis_enabled", True)
            ):
                from features.live_analysis import LiveAnalysisWorker

                self.live_analysis = LiveAnalysisWorker(
                    config,
                    self.session_id,
                    self._record_analysis_event,
                )
                self.live_analysis.start()
            if bool(
                dict(archive_config.get("recap", {}) or {}).get("enabled", True)
            ):
                from features.recap import RecapWorker

                self.recap_worker = RecapWorker(
                    config,
                    self.session_id,
                    self.store,
                    self._record_analysis_event,
                )
                self.recap_worker.start()
        except Exception as exc:
            self._record_error(exc)
            self.enabled = False

    def record_event(
        self,
        event: CaptionEvent,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if (
            not self.enabled
            or self.store is None
            or event.event_type != "caption_confirmed"
        ):
            return
        metadata = dict(event.metadata)
        if extra:
            metadata["log_extra"] = extra
        try:
            self.store.add_caption(
                self.session_id,
                text=event.text,
                offset_sec=event.audio_offset_sec,
                confidence=event.confidence,
                engine=event.engine,
                metadata=metadata,
            )
            self.last_confirmed_offset = max(
                self.last_confirmed_offset,
                float(event.audio_offset_sec),
            )
            if self.live_analysis is not None:
                self.live_analysis.submit(event.audio_offset_sec)
            if self.recap_worker is not None:
                self.recap_worker.submit(event.audio_offset_sec)
        except Exception as exc:
            self._record_error(exc)

    def close(self, metadata: dict[str, Any] | None = None, *, relaunch: bool = False) -> dict[str, str]:
        """End the session. With ``relaunch=True`` (settings change / update restart) the stream is
        still live: no recap is forced for the unfinished bucket, no report is written and the
        overlays get ``session_paused`` instead of ``broadcast_ended``."""
        if not self.enabled or self.store is None:
            return {}
        close_metadata = dict(metadata or {})
        close_metadata["relaunch"] = bool(relaunch)
        report_paths: dict[str, str] = {}
        lifecycle = "session_paused" if relaunch else "broadcast_ended"
        lifecycle_text = "session paused for restart" if relaunch else "broadcast ended"
        try:
            if self.live_analysis is not None:
                self.live_analysis.force_submit()
                if not self.live_analysis.stop(timeout=5.0):
                    self._record_error(
                        RuntimeError(
                            "live analysis worker did not stop within 5 seconds"
                        )
                    )
                close_metadata["live_analysis"] = (
                    self.live_analysis.snapshot()
                )
            if self.recap_worker is not None:
                if not relaunch:
                    self.recap_worker.finalize(self.last_confirmed_offset)
                if not self.recap_worker.stop(timeout=5.0 if relaunch else 25.0):
                    self._record_error(
                        RuntimeError("recap worker did not stop within 25 seconds")
                    )
                close_metadata["recap"] = self.recap_worker.snapshot()
            self.store.close_session(
                self.session_id,
                metadata=close_metadata,
            )
            if self.auto_report and not relaunch:
                from features.broadcast_report import write_session_report

                report_paths = write_session_report(
                    self.store,
                    self.session_id,
                    self.writer.session_dir,
                    self.config,
                )
            self._record_lifecycle_event(
                CaptionEvent(
                    event_type=lifecycle,
                    text=lifecycle_text,
                    confidence=1.0,
                    audio_offset_sec=self.last_confirmed_offset,
                    engine="session_archive",
                    metadata={
                        "session_id": self.session_id,
                        "report_ready": bool(report_paths),
                        "report_paths": report_paths,
                        "archive_error_count": self.error_count,
                        "close_metadata": close_metadata,
                    },
                ),
                wait_for_delivery=True,
            )
            return report_paths
        except Exception as exc:
            self._record_error(exc)
            self._record_lifecycle_event(
                CaptionEvent(
                    event_type=lifecycle,
                    text=lifecycle_text,
                    confidence=0.0,
                    audio_offset_sec=self.last_confirmed_offset,
                    engine="session_archive",
                    metadata={
                        "session_id": self.session_id,
                        "report_ready": False,
                        "report_paths": {},
                        "archive_error_count": self.error_count,
                        "error": str(exc),
                        "close_metadata": close_metadata,
                    },
                ),
                wait_for_delivery=True,
            )
            return {}

    def _record_lifecycle_event(
        self,
        event: CaptionEvent,
        *,
        wait_for_delivery: bool,
    ) -> None:
        try:
            self.writer.append_event(
                event,
                extra={"source": "session_lifecycle"},
            )
            if self.analysis_sink is None:
                return
            delivery = self.analysis_sink(event)
            if (
                wait_for_delivery
                and delivery is not None
                and hasattr(delivery, "result")
            ):
                delivery.result(timeout=1.0)
        except Exception as exc:
            print(f"warning: lifecycle event delivery failed: {exc}")


    def _record_analysis_event(self, event: CaptionEvent) -> None:
        try:
            self.writer.append_event(
                event,
                extra={"source": "live_analysis"},
            )
            if self.analysis_sink is not None:
                self.analysis_sink(event)
        except Exception as exc:
            self._record_error(exc)

    def _record_error(self, exc: Exception) -> None:
        self.error_count += 1
        self.last_error = str(exc)
        if self.error_count == 1:
            print(f"warning: transcript archive unavailable: {exc}")

