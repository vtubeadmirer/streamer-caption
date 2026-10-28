"""The bug-report bundle must never contain what was said on stream.

The fixtures come from the real pipeline, not hand-written dicts: STT results go through
main.pending_events / main.postprocess_event into JsonlLogWriter and the transcript archive
(SessionArchiveRecorder, which also emits the live summary and the extractive recap and writes
broadcast_report.json), then tools.broadcast_validation writes validation_report.json. Whatever
those modules log next, the bundle has to hide it.
"""
from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core.app_info as app_info  # noqa: E402
from core.admin_server import AdminState  # noqa: E402
from core.config import DEFAULTS  # noqa: E402
from core.events import CaptionEvent  # noqa: E402
from core.log_writer import JsonlLogWriter  # noqa: E402
from core.monitoring import MemoryMonitor  # noqa: E402
from core.recovery import ConfirmedRangeLedger  # noqa: E402
from core.session_archive import SessionArchiveRecorder  # noqa: E402
from core.stt_worker import SttWorker  # noqa: E402
from core.audio_capture import AudioChunk  # noqa: E402
from engines.baseline.engine import BaselineEngine  # noqa: E402
from engines.simulstreaming.route import SimulStreamingRoute  # noqa: E402
from features.defense import DefenseFilter  # noqa: E402
from features.slang import SlangDictionary  # noqa: E402
from features.summarizer import tokenize  # noqa: E402
from main import pending_events, postprocess_event  # noqa: E402
from tools.broadcast_validation import write_validation_report  # noqa: E402
from tools.make_support_bundle import (  # noqa: E402
    environment_text,
    make_issue_bundle,
    scrub_csv,
    scrub_jsonl,
    scrub_value,
)

WORDS = ["쿼카망고", "자몽펭귄", "라벤더고래", "민트수박", "zebracactus"]
SLANG_SOURCE, SLANG_TARGET = "보라호박", "슬랭치환판다"  # the target exists only after correction
VOCABULARY = {*WORDS, SLANG_SOURCE, SLANG_TARGET}  # every word the captions below are made of
MIC_LABEL, SYSTEM_LABEL = "비밀라벨여우", "컴퓨터라벨부엉이"
PROMPT = "프롬프트비밀호두"
TITLE = "타이틀비밀수달"
ANNOTATION_NOTE = "메모비밀두더지"
VOD_SECRET = "채널비밀오소리"
STATUS_TEXT = "상태비밀너구리"
ORG = "비밀회사이름"
SECRETS = [*WORDS, SLANG_SOURCE, SLANG_TARGET, MIC_LABEL, SYSTEM_LABEL, PROMPT, TITLE, ANNOTATION_NOTE,
           VOD_SECRET, STATUS_TEXT, ORG]
NOTE = "자막이 10분 뒤 멈춤"
DEVICE = "Microphone (USB Audio Device)"
# (source, what Whisper returned, confidence); 0.62 is shown dimmed with defense_reason dim_confidence.
LINES = [
    ("microphone", f"{WORDS[0]} {WORDS[1]} {WORDS[2]} {SLANG_SOURCE}", 0.91),
    ("microphone", f"{WORDS[1]} {WORDS[2]} {WORDS[4]} {WORDS[0]}", 0.62),
    ("system", f"{WORDS[2]} {WORDS[0]} {WORDS[3]} {WORDS[1]}", 0.90),
    ("microphone", f"{WORDS[3]} {WORDS[4]} {SLANG_SOURCE} {WORDS[0]}", 0.93),
]


# 정확 우선 (SimulStreaming) increments, as the server sends them: append-only words, then is_final.
SIMUL_MESSAGES = [
    {"text": f" {WORDS[0]}", "end": 1.0, "is_final": False, "words": [{"text": f" {WORDS[0]}", "end": 1.0}]},
    {"text": f" {WORDS[1]}.", "end": 1.6, "is_final": False, "words": [{"text": f" {WORDS[1]}.", "end": 1.6}]},
    {"text": f" {WORDS[2]}", "end": 2.2, "is_final": False, "words": [{"text": f" {WORDS[2]}", "end": 2.2}]},
    {"is_final": True, "end": 2.6},
]
SIMUL_CONFIRMED = 2  # "<w0> <w1>." at the sentence end and "<w2>" at is_final


