from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.profiles import apply_profile


PROFILE_LABELS = {
    "cpu_compatible": "CPU compatible",
    "gpu_8gb_safe": "Light",
    "gpu_12_16gb_balanced": "Balanced",
    "gpu_20gb_quality": "High quality",
}
PREFERENCE_MAP = {
    "light": "gpu_8gb_safe",
    "balanced": "gpu_12_16gb_balanced",
    "quality": "gpu_20gb_quality",
}


def detect_gpu() -> tuple[str, int | None]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return "not detected", None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return "not detected", None
    first = lines[0]
    if "," not in first:
        return first, None
    name, memory = first.rsplit(",", 1)
    try:
        memory_mb = int(memory.strip())
    except ValueError:
        memory_mb = None
    return name.strip(), memory_mb


def choose_profile(name: str, memory_mb: int | None) -> tuple[str, str]:
    if memory_mb is None:
        return "cpu_compatible", "NVIDIA GPU or VRAM could not be detected."
    if memory_mb < 12000:
        return "gpu_8gb_safe", "VRAM is below 12GB."
    if memory_mb < 20000:
        return "gpu_12_16gb_balanced", "VRAM is between 12GB and 19GB."
    return "gpu_20gb_quality", "VRAM is 20GB or more."


def recommendation(preference: str) -> dict[str, object]:
    name, memory_mb = detect_gpu()
    if preference == "auto":
        profile, reason = choose_profile(name, memory_mb)
    else:
        profile = PREFERENCE_MAP[preference]
        reason = f"The user selected the {preference} performance level."
    return {
        "gpu": name,
        "vram_mb": memory_mb,
        "profile": profile,
        "label": PROFILE_LABELS[profile],
        "reason": reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect GPU capacity and apply a caption profile.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--preference",
        choices=("auto", "light", "balanced", "quality"),
        default="auto",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = recommendation(args.preference)
    if not args.dry_run:
        config = apply_profile(str(result["profile"]), keep_audio_tuning=True)
        result["applied"] = True
        result["model"] = config.get("whisper", {}).get("model")
        result["compute_type"] = config.get("whisper", {}).get("compute_type")
    else:
        result["applied"] = False

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"detected_gpu: {result['gpu']}")
        print(f"detected_vram: {result['vram_mb'] if result['vram_mb'] is not None else 'unknown'} MB")
        print(f"recommended_profile: {result['profile']} ({result['label']})")
        print(f"reason: {result['reason']}")
        print(f"applied: {result['applied']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
