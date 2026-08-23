from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT, load_config
from core.metrics import character_error_rate
from engines.baseline.engine import BaselineEngine
from features.defense import DefenseFilter
from features.slang import SlangDictionary
from main import postprocess_event
from tools.transcribe_wav import read_wav_mono, resolve_wav_path


def transcribe_once(
    config: dict,
    wav_path: Path,
    source_name: str,
    engine: BaselineEngine | None = None,
) -> dict:
    audio, sample_rate = read_wav_mono(wav_path)
    engine = engine or BaselineEngine(config)
    defense = DefenseFilter(config)
    slang = SlangDictionary(config)
    started = time.perf_counter()
    output_events = []
    for event in engine.on_audio_segment(audio, sample_rate, 0.0):
        output_events.extend(postprocess_event(event, defense, slang))
    elapsed = time.perf_counter() - started

    captions = [event.text for event in output_events if event.event_type == "caption_confirmed"]
    hidden = [event.text for event in output_events if event.event_type == "caption_hidden"]
    metadata = output_events[0].metadata if output_events else {}
    return {
        "source": source_name,
        "duration_sec": len(audio) / sample_rate,
        "elapsed_sec": elapsed,
        "rtf": elapsed / (len(audio) / sample_rate) if len(audio) else None,
        "text": " ".join(captions).strip(),
        "hidden_text": " ".join(hidden).strip(),
        "event_types": [event.event_type for event in output_events],
        "metadata": metadata,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark baseline transcription on one or more WAV files.")
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--reference")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--model")
    parser.add_argument("--device")
    parser.add_argument("--compute-type")
    parser.add_argument("--out-dir", default="reports")
    parser.add_argument("--warm", action="store_true")
    parser.add_argument("--discard-first", action="store_true")
    args = parser.parse_args()

    config = load_config()
    if args.model:
        config["whisper"]["model"] = args.model
    if args.device:
        config["whisper"]["device"] = args.device
    if args.compute_type:
        config["whisper"]["compute_type"] = args.compute_type

    reference = args.reference
    if reference and Path(reference).exists():
        reference = Path(reference).read_text(encoding="utf-8").strip()

    rows = []
    shared_engine = None
    warmup_elapsed = None
    warmup_event = None
    if args.warm:
        shared_engine = BaselineEngine(config)
        started = time.perf_counter()
        warmup_event = shared_engine.warm_up()
        warmup_elapsed = time.perf_counter() - started
        print(f"warmup: {warmup_event.event_type} elapsed={warmup_elapsed:.2f}s {warmup_event.text}")

    for source in args.paths:
        try:
            wav_path = resolve_wav_path(source)
        except Exception as exc:
            print(exc)
            return 1
        for run_index in range(args.repeat):
            row = transcribe_once(config, wav_path, source, shared_engine)
            row["run_index"] = run_index + 1
            row["model"] = config["whisper"]["model"]
            row["device"] = row["metadata"].get("device", config["whisper"]["device"])
            row["compute_type"] = row["metadata"].get("compute_type", config["whisper"]["compute_type"])
            row["warm"] = args.warm
            row["discarded"] = bool(args.discard_first and run_index == 0)
            row["warmup_elapsed_sec"] = warmup_elapsed
            row["warmup_event_type"] = warmup_event.event_type if warmup_event else None
            row["cer"] = character_error_rate(reference, row["text"]) if reference is not None else None
            row["hidden"] = "caption_hidden" in row.get("event_types", [])
            rows.append(row)
            print(
                "{source} run={run}{discarded} elapsed={elapsed:.2f}s rtf={rtf:.2f} cer={cer} text={text}".format(
                    source=source,
                    run=run_index + 1,
                    discarded=" discarded" if row["discarded"] else "",
                    elapsed=row["elapsed_sec"],
                    rtf=row["rtf"] or 0.0,
                    cer="-" if row["cer"] is None else f"{row['cer']:.4f}",
                    text=row["text"] or f"[hidden:{row['hidden_text']}]",
                )
            )

    out_dir = PROJECT_ROOT / args.out_dir
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    json_path = out_dir / f"benchmark_{stamp}.json"
    csv_path = out_dir / f"benchmark_{stamp}.csv"
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "source",
                "run_index",
                "model",
                "device",
                "compute_type",
                "warm",
                "discarded",
                "warmup_elapsed_sec",
                "duration_sec",
                "elapsed_sec",
                "rtf",
                "cer",
                "text",
                "hidden_text",
                "hidden",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in writer.fieldnames})
    print(f"benchmark_json: {json_path}")
    print(f"benchmark_csv: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
