from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from engines.baseline.engine import BaselineEngine


class FakeModel:
    def __init__(self) -> None:
        self.kwargs = {}

    def transcribe(self, audio, **kwargs):
        assert np.asarray(audio).size > 0
        self.kwargs = kwargs
        segment = SimpleNamespace(
            text="recognized speech",
            avg_logprob=-0.2,
            no_speech_prob=0.15,
            compression_ratio=1.25,
        )
        return [segment], SimpleNamespace(duration=1.25)


def main() -> int:
    config = load_config()
    engine = BaselineEngine(config)
    model = FakeModel()
    engine._model = model

    events = list(
        engine.on_audio_segment(
            np.ones(1600, dtype=np.float32),
            16000,
            2.0,
        )
    )
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "caption_confirmed"
    assert abs(event.metadata["no_speech_prob"] - 0.15) < 1e-6
    assert abs(event.metadata["avg_logprob"] + 0.2) < 1e-6
    assert abs(event.metadata["compression_ratio"] - 1.25) < 1e-6
    assert event.metadata["transcript_segment_count"] == 1
    assert model.kwargs["no_speech_threshold"] == config["whisper"]["no_speech_threshold"]
    assert model.kwargs["condition_on_previous_text"] is False
    assert model.kwargs["vad_filter"] is False
    print("baseline_metadata_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())