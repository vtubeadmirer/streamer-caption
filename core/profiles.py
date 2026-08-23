from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.config import PROJECT_ROOT, deep_merge, load_config, save_config

PROFILE_DIR = PROJECT_ROOT / "data" / "profiles"
AUDIO_TUNING_KEYS = {
    "source",
    "system_device",
    "device",
    "vad_energy_threshold",
    "input_gain",
    "min_segment_peak_rms",
    "silence_duration_sec",
    "min_segment_sec",
}


def list_profiles() -> list[str]:
    if not PROFILE_DIR.exists():
        return []
    return sorted(path.stem for path in PROFILE_DIR.glob("*.json"))


def load_profile(name: str) -> dict[str, Any]:
    path = PROFILE_DIR / f"{name}.json"
    if not path.exists():
        available = ", ".join(list_profiles()) or "(none)"
        raise FileNotFoundError(f"profile not found: {name}. available: {available}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def apply_profile(name: str, keep_audio_tuning: bool = True) -> dict[str, Any]:
    current = load_config()
    profile = load_profile(name)
    merged = deep_merge(current, profile)
    if keep_audio_tuning:
        merged.setdefault("audio", {})
        for key in AUDIO_TUNING_KEYS:
            if key in current.get("audio", {}):
                merged["audio"][key] = current["audio"][key]
    save_config(merged)
    return merged
