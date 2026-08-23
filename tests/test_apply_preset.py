from __future__ import annotations

import copy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config, save_config
from tools.apply_preset import apply_preset, list_presets


def main() -> int:
    original = copy.deepcopy(load_config())
    try:
        assert "recommended_4060ti_v2_light" in list_presets()
        assert "recommended_4060ti_stable" in list_presets()
        config = apply_preset("recommended_4060ti_v2_light")
        assert config["vad"]["engine"] == "silero"
        assert config["whisper"]["compute_type"] == "int8_float16"
        stable = apply_preset("recommended_4060ti_stable")
        assert stable["streaming"]["pending_queue_max"] == 1
    finally:
        save_config(original)
    print("apply_preset_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
