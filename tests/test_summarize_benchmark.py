from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.summarize_benchmark import summarize


def main() -> int:
    path = ROOT / "reports" / "_test_benchmark.json"
    rows = [
        {"elapsed_sec": 1.0, "rtf": 0.5, "cer": 0.1, "text": "a", "discarded": True},
        {"elapsed_sec": 0.5, "rtf": 0.25, "cer": 0.0, "text": "a", "model": "tiny"},
        {"elapsed_sec": 0.7, "rtf": 0.35, "cer": 0.2, "text": "", "hidden_text": "", "hidden": True, "model": "tiny"},
    ]
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    summary = summarize(path)
    assert summary["used_rows"] == 2
    assert abs(summary["rtf"]["mean"] - 0.3) < 0.0001
    assert summary["hidden_ratio"] == 0.5
    print("summarize_benchmark_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
