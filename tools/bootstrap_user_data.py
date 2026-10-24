from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config, save_config


PERSONAL_FILES = (
    "config.json",
    "data/slang_dict.json",
    "data/whitelist.txt",
    "data/blocklist.txt",
)


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
    # Through load_config so the schema migrations (e.g. older setups' absolute paths) run
    # before save_config stamps the current config_schema.
    save_config(load_config(config_path), config_path)

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
