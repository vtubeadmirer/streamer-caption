from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _user_data_root() -> Path:
    configured = os.environ.get("STREAMER_CAPTION_USER_DATA", "").strip()
    if not configured:
        return PROJECT_ROOT
    return Path(configured).expanduser().resolve()


USER_DATA_ROOT = _user_data_root()
CONFIG_PATH = USER_DATA_ROOT / "config.json"


def user_path(value: str | Path, *, root: Path | None = None) -> Path:
    """Where a configured user-data path (log.dir, archive.db_path, ...) is on disk.

    Relative values belong to the user-data folder, not to the code folder: an installed
    copy runs from versions/<ver>/, which the next update replaces.
    """
    path = Path(value)
    if path.is_absolute():
        return path
    return (USER_DATA_ROOT if root is None else root) / path


# 2: Silero VAD became the default; stored "energy" from schema 1 is migrated.
# 3: user-data paths are stored relative (see user_path); setup's absolute ones are rewritten.
# 4: SimulStreaming (정확 우선) defaults from the 2026-10 lab runs replace the frozen experiment values.
# 5: PC sound waits 1 s instead of 0.2 s before filling in silence (late audio is no longer cut).
CONFIG_SCHEMA = 5

# simulstreaming values every config.json froze before schema 4 -> the measured defaults.
_SIMULSTREAMING_SCHEMA4 = (
    ("min_chunk_size", 0.8, 0.5),
    ("vac_chunk_size", 0.32, 0.04),
    ("frame_threshold", 25, 40),
    ("connect_timeout_sec", 90.0, 180.0),
)

