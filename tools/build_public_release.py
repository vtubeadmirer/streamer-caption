from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "runtime",
    "user-data",
    "downloads",
    "dist",
    "logs",
    "reports",
    "samples",
    "__pycache__",
    ".pytest_cache",
    "simulstreaming-runtime",
}
EXCLUDED_FILES = {
    "config.json",
    "data/slang_dict.json",
    "data/whitelist.txt",
    "data/blocklist.txt",
    "data/transcripts.sqlite3",
    "state.json",
    "updater.json",
    "snapshot.json",
    "SHA256SUMS.txt",
    "release-manifest.json",
}
EXCLUDED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".wav",
    ".mp3",
    ".flac",
    ".sqlite3",
    ".partial",
    ".zip",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _version(source: Path) -> str:
    value = (source / "VERSION").read_text(encoding="utf-8-sig").strip()
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,63}", value):
        raise ValueError(f"invalid VERSION: {value!r}")
    return value


def should_include(source: Path, path: Path) -> bool:
    relative = path.relative_to(source)
    name = relative.as_posix()
    if path.is_symlink():
        return False
    if any(part in EXCLUDED_DIRS for part in relative.parts):
        return False
    if name in EXCLUDED_FILES:
        return False
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    return path.is_file()


def _write_deterministic(zf: zipfile.ZipFile, source: Path, relative: Path) -> None:
    info = zipfile.ZipInfo(relative.as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    zf.writestr(info, source.read_bytes())


def build_public_release(source: Path = ROOT, dist: Path | None = None) -> dict[str, Any]:
    source = source.resolve()
    dist = (dist or source / "dist").resolve()
    dist.mkdir(parents=True, exist_ok=True)
    version = _version(source)
    asset_name = f"streamer-caption-{version}.zip"
    asset = dist / asset_name

    included: list[str] = []
    with zipfile.ZipFile(asset, "w", allowZip64=True) as bundle:
        for path in sorted(source.rglob("*")):
            if should_include(source, path):
                relative = path.relative_to(source)
                _write_deterministic(bundle, path, relative)
                included.append(relative.as_posix())

    required = {"VERSION", "INSTALL.bat", "launcher/updater.py", "tools/install.ps1"}
    missing = sorted(required.difference(included))
    if missing:
        asset.unlink(missing_ok=True)
        raise RuntimeError("public release is missing required files: " + ", ".join(missing))

    template_path = source / "release-manifest.json"
    template = json.loads(template_path.read_text(encoding="utf-8-sig"))
    manifest = {
        **template,
        "version": version,
        "asset_name": asset_name,
        "sha256": _sha256(asset),
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = dist / "release-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "version": version,
        "asset": str(asset),
        "manifest": str(manifest_path),
        "sha256": manifest["sha256"],
        "files": len(included),
        "bytes": asset.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a privacy-safe GitHub Release payload.")
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--dist", type=Path)
    args = parser.parse_args()
    try:
        result = build_public_release(args.source, args.dist)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
