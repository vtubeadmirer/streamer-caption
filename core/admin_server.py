from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from core import app_info
from core.app_control import AppController
from core.audio_capture import list_input_devices
from core.audio_sources import list_system_audio_devices
from core.archive_maintenance import create_archive_backup, prune_archive
from core.config import CONFIG_LIFETIME, DEFAULTS, PROJECT_ROOT, deep_merge, load_config, save_config
from core.profiles import apply_profile, list_profiles
from core.transcript_archive import TranscriptArchive
from features.broadcast_report import build_session_report
from features.summarizer import summarize_captions


def issue_url(bundle: dict[str, Any], note: str) -> str:
    """Prefilled GitHub issue form (field ids match .github/ISSUE_TEMPLATE/bug_report.yml)."""
    app = bundle.get("environment", {}).get("app", {}) if isinstance(bundle.get("environment"), dict) else {}
    version = str(app.get("version") or app_info.app_version())
    first_line = " ".join(note.strip().split())[:60]
    title = f"[버그] v{version}" + (f": {first_line}" if first_line else "")
    params = {
        "template": "bug_report.yml",
        "title": title,
        "version": version,
        "what-happened": note.strip()[:1500],
        "environment": str(bundle.get("environment_text", "")),
    }
    return f"https://github.com/{app_info.updater_repository()}/issues/new?" + urlencode(params)


def _bundle_path_in_reports(raw: str) -> Path:
    if not raw:
        raise ValueError("path required")
    reports = (PROJECT_ROOT / "reports").resolve()
    target = Path(raw).resolve(strict=True)
    if reports not in target.parents or not target.is_file():
        raise ValueError("outside the reports folder")
    return target


def reveal_in_explorer(path: Path) -> None:
    if sys.platform == "win32":
        subprocess.Popen(["explorer", f"/select,{path}"])
    else:  # pragma: no cover - Windows is the shipping platform
        subprocess.Popen(["xdg-open", str(path.parent)])


class AdminState:
    def __init__(self) -> None:
        self.started = False
        self.last_caption = ""
        self.last_error = ""
        self.pipeline_warning = ""
        self.audio_restart_count = 0
        self.caption_count = 0
        self.pending_caption_count = 0
        self.hidden_caption_count = 0
        self.segment_count = 0
        self.discarded_segment_count = 0
        self.degraded_mode = False
        self.last_memory: dict[str, Any] = {}
        self.last_audio: dict[str, Any] = {}
        self.audio_sources: dict[str, dict[str, Any]] = {}
        self.last_segment: dict[str, Any] = {}
        self.last_stt: dict[str, Any] = {}
        self.control = AppController()

    def to_dict(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "last_caption": self.last_caption,
            "last_error": self.last_error,
            "pipeline_warning": self.pipeline_warning,
            "audio_restart_count": self.audio_restart_count,
            "caption_count": self.caption_count,
            "pending_caption_count": self.pending_caption_count,
            "hidden_caption_count": self.hidden_caption_count,
            "segment_count": self.segment_count,
            "discarded_segment_count": self.discarded_segment_count,
            "degraded_mode": self.degraded_mode,
            "last_memory": self.last_memory,
            "last_audio": self.last_audio,
            "audio_sources": self.audio_sources,
            "last_segment": self.last_segment,
            "last_stt": self.last_stt,
            "restart_requested": self.control.restart_requested,
            "version": app_info.app_version(),
        }


