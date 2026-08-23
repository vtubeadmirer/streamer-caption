from __future__ import annotations

import json
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT, load_config


MILESTONES = [
    ("project_scaffold", "Separate v2 project scaffold", True, 5),
    ("baseline_capture", "Mic capture, energy VAD, baseline STT", True, 12),
    ("obs_preview", "WebSocket overlay and admin page", True, 10),
    ("gpu_profiles", "4060 Ti / 5070 Ti hardware profiles", True, 8),
    ("voice_tools", "Record, analyze, transcribe, benchmark tools", True, 10),
    ("v2_light", "v2_light pending/confirmed streaming loop", True, 12),
    ("silero_vad", "Silero VAD with energy fallback", True, 8),
    ("stability_tools", "10/30 minute stability and session summaries", True, 7),
    ("workflow_tools", "Record-tune-benchmark workflow and profile comparison", True, 6),
    ("synthetic_validation", "Synthetic voice-like pipeline validation", True, 4),
    ("real_voice_tuning", "Real voice sample tuning and quality pass", True, 8),
    ("idle_10min_validation", "10 minute live idle stability validation", True, 2),
    ("looped_voice_10min_validation", "10 minute looped real-voice STT stress validation", True, 3),
    ("listener_mode", "Windows PC-playback listener caption mode", True, 5),
    ("acoustic_hallucination_defense", "Voice-ratio and no-speech hallucination defense", True, 4),
    ("stt_process_isolation", "Dedicated STT child process with hard restart", True, 7),
    ("confirmed_recovery_ledger", "Confirmed-job replay and duplicate range ledger", True, 3),
    ("live_voice_30min_validation", "30 minute live microphone speech validation", False, 3),
    ("target_4060_process_validation", "Real 4060 Ti process-recovery and listener validation", False, 4),
    ("simulstreaming_gate", "4060 Ti SimulStreaming VRAM and latency gate", False, 5),
    ("phase23_archive", "30-minute archive, summaries, search, and reports", True, 6),
    ("phase23_analysis_tuning", "Admin analysis tuning and highlight weights", True, 4),
    ("phase23_realtime_events", "Topic and summary live WebSocket events", True, 5),
    ("phase23_broadcast_lifecycle", "Broadcast end and final report lifecycle", True, 3),
    ("phase23_platform_adapters", "Replay, recording, and viewer relay adapters", False, 5),
    ("phase23_acceptance", "Real broadcast Phase 2/3 quality acceptance", False, 5),
    ("packaging_docs", "Final operator docs and packaging polish", True, 2),
]


def build_report() -> dict:
    config = load_config()
    completed = sum(weight for _, _, done, weight in MILESTONES if done)
    total = sum(weight for _, _, _, weight in MILESTONES)
    remaining = total - completed
    return {
        "created_at": time.strftime("%Y-%m-%d_%H%M%S"),
        "percent_complete": round((completed / total) * 100, 1),
        "percent_remaining": round((remaining / total) * 100, 1),
        "completed_weight": completed,
        "remaining_weight": remaining,
        "config": {
            "profile": config.get("app", {}).get("profile"),
            "vad_engine": config.get("vad", {}).get("engine"),
            "model": config.get("whisper", {}).get("model"),
            "compute_type": config.get("whisper", {}).get("compute_type"),
        },
        "milestones": [
            {"id": key, "name": name, "done": done, "weight": weight}
            for key, name, done, weight in MILESTONES
        ],
        "next_five": [
            "On the 4060 Ti PC, run a 30-minute live broadcast and validate the latest session.",
            "Review 30-minute summaries, topic changes, and highlights with the Analysis tab presets.",
            "Measure history API response time and JSONL/SQLite parity on a two-hour session.",
            "Choose the next platform adapter: app-specific audio, replay links, or an authenticated viewer relay.",
            "Gate SimulStreaming only after baseline VRAM, latency, and hallucination checks pass.",
        ],
    }


def main() -> int:
    report = build_report()
    out_dir = PROJECT_ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"progress_{report['created_at']}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"progress_report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