class ScriptedSimulClient:
    """The route's view of a SimulStreaming server: connects, sends SIMUL_MESSAGES, then dies."""

    def __init__(self) -> None:
        self.connected = False
        self.audio_fed = 0
        self._sent = False

    def launch(self) -> None:
        self.connected = True

    def send_audio(self, samples: Any) -> None:
        self.audio_fed += len(samples)

    def get_messages(self, max_items: int = 100) -> list[dict[str, Any]]:
        if self.audio_fed and not self._sent:
            self._sent = True
            return [dict(message) for message in SIMUL_MESSAGES]
        return []

    def failure(self) -> str:
        return "crashed" if self._sent else ""

    def snapshot(self) -> dict[str, Any]:
        return {"lag_sec": 0.4, "heartbeat_age_sec": 0.2, "server_pid": 4242, "server_max_iter_sec": 0.9}

    def kill(self) -> None:
        pass

    def close(self) -> None:
        self.connected = False


def record_simulstreaming(config: dict[str, Any], defense: DefenseFilter, slang: SlangDictionary,
                          emit: Any, writer: JsonlLogWriter) -> SimulStreamingRoute:
    """main.run_v2_light's handle_route_output for a real route on a scripted server."""
    import numpy as np

    route = SimulStreamingRoute(config, "microphone", client_factory=lambda cfg, **kwargs: ScriptedSimulClient(),
                                status_provider=lambda cfg: {"available": True, "reason": ""}, gpu_probe=lambda: None)
    route_metadata = {"audio_source": "microphone", "audio_mode": "both", "source_label": MIC_LABEL, "source_priority": 0}
    route.standby_ready()
    outputs = route.poll() + route.poll()  # starting, ready
    route.feed(AudioChunk(samples=np.zeros(1024, dtype=np.float32), rms=0.0, offset_sec=30.0, duration_sec=0.064,
                          source_name="microphone"), can_switch=True)
    outputs += route.poll()  # active, captions, then the crash -> off (max_restarts 0)
    for output in outputs:
        if output.kind == "status":
            writer.append_event(CaptionEvent(event_type="engine_status", text=output.text, engine="simulstreaming",
                                             metadata=dict(output.status)))
        elif output.kind == "caption":
            event = CaptionEvent(event_type=output.event.event_type, text=output.event.text,
                                 confidence=output.event.confidence, audio_offset_sec=output.event.audio_offset_sec,
                                 engine=output.event.engine, metadata={**output.event.metadata, **route_metadata})
            converted = pending_events(event, defense, slang) if output.pending else postprocess_event(event, defense, slang)
            for out in converted:
                emit(out, extra=None if output.pending else {"raw_text": event.text, "caption_engine": "simulstreaming"})
    assert route.state == "off" and route.reason == "crashed", route.snapshot()
    return route


def pipeline_config(tmp: Path) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULTS)
    config["audio"]["source"] = "both"
    config["caption"]["microphone_label"] = MIC_LABEL
    config["caption"]["system_label"] = SYSTEM_LABEL
    config["whisper"]["initial_prompt"] = PROMPT
    slang_path = tmp / "slang_dict.json"
    slang_path.write_text(json.dumps({SLANG_SOURCE: SLANG_TARGET}, ensure_ascii=False), encoding="utf-8")
    config["slang"]["dict_path"] = str(slang_path)
    config["defense"]["blocklist_path"] = str(tmp / "blocklist.txt")
    config["defense"]["whitelist_path"] = str(tmp / "whitelist.txt")
    config["archive"]["db_path"] = str(tmp / "transcripts.sqlite3")
    config["archive"]["recap"]["provider"] = "extractive"  # never hand test captions to a local LLM or CLI
    config["log"]["dir"] = str(tmp / "logs")
    (tmp / "models").mkdir(exist_ok=True)
    (tmp / "models" / "large-v3.pt").write_bytes(b"x")
    config["simulstreaming"]["model_dir"] = str(tmp / "models")
    config["simulstreaming"]["max_restarts"] = 0
    config["streaming"]["engine"] = "simulstreaming"
    return config


