from __future__ import annotations

import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.record_wav import write_wav
from tools.tune_voice_wav import tune


def main() -> int:
    sample_rate = 16000
    t = np.arange(int(sample_rate * 1.0), dtype=np.float32) / sample_rate
    samples = (np.sin(2 * math.pi * 440.0 * t) * 0.08).astype(np.float32)
    path = ROOT / "samples" / "_test_tune.wav"
    write_wav(path, samples, sample_rate)
    report = tune(path, apply=False)
    assert report["chunk_count"] > 0
    assert report["energy"]["rms_max"] > 0.04
    assert report["recommendation"]["vad_engine"] in {"energy", "silero"}
    print("tune_voice_wav_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
