from __future__ import annotations

import argparse
import time
import zipfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import USER_DATA_ROOT


USER_SETTING_FILES = [
    "config.json",
    "data/slang_dict.json",
    "data/whitelist.txt",
    "data/blocklist.txt",
]


def export_settings() -> Path:
    out_dir = USER_DATA_ROOT / "reports" / "user_settings"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"user_settings_{time.strftime('%Y-%m-%d_%H%M%S')}.zip"
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in USER_SETTING_FILES:
            path = USER_DATA_ROOT / name
            if path.exists():
                zf.write(path, name)
    return out


def restore_settings(bundle: Path) -> None:
    if not bundle.exists():
        raise FileNotFoundError(f"user settings bundle not found: {bundle}")
    with zipfile.ZipFile(bundle, "r") as zf:
        for member in zf.namelist():
            normalized = member.replace("\\", "/")
            if normalized not in USER_SETTING_FILES:
                continue
            out = USER_DATA_ROOT / normalized
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(zf.read(member))


def main() -> int:
    parser = argparse.ArgumentParser(description="Export or restore user settings.")
    parser.add_argument("--restore", help="user_settings_*.zip path to restore")
    args = parser.parse_args()
    try:
        if args.restore:
            restore_settings(Path(args.restore))
            print(f"user_settings_restored: {args.restore}")
        else:
            out = export_settings()
            print(f"user_settings_export: {out}")
    except Exception as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
