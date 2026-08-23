from __future__ import annotations

import copy
import json
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from engines.simulstreaming.client import SimulStreamingClient


def main() -> int:
    ready = threading.Event()
    release = threading.Event()
    port_holder: list[int] = []

    def fake_server() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port_holder.append(server.getsockname()[1])
            ready.set()
            connection, _ = server.accept()
            with connection:
                assert connection.recv(4096)
                connection.sendall(json.dumps({"start": 0.0, "end": 0.8, "text": "partial", "is_final": False}).encode() + b"\n")
                connection.sendall(json.dumps({"start": 0.0, "end": 1.2, "text": "final", "is_final": True}).encode() + b"\n")
                release.wait(timeout=2.0)

    server_thread = threading.Thread(target=fake_server, daemon=True)
    server_thread.start()
    assert ready.wait(timeout=2.0)

    config = copy.deepcopy(load_config())
    config["simulstreaming"].update({
        "start_server": False,
        "host": "127.0.0.1",
        "port": port_holder[0],
        "connect_timeout_sec": 2.0,
    })
    command_config = copy.deepcopy(config)
    command_config["simulstreaming"].update({
        "start_server": True,
        "model_path": str(ROOT / "missing-large-v3.pt"),
        "allow_auto_download": True,
    })
    command = SimulStreamingClient(command_config).server_command()
    assert "--audio_max_len" in command
    assert "--frame_threshold" in command
    assert "--audio-max-len" not in command
    assert "--frame-threshold" not in command

    client = SimulStreamingClient(config)
    client.start()
    try:
        client.send_audio([0.0] * 1600)
        deadline = time.monotonic() + 2.0
        messages = []
        while time.monotonic() < deadline and len(messages) < 2:
            messages.extend(client.get_messages())
            time.sleep(0.02)
        assert [message["text"] for message in messages] == ["partial", "final"], messages
        assert messages[0]["is_final"] is False
        assert messages[1]["is_final"] is True
        assert client.snapshot()["connected"]
    finally:
        client.close()
        release.set()
        server_thread.join(timeout=2.0)
    print("simulstreaming_client_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())