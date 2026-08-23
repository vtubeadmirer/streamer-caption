from __future__ import annotations

import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.audio_capture import AudioChunk
from core.config import load_config
from core.streaming import PendingWindowScheduler


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
    config["audio"]["min_segment_peak_rms"] = 0.0
    config["audio"]["silence_duration_sec"] = 0.2
    config["audio"]["min_segment_sec"] = 0.2
    config["streaming"]["chunk_seconds"] = 0.3
    config["streaming"]["chunk_overlap_sec"] = 0.1
    config["streaming"]["pending_debounce_sec"] = 0.1

    scheduler = PendingWindowScheduler(config)
    sample_rate = config["audio"]["sample_rate"]
    actions = []
    offset = 0.0
    for amplitude in [0.0, 0.08, 0.08, 0.08, 0.08, 0.0, 0.0, 0.0]:
        chunk = make_chunk(offset, 0.1, sample_rate, amplitude)
        offset += 0.1
        actions.extend(scheduler.push(chunk))
    actions.extend(scheduler.flush())

    pending = [action for action in actions if action.action_type == "pending_window"]
    confirmed = [action for action in actions if action.action_type == "confirmed_segment"]
    assert pending, actions
    assert confirmed, actions
    assert pending[0].window is not None
    assert pending[0].window.duration_sec <= 0.41
    assert confirmed[0].segment is not None
    assert confirmed[0].segment.duration_sec >= 0.3
    print("streaming_scheduler_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
