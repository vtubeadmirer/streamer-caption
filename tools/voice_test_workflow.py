from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT, load_config
from tools.analyze_wav import analyze_wav
from tools.transcribe_wav import resolve_wav_path


def run_command(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(args, cwd=PROJECT_ROOT, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def main() -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description="Record, analyze, transcribe, and save a voice test report.")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--out", default="samples/test_clip.wav")
    parser.add_argument("--model", default=config["whisper"]["model"])
    parser.add_argument("--skip-record", action="store_true")
    args = parser.parse_args()

    wav_path = PROJECT_ROOT / args.out
    report_dir = PROJECT_ROOT / "reports"
    report_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    report_path = report_dir / f"voice_test_{stamp}.json"

    record_output = ""
    if not args.skip_record:
        code, record_output = run_command(
            [
                sys.executable,
                "tools\\record_wav.py",
                "--seconds",
                str(args.seconds),
                "--out",
                args.out,
            ]
        )
        if code != 0:
            print(record_output)
            return code

    try:
        analysis = analyze_wav(resolve_wav_path(args.out), float(config["audio"]["vad_energy_threshold"]))
    except Exception as exc:
        print(exc)
        return 1
    code, transcription_output = run_command(
        [
            sys.executable,
            "tools\\transcribe_wav.py",
            args.out,
            "--model",
            args.model,
            "--json",
        ]
    )
    if code != 0:
        print(transcription_output)
        return code

    try:
        transcription = json.loads(transcription_output)
    except json.JSONDecodeError:
        transcription = [{"raw_output": transcription_output}]

    report = {
        "wav_path": str(wav_path),
        "model": args.model,
        "record_output": record_output,
        "analysis": analysis,
        "transcription": transcription,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report_saved: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
