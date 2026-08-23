from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config, validate_config


def main() -> int:
    html = (ROOT / "overlay" / "caption.html").read_text(
        encoding="utf-8"
    )
    required = (
        'id="confirmedCaption"',
        'id="pendingCaption"',
        "let confirmedQueue = []",
        "function drainConfirmedQueue()",
        "function renderConfirmed()",
        "function renderPending()",
        "captionConfig.just_chatting_min_line_hold_sec",
        "captionConfig.history_max_items",
        "if (justChattingMode) return;",
        "confirmedQueue.length > maxLines * 2",
        '"connected | 읽기 대기 "',
    )
    for value in required:
        assert value in html, value

    assert "confirmedHistory.length > 12" not in html
    assert "caption.innerHTML =" not in html
    assert "pendingCaption.innerHTML" in html
    assert "confirmedCaption.innerHTML" in html

    config = load_config()
    validate_config(config)
    assert (
        config["caption"]["just_chatting_min_line_hold_sec"]
        == 0.65
    )
    print("caption_overlay_contract_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())