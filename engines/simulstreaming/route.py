"""정확 우선 (SimulStreaming) captions for one audio source inside the v2_light pipeline.

The faster-whisper pipeline keeps running underneath: its scheduler still sees every chunk
(VAD, segment boundaries) and its worker keeps the model loaded. The route only decides
who captions the source:

  waiting_standby -> starting -> ready -> active      (SimulStreaming captions the source)
  starting | ready | active  -- failure -->  fallback -- retry delay --> starting
                                         or  off       (faster-whisper for the rest of the run)

* The server starts after the faster-whisper standby model has loaded, so a GPU that is
  too small fails here, not in the middle of a sentence.
* The switch to SimulStreaming waits for a pause (no segment in progress, no final job in
  the faster-whisper queue), so no sentence is split between the engines.
* While active, faster-whisper's finished segments are held, not transcribed. When
  SimulStreaming fails (exit, stall, lag, out of memory), the text on the growing line is
  committed and every held segment it had not captioned yet is handed back to
  faster-whisper (cut where SimulStreaming's last word ended), so a stall loses nothing.
* Retries are bounded (simulstreaming.max_restarts with growing delays); missing files, no
  CUDA and too little GPU memory are not retried.
"""
from __future__ import annotations

import collections
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.audio_capture import AudioSegment
from core.events import CaptionEvent
from engines.simulstreaming.assembler import CaptionAssembler
from engines.simulstreaming.availability import (
    choose_model,
    engine_status,
    query_gpu_memory,
    resolve_runtime,
)
from engines.simulstreaming.client import SimulStreamingClient

NON_RETRYABLE = {
    "source_missing",
    "runtime_missing",
    "model_missing",
    "gpu_too_small",
    "not_enough_vram",
    "no_cuda",
    "out_of_memory",
}

# What the admin page tells the streamer. The reason code goes to the logs.
REASON_TEXT_KO = {
    "source_missing": "프로그램 파일 일부가 없어",
    "runtime_missing": "추가 구성요소가 없어",
    "model_missing": "음성 인식 모델 파일이 없어",
    "gpu_too_small": "그래픽카드 메모리가 부족해",
    "not_enough_vram": "그래픽 메모리가 부족해",
    "no_cuda": "그래픽카드(CUDA)를 사용할 수 없어",
    "out_of_memory": "그래픽 메모리가 부족해",
    "start_failed": "정확 우선 엔진을 시작하지 못해",
    "start_timeout": "정확 우선 엔진 준비가 너무 오래 걸려",
    "crashed": "정확 우선 엔진이 예기치 않게 꺼져",
    "stalled": "정확 우선 엔진이 응답하지 않아",
    "too_slow": "정확 우선 엔진이 음성을 따라가지 못해",
    "connection_lost": "정확 우선 엔진과 연결이 끊겨",
    "internal_error": "정확 우선 엔진 처리 중 오류가 나서",
}
STATE_TEXT_KO = {
    "waiting_standby": "기본 엔진으로 자막을 내면서 정확 우선 엔진을 준비합니다.",
    "starting": "기본 엔진으로 자막을 내면서 정확 우선 엔진을 불러오는 중입니다.",
    "ready": "정확 우선 엔진 준비 완료 — 말이 잠깐 멈추면 전환합니다.",
    "active": "정확 우선 엔진으로 자막을 만드는 중입니다.",
}


@dataclass
class RouteOutput:
    kind: str  # "caption" | "status" | "replay"
    event: CaptionEvent | None = None
    pending: bool = False
    segments: list[AudioSegment] = field(default_factory=list)
    status: dict[str, Any] = field(default_factory=dict)
    text: str = ""


