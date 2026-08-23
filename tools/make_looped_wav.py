from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT
from tools.record_wav import write_wav
from tools.transcribe_wav import read_wav_mono, resolve_wav_path


def loop_wav(source: Path, out: Path, seconds: float) -> dict:
    audio, sample_rate = read_wav_mono(source)
    if len(audio) == 0:
        raise ValueError(f"source WAV is empty: {source}")
    target_samples = max(1, int(round(seconds * sample_rate)))
    repeats = int(np.ceil(target_samples / len(audio)))
    looped = np.tile(audio, repeats)[:target_samples].astype(np.float32)
    write_wav(out, looped, sample_rate)
    return {
        "source": str(source),
        "out": str(out),
        "sample_rate": sample_rate,
        "duration_sec": len(looped) / sample_rate,
        "source_duration_sec": len(audio) / sample_rate,
        "repeats": repeats,
        "rms": float(np.sqrt(np.mean(looped * looped))),
        "peak": float(np.max(np.abs(looped))),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a longer WAV by looping a shorter real voice clip.")
    parser.add_argument("source")
    parser.add_argument("--seconds", type=float, default=600.0)
    parser.add_argument("--out", default="samples/test_clip_loop_10min.wav")
    args = parser.parse_args()
    try:
        result = loop_wav(
            resolve_wav_path(args.source),
            (PROJECT_ROOT / args.out).resolve(),
            args.seconds,
        )
    except Exception as exc:
        print(exc)
        return 1
    print(
        "looped_wav: duration={duration:.2f}s repeats={repeats} rms={rms:.5f} peak={peak:.5f}".format(
            duration=result["duration_sec"],
            repeats=result["repeats"],
            rms=result["rms"],
            peak=result["peak"],
        )
    )
    print(f"saved: {result['out']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
