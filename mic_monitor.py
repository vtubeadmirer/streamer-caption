from __future__ import annotations

import argparse
import queue
import sys
import time

from core.audio_capture import calculate_rms, list_input_devices
from core.config import load_config


def _bar(value: float, width: int = 40) -> str:
    filled = min(width, max(0, int(value * width)))
    return "#" * filled + "-" * (width - filled)


def list_devices() -> int:
    for device in list_input_devices():
        print(
            f"{device.index}: {device.name} "
            f"(channels={device.channels}, default_sr={device.default_sample_rate:.0f})"
        )
    return 0


def monitor(
    device: int | None,
    sample_rate: int,
    block_size: int,
    duration_seconds: float | None,
) -> int:
    try:
        import sounddevice as sd
    except ImportError:
        print("sounddevice is not installed. Run: pip install -r requirements.txt")
        return 2

    levels: queue.Queue[float] = queue.Queue(maxsize=8)

    def callback(indata, frames, time_info, status) -> None:
        if status:
            print(f"\nAudio status: {status}", file=sys.stderr)
        rms = calculate_rms(indata)
        try:
            levels.put_nowait(rms)
        except queue.Full:
            try:
                levels.get_nowait()
            except queue.Empty:
                pass
            levels.put_nowait(rms)

    print("Mic monitor running. Speak into the mic. Press Ctrl+C to stop.")
    print(f"device={device}, sample_rate={sample_rate}, block_size={block_size}")
    started = time.monotonic()
    with sd.InputStream(
        device=device,
        channels=1,
        samplerate=sample_rate,
        blocksize=block_size,
        dtype="float32",
        callback=callback,
    ):
        try:
            while True:
                try:
                    rms = levels.get(timeout=0.5)
                except queue.Empty:
                    rms = 0.0
                scaled = min(1.0, rms * 12.0)
                print(f"\rRMS {rms:0.5f} [{_bar(scaled)}]", end="", flush=True)
                if duration_seconds is not None and time.monotonic() - started >= duration_seconds:
                    print()
                    return 0
                time.sleep(0.05)
        except KeyboardInterrupt:
            print()
            return 0


def main() -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description="Check microphone input without OBS.")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--device", type=int, default=config["audio"]["device"])
    parser.add_argument("--sample-rate", type=int, default=config["audio"]["sample_rate"])
    parser.add_argument("--block-size", type=int, default=config["audio"]["block_size"])
    parser.add_argument("--duration-seconds", type=float)
    args = parser.parse_args()

    if args.list_devices:
        return list_devices()
    return monitor(args.device, args.sample_rate, args.block_size, args.duration_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
