from __future__ import annotations

import argparse
import time
from copy import deepcopy
from dataclasses import replace

from core.audio_capture import AudioSegmentSource
from core.audio_sources import config_for_audio_source, create_audio_chunk_source
from core.admin_server import AdminServer, AdminState
from core.app_info import RELAUNCH_EXIT_CODE
from core.config import load_config
from core.events import CaptionEvent
from core.log_writer import JsonlLogWriter
from core.monitoring import MemoryMonitor
from core.session_archive import SessionArchiveRecorder
from core.source_dedup import CrossSourceCaptionDeduplicator
from core.recovery import ConfirmedRangeLedger
from core.stt_worker import SttResult
from core.stt_worker_factory import create_and_start_stt_worker
from core.streaming import PendingWindowScheduler, StreamingAction
from core.text_processing import split_sentences
from core.ws_server import CaptionWebSocketServer
from engines.baseline.engine import BaselineEngine
from engines.simulstreaming.client import SimulStreamingClient, SimulStreamingError
from features.defense import DefenseFilter
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
    """Run the official SimulStreaming server as an isolated experiment.

    Unlike v2_light, the server receives every consecutive audio chunk and owns
    its own VAC/partial-result state. This keeps the experiment faithful to the
    upstream streaming protocol while preserving the baseline path unchanged.
    """
    writer = JsonlLogWriter(config["log"]["dir"])
    admin_state = AdminState()
    monitor = MemoryMonitor(config, writer.session_dir)
    source = create_audio_chunk_source(config)
    defense = DefenseFilter(config)
    slang = SlangDictionary(config)
    client = SimulStreamingClient(config)
    ws_server = CaptionWebSocketServer(config) if enable_ws else None
    admin_server = AdminServer(config, admin_state) if enable_admin else None
    confirmed_count = 0
    pending_count = 0
    hidden_count = 0
    started = time.monotonic()
    last_partial_text = ""
    last_partial_end = 0.0
    stopped_by_user = False
    exit_code = 0
    session_archive = SessionArchiveRecorder(
        config,
        writer,
        source=str(getattr(source, "source_name", config["audio"]["source"])),
        engine=str(client.name),
        mode="simulstreaming_experiment",
        analysis_sink=(
            ws_server.emit_event
            if ws_server is not None
            else None
        ),
    )

    def emit(event: CaptionEvent) -> None:
        nonlocal pending_count, confirmed_count, hidden_count
        writer.append_event(event)
        session_archive.record_event(event)
        if ws_server is not None:
            ws_server.emit_event(event)
        if event.event_type == "caption_pending":
            pending_count += 1
            admin_state.pending_caption_count = pending_count
            print(f"pending: {event.text}")
        elif event.event_type == "caption_confirmed":
            confirmed_count += 1
            admin_state.caption_count = confirmed_count
            admin_state.last_caption = event.text
            print(f"caption: {event.text}")
        elif event.event_type == "caption_hidden":
            hidden_count += 1
            admin_state.hidden_caption_count = hidden_count
            print(
                "caption_hidden: {text} reason={reason}".format(
                    text=event.text,
                    reason=event.metadata.get("defense_reason", ""),
                )
            )
        elif event.event_type == "engine_error":
            admin_state.last_error = event.text
            print(f"engine_error: {event.text}")

    def emit_confirmed(text: str, end_sec: float, metadata: dict) -> None:
        if not text:
            return
        event = CaptionEvent(
            event_type="caption_confirmed",
            text=text,
            confidence=1.0,
            audio_offset_sec=end_sec,
            engine=client.name,
            metadata=metadata,
        )
        for output_event in postprocess_event(event, defense, slang):
            emit(output_event)

    def handle_server_message(message: dict) -> None:
        nonlocal last_partial_text, last_partial_end
        text = str(message.get("text", "")).strip()
        is_final = bool(message.get("is_final", False))
        end_sec = float(message.get("end", last_partial_end) or last_partial_end)
        metadata = {
            "simulstreaming_start_sec": message.get("start"),
            "simulstreaming_end_sec": message.get("end"),
            "simulstreaming_emission_time": message.get("emission_time"),
            "simulstreaming_is_final": is_final,
            "simulstreaming_word_count": len(message.get("words", []) or []),
        }
        if is_final:
            emit_confirmed(text or last_partial_text, end_sec or last_partial_end, metadata)
            last_partial_text = ""
            last_partial_end = 0.0
            return
        if not text:
            return
        last_partial_text = text
        last_partial_end = end_sec
        partial = CaptionEvent(
            event_type="caption_confirmed",
            text=text,
            confidence=1.0,
            audio_offset_sec=end_sec,
            engine=client.name,
            metadata=metadata,
        )
        for output_event in pending_events(partial, defense, slang):
            emit(output_event)

    if ws_server is not None:
        ws_server.start_background()
    if admin_server is not None:
        admin_server.start_background()
        print(f"admin page: http://{config['server']['host']}:{config['server']['admin_port']}/admin")
    try:
        client.start()
    except SimulStreamingError as exc:
        emit(CaptionEvent(event_type="engine_error", text=str(exc), engine=client.name))
        client.close()
        session_archive.close({"exit_code": 1, "startup_error": str(exc)})
        return 1

    print(
        "Live SimulStreaming experiment. source={source}. Press Ctrl+C to stop.".format(
            source=getattr(source, "source_name", config["audio"]["source"])
        )
    )
    print("This experimental path uses the official server VAC, not the v2_light STT worker.")
    try:
        for chunk in source.chunks(max_seconds=max_seconds):
            client.send_audio(chunk.samples)
            info = {
                "rms": chunk.rms,
                "threshold": 0.0,
                "is_voice": False,
                "offset_sec": chunk.offset_sec,
                "duration_sec": chunk.duration_sec,
                "dropped_chunks": source.dropped_chunks,
                "status": chunk.status,
                "clip_fraction": chunk.clip_fraction,
                "input_gain": source.input_gain,
                "audio_source": getattr(source, "source_name", "microphone"),
                "source_restarts": int(getattr(source, "restart_count", 0)),
                "source_last_error": str(getattr(source, "last_error", "")),
                "device_label": getattr(source, "device_label", ""),
                "vad_engine": "simulstreaming_vac",
                "vad_score": 0.0,
                "discard": None,
            }
            admin_state.last_audio = info
            admin_state.audio_restart_count = info["source_restarts"]
            admin_state.last_stt = client.snapshot()
            if show_levels:
                print(
                    "\raudio rms={rms:.5f} vad=simulstreaming_vac      ".format(rms=chunk.rms),
                    end="",
                    flush=True,
                )
            for message in client.get_messages():
                handle_server_message(message)
            for error in client.get_errors():
                admin_state.pipeline_warning = error
                emit(CaptionEvent(event_type="engine_error", text=error, engine=client.name))
                exit_code = 1
                break
            memory_event = monitor.maybe_sample()
            if memory_event is not None:
                writer.append_event(memory_event)
                admin_state.last_memory = memory_event.metadata
            if exit_code or (max_segments is not None and confirmed_count >= max_segments):
                break
    except KeyboardInterrupt:
        stopped_by_user = True
        print("\nStopped.")
    except Exception as exc:
        exit_code = 1
        emit(CaptionEvent(event_type="engine_error", text=str(exc), engine=client.name))
    finally:
        for message in client.get_messages(max_items=10_000):
            handle_server_message(message)
        final_state = client.snapshot()
        client.close()
        writer.append_event(
            CaptionEvent(
                event_type="engine_status",
                text="simulstreaming experiment session summary",
                engine=client.name,
                metadata={
                    **final_state,
                    "confirmed_captions": confirmed_count,
                    "pending_captions": pending_count,
                    "hidden_captions": hidden_count,
                    "stopped_by_user": stopped_by_user,
                },
            )
        )
        report_paths = session_archive.close(
            {
                "confirmed_captions": confirmed_count,
                "pending_captions": pending_count,
                "hidden_captions": hidden_count,
                "stopped_by_user": stopped_by_user,
                "exit_code": exit_code,
            }
        )
        if report_paths:
            print(f"session report: {report_paths['markdown']}")
    if show_levels:
        print()
    print(
        "summary: mode=simulstreaming_experiment confirmed={confirmed} pending={pending} hidden={hidden}".format(
            confirmed=confirmed_count,
            pending=pending_count,
            hidden=hidden_count,
        )
    )
    return exit_code

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
    session_archive = SessionArchiveRecorder(
        config,
        writer,
        source=source_mode,
        engine=str(getattr(engine, "name", "baseline")),
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
        snapshot = stt_worker.snapshot()
        admin_state.last_stt = {
            **snapshot,
            "job_id": result.job.job_id,
            "job_type": result.job.job_type,
            "elapsed_sec": result.elapsed_sec,
            "event_count": len(result.events),
        }
        if (
            warned_stt_job_id == result.job.job_id
            or result.job.metadata.get("recovered")
        ):
            warned_stt_job_id = None
            admin_state.pipeline_warning = ""
        if result.job.job_type == "pending":
            for event in result.events:
                for output_event in pending_events(event, defense, slang):
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
        "device={device}, threshold={threshold}, chunk={chunk}s, overlap={overlap}s".format(
            device=config["audio"]["device"],
            threshold=config["audio"]["vad_energy_threshold"],
            chunk=config["streaming"]["chunk_seconds"],
            overlap=config["streaming"]["chunk_overlap_sec"],
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

    def handle_action(
        action: StreamingAction,
        action_source: str,
        action_scheduler: PendingWindowScheduler,
    ) -> None:
        nonlocal discarded, processed
        source_metadata = event_source_metadata(action_source)
        if action.action_type == "discarded_segment":
            discarded += 1
            admin_state.discarded_segment_count = discarded
            return
        if action.action_type == "pending_window" and action.window is not None:
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
                    "window_start_offset_sec": action.window.start_offset_sec,
                    "window_end_offset_sec": action.window.end_offset_sec,
                    "window_duration_sec": action.window.duration_sec,
                },
            )
            return
        if action.action_type == "confirmed_segment" and action.segment is not None:
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
            sample_rate = int(source_configs[action_source]["audio"]["sample_rate"])
            stt_worker.submit(
                "confirmed",
                action.segment.samples,
                sample_rate,
                action.segment.start_offset_sec,
                metadata={
                    **source_metadata,
                    "segment_duration_sec": action.segment.duration_sec,
                    "segment_peak_rms": action.segment.peak_rms,
                    "segment_voice_ratio": action.segment.voice_ratio,
                    "segment_start_offset_sec": action.segment.start_offset_sec,
                    "segment_end_offset_sec": action.segment.end_offset_sec,
                    "segment_start_sample": int(
                        round(action.segment.start_offset_sec * sample_rate)
                    ),
                    "segment_end_sample": int(
                        round(action.segment.end_offset_sec * sample_rate)
                    ),
                    "capture_ring_end_sample": action_scheduler.ring.end_sample,
                },
            )
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
            for result in stt_worker.get_results():
                emit_worker_result(result)

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

    if not stopped_by_user and pipeline_exit_code == 0:
        for flush_source, flush_scheduler in schedulers.items():
            for action in flush_scheduler.flush():
                handle_action(action, flush_source, flush_scheduler)
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
                "audio_source_statuses": (
                    source.source_statuses()
                    if hasattr(source, "source_statuses")
                    else {}
                ),
                "pipeline_exit_code": pipeline_exit_code,
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


def postprocess_event(
    event: CaptionEvent,
    defense: DefenseFilter,
    slang: SlangDictionary,
) -> list[CaptionEvent]:
    if event.event_type != "caption_confirmed":
        return [event]

    corrected = slang.apply(event.text)
    defense_result = defense.apply(
        corrected,
        event.confidence,
        {**event.metadata, "audio_offset_sec": event.audio_offset_sec},
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


def pending_events(
    event: CaptionEvent,
    defense: DefenseFilter,
    slang: SlangDictionary,
) -> list[CaptionEvent]:
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
    )
    if not defense_result.visible:
        return []
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
