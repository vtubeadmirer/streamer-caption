"""Hallucinated subtitle credits and YouTube outro lines (features/phantom_phrases.py).

* Subtitle credits ("한글자막 by 한효주") are hidden on text alone, but a sentence that only
  mentions 자막, 편집 or "by" is real speech: the old filter hid every line of 30 characters or
  fewer containing 자막, which cost three real sentences per ten minutes of the lab script.
* A whole line that is only an outro phrase ("감사합니다.", "다음 영상에서 만나요!") is hidden
  when the app's voice detector heard little speech in that line's audio (speech_share, or the
  segment's voice ratio for faster-whisper). The 정확 우선 engine reports no Whisper confidence
  values, so before this its risk score was 0 and such lines reached the screen.
* Any other line is never hidden on a low share alone, not even when it repeats: a short "네"
  or "응" spans too little audio for the voice detector and often scores under 0.3.
"""
from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.audio_capture import AudioChunk  # noqa: E402
from core.config import DEFAULTS, deep_merge, validate_config  # noqa: E402
from core.events import CaptionEvent  # noqa: E402
from core.streaming import PendingWindowScheduler, SpeechTimeline  # noqa: E402
from features.defense import DefenseFilter  # noqa: E402
from features.phantom_phrases import (  # noqa: E402
    is_phantom_line,
    is_subtitle_credit,
    strip_phantom_sentences,
)
from features.slang import SlangDictionary  # noqa: E402
import main as app  # noqa: E402

REAL_SENTENCES = (
    # The lab script lines the old filter hid (both engines).
    "지금 보시는 자막은 제 컴퓨터에서 바로 만들어지고 있습니다.",
    "마이크 소리와 디스코드 소리를 따로 구분해서 자막을 딸 수 있습니다.",
    "자막 프로그램 소개는 여기까지입니다.",
    "자막 설정 좀 바꿔볼게요",
    "자막 제공 기능도 있어요",
    "자막 켜 주세요",
    "오늘은 영상 편집을 해볼게요",
    "편집 끝났어요",
    "제작진 여러분 감사합니다",
    "step by step으로 해볼게요",
    "caption 크기 좀 키울게요",
    "시청자 자막 기능을 켜 주세요",
)
CREDITS = (
    "한글자막 by 한효주",
    "자막 by 시청자",
    "자막 제공 배달의 민족",
    "자막 제공: 배달의민족",
    "[자막 제공: 배달의 민족]",
    "자막 제작: 이덕영",
    "시청자 자막",
    "편집: 김철수",
    "영상편집: 홍길동",
    "Subtitles by the Amara.org community",
    "字幕由Amara.org社区提供",
    "(c) KBS",
    "[ⓒ MBC]",
)
PHANTOMS = (
    "감사합니다.",
    "감사합니다",
    "정말 감사합니다!",
    "감사합니다. 감사합니다.",
    "고맙습니다",
    "시청해 주셔서 감사합니다.",
    "다음 영상에서 만나요!",
    "다음 시간에 뵙겠습니다.",
    "구독과 좋아요 부탁드립니다",
    "좋아요 눌러주세요",
    "MBC 뉴스 이덕영입니다.",
    "안녕하십니까.",
    "Thank you.",
)
NOT_PHANTOMS = (
    "망치즈 감사합니다 고맙습니다",
    "네 감사합니다",
    "오늘 방송 감사합니다",
    "여러분 안녕하십니까",
    "구독자 여러분",
    "다음 판 가볼게요",
    "감사합니다 여러분 오늘은",
)


def clean_config(tmp: Path) -> dict:
    config = deep_merge(copy.deepcopy(DEFAULTS), {
        "defense": {
            "whitelist_path": str(tmp / "whitelist.txt"),
            "blocklist_path": str(tmp / "blocklist.txt"),
        },
        "slang": {"dict_path": str(tmp / "slang.json")},
    })
    validate_config(config)
    return config


def simul_line(text: str, share: float | None, offset: float = 10.0) -> CaptionEvent:
    """A 정확 우선 line: confidence 1.0 and no Whisper evidence, only the measured speech share."""
    metadata = {"stable": True} if share is None else {"speech_share": share}
    return CaptionEvent(event_type="caption_confirmed", text=text, confidence=1.0,
                        audio_offset_sec=offset, engine="simulstreaming", metadata=metadata)


