from __future__ import annotations

import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.import_audio import import_audio
from tools.record_wav import write_wav
from tools.transcribe_wav import read_wav_mono


def main() -> int:
    sample_rate = 16000
    t = np.arange(int(sample_rate * 0.5), dtype=np.float32) / sample_rate
    samples = (np.sin(2 * math.pi * 440.0 * t) * 0.2).astype(np.float32)
    source = ROOT / "samples" / "_test_import_source.wav"
    out = ROOT / "samples" / "_test_import_out.wav"
    write_wav(source, samples, sample_rate)
    result = import_audio(source, out, sample_rate)
    loaded, loaded_rate = read_wav_mono(out)
    assert result["duration_sec"] > 0.49
    assert loaded_rate == sample_rate
    assert float(np.max(np.abs(loaded))) > 0.1
    print("import_audio_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
