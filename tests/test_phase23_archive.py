from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config, validate_config
from core.transcript_archive import TranscriptArchive
from features.broadcast_report import build_session_report, write_session_report
from features.highlights import find_highlights
from features.summarizer import summarize_captions, tokenize
from features.topic_detector import detect_topic_transitions


def sample_archive(path: Path) -> tuple[TranscriptArchive, str]:
    archive = TranscriptArchive(path, bucket_minutes=30)
    session_id = "test-session"
    archive.create_session(
        session_id,
        started_at="2026-07-11T10:00:00+09:00",
        source="microphone",
        engine="test",
    )
    captions = [
        (5, "샹도웨쭈 발음을 샌드위치로 바꿉니다."),
        (120, "마이크 자막 설정을 점검합니다."),
        (1799, "첫 구간 요약을 확인합니다."),
        (1810, "게임 레이드 보스 전투를 시작합니다!"),
        (1950, "보스 패턴을 피하고 공격합니다 ㅋㅋ"),
        (3610, "부산 여행 일정과 맛집을 이야기합니다."),
    ]
    for offset, text in captions:
        archive.add_caption(
            session_id,
            text=text,
            offset_sec=offset,
            confidence=0.9,
            engine="test",
        )
    return archive, session_id


def test_archive_queries(temp: Path) -> None:
    archive, session_id = sample_archive(temp / "archive.sqlite3")
    duplicate = archive.add_caption(
        session_id,
        text="부산 여행 일정과 맛집을 이야기합니다.",
        offset_sec=3610,
        confidence=0.9,
        engine="test",
    )
    assert duplicate is None
    assert archive.get_session(session_id)["caption_count"] == 6
    assert [item["bucket_index"] for item in archive.timeline(session_id)] == [0, 1, 2]
    assert len(archive.get_captions(session_id, bucket_index=1)) == 2
    rolling = archive.get_captions(
        session_id,
        start_offset_sec=1810,
        end_offset_sec=3610,
        limit=None,
    )
    assert len(rolling) == 3
    assert archive.search(session_id, "샌드위치")[0]["offset_sec"] == 5
    stats = archive.stats(session_id)
    assert stats["caption_count"] == 6
    assert stats["duration_sec"] == 3610


def test_analysis_and_report(temp: Path) -> None:
    archive, session_id = sample_archive(temp / "analysis.sqlite3")
    captions = archive.get_captions(session_id)
    summary = summarize_captions(captions[:3], max_bullets=3)
    assert summary["caption_count"] == 3
    assert summary["bullets"]
    assert summary["keywords"]
    topics = detect_topic_transitions(
        captions,
        window_captions=2,
        similarity_threshold=0.8,
        cooldown_sec=0,
    )
    assert len(topics) >= 2
    highlights = find_highlights(captions, topics, limit=3)
    assert highlights
    report = build_session_report(archive, session_id, load_config())
    assert len(report["summaries"]) == 3
    paths = write_session_report(archive, session_id, temp / "report", load_config())
    assert Path(paths["json"]).exists()
    assert "30분 요약" in Path(paths["markdown"]).read_text(encoding="utf-8")


def test_metadata_and_slang_history(temp: Path) -> None:
    archive, session_id = sample_archive(temp / "metadata.sqlite3")
    updated = archive.update_session_meta(
        session_id,
        title="테스트 방송",
        vod_url="https://example.com/vod",
        vod_start_offset_sec=-12,
    )
    assert updated["title"] == "테스트 방송"
    assert updated["vod_start_offset_sec"] == -12
    archive.record_slang_change(
        action="added",
        source="샹도웨쭈",
        target="샌드위치",
    )
    history = archive.list_slang_history()
    assert history[0]["source"] == "샹도웨쭈"
    assert history[0]["action"] == "added"


def test_korean_keyword_normalization() -> None:
    tokens = tokenize("오늘 방송에서 부산 여행과 맛집을 이야기합니다")
    assert "부산" in tokens
    assert "여행" in tokens
    assert "맛집" in tokens
    assert "오늘" not in tokens
    assert "방송" not in tokens


def test_archive_config_validation() -> None:
    config = copy.deepcopy(load_config())
    validate_config(config)
    config["archive"]["bucket_minutes"] = 0
    try:
        validate_config(config)
    except ValueError:
        return
    raise AssertionError("invalid archive.bucket_minutes was accepted")


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        test_archive_queries(temp)
        test_analysis_and_report(temp)
        test_metadata_and_slang_history(temp)
    test_korean_keyword_normalization()
    test_archive_config_validation()
    print("phase23_archive_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

