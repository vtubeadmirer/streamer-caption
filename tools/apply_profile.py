from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.profiles import apply_profile, list_profiles


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a streamer-caption-v2 hardware profile.")
    parser.add_argument("profile", nargs="?", help="profile name")
    parser.add_argument("--list", action="store_true", help="list available profiles")
    parser.add_argument(
        "--replace-audio-tuning",
        action="store_true",
        help="allow the profile to replace tuned mic/VAD values",
    )
    args = parser.parse_args()

    if args.list or not args.profile:
        for profile in list_profiles():
            print(profile)
        return 0

    config = apply_profile(args.profile, keep_audio_tuning=not args.replace_audio_tuning)
    print(
        "applied profile={profile} model={model} compute={compute} max_segment={max_segment}s".format(
            profile=config["app"].get("profile", args.profile),
            model=config["whisper"]["model"],
            compute=config["whisper"]["compute_type"],
            max_segment=config["audio"]["max_segment_sec"],
        )
    )
    print(
        "kept audio tuning: device={device}, vad={vad}, gain={gain}, min_peak={min_peak}".format(
            device=config["audio"].get("device"),
            vad=config["audio"].get("vad_energy_threshold"),
            gain=config["audio"].get("input_gain"),
            min_peak=config["audio"].get("min_segment_peak_rms"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
