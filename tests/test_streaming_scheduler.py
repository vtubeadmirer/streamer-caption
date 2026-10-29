"""PendingWindowScheduler: confirmed VAD segments and the gray preview windows (core/streaming.py).

A preview window runs from the start of the utterance (pre-roll included) to now, so Whisper
hears the whole sentence so far; it starts only once the utterance is pending_min_utterance_sec
old, comes at most every chunk_seconds of new audio, and is capped at pending_max_window_sec,
starting again at the first pause the voice detector heard (or exactly that far back).
"""
from __future__ import annotations

import copy
import math
from pathlib import Path
import sys
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.audio_capture import AudioChunk  # noqa: E402
from core.config import DEFAULTS, save_config, validate_config  # noqa: E402
from core.streaming import PendingWindowScheduler  # noqa: E402

STEP = 0.1  # seconds per chunk


def make_chunk(offset_sec: float, duration_sec: float, sample_rate: int, amplitude: float) -> AudioChunk:
    sample_count = int(round(duration_sec * sample_rate))
    t = np.arange(sample_count, dtype=np.float32) / sample_rate
    samples = (np.sin(2 * math.pi * 440.0 * t) * amplitude).astype(np.float32)
    rms = float(np.sqrt(np.mean(np.square(samples)))) if sample_count else 0.0
    return AudioChunk(samples=samples, rms=rms, offset_sec=offset_sec, duration_sec=duration_sec)


def make_config(**streaming: float) -> dict:
    config = copy.deepcopy(DEFAULTS)
    config["vad"]["engine"] = "energy"
    config["audio"]["vad_energy_threshold"] = 0.01
    config["audio"]["min_segment_peak_rms"] = 0.0
    config["audio"]["pre_roll_sec"] = 0.3
    config["audio"]["silence_duration_sec"] = 0.5
    config["audio"]["min_segment_sec"] = 0.2
    config["audio"]["max_segment_sec"] = 12.0
    config["streaming"]["chunk_seconds"] = 0.3
    config["streaming"]["chunk_overlap_sec"] = 0.1
    config["streaming"]["pending_debounce_sec"] = 0.1
    config["streaming"].update(streaming)
    validate_config(config)
    return config


def run(config: dict, pattern: list[float]):
    """pattern: one amplitude per 0.1 s chunk (0 = silence)."""
    scheduler = PendingWindowScheduler(config)
    sample_rate = config["audio"]["sample_rate"]
    actions = []
    for index, amplitude in enumerate(pattern):
        chunk = make_chunk(round(index * STEP, 6), STEP, sample_rate, amplitude)
        for action in scheduler.push(chunk):
            actions.append((round((index + 1) * STEP, 6), action))
    actions.extend((round(len(pattern) * STEP, 6), action) for action in scheduler.flush())
    pending = [(at, action) for at, action in actions if action.action_type == "pending_window"]
    confirmed = [action for _, action in actions if action.action_type == "confirmed_segment"]
    return pending, confirmed


def close(a: float, b: float) -> bool:
    return abs(a - b) < 1e-6


def check_config() -> None:
    config = copy.deepcopy(DEFAULTS)
    assert config["streaming"]["pending_min_utterance_sec"] == 1.2
    assert config["streaming"]["pending_max_window_sec"] == 6.0
    validate_config(config)
    for key, value in (
        ("pending_min_utterance_sec", -0.1),
        ("pending_min_utterance_sec", 6.0),
        ("pending_max_window_sec", 0.5),
        ("pending_max_window_sec", 31.0),
    ):
        broken = copy.deepcopy(config)
        broken["streaming"][key] = value
        try:
            validate_config(broken)
        except ValueError:
            continue
        raise AssertionError(f"streaming.{key}={value} must be rejected")
    # buffer_max_sec may be 5 while the window is 6 s: settings still save (the admin page goes
    # through save_config) and the scheduler caps the window at the buffer.
    small_buffer = copy.deepcopy(config)
    small_buffer["streaming"]["buffer_max_sec"] = 5
    validate_config(small_buffer)
    with tempfile.TemporaryDirectory() as tmp:
        save_config(small_buffer, Path(tmp) / "config.json")
    assert PendingWindowScheduler(small_buffer).pending_max_window_sec == 5.0


def check_short_utterance_has_no_preview() -> None:
    # 0.4 s of speech: the final caption only, no gray guess at a word that has barely begun.
    pending, confirmed = run(make_config(), [0.0] * 5 + [0.08] * 4 + [0.0] * 8)
    assert not pending, pending
    assert len(confirmed) == 1 and confirmed[0].segment.duration_sec >= 0.3


