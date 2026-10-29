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


class LoopbackStalled(RuntimeError):
    """The loopback recorder delivered nothing at all, not even SoundCard's own zeros."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"PC 소리 입력이 {seconds:.0f}초 동안 멈춰서 다시 열었습니다 (loopback stalled)")
        self.seconds = seconds


def _skip_false_silence(recorder: Any, count_skipped: Any) -> None:
    """Keep SoundCard from reporting silence for time in which audio was arriving.

    When WASAPI has no packet, SoundCard 0.4.6's _Recorder._record_chunk sleeps in steps of
    under a millisecond, and once 40 ms have passed it returns zeros for all the time since it
    started waiting, without looking for packets again. A reader thread that wakes late
    (another thread held the GIL for a third of a second) thus returns zeros for audio that is
    waiting in the buffer, and that audio right after them: silence spliced into speech, and
    the clock that much ahead (7.5 s in a 23-minute live test). Here the zeros are kept only
    for the time the waiting packets do not cover, less the device period the reader waits for
    every packet anyway: before the packets when the sound started while the reader slept,
    after them when it stopped. A recorder without those internals is left alone.
    """
    original = getattr(recorder, "_record_chunk", None)
    if not (
        callable(original)
        and hasattr(recorder, "_idle_start_time")
        and hasattr(recorder, "_capture_available_frames")
    ):
        return
    import numpy as np

    channels = max(1, len(set(getattr(recorder, "channelmap", [0]))))
    try:
        period_sec = float(recorder.deviceperiod[0])
    except Exception:
        period_sec = 0.01
    period = int(period_sec * float(getattr(recorder, "samplerate", 0))) * channels

    def record_chunk() -> Any:
        was_silent = recorder._idle_start_time is not None  # its last chunk was zeros too
        chunk = original()
        if recorder._idle_start_time is None:
            return chunk  # a packet, not SoundCard's estimate of silence
        waiting: list[Any] = []
        waiting_size = 0
        while waiting_size < len(chunk) and recorder._capture_available_frames():
            packet = original()
            if not len(packet):
                break
            waiting.append(packet)
            waiting_size += len(packet)
        if not waiting:
            return chunk
        keep = max(0, len(chunk) - waiting_size - period)
        count_skipped((len(chunk) - keep) // channels)
        if was_silent:
            return np.concatenate([chunk[:keep], *waiting])
        return np.concatenate([*waiting, chunk[:keep]])

    recorder._record_chunk = record_chunk


class SystemAudioChunkSource:
    """Captures the selected Windows output device through WASAPI loopback.

    The recorder runs in its own thread and hands blocks over through a bounded queue, so a
    recorder.record() call that does not return cannot stop captions. In a real stream
    (2026-10-29) the app got no PC sound for three and a half minutes while the streamer was
    talking, and the sentence before the gap was never finished. The audio clock (samples
    handed out) has to stay on wall time as well: dual mode matches echoes by it, and VOD times
    come from it.

    * A reader thread that wakes late finds the audio of the time it slept waiting in the
      WASAPI buffer, which holds system_silence_fill_sec and a second more; SoundCard's zeros
      for that time are dropped (_skip_false_silence), so late audio is never cut.
    * No block for system_silence_fill_sec (1 s; on a silent PC SoundCard returns zeros within
      40 ms, so the reader is stuck): silent blocks keep the clock on wall time, so the voice
      detector sees the pause and finishes the sentence. Audio the recorder held back and hands
      over afterwards is dropped where the silence already stood in for it: never more than was
      filled in, and only for CATCH_UP_SEC after the first block back.
    * Still nothing after system_stall_restart_sec (5 s): the recorder is opened again
      (restart_count and stall_restart_count go up). Reopens with no audio in between wait longer
      each time, up to a minute, and never end the source. A stuck recorder thread cannot be
      killed; while MAX_STUCK_READERS of them are still inside the driver, a new recorder is
      opened only every STUCK_RETRY_SEC (the stuck calls may never return, and a new one may
      work), and none while STUCK_READER_CEILING are stuck, so the threads left behind stay few.
    * Only errors (no device, a recorder that fails to open or read) count toward
      source_max_restarts, and that count starts over once audio flows again.
    * Whatever else might put the clock ahead of the time since capture started, exactly silent
      audio beyond MAX_LEAD_SEC is dropped and no silence is filled in past it.

    Times are time.perf_counter(): before Python 3.13, time.monotonic() on Windows moves in
    15.6 ms steps, more than a block.
    """

    source_name = "system"
    POLL_SEC = 0.05
    MAX_STALL_WAIT_SEC = 60.0
    CATCH_UP_SEC = 1.0
    MAX_LEAD_SEC = 0.05
    MAX_STUCK_READERS = 3
    STUCK_RETRY_SEC = 60.0
    STUCK_READER_CEILING = 8

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        audio = config["audio"]
        self.sample_rate = int(audio["sample_rate"])
        self.block_size = int(audio["block_size"])
        self.device = str(audio.get("system_device", ""))
        self.input_gain = float(audio.get("input_gain", 1.0))
        self.retry_delay_sec = float(audio.get("source_retry_delay_sec", 1.0))
        self.max_restarts = int(audio.get("source_max_restarts", 10))
        self.silence_fill_sec = float(audio.get("system_silence_fill_sec", 1.0))
        self.stall_restart_sec = float(audio.get("system_stall_restart_sec", 5.0))
        # About four seconds of blocks; when the pipeline falls behind, the oldest are dropped.
        self.queue_blocks = max(8, int(4.0 * self.sample_rate / max(1, self.block_size)))
        self._sample_count = 0
        self._restart_count = 0
        self._stall_restarts = 0
        self._error_streak = 0
        self._quiet_reopens = 0
        self._dropped_chunks = 0
        self._silence_filled_samples = 0
        self._silence_fills = 0
        self._filling = False
        self._fill_debt = 0  # filled-in samples that audio held back by the recorder may cover
        self._catch_up_until: float | None = None
        self._false_silence_frames = 0
        self._trimmed_samples = 0
        self._stuck: list[threading.Thread] = []
        self._reader_started_at = 0.0
        self._last_error = ""
        self._device_label = ""
        self._capture_state = "starting"
        # Wall time since the last real audio (silence fill, held-back audio) and since capture
        # started (the clock never runs ahead of it).
        self._anchor_time: float | None = None
        self._anchor_samples = 0
        self._start_time: float | None = None
        self._start_samples = 0

    def chunks(
        self,
        max_seconds: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> Any:
        started = time.perf_counter()
        deadline = None if max_seconds is None else started + max_seconds
        self._anchor_time, self._anchor_samples = started, self._sample_count
        self._start_time, self._start_samples = started, self._sample_count
        self._fill_debt = 0
        while not self._should_stop(deadline, stop_event):
            try:
                yield from self._capture(deadline, stop_event)
                return
            except KeyboardInterrupt:
                raise
            except LoopbackStalled as stall:
                self._restart_count += 1
                self._stall_restarts += 1
                self._quiet_reopens += 1
                self._last_error = str(stall)
                self._capture_state = "reopening"
            except Exception as exc:
                self._restart_count += 1
                self._error_streak += 1
                self._last_error = str(exc)
                self._capture_state = "error"
                if self._error_streak > self.max_restarts:
                    raise RuntimeError(
                        f"system audio recovery failed after {self.max_restarts} restarts: {exc}"
                    ) from exc
                yield from self._wait_until(
                    time.perf_counter() + self.retry_delay_sec, deadline, stop_event
                )

    def _capture(self, deadline: float | None, stop_event: threading.Event | None) -> Any:
        """One recorder, read in its own thread, until the run ends or the recorder stalls."""
        _load_soundcard()  # COM starts on this long-lived thread, not on a reader thread
        yield from self._wait_for_stuck_readers(deadline, stop_event)
        if self._should_stop(deadline, stop_event):
            return
        yield from self._fill_silence(time.perf_counter())  # time spent closing a stalled recorder
        blocks: queue.Queue[tuple[str, Any, float]] = queue.Queue(maxsize=self.queue_blocks)
        reader_stop = threading.Event()
        reader = threading.Thread(
            target=self._read_blocks,
            args=(blocks, reader_stop),
            name="audio-system-reader",
            daemon=True,
        )
        reader.start()
        waiting_since = self._reader_started_at = time.perf_counter()
        try:
            while not self._should_stop(deadline, stop_event):
                try:
                    kind, payload, read_at = blocks.get(timeout=self.POLL_SEC)
                except queue.Empty:
                    kind, payload, read_at = "", None, 0.0
                now = time.perf_counter()
                if kind == "error":
                    raise payload
                if kind == "opened":
                    waiting_since = read_at
                    continue
                if kind == "data":
                    # Timed by when the reader got the block: a pipeline thread that is busy
                    # for a while must not make the recorder look late.
                    waiting_since = read_at
                    self._error_streak = 0
                    self._quiet_reopens = 0
                    self._capture_state = "capturing"
                    samples = self._fit_to_wall_time(self._mono(payload), read_at)
                    if len(samples):
                        yield self._chunk(samples, status="")
                        self._filling = False
                    if not self._fill_debt:
                        self._anchor_time, self._anchor_samples = read_at, self._sample_count
                    continue
                gap = now - waiting_since
                if gap >= self.silence_fill_sec:
                    if self._capture_state == "capturing":
                        self._capture_state = "silence"
                    yield from self._fill_silence(now)
                if gap >= self._stall_wait_sec():
                    raise LoopbackStalled(gap)
        finally:
            reader_stop.set()
            reader.join(timeout=0.25)  # a working reader returns within one block
            if reader.is_alive():
                # Stuck inside the audio driver; it ends by itself if the call ever returns.
                self._stuck.append(reader)

    def _stall_wait_sec(self) -> float:
        return min(
            self.MAX_STALL_WAIT_SEC,
            self.stall_restart_sec * (2 ** min(self._quiet_reopens, 8)),
        )

    def _wait_for_stuck_readers(
        self, deadline: float | None, stop_event: threading.Event | None
    ) -> Any:
        while not self._should_stop(deadline, stop_event):
            self._stuck = [thread for thread in self._stuck if thread.is_alive()]
            if len(self._stuck) < self.MAX_STUCK_READERS:
                return
            if (
                len(self._stuck) < self.STUCK_READER_CEILING
                and time.perf_counter() - self._reader_started_at >= self.STUCK_RETRY_SEC
            ):
                self._capture_state = "reopening"
                return
            self._capture_state = "stuck"
            time.sleep(self.POLL_SEC)
            yield from self._fill_silence(time.perf_counter())

    def _wait_until(
        self, until: float, deadline: float | None, stop_event: threading.Event | None
    ) -> Any:
        while time.perf_counter() < until and not self._should_stop(deadline, stop_event):
            time.sleep(self.POLL_SEC)
            yield from self._fill_silence(time.perf_counter())

    def _read_blocks(self, blocks: queue.Queue, stop: threading.Event) -> None:
        import numpy as np

        try:
            device = _select_loopback_device(self.device)
            self._device_label = str(getattr(device, "name", _device_id(device)))
            # SoundCard sizes the WASAPI buffer as blocksize / samplerate. It keeps what a reader
            # that wakes late has not picked up yet: up to system_silence_fill_sec and a second
            # more, so audio that is only late is never lost (it used to hold 256 ms).
            recorder_blocksize = max(
                4096,
                self.block_size * 4,
                int(self.sample_rate * (self.silence_fill_sec + 1.0)),
            )
            with device.recorder(
                samplerate=self.sample_rate,
                blocksize=recorder_blocksize,
            ) as recorder:
                _skip_false_silence(recorder, self._count_false_silence)
                self._offer(blocks, ("opened", None, time.perf_counter()))
                while not stop.is_set():
                    data = np.asarray(
                        recorder.record(numframes=self.block_size),
                        dtype=np.float32,
                    )
                    if stop.is_set():
                        return
                    if data.size == 0:
                        raise RuntimeError("system loopback returned an empty audio block")
                    self._offer(blocks, ("data", data, time.perf_counter()))
        except Exception as exc:
            if not stop.is_set():
                self._offer(blocks, ("error", exc, time.perf_counter()))

    def _count_false_silence(self, frames: int) -> None:
        self._false_silence_frames += frames

    def _offer(self, blocks: queue.Queue, item: tuple[str, Any, float]) -> None:
        """Never blocks the reader: when the queue is full, the oldest block is dropped."""
        try:
            blocks.put_nowait(item)
            return
        except queue.Full:
            pass
        try:
            blocks.get_nowait()
            self._dropped_chunks += 1
        except queue.Empty:
            pass
        try:
            blocks.put_nowait(item)
        except queue.Full:
            self._dropped_chunks += 1

    def _mono(self, data: Any) -> Any:
        import numpy as np

        if data.ndim == 1:
            mono = data.reshape(-1)
        else:
            # SoundCard documents a Windows single-channel corruption issue.
            mono = np.mean(data, axis=1, dtype=np.float32)
        if self.input_gain != 1.0:
            mono = np.clip(mono * self.input_gain, -1.0, 1.0).astype(np.float32)
        return mono.astype(np.float32, copy=True)

    def _fill_silence(self, now: float) -> Any:
        """Silent blocks up to wall time since the last real audio."""
        import numpy as np

        target = min(self._wall_sample_count(now), self._clock_limit(now))
        while self._sample_count + self.block_size <= target:
            if not self._filling:
                self._filling = True
                self._silence_fills += 1
            self._silence_filled_samples += self.block_size
            self._fill_debt += self.block_size
            self._catch_up_until = None
            yield self._chunk(np.zeros(self.block_size, dtype=np.float32), status="silence_fill")

    def _fit_to_wall_time(self, samples: Any, read_at: float) -> Any:
        """The part of a block that keeps the audio clock on wall time (maybe all, maybe none).

        After silence was filled in, the recorder may hand over audio it held back during the
        gap. Placed after the silence it would put the clock ahead of wall time, a little further
        with every hang, so its oldest part is dropped as far as the silence already stood in for
        it: never more than was filled in, and only until CATCH_UP_SEC after the first block
        back (what a reopened recorder delivers is new). Wall time still counts from the audio
        before the gap until then."""
        if self._fill_debt:
            if self._catch_up_until is None:
                self._catch_up_until = read_at + self.CATCH_UP_SEC
            if read_at < self._catch_up_until:
                ahead = self._sample_count + len(samples) - self._wall_sample_count(read_at)
                drop = min(max(0, ahead), self._fill_debt, len(samples))
                self._fill_debt -= drop
                self._trimmed_samples += drop
                samples = samples[drop:]
            else:
                self._fill_debt = 0
        ahead = (
            self._sample_count
            + len(samples)
            - self._clock_limit(read_at)
            - int(self.MAX_LEAD_SEC * self.sample_rate)
        )
        if ahead > 0 and not samples.any():
            keep = max(0, len(samples) - ahead)
            self._trimmed_samples += len(samples) - keep
            samples = samples[:keep]
        return samples

    def _wall_sample_count(self, now: float) -> int:
        """The sample count at `now` had audio kept coming in real time since the anchor."""
        anchor = self._anchor_time if self._anchor_time is not None else now
        return self._anchor_samples + int((now - anchor) * self.sample_rate)

    def _clock_limit(self, now: float) -> int:
        """The sample count at `now` had audio come in real time since capture started."""
        start = self._start_time if self._start_time is not None else now
        return self._start_samples + int((now - start) * self.sample_rate)

    def _chunk(self, mono: Any, *, status: str) -> AudioChunk:
        import numpy as np

        offset = self._sample_count / self.sample_rate
        duration = len(mono) / self.sample_rate
        self._sample_count += len(mono)
        return AudioChunk(
            samples=mono,
            rms=calculate_rms(mono),
            offset_sec=offset,
            duration_sec=duration,
            status=status,
            clip_fraction=float(np.mean(np.abs(mono) >= 0.999)) if len(mono) else 0.0,
            source_name="system",
        )

    @staticmethod
    def _should_stop(deadline: float | None, stop_event: threading.Event | None) -> bool:
        return (deadline is not None and time.perf_counter() >= deadline) or (
            stop_event is not None and stop_event.is_set()
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
    def stall_restart_count(self) -> int:
        return self._stall_restarts

    @property
    def silence_filled_sec(self) -> float:
        return self._silence_filled_samples / self.sample_rate

    @property
    def silence_fill_count(self) -> int:
        """How often silence was filled in because the recorder delivered nothing."""
        return self._silence_fills

    @property
    def false_silence_sec(self) -> float:
        """SoundCard silence dropped because the audio of that time arrived after all."""
        return self._false_silence_frames / self.sample_rate

    @property
    def clock_trimmed_sec(self) -> float:
        """Audio dropped to keep the clock on wall time (held back after a fill, or silence)."""
        return self._trimmed_samples / self.sample_rate

    @property
    def capture_state(self) -> str:
        return self._capture_state

    @property
    def stuck_readers(self) -> int:
        """Recorder threads still stuck inside the audio driver."""
        self._stuck = [thread for thread in self._stuck if thread.is_alive()]
        return len(self._stuck)

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
    def stall_restart_count(self) -> int:
        return sum(
            int(getattr(source, "stall_restart_count", 0))
            for source in self._sources.values()
        )

    @property
    def silence_filled_sec(self) -> float:
        return sum(
            float(getattr(source, "silence_filled_sec", 0.0))
            for source in self._sources.values()
        )

    @property
    def silence_fill_count(self) -> int:
        return sum(
            int(getattr(source, "silence_fill_count", 0))
            for source in self._sources.values()
        )

    @property
    def false_silence_sec(self) -> float:
        return sum(
            float(getattr(source, "false_silence_sec", 0.0))
            for source in self._sources.values()
        )

    @property
    def clock_trimmed_sec(self) -> float:
        return sum(
            float(getattr(source, "clock_trimmed_sec", 0.0))
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
                # PC sound only (SystemAudioChunkSource): reopens after the recorder stalled,
                # how often and how long silence was filled in while it delivered nothing,
                # SoundCard silence dropped for a reader that woke late, audio dropped to keep
                # the clock on wall time, and its state.
                "stall_restart_count": int(getattr(source, "stall_restart_count", 0)),
                "silence_fill_count": int(getattr(source, "silence_fill_count", 0)),
                "silence_filled_sec": round(float(getattr(source, "silence_filled_sec", 0.0)), 1),
                "false_silence_sec": round(float(getattr(source, "false_silence_sec", 0.0)), 1),
                "clock_trimmed_sec": round(float(getattr(source, "clock_trimmed_sec", 0.0)), 1),
                "capture_state": str(getattr(source, "capture_state", "")),
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
