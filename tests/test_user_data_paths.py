"""Relative user-data paths in config.json resolve under the user-data folder, never the code folder.

An installed copy runs its code from versions/<ver>/ (replaced by every update) and keeps the
streamer's files in user-data/ (STREAMER_CAPTION_USER_DATA). A source checkout has no such
variable and keeps both in the project root.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import (  # noqa: E402
    CONFIG_PATH,
    CONFIG_SCHEMA,
    DEFAULTS,
    PROJECT_ROOT,
    USER_DATA_ROOT,
    load_config,
    save_config,
    user_path,
)
from tools.bootstrap_user_data import bootstrap_user_data  # noqa: E402

DEFAULT_PATHS = {
    ("defense", "blocklist_path"): "data/blocklist.txt",
    ("defense", "whitelist_path"): "data/whitelist.txt",
    ("slang", "dict_path"): "data/slang_dict.json",
    ("archive", "db_path"): "data/transcripts.sqlite3",
    ("log", "dir"): "logs",
}
OLD_SETUP = r"C:\Users\someone\OneDrive\Desktop\StreamerCaption\user-data"  # where an older setup ran
CUSTOM_DB = r"D:\captions\mine.sqlite3"
CHILD = "--child"


def child() -> int:
    """Report where a fresh interpreter puts the default config's files."""
    report = {
        "user_data_root": str(USER_DATA_ROOT),
        "config_path": str(CONFIG_PATH),
        "defaults": {f"{s}.{k}": str(user_path(DEFAULTS[s][k])) for s, k in DEFAULT_PATHS},
    }
    if USER_DATA_ROOT != PROJECT_ROOT:  # the consumers below write files: only into a temp user-data
        from core.archive_maintenance import create_archive_backup
        from core.events import CaptionEvent
        from core.log_writer import JsonlLogWriter
        from core.monitoring import MemoryMonitor
        from core.transcript_archive import TranscriptArchive
        from features.defense import DefenseFilter
        from features.slang import SlangDictionary
        from tools.make_support_bundle import latest_log_dirs
        from tools.summarize_session import latest_session_jsonl

        (USER_DATA_ROOT / "data").mkdir(parents=True, exist_ok=True)
        (USER_DATA_ROOT / "data" / "blocklist.txt").write_text("blocked-term\n", encoding="utf-8")
        (USER_DATA_ROOT / "data" / "slang_dict.json").write_text('{"src": "dst"}', encoding="utf-8")
        config = load_config()  # no config.json: the shipped defaults
        writer = JsonlLogWriter(config["log"]["dir"])
        writer.append_event(CaptionEvent(event_type="engine_status", text="started"))
        archive = TranscriptArchive.from_config(config)
        report["files"] = {
            "archive": str(archive.path),
            "log_session": str(writer.session_dir),
            "latest_log_dir": str(latest_log_dirs()[0]),
            "latest_session": str(latest_session_jsonl()),
            "memory": str(MemoryMonitor(config).path),
            "backup": str(create_archive_backup(archive)),
        }
        report["blocklist"] = DefenseFilter(config).blocklist
        report["slang"] = SlangDictionary(config).entries
    print(json.dumps(report))
    return 0


def run_child(user_data: Path | None) -> dict:
    env = {key: value for key, value in os.environ.items() if key != "STREAMER_CAPTION_USER_DATA"}
    if user_data is not None:
        env["STREAMER_CAPTION_USER_DATA"] = str(user_data)
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), CHILD],
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def check_user_path() -> None:
    root = Path(r"C:\StreamerCaption\user-data")
    assert user_path("data/transcripts.sqlite3", root=root) == root / "data" / "transcripts.sqlite3"
    assert user_path(CUSTOM_DB, root=root) == Path(CUSTOM_DB), "absolute paths are used as they are"
    assert user_path(Path(CUSTOM_DB)) == Path(CUSTOM_DB)
    for (section, key), relative in DEFAULT_PATHS.items():
        assert DEFAULTS[section][key] == relative, "shipped defaults must stay relative to be relocatable"

    # Source checkout: user-data is the project root, so nothing moves for developers.
    checkout = run_child(None)
    assert Path(checkout["user_data_root"]) == PROJECT_ROOT, checkout
    assert Path(checkout["config_path"]) == PROJECT_ROOT / "config.json", checkout
    for (section, key), relative in DEFAULT_PATHS.items():
        assert Path(checkout["defaults"][f"{section}.{key}"]) == PROJECT_ROOT / relative, checkout
    assert "files" not in checkout

    # Installed copy: every default file and folder lands in user-data, none in the code folder.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        user_data = Path(directory).resolve() / "user-data"
        user_data.mkdir()
        installed = run_child(user_data)
        assert Path(installed["user_data_root"]) == user_data, installed
        assert Path(installed["config_path"]) == user_data / "config.json", installed
        for (section, key), relative in DEFAULT_PATHS.items():
            assert Path(installed["defaults"][f"{section}.{key}"]) == user_data / relative, installed
        files = {name: Path(value) for name, value in installed["files"].items()}
        assert files["archive"] == user_data / "data" / "transcripts.sqlite3", files
        assert files["log_session"].parent == user_data / "logs", files
        assert files["latest_log_dir"] == files["log_session"], files
        assert files["latest_session"] == files["log_session"] / "session.jsonl", files
        assert files["memory"].parent.parent == user_data / "logs", files
        assert files["backup"].parent == user_data / "reports" / "archive_backups", files
        assert installed["blocklist"] == ["blocked-term"], "the streamer's blocklist must apply"
        assert installed["slang"] == {"src": "dst"}, installed["slang"]


