from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config, save_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Set audio.input_gain in config.json.")
    parser.add_argument("gain", type=float)
    args = parser.parse_args()

    gain = max(0.1, min(50.0, args.gain))
    config = load_config()
    old = float(config["audio"].get("input_gain", 1.0))
    config["audio"]["input_gain"] = gain
    save_config(config)
    print(json.dumps({"old_input_gain": old, "new_input_gain": gain}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

