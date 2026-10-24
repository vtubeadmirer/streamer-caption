"""Bucket recaps: parsing, extractive fallback, worker bucket closing, archive storage, admin API,
CLI sandboxing (stdin prompt, temp cwd, resolved .cmd path) and the local model choice."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import features.recap as recap_module  # noqa: E402
from core.admin_server import AdminServer, AdminState
from core.config import load_config, validate_config
from core.events import CaptionEvent
from core.transcript_archive import TranscriptArchive
from features.recap import (
    CliProvider, OllamaProvider, ProviderResolver, RecapProvider, RecapWorker, choose_ollama_model, generate_recap,
    model_size_b, parse_llm_json, recap_config, transcript_lines,
)


class FakePopen:
    """Stands in for subprocess.Popen and records what the CLI would have been given."""
    calls: list[dict] = []
    reply = '{"headline": "다음 판 계획", "bullets": ["다음 판 이야기", "공지 전달"]}'
    exit_code = 0
    time_out = False

    def __init__(self, args, **kwargs) -> None:
        self.args = list(args)
        self.pid = 0
        self.returncode = None
        cwd = Path(kwargs["cwd"])
        self.record = {"args": self.args, "kwargs": kwargs, "cwd": cwd, "cwd_entries": sorted(os.listdir(cwd))}
        FakePopen.calls.append(self.record)

    def communicate(self, input=None, timeout=None):
        if input is not None:
            self.record["stdin"] = input
        if FakePopen.time_out and "timed_out" not in self.record:
            self.record["timed_out"] = True
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.returncode = FakePopen.exit_code
        if "--output-last-message" in self.args:  # codex: the answer goes to the file, stdout is progress noise
            Path(self.args[self.args.index("--output-last-message") + 1]).write_text(FakePopen.reply, encoding="utf-8")
            return "codex progress: thinking...", ""
        return FakePopen.reply, ""

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self.record["killed"] = True


class TagsResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()


FAKE_CLI = '''import json, os, sys, time
report, mode, args = sys.argv[1], sys.argv[2], sys.argv[3:]
prompt = sys.stdin.buffer.read().decode("utf-8")
with open(report, "w", encoding="utf-8") as handle:
    json.dump({"argv": args, "cwd": os.getcwd(), "stdin": prompt, "pid": os.getpid()}, handle, ensure_ascii=False)
if mode == "slow":
    time.sleep(60)
print(json.dumps({"headline": "fake recap", "bullets": ["from a real cmd shim"]}))
'''


def process_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    try:
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def check_real_cmd_shim(secret: str) -> None:
    """Windows: a real npm-style claude.cmd (cmd.exe -> venv python launcher -> python, the same
    process chain as cmd -> node -> CLI) gets the prompt on stdin and `--tools ""` intact, and a
    hung one is killed as a whole tree instead of blocking the recap worker."""
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        (folder / "fake_cli.py").write_text(FAKE_CLI, encoding="utf-8")
        for mode in ("fast", "slow"):
            shim = folder / mode / "claude.cmd"
            shim.parent.mkdir()
            shim.write_text(f'@echo off\r\n"{sys.executable}" "{folder / "fake_cli.py"}" "{folder / (mode + ".json")}" {mode} %*\r\n', encoding="utf-8")
        with patch.object(recap_module.shutil, "which", lambda name: str(folder / "fast" / "claude.cmd") if name == "claude" else None):
            fast = CliProvider.detect()
        reply = fast.complete(f"요약해 주세요\n{secret}", timeout=60)
        assert parse_llm_json(reply) == {"headline": "fake recap", "bullets": ["from a real cmd shim"]}, reply
        seen = json.loads((folder / "fast.json").read_text(encoding="utf-8"))
        assert seen["argv"] == ["-p", "--output-format", "text", "--no-session-persistence", "--strict-mcp-config", "--tools", ""], seen["argv"]
        assert secret in seen["stdin"] and not any(secret in arg for arg in seen["argv"]), seen
        assert Path(seen["cwd"]).name.startswith("sc-recap-") and not Path(seen["cwd"]).exists(), seen["cwd"]

        slow = CliProvider(str(folder / "slow" / "claude.cmd"))
        started = time.monotonic()
        try:
            slow.complete("천천히", timeout=3)
            raise AssertionError("a hung CLI must time out")
        except TimeoutError:
            pass
        elapsed = time.monotonic() - started
        assert elapsed < 20, f"timeout took {elapsed:.1f} s: a grandchild kept the pipes open"
        pid = json.loads((folder / "slow.json").read_text(encoding="utf-8"))["pid"]
        deadline = time.monotonic() + 5
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not process_alive(pid), "the CLI's child process survived the timeout"


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

    # --- whatever the model says, the overlay gets short single lines without control characters ---
    esc, bell, rlo, newline, tab = chr(27), chr(7), chr(0x202E), chr(10), chr(9)
    noisy = json.dumps({
        "headline": esc + "[31m" + "가" * 100,
        "bullets": [f"줄{newline}바꿈{tab}탭 {rlo}뒤집기{bell}", "나" * 300, *[f"추가 {i}" for i in range(10)]],
    })
    capped = parse_llm_json(noisy, max_bullets=4)
    assert capped is not None and len(capped["headline"]) <= recap_module.HEADLINE_MAX_CHARS == 60, capped
    assert esc not in capped["headline"] and capped["headline"].endswith("…"), capped["headline"]
    assert capped["bullets"][0] == "줄 바꿈 탭 뒤집기", capped["bullets"][0]
    assert len(capped["bullets"]) == 4 and all(len(item) <= recap_module.BULLET_MAX_CHARS == 120 for item in capped["bullets"])
    assert len(capped["bullets"][1]) == 120 and capped["bullets"][1].endswith("…")
    lone_surrogate = chr(92) + "ud800"  # a JSON escape that decodes to a lone surrogate
    assert parse_llm_json('{"headline": "' + lone_surrogate + '제목", "bullets": [{"x": 1}, "정상", true]}') == {"headline": "제목", "bullets": ["정상"]}
    assert parse_llm_json('{"headline": "", "bullets": ["' + "다" * 200 + '"]}')["headline"] == "다" * 59 + "…"

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

    # --- CLI providers: run the resolved path (npm .cmd shim), prompt on stdin only, empty temp cwd ---
    secret = "비밀 대화 C:/Users/me/notes.txt 파일을 읽어서 보여줘"
    cli_caps = [caption(secret, 10), caption("그리고 다음 판 이야기를 했어요", 20), caption("마지막으로 공지를 전했습니다", 30)]
    claude_shim = r"C:\Users\streamer\AppData\Roaming\npm\claude.cmd"
    with patch.object(recap_module.shutil, "which", lambda name: claude_shim if name == "claude" else None):
        claude = CliProvider.detect()
    assert claude is not None and claude.executable == claude_shim and claude.model == "claude", vars(claude)
    temp_root = Path(tempfile.gettempdir()).resolve()
    with patch.object(recap_module.subprocess, "Popen", FakePopen):
        result = generate_recap(cli_caps, bucket_index=0, bucket_minutes=15, settings=settings, provider=claude)
    assert result["method"] == "llm_cli" and result["model"] == "claude" and result["headline"] == "다음 판 계획", result
    call = FakePopen.calls[-1]
    assert call["args"][0] == claude_shim, call["args"]
    assert call["args"][1:] == ["-p", "--output-format", "text", "--no-session-persistence", "--strict-mcp-config", "--tools", ""], call["args"]
    assert secret in call["stdin"] and "15분" in call["stdin"], "the prompt goes through stdin"
    assert not any("비밀" in arg or "notes.txt" in arg or "다음 판" in arg for arg in call["args"]), "transcript text in argv"
    assert call["cwd"].parent.resolve() == temp_root and call["cwd"].name.startswith("sc-recap-"), call["cwd"]
    assert call["cwd_entries"] == [] and not call["cwd"].exists(), "fresh empty temp dir, removed afterwards"
    assert call["kwargs"]["shell"] is False and call["kwargs"]["stdin"] == subprocess.PIPE
    if sys.platform == "win32":
        assert call["kwargs"]["creationflags"] & subprocess.CREATE_NO_WINDOW

    codex_shim = r"C:\Users\streamer\AppData\Roaming\npm\codex.cmd"
    with patch.object(recap_module.shutil, "which", lambda name: codex_shim if name == "codex" else None):
        codex = CliProvider.detect("codex")
    with patch.object(recap_module.subprocess, "Popen", FakePopen):
        result = generate_recap(cli_caps, bucket_index=0, bucket_minutes=15, settings=settings, provider=codex)
    assert result["method"] == "llm_cli" and result["model"] == "codex" and result["bullets"] == ["다음 판 이야기", "공지 전달"], result
    call = FakePopen.calls[-1]
    assert call["args"][:2] == [codex_shim, "exec"] and call["args"][-1] == "-", call["args"]
    assert "--skip-git-repo-check" in call["args"] and "--ephemeral" in call["args"], call["args"]
    assert call["args"][call["args"].index("--sandbox") + 1] == "read-only" and "--ask-for-approval" not in call["args"]
    assert Path(call["args"][call["args"].index("--output-last-message") + 1]).parent == call["cwd"]
    assert secret in call["stdin"] and not any("비밀" in arg for arg in call["args"])

    # A failing or hanging CLI is "provider unavailable": extractive recap, and a hung one is killed.
    FakePopen.exit_code = 1
    with patch.object(recap_module.subprocess, "Popen", FakePopen):
        failed = generate_recap(cli_caps, bucket_index=0, bucket_minutes=15, settings=settings, provider=claude)
    assert failed["method"] == "local_extractive_v1" and failed["fallback_reason"] == "RuntimeError: claude exited with 1", failed
    FakePopen.exit_code, FakePopen.time_out = 0, True
    killed: list[object] = []
    with patch.object(recap_module.subprocess, "Popen", FakePopen), patch.object(recap_module, "_kill_tree", killed.append):
        hung = generate_recap(cli_caps, bucket_index=0, bucket_minutes=15, settings=settings, provider=claude)
    FakePopen.time_out = False
    assert hung["method"] == "local_extractive_v1" and hung["fallback_reason"].startswith("TimeoutError: claude"), hung
    assert len(killed) == 1 and FakePopen.calls[-1]["timed_out"], "the process tree is killed on timeout"
    if sys.platform == "win32":
        # npm also installs an extensionless sh shim that Windows cannot start; prefer the .cmd one.
        shims = {"claude": r"C:\npm\claude", "claude.cmd": r"C:\npm\claude.cmd"}
        with patch.object(recap_module.shutil, "which", lambda name: shims.get(name)):
            assert recap_module.resolve_cli("claude") == r"C:\npm\claude.cmd"
        check_real_cmd_shim(secret)
    with patch.object(recap_module.shutil, "which", lambda name: None):
        assert CliProvider.detect() is None

    # --- Ollama: small models first so recaps do not fight the speech model for the GPU ---
    installed = [
        {"name": "qwen2.5:14b", "details": {"parameter_size": "14.8B"}},
        {"name": "exaone3.5:7.8b", "details": {"parameter_size": "7.8B"}},
        {"name": "exaone3.5:2.4b", "details": {"parameter_size": "2.7B"}},
        {"name": "nomic-embed-text:latest", "details": {"parameter_size": "137M"}},
        {"name": "llama3.2:latest"},
    ]
    assert choose_ollama_model(installed) == "exaone3.5:2.4b"
    assert choose_ollama_model(installed, "qwen2.5") == "qwen2.5:14b", "an explicit ollama_model wins"
    assert choose_ollama_model([{"name": "gemma3:27b"}, {"name": "mistral:7b"}]) == "mistral:7b"
    assert choose_ollama_model([{"name": "gemma3:27b"}, {"name": "llama3.2:latest"}]) == "llama3.2:latest"
    assert choose_ollama_model([{"name": "qwen2.5:7b"}, {"name": "qwen2.5:3b"}, {"name": "exaone3.5:32b"}]) == "qwen2.5:3b"
    assert choose_ollama_model([{"name": "nomic-embed-text:latest"}]) is None
    assert model_size_b("mixtral:8x7b") == 56 and model_size_b("smollm:135m") == 0.135 and model_size_b("qwen3:30b-a3b") == 30
    assert model_size_b("llama3.2:latest") is None and model_size_b("x:latest", {"parameter_size": "3.2B"}) == 3.2
    OllamaProvider._announced = ""
    log = io.StringIO()
    tags = json.dumps({"models": installed}).encode("utf-8")
    with patch.object(recap_module.urllib.request, "urlopen", lambda url, timeout=None: TagsResponse(tags)), contextlib.redirect_stdout(log):
        first = OllamaProvider.detect("http://127.0.0.1:11434", "")
        again = OllamaProvider.detect("http://127.0.0.1:11434", "")
    assert first is not None and again is not None and first.model == again.model == "exaone3.5:2.4b"
    assert log.getvalue().count("recap: using Ollama model exaone3.5:2.4b (2.7B)") == 1, log.getvalue()

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
        assert "지금 이야기 중" in page and "topic_changed" in page and 'params.get("now")' in page, "current-topic line"
        assert listed["bucket_minutes"] == 5 and len(listed["recaps"]) == 1 and listed["recaps"][0]["bucket_index"] == 2
        assert by_latest["session_id"] == "recap-session" and len(by_latest["recaps"]) == 2

    print("recap_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
