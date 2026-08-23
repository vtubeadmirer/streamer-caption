from __future__ import annotations

import copy
import json
import time
from pathlib import Path
import sys
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.admin_server import AdminServer, AdminState
from core.config import PROJECT_ROOT, load_config
from engines.baseline.engine import BaselineEngine
from features.defense import DefenseFilter
from features.slang import SlangDictionary


def read_json(url: str) -> dict:
    with urlopen(url, timeout=2.0) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, payload: dict) -> dict:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=2.0) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    config = copy.deepcopy(load_config())
    config["server"]["admin_port"] = 9878
    slang_path = PROJECT_ROOT / config["slang"]["dict_path"]
    whitelist_path = PROJECT_ROOT / config["defense"]["whitelist_path"]
    old_slang = slang_path.read_text(encoding="utf-8") if slang_path.exists() else None
    old_whitelist = whitelist_path.read_text(encoding="utf-8") if whitelist_path.exists() else None
    try:
        server = AdminServer(config, AdminState())
        server.start_background()
        time.sleep(0.3)
        saved = post_json(
            "http://127.0.0.1:9878/api/slang",
            {"slang": {"샌드위치": "샹도웨쭈"}, "whitelist": ["샹도웨쭈"]},
        )
        loaded = read_json("http://127.0.0.1:9878/api/slang")
        assert saved == loaded
        assert loaded["slang"]["샌드위치"] == "샹도웨쭈"
        assert "샹도웨쭈" in loaded["whitelist"]

        slang = SlangDictionary(config)
        assert slang.apply("샌드위치 주세요") == "샹도웨쭈 주세요"
        assert DefenseFilter(config).apply("샹도웨쭈 주세요", 0.1).visible
        assert "샌드위치=샹도웨쭈" not in (BaselineEngine(config).initial_prompt or "")
        config["slang"]["prompt_mode"] = "terms"
        assert "샌드위치=샹도웨쭈" in (BaselineEngine(config).initial_prompt or "")
        print("slang_admin_ok")
        return 0
    finally:
        if old_slang is None:
            slang_path.unlink(missing_ok=True)
        else:
            slang_path.write_text(old_slang, encoding="utf-8")
        if old_whitelist is None:
            whitelist_path.unlink(missing_ok=True)
        else:
            whitelist_path.write_text(old_whitelist, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
