from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from core.config import PROJECT_ROOT


SCHEMA_VERSION = 2
ANNOTATION_KINDS = {"hallucination", "missed", "wrong_word", "good"}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def _decode_json(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def resolve_archive_path(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


class TranscriptArchive:
    """SQLite index for confirmed captions and broadcast session metadata."""

    def __init__(self, db_path: str | Path, bucket_minutes: int = 30) -> None:
        self.path = resolve_archive_path(db_path)
        self.bucket_minutes = max(1, int(bucket_minutes))
        self.bucket_seconds = self.bucket_minutes * 60
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "TranscriptArchive":
        archive = config.get("archive", {})
        return cls(
            archive.get("db_path", "data/transcripts.sqlite3"),
            int(archive.get("bucket_minutes", 30)),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS archive_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    source TEXT NOT NULL DEFAULT '',
                    engine TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    vod_url TEXT NOT NULL DEFAULT '',
                    vod_start_offset_sec REAL NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS captions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    offset_sec REAL NOT NULL,
                    offset_ms INTEGER NOT NULL,
                    bucket_index INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    engine TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
                    UNIQUE(session_id, offset_ms, text)
                );

                CREATE TABLE IF NOT EXISTS annotations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    offset_sec REAL NOT NULL DEFAULT 0,
                    kind TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    caption_text TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS slang_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    source TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    session_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_annotations_session_offset
                    ON annotations(session_id, offset_sec, id);
                CREATE INDEX IF NOT EXISTS idx_annotations_created
                    ON annotations(created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_slang_history_created
                    ON slang_history(created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_captions_session_offset
                    ON captions(session_id, offset_ms, id);
                CREATE INDEX IF NOT EXISTS idx_captions_session_bucket
                    ON captions(session_id, bucket_index, offset_ms, id);
                CREATE INDEX IF NOT EXISTS idx_sessions_started
                    ON sessions(started_at DESC);
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO archive_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def create_session(
        self,
        session_id: str,
        *,
        started_at: str | None = None,
        source: str = "",
        engine: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started_at = started_at or _now_iso()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions(id, started_at, source, engine, metadata_json)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source,
                    engine = excluded.engine,
                    metadata_json = excluded.metadata_json
                """,
                (session_id, started_at, source, engine, _json(metadata)),
            )
        return self.get_session(session_id) or {}

    def close_session(
        self,
        session_id: str,
        *,
        ended_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        ended_at = ended_at or _now_iso()
        current = self.get_session(session_id)
        merged_metadata = dict((current or {}).get("metadata", {}))
        if metadata:
            merged_metadata.update(metadata)
        with self._connection() as connection:
            connection.execute(
                "UPDATE sessions SET ended_at = ?, metadata_json = ? WHERE id = ?",
                (ended_at, _json(merged_metadata), session_id),
            )

    def add_caption(
        self,
        session_id: str,
        *,
        text: str,
        offset_sec: float,
        confidence: float = 0.0,
        engine: str = "",
        created_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int | None:
        clean_text = str(text).strip()
        if not clean_text:
            return None
        offset = max(0.0, float(offset_sec))
        offset_ms = int(round(offset * 1000.0))
        bucket_index = int(offset // self.bucket_seconds)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO captions(
                    session_id, created_at, offset_sec, offset_ms,
                    bucket_index, text, confidence, engine, metadata_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    created_at or _now_iso(),
                    offset,
                    offset_ms,
                    bucket_index,
                    clean_text,
                    float(confidence),
                    engine,
                    _json(metadata),
                ),
            )
            return int(cursor.lastrowid) if cursor.rowcount else None

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT s.*,
                       COUNT(c.id) AS caption_count,
                       COALESCE(MAX(c.offset_sec), 0) AS last_offset_sec,
                       MAX(c.created_at) AS last_caption_at
                FROM sessions s
                LEFT JOIN captions c ON c.session_id = s.id
                WHERE s.id = ?
                GROUP BY s.id
                """,
                (session_id,),
            ).fetchone()
        return self._session_dict(row) if row else None

    def latest_session(self) -> dict[str, Any] | None:
        sessions = self.list_sessions(limit=1)
        return sessions[0] if sessions else None

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT s.*,
                       COUNT(c.id) AS caption_count,
                       COALESCE(MAX(c.offset_sec), 0) AS last_offset_sec,
                       MAX(c.created_at) AS last_caption_at
                FROM sessions s
                LEFT JOIN captions c ON c.session_id = s.id
                GROUP BY s.id
                ORDER BY s.started_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [self._session_dict(row) for row in rows]

    def update_session_meta(
        self,
        session_id: str,
        *,
        title: str | None = None,
        vod_url: str | None = None,
        vod_start_offset_sec: float | None = None,
    ) -> dict[str, Any] | None:
        updates: list[str] = []
        values: list[Any] = []
        if title is not None:
            updates.append("title = ?")
            values.append(str(title).strip()[:200])
        if vod_url is not None:
            clean_url = str(vod_url).strip()
            if clean_url and not clean_url.startswith(("http://", "https://")):
                raise ValueError("vod_url must start with http:// or https://")
            updates.append("vod_url = ?")
            values.append(clean_url[:2000])
        if vod_start_offset_sec is not None:
            adjustment = float(vod_start_offset_sec)
            if not -86400.0 <= adjustment <= 86400.0:
                raise ValueError(
                    "vod_start_offset_sec must be between -86400 and 86400"
                )
            updates.append("vod_start_offset_sec = ?")
            values.append(adjustment)
        if updates:
            values.append(session_id)
            with self._connection() as connection:
                connection.execute(
                    f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?",
                    values,
                )
        return self.get_session(session_id)

    def get_captions(
        self,
        session_id: str,
        *,
        bucket_index: int | None = None,
        after_offset_sec: float | None = None,
        start_offset_sec: float | None = None,
        end_offset_sec: float | None = None,
        limit: int | None = 2000,
    ) -> list[dict[str, Any]]:
        clauses = ["session_id = ?"]
        values: list[Any] = [session_id]
        if bucket_index is not None:
            clauses.append("bucket_index = ?")
            values.append(max(0, int(bucket_index)))
        if after_offset_sec is not None:
            clauses.append("offset_sec > ?")
            values.append(max(0.0, float(after_offset_sec)))
        if start_offset_sec is not None:
            clauses.append("offset_sec >= ?")
            values.append(max(0.0, float(start_offset_sec)))
        if end_offset_sec is not None:
            clauses.append("offset_sec <= ?")
            values.append(max(0.0, float(end_offset_sec)))
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            values.append(max(1, min(int(limit), 20000)))
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM captions
                WHERE {' AND '.join(clauses)}
                ORDER BY offset_ms ASC, id ASC
                {limit_clause}
                """,
                values,
            ).fetchall()
        return [self._caption_dict(row) for row in rows]

    def search(
        self,
        session_id: str,
        query: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clean_query = str(query).strip()[:200]
        if not clean_query:
            return []
        escaped = clean_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM captions
                WHERE session_id = ? AND text LIKE ? ESCAPE '\\'
                ORDER BY offset_ms ASC, id ASC
                LIMIT ?
                """,
                (session_id, f"%{escaped}%", max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._caption_dict(row) for row in rows]

    def timeline(self, session_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT bucket_index,
                       COUNT(*) AS caption_count,
                       MIN(offset_sec) AS first_offset_sec,
                       MAX(offset_sec) AS last_offset_sec,
                       SUM(LENGTH(text)) AS character_count
                FROM captions
                WHERE session_id = ?
                GROUP BY bucket_index
                ORDER BY bucket_index ASC
                """,
                (session_id,),
            ).fetchall()
        return [
            {
                "bucket_index": int(row["bucket_index"]),
                "window_start_sec": int(row["bucket_index"]) * self.bucket_seconds,
                "window_end_sec": (int(row["bucket_index"]) + 1) * self.bucket_seconds,
                "caption_count": int(row["caption_count"]),
                "first_offset_sec": float(row["first_offset_sec"] or 0),
                "last_offset_sec": float(row["last_offset_sec"] or 0),
                "character_count": int(row["character_count"] or 0),
            }
            for row in rows
        ]

    def stats(self, session_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS caption_count,
                       COALESCE(MIN(offset_sec), 0) AS first_offset_sec,
                       COALESCE(MAX(offset_sec), 0) AS last_offset_sec,
                       COALESCE(SUM(LENGTH(text)), 0) AS character_count,
                       COALESCE(AVG(LENGTH(text)), 0) AS average_caption_chars,
                       COALESCE(AVG(confidence), 0) AS average_confidence
                FROM captions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        duration = float(row["last_offset_sec"] or 0)
        minutes = max(duration / 60.0, 1.0 / 60.0)
        return {
            "caption_count": int(row["caption_count"]),
            "first_offset_sec": float(row["first_offset_sec"]),
            "last_offset_sec": duration,
            "duration_sec": duration,
            "character_count": int(row["character_count"]),
            "average_caption_chars": round(float(row["average_caption_chars"]), 2),
            "average_confidence": round(float(row["average_confidence"]), 4),
            "captions_per_minute": round(int(row["caption_count"]) / minutes, 2),
        }

    def add_annotation(
        self,
        session_id: str,
        *,
        kind: str,
        offset_sec: float = 0.0,
        note: str = "",
        caption_text: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_kind = str(kind).strip().lower()
        if clean_kind not in ANNOTATION_KINDS:
            raise ValueError(
                "annotation kind must be hallucination, missed, wrong_word, or good"
            )
        if self.get_session(session_id) is None:
            raise ValueError(f"unknown session: {session_id}")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO annotations(
                    session_id, created_at, offset_sec, kind,
                    note, caption_text, metadata_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    _now_iso(),
                    max(0.0, float(offset_sec)),
                    clean_kind,
                    str(note).strip()[:1000],
                    str(caption_text).strip()[:2000],
                    _json(metadata),
                ),
            )
            annotation_id = int(cursor.lastrowid)
        return self.get_annotation(annotation_id) or {}

    def get_annotation(self, annotation_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM annotations WHERE id = ?",
                (int(annotation_id),),
            ).fetchone()
        return self._annotation_dict(row) if row else None

    def list_annotations(
        self,
        session_id: str,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM annotations
                WHERE session_id = ?
                ORDER BY offset_sec ASC, id ASC
                LIMIT ?
                """,
                (session_id, max(1, min(int(limit), 5000))),
            ).fetchall()
        return [self._annotation_dict(row) for row in rows]

    def delete_annotation(self, annotation_id: int, session_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM annotations WHERE id = ? AND session_id = ?",
                (int(annotation_id), session_id),
            )
            return bool(cursor.rowcount)

    def list_open_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT s.*,
                       COUNT(c.id) AS caption_count,
                       COALESCE(MAX(c.offset_sec), 0) AS last_offset_sec,
                       MAX(c.created_at) AS last_caption_at
                FROM sessions s
                LEFT JOIN captions c ON c.session_id = s.id
                WHERE s.ended_at IS NULL
                GROUP BY s.id
                ORDER BY s.started_at ASC
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [self._session_dict(row) for row in rows]

    def delete_session(self, session_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE id = ?",
                (session_id,),
            )
            return bool(cursor.rowcount)

    def prune_sessions_before(self, cutoff_iso: str) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id FROM sessions
                WHERE ended_at IS NOT NULL AND ended_at < ?
                ORDER BY ended_at ASC
                """,
                (str(cutoff_iso),),
            ).fetchall()
            session_ids = [str(row["id"]) for row in rows]
            if session_ids:
                connection.executemany(
                    "DELETE FROM sessions WHERE id = ?",
                    [(session_id,) for session_id in session_ids],
                )
        return session_ids

    def archive_info(self) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS session_count,
                       MIN(started_at) AS oldest_session_at,
                       MAX(started_at) AS newest_session_at
                FROM sessions
                """
            ).fetchone()
            caption_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM captions"
                ).fetchone()[0]
            )
            annotation_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM annotations"
                ).fetchone()[0]
            )
        sidecars = [
            self.path,
            Path(str(self.path) + "-wal"),
            Path(str(self.path) + "-shm"),
        ]
        size_bytes = sum(
            candidate.stat().st_size
            for candidate in sidecars
            if candidate.exists()
        )
        return {
            "path": str(self.path),
            "size_bytes": size_bytes,
            "session_count": int(row["session_count"] or 0),
            "caption_count": caption_count,
            "annotation_count": annotation_count,
            "oldest_session_at": row["oldest_session_at"],
            "newest_session_at": row["newest_session_at"],
        }

    def backup_to(self, destination: str | Path) -> Path:
        output = Path(destination).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise FileExistsError(f"archive backup already exists: {output}")
        source = self._connect()
        target = sqlite3.connect(output, timeout=10.0)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
            source.close()
        return output

    def record_slang_change(
        self,
        *,
        action: str,
        source: str,
        target: str = "",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        clean_action = str(action).strip().lower()
        if clean_action not in {"added", "updated", "deleted"}:
            raise ValueError("slang action must be added, updated, or deleted")
        clean_source = str(source).strip()
        if not clean_source:
            raise ValueError("slang source is required")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO slang_history(
                    created_at, action, source, target, session_id, metadata_json
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    _now_iso(),
                    clean_action,
                    clean_source,
                    str(target).strip(),
                    session_id,
                    _json(metadata),
                ),
            )
            return int(cursor.lastrowid)

    def list_slang_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM slang_history
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "created_at": row["created_at"],
                "action": row["action"],
                "source": row["source"],
                "target": row["target"],
                "session_id": row["session_id"],
                "metadata": _decode_json(row["metadata_json"]),
            }
            for row in rows
        ]

    @staticmethod
    def _session_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "source": row["source"],
            "engine": row["engine"],
            "title": row["title"],
            "vod_url": row["vod_url"],
            "vod_start_offset_sec": float(row["vod_start_offset_sec"] or 0),
            "metadata": _decode_json(row["metadata_json"]),
            "caption_count": int(row["caption_count"] or 0),
            "last_offset_sec": float(row["last_offset_sec"] or 0),
            "last_caption_at": row["last_caption_at"],
        }

    @staticmethod
    def _annotation_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "session_id": row["session_id"],
            "created_at": row["created_at"],
            "offset_sec": float(row["offset_sec"]),
            "kind": row["kind"],
            "note": row["note"],
            "caption_text": row["caption_text"],
            "metadata": _decode_json(row["metadata_json"]),
        }

    @staticmethod
    def _caption_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "session_id": row["session_id"],
            "created_at": row["created_at"],
            "offset_sec": float(row["offset_sec"]),
            "bucket_index": int(row["bucket_index"]),
            "text": row["text"],
            "confidence": float(row["confidence"]),
            "engine": row["engine"],
            "metadata": _decode_json(row["metadata_json"]),
        }

