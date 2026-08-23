from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.metrics import character_error_rate, normalize_for_cer


def main() -> int:
    assert normalize_for_cer("안녕 하세요") == "안녕하세요"
    assert character_error_rate("안녕하세요", "안녕하세요") == 0.0
    assert character_error_rate("안녕하세요", "안녕하세여") == 0.2
    assert character_error_rate("", "") == 0.0
    assert character_error_rate("", "abc") == 1.0
    print("metrics_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

