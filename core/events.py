from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


CaptionEventType = Literal[
    "engine_status",
    "caption_pending",
    "caption_confirmed",
    "caption_hidden",
    "engine_error",
    "audio_drop",
    "memory_sample",
    "summary_updated",
    "topic_changed",
    "broadcast_ended",
]


@dataclass(frozen=True)
class CaptionEvent:
    event_type: CaptionEventType
    text: str = ""
    confidence: float = 0.0
    audio_offset_sec: float = 0.0
    engine: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "text": self.text,
            "confidence": self.confidence,
            "audio_offset_sec": self.audio_offset_sec,
            "engine": self.engine,
            "metadata": self.metadata,
        }
