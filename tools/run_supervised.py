from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.app_info import RELAUNCH_EXIT_CODE, install_root
from core.config import load_config

RECOVERY_EXIT_CODES = {74, 75}


def build_child_command(main_args: Sequence[str]) -> list[str]:
    passthrough = list(main_args)
    if passthrough[:1] == ["--"]:
        passthrough = passthrough[1:]
    if "--v2-light" not in passthrough:
        passthrough.insert(0, "--v2-light")
    return [
        sys.executable,
        "-u",
        str(PROJECT_ROOT / "main.py"),
        *passthrough,
    ]


def run_supervised(
    main_args: Sequence[str],
    max_restarts: int,
    restart_delay_sec: float,
) -> int:
    command = build_child_command(main_args)
    restart_count = 0
    os.environ["STREAMER_CAPTION_SUPERVISED"] = "1"

    while True:
        if restart_count:
            print(
                f"supervisor: restarting caption process "
                f"({restart_count}/{max_restarts})"
            )
        try:
            result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        except KeyboardInterrupt:
            print()
            print("supervisor: stopped by user")
            return 0

        if result.returncode == RELAUNCH_EXIT_CODE:
            if install_root() is None:
                # Source checkout: nothing can change the code under us, so start again here.
                print("supervisor: restart requested from the admin page; starting again")
                continue
            # Installed/portable layout: the launcher re-reads state.json and may start a new version.
            print("supervisor: restart requested; handing over to the launcher")
            return RELAUNCH_EXIT_CODE
        if result.returncode not in RECOVERY_EXIT_CODES:
            return int(result.returncode)
        if restart_count >= max_restarts:
            print(
                "supervisor: automatic restart limit reached; "
                "collect diagnostics before the next run"
            )
            return int(result.returncode)

        restart_count += 1
        print(
            f"supervisor: child requested recovery "
            f"(exit={result.returncode}); waiting {restart_delay_sec:.1f}s"
        )
        time.sleep(restart_delay_sec)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restart v2_light only after recoverable pipeline failures."
    )
    parser.add_argument("--max-restarts", type=int)
    parser.add_argument("--restart-delay-sec", type=float)
    parser.add_argument("main_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    config = load_config()
    recovery = config["streaming"].get("error_recovery", {})
    max_restarts = (
        int(args.max_restarts)
        if args.max_restarts is not None
        else int(recovery.get("max_retry", 3))
    )
    restart_delay_sec = (
        float(args.restart_delay_sec)
        if args.restart_delay_sec is not None
        else float(config["audio"].get("source_retry_delay_sec", 1.0))
    )
    return run_supervised(
        args.main_args,
        max(0, max_restarts),
        max(0.0, restart_delay_sec),
    )


if __name__ == "__main__":
    raise SystemExit(main())