from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.verdicts import judge_stability


def main() -> int:
    result = judge_stability(
        {"samples": 3, "rss_slope_mb_per_hour": 2.0, "gpu_slope_mb_per_hour": 1.0},
        {"counts": {"caption_confirmed": 1}, "dropped_jobs": 0},
    )
    assert result["status"] == "pass"
    print("stability_verdict_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
