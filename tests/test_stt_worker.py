from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.events import CaptionEvent
from core.stt_worker import SttWorker


class FakeEngine:
    name = "fake"

    def on_audio_segment(self, audio, sample_rate: int, audio_offset_sec: float):
        yield CaptionEvent(
            event_type="caption_confirmed",
            text=f"{audio}:{sample_rate}",
            audio_offset_sec=audio_offset_sec,
            engine=self.name,
        )


def main() -> int:
    worker = SttWorker(FakeEngine(), max_queue=1)
    worker.start()
    first = worker.submit_latest("pending", "a", 16000, 0.0)
    result = worker.wait_for(first.job_id, timeout=2.0)
    assert result.job.job_type == "pending"
    assert result.events[0].text == "a:16000"

    queued = SttWorker(FakeEngine(), max_queue=1)
    queued.submit_latest("pending", "old", 16000, 0.0)
    newest = queued.submit_latest("pending", "new", 16000, 0.0)
    assert newest.job_id == 1
    assert queued.dropped_jobs == 1
    queued.start()
    result = queued.wait_for(newest.job_id, timeout=2.0)
    assert result.events[0].text == "new:16000"
    queued.stop()
    worker.stop()
    print("stt_worker_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
