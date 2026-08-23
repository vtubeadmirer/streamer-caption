from __future__ import annotations

import csv
import ctypes
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import PROJECT_ROOT
from core.events import CaptionEvent


@dataclass(frozen=True)
class MemorySnapshot:
    elapsed_sec: float
    rss_mb: float | None
    gpu_used_mb: float | None
    gpu_total_mb: float | None


class MemoryMonitor:
    def __init__(self, config: dict[str, Any], session_dir: Path | None = None) -> None:
        self.interval_sec = float(config["monitoring"]["memory_log_interval_min"]) * 60.0
        self.started = time.monotonic()
        self.last_sample = 0.0
        self.path = self._make_log_path(config, session_dir)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["elapsed_sec", "rss_mb", "gpu_used_mb", "gpu_total_mb"])

    def maybe_sample(self, force: bool = False) -> CaptionEvent | None:
        now = time.monotonic()
        if not force and now - self.last_sample < self.interval_sec:
            return None
        self.last_sample = now
        snapshot = self.snapshot()
        with self.path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    f"{snapshot.elapsed_sec:.3f}",
                    _format_optional(snapshot.rss_mb),
                    _format_optional(snapshot.gpu_used_mb),
                    _format_optional(snapshot.gpu_total_mb),
                ]
            )
        return CaptionEvent(
            event_type="memory_sample",
            engine="monitoring",
            metadata={
                "elapsed_sec": snapshot.elapsed_sec,
                "rss_mb": snapshot.rss_mb,
                "gpu_used_mb": snapshot.gpu_used_mb,
                "gpu_total_mb": snapshot.gpu_total_mb,
                "path": str(self.path),
            },
        )

    def snapshot(self) -> MemorySnapshot:
        return MemorySnapshot(
            elapsed_sec=time.monotonic() - self.started,
            rss_mb=_get_rss_mb(),
            gpu_used_mb=_get_gpu_memory()[0],
            gpu_total_mb=_get_gpu_memory()[1],
        )

    def _make_log_path(
        self,
        config: dict[str, Any],
        session_dir: Path | None,
    ) -> Path:
        if session_dir is not None:
            return Path(session_dir) / "memory.csv"
        stamp = time.strftime("%Y-%m-%d_%H%M%S")
        return PROJECT_ROOT / config["log"]["dir"] / stamp / "memory.csv"


def _format_optional(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}"


def _get_rss_mb() -> float | None:
    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    psapi = ctypes.WinDLL("psapi.dll")
    kernel32 = ctypes.WinDLL("kernel32.dll")
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    handle = kernel32.GetCurrentProcess()
    ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    if not ok:
        return None
    return counters.WorkingSetSize / (1024 * 1024)


def _get_gpu_memory() -> tuple[float | None, float | None]:
    command = [
        "nvidia-smi",
        "--query-gpu=memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=True)
    except Exception:
        return None, None
    first_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not first_line:
        return None, None
    row = next(csv.reader([first_line]))
    if len(row) < 2:
        return None, None
    return float(row[0].strip()), float(row[1].strip())
