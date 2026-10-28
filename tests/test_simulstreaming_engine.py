"""정확 우선 (SimulStreaming) engine without a GPU.

tests/simul_fake_server.py speaks the real server's protocol (text increments, is_final,
heartbeats) and fails the ways engines/simulstreaming/server.py can (exit codes, a stall, a
lagging GPU). Covers the caption assembler, the client's failure detection, the route's
start / switch / fallback / replay / retry policy, availability, the config migration and
the full v2_light pipeline with a crash in the middle of a stream.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from core.audio_capture import AudioChunk, AudioSegment  # noqa: E402
from core.config import DEFAULTS, load_config  # noqa: E402
from engines.simulstreaming import availability  # noqa: E402
from engines.simulstreaming.assembler import CaptionAssembler  # noqa: E402
from engines.simulstreaming.client import SimulStreamingClient, free_local_port  # noqa: E402
from engines.simulstreaming.route import SimulStreamingRoute  # noqa: E402

FAKE = ROOT / "tests" / "simul_fake_server.py"
SR = 16000
BLOCK = 1024


def fake_command(port: int, mode: str = "normal", **options: Any) -> list[str]:
    command = [sys.executable, str(FAKE), "--port", str(port), "--mode", mode, "--watch-stdin"]
    for key, value in options.items():
        command += ["--" + key.replace("_", "-"), str(value)]
    return command


def base_config(tmp: Path) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULTS)
    config["log"]["dir"] = str(tmp / "logs")
    models = tmp / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "large-v3.pt").write_bytes(b"not a real model")
    config["simulstreaming"].update({
        "model_dir": str(models),
        "connect_timeout_sec": 10.0,
        "stall_timeout_sec": 2.0,
        "max_restarts": 1,
        "retry_delay_sec": 1.0,
        "standby_timeout_sec": 5.0,
        "switch_timeout_sec": 3.0,
    })
    return config


def client_for(config: dict[str, Any], tmp: Path, mode: str = "normal", **options: Any) -> SimulStreamingClient:
    port = free_local_port()
    config = copy.deepcopy(config)
    config["simulstreaming"]["port"] = port
    return SimulStreamingClient(config, log_path=tmp / "server.log", command=fake_command(port, mode, **options))


def voiced(seconds: float, amplitude: float = 0.2, seed: int = 0) -> np.ndarray:
    return (np.random.default_rng(seed).uniform(-1, 1, int(seconds * SR)) * amplitude).astype(np.float32)


def silent(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.float32)


def wait_for(predicate: Any, timeout: float, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


# --------------------------------------------------------------------------- assembler


def check_assembler() -> None:
    def word(text: str, end: float) -> dict[str, Any]:
        return {"text": text, "end": end, "is_final": False, "words": [{"text": text, "end": end}]}

    a = CaptionAssembler(max_chars=28)
    out = a.push(word(" 오늘은", 0.9))
    assert [(o.kind, o.text, o.end_sec) for o in out] == [("pending", "오늘은", 0.9)], out
    out = a.push({"text": " 날씨가 좋네요.", "end": 2.0, "is_final": False,
                  "words": [{"text": " 날씨가", "end": 1.5}, {"text": " 좋네요.", "end": 1.9}]})
    assert [(o.kind, o.text) for o in out] == [("pending", "오늘은 날씨가 좋네요.")], out  # "." at the end waits
    out = a.push(word(" 그래서", 2.4))
    assert [(o.kind, o.text, o.end_sec) for o in out] == [
        ("confirmed", "오늘은 날씨가 좋네요.", 1.9), ("pending", "그래서", 2.4)], out
    out = a.push({"is_final": True, "end": 3.0})
    assert [(o.kind, o.text, o.end_sec) for o in out] == [("confirmed", "그래서", 2.4)], out
    assert a.segment == 1 and not a.text
    # Empty updates and repeated finals add nothing.
    assert a.push({"is_final": False}) == [] and a.push({"is_final": True}) == []

    # A long line is committed between words before it outgrows one caption line.
    b = CaptionAssembler(max_chars=12)
    pieces = []
    for index, text in enumerate([" 하나둘", " 셋넷다섯", " 여섯일곱", " 여덟아홉열"]):
        pieces += b.push(word(text, float(index)))
    confirmed = [p.text for p in pieces if p.kind == "confirmed"]
    assert confirmed and all(len(text) <= 12 for text in confirmed), confirmed
    assert " ".join(confirmed + [b.text]) == "하나둘 셋넷다섯 여섯일곱 여덟아홉열", (confirmed, b.text)

    # "3." may still become "3.5": no cut until a space follows.
    c = CaptionAssembler()
    c.push(word(" 버전 3.", 1.0))
    assert not [p for p in c.push(word("5", 1.2)) if p.kind == "confirmed"]
    assert c.text == "버전 3.5"
    # Word timings that do not add up to the text fall back to the message end.
    d = CaptionAssembler()
    out = d.push({"text": " 안녕", "end": 4.0, "is_final": False, "words": [{"text": "다른", "end": 1.0}]})
    assert out[0].end_sec == 4.0, out
    assert d.flush()[0].text == "안녕" and d.flush() == []


# --------------------------------------------------------------------------- server loop


def check_server_loop() -> None:
    """engines/simulstreaming/server.py's own loop with a fake online processor (no torch)."""
    import socket as socket_module
    import threading
    from types import SimpleNamespace

    from engines.simulstreaming import server as srv

    class FakeOnline:
        def __init__(self) -> None:
            self.audio: list[np.ndarray] = []
            self.calls = 0

        def init(self) -> None:
            self.audio = []

        def insert_audio_chunk(self, audio: np.ndarray) -> None:
            self.audio.append(audio)

        def process_iter(self) -> dict:
            self.calls += 1
            if self.calls == 2:
                return {"start": 0.1, "end": 0.5, "text": " 안녕", "tokens": [1, 2], "is_final": False,
                        "words": [{"start": 0.1, "end": 0.5, "text": " 안녕", "tokens": [1, 2]}]}
            return {}

    wave_samples = (np.sin(np.arange(16000) / 7.0) * 12000).astype("<i2")
    payload = wave_samples.tobytes()
    server_side, client_side = socket_module.socketpair()
    online = FakeOnline()
    args = SimpleNamespace(vac_chunk_size=0.04, heartbeat_sec=0.05, model_path="models/large-v3.pt",
                           frame_threshold=40, min_chunk_size=0.5)
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(srv.serve(server_side, online, args,
                                                                      srv.IterationWatchdog(0))))
    thread.start()
    for start in range(0, len(payload), 1001):  # odd sizes: reads end inside a sample
        client_side.sendall(payload[start:start + 1001])
        time.sleep(0.002)
    time.sleep(0.3)
    client_side.shutdown(socket_module.SHUT_WR)  # the app went away: serve() returns
    thread.join(timeout=5)
    server_side.close()  # main() closes the connection after serve()
    data = b""
    client_side.settimeout(5)
    while True:
        chunk = client_side.recv(65536)
        if not chunk:
            break
        data += chunk
    client_side.close()
    assert result == [srv.EXIT_OK], result
    lines = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
    assert lines[0]["type"] == "hello" and lines[0]["model"] == "large-v3", lines[0]
    heartbeats = [line for line in lines if line.get("type") == "heartbeat"]
    assert heartbeats and heartbeats[-1]["audio_sec"] <= 1.0, heartbeats[-1]
    texts = [line for line in lines if "text" in line]
    assert texts == [{"start": 0.1, "end": 0.5, "text": " 안녕", "is_final": False,
                      "emission_time": texts[0]["emission_time"],
                      "words": [{"start": 0.1, "end": 0.5, "text": " 안녕"}]}], texts
    received = np.concatenate(online.audio)
    expected = wave_samples.astype(np.float32) / 32768.0
    usable = len(received)
    assert usable >= 15000 and np.array_equal(received, expected[:usable]), "PCM16 alignment was lost"

    # A model update that hangs makes the process exit with the stall code.
    code = subprocess.run(
        [sys.executable, "-c", "import sys, time; sys.path.insert(0, sys.argv[1]);"
         "from engines.simulstreaming import server as s; w = s.IterationWatchdog(0.5); w.begin(); time.sleep(10)",
         str(ROOT)], timeout=20).returncode
    assert code == srv.EXIT_STALLED, code
    assert srv.is_out_of_memory(RuntimeError("CUDA out of memory. Tried to allocate 20.00 MiB"))
    assert not srv.is_out_of_memory(ValueError("Token mismatch"))


