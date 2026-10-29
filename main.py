from __future__ import annotations

import argparse
import time
from copy import deepcopy
from dataclasses import replace
from typing import Any

from core.audio_capture import AudioSegment, AudioSegmentSource
from core.audio_sources import config_for_audio_source, create_audio_chunk_source
from core.admin_server import AdminServer, AdminState
from core.app_info import RELAUNCH_EXIT_CODE
from core.config import load_config
from core.events import CaptionEvent
from core.log_writer import JsonlLogWriter
from core.monitoring import MemoryMonitor
from core.preview import PreviewTracker
from core.session_archive import SessionArchiveRecorder
from core.source_dedup import CrossSourceCaptionDeduplicator
from core.recovery import ConfirmedRangeLedger
from core.stt_worker import SttResult
from core.stt_worker_factory import create_and_start_stt_worker
from core.streaming import PendingWindowScheduler, StreamingAction
from core.text_processing import split_sentences
from core.ws_server import CaptionWebSocketServer
from engines.baseline.engine import BaselineEngine
from features.defense import DefenseFilter
from features.phantom_phrases import strip_phantom_sentences
from features.slang import SlangDictionary


AUDIO_SOURCE_EXIT_CODE = 74
STT_STALL_EXIT_CODE = 75


def prepare_runtime_config(config: dict) -> tuple[dict, str | None]:
    """Use speech-aware VAD for PC playback without overwriting user config."""
    if (
        config.get("audio", {}).get("source") in {"system", "both"}
        and config.get("vad", {}).get("engine") == "energy"
    ):
        runtime_config = deepcopy(config)
        runtime_config["vad"]["engine"] = "silero"
        return (
            runtime_config,
            "PC playback uses Silero VAD for this run to reduce music/game-audio hallucinations.",
        )
    return config, None


def run_status_check(config: dict) -> int:
    engine = BaselineEngine(config)
    for event in engine.on_audio_segment(None, config["audio"]["sample_rate"], 0.0):
        print(f"{event.event_type}: {event.text}")
    return 0


def run_live(
    config: dict,
    max_segments: int | None,
    max_seconds: float | None,
    enable_ws: bool,
    enable_admin: bool,
    show_levels: bool,
) -> int:
    if config.get("audio", {}).get("source") == "both":
        print("Dual input uses the v2_light scheduler.")
        return run_v2_light(
            config,
            max_segments,
            max_seconds,
            enable_ws,
            enable_admin,
            show_levels,
        )
    config, runtime_warning = prepare_runtime_config(config)
    writer = JsonlLogWriter(config["log"]["dir"])
    admin_state = AdminState()
    admin_state.engine = baseline_engine_snapshot(
        config,
        note="이 실행 방식(기본 모드)에서는 기본 엔진만 씁니다.",
    )
    monitor = MemoryMonitor(config, writer.session_dir)
    engine = BaselineEngine(config)
    source = AudioSegmentSource(config)
    defense = DefenseFilter(config)
    slang = SlangDictionary(config)
    ws_server = CaptionWebSocketServer(config) if enable_ws else None
    admin_server = AdminServer(config, admin_state) if enable_admin else None
    processed = 0
    hidden = 0
    discarded = 0
    started = time.monotonic()
    last_level_print = 0.0
    baseline_exit_code = 0
    session_archive = SessionArchiveRecorder(
        config,
        writer,
        source=str(config["audio"].get("source", "microphone")),
        engine=str(getattr(engine, "name", "baseline")),
        mode="baseline",
        analysis_sink=(
            ws_server.emit_event
            if ws_server is not None
            else None
        ),
    )

    def on_audio_chunk(info: dict) -> None:
        nonlocal discarded, last_level_print
        admin_state.last_audio = info
        if info.get("discard"):
            discarded += 1
            admin_state.discarded_segment_count = discarded
        should_print = show_levels and (
            time.monotonic() - last_level_print >= 1.0 or bool(info.get("discard"))
        )
        if should_print:
            last_level_print = time.monotonic()
            voice = "VOICE" if info["is_voice"] else "quiet"
            print(
                "\r{source} audio rms={rms:.5f} vad={vad_engine}:{vad_score:.3f}/{threshold:.3f} gain={gain:.1f} {voice}{discard}      ".format(
                    source=info.get("audio_source", "audio"),
                rms=info["rms"],
                    vad_engine=info.get("vad_engine", "energy"),
                    vad_score=info.get("vad_score", info["rms"]),
                    threshold=info["threshold"],
                    gain=info["input_gain"],
                    voice=voice,
                    discard=(
                        " discarded=" + info["discard"]["reason"]
                        if info.get("discard")
                        else ""
                    ),
                ),
                end="",
                flush=True,
            )

    if ws_server is not None:
        ws_server.start_background()
        ws_server.emit_status("caption websocket started")
    if admin_server is not None:
        admin_server.start_background()
        print(f"admin page: http://{config['server']['host']}:{config['server']['admin_port']}/admin")
    initial_memory = monitor.maybe_sample(force=True)
    if initial_memory is not None:
        writer.append_event(initial_memory)
        admin_state.last_memory = initial_memory.metadata

    source_name = config["audio"].get("source", "microphone")
    instruction = (
        "Play the broadcast on this PC."
        if source_name == "system"
        else "Speak into the microphone."
    )
    print(
        f"Live baseline mode. source={source_name}. "
        f"{instruction} Press Ctrl+C to stop."
    )
    if ws_server is not None:
        print(f"caption preview: overlay/caption.html  ws://{config['server']['host']}:{config['server']['port']}")
    print(
        "device={device}, threshold={threshold}, min={min_sec}s, silence={silence}s".format(
            device=config["audio"]["device"],
            threshold=config["audio"]["vad_energy_threshold"],
            min_sec=config["audio"]["min_segment_sec"],
            silence=config["audio"]["silence_duration_sec"],
        )
    )

    try:
        for segment in source.segments(max_seconds=max_seconds, on_chunk=on_audio_chunk):
            if admin_state.control.restart_requested:
                baseline_exit_code = RELAUNCH_EXIT_CODE
                print("\nRestart requested from the admin page.")
                break
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                print("\nMax seconds reached.")
                break
            processed += 1
            admin_state.segment_count = processed
            admin_state.last_segment = {
                "duration_sec": segment.duration_sec,
                "peak_rms": segment.peak_rms,
                "start_offset_sec": segment.start_offset_sec,
                "end_offset_sec": segment.end_offset_sec,
            }
            print(
                "\nsegment #{idx}: {duration:.2f}s peak_rms={rms:.5f}".format(
                    idx=processed,
                    duration=segment.duration_sec,
                    rms=segment.peak_rms,
                )
            )
            for event in engine.on_audio_segment(
                segment.samples,
                config["audio"]["sample_rate"],
                segment.start_offset_sec,
            ):
                event = replace(
                    event,
                    metadata={
                        **event.metadata,
                        "segment_duration_sec": segment.duration_sec,
                        "segment_peak_rms": segment.peak_rms,
                        "segment_voice_ratio": segment.voice_ratio,
                    },
                )
                for output_event in postprocess_event(event, defense, slang):
                    writer.append_event(
                        output_event,
                        extra={
                            "raw_text": event.text,
                            "segment_duration_sec": segment.duration_sec,
                            "segment_peak_rms": segment.peak_rms,
                            "segment_start_offset_sec": segment.start_offset_sec,
                            "segment_end_offset_sec": segment.end_offset_sec,
                        },
                    )
                    session_archive.record_event(output_event)
                    if ws_server is not None:
                        ws_server.emit_event(output_event)
                    if output_event.event_type == "caption_confirmed":
                        admin_state.caption_count += 1
                        admin_state.last_caption = output_event.text
                        print(f"caption: {output_event.text} (conf={output_event.confidence:.2f})")
                    elif output_event.event_type == "caption_hidden":
                        hidden += 1
                        admin_state.hidden_caption_count = hidden
                        print(
                            "caption_hidden: {text} reason={reason}".format(
                                text=output_event.text,
                                reason=output_event.metadata.get("defense_reason", ""),
                            )
                        )
                    elif output_event.event_type == "engine_error":
                        admin_state.last_error = output_event.text
                        print(f"engine_error: {output_event.text}")
                    else:
                        print(f"{output_event.event_type}: {output_event.text}")
                    if output_event.metadata.get("degraded_reason"):
                        admin_state.degraded_mode = True

            memory_event = monitor.maybe_sample()
            if memory_event is not None:
                writer.append_event(memory_event)
                admin_state.last_memory = memory_event.metadata

            if max_segments is not None and processed >= max_segments:
                break
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                print("\nMax seconds reached.")
                break
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as exc:
        baseline_exit_code = AUDIO_SOURCE_EXIT_CODE
        admin_state.last_error = str(exc)
        error_event = CaptionEvent(
            event_type="engine_error",
            text=str(exc),
            engine=str(getattr(engine, "name", "baseline")),
            metadata={"reason": "baseline_runtime_failure"},
        )
        writer.append_event(error_event)
        if ws_server is not None:
            ws_server.emit_event(error_event)
        print(f"error: {exc}")

    elapsed = time.monotonic() - started
    if show_levels:
        print()
    print(
        "summary: elapsed={elapsed:.1f}s segments={segments} captions={captions} hidden={hidden} discarded={discarded}".format(
            elapsed=elapsed,
            segments=processed,
            captions=admin_state.caption_count,
            hidden=admin_state.hidden_caption_count,
            discarded=admin_state.discarded_segment_count,
        )
    )
    if processed == 0 and admin_state.discarded_segment_count == 0:
        print("summary_note: no speech segment reached the VAD threshold during this run.")
    elif processed == 0:
        print("summary_note: audio crossed the VAD threshold, but segments were discarded before STT.")
    report_paths = session_archive.close(
        {
            "elapsed_sec": round(elapsed, 3),
            "segments": processed,
            "captions": admin_state.caption_count,
            "hidden": admin_state.hidden_caption_count,
            "discarded": admin_state.discarded_segment_count,
            "exit_code": baseline_exit_code,
        },
        relaunch=baseline_exit_code == RELAUNCH_EXIT_CODE,
    )
    if report_paths:
        print(f"session report: {report_paths['markdown']}")
    return baseline_exit_code


