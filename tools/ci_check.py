"""Tests that need no GPU, microphone or model download: the set CI runs on every push and
before every published release. tools/final_check.py is the superset run on the dev PC."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_TESTS = [
    "tests/test_text_processing.py",
    "tests/test_recovery_ledger.py",
    "tests/test_caption_overlay_contract.py",
    "tests/test_analysis_tuning.py",
    "tests/test_live_analysis.py",
    "tests/test_summarize_session.py",
    "tests/test_phase23_archive.py",
    "tests/test_phase23_api.py",
    "tests/test_recap.py",
    "tests/test_app_control.py",
    "tests/test_support_bundle_privacy.py",
    "tests/test_admin_engine_api.py",
    "tests/test_simulstreaming_engine.py",
    "tests/test_user_data_paths.py",
    "tests/test_updater_cache.py",
    "tests/test_updater_no_release.py",
    "tests/test_updater_runtime.py",
    "tests/test_release_system.py",
    "tests/test_portable_release.py",
    "tests/test_admin_hidden_css.py",
]


def main() -> int:
    failed: list[str] = []
    for test in CI_TESTS:
        result = subprocess.run([sys.executable, test], cwd=ROOT)
        print(f"{test}: rc={result.returncode}", flush=True)
        if result.returncode:
            failed.append(test)
    if failed:
        print("CI_CHECK_FAILED:", ", ".join(failed))
        return 1
    print(f"ci_check_ok ({len(CI_TESTS)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
