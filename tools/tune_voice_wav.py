from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.audio_capture import AudioChunk, calculate_rms
from core.config import PROJECT_ROOT, load_config, save_config
from core.verdicts import judge_voice_tuning
from core.vad import SileroVoiceDetector
from tools.benchmark_streaming_wav import make_chunks
from tools.transcribe_wav import read_wav_mono, resolve_wav_path


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    arr = sorted(values)
    index = min(len(arr) - 1, max(0, int(round((pct / 100.0) * (len(arr) - 1)))))
    return float(arr[index])


def analyze_energy(chunks: list[AudioChunk], current_threshold: float) -> dict[str, Any]:
    rms_values = [chunk.rms for chunk in chunks]
    suggested = max(0.003, min(0.08, percentile(rms_values, 70) * 0.8))
    voiced = [value for value in rms_values if value >= current_threshold]
    return {
        "rms_min": min(rms_values) if rms_values else 0.0,
        "rms_median": statistics.median(rms_values) if rms_values else 0.0,
        "rms_p70": percentile(rms_values, 70),
        "rms_p90": percentile(rms_values, 90),
        "rms_max": max(rms_values) if rms_values else 0.0,
        "current_threshold": current_threshold,
        "current_voiced_ratio": len(voiced) / len(rms_values) if rms_values else 0.0,
        "suggested_threshold": suggested,
        "suggested_min_segment_peak_rms": max(0.006, min(0.04, percentile(rms_values, 90) * 0.7)),
    }


def analyze_silero(chunks: list[AudioChunk], config: dict[str, Any]) -> dict[str, Any]:
    detector = SileroVoiceDetector(config)
    if not detector.available:
        return {
            "available": False,
            "fallback_reason": detector.fallback_reason,
        }
    scores = [detector.decide(chunk).score for chunk in chunks]
    threshold = float(config["vad"].get("speech_threshold", 0.5))
    voiced = [score for score in scores if score >= threshold]
    return {
        "available": True,
        "score_min": min(scores) if scores else 0.0,
        "score_median": statistics.median(scores) if scores else 0.0,
        "score_p70": percentile(scores, 70),
        "score_p90": percentile(scores, 90),
        "score_max": max(scores) if scores else 0.0,
        "current_threshold": threshold,
        "current_voiced_ratio": len(voiced) / len(scores) if scores else 0.0,
        "suggested_threshold": 0.5,
    }


def tune(path: Path, apply: bool = False, config_override: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config_override or load_config()
    audio, sample_rate = read_wav_mono(path)
    chunks = make_chunks(audio, sample_rate, int(config["audio"]["block_size"]))
    energy = analyze_energy(chunks, float(config["audio"]["vad_energy_threshold"]))
    silero = analyze_silero(chunks, config)
    recommended_engine = "silero" if silero.get("available") else "energy"
    report = {
        "source": str(path),
        "duration_sec": len(audio) / sample_rate if sample_rate else 0.0,
        "sample_rate": sample_rate,
        "chunk_count": len(chunks),
        "energy": energy,
        "silero": silero,
        "recommendation": {
            "vad_engine": recommended_engine,
            "vad_energy_threshold": energy["suggested_threshold"],
            "min_segment_peak_rms": energy["suggested_min_segment_peak_rms"],
            "speech_threshold": silero.get("suggested_threshold", config["vad"].get("speech_threshold", 0.5)),
        },
    }
    report["verdict"] = judge_voice_tuning(report)
    if apply:
        config["vad"]["engine"] = recommended_engine
        config["vad"]["speech_threshold"] = report["recommendation"]["speech_threshold"]
        config["audio"]["vad_energy_threshold"] = report["recommendation"]["vad_energy_threshold"]
        config["audio"]["min_segment_peak_rms"] = report["recommendation"]["min_segment_peak_rms"]
        save_config(config)
        report["applied"] = True
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze a WAV clip and recommend VAD tuning.")
    parser.add_argument("path")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--vad-engine", choices=["energy", "silero"])
    parser.add_argument("--out-dir", default="reports")
    args = parser.parse_args()

    try:
        wav_path = resolve_wav_path(args.path)
        if args.vad_engine:
            config = load_config()
            config["vad"]["engine"] = args.vad_engine
        else:
            config = None
        report = tune(wav_path, apply=args.apply, config_override=config)
    except Exception as exc:
        print(exc)
        return 1

    out_dir = PROJECT_ROOT / args.out_dir
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"voice_tuning_{time.strftime('%Y-%m-%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["recommendation"], ensure_ascii=False, indent=2))
    print(json.dumps({"verdict": report["verdict"]}, ensure_ascii=False, indent=2))
    print(f"tuning_report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
