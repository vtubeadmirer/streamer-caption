# streamer-caption-v2 Next Phase Handoff

Last updated: 2026-07-11
Project root: `<project-root>`

## 1. Purpose and current state

This is a local Windows real-time Korean caption tool for streamers and for viewers who want to caption PC playback. It targets RTX 4060 Ti 16GB as the minimum deployment GPU and was developed on a stronger GPU machine.

The software implementation is approximately 90.6% complete. The remaining 9.4% is primarily real-hardware validation and tuning on the RTX 4060 Ti 16GB target PC, plus an optional SimulStreaming evaluation. The code is not waiting on a design decision to run basic live use.

## 2. Completed functionality

### Caption pipeline

- Local audio capture for microphone and PC playback (`audio.source = microphone | system`).
- Energy VAD and Silero VAD support, with Energy fallback when Silero cannot load.
- `baseline_v1_mode` and supervised `v2_light` runtime modes.
- faster-whisper `large-v3` support with CUDA and CPU fallback.
- Pending and confirmed caption events via local WebSocket.
- Korean sentence splitting with a user-adjustable character limit.
- JSONL session and memory logging.

### Reliability and recovery

- STT runs in an isolated child process by default.
- STT child stalls or crashes trigger child-only recovery without stopping audio capture/admin/WebSocket.
- A confirmed-job recovery ledger replays the active confirmed job and rejects duplicate confirmed results by job/sample range.
- Watchdog thresholds: warning 20 seconds, normal job restart 60 seconds, model cold-start limit 300 seconds, process start limit 60 seconds.
- Thread-mode compatibility fallback remains available if Windows process spawn fails.

### Output and admin UI

- Admin page: `http://127.0.0.1:8766/admin`
- Caption page: `http://127.0.0.1:8766/caption`
- OBS can use the caption URL as a browser source. A preview is available without OBS.
- Caption size range: 14 to 200px.
- Caption history: 1 to 12 lines.
- Just Chatting mode keeps confirmed caption history rather than clearing it after the normal hold time.
- Korean/English UI toggle and light/dark admin themes.
- Tooltips and a detailed built-in operator guide.

### Slang and text defense

- Slang dictionary supports transcription variants mapped to the desired display text.
- Whitelist/protected terms support important terms that should not be hidden only because recognition confidence is low.
- Blocklist masks selected terms.
- Filters exist for empty text, single characters, repeat characters, known subtitle/credit patterns, low confidence, low voice ratio, and high no-speech probability.
- As of this handoff, slang entries are no longer automatically injected into Whisper's initial prompt. This prevents registered terms such as sandwich-related phrases from being echoed during non-speech audio. The dictionary still performs post-recognition replacement.
- A protected term cannot bypass acoustic evidence. A segment identified as non-speech still remains hidden.

## 3. Recent fixes included in the latest package

1. `caption.html` opened directly from disk previously used the browser default because `/api/config` resolved as a local file path. It now explicitly reads `http://127.0.0.1:8766/api/config` when opened using `file:` and polls settings every second. Caption size, max lines, and Just Chatting changes now apply live while the app is running.
2. PC playback with Energy VAD produced false captions from music, effects, and broad system audio. During a `system` source run, the runtime now selects Silero VAD without overwriting the saved user configuration. If Silero fails to load, the existing fallback remains Energy VAD and the displayed VAD engine should be checked.
3. Slang prompt leakage was removed by default. Use post-recognition mappings for source variants such as `산도웨쭈`, `샨도웨이쭈` -> desired display text.
4. Whitelisted/protected terms no longer bypass `low_voice_ratio` or `no_speech_probability` filters.

## 4. Latest deployment package

Latest update ZIP, preserves existing user settings:

`<project-root>\reports\streamer_caption_v2_update_keep_settings_2026-07-11_010147.zip`

It includes the recent hallucination and caption-setting fixes. It deliberately excludes:

- `config.json`
- `data\slang_dict.json`
- `data\whitelist.txt`
- `data\blocklist.txt`

Deployment to an existing target PC:

1. Extract the ZIP into the existing `streamer-caption-v2` installation folder and overwrite code files.
2. Run `START_HERE.bat`.
3. Select `AH` to install/update dependencies.
4. Select `B` for the normal v2_light run.
5. Open `http://127.0.0.1:8766/admin` and `http://127.0.0.1:8766/caption`.

For a new PC, use the full transfer ZIP workflow or run `setup_4060ti_pc.bat` after copying the full project folder.

## 5. Essential operational flow

- Broadcaster microphone: set input source to Microphone, then start `B`.
- Viewer/PC playback: set input source to PC playback, then start `B`. The runtime uses Silero VAD for this run to reduce non-speech captions.
- OBS browser source URL: `http://127.0.0.1:8766/caption?preview=0`
- Preview without OBS: `http://127.0.0.1:8766/caption`
- Directly opening `overlay\caption.html` also receives settings while `B` is running, but the HTTP caption URL is preferred.
- Stop live processing with `Ctrl+C`.

## 6. Important configuration behavior

Hot-applied without restarting `B`:

- Caption size (`caption.ko_size`)
- Maximum caption lines (`caption.max_lines`)
- Just Chatting mode (`caption.just_chatting_mode`)
- Previous-caption opacity
- Pending/confirmed display times

Requires stopping `B` with `Ctrl+C` and starting it again:

