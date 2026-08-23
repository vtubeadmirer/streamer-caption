from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import user_settings_bundle
from tools.bootstrap_user_data import bootstrap_user_data
from tools.make_update_zip import make_update_zip


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="streamer-settings-test-") as temp:
        user_data = Path(temp)
        bootstrap_user_data(user_data)
        original_root = user_settings_bundle.USER_DATA_ROOT
        user_settings_bundle.USER_DATA_ROOT = user_data
        try:
            settings = user_settings_bundle.export_settings()
            with zipfile.ZipFile(settings) as zf:
                names = set(zf.namelist())
        finally:
            user_settings_bundle.USER_DATA_ROOT = original_root
    assert "config.json" in names

    update = make_update_zip()
    with zipfile.ZipFile(update) as zf:
        update_names = set(zf.namelist())
    assert "config.json" not in update_names
    assert "data/slang_dict.json" not in update_names
    assert "overlay/admin.html" in update_names
    assert "engines/simulstreaming/client.py" in update_names
    assert "setup_simulstreaming_experiment.bat" in update_names
    assert not any(name.startswith("experiments/simulstreaming-runtime/") for name in update_names)
    print("update_and_settings_bundles_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
