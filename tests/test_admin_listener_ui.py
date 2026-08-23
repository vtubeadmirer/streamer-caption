from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    html = (ROOT / "overlay" / "admin.html").read_text(encoding="utf-8")
    required_ids = (
        'id="audioSource"',
        'id="microphoneDeviceField"',
        'id="systemAudioDeviceField"',
        'id="systemAudioDevice"',
        'id="warningText"',
        'id="sttWorkerMode"',
        'id="justChattingLineHold"',
        'id="defenseScoreThreshold"',
        'id="shortUtteranceChars"',
        'id="latencyTargetP95"',
        'id="summaryMaxBullets"',
        'id="topicSimilarityThreshold"',
        'id="highlightLaughterWeight"',
        'id="saveAnalysis"',
    )
    for required in required_ids:
        assert required in html, required
    guide_requirements = (
        "Caption a broadcast playing on this PC",
        "Use the validation screen during a broadcast",
        "Read the post-broadcast validation report",
        "Tune hallucination, misses, latency, and stalls",
        "Fast speech reading hold",
        "Tune broadcast analysis",
        "Broadcast archive management",
        "Recovery after a force stop or stalled recognition",
        "Best workflow for sending diagnostics to Codex",
        "PASS",
        "REVIEW",
        "WARN",
        "FAIL",
        'href="/validate"',
        'href="/history"',
    )
    for requirement in guide_requirements:
        assert requirement in html, requirement

    start_here = (ROOT / "START_HERE.bat").read_text(encoding="utf-8")
    for option in (
        "1. ",
        "Start live captions",
        "Auto GPU preset",
        "Validate latest broadcast",
        "Make support bundle",
        "START_ADVANCED.bat",
    ):
        assert option in start_here, option

    start_advanced = (ROOT / "START_ADVANCED.bat").read_text(encoding="utf-8")
    for option in ("AL.", "AM.", "AN.", "AE."):
        assert option in start_advanced, option

    auto_gpu = (ROOT / "tools" / "apply_auto_gpu_preset.py").read_text(encoding="utf-8")
    assert "gpu_12_16gb_balanced" in auto_gpu
    assert "gpu_20gb_quality" in auto_gpu

    assert "devices.system_devices || []" in html
    assert 'uiText("교체된 임시 ", "superseded ")' in html
    assert "if (latestStatus) bindStatus(latestStatus)" in html
    assert "});    document.getElementById" not in html
    assert "updateAudioSourceVisibility();      document" not in html
    print("admin_listener_ui_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())