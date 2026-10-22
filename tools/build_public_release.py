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
    "update-check-cache.json",
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

    required = {"VERSION", "INSTALL.bat", "launcher/updater.py", "tools/install.ps1", "launcher/bootstrap.ps1", "launcher/StreamerCaption.bat", "launcher/launcher.ps1"}
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
    manifest.update(_runtime_fields(template, dist, _repository(source)))
    manifest_path = dist / "release-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    portable = build_portable_bundle(source, dist, version, included, manifest)
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
        "portable": str(portable),
        "portable_sha256": manifest.get("portable_sha256", ""),
    }


def _repository(source: Path) -> str:
    try:
        example = json.loads((source / "updater.example.json").read_text(encoding="utf-8-sig"))
        return str(example.get("repository", ""))
    except (OSError, ValueError):
        return ""


def _runtime_fields(template: dict[str, Any], dist: Path, repository: str) -> dict[str, Any]:
    """Describe the portable runtime: from dist/runtime-manifest.json when a runtime was built, else the template."""
    runtime_version = str(template.get("runtime_version") or "py312-v1")
    asset_name = str(template.get("runtime_asset_name") or f"runtime-{runtime_version}.zip")
    sha256 = str(template.get("runtime_sha256") or "")
    size = int(template.get("runtime_size") or 0)
    runtime_manifest = dist / "runtime-manifest.json"
    if runtime_manifest.exists():
        built = json.loads(runtime_manifest.read_text(encoding="utf-8-sig"))
        runtime_version = str(built.get("runtime_version", runtime_version))
        asset_name = str(built.get("asset_name", asset_name))
        sha256 = str(built.get("sha256", sha256))
        size = int(built.get("size", size))
    tag = str(template.get("runtime_release_tag") or f"runtime-{runtime_version}")
    url = f"https://github.com/{repository}/releases/download/{tag}/{asset_name}" if repository else ""
    return {
        "runtime_version": runtime_version,
        "runtime_asset_name": asset_name,
        "runtime_sha256": sha256,
        "runtime_size": size,
        "runtime_release_tag": tag,
        "runtime_url": url,
    }


PORTABLE_ROOT = "StreamerCaption"

PORTABLE_README = (
    "Streamer Caption - no-install version\n"
    "\n"
    "1. Put this folder anywhere (avoid synced folders such as OneDrive).\n"
    "2. Double-click StreamerCaption.bat.\n"
    "   - The first run downloads the runtime (about 1 GB) and the speech model once; a progress bar is shown.\n"
    "   - Then the browser opens the first-time setup page: pick the microphone and copy the OBS URLs.\n"
    "3. From then on one double-click starts the captions.\n"
    "\n"
    "Offline PC: download runtime-*.zip, place it in this folder, and run; it is used instead of downloading.\n"
    "Problems: open http://127.0.0.1:8766/admin and use the bug report button.\n"
)


def build_portable_bundle(source: Path, dist: Path, version: str, included: list[str], manifest: dict[str, Any]) -> Path:
    """No-install bundle: unzip anywhere, double-click StreamerCaption.bat. The runtime is fetched on first run."""
    portable = dist / f"StreamerCaption-{version}-portable.zip"
    runtime_info = {
        "runtime_version": manifest["runtime_version"],
        "asset_name": manifest["runtime_asset_name"],
        "sha256": manifest["runtime_sha256"],
        "size": manifest["runtime_size"],
        "url": manifest["runtime_url"],
    }
    state = {"current_version": version, "previous_version": None, "installed_at": None, "install_root": None, "portable": True}
    updater = json.loads((source / "updater.example.json").read_text(encoding="utf-8-sig"))
    if "-" in version:
        # A dev/pre-release build should keep receiving dev builds; GitHub's /releases/latest hides prereleases.
        updater["allow_prerelease"] = True
        updater["channel"] = "dev"

    def write_text(relative: str, payload: str) -> None:
        info = zipfile.ZipInfo((Path(PORTABLE_ROOT) / relative).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        bundle.writestr(info, payload.encode("utf-8"))

    with zipfile.ZipFile(portable, "w", allowZip64=True) as bundle:
        for relative in included:
            _write_deterministic(bundle, source / relative, Path(PORTABLE_ROOT) / "versions" / version / relative)
        for name in ("bootstrap.ps1", "launcher.ps1", "updater.py"):
            _write_deterministic(bundle, source / "launcher" / name, Path(PORTABLE_ROOT) / "launcher" / name)
        _write_deterministic(bundle, source / "launcher" / "StreamerCaption.bat", Path(PORTABLE_ROOT) / "StreamerCaption.bat")
        write_text("runtime.json", json.dumps(runtime_info, ensure_ascii=False, indent=2) + "\n")
        write_text("state.json", json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        write_text("updater.json", json.dumps(updater, ensure_ascii=False, indent=2) + "\n")
        write_text("README-FIRST.txt", PORTABLE_README)
    manifest["portable_asset_name"] = portable.name
    manifest["portable_sha256"] = _sha256(portable)
    return portable


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
