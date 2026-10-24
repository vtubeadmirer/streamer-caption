"""Update checks, update installs and relaunch requests driven from the admin page.

The admin HTTP server owns one AppController; the capture loop in main.py polls
``restart_requested`` and exits with RELAUNCH_EXIT_CODE so the supervisor/launcher
starts the (possibly new) current version again.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any

from core import app_info

CHECK_TTL_SEC = 60 * 60  # in-process cache so opening the admin page does not hit GitHub every time
CHECK_ERROR_RETRY_SEC = 10 * 60  # offline or rate-limited: retry a failed check this often at most


class AppController:
    def __init__(self, install_root: Path | None = None, updater: ModuleType | None = None) -> None:
        self._lock = threading.Lock()
        self._install_root = install_root if install_root is not None else app_info.install_root()
        self._updater = updater
        self.restart_requested = False
        self.restart_reason = ""
        self.restart_requested_at = 0.0
        self._check: dict[str, Any] = {"state": "idle"}
        self._check_at = 0.0
        self._check_thread: threading.Thread | None = None
        self._install: dict[str, Any] = {"state": "idle"}
        self._install_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ helpers
    @property
    def install_root(self) -> Path | None:
        return self._install_root

    @property
    def managed(self) -> bool:
        return self._install_root is not None

    def _module(self) -> ModuleType | None:
        if self._updater is None:
            self._updater = app_info.load_updater()
        return self._updater

    @staticmethod
    def _public_check(result: dict[str, Any]) -> dict[str, Any]:
        public = {key: value for key, value in result.items() if key != "manifest"}
        manifest = result.get("manifest")
        if isinstance(manifest, dict):
            public["release_notes"] = str(manifest.get("release_notes", result.get("release_notes", "")))
            public["portable_asset_name"] = manifest.get("portable_asset_name")
            public["runtime_version"] = manifest.get("runtime_version")
        return public

    # ------------------------------------------------------------------ update check
    def snapshot(self) -> dict[str, Any]:
        """State for GET /api/update/status, which the admin page calls on every load. It also
        starts a cached (non-forced) check when none ran within CHECK_TTL_SEC, so the page never
        has to force one: the updater's on-disk cache then answers most loads without GitHub."""
        with self._lock:
            installing = self._install_thread is not None and self._install_thread.is_alive()
        if not installing:
            self._start_check(force=False)
        return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        app = app_info.describe()  # reads VERSION, state.json and updater.json: keep it out of the lock
        with self._lock:
            return {
                "app": app,
                "managed": self.managed,
                "check": dict(self._check),
                "install": dict(self._install),
                "restart_requested": self.restart_requested,
                "restart_reason": self.restart_reason,
            }

    def check(self, *, force: bool = False, wait: bool = False) -> dict[str, Any]:
        """Start (or reuse) an update check. Returns the current snapshot. force=True bypasses both
        caches and is meant for an explicit user action (POST /api/update/check, tools)."""
        thread = self._start_check(force=force)
        if wait and thread is not None:
            thread.join(timeout=90)
        return self._snapshot()

    def _start_check(self, *, force: bool) -> threading.Thread | None:
        """The running check thread, a new one when the last result is stale (or force), else None."""
        if not self.managed:
            with self._lock:
                self._check = {"state": "done", "available": False, "configured": False, "reason": "not_managed"}
            return None
        with self._lock:
            if self._check_thread is not None and self._check_thread.is_alive():
                return self._check_thread
            age = time.monotonic() - self._check_at
            state = self._check.get("state")
            fresh = self._check_at > 0 and (
                (state == "done" and age < CHECK_TTL_SEC) or (state == "error" and age < CHECK_ERROR_RETRY_SEC)
            )
            if fresh and not force:
                return None
            # Keep the last answer visible (banner stays up) while the new check runs.
            self._check = {**self._check, "state": "checking"} if state == "done" else {"state": "checking"}
            thread = threading.Thread(target=self._run_check, args=(force,), name="update-check", daemon=True)
            self._check_thread = thread
            thread.start()
            return thread

    def _run_check(self, force: bool) -> None:
        module = self._module()
        try:
            if module is None:
                raise RuntimeError("updater module not found")
            result = module.check_for_update(self._install_root, force=force)
            public = self._public_check(result)
            public["state"] = "done"
        except Exception as exc:  # network errors are reported, never raised into the page
            public = {"state": "error", "available": False, "error": f"{type(exc).__name__}: {exc}"}
        with self._lock:
            self._check = public
            self._check_at = time.monotonic()

    def skip_version(self, version: str) -> dict[str, Any]:
        version = str(version or "").strip().lstrip("v")
        if not version:
            raise ValueError("version required")
        if self.managed:
            path = self._install_root / "updater.json"
            try:
                config = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
            except ValueError:
                config = {}
            config["skipped_version"] = version
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
            cache = self._install_root / "update-check-cache.json"
            cache.unlink(missing_ok=True)
        with self._lock:
            if self._check.get("latest_version") == version:
                self._check["available"] = False
                self._check["skipped"] = True
        return self._snapshot()

    # ------------------------------------------------------------------ install
    def start_update(self) -> dict[str, Any]:
        if not self.managed:
            raise RuntimeError("this copy is not installed with the launcher, so it cannot update itself")
        with self._lock:
            # A second click while installing just reports progress (the snapshot takes the lock itself).
            if self._install_thread is None or not self._install_thread.is_alive():
                self._install = {"state": "starting", "label": "", "done": 0, "total": 0, "percent": 0}
                thread = threading.Thread(target=self._run_update, name="update-install", daemon=True)
                self._install_thread = thread
                thread.start()
        return self._snapshot()

    def _progress(self, label: str, done: int, total: int) -> None:
        with self._lock:
            self._install.update(
                {
                    "state": "downloading",
                    "label": label,
                    "done": int(done),
                    "total": int(total),
                    "percent": int(done * 100 / total) if total else 0,
                }
            )

    def _run_update(self) -> None:
        module = self._module()
        try:
            if module is None:
                raise RuntimeError("updater module not found")
            with self._lock:
                self._install["state"] = "checking"
            result = module.update_from_github(self._install_root, progress=self._progress)
            with self._lock:
                if result.get("installed") or result.get("current_version") not in (None, app_info.app_version()):
                    self._install = {
                        "state": "done",
                        "current_version": result.get("installed") or result.get("current_version"),
                        "previous_version": result.get("previous_version"),
                        "runtime_pending": bool(result.get("runtime_pending")),
                        "launcher_synced": list(result.get("launcher_synced") or []),
                        "percent": 100,
                    }
                else:
                    self._install = {"state": "idle", "reason": result.get("reason", "nothing_to_install")}
                self._check_at = 0.0  # next status call re-checks
        except Exception as exc:
            with self._lock:
                self._install = {"state": "failed", "error": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------------ restart
    def request_restart(self, reason: str = "") -> dict[str, Any]:
        with self._lock:
            self.restart_requested = True
            self.restart_reason = str(reason or "")
            self.restart_requested_at = time.time()
        return {"ok": True, "restart_requested": True, "reason": self.restart_reason, "exit_code": app_info.RELAUNCH_EXIT_CODE}
