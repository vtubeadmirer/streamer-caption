# SimulStreaming Experimental Adapter

Last updated: 2026-07-11
Status: integrated as an opt-in experiment; baseline `B` remains unchanged.

## Architecture decision

SimulStreaming is not implemented as another segment-level `CaptionEngine`. The official runtime expects one continuous 16 kHz mono PCM stream and emits JSON partial updates plus `is_final` voice-end events. Feeding isolated VAD segments through the existing STT worker would remove its streaming behavior.

The experimental path therefore uses:

1. Existing microphone or PC-playback chunk source.
2. `SimulStreamingClient` sending consecutive PCM16 chunks over TCP.
3. The official SimulStreaming Whisper server in a separate Python 3.12 process.
4. Official VAC for voice boundaries.
5. Conversion of non-final JSON to `caption_pending` and final JSON to `caption_confirmed`.
6. Existing slang correction, hallucination defense, sentence splitting, WebSocket output, admin status, and JSONL logging after conversion.

The normal faster-whisper pipeline, process worker, recovery ledger, and menu option `B` are untouched.

## Added files

- `engines/simulstreaming/client.py`: official TCP protocol adapter and server process owner.
- `tools/simulstreaming_probe.py`: source/runtime/model readiness check.
- `tools/test_simulstreaming_wav.py`: end-to-end WAV streaming experiment and metrics.
- `run_simulstreaming_experiment.bat`: live experiment launcher.
- `simulstreaming_probe.bat`: readiness launcher.
- `setup_simulstreaming_experiment.bat`: isolated Windows CUDA setup.
- `requirements_simulstreaming_windows.txt`: pinned Windows CUDA experiment dependencies.
- `experiments/vendor/SimulStreaming`: official source at commit `077ea37`.

The generated runtime and models live under `experiments/simulstreaming-runtime` and are excluded from git and transfer/update ZIP files.

## Menu

- `AI`: run live SimulStreaming experiment.
- `AJ`: check official source, model, and runtime imports.
- `AK`: install the isolated CUDA runtime, cache Silero VAC, and download large-v3.

Run `AK` only when several GB of downloads and storage are acceptable. Run `AJ` before `AI`.

## Verified development-PC result

Hardware/runtime:

- GPU: NVIDIA GeForce RTX 5070 Ti 16GB.
- Python: 3.12 isolated runtime.
- PyTorch: 2.11.0+cu128.
- CUDA visible: yes, CUDA 12.8.
- Model: Whisper large-v3 `.pt` (2.88GB).
- VAC: official Silero VAC path.
- Test audio: `samples/test_clip.wav`, 22.0 seconds, Korean.

Official file simulation:

- large-v3 + Korean + VAC completed successfully.
- JSON output included non-final partial text and final voice-segment events.

Project TCP adapter end-to-end test:

- Wall time including server/model startup: 41.33 seconds.
- Server connected time: 29.48 seconds.
- Partial outputs: 6.
- Final events: 5.
- Adapter/server errors: 0.
- Average algorithmic output latency: 1.26 seconds.
- Maximum algorithmic output latency: 2.24 seconds.
- GPU total memory before test: 2,749 MiB.
- GPU total memory peak: 12,836 MiB.
- Approximate increase: 10,087 MiB.

The total-memory measurement includes all GPU processes, so it is an upper-bound style observation rather than exact per-process allocation. Windows WDDM did not expose a usable process-specific memory figure through `nvidia-smi`.

## Findings

- The adapter protocol works end-to-end with the official server.
- large-v3 fits on the 5070 Ti 16GB with roughly 3GB of total-card headroom in this test environment.
- A 4060 Ti 16GB is likely to fit by capacity, but speed and headroom are not proven. A target-machine run is still required.
- Windows cannot use the upstream Triton acceleration path in this setup. SimulStreaming falls back to a slower median-kernel implementation and prints a warning.
- The tiny model worked without VAC but hit an upstream Korean tokenizer `IndexError` when combined with VAC. large-v3 + VAC did not reproduce the error.
- Output quality from the 22-second sample was mixed. This experiment proves integration and timing, not production caption accuracy.

## Target RTX 4060 Ti gate

1. Install/update the normal project first.
2. Run `AK` and allow the CUDA/runtime/model downloads to complete.
3. Run `AJ`; every check must be `ok` except explicitly documented warnings.
4. Run `tools/test_simulstreaming_wav.py` using a real Korean voice clip.
5. Record total VRAM increase, average/max latency, partial/final counts, and errors.
6. Run `AI` for ten minutes with live voice and five minutes idle.
7. Compare against `B` using the same input and record:
   - missed speech,
   - false captions,
   - time to first pending text,
   - time to final text,
   - peak VRAM,
   - crashes/restarts.
8. Promote SimulStreaming beyond experimental status only if it is materially better without exhausting 16GB VRAM.

## Safety boundaries

- Automatic model download from the normal live path is disabled by default.
- The runtime and 3GB model are not added to update ZIPs.
- SimulStreaming does not replace the baseline engine or its recovery path.
- Live experiment failures must not be interpreted as failures of menu option `B`.