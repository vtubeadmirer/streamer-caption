# Architecture Notes

## Key Correction

The original v2 plan assumes a working v1 codebase exists. It does not exist in
this workspace. To avoid building on a ghost baseline, `baseline_v1_mode` is now
defined as the first real implementation target.

That baseline is not a claim of byte-for-byte v1 compatibility. It means:

- Same user-facing shape as the handoff: local mic capture, energy VAD,
  segment-level faster-whisper, OBS overlay through WebSocket, admin/config/logs.
- Same operational defaults where known: 16 kHz mono, mic device index 1,
  localhost-only servers.
- Same safety philosophy: if the GPU path fails, keep the system alive in a
  degraded mode instead of crashing.

## Risks Fixed Before Implementation

1. No v1 source exists.
   - Fixed by making `baseline_v1_mode` the official reference target.
2. Engine churn can break callers.
   - Fixed by defining one `CaptionEngine` event contract for baseline,
     whisper_streaming, and SimulStreaming adapters.
3. RTX 4060 Ti 16GB must remain viable.
   - Fixed by forbidding dual-model runtime architecture for now and making
     memory monitoring part of the normal app.
4. Pending text can make OBS captions visually unstable.
   - Fixed by separating `pending` and `confirmed`, with debounce controlled by
     config.
5. Audio callbacks cannot safely await.
   - Fixed by forcing `sounddevice callback -> threading.Queue -> worker`.
6. Recovery logic can become too complex too early.
   - Fixed by implementing sample-counted `AudioRingBuffer` first, then replay
     recovery.
7. Config changes have different lifetimes.
   - Fixed by tagging config fields as `hot`, `warm`, or `cold`.
8. Logs need both broadcast records and debug events.
   - Fixed by using event-style JSONL records from the start.

## Engine Event Contract

Every STT engine adapter must expose chunk input and return caption events in
this shape:

```python
CaptionEvent(
    event_type="caption_pending" | "caption_confirmed" | "engine_error",
    text=str,
    confidence=float,
    audio_offset_sec=float,
    engine=str,
    metadata=dict,
)
```

Input audio contract:

- sample rate: 16000 Hz
- dtype: float32
- channels: mono
- delivery: callback thread pushes to a bounded queue; STT worker consumes
- backpressure: keep at most `streaming.buffer_max_sec`, drop oldest, log drops
- recovery buffer: `core.ring_buffer.AudioRingBuffer` stores float32 mono audio
  with absolute sample offsets so v2 can replay recent audio without drifting
  timestamps

## WebSocket Protocol Direction

Legacy-compatible caption messages remain available:

```json
{"type": "caption", "text": "...", "confidence": 0.91}
```

v2 messages add explicit stream state:

```json
{"type": "caption_pending", "text": "...", "audio_offset_sec": 12.8}
{"type": "caption_confirmed", "text": "...", "audio_offset_sec": 13.6}
{"type": "sync", "items": []}
```

## v2_light Status

The detailed phase-2/phase-3 design is in
[ARCHITECTURE_V2_V3.md](ARCHITECTURE_V2_V3.md).

main.py --v2-light currently uses:

- create_audio_chunk_source() to select microphone or Windows PC-playback
  loopback input behind the same AudioChunk contract.
- PendingWindowScheduler for sample-counted pending windows.
- AudioRingBuffer for recent audio windows and absolute sample offsets.
- Baseline faster-whisper STT in one dedicated child process.
- ProcessSttWorker with confirmed-job priority. Busy preview requests are
  skipped; queued confirmed jobs are never discarded.
- A stalled active confirmed job is re-sent from parent-owned audio after the
  STT child restarts.
- ConfirmedRangeLedger suppresses duplicate job ids and sample ranges.
- Voice-ratio and Whisper no-speech metadata before visible captions.
- run_supervised.py as the final fallback after exhausted audio recovery or
  exhausted STT-child recovery. Ordinary STT stalls restart only the model
  child and keep audio, VAD, WebSocket, and admin services alive.
- Existing caption_pending and caption_confirmed WebSocket messages.

VAD now supports:

- Energy VAD as the built-in fallback.
- Silero VAD on CPU through silero-vad/torch.
- tools/set_vad_engine.py for manual switching.
- tools/tune_voice_wav.py for real-clip tuning reports.

The dedicated Baseline STT child is now the default hard-recovery boundary.
Phase 3B is the optional SimulStreaming engine adapter. It remains gated on a
real RTX 4060 Ti VRAM and latency comparison because it uses a separate
PyTorch Whisper model rather than the existing CTranslate2 model.

## Stability Tooling

Long-run checks use the normal v2_light runner with fixed durations:

- `run_v2_light_10min.bat`
- `run_v2_light_30min.bat`
- `tools\summarize_memory.py --latest`
- `tools\summarize_session.py --latest`

Overlay pending captions are client-filtered by audio offset and text key so
older or duplicate pending messages do not repaint over newer confirmed text.
