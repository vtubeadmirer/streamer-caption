"""Runtime upgrades are staged by the updater (download + SHA-256 + runtime.json) and swapped in by
bootstrap.ps1 before the next start; app updates also refresh the root launcher files, prune old
versions/downloads/staging folders (keep_versions) and order prereleases numerically."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "launcher"))

import updater  # noqa: E402


def make_runtime_zip(path: Path, version: str) -> str:
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(".venv/Scripts/python.exe", b"fake python")
        bundle.writestr(".venv/RUNTIME_VERSION", version + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_app_zip(path: Path, version: str) -> dict:
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("VERSION", version + "\n")
        bundle.writestr("main.py", "print('app')\n")
    return {"version": version, "asset_name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def link_dir(link: Path, target: Path) -> None:
    """What launcher.ps1 does for versions/<v>/.venv and logs: an NTFS junction (a symlink elsewhere)."""
    if sys.platform == "win32":
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        assert result.returncode == 0, result
    else:
        os.symlink(target, link, target_is_directory=True)


def age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def check_version_order() -> None:
    key = updater._version_key
    assert key("1.2.0-dev.10") > key("1.2.0-dev.9"), "numeric prerelease parts compare as numbers"
    assert key("1.2.0") > key("1.2.0-rc.1") > key("1.2.0-dev.10") > key("1.1.9")
    chain = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
    assert sorted(chain, key=key) == chain and sorted(reversed(chain), key=key) == chain, "SemVer precedence"
    assert key("v1.10.0") > key("1.9.9") and key("V1.2.0-DEV.3") == key("1.2.0-dev.3")
    assert key("garbage") < key("0.0.1")
    sorted(["garbage", "0-dev.1", "0", "0-1"], key=key)  # unparsable and prerelease keys stay comparable

    original = updater._resolve_github_release
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "updater.json").write_text(json.dumps({"repository": "vtubeadmirer/streamer-caption", "allow_prerelease": True, "check_interval_hours": 0}), encoding="utf-8")
        try:
            for current, latest, available in (("1.2.0-dev.9", "1.2.0-dev.10", True), ("1.2.0-dev.10", "1.2.0-dev.9", False), ("1.2.0-dev.10", "1.2.0", True)):
                (root / "state.json").write_text(json.dumps({"current_version": current}), encoding="utf-8")
                updater._resolve_github_release = lambda repository, prerelease, latest=latest: {
                    "manifest": {"version": latest, "asset_name": "a.zip", "sha256": "0" * 64}, "asset_url": "https://example.test/a.zip"}
                result = updater.check_for_update(root, force=True)
                assert result["available"] is available, (current, latest, result)
        finally:
            updater._resolve_github_release = original


def check_prune() -> None:
    """keep_versions: oldest versions go first, never current/previous/running; junction targets survive."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "StreamerCaption"
        runtime = root / "runtime" / ".venv"
        runtime.mkdir(parents=True)
        (runtime / "RUNTIME_VERSION").write_text("py312-v2\n", encoding="utf-8")
        (runtime / "python-sentinel.txt").write_text("shared runtime", encoding="utf-8")
        logs = root / "user-data" / "logs"
        logs.mkdir(parents=True)
        (logs / "keep.log").write_text("user data", encoding="utf-8")
        for version in ("1.0.0", "1.1.0", "1.2.0"):
            folder = root / "versions" / version
            folder.mkdir(parents=True)
            (folder / "VERSION").write_text(version + "\n", encoding="utf-8")
            link_dir(folder / ".venv", runtime)
            link_dir(folder / "logs", logs)
        (root / "versions" / "notes").mkdir()  # not a version folder: never touched
        (root / "state.json").write_text(json.dumps({"current_version": "1.2.0", "previous_version": "1.1.0"}), encoding="utf-8")
        (root / "updater.json").write_text(json.dumps({"repository": "vtubeadmirer/streamer-caption", "keep_versions": 2}), encoding="utf-8")

        downloads = root / "downloads"
        downloads.mkdir()
        for name in ("1.0.0.zip", "1.2.0.zip.partial", "runtime-py312-v1.zip", "runtime-py312-v3.zip", "notes.txt"):
            (downloads / name).write_bytes(b"x")
        # runtime.json waits for py312-v3 (bootstrap.ps1 installs it on the next start): keep its ZIP.
        (root / "runtime.json").write_text(json.dumps({"runtime_version": "py312-v3", "asset_name": "runtime-py312-v3.zip", "sha256": "a" * 64}), encoding="utf-8")

        stale = [root / "versions" / ".staging-deadbeef", root / "versions" / (".1.1.0.staging-" + "ab" * 16),
                 root / "versions" / ".trash-cafebabe", root / "runtime" / ".staging-0badf00d", root / "runtime" / ".old-12345678"]
        for folder in stale:
            (folder / "deep" / "deeper").mkdir(parents=True)
            (folder / "deep" / "deeper" / "file.bin").write_bytes(b"x")
            age(folder, 2 * 3600)
        fresh = root / "runtime" / ".staging-f0f0f0f0"  # maybe an install in progress right now
        fresh.mkdir()

        staging_names: list[str] = []
        original_extract = updater._safe_extract

        def recording_extract(archive: Path, destination: Path) -> None:
            staging_names.append(destination.name)
            original_extract(archive, destination)

        updater._safe_extract = recording_extract
        try:
            # The app running 1.0.0 installs 1.3.0 (an install without a restart in between).
            archive = downloads / "1.3.0.zip"
            result = updater.install_archive(root, make_app_zip(archive, "1.3.0"), archive, running_version="1.0.0")
        finally:
            updater._safe_extract = original_extract
        assert staging_names and re.fullmatch(r"\.staging-[0-9a-f]{8}", staging_names[0]), staging_names
        pruned = result["pruned"]
        assert pruned["versions"] == ["1.1.0"], pruned
        left = sorted(item.name for item in (root / "versions").iterdir())
        assert left == ["1.0.0", "1.2.0", "1.3.0", "notes"], left
        assert (runtime / "python-sentinel.txt").read_text(encoding="utf-8") == "shared runtime", "junction target deleted"
        assert (logs / "keep.log").read_text(encoding="utf-8") == "user data", "user-data deleted through a junction"
        assert sorted(item.name for item in downloads.iterdir()) == ["1.3.0.zip", "notes.txt", "runtime-py312-v3.zip"], list(downloads.iterdir())
        assert sorted(pruned["downloads"]) == ["1.0.0.zip", "1.2.0.zip.partial", "runtime-py312-v1.zip"], pruned
        assert not any(folder.exists() for folder in stale) and fresh.exists(), pruned
        assert len(pruned["staging"]) == len(stale) and not pruned["errors"], pruned

        # Next update (not running from a version folder): 1.0.0 and 1.2.0 are now the oldest extras.
        (runtime / "RUNTIME_VERSION").write_text("py312-v3\n", encoding="utf-8")  # bootstrap installed it
        archive = downloads / "1.4.0.zip"
        result = updater.install_archive(root, make_app_zip(archive, "1.4.0"), archive)
        assert result["pruned"]["versions"] == ["1.0.0", "1.2.0"], result["pruned"]
        assert sorted(item.name for item in (root / "versions").iterdir() if not item.name.startswith(".")) == ["1.3.0", "1.4.0", "notes"]
        assert sorted(item.name for item in downloads.iterdir()) == ["1.4.0.zip", "notes.txt"], "installed runtime ZIP is no longer kept"
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
        assert state["current_version"] == "1.4.0" and state["previous_version"] == "1.3.0"
        updater.rollback(root)  # previous_version is always kept for rollback
        assert json.loads((root / "state.json").read_text(encoding="utf-8"))["current_version"] == "1.3.0"

        # keep_versions 3 keeps one extra; a folder still in use (an open file inside) is skipped whole.
        (root / "updater.json").write_text(json.dumps({"keep_versions": 3}), encoding="utf-8")
        for version in ("1.5.0", "1.6.0"):
            archive = downloads / f"{version}.zip"
            updater.install_archive(root, make_app_zip(archive, version), archive)
        assert sorted(item.name for item in (root / "versions").iterdir() if not item.name.startswith(".")) == ["1.4.0", "1.5.0", "1.6.0", "notes"]
        if sys.platform == "win32":
            with open(root / "versions" / "1.4.0" / "VERSION", "rb"):  # e.g. a second window still running 1.4.0
                archive = downloads / "1.7.0.zip"
                result = updater.install_archive(root, make_app_zip(archive, "1.7.0"), archive)
            assert result["pruned"]["versions"] == ["1.5.0"] and result["pruned"]["errors"][0].startswith("versions/1.4.0"), result["pruned"]
            assert (root / "versions" / "1.4.0" / "VERSION").is_file(), "an in-use version must not be half-deleted"
            archive = downloads / "1.8.0.zip"
            result = updater.install_archive(root, make_app_zip(archive, "1.8.0"), archive)
            assert result["pruned"]["versions"] == ["1.4.0"] and not result["pruned"]["errors"], result["pruned"]
            assert sorted(item.name for item in (root / "versions").iterdir() if not item.name.startswith(".")) == ["1.6.0", "1.7.0", "1.8.0", "notes"]

        # A version started without the launcher (a .bat inside versions/<v>) wrote the streamer's data
        # into its own folder: that folder is never deleted.
        legacy = root / "versions" / "0.9.0"
        (legacy / "data").mkdir(parents=True)
        (legacy / "VERSION").write_text("0.9.0\n", encoding="utf-8")
        (legacy / "data" / "transcripts.sqlite3").write_bytes(b"captions")
        archive = downloads / "1.9.0.zip"
        result = updater.install_archive(root, make_app_zip(archive, "1.9.0"), archive)
        assert "versions/0.9.0: kept, it holds user data (data/transcripts.sqlite3)" in result["pruned"]["errors"], result["pruned"]
        assert (legacy / "data" / "transcripts.sqlite3").read_bytes() == b"captions" and "0.9.0" not in result["pruned"]["versions"]
        probe = Path(directory) / "probe"
        (probe / "logs").mkdir(parents=True)
        assert updater._user_data_in(probe) is None, "an empty folder is not user data"
        (probe / "logs" / "session.log").write_text("x", encoding="utf-8")
        assert updater._user_data_in(probe) == "logs/"
        (probe / "config.json").write_text("{}", encoding="utf-8")
        assert updater._user_data_in(probe) == "config.json"


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "src"
        source.mkdir()
        zip_v2 = source / "runtime-py312-v2.zip"
        sha_v2 = make_runtime_zip(zip_v2, "py312-v2")
        manifest = {"version": "1.2.0", "runtime_version": "py312-v2", "runtime_asset_name": "runtime-py312-v2.zip",
                    "runtime_sha256": sha_v2, "runtime_size": zip_v2.stat().st_size,
                    "runtime_url": "https://example.test/releases/runtime-py312-v2.zip"}

        downloads: list[str] = []
        original_download = updater._download

        def fake_download(url: str, target: Path, progress=None, label: str = "") -> None:
            downloads.append(url)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / Path(url).name, target)

        updater._download = fake_download
        try:
            # Venv install (no runtime.json): runtime fields in the manifest are ignored.
            result = updater.stage_runtime(root, manifest)
            assert result["runtime_pending"] is False and not downloads, result

            # Portable install on py312-v1: the v2 runtime is downloaded, verified, kept in downloads/
            # and runtime.json now names it; the installed runtime is untouched until bootstrap swaps it.
            venv = root / "runtime" / ".venv"
            venv.mkdir(parents=True)
            (venv / "RUNTIME_VERSION").write_text("py312-v1\n", encoding="utf-8")
            (root / "runtime.json").write_text(json.dumps({"runtime_version": "py312-v1", "asset_name": "runtime-py312-v1.zip", "sha256": "a" * 64, "size": 1, "url": "https://example.test/v1.zip"}), encoding="utf-8")
            result = updater.stage_runtime(root, manifest)
            assert result["runtime_pending"] is True and result["runtime_version"] == "py312-v2", result
            assert (root / "downloads" / "runtime-py312-v2.zip").exists(), "archive stays for bootstrap.ps1"
            assert updater.current_runtime_version(root) == "py312-v1", "no in-process swap"
            staged = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
            assert staged["runtime_version"] == "py312-v2" and staged["sha256"] == sha_v2 and staged["asset_name"] == "runtime-py312-v2.zip"
            assert len(downloads) == 1

            # Staging again reuses the verified archive.
            result = updater.stage_runtime(root, manifest)
            assert result["runtime_pending"] is True and len(downloads) == 1

            # Once bootstrap installed it (marker updated), nothing is pending.
            (venv / "RUNTIME_VERSION").write_text("py312-v2\n", encoding="utf-8")
            assert updater.stage_runtime(root, manifest)["runtime_pending"] is False

            # Wrong hash: refused, archive removed, runtime.json untouched.
            bad = {**manifest, "runtime_version": "py312-v3", "runtime_asset_name": "runtime-py312-v2.zip", "runtime_sha256": "0" * 64}
            try:
                updater.stage_runtime(root, bad)
                raise AssertionError("hash mismatch accepted")
            except updater.UpdateError as exc:
                assert "SHA-256" in str(exc)
            assert not (root / "downloads" / "runtime-py312-v2.zip").exists()
            assert json.loads((root / "runtime.json").read_text(encoding="utf-8"))["runtime_version"] == "py312-v2"

            # Empty hash: refused before downloading.
            empty = {**manifest, "runtime_version": "py312-v4", "runtime_asset_name": "runtime-py312-v4.zip", "runtime_sha256": ""}
            try:
                updater.stage_runtime(root, empty)
                raise AssertionError("empty hash accepted")
            except updater.UpdateError as exc:
                assert "SHA-256" in str(exc)
        finally:
            updater._download = original_download

        # Launcher sync: a new version's launcher files replace the root copies (unchanged files are skipped).
        version_root = root / "versions" / "1.2.0"
        (version_root / "launcher").mkdir(parents=True)
        (root / "launcher").mkdir()
        (root / "launcher" / "updater.py").write_text("old updater\n", encoding="utf-8")
        (root / "launcher" / "launcher.ps1").write_text("same\n", encoding="utf-8")
        (version_root / "launcher" / "updater.py").write_text("new updater\n", encoding="utf-8")
        (version_root / "launcher" / "launcher.ps1").write_text("same\n", encoding="utf-8")
        (version_root / "launcher" / "bootstrap.ps1").write_text("new bootstrap\n", encoding="utf-8")
        synced = updater.sync_launcher(root, version_root)
        assert sorted(synced) == ["bootstrap.ps1", "updater.py"], synced
        assert (root / "launcher" / "updater.py").read_text(encoding="utf-8") == "new updater\n"
        assert (root / "launcher" / "bootstrap.ps1").read_text(encoding="utf-8") == "new bootstrap\n"
        assert not list((root / "launcher").glob("*.new"))
        assert updater.sync_launcher(root, root / "versions" / "missing") == []

        # StreamerCaption.bat belongs at the install ROOT (portable layout). INSTALL.bat installs have
        # none there, so none is created; launcher/StreamerCaption.bat is never created.
        (version_root / "launcher" / "StreamerCaption.bat").write_bytes(b"@echo new\r\n")
        assert updater.sync_launcher(root, version_root) == []
        assert not (root / "StreamerCaption.bat").exists() and not (root / "launcher" / "StreamerCaption.bat").exists()
        (root / "StreamerCaption.bat").write_bytes(b"@echo old\r\n")
        (root / "launcher" / "StreamerCaption.bat").write_bytes(b"@echo stray from 1.1.3\r\n")
        assert updater.sync_launcher(root, version_root) == ["StreamerCaption.bat"]
        assert (root / "StreamerCaption.bat").read_bytes() == b"@echo new\r\n"
        assert not (root / "launcher" / "StreamerCaption.bat").exists(), "the stray copy 1.1.3 left in launcher/ is removed"
        assert updater.sync_launcher(root, version_root) == [], "unchanged files are skipped"
        # A source checkout (main.py at the top) keeps its launcher/StreamerCaption.bat: it is the original.
        (root / "main.py").write_bytes(b"")
        (root / "launcher" / "StreamerCaption.bat").write_bytes(b"@echo source\r\n")
        updater.sync_launcher(root, version_root)
        assert (root / "launcher" / "StreamerCaption.bat").exists()

    check_version_order()
    check_prune()
    print("updater_runtime_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
