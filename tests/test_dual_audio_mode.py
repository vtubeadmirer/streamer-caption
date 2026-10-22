from __future__ import annotations

import copy
import queue
import sys
import tempfile
import threading
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.audio_capture import AudioChunk
from core.audio_sources import DualAudioChunkSource, config_for_audio_source
from core.config import load_config, validate_config
from core.events import CaptionEvent
from core.log_writer import JsonlLogWriter
from core.recovery import ConfirmedRangeLedger
from core.source_dedup import CrossSourceCaptionDeduplicator
from core.stt_worker import SttJob, enqueue_confirmed_by_priority


class FakeSource:
    def __init__(self, source_name: str, *, fail: bool = False) -> None:
        self.source_name = source_name
        self.fail = fail
        self.dropped_chunks = 0
        self.sample_count = 160
        self.restart_count = 0
        self.last_error = ""
        self.device_label = source_name
        self.input_gain = 1.0

    def chunks(self, max_seconds=None, stop_event=None):
        del max_seconds
        if self.fail:
            raise RuntimeError(f"{self.source_name} failed")
        if stop_event is not None and stop_event.is_set():
            return
        yield AudioChunk(
            samples=[0.1] * 160,
            rms=0.1,
            offset_sec=0.0,
            duration_sec=0.01,
            source_name=self.source_name,
        )


def fake_dual(*sources: FakeSource) -> DualAudioChunkSource:
    dual = DualAudioChunkSource.__new__(DualAudioChunkSource)
    dual.config = {}
    dual.sample_rate = 16000
    dual._sources = {source.source_name: source for source in sources}
    dual._queue = queue.Queue(maxsize=16)
    dual._stop_event = threading.Event()
    dual._errors = {}
    dual._threads = []
    dual._dropped_merge_chunks = 0
    return dual


def main() -> int:
    config = copy.deepcopy(load_config())
    config["audio"]["source"] = "both"
    validate_config(config)
    config["caption"]["max_lines"] = 500
    validate_config(config)
    invalid_lines = copy.deepcopy(config)
    invalid_lines["caption"]["max_lines"] = 501
    try:
        validate_config(invalid_lines)
    except ValueError:
        pass
    else:
        raise AssertionError("caption.max_lines=501 must be rejected")
    config["caption"]["ko_size"] = 6
    validate_config(config)
    invalid_size = copy.deepcopy(config)
    invalid_size["caption"]["ko_size"] = 5
    try:
        validate_config(invalid_size)
    except ValueError:
        pass
    else:
        raise AssertionError("caption.ko_size=5 must be rejected")

    mic_config = config_for_audio_source(config, "microphone")
    system_config = config_for_audio_source(config, "system")
    assert mic_config["audio"]["source"] == "microphone"
    assert system_config["audio"]["source"] == "system"
    assert mic_config["audio"]["input_gain"] == config["audio"]["microphone_input_gain"]
    assert system_config["audio"]["vad_energy_threshold"] == config["audio"]["system_vad_energy_threshold"]

    dual = fake_dual(FakeSource("microphone"), FakeSource("system"))
    chunks = list(dual.chunks(max_seconds=1.0))
    assert {chunk.source_name for chunk in chunks} == {"microphone", "system"}

    degraded = fake_dual(FakeSource("microphone"), FakeSource("system", fail=True))
    chunks = list(degraded.chunks(max_seconds=1.0))
    assert [chunk.source_name for chunk in chunks] == ["microphone"]
    assert "system" in degraded.source_statuses()
    assert "failed" in degraded.source_statuses()["system"]["last_error"]

    jobs: deque[SttJob] = deque()
    system_job = SttJob(1, "confirmed", [], 16000, 0.0, {"source_priority": 1})
    mic_job = SttJob(2, "confirmed", [], 16000, 0.0, {"source_priority": 0})
    enqueue_confirmed_by_priority(jobs, system_job)
    enqueue_confirmed_by_priority(jobs, mic_job)
    assert [job.job_id for job in jobs] == [2, 1]

    ledger = ConfirmedRangeLedger()
    common = {"segment_start_sample": 0, "segment_end_sample": 16000}
    assert ledger.claim(1, {**common, "audio_source": "microphone"})
    assert ledger.claim(2, {**common, "audio_source": "system"})
    assert not ledger.claim(3, {**common, "audio_source": "system"})

    dedupe = CrossSourceCaptionDeduplicator(config)
    mic_event = CaptionEvent(
        event_type="caption_confirmed",
        text="안녕하세요 반갑습니다",
        audio_offset_sec=10.0,
        metadata={"audio_source": "microphone"},
    )
    system_event = CaptionEvent(
        event_type="caption_confirmed",
        text="안녕하세요, 반갑습니다.",
        audio_offset_sec=10.4,
        metadata={"audio_source": "system"},
    )
    assert dedupe.check(mic_event) is None
    duplicate = dedupe.check(system_event)
    assert duplicate is not None
    assert duplicate.matched_source == "microphone"

    with tempfile.TemporaryDirectory() as directory:
        writer = JsonlLogWriter(str(Path(directory) / "logs"))
        writer.append_event(
            CaptionEvent(
                event_type="caption_confirmed",
                text="듀얼 자막",
                audio_offset_sec=65.0,
                metadata={
                    "audio_source": "microphone",
                    "source_label": "방송자",
                },
            )
        )
        assert writer.caption_path.read_text(encoding="utf-8") == (
            "[00:01:05] [방송자] 듀얼 자막\n"
        )

    admin_html = (ROOT / "overlay" / "admin.html").read_text(encoding="utf-8")
    caption_html = (ROOT / "overlay" / "caption.html").read_text(encoding="utf-8")
    assert '<option value="both">' in admin_html
    assert 'id="koSize" type="number" min="6"' in admin_html
    assert 'id="maxLines" type="number" min="1" max="500"' in admin_html
    assert '500,' in caption_html
    assert 'id="dualAudioSettings"' in admin_html
    assert 'id="microphoneLabel"' in admin_html
    assert 'params.get("source")' in caption_html
    assert "source-microphone" in caption_html

    print("dual_audio_mode_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())