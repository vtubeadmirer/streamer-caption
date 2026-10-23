"""The portable bundle must be self-describing: launcher, versioned app, runtime pointer, state, updater config."""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.build_public_release import build_public_release


def main() -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8-sig").strip()
    with tempfile.TemporaryDirectory() as directory:
        dist = Path(directory)
        (dist / "runtime-manifest.json").write_text(json.dumps({
            "runtime_version": "py312-test", "asset_name": "runtime-py312-test.zip", "sha256": "a" * 64, "size": 12345,
        }), encoding="utf-8")
        built = build_public_release(ROOT, dist)
        manifest = json.loads(Path(built["manifest"]).read_text(encoding="utf-8"))
        assert manifest["runtime_version"] == "py312-test", manifest
        assert manifest["runtime_sha256"] == "a" * 64
        assert manifest["runtime_url"].endswith("/releases/download/runtime-py312-v1/runtime-py312-test.zip"), manifest["runtime_url"]
        assert manifest["portable_asset_name"] == f"StreamerCaption-{version}-portable.zip"
        assert len(manifest["portable_sha256"]) == 64

        portable = Path(built["portable"])
        assert portable.exists()
        with zipfile.ZipFile(portable) as bundle:
            names = set(bundle.namelist())
            root = "StreamerCaption/"
            for required in ("StreamerCaption.bat", "launcher/bootstrap.ps1", "launcher/launcher.ps1", "launcher/updater.py",
                             "runtime.json", "state.json", "updater.json", "README-FIRST.txt",
                             f"versions/{version}/main.py", f"versions/{version}/tools/run_supervised.py",
                             f"versions/{version}/tools/bootstrap_user_data.py", f"versions/{version}/overlay/setup.html",
                             f"versions/{version}/updater.example.json", f"versions/{version}/VERSION"):
                assert root + required in names, required
            forbidden = [name for name in names if name.endswith(("config.json", "slang_dict.json", ".sqlite3", ".wav")) and "example" not in name]
            assert not forbidden, forbidden
            assert not any("/.venv/" in name or "/runtime/." in name for name in names)
            runtime = json.loads(bundle.read(root + "runtime.json"))
            assert runtime == {"runtime_version": "py312-test", "asset_name": "runtime-py312-test.zip", "sha256": "a" * 64, "size": 12345, "url": manifest["runtime_url"]}, runtime
            state = json.loads(bundle.read(root + "state.json"))
            assert state["current_version"] == version and state["portable"] is True and state["previous_version"] is None
            updater = json.loads(bundle.read(root + "updater.json"))
            assert updater["repository"] == "vtubeadmirer/streamer-caption"
            assert updater["allow_prerelease"] is ("-" in version), "prerelease builds follow the dev channel"
            assert bundle.read(root + f"versions/{version}/VERSION").decode("utf-8-sig").strip() == version

        # Without a runtime manifest the template values are used and the URL still points at the runtime release.
        (dist / "runtime-manifest.json").unlink()
        built = build_public_release(ROOT, dist)
        manifest = json.loads(Path(built["manifest"]).read_text(encoding="utf-8"))
        assert manifest["runtime_version"] == "py312-v1" and manifest["runtime_asset_name"] == "runtime-py312-v1.zip"
        assert manifest["runtime_url"].endswith("/releases/download/runtime-py312-v1/runtime-py312-v1.zip")

    for script in sorted((ROOT / "launcher").glob("*.ps1")) + sorted((ROOT / "tools").glob("*.ps1")):
        raw = script.read_bytes()
        if any(byte > 127 for byte in raw):
            assert raw.startswith(bytes([0xEF, 0xBB, 0xBF])), f"{script.name} has non-ASCII text but no UTF-8 BOM (Windows PowerShell 5.1 would print mojibake)"
    print("portable_release_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
