from __future__ import annotations

import queue
import time
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    channels: int
    default_sample_rate: float


@dataclass(frozen=True)
class AudioChunk:
    samples: Any
    rms: float
    offset_sec: float
    duration_sec: float
    status: str = ""
    clip_fraction: float = 0.0


@dataclass(frozen=True)
class AudioSegment:
    samples: Any
    start_offset_sec: float
    end_offset_sec: float
    peak_rms: float
    voice_ratio: float = 0.0

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_offset_sec - self.start_offset_sec)


def list_input_devices() -> list[InputDevice]:
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError("sounddevice is not installed. Install requirements first.") from exc

    devices: list[InputDevice] = []
    for index, device in enumerate(sd.query_devices()):
        channels = int(device.get("max_input_channels", 0))
        if channels <= 0:
            continue
        devices.append(
            InputDevice(
                index=index,
                name=str(device.get("name", "")),
                channels=channels,
                default_sample_rate=float(device.get("default_samplerate", 0.0)),
            )
        )
    return devices


def calculate_rms(samples: Any) -> float:
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("numpy is not installed. Install requirements first.") from exc

    if samples is None:
        return 0.0
    arr = np.asarray(samples, dtype=np.float32)
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(arr))))


class EnergyVadSegmenter:
    def __init__(self, config: dict[str, Any]) -> None:
        self.sample_rate = int(config["audio"]["sample_rate"])
        self.threshold = float(config["audio"]["vad_energy_threshold"])
        self.min_peak_rms = float(config["audio"].get("min_segment_peak_rms", 0.0))
        self.silence_duration_sec = float(config["audio"]["silence_duration_sec"])
        self.max_segment_sec = float(config["audio"]["max_segment_sec"])
        self.min_segment_sec = float(config["audio"]["min_segment_sec"])
        self.pre_roll_sec = max(0.0, float(config["audio"].get("pre_roll_sec", 0.3)))
        self._pre_roll: deque[AudioChunk] = deque()
        self._chunks: list[AudioChunk] = []
        self._segment_start: float | None = None
        self._last_voice_offset: float | None = None
        self._peak_rms = 0.0
        self._voice_duration_sec = 0.0
        self._last_discard: dict[str, Any] | None = None

    def push(self, chunk: AudioChunk) -> AudioSegment | None:
        return self._push_with_voice(chunk, chunk.rms >= self.threshold)

    def _push_with_voice(self, chunk: AudioChunk, is_voice: bool) -> AudioSegment | None:
        if self._segment_start is None:
            if not is_voice:
                self._remember_pre_roll(chunk)
                return None
            if self._pre_roll:
                self._chunks.extend(self._pre_roll)
                self._segment_start = self._pre_roll[0].offset_sec
                self._pre_roll.clear()
            else:
                self._segment_start = chunk.offset_sec

        self._chunks.append(chunk)
        self._peak_rms = max(self._peak_rms, chunk.rms)
        if is_voice:
            self._last_voice_offset = chunk.offset_sec + chunk.duration_sec
            self._voice_duration_sec += chunk.duration_sec

        segment_end = chunk.offset_sec + chunk.duration_sec
        segment_duration = segment_end - self._segment_start
        silence_duration = (
            segment_end - self._last_voice_offset
            if self._last_voice_offset is not None
            else segment_duration
        )

        if segment_duration >= self.max_segment_sec:
            return self._flush(segment_end)

        if silence_duration >= self.silence_duration_sec:
            if segment_duration >= self.min_segment_sec:
                end = self._last_voice_offset or segment_end
                return self._flush(end)
            self.reset()
            return None

        return None

    def _remember_pre_roll(self, chunk: AudioChunk) -> None:
        if self.pre_roll_sec <= 0:
            return
        self._pre_roll.append(chunk)
        while (
            len(self._pre_roll) > 1
            and self._pre_roll[-1].offset_sec
            + self._pre_roll[-1].duration_sec
            - self._pre_roll[0].offset_sec
            > self.pre_roll_sec
        ):
            self._pre_roll.popleft()

    def flush(self) -> AudioSegment | None:
        if self._segment_start is None or not self._chunks:
            return None
        end = self._chunks[-1].offset_sec + self._chunks[-1].duration_sec
        if end - self._segment_start < self.min_segment_sec:
            self.reset()
            return None
        return self._flush(end)

    def reset(self) -> None:
        self._pre_roll.clear()
        self._chunks = []
        self._segment_start = None
        self._last_voice_offset = None
        self._peak_rms = 0.0
        self._voice_duration_sec = 0.0

    def pop_discard(self) -> dict[str, Any] | None:
        value = self._last_discard
        self._last_discard = None
        return value

    def _flush(self, end_offset_sec: float) -> AudioSegment | None:
        import numpy as np

        if self._peak_rms < self.min_peak_rms:
            self._last_discard = {
                "reason": "low_peak_rms",
                "peak_rms": self._peak_rms,
                "min_peak_rms": self.min_peak_rms,
            }
            self.reset()
            return None
        start_offset_sec = self._segment_start or 0.0
        samples = np.concatenate([chunk.samples for chunk in self._chunks], axis=0)
        segment = AudioSegment(
            samples=samples,
            start_offset_sec=start_offset_sec,
            end_offset_sec=end_offset_sec,
            peak_rms=self._peak_rms,
            voice_ratio=min(
                1.0,
                self._voice_duration_sec
                / max(end_offset_sec - start_offset_sec, 0.001),
            ),
        )
        self.reset()
        return segment


