"""Bucket recaps: every N minutes, turn the confirmed captions of that window into
a short Korean recap the streamer can show on screen ("지난 15분 동안 이런 얘기를 했다").

Zero-config by design. Provider resolution ("auto"):
  1. an OpenAI-compatible endpoint when an API key env var and model are configured,
  2. a local Ollama server if one answers on 127.0.0.1:11434,
  3. an installed `claude` or `codex` CLI,
  4. otherwise the existing extractive summary (always works, no network).
Every path falls back to the extractive summary on any error, so recaps never
block or break captioning.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Callable

from core.events import CaptionEvent
from features.summarizer import summarize_captions

RECAP_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "provider": "auto",
    "max_bullets": 4,
    "min_captions": 3,
    "timeout_sec": 60,
    "max_transcript_chars": 7000,
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "",
    "openai_base_url": "",
    "openai_model": "",
    "openai_api_key_env": "OPENAI_API_KEY",
    "cli_command": "",
}

PREFERRED_LOCAL_MODELS = ("exaone", "qwen", "gemma", "llama", "mistral", "phi")


def recap_config(config: dict[str, Any]) -> dict[str, Any]:
    merged = dict(RECAP_DEFAULTS)
    merged.update(dict(config.get("archive", {}).get("recap", {}) or {}))
    return merged


def format_offset(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def transcript_lines(captions: list[dict[str, Any]], max_chars: int = 7000) -> str:
    """One line per caption with timestamp and speaker label; trimmed from the start if too long."""
    lines: list[str] = []
    for caption in captions:
        text = str(caption.get("text", "")).strip()
        if not text:
            continue
        metadata = caption.get("metadata") or {}
        label = str(metadata.get("source_label") or metadata.get("source") or "").strip()
        prefix = f"[{format_offset(float(caption.get('offset_sec', 0.0)))}]"
        lines.append(f"{prefix} {label + ': ' if label else ''}{text}")
    joined = "\n".join(lines)
    if len(joined) > max_chars:
        joined = joined[-max_chars:]
        joined = joined[joined.find("\n") + 1:] if "\n" in joined else joined
    return joined


def recap_prompt(transcript: str, minutes: int, max_bullets: int) -> str:
    return (
        f"다음은 한국어 인터넷 방송에서 최근 {minutes}분 동안 실시간 자막으로 받아 적은 대화입니다. "
        "자막은 음성 인식 결과라 오타와 끊김이 있을 수 있습니다. 라벨이 있으면 '방송자'는 스트리머, '컴퓨터'는 상대방(디스코드 등)입니다.\n"
        f"이 구간에서 실제로 오간 이야기를 시청자가 한눈에 알 수 있게 정리하세요. 추측이나 자막에 없는 내용은 넣지 마세요.\n"
        "반드시 아래 JSON 형식으로만 답하세요. 다른 말은 쓰지 마세요.\n"
        '{"headline": "한 줄 제목(20자 이내)", "bullets": ["핵심 1", "핵심 2", "..."]}\n'
        f"bullets는 {max_bullets}개 이하, 각 항목은 40자 이내의 한국어 문장으로.\n\n"
        f"=== 자막 ===\n{transcript}\n=== 끝 ==="
    )


def parse_llm_json(text: str, max_bullets: int = 6) -> dict[str, Any] | None:
    """Accept the first JSON object in the model output; reject anything that is not {headline, bullets[]}."""
    if not text:
        return None
    candidates = [text.strip()]
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        headline = str(data.get("headline", "")).strip()
        bullets_raw = data.get("bullets", [])
        if not isinstance(bullets_raw, list):
            continue
        bullets = [str(item).strip() for item in bullets_raw if str(item).strip()]
        if not headline and not bullets:
            continue
        return {"headline": headline[:60] or (bullets[0][:60] if bullets else "주요 대화"), "bullets": bullets[: max(1, max_bullets)]}
    return None


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class RecapProvider:
    name = "extractive"
    model = ""

    def complete(self, prompt: str, timeout: float) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class OllamaProvider(RecapProvider):
    name = "ollama"

    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    @classmethod
    def detect(cls, base_url: str, preferred_model: str, timeout: float = 1.5) -> "OllamaProvider | None":
        try:
            with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception:
            return None
        names = [str(item.get("name", "")) for item in data.get("models", []) if isinstance(item, dict)]
        if not names:
            return None
        if preferred_model and any(name == preferred_model or name.startswith(preferred_model + ":") for name in names):
            chosen = next(name for name in names if name == preferred_model or name.startswith(preferred_model + ":"))
            return cls(base_url, chosen)
        for family in PREFERRED_LOCAL_MODELS:
            for name in names:
                if family in name.lower():
                    return cls(base_url, name)
        return cls(base_url, names[0])

    def complete(self, prompt: str, timeout: float) -> str:
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
        }).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}/api/chat", data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return str((data.get("message") or {}).get("content", ""))


class OpenAICompatibleProvider(RecapProvider):
    name = "openai"

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def complete(self, prompt: str, timeout: float) -> str:
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        choices = data.get("choices") or []
        return str(((choices[0] if choices else {}).get("message") or {}).get("content", ""))


class CliProvider(RecapProvider):
    """`claude -p` or `codex exec`: whichever the streamer already has installed and logged in."""
    name = "cli"

    def __init__(self, command: str) -> None:
        self.command = command
        self.model = command

    @classmethod
    def detect(cls, preferred: str = "") -> "CliProvider | None":
        order = [preferred] if preferred else []
        order += [name for name in ("claude", "codex") if name not in order]
        for name in order:
            if name and shutil.which(name):
                return cls(name)
        return None

    def complete(self, prompt: str, timeout: float) -> str:
        if self.command == "codex":
            args = ["codex", "exec", "--ephemeral", "--sandbox", "read-only", "--ask-for-approval", "never", prompt]
        else:
            args = [self.command, "-p", prompt, "--output-format", "text"]
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, shell=False, creationflags=_creation_flags())
        if result.returncode != 0:
            raise RuntimeError(f"{self.command} exited with {result.returncode}")
        return result.stdout


class ProviderResolver:
    """Cache the provider probe so collection runs do not re-scan every bucket."""

    def __init__(self, settings: dict[str, Any], ttl_sec: float = 300.0) -> None:
        self.settings = settings
        self.ttl_sec = ttl_sec
        self._cached: RecapProvider | None = None
        self._checked_at = 0.0
        self._lock = threading.Lock()

    def resolve(self) -> RecapProvider | None:
        with self._lock:
            now = time.monotonic()
            if self._checked_at and now - self._checked_at < self.ttl_sec:
                return self._cached
            self._cached = self._probe()
            self._checked_at = now
            return self._cached

    def _probe(self) -> RecapProvider | None:
        mode = str(self.settings.get("provider", "auto")).lower()
        if mode in {"none", "extractive", "off"}:
            return None
        key = os.environ.get(str(self.settings.get("openai_api_key_env") or "OPENAI_API_KEY"), "")
        if mode in {"auto", "openai"} and key and self.settings.get("openai_model") and self.settings.get("openai_base_url"):
            return OpenAICompatibleProvider(str(self.settings["openai_base_url"]), key, str(self.settings["openai_model"]))
        if mode in {"auto", "ollama"}:
            provider = OllamaProvider.detect(str(self.settings.get("ollama_url") or RECAP_DEFAULTS["ollama_url"]), str(self.settings.get("ollama_model") or ""))
            if provider or mode == "ollama":
                return provider
        if mode in {"auto", "cli"}:
            return CliProvider.detect(str(self.settings.get("cli_command") or ""))
        return None


def extractive_recap(captions: list[dict[str, Any]], *, bucket_index: int, bucket_minutes: int, max_bullets: int) -> dict[str, Any]:
    summary = summarize_captions(captions, bucket_index=bucket_index, bucket_minutes=bucket_minutes, max_bullets=max_bullets)
    return {
        "headline": str(summary.get("headline", "주요 대화")),
        "bullets": [str(item.get("text", "")) for item in summary.get("bullets", []) if str(item.get("text", "")).strip()],
        "keywords": summary.get("keywords", []),
        "method": "local_extractive_v1",
        "model": "",
    }


def generate_recap(
    captions: list[dict[str, Any]],
    *,
    bucket_index: int,
    bucket_minutes: int,
    settings: dict[str, Any],
    provider: RecapProvider | None,
) -> dict[str, Any]:
    """LLM recap when a provider is available, extractive otherwise; never raises."""
    max_bullets = max(1, min(int(settings.get("max_bullets", 4)), 6))
    fallback = extractive_recap(captions, bucket_index=bucket_index, bucket_minutes=bucket_minutes, max_bullets=max_bullets)
    if provider is None or not captions:
        return fallback
    try:
        transcript = transcript_lines(captions, int(settings.get("max_transcript_chars", 7000)))
        raw = provider.complete(recap_prompt(transcript, bucket_minutes, max_bullets), float(settings.get("timeout_sec", 60)))
        parsed = parse_llm_json(raw, max_bullets)
        if parsed is None:
            raise ValueError("provider returned no usable JSON")
        return {**parsed, "keywords": fallback.get("keywords", []), "method": f"llm_{provider.name}", "model": provider.model}
    except Exception as exc:  # noqa: BLE001 - recaps must never break captioning
        return {**fallback, "fallback_reason": f"{type(exc).__name__}: {exc}"[:200]}


class RecapWorker:
    """Generates one recap per closed bucket in the background and emits `recap_updated`."""

    def __init__(self, config: dict[str, Any], session_id: str, store: Any, callback: Callable[[CaptionEvent], Any]) -> None:
        self.config = config
        self.settings = recap_config(config)
        self.session_id = session_id
        self.store = store
        self.callback = callback
        self.resolver = ProviderResolver(self.settings)
        self.bucket_seconds = int(store.bucket_seconds)
        self.bucket_minutes = int(store.bucket_minutes)
        self._queue: deque[tuple[int, float, bool]] = deque()
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._last_queued = -1
        self.done: dict[int, str] = {}
        self.error_count = 0
        self.last_error = ""
        self.events_emitted = 0

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="recap-worker", daemon=True)
            self._thread.start()

    def submit(self, latest_offset_sec: float) -> None:
        """Called on every confirmed caption; queues every bucket that has closed since the last call."""
        closed = int(latest_offset_sec // self.bucket_seconds) - 1
        if closed <= self._last_queued:
            return
        with self._condition:
            for bucket in range(self._last_queued + 1, closed + 1):
                self._queue.append((bucket, (bucket + 1) * self.bucket_seconds, False))
            self._last_queued = closed
            self._condition.notify()

    def finalize(self, latest_offset_sec: float) -> None:
        """At session end, recap the partial current bucket too."""
        current = int(latest_offset_sec // self.bucket_seconds)
        with self._condition:
            if current > self._last_queued:
                for bucket in range(self._last_queued + 1, current):
                    self._queue.append((bucket, (bucket + 1) * self.bucket_seconds, False))
                self._queue.append((current, float(latest_offset_sec), True))
                self._last_queued = current
                self._condition.notify()

    def stop(self, timeout: float = 20.0) -> bool:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def snapshot(self) -> dict[str, Any]:
        provider = self.resolver._cached
        return {
            "buckets_done": len(self.done),
            "methods": sorted(set(self.done.values())),
            "provider": provider.name if provider else "extractive",
            "model": provider.model if provider else "",
            "error_count": self.error_count,
            "last_error": self.last_error,
            "events_emitted": self.events_emitted,
        }

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stopping:
                    self._condition.wait()
                if not self._queue and self._stopping:
                    return
                bucket, window_end, partial = self._queue.popleft()
            try:
                self._process(bucket, window_end, partial)
            except Exception as exc:  # noqa: BLE001
                self.error_count += 1
                self.last_error = str(exc)

    def _process(self, bucket: int, window_end: float, partial: bool) -> None:
        captions = self.store.get_captions(self.session_id, bucket_index=bucket, limit=None)
        if len(captions) < int(self.settings.get("min_captions", 3)):
            self.done[bucket] = "skipped"
            return
        provider = self.resolver.resolve()
        recap = generate_recap(captions, bucket_index=bucket, bucket_minutes=self.bucket_minutes, settings=self.settings, provider=provider)
        window_start = float(bucket * self.bucket_seconds)
        stored = self.store.upsert_recap(
            self.session_id, bucket,
            headline=recap["headline"], bullets=recap["bullets"], method=recap["method"], model=recap.get("model", ""),
            caption_count=len(captions), window_start_sec=window_start, window_end_sec=float(window_end), partial=partial,
            metadata={"keywords": recap.get("keywords", []), "fallback_reason": recap.get("fallback_reason", "")},
        )
        self.done[bucket] = recap["method"]
        self.callback(CaptionEvent(
            event_type="recap_updated",
            text=str(recap["headline"]),
            confidence=1.0,
            audio_offset_sec=float(window_end),
            engine="recap",
            metadata={"session_id": self.session_id, "recap": stored},
        ))
        self.events_emitted += 1