DEFAULTS: dict[str, Any] = {
    "config_schema": CONFIG_SCHEMA,
    "app": {"mode": "baseline_v1_mode", "profile": "custom"},
    "caption": {
        "style": "classic_outline",
        "ko_size": 34,
        "en_size": 30,
        "show_english": False,
        "max_lines": 2,
        "max_chars_per_line": 28,
        "just_chatting_mode": False,
        "just_chatting_min_line_hold_sec": 0.65,
        "history_retention_sec": 1800,
        "history_max_items": 1000,
        "prev_opacity": 0.55,
        "animation": "slide_up",
        "position": "bottom_center",
        "font": "Malgun Gothic",
        "hold_time_auto": True,
        "show_source_labels": True,
        "microphone_label": "방송자",
        "system_label": "컴퓨터",
    },
    # 내 화면 자막: captions drawn on the streamer's own monitor (no OBS needed).
    "desktop_overlay": {
        "enabled": False,
        "font_size": 34,
        "max_lines": 2,
        "position": "bottom",
        "margin_percent": 6,
        "width_percent": 80,
        "opacity": 1.0,
        "background": 0.35,
        # The box the streamer dragged and sized on screen (pixels); width 0 = not placed yet,
        # then position/margin_percent/width_percent place a strip above the taskbar.
        "x": 0,
        "y": 0,
        "width": 0,
        "height": 0,
        "adjusting": False,
    },
    "whisper": {
        "model": "large-v3",
        "device": "cuda",
        "compute_type": "float16",
        "language": "ko",
        "task": "transcribe",
        "initial_prompt": "",
        "beam_size": 5,
        "temperature": 0.0,
        "compression_ratio_threshold": 2.4,
        "log_prob_threshold": -1.0,
        "no_speech_threshold": 0.6,
        "condition_on_previous_text": False,
        "repetition_penalty": 1.05,
        "no_repeat_ngram_size": 3,
    },
    "audio": {
        "source": "microphone",
        "system_device": "",
        "source_stall_timeout_sec": 3.0,
        "source_retry_delay_sec": 1.0,
        "source_max_restarts": 10,
        # PC sound (WASAPI loopback): when the recorder delivers nothing for this long, silent
        # blocks keep the audio clock running, and after system_stall_restart_sec it is reopened.
        # Audio that is only late (the recorder thread woke up late) arrives within it, whole.
        "system_silence_fill_sec": 1.0,
        "system_stall_restart_sec": 5.0,
        "sample_rate": 16000,
        "channels": 1,
        "block_size": 1024,
        "device": 1,
        "vad_energy_threshold": 0.015,
        "input_gain": 1.0,
        "microphone_input_gain": 1.0,
        "system_input_gain": 1.0,
        "microphone_vad_energy_threshold": 0.015,
        "system_vad_energy_threshold": 0.02,
        "microphone_min_segment_peak_rms": 0.012,
        "system_min_segment_peak_rms": 0.015,
        "dual": {
            "microphone_priority": True,
            "system_pending_enabled": False,
            "echo_suppression_enabled": True,
            "echo_window_sec": 1.5,
            "echo_similarity": 0.88,
        },
        "min_segment_peak_rms": 0.012,
        "pre_roll_sec": 0.3,
        "silence_duration_sec": 0.7,
        "max_segment_sec": 12.0,
        "min_segment_sec": 0.4,
    },
    "streaming": {
        # Caption engine: "baseline" = 기본 (faster-whisper, gray preview then final) or
        # "simulstreaming" = 정확 우선 (about 1 s later, text never changes; falls back to baseline).
        "engine": "baseline",
        "worker_mode": "process",
        "model": "large-v3",
        # Gray previews: one new guess every chunk_seconds of speech at most.
        "chunk_seconds": 0.8,
        # No longer used: a preview window now runs from the utterance start (below).
        "chunk_overlap_sec": 0.2,
        "buffer_max_sec": 30,
        "silence_reset_sec": 2,
        "recovery_window_max_sec": 30,
        "pending_debounce_sec": 0.3,
        # A preview window runs from the start of the utterance to now. No preview until the
        # utterance is this old (shorter windows were mostly guesses and outro phantoms) ...
        "pending_min_utterance_sec": 1.2,
        # ... and the window is at most this long; past that it restarts at a short pause.
        "pending_max_window_sec": 6.0,
        "pending_enabled": True,
        "pending_skip_when_busy": True,
        "pending_queue_max": 2,
        "pending_min_chars": 2,
        "pending_stale_sec": 3.0,
        "pending_display_mode": "overlay_gray",
        "pending_typing_effect": True,
        "confirmed_hold_sec": 6.0,
        "stt_stall_warn_sec": 20.0,
        "stt_stall_restart_sec": 60.0,
        "stt_cold_start_restart_sec": 300.0,
        "stt_process_start_restart_sec": 60.0,
        "stt_heartbeat_interval_sec": 2.0,
        "stt_heartbeat_timeout_sec": 12.0,
        "stt_latency_target_p95_sec": 4.0,
        "error_recovery": {"max_retry": 3, "fallback_to": "baseline_v1_mode"},
        "gpu_fallback_on_fail": "degrade_to_cpu",
        "ws_cache_sec": 15,
    },
    "simulstreaming": {
        "enabled": False,  # unused since streaming.engine selects the engine; kept for old configs
        "repo_dir": "experiments/vendor/SimulStreaming",
        "python_executable": "",  # "" = the optional runtime (see engines/simulstreaming/availability.py)
        # large-v3-turbo (default: 1.3 s, CER 1.0%, ~5.3 GB) or large-v3 (advanced: CER 0.7% but
        # ~10 GB and spikes higher, too tight beside OBS or a game; turbo is used if it does not fit).
        # The .pt file names must stay exact: alignment heads are chosen by file name.
        "model": "large-v3-turbo",
        "model_dir": "",  # "" = <runtime>/models
        "model_path": "",  # an explicit .pt file overrides model/model_dir
        "allow_auto_download": False,  # the server never downloads a missing model
        "start_server": True,
        "host": "127.0.0.1",
        "port": 0,  # 0 = a free local port for each start
        "min_chunk_size": 0.5,
        "vac_chunk_size": 0.04,
        "audio_max_len": 30.0,
        "beams": 1,
        "frame_threshold": 40,  # lab, large-v3 / turbo: 40 -> CER 0.7% / 1.0%, no phantoms; 25 -> 4.1% / 1.8%
        "use_vac": True,
        "never_fire": False,
        "connect_timeout_sec": 180.0,  # model load; faster-whisper captions meanwhile
        "stall_timeout_sec": 15.0,  # no heartbeat this long -> fall back
        "max_lag_sec": 10.0,  # this far behind the audio for 5 s -> fall back
        "max_restarts": 2,  # retries per run after a crash/stall; 0 = never retry
        "retry_delay_sec": 30.0,  # doubles with each retry
        "standby_timeout_sec": 90.0,  # wait this long for the faster-whisper standby model
        "switch_timeout_sec": 20.0,  # switch even without a pause after this long
    },
    "vad": {
        "engine": "silero",  # speech-probability VAD; falls back to energy if torch is unavailable
        "device": "cpu",
        "speech_threshold": 0.5,
        "runtime_timeout_ms": 50,
        "fallback_on_load_fail": "energy_vad_permanent",
        "fallback_on_runtime_fail": "energy_vad_this_chunk_only",
    },
    "defense": {
        "min_confidence_show": 0.55,
        "dim_confidence_threshold": 0.7,
        "blocklist_path": "data/blocklist.txt",
        "whitelist_path": "data/whitelist.txt",
        "filter_single_char": True,
        "filter_repeat_char": True,
        "filter_hallucination_patterns": True,
        "min_segment_voice_ratio": 0.06,
        "max_no_speech_prob": 0.75,
        "hallucination_score_threshold": 0.72,
        "min_avg_logprob": -0.95,
        "max_compression_ratio": 2.6,
        "short_utterance_max_chars": 12,
        "short_utterance_min_confidence": 0.72,
        "short_utterance_voice_ratio_floor": 0.02,
        "repeat_window_sec": 45,
        "repeat_limit": 2,
        # A whole line that is only a YouTube outro phrase ("감사합니다.", "다음 영상에서 만나요")
        # is hidden when the voice detector heard speech in less than this share of its audio.
        "phantom_max_speech_share": 0.3,
        # The same for a 기본 engine line, measured by its segment's voice ratio instead.
        "phantom_max_voice_ratio": 0.65,
    },
    "server": {"host": "127.0.0.1", "port": 8765, "admin_port": 8766},
    "slang": {
        "dict_path": "data/slang_dict.json",
        "prompt_mode": "off",
        "prompt_max_chars": 800,
    },
    "monitoring": {
        "memory_log_interval_min": 10,
        "leak_threshold_mb_per_hour": 50,
        "leak_confirm_count": 3,
        "warmup_exclude_min": 30,
    },
    "archive": {
        "enabled": True,
        "db_path": "data/transcripts.sqlite3",
        "bucket_minutes": 15,
        "summary_max_bullets": 5,
        "topic_window_captions": 6,
        "topic_similarity_threshold": 0.22,
        "topic_cooldown_sec": 30,
        "highlight_limit": 10,
        "highlight_weights": {
            "strong_expression": 0.45,
            "laughter": 0.65,
            "keyword": 0.18,
            "topic_change": 0.4,
        },
        "live_analysis_enabled": True,
        "live_analysis_every_captions": 8,
        "live_analysis_min_interval_sec": 15,
        "auto_report_on_close": True,
        "recap": {
            "enabled": True,
            "provider": "auto",
            "max_bullets": 4,
            "min_captions": 3,
            "timeout_sec": 60,
            "max_transcript_chars": 7000,
            "ollama_url": "http://127.0.0.1:11434",
            "ollama_model": "",
            "openai_base_url": "",
            "openai_model": "",
            "openai_api_key_env": "OPENAI_API_KEY",
            "cli_command": "",
        },
    },
    "log": {"dir": "logs"},
}


