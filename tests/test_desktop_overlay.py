"""내 화면 자막: caption rules, wrapping, drawing and the process keeper (no window is opened)."""
from __future__ import annotations

import copy
import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import DEFAULTS, validate_config  # noqa: E402
from core.desktop_overlay import (  # noqa: E402
    CaptionState,
    DesktopOverlayKeeper,
    OverlayLine,
    OverlaySettings,
    compose,
    wrap_spans,
)


def confirmed(text: str, offset: float, source: str = "microphone") -> dict:
    return {"type": "caption_confirmed", "text": text, "audio_offset_sec": offset, "metadata": {"audio_source": source}}


def pending(text: str, offset: float, stable_chars: int = 0, stable: bool = False) -> dict:
    return {
        "type": "caption_pending",
        "text": text,
        "audio_offset_sec": offset,
        "metadata": {"audio_source": "microphone", "stable_chars": stable_chars, "stable": stable},
    }


def check_caption_rules() -> None:
    settings = OverlaySettings(max_lines=2, hold_sec=6.0, stale_sec=3.0)
    state = CaptionState()
    state.apply({"type": "sync", "items": [confirmed("방송 전 자막", 1.0)]}, 0.0, settings)
    assert state.lines(0.0, settings) == [], "lines from before the overlay started must not flash"

    state.apply(pending("안녕하세요 오늘은", 10.0, stable_chars=5), 0.0, settings)
    assert state.lines(0.1, settings) == [OverlayLine("안녕하세요 오늘은", 5, "pending")]

    state.apply(confirmed("안녕하세요 오늘은 게임해요.", 10.0), 0.5, settings)
    assert state.lines(0.6, settings) == [OverlayLine("안녕하세요 오늘은 게임해요.", 15, "current")]

    state.apply(pending("늦게 온 미리보기", 9.0), 0.7, settings)
    assert len(state.lines(0.8, settings)) == 1, "a preview older than the confirmed line is dropped"

    state.apply(confirmed("안녕하세요 오늘은 게임해요.", 10.0), 0.9, settings)
    state.apply(confirmed("두 번째 줄", 12.0), 1.0, settings)
    state.apply(confirmed("세 번째 줄", 14.0), 1.5, settings)
    lines = state.lines(1.6, settings)
    assert [line.text for line in lines] == ["두 번째 줄", "세 번째 줄"], lines
    assert [line.kind for line in lines] == ["previous", "current"], lines

    state.apply(pending("곧 숨겨질 말", 16.0), 2.0, settings)
    state.apply({"type": "caption_hidden", "audio_offset_sec": 16.0, "metadata": {"audio_source": "microphone"}}, 2.1, settings)
    assert all(line.kind != "pending" for line in state.lines(2.2, settings))

    state.apply(pending("멈춘 미리보기", 18.0), 3.0, settings)
    assert state.lines(5.9, settings)[-1].kind == "pending"
    assert state.lines(6.1, settings)[-1].kind != "pending", "an unconfirmed preview goes stale"
    assert state.lines(8.0, settings) == [], "confirmed lines leave after the hold time"

    stable = CaptionState()
    stable.apply(pending("정확 우선 문장", 1.0, stable=True), 0.0, settings)
    line = stable.lines(0.1, settings)[0]
    assert line.agreed == len(line.text), "정확 우선 words are final and drawn in the caption colour"

    both = OverlaySettings(labels={"microphone": "방송자", "system": "컴퓨터"})
    labelled = CaptionState()
    labelled.apply(confirmed("게임 소리", 1.0, source="system"), 0.0, both)
    assert labelled.lines(0.1, both)[0].text == "[컴퓨터] 게임 소리"


def check_settings() -> None:
    config = copy.deepcopy(DEFAULTS)
    settings = OverlaySettings.from_config(config)
    assert settings.enabled is False and settings.labels is None
    config["audio"]["source"] = "both"
    config["desktop_overlay"].update(enabled=True, font_size=40, position="top")
    settings = OverlaySettings.from_config(config)
    assert settings.enabled and settings.font_size == 40 and settings.position == "top"
    assert settings.labels == {"microphone": "방송자", "system": "컴퓨터"}

    validate_config(copy.deepcopy(DEFAULTS))
    for key, value in (("position", "left"), ("enabled", "yes"), ("opacity", 0.05), ("max_lines", 9)):
        bad = copy.deepcopy(DEFAULTS)
        bad["desktop_overlay"][key] = value
        try:
            validate_config(bad)
        except (TypeError, ValueError):
            continue
        raise AssertionError(f"desktop_overlay.{key}={value!r} must be rejected")


