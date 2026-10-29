"""Gray preview captions (v2_light pending windows) while a sentence is still being spoken.

A pending window runs from the start of the utterance (core/streaming.py), so each preview is a
new guess at the whole sentence so far. Until 2026-10-29 it was only the last 0.7 s: Whisper
guessed without context, and in a broadcast test a fifth of the preview characters survived into
the final caption, with "감사합니다" and "다음 영상에서 만나요" invented for short windows.

PreviewTracker keeps, per audio source:
* the end of the last sentence handed to its final caption: a preview of audio before that
  is stale and is dropped;
* local agreement: the words at the start of a preview that the previous preview of the same
  window also had are stable (metadata stable_chars). The overlay shows them in the caption
  colour and the rest in gray, so only the guess that is still changing looks unsure.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from features.phantom_phrases import compact


@dataclass(frozen=True)
class PreviewUpdate:
    text: str
    stable_chars: int  # leading characters of text that two previews in a row agree on
    agreed_words: int


class PreviewTracker:
    def __init__(self) -> None:
        self._key: tuple[Any, float | None] | None = None
        self._words: list[str] = []
        self._confirmed_end = -math.inf

    def confirmed(self, end_offset_sec: float) -> None:
        """A sentence of this source went to its final caption; previews up to its end are stale."""
        self._confirmed_end = max(self._confirmed_end, float(end_offset_sec))
        self._key = None
        self._words = []

    def accept(self, text: str, metadata: dict[str, Any]) -> PreviewUpdate | None:
        """The preview to show for one pending result, or None when it is stale or empty."""
        window_end = _number(metadata.get("window_end_offset_sec"))
        if window_end is not None and window_end <= self._confirmed_end:
            return None
        words = str(text or "").split()
        if not words:
            return None
        window_start = _number(metadata.get("window_start_offset_sec"))
        key = (
            metadata.get("preview_utterance_id"),
            round(window_start, 3) if window_start is not None else None,
        )
        agreed = 0
        if self._key is not None and key == self._key:
            for previous, current in zip(self._words, words):
                if _comparable(previous) != _comparable(current):
                    break
                agreed += 1
        self._key = key
        self._words = words
        display = " ".join(words)
        stable_chars = len(" ".join(words[:agreed]))
        return PreviewUpdate(display, stable_chars, agreed)


def _comparable(word: str) -> str:
    """Punctuation does not break agreement: "날씨가," and "날씨가" are the same word."""
    return compact(word) or word


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