CONFIG_LIFETIME: dict[str, str] = {
    "caption.ko_size": "hot",
    "caption.max_lines": "hot",
    "caption.just_chatting_mode": "hot",
    "caption.just_chatting_min_line_hold_sec": "hot",
    "caption.prev_opacity": "hot",
    "caption.max_chars_per_line": "cold",
    "caption.show_source_labels": "hot",
    "caption.microphone_label": "hot",
    "caption.system_label": "hot",
    "desktop_overlay.enabled": "hot",
    "desktop_overlay.font_size": "hot",
    "desktop_overlay.max_lines": "hot",
    "desktop_overlay.position": "hot",
    "desktop_overlay.margin_percent": "hot",
    "desktop_overlay.width_percent": "hot",
    "desktop_overlay.opacity": "hot",
    "desktop_overlay.background": "hot",
    "desktop_overlay.x": "hot",
    "desktop_overlay.y": "hot",
    "desktop_overlay.width": "hot",
    "desktop_overlay.height": "hot",
    "desktop_overlay.adjusting": "hot",
    "app.mode": "cold",
    "whisper.model": "cold",
    "whisper.device": "cold",
    "whisper.compute_type": "cold",
    "whisper.beam_size": "cold",
    "whisper.no_speech_threshold": "cold",
    "whisper.condition_on_previous_text": "cold",
    "whisper.repetition_penalty": "cold",
    "whisper.no_repeat_ngram_size": "cold",
    "audio.source": "cold",
    "audio.system_device": "cold",
    "audio.system_silence_fill_sec": "cold",
    "audio.system_stall_restart_sec": "cold",
    "audio.device": "cold",
    "audio.vad_energy_threshold": "cold",
    "audio.input_gain": "cold",
    "audio.microphone_input_gain": "cold",
    "audio.system_input_gain": "cold",
    "audio.microphone_vad_energy_threshold": "cold",
    "audio.system_vad_energy_threshold": "cold",
    "audio.microphone_min_segment_peak_rms": "cold",
    "audio.system_min_segment_peak_rms": "cold",
    "audio.dual.microphone_priority": "cold",
    "audio.dual.system_pending_enabled": "cold",
    "audio.dual.echo_suppression_enabled": "cold",
    "audio.dual.echo_window_sec": "cold",
    "audio.dual.echo_similarity": "cold",
    "audio.min_segment_peak_rms": "cold",
    "audio.pre_roll_sec": "cold",
    "audio.silence_duration_sec": "cold",
    "streaming.engine": "cold",
    "streaming.worker_mode": "cold",
    "streaming.model": "cold",
    "streaming.chunk_seconds": "cold",
    "streaming.chunk_overlap_sec": "cold",
    "streaming.buffer_max_sec": "cold",
    "streaming.pending_debounce_sec": "cold",
    "streaming.pending_min_utterance_sec": "cold",
    "streaming.pending_max_window_sec": "cold",
    "streaming.pending_enabled": "cold",
    "streaming.pending_skip_when_busy": "cold",
    "streaming.pending_queue_max": "cold",
    "streaming.pending_min_chars": "cold",
    "streaming.pending_stale_sec": "hot",
    "streaming.pending_display_mode": "hot",
    "streaming.pending_typing_effect": "hot",
    "streaming.confirmed_hold_sec": "hot",
    "streaming.stt_stall_warn_sec": "cold",
    "streaming.stt_stall_restart_sec": "cold",
    "streaming.stt_cold_start_restart_sec": "cold",
    "streaming.stt_process_start_restart_sec": "cold",
    "streaming.stt_heartbeat_interval_sec": "cold",
    "streaming.stt_heartbeat_timeout_sec": "cold",
    "streaming.stt_latency_target_p95_sec": "cold",
    "streaming.ws_cache_sec": "cold",
    "simulstreaming.enabled": "cold",
    "simulstreaming.model": "cold",
    "simulstreaming.model_dir": "cold",
    "simulstreaming.model_path": "cold",
    "simulstreaming.python_executable": "cold",
    "simulstreaming.min_chunk_size": "cold",
    "simulstreaming.vac_chunk_size": "cold",
    "simulstreaming.frame_threshold": "cold",
    "simulstreaming.stall_timeout_sec": "cold",
    "simulstreaming.max_lag_sec": "cold",
    "simulstreaming.max_restarts": "cold",
    "vad.engine": "cold",
    "vad.device": "cold",
    "vad.speech_threshold": "cold",
    "vad.runtime_timeout_ms": "cold",
    "defense.hallucination_score_threshold": "cold",
    "defense.min_avg_logprob": "cold",
    "defense.max_compression_ratio": "cold",
    "defense.short_utterance_max_chars": "cold",
    "defense.short_utterance_min_confidence": "cold",
    "defense.short_utterance_voice_ratio_floor": "cold",
    "defense.repeat_window_sec": "cold",
    "defense.repeat_limit": "cold",
    "defense.phantom_max_speech_share": "cold",
    "defense.phantom_max_voice_ratio": "cold",
    "slang.dict_path": "cold",
    "slang.prompt_mode": "cold",
    "monitoring.memory_log_interval_min": "cold",
    "archive.enabled": "cold",
    "archive.db_path": "cold",
    "archive.bucket_minutes": "cold",
    "archive.summary_max_bullets": "hot",
    "archive.topic_window_captions": "hot",
    "archive.topic_similarity_threshold": "hot",
    "archive.topic_cooldown_sec": "hot",
    "archive.highlight_limit": "hot",
    "archive.highlight_weights.strong_expression": "hot",
    "archive.highlight_weights.laughter": "hot",
    "archive.highlight_weights.keyword": "hot",
    "archive.highlight_weights.topic_change": "hot",
    "archive.live_analysis_enabled": "cold",
    "archive.live_analysis_every_captions": "cold",
    "archive.live_analysis_min_interval_sec": "cold",
    "archive.auto_report_on_close": "cold",
    "archive.recap.enabled": "cold",
    "archive.recap.provider": "cold",
    "archive.recap.max_bullets": "cold",
    "archive.recap.min_captions": "cold",
    "archive.recap.timeout_sec": "cold",
    "archive.recap.max_transcript_chars": "cold",
    "archive.recap.ollama_url": "cold",
    "archive.recap.ollama_model": "cold",
    "archive.recap.openai_base_url": "cold",
    "archive.recap.openai_model": "cold",
    "archive.recap.openai_api_key_env": "cold",
    "archive.recap.cli_command": "cold",
}

