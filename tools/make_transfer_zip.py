from __future__ import annotations

import time
import zipfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT


SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "simulstreaming-runtime",
    "__pycache__",
    ".pytest_cache",
}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def should_include(path: Path) -> bool:
    relative = path.relative_to(PROJECT_ROOT)
    parts = set(relative.parts)
    if parts & SKIP_DIRS:
        return False
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    if relative.parts and relative.parts[0] == "logs":
        return path.name == ".gitkeep"
    if relative.parts and relative.parts[0] == "reports":
        return path.name == ".gitkeep" or path.name.startswith("progress_") or path.name.startswith("final_check_")
    if relative.parts[:2] == ("samples", "incoming"):
        return path.name == ".gitkeep"
    return True


def make_transfer_zip() -> Path:
    out_dir = PROJECT_ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    out = out_dir / f"streamer_caption_v2_transfer_4060ti_{stamp}.zip"
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(PROJECT_ROOT.rglob("*")):
            if path.is_file() and should_include(path):
                zf.write(path, path.relative_to(PROJECT_ROOT))
    return out


def main() -> int:
    try:
        out = make_transfer_zip()
    except Exception as exc:
        print(exc)
        return 1
    print(f"transfer_zip: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
