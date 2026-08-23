from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT, load_config


def write_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(pcm.tobytes())


def resample_linear(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate or samples.size == 0:
        return samples.astype(np.float32)
    duration = samples.size / source_rate
    target_count = max(1, int(round(duration * target_rate)))
    source_x = np.linspace(0.0, duration, num=samples.size, endpoint=False)
    target_x = np.linspace(0.0, duration, num=target_count, endpoint=False)
    return np.interp(target_x, source_x, samples).astype(np.float32)


def apply_gain(samples: np.ndarray, gain: float) -> np.ndarray:
    if gain == 1.0:
        return samples.astype(np.float32)
    return np.clip(samples * gain, -1.0, 1.0).astype(np.float32)


def record_mono(device: int, seconds: float, sample_rate: int, gain: float = 1.0) -> tuple[np.ndarray, int, str]:
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError("sounddevice is not installed. Use .venv and install requirements.") from exc

    sample_count = int(seconds * sample_rate)
    try:
        audio = sd.rec(
            sample_count,
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            device=device,
        )
        sd.wait()
        mono = np.asarray(audio, dtype=np.float32).reshape(-1)
        return apply_gain(mono, gain), sample_rate, ""
    except Exception as exc:
        devices = sd.query_devices()
        default_rate = int(float(devices[device]["default_samplerate"]))
        sample_count = int(seconds * default_rate)
        audio = sd.rec(
            sample_count,
            samplerate=default_rate,
            channels=1,
            dtype="float32",
            device=device,
        )
        sd.wait()
        mono = np.asarray(audio, dtype=np.float32).reshape(-1)
        return (
            apply_gain(resample_linear(mono, default_rate, sample_rate), gain),
            sample_rate,
            f"requested sample_rate failed ({exc}); recorded at default {default_rate}Hz and resampled",
        )


def main() -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description="Record a mono WAV test clip.")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--device", type=int, default=config["audio"]["device"])
    parser.add_argument("--sample-rate", type=int, default=config["audio"]["sample_rate"])
    parser.add_argument("--gain", type=float, default=float(config["audio"].get("input_gain", 1.0)))
    parser.add_argument("--out", default="samples/test_clip.wav")
    args = parser.parse_args()

    print(
        "Recording {seconds:.1f}s from device={device} to {path}".format(
            seconds=args.seconds,
            device=args.device,
            path=args.out,
        )
    )
    try:
        mono, saved_rate, warning = record_mono(args.device, args.seconds, args.sample_rate, args.gain)
    except Exception as exc:
        print(exc)
        return 2
    if warning:
        print("warning:", warning)
    out_path = PROJECT_ROOT / args.out
    write_wav(out_path, mono, saved_rate)
    rms = float(np.sqrt(np.mean(np.square(mono)))) if mono.size else 0.0
    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    print(f"saved: {out_path}")
    print(f"rms={rms:.5f} peak={peak:.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
