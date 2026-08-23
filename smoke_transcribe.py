from __future__ import annotations

import argparse
import copy

import numpy as np

from core.config import load_config
from engines.baseline.engine import BaselineEngine
from features.defense import DefenseFilter
from features.slang import SlangDictionary
from main import postprocess_event


def main() -> int:
    parser = argparse.ArgumentParser(description="Load a small faster-whisper model and transcribe synthetic audio.")
    parser.add_argument("--model", default="tiny")
    parser.add_argument("--device", default=None)
    parser.add_argument("--compute-type", default=None)
    parser.add_argument("--seconds", type=float, default=1.0)
    parser.add_argument("--postprocess", action="store_true")
    args = parser.parse_args()

    config = copy.deepcopy(load_config())
    config["whisper"]["model"] = args.model
    if args.device is not None:
        config["whisper"]["device"] = args.device
    if args.compute_type is not None:
        config["whisper"]["compute_type"] = args.compute_type

    sample_rate = int(config["audio"]["sample_rate"])
    sample_count = int(sample_rate * args.seconds)
    audio = np.zeros(sample_count, dtype=np.float32)

    engine = BaselineEngine(config)
    defense = DefenseFilter(config)
    slang = SlangDictionary(config)
    print(
        "loading model={model} device={device} compute_type={compute}".format(
            model=config["whisper"]["model"],
            device=config["whisper"]["device"],
            compute=config["whisper"]["compute_type"],
        )
    )
    for event in engine.on_audio_segment(audio, sample_rate, 0.0):
        events = postprocess_event(event, defense, slang) if args.postprocess else [event]
        for output_event in events:
            print(f"{output_event.event_type}: {output_event.text}")
            print("metadata:", output_event.metadata)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