def run_simulstreaming_experiment(
    config: dict,
    max_segments: int | None,
    max_seconds: float | None,
    enable_ws: bool,
    enable_admin: bool,
    show_levels: bool,
) -> int:
    """START_ADVANCED menu AI: v2_light with the 정확 우선 engine for this run only."""
    runtime_config = deepcopy(config)
    runtime_config["streaming"]["engine"] = "simulstreaming"
    return run_v2_light(
        runtime_config,
        max_segments,
        max_seconds,
        enable_ws,
        enable_admin,
        show_levels,
    )


def baseline_engine_snapshot(config: dict, *, note: str = "") -> dict:
    """Admin/status view of the engine when no SimulStreaming route runs."""
    requested = str(config.get("streaming", {}).get("engine", "baseline") or "baseline")
    return {
        "requested_engine": requested,
        "active_engine": "baseline",
        "state": "active" if requested == "baseline" else "off",
        "reason": "" if requested == "baseline" else "unsupported_mode",
        "model": "",
        "model_setting": str(config.get("simulstreaming", {}).get("model", "large-v3-turbo")),
        "notice": note if requested != "baseline" else "",
    }


def create_engine_route(config: dict, source_names: tuple[str, ...], log_dir) -> Any:
    """The 정확 우선 route for the main source, or None for the 기본 engine.

    SimulStreaming needs one GPU model per audio stream, so in dual input it captions the
    microphone (the streamer) and PC audio stays on faster-whisper.
    """
    if str(config.get("streaming", {}).get("engine", "baseline")) != "simulstreaming":
        return None
    from engines.simulstreaming.route import SimulStreamingRoute

    source = "microphone" if "microphone" in source_names else source_names[0]
    return SimulStreamingRoute(config, source, log_dir=log_dir)