class MicrophoneChunkSource:
    source_name = "microphone"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.sample_rate = int(config["audio"]["sample_rate"])
        self.block_size = int(config["audio"]["block_size"])
        self.device = config["audio"]["device"]
        self.input_gain = float(config["audio"].get("input_gain", 1.0))
        self.stall_timeout_sec = float(
            config["audio"].get("source_stall_timeout_sec", 3.0)
        )
        self.retry_delay_sec = float(
            config["audio"].get("source_retry_delay_sec", 1.0)
        )
        self.max_restarts = int(config["audio"].get("source_max_restarts", 10))
        self._queue: queue.Queue[AudioChunk] = queue.Queue(maxsize=256)
        self._sample_count = 0
        self._dropped_chunks = 0
        self._restart_count = 0
        self._last_error = ""

    def chunks(self, max_seconds: float | None = None) -> Any:
        started = time.monotonic()
        deadline = None if max_seconds is None else started + max_seconds
        while deadline is None or time.monotonic() < deadline:
            self._clear_queue()
            try:
                yield from self._capture_once(deadline)
                return
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self._restart_count += 1
                self._last_error = str(exc)
                if self._restart_count > self.max_restarts:
                    raise RuntimeError(
                        f"microphone recovery failed after {self.max_restarts} restarts: {exc}"
                    ) from exc
                if deadline is not None and time.monotonic() >= deadline:
                    return
                time.sleep(self.retry_delay_sec)

    def _capture_once(self, deadline: float | None) -> Any:
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("numpy and sounddevice are required for mic capture.") from exc

        def callback(indata, frames, time_info, status) -> None:
            del frames, time_info
            mono = np.asarray(indata, dtype=np.float32).reshape(-1)
            if self.input_gain != 1.0:
                mono = np.clip(mono * self.input_gain, -1.0, 1.0).astype(np.float32)
            offset = self._sample_count / self.sample_rate
            duration = len(mono) / self.sample_rate
            self._sample_count += len(mono)
            chunk = AudioChunk(
                samples=mono.copy(),
                rms=calculate_rms(mono),
                offset_sec=offset,
                duration_sec=duration,
                status=str(status) if status else "",
                clip_fraction=float(np.mean(np.abs(mono) >= 0.999)),
            )
            try:
                self._queue.put_nowait(chunk)
            except queue.Full:
                self._dropped_chunks += 1
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                self._queue.put_nowait(chunk)

        with sd.InputStream(
            device=self.device,
            channels=1,
            samplerate=self.sample_rate,
            blocksize=self.block_size,
            dtype="float32",
            callback=callback,
        ):
            last_chunk_at = time.monotonic()
            while deadline is None or time.monotonic() < deadline:
                try:
                    chunk = self._queue.get(timeout=0.5)
                except queue.Empty:
                    if time.monotonic() - last_chunk_at >= self.stall_timeout_sec:
                        raise RuntimeError(
                            f"microphone callback stalled for {self.stall_timeout_sec:.1f}s"
                        )
                    continue
                last_chunk_at = time.monotonic()
                yield chunk

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

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