# --------------------------------------------------------------------------- client


def stream(client: SimulStreamingClient, audio: np.ndarray, *, realtime: float = 1.0) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    started = time.monotonic()
    for index in range(0, len(audio), BLOCK):
        client.send_audio(audio[index:index + BLOCK])
        target = started + (index + BLOCK) / SR / realtime
        time.sleep(max(0.0, target - time.monotonic()))
        messages += client.get_messages()
        if client.failure():
            break
    return messages


def check_client(tmp: Path) -> None:
    config = base_config(tmp)
    client = client_for(config, tmp)
    client.launch()
    try:
        assert wait_for(lambda: client.connected, 10), client.snapshot()
        messages = stream(client, np.concatenate([voiced(2.0), silent(1.0)]))
        messages += [m for _ in range(20) for m in (time.sleep(0.05) or client.get_messages())]
        texts = [m["text"] for m in messages if m.get("text")]
        assert len(texts) >= 3 and all(t.startswith(" ") for t in texts), messages
        assert any(m.get("is_final") for m in messages), messages
        assert client.failure() == "", client.snapshot()
        snap = client.snapshot()
        assert snap["connected"] and snap["heartbeat_age_sec"] < 1.0 and snap["lag_sec"] < 1.0, snap
        pid = snap["server_pid"]
    finally:
        client.close()
    assert not client.snapshot()["server_process_alive"], "close() must stop the server"
    assert pid

    def failure_of(mode: str, audio_sec: float = 0.0, setup: Any = None, timeout: float = 10.0, **options: Any) -> str:
        cfg = copy.deepcopy(config)
        if setup:
            setup(cfg)
        c = client_for(cfg, tmp, mode, **options)
        c.launch()
        try:
            if audio_sec:
                assert wait_for(lambda: c.connected or bool(c.failure()), 10), mode
                stream(c, voiced(audio_sec), realtime=2.0)
            assert wait_for(lambda: bool(c.failure()), timeout), (mode, c.snapshot())
            return c.failure()
        finally:
            c.close()
            assert not c.snapshot()["server_process_alive"], mode

    assert failure_of("runtime_missing") == "runtime_missing"
    assert failure_of("oom_start") == "out_of_memory"
    assert failure_of("no_cuda") == "no_cuda"
    assert failure_of("hang_start", setup=lambda c: c["simulstreaming"].update(connect_timeout_sec=1.5)) == "start_timeout"
    result = failure_of("crash_after", audio_sec=3.0, after_sec=1.0)
    assert result == "crashed", result
    assert failure_of("oom_after", audio_sec=3.0, after_sec=1.0) == "out_of_memory"
    assert failure_of("stall_after", audio_sec=3.0, after_sec=0.5, timeout=6.0) == "stalled"
    assert failure_of("close_after", audio_sec=3.0, after_sec=0.5, timeout=8.0) == "connection_lost"

    slow = client_for(config, tmp, "slow")
    slow.max_lag_sec = 0.5
    slow.lag_grace_sec = 0.5
    slow.launch()
    try:
        assert wait_for(lambda: slow.connected, 10)
        stream(slow, voiced(4.0), realtime=2.0)
        assert wait_for(lambda: bool(slow.failure()), 5), slow.snapshot()
        assert slow.failure() == "too_slow", slow.snapshot()
    finally:
        slow.close()


