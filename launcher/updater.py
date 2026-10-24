from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


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


PrereleaseKey = tuple[tuple[int, Any], ...]


def _prerelease_key(suffix: str) -> PrereleaseKey:
    """Dot-separated parts; numeric parts compare as numbers (dev.9 < dev.10) and sort before
    words (alpha.1 < alpha.beta); a shorter prefix sorts first (rc < rc.1), as in SemVer."""
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in suffix.split("."))


def _version_key(value: str) -> tuple[tuple[int, ...], int, PrereleaseKey]:
    cleaned = value.strip().lower().lstrip("v")
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:[-+]([0-9a-z.-]+))?", cleaned)
    if not match:
        return ((0,), 0, ((1, cleaned),))
    numbers = tuple(int(part) for part in match.group(1).split("."))
    suffix = match.group(2) or ""
    return (numbers, 1 if not suffix else 0, _prerelease_key(suffix) if suffix else ())


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


ProgressHook = Callable[[str, int, int], None]  # (label, done_bytes, total_bytes or 0)


def _download(url: str, target: Path, progress: ProgressHook | None = None, label: str = "") -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".partial")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            total = 0
            try:
                total = int(response.headers.get("Content-Length") or 0)
            except ValueError:
                total = 0
            done = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
                done += len(block)
                if progress is not None:
                    progress(label or target.name, done, total)
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
        # Runtime releases (runtime-*) share the repository; only an app release carries the manifest.
        release = next(
            (
                item
                for item in releases
                if isinstance(item, dict)
                and not item.get("draft")
                and not str(item.get("tag_name", "")).startswith("runtime-")
                and MANIFEST_ASSET in _asset_map(item)
            ),
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


def _fresh_dir(parent: Path, prefix: str) -> Path:
    """<parent>/<prefix><8 hex>. Short on purpose: without long-path support Windows stops at
    260 characters, and the staging folder name is part of every extracted path."""
    for _ in range(16):
        candidate = parent / f"{prefix}{uuid.uuid4().hex[:8]}"
        if not candidate.exists():
            return candidate
    raise UpdateError(f"no free folder name for {prefix}* in {parent}")


def install_archive(
    install_root: Path,
    manifest: dict[str, Any],
    archive: Path,
    *,
    running_version: str | None = None,
) -> dict[str, Any]:
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
        staging = _fresh_dir(versions, ".staging-")
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
    synced = sync_launcher(install_root, target)
    try:
        pruned = prune_install(install_root, keep_archive=archive, running_version=running_version)
    except Exception as exc:  # housekeeping must never fail an install that already succeeded
        pruned = {"versions": [], "downloads": [], "staging": [], "errors": [f"{type(exc).__name__}: {exc}"]}
    return {
        "installed": version,
        "previous_version": state.get("previous_version"),
        "path": str(target),
        "launcher_synced": synced,
        "pruned": pruned,
    }


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


LAUNCHER_FILES = ("bootstrap.ps1", "launcher.ps1", "updater.py")
# The portable one-button entry lives at the install ROOT (<root>/StreamerCaption.bat calls
# launcher\bootstrap.ps1 next to it). It is refreshed only where it already exists: INSTALL.bat
# installs start from START_STREAMER_CAPTION.bat and never had one.
ROOT_LAUNCHER_FILES = ("StreamerCaption.bat",)


def _replace_file(source: Path, target: Path) -> bool:
    if target.exists() and target.read_bytes() == source.read_bytes():
        return False
    temporary = target.with_suffix(target.suffix + ".new")
    shutil.copyfile(source, temporary)
    os.replace(temporary, target)
    return True


def sync_launcher(install_root: Path, version_root: Path) -> list[str]:
    """Copy the freshly installed version's launcher files over <root>/launcher (and the root
    StreamerCaption.bat) so fixes to the bootstrap/launcher/updater reach existing installs.
    PowerShell reads a script fully before running it, so replacing the file under a running
    launcher is safe; it takes effect next start."""
    source_dir = version_root / "launcher"
    target_dir = install_root / "launcher"
    if not source_dir.is_dir() or not target_dir.is_dir():
        return []
    synced: list[str] = []
    for name in LAUNCHER_FILES:
        source = source_dir / name
        if source.is_file() and _replace_file(source, target_dir / name):
            synced.append(name)
    for name in ROOT_LAUNCHER_FILES:
        source = source_dir / name
        if source.is_file() and (install_root / name).is_file() and _replace_file(source, install_root / name):
            synced.append(name)
        # 1.1.3 copied it into launcher/, where its %~dp0launcher\ paths point nowhere. An install
        # root never holds main.py; a source checkout (where launcher/ is the original) does.
        stray = target_dir / name
        if stray.is_file() and not (install_root / "main.py").exists():
            try:
                stray.unlink()
            except OSError:
                pass
    return synced


DEFAULT_KEEP_VERSIONS = 2
STALE_FOLDER_SEC = 60 * 60  # staging folders younger than this may belong to an install in progress
_VERSION_DIR = re.compile(r"\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?")


def _keep_versions(config: dict[str, Any]) -> int:
    try:
        value = int(config.get("keep_versions") or DEFAULT_KEEP_VERSIONS)
    except (TypeError, ValueError):
        return DEFAULT_KEEP_VERSIONS
    return min(max(value, 1), 100)


def _running_version(install_root: Path) -> str | None:
    """The version whose copy of this module is running (<root>/versions/<v>/launcher/updater.py):
    that is the app installing the update, so its folder is in use. None for the root launcher."""
    try:
        version_dir = Path(__file__).resolve().parent.parent
        if version_dir.parent.name == "versions" and version_dir.parent.parent == install_root.resolve():
            return version_dir.name
    except (OSError, NameError):
        pass
    return None


def _is_link(path: Path) -> bool:
    """Symlink or NTFS junction. The launcher links versions/<v>/.venv to the shared runtime and
    logs/reports/samples to user-data: deleting a version must drop the link, never its target."""
    try:
        if path.is_symlink():
            return True
        isjunction = getattr(os.path, "isjunction", None)  # Python 3.12+
        if isjunction is not None and isjunction(path):
            return True
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return False


def _unlink_link(path: Path) -> None:
    try:
        os.unlink(path)  # on Windows this removes a junction or directory symlink itself
    except OSError:
        os.rmdir(path)


def _long_path(path: Path) -> str:
    """Extended-length path on Windows, so trees deeper than 260 characters can still be deleted."""
    text = os.path.abspath(str(path))  # abspath, not resolve(): resolve() would follow a junction
    if sys.platform != "win32" or text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _remove_tree(path: Path) -> None:
    """Delete a folder; links inside it are removed as links (shutil.rmtree does not follow them
    either, but the junctions into runtime/ and user-data/ are unlinked first regardless)."""
    if _is_link(path):
        _unlink_link(path)
        return
    with os.scandir(path) as entries:
        links = [Path(entry.path) for entry in entries if _is_link(Path(entry.path))]
    for link in links:
        _unlink_link(link)

    def make_writable_and_retry(function: Callable[..., Any], target: str, *_: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(_long_path(path), onexc=make_writable_and_retry)
    else:  # pragma: no cover - the runtime is Python 3.12
        shutil.rmtree(_long_path(path), onerror=make_writable_and_retry)


# Never shipped in a release ZIP (tools/build_public_release.py excludes them). Finding one in a
# version folder means the app ran from there without the launcher's user-data folder (a .bat
# started inside versions/<v>), so that folder holds the streamer's data and is never deleted.
USER_DATA_FILES = ("config.json", "data/slang_dict.json", "data/whitelist.txt", "data/blocklist.txt")
USER_DATA_SUFFIXES = (".sqlite3", ".wav", ".mp3", ".flac")
USER_DATA_DIRS = ("logs", "reports", "samples")  # junctions into user-data/ when started by the launcher


def _user_data_in(version_dir: Path) -> str | None:
    for relative in USER_DATA_FILES:
        if (version_dir / relative).is_file():
            return relative
    data = version_dir / "data"
    if data.is_dir() and not _is_link(data):
        for item in data.iterdir():
            if item.suffix.lower() in USER_DATA_SUFFIXES:
                return f"data/{item.name}"
    for name in USER_DATA_DIRS:
        folder = version_dir / name
        if folder.is_dir() and not _is_link(folder) and any(folder.iterdir()):
            return f"{name}/"
    return None


def _pending_runtime_asset(install_root: Path) -> str | None | bool:
    """The runtime ZIP bootstrap.ps1 still has to install (runtime.json differs from the installed
    marker), None when nothing is pending, False when runtime.json cannot be read."""
    path = install_root / "runtime.json"
    if not path.exists():
        return None
    try:
        info = _read_json(path)
    except (OSError, ValueError, UpdateError):
        return False
    wanted = str(info.get("runtime_version") or "").strip()
    if not wanted or wanted == current_runtime_version(install_root):
        return None
    return str(info.get("asset_name") or f"runtime-{wanted}.zip")


def prune_install(
    install_root: Path,
    *,
    keep_archive: Path | None = None,
    running_version: str | None = None,
) -> dict[str, list[str]]:
    """Housekeeping after an install (best effort, never raises for a single item):
    - versions/: keep at most `keep_versions` (updater.json, default 2) folders, deleting the
      oldest first, but never current_version, previous_version (rollback), the running one or
      one holding user data (see USER_DATA_FILES); a folder is renamed before deletion, so one
      still in use by any process is skipped whole;
    - downloads/: delete *.zip and *.partial except `keep_archive` (the app ZIP just installed)
      and the runtime ZIP named in runtime.json while bootstrap.ps1 still has to install it;
    - delete versions/.staging-*, versions/.trash-*, runtime/.staging-* and runtime/.old-* folders
      older than STALE_FOLDER_SEC, left behind by interrupted installs."""
    install_root = install_root.resolve()
    report: dict[str, list[str]] = {"versions": [], "downloads": [], "staging": [], "errors": []}
    state = load_state(install_root)
    try:
        config = load_updater_config(install_root)
    except (OSError, ValueError, UpdateError):
        config = {}
    keep = _keep_versions(config)
    protected = {
        str(value)
        for value in (state.get("current_version"), state.get("previous_version"), running_version or _running_version(install_root))
        if value
    }

    versions_dir = install_root / "versions"
    if versions_dir.is_dir():
        installed = sorted(
            (item for item in versions_dir.iterdir() if _VERSION_DIR.fullmatch(item.name) and item.is_dir()),
            key=lambda item: _version_key(item.name),
        )
        excess = len(installed) - keep
        for item in installed:  # oldest first
            if excess <= 0:
                break
            if item.name in protected:
                continue
            try:
                user_data = _user_data_in(item)
                if user_data:
                    report["errors"].append(f"versions/{item.name}: kept, it holds user data ({user_data})")
                    continue
                trash = _fresh_dir(versions_dir, ".trash-")
                os.replace(item, trash)  # fails while a process runs from it or holds a file open in it
            except (OSError, UpdateError) as exc:
                report["errors"].append(f"versions/{item.name}: {type(exc).__name__}: {exc}")
                continue
            excess -= 1
            report["versions"].append(item.name)
            try:
                _remove_tree(trash)
            except OSError as exc:  # swept on a later run (it is no longer a version folder)
                report["errors"].append(f"versions/{trash.name}: {type(exc).__name__}: {exc}")

    downloads = install_root / "downloads"
    pending = _pending_runtime_asset(install_root)
    if downloads.is_dir() and pending is not False:
        keep_names = {pending} if pending else set()
        kept_archive = keep_archive.resolve() if keep_archive is not None else None
        for item in downloads.iterdir():
            name = item.name.lower()
            if not item.is_file() or item.name in keep_names or (kept_archive is not None and item.resolve() == kept_archive):
                continue
            if name.endswith(".zip") or name.endswith(".partial"):
                try:
                    item.unlink()
                    report["downloads"].append(item.name)
                except OSError as exc:
                    report["errors"].append(f"downloads/{item.name}: {type(exc).__name__}: {exc}")

    cutoff = time.time() - STALE_FOLDER_SEC
    for parent, prefixes in ((versions_dir, (".staging-", ".trash-")), (install_root / "runtime", (".staging-", ".old-"))):
        if not parent.is_dir():
            continue
        for item in parent.iterdir():
            # 1.1.x named version staging folders versions/.<version>.staging-<32 hex>
            legacy = parent == versions_dir and item.name.startswith(".") and ".staging-" in item.name
            if not (item.name.startswith(prefixes) or legacy):
                continue
            try:
                if item.lstat().st_mtime > cutoff:
                    continue
                _remove_tree(item)
                report["staging"].append(f"{parent.name}/{item.name}")
            except OSError as exc:
                report["errors"].append(f"{parent.name}/{item.name}: {type(exc).__name__}: {exc}")
    return report


def stage_runtime(install_root: Path, manifest: dict[str, Any], progress: ProgressHook | None = None) -> dict[str, Any]:
    """Portable installs only: download and verify the runtime a release needs, then point
    runtime.json at it. The swap itself happens in bootstrap.ps1 on the next start, because the
    running app (and this updater) execute from runtime/.venv and Windows cannot rename a
    directory with mapped DLLs. Venv installs (no runtime.json) are left alone."""
    runtime_json = install_root / "runtime.json"
    info = runtime_info_from_manifest(manifest)
    current = current_runtime_version(install_root)
    if info is None or not runtime_json.exists():
        return {"runtime_version": current, "runtime_pending": False}
    if current == info["runtime_version"]:
        return {"runtime_version": current, "runtime_pending": False}
    expected = str(info.get("sha256") or "").strip().lower()
    if len(expected) != 64:
        raise UpdateError("release manifest has no runtime SHA-256; refusing to stage an unverified runtime")
    archive = install_root / "downloads" / info["asset_name"]
    if archive.exists() and _sha256(archive) != expected:
        archive.unlink()
    if not archive.exists():
        _download(info["url"], archive, progress, label=f"runtime {info['runtime_version']}")
        actual = _sha256(archive)
        if actual != expected:
            archive.unlink(missing_ok=True)
            raise UpdateError(f"runtime SHA-256 mismatch: expected {expected}, got {actual}")
    _atomic_json(runtime_json, {**info, "sha256": expected})
    return {"runtime_version": info["runtime_version"], "runtime_pending": True, "runtime_archive": str(archive)}


def update_from_github(
    install_root: Path,
    progress: ProgressHook | None = None,
    *,
    running_version: str | None = None,
) -> dict[str, Any]:
    status = check_for_update(install_root, force=True)
    if not status.get("available"):
        return status
    manifest = status["manifest"]
    version = _validate_version(str(manifest["version"]))
    archive = install_root / "downloads" / f"{version}.zip"
    _download(str(status["asset_url"]), archive, progress, label=f"app {version}")
    # Runtime first: the new version is only activated once its runtime is verified and staged
    # (bootstrap.ps1 swaps it in before the next start, outside any running Python).
    runtime = stage_runtime(install_root, manifest, progress)
    result = install_archive(install_root, manifest, archive, running_version=running_version)
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
