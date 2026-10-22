from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from core.events import CaptionEvent


_NORMALIZE_PATTERN = re.compile(r"[^0-9A-Za-z가-힣]+")


@dataclass(frozen=True)
class SourceDuplicate:
    source: str
    matched_source: str
    similarity: float
    offset_delta_sec: float


class CrossSourceCaptionDeduplicator:
    """Suppresses near-identical captions captured by mic and loopback."""

    def __init__(self, config: dict[str, Any]) -> None:
        audio = config.get("audio", {})
        dual = audio.get("dual", {})
        self.enabled = bool(
            audio.get("source") == "both"
            and dual.get("echo_suppression_enabled", True)
        )
        self.window_sec = float(dual.get("echo_window_sec", 1.5))
        self.similarity_threshold = float(dual.get("echo_similarity", 0.88))
        self._recent: deque[tuple[float, str, str]] = deque(maxlen=100)

    def check(self, event: CaptionEvent) -> SourceDuplicate | None:
        if not self.enabled or event.event_type != "caption_confirmed":
            return None
        source = str(event.metadata.get("audio_source", "")).strip().lower()
        normalized = self._normalize(event.text)
        if source not in {"microphone", "system"} or not normalized:
            return None
        offset = max(0.0, float(event.audio_offset_sec))
        self._prune(offset)
        for recent_offset, recent_source, recent_text in reversed(self._recent):
            if recent_source == source:
                continue
            delta = abs(offset - recent_offset)
            if delta > self.window_sec:
                continue
            similarity = SequenceMatcher(None, normalized, recent_text).ratio()
            if similarity >= self.similarity_threshold:
                return SourceDuplicate(
                    source=source,
                    matched_source=recent_source,
                    similarity=similarity,
                    offset_delta_sec=delta,
                )
        self._recent.append((offset, source, normalized))
        return None

    def _prune(self, offset: float) -> None:
        while self._recent and offset - self._recent[0][0] > self.window_sec * 2:
            self._recent.popleft()

    @staticmethod
    def _normalize(text: str) -> str:
        return _NORMALIZE_PATTERN.sub("", str(text).lower())