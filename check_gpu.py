from __future__ import annotations

from engines.baseline.engine import BaselineEngine
from core.config import load_config


def main() -> int:
    config = load_config()
    engine = BaselineEngine(config)
    registered = engine._register_cuda_dll_dirs()
    print("configured_device:", config["whisper"]["device"])
    print("configured_compute_type:", config["whisper"]["compute_type"])
    print("registered_dll_dirs:")
    for path in registered:
        print(" ", path)

    try:
        import ctranslate2
    except ImportError:
        print("ctranslate2: not installed")
        return 1

    print("ctranslate2:", getattr(ctranslate2, "__version__", "unknown"))
    try:
        count = ctranslate2.get_cuda_device_count()
    except Exception as exc:
        print("cuda_device_count_error:", exc)
        return 1
    print("cuda_device_count:", count)
    return 0 if count >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

