from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT, load_config
from tools.transcribe_wav import read_wav_mono, resolve_wav_path


def analyze_wav(path: Path, threshold: float, window_ms: float = 100.0) -> dict:
    audio, sample_rate = read_wav_mono(path)
    window = max(1, int(sample_rate * window_ms / 1000.0))
    values = []
    for start in range(0, len(audio), window):
        chunk = audio[start : start + window]
        if chunk.size == 0:
            continue
        values.append(float(np.sqrt(np.mean(np.square(chunk)))))

    arr = np.asarray(values, dtype=np.float32)
    if arr.size == 0:
        return {"error": "empty audio"}

    voiced_ratio = float(np.mean(arr >= threshold))
    return {
        "path": str(path),
        "duration_sec": len(audio) / sample_rate,
        "sample_rate": sample_rate,
        "threshold": threshold,
        "rms_mean": float(np.mean(arr)),
        "rms_median": float(np.median(arr)),
        "rms_p90": float(np.percentile(arr, 90)),
        "rms_p95": float(np.percentile(arr, 95)),
        "rms_max": float(np.max(arr)),
        "voiced_ratio": voiced_ratio,
        "suggested_threshold_quiet_room": float(max(0.001, np.percentile(arr, 80) * 0.8)),
        "suggested_input_gain_for_threshold": float(
            min(50.0, max(1.0, (threshold * 1.2) / max(float(np.percentile(arr, 95)), 1e-6)))
        ),
    }


def main() -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description="Analyze WAV loudness for VAD tuning.")
    parser.add_argument("path")
    parser.add_argument("--window-ms", type=float, default=100.0)
    parser.add_argument("--threshold", type=float, default=config["audio"]["vad_energy_threshold"])
    args = parser.parse_args()

    try:
        result = analyze_wav(resolve_wav_path(args.path), args.threshold, args.window_ms)
    except Exception as exc:
        print(exc)
        return 1
    if "error" in result:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    result["path"] = args.path
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