def check_migration() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cfg = Path(directory) / "config.json"
        # Older setups wrote absolute ".../user-data/<default>" paths; they become relative once.
        cfg.write_text(json.dumps({
            "config_schema": 2,
            "defense": {
                "blocklist_path": OLD_SETUP + r"\data\blocklist.txt",
                "whitelist_path": OLD_SETUP + r"\data\whitelist.txt",
            },
            "slang": {"dict_path": "C:/Moved/USER-DATA/data/slang_dict.json"},  # any separator or case
            "archive": {"db_path": CUSTOM_DB},  # a location the streamer chose stays
            "log": {"dir": OLD_SETUP + r"\logs"},
        }), encoding="utf-8")
        migrated = load_config(cfg)
        assert CONFIG_SCHEMA == 4 and migrated["config_schema"] == CONFIG_SCHEMA, migrated["config_schema"]
        for (section, key), relative in DEFAULT_PATHS.items():
            expected = CUSTOM_DB if (section, key) == ("archive", "db_path") else relative
            assert migrated[section][key] == expected, (section, key, migrated[section][key])
        assert json.loads(cfg.read_text(encoding="utf-8"))["log"]["dir"] == OLD_SETUP + r"\logs", "load does not write"
        save_config(migrated, cfg)
        stored = json.loads(cfg.read_text(encoding="utf-8"))
        assert stored["config_schema"] == CONFIG_SCHEMA and stored["log"]["dir"] == "logs", stored["log"]
        assert stored["archive"]["db_path"] == CUSTOM_DB, stored["archive"]

        # One-time: at schema 3 an absolute path is the streamer's own choice.
        cfg.write_text(json.dumps({"config_schema": 3, "log": {"dir": OLD_SETUP + r"\logs"}}), encoding="utf-8")
        assert load_config(cfg)["log"]["dir"] == OLD_SETUP + r"\logs"

        # Installs that stored the old full-default snapshot (schema 1, energy VAD) move to Silero;
        # an explicit schema-2 choice of energy is respected.
        assert DEFAULTS["vad"]["engine"] == "silero"
        cfg.write_text(json.dumps({"vad": {"engine": "energy"}}), encoding="utf-8")
        assert load_config(cfg)["vad"]["engine"] == "silero"
        cfg.write_text(json.dumps({"config_schema": 2, "vad": {"engine": "energy"}}), encoding="utf-8")
        assert load_config(cfg)["vad"]["engine"] == "energy"
        assert load_config(cfg)["config_schema"] == CONFIG_SCHEMA

    with tempfile.TemporaryDirectory() as directory:
        user_data = Path(directory) / "user-data"
        bootstrap_user_data(user_data)
        fresh = json.loads((user_data / "config.json").read_text(encoding="utf-8"))
        for (section, key), relative in DEFAULT_PATHS.items():
            assert fresh[section][key] == relative, (section, key, fresh[section][key])
        # Re-running setup over an older config (schema 1 snapshot, absolute paths, BOM) upgrades it
        # instead of stamping the new schema over unmigrated values.
        (user_data / "config.json").write_text(json.dumps({
            "vad": {"engine": "energy"},
            "archive": {"db_path": OLD_SETUP + r"\data\transcripts.sqlite3"},
            "log": {"dir": OLD_SETUP + r"\logs"},
        }), encoding="utf-8-sig")
        bootstrap_user_data(user_data)
        upgraded = json.loads((user_data / "config.json").read_text(encoding="utf-8"))
        assert upgraded["config_schema"] == CONFIG_SCHEMA and upgraded["vad"]["engine"] == "silero", upgraded
        assert upgraded["archive"]["db_path"] == "data/transcripts.sqlite3", upgraded["archive"]
        assert upgraded["log"]["dir"] == "logs", upgraded["log"]


def main() -> int:
    if CHILD in sys.argv:
        return child()
    check_user_path()
    check_migration()
    print("user_data_paths_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
