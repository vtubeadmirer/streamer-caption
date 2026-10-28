from __future__ import annotations

import argparse
import array
import copy
import json
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from engines.simulstreaming.client import SimulStreamingClient


def read_pcm16(path: Path) -> tuple[list[float], int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("The experiment WAV must be mono PCM16")
        sample_rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    values = array.array("h")
    values.frombytes(raw)
    if sys.byteorder != "little":
        values.byteswap()
    return [value / 32768.0 for value in values], sample_rate


def gpu_memory_mib() -> float | None:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=True,
        )
        first_line = result.stdout.strip().splitlines()[0]
        return float(first_line.strip())
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end SimulStreaming TCP adapter WAV experiment.")
    parser.add_argument("audio", nargs="?", default="samples/test_clip.wav")
    parser.add_argument("--python", dest="runtime_python", default="experiments/simulstreaming-runtime/.venv/Scripts/python.exe")
    parser.add_argument("--model", default="experiments/simulstreaming-runtime/models/large-v3.pt")
    parser.add_argument("--chunk-sec", type=float, default=0.064, help="feed block size (the app's capture block)")
    parser.add_argument("--no-realtime", action="store_true")
    args = parser.parse_args()

    audio_path = (ROOT / args.audio).resolve() if not Path(args.audio).is_absolute() else Path(args.audio)
    runtime_python = (ROOT / args.runtime_python).resolve() if not Path(args.runtime_python).is_absolute() else Path(args.runtime_python)
    model_path = (ROOT / args.model).resolve() if not Path(args.model).is_absolute() else Path(args.model)
    samples, sample_rate = read_pcm16(audio_path)
    if sample_rate != 16000:
        raise ValueError(f"Expected 16000 Hz WAV, got {sample_rate}")

    config = copy.deepcopy(load_config())
    config["simulstreaming"].update({
        "python_executable": str(runtime_python),
        "model_path": str(model_path),
        "allow_auto_download": False,
        "start_server": True,
        "use_vac": True,
    })
    client = SimulStreamingClient(config)
    messages: list[dict] = []
    chunk_size = max(1, int(sample_rate * args.chunk_sec))
    started = time.monotonic()
    baseline_gpu_memory_mib = gpu_memory_mib()
    peak_gpu_memory_mib = baseline_gpu_memory_mib or 0.0
    last_gpu_sample_at = 0.0
    client.start()
    try:
        for offset in range(0, len(samples), chunk_size):
            chunk = samples[offset: offset + chunk_size]
            client.send_audio(chunk)
            if not args.no_realtime:
                time.sleep(len(chunk) / sample_rate)
            messages.extend(client.get_messages())
            now = time.monotonic()
            if now - last_gpu_sample_at >= 1.0:
                last_gpu_sample_at = now
                memory = gpu_memory_mib()
                if memory is not None:
                    peak_gpu_memory_mib = max(peak_gpu_memory_mib, memory)
        for _ in range(max(1, int(1.0 / args.chunk_sec))):
            client.send_audio([0.0] * chunk_size)
            if not args.no_realtime:
                time.sleep(args.chunk_sec)
            messages.extend(client.get_messages())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            batch = client.get_messages()
            if batch:
                messages.extend(batch)
                deadline = time.monotonic() + 1.0
            time.sleep(0.05)
        errors = client.get_errors()
    finally:
        snapshot = client.snapshot()
        client.close()

    partials = [item for item in messages if not item.get("is_final", False) and item.get("text")]
    finals = [item for item in messages if item.get("is_final", False)]
    latencies = []
    for item in messages:
        try:
            latency = float(item["emission_time"]) - float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if latency >= 0:
            latencies.append(latency)
    report = {
        "audio": str(audio_path),
        "audio_duration_sec": len(samples) / sample_rate,
        "wall_elapsed_sec": time.monotonic() - started,
        "message_count": len(messages),
        "partial_count": len(partials),
        "final_count": len(finals),
        "algorithmic_latency_avg_sec": sum(latencies) / len(latencies) if latencies else None,
        "algorithmic_latency_max_sec": max(latencies) if latencies else None,
        "baseline_gpu_total_memory_mib": baseline_gpu_memory_mib,
        "peak_gpu_total_memory_mib": peak_gpu_memory_mib or None,
        "gpu_memory_increase_mib": (
            peak_gpu_memory_mib - baseline_gpu_memory_mib
            if baseline_gpu_memory_mib is not None else None
        ),
        "errors": errors,
        "server": snapshot,
        "final_texts": [str(item.get("text", "")).strip() for item in finals if item.get("text")],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if errors or not partials or not finals:
        return 1
    print("simulstreaming_wav_adapter_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())