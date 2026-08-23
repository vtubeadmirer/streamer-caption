from __future__ import annotations

import importlib
import json
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import CONFIG_PATH, PROJECT_ROOT, USER_DATA_ROOT, load_config


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def ok(name: str, detail: str) -> CheckResult:
    return CheckResult(name, "OK", detail)


def warn(name: str, detail: str) -> CheckResult:
    return CheckResult(name, "WARN", detail)


def fail(name: str, detail: str) -> CheckResult:
    return CheckResult(name, "FAIL", detail)


def check_python() -> CheckResult:
    version = sys.version.split()[0]
    prefix = sys.prefix
    if not (3, 11) <= sys.version_info[:2] <= (3, 13):
        return fail("python", f"{version}, supported versions are 3.11 through 3.13")
    if ".venv" not in prefix:
        return warn("python", f"{version}, not running from project .venv: {prefix}")
    return ok("python", f"{version}, {prefix}")

def check_imports() -> CheckResult:
    modules = [
        "numpy",
        "sounddevice",
        "faster_whisper",
        "ctranslate2",
        "websockets",
    ]
    missing = []
    versions = []
    for name in modules:
        try:
            module = importlib.import_module(name)
            versions.append(f"{name}={getattr(module, '__version__', 'unknown')}")
        except Exception as exc:
            missing.append(f"{name}: {exc}")
    if missing:
        return fail("imports", "; ".join(missing))
    return ok("imports", ", ".join(versions))


def check_config() -> CheckResult:
    try:
        config = load_config()
    except Exception as exc:
        return fail("config", str(exc))
    audio = config["audio"]
    whisper = config["whisper"]
    vad = config.get("vad", {})
    return ok(
        "config",
        "profile={profile}, source={source}, worker={worker}, vad_engine={vad_engine}, speech_threshold={speech_threshold}, device={device}, vad={vad}, gain={gain}, min_peak={min_peak}, model={model}, compute={compute}".format(
            profile=config.get("app", {}).get("profile", "custom"),
            source=audio.get("source", "microphone"),
            worker=config.get("streaming", {}).get(
                "worker_mode",
                "process",
            ),
            vad_engine=vad.get("engine", "energy"),
            speech_threshold=vad.get("speech_threshold", 0.5),
            device=audio["device"],
            vad=audio["vad_energy_threshold"],
            gain=audio.get("input_gain", 1.0),
            min_peak=audio.get("min_segment_peak_rms", 0.0),
            model=whisper["model"],
            compute=whisper["compute_type"],
        ),
    )


def check_gpu() -> CheckResult:
    try:
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
    except Exception as exc:
        return fail("gpu", str(exc))
    if count <= 0:
        return warn("gpu", "no CUDA devices visible")

    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        detail = result.stdout.strip().splitlines()[0]
    except Exception:
        detail = f"cuda_device_count={count}, nvidia-smi unavailable"
    return ok("gpu", detail)


def check_mic() -> CheckResult:
    config = load_config()
    audio = config["audio"]
    source = str(audio.get("source", "microphone"))
    if source == "system":
        try:
            from core.audio_sources import list_system_audio_devices

            devices = list_system_audio_devices()
        except Exception as exc:
            return fail("audio", f"PC playback unavailable: {exc}")
        if not devices:
            return fail("audio", "no Windows loopback output devices found")
        configured = str(audio.get("system_device", "")).strip().lower()
        if configured:
            matches = [
                device
                for device in devices
                if configured in {
                    device.id.lower(),
                    device.name.lower(),
                }
                or configured in device.name.lower()
            ]
            if not matches:
                return fail(
                    "audio",
                    f"configured PC output was not found: {configured}",
                )
            selected = matches[0]
        else:
            selected = next(
                (device for device in devices if device.is_default),
                devices[0],
            )
        return ok(
            "audio",
            f"system: {selected.name} id={selected.id}",
        )

    try:
        import sounddevice as sd

        devices = sd.query_devices()
    except Exception as exc:
        return fail("audio", str(exc))
    device_index = int(audio["device"])
    if device_index < 0 or device_index >= len(devices):
        return fail(
            "audio",
            f"configured microphone index out of range: {device_index}",
        )
    device = devices[device_index]
    channels = int(device.get("max_input_channels", 0))
    if channels <= 0:
        return fail(
            "audio",
            f"device {device_index} has no input channels: "
            f"{device.get('name')}",
        )
    return ok(
        "audio",
        f"microphone {device_index}: {device.get('name')} "
        f"channels={channels}",
    )

def check_ports() -> CheckResult:
    config = load_config()
    checks = [
        ("ws", config["server"]["host"], int(config["server"]["port"])),
        ("admin", config["server"]["host"], int(config["server"]["admin_port"])),
    ]
    busy = []
    free = []
    for label, host, port in checks:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
            free.append(f"{label}:{port}")
        except OSError:
            busy.append(f"{label}:{port}")
        finally:
            sock.close()
    if busy:
        return warn("ports", f"already in use: {', '.join(busy)}; free: {', '.join(free)}")
    return ok("ports", f"free: {', '.join(free)}")


def check_files() -> CheckResult:
    required = [
        "main.py",
        "overlay/caption.html",
        "overlay/admin.html",
        "START_HERE.bat",
        "run_baseline.bat",
    ]
    missing = [path for path in required if not (PROJECT_ROOT / path).exists()]
    if missing:
        return fail("files", "missing: " + ", ".join(missing))
    config_detail = (
        f"config={CONFIG_PATH}"
        if CONFIG_PATH.exists()
        else f"config defaults active; user_data={USER_DATA_ROOT}"
    )
    sample = PROJECT_ROOT / "samples" / "test_clip.wav"
    if not sample.exists():
        return warn("files", f"core files present; {config_detail}; test clip not recorded")
    return ok("files", f"core files present; {config_detail}; test clip present")


def run_checks() -> list[CheckResult]:
    checks: list[Callable[[], CheckResult]] = [
        check_python,
        check_imports,
        check_config,
        check_gpu,
        check_mic,
        check_ports,
        check_files,
    ]
    results = []
    for check in checks:
        try:
            results.append(check())
        except Exception as exc:
            results.append(fail(check.__name__, str(exc)))
    return results


def main() -> int:
    json_mode = "--json" in sys.argv
    results = run_checks()
    if json_mode:
        print(json.dumps([result.to_dict() for result in results], ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(f"[{result.status}] {result.name}: {result.detail}")
    return 1 if any(result.status == "FAIL" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
