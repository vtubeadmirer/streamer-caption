from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT, deep_merge, load_config, save_config

PRESET_DIR = PROJECT_ROOT / "data" / "presets"


def list_presets() -> list[str]:
    return sorted(path.stem for path in PRESET_DIR.glob("*.json")) if PRESET_DIR.exists() else []


def apply_preset(name: str, keep_device: bool = True) -> dict:
    path = PRESET_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"preset not found: {name}. available: {', '.join(list_presets())}")
    current = load_config()
    with path.open("r", encoding="utf-8") as f:
        preset = json.load(f)
    merged = deep_merge(current, preset)
    if keep_device:
        merged["audio"]["device"] = current["audio"]["device"]
        merged["audio"]["vad_energy_threshold"] = current["audio"]["vad_energy_threshold"]
    save_config(merged)
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a locked preset.")
    parser.add_argument("preset", nargs="?")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--replace-device", action="store_true")
    args = parser.parse_args()
    if args.list or not args.preset:
        for name in list_presets():
            print(name)
        return 0
    config = apply_preset(args.preset, keep_device=not args.replace_device)
    print(
        "applied preset={preset} profile={profile} model={model} compute={compute} vad={vad}".format(
            preset=args.preset,
            profile=config["app"].get("profile"),
            model=config["whisper"]["model"],
            compute=config["whisper"]["compute_type"],
            vad=config["vad"]["engine"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
