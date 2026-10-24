from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import threading
from typing import Any

from core.config import user_path
from core.events import CaptionEvent


class JsonlLogWriter:
    def __init__(self, log_dir: str = "logs") -> None:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")[:-3]
        self.session_dir = user_path(log_dir) / stamp
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.session_dir / "session.jsonl"
        self.caption_path = self.session_dir / "captions.txt"
        self.latest_caption_path = self.session_dir.parent / "latest_captions.txt"
        self.caption_path.write_text("", encoding="utf-8")
        self.latest_caption_path.write_text("", encoding="utf-8")
        self._caption_keys: set[tuple[int, str, str]] = set()
        self._lock = threading.Lock()

    @property
    def session_id(self) -> str:
        return self.session_dir.name

    def append_event(self, event: CaptionEvent, extra: dict[str, Any] | None = None) -> None:
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            **event.to_dict(),
        }
        if extra:
            record["extra"] = extra
        with self._lock:
            with self.path.open(
                "a",
                encoding="utf-8",
                newline="\n",
            ) as f:
                f.write(
                    json.dumps(record, ensure_ascii=False) + "\n"
                )
            self._append_caption_text(event)

    def _append_caption_text(self, event: CaptionEvent) -> None:
        if event.event_type != "caption_confirmed":
            return
        text = event.text.strip()
        if not text:
            return
        offset_ms = max(0, round(float(event.audio_offset_sec) * 1000))
        source = str(event.metadata.get("audio_source", ""))
        key = (offset_ms, source, text)
        if key in self._caption_keys:
            return
        self._caption_keys.add(key)
        total_seconds = offset_ms // 1000
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        source_label = str(event.metadata.get("source_label", "")).strip()
        prefix = f"[{source_label}] " if source_label else ""
        line = f"[{hours:02d}:{minutes:02d}:{seconds:02d}] {prefix}{text}\n"
        for output_path in (self.caption_path, self.latest_caption_path):
            with output_path.open("a", encoding="utf-8", newline="\n") as output:
                output.write(line)