# --------------------------------------------------------------------------- route


class Timeline:
    """Feeds a route chunk by chunk in real time, the way run_v2_light does."""

    def __init__(self, route: SimulStreamingRoute) -> None:
        self.route = route
        self.offset = 0.0
        self.outputs: list[Any] = []

    def play(self, audio: np.ndarray, *, voiced_audio: bool) -> None:
        for index in range(0, len(audio), BLOCK):
            block = audio[index:index + BLOCK]
            chunk = AudioChunk(samples=block, rms=float(np.sqrt(np.mean(block ** 2))) if block.size else 0.0,
                               offset_sec=self.offset, duration_sec=len(block) / SR, source_name="microphone")
            self.route.feed(chunk, can_switch=not voiced_audio)
            self.offset += len(block) / SR
            self.outputs += self.route.poll()
            time.sleep(len(block) / SR)

    def idle(self, seconds: float) -> None:
        self.play(silent(seconds), voiced_audio=False)

    def captions(self, *, pending: bool | None = None) -> list[Any]:
        return [o for o in self.outputs if o.kind == "caption" and (pending is None or o.pending == pending)]

    def states(self) -> list[tuple[str, str]]:
        return [(o.status["state"], o.status["reason"]) for o in self.outputs if o.kind == "status"]


def route_with(config: dict[str, Any], tmp: Path, modes: list[str], **options: Any) -> SimulStreamingRoute:
    launches: list[str] = []
    models: list[str] = []

    def factory(cfg: dict[str, Any], *, model_path: Any = None, log_path: Any = None) -> SimulStreamingClient:
        mode = modes[min(len(launches), len(modes) - 1)]
        launches.append(mode)
        models.append(Path(str(model_path)).name)
        port = free_local_port()
        cfg = copy.deepcopy(cfg)
        cfg["simulstreaming"]["port"] = port
        return SimulStreamingClient(cfg, model_path=model_path, log_path=log_path,
                                    command=fake_command(port, mode, **options))

    route = SimulStreamingRoute(config, "microphone", log_dir=tmp, client_factory=factory,
                                status_provider=lambda cfg: {"available": True, "reason": ""},
                                gpu_probe=lambda: None)
    route.launches = launches  # type: ignore[attr-defined]
    route.launch_models = models  # type: ignore[attr-defined]
    return route


