from __future__ import annotations

import copy
import queue
import threading
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

    def chunks(
        self,
        max_seconds: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> Any:
        started = time.monotonic()
        deadline = None if max_seconds is None else started + max_seconds
        while (
            (deadline is None or time.monotonic() < deadline)
            and not (stop_event is not None and stop_event.is_set())
        ):
            try:
                yield from self._record_once(deadline, stop_event)
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

    def _record_once(self, deadline: float | None, stop_event: threading.Event | None) -> Any:
        import numpy as np

        device = _select_loopback_device(self.device)
        self._device_label = str(getattr(device, "name", _device_id(device)))
        recorder_blocksize = max(4096, self.block_size * 4)
        with device.recorder(
            samplerate=self.sample_rate,
            blocksize=recorder_blocksize,
        ) as recorder:
            while (
                (deadline is None or time.monotonic() < deadline)
                and not (stop_event is not None and stop_event.is_set())
            ):
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
                    source_name="system",
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


def config_for_audio_source(
    config: dict[str, Any],
    source_name: str,
) -> dict[str, Any]:
    """Build an isolated per-source runtime config for VAD and gain."""
    runtime = copy.deepcopy(config)
    audio = runtime["audio"]
    audio["source"] = source_name
    audio["input_gain"] = float(
        audio.get(f"{source_name}_input_gain", audio.get("input_gain", 1.0))
    )
    audio["vad_energy_threshold"] = float(
        audio.get(
            f"{source_name}_vad_energy_threshold",
            audio.get("vad_energy_threshold", 0.015),
        )
    )
    audio["min_segment_peak_rms"] = float(
        audio.get(
            f"{source_name}_min_segment_peak_rms",
            audio.get("min_segment_peak_rms", 0.012),
        )
    )
    return runtime


class DualAudioChunkSource:
    """Merges microphone and Windows loopback chunks without mixing samples."""

    source_name = "both"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.sample_rate = int(config["audio"]["sample_rate"])
        self._sources = {
            "microphone": MicrophoneChunkSource(
                config_for_audio_source(config, "microphone")
            ),
            "system": SystemAudioChunkSource(
                config_for_audio_source(config, "system")
            ),
        }
        self._queue: queue.Queue[AudioChunk] = queue.Queue(maxsize=512)
        self._stop_event = threading.Event()
        self._errors: dict[str, str] = {}
        self._threads: list[threading.Thread] = []
        self._dropped_merge_chunks = 0

    def chunks(self, max_seconds: float | None = None) -> Any:
        self._stop_event.clear()
        self._errors.clear()
        self._threads = [
            threading.Thread(
                target=self._pump,
                args=(name, source, max_seconds),
                name=f"audio-{name}",
                daemon=True,
            )
            for name, source in self._sources.items()
        ]
        for thread in self._threads:
            thread.start()
        deadline = (
            None if max_seconds is None else time.monotonic() + max_seconds
        )
        try:
            while any(thread.is_alive() for thread in self._threads) or not self._queue.empty():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                try:
                    yield self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
        finally:
            self._stop_event.set()
            for thread in self._threads:
                thread.join(timeout=2.0)
        if self._errors and len(self._errors) == len(self._sources):
            details = "; ".join(
                f"{name}: {message}" for name, message in self._errors.items()
            )
            raise RuntimeError(f"all dual audio sources failed: {details}")

    def _pump(
        self,
        name: str,
        source: Any,
        max_seconds: float | None,
    ) -> None:
        try:
            for chunk in source.chunks(
                max_seconds=max_seconds,
                stop_event=self._stop_event,
            ):
                if self._stop_event.is_set():
                    return
                try:
                    self._queue.put(chunk, timeout=0.1)
                except queue.Full:
                    self._dropped_merge_chunks += 1
        except Exception as exc:
            self._errors[name] = str(exc)

    @property
    def sources(self) -> dict[str, Any]:
        return dict(self._sources)

    @property
    def dropped_chunks(self) -> int:
        return self._dropped_merge_chunks + sum(
            int(getattr(source, "dropped_chunks", 0))
            for source in self._sources.values()
        )

    @property
    def sample_count(self) -> int:
        return max(
            int(getattr(source, "sample_count", 0))
            for source in self._sources.values()
        )

    @property
    def restart_count(self) -> int:
        return sum(
            int(getattr(source, "restart_count", 0))
            for source in self._sources.values()
        )

    @property
    def last_error(self) -> str:
        messages = [
            f"{name}: {message}"
            for name, message in self._errors.items()
            if message
        ]
        return "; ".join(messages)

    @property
    def device_label(self) -> str:
        return " | ".join(
            f"{name}={getattr(source, 'device_label', '')}"
            for name, source in self._sources.items()
        )

    def source_statuses(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "restart_count": int(getattr(source, "restart_count", 0)),
                "last_error": self._errors.get(
                    name,
                    str(getattr(source, "last_error", "")),
                ),
                "dropped_chunks": int(getattr(source, "dropped_chunks", 0)),
                "device_label": str(getattr(source, "device_label", "")),
            }
            for name, source in self._sources.items()
        }

def create_audio_chunk_source(config: dict[str, Any]) -> Any:
    source = str(config.get("audio", {}).get("source", "microphone")).lower()
    if source == "system":
        return SystemAudioChunkSource(config)
    if source == "microphone":
        return MicrophoneChunkSource(config)
    if source == "both":
        return DualAudioChunkSource(config)
    raise ValueError(f"Unsupported audio source: {source}")
