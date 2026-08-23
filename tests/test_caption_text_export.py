from __future__ import annotations

import copy
import socket
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.admin_server import AdminServer, AdminState
from core.config import load_config
from core.events import CaptionEvent
from core.log_writer import JsonlLogWriter
from core.transcript_archive import TranscriptArchive


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        writer = JsonlLogWriter(str(temp / "logs"))
        writer.append_event(
            CaptionEvent(
                event_type="caption_pending",
                text="저장하면 안 되는 중간 자막",
                audio_offset_sec=3.0,
            )
        )
        first = CaptionEvent(
            event_type="caption_confirmed",
            text="첫 번째 확정 자막",
            audio_offset_sec=5.2,
        )
        writer.append_event(first)
        writer.append_event(first)
        writer.append_event(
            CaptionEvent(
                event_type="caption_confirmed",
                text="같은 시각의 두 번째 줄",
                audio_offset_sec=5.2,
            )
        )
        expected = (
            "[00:00:05] 첫 번째 확정 자막\n"
            "[00:00:05] 같은 시각의 두 번째 줄\n"
        )
        assert writer.caption_path.read_text(encoding="utf-8") == expected
        assert writer.latest_caption_path.read_text(encoding="utf-8") == expected

        db_path = temp / "archive.sqlite3"
        archive = TranscriptArchive(db_path)
        archive.create_session("text-api", source="microphone", engine="test")
        archive.add_caption(
            "text-api",
            text="과거 방송 자막도 저장됩니다.",
            offset_sec=3661.9,
            confidence=0.95,
            engine="test",
        )
        config = copy.deepcopy(load_config())
        config["archive"]["db_path"] = str(db_path)
        config["server"]["admin_port"] = available_port()
        server = AdminServer(config, AdminState())
        server.start_background()
        base = f"http://127.0.0.1:{config['server']['admin_port']}"
        time.sleep(0.15)
        try:
            with urlopen(base + "/history", timeout=3.0) as response:
                history_html = response.read().decode("utf-8")
            with urlopen(
                base + "/api/archive/export-text?session_id=text-api",
                timeout=3.0,
            ) as response:
                body = response.read().decode("utf-8-sig")
                disposition = response.headers["Content-Disposition"]
                content_type = response.headers["Content-Type"]
        finally:
            server.stop()

        assert 'id="downloadTranscript"' in history_html
        assert body == "[01:01:01] 과거 방송 자막도 저장됩니다.\n"
        assert disposition == 'attachment; filename="text-api_captions.txt"'
        assert content_type == "text/plain; charset=utf-8"

    print("caption_text_export_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())