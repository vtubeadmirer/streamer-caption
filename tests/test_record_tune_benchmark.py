from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.record_tune_benchmark import run_command


def main() -> int:
    result = run_command([sys.executable, "-c", "print('ok')"])
    assert result["returncode"] == 0
    assert "ok" in result["stdout"]
    assert result["elapsed_sec"] >= 0
    print("record_tune_benchmark_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