def check_matchers() -> None:
    for text in CREDITS:
        assert is_subtitle_credit(text), text
    for text in REAL_SENTENCES:
        assert not is_subtitle_credit(text), text
    for text in PHANTOMS:
        assert is_phantom_line(text), text
    for text in NOT_PHANTOMS + REAL_SENTENCES:
        assert not is_phantom_line(text), text
    assert strip_phantom_sentences("오늘 날씨가. 감사합니다.") == "오늘 날씨가."
    assert strip_phantom_sentences("감사합니다.") == ""
    assert strip_phantom_sentences("안녕하십니까. 오늘은") == "오늘은"
    assert strip_phantom_sentences("한글자막 by 한효주") == ""
    assert strip_phantom_sentences("망치즈 감사합니다 고맙습니다") == "망치즈 감사합니다 고맙습니다"


def check_credit_filter(config: dict) -> None:
    spoken = {"segment_voice_ratio": 0.6, "no_speech_prob": 0.05, "avg_logprob": -0.2}
    defense = DefenseFilter(config)
    for index, text in enumerate(REAL_SENTENCES):
        result = defense.apply(text, 0.9, {**spoken, "audio_offset_sec": 100.0 + index * 5})
        assert result.visible, (text, result)
    for index, text in enumerate(CREDITS):
        result = defense.apply(text, 0.95, {**spoken, "audio_offset_sec": 300.0 + index * 5})
        assert not result.visible and result.reason == "hallucination_pattern", (text, result)
    # Both engines: the 정확 우선 path keeps the credit filter as well.
    slang = SlangDictionary(config)
    hidden = app.postprocess_event(simul_line("한글자막 by 한효주", 0.9), DefenseFilter(config), slang, allow_single_char=True)
    assert hidden[0].event_type == "caption_hidden", hidden
    shown = app.postprocess_event(simul_line(REAL_SENTENCES[0], 0.9), DefenseFilter(config), slang, allow_single_char=True)
    assert [event.event_type for event in shown] == ["caption_confirmed"] * len(shown) and shown, shown
    # A long run of two or three characters is still a hallucination.
    assert not defense.apply("아아아아아아아아 아아아아아아아아 아아", 0.95, {**spoken, "audio_offset_sec": 900.0}).visible


def check_phantom_lines(config: dict) -> None:
    slang = SlangDictionary(config)
    defense = DefenseFilter(config)
    threshold = config["defense"]["phantom_max_speech_share"]
    assert threshold == 0.3
    # 정확 우선: the reported lines are hidden over music or noise ...
    for index, text in enumerate(("감사합니다.", "다음 영상에서 만나요!", "감사합니다")):
        out = app.postprocess_event(simul_line(text, 0.1, 100.0 + index * 30), defense, slang, allow_single_char=True)
        assert len(out) == 1 and out[0].event_type == "caption_hidden", (text, out)
        assert out[0].metadata["defense_reason"] == "phantom_phrase", out[0].metadata
        assert "low_speech_share" in out[0].metadata["defense_signals"], out[0].metadata
    # ... but shown when the streamer said them (the voice detector heard speech).
    for index, text in enumerate(("감사합니다.", "다음 영상에서 만나요!")):
        out = app.postprocess_event(simul_line(text, 0.75, 300.0 + index * 30), defense, slang, allow_single_char=True)
        assert [event.event_type for event in out] == ["caption_confirmed"], (text, out)
    # A thank-you inside a longer line is real (a donation read out) even with a low share.
    out = app.postprocess_event(simul_line("망치즈 감사합니다 고맙습니다", 0.1, 500.0), defense, slang, allow_single_char=True)
    assert [event.event_type for event in out] == ["caption_confirmed"], out
    # No measurement at all (no audio seen for that span): nothing to judge, shown.
    out = app.postprocess_event(simul_line("감사합니다.", None, 600.0), defense, slang, allow_single_char=True)
    assert [event.event_type for event in out] == ["caption_confirmed"], out
    # The growing (pending) line follows the same rule.
    assert app.pending_events(simul_line("감사합니다.", 0.1, 700.0), defense, slang, min_chars=1, allow_single_char=True) == []
    assert app.pending_events(simul_line("감사합니다.", 0.8, 710.0), defense, slang, min_chars=1, allow_single_char=True)

    # faster-whisper: the segment's voice ratio stands in for the share.
    baseline = DefenseFilter(config)
    weak = baseline.apply("감사합니다.", 0.9, {"segment_voice_ratio": 0.2, "no_speech_prob": 0.1, "audio_offset_sec": 50.0})
    assert not weak.visible and weak.reason == "phantom_phrase", weak
    spoken = baseline.apply("감사합니다.", 0.9, {"segment_voice_ratio": 0.6, "no_speech_prob": 0.1, "audio_offset_sec": 80.0})
    assert spoken.visible, spoken

    # The whitelist still wins over the phrase rule.
    tmp = Path(config["defense"]["whitelist_path"]).parent
    (tmp / "whitelist.txt").write_text("감사합니다\n", encoding="utf-8")
    try:
        allowed = DefenseFilter(config).apply("감사합니다.", 1.0, {"speech_share": 0.05, "audio_offset_sec": 1.0})
        assert allowed.visible and allowed.reason == "whitelist", allowed
    finally:
        (tmp / "whitelist.txt").unlink()

    # The share also feeds the risk score, but alone it only marks the line, even a repeated one:
    # a short "네" or "응" spans too little audio for the voice detector and often scores under
    # 0.3, and allow_single_char is there so that every one of them shows.
    for text in ("네", "응", "오늘은 여기까지 할게요"):
        risk = DefenseFilter(config)
        results = [risk.apply(text, 1.0, {"speech_share": 0.15, "audio_offset_sec": 20.0 + 10 * k}, allow_single_char=True)
                   for k in range(4)]
        assert all(result.visible for result in results), (text, results)
        assert all("low_speech_share" in result.signals and 0 < result.risk_score < 0.72 for result in results), results
        assert not any("repeated_weak_caption" in result.signals for result in results), results
    backchannel = DefenseFilter(config)
    for k in range(3):
        out = app.postprocess_event(simul_line("네", 0.15, 800.0 + 5 * k), backchannel, slang, allow_single_char=True)
        assert [event.event_type for event in out] == ["caption_confirmed"], (k, out)
    assert app.pending_events(simul_line("네", 0.15, 816.0), backchannel, slang, min_chars=1, allow_single_char=True)
    # Outro phrases over music stay hidden by the phrase rule, however often they come.
    phrases = DefenseFilter(config)
    for k in range(3):
        out = app.postprocess_event(simul_line("감사합니다.", 0.2, 900.0 + 5 * k), phrases, slang, allow_single_char=True)
        assert out[0].event_type == "caption_hidden" and out[0].metadata["defense_reason"] == "phantom_phrase", out


