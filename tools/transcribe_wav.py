from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT, load_config
from core.events import CaptionEvent
from engines.baseline.engine import BaselineEngine
from features.defense import DefenseFilter
from features.slang import SlangDictionary
from main import postprocess_event


def resolve_wav_path(value: str) -> Path:
    path = (PROJECT_ROOT / value).resolve()
    if not path.exists():
        raise FileNotFoundError(
            f"WAV file not found: {path}\n"
            "Record one first with: .\\.venv\\Scripts\\python.exe tools\\record_wav.py --seconds 10 --out samples\\test_clip.wav"
        )
    return path


def read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as f:
        channels = f.getnchannels()
        sample_width = f.getsampwidth()
        sample_rate = f.getframerate()
        frames = f.readframes(f.getnframes())
    if sample_width != 2:
        raise ValueError(f"Only 16-bit PCM WAV is supported, got sample_width={sample_width}")
    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1).astype(np.float32)
    return data, sample_rate


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcribe a WAV file through the baseline pipeline.")
    parser.add_argument("path")
    parser.add_argument("--model")
    parser.add_argument("--device")
    parser.add_argument("--compute-type")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = load_config()
    if args.model:
        config["whisper"]["model"] = args.model
    if args.device:
        config["whisper"]["device"] = args.device
    if args.compute_type:
        config["whisper"]["compute_type"] = args.compute_type

    try:
        audio, sample_rate = read_wav_mono(resolve_wav_path(args.path))
    except Exception as exc:
        print(exc)
        return 1
    engine = BaselineEngine(config)
    defense = DefenseFilter(config)
    slang = SlangDictionary(config)
    output: list[dict] = []

    for event in engine.on_audio_segment(audio, sample_rate, 0.0):
        for processed in postprocess_event(event, defense, slang):
            record = {
                **processed.to_dict(),
                "source_path": args.path,
                "duration_sec": len(audio) / sample_rate,
            }
            output.append(record)
            if not args.json:
                if processed.event_type == "caption_confirmed":
                    print(f"caption: {processed.text} (conf={processed.confidence:.2f})")
                else:
                    print(f"{processed.event_type}: {processed.text}")

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
