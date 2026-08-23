from __future__ import annotations


def main() -> int:
    imports = [
        "numpy",
        "sounddevice",
        "faster_whisper",
        "ctranslate2",
        "websockets",
        "watchdog",
        "pynput",
    ]
    for name in imports:
        module = __import__(name)
        version = getattr(module, "__version__", "unknown")
        print(f"{name}: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

