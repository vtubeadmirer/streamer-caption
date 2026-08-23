from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.recovery import ConfirmedRangeLedger


def main() -> int:
    ledger = ConfirmedRangeLedger()
    first = {
        "segment_start_sample": 16000,
        "segment_end_sample": 32000,
    }
    assert ledger.claim(7, first)
    assert not ledger.claim(7, first)
    assert not ledger.claim(
        8,
        {
            "segment_start_sample": 16000,
            "segment_end_sample": 32000,
        },
    )
    assert ledger.claim(
        9,
        {
            "segment_start_sample": 32000,
            "segment_end_sample": 48000,
        },
    )
    snapshot = ledger.snapshot()
    assert snapshot["committed_confirmed_jobs"] == 2
    assert snapshot["duplicate_confirmed_results"] == 2
    assert snapshot["last_committed_end_sample"] == 48000
    print("recovery_ledger_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())