def check_wrap() -> None:
    measure = len  # one unit per character
    text = "아니 근데 오늘 방송 진짜 재밌었다"
    spans = wrap_spans(text, measure, 10)
    rows = [text[start:end] for start, end in spans]
    assert rows == ["아니 근데 오늘", "방송 진짜 재밌었다"], rows
    assert all(measure(row) <= 10 for row in rows)
    long_word = "가" * 25
    assert [end - start for start, end in wrap_spans(long_word, measure, 10)] == [10, 10, 5]
    assert wrap_spans("", measure, 10) == []


def check_compose() -> None:
    masks = np.zeros((20, 40, 4), dtype=np.float32)
    masks[8:12, 5:10, 2] = 1.0  # current text
    masks[8:12, 20:25, 0] = 1.0  # gray preview text
    image = compose(masks, 2, OverlaySettings(background=0.0, prev_opacity=0.55))
    assert image.dtype == np.uint8 and image.shape == (20, 40, 4)
    assert tuple(image[10, 7]) == (255, 255, 255, 255), "current text is opaque white"
    gray = image[10, 22]
    assert gray[3] < 255 and gray[2] <= gray[3], "preview text is translucent and premultiplied"
    assert image[10, 11, 3] > 0 and tuple(image[10, 11, :3]) == (0, 0, 0), "a dark outline surrounds text"
    assert image[0, 39, 3] == 0, "no background box: empty pixels stay fully transparent"
    boxed = compose(masks, 2, OverlaySettings(background=0.35))
    assert boxed[0, 39, 3] == round(0.35 * 255) or abs(int(boxed[0, 39, 3]) - 89) <= 1


def check_renderer() -> None:
    if os.name != "nt":
        return
    from core.desktop_overlay import TextRenderer

    renderer = TextRenderer(1.0)
    try:
        lines = [OverlayLine("첫 줄", 4, "previous"), OverlayLine("둘째 줄입니다", 7, "current"), OverlayLine("미리보기", 2, "pending")]
        image, width, height = renderer.render(lines, OverlaySettings(), 800)
        assert image.shape == (height, width, 4) and width <= 800, (width, height)
        assert (image[..., 3] == 255).any(), "glyphs are drawn"
        narrow, _w, narrow_height = renderer.render([OverlayLine("가나다 " * 20, 0, "current")], OverlaySettings(), 300)
        assert narrow.shape[1] <= 300 and narrow_height > height // 2, "long lines wrap inside the strip"
    finally:
        renderer.close()


def check_keeper(tmp: Path) -> None:
    config = {"desktop_overlay": {"enabled": False}}
    keeper = DesktopOverlayKeeper(
        lambda: config, "http://127.0.0.1:1", log_path=tmp / "overlay.err",
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    keeper.sync()
    assert not keeper.running()
    config["desktop_overlay"]["enabled"] = True
    keeper.sync()
    assert keeper.running(), "turning it on starts the overlay"
    first = keeper._process
    keeper.sync()
    assert keeper._process is first, "a running overlay is left alone"
    config["desktop_overlay"]["enabled"] = False
    keeper.sync()
    assert not keeper.running() and first.poll() is not None, "turning it off stops the overlay"

    crashing = DesktopOverlayKeeper(
        lambda: {"desktop_overlay": {"enabled": True}}, "http://127.0.0.1:1",
        command=[sys.executable, "-c", "raise SystemExit(3)"],
    )
    for _ in range(6):
        crashing.sync()
        deadline = time.monotonic() + 5
        while crashing.running() and time.monotonic() < deadline:
            time.sleep(0.05)
    assert "not restarting" in crashing.last_error, crashing.last_error
    crashing.stop()


def main() -> int:
    import tempfile

    check_caption_rules()
    check_settings()
    check_wrap()
    check_compose()
    check_renderer()
    with tempfile.TemporaryDirectory() as tmp:
        check_keeper(Path(tmp))
    print("desktop overlay ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
