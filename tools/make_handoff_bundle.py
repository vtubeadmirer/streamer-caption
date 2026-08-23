from __future__ import annotations

import argparse
import json
import time
import zipfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT, load_config
from tools.progress_report import build_report


def latest_files(pattern: str, limit: int = 5) -> list[Path]:
    files = sorted(PROJECT_ROOT.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return [path for path in files if path.is_file()][:limit]


def make_bundle() -> Path:
    out_dir = PROJECT_ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    config = load_config()
    manifest = {
        "created_at": stamp,
        "config": {
            "app": config.get("app", {}),
            "whisper": config.get("whisper", {}),
            "vad": config.get("vad", {}),
            "streaming": config.get("streaming", {}),
        },
        "progress": build_report(),
    }
    manifest_path = out_dir / f"handoff_manifest_{stamp}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    bundle = out_dir / f"streamer_caption_v2_handoff_{stamp}.zip"
    include = [
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "config.json",
        manifest_path,
        PROJECT_ROOT / "docs" / "ARCHITECTURE.md",
        PROJECT_ROOT / "docs" / "NEXT_STEPS.md",
        PROJECT_ROOT / "docs" / "OPERATOR_GUIDE.md",
        PROJECT_ROOT / "docs" / "OVERLAY.md",
    ]
    include.extend(latest_files("reports/*.json", limit=8))
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        seen: set[Path] = set()
        for path in include:
            if path.exists() and path not in seen:
                seen.add(path)
                zf.write(path, path.relative_to(PROJECT_ROOT))
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a handoff bundle zip.")
    parser.parse_args()
    try:
        bundle = make_bundle()
    except Exception as exc:
        print(exc)
        return 1
    print(f"handoff_bundle: {bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
