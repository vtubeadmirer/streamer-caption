from __future__ import annotations

import copy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config, save_config


def main() -> int:
    original = copy.deepcopy(load_config())
    save_config(original)
    restored = load_config()
    assert restored["whisper"]["model"] == original["whisper"]["model"]
    print("compare_profiles_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
