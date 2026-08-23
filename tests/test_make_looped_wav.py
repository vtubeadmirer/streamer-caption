from __future__ import annotations

from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.make_looped_wav import loop_wav
from tools.record_wav import write_wav
from tools.transcribe_wav import read_wav_mono


def main() -> int:
    sample_rate = 16000
    source = ROOT / "samples" / "_test_loop_source.wav"
    out = ROOT / "samples" / "_test_loop_out.wav"
    samples = np.linspace(-0.2, 0.2, sample_rate // 10, dtype=np.float32)
    write_wav(source, samples, sample_rate)
    result = loop_wav(source, out, 0.5)
    loaded, loaded_rate = read_wav_mono(out)
    assert loaded_rate == sample_rate
    assert len(loaded) == sample_rate // 2
    assert result["repeats"] == 5
    print("make_looped_wav_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
