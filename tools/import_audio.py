from __future__ import annotations

import argparse
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT, load_config
from tools.record_wav import resample_linear, write_wav


def resolve_input(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / value
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"audio file not found: {path}")
    return path


def decode_audio(path: Path, target_rate: int) -> tuple[np.ndarray, int, dict]:
    try:
        import av
    except ImportError as exc:
        raise RuntimeError("PyAV is required to import non-WAV audio. Install package: av") from exc

    chunks: list[np.ndarray] = []
    source_rate = target_rate
    source_channels = 0
    with av.open(str(path)) as container:
        stream = next((item for item in container.streams if item.type == "audio"), None)
        if stream is None:
            raise ValueError(f"no audio stream found: {path}")
        for frame in container.decode(stream):
            source_rate = int(frame.sample_rate or target_rate)
            array = frame.to_ndarray()
            data = np.asarray(array, dtype=np.float32)
            if data.ndim == 2:
                source_channels = max(source_channels, data.shape[0])
                data = data.mean(axis=0)
            else:
                source_channels = max(source_channels, 1)
                data = data.reshape(-1)
            # PyAV returns integer arrays for many PCM formats and float arrays for others.
            if array.dtype.kind in {"i", "u"}:
                max_value = float(np.iinfo(array.dtype).max)
                if max_value > 0:
                    data = data / max_value
            chunks.append(data.astype(np.float32))
    if not chunks:
        raise ValueError(f"audio stream is empty: {path}")
    mono = np.concatenate(chunks, axis=0).astype(np.float32)
    mono = resample_linear(mono, source_rate, target_rate)
    mono = np.clip(mono, -1.0, 1.0).astype(np.float32)
    return mono, target_rate, {"source_rate": source_rate, "source_channels": source_channels}


def import_audio(path: Path, out: Path, target_rate: int) -> dict:
    samples, sample_rate, meta = decode_audio(path, target_rate)
    write_wav(out, samples, sample_rate)
    rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    return {
        "source": str(path),
        "out": str(out),
        "sample_rate": sample_rate,
        "duration_sec": len(samples) / sample_rate if sample_rate else 0.0,
        "rms": rms,
        "peak": peak,
        **meta,
    }


def main() -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description="Import any common audio file as 16kHz mono WAV.")
    parser.add_argument("path")
    parser.add_argument("--out", default="samples/test_clip.wav")
    parser.add_argument("--sample-rate", type=int, default=int(config["audio"]["sample_rate"]))
    args = parser.parse_args()

    try:
        source = resolve_input(args.path)
        out = (PROJECT_ROOT / args.out).resolve()
        result = import_audio(source, out, args.sample_rate)
    except Exception as exc:
        print(exc)
        return 1

    print(f"imported: {result['source']}")
    print(f"saved: {result['out']}")
    print(
        "duration={duration:.2f}s sample_rate={rate} rms={rms:.5f} peak={peak:.5f}".format(
            duration=result["duration_sec"],
            rate=result["sample_rate"],
            rms=result["rms"],
            peak=result["peak"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
