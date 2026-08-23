from __future__ import annotations

from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.import_latest_audio import latest_audio


def main() -> int:
    folder = ROOT / "reports" / "_test_latest_audio"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True, exist_ok=True)
    old = folder / "_old.wav"
    new = folder / "_new.wav"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    assert latest_audio(folder) == new
    shutil.rmtree(folder)
    print("import_latest_audio_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
