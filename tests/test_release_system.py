from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launcher.updater import UpdateError, _safe_extract, install_archive, rollback
from tools.apply_auto_gpu_preset import choose_profile
from tools.bootstrap_user_data import bootstrap_user_data
from tools.build_public_release import build_public_release


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def fake_release(root: Path, version: str) -> tuple[Path, dict]:
    asset = root / f"streamer-caption-{version}.zip"
    with zipfile.ZipFile(asset, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("VERSION", version + "\n")
        bundle.writestr("START_HERE.bat", "@echo off\n")
    manifest = {
        "version": version,
        "asset_name": asset.name,
        "sha256": sha256(asset),
        "minimum_launcher_version": "1.0.0",
    }
    return asset, manifest


def main() -> int:
    assert choose_profile("NVIDIA RTX", 8192)[0] == "gpu_8gb_safe"
    assert choose_profile("NVIDIA RTX", 16384)[0] == "gpu_12_16gb_balanced"
    assert choose_profile("NVIDIA RTX", 24576)[0] == "gpu_20gb_quality"
    assert choose_profile("not detected", None)[0] == "cpu_compatible"

    with tempfile.TemporaryDirectory(prefix="streamer-release-test-") as temp:
        base = Path(temp)
        user_data = base / "user-data"
        bootstrap_user_data(user_data)
        slang = user_data / "data" / "slang_dict.json"
        slang.write_text('{"custom": "protected"}\n', encoding="utf-8")
        bootstrap_user_data(user_data)
        assert json.loads(slang.read_text(encoding="utf-8"))["custom"] == "protected"
        config = json.loads((user_data / "config.json").read_text(encoding="utf-8"))
        assert Path(config["archive"]["db_path"]).is_absolute()
        assert str(user_data.resolve()) in config["archive"]["db_path"]

        dist = base / "dist"
        built = build_public_release(ROOT, dist)
        asset = Path(built["asset"])
        manifest = json.loads(Path(built["manifest"]).read_text(encoding="utf-8"))
        assert sha256(asset) == manifest["sha256"]
        with zipfile.ZipFile(asset) as bundle:
            names = set(bundle.namelist())
        assert "config.json" not in names
        assert "data/slang_dict.json" not in names
        assert "data/whitelist.txt" not in names
        assert "data/blocklist.txt" not in names
        assert "config.example.json" in names
        assert "INSTALL.bat" in names
        assert not any(name.startswith("logs/") for name in names)
        assert not any(name.endswith(".wav") for name in names)
        assert not any(name.endswith(".sqlite3") for name in names)
        assert "update-check-cache.json" not in names

        install_root = base / "installed"
        install_archive(install_root, manifest, asset)
        persistent = install_root / "user-data" / "keep.txt"
        persistent.parent.mkdir(parents=True)
        persistent.write_text("keep", encoding="utf-8")
        second_asset, second_manifest = fake_release(base, "1.0.1")
        install_archive(install_root, second_manifest, second_asset)
        state = json.loads((install_root / "state.json").read_text(encoding="utf-8"))
        assert state["current_version"] == "1.0.1"
        assert state["previous_version"] == "1.0.0-dev"
        rollback(install_root)
        state = json.loads((install_root / "state.json").read_text(encoding="utf-8"))
        assert state["current_version"] == "1.0.0-dev"
        assert persistent.read_text(encoding="utf-8") == "keep"

        malicious = base / "malicious.zip"
        with zipfile.ZipFile(malicious, "w") as bundle:
            bundle.writestr("../escape.txt", "blocked")
        try:
            _safe_extract(malicious, base / "unsafe")
        except UpdateError:
            pass
        else:
            raise AssertionError("unsafe ZIP path was not rejected")
        assert not (base / "escape.txt").exists()

    print("release_system_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
