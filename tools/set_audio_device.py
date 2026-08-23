from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.audio_capture import list_input_devices
from core.config import load_config, save_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Set audio.device in config.json.")
    parser.add_argument("device", type=int)
    args = parser.parse_args()

    devices = {device.index: device for device in list_input_devices()}
    if args.device not in devices:
        print(f"Input device not found: {args.device}")
        return 1
    config = load_config()
    old = config["audio"]["device"]
    config["audio"]["device"] = args.device
    save_config(config)
    print(
        json.dumps(
            {
                "old_device": old,
                "new_device": args.device,
                "name": devices[args.device].name,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

