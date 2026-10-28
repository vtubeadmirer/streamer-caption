"""Is the 정확 우선 (SimulStreaming) engine ready on this PC?

Checks the files (source, runtime, model) like the admin page does, then asks the runtime
itself: engines/simulstreaming/server.py --self-check imports torch, checks CUDA, the model
file and the Silero VAC, and exits without loading the model.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config  # noqa: E402
from engines.simulstreaming.availability import (  # noqa: E402
    choose_model,
    engine_status,
    query_gpu_memory,
    resolve_runtime,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the optional SimulStreaming runtime.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    config = load_config()
    runtime = resolve_runtime(config)
    status = engine_status(config)
    checks: list[dict[str, str]] = [
        {"name": "engine_files", "status": "ok" if status["available"] else "fail",
         "detail": status["message"] or "source, runtime and model files found"},
    ]
    gpu = query_gpu_memory()
    checks.append({
        "name": "gpu_memory",
        "status": "ok" if gpu else "warn",
        "detail": f"total {gpu['total_mib']} MiB, free {gpu['free_mib']} MiB" if gpu else "nvidia-smi not available",
    })
    model, model_path, problem = choose_model(config, runtime, gpu["free_mib"] if gpu else None)
    checks.append({"name": "model_choice", "status": "fail" if problem else "ok",
                   "detail": problem or f"{model} ({config['simulstreaming'].get('model', 'auto')})"})
    if status["available"] and model_path is not None:
        command = [str(runtime["python"]), str(runtime["server_script"]), "--repo", str(runtime["repo_dir"]),
                   "--self-check", "--model_path", str(model_path)]
        env = None
        if runtime["torch_home"] is not None:
            import os

            env = {**os.environ, "TORCH_HOME": str(runtime["torch_home"])}
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=300, check=False, env=env)
            lines = [line for line in (result.stdout or "").splitlines() if line.startswith("{")]
            report = json.loads(lines[-1]) if lines else {}
            ok = result.returncode == 0 and report.get("cuda") and report.get("vac")
            checks.append({
                "name": "runtime_self_check",
                "status": "ok" if ok else "fail",
                "detail": json.dumps(report, ensure_ascii=False) if report else (result.stderr or "")[-500:],
            })
        except Exception as exc:
            checks.append({"name": "runtime_self_check", "status": "fail", "detail": f"{type(exc).__name__}: {exc}"})
    if args.json:
        print(json.dumps(checks, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            print("[{status}] {name}: {detail}".format(**check))
    return 1 if any(check["status"] == "fail" for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
