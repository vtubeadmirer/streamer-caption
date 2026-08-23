from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.admin_server import AdminServer, AdminState
from core.archive_maintenance import (
    create_archive_backup,
    prune_archive,
    recover_unclean_sessions,
)
from core.config import load_config
from core.transcript_archive import TranscriptArchive
from features.broadcast_report import build_session_report, render_report_markdown
from tools.broadcast_validation import write_validation_report
from tools.make_support_bundle import latest_log_dir, latest_log_dirs


def get_json(base_url: str, path: str) -> dict:
    with urlopen(base_url + path, timeout=3.0) as response:
        return json.loads(response.read().decode("utf-8"))


def get_text(base_url: str, path: str) -> str:
    with urlopen(base_url + path, timeout=3.0) as response:
        return response.read().decode("utf-8")


def post_json(base_url: str, path: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        base_url + path,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=3.0) as response:
        return json.loads(response.read().decode("utf-8"))


def test_annotations_reports_and_validation(temp: Path) -> None:
    archive = TranscriptArchive(temp / "validation.sqlite3", bucket_minutes=30)
    session_id = "2026-07-11_120000"
    archive.create_session(
        session_id,
        started_at="2026-07-11T12:00:00+09:00",
        source="microphone",
        engine="test",
    )
    archive.add_caption(
        session_id,
        text="샌드위치 테스트입니다.",
        offset_sec=2.5,
        confidence=0.92,
        engine="test",
        created_at="2026-07-11T12:00:02.500+09:00",
    )
    annotation = archive.add_annotation(
        session_id,
        kind="wrong_word",
        offset_sec=2.5,
        note="샹도웨쭈로 들림",
        caption_text="샌드위치 테스트입니다.",
    )
    assert annotation["kind"] == "wrong_word"
    assert archive.archive_info()["annotation_count"] == 1

    session_dir = temp / session_id
    session_dir.mkdir()
    records = [
        {
            "ts": "2026-07-11T12:00:00+09:00",
            "event_type": "session_started",
            "text": "",
            "audio_offset_sec": 0.0,
        },
        {
            "ts": "2026-07-11T12:00:02.500+09:00",
            "event_type": "caption_confirmed",
            "text": "샌드위치 테스트입니다.",
            "audio_offset_sec": 2.5,
            "extra": {"stt_elapsed_sec": 0.42},
        },
        {
            "ts": "2026-07-11T12:00:05+09:00",
            "event_type": "session_stopped",
            "text": "",
            "audio_offset_sec": 5.0,
        },
    ]
    session_jsonl = session_dir / "session.jsonl"
    session_jsonl.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records),
        encoding="utf-8",
    )
    (session_dir / "memory.csv").write_text(
        "elapsed_sec,rss_mb,gpu_used_mb,gpu_total_mb\n"
        "0,,2000,16000\n"
        "1800,100,3000,16000\n"
        "3600,110,4000,16000\n",
        encoding="utf-8",
    )

    config = copy.deepcopy(load_config())
    paths = write_validation_report(session_jsonl, archive, config)
    report = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert report["verdict"] == "REVIEW", report["checks"]
    assert report["report_version"] == 2
    assert report["diagnostics"]["latency_target_p95_sec"] == 4.0
    assert report["archive_parity"]["exact_unique_match"] is True
    assert report["latency"]["p95_sec"] == 0.42
    assert report["memory"]["rss_slope_mb_per_hour"] == 20.0
    assert report["annotation_counts"]["wrong_word"] == 1
    assert "단어 오인식" in Path(paths["markdown"]).read_text(encoding="utf-8")

    broadcast_report = build_session_report(archive, session_id, config)
    assert broadcast_report["report_version"] == 2
    assert broadcast_report["annotations"][0]["note"] == "샹도웨쭈로 들림"
    assert "방송 중 사용자 표시" in render_report_markdown(broadcast_report)
    assert archive.delete_annotation(annotation["id"], session_id) is True


