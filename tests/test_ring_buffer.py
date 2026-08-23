from __future__ import annotations

from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.ring_buffer import AudioRingBuffer


def main() -> int:
    ring = AudioRingBuffer(sample_rate=10, max_seconds=1.0)
    first = ring.append(np.arange(6, dtype=np.float32))
    assert first.start_sample == 0
    assert first.end_sample == 6
    assert ring.duration_sec == 0.6

    second = ring.append(np.arange(6, 14, dtype=np.float32))
    assert second.start_sample == 6
    assert second.end_sample == 14
    assert ring.start_sample == 4
    assert ring.end_sample == 14
    assert ring.duration_sec == 1.0

    latest = ring.latest(0.5)
    assert latest.start_sample == 9
    assert latest.end_sample == 14
    assert latest.samples.tolist() == [9.0, 10.0, 11.0, 12.0, 13.0]

    clipped = ring.window(0, 6)
    assert clipped.start_sample == 4
    assert clipped.end_sample == 6
    assert clipped.samples.tolist() == [4.0, 5.0]
    print("ring_buffer_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
