from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

from core.audio_capture import AudioChunk, AudioSegment
from core.ring_buffer import AudioRingBuffer, AudioWindow
from core.vad import create_vad_segmenter


StreamingActionType = Literal["pending_window", "confirmed_segment", "discarded_segment"]


class SpeechTimeline:
    """The voice detector's verdict for every chunk of one source over the last minutes.

    speech_share() is the part of a time span heard as speech. The 정확 우선 engine reports no
    Whisper confidence values, so DefenseFilter uses this share to tell a "감사합니다." said by
    the streamer from one invented over music or game sound.
    """

    def __init__(self, max_sec: float = 120.0) -> None:
        self.max_sec = float(max_sec)
        self._spans: deque[tuple[float, float, bool]] = deque()

    def add(self, start_sec: float, end_sec: float, is_voice: bool) -> None:
        self._spans.append((float(start_sec), float(end_sec), bool(is_voice)))
        while self._spans and end_sec - self._spans[0][1] > self.max_sec:
            self._spans.popleft()

    def speech_share(self, start_sec: float, end_sec: float) -> float | None:
        """Voiced part of [start_sec, end_sec], or None when no chunk of it was seen."""
        if end_sec <= start_sec:
            return None
        voiced = covered = 0.0
        for span_start, span_end, is_voice in reversed(self._spans):
            if span_end <= start_sec:
                break
            overlap = min(span_end, end_sec) - max(span_start, start_sec)
            if overlap <= 0:
                continue
            covered += overlap
            if is_voice:
                voiced += overlap
        if covered <= 0:
            return None
        return voiced / covered

    def share_around(
        self,
        start_sec: float,
        end_sec: float,
        *,
        margin_sec: float = 0.2,
        min_span_sec: float = 0.6,
    ) -> float | None:
        """speech_share() of a caption line's audio, widened a little: engine word times can be
        a frame or two off, and a one-word line spans too little audio to judge alone."""
        start = float(start_sec) - margin_sec
        end = float(end_sec) + margin_sec
        if end - start < min_span_sec:
            middle = (start + end) / 2.0
            start, end = middle - min_span_sec / 2.0, middle + min_span_sec / 2.0
        return self.speech_share(start, end)


@dataclass(frozen=True)
class StreamingAction:
    action_type: StreamingActionType
    window: AudioWindow | None = None
    segment: AudioSegment | None = None
    discard: dict[str, Any] | None = None


class PendingWindowScheduler:
    """Turns mic chunks into v2 pending windows and confirmed VAD segments.

    The scheduler owns only timing and offsets. STT remains outside this class so
    the same logic can feed faster-whisper, whisper_streaming, or a later
    SimulStreaming adapter.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.sample_rate = int(config["audio"]["sample_rate"])
        self.chunk_seconds = float(config["streaming"]["chunk_seconds"])
        self.chunk_overlap_sec = float(config["streaming"].get("chunk_overlap_sec", 0.0))
        self.pending_debounce_sec = float(config["streaming"]["pending_debounce_sec"])
        self.ring = AudioRingBuffer(
            sample_rate=self.sample_rate,
            max_seconds=float(config["streaming"]["buffer_max_sec"]),
        )
        self.segmenter = create_vad_segmenter(config)
        self.speech = SpeechTimeline()
        self._last_pending_end_sample = 0
        self._last_pending_emit_sec = -1_000_000.0

    def push(self, chunk: AudioChunk) -> list[StreamingAction]:
        actions: list[StreamingAction] = []
        self.ring.append(chunk.samples)

        segment = self.segmenter.push(chunk)
        discard = self.segmenter.pop_discard()
        if discard is not None:
            actions.append(StreamingAction(action_type="discarded_segment", discard=discard))

        decision = getattr(self.segmenter, "last_decision", None)
        is_voice = bool(decision.is_voice) if decision is not None else chunk.rms >= self.segmenter.threshold
        self.speech.add(chunk.offset_sec, chunk.offset_sec + chunk.duration_sec, is_voice)
        if is_voice and self._pending_due(chunk):
            window = self._pending_window()
            if window.samples.size > 0:
                actions.append(StreamingAction(action_type="pending_window", window=window))
                self._last_pending_end_sample = window.end_sample
                self._last_pending_emit_sec = chunk.offset_sec + chunk.duration_sec

        if segment is not None:
            actions.append(StreamingAction(action_type="confirmed_segment", segment=segment))
            self._last_pending_end_sample = self.ring.end_sample
        return actions

    def flush(self) -> list[StreamingAction]:
        actions: list[StreamingAction] = []
        segment = self.segmenter.flush()
        discard = self.segmenter.pop_discard()
        if discard is not None:
            actions.append(StreamingAction(action_type="discarded_segment", discard=discard))
        if segment is not None:
            actions.append(StreamingAction(action_type="confirmed_segment", segment=segment))
        return actions

    def _pending_due(self, chunk: AudioChunk) -> bool:
        chunk_end_sec = chunk.offset_sec + chunk.duration_sec
        if chunk_end_sec - self._last_pending_emit_sec < self.pending_debounce_sec:
            return False
        pending_sample_gap = self.ring.end_sample - self._last_pending_end_sample
        return pending_sample_gap >= int(self.chunk_seconds * self.sample_rate)

    def _pending_window(self) -> AudioWindow:
        seconds = max(self.chunk_seconds, self.chunk_seconds + self.chunk_overlap_sec)
        return self.ring.latest(seconds)
