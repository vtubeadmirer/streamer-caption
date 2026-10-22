"""The bug-report bundle must never contain what was said on stream."""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config  # noqa: E402
from tools.make_support_bundle import environment_text, make_issue_bundle, scrub_jsonl, scrub_value  # noqa: E402

SECRETS = ["오늘 비밀 이야기 하나 할게요", "my secret caption", "요약: 시청자와 비밀 이야기", "초기 프롬프트 비밀"]


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        logs = tmp / "logs"
        session = logs / "2026-10-23_000000_000"
        session.mkdir(parents=True)
        records = [
            {"ts": "2026-10-23T00:00:01", "event_type": "caption_confirmed", "text": SECRETS[0], "engine": "baseline",
             "audio_offset_sec": 1.5, "metadata": {"audio_source": "microphone", "source_label": "방송자", "no_speech_prob": 0.1}},
            {"ts": "2026-10-23T00:00:02", "event_type": "caption_pending", "text": SECRETS[1], "metadata": {"rms": 0.02}},
            {"ts": "2026-10-23T00:00:03", "event_type": "memory", "text": "", "metadata": {"rss_mb": 512.0, "gpu_used_mb": 3000}},
        ]
        (session / "session.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
        (session / "captions.txt").write_text(f"[00:00:01] [방송자] {SECRETS[0]}\n", encoding="utf-8")
        (logs / "latest_captions.txt").write_text(f"[00:00:01] {SECRETS[0]}\n", encoding="utf-8")
        (session / "memory.csv").write_text("elapsed_sec,rss_mb\n1,512\n", encoding="utf-8")
        (session / "broadcast_report.json").write_text(json.dumps({"summary": SECRETS[2], "captions": 3, "highlights": [{"text": SECRETS[1], "score": 0.9}]}, ensure_ascii=False), encoding="utf-8")
        (session / "broadcast_report.md").write_text(f"# report\n{SECRETS[2]}\n", encoding="utf-8")

        config = load_config()
        config["whisper"]["initial_prompt"] = SECRETS[3]
        status = {"last_caption": SECRETS[1], "caption_count": 3, "last_stt": {"text": SECRETS[0], "duration_sec": 1.2}}
        result = make_issue_bundle(status=status, note="자막이 멈춤", log_root=logs, out_dir=tmp / "out", config=config)

        with zipfile.ZipFile(result["path"]) as bundle:
            names = bundle.namelist()
            blob = b"".join(bundle.read(name) for name in names).decode("utf-8", errors="replace")
        for secret in SECRETS:
            assert secret not in blob, f"secret leaked: {secret}"
        assert not any(name.endswith(("captions.txt", "broadcast_report.md")) for name in names), names
        assert "logs/2026-10-23_000000_000/session.jsonl" in names, names
        assert "logs/2026-10-23_000000_000/memory.csv" in names, names
        assert "environment.json" in names and "config.json" in names and "status.json" in names and "note.txt" in names, names
        assert "[redacted" in blob and '"rss_mb": 512.0' in blob, "structure and numbers must survive scrubbing"
        assert "자막이 멈춤" in blob, "the user's own note is kept"
        assert result["environment_text"].startswith("version:"), result["environment_text"]
        assert "gpu:" in environment_text(result["environment"])

    scrubbed = scrub_value({"text": "hello", "n": 3, "nested": [{"summary": "x", "ok": True}]})
    assert scrubbed == {"text": "[redacted 5 chars]", "n": 3, "nested": [{"summary": "[redacted 1 chars]", "ok": True}]}, scrubbed
    assert scrub_jsonl('{"text": "a"}\nnot json\n') == '{"text": "[redacted 1 chars]"}\n{"unparsable_line": true}\n'
    print("support_bundle_privacy_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