def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# Settings that name the streamer's own files and folders; turn them into paths with user_path.
USER_DATA_PATH_KEYS: tuple[tuple[str, str], ...] = (
    ("defense", "blocklist_path"),
    ("defense", "whitelist_path"),
    ("slang", "dict_path"),
    ("archive", "db_path"),
    ("archive", "backup_dir"),
    ("log", "dir"),
)


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    with path.open("r", encoding="utf-8-sig") as f:
        user_config = json.load(f)
    return _migrate(deep_merge(DEFAULTS, user_config), user_config)


def _migrate(config: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    """Upgrade settings that first-run setup froze as a full snapshot of older defaults."""
    try:
        stored_schema = int(stored.get("config_schema", 1))
    except (TypeError, ValueError):
        stored_schema = 1
    if stored_schema < 2 and config.get("vad", {}).get("engine") == "energy":
        config["vad"]["engine"] = "silero"
    if stored_schema < 3:
        # Setup used to store "<install>/user-data/<default>" as an absolute path, which kept
        # pointing at the old folder after the install moved. A custom location is kept.
        for section, key in USER_DATA_PATH_KEYS:
            default = DEFAULTS[section].get(key)
            value = config.get(section, {}).get(key)
            if (
                isinstance(default, str)
                and isinstance(value, str)
                and Path(value).is_absolute()
                and Path(value).as_posix().lower().endswith("/user-data/" + default.lower())
            ):
                config[section][key] = default
    if stored_schema < 4:
        # save_config writes every default, so each config.json froze the experiment's values;
        # only those exact values move, a deliberate setting stays.
        simul = config.get("simulstreaming")
        if isinstance(simul, dict):
            for key, old, new in _SIMULSTREAMING_SCHEMA4:
                if simul.get(key) == old:
                    simul[key] = new
            if simul.get("port") == 43007 and simul.get("start_server", True):
                simul["port"] = 0
    if stored_schema < 5:
        # Every config.json froze 0.2 s, which cut PC sound that was only late; other values stay.
        audio = config.get("audio")
        if isinstance(audio, dict) and audio.get("system_silence_fill_sec") == 0.2:
            audio["system_silence_fill_sec"] = DEFAULTS["audio"]["system_silence_fill_sec"]
    simul = config.get("simulstreaming")
    if isinstance(simul, dict) and simul.get("model") == "auto":
        # Development builds briefly stored "auto"; turbo is the default it meant on most PCs.
        simul["model"] = DEFAULTS["simulstreaming"]["model"]
    config["config_schema"] = CONFIG_SCHEMA
    return config


def _bounded_number(
    config: dict[str, Any],
    section: str,
    key: str,
    minimum: float,
    maximum: float,
) -> float:
    value = config[section][key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{section}.{key} must be a number")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(
            f"{section}.{key} must be between {minimum} and {maximum}"
        )
    return number


def _validate_simulstreaming(simul: Any) -> None:
    if not isinstance(simul, dict):
        raise TypeError("simulstreaming must be an object")
    if simul.get("model", "large-v3-turbo") not in {"large-v3", "large-v3-turbo"}:
        raise ValueError("simulstreaming.model must be large-v3-turbo or large-v3")
    for key, low, high, integer in (
        ("min_chunk_size", 0.1, 5, False),
        ("vac_chunk_size", 0.02, 1, False),
        ("audio_max_len", 5, 60, False),
        ("frame_threshold", 1, 200, True),
        ("beams", 1, 10, True),
        ("connect_timeout_sec", 10, 1800, False),
        ("stall_timeout_sec", 5, 300, False),
        ("max_lag_sec", 2, 300, False),
        ("max_restarts", 0, 10, True),
        ("retry_delay_sec", 1, 3600, False),
        ("standby_timeout_sec", 0, 1800, False),
        ("switch_timeout_sec", 1, 600, False),
    ):
        if key not in simul:
            continue
        value = simul[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"simulstreaming.{key} must be a number")
        if not low <= float(value) <= high or (integer and int(value) != value):
            raise ValueError(f"simulstreaming.{key} must be between {low} and {high}")
    port = simul.get("port", 0)
    if isinstance(port, bool) or not isinstance(port, int) or not (port == 0 or 1024 <= port <= 65535):
        raise ValueError("simulstreaming.port must be 0 (automatic) or between 1024 and 65535")
    if float(simul.get("min_chunk_size", 0.5)) >= float(simul.get("audio_max_len", 30.0)):
        raise ValueError("simulstreaming.min_chunk_size must be smaller than audio_max_len")


def validate_config(config: dict[str, Any]) -> None:
    _bounded_number(config, "caption", "ko_size", 6, 200)
    _bounded_number(config, "caption", "max_lines", 1, 500)
    _bounded_number(config, "caption", "max_chars_per_line", 4, 200)
    _bounded_number(
        config,
        "caption",
        "just_chatting_min_line_hold_sec",
        0,
        5,
    )
    _bounded_number(config, "caption", "history_retention_sec", 15, 86400)
    _bounded_number(config, "caption", "history_max_items", 12, 10000)
    for key in ("enabled", "adjusting"):
        if not isinstance(config["desktop_overlay"].get(key), bool):
            raise TypeError(f"desktop_overlay.{key} must be boolean")
    for key in ("x", "y", "width", "height"):
        value = config["desktop_overlay"].get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"desktop_overlay.{key} must be an integer")
    _bounded_number(config, "desktop_overlay", "x", -32768, 32767)
    _bounded_number(config, "desktop_overlay", "y", -32768, 32767)
    _bounded_number(config, "desktop_overlay", "width", 0, 16384)
    _bounded_number(config, "desktop_overlay", "height", 0, 16384)
    if config["desktop_overlay"].get("position") not in {"bottom", "top"}:
        raise ValueError("desktop_overlay.position must be bottom or top")
    _bounded_number(config, "desktop_overlay", "font_size", 12, 120)
    _bounded_number(config, "desktop_overlay", "max_lines", 1, 6)
    _bounded_number(config, "desktop_overlay", "margin_percent", 0, 45)
    _bounded_number(config, "desktop_overlay", "width_percent", 30, 100)
    _bounded_number(config, "desktop_overlay", "opacity", 0.2, 1)
    _bounded_number(config, "desktop_overlay", "background", 0, 0.9)
    beam_size = _bounded_number(config, "whisper", "beam_size", 1, 20)
    if int(beam_size) != beam_size:
        raise ValueError("whisper.beam_size must be an integer")
    _bounded_number(config, "whisper", "temperature", 0, 2)
    _bounded_number(
        config,
        "whisper",
        "compression_ratio_threshold",
        1,
        10,
    )
    _bounded_number(config, "whisper", "log_prob_threshold", -10, 1)
    _bounded_number(config, "whisper", "no_speech_threshold", 0, 1)
    _bounded_number(config, "whisper", "repetition_penalty", 1, 2)
    no_repeat_ngram_size = _bounded_number(
        config, "whisper", "no_repeat_ngram_size", 0, 10
    )
    if int(no_repeat_ngram_size) != no_repeat_ngram_size:
        raise ValueError("whisper.no_repeat_ngram_size must be an integer")
    if not isinstance(
        config["whisper"].get("condition_on_previous_text"),
        bool,
    ):
        raise TypeError(
            "whisper.condition_on_previous_text must be boolean"
        )
    for key in ("pending_enabled", "pending_skip_when_busy"):
        if not isinstance(config["streaming"].get(key), bool):
            raise TypeError(f"streaming.{key} must be boolean")
    if config["audio"].get("source") not in {"microphone", "system", "both"}:
        raise ValueError("audio.source must be microphone, system, or both")
    _bounded_number(config, "audio", "source_stall_timeout_sec", 1, 30)
    _bounded_number(config, "audio", "source_retry_delay_sec", 0.1, 30)
    _bounded_number(config, "audio", "source_max_restarts", 0, 100)
    _bounded_number(config, "audio", "system_silence_fill_sec", 0.05, 2)
    _bounded_number(config, "audio", "system_stall_restart_sec", 1, 60)
    _bounded_number(config, "audio", "sample_rate", 8000, 96000)
    _bounded_number(config, "audio", "block_size", 128, 8192)
    _bounded_number(config, "audio", "vad_energy_threshold", 0, 1)
    _bounded_number(config, "audio", "input_gain", 0.1, 20)
    for key in (
        "microphone_input_gain",
        "system_input_gain",
    ):
        _bounded_number(config, "audio", key, 0.1, 20)
    for key in (
        "microphone_vad_energy_threshold",
        "system_vad_energy_threshold",
        "microphone_min_segment_peak_rms",
        "system_min_segment_peak_rms",
    ):
        _bounded_number(config, "audio", key, 0, 1)
    dual = config["audio"].get("dual", {})
    for key in (
        "microphone_priority",
        "system_pending_enabled",
        "echo_suppression_enabled",
    ):
        if not isinstance(dual.get(key), bool):
            raise TypeError(f"audio.dual.{key} must be boolean")
    echo_window = float(dual.get("echo_window_sec", 1.5))
    if not 0.1 <= echo_window <= 10:
        raise ValueError("audio.dual.echo_window_sec must be between 0.1 and 10")
    echo_similarity = float(dual.get("echo_similarity", 0.88))
    if not 0.5 <= echo_similarity <= 1:
        raise ValueError("audio.dual.echo_similarity must be between 0.5 and 1")
    _bounded_number(config, "audio", "min_segment_peak_rms", 0, 1)
    _bounded_number(config, "audio", "pre_roll_sec", 0, 2)
    _bounded_number(config, "audio", "silence_duration_sec", 0.1, 5)
    min_segment = _bounded_number(config, "audio", "min_segment_sec", 0.1, 10)
    max_segment = _bounded_number(config, "audio", "max_segment_sec", 1, 60)
    if min_segment > max_segment:
        raise ValueError("audio.min_segment_sec cannot exceed audio.max_segment_sec")

    if config["streaming"].get("worker_mode") not in {"thread", "process"}:
        raise ValueError(
            "streaming.worker_mode must be thread or process"
        )
    if config["streaming"].get("engine", "baseline") not in {"baseline", "simulstreaming"}:
        raise ValueError("streaming.engine must be baseline or simulstreaming")
    _validate_simulstreaming(config.get("simulstreaming", {}))
    chunk = _bounded_number(config, "streaming", "chunk_seconds", 0.2, 10)
    overlap = _bounded_number(config, "streaming", "chunk_overlap_sec", 0, 5)
    if overlap >= chunk:
        raise ValueError("streaming.chunk_overlap_sec must be smaller than chunk_seconds")
    _bounded_number(config, "streaming", "buffer_max_sec", 5, 300)
    _bounded_number(config, "streaming", "pending_debounce_sec", 0.05, 10)
    _bounded_number(config, "streaming", "pending_min_utterance_sec", 0, 5)
    # May exceed buffer_max_sec (5 s is allowed): PendingWindowScheduler caps it at the buffer.
    _bounded_number(config, "streaming", "pending_max_window_sec", 1, 30)
    _bounded_number(config, "streaming", "pending_queue_max", 1, 20)
    _bounded_number(config, "streaming", "pending_min_chars", 1, 100)
    _bounded_number(config, "streaming", "pending_stale_sec", 0.25, 60)
    _bounded_number(config, "streaming", "confirmed_hold_sec", 0.25, 300)
    stall_warn = _bounded_number(
        config, "streaming", "stt_stall_warn_sec", 2, 300
    )
    stall_restart = _bounded_number(
        config, "streaming", "stt_stall_restart_sec", 5, 600
    )
    if stall_restart <= stall_warn:
        raise ValueError(
            "streaming.stt_stall_restart_sec must exceed stt_stall_warn_sec"
        )
    cold_start_restart = _bounded_number(
        config,
        "streaming",
        "stt_cold_start_restart_sec",
        30,
        1800,
    )
    if cold_start_restart <= stall_warn:
        raise ValueError(
            "streaming.stt_cold_start_restart_sec must exceed "
            "stt_stall_warn_sec"
        )
    _bounded_number(
        config,
        "streaming",
        "stt_process_start_restart_sec",
        10,
        600,
    )
    heartbeat_interval = _bounded_number(
        config,
        "streaming",
        "stt_heartbeat_interval_sec",
        0.25,
        30,
    )
    heartbeat_timeout = _bounded_number(
        config,
        "streaming",
        "stt_heartbeat_timeout_sec",
        1,
        120,
    )
    if heartbeat_timeout <= heartbeat_interval * 2:
        raise ValueError(
            "streaming.stt_heartbeat_timeout_sec must exceed twice "
            "stt_heartbeat_interval_sec"
        )
    _bounded_number(
        config,
        "streaming",
        "stt_latency_target_p95_sec",
        0.25,
        60,
    )
    _bounded_number(config, "vad", "speech_threshold", 0.05, 0.99)
    _bounded_number(config, "defense", "min_segment_voice_ratio", 0, 1)
    _bounded_number(config, "defense", "max_no_speech_prob", 0, 1)
    _bounded_number(config, "defense", "hallucination_score_threshold", 0, 1)
    _bounded_number(config, "defense", "min_avg_logprob", -10, 1)
    _bounded_number(config, "defense", "max_compression_ratio", 1, 10)
    short_chars = _bounded_number(
        config, "defense", "short_utterance_max_chars", 1, 100
    )
    if int(short_chars) != short_chars:
        raise ValueError(
            "defense.short_utterance_max_chars must be an integer"
        )
    _bounded_number(
        config, "defense", "short_utterance_min_confidence", 0, 1
    )
    _bounded_number(
        config, "defense", "short_utterance_voice_ratio_floor", 0, 1
    )
    _bounded_number(config, "defense", "repeat_window_sec", 1, 600)
    repeat_limit = _bounded_number(
        config, "defense", "repeat_limit", 1, 20
    )
    if int(repeat_limit) != repeat_limit:
        raise ValueError("defense.repeat_limit must be an integer")
    _bounded_number(config, "defense", "phantom_max_speech_share", 0, 1)
    _bounded_number(config, "defense", "phantom_max_voice_ratio", 0, 1)
    _bounded_number(config, "server", "port", 1024, 65535)
    _bounded_number(config, "server", "admin_port", 1024, 65535)
    if int(config["server"]["port"]) == int(config["server"]["admin_port"]):
        raise ValueError("server.port and server.admin_port must be different")
    for key in (
        "enabled",
        "auto_report_on_close",
        "live_analysis_enabled",
    ):
        if not isinstance(config["archive"].get(key), bool):
            raise TypeError(f"archive.{key} must be boolean")
    db_path = config["archive"].get("db_path")
    if not isinstance(db_path, str) or not db_path.strip():
        raise ValueError("archive.db_path must be a non-empty string")
    bucket_minutes = _bounded_number(
        config, "archive", "bucket_minutes", 5, 120
    )
    if int(bucket_minutes) != bucket_minutes:
        raise ValueError("archive.bucket_minutes must be an integer")
    recap = config["archive"].get("recap")
    if not isinstance(recap, dict):
        raise TypeError("archive.recap must be an object")
    if not isinstance(recap.get("enabled", True), bool):
        raise TypeError("archive.recap.enabled must be boolean")
    provider = str(recap.get("provider", "auto")).lower()
    if provider not in {"auto", "none", "extractive", "off", "ollama", "openai", "cli"}:
        raise ValueError("archive.recap.provider must be auto, none, ollama, openai or cli")
    for key, low, high in (("max_bullets", 1, 6), ("min_captions", 1, 50), ("timeout_sec", 5, 600), ("max_transcript_chars", 1000, 50000)):
        value = recap.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not (low <= float(value) <= high):
            raise ValueError(f"archive.recap.{key} must be between {low} and {high}")
    summary_bullets = _bounded_number(
        config, "archive", "summary_max_bullets", 1, 10
    )
    if int(summary_bullets) != summary_bullets:
        raise ValueError("archive.summary_max_bullets must be an integer")
    topic_window = _bounded_number(
        config, "archive", "topic_window_captions", 2, 100
    )
    if int(topic_window) != topic_window:
        raise ValueError("archive.topic_window_captions must be an integer")
    _bounded_number(
        config, "archive", "topic_similarity_threshold", 0, 1
    )
    _bounded_number(config, "archive", "topic_cooldown_sec", 0, 3600)
    highlight_limit = _bounded_number(
        config, "archive", "highlight_limit", 1, 50
    )
    if int(highlight_limit) != highlight_limit:
        raise ValueError("archive.highlight_limit must be an integer")
    live_every = _bounded_number(
        config,
        "archive",
        "live_analysis_every_captions",
        1,
        1000,
    )
    if int(live_every) != live_every:
        raise ValueError(
            "archive.live_analysis_every_captions must be an integer"
        )
    _bounded_number(
        config,
        "archive",
        "live_analysis_min_interval_sec",
        0,
        3600,
    )
    highlight_weights = config["archive"].get("highlight_weights")
    if not isinstance(highlight_weights, dict):
        raise TypeError("archive.highlight_weights must be an object")
    for key in (
        "strong_expression",
        "laughter",
        "keyword",
        "topic_change",
    ):
        value = highlight_weights.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 <= float(value) <= 3
        ):
            raise ValueError(
                f"archive.highlight_weights.{key} must be between 0 and 3"
            )


def save_config(config: dict[str, Any], path: Path = CONFIG_PATH) -> None:
    merged = deep_merge(DEFAULTS, config)
    validate_config(merged)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
    temporary.replace(path)
