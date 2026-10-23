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


CONFIG_SCHEMA = 2  # 2: Silero VAD became the default; stored "energy" from schema 1 is migrated

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
        "engine": "baseline",
        "worker_mode": "process",
        "model": "large-v3",
        "chunk_seconds": 0.8,
        "chunk_overlap_sec": 0.2,
        "buffer_max_sec": 30,
        "silence_reset_sec": 2,
        "recovery_window_max_sec": 30,
        "pending_debounce_sec": 0.3,
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
        "enabled": False,
        "repo_dir": "experiments/vendor/SimulStreaming",
        "python_executable": "",
        "model_path": "",
        "allow_auto_download": False,
        "start_server": True,
        "host": "127.0.0.1",
        "port": 43007,
        "min_chunk_size": 0.8,
        "vac_chunk_size": 0.32,
        "audio_max_len": 30.0,
        "beams": 1,
        "frame_threshold": 25,
        "use_vac": True,
        "never_fire": False,
        "connect_timeout_sec": 90.0,
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
    "simulstreaming.model_path": "cold",
    "simulstreaming.python_executable": "cold",
    "simulstreaming.min_chunk_size": "cold",
    "simulstreaming.vac_chunk_size": "cold",
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


# Files that first-run setup (tools/bootstrap_user_data.py) places under user-data with
# absolute paths. When the whole install folder is moved (e.g. out of OneDrive), those
# paths still point at the old place; load_config re-anchors them to the current root.
USER_DATA_FILES: dict[tuple[str, str], str] = {
    ("defense", "blocklist_path"): "data/blocklist.txt",
    ("defense", "whitelist_path"): "data/whitelist.txt",
    ("slang", "dict_path"): "data/slang_dict.json",
    ("archive", "db_path"): "data/transcripts.sqlite3",
    ("log", "dir"): "logs",
}


def _anchor_user_data_paths(config: dict[str, Any], user_data_root: Path = USER_DATA_ROOT) -> dict[str, Any]:
    if user_data_root == PROJECT_ROOT:
        return config
    root = user_data_root.resolve()
    for (section, key), relative in USER_DATA_FILES.items():
        value = config.get(section, {}).get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        current = Path(value)
        if not current.is_absolute():
            continue
        try:
            current.resolve().relative_to(root)
            continue  # already under this user-data folder
        except ValueError:
            pass
        # Only paths that setup generated (".../user-data/<relative>") are moved along;
        # a custom location the user typed is left alone.
        if current.as_posix().lower().endswith(("/user-data/" + relative).lower()):
            config[section][key] = str(root / relative)
    return config


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    with path.open("r", encoding="utf-8") as f:
        user_config = json.load(f)
    return _anchor_user_data_paths(_migrate(deep_merge(DEFAULTS, user_config), user_config))


def _migrate(config: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    """Upgrade settings that first-run setup froze as a full snapshot of older defaults."""
    try:
        stored_schema = int(stored.get("config_schema", 1))
    except (TypeError, ValueError):
        stored_schema = 1
    if stored_schema < 2 and config.get("vad", {}).get("engine") == "energy":
        config["vad"]["engine"] = "silero"
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
    chunk = _bounded_number(config, "streaming", "chunk_seconds", 0.2, 10)
    overlap = _bounded_number(config, "streaming", "chunk_overlap_sec", 0, 5)
    if overlap >= chunk:
        raise ValueError("streaming.chunk_overlap_sec must be smaller than chunk_seconds")
    _bounded_number(config, "streaming", "buffer_max_sec", 5, 300)
    _bounded_number(config, "streaming", "pending_debounce_sec", 0.05, 10)
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
