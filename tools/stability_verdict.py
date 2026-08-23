from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import PROJECT_ROOT
from core.verdicts import judge_stability
from tools.summarize_memory import latest_memory_csv, summarize as summarize_memory
from tools.summarize_session import latest_session_jsonl, summarize as summarize_session


def main() -> int:
    parser = argparse.ArgumentParser(description="Judge latest v2_light stability run.")
    parser.add_argument("--memory")
    parser.add_argument("--session")
    parser.add_argument("--out-dir", default="reports")
    args = parser.parse_args()

    memory_path = Path(args.memory) if args.memory else latest_memory_csv()
    session_path = Path(args.session) if args.session else latest_session_jsonl()
    if memory_path is None:
        print("No memory.csv found.")
        return 1
    if session_path is None:
        print("No session.jsonl found.")
        return 1

    memory = summarize_memory(memory_path)
    session = summarize_session(session_path)
    result = {
        "memory": memory,
        "session": session,
        "verdict": judge_stability(memory, session),
    }
    out_dir = PROJECT_ROOT / args.out_dir
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"stability_verdict_{time.strftime('%Y-%m-%d_%H%M%S')}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["verdict"], ensure_ascii=False, indent=2))
    print(f"stability_report: {out}")
    return 0 if result["verdict"]["status"] != "fail" else 2


if __name__ == "__main__":
    raise SystemExit(main())
