from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.final_check import run


def main() -> int:
    result = run([sys.executable, "-c", "print('ok')"])
    assert result["returncode"] == 0
    assert "ok" in result["stdout"]
    print("final_check_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
