from __future__ import annotations

import argparse
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT, load_config
from tools.record_wav import write_wav


def make_voice_like(sample_rate: int, seconds: float, amplitude: float) -> np.ndarray:
    total = int(sample_rate * seconds)
    samples = np.zeros(total, dtype=np.float32)
    cursor = int(sample_rate * 0.5)
    rng = np.random.default_rng(7)
    syllable_lengths = [0.22, 0.18, 0.28, 0.2, 0.32, 0.24, 0.2, 0.26]
    freqs = [180, 220, 260, 210, 310, 240, 190, 280]
    index = 0
    while cursor < total - int(sample_rate * 0.5):
        duration = syllable_lengths[index % len(syllable_lengths)]
        length = int(sample_rate * duration)
        end = min(total, cursor + length)
        t = np.arange(end - cursor, dtype=np.float32) / sample_rate
        f0 = freqs[index % len(freqs)]
        tone = (
            np.sin(2 * np.pi * f0 * t)
            + 0.35 * np.sin(2 * np.pi * f0 * 2.0 * t)
            + 0.16 * np.sin(2 * np.pi * f0 * 3.0 * t)
        )
        envelope = np.sin(np.linspace(0.0, np.pi, end - cursor, dtype=np.float32))
        samples[cursor:end] = (tone * envelope * amplitude).astype(np.float32)
        pause = int(sample_rate * float(rng.uniform(0.06, 0.18)))
        cursor = end + pause
        index += 1
    noise = rng.normal(0.0, amplitude * 0.015, size=total).astype(np.float32)
    return np.clip(samples + noise, -1.0, 1.0).astype(np.float32)


def main() -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description="Create a deterministic synthetic voice-like WAV.")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--amplitude", type=float, default=0.12)
    parser.add_argument("--out", default="samples/synthetic_voice.wav")
    args = parser.parse_args()

    sample_rate = int(config["audio"]["sample_rate"])
    samples = make_voice_like(sample_rate, args.seconds, args.amplitude)
    out = PROJECT_ROOT / args.out
    write_wav(out, samples, sample_rate)
    rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    print(f"saved: {out}")
    print(f"rms={rms:.5f} peak={peak:.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