- Input source/device
- VAD engine and threshold
- Audio gain and silence segmentation
- Maximum characters per caption line
- STT model, profile, worker mode
- Slang dictionary changes

## 7. Key files and paths

### Application entry points

- Main pipeline: `<project-root>\main.py`
- Menu launcher: `<project-root>\START_HERE.bat`
- 4060 Ti setup: `<project-root>\setup_4060ti_pc.bat`
- Dependency updater: `<project-root>\install_update_dependencies.bat`

### Audio, VAD, STT, and recovery

- Audio capture: `<project-root>\core\audio_capture.py`
- PC playback source: `<project-root>\core\audio_sources.py`
- VAD implementations: `<project-root>\core\vad.py`
- Streaming scheduler: `<project-root>\core\streaming.py`
- STT process worker: `<project-root>\core\stt_process_worker.py`
- Worker factory/fallback: `<project-root>\core\stt_worker_factory.py`
- Confirmed-result ledger: `<project-root>\core\recovery.py`
- Whisper engine: `<project-root>\engines\baseline\engine.py`

### UI and admin server

- Caption overlay: `<project-root>\overlay\caption.html`
- Admin UI: `<project-root>\overlay\admin.html`
- Admin theme: `<project-root>\overlay\admin-theme.css`
- Admin API server: `<project-root>\core\admin_server.py`
- WebSocket server: `<project-root>\core\ws_server.py`

### Text, settings, diagnostics, and packages

- Config defaults/validation: `<project-root>\core\config.py`
- Current saved config: `<project-root>\config.json`
- Text defense: `<project-root>\features\defense.py`
- Slang mapping: `<project-root>\features\slang.py`
- Slang data: `<project-root>\data\slang_dict.json`
- Protected terms: `<project-root>\data\whitelist.txt`
- Blocked terms: `<project-root>\data\blocklist.txt`
- Session summary: `<project-root>\tools\summarize_session.py`
- Diagnostics collector: `<project-root>\tools\collect_diagnostics.py`
- Update package builder: `<project-root>\tools\make_update_zip.py`
- Full transfer package builder: `<project-root>\tools\make_transfer_zip.py`
- Logs: `<project-root>\logs\`
- Reports/packages: `<project-root>\reports\`

## 8. Verification completed

Previous implementation verification:

- 17 regression tests passed after STT process isolation/recovery implementation.
- Python compile checks passed for the touched product modules/tools.
- Admin JavaScript syntax check passed.

Latest hallucination/caption fix verification:

- `tests\test_reliability_controls.py` passed.
- `tests\test_slang_admin.py` passed.
- `tests\test_admin_server.py` passed.
- Python compile passed for `main.py`, `core\config.py`, `features\defense.py`, and `engines\baseline\engine.py`.
- Direct check confirmed system audio changes to Silero in runtime memory while the saved config remains unchanged.
- Direct check confirmed slang prompt injection is off unless `slang.prompt_mode = "terms"` is explicitly set.

## 9. Remaining work for the next phase

### Required before calling the project production-ready

1. Validate on the actual RTX 4060 Ti 16GB PC.
   - Run `AH`, then `AG` to verify devices.
   - Run `AF` with a real broadcast playing through PC audio.
   - Run a 10-minute idle/no-speech test to measure false captions.
   - Run `B` for at least 30 minutes with real speech.
   - Run `N` and collect the latest session summary.
   - Inspect admin status for STT process restarts, recovered confirmed jobs, and actual VAD engine.
2. Tune VAD based on real target-PC evidence.
   - First try Silero threshold 0.50.
   - If noise still becomes captions, raise to 0.55.
   - If quiet speech is missed, lower to 0.45.
   - Do not lower thresholds broadly before observing RMS/VAD data.
3. Confirm caption presentation on the target display/OBS resolution.
   - Check 14px through normal broadcast sizes.
   - Check Just Chatting at 12 lines and a long spoken segment.

### Optional planned work

- SimulStreaming experimental TCP adapter is implemented and verified on RTX 5070 Ti with large-v3 + VAC. It remains opt-in until RTX 4060 Ti VRAM, latency, accuracy, and stability pass the gate in `docs/SIMULSTREAMING_EXPERIMENT.md`.
- Acoustic personalized-word recognition from reference recordings. This is not implemented. It needs a separate speaker/phrase matching design and careful false-positive controls; it should not be mixed into the current simple text replacement dictionary without a design pass.
- Optional privacy-sensitive disk audio journal for recovery after a complete parent-process/app crash. Current recovery preserves the active confirmed job within the running parent process; disk persistence is intentionally not enabled.

## 10. Known limitations and cautions

- SimulStreaming file/TCP validation was completed on the RTX 5070 Ti development PC. Real validation on the separate RTX 4060 Ti target environment remains required.
- The development machine's existing `.venv` references an unavailable old Anaconda Python path. Package creation used the installed Python 3.14 directly. On a target PC, `setup_4060ti_pc.bat` creates a clean environment using Python 3.11, 3.12, or 3.13.
- If Silero cannot load, the system falls back to Energy VAD. This should be visible through the runtime audio status and can permit more background-audio false positives.
- A text whitelist is not a voice-recognition model. It only protects/mapping results after STT; it does not guarantee that a particular unusual pronunciation will be recognized from raw audio.