"""Runtime upgrades are staged by the updater (download + SHA-256 + runtime.json) and swapped in by
bootstrap.ps1 before the next start; app updates also refresh the root launcher files."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
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

    print("updater_runtime_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
