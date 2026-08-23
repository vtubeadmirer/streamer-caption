from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.collect_diagnostics import collect


def main() -> int:
    report = collect(include_model_load=False)
    assert "health" in report
    assert "config" in report
    assert report["config"]["whisper"]["model"]
    assert all("status" in item for item in report["health"])
    print("collect_diagnostics_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
