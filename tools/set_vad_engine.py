from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config, save_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Set VAD engine.")
    parser.add_argument("engine", choices=["energy", "silero"])
    parser.add_argument("--speech-threshold", type=float)
    args = parser.parse_args()

    config = load_config()
    config.setdefault("vad", {})["engine"] = args.engine
    if args.speech_threshold is not None:
        config["vad"]["speech_threshold"] = args.speech_threshold
    save_config(config)
    print(
        "vad engine={engine} speech_threshold={threshold}".format(
            engine=config["vad"]["engine"],
            threshold=config["vad"].get("speech_threshold", 0.5),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
