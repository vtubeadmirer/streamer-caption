from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from core.audio_capture import AudioChunk, MicrophoneChunkSource, calculate_rms


@dataclass(frozen=True)
class SystemAudioDevice:
    id: str
    name: str
    is_default: bool = False


def _load_soundcard() -> Any:
    try:
        import soundcard as sc
    except ImportError as exc:
        raise RuntimeError(
            "SoundCard is required for PC audio capture. Run setup_4060ti_pc.bat again."
        ) from exc
    return sc


def _device_id(device: Any) -> str:
    return str(getattr(device, "id", getattr(device, "_id", "")))


def list_system_audio_devices() -> list[SystemAudioDevice]:
    sc = _load_soundcard()
    default_speaker = sc.default_speaker()
    default_id = _device_id(default_speaker)
    default_name = str(getattr(default_speaker, "name", ""))
    devices = []
    for microphone in sc.all_microphones(include_loopback=True):
        if not bool(getattr(microphone, "isloopback", False)):
            continue
        identifier = _device_id(microphone)
        name = str(getattr(microphone, "name", identifier))
        is_default = bool(
            identifier == default_id
            or (default_name and default_name.lower() in name.lower())
        )
        devices.append(
            SystemAudioDevice(id=identifier, name=name, is_default=is_default)
        )
    return devices


def _select_loopback_device(configured: str) -> Any:
    sc = _load_soundcard()
    loopbacks = [
        device
        for device in sc.all_microphones(include_loopback=True)
        if bool(getattr(device, "isloopback", False))
    ]
    if not loopbacks:
        raise RuntimeError("No Windows loopback audio device was found.")

    requested = configured.strip().lower()
    if requested:
        for device in loopbacks:
            identifier = _device_id(device).lower()
            name = str(getattr(device, "name", "")).lower()
            if requested in {identifier, name} or requested in name:
                return device
        raise RuntimeError(f"Configured loopback device was not found: {configured}")

    speaker = sc.default_speaker()
    speaker_id = _device_id(speaker)
    speaker_name = str(getattr(speaker, "name", "")).lower()
    for device in loopbacks:
        if _device_id(device) == speaker_id:
            return device
        if speaker_name and speaker_name in str(getattr(device, "name", "")).lower():
            return device
    return loopbacks[0]


class SystemAudioChunkSource:
    """Captures the selected Windows output device through WASAPI loopback."""

    source_name = "system"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        audio = config["audio"]
        self.sample_rate = int(audio["sample_rate"])
        self.block_size = int(audio["block_size"])
        self.device = str(audio.get("system_device", ""))
        self.input_gain = float(audio.get("input_gain", 1.0))
        self.retry_delay_sec = float(audio.get("source_retry_delay_sec", 1.0))
        self.max_restarts = int(audio.get("source_max_restarts", 10))
        self._sample_count = 0
        self._restart_count = 0
        self._dropped_chunks = 0
        self._last_error = ""
        self._device_label = ""

    def chunks(self, max_seconds: float | None = None) -> Any:
        started = time.monotonic()
        deadline = None if max_seconds is None else started + max_seconds
        while deadline is None or time.monotonic() < deadline:
            try:
                yield from self._record_once(deadline)
                return
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self._restart_count += 1
                self._last_error = str(exc)
                if self._restart_count > self.max_restarts:
                    raise RuntimeError(
                        f"system audio recovery failed after {self.max_restarts} restarts: {exc}"
                    ) from exc
                if deadline is not None and time.monotonic() >= deadline:
                    return
                time.sleep(self.retry_delay_sec)

    def _record_once(self, deadline: float | None) -> Any:
        import numpy as np

        device = _select_loopback_device(self.device)
        self._device_label = str(getattr(device, "name", _device_id(device)))
        recorder_blocksize = max(4096, self.block_size * 4)
        with device.recorder(
            samplerate=self.sample_rate,
            blocksize=recorder_blocksize,
        ) as recorder:
            while deadline is None or time.monotonic() < deadline:
                data = np.asarray(
                    recorder.record(numframes=self.block_size),
                    dtype=np.float32,
                )
                if data.size == 0:
                    raise RuntimeError("system loopback returned an empty audio block")
                if data.ndim == 1:
                    mono = data.reshape(-1)
                else:
                    # SoundCard documents a Windows single-channel corruption issue.
                    mono = np.mean(data, axis=1, dtype=np.float32)
                if self.input_gain != 1.0:
                    mono = np.clip(mono * self.input_gain, -1.0, 1.0).astype(
                        np.float32
                    )
                offset = self._sample_count / self.sample_rate
                duration = len(mono) / self.sample_rate
                self._sample_count += len(mono)
                yield AudioChunk(
                    samples=mono.copy(),
                    rms=calculate_rms(mono),
                    offset_sec=offset,
                    duration_sec=duration,
                    status="",
                    clip_fraction=float(np.mean(np.abs(mono) >= 0.999)),
                )

    @property
    def dropped_chunks(self) -> int:
        return self._dropped_chunks

    @property
    def sample_count(self) -> int:
        return self._sample_count

    @property
    def restart_count(self) -> int:
        return self._restart_count

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def device_label(self) -> str:
        return self._device_label


def create_audio_chunk_source(config: dict[str, Any]) -> Any:
    source = str(config.get("audio", {}).get("source", "microphone")).lower()
    if source == "system":
        return SystemAudioChunkSource(config)
    if source == "microphone":
        return MicrophoneChunkSource(config)
    raise ValueError(f"Unsupported audio source: {source}")
