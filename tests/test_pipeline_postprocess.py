from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.events import CaptionEvent
from features.defense import DefenseFilter
from features.slang import SlangDictionary
from main import postprocess_event


def main() -> int:
    config = load_config()
    events = postprocess_event(
        CaptionEvent(
            event_type="caption_confirmed",
            text="한글자막 by 한효주",
            confidence=0.9,
            audio_offset_sec=1.0,
            engine="test",
        ),
        DefenseFilter(config),
        SlangDictionary(config),
    )
    assert len(events) == 1
    assert events[0].event_type == "caption_hidden"

    config["caption"]["max_chars_per_line"] = 12
    events = postprocess_event(
        CaptionEvent(
            event_type="caption_confirmed",
            text="오늘은 정말 긴 문장을 일부러 테스트하고 있습니다",
            confidence=0.9,
            audio_offset_sec=1.0,
            engine="test",
        ),
        DefenseFilter(config),
        SlangDictionary(config),
    )
    assert len(events) >= 2
    assert all(len(event.text) <= 16 for event in events), [event.text for event in events]
    print("pipeline_postprocess_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
