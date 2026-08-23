from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.audio_capture import AudioChunk, EnergyVadSegmenter
from core.config import load_config, validate_config
from core.events import CaptionEvent
from core.stt_worker import SttWorker
from features.defense import DefenseFilter


class FakeNumpy:
    @staticmethod
    def concatenate(items, axis=0):
        del axis
        return [value for item in items for value in item]


class FakeEngine:
    name = "fake"

    def on_audio_segment(self, audio, sample_rate, audio_offset_sec):
        del sample_rate
        yield CaptionEvent(
            event_type="caption_confirmed",
            text=str(audio),
            audio_offset_sec=audio_offset_sec,
            engine=self.name,
        )


def test_normal_greeting_is_visible() -> None:
    assert DefenseFilter(load_config()).apply("감사합니다", 0.9).visible


def test_vad_keeps_pre_roll() -> None:
    config = copy.deepcopy(load_config())
    config["audio"].update(
        {
            "vad_energy_threshold": 0.5,
            "min_segment_peak_rms": 0.0,
            "pre_roll_sec": 0.2,
            "silence_duration_sec": 0.1,
            "min_segment_sec": 0.1,
            "max_segment_sec": 2.0,
        }
    )
    segmenter = EnergyVadSegmenter(config)
    chunks = [
        AudioChunk([0], 0.0, 0.0, 0.1),
        AudioChunk([1], 1.0, 0.1, 0.1),
        AudioChunk([0], 0.0, 0.2, 0.1),
    ]
    old_numpy = sys.modules.get("numpy")
    sys.modules["numpy"] = FakeNumpy()
    try:
        segment = None
        for chunk in chunks:
            segment = segmenter.push(chunk) or segment
    finally:
        if old_numpy is None:
            sys.modules.pop("numpy", None)
        else:
            sys.modules["numpy"] = old_numpy
    assert segment is not None
    assert segment.start_offset_sec == 0.0
    assert segment.samples[:2] == [0, 1]


def test_confirmed_job_replaces_queued_preview() -> None:
    worker = SttWorker(FakeEngine(), max_queue=1)
    worker.submit_latest("pending", "preview", 16000, 0.0)
    confirmed = worker.submit("confirmed", "final", 16000, 0.0)
    assert worker.dropped_jobs == 1
    worker.start()
    result = worker.wait_for(confirmed.job_id, timeout=2.0)
    worker.stop()
    assert result.job.job_type == "confirmed"
    assert result.events[0].text == "final"


def test_invalid_config_is_rejected() -> None:
    config = copy.deepcopy(load_config())
    config["streaming"]["chunk_seconds"] = 0.5
    config["streaming"]["chunk_overlap_sec"] = 0.5
    try:
        validate_config(config)
    except ValueError:
        return
    raise AssertionError("invalid overlap was accepted")


def test_overlay_uses_runtime_display_settings() -> None:
    html = (ROOT / "overlay" / "caption.html").read_text(encoding="utf-8")
    assert "fetch(" in html
    assert 'adminUrl("/api/config")' in html
    assert "streamingConfig.confirmed_hold_sec" in html
    assert "streamingConfig.pending_stale_sec" in html
    assert "function scheduleTimers()" in html
    assert 'location.hostname || "127.0.0.1"' in html


def test_log_tuned_4060_preset() -> None:
    preset = json.loads(
        (
            ROOT
            / "data"
            / "presets"
            / "recommended_4060ti_stable.json"
        ).read_text(encoding="utf-8")
    )
    assert preset["caption"]["just_chatting_min_line_hold_sec"] == 0.2
    assert preset["whisper"]["repetition_penalty"] == 1.05
    assert preset["whisper"]["no_repeat_ngram_size"] == 3
    assert preset["streaming"]["stt_stall_warn_sec"] == 8.0
    assert preset["streaming"]["stt_stall_restart_sec"] == 20.0
    assert preset["archive"]["topic_window_captions"] == 10
    assert preset["archive"]["topic_similarity_threshold"] == 0.15
    assert preset["archive"]["topic_cooldown_sec"] == 120

def main() -> int:
    tests = [
        test_normal_greeting_is_visible,
        test_vad_keeps_pre_roll,
        test_confirmed_job_replaces_queued_preview,
        test_invalid_config_is_rejected,
        test_overlay_uses_runtime_display_settings,
        test_log_tuned_4060_preset,
    ]
    for test in tests:
        test()
    print("broadcast_regressions_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
