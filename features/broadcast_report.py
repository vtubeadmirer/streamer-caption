from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from core.transcript_archive import TranscriptArchive
from features.highlights import find_highlights
from features.summarizer import extract_keywords, summarize_captions
from features.topic_detector import detect_topic_transitions


def format_offset(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def build_session_report(
    archive: TranscriptArchive,
    session_id: str,
    config: dict[str, Any] | None = None,
    *,
    include_summaries: bool = True,
) -> dict[str, Any]:
    config = config or {}
    archive_config = config.get("archive", {})
    session = archive.get_session(session_id)
    if session is None:
        raise ValueError(f"unknown session: {session_id}")
    captions = archive.get_captions(session_id, limit=None)
    timeline = archive.timeline(session_id)
    max_bullets = int(archive_config.get("summary_max_bullets", 5))
    summaries = (
        [
            summarize_captions(
                archive.get_captions(
                    session_id,
                    bucket_index=int(bucket["bucket_index"]),
                    limit=None,
                ),
                bucket_index=int(bucket["bucket_index"]),
                bucket_minutes=archive.bucket_minutes,
                max_bullets=max_bullets,
            )
            for bucket in timeline
        ]
        if include_summaries
        else []
    )
    topics = detect_topic_transitions(
        captions,
        window_captions=int(archive_config.get("topic_window_captions", 6)),
        similarity_threshold=float(
            archive_config.get("topic_similarity_threshold", 0.22)
        ),
        cooldown_sec=float(archive_config.get("topic_cooldown_sec", 30.0)),
    )
    highlights = find_highlights(
        captions,
        topics,
        limit=int(archive_config.get("highlight_limit", 10)),
        weights=archive_config.get("highlight_weights"),
    )
    annotations = archive.list_annotations(session_id, limit=5000)
    return {
        "report_version": 2,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "analysis_method": "local_deterministic_prototype",
        "analysis_settings": {
            "summary_max_bullets": max_bullets,
            "topic_window_captions": int(
                archive_config.get("topic_window_captions", 6)
            ),
            "topic_similarity_threshold": float(
                archive_config.get(
                    "topic_similarity_threshold",
                    0.22,
                )
            ),
            "topic_cooldown_sec": float(
                archive_config.get("topic_cooldown_sec", 30.0)
            ),
            "highlight_limit": int(
                archive_config.get("highlight_limit", 10)
            ),
            "highlight_weights": dict(
                archive_config.get("highlight_weights") or {}
            ),
        },
        "session": session,
        "stats": archive.stats(session_id),
        "keywords": extract_keywords(captions, limit=20),
        "timeline": timeline,
        "summaries": summaries,
        "topics": topics,
        "highlights": highlights,
        "annotations": annotations,
    }


def render_report_markdown(report: dict[str, Any]) -> str:
    session = report["session"]
    stats = report["stats"]
    title = session.get("title") or session["id"]
    lines = [
        f"# 방송 리포트: {title}",
        "",
        f"- 세션: `{session['id']}`",
        f"- 시작: {session['started_at']}",
        f"- 길이: {format_offset(stats['duration_sec'])}",
        f"- 확정 자막: {stats['caption_count']}개",
        f"- 분석 방식: {report['analysis_method']}",
        "",
        "## 30분 요약",
        "",
    ]
    for summary in report["summaries"]:
        lines.append(
            f"### {format_offset(summary['window_start_sec'])} - "
            f"{format_offset(summary['window_end_sec'])} · {summary['headline']}"
        )
        for bullet in summary["bullets"]:
            lines.append(
                f"- `{format_offset(bullet['offset_sec'])}` {bullet['text']}"
            )
        lines.append("")
    lines.extend(["## 주제 전환", ""])
    for topic in report["topics"]:
        lines.append(
            f"- `{format_offset(topic['offset_sec'])}` {topic['label']}"
        )
    lines.extend(["", "## 하이라이트 후보", ""])
    for highlight in report["highlights"]:
        lines.append(
            f"- `{format_offset(highlight['offset_sec'])}` {highlight['text']}"
        )
    annotations = report.get("annotations", [])
    if annotations:
        labels = {
            "hallucination": "환각 자막",
            "missed": "말했지만 누락",
            "wrong_word": "단어 오인식",
            "good": "정상 인식",
        }
        lines.extend(["", "## 방송 중 사용자 표시", ""])
        for annotation in annotations:
            note = f" · {annotation['note']}" if annotation.get("note") else ""
            label = labels.get(annotation.get("kind"), annotation.get("kind", "표시"))
            lines.append(
                f"- `{format_offset(annotation['offset_sec'])}` {label}{note}"
            )
    lines.append("")
    return "\n".join(lines)


def write_session_report(
    archive: TranscriptArchive,
    session_id: str,
    output_dir: str | Path,
    config: dict[str, Any] | None = None,
) -> dict[str, str]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    report = build_session_report(archive, session_id, config)
    json_path = output / "broadcast_report.json"
    markdown_path = output / "broadcast_report.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_report_markdown(report), encoding="utf-8")
    annotations = archive.list_annotations(session_id, limit=5000)
    return {"json": str(json_path), "markdown": str(markdown_path)}

