"""자막 엔진 choice through the admin HTTP API: shown disabled with a plain reason when the
optional runtime is missing, saved and flagged for restart when it is there."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHILD = "--engine-api-child"


def call(port: int, path: str, payload: dict | None = None, origin: str | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(f"http://127.0.0.1:{port}{path}", data=data, method="GET" if payload is None else "POST")
    request.add_header("Content-Type", "application/json")
    if origin:
        request.add_header("Origin", origin)
    try:
        with urlopen(request, timeout=10.0) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def child() -> int:
    from core.admin_server import AdminServer, AdminState
    from core.config import USER_DATA_ROOT, load_config, save_config
    from engines.simulstreaming import availability

    availability.query_gpu_memory = lambda max_age_sec=30.0: None  # no nvidia-smi dependency in tests
    config = load_config()
    config["simulstreaming"]["python_executable"] = str(USER_DATA_ROOT / "missing" / "python.exe")
    save_config(config)
    config["server"]["admin_port"] = 0
    state = AdminState()
    server = AdminServer(config, state)
    server.start_background()
    port = int(server._httpd.server_address[1])
    try:
        status, overview = call(port, "/api/engine")
        assert status == 200 and overview["selected"] == "baseline", overview
        accurate = next(option for option in overview["options"] if option["id"] == "simulstreaming")
        assert accurate["available"] is False and accurate["reason"] == "runtime_missing", accurate
        assert accurate["message"] == "추가 구성요소가 필요합니다 — 다음 업데이트에서 내려받기 지원 예정", accurate
        assert next(option for option in overview["options"] if option["id"] == "baseline")["available"]

        status, body = call(port, "/api/engine", {"engine": "simulstreaming"})
        assert status == 409 and body["reason"] == "runtime_missing" and "추가 구성요소" in body["error"], body
        assert load_config()["streaming"]["engine"] == "baseline", "a refused choice is not saved"

        # The optional runtime appears (phase 3 downloads it): a venv with torch and a model.
        runtime = USER_DATA_ROOT / "simulstreaming"
        python = runtime / ".venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"")
        torch_dir = runtime / ".venv" / "Lib" / "site-packages" / "torch"
        torch_dir.mkdir(parents=True)
        (torch_dir / "__init__.py").write_text("", encoding="utf-8")
        (runtime / "models").mkdir()
        (runtime / "models" / "large-v3.pt").write_bytes(b"x")
        status, _ = call(port, "/api/config", {"simulstreaming": {"python_executable": str(python)}})
        assert status == 200
        status, overview = call(port, "/api/engine")
        accurate = next(option for option in overview["options"] if option["id"] == "simulstreaming")
        assert accurate["available"] is True and accurate["reason"] == "", accurate
        models = {item["id"]: item["available"] for item in overview["simulstreaming"]["models"]}
        assert models == {"large-v3-turbo": False, "large-v3": True}, models

        # A page on another site must not switch the engine.
        status, body = call(port, "/api/engine", {"engine": "simulstreaming"}, origin="http://evil.example")
        assert status == 403, body
        status, body = call(port, "/api/engine", {"engine": "turbo-max"})
        assert status == 400, body
        status, body = call(port, "/api/engine", {"engine": "simulstreaming", "model": "medium"})
        assert status == 400, body

        state.engine = {"requested_engine": "baseline", "active_engine": "baseline", "state": "active"}
        status, overview = call(port, "/api/engine", {"engine": "simulstreaming", "model": "large-v3"})
        assert status == 200 and overview["selected"] == "simulstreaming", overview
        assert overview["restart_required"] is True, "the running app still uses the 기본 engine"
        saved = load_config()
        assert saved["streaming"]["engine"] == "simulstreaming" and saved["simulstreaming"]["model"] == "large-v3"

        state.engine = {"requested_engine": "simulstreaming", "active_engine": "baseline", "state": "fallback",
                        "reason": "stalled", "model_setting": "large-v3", "notice": "..."}
        status, live = call(port, "/api/status")
        assert live["engine"]["reason"] == "stalled" and live["engine"]["active_engine"] == "baseline", live["engine"]
        status, overview = call(port, "/api/engine")
        assert overview["restart_required"] is False and overview["running"]["state"] == "fallback", overview

        status, overview = call(port, "/api/engine", {"engine": "baseline"})
        assert status == 200 and load_config()["streaming"]["engine"] == "baseline"
        assert overview["restart_required"] is True
    finally:
        server.stop()
    return 0


def main() -> int:
    if CHILD in sys.argv:
        code = child()
        print("admin_engine_child_ok")
        return code
    html = (ROOT / "overlay" / "admin.html").read_text(encoding="utf-8")
    for needle in ('id="enginePanel"', '"/api/engine"', "정확 우선", "기본 (빠름)", "저장하고 다시 시작",
                   'data-engine="simulstreaming"', "renderEngine(status.engine)"):
        assert needle in html, needle
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        user_data = Path(directory) / "user-data"
        user_data.mkdir()
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), CHILD],
            env={**os.environ, "STREAMER_CAPTION_USER_DATA": str(user_data), "PYTHONIOENCODING": "utf-8"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    assert result.returncode == 0 and "admin_engine_child_ok" in result.stdout, result.stdout + result.stderr
    print("admin_engine_api_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
