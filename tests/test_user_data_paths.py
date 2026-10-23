"""A moved install (e.g. out of OneDrive) must keep using its own user-data files, not the old absolute paths."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import DEFAULTS, _anchor_user_data_paths, deep_merge  # noqa: E402


def main() -> int:
    new_root = Path(r"C:\StreamerCaption\user-data")
    old = r"C:\Users\someone\OneDrive\Desktop\StreamerCaption\user-data"
    config = deep_merge(DEFAULTS, {
        "defense": {"blocklist_path": old + r"\data\blocklist.txt", "whitelist_path": old + r"\data\whitelist.txt"},
        "slang": {"dict_path": old + r"\data\slang_dict.json"},
        "archive": {"db_path": r"D:\captions\mine.sqlite3"},  # custom location: must stay
        "log": {"dir": old + r"\logs"},
    })
    moved = _anchor_user_data_paths(config, new_root)
    assert moved["defense"]["blocklist_path"] == str(new_root / "data/blocklist.txt"), moved["defense"]
    assert moved["defense"]["whitelist_path"] == str(new_root / "data/whitelist.txt"), moved["defense"]
    assert moved["slang"]["dict_path"] == str(new_root / "data/slang_dict.json"), moved["slang"]
    assert moved["log"]["dir"] == str(new_root / "logs"), moved["log"]
    assert moved["archive"]["db_path"] == r"D:\captions\mine.sqlite3", "custom absolute paths are left alone"

    # Already under the current root: unchanged. Relative defaults: unchanged.
    same = _anchor_user_data_paths(deep_merge(DEFAULTS, {"log": {"dir": str(new_root / "logs")}}), new_root)
    assert same["log"]["dir"] == str(new_root / "logs") and same["archive"]["db_path"] == "data/transcripts.sqlite3", same

    # Source checkout (user-data == project root): nothing is touched.
    from core.config import PROJECT_ROOT
    untouched = _anchor_user_data_paths(deep_merge(DEFAULTS, {"log": {"dir": old + r"\logs"}}), PROJECT_ROOT)
    assert untouched["log"]["dir"] == old + r"\logs"
    assert DEFAULTS["vad"]["engine"] == "silero"

    # Installs that stored the old full-default snapshot (schema 1, energy VAD) move to Silero;
    # an explicit schema-2 choice of energy is respected.
    import json, tempfile
    from core.config import load_config
    with tempfile.TemporaryDirectory() as directory:
        cfg = Path(directory) / "config.json"
        cfg.write_text(json.dumps({"vad": {"engine": "energy"}}), encoding="utf-8")
        assert load_config(cfg)["vad"]["engine"] == "silero"
        cfg.write_text(json.dumps({"config_schema": 2, "vad": {"engine": "energy"}}), encoding="utf-8")
        assert load_config(cfg)["vad"]["engine"] == "energy"
        assert load_config(cfg)["config_schema"] == 2
    print("user_data_paths_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
