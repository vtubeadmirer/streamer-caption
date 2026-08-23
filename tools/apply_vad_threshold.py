from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT, load_config, save_config
from tools.analyze_wav import analyze_wav
from tools.transcribe_wav import resolve_wav_path


def main() -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description="Apply a VAD threshold from a WAV analysis.")
    parser.add_argument("path")
    parser.add_argument("--threshold", type=float)
    parser.add_argument("--from-analysis", action="store_true")
    parser.add_argument("--min-threshold", type=float, default=0.003)
    parser.add_argument("--max-threshold", type=float, default=0.08)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.from_analysis:
        try:
            wav_path = resolve_wav_path(args.path)
            result = analyze_wav(wav_path, float(config["audio"]["vad_energy_threshold"]))
        except Exception as exc:
            print(exc)
            return 1
        if "error" in result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        threshold = float(result["suggested_threshold_quiet_room"])
    elif args.threshold is not None:
        threshold = args.threshold
        result = {}
    else:
        raise SystemExit("--threshold or --from-analysis is required")

    old = float(config["audio"]["vad_energy_threshold"])
    threshold = max(args.min_threshold, min(args.max_threshold, threshold))
    config["audio"]["vad_energy_threshold"] = threshold
    payload = {
        "old_threshold": old,
        "new_threshold": threshold,
        "current_input_gain": config["audio"].get("input_gain", 1.0),
        "dry_run": args.dry_run,
        "analysis": result,
    }
    if not args.dry_run:
        save_config(config)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
