from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.backup_config import backup_config
from tools.make_handoff_bundle import make_bundle


def main() -> int:
    backup = backup_config(ROOT / "reports" / "_test_config_backups")
    assert backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8"))["whisper"]["model"]
    bundle = make_bundle()
    assert bundle.exists()
    assert bundle.suffix == ".zip"
    print("backup_and_bundle_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
