from __future__ import annotations

import copy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.profiles import apply_profile, list_profiles


def main() -> int:
    original = load_config()
    assert "rtx_4060ti_16gb" in list_profiles()
    try:
        configured = copy.deepcopy(original)
        configured["audio"]["device"] = 99
        configured["audio"]["vad_energy_threshold"] = 0.01234
        from core.config import save_config

        save_config(configured)
        applied = apply_profile("rtx_4060ti_16gb")
        assert applied["app"]["profile"] == "rtx_4060ti_16gb"
        assert applied["whisper"]["compute_type"] == "int8_float16"
        assert applied["audio"]["device"] == 99
        assert applied["audio"]["vad_energy_threshold"] == 0.01234
    finally:
        from core.config import save_config

        save_config(original)
    print("apply_profile_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