def run_v2_light(
    config: dict,
    max_segments: int | None,
    max_seconds: float | None,
    enable_ws: bool,
    enable_admin: bool,
    show_levels: bool,
) -> int:
    config, runtime_warning = prepare_runtime_config(config)
    writer = JsonlLogWriter(config["log"]["dir"])
    admin_state = AdminState()
    monitor = MemoryMonitor(config, writer.session_dir)
    engine = BaselineEngine(config)
    stt_worker, worker_start_warning = create_and_start_stt_worker(
        config,
        engine,
    )
    if worker_start_warning:
        admin_state.pipeline_warning = worker_start_warning
        writer.append_event(
            CaptionEvent(
                event_type="engine_status",
                text=worker_start_warning,
                engine=getattr(engine, "name", "baseline"),
                metadata={"worker_fallback": "thread"},
            )
        )
        print(f"warning: {worker_start_warning}")
    if runtime_warning:
        admin_state.pipeline_warning = runtime_warning
        writer.append_event(
            CaptionEvent(
                event_type="engine_status",
                text=runtime_warning,
                engine=getattr(engine, "name", "baseline"),
                metadata={"runtime_vad_override": "silero"},
            )
        )
        print(f"warning: {runtime_warning}")
    source = create_audio_chunk_source(config)
    source_mode = str(getattr(source, "source_name", config["audio"]["source"]))
    source_names = (
        ("microphone", "system")
        if source_mode == "both"
        else (source_mode,)
    )
    source_configs = {
        name: config_for_audio_source(config, name)
        for name in source_names
    }
    schedulers = {
        name: PendingWindowScheduler(source_configs[name])
        for name in source_names
    }
    preview_trackers = {name: PreviewTracker() for name in source_names}
    caption_config = config.get("caption", {})
    source_labels = {
        "microphone": str(caption_config.get("microphone_label", "방송자")),
        "system": str(caption_config.get("system_label", "컴퓨터")),
    }
    dual_config = config.get("audio", {}).get("dual", {})
    microphone_priority = bool(dual_config.get("microphone_priority", True))
    confirmed_ledger = ConfirmedRangeLedger()
    source_deduplicator = CrossSourceCaptionDeduplicator(config)
    defense = DefenseFilter(config)
    slang = SlangDictionary(config)
    ws_server = CaptionWebSocketServer(config) if enable_ws else None
    admin_server = AdminServer(config, admin_state) if enable_admin else None
    processed = 0
    discarded = 0
    started = time.monotonic()
    last_level_print = 0.0
    pipeline_exit_code = 0
    stopped_by_user = False
    warned_stt_job_id: int | None = None
    simul_route = create_engine_route(config, source_names, writer.session_dir)
    if simul_route is None:
        admin_state.engine = baseline_engine_snapshot(config)
    else:
        admin_state.engine = simul_route.snapshot()
    # Load the faster-whisper model now, in the STT worker, not with the first sentence (about
    # 15 s late). Nothing waits for it: the overlay and admin page start at once, and captions
    # queue behind it as they did behind the first job. With 정확 우선 it is the standby model,
    # loaded once: it captions while SimulStreaming loads and takes over at once if
    # SimulStreaming fails later. The result is not shown.
    import numpy as np

    warmup_source = simul_route.source_name if simul_route is not None else source_names[0]
    warmup_rate = int(source_configs[warmup_source]["audio"]["sample_rate"])
    stt_worker.submit(
        "confirmed",
        np.zeros(warmup_rate // 2, dtype=np.float32),
        warmup_rate,
        0.0,
        metadata={"model_warmup": True, "audio_source": warmup_source},
    )
    session_archive = SessionArchiveRecorder(
        config,
        writer,
        source=source_mode,
        engine=(
            "simulstreaming"
            if simul_route is not None and simul_route.state != "off"
            else str(getattr(engine, "name", "baseline"))
        ),
        mode="v2_light",
        analysis_sink=(
            ws_server.emit_event
            if ws_server is not None
            else None
        ),
    )

    def emit(output_event: CaptionEvent, extra: dict | None = None) -> None:
        writer.append_event(output_event, extra=extra)
        session_archive.record_event(output_event, extra=extra)
        if ws_server is not None:
            ws_server.emit_event(output_event)
        if output_event.event_type == "caption_pending":
            admin_state.pending_caption_count += 1
            print(f"pending: {output_event.text} (conf={output_event.confidence:.2f})")
        elif output_event.event_type == "caption_confirmed":
            admin_state.caption_count += 1
            admin_state.last_caption = output_event.text
            print(f"caption: {output_event.text} (conf={output_event.confidence:.2f})")
        elif output_event.event_type == "caption_hidden":
            admin_state.hidden_caption_count += 1
            print(
                "caption_hidden: {text} reason={reason}".format(
                    text=output_event.text,
                    reason=output_event.metadata.get("defense_reason", ""),
                )
            )
        elif output_event.event_type == "engine_error":
            admin_state.last_error = output_event.text
            print(f"engine_error: {output_event.text}")
        else:
            print(f"{output_event.event_type}: {output_event.text}")
        if output_event.metadata.get("degraded_reason"):
            admin_state.degraded_mode = True

    def emit_caption(
        output_event: CaptionEvent,
        extra: dict | None = None,
    ) -> None:
        duplicate = source_deduplicator.check(output_event)
        if duplicate is None:
            emit(output_event, extra=extra)
            return
        emit(
            CaptionEvent(
                event_type="caption_hidden",
                text=output_event.text,
                confidence=output_event.confidence,
                audio_offset_sec=output_event.audio_offset_sec,
                engine=output_event.engine,
                metadata={
                    **output_event.metadata,
                    "defense_reason": "cross_source_duplicate",
                    "duplicate_source": duplicate.matched_source,
                    "duplicate_similarity": round(duplicate.similarity, 4),
                    "duplicate_offset_delta_sec": round(
                        duplicate.offset_delta_sec,
                        4,
                    ),
                },
            ),
            extra=extra,
        )
    def emit_worker_result(result: SttResult) -> None:
        nonlocal warned_stt_job_id
        if (
            warned_stt_job_id == result.job.job_id
            or result.job.metadata.get("recovered")
        ):
            # Also for the warm-up: a slow model load must not leave its warning up.
            warned_stt_job_id = None
            admin_state.pipeline_warning = ""
        if result.job.metadata.get("model_warmup"):
            if simul_route is not None:
                simul_route.standby_ready()
            return
        snapshot = stt_worker.snapshot()
        admin_state.last_stt = {
            **snapshot,
            "job_id": result.job.job_id,
            "job_type": result.job.job_type,
            "elapsed_sec": result.elapsed_sec,
            "event_count": len(result.events),
        }
        if result.job.job_type == "pending":
            tracker = preview_trackers.get(str(result.job.metadata.get("audio_source", "")))
            for event in result.events:
                for output_event in preview_events(
                    event, defense, slang, tracker, result.job.metadata
                ):
                    emit_caption(
                        output_event,
                        extra={
                            **result.job.metadata,
                            "stt_elapsed_sec": result.elapsed_sec,
                            "stt_job_id": result.job.job_id,
                            "stt_job_type": result.job.job_type,
                        },
                    )
            return
        if result.job.job_type == "confirmed":
            if not confirmed_ledger.claim(
                result.job.job_id,
                result.job.metadata,
            ):
                ledger_state = confirmed_ledger.snapshot()
                admin_state.last_stt = {
                    **admin_state.last_stt,
                    **ledger_state,
                }
                print(
                    "duplicate confirmed result suppressed: "
                    f"job_id={result.job.job_id}"
                )
                return
            admin_state.last_stt = {
                **admin_state.last_stt,
                **confirmed_ledger.snapshot(),
            }
            for event in result.events:
                for output_event in postprocess_event(event, defense, slang):
                    emit_caption(
                        output_event,
                        extra={
                            **result.job.metadata,
                            "raw_text": event.text,
                            "stt_elapsed_sec": result.elapsed_sec,
                            "stt_job_id": result.job.job_id,
                            "stt_job_type": result.job.job_type,
                        },
                    )

    def print_level(info: dict) -> None:
        nonlocal last_level_print
        should_print = show_levels and (
            time.monotonic() - last_level_print >= 1.0 or bool(info.get("discard"))
        )
        if not should_print:
            return
        last_level_print = time.monotonic()
        voice = "VOICE" if info["is_voice"] else "quiet"
        print(
            "\r{source} audio rms={rms:.5f} vad={vad_engine}:{vad_score:.3f}/{threshold:.3f} gain={gain:.1f} {voice}{discard}      ".format(
                source=info.get("audio_source", "audio"),
                rms=info["rms"],
                vad_engine=info.get("vad_engine", "energy"),
                vad_score=info.get("vad_score", info["rms"]),
                threshold=info["threshold"],
                gain=info["input_gain"],
                voice=voice,
                discard=(
                    " discarded=" + info["discard"]["reason"]
                    if info.get("discard")
                    else ""
                ),
            ),
            end="",
            flush=True,
        )

    if ws_server is not None:
        ws_server.start_background()
        ws_server.emit_status("caption websocket started")
    if admin_server is not None:
        admin_server.start_background()
        print(f"admin page: http://{config['server']['host']}:{config['server']['admin_port']}/admin")

    initial_memory = monitor.maybe_sample(force=True)
    if initial_memory is not None:
        writer.append_event(initial_memory)
        admin_state.last_memory = initial_memory.metadata

    source_name = source_mode
    source_instruction = (
        "Speak into the microphone and play PC audio."
        if source_name == "both"
        else (
            "Play the broadcast on this PC."
            if source_name == "system"
            else "Speak into the microphone."
        )
    )
    print(
        f"Live v2_light mode. source={source_name}. "
        f"{source_instruction} Press Ctrl+C to stop."
    )
    if ws_server is not None:
        print(f"caption preview: overlay/caption.html  ws://{config['server']['host']}:{config['server']['port']}")
    print(
        "device={device}, threshold={threshold}, chunk={chunk}s, "
        "preview from {min_age}s into a sentence, window up to {max_window}s".format(
            device=config["audio"]["device"],
            threshold=config["audio"]["vad_energy_threshold"],
            chunk=config["streaming"]["chunk_seconds"],
            min_age=config["streaming"]["pending_min_utterance_sec"],
            max_window=config["streaming"]["pending_max_window_sec"],
        )
    )

    def event_source_metadata(source_name: str) -> dict:
        source_priority = (
            0
            if source_name == "microphone" or not microphone_priority
            else 1
        )
        return {
            "audio_source": source_name,
            "audio_mode": source_mode,
            "source_label": (
                source_labels.get(source_name, source_name)
                if source_mode == "both"
                else ""
            ),
            "source_priority": source_priority,
        }

    def submit_confirmed_segment(
        segment: AudioSegment,
        segment_source: str,
        segment_scheduler: PendingWindowScheduler,
        extra_metadata: dict | None = None,
    ) -> None:
        sample_rate = int(source_configs[segment_source]["audio"]["sample_rate"])
        stt_worker.submit(
            "confirmed",
            segment.samples,
            sample_rate,
            segment.start_offset_sec,
            metadata={
                **event_source_metadata(segment_source),
                "segment_duration_sec": segment.duration_sec,
                "segment_peak_rms": segment.peak_rms,
                "segment_voice_ratio": segment.voice_ratio,
                "segment_start_offset_sec": segment.start_offset_sec,
                "segment_end_offset_sec": segment.end_offset_sec,
                "segment_start_sample": int(
                    round(segment.start_offset_sec * sample_rate)
                ),
                "segment_end_sample": int(
                    round(segment.end_offset_sec * sample_rate)
                ),
                "capture_ring_end_sample": segment_scheduler.ring.end_sample,
                **(extra_metadata or {}),
            },
        )

    def handle_action(
        action: StreamingAction,
        action_source: str,
        action_scheduler: PendingWindowScheduler,
    ) -> None:
        nonlocal discarded, processed
        source_metadata = event_source_metadata(action_source)
        routed = simul_route is not None and action_source == simul_route.source_name
        if action.action_type == "discarded_segment":
            discarded += 1
            admin_state.discarded_segment_count = discarded
            return
        if action.action_type == "pending_window" and action.window is not None:
            if routed and simul_route.owns_captions:
                return  # SimulStreaming shows this source's words itself
            streaming = source_configs[action_source]["streaming"]
            if not bool(streaming.get("pending_enabled", True)):
                return
            if (
                source_mode == "both"
                and action_source == "system"
                and not bool(dual_config.get("system_pending_enabled", False))
            ):
                return
            if (
                bool(streaming.get("pending_skip_when_busy", True))
                and not stt_worker.can_accept_pending()
            ):
                stt_worker.mark_pending_skipped()
                return
            stt_worker.submit_latest(
                "pending",
                action.window.samples,
                action.window.sample_rate,
                action.window.start_offset_sec,
                metadata={
                    **source_metadata,
                    **(action.metadata or {}),
                    "window_start_offset_sec": action.window.start_offset_sec,
                    "window_end_offset_sec": action.window.end_offset_sec,
                    "window_duration_sec": action.window.duration_sec,
                },
            )
            return
        if action.action_type == "confirmed_segment" and action.segment is not None:
            # Gray previews of this sentence's audio are stale from now on.
            preview_trackers[action_source].confirmed(action.segment.end_offset_sec)
            processed += 1
            admin_state.segment_count = processed
            admin_state.last_segment = {
                **source_metadata,
                "duration_sec": action.segment.duration_sec,
                "peak_rms": action.segment.peak_rms,
                "voice_ratio": action.segment.voice_ratio,
                "start_offset_sec": action.segment.start_offset_sec,
                "end_offset_sec": action.segment.end_offset_sec,
            }
            print(
                "\nconfirmed {source} segment #{idx}: "
                "{duration:.2f}s peak_rms={rms:.5f}".format(
                    source=action_source,
                    idx=processed,
                    duration=action.segment.duration_sec,
                    rms=action.segment.peak_rms,
                )
            )
            segment: AudioSegment | None = action.segment
            if routed:
                if simul_route.claims(action.segment):
                    # Kept until SimulStreaming has captioned past it; replayed on a fallback.
                    simul_route.hold_segment(action.segment)
                    return
                segment = simul_route.trim_segment(action.segment)
                if segment is None:
                    return
            submit_confirmed_segment(segment, action_source, action_scheduler)

    def handle_route_output(output: Any) -> None:
        if simul_route is None:
            return
        route_source = simul_route.source_name
        if output.kind == "caption" and output.event is not None:
            metadata = {**output.event.metadata, **event_source_metadata(route_source)}
            share = line_speech_share(metadata, schedulers[route_source].speech)
            if share is not None:
                metadata["speech_share"] = share
            event = replace(output.event, metadata=metadata)
            converted = (
                pending_events(event, defense, slang, min_chars=1, allow_single_char=True)
                if output.pending
                else postprocess_event(event, defense, slang, allow_single_char=True)
            )
            for output_event in converted:
                emit_caption(
                    output_event,
                    extra=None if output.pending else {"raw_text": event.text, "caption_engine": "simulstreaming"},
                )
            return
        if output.kind == "status":
            status = dict(output.status)
            writer.append_event(
                CaptionEvent(
                    event_type="engine_status",
                    text=output.text,
                    engine="simulstreaming",
                    metadata=status,
                )
            )
            print()
            print(
                "caption engine: {state}{reason}{model}".format(
                    state=status.get("state", ""),
                    reason=f" reason={status['reason']}" if status.get("reason") else "",
                    model=f" model={status['model']}" if status.get("model") else "",
                )
            )
            if status.get("state") in {"fallback", "off"} and status.get("reason"):
                admin_state.pipeline_warning = simul_route.notice()
            elif status.get("state") == "active" and admin_state.pipeline_warning == simul_route.notice():
                admin_state.pipeline_warning = ""
            return
        if output.kind == "replay":
            for replay_segment in output.segments:
                submit_confirmed_segment(
                    replay_segment,
                    route_source,
                    schedulers[route_source],
                    {"replayed_after_fallback": True},
                )
            print(f"caption engine: {len(output.segments)} segment(s) handed back to faster-whisper")

    try:
        for chunk in source.chunks(max_seconds=max_seconds):
            if admin_state.control.restart_requested:
                pipeline_exit_code = RELAUNCH_EXIT_CODE
                stopped_by_user = True
                print("\nRestart requested from the admin page.")
                break
            chunk_source = str(
                getattr(chunk, "source_name", "") or source_name
            )
            if chunk_source not in schedulers:
                continue
            scheduler = schedulers[chunk_source]
            actions = scheduler.push(chunk)
            decision = getattr(scheduler.segmenter, "last_decision", None)
            input_source = (
                source.sources.get(chunk_source, source)
                if source_mode == "both"
                else source
            )
            source_restarts = int(getattr(input_source, "restart_count", 0))
            source_last_error = str(getattr(input_source, "last_error", ""))
            info = {
                "rms": chunk.rms,
                "threshold": scheduler.segmenter.threshold,
                "is_voice": bool(decision.is_voice)
                if decision is not None
                else chunk.rms >= scheduler.segmenter.threshold,
                "offset_sec": chunk.offset_sec,
                "duration_sec": chunk.duration_sec,
                "dropped_chunks": int(getattr(input_source, "dropped_chunks", 0)),
                "status": chunk.status,
                "clip_fraction": chunk.clip_fraction,
                "input_gain": float(getattr(input_source, "input_gain", 1.0)),
                "audio_source": chunk_source,
                "source_restarts": source_restarts,
                "source_last_error": source_last_error,
                # PC sound: reopens after the loopback recorder stalled, how often and how long
                # silence was filled in meanwhile, SoundCard silence dropped for a reader that
                # woke late, audio dropped to keep the clock on wall time, and capturing /
                # silence / reopening / stuck / error.
                "source_stall_restarts": int(getattr(input_source, "stall_restart_count", 0)),
                "silence_fill_count": int(getattr(input_source, "silence_fill_count", 0)),
                "silence_filled_sec": round(float(getattr(input_source, "silence_filled_sec", 0.0)), 1),
                "false_silence_sec": round(float(getattr(input_source, "false_silence_sec", 0.0)), 1),
                "clock_trimmed_sec": round(float(getattr(input_source, "clock_trimmed_sec", 0.0)), 1),
                "capture_state": str(getattr(input_source, "capture_state", "")),
                "device_label": getattr(input_source, "device_label", ""),
                "vad_engine": getattr(
                    decision,
                    "engine",
                    getattr(scheduler.segmenter, "engine", "energy"),
                ),
                "vad_score": getattr(decision, "score", chunk.rms),
                "discard": None,
            }
            admin_state.last_audio = info
            admin_state.audio_sources[chunk_source] = info
            if source_mode == "both" and hasattr(source, "source_statuses"):
                for status_source, status_info in source.source_statuses().items():
                    admin_state.audio_sources.setdefault(status_source, {}).update(
                        status_info
                    )
            admin_state.audio_restart_count = int(
                getattr(source, "restart_count", source_restarts)
            )
            print_level(info)
            for action in actions:
                if action.discard is not None:
                    info = {**info, "discard": action.discard}
                    admin_state.last_audio = info
                    admin_state.audio_sources[chunk_source] = info
                    print_level(info)
                handle_action(action, chunk_source, scheduler)
            if simul_route is not None and chunk_source == simul_route.source_name:
                # Switch engines only in a pause: no segment open and no final job queued, so
                # one sentence is never split between them and offsets stay in order.
                worker_state = stt_worker.snapshot()
                fw_busy = (
                    worker_state.get("active_job_type") == "confirmed"
                    or int(worker_state.get("confirmed_queue", 0) or 0) > 0
                )
                try:
                    simul_route.feed(
                        chunk,
                        can_switch=not actions and not scheduler.segmenter.in_segment and not fw_busy,
                    )
                except Exception as exc:  # never let the optional engine stop captions
                    print(f"\nwarning: caption engine error: {type(exc).__name__}: {exc}")
                    for route_output in simul_route.fail_internal(exc):
                        handle_route_output(route_output)
            for result in stt_worker.get_results():
                emit_worker_result(result)
            if simul_route is not None:
                try:
                    route_outputs = simul_route.poll()
                except Exception as exc:
                    print(f"\nwarning: caption engine error: {type(exc).__name__}: {exc}")
                    route_outputs = simul_route.fail_internal(exc)
                for route_output in route_outputs:
                    handle_route_output(route_output)
                admin_state.engine = simul_route.snapshot()

            snapshot = stt_worker.snapshot()
            admin_state.last_stt = {
                **admin_state.last_stt,
                **snapshot,
            }
            active_job_id = snapshot["active_job_id"]
            active_elapsed = float(snapshot["active_elapsed_sec"])
            stall_warn_sec = float(
                config["streaming"].get("stt_stall_warn_sec", 20.0)
            )
            stall_restart_sec = float(
                config["streaming"].get("stt_stall_restart_sec", 60.0)
            )
            cold_start_restart_sec = float(
                config["streaming"].get(
                    "stt_cold_start_restart_sec",
                    300.0,
                )
            )
            process_start_restart_sec = float(
                config["streaming"].get(
                    "stt_process_start_restart_sec",
                    60.0,
                )
            )
            fatal_error = str(snapshot.get("fatal_error", ""))
            if fatal_error:
                pipeline_exit_code = STT_STALL_EXIT_CODE
                admin_state.pipeline_warning = fatal_error
                print()
                print(f"error: {fatal_error}")
                break

            process_state = str(snapshot.get("process_state", ""))
            process_start_elapsed = float(
                snapshot.get("process_start_elapsed_sec", 0.0)
            )
            if (
                process_state == "starting"
                and process_start_elapsed >= process_start_restart_sec
            ):
                restart_method = getattr(
                    stt_worker,
                    "restart_stalled_job",
                    None,
                )
                reason = (
                    "STT child startup stalled for "
                    f"{process_start_elapsed:.1f}s"
                )
                if callable(restart_method) and restart_method(reason):
                    admin_state.pipeline_warning = (
                        f"{reason}; child restart requested."
                    )
                    print()
                    print(f"warning: {admin_state.pipeline_warning}")
                else:
                    pipeline_exit_code = STT_STALL_EXIT_CODE
                    admin_state.pipeline_warning = reason
                    print()
                    print(f"error: {reason}")
                    break

            worker_mode = str(snapshot.get("worker_mode", ""))
            controller_alive = bool(
                snapshot.get("controller_alive", True)
            )
            if worker_mode == "process" and not controller_alive:
                pipeline_exit_code = STT_STALL_EXIT_CODE
                admin_state.pipeline_warning = (
                    "STT controller stopped unexpectedly; "
                    "requesting whole-process restart."
                )
                print()
                print(f"error: {admin_state.pipeline_warning}")
                break

            heartbeat_age_raw = snapshot.get("child_heartbeat_age_sec")
            heartbeat_age = (
                float(heartbeat_age_raw)
                if isinstance(heartbeat_age_raw, (int, float))
                else None
            )
            heartbeat_timeout = float(
                config["streaming"].get(
                    "stt_heartbeat_timeout_sec",
                    12.0,
                )
            )
            if (
                worker_mode == "process"
                and process_state in {"ready", "busy"}
                and heartbeat_age is not None
                and heartbeat_age >= heartbeat_timeout
            ):
                reason = (
                    "STT child heartbeat missing for "
                    f"{heartbeat_age:.1f}s"
                )
                restart_method = getattr(
                    stt_worker,
                    "restart_stalled_job",
                    None,
                )
                if callable(restart_method) and restart_method(reason):
                    process_state = "restart_requested"
                    warned_stt_job_id = None
                    admin_state.pipeline_warning = (
                        f"{reason}; STT child restart requested. "
                        "Confirmed audio remains queued."
                    )
                    print()
                    print(f"warning: {admin_state.pipeline_warning}")
                else:
                    pipeline_exit_code = STT_STALL_EXIT_CODE
                    admin_state.pipeline_warning = (
                        f"{reason}; requesting whole-process restart."
                    )
                    print()
                    print(f"error: {admin_state.pipeline_warning}")
                    break

            if (
                active_job_id is not None
                and active_elapsed >= stall_warn_sec
                and warned_stt_job_id != active_job_id
            ):
                warned_stt_job_id = active_job_id
                admin_state.pipeline_warning = (
                    f"STT job {active_job_id} is slow "
                    f"({active_elapsed:.1f}s). Monitoring for recovery."
                )
                print()
                print(f"warning: {admin_state.pipeline_warning}")

            active_restart_sec = (
                cold_start_restart_sec
                if bool(snapshot.get("active_cold_start", False))
                else stall_restart_sec
            )
            if (
                active_job_id is not None
                and active_elapsed >= active_restart_sec
                and process_state != "restart_requested"
            ):
                reason = (
                    f"STT job {active_job_id} stalled for "
                    f"{active_elapsed:.1f}s"
                )
                restart_method = getattr(
                    stt_worker,
                    "restart_stalled_job",
                    None,
                )
                if callable(restart_method) and restart_method(reason):
                    warned_stt_job_id = None
                    admin_state.pipeline_warning = (
                        f"{reason}; STT child restart requested. "
                        "Confirmed audio remains queued."
                    )
                    print()
                    print(f"warning: {admin_state.pipeline_warning}")
                else:
                    pipeline_exit_code = STT_STALL_EXIT_CODE
                    admin_state.pipeline_warning = (
                        f"{reason}; requesting whole-process restart."
                    )
                    print()
                    print(f"error: {admin_state.pipeline_warning}")
                    break
            memory_event = monitor.maybe_sample()
            if memory_event is not None:
                writer.append_event(memory_event)
                admin_state.last_memory = memory_event.metadata
            if max_segments is not None and processed >= max_segments:
                break
    except KeyboardInterrupt:
        stopped_by_user = True
        print()
        print("Stopped.")
    except Exception as exc:
        pipeline_exit_code = AUDIO_SOURCE_EXIT_CODE
        admin_state.pipeline_warning = (
            f"Audio source failed: {exc}. Requesting process restart."
        )
        emit(
            CaptionEvent(
                event_type="engine_error",
                text=str(exc),
                engine=getattr(engine, "name", "baseline"),
                metadata={
                    "reason": "audio_source_failure",
                    "audio_source": getattr(
                        source,
                        "source_name",
                        config["audio"]["source"],
                    ),
                },
            )
        )

    graceful_end = not stopped_by_user and pipeline_exit_code == 0
    if graceful_end:
        for flush_source, flush_scheduler in schedulers.items():
            for action in flush_scheduler.flush():
                handle_action(action, flush_source, flush_scheduler)
    if simul_route is not None:
        # Before the faster-whisper drain: a fallback here can still hand segments back.
        try:
            for route_output in simul_route.finish(graceful=graceful_end):
                handle_route_output(route_output)
        except Exception as exc:  # the captions already shown matter more than the last line
            print(f"warning: caption engine shutdown failed: {type(exc).__name__}: {exc}")
        simul_route.close()
        admin_state.engine = simul_route.snapshot()
    if graceful_end:
        drain_timeout = max(
            float(
                config["streaming"].get(
                    "stt_stall_restart_sec",
                    60.0,
                )
            ),
            float(
                config["streaming"].get(
                    "stt_cold_start_restart_sec",
                    300.0,
                )
            ),
        )
        if not stt_worker.wait_until_idle(timeout=drain_timeout):
            pipeline_exit_code = STT_STALL_EXIT_CODE
            admin_state.pipeline_warning = (
                f"STT worker did not drain within {drain_timeout:.0f}s; "
                "requesting process restart."
            )
            print(f"warning: {admin_state.pipeline_warning}")
    for result in stt_worker.get_results(max_items=10_000):
        emit_worker_result(result)
    stt_worker.stop(
        timeout=2.0 if stopped_by_user or pipeline_exit_code else 30.0
    )
    final_worker_state = stt_worker.snapshot()
    final_ledger_state = confirmed_ledger.snapshot()
    writer.append_event(
        CaptionEvent(
            event_type="engine_status",
            text="v2_light session summary",
            engine=getattr(engine, "name", "baseline"),
            metadata={
                **final_worker_state,
                **final_ledger_state,
                "audio_source": getattr(
                    source,
                    "source_name",
                    config["audio"]["source"],
                ),
                "audio_source_restarts": int(
                    getattr(source, "restart_count", 0)
                ),
                "audio_stall_restarts": int(getattr(source, "stall_restart_count", 0)),
                "audio_silence_fill_count": int(getattr(source, "silence_fill_count", 0)),
                "audio_silence_filled_sec": round(float(getattr(source, "silence_filled_sec", 0.0)), 1),
                "audio_false_silence_sec": round(float(getattr(source, "false_silence_sec", 0.0)), 1),
                "audio_clock_trimmed_sec": round(float(getattr(source, "clock_trimmed_sec", 0.0)), 1),
                "audio_source_statuses": (
                    source.source_statuses()
                    if hasattr(source, "source_statuses")
                    else {}
                ),
                "pipeline_exit_code": pipeline_exit_code,
                "caption_engine": admin_state.engine,
            },
        )
    )
    elapsed = time.monotonic() - started
    if show_levels:
        print()
    print(
        "summary: mode=v2_light elapsed={elapsed:.1f}s segments={segments} captions={captions} pending={pending} hidden={hidden} discarded={discarded}".format(
            elapsed=elapsed,
            segments=processed,
            captions=admin_state.caption_count,
            pending=admin_state.pending_caption_count,
            hidden=admin_state.hidden_caption_count,
            discarded=admin_state.discarded_segment_count,
        )
    )
    if stt_worker.superseded_pending_jobs:
        print(
            "summary_note: superseded pending previews="
            f"{stt_worker.superseded_pending_jobs} "
            "(confirmed jobs were preserved)"
        )
    if stt_worker.skipped_pending_jobs:
        print(
            "summary_note: skipped pending previews while STT was busy="
            f"{stt_worker.skipped_pending_jobs}"
        )
    restart_count = int(getattr(source, "restart_count", 0))
    if restart_count:
        print(f"summary_note: audio source restarts={restart_count}")
    process_restarts = int(
        getattr(stt_worker, "process_restarts", 0)
    )
    recovered_jobs = int(
        getattr(stt_worker, "recovered_confirmed_jobs", 0)
    )
    if process_restarts:
        print(f"summary_note: STT child restarts={process_restarts}")
    if recovered_jobs:
        print(
            "summary_note: recovered confirmed STT jobs="
            f"{recovered_jobs}"
        )
    ledger_state = confirmed_ledger.snapshot()
    if ledger_state["duplicate_confirmed_results"]:
        print(
            "summary_note: duplicate confirmed results suppressed="
            f"{ledger_state['duplicate_confirmed_results']}"
        )
    if pipeline_exit_code == RELAUNCH_EXIT_CODE:
        print("summary_note: restarting on request (settings or update)")
    elif pipeline_exit_code:
        print(f"summary_note: recovery exit code={pipeline_exit_code}")
    report_paths = session_archive.close(
        {
            "elapsed_sec": round(elapsed, 3),
            "segments": processed,
            "captions": admin_state.caption_count,
            "pending": admin_state.pending_caption_count,
            "hidden": admin_state.hidden_caption_count,
            "discarded": admin_state.discarded_segment_count,
            "pipeline_exit_code": pipeline_exit_code,
            "stopped_by_user": stopped_by_user,
        },
        relaunch=pipeline_exit_code == RELAUNCH_EXIT_CODE,
    )
    if report_paths:
        print(f"session report: {report_paths['markdown']}")
    return pipeline_exit_code


def line_speech_share(metadata: dict, timeline: Any) -> float | None:
    """How much of a 정확 우선 line's audio the app's own voice detector heard as speech
    (SpeechTimeline.share_around over line_start/end_offset_sec), for DefenseFilter."""
    end = metadata.get("line_end_offset_sec")
    if timeline is None or isinstance(end, bool) or not isinstance(end, (int, float)):
        return None
    start = metadata.get("line_start_offset_sec")
    if isinstance(start, bool) or not isinstance(start, (int, float)) or start > end:
        start = float(end) - 1.0  # no word start times: judge the last second
    share = timeline.share_around(float(start), float(end))
    return None if share is None else round(share, 3)


def postprocess_event(
    event: CaptionEvent,
    defense: DefenseFilter,
    slang: SlangDictionary,
    *,
    allow_single_char: bool = False,
) -> list[CaptionEvent]:
    if event.event_type != "caption_confirmed":
        return [event]

    corrected = slang.apply(event.text)
    defense_result = defense.apply(
        corrected,
        event.confidence,
        {**event.metadata, "audio_offset_sec": event.audio_offset_sec},
        allow_single_char=allow_single_char,
    )
    defense_metadata = {
        "defense_reason": defense_result.reason,
        "defense_score": defense_result.risk_score,
        "defense_signals": list(defense_result.signals),
    }
    if not defense_result.visible:
        return [
            CaptionEvent(
                event_type="caption_hidden",
                text=corrected,
                confidence=event.confidence,
                audio_offset_sec=event.audio_offset_sec,
                engine=event.engine,
                metadata={**event.metadata, **defense_metadata},
            )
        ]

    max_chars = int(defense.config.get("caption", {}).get("max_chars_per_line", 28))
    chunks = split_sentences(defense_result.text, max_chars=max_chars)
    return [
        CaptionEvent(
            event_type="caption_confirmed",
            text=chunk,
            confidence=event.confidence,
            audio_offset_sec=event.audio_offset_sec,
            engine=event.engine,
            metadata={
                **event.metadata,
                "raw_text": event.text,
                "corrected_text": corrected,
                "split_index": index,
                "split_count": len(chunks),
                "opacity": defense_result.opacity,
                **defense_metadata,
            },
        )
        for index, chunk in enumerate(chunks)
    ]


def preview_events(
    event: CaptionEvent,
    defense: DefenseFilter,
    slang: SlangDictionary,
    tracker: PreviewTracker | None,
    job_metadata: dict,
) -> list[CaptionEvent]:
    """A faster-whisper gray preview (core/preview.py): whole sentences that are only an outro
    phrase or a subtitle credit are removed first ("오늘 날씨가. 감사합니다." shows "오늘 날씨가."),
    a preview of audio already sent for its final caption is dropped, and metadata stable_chars
    marks the words this preview and the previous one of the same window agree on."""
    if event.event_type != "caption_confirmed":
        return pending_events(event, defense, slang)
    text = strip_phantom_sentences(event.text)
    if not text:
        return []
    outputs = pending_events(replace(event, text=text), defense, slang)
    if tracker is None:
        return outputs
    shown: list[CaptionEvent] = []
    for output in outputs:
        if output.event_type != "caption_pending":
            shown.append(output)
            continue
        update = tracker.accept(output.text, job_metadata)
        if update is None:
            continue
        shown.append(
            replace(
                output,
                text=update.text,
                metadata={
                    **output.metadata,
                    "raw_text": event.text,
                    "stable_chars": update.stable_chars,
                },
            )
        )
    return shown


def pending_events(
    event: CaptionEvent,
    defense: DefenseFilter,
    slang: SlangDictionary,
    *,
    min_chars: int | None = None,
    allow_single_char: bool = False,
) -> list[CaptionEvent]:
    """min_chars / allow_single_char: the 정확 우선 engine's growing line is never revised,
    so a one-syllable start ("네") is shown instead of waiting for the next word."""
    if event.event_type == "engine_error":
        return [event]
    if event.event_type != "caption_confirmed":
        return []

    corrected = slang.apply(event.text)
    defense_result = defense.apply(
        corrected,
        event.confidence,
        {**event.metadata, "audio_offset_sec": event.audio_offset_sec},
        remember=False,
        allow_single_char=allow_single_char,
    )
    if not defense_result.visible:
        return []
    if min_chars is None:
        min_chars = int(defense.config.get("streaming", {}).get("pending_min_chars", 2))
    if len(defense_result.text.strip()) < min_chars:
        return []
    return [
        CaptionEvent(
            event_type="caption_pending",
            text=defense_result.text,
            confidence=event.confidence,
            audio_offset_sec=event.audio_offset_sec,
            engine=event.engine,
            metadata={
                **event.metadata,
                "raw_text": event.text,
                "corrected_text": corrected,
                "opacity": defense_result.opacity,
                "defense_reason": defense_result.reason,
                "defense_score": defense_result.risk_score,
                "defense_signals": list(defense_result.signals),
            },
        )
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="streamer-caption-v2 baseline runner")
    parser.add_argument("--status-only", action="store_true")
    parser.add_argument("--max-segments", type=int)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--no-ws", action="store_true")
    parser.add_argument("--no-admin", action="store_true")
    parser.add_argument("--show-levels", action="store_true")
    parser.add_argument("--v2-light", action="store_true")
    parser.add_argument("--simulstreaming-experiment", action="store_true")
    parser.add_argument(
        "--audio-source",
        choices=("microphone", "system", "both"),
        help="Override audio.source for this run.",
    )
    args = parser.parse_args()

    config = load_config()
    if args.audio_source:
        config["audio"]["source"] = args.audio_source
    if args.status_only:
        return run_status_check(config)
    if args.simulstreaming_experiment:
        return run_simulstreaming_experiment(
            config,
            args.max_segments,
            args.max_seconds,
            not args.no_ws,
            not args.no_admin,
            args.show_levels,
        )
    if args.v2_light:
        return run_v2_light(
            config,
            args.max_segments,
            args.max_seconds,
            not args.no_ws,
            not args.no_admin,
            args.show_levels,
        )
    return run_live(
        config,
        args.max_segments,
        args.max_seconds,
        not args.no_ws,
        not args.no_admin,
        args.show_levels,
    )


if __name__ == "__main__":
    raise SystemExit(main())
