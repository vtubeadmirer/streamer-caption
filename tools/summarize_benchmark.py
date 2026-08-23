from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT


def load_rows(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("benchmark JSON must contain a list")
    return [row for row in data if isinstance(row, dict)]


def numeric_values(rows: list[dict[str, Any]], key: str, include_discarded: bool) -> list[float]:
    values: list[float] = []
    for row in rows:
        if row.get("discarded") and not include_discarded:
            continue
        value = row.get(key)
        if value is None:
            continue
        values.append(float(value))
    return values


def summarize_values(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"count": 0, "mean": None, "stdev": None, "min": None, "max": None}
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "max": max(values),
    }


def summarize(path: Path, include_discarded: bool = False) -> dict[str, Any]:
    rows = load_rows(path)
    active_rows = [row for row in rows if include_discarded or not row.get("discarded")]
    hidden_count = sum(
        1
        for row in active_rows
        if row.get("hidden") or ("caption_hidden" in row.get("event_types", []))
    )
    total = len(active_rows)
    return {
        "path": str(path),
        "total_rows": len(rows),
        "used_rows": total,
        "model": _first(active_rows, "model"),
        "device": _first(active_rows, "device"),
        "compute_type": _first(active_rows, "compute_type"),
        "warm": _first(active_rows, "warm"),
        "elapsed_sec": summarize_values(numeric_values(rows, "elapsed_sec", include_discarded)),
        "rtf": summarize_values(numeric_values(rows, "rtf", include_discarded)),
        "cer": summarize_values(numeric_values(rows, "cer", include_discarded)),
        "hidden_ratio": hidden_count / total if total else None,
        "texts": [row.get("text") or f"[hidden:{row.get('hidden_text', '')}]" for row in active_rows],
    }


def _first(rows: list[dict[str, Any]], key: str) -> Any:
    for row in rows:
        if key in row:
            return row[key]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize benchmark JSON output.")
    parser.add_argument("path", nargs="?")
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--include-discarded", action="store_true")
    args = parser.parse_args()

    if args.latest:
        candidates = sorted((PROJECT_ROOT / "reports").glob("benchmark_*.json"))
        if not candidates:
            print("No benchmark reports found.")
            return 1
        path = candidates[-1]
    elif args.path:
        path = (PROJECT_ROOT / args.path).resolve()
    else:
        raise SystemExit("Provide a path or --latest")

    summary = summarize(path, args.include_discarded)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
