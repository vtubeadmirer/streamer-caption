from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from core.events import CaptionEvent


class CaptionEngine(ABC):
    name: str

    @abstractmethod
    def on_audio_segment(
        self,
        audio: object,
        sample_rate: int,
        audio_offset_sec: float,
    ) -> Iterable[CaptionEvent]:
        """Process a complete segment or chunk and yield caption events."""

    def close(self) -> None:
        pass

