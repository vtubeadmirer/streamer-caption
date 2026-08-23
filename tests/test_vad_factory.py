from __future__ import annotations

import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.audio_capture import AudioChunk
from core.config import load_config
from core.vad import EnergyVoiceDetector, create_vad_segmenter, create_voice_detector


def make_chunk(offset_sec: float, duration_sec: float, sample_rate: int, amplitude: float) -> AudioChunk:
    sample_count = int(duration_sec * sample_rate)
    t = np.arange(sample_count, dtype=np.float32) / sample_rate
    samples = (np.sin(2 * math.pi * 440.0 * t) * amplitude).astype(np.float32)
    rms = float(np.sqrt(np.mean(np.square(samples)))) if sample_count else 0.0
    return AudioChunk(samples=samples, rms=rms, offset_sec=offset_sec, duration_sec=duration_sec)


def main() -> int:
    config = load_config()
    config["vad"]["engine"] = "energy"
    config["audio"]["vad_energy_threshold"] = 0.01
    detector = create_voice_detector(config)
    assert isinstance(detector, EnergyVoiceDetector)

    segmenter = create_vad_segmenter(config)
    sample_rate = config["audio"]["sample_rate"]
    chunk = make_chunk(0.0, 0.1, sample_rate, 0.08)
    assert segmenter.push(chunk) is None
    assert segmenter.last_decision is not None
    assert segmenter.last_decision.is_voice is True
    assert segmenter.last_decision.engine == "energy"

    config["vad"]["engine"] = "silero"
    fallback_detector = create_voice_detector(config)
    assert hasattr(fallback_detector, "decide")
    print(f"vad_factory_ok engine={getattr(fallback_detector, 'engine', 'unknown')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
