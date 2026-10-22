from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


LAUNCHER_VERSION = "1.0.0"
MANIFEST_ASSET = "release-manifest.json"
USER_AGENT = "StreamerCaptionUpdater/1.0"
UPDATE_CHECK_CACHE = "update-check-cache.json"
DEFAULT_CHECK_INTERVAL_HOURS = 24.0


class UpdateError(RuntimeError):
    pass


class NoPublishedRelease(UpdateError):
    """The repository exists but has no published (non-draft) release yet."""


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise UpdateError(f"expected JSON object: {path}")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _version_key(value: str) -> tuple[tuple[int, ...], int, str]:
    cleaned = value.strip().lower().lstrip("v")
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:[-+]([0-9a-z.-]+))?", cleaned)
    if not match:
        return ((0,), 0, cleaned)
    numbers = tuple(int(part) for part in match.group(1).split("."))
    suffix = match.group(2) or ""
    return (numbers, 1 if not suffix else 0, suffix)


def _validate_version(value: str) -> str:
    cleaned = value.strip().lstrip("v")
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,63}", cleaned):
        raise UpdateError(f"invalid version: {value!r}")
    return cleaned


def _request_json(url: str) -> dict[str, Any] | list[Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # A repository with no published release answers 404 on /releases/latest.
            raise NoPublishedRelease(f"no published release at {url}") from exc
        raise UpdateError(f"request failed: {url}: {exc}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise UpdateError(f"request failed: {url}: {exc}") from exc


def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".partial")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"download failed: {url}: {exc}") from exc
    partial.replace(target)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _asset_map(release: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for asset in release.get("assets", []):
        if isinstance(asset, dict) and asset.get("name") and asset.get("browser_download_url"):
            result[str(asset["name"])] = str(asset["browser_download_url"])
    return result


def _resolve_github_release(repository: str, allow_prerelease: bool) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise UpdateError("updater.repository must be OWNER/REPOSITORY")
    if allow_prerelease:
        releases = _request_json(f"https://api.github.com/repos/{repository}/releases?per_page=20")
        if not isinstance(releases, list):
            raise UpdateError("unexpected GitHub releases response")
        release = next(
            (item for item in releases if isinstance(item, dict) and not item.get("draft")),
            None,
        )
        if release is None:
            raise NoPublishedRelease("no published release was found")
    else:
        release = _request_json(f"https://api.github.com/repos/{repository}/releases/latest")
        if not isinstance(release, dict):
            raise UpdateError("unexpected GitHub release response")

    assets = _asset_map(release)
    manifest_url = assets.get(MANIFEST_ASSET)
    if not manifest_url:
        raise UpdateError(f"release asset is missing: {MANIFEST_ASSET}")
    manifest = _request_json(manifest_url)
    if not isinstance(manifest, dict):
        raise UpdateError("release manifest must be a JSON object")
    asset_name = str(manifest.get("asset_name", ""))
    asset_url = assets.get(asset_name)
    if not asset_name or not asset_url:
        raise UpdateError(f"release asset is missing: {asset_name or '(empty)'}")
    return {"release": release, "manifest": manifest, "asset_url": asset_url}


def load_state(install_root: Path) -> dict[str, Any]:
    path = install_root / "state.json"
    if not path.exists():
        return {"current_version": None, "previous_version": None}
    return _read_json(path)


def load_updater_config(install_root: Path) -> dict[str, Any]:
    path = install_root / "updater.json"
    if not path.exists():
        return {}
    return _read_json(path)


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _check_interval_hours(config: dict[str, Any]) -> float:
    try:
        interval = float(config.get("check_interval_hours", DEFAULT_CHECK_INTERVAL_HOURS))
    except (TypeError, ValueError):
        interval = DEFAULT_CHECK_INTERVAL_HOURS
    return min(max(interval, 0.0), 24.0 * 30.0)


def _cached_update_status(
    install_root: Path,
    *,
    repository: str,
    allow_prerelease: bool,
    current_version: str,
    interval_hours: float,
    now: datetime,
) -> dict[str, Any] | None:
    if interval_hours <= 0:
        return None
    path = install_root / UPDATE_CHECK_CACHE
    if not path.exists():
        return None
    try:
        cache = _read_json(path)
        checked = datetime.fromisoformat(str(cache.get("checked_at", "")).replace("Z", "+00:00"))
    except (OSError, ValueError, json.JSONDecodeError, UpdateError):
        return None
    if (
        cache.get("repository") != repository
        or bool(cache.get("allow_prerelease")) != allow_prerelease
        or str(cache.get("current_version")) != current_version
    ):
        return None
    age = now - _utc(checked)
    if age < timedelta(0) or age >= timedelta(hours=interval_hours):
        return None
    status = cache.get("status")
    if not isinstance(status, dict):
        return None
    result = dict(status)
    result["cached"] = True
    result["checked_at"] = cache["checked_at"]
    return result


def _write_update_cache(
    install_root: Path,
    *,
    repository: str,
    allow_prerelease: bool,
    current_version: str,
    checked_at: datetime,
    status: dict[str, Any],
) -> None:
    _atomic_json(
        install_root / UPDATE_CHECK_CACHE,
        {
            "schema_version": 1,
            "checked_at": checked_at.isoformat(),
            "repository": repository,
            "allow_prerelease": allow_prerelease,
            "current_version": current_version,
            "status": status,
        },
    )


def check_for_update(
    install_root: Path,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    state = load_state(install_root)
    config = load_updater_config(install_root)
    current = str(state.get("current_version") or "0.0.0")
    repository = str(config.get("repository") or "").strip()
    if not repository or repository.startswith("GITHUB_USERNAME/"):
        return {
            "configured": False,
            "available": False,
            "current_version": current,
            "reason": "repository_not_configured",
        }
    allow_prerelease = bool(config.get("allow_prerelease", False))
    checked_at = _utc(now)
    if not force:
        cached = _cached_update_status(
            install_root,
            repository=repository,
            allow_prerelease=allow_prerelease,
            current_version=current,
            interval_hours=_check_interval_hours(config),
            now=checked_at,
        )
        if cached is not None:
            return cached

    try:
        resolved = _resolve_github_release(repository, allow_prerelease)
    except NoPublishedRelease:
        # Nothing to offer yet; this is a normal state for a fresh project, not an error.
        return {
            "configured": True,
            "available": False,
            "current_version": current,
            "reason": "no_published_release",
            "cached": False,
            "checked_at": checked_at.isoformat(),
        }
    manifest = resolved["manifest"]
    latest = _validate_version(str(manifest.get("version", "")))
    skipped = str(config.get("skipped_version") or "").lstrip("v")
    available = _version_key(latest) > _version_key(current) and latest != skipped
    result = {
        "configured": True,
        "available": available,
        "current_version": current,
        "latest_version": latest,
        "mandatory": bool(manifest.get("mandatory", False)),
        "release_notes": str(manifest.get("release_notes", "")),
        "manifest": manifest,
        "asset_url": resolved["asset_url"],
        "cached": False,
        "checked_at": checked_at.isoformat(),
    }
    try:
        _write_update_cache(
            install_root,
            repository=repository,
            allow_prerelease=allow_prerelease,
            current_version=current,
            checked_at=checked_at,
            status=result,
        )
    except OSError:
        pass
    return result


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    base = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            mode = (item.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise UpdateError(f"symbolic links are not allowed: {item.filename}")
            target = (destination / item.filename).resolve()
            try:
                target.relative_to(base)
            except ValueError as exc:
                raise UpdateError(f"unsafe archive path: {item.filename}") from exc
        bundle.extractall(destination)


def _validate_manifest(manifest: dict[str, Any]) -> tuple[str, str]:
    version = _validate_version(str(manifest.get("version", "")))
    expected = str(manifest.get("sha256", "")).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise UpdateError("manifest.sha256 must be a 64-character lowercase SHA-256")
    minimum = str(manifest.get("minimum_launcher_version", "0.0.0"))
    if _version_key(minimum) > _version_key(LAUNCHER_VERSION):
        raise UpdateError(
            f"launcher {minimum} or newer is required; installed launcher is {LAUNCHER_VERSION}"
        )
    return version, expected


def install_archive(install_root: Path, manifest: dict[str, Any], archive: Path) -> dict[str, Any]:
    install_root = install_root.resolve()
    version, expected = _validate_manifest(manifest)
    actual = _sha256(archive)
    if actual != expected:
        raise UpdateError(f"SHA-256 mismatch: expected {expected}, got {actual}")

    versions = install_root / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    target = versions / version
    if target.exists():
        if not (target / "VERSION").exists():
            raise UpdateError(f"existing version directory is incomplete: {target}")
    else:
        staging = versions / f".{version}.staging-{uuid.uuid4().hex}"
        try:
            _safe_extract(archive, staging)
            payload = staging
            children = [item for item in staging.iterdir()]
            if len(children) == 1 and children[0].is_dir():
                payload = children[0]
            version_file = payload / "VERSION"
            if not version_file.exists():
                raise UpdateError("release archive does not contain VERSION")
            packaged = _validate_version(version_file.read_text(encoding="utf-8-sig").strip())
            if packaged != version:
                raise UpdateError(f"archive version {packaged} does not match manifest {version}")
            if payload == staging:
                staging.replace(target)
            else:
                payload.replace(target)
                shutil.rmtree(staging, ignore_errors=True)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    state = load_state(install_root)
    current = state.get("current_version")
    if current != version:
        state["previous_version"] = current
        state["current_version"] = version
    state["last_update_sha256"] = actual
    state["last_update_status"] = "installed"
    _atomic_json(install_root / "state.json", state)
    return {"installed": version, "previous_version": state.get("previous_version"), "path": str(target)}


RUNTIME_MARKER = "RUNTIME_VERSION"


def current_runtime_version(install_root: Path) -> str:
    marker = install_root / "runtime" / ".venv" / RUNTIME_MARKER
    try:
        return marker.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def runtime_info_from_manifest(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """The portable runtime a release expects, or None when the manifest predates runtime fields."""
    version = str(manifest.get("runtime_version") or "").strip()
    url = str(manifest.get("runtime_url") or "").strip()
    if not version or not url:
        return None
    return {
        "runtime_version": version,
        "asset_name": str(manifest.get("runtime_asset_name") or f"runtime-{version}.zip"),
        "sha256": str(manifest.get("runtime_sha256") or "").lower(),
        "size": int(manifest.get("runtime_size") or 0),
        "url": url,
    }


def install_runtime_archive(install_root: Path, archive: Path, info: dict[str, Any]) -> dict[str, Any]:
    """Verify and swap in a runtime ZIP (.venv/ layout). The old runtime is replaced only after a full extract."""
    expected = info.get("sha256") or ""
    if expected:
        actual = _sha256(archive)
        if actual != expected:
            raise UpdateError(f"runtime SHA-256 mismatch: expected {expected}, got {actual}")
    runtime_root = install_root / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    staging = runtime_root / f".staging-{uuid.uuid4().hex}"
    try:
        _safe_extract(archive, staging)
        extracted = staging / ".venv"
        if not (extracted / "Scripts" / "python.exe").exists():
            raise UpdateError("runtime archive has no .venv/Scripts/python.exe")
        marker = (extracted / RUNTIME_MARKER).read_text(encoding="utf-8-sig").strip() if (extracted / RUNTIME_MARKER).exists() else ""
        if marker and marker != info["runtime_version"]:
            raise UpdateError(f"runtime archive is {marker}, expected {info['runtime_version']}")
        target = runtime_root / ".venv"
        if target.exists():
            old = runtime_root / f".old-{uuid.uuid4().hex}"
            target.rename(old)
            shutil.rmtree(old, ignore_errors=True)
        extracted.rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _atomic_json(install_root / "runtime.json", info)
    return {"runtime_version": info["runtime_version"], "runtime_installed": True}


def ensure_runtime(install_root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Download the runtime a release needs when the installed one differs. No-op for venv installs without runtime fields."""
    info = runtime_info_from_manifest(manifest)
    if info is None:
        return {"runtime_version": current_runtime_version(install_root), "runtime_installed": False}
    if current_runtime_version(install_root) == info["runtime_version"]:
        return {"runtime_version": info["runtime_version"], "runtime_installed": False}
    archive = install_root / "downloads" / info["asset_name"]
    if not archive.exists():
        _download(info["url"], archive)
    try:
        return install_runtime_archive(install_root, archive, info)
    finally:
        archive.unlink(missing_ok=True)


def update_from_github(install_root: Path) -> dict[str, Any]:
    status = check_for_update(install_root, force=True)
    if not status.get("available"):
        return status
    manifest = status["manifest"]
    version = _validate_version(str(manifest["version"]))
    archive = install_root / "downloads" / f"{version}.zip"
    _download(str(status["asset_url"]), archive)
    # Runtime first: a new app version must never be activated on a runtime it cannot run on.
    runtime = ensure_runtime(install_root, manifest)
    result = install_archive(install_root, manifest, archive)
    result["downloaded"] = str(archive)
    result.update(runtime)
    return result


def rollback(install_root: Path) -> dict[str, Any]:
    state = load_state(install_root)
    current = state.get("current_version")
    previous = state.get("previous_version")
    if not previous:
        raise UpdateError("no previous version is available")
    previous_path = install_root / "versions" / str(previous)
    if not previous_path.exists():
        raise UpdateError(f"previous version directory is missing: {previous_path}")
    state["current_version"] = previous
    state["previous_version"] = current
    state["last_update_status"] = "rolled_back"
    _atomic_json(install_root / "state.json", state)
    return {"current_version": previous, "previous_version": current}


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Streamer Caption side-by-side updater")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--install-root", required=True, type=Path)
    check.add_argument("--force", action="store_true")
    for command in ("update", "rollback"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--install-root", required=True, type=Path)
    local = subparsers.add_parser("install-local")
    local.add_argument("--install-root", required=True, type=Path)
    local.add_argument("--manifest", required=True, type=Path)
    local.add_argument("--asset", required=True, type=Path)
    args = parser.parse_args()

    try:
        if args.command == "check":
            result = check_for_update(args.install_root, force=args.force)
        elif args.command == "update":
            result = update_from_github(args.install_root)
        elif args.command == "rollback":
            result = rollback(args.install_root)
        else:
            result = install_archive(args.install_root, _read_json(args.manifest), args.asset)
    except UpdateError as exc:
        _print({"ok": False, "error": str(exc)})
        return 1
    _print({"ok": True, **result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
