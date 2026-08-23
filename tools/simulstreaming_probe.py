from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from engines.simulstreaming.client import SimulStreamingClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the isolated SimulStreaming experiment runtime.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    config = load_config()
    client = SimulStreamingClient(config)
    script = client.repo_dir / "simulstreaming_whisper_server.py"
    checks: list[dict[str, str]] = []
    checks.append({"name": "official_source", "status": "ok" if script.exists() else "fail", "detail": str(script)})
    model_path = Path(client.model_path) if client.model_path else None
    checks.append({
        "name": "model_path",
        "status": "ok" if model_path is not None and model_path.exists() else "warn",
        "detail": str(model_path) if model_path else "Not configured. Set simulstreaming.model_path before a live experiment.",
    })
    try:
        result = subprocess.run(
            [client.python_executable, str(script), "--help"],
            cwd=client.repo_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        checks.append({
            "name": "runtime_imports",
            "status": "ok" if result.returncode == 0 else "fail",
            "detail": "official server help completed" if result.returncode == 0 else (result.stderr or result.stdout)[-500:],
        })
    except Exception as exc:
        checks.append({"name": "runtime_imports", "status": "fail", "detail": str(exc)})
    if args.json:
        print(json.dumps(checks, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            print("[{status}] {name}: {detail}".format(**check))
    return 1 if any(check["status"] == "fail" for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())