from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from typing import Any

from core.events import CaptionEvent


class CaptionWebSocketServer:
    def __init__(self, config: dict[str, Any]) -> None:
        self.host = config["server"]["host"]
        self.port = int(config["server"]["port"])
        self.cache_sec = float(config["streaming"]["ws_cache_sec"])
        if bool(config.get("caption", {}).get("just_chatting_mode", False)):
            self.cache_sec = max(
                self.cache_sec,
                float(config["caption"].get("history_retention_sec", 1800)),
            )
        self._clients: set[Any] = set()
        history_max_items = max(
            12, int(config.get("caption", {}).get("history_max_items", 1000))
        )
        self._cache: deque[tuple[float, dict[str, Any]]] = deque(
            maxlen=history_max_items
        )
        self._latest_analysis: dict[str, dict[str, Any]] = {}
        self._cache_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start_background(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_loop, name="caption-ws", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def emit_event(self, event: CaptionEvent) -> Any:
        message = self._event_to_message(event)
        if event.event_type == "caption_confirmed":
            self._cache_confirmed(message)
        elif event.event_type in {
            "summary_updated",
            "topic_changed",
            "recap_updated",
            "broadcast_ended",
        }:
            self._cache_analysis(event.event_type, message)
        return self._submit(self._broadcast(message))

    def emit_status(self, text: str) -> Any:
        return self._submit(
            self._broadcast({"type": "status", "text": text})
        )

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        import websockets

        async with websockets.serve(self._handle_client, self.host, self.port):
            self._ready.set()
            await asyncio.Future()

    async def _handle_client(self, websocket: Any) -> None:
        self._clients.add(websocket)
        try:
            await websocket.send(json.dumps({"type": "sync", "items": self._cached_items()}, ensure_ascii=False))
            async for _ in websocket:
                pass
        finally:
            self._clients.discard(websocket)

    async def _broadcast(self, message: dict[str, Any]) -> None:
        if not self._clients:
            return
        payload = json.dumps(message, ensure_ascii=False)
        dead = []
        for client in list(self._clients):
            try:
                await client.send(payload)
            except Exception:
                dead.append(client)
        for client in dead:
            self._clients.discard(client)

    def _submit(self, coro: Any) -> Any:
        if self._loop is None:
            coro.close()
            return None
        return asyncio.run_coroutine_threadsafe(
            coro,
            self._loop,
        )

    def _cache_confirmed(self, message: dict[str, Any]) -> None:
        now = time.monotonic()
        with self._cache_lock:
            self._cache.append((now, message))
            while (
                self._cache
                and now - self._cache[0][0] > self.cache_sec
            ):
                self._cache.popleft()

    def _cache_analysis(
        self,
        event_type: str,
        message: dict[str, Any],
    ) -> None:
        with self._cache_lock:
            self._latest_analysis[event_type] = message

    def _cached_items(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._cache_lock:
            captions = [
                item
                for ts, item in self._cache
                if now - ts <= self.cache_sec
            ]
            analysis = [
                self._latest_analysis[key]
                for key in (
                    "topic_changed",
                    "summary_updated",
                    "broadcast_ended",
                )
                if key in self._latest_analysis
            ]
        return captions + analysis

    def _event_to_message(self, event: CaptionEvent) -> dict[str, Any]:
        if event.event_type == "caption_confirmed":
            return {
                "type": "caption_confirmed",
                "text": event.text,
                "confidence": event.confidence,
                "audio_offset_sec": event.audio_offset_sec,
                "metadata": event.metadata,
            }
        if event.event_type == "caption_pending":
            return {
                "type": "caption_pending",
                "text": event.text,
                "confidence": event.confidence,
                "audio_offset_sec": event.audio_offset_sec,
                "metadata": event.metadata,
            }
        if event.event_type == "caption_hidden":
            return {
                "type": "caption_hidden",
                "audio_offset_sec": event.audio_offset_sec,
                "metadata": event.metadata,
            }
        return {
            "type": event.event_type,
            "text": event.text,
            "confidence": event.confidence,
            "audio_offset_sec": event.audio_offset_sec,
            "engine": event.engine,
            "metadata": event.metadata,
        }
