"""Where this copy of Streamer Caption lives and which version it is.

Three layouts exist:
  * developer checkout      -> PROJECT_ROOT/launcher/updater.py, no state.json
  * installed (INSTALL.bat) -> <root>/versions/<ver>/..., <root>/state.json, <root>/launcher/updater.py
  * portable (ZIP)          -> same as installed plus <root>/runtime.json
Only the managed layouts can update or relaunch themselves.
"""
from __future__ import annotations

import importlib.util
import json
import platform
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from core.config import PROJECT_ROOT, USER_DATA_ROOT

RELAUNCH_EXIT_CODE = 76  # main.py exits with this when the admin page asks for a restart
ISSUES_URL = "https://github.com/vtubeadmirer/streamer-caption/issues"


def app_version() -> str:
    path = PROJECT_ROOT / "VERSION"
    try:
        value = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return "dev"
    return value or "dev"


def install_root() -> Path | None:
    """The side-by-side install root, or None for a plain developer checkout."""
    parent = PROJECT_ROOT.parent
    if parent.name != "versions":
        return None
    root = parent.parent
    if (root / "state.json").is_file():
        return root
    return None


def is_portable() -> bool:
    root = install_root()
    return root is not None and (root / "runtime.json").is_file()


def updater_path() -> Path | None:
    # The running version's copy first: the root launcher may predate fixes shipped with the app.
    root = install_root()
    candidates = [PROJECT_ROOT / "launcher" / "updater.py"]
    if root is not None:
        candidates.append(root / "launcher" / "updater.py")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_updater() -> ModuleType | None:
    path = updater_path()
    if path is None:
        return None
    spec = importlib.util.spec_from_file_location("streamer_caption_updater", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def updater_repository() -> str:
    root = install_root()
    for path in ([root / "updater.json"] if root else []) + [PROJECT_ROOT / "updater.example.json"]:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        repository = str(value.get("repository") or "").strip()
        if repository and not repository.startswith("GITHUB_USERNAME/"):
            return repository
    return "vtubeadmirer/streamer-caption"


def describe() -> dict[str, Any]:
    root = install_root()
    return {
        "version": app_version(),
        "managed": root is not None,
        "portable": is_portable(),
        "install_root": str(root) if root else None,
        "project_root": str(PROJECT_ROOT),
        "user_data_root": str(USER_DATA_ROOT),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "repository": updater_repository(),
        "issues_url": f"https://github.com/{updater_repository()}/issues",
    }
