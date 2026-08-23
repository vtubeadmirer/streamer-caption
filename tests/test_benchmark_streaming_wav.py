from __future__ import annotations

import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.events import CaptionEvent
from tools.benchmark_streaming_wav import benchmark_audio


class FakeEngine:
    name = "fake"

    def on_audio_segment(self, audio, sample_rate: int, audio_offset_sec: float):
        del audio, sample_rate
        yield CaptionEvent(
            event_type="caption_confirmed",
            text="테스트 문장입니다",
            confidence=0.95,
            audio_offset_sec=audio_offset_sec,
            engine=self.name,
        )


def main() -> int:
    config = load_config()
    config["vad"]["engine"] = "energy"
    config["audio"]["vad_energy_threshold"] = 0.01
    config["audio"]["min_segment_peak_rms"] = 0.0
    config["audio"]["silence_duration_sec"] = 0.2
    config["audio"]["block_size"] = 1600
    config["streaming"]["chunk_seconds"] = 0.3
    config["streaming"]["chunk_overlap_sec"] = 0.1
    config["streaming"]["pending_debounce_sec"] = 0.1

    sample_rate = int(config["audio"]["sample_rate"])
    t = np.arange(int(sample_rate * 1.0), dtype=np.float32) / sample_rate
    audio = (np.sin(2 * math.pi * 440.0 * t) * 0.08).astype(np.float32)
    report = benchmark_audio(config, audio, sample_rate, FakeEngine())
    assert report["chunk_count"] == 10
    assert report["pending_jobs"] >= 1
    assert report["confirmed_jobs"] >= 1
    print("benchmark_streaming_wav_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
