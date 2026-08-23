from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.transcribe_wav import resolve_wav_path


def main() -> int:
    try:
        resolve_wav_path("samples/does_not_exist.wav")
    except FileNotFoundError as exc:
        message = str(exc)
        assert "WAV file not found" in message
        assert "record_wav.py" in message
        print("missing_wav_message_ok")
        return 0
    raise AssertionError("expected FileNotFoundError")


if __name__ == "__main__":
    raise SystemExit(main())

