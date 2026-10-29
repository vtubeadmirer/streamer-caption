"""Gray previews of the 기본 engine (core/preview.py, main.preview_events).

* Local agreement: the words at the start of a preview that the previous preview of the same
  window also had are marked stable (metadata stable_chars); the overlay shows them in the
  caption colour. A new utterance or a window that moved starts over.
* A preview of audio that already went to its final caption is dropped.
* Whole sentences that are only an outro phrase or a subtitle credit are removed from previews,
  which Whisper adds to short windows ("오늘 날씨가. 감사합니다.").
"""
from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import DEFAULTS, deep_merge, validate_config  # noqa: E402
from core.events import CaptionEvent  # noqa: E402
from core.preview import PreviewTracker  # noqa: E402
from features.defense import DefenseFilter  # noqa: E402
from features.slang import SlangDictionary  # noqa: E402
import main as app  # noqa: E402


def window(utterance: int, start: float, end: float) -> dict:
    return {
        "preview_utterance_id": utterance,
        "window_start_offset_sec": start,
        "window_end_offset_sec": end,
        "audio_source": "microphone",
    }


def check_tracker() -> None:
    tracker = PreviewTracker()
    first = tracker.accept("오늘은 날씨가 좋", window(1, 0.2, 1.7))
    assert first is not None and first.stable_chars == 0 and first.text == "오늘은 날씨가 좋"
    # The cut-off last word ("좋") is not agreed; the two before it are.
    second = tracker.accept("오늘은 날씨가 좋네요", window(1, 0.2, 2.2))
    assert second.agreed_words == 2 and second.text[: second.stable_chars] == "오늘은 날씨가"
    # Punctuation does not break agreement; a changed word ends it.
    third = tracker.accept("오늘은, 날씨가 좋네요. 그래서", window(1, 0.2, 2.7))
    assert third.agreed_words == 3 and third.text[: third.stable_chars] == "오늘은, 날씨가 좋네요."
    fourth = tracker.accept("오늘은 날씨가 좋네요. 그래서 산책을", window(1, 0.2, 3.2))
    assert fourth.agreed_words == 4
    fifth = tracker.accept("오늘 날씨가 좋네요. 그래서 산책을 갔", window(1, 0.2, 3.7))
    assert fifth.agreed_words == 0 and fifth.stable_chars == 0
    # The window moved (a long sentence folded at a pause): no agreement across windows.
    moved = tracker.accept("그래서 산책을 갔어요", window(1, 3.0, 4.2))
    assert moved.stable_chars == 0
    again = tracker.accept("그래서 산책을 갔어요 공원에", window(1, 3.0, 4.7))
    assert again.text[: again.stable_chars] == "그래서 산책을 갔어요"
    # A new utterance starts over even when its window starts at the same time.
    other = tracker.accept("그래서 산책을 갔어요 공원에", window(2, 3.0, 5.2))
    assert other.stable_chars == 0
    # Identical guesses agree entirely.
    same = tracker.accept("그래서 산책을 갔어요 공원에", window(2, 3.0, 5.7))
    assert same.stable_chars == len(same.text)

    # The sentence up to 6.0 s went to its final caption: older previews are stale.
    tracker.confirmed(6.0)
    assert tracker.accept("그래서 산책을 갔어요 공원에서", window(2, 3.0, 5.9)) is None
    assert tracker.accept("그래서 산책을 갔어요 공원에서", window(2, 3.0, 6.0)) is None
    fresh = tracker.accept("다음 문장", window(3, 6.5, 8.0))
    assert fresh is not None and fresh.stable_chars == 0
    assert tracker.accept("   ", window(3, 6.5, 8.5)) is None


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


def hypothesis(text: str, end: float) -> CaptionEvent:
    return CaptionEvent(
        event_type="caption_confirmed",
        text=text,
        confidence=0.9,
        audio_offset_sec=end,
        engine="baseline",
        metadata={"no_speech_prob": 0.05, "avg_logprob": -0.2},
    )


def check_preview_events(config: dict) -> None:
    defense = DefenseFilter(config)
    slang = SlangDictionary(config)
    tracker = PreviewTracker()

    def show(text: str, end: float, utterance: int = 1, start: float = 0.2) -> list[CaptionEvent]:
        metadata = {**window(utterance, start, end), "speech_share": 0.9}
        return app.preview_events(hypothesis(text, end), defense, slang, tracker, metadata)

    out = show("오늘 날씨가. 감사합니다.", 1.7)
    assert [event.event_type for event in out] == ["caption_pending"]
    assert out[0].text == "오늘 날씨가." and out[0].metadata["stable_chars"] == 0
    assert out[0].metadata["raw_text"] == "오늘 날씨가. 감사합니다."
    out = show("오늘 날씨가. 정말 좋네요 다음 영상에서 만나요!", 2.2)
    assert out[0].text == "오늘 날씨가. 정말 좋네요 다음 영상에서 만나요!"  # not a whole sentence
    assert out[0].text[: out[0].metadata["stable_chars"]] == "오늘 날씨가."
    # A preview that is only a phantom or a credit shows nothing.
    assert show("감사합니다.", 2.7) == []
    assert show("다음 영상에서 만나요! 한글자막 by 한효주", 3.2) == []
    # A thank-you with other words around it stays.
    out = show("망치즈님 감사합니다 고맙습니다", 3.7, utterance=2, start=3.4)
    assert out and out[0].text == "망치즈님 감사합니다 고맙습니다"
    # After the final caption of that sentence, its late previews are dropped.
    tracker.confirmed(4.0)
    assert show("망치즈님 감사합니다 고맙습니다 여러분", 3.9, utterance=2, start=3.4) == []
    # Without a tracker (no source) previews still pass, phantoms removed.
    out = app.preview_events(hypothesis("오늘 날씨가. 감사합니다.", 9.0), defense, slang, None, {})
    assert [event.text for event in out] == ["오늘 날씨가."]
    # Engine errors pass through unchanged.
    error = CaptionEvent(event_type="engine_error", text="boom", audio_offset_sec=1.0, engine="baseline")
    assert app.preview_events(error, defense, slang, tracker, {}) == [error]


def main() -> int:
    check_tracker()
    with tempfile.TemporaryDirectory() as directory:
        check_preview_events(clean_config(Path(directory)))
    print("preview_tracker_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