def record_session(config: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    """main.run_v2_light minus audio and Whisper: STT results -> pending/postprocess -> emit()."""
    writer = JsonlLogWriter(config["log"]["dir"])
    recorder = SessionArchiveRecorder(config, writer, source="both", engine="baseline", mode="v2_light")
    monitor = MemoryMonitor(config, writer.session_dir)
    defense, slang = DefenseFilter(config), SlangDictionary(config)
    engine = BaselineEngine(config)
    engine_metadata = engine._metadata(16000)  # model/device/compute_type exactly as the engine logs them
    ledger = ConfirmedRangeLedger()
    counts = {"caption_confirmed": 0, "caption_pending": 0, "caption_hidden": 0}
    last = {"caption": "", "error": ""}

    def emit(event: CaptionEvent, extra: dict[str, Any] | None = None) -> None:
        writer.append_event(event, extra=extra)
        recorder.record_event(event, extra=extra)
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
        if event.event_type == "caption_confirmed":
            last["caption"] = event.text
        if event.event_type == "engine_error":
            last["error"] = event.text

    memory = monitor.maybe_sample(force=True)
    assert memory is not None
    writer.append_event(memory)
    job_id = 0
    for index, (source, text, confidence) in enumerate(LINES):
        start = index * 4.0
        job = {
            "audio_source": source,
            "audio_mode": "both",
            "source_label": MIC_LABEL if source == "microphone" else SYSTEM_LABEL,
            "source_priority": 0 if source == "microphone" else 1,
            "segment_duration_sec": 2.5,
            "segment_peak_rms": 0.21,
            "segment_voice_ratio": 0.82,
            "segment_start_offset_sec": start,
            "segment_end_offset_sec": start + 2.5,
            "segment_start_sample": int(start * 16000),
            "segment_end_sample": int((start + 2.5) * 16000),
        }
        result = CaptionEvent(
            event_type="caption_confirmed", text=text, confidence=confidence, audio_offset_sec=start + 2.5,
            engine="baseline",
            metadata={**job, **engine_metadata, "no_speech_prob": round(0.04 + index / 100, 2), "avg_logprob": -0.21,
                      "compression_ratio": 1.3, "transcript_segment_count": 1},
        )
        if source == "microphone":  # system previews are off by default in dual mode
            job_id += 1
            for event in pending_events(result, defense, slang):
                emit(event, extra={**job, "stt_elapsed_sec": 0.31, "stt_job_id": job_id, "stt_job_type": "pending"})
        job_id += 1
        assert ledger.claim(job_id, job)
        for event in postprocess_event(result, defense, slang):
            emit(event, extra={**job, "raw_text": result.text, "stt_elapsed_sec": 0.42, "stt_job_id": job_id,
                               "stt_job_type": "confirmed"})

    noise = CaptionEvent(
        event_type="caption_confirmed", text=f"{WORDS[3]} {WORDS[4]}", confidence=0.4, audio_offset_sec=20.0,
        engine="baseline",
        metadata={"audio_source": "system", "audio_mode": "both", "source_label": SYSTEM_LABEL, "source_priority": 1,
                  **engine_metadata, "no_speech_prob": 0.93},
    )
    for event in postprocess_event(noise, defense, slang):
        emit(event, extra={"stt_job_type": "confirmed"})
    failure = CaptionEvent(event_type="engine_error", text=f"RuntimeError: cannot read {Path.home() / 'models' / 'model.bin'}",
                           audio_offset_sec=22.0, engine="baseline", metadata={"audio_source": "microphone"})
    for event in postprocess_event(failure, defense, slang):
        emit(event)
    route = record_simulstreaming(config, defense, slang, emit, writer)

    store = recorder.store
    assert store is not None, recorder.last_error
    store.add_annotation(writer.session_id, kind="wrong_word", offset_sec=2.5, note=ANNOTATION_NOTE, caption_text=LINES[0][1])
    store.update_session_meta(writer.session_id, title=TITLE, vod_url=f"https://vod.example.test/{VOD_SECRET}")
    writer.append_event(CaptionEvent(
        event_type="engine_status", text="v2_light session summary", engine="baseline",
        metadata={**SttWorker(engine).snapshot(), **ledger.snapshot(), "audio_source": "both", "audio_source_restarts": 0,
                  "audio_source_statuses": {"microphone": {"restart_count": 0, "last_error": "", "dropped_chunks": 0,
                                                           "device_label": DEVICE}},
                  "pipeline_exit_code": 0},
    ))
    recorder.close({"elapsed_sec": 30.0, "segments": len(LINES) + 1, "captions": counts["caption_confirmed"],
                    "pending": counts["caption_pending"], "hidden": counts["caption_hidden"], "discarded": 0,
                    "pipeline_exit_code": 0, "stopped_by_user": True})
    write_validation_report(writer.path, store, config)

    state = AdminState()
    state.last_caption = last["caption"]
    state.last_error = last["error"]
    state.caption_count = counts["caption_confirmed"]
    state.pending_caption_count = counts["caption_pending"]
    state.hidden_caption_count = counts["caption_hidden"]
    state.pipeline_warning = "STT job 7 is slow (21.0s). Monitoring for recovery."
    # An installed copy keeps its logs under the user profile.
    state.last_memory = {**memory.metadata,
                         "path": str(Path.home() / "AppData" / "Local" / "StreamerCaption" / "logs" / "memory.csv")}
    state.last_audio = {"rms": 0.021, "threshold": 0.5, "is_voice": True, "offset_sec": 12.0, "duration_sec": 0.064,
                        "dropped_chunks": 0, "status": "", "clip_fraction": 0.0, "input_gain": 1.0,
                        "audio_source": "microphone", "source_restarts": 0, "source_last_error": "",
                        "device_label": DEVICE, "vad_engine": "silero", "vad_score": 0.91,
                        "discard": {"reason": "low_peak_rms", "peak_rms": 0.004, "min_peak_rms": 0.012}}
    state.audio_sources = {"microphone": state.last_audio}
    state.last_segment = {"audio_source": "microphone", "audio_mode": "both", "source_label": MIC_LABEL,
                          "source_priority": 0, "duration_sec": 2.5, "peak_rms": 0.21, "voice_ratio": 0.82}
    state.last_stt = {**SttWorker(engine).snapshot(), **ledger.snapshot(), "job_id": job_id, "job_type": "confirmed",
                      "elapsed_sec": 0.42, "event_count": 1, "text": STATUS_TEXT}
    state.engine = route.snapshot()
    return writer.session_dir, state.to_dict()


def read_rows(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def first(rows: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    return next(row for row in rows if row["event_type"] == event_type)


def numbers(value: Any, path: str = "") -> dict[str, Any]:
    """Every number, flag and null in a JSON value, by path."""
    if isinstance(value, dict):
        return {k: v for key, item in value.items() for k, v in numbers(item, f"{path}.{key}").items()}
    if isinstance(value, list):
        return {k: v for index, item in enumerate(value) for k, v in numbers(item, f"{path}[{index}]").items()}
    return {} if isinstance(value, str) else {path: value}


def check_fixture(session_dir: Path) -> None:
    """The real pipeline really writes spoken words where the old deny-list did not look."""
    rows = read_rows((session_dir / "session.jsonl").read_text(encoding="utf-8"))
    assert {"caption_pending", "caption_confirmed", "caption_hidden", "summary_updated", "recap_updated",
            "broadcast_ended", "memory_sample", "engine_error", "engine_status"} <= {row["event_type"] for row in rows}
    confirmed = first(rows, "caption_confirmed")["metadata"]
    assert SLANG_TARGET in confirmed["corrected_text"] and confirmed["source_label"] == MIC_LABEL, confirmed
    summary = first(rows, "summary_updated")["metadata"]["summary"]
    assert set(summary["headline"].split(" · ")) <= VOCABULARY and summary["keywords"][0]["term"] in VOCABULARY, summary
    recap = first(rows, "recap_updated")["metadata"]["recap"]
    assert set(recap["headline"].split(" · ")) <= VOCABULARY, recap
    assert recap["metadata"]["keywords"][0]["term"] in VOCABULARY, recap
    report = json.loads((session_dir / "broadcast_report.json").read_text(encoding="utf-8"))
    assert set(report["summaries"][0]["headline"].split(" · ")) <= VOCABULARY, report["summaries"]
    assert report["keywords"][0]["term"] in VOCABULARY, report["keywords"]
    assert report["session"]["title"] == TITLE and VOD_SECRET in report["session"]["vod_url"]
    validation = json.loads((session_dir / "validation_report.json").read_text(encoding="utf-8"))
    assert validation["annotations"][0]["caption_text"] == LINES[0][1], validation["annotations"]
    assert validation["annotations"][0]["note"] == ANNOTATION_NOTE
    # Every keyword the summarizer can extract from these captions is a secret word.
    assert all(set(tokenize(text)) <= VOCABULARY for _, text, _ in LINES)


def bundle_config(config: dict[str, Any]) -> dict[str, Any]:
    """The same settings with the paths a OneDrive-synced install writes into config.json."""
    shipped = copy.deepcopy(config)
    data = Path.home() / "OneDrive" / "문서" / "Streamer Caption" / "user-data" / "data"
    shipped["slang"]["dict_path"] = str(data / "slang_dict.json")
    shipped["defense"]["blocklist_path"] = str(data / "blocklist.txt")
    shipped["defense"]["whitelist_path"] = str(data / "whitelist.txt")
    shipped["archive"]["db_path"] = str(data / "transcripts.sqlite3")
    shipped["archive"]["backup_dir"] = str(Path.home() / f"OneDrive - {ORG}" / "backups")
    shipped["log"]["dir"] = str(data.parent / "logs")
    shipped["simulstreaming"]["model_path"] = (Path.home() / "models" / "large-v3").as_posix()
    return shipped


def check_unit_rules() -> None:
    # The property the allow-list guarantees: a key nobody has reviewed yet hides its text.
    assert scrub_value({"brand_new_key": "secret"}) == {"brand_new_key": "[redacted 6 chars]"}
    scrubbed = scrub_value({"text": "hello", "n": 3, "ok": True, "gone": None,
                            "nested": [{"summary": "x", "event_type": "caption_confirmed"}]})
    assert scrubbed == {"text": "[redacted 5 chars]", "n": 3, "ok": True, "gone": None,
                        "nested": [{"summary": "[redacted 1 chars]", "event_type": "caption_confirmed"}]}, scrubbed
    assert scrub_jsonl('{"text": "a"}\nnot json\n') == '{"text": "[redacted 1 chars]"}\n{"unparsable_line": true}\n'
    # "name" is structural only inside a GPU row or a check result.
    assert scrub_value({"gpus": [{"name": "RTX 4060 Ti"}], "name": "방송 제목"}) == {
        "gpus": [{"name": "RTX 4060 Ti"}], "name": "[redacted 5 chars]"}
    # Exception text survives, capped; raw engine output quoted inside it does not.
    assert len(scrub_value({"error": "x" * 500})["error"]) == 300
    quoted = '{"text": "비밀 자막"'
    assert scrub_value({"last_error": f"Invalid SimulStreaming JSON: {quoted}"})["last_error"] == (
        f"Invalid SimulStreaming JSON: [redacted {len(quoted)} chars]")
    # Any separator and any letter case of the profile path is masked.
    shouted = str(Path.home()).upper().replace("\\", "/") + "/logs/memory.csv"
    assert scrub_value({"path": shouted})["path"] == "%USERPROFILE%/logs/memory.csv"
    assert scrub_csv("elapsed_sec,rss_mb\n1.5,512.25\nhello,\n") == "elapsed_sec,rss_mb\n1.5,512.25\n[redacted 5 chars],\n"


def check_bundle(result: dict[str, Any], members: dict[str, str], session_dir: Path, status: dict[str, Any],
                 config: dict[str, Any]) -> None:
    session = f"logs/{session_dir.name}/"
    names = set(members)
    assert names == set(result["files"]), (sorted(names), result["files"])
    for expected in ("README.txt", "environment.json", "config.json", "status.json", "note.txt", session + "session.jsonl",
                     session + "memory.csv", session + "broadcast_report.json", session + "validation_report.json"):
        assert expected in names, (expected, sorted(names))
    # captions.txt, latest_captions.txt, broadcast_report.md and validation_report.md stay home.
    assert not [name for name in names - {"README.txt", "note.txt"} if name.endswith((".txt", ".md"))], sorted(names)

    public = "\n".join(members.values()) + json.dumps(result["environment"], ensure_ascii=False) + result["environment_text"]
    for secret in SECRETS:
        for form in {secret, json.dumps(secret)[1:-1]}:
            assert form not in public, f"secret leaked: {secret}"
    home = str(Path.home())
    lowered = public.lower()
    for form in {home, home.replace("\\", "\\\\"), home.replace("\\", "/")}:
        assert form.lower() not in lowered, f"user profile path leaked: {form}"
    separator = r"(?:\\\\|\\|/)+"
    profile = re.compile("[a-z]:" + separator + "users" + separator + re.escape(Path.home().name.lower()) + r"(?![\w.-])")
    assert not profile.search(lowered), profile.search(lowered)

    # Structure survives: same events in the same order, every number and flag unchanged.
    raw = read_rows((session_dir / "session.jsonl").read_text(encoding="utf-8"))
    shipped = read_rows(members[session + "session.jsonl"])
    assert [row["event_type"] for row in shipped] == [row["event_type"] for row in raw]
    for before, after in zip(raw, shipped):
        assert numbers(after) == numbers(before), before["event_type"]
    confirmed, raw_confirmed = first(shipped, "caption_confirmed"), first(raw, "caption_confirmed")
    assert confirmed["text"] == f"[redacted {len(raw_confirmed['text'])} chars]", confirmed
    assert confirmed["metadata"]["corrected_text"].startswith("[redacted"), confirmed
    assert confirmed["metadata"]["source_label"] == f"[redacted {len(MIC_LABEL)} chars]", confirmed
    assert confirmed["extra"]["raw_text"].startswith("[redacted"), confirmed
    assert confirmed["metadata"]["no_speech_prob"] == raw_confirmed["metadata"]["no_speech_prob"]
    assert confirmed["metadata"]["audio_source"] == "microphone" and confirmed["engine"] == "baseline"
    assert confirmed["metadata"]["model"] == raw_confirmed["metadata"]["model"] and confirmed["ts"] == raw_confirmed["ts"]
    dimmed = [row for row in shipped if row["metadata"].get("defense_reason") == "dim_confidence"]
    assert dimmed and dimmed[0]["metadata"]["defense_signals"] == ["weak_confidence"], dimmed
    hidden = first(shipped, "caption_hidden")["metadata"]
    assert hidden["defense_reason"] == "no_speech_probability", hidden
    assert hidden["defense_signals"] == ["high_no_speech_probability"], hidden
    summary = first(shipped, "summary_updated")["metadata"]["summary"]
    assert summary["headline"].startswith("[redacted") and summary["method"] == "local_extractive_v1", summary
    assert all(item["term"].startswith("[redacted") for item in summary["keywords"]), summary
    recap = first(shipped, "recap_updated")["metadata"]["recap"]
    assert recap["headline"].startswith("[redacted") and recap["method"] == "local_extractive_v1", recap
    assert first(shipped, "memory_sample")["metadata"]["rss_mb"] == first(raw, "memory_sample")["metadata"]["rss_mb"]
    assert members[session + "memory.csv"] == (session_dir / "memory.csv").read_text(encoding="utf-8")
    report = json.loads(members[session + "broadcast_report.json"])
    assert report["summaries"][0]["headline"].startswith("[redacted") and report["keywords"][0]["term"].startswith("[redacted")
    assert report["stats"]["caption_count"] == len(LINES) + SIMUL_CONFIRMED, report["stats"]
    # 정확 우선 engine rows: words redacted, engine state and fallback reason kept.
    simul_raw = [row for row in raw if row["engine"] == "simulstreaming"]
    simul = [row for row in shipped if row["engine"] == "simulstreaming"]
    assert len(simul) == len(simul_raw) and simul, (len(simul), len(simul_raw))
    simul_captions = [row for row in simul if row["event_type"] in {"caption_pending", "caption_confirmed"}]
    assert len([row for row in simul_captions if row["event_type"] == "caption_confirmed"]) == SIMUL_CONFIRMED
    assert all(row["text"].startswith("[redacted") for row in simul_captions), simul_captions
    assert all(row["metadata"]["stable"] is True for row in simul_captions if row["event_type"] == "caption_pending")
    statuses = [(row["metadata"]["state"], row["metadata"]["reason"], row["metadata"]["active_engine"])
                for row in simul if row["event_type"] == "engine_status"]
    assert ("active", "", "simulstreaming") in statuses and ("off", "crashed", "baseline") in statuses, statuses
    confirmed_simul = next(row for row in simul_captions if row["event_type"] == "caption_confirmed")
    assert confirmed_simul["extra"]["caption_engine"] == "simulstreaming", confirmed_simul["extra"]
    assert confirmed_simul["extra"]["raw_text"].startswith("[redacted"), confirmed_simul["extra"]
    assert confirmed_simul["metadata"]["engine_model"] == "large-v3", confirmed_simul["metadata"]
    validation = json.loads(members[session + "validation_report.json"])
    assert validation["annotations"][0]["caption_text"].startswith("[redacted") and validation["annotations"][0]["kind"] == "wrong_word"
    assert validation["verdict"] in {"PASS", "WARN", "REVIEW", "FAIL"} and validation["checks"][0]["name"] == "jsonl_parse"

    state = json.loads(members["status.json"])
    assert state["last_caption"] == f"[redacted {len(status['last_caption'])} chars]", state["last_caption"]
    assert state["last_stt"]["text"] == f"[redacted {len(STATUS_TEXT)} chars]", state["last_stt"]
    assert state["last_stt"]["worker_mode"] == "thread" and state["caption_count"] == status["caption_count"]
    assert state["last_segment"]["source_label"] == f"[redacted {len(MIC_LABEL)} chars]", state["last_segment"]
    assert state["last_audio"]["device_label"] == DEVICE, "device names help and are not speech"
    assert state["last_memory"]["path"].startswith("%USERPROFILE%"), state["last_memory"]
    assert (state["engine"]["state"], state["engine"]["reason"], state["engine"]["requested_engine"]) == (
        "off", "crashed", "simulstreaming"), state["engine"]
    assert state["last_memory"]["rss_mb"] == status["last_memory"]["rss_mb"]
    assert state["last_error"].startswith("RuntimeError: cannot read %USERPROFILE%"), state["last_error"]
    assert members["note.txt"] == NOTE + "\n", "the user's own note is kept"

    shipped_config = json.loads(members["config.json"])
    assert shipped_config["whisper"]["initial_prompt"] == f"[redacted {len(PROMPT)} chars]"
    assert shipped_config["caption"]["microphone_label"] == f"[redacted {len(MIC_LABEL)} chars]"
    assert shipped_config["whisper"]["beam_size"] == config["whisper"]["beam_size"]
    assert shipped_config["slang"]["dict_path"].startswith("%USERPROFILE%"), shipped_config["slang"]
    assert shipped_config["archive"]["backup_dir"].startswith("%USERPROFILE%"), shipped_config["archive"]
    assert "OneDrive - [redacted]" in shipped_config["archive"]["backup_dir"], shipped_config["archive"]
    assert shipped_config["simulstreaming"]["model_path"] == "%USERPROFILE%/models/large-v3"
    environment = json.loads(members["environment.json"])
    assert environment == result["environment"]
    for key in ("project_root", "user_data_root"):
        assert environment["app"][key].startswith("%USERPROFILE%"), environment["app"]

    text = result["environment_text"]
    assert text.startswith("version:") and "gpu:" in text, text
    assert "\\" not in text and not re.search(r"[A-Za-z]:/", text), text
    custom = copy.deepcopy(result["environment"])
    custom["whisper"]["model"] = str(Path.home() / "models" / "large-v3")
    custom["gpu"] = {"ok": False, "error": f"[WinError 2] {Path.home() / 'nvidia-smi.exe'}"}
    text = environment_text(custom)
    assert "\\" not in text and home not in text and "model: (local path)" in text, text


def main() -> int:
    check_unit_rules()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        tmp = Path(directory).resolve()
        config = pipeline_config(tmp)
        session_dir, status = record_session(config)
        check_fixture(session_dir)
        # environment.json reads the install and user-data folders from core.app_info; put them
        # under the profile the way an installed copy has them.
        saved = app_info.PROJECT_ROOT, app_info.USER_DATA_ROOT
        install = Path.home() / "AppData" / "Local" / "StreamerCaption"
        app_info.PROJECT_ROOT, app_info.USER_DATA_ROOT = install / "app", install / "user-data"
        try:
            result = make_issue_bundle(status=status, note=NOTE, log_root=tmp / "logs", out_dir=tmp / "out",
                                       config=bundle_config(config))
        finally:
            app_info.PROJECT_ROOT, app_info.USER_DATA_ROOT = saved
        with zipfile.ZipFile(result["path"]) as bundle:
            members = {name: bundle.read(name).decode("utf-8") for name in bundle.namelist()}
        check_bundle(result, members, session_dir, status, config)
    print("support_bundle_privacy_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
