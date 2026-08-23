from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.audio_capture import list_input_devices
from core.config import load_config
from tools.record_wav import record_mono


def probe_device(device_index: int, seconds: float, sample_rate: int, gain: float) -> dict:
    try:
        mono, used_sample_rate, warning = record_mono(device_index, seconds, sample_rate, gain)
        rms = float(np.sqrt(np.mean(np.square(mono)))) if mono.size else 0.0
        peak = float(np.max(np.abs(mono))) if mono.size else 0.0
        return {
            "device": device_index,
            "ok": True,
            "rms": rms,
            "peak": peak,
            "used_sample_rate": used_sample_rate,
            "warning": warning,
            "error": "",
        }
    except Exception as exc:
        return {
            "device": device_index,
            "ok": False,
            "rms": 0.0,
            "peak": 0.0,
            "used_sample_rate": sample_rate,
            "warning": "",
            "error": str(exc),
        }


def main() -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description="Record short snippets from input devices and compare levels.")
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--sample-rate", type=int, default=config["audio"]["sample_rate"])
    parser.add_argument("--gain", type=float, default=float(config["audio"].get("input_gain", 1.0)))
    parser.add_argument("--devices", nargs="*", type=int)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    devices = list_input_devices()
    candidates = args.devices if args.devices else [device.index for device in devices]
    device_names = {device.index: device.name for device in devices}
    results = []

    if not args.json:
        print(f"Probing {len(candidates)} input devices for {args.seconds:.1f}s each.")
        print("Speak normally while this runs.")

    for index in candidates:
        result = probe_device(index, args.seconds, args.sample_rate, args.gain)
        result["name"] = device_names.get(index, "")
        results.append(result)
        if not args.json:
            status = "OK" if result["ok"] else "FAIL"
            print(
                "[{status}] {idx}: {name} rms={rms:.5f} peak={peak:.5f} {error}".format(
                    status=status,
                    idx=index,
                    name=result["name"],
                    rms=result["rms"],
                    peak=result["peak"],
                    error=result["error"] or result.get("warning", ""),
                )
            )

    best = max((row for row in results if row["ok"]), key=lambda row: row["rms"], default=None)
    payload = {"results": results, "best": best}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif best:
        print(f"Best by RMS: {best['device']} {best['name']} rms={best['rms']:.5f}")
    return 0 if any(row["ok"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
