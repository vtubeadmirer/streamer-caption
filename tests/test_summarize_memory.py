from __future__ import annotations

import csv
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.summarize_memory import summarize


def main() -> int:
    path = ROOT / "reports" / "_test_memory.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["elapsed_sec", "rss_mb", "gpu_used_mb", "gpu_total_mb"])
        writer.writeheader()
        writer.writerow({"elapsed_sec": 0, "rss_mb": 100, "gpu_used_mb": 1000, "gpu_total_mb": 16000})
        writer.writerow({"elapsed_sec": 1800, "rss_mb": 125, "gpu_used_mb": 1100, "gpu_total_mb": 16000})
    summary = summarize(path)
    assert summary["samples"] == 2
    assert summary["rss_slope_mb_per_hour"] == 50.0
    assert summary["gpu_slope_mb_per_hour"] == 200.0
    print("summarize_memory_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