def chunk(offset: float, voiced: bool, duration: float = 0.064) -> AudioChunk:
    samples = np.full(int(duration * 16000), 0.2 if voiced else 0.0, dtype=np.float32)
    return AudioChunk(samples=samples, rms=0.2 if voiced else 0.0, offset_sec=offset, duration_sec=duration,
                      source_name="microphone")


def check_speech_timeline(config: dict) -> None:
    timeline = SpeechTimeline(max_sec=10.0)
    for index in range(100):  # 0..6.4 s: voice only between 2.0 and 3.0 s
        start = index * 0.064
        timeline.add(start, start + 0.064, 2.0 <= start < 3.0)
    assert timeline.speech_share(2.0, 3.0) > 0.95
    assert timeline.speech_share(4.0, 6.0) == 0.0
    assert abs(timeline.speech_share(1.5, 3.5) - 0.5) < 0.05
    assert timeline.speech_share(50.0, 60.0) is None, "no chunks seen there"
    assert timeline.speech_share(3.0, 3.0) is None
    # A line's span gets 0.2 s on each side; one short word is widened to 0.6 s around it.
    assert abs(timeline.share_around(1.0, 2.5) - timeline.speech_share(0.8, 2.7)) < 1e-9
    assert abs(timeline.share_around(2.9, 3.0) - timeline.speech_share(2.65, 3.25)) < 1e-9
    for index in range(100, 400):  # old spans are dropped after max_sec
        timeline.add(index * 0.064, index * 0.064 + 0.064, False)
    assert timeline.speech_share(2.0, 3.0) is None

    # The scheduler records every chunk's verdict; run_v2_light reads it for 정확 우선 lines.
    source_config = deep_merge(copy.deepcopy(config), {"vad": {"engine": "energy"}})
    scheduler = PendingWindowScheduler(source_config)
    offset = 0.0
    for voiced in [False] * 20 + [True] * 20 + [False] * 20:
        scheduler.push(chunk(offset, voiced))
        offset += 0.064
    music_line = {"line_start_offset_sec": 2.7, "line_end_offset_sec": 3.6}
    speech_line = {"line_start_offset_sec": 1.4, "line_end_offset_sec": 2.4}
    assert app.line_speech_share(music_line, scheduler.speech) < 0.3
    assert app.line_speech_share(speech_line, scheduler.speech) > 0.7
    assert app.line_speech_share({"line_end_offset_sec": 2.4}, scheduler.speech) is not None, "start missing: last second"
    assert app.line_speech_share({}, scheduler.speech) is None
    assert app.line_speech_share(speech_line, None) is None


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    config = clean_config(tmp)
    check_matchers()
    check_credit_filter(config)
    check_phantom_lines(config)
    check_speech_timeline(config)
    print("hallucination_phrases_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
