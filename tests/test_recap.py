"""Bucket recaps: parsing, extractive fallback, worker bucket closing, archive storage, admin API."""
from __future__ import annotations

import copy
import json
import socket
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.admin_server import AdminServer, AdminState
from core.config import load_config, validate_config
from core.events import CaptionEvent
from core.transcript_archive import TranscriptArchive
from features.recap import (
    ProviderResolver, RecapProvider, RecapWorker, generate_recap, parse_llm_json, recap_config, transcript_lines,
)


class FakeProvider(RecapProvider):
    name = "fake"
    model = "fake-1"

    def __init__(self, reply: str, fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.prompts: list[str] = []

    def complete(self, prompt: str, timeout: float) -> str:
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("provider down")
        return self.reply


def caption(text: str, offset: float, label: str = "방송자") -> dict:
    return {"text": text, "offset_sec": offset, "confidence": 0.9, "metadata": {"source_label": label}}


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def get_json(url: str) -> dict:
    with urlopen(url, timeout=3.0) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    # --- parsing is strict about shape, lenient about surrounding text ---
    assert parse_llm_json('여기요: {"headline": "제목", "bullets": ["하나", "둘", ""]} 끝') == {"headline": "제목", "bullets": ["하나", "둘"]}
    assert parse_llm_json('{"headline": "", "bullets": []}') is None
    assert parse_llm_json("not json") is None
    assert parse_llm_json('{"bullets": ["a", "b", "c"]}', max_bullets=2) == {"headline": "a", "bullets": ["a", "b"]}
    assert parse_llm_json('["list"]') is None

    # --- transcript lines carry timestamps and speaker labels, trimmed from the front ---
    lines = transcript_lines([caption("안녕하세요", 61), caption("네 안녕하세요", 65, "컴퓨터")])
    assert lines == "[00:01:01] 방송자: 안녕하세요\n[00:01:05] 컴퓨터: 네 안녕하세요", lines
    long = transcript_lines([caption("x" * 50, i) for i in range(100)], max_chars=300)
    assert len(long) <= 300 and long.startswith("[")

    # --- generate_recap: LLM path, invalid JSON path, failing provider path ---
    settings = recap_config({"archive": {"recap": {"max_bullets": 3}}})
    caps = [caption("오늘 게임 패치 얘기를 했습니다 밸런스가 바뀌었어요", 10), caption("그리고 채팅에서 투표를 했죠", 20), caption("결과는 찬성이 많았습니다", 30)]
    good = FakeProvider('{"headline": "패치 토론", "bullets": ["밸런스 변경 언급", "채팅 투표 진행", "찬성 우세", "넷째는 잘림"]}')
    recap = generate_recap(caps, bucket_index=0, bucket_minutes=15, settings=settings, provider=good)
    assert recap["method"] == "llm_fake" and recap["model"] == "fake-1", recap
    assert recap["headline"] == "패치 토론" and recap["bullets"] == ["밸런스 변경 언급", "채팅 투표 진행", "찬성 우세"], recap
    assert "15분" in good.prompts[0] and "방송자: 오늘 게임 패치" in good.prompts[0]
    bad = generate_recap(caps, bucket_index=0, bucket_minutes=15, settings=settings, provider=FakeProvider("아무 말"))
    assert bad["method"] == "local_extractive_v1" and bad["bullets"] and "fallback_reason" in bad, bad
    down = generate_recap(caps, bucket_index=0, bucket_minutes=15, settings=settings, provider=FakeProvider("", fail=True))
    assert down["method"] == "local_extractive_v1" and "provider down" in down["fallback_reason"]
    none = generate_recap(caps, bucket_index=0, bucket_minutes=15, settings=settings, provider=None)
    assert none["method"] == "local_extractive_v1" and "fallback_reason" not in none

    # --- provider "none" never probes anything ---
    assert ProviderResolver({"provider": "none"}).resolve() is None

    # --- config validation accepts defaults and rejects nonsense ---
    config = copy.deepcopy(load_config())
    validate_config(config)
    assert config["archive"]["recap"]["provider"] == "auto"
    broken = copy.deepcopy(config)
    broken["archive"]["recap"]["provider"] = "magic"
    try:
        validate_config(broken)
        raise AssertionError("invalid provider accepted")
    except ValueError:
        pass

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "recap.sqlite3"
        archive = TranscriptArchive(db_path, bucket_minutes=5)
        archive.create_session("recap-session", source="both", engine="test")

        # --- archive storage round-trip and upsert ---
        stored = archive.upsert_recap("recap-session", 0, headline="첫 구간", bullets=["a", " b ", ""], method="llm_fake", model="m", caption_count=3, window_start_sec=0, window_end_sec=300)
        assert stored["bullets"] == ["a", "b"] and stored["partial"] is False
        archive.upsert_recap("recap-session", 0, headline="첫 구간 수정", bullets=["c"], method="local_extractive_v1", caption_count=4, window_start_sec=0, window_end_sec=300)
        assert archive.get_recap("recap-session", 0)["headline"] == "첫 구간 수정"
        assert len(archive.list_recaps("recap-session")) == 1
        assert archive.get_recap("recap-session", 9) is None

        # --- worker: closes buckets as offsets advance, skips thin buckets, finalizes the partial one ---
        for offset in (10, 20, 30):
            archive.add_caption("recap-session", text=f"첫 번째 구간 대화 {offset}초", offset_sec=offset, confidence=0.9, engine="test")
        archive.add_caption("recap-session", text="두 번째 구간은 한 줄뿐", offset_sec=400, confidence=0.9, engine="test")
        for offset in (610, 620, 630, 640):
            archive.add_caption("recap-session", text=f"세 번째 구간 대화 {offset}초", offset_sec=offset, confidence=0.9, engine="test")
        events: list[CaptionEvent] = []
        worker_config = copy.deepcopy(config)
        worker_config["archive"]["recap"]["provider"] = "none"
        worker_config["archive"]["recap"]["min_captions"] = 2
        worker = RecapWorker(worker_config, "recap-session", archive, events.append)
        worker.start()
        worker.submit(30)          # bucket 0 still open
        worker.submit(400)         # bucket 0 closed
        worker.submit(640)         # bucket 1 closed (thin → skipped)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(worker.done) < 2:
            time.sleep(0.05)
        assert worker.done.get(0) == "local_extractive_v1", worker.done
        assert worker.done.get(1) == "skipped", worker.done
        assert len(events) == 1 and events[0].event_type == "recap_updated"
        assert events[0].metadata["recap"]["bucket_index"] == 0 and events[0].metadata["recap"]["window_end_sec"] == 300
        worker.finalize(640)       # bucket 2 is partial at session end
        assert worker.stop(timeout=5)
        assert worker.done.get(2) == "local_extractive_v1", worker.done
        recaps = archive.list_recaps("recap-session")
        assert [r["bucket_index"] for r in recaps] == [2, 0]
        assert recaps[0]["partial"] is True and recaps[0]["window_end_sec"] == 640
        snapshot = worker.snapshot()
        assert snapshot["provider"] == "extractive" and snapshot["buckets_done"] == 3, snapshot

        # --- admin API and overlay page ---
        api_config = copy.deepcopy(config)
        api_config["archive"]["db_path"] = str(db_path)
        api_config["archive"]["bucket_minutes"] = 5
        api_config["server"]["admin_port"] = available_port()
        server = AdminServer(api_config, AdminState())
        server.start_background()
        base = f"http://127.0.0.1:{api_config['server']['admin_port']}"
        time.sleep(0.15)
        try:
            with urlopen(base + "/recap", timeout=3.0) as response:
                page = response.read().decode("utf-8")
            listed = get_json(base + "/api/archive/recaps?session_id=recap-session&limit=1")
            by_latest = get_json(base + "/api/archive/recaps")
        finally:
            server.stop()
        assert "구간 요약" in page and "/api/archive/recaps" in page
        assert listed["bucket_minutes"] == 5 and len(listed["recaps"]) == 1 and listed["recaps"][0]["bucket_index"] == 2
        assert by_latest["session_id"] == "recap-session" and len(by_latest["recaps"]) == 2

    print("recap_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
