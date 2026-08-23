from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config, validate_config
from features.defense import DefenseFilter


def main() -> int:
    config = load_config()
    validate_config(config)

    defense = DefenseFilter(config)
    short_spoken = defense.apply(
        "안녕하세요",
        0.95,
        {
            "segment_voice_ratio": 0.03,
            "no_speech_prob": 0.1,
            "audio_offset_sec": 1.0,
        },
    )
    assert short_spoken.visible, short_spoken
    assert "short_speech_rescue" in short_spoken.signals

    weak_repeat = {
        "segment_voice_ratio": 0.08,
        "no_speech_prob": 0.5,
    }
    first = defense.apply(
        "배달의 민족 주문",
        0.9,
        {**weak_repeat, "audio_offset_sec": 10.0},
    )
    second = defense.apply(
        "배달의 민족 주문",
        0.9,
        {**weak_repeat, "audio_offset_sec": 20.0},
    )
    third = defense.apply(
        "배달의 민족 주문",
        0.9,
        {**weak_repeat, "audio_offset_sec": 30.0},
    )
    assert first.visible and second.visible
    assert not third.visible
    assert third.reason == "hallucination_score"
    assert "repeated_weak_caption" in third.signals
    assert third.risk_score >= config["defense"]["hallucination_score_threshold"]

    clear_defense = DefenseFilter(config)
    clear_results = [
        clear_defense.apply(
            "샌드위치 주세요",
            0.95,
            {
                "segment_voice_ratio": 0.5,
                "no_speech_prob": 0.1,
                "avg_logprob": -0.2,
                "compression_ratio": 1.2,
                "audio_offset_sec": float(offset),
            },
        )
        for offset in (1, 10, 20)
    ]
    assert all(result.visible for result in clear_results)

    risky = DefenseFilter(config).apply(
        "의심되는 반복 문장",
        0.65,
        {
            "segment_voice_ratio": 0.08,
            "no_speech_prob": 0.65,
            "avg_logprob": -1.4,
            "compression_ratio": 3.4,
            "audio_offset_sec": 1.0,
        },
    )
    assert not risky.visible
    assert risky.reason == "hallucination_score"
    assert "low_avg_logprob" in risky.signals
    assert "high_compression_ratio" in risky.signals

    print("defense_score_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())