from __future__ import annotations

import copy
import json
import time
from pathlib import Path
import sys
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.admin_server import AdminServer, AdminState
from core.config import load_config


def read_json(url: str) -> dict:
    with urlopen(url, timeout=2.0) as response:
        return json.loads(response.read().decode("utf-8"))


def read_text(url: str) -> str:
    with urlopen(url, timeout=2.0) as response:
        return response.read().decode("utf-8")


def main() -> int:
    config = copy.deepcopy(load_config())
    config["server"]["admin_port"] = 9877
    state = AdminState()
    state.last_caption = "테스트"
    server = AdminServer(config, state)
    server.start_background()
    time.sleep(0.3)

    cfg = read_json("http://127.0.0.1:9877/api/config")
    status = read_json("http://127.0.0.1:9877/api/status")
    devices = read_json("http://127.0.0.1:9877/api/devices")
    caption_html = read_text("http://127.0.0.1:9877/caption")
    admin_html = read_text("http://127.0.0.1:9877/admin")
    theme_css = read_text("http://127.0.0.1:9877/admin-theme.css")
    assert cfg["server"]["admin_port"] == 8766, cfg["server"]
    assert status["last_caption"] == "테스트", status
    assert "devices" in devices
    assert "system_devices" in devices
    assert "errors" in devices
    assert "caption config unavailable" in caption_html
    assert "confirmedHistory" in caption_html
    assert "justChattingMode" in caption_html
    assert "Math.min(\n            500," in caption_html.replace("\r\n", "\n")
    assert "themeLight" in admin_html and "themeDark" in admin_html
    assert 'id="openCaptionPreview"' in admin_html
    assert 'id="koSize" type="number" min="6" max="200"' in admin_html
    assert 'id="maxLines" type="number" min="1" max="500"' in admin_html
    assert '"/caption?preview=1"' in admin_html
    assert 'body[data-theme="dark"]' in theme_css
    print("admin_server_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
