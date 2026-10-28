from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT, load_config
from tools.progress_report import build_report


def run(args: list[str]) -> dict:
    started = time.perf_counter()
    result = subprocess.run(args, cwd=PROJECT_ROOT, capture_output=True, text=True)
    return {
        "args": args,
        "returncode": result.returncode,
        "elapsed_sec": time.perf_counter() - started,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def main() -> int:
    checks = [
        run([sys.executable, "tools/health_check.py"]),
        run([sys.executable, "tests/test_vad_factory.py"]),
        run([sys.executable, "tests/test_text_processing.py"]),
        run([sys.executable, "tests/test_stt_worker.py"]),
        run([sys.executable, "tests/test_stt_process_worker.py"]),
        run([sys.executable, "tests/test_stt_worker_factory.py"]),
        run([sys.executable, "tests/test_recovery_ledger.py"]),
        run([sys.executable, "tests/test_audio_sources.py"]),
        run([sys.executable, "tests/test_admin_server.py"]),
        run([sys.executable, "tests/test_admin_listener_ui.py"]),
        run([sys.executable, "tests/test_caption_overlay_contract.py"]),
        run([sys.executable, "tests/test_analysis_tuning.py"]),
        run([sys.executable, "tests/test_live_analysis.py"]),
        run([sys.executable, "tests/test_recap.py"]),
        run([sys.executable, "tests/test_updater_no_release.py"]),
        run([sys.executable, "tests/test_portable_release.py"]),
        run([sys.executable, "tests/test_updater_runtime.py"]),
        run([sys.executable, "tests/test_support_bundle_privacy.py"]),
        run([sys.executable, "tests/test_admin_engine_api.py"]),
        run([sys.executable, "tests/test_simulstreaming_engine.py"]),
        run([sys.executable, "tests/test_simulstreaming_client.py"]),
        run([sys.executable, "tests/test_app_control.py"]),
        run([sys.executable, "tests/test_user_data_paths.py"]),
        run([sys.executable, "tests/test_phase23_archive.py"]),
        run([sys.executable, "tests/test_phase23_api.py"]),
        run([sys.executable, "tests/test_validation_workflow.py"]),
        run([sys.executable, "tests/test_monitoring.py"]),
        run([sys.executable, "tests/test_backup_and_bundle.py"]),
        run([sys.executable, "tests/test_update_and_settings_bundles.py"]),
        run([sys.executable, "tests/test_release_system.py"]),
        run([sys.executable, "tests/test_summarize_session.py"]),
        run([sys.executable, "tests/test_broadcast_regressions.py"]),
        run([sys.executable, "tests/test_benchmark_streaming_wav.py"]),
        run([sys.executable, "tests/test_stability_verdict.py"]),
        run([sys.executable, "tools/progress_report.py"]),
    ]
    config = load_config()
    report = {
        "created_at": time.strftime("%Y-%m-%d_%H%M%S"),
        "config": {
            "profile": config.get("app", {}).get("profile"),
            "vad_engine": config.get("vad", {}).get("engine"),
            "model": config.get("whisper", {}).get("model"),
            "compute_type": config.get("whisper", {}).get("compute_type"),
        },
        "progress": build_report(),
        "checks": checks,
        "passed": all(check["returncode"] == 0 for check in checks),
    }
    out_dir = PROJECT_ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"final_check_{report['created_at']}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for check in checks:
        print("{name}: rc={rc} elapsed={elapsed:.2f}s".format(
            name=" ".join(check["args"][1:2]),
            rc=check["returncode"],
            elapsed=check["elapsed_sec"],
        ))
    print(f"final_check_report: {out}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
