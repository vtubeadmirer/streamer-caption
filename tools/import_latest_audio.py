from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT, load_config
from tools.import_audio import import_audio

EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".webm", ".mp4", ".mov", ".mkv"}


def latest_audio(folder: Path) -> Path | None:
    if not folder.exists():
        return None
    candidates = [path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in EXTENSIONS]
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[0]


def main() -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description="Import newest audio from samples/incoming.")
    parser.add_argument("--folder", default="samples/incoming")
    parser.add_argument("--out", default="samples/test_clip.wav")
    parser.add_argument("--sample-rate", type=int, default=int(config["audio"]["sample_rate"]))
    args = parser.parse_args()

    folder = (PROJECT_ROOT / args.folder).resolve()
    source = latest_audio(folder)
    if source is None:
        print(f"No audio file found in: {folder}")
        print("Supported: " + ", ".join(sorted(EXTENSIONS)))
        return 1
    result = import_audio(source, (PROJECT_ROOT / args.out).resolve(), args.sample_rate)
    print(f"imported_latest: {source}")
    print(f"saved: {result['out']}")
    print(
        "duration={duration:.2f}s rms={rms:.5f} peak={peak:.5f}".format(
            duration=result["duration_sec"],
            rms=result["rms"],
            peak=result["peak"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
