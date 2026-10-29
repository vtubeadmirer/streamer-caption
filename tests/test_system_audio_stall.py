"""PC sound (WASAPI loopback) that stops delivering audio (core/audio_sources.py).

In a real stream (2026-10-29) the app got no PC sound for three and a half minutes while the
streamer was talking: recorder.record() did not return, nothing restarted it, and the sentence
before the gap was never finished. The recorder now runs in its own thread, and these tests drive
it with a fake recorder that delivers audio in real time, hangs and resumes:

* a gap of 0.2 s or more is filled with silent blocks, so the audio clock stays on wall time;
  audio the recorder held back during the gap and hands over at once afterwards never takes
  the clock past wall time, however many hangs there are;
* after system_stall_restart_sec the recorder is opened again, however often it happens, without
  ending the source (a quiet or sleeping output device is not broken), and no more than
  MAX_STUCK_READERS recorder threads are ever left stuck in the driver;
* real errors still count toward source_max_restarts, and that count starts over once audio flows;
* closing the generator stops the reader, and dual mode keeps working.
"""
from __future__ import annotations

import copy
import queue
import sys
import threading
import time
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.audio_capture import AudioChunk  # noqa: E402
from core.audio_sources import DualAudioChunkSource, SystemAudioChunkSource  # noqa: E402
from core.config import DEFAULTS, validate_config  # noqa: E402

SR = 16000
BLOCK = 160  # 10 ms


class FakeRecorder:
    """Steps: ("data", seconds or None, value) in real time, ("burst", seconds, value) all at
    once (what the driver held back during a hang), ("hang", seconds or None for
    "until device.release"), ("error", message)."""

    def __init__(self, device: "FakeDevice", steps: list[tuple]) -> None:
        self.device = device
        self.steps = list(steps)
        self.t0: float | None = None
        self.delivered = 0
        self.burst_sent = 0

    def __enter__(self) -> "FakeRecorder":
        self.device.opened += 1
        return self

    def __exit__(self, *exc: object) -> bool:
        self.device.closed += 1
        return False

    def record(self, numframes: int) -> np.ndarray:
        while self.steps:
            kind = self.steps[0][0]
            if kind == "hang":
                seconds = self.steps[0][1]
                if seconds is None:
                    self.device.release.wait()
                else:
                    time.sleep(seconds)
                self.steps.pop(0)
                self.t0 = None  # what the driver held back is gone
                continue
            if kind == "error":
                message = self.steps.pop(0)[1]
                raise RuntimeError(message)
            if kind == "burst":
                _, seconds, value = self.steps[0]
                self.burst_sent += numframes
                if self.burst_sent >= seconds * SR:
                    self.steps.pop(0)
                    self.burst_sent = 0
                return np.full((numframes, 2), value, dtype=np.float32)
            _, seconds, value = self.steps[0]
            now = time.monotonic()
            if self.t0 is None:
                self.t0, self.delivered = now, 0
            due = self.t0 + (self.delivered + numframes) / SR
            if due > now:
                time.sleep(due - now)
            self.delivered += numframes
            if seconds is not None and self.delivered >= seconds * SR:
                self.steps.pop(0)
                self.t0 = None
            return np.full((numframes, 2), value, dtype=np.float32)
        return np.zeros((numframes, 2), dtype=np.float32)


class FakeDevice:
    """One plan per recorder() call; the last plan repeats. An exception plan fails the open."""

    id = "loop-1"
    name = "Speakers (Loopback)"
    isloopback = True

    def __init__(self, *plans: object) -> None:
        self.plans = list(plans)
        self.opened = 0
        self.closed = 0
        self.release = threading.Event()

    def recorder(self, samplerate: int, blocksize: int) -> FakeRecorder:
        assert samplerate == SR
        plan = self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
        if isinstance(plan, Exception):
            raise plan
        return FakeRecorder(self, plan)


def install(device: FakeDevice) -> None:
    speaker = types.SimpleNamespace(id="speaker-1", name="Speakers", isloopback=False)
    sys.modules["soundcard"] = types.SimpleNamespace(
        default_speaker=lambda: speaker,
        all_microphones=lambda include_loopback=False: [device],
    )


