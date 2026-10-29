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

    def first_pause(self, start_sec: float, end_sec: float) -> float | None:
        """Start of the earliest unvoiced chunk that begins inside [start_sec, end_sec]."""
        found = None
        for span_start, span_end, is_voice in reversed(self._spans):
            if span_end <= start_sec:
                break
            if not is_voice and start_sec <= span_start <= end_sec:
                found = span_start
        return found


@dataclass(frozen=True)
class StreamingAction:
    action_type: StreamingActionType
    window: AudioWindow | None = None
    segment: AudioSegment | None = None
    discard: dict[str, Any] | None = None
    # pending_window: preview_utterance_id, utterance_start_offset_sec, speech_share
    metadata: dict[str, Any] | None = None


class PendingWindowScheduler:
    """Turns mic chunks into v2 pending windows and confirmed VAD segments.

    The scheduler owns only timing and offsets. STT remains outside this class so
    the same logic can feed faster-whisper, whisper_streaming, or a later
    SimulStreaming adapter.

    A pending (gray preview) window runs from the start of the current utterance, pre-roll
    included, to now: Whisper hears the whole sentence so far instead of the last 0.7 s, which
    it could only guess at (2026-10-29: a fifth of those preview characters survived into the
    final caption). Previews start once the utterance is pending_min_utterance_sec old, come at
    most every chunk_seconds of new audio, and the window is at most pending_max_window_sec
    long: past that it starts again at the first pause the voice detector heard, or exactly
    that far back when there was none.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        streaming = config["streaming"]
        self.sample_rate = int(config["audio"]["sample_rate"])
        self.chunk_seconds = float(streaming["chunk_seconds"])
        self.chunk_overlap_sec = float(streaming.get("chunk_overlap_sec", 0.0))
        self.pending_debounce_sec = float(streaming["pending_debounce_sec"])
        self.pending_min_utterance_sec = float(streaming.get("pending_min_utterance_sec", 1.2))
        # A window cannot reach further back than the ring keeps audio (buffer_max_sec may be 5).
        self.pending_max_window_sec = min(
            float(streaming.get("pending_max_window_sec", 6.0)),
            float(streaming["buffer_max_sec"]),
        )
        self.ring = AudioRingBuffer(
            sample_rate=self.sample_rate,
            max_seconds=float(streaming["buffer_max_sec"]),
        )
        self.segmenter = create_vad_segmenter(config)
        self.speech = SpeechTimeline()
        self._last_pending_end_sample = 0
        self._last_pending_emit_sec = -1_000_000.0
        self._utterance_id = 0
        self._utterance_start_sec: float | None = None  # segment start, pre-roll included
        self._utterance_voice_sec: float | None = None  # first voiced chunk
        self._window_start_sec: float | None = None  # moves forward when a long window folds

    def push(self, chunk: AudioChunk) -> list[StreamingAction]:
        actions: list[StreamingAction] = []
        self.ring.append(chunk.samples)

        was_open = bool(getattr(self.segmenter, "in_segment", False))
        segment = self.segmenter.push(chunk)
        discard = self.segmenter.pop_discard()
        if discard is not None:
            actions.append(StreamingAction(action_type="discarded_segment", discard=discard))

        decision = getattr(self.segmenter, "last_decision", None)
        is_voice = bool(decision.is_voice) if decision is not None else chunk.rms >= self.segmenter.threshold
        self.speech.add(chunk.offset_sec, chunk.offset_sec + chunk.duration_sec, is_voice)
        self._track_utterance(chunk, was_open)
        if is_voice and self._pending_due(chunk):
            window = self._preview_window(chunk)
            if window is not None and window.samples.size > 0:
                actions.append(
                    StreamingAction(
                        action_type="pending_window",
                        window=window,
                        metadata=self._preview_metadata(window),
                    )
                )
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

    def _track_utterance(self, chunk: AudioChunk, was_open: bool) -> None:
        start = getattr(self.segmenter, "segment_start_sec", None)
        if start is None:
            self._utterance_start_sec = self._utterance_voice_sec = self._window_start_sec = None
            return
        if not was_open or self._utterance_start_sec is None:
            self._utterance_id += 1
            self._utterance_start_sec = self._window_start_sec = float(start)
            self._utterance_voice_sec = chunk.offset_sec

    def _preview_window(self, chunk: AudioChunk) -> AudioWindow | None:
        if self._window_start_sec is None or self._utterance_voice_sec is None:
            return None
        end_sec = chunk.offset_sec + chunk.duration_sec
        if end_sec - self._utterance_voice_sec < self.pending_min_utterance_sec:
            return None
        if end_sec - self._window_start_sec > self.pending_max_window_sec + 1e-9:
            earliest = end_sec - self.pending_max_window_sec
            pause = self.speech.first_pause(earliest, end_sec - self.pending_min_utterance_sec)
            self._window_start_sec = pause if pause is not None else earliest
        # Chunk offsets and ring samples count the same audio: place the start back from the end.
        length = int(round((end_sec - self._window_start_sec) * self.sample_rate))
        return self.ring.window(self.ring.end_sample - length, self.ring.end_sample)

    def _preview_metadata(self, window: AudioWindow) -> dict[str, Any]:
        metadata: dict[str, Any] = {"preview_utterance_id": self._utterance_id}
        if self._utterance_start_sec is not None:
            metadata["utterance_start_offset_sec"] = round(self._utterance_start_sec, 3)
        share = self.speech.speech_share(window.start_offset_sec, window.end_offset_sec)
        if share is not None:
            metadata["speech_share"] = round(share, 3)
        return metadata
