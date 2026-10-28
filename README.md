# Streamer Caption

방송 중 말하는 내용을 **내 PC의 GPU로** 실시간 자막으로 만들어 OBS에 띄워 주는 프로그램입니다.
음성도 자막도 인터넷으로 보내지 않습니다.
Real-time Korean captions for streamers, running entirely on your own NVIDIA GPU. Nothing leaves your PC.

## 이렇게 시작하세요 (설치 없음)

1. [Releases](https://github.com/vtubeadmirer/streamer-caption/releases)에서
   `StreamerCaption-<버전>-portable.zip`을 받아 아무 폴더에나 풉니다.
   (OneDrive 같은 동기화 폴더는 피하세요.)
2. `StreamerCaption.bat`을 더블클릭합니다.
   - 처음 한 번만 실행 환경(약 800 MB)을 받고, 이어서 음성 인식 모델(1.6~3 GB)을 받습니다.
     진행률이 표시됩니다.
   - 설정 페이지가 브라우저에 열립니다: 마이크 선택 → (선택) PC 소리도 자막으로 →
     OBS 주소 복사 → 저장하고 시작.
3. OBS에서 **소스 추가 → 브라우저**, URL에 `http://127.0.0.1:8766/caption`을 넣습니다.
4. 다음부터는 더블클릭 한 번이면 자막이 시작됩니다. 관리자 페이지: `http://127.0.0.1:8766/admin`

### 필요한 것

- Windows 10/11, NVIDIA 그래픽카드 (VRAM 8 GB 이상 권장, RTX 4060 Ti 16 GB 기준으로 조정됨)
- 그 외 설치할 것 없음 (Python, CUDA 별도 설치 불필요)

### 할 수 있는 것

- **실시간 자막**: 마이크 음성을 1~2초 안에 자막으로. 유행어·보호 단어 사전으로 오인식을 바로잡습니다.
- **듀얼 자막 (베타)**: 마이크 + PC 재생 소리(디스코드, 영상)를 각각 라벨을 붙여 동시에 자막으로.
- **15분 요약**: 15분마다 "지금까지 이야기한 것" 요약을 OBS 오버레이로 (`/recap`).
  오버레이 맨 위에는 **지금 이야기 중** 주제 한 줄이 주제가 바뀔 때마다 갱신됩니다
  (`/recap?now=0`으로 끌 수 있음).
  기본은 로컬 추출 요약이고, Ollama 같은 로컬 AI나 Claude·Codex CLI가 설치되어 있으면
  자동으로 사용합니다. 구독 토큰이나 API 키를 저장하지 않습니다.
- **방송 기록**: 방송 중 자막을 검색·내보내기 (`/history`).
- **업데이트 · 다시 시작 · 버그 보내기**: 관리자 페이지에서 버튼 하나. 버그 번들에는
  자막 텍스트가 들어가지 않습니다.

### 문제가 생기면

- 관리자 페이지의 **버그 보내기** → 번들 만들기 → GitHub 이슈 열기. ZIP 파일을 글에 드래그해 주세요.
- 바로 이슈 쓰기: https://github.com/vtubeadmirer/streamer-caption/issues/new/choose

---

## Developer notes

이하는 소스에서 직접 실행하거나 수정하는 사람을 위한 기록입니다. 사용자는 위의
안내만 보면 됩니다.

### streamer-caption-v2 (original build log)

Real-time local captioning for streamers, rebuilt from the handoff documents.

Important project rule: there is no usable v1 source tree in this workspace. This
project therefore starts with a document-based `baseline_v1_mode`, then layers
v2 streaming behavior on top.

## Build Order

1. `baseline_v1_mode`
   - Energy VAD.
   - Segment-level faster-whisper transcription.
   - Local WebSocket caption output.
   - JSONL logging.
2. `v2_light`
   - Silero VAD on CPU.
   - Streaming STT interface with pending/confirmed events.
   - Sample-count based audio offsets.
   - Ring buffer, recovery, and memory monitoring.
3. `v2_full`
   - Replace only the STT engine with SimulStreaming-compatible adapter.

## Hardware Target

The implementation should be tuned for RTX 4060 Ti 16GB even if development is
done on a stronger GPU. Use one STT model at a time, prefer GPU fallback over
crashing, and keep memory logging enabled during long tests.

Hardware profiles are available:

```powershell
.\.venv\Scripts\python.exe tools\apply_profile.py --list
.\.venv\Scripts\python.exe tools\apply_profile.py rtx_4060ti_16gb
.\.venv\Scripts\python.exe tools\apply_profile.py rtx_5070ti_quality
```

The 4060 Ti profile keeps `large-v3` but switches CUDA compute to
`int8_float16` and uses shorter buffers to reduce VRAM pressure. The profile
tool keeps the tuned mic device, VAD threshold, input gain, and min peak RMS by
default.

## Dual Input Beta

The V2.1 beta can capture the broadcaster microphone and Windows PC playback
at the same time while keeping one STT model loaded. Each input has an isolated
capture thread, VAD segmenter, ring buffer, recovery status, and source label.
Microphone confirmed jobs are queued before PC playback jobs. PC pending
captions are disabled by default to protect latency on RTX 4060 Ti 16GB.

Start it directly with `START_HERE.bat` option 9 or `run_dual_input.bat`.
The same mode can be saved from Admin > Audio > Input source > Dual input.
Select both the microphone and Windows output device, then restart captions.

Caption URLs:

- Combined: `http://127.0.0.1:8766/caption`
- Broadcaster microphone only: `http://127.0.0.1:8766/caption?source=microphone`
- PC playback only: `http://127.0.0.1:8766/caption?source=system`

Combined captions, the transcript archive, and TXT exports retain the configured
source labels. Cross-source duplicate suppression hides near-identical text
captured by both inputs within the configured echo window. Headphones are still
recommended because software text deduplication is not acoustic echo cancellation.
## Quick Check

```powershell
.\.venv\Scripts\python.exe -m py_compile main.py core\*.py engines\baseline\*.py
.\.venv\Scripts\python.exe check_runtime.py
.\.venv\Scripts\python.exe check_gpu.py
.\.venv\Scripts\python.exe tests\test_energy_vad.py
.\.venv\Scripts\python.exe tests\test_ws_server.py
.\.venv\Scripts\python.exe tests\test_text_processing.py
.\.venv\Scripts\python.exe tests\test_admin_server.py
```

Small model load/transcription smoke test:

```powershell
.\.venv\Scripts\python.exe smoke_transcribe.py --model tiny
```

Large model CUDA smoke test with post-processing:

```powershell
.\.venv\Scripts\python.exe smoke_transcribe.py --model large-v3 --seconds 1 --postprocess
```

The silent-input `large-v3` smoke test may produce a subtitle-credit hallucination.
The post-processing path should hide it as `caption_hidden`.

## Mic Check Without OBS

Install the lightweight mic-check dependencies in the project venv:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install numpy sounddevice
```

List input devices:

```powershell
.\.venv\Scripts\python.exe mic_monitor.py --list-devices
```

Monitor the configured mic device from `config.json`:

```powershell
.\.venv\Scripts\python.exe mic_monitor.py
```

Speak into the mic and watch the RMS bar. If it moves, the app can see mic
input. If it stays flat, try another device index:

```powershell
.\.venv\Scripts\python.exe mic_monitor.py --device 1
```

Run a short automated check:

```powershell
.\.venv\Scripts\python.exe mic_monitor.py --duration-seconds 5
```

## Browser Caption Preview Without OBS

Open this file in a browser:

```text
<project-root>\overlay\caption.html
```

Then start live baseline mode:

```powershell
.\.venv\Scripts\python.exe main.py
```

When speech is detected and transcribed, captions are sent to the browser through
`ws://127.0.0.1:8765`.

The admin page is available at:

```text
http://127.0.0.1:8766/admin
```

For a short controlled run:

```powershell
.\.venv\Scripts\python.exe main.py --max-segments 1
.\.venv\Scripts\python.exe main.py --max-seconds 10
```

Show live RMS/threshold levels in the console:

```powershell
.\.venv\Scripts\python.exe main.py --max-seconds 10 --show-levels
```

Run the first v2_light streaming path with pending captions:

```powershell
.\.venv\Scripts\python.exe main.py --v2-light
.\.venv\Scripts\python.exe main.py --v2-light --max-seconds 10 --show-levels
```

Primary supervised launch:

    run_v2_light.bat

v2_light keeps the STT model in one child process. An ordinary STT stall
restarts only that child and re-sends the active confirmed audio while
audio/VAD/admin services stay alive. The outer supervisor restarts the whole
caption app only after audio recovery or STT-child recovery is exhausted.
Normal exit and Ctrl+C do not restart.

Caption a broadcast playing on the same PC:

    list_audio_sources.bat
    run_listener_mode.bat

The listener mode uses Windows output loopback. It captures every sound sent to
the selected output device, not one specific browser or application. Run START_HERE option AH after updating so the SoundCard dependency is installed without clearing the existing virtual environment.
The same actions are START_HERE options AG (list) and AF (run listener mode).

Run longer v2_light stability checks:

```powershell
run_v2_light_10min.bat
run_v2_light_30min.bat
.\.venv\Scripts\python.exe tools\summarize_session.py --latest
.\.venv\Scripts\python.exe tools\summarize_memory.py --latest
```

Status-only check without opening the mic:

```powershell
.\.venv\Scripts\python.exe main.py --status-only
```

## Repeatable Voice Test Clips

For the easiest path, double-click:

```text
START_HERE.bat
```

If anything feels off, run:

```text
health_check.bat
```

or:

```powershell
.\.venv\Scripts\python.exe tools\health_check.py
```

If the mic opens but recorded RMS is very low, compare input devices:

```powershell
.\.venv\Scripts\python.exe tools\probe_input_devices.py --seconds 2
```

Change the configured input device:

```powershell
.\.venv\Scripts\python.exe tools\set_audio_device.py 21
```

Boost low microphone input in software:

```powershell
.\.venv\Scripts\python.exe tools\set_input_gain.py 10
```

Record a 10-second WAV clip:

```powershell
.\.venv\Scripts\python.exe tools\record_wav.py --seconds 10 --out samples\test_clip.wav
```

Import a recording file you already have, including common formats such as WAV,
MP3, M4A, or WEBM, into the standard test clip path:

```powershell
.\.venv\Scripts\python.exe tools\import_audio.py "path\to\recording.m4a" --out samples\test_clip.wav
```

Or place the file in `samples\incoming` and import the newest file:

```powershell
.\.venv\Scripts\python.exe tools\import_latest_audio.py
```

Transcribe that same clip repeatedly while tuning settings:

```powershell
.\.venv\Scripts\python.exe tools\transcribe_wav.py samples\test_clip.wav
```

Analyze loudness for VAD threshold tuning:

```powershell
.\.venv\Scripts\python.exe tools\analyze_wav.py samples\test_clip.wav
```

Analyze both energy VAD and Silero VAD on the same clip:

```powershell
.\.venv\Scripts\python.exe tools\tune_voice_wav.py samples\test_clip.wav
```

Apply the recommended VAD settings only after recording a real speech clip:

```powershell
.\.venv\Scripts\python.exe tools\tune_voice_wav.py samples\test_clip.wav --apply
```

Switch VAD engines manually:

```powershell
.\.venv\Scripts\python.exe tools\set_vad_engine.py silero --speech-threshold 0.5
.\.venv\Scripts\python.exe tools\set_vad_engine.py energy
```

Run the full record, tune, transcribe, and v2_light benchmark workflow:

```powershell
.\.venv\Scripts\python.exe tools\record_tune_benchmark.py --seconds 10 --apply-tuning
.\.venv\Scripts\python.exe tools\record_tune_benchmark.py --skip-record --apply-tuning
```

Compare 4060 Ti and 5070 Ti profiles on the same clip:

```powershell
.\.venv\Scripts\python.exe tools\compare_profiles.py samples\test_clip.wav
```

Generate a progress report:

```powershell
.\.venv\Scripts\python.exe tools\progress_report.py
```

Backup the current config and create a handoff bundle:

```powershell
.\.venv\Scripts\python.exe tools\backup_config.py
.\.venv\Scripts\python.exe tools\make_handoff_bundle.py
```

Streamer-specific slang and protected words can be edited in the admin page
under `Slang / Protected Terms`. Use `heard=show` pairs such as
`샌드위치=샹도웨쭈`, and add protected words such as `샹도웨쭈` one per line when
the defense filter should not hide them.

For one streamer-specific word with several possible recognitions, register
all heard variants:

```text
샌드위치=샹도웨쭈
산도웨쭈=샹도웨쭈
샨도웨이쭈=샹도웨쭈
```

Caption size and line count are edited in the admin `Caption` section. The
browser caption page refreshes these display settings automatically. Set
`한글 크기` as low as 14 when a small browser overlay is needed. `최대 줄 수`
supports up to 12 lines. Enable `저스트 채팅 모드` to keep a rolling caption
history on screen for viewers who arrive late.
 Confirmed captions and pending previews use separate display areas. For fast
speakers, start the Fast speech reading hold setting at 0.65 seconds; use
0.8-1.0 seconds for longer reading time or 0.4-0.5 seconds when the display
backlog grows.

See UPDATE_NOTES_2026-07-11.md for the current stability update and recommended
hallucination, short-speech, latency, and Just Chatting settings.

For updates after the first install, use `make_update_zip.bat`. The generated
`streamer_caption_v2_update_keep_settings_*.zip` does not overwrite
`config.json`, `data/slang_dict.json`, `data/whitelist.txt`, or
`data/blocklist.txt`. Use `backup_user_settings.bat` before risky updates and
`restore_user_settings.bat` if settings need to be restored.

When asking Codex to review a failed run, use `make_support_bundle.bat` and send
the generated `streamer_caption_v2_support_*.zip`. It includes config, latest
reports, and the newest log folder without bundling audio samples.

Apply the recommended 4060 Ti preset and run final checks:

```powershell
.\.venv\Scripts\python.exe tools\apply_preset.py recommended_4060ti_v2_light
.\.venv\Scripts\python.exe tools\final_check.py
```

When no real voice clip is available, run a synthetic pipeline check:

```powershell
.\.venv\Scripts\python.exe tools\make_synthetic_voice_clip.py --out samples\synthetic_voice.wav
.\.venv\Scripts\python.exe tools\tune_voice_wav.py samples\synthetic_voice.wav --vad-engine energy
.\.venv\Scripts\python.exe tools\benchmark_streaming_wav.py samples\synthetic_voice.wav --vad-engine energy
```

Run the whole voice-test workflow and save a JSON report:

```powershell
.\.venv\Scripts\python.exe tools\voice_test_workflow.py --seconds 10 --out samples\test_clip.wav
```

Apply the suggested VAD threshold from that clip:

```powershell
.\.venv\Scripts\python.exe tools\apply_vad_threshold.py samples\test_clip.wav --from-analysis
```

The apply tool clamps suggestions to a safer range by default
(`0.003` to `0.08`) so a quiet-room clip does not make the mic too sensitive.

Benchmark the same clip repeatedly:

```powershell
.\.venv\Scripts\python.exe tools\benchmark_wav.py samples\test_clip.wav --repeat 3
```

Benchmark the v2_light streaming path on the same clip:

```powershell
.\.venv\Scripts\python.exe tools\benchmark_streaming_wav.py samples\test_clip.wav
```

Warm benchmark, closer to live use after the model is already loaded:

```powershell
.\.venv\Scripts\python.exe tools\benchmark_wav.py samples\test_clip.wav --repeat 3 --warm
```

For cleaner warm numbers, discard the first measured run:

```powershell
.\.venv\Scripts\python.exe tools\benchmark_wav.py samples\test_clip.wav --repeat 4 --warm --discard-first
```

With a reference transcript:

```powershell
.\.venv\Scripts\python.exe tools\benchmark_wav.py samples\test_clip.wav --reference "여기에 정답 문장" --repeat 3
```

Summarize the latest benchmark:

```powershell
.\.venv\Scripts\python.exe tools\summarize_benchmark.py --latest
```

Collect a shareable local diagnostics report:

```powershell
.\.venv\Scripts\python.exe tools\collect_diagnostics.py
.\.venv\Scripts\python.exe tools\collect_diagnostics.py --model-load
```

Summarize the latest memory log:

```powershell
.\.venv\Scripts\python.exe tools\summarize_memory.py --latest
```

Summarize the latest caption/session log:

```powershell
.\.venv\Scripts\python.exe tools\summarize_session.py --latest
```

Double-click helpers are also available:

```text
record_test_clip.bat
transcribe_test_clip.bat
analyze_test_clip.bat
voice_test_workflow.bat
apply_vad_from_test_clip.bat
benchmark_test_clip.bat
benchmark_test_clip_warm.bat
benchmark_streaming_test_clip.bat
tune_voice_test_clip.bat
apply_voice_tuning_test_clip.bat
set_vad_silero.bat
set_vad_energy.bat
record_tune_benchmark.bat
tune_benchmark_existing_clip.bat
compare_profiles_test_clip.bat
progress_report.bat
apply_recommended_4060ti.bat
apply_stable_4060ti.bat
synthetic_pipeline_check.bat
import_audio_to_test_clip.bat
import_latest_audio_to_test_clip.bat
stability_verdict_latest.bat
backup_config.bat
make_handoff_bundle.bat
benchmark_looped_10min.bat
FINAL_CHECK.bat
summarize_latest_benchmark.bat
health_check.bat
run_baseline.bat
run_baseline_10sec.bat
run_v2_light.bat
run_v2_light_10sec.bat
run_v2_light_10min.bat
run_v2_light_30min.bat
apply_4060ti_profile.bat
apply_5070ti_profile.bat
collect_diagnostics.bat
collect_diagnostics_model_load.bat
summarize_memory_latest.bat
summarize_latest_session.bat
```

If a tool says `WAV file not found`, record `samples\test_clip.wav` first with
`record_test_clip.bat` or option 2 in `START_HERE.bat`.
## Phase 2/3 archive prototype

Confirmed captions are indexed locally in `data/transcripts.sqlite3` without storing raw audio.

- Conversation archive: `http://127.0.0.1:8766/history`
- 30-minute summary overlay: `http://127.0.0.1:8766/summary`
- GPU-free demo: `PREVIEW_PHASE23.bat`
- Architecture and implementation report: `docs/PHASE_2_3_INTEGRATION_REPORT.md`
- Post-broadcast reports: `logs/<session>/broadcast_report.json` and `broadcast_report.md`
- Live confirmed-caption text: `logs/<session>/captions.txt`
- Current session text at a fixed path: `logs/latest_captions.txt`
- Past-session TXT export: select a broadcast in `/history`, then click `선택 방송 TXT 저장`
- Live issue marker: `http://127.0.0.1:8766/validate`
- One-click validation report: `VALIDATE_LATEST_BROADCAST.bat`

During a broadcast, keep `/validate` open and mark hallucinations, missed speech,
wrong words, or a correctly recognized reference moment. Each marker stores the
current audio offset, the latest caption, and an optional note. After the run,
`VALIDATE_LATEST_BROADCAST.bat` writes `validation_report.json` and
`validation_report.md` beside the session log. `make_support_bundle.bat` also
creates this report automatically before collecting the latest logs.

If the app is force-killed, the next normal start or validation run closes the
orphaned archive session and regenerates its report when the log directory is
available. A session whose saved process is still alive is never recovered or
deleted.

Use the Archive Management section in `/history` to export the entire SQLite
archive, prune ended sessions older than a selected number of days, or delete a
selected ended broadcast. Prune and delete operations create a backup first in
`reports/archive_backups`. They remove the SQLite archive entry only; original
`logs/<session>` files are retained. See
`docs/BROADCAST_VALIDATION_AND_ARCHIVE_OPERATIONS.md` for the full workflow and
API contract.

The demo uses a separate database under `reports/phase23_demo`; it does not add synthetic captions to the production archive. Update bundles preserve the production transcript database.
## 15-Minute Recaps (v2.2)

While live captions run, every 15 minutes the confirmed captions of that block
are turned into a short Korean recap (headline + up to four bullets) and stored
with the transcript. Show it in OBS with one browser source:

    http://127.0.0.1:8766/recap          (latest block large, previous block small)
    http://127.0.0.1:8766/recap?count=1  (latest block only; also ?theme=light, ?position=top, ?now=0 hides the current-topic line)

`START_HERE.bat` option R opens the page. The block length is
`archive.bucket_minutes` (10-20 is a good range for just chatting).

Recap quality depends on what is installed, and nothing needs configuring:

1. A local Ollama server (`ollama pull exaone3.5:7.8b` or any Korean-capable
   model) is used automatically when it answers on 127.0.0.1:11434.
2. Otherwise an installed `claude` or `codex` CLI is used.
3. Otherwise the built-in keyword summary runs (always works, no network).

Set `archive.recap.provider` to `none` to force the built-in summary, or to
`openai` with `openai_base_url`, `openai_model` and an API key in the
`OPENAI_API_KEY` environment variable for a hosted model. Recap generation runs
in its own thread, falls back on any error, and never delays captions.

## Faster Captions with large-v3-turbo

`large-v3-turbo` decodes several times faster than `large-v3` with almost the
same Korean accuracy, so confirmed captions appear sooner after a sentence ends.
Apply it with `START_HERE.bat` option T or:

    .\.venv\Scripts\python.exe tools\apply_profile.py rtx_4060ti_turbo

The model (about 1.6 GB) is downloaded once on the next caption start. Switch
back with `apply_4060ti_profile.bat`. For segmentation quality, the Silero VAD
(`vad.engine: "silero"`, set by the auto GPU preset) is recommended over the
energy VAD.

## 정확 우선 engine (SimulStreaming) — in development

The admin page's **자막 엔진** choice adds **정확 우선**: captions about 1.3 seconds later
whose words never change once shown (lab, large-v3-turbo: 1.0% CER, no phantom captions),
falling back to the default faster-whisper engine automatically on a crash, stall, lag or full
GPU. It needs an optional CUDA PyTorch runtime and the Whisper `large-v3-turbo.pt` model that
releases do not download yet; without them the option is shown disabled. Design, lab numbers,
fallback rules and the remaining download work: `docs/simulstreaming.md`.

## Release installer and automatic updates

The versioned release workflow is documented in `docs/RELEASE_AND_UPDATE_ARCHITECTURE.md`.

- New users run `INSTALL.bat` once.
- Installed users start with `START_STREAMER_CAPTION.bat`.
- Application versions, shared runtime, and persistent user data are stored separately.
- `BUILD_RELEASE.bat` creates a privacy-safe GitHub Release ZIP and SHA-256 manifest.
- FFmpeg is optional and is installed separately with `INSTALL_OPTIONAL_FFMPEG.bat`.

## Updates, restart and bug reports from the admin page

- The admin page (`http://127.0.0.1:8766/admin`) checks GitHub Releases once an
  hour. When a newer version exists a banner offers **Update now**, **Skip this
  version** and **Later**. The download runs in the background with a progress
  bar; when it is installed, **Restart to apply** relaunches the app on the new
  version (the launcher re-reads `state.json`, so rollback keeps working).
- **Restart** in the header applies "cold" settings (microphone, dual input,
  model) without closing the console window. The setup wizard offers the same
  button after saving such settings. Technically the app exits with code 76 and
  the supervisor/launcher starts it again.
- **Report a bug** builds `reports/streamer_caption_bugreport_*.zip`. It never
  contains caption text: every spoken string in the session logs, summaries,
  prompts and word lists is replaced by `[redacted N chars]`. The button opens a
  prefilled GitHub issue (version, GPU, model, your description); drag the ZIP
  into the issue. `make_support_bundle.bat` still produces the full bundle for
  private troubleshooting.

## License

Streamer Caption source code is available under the MIT License. See `LICENSE`.
Third-party packages, vendored components, model weights, and optional FFmpeg
binaries remain subject to their own license terms. See
`THIRD_PARTY_NOTICES.md` before redistribution.

## No-Install (Portable) Version

`StreamerCaption-<version>-portable.zip` is the one-button build for streamers:

1. Unzip anywhere (not inside a synced folder such as OneDrive).
2. Double-click `StreamerCaption.bat`.
   - First run only: the shared Python runtime (about 800 MB, `runtime-py312-v3.zip`
     from the `runtime-py312-v3` GitHub release) is downloaded with a progress bar,
     verified by SHA-256 and unpacked into `runtime/.venv`. It bundles NVIDIA cuBLAS,
     so GPU captions need only the NVIDIA driver, not a CUDA Toolkit. The speech model
     is downloaded when captions start.
   - The browser then opens `http://127.0.0.1:8766/setup`: pick the microphone,
     optionally turn on dual (PC playback) captions, copy the OBS URLs, choose how
     recaps are generated. Everything is saved under `user-data/` and survives updates.
3. Every later run is one double-click: captions start and the admin page is one
   click away.

Offline PCs: download `runtime-py312-v3.zip` once, put it next to
`StreamerCaption.bat`, and it is used instead of the network. The layout is the
same side-by-side scheme as the installed version (`versions/<ver>`, `runtime/`,
`user-data/`), so updates and rollback work identically; when a release needs a
newer runtime, the updater fetches it before switching the app version.

Build locally with `python tools/build_runtime.py` (runtime) and
`BUILD_RELEASE.bat` (app ZIP + portable ZIP + manifest). On GitHub the
`Build portable runtime` workflow publishes the runtime release and the normal
tag release attaches both app ZIPs.
