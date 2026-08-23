# Next Steps

## Immediate

1. On the RTX 4060 Ti PC, run START_HERE option AH so SoundCard is available without recreating the virtual environment.
2. Run START_HERE option AG and verify both microphone and PC-playback devices.
3. Run AF while a broadcast plays on the selected Windows output; confirm
   system | voice and moving RMS on the admin page.
4. Run a 10-minute idle test and confirm zero visible hallucination captions.
5. Run B for 30 minutes with normal speech and inspect STT stall/restart counts.
6. Collect diagnostics and the latest session JSONL if any restart occurs.
7. Run FINAL_CHECK.bat before creating the next update package.

## Current Verification Notes

- `check_gpu.py` sees one CUDA device.
- CUDA DLL directories are registered from the project venv.
- `smoke_transcribe.py --model tiny` loads on `cuda` with `float16`.
- `smoke_transcribe.py --model large-v3 --postprocess` loads on `cuda`.
  Current 4060 Ti profile uses `int8_float16`; silent-input subtitle-credit
  hallucination is hidden by defense.
- Hardware profiles are available for `rtx_4060ti_16gb`, `rtx_5070ti_quality`,
  and `low_vram_safe`.
- Admin page, live console levels, diagnostics reports, and profile switching
  are implemented.
- AudioRingBuffer keeps absolute sample offsets in the parent audio process.
- main.py --v2-light uses the microphone/PC-playback source factory,
  PendingWindowScheduler, pending WebSocket events, and confirmed VAD commits.
- ProcessSttWorker is the default. It keeps one model in a child process,
  restarts only that child on a stall, and re-sends the active confirmed job.
- ConfirmedRangeLedger blocks duplicate job ids and sample ranges.
- The previous SttWorker remains available through worker_mode=thread.
- Silero VAD is installed and integrated with energy VAD fallback.
- `tools\set_vad_engine.py` and `tools\tune_voice_wav.py` are available.
- `tools\benchmark_streaming_wav.py` records pending/confirmed STT timings for
  WAV clips.
- Admin UI exposes v2_light streaming controls.
- Overlay suppresses duplicate/stale pending captions.
- `run_v2_light_10min.bat`, `run_v2_light_30min.bat`, and
  `tools\summarize_session.py` are available for stability checks.
- `tools\record_tune_benchmark.py`, `tools\compare_profiles.py`, and
  `tools\progress_report.py` are available.
- `tools\apply_preset.py`, `FINAL_CHECK.bat`, and `docs\OPERATOR_GUIDE.md`
  are available.
- `tools\make_synthetic_voice_clip.py` and `synthetic_pipeline_check.bat` are
  available for no-real-voice pipeline validation.
- `tools\make_looped_wav.py` and `benchmark_looped_10min.bat` are available
  for repeated real-voice STT stress tests without live microphone speech.
- A real voice clip has been imported to `samples\test_clip.wav`; real voice
  VAD tuning and streaming WAV benchmark pass on the current 4060 Ti profile.
- 10 minute live idle stability validation passed with no errors.
- 10 minute looped real-voice STT stress benchmark passed with no dropped jobs.
- `run_v2_light.bat` and `run_v2_light_10sec.bat` are available.
- `tests/test_monitoring.py` records RSS and VRAM into `logs/*/memory.csv`.
- The first non-elevated model download can fail with Windows socket access
  denial. Re-running the same command with approved network access succeeded.

## New Reliability Verification

- Unit coverage now includes loopback-device selection, stereo downmix,
  voice-ratio/no-speech defense, process restart with confirmed-job replay,
  duplicate range suppression, and listener-mode admin controls.
- Local synthetic tests do not prove that the target 4060 Ti Windows audio
  driver exposes the expected loopback endpoint.
- The STT-only process boundary and parent-owned confirmed replay are
  implemented. Whole-process restart remains only as the final fallback.
- Raw audio replay after the parent process itself dies remains optional because
  it requires a privacy-sensitive disk journal.
- See ARCHITECTURE_V2_V3.md for phase ownership and completion criteria.

## After Baseline Works

1. Add chunk-size benchmark output: confirmed latency, pending latency, CER.
2. Run real speech benchmark targets for 4060 Ti and 5070 Ti profiles.
3. Lock recommended defaults after real 10/30 minute validation.
4. Run the implemented SimulStreaming TCP adapter on the real 4060 Ti and compare VRAM, latency, false captions, and stability against `B` before promotion from experimental status.
