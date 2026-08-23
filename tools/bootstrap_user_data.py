from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import DEFAULTS, deep_merge, save_config


PERSONAL_FILES = (
    "config.json",
    "data/slang_dict.json",
    "data/whitelist.txt",
    "data/blocklist.txt",
)


def _absolute_user_paths(config: dict, user_data: Path) -> dict:
    result = copy.deepcopy(config)
    result["defense"]["blocklist_path"] = str(user_data / "data" / "blocklist.txt")
    result["defense"]["whitelist_path"] = str(user_data / "data" / "whitelist.txt")
    result["slang"]["dict_path"] = str(user_data / "data" / "slang_dict.json")
    result["archive"]["db_path"] = str(user_data / "data" / "transcripts.sqlite3")
    result["log"]["dir"] = str(user_data / "logs")
    return result


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def bootstrap_user_data(user_data: Path, migrate_from: Path | None = None) -> dict:
    user_data = user_data.expanduser().resolve()
    for relative in ("data", "logs", "reports", "samples", "backups"):
        (user_data / relative).mkdir(parents=True, exist_ok=True)

    migrated: list[str] = []
    if migrate_from is not None:
        migrate_from = migrate_from.expanduser().resolve()
        for relative in PERSONAL_FILES:
            source = migrate_from / relative
            target = user_data / relative
            if source.exists() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                migrated.append(relative)

    config_path = user_data / "config.json"
    if config_path.exists():
        config = deep_merge(DEFAULTS, _read_json(config_path))
    else:
        config = copy.deepcopy(DEFAULTS)
    config = _absolute_user_paths(config, user_data)
    save_config(config, config_path)

    defaults = {
        user_data / "data" / "slang_dict.json": "{}\n",
        user_data / "data" / "whitelist.txt": "# One protected term per line.\n",
        user_data / "data" / "blocklist.txt": "# One blocked phrase per line.\n",
    }
    for path, content in defaults.items():
        if not path.exists():
            path.write_text(content, encoding="utf-8", newline="\n")

    return {
        "user_data": str(user_data),
        "config": str(config_path),
        "migrated": migrated,
        "created": sorted(str(path.relative_to(user_data)) for path in defaults if path.exists()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create persistent Streamer Caption user data.")
    parser.add_argument("--user-data", required=True, type=Path)
    parser.add_argument("--migrate-from", type=Path)
    args = parser.parse_args()
    result = bootstrap_user_data(args.user_data, args.migrate_from)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
