from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT, load_config, save_config
from core.profiles import apply_profile
from engines.baseline.engine import BaselineEngine
from tools.benchmark_streaming_wav import benchmark_audio
from tools.transcribe_wav import read_wav_mono, resolve_wav_path


def benchmark_profile(profile: str, wav_path: Path) -> dict[str, Any]:
    config = apply_profile(profile, keep_audio_tuning=True)
    audio, sample_rate = read_wav_mono(wav_path)
    report = benchmark_audio(config, audio, sample_rate, BaselineEngine(config))
    report.update(
        {
            "profile": profile,
            "model": config["whisper"]["model"],
            "device": config["whisper"]["device"],
            "compute_type": config["whisper"]["compute_type"],
            "vad_engine": config["vad"]["engine"],
        }
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare streaming benchmark results across hardware profiles.")
    parser.add_argument("path", nargs="?", default="samples/test_clip.wav")
    parser.add_argument("--profiles", nargs="+", default=["rtx_4060ti_16gb", "rtx_5070ti_quality"])
    parser.add_argument("--out-dir", default="reports")
    args = parser.parse_args()

    original = copy.deepcopy(load_config())
    try:
        wav_path = resolve_wav_path(args.path)
        rows = [benchmark_profile(profile, wav_path) for profile in args.profiles]
    except Exception as exc:
        print(exc)
        return 1
    finally:
        save_config(original)

    out_dir = PROJECT_ROOT / args.out_dir
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"profile_compare_{time.strftime('%Y-%m-%d_%H%M%S')}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in rows:
        print(
            "{profile}: elapsed={elapsed:.2f}s rtf={rtf} pending={pending} confirmed={confirmed} dropped={dropped} compute={compute}".format(
                profile=row["profile"],
                elapsed=row["elapsed_sec"],
                rtf="-" if row["rtf"] is None else f"{row['rtf']:.3f}",
                pending=row["pending_jobs"],
                confirmed=row["confirmed_jobs"],
                dropped=row["dropped_jobs"],
                compute=row["compute_type"],
            )
        )
    print(f"profile_compare_report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
