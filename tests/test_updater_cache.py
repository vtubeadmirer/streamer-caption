from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launcher.updater import check_for_update


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="streamer-updater-cache-") as temp:
        install_root = Path(temp)
        write_json(
            install_root / "state.json",
            {"current_version": "1.0.0", "previous_version": None},
        )
        write_json(
            install_root / "updater.json",
            {
                "repository": "vtubeadmirer/streamer-caption",
                "allow_prerelease": False,
                "check_interval_hours": 24,
            },
        )
        resolved = {
            "manifest": {
                "version": "1.0.1",
                "asset_name": "streamer-caption-1.0.1.zip",
                "sha256": "0" * 64,
                "release_notes": "Cache test",
            },
            "asset_url": "https://example.invalid/streamer-caption-1.0.1.zip",
        }
        start = datetime(2026, 8, 24, tzinfo=timezone.utc)
        with patch("launcher.updater._resolve_github_release", return_value=resolved) as resolve:
            first = check_for_update(install_root, now=start)
            assert first["available"] is True
            assert first["cached"] is False
            assert resolve.call_count == 1

            cached = check_for_update(install_root, now=start + timedelta(hours=1))
            assert cached["available"] is True
            assert cached["cached"] is True
            assert resolve.call_count == 1

            forced = check_for_update(
                install_root,
                force=True,
                now=start + timedelta(hours=2),
            )
            assert forced["cached"] is False
            assert resolve.call_count == 2

            expired = check_for_update(install_root, now=start + timedelta(hours=27))
            assert expired["cached"] is False
            assert resolve.call_count == 3

            write_json(
                install_root / "state.json",
                {"current_version": "1.0.1", "previous_version": "1.0.0"},
            )
            new_version = check_for_update(install_root, now=start + timedelta(hours=28))
            assert new_version["available"] is False
            assert new_version["cached"] is False
            assert resolve.call_count == 4

            (install_root / "update-check-cache.json").write_text("{", encoding="utf-8")
            malformed = check_for_update(install_root, now=start + timedelta(hours=29))
            assert malformed["cached"] is False
            assert resolve.call_count == 5

    print("updater_cache_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
