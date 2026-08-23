# Operator Guide

## First Run

1. Double-click `START_HERE.bat`.
2. Run `1. Health check`.
3. Run `2. Check microphone devices` if the configured mic is wrong.
4. Run `O. Record-tune-benchmark workflow` and speak clearly for 10 seconds.
5. Run `M. Run v2_light 10 minute stability test`.

If you already have a recording file, run `W. Import audio file to test clip`
from `START_HERE.bat`, paste the file path, then run
`P. Tune-benchmark existing clip`.

Alternatively, put the recording in `samples\incoming`, run
`X. Import latest audio from samples\incoming`, then run
`P. Tune-benchmark existing clip`.

## Recommended 4060 Ti Setup

Double-click:

```text
apply_recommended_4060ti.bat
```

Current recommended runtime:

- RTX 4060 Ti 16GB target profile
- `large-v3`
- CUDA `int8_float16`
- Silero VAD on CPU
- v2_light pending/confirmed overlay

## Viewer PC Playback Mode

To caption a broadcast playing on this computer:

1. Run START_HERE option AH once after this update.
2. Run START_HERE option AG and verify a PC playback loopback device appears.
3. Open the admin Audio tab, choose PC playback, select the output device,
   save Audio, and restart B.
4. For a one-run override, use START_HERE option AF.
5. Confirm the admin input status shows system | voice while the broadcast
   plays.

This captures all sound on the selected Windows output. Notifications and
other application audio are included.

## OBS / Browser Source

Use:

```text
overlay\caption.html?preview=0
```

Optional:

```text
overlay\caption.html?preview=0&holdMs=8000&pendingStaleMs=2000
```

## STT Recovery Mode

The recommended setting in Admin > Streaming is Process. If recognition stalls,
only the STT model child restarts and the active confirmed audio is re-sent.
The status row shows process state, restart count, and recovered jobs.

Use Thread only as a compatibility fallback if the target Windows environment
cannot start a child process. Changing this setting requires restarting B.

## Stability Check

Run:

```text
run_v2_light_10min.bat
```

Then inspect:

```text
summarize_memory_latest.bat
summarize_latest_session.bat
stability_verdict_latest.bat
```

## Final Check

Run:

```text
FINAL_CHECK.bat
```

Before handing the project off, run:

```text
backup_config.bat
make_handoff_bundle.bat
```