class AdminServer:
    def __init__(self, config: dict[str, Any], state: AdminState | None = None) -> None:
        self.host = config["server"]["host"]
        self.port = int(config["server"]["admin_port"])
        self.state = state or AdminState()
        self.config = config
        self._thread: threading.Thread | None = None
        self._httpd: ThreadingHTTPServer | None = None

    def start_background(self) -> None:
        if self._thread is not None:
            return
        handler = self._make_handler(self.state, self.config)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="admin-http", daemon=True)
        self._thread.start()
        self.state.started = True

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.state.started = False

    @staticmethod
    def _make_handler(
        state: AdminState,
        server_config: dict[str, Any] | None = None,
    ) -> type[BaseHTTPRequestHandler]:
        archive_config = server_config or load_config()
        archive_backup_dir = archive_config.get("archive", {}).get("backup_dir")
        analysis_cache: dict[
            tuple[str, str], tuple[tuple[Any, ...], dict[str, Any]]
        ] = {}
        analysis_cache_lock = threading.Lock()
        archive_store: TranscriptArchive | None = None
        archive_store_lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            server_version = "StreamerCaptionAdmin/0.1"

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query, keep_blank_values=True)
                if path in {"/", "/admin"}:
                    self._send_file(PROJECT_ROOT / "overlay" / "admin.html", "text/html; charset=utf-8")
                    return
                if path in {"/caption", "/caption.html"}:
                    self._send_file(PROJECT_ROOT / "overlay" / "caption.html", "text/html; charset=utf-8")
                    return
                if path in {"/history", "/history.html"}:
                    self._send_file(PROJECT_ROOT / "overlay" / "history.html", "text/html; charset=utf-8")
                    return
                if path in {"/setup", "/setup.html"}:
                    self._send_file(PROJECT_ROOT / "overlay" / "setup.html", "text/html; charset=utf-8")
                    return
                if path in {"/recap", "/recap.html"}:
                    self._send_file(PROJECT_ROOT / "overlay" / "recap.html", "text/html; charset=utf-8")
                    return
                if path in {"/summary", "/summary.html"}:
                    self._send_file(PROJECT_ROOT / "overlay" / "summary.html", "text/html; charset=utf-8")
                    return
                if path in {"/validate", "/validate.html"}:
                    self._send_file(PROJECT_ROOT / "overlay" / "validate.html", "text/html; charset=utf-8")
                    return
                if path == "/admin-theme.css":
                    self._send_file(PROJECT_ROOT / "overlay" / "admin-theme.css", "text/css; charset=utf-8")
                    return
                if path == "/api/config":
                    self._send_json(load_config())
                    return
                if path == "/api/defaults":
                    self._send_json(DEFAULTS)
                    return
                if path == "/api/config-lifetime":
                    self._send_json(CONFIG_LIFETIME)
                    return
                if path == "/api/status":
                    self._send_json(state.to_dict())
                    return
                if path == "/api/devices":
                    result: dict[str, Any] = {
                        "devices": [],
                        "system_devices": [],
                        "errors": {},
                    }
                    try:
                        result["devices"] = [
                            device.__dict__ for device in list_input_devices()
                        ]
                    except Exception as exc:
                        result["errors"]["microphone"] = str(exc)
                    try:
                        result["system_devices"] = [
                            device.__dict__
                            for device in list_system_audio_devices()
                        ]
                    except Exception as exc:
                        result["errors"]["system"] = str(exc)
                    self._send_json(result)
                    return
                if path == "/api/profiles":
                    self._send_json({"profiles": list_profiles()})
                    return
                if path == "/api/slang":
                    self._send_json(self._read_slang_files())
                    return
                if path == "/api/app":
                    self._send_json({**app_info.describe(), "restart_requested": state.control.restart_requested})
                    return
                if path == "/api/update/status":
                    self._send_json(state.control.snapshot())
                    return
                if path.startswith("/api/archive/"):
                    self._handle_archive_get(path, query)
                    return
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                path = parsed.path
                if not self._same_origin():
                    self._send_json({"error": "cross-origin request refused"}, HTTPStatus.FORBIDDEN)
                    return
                if path == "/api/config":
                    payload = self._read_json()
                    if not isinstance(payload, dict):
                        self._send_json({"error": "json object required"}, HTTPStatus.BAD_REQUEST)
                        return
                    config = deep_merge(load_config(), payload)
                    try:
                        save_config(config)
                    except (KeyError, TypeError, ValueError) as exc:
                        self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                        return
                    with analysis_cache_lock:
                        archive_config.clear()
                        archive_config.update(config)
                        analysis_cache.clear()
                    self._send_json(config)
                    return
                if path == "/api/reset":
                    current = load_config()
                    reset = deep_merge(
                        DEFAULTS,
                        {
                            "audio": {
                                "source": current["audio"].get(
                                    "source", "microphone"
                                ),
                                "device": current["audio"]["device"],
                                "system_device": current["audio"].get(
                                    "system_device", ""
                                ),
                            }
                        },
                    )
                    save_config(reset)
                    with analysis_cache_lock:
                        archive_config.clear()
                        archive_config.update(reset)
                        analysis_cache.clear()
                    self._send_json(reset)
                    return
                if path == "/api/profile":
                    payload = self._read_json()
                    if not isinstance(payload, dict) or not payload.get("profile"):
                        self._send_json({"error": "profile required"}, HTTPStatus.BAD_REQUEST)
                        return
                    try:
                        config = apply_profile(
                            str(payload["profile"]),
                            keep_audio_tuning=bool(payload.get("keep_audio_tuning", True)),
                        )
                    except Exception as exc:
                        self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                        return
                    self._send_json(config)
                    return
                if path == "/api/slang":
                    payload = self._read_json()
                    if not isinstance(payload, dict):
                        self._send_json({"error": "json object required"}, HTTPStatus.BAD_REQUEST)
                        return
                    try:
                        self._write_slang_files(payload)
                    except Exception as exc:
                        self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                        return
                    self._send_json(self._read_slang_files())
                    return
                if path == "/api/update/check":
                    self._send_json(state.control.check(force=True))
                    return
                if path == "/api/update/install":
                    try:
                        self._send_json(state.control.start_update())
                    except Exception as exc:
                        self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                if path == "/api/update/skip":
                    payload = self._read_json()
                    version = str(payload.get("version", "")) if isinstance(payload, dict) else ""
                    try:
                        self._send_json(state.control.skip_version(version))
                    except Exception as exc:
                        self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                if path == "/api/app/restart":
                    payload = self._read_json()
                    reason = str(payload.get("reason", "")) if isinstance(payload, dict) else ""
                    result = state.control.request_restart(reason)
                    result["supervised"] = os.environ.get("STREAMER_CAPTION_SUPERVISED") == "1"
                    self._send_json(result)
                    return
                if path == "/api/support/bundle":
                    payload = self._read_json()
                    note = str(payload.get("note", "")) if isinstance(payload, dict) else ""
                    try:
                        from tools.make_support_bundle import make_issue_bundle

                        result = make_issue_bundle(status=state.to_dict(), note=note[:4000])
                    except Exception as exc:
                        self._send_json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                        return
                    result["issue_url"] = issue_url(result, note)
                    self._send_json(result)
                    return
                if path == "/api/support/reveal":
                    payload = self._read_json()
                    raw = str(payload.get("path", "")) if isinstance(payload, dict) else ""
                    try:
                        target = _bundle_path_in_reports(raw)
                    except (OSError, ValueError) as exc:
                        self._send_json({"error": f"not allowed: {exc}"}, HTTPStatus.BAD_REQUEST)
                        return
                    reveal_in_explorer(target)
                    self._send_json({"ok": True, "path": str(target)})
                    return
                if path == "/api/archive/session-meta":
                    self._handle_archive_session_meta()
                    return
                if path in {
                    "/api/archive/annotation",
                    "/api/archive/annotation-delete",
                    "/api/archive/export",
                    "/api/archive/delete-session",
                    "/api/archive/prune",
                }:
                    self._handle_archive_mutation(path)
                    return
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

            def _archive(self) -> TranscriptArchive:
                nonlocal archive_store
                if archive_store is None:
                    with archive_store_lock:
                        if archive_store is None:
                            archive_store = TranscriptArchive.from_config(
                                archive_config
                            )
                return archive_store

            def _handle_archive_get(
                self,
                path: str,
                query: dict[str, list[str]],
            ) -> None:
                try:
                    archive = self._archive()
                    if path == "/api/archive/export-text":
                        session_id = self._required_query(query, "session_id")
                        session = archive.get_session(session_id)
                        if session is None:
                            raise ValueError(f"unknown session: {session_id}")
                        captions = archive.get_captions(session_id, limit=None)
                        lines = []
                        for caption in captions:
                            total_seconds = max(0, int(float(caption["offset_sec"])))
                            hours, remainder = divmod(total_seconds, 3600)
                            minutes, seconds = divmod(remainder, 60)
                            metadata = caption.get("metadata") or {}
                            source_label = str(
                                metadata.get("source_label", "")
                            ).strip()
                            prefix = f"[{source_label}] " if source_label else ""
                            lines.append(
                                f"[{hours:02d}:{minutes:02d}:{seconds:02d}] "
                                f"{prefix}{str(caption['text']).strip()}"
                            )
                        body = ("\ufeff" + "\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
                        safe_id = "".join(
                            character
                            if character.isalnum() or character in "-_"
                            else "_"
                            for character in session_id
                        )[:100] or "captions"
                        self._send_download(
                            body,
                            f"{safe_id}_captions.txt",
                            "text/plain; charset=utf-8",
                        )
                        return
                    if path == "/api/archive/info":
                        self._send_json({"archive": archive.archive_info()})
                        return
                    if path == "/api/archive/annotations":
                        session_id = self._required_query(query, "session_id")
                        limit = self._query_int(query, "limit", 500, 1, 5000)
                        annotations = archive.list_annotations(
                            session_id,
                            limit=limit,
                        )
                        self._send_json(
                            {"annotations": annotations, "count": len(annotations)}
                        )
                        return
                    if path == "/api/archive/sessions":
                        limit = self._query_int(query, "limit", 50, 1, 500)
                        self._send_json(
                            {
                                "sessions": archive.list_sessions(limit=limit),
                                "bucket_minutes": archive.bucket_minutes,
                            }
                        )
                        return
                    if path == "/api/archive/recaps":
                        session_id = self._query_value(query, "session_id", "")
                        if not session_id:
                            latest = archive.latest_session()
                            session_id = str(latest["id"]) if latest else ""
                        limit = self._query_int(query, "limit", 10, 1, 200)
                        self._send_json(
                            {
                                "session_id": session_id,
                                "bucket_minutes": archive.bucket_minutes,
                                "recaps": archive.list_recaps(session_id, limit=limit) if session_id else [],
                            }
                        )
                        return
                    if path == "/api/archive/latest":
                        session = archive.latest_session()
                        self._send_json(
                            {
                                "session": session,
                                "timeline": archive.timeline(session["id"])
                                if session
                                else [],
                                "stats": archive.stats(session["id"])
                                if session
                                else {},
                                "bucket_minutes": archive.bucket_minutes,
                            }
                        )
                        return
                    if path == "/api/archive/captions":
                        session_id = self._required_query(query, "session_id")
                        bucket_value = self._query_value(query, "bucket", "")
                        bucket = int(bucket_value) if bucket_value != "" else None
                        if bucket is not None and bucket < 0:
                            raise ValueError("bucket must be zero or greater")
                        after_value = self._query_value(query, "after", "")
                        after = float(after_value) if after_value != "" else None
                        limit = self._query_int(query, "limit", 2000, 1, 20000)
                        captions = archive.get_captions(
                            session_id,
                            bucket_index=bucket,
                            after_offset_sec=after,
                            limit=limit,
                        )
                        self._send_json(
                            {"captions": captions, "count": len(captions)}
                        )
                        return
                    if path == "/api/archive/search":
                        session_id = self._required_query(query, "session_id")
                        search_query = self._required_query(query, "q")
                        limit = self._query_int(query, "limit", 100, 1, 500)
                        results = archive.search(
                            session_id,
                            search_query,
                            limit=limit,
                        )
                        self._send_json(
                            {
                                "query": search_query,
                                "results": results,
                                "count": len(results),
                            }
                        )
                        return
                    if path == "/api/archive/summary":
                        session_id = self._required_query(query, "session_id")
                        window_mode = self._query_value(query, "window", "bucket")
                        if window_mode == "latest":
                            session = archive.get_session(session_id)
                            if session is None:
                                raise ValueError(f"unknown session: {session_id}")
                            window_end = float(session["last_offset_sec"])
                            window_start = max(
                                0.0,
                                window_end - archive.bucket_seconds,
                            )
                            bucket = int(window_end // archive.bucket_seconds)
                            captions = archive.get_captions(
                                session_id,
                                start_offset_sec=window_start,
                                end_offset_sec=window_end,
                                limit=20000,
                            )
                        elif window_mode == "bucket":
                            bucket = self._query_int(
                                query, "bucket", 0, 0, 100000
                            )
                            window_start = bucket * archive.bucket_seconds
                            window_end = (bucket + 1) * archive.bucket_seconds
                            captions = archive.get_captions(
                                session_id,
                                bucket_index=bucket,
                                limit=20000,
                            )
                        else:
                            raise ValueError("window must be bucket or latest")
                        summary = summarize_captions(
                            captions,
                            bucket_index=bucket,
                            bucket_minutes=archive.bucket_minutes,
                            max_bullets=int(
                                archive_config.get("archive", {}).get(
                                    "summary_max_bullets", 5
                                )
                            ),
                        )
                        summary["window_start_sec"] = window_start
                        summary["window_end_sec"] = window_end
                        summary["window_mode"] = window_mode
                        self._send_json(summary)
                        return
                    if path in {"/api/archive/timeline", "/api/archive/report"}:
                        session_id = self._required_query(query, "session_id")
                        session = archive.get_session(session_id)
                        if session is None:
                            raise ValueError(f"unknown session: {session_id}")
                        analysis_signature = json.dumps(
                            archive_config.get("archive", {}),
                            sort_keys=True,
                            ensure_ascii=True,
                        )
                        signature = (
                            session["caption_count"],
                            session["ended_at"],
                            session["title"],
                            session["vod_url"],
                            session["vod_start_offset_sec"],
                            analysis_signature,
                        )
                        cache_key = (session_id, path)
                        with analysis_cache_lock:
                            cached = analysis_cache.get(cache_key)
                        if cached is not None and cached[0] == signature:
                            report = cached[1]
                        else:
                            report = build_session_report(
                                archive,
                                session_id,
                                archive_config,
                                include_summaries=(
                                    path == "/api/archive/report"
                                ),
                            )
                            with analysis_cache_lock:
                                analysis_cache[cache_key] = (signature, report)
                                while len(analysis_cache) > 64:
                                    oldest_key = next(iter(analysis_cache))
                                    analysis_cache.pop(oldest_key, None)
                        if path == "/api/archive/report":
                            self._send_json(report)
                        else:
                            self._send_json(
                                {
                                    "session": report["session"],
                                    "stats": report["stats"],
                                    "keywords": report["keywords"],
                                    "timeline": report["timeline"],
                                    "topics": report["topics"],
                                    "highlights": report["highlights"],
                                }
                            )
                        return
                    if path == "/api/archive/slang-history":
                        limit = self._query_int(query, "limit", 100, 1, 1000)
                        self._send_json(
                            {"history": archive.list_slang_history(limit=limit)}
                        )
                        return
                    self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                except ValueError as exc:
                    self._send_json(
                        {"error": str(exc)},
                        HTTPStatus.BAD_REQUEST,
                    )
                except Exception as exc:
                    self._send_json(
                        {"error": f"archive unavailable: {exc}"},
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )

            def _handle_archive_mutation(self, path: str) -> None:
                try:
                    payload = self._read_json()
                    if not isinstance(payload, dict):
                        raise ValueError("json object required")
                    archive = self._archive()
                    if path == "/api/archive/annotation":
                        session_id = str(payload.get("session_id", "")).strip()
                        if not session_id:
                            raise ValueError("session_id required")
                        annotation = archive.add_annotation(
                            session_id,
                            kind=str(payload.get("kind", "")),
                            offset_sec=float(payload.get("offset_sec", 0.0)),
                            note=str(payload.get("note", "")),
                            caption_text=str(payload.get("caption_text", "")),
                            metadata=(
                                payload.get("metadata")
                                if isinstance(payload.get("metadata"), dict)
                                else {"source": "validation_ui"}
                            ),
                        )
                        self._invalidate_archive_cache(
                            analysis_cache,
                            analysis_cache_lock,
                            session_id,
                        )
                        self._send_json({"annotation": annotation})
                        return
                    if path == "/api/archive/annotation-delete":
                        session_id = str(payload.get("session_id", "")).strip()
                        if not session_id:
                            raise ValueError("session_id required")
                        annotation_id = int(payload.get("annotation_id", 0))
                        if annotation_id <= 0:
                            raise ValueError("annotation_id must be positive")
                        deleted = archive.delete_annotation(
                            annotation_id,
                            session_id,
                        )
                        if not deleted:
                            self._send_json(
                                {"error": "annotation not found"},
                                HTTPStatus.NOT_FOUND,
                            )
                            return
                        self._invalidate_archive_cache(
                            analysis_cache,
                            analysis_cache_lock,
                            session_id,
                        )
                        self._send_json({"deleted": True})
                        return
                    if path == "/api/archive/export":
                        backup_path = create_archive_backup(archive, archive_backup_dir)
                        self._send_json(
                            {
                                "backup_path": str(backup_path),
                                "archive": archive.archive_info(),
                            }
                        )
                        return
                    if path == "/api/archive/delete-session":
                        session_id = str(payload.get("session_id", "")).strip()
                        confirmation = str(
                            payload.get("confirm_session_id", "")
                        ).strip()
                        if not session_id or confirmation != session_id:
                            raise ValueError(
                                "confirm_session_id must exactly match session_id"
                            )
                        session = archive.get_session(session_id)
                        if session is None:
                            self._send_json(
                                {"error": "session not found"},
                                HTTPStatus.NOT_FOUND,
                            )
                            return
                        if session.get("ended_at") is None:
                            self._send_json(
                                {"error": "open session cannot be deleted"},
                                HTTPStatus.CONFLICT,
                            )
                            return
                        backup_path = create_archive_backup(archive, archive_backup_dir)
                        deleted = archive.delete_session(session_id)
                        self._invalidate_archive_cache(
                            analysis_cache,
                            analysis_cache_lock,
                            session_id,
                        )
                        self._send_json(
                            {
                                "deleted": deleted,
                                "session_id": session_id,
                                "backup_path": str(backup_path),
                                "archive": archive.archive_info(),
                            }
                        )
                        return
                    if path == "/api/archive/prune":
                        retention_days = int(payload.get("retention_days", 0))
                        if not 1 <= retention_days <= 3650:
                            raise ValueError(
                                "retention_days must be between 1 and 3650"
                            )
                        backup_path = create_archive_backup(archive, archive_backup_dir)
                        result = prune_archive(archive, retention_days)
                        self._invalidate_archive_cache(
                            analysis_cache,
                            analysis_cache_lock,
                        )
                        result["backup_path"] = str(backup_path)
                        result["archive"] = archive.archive_info()
                        self._send_json(result)
                        return
                    self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                except (TypeError, ValueError) as exc:
                    self._send_json(
                        {"error": str(exc)},
                        HTTPStatus.BAD_REQUEST,
                    )
                except Exception as exc:
                    self._send_json(
                        {"error": f"archive unavailable: {exc}"},
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )

            @staticmethod
            def _invalidate_archive_cache(
                cache: dict[tuple[str, str], tuple[tuple[Any, ...], dict[str, Any]]],
                cache_lock: threading.Lock,
                session_id: str | None = None,
            ) -> None:
                with cache_lock:
                    for cache_key in list(cache):
                        if session_id is None or cache_key[0] == session_id:
                            cache.pop(cache_key, None)
            def _handle_archive_session_meta(self) -> None:
                payload = self._read_json()
                if not isinstance(payload, dict):
                    self._send_json(
                        {"error": "json object required"},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                session_id = str(payload.get("session_id", "")).strip()
                if not session_id:
                    self._send_json(
                        {"error": "session_id required"},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                try:
                    archive = self._archive()
                    session = archive.update_session_meta(
                        session_id,
                        title=payload.get("title"),
                        vod_url=payload.get("vod_url"),
                        vod_start_offset_sec=payload.get(
                            "vod_start_offset_sec"
                        ),
                    )
                    if session is None:
                        self._send_json(
                            {"error": "session not found"},
                            HTTPStatus.NOT_FOUND,
                        )
                        return
                    with analysis_cache_lock:
                        for cache_key in list(analysis_cache):
                            if cache_key[0] == session_id:
                                analysis_cache.pop(cache_key, None)
                    self._send_json({"session": session})
                except (TypeError, ValueError) as exc:
                    self._send_json(
                        {"error": str(exc)},
                        HTTPStatus.BAD_REQUEST,
                    )
                except Exception as exc:
                    self._send_json(
                        {"error": f"archive unavailable: {exc}"},
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )

            @staticmethod
            def _query_value(
                query: dict[str, list[str]],
                key: str,
                default: str,
            ) -> str:
                values = query.get(key)
                return values[0].strip() if values else default

            def _required_query(
                self,
                query: dict[str, list[str]],
                key: str,
            ) -> str:
                value = self._query_value(query, key, "")
                if not value:
                    raise ValueError(f"{key} required")
                return value

            def _query_int(
                self,
                query: dict[str, list[str]],
                key: str,
                default: int,
                minimum: int,
                maximum: int,
            ) -> int:
                raw = self._query_value(query, key, str(default))
                value = int(raw)
                if not minimum <= value <= maximum:
                    raise ValueError(
                        f"{key} must be between {minimum} and {maximum}"
                    )
                return value

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _same_origin(self) -> bool:
                """Browsers always send Origin on cross-site POSTs; refuse those so a random web
                page cannot restart captions or start downloads on the streamer's PC. Requests
                without Origin (local tools, curl) and same-origin pages are allowed."""
                origin = self.headers.get("Origin")
                if not origin or origin == "null":
                    return not origin
                host = self.headers.get("Host", "")
                allowed = {f"http://{host}"} if host else set()
                for name in ("127.0.0.1", "localhost", "[::1]"):
                    allowed.add(f"http://{name}:{self.server.server_address[1]}")
                return origin.rstrip("/") in allowed

            def _read_json(self) -> Any:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))

            def _send_file(self, path: Path, content_type: str) -> None:
                if not path.exists():
                    self._send_json({"error": "file not found"}, HTTPStatus.NOT_FOUND)
                    return
                body = path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def _send_download(
                self,
                body: bytes,
                filename: str,
                content_type: str,
            ) -> None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{filename}"',
                )
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def _send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
                body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def _read_slang_files(self) -> dict[str, Any]:
                config = load_config()
                slang_path = PROJECT_ROOT / config["slang"]["dict_path"]
                whitelist_path = PROJECT_ROOT / config["defense"]["whitelist_path"]
                slang = {}
                if slang_path.exists():
                    slang = json.loads(slang_path.read_text(encoding="utf-8") or "{}")
                whitelist = []
                if whitelist_path.exists():
                    whitelist = [
                        line.strip()
                        for line in whitelist_path.read_text(encoding="utf-8").splitlines()
                        if line.strip() and not line.strip().startswith("#")
                    ]
                return {"slang": slang if isinstance(slang, dict) else {}, "whitelist": whitelist}

            def _write_slang_files(self, payload: dict[str, Any]) -> None:
                previous_slang = dict(self._read_slang_files().get("slang", {}))
                config = load_config()
                slang_path = PROJECT_ROOT / config["slang"]["dict_path"]
                whitelist_path = PROJECT_ROOT / config["defense"]["whitelist_path"]
                slang = payload.get("slang", {})
                whitelist = payload.get("whitelist", [])
                if not isinstance(slang, dict):
                    raise ValueError("slang must be an object")
                if not isinstance(whitelist, list):
                    raise ValueError("whitelist must be a list")
                slang_path.parent.mkdir(parents=True, exist_ok=True)
                whitelist_path.parent.mkdir(parents=True, exist_ok=True)
                slang_path.write_text(json.dumps(slang, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                lines = ["# One term per line.", ""]
                lines.extend(str(term).strip() for term in whitelist if str(term).strip())
                whitelist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                try:
                    archive = self._archive()
                    for source in sorted(set(previous_slang) | set(slang)):
                        old_target = str(previous_slang.get(source, ""))
                        new_target = str(slang.get(source, ""))
                        if source not in previous_slang:
                            action = "added"
                        elif source not in slang:
                            action = "deleted"
                        elif old_target != new_target:
                            action = "updated"
                        else:
                            continue
                        archive.record_slang_change(
                            action=action,
                            source=str(source),
                            target=new_target or old_target,
                            metadata={"previous_target": old_target},
                        )
                except Exception:
                    pass

        return Handler