def check_route_normal(tmp: Path) -> None:
    config = base_config(tmp)
    route = route_with(config, tmp, ["normal"])
    timeline = Timeline(route)
    try:
        assert route.state == "waiting_standby" and not route.owns_captions
        timeline.idle(0.3)
        assert route.state == "waiting_standby", "the server waits for the faster-whisper standby model"
        route.standby_ready()
        assert wait_for(lambda: (timeline.idle(0.1) or route.state) == "active", 10), timeline.states()
        origin = route.origin_offset
        assert origin > 0 and route.owns_captions
        for _ in range(2):
            timeline.play(voiced(2.2), voiced_audio=True)  # four words per burst
            timeline.idle(1.2)
        route.hold_segment(AudioSegment(samples=voiced(1.0), start_offset_sec=origin + 0.2,
                                        end_offset_sec=origin + 1.2, peak_rms=0.2, voice_ratio=1.0))
        timeline.outputs += route.finish(graceful=True)
        pending = timeline.captions(pending=True)
        confirmed = timeline.captions(pending=False)
        assert pending and all(o.event.metadata["stable"] for o in pending), pending
        assert len(confirmed) >= 2, [o.event.text for o in confirmed]
        assert "오늘은 날씨가 정말 좋네요." in [o.event.text for o in confirmed], [o.event.text for o in confirmed]
        offsets = [o.event.audio_offset_sec for o in timeline.captions()]
        assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets), offsets
        assert all(offset >= origin for offset in offsets), (origin, offsets)
        assert all(o.event.engine == "simulstreaming" for o in timeline.captions())
        assert not [o for o in timeline.outputs if o.kind == "replay"], "held audio SimulStreaming covered is dropped"
        assert route.snapshot()["active_engine"] == "simulstreaming"
        assert ("active", "") in timeline.states()
    finally:
        route.close()
    assert route.state == "stopped" and route.client is None and route.notice() == ""


