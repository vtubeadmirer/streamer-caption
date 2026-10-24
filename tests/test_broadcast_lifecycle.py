from __future__ import annotations

import concurrent.futures
import copy
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config, validate_config
from core.events import CaptionEvent
from core.log_writer import JsonlLogWriter
from core.session_archive import SessionArchiveRecorder
from core.ws_server import CaptionWebSocketServer


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        config = copy.deepcopy(load_config())
        config["archive"]["db_path"] = str(temp / "lifecycle.sqlite3")
        config["archive"]["live_analysis_enabled"] = False
        config["archive"]["auto_report_on_close"] = True
        validate_config(config)

        delivered = []
        delivery_waited = []

        def receive(event: CaptionEvent):
            delivered.append(event)
            future: concurrent.futures.Future[None] = (
                concurrent.futures.Future()
            )
            future.set_result(None)
            original_result = future.result

            def tracked_result(timeout=None):
                delivery_waited.append(timeout)
                return original_result(timeout=timeout)

            future.result = tracked_result
            return future

        writer = JsonlLogWriter(str(temp / "logs"))
        recorder = SessionArchiveRecorder(
            config,
            writer,
            source="microphone",
            engine="test",
            mode="v2_light",
            analysis_sink=receive,
        )
        recorder.record_event(
            CaptionEvent(
                event_type="caption_confirmed",
                text="lifecycle test caption",
                confidence=0.94,
                audio_offset_sec=12.5,
                engine="test",
            )
        )

        paths = recorder.close({"stop_reason": "test_complete"})
        assert set(paths) == {"json", "markdown"}, paths
        assert all(Path(path).exists() for path in paths.values())
        assert delivery_waited == [1.0], delivery_waited

        assert len(delivered) == 1
        ended = delivered[0]
        assert ended.event_type == "broadcast_ended"
        assert ended.audio_offset_sec == 12.5
        assert ended.metadata["report_ready"] is True
        assert ended.metadata["report_paths"] == paths
        assert ended.metadata["close_metadata"]["stop_reason"] == (
            "test_complete"
        )

        ws = CaptionWebSocketServer(config)
        ws.emit_event(ended)
        cached = ws._cached_items()
        assert [item["type"] for item in cached] == ["broadcast_ended"]
        assert cached[0]["metadata"]["report_ready"] is True

        records = [
            json.loads(line)
            for line in writer.path.read_text(encoding="utf-8").splitlines()
        ]
        lifecycle = [
            item
            for item in records
            if item["event_type"] == "broadcast_ended"
        ]
        assert len(lifecycle) == 1
        assert lifecycle[0]["extra"]["source"] == "session_lifecycle"
        assert lifecycle[0]["metadata"]["report_ready"] is True

        session = recorder.store.get_session(recorder.session_id)
        assert session["ended_at"]
        assert session["caption_count"] == 1

        # A relaunch (settings change / update restart) is not the end of the broadcast:
        # no report, no forced recap, and overlays get session_paused instead of broadcast_ended.
        delivered.clear()
        paused_writer = JsonlLogWriter(str(temp / "logs"))
        paused = SessionArchiveRecorder(config, paused_writer, source="microphone", engine="test", mode="v2_light", analysis_sink=receive)
        paused.record_event(CaptionEvent(event_type="caption_confirmed", text="before restart", confidence=0.9, audio_offset_sec=3.0, engine="test"))
        assert paused.close({"pipeline_exit_code": 76}, relaunch=True) == {}
        assert [event.event_type for event in delivered] == ["session_paused"], [e.event_type for e in delivered]
        assert delivered[0].metadata["close_metadata"]["relaunch"] is True
        assert not list(paused_writer.session_dir.glob("broadcast_report.*")), "no report on relaunch"
        assert paused.store.get_session(paused.session_id)["ended_at"], "the archive session is still closed cleanly"

    summary_html = (ROOT / "overlay" / "summary.html").read_text(
        encoding="utf-8"
    )
    assert 'item.type === "broadcast_ended"' in summary_html
    assert 'id="sessionState"' in summary_html

    print("broadcast_lifecycle_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
