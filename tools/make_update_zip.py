from __future__ import annotations

import time
import zipfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT
from tools.make_transfer_zip import SKIP_DIRS, SKIP_SUFFIXES


PRESERVE_ON_UPDATE = {
    "config.json",
    "data/slang_dict.json",
    "data/whitelist.txt",
    "data/blocklist.txt",
}


def should_include_update(path: Path) -> bool:
    relative = path.relative_to(PROJECT_ROOT)
    name = relative.as_posix()
    parts = set(relative.parts)
    if parts & SKIP_DIRS:
        return False
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    if name in PRESERVE_ON_UPDATE:
        return False
    if name.startswith("data/transcripts.sqlite3"):
        return False
    if relative.parts and relative.parts[0] in {"logs", "reports", "samples"}:
        return path.name == ".gitkeep"
    return True


def make_update_zip() -> Path:
    out_dir = PROJECT_ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"streamer_caption_v2_update_keep_settings_{time.strftime('%Y-%m-%d_%H%M%S')}.zip"
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(PROJECT_ROOT.rglob("*")):
            if path.is_file() and should_include_update(path):
                zf.write(path, path.relative_to(PROJECT_ROOT))
    return out


def main() -> int:
    try:
        out = make_update_zip()
    except Exception as exc:
        print(exc)
        return 1
    print(f"update_zip: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
