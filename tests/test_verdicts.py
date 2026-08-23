from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.verdicts import judge_stability, judge_streaming_benchmark, judge_voice_tuning


def main() -> int:
    assert judge_voice_tuning({"duration_sec": 1})["status"] == "fail"
    assert judge_voice_tuning({"duration_sec": 10, "energy": {"rms_max": 0.08}, "silero": {"available": True, "current_voiced_ratio": 0.2}})["status"] == "pass"
    assert judge_voice_tuning(
        {
            "duration_sec": 10,
            "energy": {"rms_max": 0.08, "current_voiced_ratio": 1.0},
            "silero": {"available": True, "current_voiced_ratio": 0.2},
        }
    )["status"] == "pass"
    assert judge_streaming_benchmark({"duration_sec": 10, "confirmed_jobs": 0})["status"] == "fail"
    assert judge_streaming_benchmark({"duration_sec": 10, "confirmed_jobs": 1, "pending_jobs": 1, "dropped_jobs": 0, "rtf": 0.5})["status"] == "pass"
    assert judge_stability({"samples": 1}, {"counts": {}})["status"] == "warn"
    assert judge_stability({"samples": 2, "rss_slope_mb_per_hour": 1, "gpu_slope_mb_per_hour": 1}, {"counts": {}, "dropped_jobs": 0})["status"] == "pass"
    print("verdicts_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