def check_route_crash_replay_retry(tmp: Path) -> None:
    config = base_config(tmp)
    route = route_with(config, tmp, ["crash_after", "normal", "normal"], after_sec=3.0)
    timeline = Timeline(route)
    try:
        route.standby_ready()
        assert wait_for(lambda: (timeline.idle(0.1) or route.state) == "active", 10), timeline.states()
        origin = route.origin_offset
        timeline.play(voiced(2.0), voiced_audio=True)  # captioned by SimulStreaming
        covered = route.covered_until
        assert covered > origin, route.snapshot()
        # faster-whisper finished two segments meanwhile: one SimulStreaming covered, one it did not.
        route.hold_segment(AudioSegment(samples=voiced(1.5), start_offset_sec=origin + 0.1,
                                        end_offset_sec=origin + 1.6, peak_rms=0.2, voice_ratio=1.0))
        late = AudioSegment(samples=voiced(3.5), start_offset_sec=covered - 0.5, end_offset_sec=covered + 3.0,
                            peak_rms=0.2, voice_ratio=1.0)
        route.hold_segment(late)
        timeline.play(voiced(1.5), voiced_audio=True)  # the fake exits after 3 s of audio
        assert wait_for(lambda: (timeline.idle(0.1) or route.state) == "fallback", 5), timeline.states()
        assert route.reason == "crashed" and not route.owns_captions
        replay = [segment for o in timeline.outputs if o.kind == "replay" for segment in o.segments]
        assert len(replay) == 1, replay
        assert replay[0].start_offset_sec > covered - 0.5 and replay[0].end_offset_sec == late.end_offset_sec, replay
        assert len(replay[0].samples) == round((replay[0].end_offset_sec - replay[0].start_offset_sec) * SR), replay
        # The segment still open at faster-whisper is cut where SimulStreaming's words ended.
        cut = route.trim_before
        assert cut is not None
        open_segment = AudioSegment(samples=voiced(3.0), start_offset_sec=cut - 1.0, end_offset_sec=cut + 2.0,
                                    peak_rms=0.2, voice_ratio=1.0)
        trimmed = route.trim_segment(open_segment)
        assert trimmed is not None and trimmed.start_offset_sec == cut, trimmed
        assert route.trim_segment(open_segment) is open_segment, "only the first segment is cut"
        assert "기본 엔진으로 자막을 이어갑니다" in route.notice() and "다시 시도" in route.notice()
        # Retry after retry_delay_sec: a new server, a new switch at the next pause.
        assert wait_for(lambda: (timeline.idle(0.1) or route.state) == "active", 12), timeline.states()
        assert route.restarts == 1 and route.launches == ["crash_after", "normal"]
        timeline.play(voiced(1.5), voiced_audio=True)
        timeline.idle(1.0)
        timeline.outputs += route.finish(graceful=True)
        states = timeline.states()
        assert ("fallback", "crashed") in states and states[-1][0] == "active", states
        offsets = [o.event.audio_offset_sec for o in timeline.captions()]
        assert offsets == sorted(offsets), offsets
    finally:
        route.close()


def check_route_retry_limit_and_unretryable(tmp: Path) -> None:
    config = base_config(tmp)
    config["simulstreaming"]["max_restarts"] = 1
    route = route_with(config, tmp, ["crash_after"], after_sec=0.5)
    timeline = Timeline(route)
    try:
        route.standby_ready()
        for _ in range(2):
            assert wait_for(lambda: (timeline.idle(0.1) or route.state) == "active", 12), timeline.states()
            timeline.play(voiced(1.0), voiced_audio=True)
            assert wait_for(lambda: (timeline.idle(0.1) or route.state) in {"fallback", "off"}, 5), timeline.states()
        assert route.state == "off" and route.restarts == 1, (route.state, route.restarts)
        assert route.launches == ["crash_after", "crash_after"]
        timeline.idle(1.5)
        assert route.state == "off" and len(route.launches) == 2, "no third start"
        assert "이번 방송에서는 기본 엔진을 계속 씁니다" in route.notice()
    finally:
        route.close()

    route = route_with(config, tmp, ["oom_start"])
    timeline = Timeline(route)
    try:
        route.standby_ready()
        assert wait_for(lambda: (timeline.idle(0.1) or route.state) == "off", 8), timeline.states()
        assert route.reason == "out_of_memory" and route.launches == ["oom_start"]
        timeline.idle(1.5)
        assert len(route.launches) == 1, "out of memory is not retried"
        assert "그래픽 메모리가 부족해" in route.notice()
    finally:
        route.close()


def check_route_lighter_model_and_internal_error(tmp: Path) -> None:
    config = base_config(tmp)
    (Path(config["simulstreaming"]["model_dir"]) / "large-v3-turbo.pt").write_bytes(b"x")
    config["simulstreaming"]["model"] = "auto"
    route = route_with(config, tmp, ["oom_start", "normal"])
    timeline = Timeline(route)
    try:
        route.standby_ready()
        assert wait_for(lambda: (timeline.idle(0.1) or route.state) == "active", 12), timeline.states()
        # "auto": large-v3 ran out of memory, so the retry used the lighter model.
        assert route.launch_models == ["large-v3.pt", "large-v3-turbo.pt"], route.launch_models
        assert route.model == "large-v3-turbo" and route.restarts == 1, route.snapshot()
        assert ("fallback", "out_of_memory") in timeline.states(), timeline.states()
        timeline.play(voiced(1.2), voiced_audio=True)
        origin = route.origin_offset
        uncovered = AudioSegment(samples=voiced(2.0), start_offset_sec=route.covered_until + 0.5,
                                 end_offset_sec=route.covered_until + 2.5, peak_rms=0.2, voice_ratio=1.0)
        route.hold_segment(uncovered)
        outputs = route.fail_internal(RuntimeError("bug"))
        assert route.state == "off" and route.reason == "internal_error" and route.client is None, route.snapshot()
        replay = [segment for o in outputs if o.kind == "replay" for segment in o.segments]
        assert replay and replay[0].end_offset_sec == uncovered.end_offset_sec, outputs
        assert origin <= replay[0].start_offset_sec
        assert [o.status["state"] for o in outputs if o.kind == "status"] == ["off"], outputs
    finally:
        route.close()

    config["simulstreaming"]["model"] = "large-v3"  # an explicit choice is not swapped for another model
    route = route_with(config, tmp, ["oom_start"])
    timeline = Timeline(route)
    try:
        route.standby_ready()
        assert wait_for(lambda: (timeline.idle(0.1) or route.state) == "off", 8), timeline.states()
        assert route.reason == "out_of_memory" and route.launch_models == ["large-v3.pt"]
    finally:
        route.close()