def make_config(**audio: object) -> dict:
    config = copy.deepcopy(DEFAULTS)
    config["audio"].update(
        {
            "source": "system",
            "system_device": "loop-1",
            "sample_rate": SR,
            "block_size": BLOCK,
            "input_gain": 1.0,
            "source_retry_delay_sec": 0.1,
            "source_max_restarts": 1,
            "system_silence_fill_sec": 0.2,
            "system_stall_restart_sec": 0.6,
        }
    )
    config["audio"].update(audio)
    return config


def consume(source: SystemAudioChunkSource, seconds: float) -> tuple[list[AudioChunk], list[str], float]:
    """The chunks, the capture state after each, and the wall time when the last one arrived."""
    chunks: list[AudioChunk] = []
    states: list[str] = []
    started = time.monotonic()
    last_at = 0.0
    for chunk in source.chunks(max_seconds=seconds):
        chunks.append(chunk)
        states.append(source.capture_state)
        last_at = time.monotonic() - started
    return chunks, states, last_at


def runs(chunks: list[AudioChunk]) -> list[tuple[str, float]]:
    """Consecutive chunks grouped by status: [(status, seconds), ...]."""
    grouped: list[tuple[str, float]] = []
    for chunk in chunks:
        if grouped and grouped[-1][0] == chunk.status:
            grouped[-1] = (chunk.status, grouped[-1][1] + chunk.duration_sec)
        else:
            grouped.append((chunk.status, chunk.duration_sec))
    return grouped


def assert_contiguous(chunks: list[AudioChunk]) -> None:
    expected = chunks[0].offset_sec
    for chunk in chunks:
        assert abs(chunk.offset_sec - expected) < 1e-9, (chunk.offset_sec, expected)
        expected = chunk.offset_sec + chunk.duration_sec


def assert_on_wall_time(source: SystemAudioChunkSource, elapsed: float, tolerance: float) -> None:
    clock = source.sample_count / SR
    assert abs(clock - elapsed) <= tolerance, (clock, elapsed)


