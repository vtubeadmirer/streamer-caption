from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from core.audio_capture import AudioChunk, AudioSegment
from core.ring_buffer import AudioRingBuffer, AudioWindow
from core.vad import create_vad_segmenter


StreamingActionType = Literal["pending_window", "confirmed_segment", "discarded_segment"]


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