def check_route_stall(tmp: Path) -> None:
    config = base_config(tmp)
    config["simulstreaming"]["max_restarts"] = 0
    route = route_with(config, tmp, ["stall_after"], after_sec=1.0)
    timeline = Timeline(route)
    try:
        route.standby_ready()
        assert wait_for(lambda: (timeline.idle(0.1) or route.state) == "active", 10), timeline.states()
        timeline.play(voiced(1.6), voiced_audio=True)
        started = time.monotonic()
        assert wait_for(lambda: (timeline.idle(0.1) or route.state) == "off", 6), timeline.states()
        assert route.reason == "stalled" and time.monotonic() - started < 5.0
        assert "응답하지 않아" in route.notice()
    finally:
        route.close()


def check_route_unavailable_and_vram(tmp: Path) -> None:
    config = base_config(tmp)
    route = SimulStreamingRoute(config, "microphone", status_provider=lambda cfg: {
        "available": False, "reason": "runtime_missing"}, gpu_probe=lambda: None)
    assert route.state == "off" and route.reason == "runtime_missing" and not route.owns_captions
    outputs = route.poll()
    assert [o.status["state"] for o in outputs if o.kind == "status"] == ["off"], outputs
    assert "추가 구성요소" in route.notice() and route.snapshot()["active_engine"] == "baseline"
    route.close()

    config["simulstreaming"]["model"] = "auto"
    route = SimulStreamingRoute(config, "microphone", status_provider=lambda cfg: {"available": True},
                                gpu_probe=lambda: {"total_mib": 16380, "free_mib": 3000})
    route.standby_ready()
    route.poll()
    assert route.state == "off" and route.reason == "not_enough_vram", route.snapshot()
    route.close()


# --------------------------------------------------------------------------- availability / config


def make_runtime(root: Path, models: tuple[str, ...] = ("large-v3.pt",), torch: bool = True) -> Path:
    python = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"")
    if torch:
        site = root / ".venv" / "Lib" / "site-packages" / "torch"
        if os.name != "nt":
            site = root / ".venv" / "lib" / "python3.12" / "site-packages" / "torch"
        site.mkdir(parents=True, exist_ok=True)
        (site / "__init__.py").write_text("", encoding="utf-8")
    (root / "models").mkdir(parents=True, exist_ok=True)
    for name in models:
        (root / "models" / name).write_bytes(b"x")
    return python


