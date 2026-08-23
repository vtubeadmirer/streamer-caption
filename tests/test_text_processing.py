from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.text_processing import split_sentences
from features.defense import DefenseFilter
from features.slang import SlangDictionary


def main() -> int:
    config = load_config()
    defense = DefenseFilter(config)
    spoken = {
        "segment_voice_ratio": 0.5,
        "no_speech_prob": 0.1,
    }
    assert not defense.apply("안", 0.9, spoken).visible
    assert defense.apply(
        "감사합니다",
        0.9,
        spoken,
    ).visible

    thanks = (
        "시청해주셔서 "
        "감사합니다."
    )
    assert defense.apply(thanks, 0.9, spoken).visible
    assert not defense.apply(
        thanks,
        0.9,
        {
            "segment_voice_ratio": 0.01,
            "no_speech_prob": 0.9,
        },
    ).visible

    credit = (
        "한글 자막 by "
        "한효주"
    )
    assert not defense.apply(credit, 0.9, spoken).visible
    assert defense.apply(
        "정상 문장입니다",
        0.9,
        spoken,
    ).visible

    slang = SlangDictionary(config)
    assert isinstance(
        slang.apply("테스트"),
        str,
    )

    chunks = split_sentences(
        "오늘 게임을 시작하는데 "
        "진짜 너무 어려웠어요. "
        "그래도 천천히 해볼게요"
    )
    assert len(chunks) >= 2, chunks
    print("text_processing_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())