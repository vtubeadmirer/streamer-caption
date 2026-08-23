from __future__ import annotations

import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.audio_capture import AudioChunk, EnergyVadSegmenter
from core.config import load_config


def make_chunk(offset_sec: float, duration_sec: float, sample_rate: int, amplitude: float) -> AudioChunk:
    sample_count = int(duration_sec * sample_rate)
    t = np.arange(sample_count, dtype=np.float32) / sample_rate
    samples = (np.sin(2 * math.pi * 440.0 * t) * amplitude).astype(np.float32)
    rms = float(np.sqrt(np.mean(np.square(samples)))) if sample_count else 0.0
    return AudioChunk(samples=samples, rms=rms, offset_sec=offset_sec, duration_sec=duration_sec)


def main() -> int:
    config = load_config()
    config["audio"]["vad_energy_threshold"] = 0.01
    config["audio"]["silence_duration_sec"] = 0.2
    config["audio"]["min_segment_sec"] = 0.2
    sample_rate = config["audio"]["sample_rate"]
    segmenter = EnergyVadSegmenter(config)

    segments = []
    offset = 0.0
    pattern = [
        (0.2, 0.0),
        (0.6, 0.08),
        (0.4, 0.0),
    ]
    for duration, amplitude in pattern:
        for _ in range(int(duration / 0.1)):
            chunk = make_chunk(offset, 0.1, sample_rate, amplitude)
            offset += 0.1
            segment = segmenter.push(chunk)
            if segment is not None:
                segments.append(segment)

    tail = segmenter.flush()
    if tail is not None:
        segments.append(tail)

    assert len(segments) == 1, f"expected 1 segment, got {len(segments)}"
    assert segments[0].duration_sec >= 0.49, segments[0].duration_sec
    assert segments[0].peak_rms > 0.03, segments[0].peak_rms
    assert 0.5 <= segments[0].voice_ratio <= 1.0, segments[0].voice_ratio
    config["audio"]["min_segment_peak_rms"] = 0.02
    quiet_segmenter = EnergyVadSegmenter(config)
    offset = 0.0
    quiet_segments = []
    for _ in range(10):
        chunk = make_chunk(offset, 0.1, sample_rate, 0.016)
        offset += 0.1
        segment = quiet_segmenter.push(chunk)
        if segment is not None:
            quiet_segments.append(segment)
    assert quiet_segments == [], quiet_segments
    discard = quiet_segmenter.flush()
    assert discard is None
    discard_info = quiet_segmenter.pop_discard()
    assert discard_info is not None
    assert discard_info["reason"] == "low_peak_rms"
    print(
        "energy_vad_ok duration={duration:.2f}s peak_rms={rms:.5f}".format(
            duration=segments[0].duration_sec,
            rms=segments[0].peak_rms,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
