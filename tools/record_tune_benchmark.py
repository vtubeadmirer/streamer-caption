from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT, load_config
from tools.transcribe_wav import resolve_wav_path


def run_command(args: list[str]) -> dict[str, Any]:
    started = time.perf_counter()
    result = subprocess.run(args, cwd=PROJECT_ROOT, capture_output=True, text=True)
    return {
        "args": args,
        "returncode": result.returncode,
        "elapsed_sec": time.perf_counter() - started,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def main() -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description="Record, tune VAD, transcribe, and benchmark v2_light.")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--out", default="samples/test_clip.wav")
    parser.add_argument("--skip-record", action="store_true")
    parser.add_argument("--apply-tuning", action="store_true")
    parser.add_argument("--skip-transcribe", action="store_true")
    parser.add_argument("--skip-benchmark", action="store_true")
    args = parser.parse_args()

    report_dir = PROJECT_ROOT / "reports"
    report_dir.mkdir(exist_ok=True)
    report_path = report_dir / f"record_tune_benchmark_{time.strftime('%Y-%m-%d_%H%M%S')}.json"

    steps: list[dict[str, Any]] = []
    if not args.skip_record:
        steps.append(
            run_command(
                [
                    sys.executable,
                    "tools\\record_wav.py",
                    "--seconds",
                    str(args.seconds),
                    "--out",
                    args.out,
                ]
            )
        )
        if steps[-1]["returncode"] != 0:
            print(steps[-1]["stdout"] + steps[-1]["stderr"])
            return int(steps[-1]["returncode"])

    try:
        resolve_wav_path(args.out)
    except Exception as exc:
        print(exc)
        return 1

    tune_args = [sys.executable, "tools\\tune_voice_wav.py", args.out]
    if args.apply_tuning:
        tune_args.append("--apply")
    steps.append(run_command(tune_args))
    if steps[-1]["returncode"] != 0:
        print(steps[-1]["stdout"] + steps[-1]["stderr"])
        return int(steps[-1]["returncode"])

    if not args.skip_transcribe:
        steps.append(
            run_command(
                [
                    sys.executable,
                    "tools\\transcribe_wav.py",
                    args.out,
                    "--model",
                    config["whisper"]["model"],
                    "--json",
                ]
            )
        )
        if steps[-1]["returncode"] != 0:
            print(steps[-1]["stdout"] + steps[-1]["stderr"])
            return int(steps[-1]["returncode"])

    if not args.skip_benchmark:
        steps.append(run_command([sys.executable, "tools\\benchmark_streaming_wav.py", args.out]))
        if steps[-1]["returncode"] != 0:
            print(steps[-1]["stdout"] + steps[-1]["stderr"])
            return int(steps[-1]["returncode"])

    report = {
        "wav_path": str((PROJECT_ROOT / args.out).resolve()),
        "apply_tuning": args.apply_tuning,
        "config_snapshot": {
            "profile": config.get("app", {}).get("profile"),
            "vad": config.get("vad", {}),
            "audio": config.get("audio", {}),
            "whisper": config.get("whisper", {}),
            "streaming": config.get("streaming", {}),
        },
        "steps": steps,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"workflow_report: {report_path}")
    for step in steps:
        label = " ".join(step["args"][1:3])
        print(f"{label}: rc={step['returncode']} elapsed={step['elapsed_sec']:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
