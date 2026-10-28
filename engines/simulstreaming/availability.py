"""Can this PC run the 정확 우선 (SimulStreaming) engine, and with which files?

Only file-existence checks and an optional nvidia-smi call: the admin HTTP thread uses this
module, and importing torch or ctranslate2 there has crashed the app before.

Where the optional runtime lives (phase 3 downloads it to the first location):
  <install root>/simulstreaming/.venv            CUDA PyTorch runtime
  <install root>/simulstreaming/models/*.pt      Whisper .pt weights
  <install root>/simulstreaming/torch_home       Silero VAD torch.hub cache (optional)
A developer checkout uses experiments/simulstreaming-runtime/ (setup_simulstreaming_experiment.bat).
config.simulstreaming.python_executable / model_dir / model_path override the defaults.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from core.app_info import install_root
from core.config import PROJECT_ROOT

SERVER_SCRIPT = Path(__file__).resolve().with_name("server.py")

# VRAM figures from the 2026-10 lab runs on an RTX 4060 Ti 16GB (peak growth of total GPU
# memory while streaming, plus ~600 MiB margin). min_total is the card size we offer it on;
# the faster-whisper standby model and the desktop need the rest.
MODELS: dict[str, dict[str, Any]] = {
    "large-v3": {
        "file": "large-v3.pt",
        "need_free_mib": 10_800,
        "min_total_mib": 15_000,
        "label_ko": "large-v3 (가장 정확, 그래픽 메모리 약 10GB)",
        "label_en": "large-v3 (most accurate, about 10 GB of GPU memory)",
    },
    "large-v3-turbo": {
        "file": "large-v3-turbo.pt",
        "need_free_mib": 6_300,
        "min_total_mib": 10_000,
        "label_ko": "large-v3-turbo (가벼움, 그래픽 메모리 약 6GB)",
        "label_en": "large-v3-turbo (lighter, about 6 GB of GPU memory)",
    },
}
MODEL_CHOICES = ("auto", "large-v3", "large-v3-turbo")
AUTO_ORDER = ("large-v3", "large-v3-turbo")
ENGINE_CHOICES = ("baseline", "simulstreaming")

# Shown to streamers next to the disabled option; plain words, no paths.
UNAVAILABLE_MESSAGES = {
    "source_missing": "프로그램 파일 일부가 없습니다 — 다시 설치해 주세요",
    "runtime_missing": "추가 구성요소가 필요합니다 — 다음 업데이트에서 내려받기 지원 예정",
    "model_missing": "음성 인식 모델 파일이 필요합니다 — 다음 업데이트에서 내려받기 지원 예정",
    "gpu_too_small": "그래픽카드 메모리가 부족합니다 (정확 우선은 그래픽 메모리 10GB 이상 필요)",
}


def _runtime_roots() -> list[Path]:
    roots: list[Path] = []
    root = install_root()
    if root is not None:
        roots.append(root / "simulstreaming")
    roots.append(PROJECT_ROOT / "experiments" / "simulstreaming-runtime")
    return roots


def _venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def _root_of(python: Path) -> Path:
    """<root>/.venv/Scripts/python.exe -> <root>; an embedded python.exe -> its folder."""
    parent = python.parent
    if parent.name.lower() in {"scripts", "bin"} and parent.parent.name.lower().endswith("venv"):
        return parent.parent.parent
    return parent


def _has_torch(python: Path) -> bool:
    candidates = [
        python.parent.parent / "Lib" / "site-packages" / "torch" / "__init__.py",
        python.parent / "Lib" / "site-packages" / "torch" / "__init__.py",
    ]
    if any(path.is_file() for path in candidates):
        return True
    return any(python.parent.parent.glob("lib/python3*/site-packages/torch/__init__.py"))


def resolve_runtime(config: dict[str, Any]) -> dict[str, Any]:
    """Paths the engine would use; nothing is checked for usability beyond existence."""
    settings = config.get("simulstreaming", {}) or {}
    repo_dir = Path(str(settings.get("repo_dir", "experiments/vendor/SimulStreaming")))
    if not repo_dir.is_absolute():
        repo_dir = PROJECT_ROOT / repo_dir
    configured = str(settings.get("python_executable", "") or "").strip()
    python: Path | None = None
    if configured:
        python = Path(configured)
        root = _root_of(python)
    else:
        roots = _runtime_roots()
        root = roots[0]
        for candidate_root in roots:
            candidate = _venv_python(candidate_root)
            if candidate.is_file():
                python, root = candidate, candidate_root
                break
    configured_dir = str(settings.get("model_dir", "") or "").strip()
    model_dir = Path(configured_dir) if configured_dir else root / "models"
    model_files: dict[str, Path] = {}
    explicit = str(settings.get("model_path", "") or "").strip()
    if explicit:
        path = Path(explicit)
        if path.is_file():
            model_files[path.stem] = path
    else:
        for name, spec in MODELS.items():
            path = model_dir / spec["file"]
            if path.is_file():
                model_files[name] = path
    torch_home = root / "torch_home"
    return {
        "repo_dir": repo_dir,
        "server_script": SERVER_SCRIPT,
        "python": python,
        "runtime_root": root,
        "model_dir": model_dir,
        "model_files": model_files,
        "explicit_model": bool(explicit),
        "torch_home": torch_home if torch_home.is_dir() else None,
    }


_GPU_LOCK = threading.Lock()
_GPU_CACHE: dict[str, Any] = {"at": -1e9, "value": None}


def query_gpu_memory(max_age_sec: float = 30.0) -> dict[str, int] | None:
    """Largest total and free MiB over the NVIDIA GPUs, or None without nvidia-smi."""
    with _GPU_LOCK:
        if time.monotonic() - float(_GPU_CACHE["at"]) <= max_age_sec:
            return _GPU_CACHE["value"]
    value: dict[str, int] | None = None
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        rows = []
        for line in result.stdout.strip().splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) >= 2:
                rows.append((int(float(parts[0])), int(float(parts[1]))))
        if rows:
            value = {"total_mib": max(row[0] for row in rows), "free_mib": max(row[1] for row in rows)}
    except Exception:
        value = None
    with _GPU_LOCK:
        _GPU_CACHE.update({"at": time.monotonic(), "value": value})
    return value


GpuProbe = Callable[[], "dict[str, int] | None"]
_DEFAULT_PROBE: Any = object()  # "query_gpu_memory, looked up at call time" (tests replace it)


def _probe(gpu_probe: Any) -> dict[str, int] | None:
    if gpu_probe is _DEFAULT_PROBE:
        return query_gpu_memory()
    return gpu_probe() if gpu_probe is not None else None


def engine_status(config: dict[str, Any], gpu_probe: Any = _DEFAULT_PROBE) -> dict[str, Any]:
    """{"available", "reason", "message", "models": {name: usable}} without paths."""
    runtime = resolve_runtime(config)
    gpu = _probe(gpu_probe)
    models: dict[str, bool] = {}
    for name, path in runtime["model_files"].items():
        spec = MODELS.get(name, MODELS["large-v3"])
        models[name] = gpu is None or gpu["total_mib"] >= spec["min_total_mib"]
    reason = ""
    if not runtime["repo_dir"].joinpath("simulstreaming_whisper.py").is_file() or not SERVER_SCRIPT.is_file():
        reason = "source_missing"
    elif runtime["python"] is None or not runtime["python"].is_file() or not _has_torch(runtime["python"]):
        reason = "runtime_missing"
    elif not models:
        reason = "model_missing"
    elif not any(models.values()):
        reason = "gpu_too_small"
    return {
        "available": not reason,
        "reason": reason,
        "message": UNAVAILABLE_MESSAGES.get(reason, ""),
        "models": models,
        "gpu_total_mib": gpu["total_mib"] if gpu else None,
    }


def choose_model(
    config: dict[str, Any],
    runtime: dict[str, Any],
    free_mib: int | None,
    exclude: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, Path | None, str]:
    """(model name, .pt path, "") or ("", None, reason) for the configured choice and the
    GPU memory that is free right now. `exclude`: models that already ran out of memory."""
    files: dict[str, Path] = runtime["model_files"]
    if not files:
        return "", None, "model_missing"
    wanted = str((config.get("simulstreaming", {}) or {}).get("model", "auto") or "auto")
    if runtime["explicit_model"]:
        candidates = list(files)
    elif wanted == "auto":
        candidates = [name for name in AUTO_ORDER if name in files]
    else:
        candidates = [wanted] if wanted in files else []
    if not candidates:
        return "", None, "model_missing"
    candidates = [name for name in candidates if name not in exclude]
    if not candidates:
        return "", None, "out_of_memory"
    for name in candidates:
        need = MODELS.get(name, MODELS["large-v3"])["need_free_mib"]
        if free_mib is None or free_mib >= need:
            return name, files[name], ""
    return "", None, "not_enough_vram"


def engine_overview(
    config: dict[str, Any],
    running: dict[str, Any] | None,
    gpu_probe: Any = _DEFAULT_PROBE,
) -> dict[str, Any]:
    """What the admin page needs for the 자막 엔진 choice."""
    selected = str(config.get("streaming", {}).get("engine", "baseline") or "baseline")
    if selected not in ENGINE_CHOICES:
        selected = "baseline"
    status = engine_status(config, gpu_probe)
    settings = config.get("simulstreaming", {}) or {}
    model = str(settings.get("model", "auto") or "auto")
    running = dict(running or {})
    models = [{"id": "auto", "label_ko": "자동 (추천)", "label_en": "Automatic (recommended)", "available": status["available"]}]
    for name in AUTO_ORDER:
        models.append({
            "id": name,
            "label_ko": MODELS[name]["label_ko"],
            "label_en": MODELS[name]["label_en"],
            "available": bool(status["models"].get(name)),
        })
    restart_required = bool(running) and (
        running.get("requested_engine", selected) != selected
        or (selected == "simulstreaming" and running.get("model_setting", model) != model)
    )
    return {
        "selected": selected,
        "options": [
            {
                "id": "baseline",
                "label_ko": "기본 (빠름)",
                "label_en": "Standard (fast)",
                "description_ko": "말하는 즉시 회색 미리보기가 뜨고, 말이 끝나면 확정 자막으로 바뀝니다.",
                "description_en": "A gray preview appears right away and turns into the final caption when you pause.",
                "available": True,
                "reason": "",
            },
            {
                "id": "simulstreaming",
                "label_ko": "정확 우선",
                "label_en": "Accuracy first",
                "description_ko": "약 1초 늦게 뜨지만, 한 번 뜬 글자는 바뀌지 않습니다. 문제가 생기면 자동으로 기본 엔진으로 바뀝니다.",
                "description_en": "About one second later, but text never changes once shown. Falls back to Standard automatically.",
                "available": status["available"],
                "reason": status["reason"],
                "message": status["message"],
            },
        ],
        "simulstreaming": {
            "model": model if model in MODEL_CHOICES else "auto",
            "models": models,
            "frame_threshold": settings.get("frame_threshold"),
        },
        "running": running,
        "restart_required": restart_required,
    }