def check_window_grows_from_the_utterance_start() -> None:
    # 0.5 s quiet, then 3 s of speech, then quiet.
    pending, confirmed = run(make_config(), [0.0] * 5 + [0.08] * 30 + [0.0] * 8)
    assert len(confirmed) == 1
    segment = confirmed[0].segment
    assert close(segment.start_offset_sec, 0.2), segment.start_offset_sec  # 0.3 s pre-roll
    assert pending, "a 3 s utterance needs previews"
    first_at, _ = pending[0]
    # Speech starts at 0.5 s: no preview before 1.2 s of it.
    assert first_at >= 0.5 + 1.2 - 1e-6, first_at
    ids = {action.metadata["preview_utterance_id"] for _, action in pending}
    assert len(ids) == 1
    previous_end = 0.0
    for at, action in pending:
        window = action.window
        assert close(window.start_offset_sec, 0.2), window.start_offset_sec  # whole sentence so far
        assert close(window.end_offset_sec, at)
        assert window.end_offset_sec - previous_end >= 0.3 - 1e-6  # stride: chunk_seconds
        previous_end = window.end_offset_sec
        assert close(action.metadata["utterance_start_offset_sec"], 0.2)
        assert 0.8 <= action.metadata["speech_share"] <= 1.0, action.metadata
    assert len(pending) >= 4


def check_long_utterance_folds_at_a_pause() -> None:
    # 5 s speech, a 0.2 s pause (shorter than silence_duration_sec), 4 s more speech.
    pattern = [0.0] * 5 + [0.08] * 50 + [0.0] * 2 + [0.08] * 40 + [0.0] * 8
    pending, confirmed = run(make_config(pending_max_window_sec=6.0), pattern)
    assert len(confirmed) == 1, "the short pause must not split the sentence"
    starts = sorted({round(action.window.start_offset_sec, 3) for _, action in pending})
    assert starts[0] == 0.2
    # Once the window would pass 6 s it starts again at the pause (5.5 s).
    assert 5.5 in starts, starts
    for _, action in pending:
        assert action.window.duration_sec <= 6.0 + 1e-6, action.window.duration_sec
        assert action.metadata["preview_utterance_id"] == pending[0][1].metadata["preview_utterance_id"]
        assert close(action.metadata["utterance_start_offset_sec"], 0.2)


def check_window_is_capped_at_the_buffer() -> None:
    # A 5 s buffer with the 6 s default: past 5 s the window starts again at the pause (1.0 s)
    # instead of wherever the buffer happens to begin.
    pattern = [0.0] * 5 + [0.08] * 5 + [0.0] * 2 + [0.08] * 60 + [0.0] * 8
    pending, confirmed = run(make_config(buffer_max_sec=5.0), pattern)
    assert len(confirmed) == 1
    starts = sorted({round(action.window.start_offset_sec, 3) for _, action in pending})
    assert 1.0 in starts, starts
    for _, action in pending:
        assert action.window.duration_sec <= 5.0 + 1e-6, action.window.duration_sec


def check_long_utterance_without_pause_slides() -> None:
    pending, _ = run(make_config(pending_max_window_sec=3.0), [0.0] * 5 + [0.08] * 60 + [0.0] * 8)
    late = [action for at, action in pending if at > 0.2 + 3.0 + 0.3]
    assert late
    for action in late:
        assert close(action.window.duration_sec, 3.0), action.window.duration_sec


def check_new_utterance_gets_a_new_id() -> None:
    pattern = [0.0] * 5 + [0.08] * 20 + [0.0] * 8 + [0.08] * 20 + [0.0] * 8
    pending, confirmed = run(make_config(), pattern)
    assert len(confirmed) == 2
    ids = [action.metadata["preview_utterance_id"] for _, action in pending]
    assert len(set(ids)) == 2 and ids == sorted(ids), ids
    second = [action for _, action in pending if action.metadata["preview_utterance_id"] == ids[-1]]
    assert all(close(action.window.start_offset_sec, confirmed[1].segment.start_offset_sec) for action in second)


def check_old_style_short_windows_still_possible() -> None:
    # pending_min_utterance_sec 0 gives previews from the first voiced chunks.
    pending, confirmed = run(
        make_config(pending_min_utterance_sec=0.0),
        [0.0, 0.08, 0.08, 0.08, 0.08, 0.0, 0.0, 0.0, 0.0, 0.0],
    )
    assert pending and confirmed
    assert pending[0][1].window.duration_sec <= 0.41


def main() -> int:
    check_config()
    check_short_utterance_has_no_preview()
    check_window_grows_from_the_utterance_start()
    check_long_utterance_folds_at_a_pause()
    check_window_is_capped_at_the_buffer()
    check_long_utterance_without_pause_slides()
    check_new_utterance_gets_a_new_id()
    check_old_style_short_windows_still_possible()
    print("streaming_scheduler_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
