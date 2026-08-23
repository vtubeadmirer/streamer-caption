from __future__ import annotations

import copy
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config, validate_config
from core.transcript_archive import TranscriptArchive
from features.broadcast_report import build_session_report
from features.highlights import find_highlights


def main() -> int:
    captions = [
        {
            "text": "오늘 게임 진행 상황을 차분하게 설명합니다.",
            "offset_sec": 1.0,
            "confidence": 0.9,
        },
        {
            "text": "이 장면 정말 웃겨요 ㅋㅋㅋㅋ!",
            "offset_sec": 10.0,
            "confidence": 0.9,
        },
    ]
    disabled = {
        "strong_expression": 0.0,
        "laughter": 0.0,
        "keyword": 0.0,
        "topic_change": 0.0,
    }
    emphasized = {
        **disabled,
        "strong_expression": 1.0,
        "laughter": 2.0,
    }
    low = find_highlights(
        captions,
        limit=2,
        weights=disabled,
    )
    high = find_highlights(
        captions,
        limit=2,
        weights=emphasized,
    )
    low_target = next(
        item for item in low if "ㅋㅋ" in item["text"]
    )
    high_target = next(
        item for item in high if "ㅋㅋ" in item["text"]
    )
    assert high_target["score"] > low_target["score"] + 2.9
    assert high_target["score_parts"]["laughter"] == 2.0
    assert high_target["score_parts"]["strong_expression"] == 1.0

    config = copy.deepcopy(load_config())
    config["archive"]["highlight_weights"] = emphasized
    validate_config(config)

    invalid = copy.deepcopy(config)
    invalid["archive"]["highlight_weights"]["laughter"] = 3.1
    try:
        validate_config(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid highlight weight was accepted")

    with tempfile.TemporaryDirectory() as directory:
        archive = TranscriptArchive(Path(directory) / "analysis.sqlite3")
        archive.create_session(
            "analysis-session",
            source="microphone",
            engine="test",
        )
        for caption in captions:
            archive.add_caption(
                "analysis-session",
                text=caption["text"],
                offset_sec=caption["offset_sec"],
                confidence=caption["confidence"],
                engine="test",
            )
        archive.close_session("analysis-session")
        report = build_session_report(
            archive,
            "analysis-session",
            config,
        )

    settings = report["analysis_settings"]
    assert settings["summary_max_bullets"] == 5
    assert settings["highlight_weights"]["laughter"] == 2.0
    assert report["highlights"][0]["score_parts"]

    html = (ROOT / "overlay" / "admin.html").read_text(
        encoding="utf-8"
    )
    for value in (
        'id="summaryMaxBullets"',
        'id="topicWindowCaptions"',
        'id="topicSimilarityThreshold"',
        'id="topicCooldownSec"',
        'id="highlightLimit"',
        'id="highlightLaughterWeight"',
        'id="saveAnalysis"',
        '["analysis", "분석", "Analysis"]',
        "analysisPresets",
    ):
        assert value in html, value

    print("analysis_tuning_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())