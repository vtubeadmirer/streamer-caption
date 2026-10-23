"""A fresh repository has no published release: the launcher must report "no update", not fail."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "launcher"))

import updater  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "state.json").write_text(json.dumps({"current_version": "1.0.0", "previous_version": None}), encoding="utf-8")
        (root / "updater.json").write_text(json.dumps({"repository": "vtubeadmirer/streamer-caption", "allow_prerelease": False, "check_interval_hours": 24}), encoding="utf-8")

        original = updater.urllib.request.urlopen

        def not_found(request, timeout=15):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))

        updater.urllib.request.urlopen = not_found
        try:
            result = updater.check_for_update(root, force=True)
        finally:
            updater.urllib.request.urlopen = original
        assert result["configured"] is True, result
        assert result["available"] is False, result
        assert result["reason"] == "no_published_release", result

        def server_error(request, timeout=15):
            raise urllib.error.HTTPError(request.full_url, 500, "Server Error", {}, io.BytesIO(b"{}"))

        updater.urllib.request.urlopen = server_error
        try:
            updater.check_for_update(root, force=True)
            raise AssertionError("a 500 must still surface as an error")
        except updater.UpdateError as exc:
            assert "500" in str(exc)
        finally:
            updater.urllib.request.urlopen = original

        original_request_json = updater._request_json
        (root / "updater.json").write_text(json.dumps({"repository": "vtubeadmirer/streamer-caption", "allow_prerelease": True, "check_interval_hours": 24}), encoding="utf-8")
        updater._request_json = lambda url: []
        try:
            result = updater.check_for_update(root, force=True)
        finally:
            updater._request_json = original_request_json
        assert result["available"] is False and result["reason"] == "no_published_release", result

    # Dev channel: a newer runtime-* release (no release-manifest.json) must not shadow the app release.
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "state.json").write_text(json.dumps({"current_version": "1.0.0", "previous_version": None}), encoding="utf-8")
        (root / "updater.json").write_text(json.dumps({"repository": "vtubeadmirer/streamer-caption", "allow_prerelease": True, "check_interval_hours": 0}), encoding="utf-8")
        manifest = {"version": "1.2.0-dev.1", "asset_name": "streamer-caption-1.2.0-dev.1.zip", "sha256": "0" * 64}
        releases = [
            {"tag_name": "runtime-py312-v2", "draft": False, "assets": [{"name": "runtime-py312-v2.zip", "browser_download_url": "https://x/r.zip"}]},
            {"tag_name": "v1.2.0-dev.1", "draft": False, "assets": [
                {"name": "release-manifest.json", "browser_download_url": "https://x/release-manifest.json"},
                {"name": manifest["asset_name"], "browser_download_url": "https://x/app.zip"}]},
        ]
        original_request_json = updater._request_json
        updater._request_json = lambda url: releases if "/releases?" in url else manifest
        try:
            result = updater.check_for_update(root, force=True)
        finally:
            updater._request_json = original_request_json
        assert result["available"] is True and result["latest_version"] == "1.2.0-dev.1", result
    print("updater_no_release_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
