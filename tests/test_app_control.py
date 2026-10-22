"""Admin-page update banner, skip, restart request and bug-bundle routes."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.admin_server import AdminServer, AdminState, issue_url  # noqa: E402
from core.app_control import AppController  # noqa: E402
from core.config import load_config  # noqa: E402

PORT = 9879


def http(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload or {}).encode("utf-8") if method == "POST" else None
    request = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def fake_github(latest_version: str):
    manifest = {"version": latest_version, "asset_name": f"streamer-caption-{latest_version}.zip", "sha256": "0" * 64,
                "release_notes": "faster captions", "mandatory": False}
    release = {"tag_name": f"v{latest_version}", "draft": False, "assets": [
        {"name": "release-manifest.json", "browser_download_url": "https://example.test/release-manifest.json"},
        {"name": manifest["asset_name"], "browser_download_url": f"https://example.test/{manifest['asset_name']}"},
    ]}

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def urlopen(request, timeout=15):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if url.endswith("/releases/latest"):
            return Response(json.dumps(release).encode("utf-8"))
        if url.endswith("release-manifest.json"):
            return Response(json.dumps(manifest).encode("utf-8"))
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))

    return urlopen


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "versions" / "1.0.0").mkdir(parents=True)
        (root / "state.json").write_text(json.dumps({"current_version": "1.0.0", "previous_version": None}), encoding="utf-8")
        (root / "updater.json").write_text(json.dumps({"repository": "vtubeadmirer/streamer-caption", "check_interval_hours": 24, "allow_prerelease": False}), encoding="utf-8")
        (root / "launcher").mkdir()
        shutil.copy2(ROOT / "launcher" / "updater.py", root / "launcher" / "updater.py")
        spec = importlib.util.spec_from_file_location("updater_under_test", root / "launcher" / "updater.py")
        updater = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(updater)

        original = updater.urllib.request.urlopen
        updater.urllib.request.urlopen = fake_github("1.2.0")
        try:
            control = AppController(install_root=root, updater=updater)
            snapshot = control.check(force=True, wait=True)
            check = snapshot["check"]
            assert check["state"] == "done" and check["available"] is True and check["latest_version"] == "1.2.0", check
            assert "manifest" not in check and check["release_notes"] == "faster captions", check
            assert snapshot["managed"] is True and snapshot["install"]["state"] == "idle", snapshot

            # Cached: a second non-forced check returns immediately with the same answer.
            updater.urllib.request.urlopen = original
            again = control.check(force=False, wait=True)["check"]
            assert again["available"] is True, again

            # Skip writes updater.json and hides the banner.
            skipped = control.skip_version("1.2.0")["check"]
            assert skipped["available"] is False and skipped.get("skipped") is True, skipped
            assert json.loads((root / "updater.json").read_text(encoding="utf-8"))["skipped_version"] == "1.2.0"
            updater.urllib.request.urlopen = fake_github("1.2.0")
            assert control.check(force=True, wait=True)["check"]["available"] is False, "skipped version must stay hidden after a fresh check"
            updater.urllib.request.urlopen = fake_github("1.3.0")
            assert control.check(force=True, wait=True)["check"]["available"] is True
        finally:
            updater.urllib.request.urlopen = original

        # Not managed (source checkout): no banner, install refused, restart still works.
        plain = AppController(install_root=None)
        assert plain.check(wait=True)["check"]["reason"] == "not_managed"
        try:
            plain.start_update()
            raise AssertionError("install must be refused outside a managed install")
        except RuntimeError:
            pass

        # HTTP routes on a live admin server.
        config = copy.deepcopy(load_config())
        config["server"]["admin_port"] = PORT
        state = AdminState()
        state.control = control
        state.last_caption = "비밀 자막"
        server = AdminServer(config, state)
        server.start_background()
        time.sleep(0.3)
        try:
            code, app = http("GET", "/api/app")
            assert code == 200 and app["version"] and app["issues_url"].startswith("https://github.com/"), app
            code, status = http("GET", "/api/update/status")
            assert code == 200 and status["check"]["latest_version"] == "1.3.0", status
            code, skip = http("POST", "/api/update/skip", {"version": ""})
            assert code == 400, skip
            code, status = http("GET", "/api/status")
            assert status["restart_requested"] is False and status["version"], status

            code, bundle = http("POST", "/api/support/bundle", {"note": "자막이 10분 뒤 멈춤"})
            assert code == 200 and bundle["name"].startswith("streamer_caption_bugreport_"), bundle
            assert "template=bug_report.yml" in bundle["issue_url"] and "what-happened=" in bundle["issue_url"], bundle["issue_url"]
            assert "비밀 자막" not in json.dumps(bundle, ensure_ascii=False)
            bundle_path = Path(bundle["path"])
            assert bundle_path.is_file()
            import zipfile

            with zipfile.ZipFile(bundle_path) as zf:
                blob = b"".join(zf.read(name) for name in zf.namelist()).decode("utf-8", errors="replace")
            assert "비밀 자막" not in blob and "status.json" in zf.namelist()
            bundle_path.unlink()

            code, reveal = http("POST", "/api/support/reveal", {"path": str(ROOT / "README.md")})
            assert code == 400, reveal

            code, restart = http("POST", "/api/app/restart", {"reason": "test"})
            assert code == 200 and restart["restart_requested"] is True and restart["exit_code"] == 76, restart
            code, status = http("GET", "/api/status")
            assert status["restart_requested"] is True
            assert control.restart_requested and control.restart_reason == "test"

            html = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/admin", timeout=5).read().decode("utf-8")
            for marker in ("updateBanner", "bugDialog", "/api/update/status", "/api/app/restart", "/api/support/bundle"):
                assert marker in html, marker
            setup_html = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/setup", timeout=5).read().decode("utf-8")
            assert "/api/app/restart" in setup_html
        finally:
            server.stop()

    url = issue_url({"environment": {"app": {"version": "9.9.9"}}, "environment_text": "gpu: x"}, "자막 멈춤")
    assert url.startswith("https://github.com/vtubeadmirer/streamer-caption/issues/new?template=bug_report.yml") and "version=9.9.9" in url, url
    print("app_control_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
