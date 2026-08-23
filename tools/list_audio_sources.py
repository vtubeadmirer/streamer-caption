from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.audio_capture import list_input_devices
from core.audio_sources import list_system_audio_devices


def main() -> int:
    print("Microphone inputs")
    try:
        microphones = list_input_devices()
    except Exception as exc:
        print(f"  unavailable: {exc}")
    else:
        if not microphones:
            print("  (none)")
        for device in microphones:
            print(
                f"  {device.index}: {device.name} "
                f"(channels={device.channels}, "
                f"default_rate={device.default_sample_rate:.0f})"
            )

    print()
    print("PC playback loopback inputs")
    try:
        loopbacks = list_system_audio_devices()
    except Exception as exc:
        print(f"  unavailable: {exc}")
        return 1
    if not loopbacks:
        print("  (none)")
    for device in loopbacks:
        default = " [default]" if device.is_default else ""
        print(f"  {device.id}: {device.name}{default}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())