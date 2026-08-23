from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.health_check import run_checks


def main() -> int:
    results = run_checks()
    names = {result.name for result in results}
    assert "python" in names
    assert "config" in names
    assert "files" in names
    print("health_check_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

