from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.progress_report import build_report


def main() -> int:
    report = build_report()
    assert 0 < report["percent_complete"] <= 100
    assert report["next_five"]
    print("progress_report_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