class AudioSegmentSource:
    """Builds VAD segments from any configured audio chunk source."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.sample_rate = int(config["audio"]["sample_rate"])
        self.input_gain = float(config["audio"].get("input_gain", 1.0))

        from core.audio_sources import create_audio_chunk_source
        from core.vad import create_vad_segmenter

        self._chunks = create_audio_chunk_source(config)
        self._segmenter = create_vad_segmenter(config)

    def segments(
        self,
        max_seconds: float | None = None,
        on_chunk: Any | None = None,
    ) -> Any:
        for chunk in self._chunks.chunks(max_seconds=max_seconds):
            segment = self._segmenter.push(chunk)
            discard = self._segmenter.pop_discard()
            if on_chunk is not None:
                decision = getattr(self._segmenter, "last_decision", None)
                on_chunk(
                    {
                        "rms": chunk.rms,
                        "threshold": self._segmenter.threshold,
                        "is_voice": (
                            bool(decision.is_voice)
                            if decision is not None
                            else chunk.rms >= self._segmenter.threshold
                        ),
                        "vad_engine": getattr(
                            decision,
                            "engine",
                            getattr(self._segmenter, "engine", "energy"),
                        ),
                        "vad_score": getattr(decision, "score", chunk.rms),
                        "offset_sec": chunk.offset_sec,
                        "duration_sec": chunk.duration_sec,
                        "dropped_chunks": self._chunks.dropped_chunks,
                        "status": chunk.status,
                        "audio_source": getattr(
                            self._chunks,
                            "source_name",
                            "microphone",
                        ),
                        "source_restarts": getattr(
                            self._chunks,
                            "restart_count",
                            0,
                        ),
                        "source_last_error": getattr(
                            self._chunks,
                            "last_error",
                            "",
                        ),
                        "device_label": getattr(
                            self._chunks,
                            "device_label",
                            "",
                        ),
                        "clip_fraction": chunk.clip_fraction,
                        "input_gain": self.input_gain,
                        "discard": discard,
                    }
                )
            if segment is not None:
                yield segment

        segment = self._segmenter.flush()
        discard = self._segmenter.pop_discard()
        if discard is not None and on_chunk is not None:
            on_chunk(
                {
                    "rms": 0.0,
                    "threshold": self._segmenter.threshold,
                    "is_voice": False,
                    "offset_sec": (
                        self._chunks.sample_count / self.sample_rate
                    ),
                    "duration_sec": 0.0,
                    "dropped_chunks": self._chunks.dropped_chunks,
                    "status": "",
                    "audio_source": getattr(
                        self._chunks,
                        "source_name",
                        "microphone",
                    ),
                    "source_restarts": getattr(
                        self._chunks,
                        "restart_count",
                        0,
                    ),
                    "source_last_error": getattr(
                        self._chunks,
                        "last_error",
                        "",
                    ),
                    "device_label": getattr(
                        self._chunks,
                        "device_label",
                        "",
                    ),
                    "clip_fraction": 0.0,
                    "input_gain": self.input_gain,
                    "discard": discard,
                }
            )
        if segment is not None:
            yield segment

    @property
    def dropped_chunks(self) -> int:
        return self._chunks.dropped_chunks


MicrophoneSegmentSource = AudioSegmentSource