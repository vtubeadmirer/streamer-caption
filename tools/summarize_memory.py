from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config, user_path


def latest_memory_csv() -> Path | None:
    log_dir = user_path(load_config()["log"]["dir"])
    paths = sorted(log_dir.glob("*/memory.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def read_rows(path: Path) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            parsed: dict[str, float] = {}
            for key, value in row.items():
                try:
                    parsed[key] = float(value)
                except (TypeError, ValueError):
                    continue
            if "elapsed_sec" in parsed:
                rows.append(parsed)
    return rows


def slope_per_hour(rows: list[dict[str, float]], key: str) -> float | None:
    values = [(row["elapsed_sec"], row[key]) for row in rows if key in row]
    if len(values) < 2:
        return None
    start_time, start_value = values[0]
    end_time, end_value = values[-1]
    elapsed_hours = (end_time - start_time) / 3600.0
    if elapsed_hours <= 0:
        return None
    return (end_value - start_value) / elapsed_hours


def summarize(path: Path) -> dict[str, Any]:
    rows = read_rows(path)
    if not rows:
        return {"path": str(path), "samples": 0}
    first = rows[0]
    last = rows[-1]
    return {
        "path": str(path),
        "samples": len(rows),
        "elapsed_sec": last["elapsed_sec"] - first["elapsed_sec"],
        "rss_start_mb": first.get("rss_mb"),
        "rss_end_mb": last.get("rss_mb"),
        "rss_slope_mb_per_hour": slope_per_hour(rows, "rss_mb"),
        "gpu_start_mb": first.get("gpu_used_mb"),
        "gpu_end_mb": last.get("gpu_used_mb"),
        "gpu_slope_mb_per_hour": slope_per_hour(rows, "gpu_used_mb"),
        "gpu_total_mb": last.get("gpu_total_mb"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize memory.csv from streamer-caption-v2 logs.")
    parser.add_argument("path", nargs="?", help="memory.csv path")
    parser.add_argument("--latest", action="store_true", help="use the newest logs/*/memory.csv")
    args = parser.parse_args()

    path = Path(args.path) if args.path else latest_memory_csv()
    if path is None:
        print("No memory.csv found. Run the app or tests first.")
        return 1
    summary = summarize(path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
