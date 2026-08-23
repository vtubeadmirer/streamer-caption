from __future__ import annotations

from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.make_synthetic_voice_clip import make_voice_like


def main() -> int:
    samples = make_voice_like(16000, 2.0, 0.12)
    assert samples.shape[0] == 32000
    assert float(np.max(np.abs(samples))) > 0.05
    assert float(np.sqrt(np.mean(np.square(samples)))) > 0.005
    print("synthetic_voice_clip_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
