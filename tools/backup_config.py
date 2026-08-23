from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import CONFIG_PATH, PROJECT_ROOT, load_config


def backup_config(out_dir: Path | None = None) -> Path:
    out_dir = out_dir or PROJECT_ROOT / "reports" / "config_backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"config_{time.strftime('%Y-%m-%d_%H%M%S')}.json"
    config = load_config()
    out.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def restore_config(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"config backup not found: {path}")
    # Validate JSON before replacing current config.
    json.loads(path.read_text(encoding="utf-8"))
    shutil.copyfile(path, CONFIG_PATH)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup or restore config.json.")
    parser.add_argument("--restore", help="backup JSON path to restore")
    args = parser.parse_args()
    try:
        if args.restore:
            restore_config(Path(args.restore))
            print(f"restored config: {args.restore}")
        else:
            out = backup_config()
            print(f"config_backup: {out}")
    except Exception as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