def wait_for(condition, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def check_config() -> None:
    config = copy.deepcopy(DEFAULTS)
    assert config["audio"]["system_silence_fill_sec"] == 0.2
    assert config["audio"]["system_stall_restart_sec"] == 5.0
    validate_config(config)
    for key, value in (
        ("system_silence_fill_sec", 0.0),
        ("system_silence_fill_sec", 5.0),
        ("system_stall_restart_sec", 0.5),
        ("system_stall_restart_sec", 120.0),
    ):
        broken = copy.deepcopy(config)
        broken["audio"][key] = value
        try:
            validate_config(broken)
        except ValueError:
            continue
        raise AssertionError(f"audio.{key}={value} must be rejected")


def check_short_gap_is_filled_with_silence() -> None:
    device = FakeDevice([("data", 0.3, 0.5), ("hang", 0.45), ("data", None, 0.5)])
    install(device)
    source = SystemAudioChunkSource(make_config())
    chunks, _states, elapsed = consume(source, 1.3)
    grouped = runs(chunks)
    assert [status for status, _ in grouped] == ["", "silence_fill", ""], grouped
    filled = grouped[1][1]
    assert 0.25 <= filled <= 0.5, grouped
    assert abs(source.silence_filled_sec - filled) < 1e-9
    for chunk in chunks:
        if chunk.status == "silence_fill":
            assert not chunk.samples.any() and chunk.rms == 0.0
        else:
            assert np.allclose(chunk.samples, 0.5) and chunk.source_name == "system"
    assert_contiguous(chunks)
    assert_on_wall_time(source, elapsed, 0.2)
    assert source.restart_count == 0 and source.stall_restart_count == 0
    assert device.opened == 1 and source.capture_state == "capturing"
    assert wait_for(lambda: device.closed == 1)


def check_held_back_audio_never_runs_the_clock_ahead() -> None:
    # After each hang the recorder hands over 0.25 s it held back, all at once. Placed after the
    # filled silence it would put the clock 0.25 s further ahead of wall time with every hang.
    steps: list[tuple] = []
    for _ in range(3):
        steps += [("data", 0.3, 0.5), ("hang", 0.45), ("burst", 0.25, 0.9)]
    device = FakeDevice(steps + [("data", None, 0.5)])
    install(device)
    source = SystemAudioChunkSource(make_config())
    chunks: list[AudioChunk] = []
    ahead = []
    started = time.monotonic()
    for chunk in source.chunks(max_seconds=3.0):
        chunks.append(chunk)
        ahead.append(chunk.offset_sec + chunk.duration_sec - (time.monotonic() - started))
    elapsed = time.monotonic() - started
    assert max(ahead) <= 0.05, max(ahead)  # timer resolution; without the fix about 0.7 s
    statuses = [status for status, _ in runs(chunks)]
    assert statuses.count("silence_fill") == 3, runs(chunks)
    kept = sum(chunk.duration_sec for chunk in chunks if chunk.status == "" and np.allclose(chunk.samples, 0.9))
    assert kept <= 0.25, kept  # of 0.75 s held back: only what fit before wall time
    assert all(np.allclose(chunk.samples, 0.5) or np.allclose(chunk.samples, 0.9)
               for chunk in chunks if chunk.status == "")
    assert_contiguous(chunks)
    assert_on_wall_time(source, elapsed, 0.2)
    # Real-time audio after the burst was kept: the run ends on the steady 0.5 data.
    assert np.allclose(chunks[-1].samples, 0.5) and source.capture_state == "capturing"
    assert source.restart_count == 0 and device.opened == 1


def check_long_stall_reopens_the_recorder() -> None:
    device = FakeDevice(
        [("data", 0.3, 0.5), ("hang", None)],  # stuck in the driver until released
        [("data", None, 0.7)],
    )
    install(device)
    source = SystemAudioChunkSource(make_config())
    chunks, states, elapsed = consume(source, 2.2)
    grouped = runs(chunks)
    assert [status for status, _ in grouped] == ["", "silence_fill", ""], grouped
    assert grouped[1][1] >= 0.6, grouped
    values = [float(chunk.samples[0]) for chunk in chunks if chunk.status == ""]
    assert values[0] == 0.5 and values[-1] == np.float32(0.7), values[:3]
    assert "reopening" in states and states[-1] == "capturing"
    assert_contiguous(chunks)
    assert_on_wall_time(source, elapsed, 0.25)
    assert device.opened == 2
    assert source.restart_count == 1 and source.stall_restart_count == 1
    assert "멈춰" in source.last_error and "loopback stalled" in source.last_error
    assert source.stuck_readers == 1
    device.release.set()
    assert wait_for(lambda: source.stuck_readers == 0)
    assert wait_for(lambda: device.closed == 2)


def check_quiet_device_never_ends_the_source() -> None:
    # Every recorder hangs at once: far more reopens than source_max_restarts (0), no exception,
    # and the clock keeps running on silence.
    device = FakeDevice([("hang", None)])
    install(device)
    source = SystemAudioChunkSource(
        make_config(source_max_restarts=0, system_stall_restart_sec=0.4)
    )
    chunks, _states, elapsed = consume(source, 3.0)
    assert {chunk.status for chunk in chunks} == {"silence_fill"}
    assert_contiguous(chunks)
    assert_on_wall_time(source, elapsed, 0.25)
    # Waits double between reopens with no audio in between: 0.4 s, then 0.8 s, then 1.6 s.
    assert source.stall_restart_count == 2 and source.restart_count == 2, (
        source.stall_restart_count,
        source.restart_count,
    )
    assert device.opened == 3
    device.release.set()
    assert wait_for(lambda: source.stuck_readers == 0)


def check_stuck_readers_are_capped() -> None:
    device = FakeDevice([("hang", None)], [("data", None, 0.6)])
    install(device)
    source = SystemAudioChunkSource(make_config(system_stall_restart_sec=0.4))
    source.MAX_STUCK_READERS = 1
    timer = threading.Timer(1.3, device.release.set)
    timer.start()
    try:
        chunks, states, elapsed = consume(source, 2.2)
    finally:
        timer.cancel()
        device.release.set()
    assert "stuck" in states and states[-1] == "capturing", sorted(set(states))
    first_stuck = states.index("stuck")
    assert all(chunk.status == "silence_fill" for chunk in chunks[: first_stuck + 1])
    # No second recorder while the first was stuck in the driver; one once it came back.
    assert device.opened == 2
    assert [status for status, _ in runs(chunks)] == ["silence_fill", ""]
    assert_contiguous(chunks)
    assert_on_wall_time(source, elapsed, 0.25)


def check_errors_count_and_reset_when_audio_flows() -> None:
    # A device that cannot be opened ends the source after source_max_restarts retries.
    device = FakeDevice(RuntimeError("device gone"))
    install(device)
    source = SystemAudioChunkSource(make_config())
    chunks: list[AudioChunk] = []
    try:
        for chunk in source.chunks(max_seconds=5.0):
            chunks.append(chunk)
    except RuntimeError as exc:
        assert "recovery failed after 1 restarts: device gone" in str(exc)
    else:
        raise AssertionError("a device that never opens must end the source")
    assert source.restart_count == 2 and source.stall_restart_count == 0
    assert source.last_error == "device gone" and source.capture_state == "error"
    assert chunks and {chunk.status for chunk in chunks} == {"silence_fill"}

    # Errors separated by real audio never add up to the limit.
    device = FakeDevice(
        RuntimeError("busy"),
        [("data", 0.2, 0.5), ("error", "read failed")],
        [("data", None, 0.5)],
    )
    install(device)
    source = SystemAudioChunkSource(make_config())
    chunks, _states, elapsed = consume(source, 1.2)
    assert source.restart_count == 2 and source.last_error == "read failed"
    assert device.opened == 2 and source.capture_state == "capturing"
    assert_contiguous(chunks)
    assert_on_wall_time(source, elapsed, 0.25)


def check_close_stops_the_reader() -> None:
    device = FakeDevice([("data", None, 0.5)])
    install(device)
    source = SystemAudioChunkSource(make_config())
    stream = source.chunks()
    first = next(stream)
    assert first.status == "" and source.device_label == "Speakers (Loopback)"
    stream.close()
    assert wait_for(lambda: device.closed == 1)
    assert wait_for(
        lambda: not any(
            thread.name == "audio-system-reader" and thread.is_alive()
            for thread in threading.enumerate()
        )
    )


class FakeMicrophone:
    source_name = "microphone"
    restart_count = 0
    last_error = ""
    dropped_chunks = 0
    device_label = "Mic"

    def __init__(self) -> None:
        self.sample_count = 0

    def chunks(self, max_seconds=None, stop_event=None):
        started = time.monotonic()
        while not (stop_event is not None and stop_event.is_set()):
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                return
            time.sleep(0.02)
            samples = np.full(320, 0.1, dtype=np.float32)
            offset = self.sample_count / SR
            self.sample_count += samples.size
            yield AudioChunk(samples, 0.1, offset, samples.size / SR, source_name="microphone")


def check_dual_mode() -> None:
    device = FakeDevice([("data", 0.3, 0.5), ("hang", 0.5), ("data", None, 0.5)])
    install(device)
    system = SystemAudioChunkSource(make_config())
    dual = DualAudioChunkSource.__new__(DualAudioChunkSource)
    dual.config = {}
    dual.sample_rate = SR
    dual._sources = {"microphone": FakeMicrophone(), "system": system}
    dual._queue = queue.Queue(maxsize=512)
    dual._stop_event = threading.Event()
    dual._errors = {}
    dual._threads = []
    dual._dropped_merge_chunks = 0
    started = time.monotonic()
    chunks = list(dual.chunks(max_seconds=1.3))
    assert time.monotonic() - started < 2.5
    from_system = [chunk for chunk in chunks if chunk.source_name == "system"]
    from_microphone = [chunk for chunk in chunks if chunk.source_name == "microphone"]
    assert from_microphone and from_system
    assert [status for status, _ in runs(from_system)] == ["", "silence_fill", ""]
    assert_contiguous(from_system)
    status = dual.source_statuses()
    assert status["system"]["stall_restart_count"] == 0
    assert status["system"]["silence_filled_sec"] >= 0.2
    assert status["system"]["capture_state"] == "capturing"
    assert status["microphone"]["stall_restart_count"] == 0
    assert status["microphone"]["capture_state"] == ""
    assert dual.stall_restart_count == 0 and dual.silence_filled_sec >= 0.2
    assert not any(thread.is_alive() for thread in dual._threads)


def main() -> int:
    previous = sys.modules.get("soundcard")
    try:
        check_config()
        check_short_gap_is_filled_with_silence()
        check_held_back_audio_never_runs_the_clock_ahead()
        check_long_stall_reopens_the_recorder()
        check_quiet_device_never_ends_the_source()
        check_stuck_readers_are_capped()
        check_errors_count_and_reset_when_audio_flows()
        check_close_stops_the_reader()
        check_dual_mode()
    finally:
        if previous is None:
            sys.modules.pop("soundcard", None)
        else:
            sys.modules["soundcard"] = previous
    print("system_audio_stall_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
