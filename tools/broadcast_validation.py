from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.archive_maintenance import recover_unclean_sessions
from core.config import load_config
from core.transcript_archive import TranscriptArchive
from tools.summarize_session import latest_session_jsonl, summarize


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * max(0.0, min(percent, 100.0)) / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _load_records(path: Path) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    invalid_lines = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines += 1
                continue
            if isinstance(value, dict):
                records.append(value)
            else:
                invalid_lines += 1
    return records, invalid_lines


def _load_memory_csv(path: Path) -> list[dict[str, float | None]]:
    if not path.exists():
        return []
    rows: list[dict[str, float | None]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            parsed: dict[str, float | None] = {}
            for key in (
                "elapsed_sec",
                "rss_mb",
                "gpu_used_mb",
                "gpu_total_mb",
            ):
                raw = row.get(key, "")
                try:
                    parsed[key] = float(raw) if raw not in {None, ""} else None
                except ValueError:
                    parsed[key] = None
            rows.append(parsed)
    return rows


def _identity(offset_sec: Any, text: Any) -> tuple[int, str]:
    try:
        offset_ms = int(round(float(offset_sec or 0.0) * 1000.0))
    except (TypeError, ValueError):
        offset_ms = 0
    return offset_ms, str(text or "").strip()


def _memory_metrics(rows: list[dict[str, float | None]]) -> dict[str, Any]:
    rss_points = sorted(
        (
            (float(row["elapsed_sec"]), float(row["rss_mb"]))
            for row in rows
            if row.get("elapsed_sec") is not None
            and row.get("rss_mb") is not None
        ),
        key=lambda point: point[0],
    )
    rss = [point[1] for point in rss_points]
    gpu = [
        float(row["gpu_used_mb"])
        for row in rows
        if row.get("gpu_used_mb") is not None
    ]
    gpu_totals = [
        float(row["gpu_total_mb"])
        for row in rows
        if row.get("gpu_total_mb") is not None
    ]
    gpu_ratios = [
        float(row["gpu_used_mb"]) / float(row["gpu_total_mb"])
        for row in rows
        if row.get("gpu_used_mb") is not None
        and row.get("gpu_total_mb") not in {None, 0.0}
    ]
    rss_slope = None
    if len(rss_points) >= 2 and rss_points[-1][0] > rss_points[0][0]:
        elapsed_hours = (rss_points[-1][0] - rss_points[0][0]) / 3600.0
        rss_slope = (rss_points[-1][1] - rss_points[0][1]) / elapsed_hours
    return {
        "sample_count": len(rows),
        "rss_start_mb": rss[0] if rss else None,
        "rss_end_mb": rss[-1] if rss else None,
        "rss_peak_mb": max(rss) if rss else None,
        "rss_slope_mb_per_hour": rss_slope,
        "gpu_peak_mb": max(gpu) if gpu else None,
        "gpu_total_mb": max(gpu_totals) if gpu_totals else None,
        "gpu_peak_ratio": max(gpu_ratios) if gpu_ratios else None,
    }


def build_validation_report(
    session_jsonl: Path,
    archive: TranscriptArchive,
    config: dict[str, Any],
) -> dict[str, Any]:
    records, invalid_lines = _load_records(session_jsonl)
    session_id = session_jsonl.parent.name
    basic = summarize(session_jsonl)
    event_counts = Counter(str(row.get("event_type", "unknown")) for row in records)
    confirmed = [row for row in records if row.get("event_type") == "caption_confirmed"]
    hidden = [row for row in records if row.get("event_type") == "caption_hidden"]
    engine_errors = [row for row in records if row.get("event_type") == "engine_error"]
    stt_elapsed: list[float] = []
    hidden_reasons: Counter[str] = Counter()
    for record in records:
        metadata = record.get("metadata") or {}
        extra = record.get("extra") or {}
        value = extra.get("stt_elapsed_sec")
        if value is None:
            value = metadata.get("stt_elapsed_sec")
        if isinstance(value, (int, float)):
            stt_elapsed.append(float(value))
    for record in hidden:
        metadata = record.get("metadata") or {}
        hidden_reasons[str(metadata.get("defense_reason", "unknown"))] += 1

    timestamps = [
        parsed
        for parsed in (_parse_timestamp(row.get("ts")) for row in records)
        if parsed is not None
    ]
    first_caption_timestamp = next(
        (
            parsed
            for parsed in (
                _parse_timestamp(row.get("ts")) for row in confirmed
            )
            if parsed is not None
        ),
        None,
    )
    wall_duration = (
        (timestamps[-1] - timestamps[0]).total_seconds()
        if len(timestamps) >= 2
        else 0.0
    )
    time_to_first_caption = (
        (first_caption_timestamp - timestamps[0]).total_seconds()
        if timestamps and first_caption_timestamp is not None
        else None
    )
    audio_duration = max(
        (float(row.get("audio_offset_sec", 0.0) or 0.0) for row in records),
        default=0.0,
    )

    jsonl_identities = Counter(
        _identity(row.get("audio_offset_sec"), row.get("text"))
        for row in confirmed
    )
    session = archive.get_session(session_id)
    archived_captions = (
        archive.get_captions(session_id, limit=None) if session is not None else []
    )
    archive_identities = Counter(
        _identity(row.get("offset_sec"), row.get("text"))
        for row in archived_captions
    )
    jsonl_unique = set(jsonl_identities)
    archive_unique = set(archive_identities)
    missing_in_archive = sorted(jsonl_unique - archive_unique)
    extra_in_archive = sorted(archive_unique - jsonl_unique)
    archive_parity = {
        "session_found": session is not None,
        "exact_unique_match": not missing_in_archive and not extra_in_archive,
        "jsonl_confirmed_count": len(confirmed),
        "jsonl_unique_count": len(jsonl_unique),
        "archive_caption_count": len(archived_captions),
        "jsonl_duplicate_count": sum(
            count - 1 for count in jsonl_identities.values() if count > 1
        ),
        "missing_in_archive_count": len(missing_in_archive),
        "extra_in_archive_count": len(extra_in_archive),
        "missing_samples": [
            {"offset_ms": offset, "text": text}
            for offset, text in missing_in_archive[:20]
        ],
        "extra_samples": [
            {"offset_ms": offset, "text": text}
            for offset, text in extra_in_archive[:20]
        ],
    }

    annotations = archive.list_annotations(session_id, limit=5000) if session else []
    annotation_counts = Counter(item["kind"] for item in annotations)
    memory = _memory_metrics(_load_memory_csv(session_jsonl.parent / "memory.csv"))
    latency = {
        "sample_count": len(stt_elapsed),
        "average_sec": (sum(stt_elapsed) / len(stt_elapsed)) if stt_elapsed else None,
        "p50_sec": percentile(stt_elapsed, 50),
        "p95_sec": percentile(stt_elapsed, 95),
        "max_sec": max(stt_elapsed) if stt_elapsed else None,
        "time_to_first_caption_sec": time_to_first_caption,
    }

    checks: list[dict[str, Any]] = []

    def add_check(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    add_check(
        "jsonl_parse",
        "PASS" if invalid_lines == 0 else "WARN",
        f"invalid_lines={invalid_lines}",
    )
    add_check(
        "archive_parity",
        "PASS" if archive_parity["exact_unique_match"] else "FAIL",
        "missing={missing} extra={extra}".format(
            missing=archive_parity["missing_in_archive_count"],
            extra=archive_parity["extra_in_archive_count"],
        ),
    )
    add_check(
        "engine_errors",
        "PASS" if not engine_errors else "FAIL",
        f"count={len(engine_errors)}",
    )
    add_check(
        "failed_recoveries",
        "PASS" if int(basic.get("failed_recoveries", 0)) == 0 else "FAIL",
        f"count={basic.get('failed_recoveries', 0)}",
    )
    streaming = config.get("streaming", {})
    latency_target = float(
        streaming.get("stt_latency_target_p95_sec", 4.0)
    )
    latency_p95 = latency["p95_sec"]
    add_check(
        "stt_p95",
        "PASS"
        if latency_p95 is None or latency_p95 <= latency_target
        else "WARN",
        f"p95={latency_p95} target={latency_target}",
    )
    leak_threshold = float(
        config.get("monitoring", {}).get("leak_threshold_mb_per_hour", 50.0)
    )
    slope = memory["rss_slope_mb_per_hour"]
    add_check(
        "memory_slope",
        "PASS" if slope is None or slope <= leak_threshold else "WARN",
        f"slope_mb_per_hour={slope} threshold={leak_threshold}",
    )
    gpu_ratio = memory["gpu_peak_ratio"]
    add_check(
        "gpu_headroom",
        "PASS" if gpu_ratio is None or gpu_ratio < 0.95 else "WARN",
        f"peak_ratio={gpu_ratio}",
    )
    superseded_pending = int(
        basic.get("superseded_pending_jobs", 0) or 0
    )
    skipped_pending = int(
        basic.get("skipped_pending_jobs", 0) or 0
    )
    pending_pressure = superseded_pending + skipped_pending
    pending_pressure_limit = max(10, len(confirmed) * 2)
    add_check(
        "pending_preview_pressure",
        "PASS" if pending_pressure <= pending_pressure_limit else "WARN",
        (
            f"superseded={superseded_pending} "
            f"skipped={skipped_pending} "
            f"limit={pending_pressure_limit}"
        ),
    )
    process_restarts = int(basic.get("process_restarts", 0) or 0)
    add_check(
        "stt_auto_recovery",
        "PASS" if process_restarts == 0 else "REVIEW",
        (
            f"process_restarts={process_restarts} "
            f"recovered={basic.get('recovered_confirmed_jobs', 0)}"
        ),
    )
    audio_source_restarts = int(
        basic.get("audio_source_restarts", 0) or 0
    )
    add_check(
        "audio_source_recovery",
        "PASS" if audio_source_restarts == 0 else "REVIEW",
        f"restarts={audio_source_restarts}",
    )
    user_issue_count = sum(
        annotation_counts.get(kind, 0)
        for kind in ("hallucination", "missed", "wrong_word")
    )
    add_check(
        "user_feedback",
        "PASS" if user_issue_count == 0 else "REVIEW",
        f"issues={user_issue_count} good={annotation_counts.get('good', 0)}",
    )

    statuses = {item["status"] for item in checks}
    if "FAIL" in statuses:
        verdict = "FAIL"
    elif "WARN" in statuses:
        verdict = "WARN"
    elif "REVIEW" in statuses:
        verdict = "REVIEW"
    else:
        verdict = "PASS"

    recommendations: list[str] = []
    if not archive_parity["exact_unique_match"]:
        recommendations.append(
            "JSONL과 SQLite 자막 수가 다릅니다. 지원 묶음을 만든 뒤 "
            "세션 복구를 먼저 실행하세요."
        )
    if engine_errors:
        recommendations.append(
            "엔진 오류가 기록됐습니다. 마지막 오류와 GPU 메모리 사용량을 "
            "확인하고 안정형 GPU 프리셋을 적용하세요."
        )
    if latency_p95 is not None and latency_p95 > latency_target:
        if gpu_ratio is not None and gpu_ratio >= 0.9:
            recommendations.append(
                "STT P95 지연과 GPU 사용량이 함께 높습니다. 모델 또는 beam_size를 "
                "낮추고 안정형 GPU 프리셋으로 다시 측정하세요."
            )
        else:
            recommendations.append(
                "STT P95 지연이 목표를 넘었습니다. chunk_seconds를 0.2초씩 "
                "줄이고 pending_debounce_sec를 0.1초씩 늘려 다시 측정하세요."
            )
    if pending_pressure > pending_pressure_limit:
        recommendations.append(
            "미리보기 자막 대기 압력이 높습니다. 확정 자막은 보존되지만 "
            "pending_debounce_sec를 늘리거나 pending 미리보기를 끄면 지연이 줄어듭니다."
        )
    if process_restarts:
        recommendations.append(
            "STT 자동 복구가 발생했습니다. 복구된 자막 수와 recovery_reason을 "
            "확인하고 30분 안정성 테스트를 한 번 더 실행하세요."
        )
    if int(basic.get("failed_recoveries", 0) or 0):
        recommendations.append(
            "STT 복구 실패가 있습니다. 지원 묶음을 만들어 세션 로그와 함께 "
            "분석해야 합니다."
        )
    if audio_source_restarts:
        recommendations.append(
            "오디오 입력이 재시작됐습니다. 장치 독점 모드, USB 절전, "
            "기본 입력 장치 변경 여부를 확인하세요."
        )
    if annotation_counts.get("hallucination", 0):
        recommendations.append(
            "환각 표시가 있습니다. vad.speech_threshold를 0.03 높이거나 "
            "defense.hallucination_score_threshold를 0.05 낮춰 한 항목씩 비교하세요."
        )
    if annotation_counts.get("missed", 0):
        recommendations.append(
            "발화 누락 표시가 있습니다. vad.speech_threshold를 0.03 낮추고 "
            "audio.pre_roll_sec를 0.5초로 늘린 뒤 짧은 인사말을 재시험하세요."
        )
    if annotation_counts.get("wrong_word", 0):
        recommendations.append(
            "단어 오인식 표시를 유행어 사전의 입력 예시 또는 Whisper prompt "
            "후보로 검토하세요."
        )

    return {
        "report_version": 2,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "verdict": verdict,
        "session_id": session_id,
        "session_jsonl": str(session_jsonl),
        "session": session,
        "duration": {
            "wall_sec": wall_duration,
            "audio_offset_sec": audio_duration,
        },
        "event_counts": dict(event_counts),
        "hidden_reasons": dict(hidden_reasons),
        "latency": latency,
        "memory": memory,
        "archive_parity": archive_parity,
        "recovery": {
            key: basic.get(key)
            for key in (
                "dropped_jobs",
                "superseded_pending_jobs",
                "skipped_pending_jobs",
                "child_heartbeat_count",
                "process_restarts",
                "recovered_confirmed_jobs",
                "failed_recoveries",
                "committed_confirmed_jobs",
                "duplicate_confirmed_results",
                "audio_source_restarts",
                "max_recovery_attempt",
                "recovery_reasons",
            )
        },
        "annotation_counts": dict(annotation_counts),
        "annotations": annotations,
        "diagnostics": {
            "latency_target_p95_sec": latency_target,
            "pending_preview_pressure": pending_pressure,
            "pending_preview_pressure_limit": pending_pressure_limit,
            "hallucination_markers": annotation_counts.get(
                "hallucination", 0
            ),
            "missed_markers": annotation_counts.get("missed", 0),
            "wrong_word_markers": annotation_counts.get(
                "wrong_word", 0
            ),
            "hallucination_filtered": sum(
                hidden_reasons.get(reason, 0)
                for reason in (
                    "hallucination_score",
                    "hallucination_pattern",
                    "no_speech_probability",
                    "low_voice_ratio",
                )
            ),
        },
        "checks": checks,
        "recommendations": recommendations,
    }


def _format_number(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    latency = report["latency"]
    memory = report["memory"]
    parity = report["archive_parity"]
    diagnostics = report.get("diagnostics", {})
    lines = [
        f"# 방송 검증 보고서: {report['session_id']}",
        "",
        f"- 판정: **{report['verdict']}**",
        f"- 생성: {report['created_at']}",
        f"- 방송 경과: {_format_number(report['duration']['wall_sec'])}초",
        f"- 확정 자막: {parity['jsonl_confirmed_count']}개",
        "",
        "## 지연",
        "",
        f"- 첫 자막: {_format_number(latency['time_to_first_caption_sec'])}초",
        f"- STT P50: {_format_number(latency['p50_sec'])}초",
        f"- STT P95: {_format_number(latency['p95_sec'])}초",
        f"- STT 최대: {_format_number(latency['max_sec'])}초",
        "",
        "## 저장 일치",
        "",
        f"- JSONL 고유 자막: {parity['jsonl_unique_count']}개",
        f"- SQLite 자막: {parity['archive_caption_count']}개",
        f"- DB 누락: {parity['missing_in_archive_count']}개",
        f"- DB 추가: {parity['extra_in_archive_count']}개",
        "",
        "## 자원",
        "",
        f"- RSS 시작/끝: {_format_number(memory['rss_start_mb'])} / {_format_number(memory['rss_end_mb'])} MB",
        f"- RSS 시간당 변화: {_format_number(memory['rss_slope_mb_per_hour'])} MB/h",
        f"- GPU 최대: {_format_number(memory['gpu_peak_mb'])} / {_format_number(memory['gpu_total_mb'])} MB",
        "",
        "## 자동 진단",
        "",
        f"- STT P95 목표: {_format_number(diagnostics.get('latency_target_p95_sec'))}초",
        (
            "- 미리보기 대기 압력: "
            f"{diagnostics.get('pending_preview_pressure', 0)} / "
            f"{diagnostics.get('pending_preview_pressure_limit', 0)}"
        ),
        f"- 환각 차단 자막: {diagnostics.get('hallucination_filtered', 0)}개",
        f"- 환각/누락 표시: {diagnostics.get('hallucination_markers', 0)} / {diagnostics.get('missed_markers', 0)}",
        "",
        "## 검사",
        "",
    ]
    for check in report["checks"]:
        lines.append(
            f"- **{check['status']}** `{check['name']}`: {check['detail']}"
        )
    lines.extend(["", "## 사용자 표시", ""])
    if report["annotations"]:
        annotation_labels = {
            "hallucination": "환각 자막",
            "missed": "말했지만 누락",
            "wrong_word": "단어 오인식",
            "good": "정상 인식",
        }
        for item in report["annotations"]:
            lines.append(
                "- `{offset}` **{kind}** {caption} {note}".format(
                    offset=_format_number(item["offset_sec"]),
                    kind=annotation_labels.get(item["kind"], item["kind"]),
                    caption=item["caption_text"],
                    note=item["note"],
                ).rstrip()
            )
    else:
        lines.append("- 표시 없음")
    lines.extend(["", "## 권장 조치", ""])
    if report["recommendations"]:
        lines.extend(f"- {item}" for item in report["recommendations"])
    else:
        lines.append("- 즉시 필요한 조치 없음")
    lines.append("")
    return "\n".join(lines)


def write_validation_report(
    session_jsonl: Path,
    archive: TranscriptArchive,
    config: dict[str, Any],
) -> dict[str, str]:
    report = build_validation_report(session_jsonl, archive, config)
    json_path = session_jsonl.parent / "validation_report.json"
    markdown_path = session_jsonl.parent / "validation_report.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return {
        "json": str(json_path),
        "markdown": str(markdown_path),
        "verdict": str(report["verdict"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a broadcast validation report")
    parser.add_argument("path", nargs="?", help="logs/<session>/session.jsonl")
    parser.add_argument("--latest", action="store_true")
    args = parser.parse_args()
    config = load_config()
    archive = TranscriptArchive.from_config(config)
    recovered = recover_unclean_sessions(archive, config)
    if recovered:
        print(f"recovered_unclean_sessions: {len(recovered)}")
    path = Path(args.path) if args.path else latest_session_jsonl()
    if path is None or not path.exists():
        print("No session.jsonl found. Run the app first.")
        return 1
    paths = write_validation_report(path.resolve(), archive, config)
    print(f"validation_verdict: {paths['verdict']}")
    print(f"validation_json: {paths['json']}")
    print(f"validation_markdown: {paths['markdown']}")
    return 2 if paths["verdict"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())