def check_availability(tmp: Path) -> None:
    config = copy.deepcopy(DEFAULTS)
    config["simulstreaming"]["python_executable"] = str(tmp / "nowhere" / "python.exe")
    status = availability.engine_status(config, gpu_probe=None)
    assert not status["available"] and status["reason"] == "runtime_missing", status
    assert status["message"] == "추가 구성요소가 필요합니다 — 다음 업데이트에서 내려받기 지원 예정"

    no_torch = make_runtime(tmp / "rt_no_torch", torch=False)
    config["simulstreaming"]["python_executable"] = str(no_torch)
    assert availability.engine_status(config, gpu_probe=None)["reason"] == "runtime_missing"

    no_model = make_runtime(tmp / "rt_no_model", models=())
    config["simulstreaming"]["python_executable"] = str(no_model)
    assert availability.engine_status(config, gpu_probe=None)["reason"] == "model_missing"

    python = make_runtime(tmp / "rt", models=("large-v3.pt", "large-v3-turbo.pt"))
    config["simulstreaming"]["python_executable"] = str(python)
    status = availability.engine_status(config, gpu_probe=None)
    assert status["available"] and status["models"] == {"large-v3": True, "large-v3-turbo": True}, status
    small = availability.engine_status(config, gpu_probe=lambda: {"total_mib": 12288, "free_mib": 9000})
    assert small["available"] and small["models"] == {"large-v3": False, "large-v3-turbo": True}, small
    tiny = availability.engine_status(config, gpu_probe=lambda: {"total_mib": 8192, "free_mib": 7000})
    assert tiny["reason"] == "gpu_too_small", tiny

    runtime = availability.resolve_runtime(config)
    assert runtime["runtime_root"] == tmp / "rt" and runtime["model_dir"] == tmp / "rt" / "models"
    assert availability.choose_model(config, runtime, 12000)[0] == "large-v3"
    assert availability.choose_model(config, runtime, 8000)[0] == "large-v3-turbo"
    assert availability.choose_model(config, runtime, None)[0] == "large-v3"
    assert availability.choose_model(config, runtime, 4000) == ("", None, "not_enough_vram")
    assert availability.choose_model(config, runtime, 12000, {"large-v3"})[0] == "large-v3-turbo"
    assert availability.choose_model(config, runtime, 12000, {"large-v3", "large-v3-turbo"}) == (
        "", None, "out_of_memory")
    config["simulstreaming"]["model"] = "large-v3-turbo"
    assert availability.choose_model(config, runtime, 12000)[0] == "large-v3-turbo"
    config["simulstreaming"]["model"] = "large-v3"
    assert availability.choose_model(config, runtime, 8000) == ("", None, "not_enough_vram")

    overview = availability.engine_overview(config, {"requested_engine": "baseline", "model_setting": "auto"},
                                            gpu_probe=None)
    assert overview["selected"] == "baseline" and not overview["restart_required"]
    accurate = next(option for option in overview["options"] if option["id"] == "simulstreaming")
    assert accurate["available"] and accurate["label_ko"] == "정확 우선"
    config["streaming"]["engine"] = "simulstreaming"
    overview = availability.engine_overview(config, {"requested_engine": "baseline"}, gpu_probe=None)
    assert overview["restart_required"], "the running engine differs from the saved choice"


def check_config_migration(tmp: Path) -> None:
    path = tmp / "config.json"
    frozen = copy.deepcopy(DEFAULTS)
    frozen["config_schema"] = 3
    frozen["simulstreaming"].update({"min_chunk_size": 0.8, "vac_chunk_size": 0.32, "frame_threshold": 25,
                                     "port": 43007, "connect_timeout_sec": 90.0})
    path.write_text(json.dumps(frozen), encoding="utf-8")
    migrated = load_config(path)["simulstreaming"]
    assert (migrated["min_chunk_size"], migrated["vac_chunk_size"], migrated["frame_threshold"],
            migrated["port"], migrated["connect_timeout_sec"]) == (0.5, 0.04, 40, 0, 180.0), migrated
    custom = copy.deepcopy(frozen)
    custom["simulstreaming"].update({"frame_threshold": 30, "min_chunk_size": 1.0})
    path.write_text(json.dumps(custom), encoding="utf-8")
    kept = load_config(path)["simulstreaming"]
    assert kept["frame_threshold"] == 30 and kept["min_chunk_size"] == 1.0, kept
    assert DEFAULTS["streaming"]["engine"] == "baseline", "기본 stays the default engine"
    assert DEFAULTS["simulstreaming"]["frame_threshold"] == 40 and DEFAULTS["simulstreaming"]["min_chunk_size"] == 0.5


# --------------------------------------------------------------------------- full pipeline


class FakeSource:
    source_name = "microphone"
    dropped_chunks = 0
    restart_count = 0
    last_error = ""
    input_gain = 1.0
    device_label = "fake"

    def __init__(self, audio: np.ndarray) -> None:
        self.audio = audio

    def chunks(self, max_seconds: float | None = None):
        started = time.monotonic()
        for index in range(0, len(self.audio), BLOCK):
            block = self.audio[index:index + BLOCK]
            target = started + (index + BLOCK) / SR
            time.sleep(max(0.0, target - time.monotonic()))
            yield AudioChunk(samples=block, rms=float(np.sqrt(np.mean(block ** 2))), offset_sec=index / SR,
                             duration_sec=len(block) / SR, source_name="microphone")


