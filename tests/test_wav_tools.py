from __future__ import annotations

import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.record_wav import write_wav
from tools.transcribe_wav import read_wav_mono


def main() -> int:
    sample_rate = 16000
    duration_sec = 0.2
    t = np.arange(int(sample_rate * duration_sec), dtype=np.float32) / sample_rate
    samples = (np.sin(2 * math.pi * 440.0 * t) * 0.25).astype(np.float32)
    path = ROOT / "samples" / "_test_roundtrip.wav"
    write_wav(path, samples, sample_rate)
    loaded, loaded_rate = read_wav_mono(path)
    assert loaded_rate == sample_rate
    assert abs(len(loaded) - len(samples)) <= 1
    assert float(np.max(np.abs(loaded))) > 0.2
    print("wav_tools_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

