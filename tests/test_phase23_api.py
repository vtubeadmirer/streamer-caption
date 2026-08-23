from __future__ import annotations

import copy
import json
import socket
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.admin_server import AdminServer, AdminState
from core.config import load_config
from core.transcript_archive import TranscriptArchive


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def get_json(url: str) -> dict:
    with urlopen(url, timeout=3.0) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, payload: dict) -> dict:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=3.0) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "api.sqlite3"
        archive = TranscriptArchive(db_path)
        archive.create_session("api-session", source="microphone", engine="test")
        archive.add_caption(
            "api-session",
            text="샌드위치 자막 검색 테스트입니다.",
            offset_sec=12,
            confidence=0.9,
            engine="test",
        )
        archive.record_slang_change(
            action="added", source="샹도웨쭈", target="샌드위치"
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
                history_headers = dict(response.headers.items())
            with urlopen(base + "/summary", timeout=3.0) as response:
                summary_html = response.read().decode("utf-8")
            sessions = get_json(base + "/api/archive/sessions")
            summary = get_json(
                base + "/api/archive/summary?session_id=api-session&bucket=0"
            )
            rolling_summary = get_json(
                base
                + "/api/archive/summary?session_id=api-session&window=latest"
            )
            search = get_json(
                base + "/api/archive/search?session_id=api-session&q="
                + "%EC%83%8C%EB%93%9C%EC%9C%84%EC%B9%98"
            )
            timeline = get_json(
                base + "/api/archive/timeline?session_id=api-session"
            )
            slang = get_json(base + "/api/archive/slang-history")
            updated = post_json(
                base + "/api/archive/session-meta",
                {
                    "session_id": "api-session",
                    "title": "API 테스트",
                    "vod_url": "https://example.com/vod",
                    "vod_start_offset_sec": -5,
                },
            )
        finally:
            server.stop()

        assert "Streamer Caption Archive" in history_html
        assert "지난 30분 요약" in summary_html
        assert "Access-Control-Allow-Origin" not in history_headers
        assert history_headers["X-Content-Type-Options"] == "nosniff"
        assert sessions["sessions"][0]["id"] == "api-session"
        assert summary["caption_count"] == 1
        assert rolling_summary["window_mode"] == "latest"
        assert rolling_summary["window_start_sec"] == 0
        assert search["count"] == 1
        assert timeline["stats"]["caption_count"] == 1
        assert slang["history"][0]["source"] == "샹도웨쭈"
        assert updated["session"]["title"] == "API 테스트"
        assert updated["session"]["vod_start_offset_sec"] == -5
    print("phase23_api_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