def check_pipeline(tmp: Path) -> None:
    import main
    from core.stt_process_worker import _EchoEngine
    from core.stt_worker import SttWorker

    config = base_config(tmp)
    config["archive"]["db_path"] = str(tmp / "transcripts.sqlite3")
    config["archive"]["recap"]["provider"] = "extractive"
    config["defense"]["blocklist_path"] = str(tmp / "blocklist.txt")
    config["defense"]["whitelist_path"] = str(tmp / "whitelist.txt")
    config["slang"]["dict_path"] = str(tmp / "slang.json")
    config["vad"]["engine"] = "energy"
    config["audio"]["source"] = "microphone"
    config["streaming"]["engine"] = "simulstreaming"
    config["streaming"]["worker_mode"] = "thread"
    parts = [silent(1.0)]
    for index in range(5):
        parts += [voiced(2.0, seed=index), silent(1.2)]
    audio = np.concatenate(parts)
    routes: list[SimulStreamingRoute] = []

    def make_route(cfg: dict[str, Any], source_names: tuple[str, ...], log_dir: Any) -> SimulStreamingRoute:
        route = route_with(cfg, Path(log_dir), ["crash_after", "normal"], after_sec=5.0)
        routes.append(route)
        return route

    originals = (main.create_audio_chunk_source, main.create_and_start_stt_worker, main.create_engine_route)
    main.create_audio_chunk_source = lambda cfg: FakeSource(audio)
    main.create_and_start_stt_worker = lambda cfg, engine: (_started(SttWorker(_EchoEngine(cfg))), "")
    main.create_engine_route = make_route
    try:
        code = main.run_v2_light(config, None, None, False, False, False)
    finally:
        main.create_audio_chunk_source, main.create_and_start_stt_worker, main.create_engine_route = originals
    assert code == 0, code
    session = sorted((tmp / "logs").glob("*/session.jsonl"))[-1]
    rows = [json.loads(line) for line in session.read_text(encoding="utf-8").splitlines() if line.strip()]
    states = [(row["metadata"].get("state"), row["metadata"].get("reason")) for row in rows
              if row["event_type"] == "engine_status" and row["engine"] == "simulstreaming"]
    assert ("active", "") in states and ("fallback", "crashed") in states, states
    assert states.index(("fallback", "crashed")) < len(states) - 1 and states[-1][0] == "active", states
    simul = [row for row in rows if row["event_type"] == "caption_confirmed" and row["engine"] == "simulstreaming"]
    pending = [row for row in rows if row["event_type"] == "caption_pending" and row["engine"] == "simulstreaming"]
    echo = [row for row in rows if row["event_type"] == "caption_confirmed" and row["engine"] == "echo"]
    assert simul and pending and all(row["metadata"].get("stable") for row in pending), (len(simul), len(pending))
    assert echo, "faster-whisper (echo) must caption while SimulStreaming is down"
    assert all(row["metadata"]["audio_source"] == "microphone" for row in simul + echo)
    assert not [row for row in rows if row["event_type"] == "engine_error"], "no engine_error rows"
    summary = [row for row in rows if row["event_type"] == "engine_status" and row["text"] == "v2_light session summary"]
    assert summary and summary[-1]["metadata"]["caption_engine"]["fallbacks"] >= 1, summary
    assert not routes[0].client, "the route closed its server"
    # The archive got SimulStreaming captions as well (recaps read the archive).
    from core.transcript_archive import TranscriptArchive

    archive = TranscriptArchive.from_config(config)
    session_id = session.parent.name
    engines = {caption["engine"] for caption in archive.get_captions(session_id, limit=None)}
    assert {"simulstreaming", "echo"} <= engines, engines


def _started(worker: Any) -> Any:
    worker.start()
    return worker


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        tmp = Path(directory)
        check_assembler()
        check_server_loop()
        print("assembler/server loop ok", flush=True)
        check_availability(tmp / "availability")
        check_config_migration(tmp)
        print("availability/config ok", flush=True)
        check_client(tmp / "client")
        print("client ok", flush=True)
        check_route_unavailable_and_vram(tmp / "unavailable")
        check_route_normal(tmp / "normal")
        check_route_crash_replay_retry(tmp / "crash")
        check_route_retry_limit_and_unretryable(tmp / "limit")
        check_route_stall(tmp / "stall")
        check_route_lighter_model_and_internal_error(tmp / "lighter")
        print("route ok", flush=True)
        check_pipeline(tmp / "pipeline")
        print("pipeline ok", flush=True)
    print("simulstreaming_engine_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
