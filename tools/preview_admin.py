from __future__ import annotations

import argparse
import copy
import socket
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.admin_server import AdminServer, AdminState
from core.config import load_config


def find_available_port(host: str, preferred: int) -> int:
    for port in (preferred, 8876, 8976):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError("No preview port is available (8766, 8876, 8976).")


def main() -> int:
    parser = argparse.ArgumentParser(description="GPU-free admin UI preview")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--page",
        choices=("admin", "history", "validate"),
        default="admin",
    )
    args = parser.parse_args()

    config = copy.deepcopy(load_config())
    host = "127.0.0.1"
    port = find_available_port(host, args.port)
    config["server"]["host"] = host
    config["server"]["admin_port"] = port

    state = AdminState()
    server = AdminServer(config, state)
    server.start_background()
    url = f"http://{host}:{port}/{args.page}?preview=1"
    print("Admin UI preview only. GPU, Whisper, and microphone capture are not running.")
    print(f"preview_url: {url}")
    print(f"history_url: http://{host}:{port}/history")
    print(f"validation_url: http://{host}:{port}/validate")
    print("Press Ctrl+C to stop.")

    if not args.no_browser:
        webbrowser.open(url)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print()
        print("Preview stopped.")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