def test_unclean_recovery_backup_and_retention(temp: Path) -> None:
    archive = TranscriptArchive(temp / "maintenance.sqlite3")
    config = copy.deepcopy(load_config())
    config["log"]["dir"] = str(temp / "logs")

    archive.create_session(
        "dead-session",
        started_at="2020-01-01T00:00:00+09:00",
        metadata={"process_id": 2_147_000_000},
    )
    archive.add_caption(
        "dead-session",
        text="복구할 자막",
        offset_sec=3.0,
        created_at="2020-01-01T00:00:03+09:00",
    )
    archive.create_session(
        "live-session",
        started_at="2026-07-11T12:00:00+09:00",
        metadata={"process_id": os.getpid()},
    )
    recovered = recover_unclean_sessions(
        archive,
        config,
        generate_reports=False,
        stale_after_sec=0,
    )
    assert [item["session_id"] for item in recovered] == ["dead-session"]
    dead = archive.get_session("dead-session")
    assert dead["ended_at"] == "2020-01-01T00:00:03+09:00"
    assert dead["metadata"]["unclean_shutdown_recovered"] is True
    assert archive.get_session("live-session")["ended_at"] is None

    archive.add_annotation(
        "dead-session",
        kind="good",
        offset_sec=3.0,
        note="복구 기준",
    )
    backup = create_archive_backup(archive, temp / "backups")
    assert backup.exists()
    with zipfile.ZipFile(backup) as bundle:
        names = set(bundle.namelist())
        assert "manifest.json" in names
        assert any(name.startswith("data/transcripts_") for name in names)

    result = prune_archive(archive, retention_days=30)
    assert result["deleted_session_ids"] == ["dead-session"]
    assert archive.get_session("dead-session") is None
    assert archive.get_session("live-session") is not None


def test_support_bundle_prefers_session_log(temp: Path) -> None:
    log_root = temp / "split-logs"
    session_dir = log_root / "2026-07-11_120000"
    memory_only_dir = log_root / "2026-07-11_120001"
    session_dir.mkdir(parents=True)
    (session_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")
    memory_only_dir.mkdir()
    (memory_only_dir / "memory.csv").write_text(
        "elapsed_sec,rss_mb,gpu_used_mb,gpu_total_mb\n",
        encoding="utf-8",
    )
    assert latest_log_dir(log_root) == session_dir
    assert latest_log_dirs(log_root, limit=3) == [session_dir]

def test_admin_annotation_api_and_pages(temp: Path) -> None:
    config = copy.deepcopy(load_config())
    config["archive"]["db_path"] = str(temp / "admin.sqlite3")
    config["archive"]['backup_dir'] = str(temp / "api_backups")
    config["server"]["admin_port"] = 0
    archive = TranscriptArchive.from_config(config)
    archive.create_session(
        "api-session",
        started_at="2026-07-11T13:00:00+09:00",
        source="microphone",
        engine="test",
    )
    archive.close_session(
        "api-session",
        ended_at="2026-07-11T13:10:00+09:00",
    )
    archive.create_session(
        "old-api-session",
        started_at="2020-01-01T00:00:00+09:00",
        source="microphone",
        engine="test",
    )
    archive.close_session(
        "old-api-session",
        ended_at="2020-01-01T01:00:00+09:00",
    )
    state = AdminState()
    state.last_caption = "API 테스트 자막"
    state.last_audio = {"offset_sec": 4.0, "duration_sec": 1.0}
    server = AdminServer(config, state)
    server.start_background()
    assert server._httpd is not None
    port = int(server._httpd.server_address[1])
    base_url = f"http://127.0.0.1:{port}"
    try:
        validate_html = get_text(base_url, "/validate")
        history_html = get_text(base_url, "/history")
        assert 'data-kind="hallucination"' in validate_html
        assert 'id="backupArchive"' in history_html
        assert get_json(base_url, "/api/archive/info")["archive"]["session_count"] == 2

        created = post_json(
            base_url,
            "/api/archive/annotation",
            {
                "session_id": "api-session",
                "kind": "missed",
                "offset_sec": 5.0,
                "note": "작게 말한 부분",
                "caption_text": "API 테스트 자막",
            },
        )["annotation"]
        listed = get_json(
            base_url,
            "/api/archive/annotations?session_id=api-session",
        )
        assert listed["count"] == 1
        assert listed["annotations"][0]["note"] == "작게 말한 부분"
        deleted = post_json(
            base_url,
            "/api/archive/annotation-delete",
            {"session_id": "api-session", "annotation_id": created["id"]},
        )
        assert deleted["deleted"] is True

        exported = post_json(base_url, "/api/archive/export", {})
        assert Path(exported["backup_path"]).parent == temp / "api_backups"
        assert Path(exported["backup_path"]).exists()
        removed = post_json(
            base_url,
            "/api/archive/delete-session",
            {
                "session_id": "api-session",
                "confirm_session_id": "api-session",
            },
        )
        assert removed["deleted"] is True
        assert Path(removed["backup_path"]).exists()
        pruned = post_json(
            base_url,
            "/api/archive/prune",
            {"retention_days": 30},
        )
        assert pruned["deleted_session_ids"] == ["old-api-session"]
        assert Path(pruned["backup_path"]).exists()
        assert get_json(base_url, "/api/archive/info")["archive"]["session_count"] == 0
    finally:
        server.stop()


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        test_annotations_reports_and_validation(temp)
        test_unclean_recovery_backup_and_retention(temp)
        test_support_bundle_prefers_session_log(temp)
        test_admin_annotation_api_and_pages(temp)
    print("validation_workflow_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
