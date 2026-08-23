from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.audio_capture import AudioChunk, calculate_rms
from core.config import PROJECT_ROOT, load_config
from core.stt_worker import SttResult, SttWorker
from core.streaming import PendingWindowScheduler, StreamingAction
from core.verdicts import judge_streaming_benchmark
from engines.baseline.engine import BaselineEngine
from features.defense import DefenseFilter
from features.slang import SlangDictionary
from main import pending_events, postprocess_event
from tools.transcribe_wav import read_wav_mono, resolve_wav_path


def make_chunks(audio: np.ndarray, sample_rate: int, block_size: int) -> list[AudioChunk]:
    chunks = []
    offset_samples = 0
    for start in range(0, len(audio), block_size):
        samples = audio[start : start + block_size].astype(np.float32)
        chunks.append(
            AudioChunk(
                samples=samples,
                rms=calculate_rms(samples),
                offset_sec=offset_samples / sample_rate,
                duration_sec=len(samples) / sample_rate,
            )
        )
        offset_samples += len(samples)
    return chunks


def output_events_for_result(
    result: SttResult,
    defense: DefenseFilter,
    slang: SlangDictionary,
) -> list[Any]:
    output = []
    for event in result.events:
        if result.job.job_type == "pending":
            output.extend(pending_events(event, defense, slang))
        else:
            output.extend(postprocess_event(event, defense, slang))
    return output


def benchmark_audio(config: dict[str, Any], audio: np.ndarray, sample_rate: int, engine: Any) -> dict[str, Any]:
    if sample_rate != int(config["audio"]["sample_rate"]):
        raise ValueError(
            f"WAV sample rate {sample_rate} does not match configured sample rate {config['audio']['sample_rate']}"
        )
    scheduler = PendingWindowScheduler(config)
    worker = SttWorker(engine, max_queue=int(config["streaming"].get("pending_queue_max", 2)))
    defense = DefenseFilter(config)
    slang = SlangDictionary(config)
    chunks = make_chunks(audio, sample_rate, int(config["audio"]["block_size"]))
    rows: list[dict[str, Any]] = []
    started = time.perf_counter()
    worker.start()

    def handle_result(result: SttResult) -> None:
        output = output_events_for_result(result, defense, slang)
        rows.append(
            {
                "job_id": result.job.job_id,
                "job_type": result.job.job_type,
                "audio_offset_sec": result.job.audio_offset_sec,
                "audio_duration_sec": result.job.metadata.get("window_duration_sec")
                or result.job.metadata.get("segment_duration_sec"),
                "stt_elapsed_sec": result.elapsed_sec,
                "output_event_types": [event.event_type for event in output],
                "text": " ".join(event.text for event in output if event.text).strip(),
                "dropped_jobs": worker.dropped_jobs,
            }
        )

    for chunk in chunks:
        for action in scheduler.push(chunk):
            result = submit_action(action, worker, config)
            if result is not None:
                handle_result(result)
        for result in worker.get_results():
            handle_result(result)

    for action in scheduler.flush():
        result = submit_action(action, worker, config)
        if result is not None:
            handle_result(result)

    # Wait for any confirmed jobs by draining briefly. Pending jobs left in the
    # queue are best-effort and may be dropped in live use too.
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        results = worker.get_results()
        for result in results:
            handle_result(result)
        if len(rows) >= worker._next_job_id - worker.dropped_jobs:
            break
        time.sleep(0.05)
    worker.stop()

    pending_rows = [row for row in rows if row["job_type"] == "pending"]
    confirmed_rows = [row for row in rows if row["job_type"] == "confirmed"]
    elapsed = time.perf_counter() - started
    return {
        "duration_sec": len(audio) / sample_rate if sample_rate else 0.0,
        "elapsed_sec": elapsed,
        "rtf": elapsed / (len(audio) / sample_rate) if len(audio) else None,
        "chunk_count": len(chunks),
        "pending_jobs": len(pending_rows),
        "confirmed_jobs": len(confirmed_rows),
        "dropped_jobs": worker.dropped_jobs,
        "rows": rows,
    }


def submit_action(action: StreamingAction, worker: SttWorker, config: dict[str, Any]) -> SttResult | None:
    if action.action_type == "pending_window" and action.window is not None:
        worker.submit_latest(
            "pending",
            action.window.samples,
            action.window.sample_rate,
            action.window.start_offset_sec,
            metadata={
                "window_start_offset_sec": action.window.start_offset_sec,
                "window_end_offset_sec": action.window.end_offset_sec,
                "window_duration_sec": action.window.duration_sec,
            },
        )
        return None
    elif action.action_type == "confirmed_segment" and action.segment is not None:
        job = worker.submit(
            "confirmed",
            action.segment.samples,
            int(config["audio"]["sample_rate"]),
            action.segment.start_offset_sec,
            metadata={
                "segment_duration_sec": action.segment.duration_sec,
                "segment_peak_rms": action.segment.peak_rms,
                "segment_start_offset_sec": action.segment.start_offset_sec,
                "segment_end_offset_sec": action.segment.end_offset_sec,
            },
            drop_oldest=True,
        )
        return worker.wait_for(job.job_id)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark v2_light streaming behavior on a WAV file.")
    parser.add_argument("path")
    parser.add_argument("--model")
    parser.add_argument("--device")
    parser.add_argument("--compute-type")
    parser.add_argument("--vad-engine", choices=["energy", "silero"])
    parser.add_argument("--out-dir", default="reports")
    args = parser.parse_args()

    config = load_config()
    if args.model:
        config["whisper"]["model"] = args.model
    if args.device:
        config["whisper"]["device"] = args.device
    if args.compute_type:
        config["whisper"]["compute_type"] = args.compute_type
    if args.vad_engine:
        config["vad"]["engine"] = args.vad_engine

    try:
        wav_path = resolve_wav_path(args.path)
        audio, sample_rate = read_wav_mono(wav_path)
        report = benchmark_audio(config, audio, sample_rate, BaselineEngine(config))
    except Exception as exc:
        print(exc)
        return 1

    report.update(
        {
            "source": args.path,
            "model": config["whisper"]["model"],
            "device": config["whisper"]["device"],
            "compute_type": config["whisper"]["compute_type"],
        }
    )
    report["verdict"] = judge_streaming_benchmark(report)
    out_dir = PROJECT_ROOT / args.out_dir
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    json_path = out_dir / f"streaming_benchmark_{stamp}.json"
    csv_path = out_dir / f"streaming_benchmark_{stamp}.csv"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        fieldnames = [
            "job_id",
            "job_type",
            "audio_offset_sec",
            "audio_duration_sec",
            "stt_elapsed_sec",
            "output_event_types",
            "text",
            "dropped_jobs",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in report["rows"]:
            writer.writerow({key: row.get(key) for key in fieldnames})
    print(
        "streaming benchmark: duration={duration:.2f}s elapsed={elapsed:.2f}s pending={pending} confirmed={confirmed} dropped={dropped}".format(
            duration=report["duration_sec"],
            elapsed=report["elapsed_sec"],
            pending=report["pending_jobs"],
            confirmed=report["confirmed_jobs"],
            dropped=report["dropped_jobs"],
        )
    )
    print(f"verdict: {report['verdict']['status']} {report['verdict']['reason']}")
    print(f"benchmark_json: {json_path}")
    print(f"benchmark_csv: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
