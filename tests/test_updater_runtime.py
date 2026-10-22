"""Runtime upgrades: the updater swaps the shared runtime only when a release asks for a different one, after SHA-256 and layout checks."""
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
        bundle.writestr(".venv/Lib/site-packages/README", b"deps")
        bundle.writestr(".venv/RUNTIME_VERSION", version + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "src"
        source.mkdir()
        zip_v1 = source / "runtime-py312-v1.zip"
        sha_v1 = make_runtime_zip(zip_v1, "py312-v1")

        downloads: list[str] = []
        original_download = updater._download

        def fake_download(url: str, target: Path) -> None:
            downloads.append(url)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / Path(url).name, target)

        updater._download = fake_download
        try:
            manifest = {"version": "1.2.0", "runtime_version": "py312-v1", "runtime_asset_name": "runtime-py312-v1.zip",
                        "runtime_sha256": sha_v1, "runtime_size": zip_v1.stat().st_size, "runtime_url": "https://example.test/releases/runtime-py312-v1.zip"}

            # Fresh portable root: runtime is downloaded and installed.
            result = updater.ensure_runtime(root, manifest)
            assert result["runtime_installed"] is True, result
            assert (root / "runtime" / ".venv" / "Scripts" / "python.exe").read_bytes() == b"fake python"
            assert updater.current_runtime_version(root) == "py312-v1"
            assert json.loads((root / "runtime.json").read_text(encoding="utf-8"))["sha256"] == sha_v1
            assert not (root / "downloads" / "runtime-py312-v1.zip").exists(), "archive is removed after install"
            assert len(downloads) == 1

            # Same runtime again: no download, no change.
            result = updater.ensure_runtime(root, manifest)
            assert result["runtime_installed"] is False and len(downloads) == 1

            # Manifest without runtime fields (venv install): no-op.
            result = updater.ensure_runtime(root, {"version": "1.2.1"})
            assert result["runtime_installed"] is False and len(downloads) == 1

            # New runtime version replaces the old one atomically.
            zip_v2 = source / "runtime-py312-v2.zip"
            sha_v2 = make_runtime_zip(zip_v2, "py312-v2")
            manifest_v2 = {**manifest, "runtime_version": "py312-v2", "runtime_asset_name": "runtime-py312-v2.zip", "runtime_sha256": sha_v2, "runtime_url": "https://example.test/releases/runtime-py312-v2.zip"}
            result = updater.ensure_runtime(root, manifest_v2)
            assert result["runtime_installed"] is True and updater.current_runtime_version(root) == "py312-v2"
            assert not any(p.name.startswith(".old-") or p.name.startswith(".staging-") for p in (root / "runtime").iterdir())

            # Wrong hash is refused and the current runtime stays.
            bad = {**manifest, "runtime_version": "py312-v3", "runtime_asset_name": "runtime-py312-v1.zip", "runtime_sha256": "0" * 64}
            try:
                updater.ensure_runtime(root, bad)
                raise AssertionError("hash mismatch accepted")
            except updater.UpdateError as exc:
                assert "SHA-256" in str(exc)
            assert updater.current_runtime_version(root) == "py312-v2"
        finally:
            updater._download = original_download

    print("updater_runtime_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
