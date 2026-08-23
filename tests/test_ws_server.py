from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.events import CaptionEvent
from core.ws_server import CaptionWebSocketServer


async def read_sync() -> dict:
    import websockets

    async with websockets.connect("ws://127.0.0.1:9876") as websocket:
        raw = await asyncio.wait_for(websocket.recv(), timeout=2.0)
        return json.loads(raw)


def main() -> int:
    config = copy.deepcopy(load_config())
    config["server"]["port"] = 9876
    server = CaptionWebSocketServer(config)
    server.start_background()
    time.sleep(0.4)
    server.emit_event(
        CaptionEvent(
            event_type="caption_confirmed",
            text="테스트 자막",
            confidence=0.99,
            audio_offset_sec=1.2,
            engine="test",
        )
    )
    message = asyncio.run(read_sync())
    assert message["type"] == "sync", message
    assert message["items"], message
    assert message["items"][0]["text"] == "테스트 자막", message
    print("ws_server_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

