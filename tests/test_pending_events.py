from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.events import CaptionEvent
from features.defense import DefenseFilter
from features.slang import SlangDictionary
from main import pending_events


def main() -> int:
    config = load_config()
    defense = DefenseFilter(config)
    slang = SlangDictionary(config)
    event = CaptionEvent(
        event_type="caption_confirmed",
        text="테스트 문장입니다",
        confidence=0.95,
        engine="test",
    )
    output = pending_events(event, defense, slang)
    assert len(output) == 1
    assert output[0].event_type == "caption_pending"
    assert output[0].text == "테스트 문장입니다"

    hidden = CaptionEvent(
        event_type="caption_confirmed",
        text="한글자막 by 한효주",
        confidence=0.2,
        engine="test",
    )
    assert pending_events(hidden, defense, slang) == []
    config["streaming"]["pending_min_chars"] = 3
    short = CaptionEvent(
        event_type="caption_confirmed",
        text="가",
        confidence=0.95,
        engine="test",
    )
    assert pending_events(short, defense, slang) == []
    print("pending_events_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
