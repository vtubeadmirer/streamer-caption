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


def main() -> int:
    config = copy.deepcopy(load_config())
    config["server"]["admin_port"] = 9878
    server = AdminServer(config, AdminState())
    server.start_background()
    time.sleep(0.3)

    profiles = read_json("http://127.0.0.1:9878/api/profiles")
    assert "rtx_4060ti_16gb" in profiles["profiles"], profiles
    assert "rtx_5070ti_quality" in profiles["profiles"], profiles
    print("admin_profiles_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