class SimulStreamingRoute:
    def __init__(
        self,
        config: dict[str, Any],
        source_name: str,
        *,
        log_dir: str | Path | None = None,
        client_factory: Callable[..., Any] | None = None,
        status_provider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        gpu_probe: Callable[[], dict[str, int] | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.source_name = source_name
        settings = config.get("simulstreaming", {}) or {}
        self.sample_rate = int(config.get("audio", {}).get("sample_rate", 16000))
        self.model_setting = str(settings.get("model", "large-v3-turbo") or "large-v3-turbo")
        self.max_restarts = max(0, int(settings.get("max_restarts", 2)))
        self.retry_delay_sec = max(1.0, float(settings.get("retry_delay_sec", 30.0)))
        self.standby_timeout_sec = max(0.0, float(settings.get("standby_timeout_sec", 90.0)))
        self.switch_timeout_sec = max(1.0, float(settings.get("switch_timeout_sec", 20.0)))
        self.max_chars = int(config.get("caption", {}).get("max_chars_per_line", 28))
        # A segment that begins inside the VAD pre-roll before the switch still belongs to us.
        self.claim_tolerance_sec = float(config.get("audio", {}).get("pre_roll_sec", 0.3)) + 0.15
        self.min_replay_sec = float(config.get("audio", {}).get("min_segment_sec", 0.4))
        self._log_path = Path(log_dir) / "simulstreaming_server.log" if log_dir else None
        self._client_factory = client_factory or SimulStreamingClient
        self._gpu_probe = gpu_probe if gpu_probe is not None else (lambda: query_gpu_memory(max_age_sec=0.0))
        self._clock = clock
        self._created_at = clock()
        self._standby_ready = False
        self.state = "waiting_standby"
        self.reason = ""
        self.restarts = 0
        self.fallbacks = 0
        self.model = ""
        self.client: Any | None = None
        self.assembler: CaptionAssembler | None = None
        self.origin_offset = 0.0  # source-clock offset of the first chunk this server got
        self.covered_until = 0.0  # source-clock end of the last word SimulStreaming emitted
        self.trim_before: float | None = None
        self._held: collections.deque[AudioSegment] = collections.deque()
        self._last_offset = 0.0
        self._state_since = self._created_at
        self._retry_at: float | None = None
        self._outputs: list[RouteOutput] = []
        self._out_of_memory_models: set[str] = set()
        self.confirmed_count = 0
        self.replayed_segments = 0
        status = (status_provider or engine_status)(config)
        if not status.get("available"):
            self._fallback(str(status.get("reason") or "runtime_missing"), retry=False)

    # ------------------------------------------------------------------ queries

    @property
    def owns_captions(self) -> bool:
        return self.state == "active"

    def claims(self, segment: AudioSegment) -> bool:
        """Whether a finished faster-whisper segment of this source is ours to caption."""
        return self.state == "active" and segment.start_offset_sec >= self.origin_offset - self.claim_tolerance_sec

    def snapshot(self) -> dict[str, Any]:
        client = self.client.snapshot() if self.client is not None else {}
        retry_in = None
        if self._retry_at is not None:
            retry_in = max(0.0, round(self._retry_at - self._clock(), 1))
        return {
            "requested_engine": "simulstreaming",
            "active_engine": "simulstreaming" if self.state == "active" else "baseline",
            "state": self.state,
            "reason": self.reason,
            "model": self.model,
            "model_setting": self.model_setting,
            "audio_source": self.source_name,
            "restarts": self.restarts,
            "max_restarts": self.max_restarts,
            "fallbacks": self.fallbacks,
            "retry_in_sec": retry_in,
            "confirmed_captions": self.confirmed_count,
            "replayed_segments": self.replayed_segments,
            "lag_sec": client.get("lag_sec"),
            "heartbeat_age_sec": client.get("heartbeat_age_sec"),
            "server_pid": client.get("server_pid"),
            "server_max_iter_sec": client.get("server_max_iter_sec"),
            "notice": self.notice(),
        }

    def notice(self) -> str:
        if self.state == "stopped":
            return ""
        if self.state in STATE_TEXT_KO and not self.reason:
            return STATE_TEXT_KO[self.state]
        cause = REASON_TEXT_KO.get(self.reason, "정확 우선 엔진에 문제가 생겨")
        if self.state == "off":
            return f"{cause} 기본 엔진으로 자막을 이어갑니다. 이번 방송에서는 기본 엔진을 계속 씁니다."
        if self.state == "fallback":
            return f"{cause} 기본 엔진으로 자막을 이어갑니다. 잠시 후 정확 우선 엔진을 다시 시도합니다."
        if self.state == "active":
            return STATE_TEXT_KO["active"]
        return f"{cause} 기본 엔진으로 자막을 이어가는 중입니다. 정확 우선 엔진을 다시 준비합니다."

    # ------------------------------------------------------------------ driving

    def standby_ready(self) -> None:
        """The faster-whisper standby model answered its warm-up job."""
        self._standby_ready = True

    def feed(self, chunk: Any, *, can_switch: bool) -> None:
        """Called for every chunk of this source, after the scheduler's actions for it."""
        now = self._clock()
        if self.state == "ready" and (can_switch or now - self._state_since >= self.switch_timeout_sec):
            self._activate(chunk.offset_sec, forced=not can_switch)
        if self.state == "active" and self.client is not None:
            self.client.send_audio(chunk.samples)

    def hold_segment(self, segment: AudioSegment) -> None:
        self._held.append(segment)
        self._prune_held()

    def trim_segment(self, segment: AudioSegment) -> AudioSegment | None:
        """After a fallback, faster-whisper must not repeat what SimulStreaming already showed."""
        if self.trim_before is None:
            return segment
        cut = self.trim_before
        if segment.end_offset_sec <= cut:
            return None
        self.trim_before = None
        return self._cut_segment(segment, cut)

    def poll(self) -> list[RouteOutput]:
        now = self._clock()
        if self.state == "waiting_standby":
            if self._standby_ready or now - self._created_at >= self.standby_timeout_sec:
                self._launch()
        elif self.state == "fallback":
            if self._retry_at is not None and now >= self._retry_at:
                self.restarts += 1
                self._retry_at = None
                self._launch()
        elif self.state in {"starting", "ready", "active"} and self.client is not None:
            if self.state == "active":
                self._drain_messages()
            reason = self.client.failure()
            if reason:
                self._fallback(reason, retry=True)
            elif self.state == "starting" and self.client.connected:
                self._set_state("ready")
        outputs, self._outputs = self._outputs, []
        return outputs

    def finish(self, *, graceful: bool, drain_sec: float = 6.0) -> list[RouteOutput]:
        """End of the run: let the server finish the last words, then commit the line."""
        if self.state == "active" and self.client is not None and graceful:
            import numpy as np

            self.client.send_audio(np.zeros(self.sample_rate, dtype=np.float32))  # VAC closes the segment
            deadline = self._clock() + drain_sec
            quiet_since = self._clock()
            while self._clock() < deadline and not self.client.failure():
                messages = self.client.get_messages(10_000)
                if messages:
                    self._handle_messages(messages)
                    quiet_since = self._clock()
                elif self._clock() - quiet_since > 1.5 and self.assembler is not None and not self.assembler.text:
                    break
                time.sleep(0.05)
        if self.state == "active":
            reason = self.client.failure() if self.client is not None else ""
            if reason:
                self._fallback(reason, retry=False)  # still hands uncaptioned audio to faster-whisper
            else:
                self._drain_messages()
                self._commit_line()
        outputs, self._outputs = self._outputs, []
        return outputs

    def fail_internal(self, exc: BaseException) -> list[RouteOutput]:
        """A bug in this route must not stop captions: fall back for the rest of the run."""
        try:
            self._fallback("internal_error", retry=False, error=type(exc).__name__)
        except Exception:
            self.state = "off"
            self.reason = "internal_error"
            if self.client is not None:
                try:
                    self.client.close()
                except Exception:
                    pass
                self.client = None
        outputs, self._outputs = self._outputs, []
        return outputs

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.state not in {"off", "fallback"}:
            self.state = "stopped"  # the run ended normally

    # ------------------------------------------------------------------ transitions

    def _set_state(self, state: str, **extra: Any) -> None:
        self.state = state
        self._state_since = self._clock()
        status = {
            "state": state,
            "reason": self.reason,
            "engine": "simulstreaming",
            "active_engine": "simulstreaming" if state == "active" else "baseline",
            "model": self.model,
            "audio_source": self.source_name,
            "restarts": self.restarts,
            "fallbacks": self.fallbacks,
            **extra,
        }
        self._outputs.append(RouteOutput(kind="status", status=status, text=f"caption engine {state}"))

    def _launch(self) -> None:
        runtime = resolve_runtime(self.config)
        gpu = self._gpu_probe() if self._gpu_probe is not None else None
        free_mib = gpu.get("free_mib") if gpu else None
        model, model_path, problem = choose_model(self.config, runtime, free_mib, self._out_of_memory_models)
        if problem:
            self._fallback(problem, retry=False, free_mib=free_mib)
            return
        self.model = model
        self.client = self._client_factory(self.config, model_path=model_path, log_path=self._log_path)
        self.client.launch()
        self.reason = ""
        self._set_state("starting", free_mib=free_mib)

    def _activate(self, offset: float, *, forced: bool) -> None:
        self.origin_offset = float(offset)
        self.assembler = CaptionAssembler(self.max_chars)
        self._held.clear()
        self.trim_before = None
        self.covered_until = max(self.covered_until, self.origin_offset)
        self._set_state("active", forced_switch=forced)

    def _fallback(self, reason: str, *, retry: bool, **extra: Any) -> None:
        was_active = self.state == "active"
        if was_active:
            self._drain_messages()
            self._commit_line()
        replay: list[AudioSegment] = []
        if was_active:
            cut = max(self.covered_until, self._last_offset) + 0.05
            for segment in sorted(self._held, key=lambda item: item.start_offset_sec):
                trimmed = self._cut_segment(segment, cut) if segment.end_offset_sec > cut else None
                if trimmed is not None:
                    replay.append(trimmed)
            self._held.clear()
            self.trim_before = cut  # the segment still in progress at faster-whisper
        if self.client is not None:
            if reason in {"stalled", "too_slow"}:
                self.client.kill()  # a wedged server would only exit on its own watchdog
            self.client.close()
            self.client = None
        self.reason = reason
        self.fallbacks += 1 if was_active or self.state in {"starting", "ready"} else 0
        retryable = reason not in NON_RETRYABLE
        if reason == "out_of_memory" and self.model == "large-v3":
            # The advanced model did not fit after all: continue with turbo rather than without.
            self._out_of_memory_models.add(self.model)
            runtime = resolve_runtime(self.config)
            retryable = any(name not in self._out_of_memory_models for name in runtime["model_files"])
        can_retry = retry and retryable and self.restarts < self.max_restarts
        if can_retry:
            self._retry_at = self._clock() + self.retry_delay_sec * (2 ** self.restarts)
            self._set_state("fallback", was_active=was_active, **extra)
        else:
            self._retry_at = None
            self._set_state("off", was_active=was_active, **extra)
        if replay:
            self.replayed_segments += len(replay)
            self._outputs.append(RouteOutput(kind="replay", segments=replay))

    # ------------------------------------------------------------------ captions

    def _drain_messages(self) -> None:
        if self.client is None or self.assembler is None:
            return
        messages = self.client.get_messages(10_000)
        if messages:
            self._handle_messages(messages)

    def _handle_messages(self, messages: list[dict[str, Any]]) -> None:
        assert self.assembler is not None
        for message in messages:
            for piece in self.assembler.push(message):
                self._emit(piece.kind, piece.text, piece.end_sec, piece.segment, message, piece.start_sec)
        self._prune_held()

    def _commit_line(self) -> None:
        if self.assembler is None:
            return
        for piece in self.assembler.flush():
            self._emit(piece.kind, piece.text, piece.end_sec, piece.segment, {}, piece.start_sec)

    def _emit(
        self,
        kind: str,
        text: str,
        end_sec: float,
        segment: int,
        message: dict[str, Any],
        start_sec: float | None = None,
    ) -> None:
        if not text:
            return
        offset = max(self.origin_offset + float(end_sec), self._last_offset + 0.001)
        self._last_offset = offset
        self.covered_until = max(self.covered_until, offset)
        pending = kind == "pending"
        if not pending:
            self.confirmed_count += 1
        metadata = {
            "simulstreaming_segment": segment,
            "simulstreaming_end_sec": round(float(end_sec), 3),
            "simulstreaming_emission_time": message.get("emission_time"),
            "engine_model": self.model,
            # The line's audio on the source clock: run_v2_light measures how much of it the
            # app's voice detector heard as speech (DefenseFilter's speech_share).
            "line_end_offset_sec": round(self.origin_offset + float(end_sec), 3),
        }
        if start_sec is not None and float(start_sec) <= float(end_sec):
            metadata["line_start_offset_sec"] = round(self.origin_offset + float(start_sec), 3)
        if pending:
            metadata["stable"] = True  # never revised: the overlay shows it in the confirmed style
        event = CaptionEvent(
            event_type="caption_confirmed",
            text=text,
            confidence=1.0,
            audio_offset_sec=round(offset, 3),
            engine="simulstreaming",
            metadata=metadata,
        )
        self._outputs.append(RouteOutput(kind="caption", event=event, pending=pending))

    # ------------------------------------------------------------------ segments

    def _prune_held(self) -> None:
        horizon = self.covered_until
        while self._held and self._held[0].end_offset_sec <= horizon:
            self._held.popleft()
        while len(self._held) > 64:  # a minute of speech or more; older audio cannot be replayed usefully
            self._held.popleft()

    def _cut_segment(self, segment: AudioSegment, cut: float) -> AudioSegment | None:
        if segment.start_offset_sec >= cut:
            return segment
        skip = int(round((cut - segment.start_offset_sec) * self.sample_rate))
        samples = segment.samples[skip:]
        duration = len(samples) / self.sample_rate
        if duration < self.min_replay_sec:
            return None
        return AudioSegment(
            samples=samples,
            start_offset_sec=cut,
            end_offset_sec=segment.end_offset_sec,
            peak_rms=segment.peak_rms,
            voice_ratio=segment.voice_ratio,
        )
