from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from engines.baseline.engine import BaselineEngine
from tools.health_check import run_checks


def run_nvidia_smi() -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    rows = []
    for line in result.stdout.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 5:
            rows.append(
                {
                    "name": parts[0],
                    "driver_version": parts[1],
                    "memory_used_mb": parts[2],
                    "memory_total_mb": parts[3],
                    "temperature_c": parts[4],
                }
            )
    return {"ok": True, "gpus": rows}


def model_load_check(config: dict[str, Any]) -> dict[str, Any]:
    event = BaselineEngine(config).warm_up()
    return {
        "event_type": event.event_type,
        "text": event.text,
        "metadata": event.metadata,
    }


def collect(include_model_load: bool) -> dict[str, Any]:
    config = load_config()
    report: dict[str, Any] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "python": {
            "version": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "config": {
            "app": config.get("app", {}),
            "audio": config.get("audio", {}),
            "whisper": config.get("whisper", {}),
            "streaming": config.get("streaming", {}),
            "vad": config.get("vad", {}),
        },
        "health": [result.to_dict() for result in run_checks()],
        "nvidia_smi": run_nvidia_smi(),
    }
    if include_model_load:
        report["model_load"] = model_load_check(config)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect streamer-caption-v2 diagnostics into a JSON report.")
    parser.add_argument("--model-load", action="store_true", help="also load the configured Whisper model")
    parser.add_argument("--out", default=None, help="output JSON path")
    args = parser.parse_args()

    report = collect(args.model_load)
    out = Path(args.out) if args.out else ROOT / "reports" / f"diagnostics_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"diagnostics written: {out}")
    failures = [item for item in report["health"] if item["status"] == "FAIL"]
    if failures:
        print("health failures: " + ", ".join(item["name"] for item in failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
