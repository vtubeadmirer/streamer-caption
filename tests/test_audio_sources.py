from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.audio_sources import (
    SystemAudioChunkSource,
    create_audio_chunk_source,
    list_system_audio_devices,
)
from core.config import load_config


class FakeRecorder:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def record(self, numframes: int):
        left = np.full(numframes, 0.2, dtype=np.float32)
        right = np.full(numframes, 0.4, dtype=np.float32)
        return np.column_stack((left, right))


class FakeDevice:
    def __init__(self, identifier: str, name: str, loopback: bool):
        self.id = identifier
        self.name = name
        self.isloopback = loopback

    def recorder(self, samplerate: int, blocksize: int):
        assert samplerate == 16000
        assert blocksize >= 4096
        return FakeRecorder()


def main() -> int:
    speaker = FakeDevice("speaker-1", "Speakers", False)
    loopback = FakeDevice("loop-1", "Speakers (Loopback)", True)
    microphone = FakeDevice("mic-1", "Microphone", False)
    fake_soundcard = types.SimpleNamespace(
        default_speaker=lambda: speaker,
        all_microphones=lambda include_loopback=False: [microphone, loopback],
    )
    previous = sys.modules.get("soundcard")
    sys.modules["soundcard"] = fake_soundcard
    try:
        devices = list_system_audio_devices()
        assert len(devices) == 1
        assert devices[0].id == "loop-1"
        assert devices[0].is_default

        config = load_config()
        config["audio"]["source"] = "system"
        config["audio"]["system_device"] = "loop-1"
        config["audio"]["sample_rate"] = 16000
        config["audio"]["block_size"] = 8
        config["audio"]["input_gain"] = 1.0
        source = create_audio_chunk_source(config)
        assert isinstance(source, SystemAudioChunkSource)
        chunks = source.chunks(max_seconds=1.0)
        chunk = next(chunks)
        chunks.close()
        assert chunk.samples.shape == (8,)
        assert np.allclose(chunk.samples, 0.3, atol=1e-6)
        assert abs(chunk.rms - 0.3) < 1e-5
        assert source.device_label == "Speakers (Loopback)"
    finally:
        if previous is None:
            sys.modules.pop("soundcard", None)
        else:
            sys.modules["soundcard"] = previous

    print("audio_sources_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())