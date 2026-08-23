from __future__ import annotations

from typing import Any


def verdict(status: str, reason: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"status": status, "reason": reason, "details": details or {}}


def judge_voice_tuning(report: dict[str, Any]) -> dict[str, Any]:
    energy = report.get("energy", {})
    silero = report.get("silero", {})
    duration = float(report.get("duration_sec") or 0.0)
    rms_max = float(energy.get("rms_max") or 0.0)
    rms_median = float(energy.get("rms_median") or 0.0)
    silero_ratio = float(silero.get("current_voiced_ratio") or 0.0)
    energy_ratio = float(energy.get("current_voiced_ratio") or 0.0)

    if duration < 5.0:
        return verdict("fail", "clip_too_short", {"duration_sec": duration})
    if rms_max < 0.01:
        return verdict("fail", "voice_too_quiet_or_silent", {"rms_max": rms_max})
    if rms_median < 0.001 and rms_max < 0.03:
        return verdict("warn", "mostly_silent_clip", {"rms_median": rms_median, "rms_max": rms_max})
    if silero.get("available") and silero_ratio <= 0.0 and energy_ratio > 0.1:
        return verdict(
            "warn",
            "energy_detected_but_silero_did_not",
            {"silero_voiced_ratio": silero_ratio, "energy_voiced_ratio": energy_ratio},
        )
    if energy_ratio > 0.8 and not (silero.get("available") and 0.0 < silero_ratio <= 0.8):
        return verdict("warn", "vad_too_sensitive", {"energy_voiced_ratio": energy_ratio})
    return verdict("pass", "voice_clip_usable", {"rms_max": rms_max, "silero_voiced_ratio": silero_ratio})


def judge_streaming_benchmark(report: dict[str, Any]) -> dict[str, Any]:
    duration = float(report.get("duration_sec") or 0.0)
    pending = int(report.get("pending_jobs") or 0)
    confirmed = int(report.get("confirmed_jobs") or 0)
    dropped = int(report.get("dropped_jobs") or 0)
    rtf = report.get("rtf")

    if duration < 5.0:
        return verdict("fail", "clip_too_short", {"duration_sec": duration})
    if confirmed == 0:
        return verdict("fail", "no_confirmed_segments", {"pending_jobs": pending})
    if dropped > 0:
        return verdict(
            "warn",
            "stt_jobs_dropped",
            {
                "dropped_jobs": dropped,
                "recommendation": "increase streaming.pending_debounce_sec, increase chunk_seconds, or use recommended_4060ti_stable",
            },
        )
    if isinstance(rtf, (int, float)) and rtf > 1.0:
        return verdict("warn", "slower_than_realtime", {"rtf": rtf})
    return verdict("pass", "streaming_benchmark_usable", {"pending_jobs": pending, "confirmed_jobs": confirmed})


def judge_stability(memory_summary: dict[str, Any], session_summary: dict[str, Any]) -> dict[str, Any]:
    memory_samples = int(memory_summary.get("samples") or 0)
    errors = int(session_summary.get("counts", {}).get("engine_error", 0))
    dropped_jobs = int(session_summary.get("dropped_jobs") or 0)
    rss_slope = memory_summary.get("rss_slope_mb_per_hour")
    gpu_slope = memory_summary.get("gpu_slope_mb_per_hour")

    if memory_samples < 2:
        return verdict("warn", "not_enough_memory_samples", {"samples": memory_samples})
    if errors > 0:
        return verdict("fail", "engine_errors_seen", {"engine_errors": errors})
    if dropped_jobs > 0:
        return verdict("warn", "stt_jobs_dropped", {"dropped_jobs": dropped_jobs})
    if isinstance(rss_slope, (int, float)) and rss_slope > 150:
        return verdict("warn", "rss_growth_high", {"rss_slope_mb_per_hour": rss_slope})
    if isinstance(gpu_slope, (int, float)) and gpu_slope > 150:
        return verdict("warn", "gpu_growth_high", {"gpu_slope_mb_per_hour": gpu_slope})
    return verdict("pass", "stability_summary_ok")
