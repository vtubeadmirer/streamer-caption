from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.events import CaptionEvent
from core.stt_worker import SttWorker
from features.defense import DefenseFilter
from tools import run_supervised


class FakeEngine:
    name = "fake"

    def on_audio_segment(self, audio, sample_rate: int, audio_offset_sec: float):
        yield CaptionEvent(
            event_type="caption_confirmed",
            text=str(audio),
            confidence=0.95,
            audio_offset_sec=audio_offset_sec,
            engine=self.name,
        )


def main() -> int:
    config = load_config()
    defense = DefenseFilter(config)

    low_voice = defense.apply(
        "hello",
        0.95,
        {"segment_voice_ratio": 0.01, "no_speech_prob": 0.1},
    )
    assert not low_voice.visible
    assert low_voice.reason == "low_voice_ratio"

    no_speech = defense.apply(
        "hello",
        0.95,
        {"segment_voice_ratio": 0.5, "no_speech_prob": 0.9},
    )
    assert not no_speech.visible
    assert no_speech.reason == "no_speech_probability"

    greeting = "안녕하세요 감사합니다"
    spoken = defense.apply(
        greeting,
        0.95,
        {"segment_voice_ratio": 0.5, "no_speech_prob": 0.1},
    )
    assert spoken.visible, spoken

    defense.whitelist.add(greeting)
    protected_non_speech = defense.apply(
        greeting,
        0.1,
        {"segment_voice_ratio": 0.0, "no_speech_prob": 1.0},
    )
    assert not protected_non_speech.visible
    assert protected_non_speech.reason == "low_voice_ratio"

    protected = defense.apply(
        greeting,
        0.1,
        {"segment_voice_ratio": 0.5, "no_speech_prob": 0.1},
    )
    assert protected.visible
    assert protected.reason == "whitelist"

    worker = SttWorker(FakeEngine(), max_queue=1)
    assert worker.can_accept_pending()
    worker.submit_latest("pending", "preview", 16000, 0.0)
    assert not worker.can_accept_pending()
    worker.mark_pending_skipped()
    confirmed = worker.submit(
        "confirmed",
        "final",
        16000,
        0.0,
        metadata={"segment_voice_ratio": 0.5},
    )
    queued = worker.snapshot()
    assert queued["superseded_pending_jobs"] == 1
    assert queued["skipped_pending_jobs"] == 1
    worker.start()
    result = worker.wait_for(confirmed.job_id, timeout=2.0)
    worker.stop()
    assert result.job.job_type == "confirmed"
    assert result.events[0].metadata["segment_voice_ratio"] == 0.5

    command = run_supervised.build_child_command(["--audio-source", "system"])
    assert "--v2-light" in command
    assert command[-2:] == ["--audio-source", "system"]

    calls = []
    returncodes = iter((75, 0))
    original_run = run_supervised.subprocess.run
    original_sleep = run_supervised.time.sleep

    def fake_run(command, cwd, check):
        calls.append((command, cwd, check))
        return SimpleNamespace(returncode=next(returncodes))

    run_supervised.subprocess.run = fake_run
    run_supervised.time.sleep = lambda seconds: None
    try:
        exit_code = run_supervised.run_supervised([], 1, 0.0)
    finally:
        run_supervised.subprocess.run = original_run
        run_supervised.time.sleep = original_sleep
    assert exit_code == 0
    assert len(calls) == 2

    print("reliability_controls_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())