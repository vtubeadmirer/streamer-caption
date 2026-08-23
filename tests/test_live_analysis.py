from __future__ import annotations

import copy
import tempfile
import threading
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config, validate_config
from core.transcript_archive import TranscriptArchive
from core.ws_server import CaptionWebSocketServer
from features.live_analysis import LiveAnalysisWorker


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        config = copy.deepcopy(load_config())
        config["archive"]["db_path"] = str(
            Path(directory) / "live-analysis.sqlite3"
        )
        config["archive"]["live_analysis_enabled"] = True
        config["archive"]["live_analysis_every_captions"] = 1
        config["archive"]["live_analysis_min_interval_sec"] = 0
        config["archive"]["summary_max_bullets"] = 3
        config["archive"]["topic_window_captions"] = 2
        config["archive"]["topic_similarity_threshold"] = 1.0
        config["archive"]["topic_cooldown_sec"] = 0
        validate_config(config)

        archive = TranscriptArchive.from_config(config)
        session_id = "live-analysis-session"
        archive.create_session(
            session_id,
            source="microphone",
            engine="test",
        )
        rows = [
            ("dragon raid boss attack", 1.0),
            ("dragon raid party damage", 2.0),
            ("sandwich recipe bread cheese", 30.0),
            ("sandwich cooking kitchen sauce", 31.0),
        ]
        for text, offset in rows:
            archive.add_caption(
                session_id,
                text=text,
                offset_sec=offset,
                confidence=0.95,
                engine="test",
            )

        events = []
        ready = threading.Event()

        def receive(event):
            events.append(event)
            if {
                item.event_type for item in events
            } >= {"summary_updated", "topic_changed"}:
                ready.set()

        worker = LiveAnalysisWorker(
            config,
            session_id,
            receive,
        )
        worker.start()
        worker.submit(31.0)
        assert ready.wait(timeout=3.0), [
            event.event_type for event in events
        ]
        worker.force_submit()
        assert worker.stop(timeout=3.0)
        snapshot = worker.snapshot()
        assert snapshot["error_count"] == 0, snapshot

        event_types = {event.event_type for event in events}
        assert "summary_updated" in event_types
        assert "topic_changed" in event_types
        summary_event = next(
            event
            for event in events
            if event.event_type == "summary_updated"
        )
        topic_event = next(
            event
            for event in events
            if event.event_type == "topic_changed"
        )
        assert summary_event.metadata["session_id"] == session_id
        assert summary_event.metadata["summary"]["window_mode"] == "live"
        assert topic_event.audio_offset_sec == 30.0
        assert topic_event.confidence > 0

        ws = CaptionWebSocketServer(config)
        ws.emit_event(topic_event)
        ws.emit_event(summary_event)
        cached = ws._cached_items()
        cached_types = [item["type"] for item in cached]
        assert cached_types == ["topic_changed", "summary_updated"]
        assert cached[0]["audio_offset_sec"] == 30.0
        assert cached[1]["metadata"]["session_id"] == session_id

    summary_html = (
        ROOT / "overlay" / "summary.html"
    ).read_text(encoding="utf-8")
    assert "connectAnalysisEvents" in summary_html
    assert 'item.type !== "summary_updated"' in summary_html
    assert 'item.type !== "topic_changed"' in summary_html

    print("live_analysis_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())