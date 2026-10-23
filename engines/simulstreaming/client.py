from __future__ import annotations

import json
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


class SimulStreamingError(RuntimeError):
    pass


class SimulStreamingClient:
    """TCP client for the official SimulStreaming Whisper server.

    This intentionally lives outside the normal segment STT worker. The official
    server owns a continuous audio session and emits partial JSON updates, so
    feeding it isolated VAD segments would remove its streaming advantage.
    """

    name = "simulstreaming_experiment"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        settings = config.get("simulstreaming", {})
        self.host = str(settings.get("host", "127.0.0.1"))
        self.port = int(settings.get("port", 43007))
        self.repo_dir = Path(str(settings.get("repo_dir", "experiments/vendor/SimulStreaming")))
        if not self.repo_dir.is_absolute():
            self.repo_dir = Path(__file__).resolve().parents[2] / self.repo_dir
        project_root = Path(__file__).resolve().parents[2]
        default_runtime = project_root / "experiments" / "simulstreaming-runtime" / ".venv" / "Scripts" / "python.exe"
        default_model = project_root / "experiments" / "simulstreaming-runtime" / "models" / "large-v3.pt"
        configured_python = str(settings.get("python_executable", "")).strip()
        self.python_executable = configured_python or (
            str(default_runtime) if default_runtime.exists() else sys.executable
        )
        configured_model = str(settings.get("model_path", "")).strip()
        self.model_path = configured_model or (
            str(default_model) if default_model.exists() else ""
        )
        self.allow_auto_download = bool(settings.get("allow_auto_download", False))
        self.start_server = bool(settings.get("start_server", True))
        self.min_chunk_size = float(settings.get("min_chunk_size", 0.8))
        self.vac_chunk_size = float(settings.get("vac_chunk_size", 0.32))
        self.audio_max_len = float(settings.get("audio_max_len", 30.0))
        self.beams = int(settings.get("beams", 1))
        self.frame_threshold = int(settings.get("frame_threshold", 25))
        self.use_vac = bool(settings.get("use_vac", True))
        self.never_fire = bool(settings.get("never_fire", False))
        self.connect_timeout_sec = float(settings.get("connect_timeout_sec", 90.0))
        self._process: subprocess.Popen[bytes] | None = None
        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._stop_reader = threading.Event()
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._errors: queue.Queue[str] = queue.Queue()
        self._server_log: Any | None = None
        self._started_at = 0.0
        self._last_error = ""

    def server_command(self) -> list[str]:
        script = self.repo_dir / "simulstreaming_whisper_server.py"
        if not script.exists():
            raise SimulStreamingError(
                "Official SimulStreaming source was not found: " + str(script)
            )
        if not self.model_path:
            raise SimulStreamingError("simulstreaming.model_path is required")
        if not Path(self.model_path).exists() and not self.allow_auto_download:
            raise SimulStreamingError(
                "SimulStreaming model was not found and automatic download is disabled: " + self.model_path
            )
        command = [
            self.python_executable,
            str(script),
            "--host", self.host,
            "--port", str(self.port),
            "--lan", str(self.config["whisper"].get("language", "ko")),
            "--task", "transcribe",
            "--min-chunk-size", str(self.min_chunk_size),
            "--audio_max_len", str(self.audio_max_len),
            "--beams", str(self.beams),
            "--frame_threshold", str(self.frame_threshold),
        ]
        if self.never_fire:
            command.append("--never_fire")
        if self.model_path:
            command.extend(["--model_path", self.model_path])
        if self.use_vac:
            command.extend(["--vac", "--vac-chunk-size", str(self.vac_chunk_size)])
        base_prompt = str(self.config.get("whisper", {}).get("initial_prompt", "")).strip()
        if base_prompt:
            command.extend(["--static_init_prompt", base_prompt])
        return command

    def start(self) -> None:
        if self._socket is not None:
            return
        if self.start_server:
            logs_dir = Path(__file__).resolve().parents[2] / "logs"
            logs_dir.mkdir(exist_ok=True)
            log_path = logs_dir / f"simulstreaming_server_{time.strftime('%Y%m%d_%H%M%S')}.log"
            self._server_log = log_path.open("wb")
            self._process = subprocess.Popen(
                self.server_command(),
                cwd=self.repo_dir,
                stdin=subprocess.DEVNULL,
                stdout=self._server_log,
                stderr=subprocess.STDOUT,
            )
        self._connect()
        self._started_at = time.monotonic()
        self._reader = threading.Thread(target=self._read_loop, name="simulstreaming-reader", daemon=True)
        self._reader.start()

    def send_audio(self, samples: Any) -> None:
        if self._socket is None:
            raise SimulStreamingError("SimulStreaming client is not connected")
        try:
            import numpy as np
        except ImportError:
            values = [
                max(-32767, min(32767, int(float(value) * 32767.0)))
                for value in samples
            ]
            if not values:
                return
            payload = struct.pack(f"<{len(values)}h", *values)
        else:
            audio = np.asarray(samples, dtype=np.float32).reshape(-1)
            if audio.size == 0:
                return
            pcm = np.clip(audio, -1.0, 1.0)
            payload = (pcm * 32767.0).astype("<i2", copy=False).tobytes()
        try:
            self._socket.sendall(payload)
        except OSError as exc:
            self._record_error(f"audio send failed: {exc}")
            raise SimulStreamingError(self._last_error) from exc

    def get_messages(self, max_items: int = 100) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for _ in range(max(1, max_items)):
            try:
                messages.append(self._messages.get_nowait())
            except queue.Empty:
                break
        return messages

    def get_errors(self) -> list[str]:
        errors: list[str] = []
        while True:
            try:
                errors.append(self._errors.get_nowait())
            except queue.Empty:
                return errors

    def snapshot(self) -> dict[str, Any]:
        process_alive = self._process is not None and self._process.poll() is None
        return {
            "engine": self.name,
            "connected": self._socket is not None,
            "server_started_by_app": self.start_server,
            "server_process_alive": process_alive,
            "server_pid": self._process.pid if self._process is not None else None,
            "host": self.host,
            "port": self.port,
            "elapsed_sec": max(0.0, time.monotonic() - self._started_at) if self._started_at else 0.0,
            "last_error": self._last_error,
        }

    def close(self) -> None:
        self._stop_reader.set()
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
        if self._process is not None:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    self._process.kill()
            self._process = None
        if self._server_log is not None:
            self._server_log.close()
            self._server_log = None

    def _connect(self) -> None:
        deadline = time.monotonic() + self.connect_timeout_sec
        last_error = ""
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise SimulStreamingError(
                    "SimulStreaming server exited before accepting a connection. Check logs/simulstreaming_server_*.log"
                )
            try:
                sock = socket.create_connection((self.host, self.port), timeout=1.0)
                sock.settimeout(0.25)
                self._socket = sock
                return
            except OSError as exc:
                last_error = str(exc)
                time.sleep(0.25)
        raise SimulStreamingError(
            f"Could not connect to SimulStreaming server at {self.host}:{self.port}: {last_error}"
        )

    def _read_loop(self) -> None:
        sock = self._socket
        if sock is None:
            return
        buffer = b""
        while not self._stop_reader.is_set():
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop_reader.is_set():
                    self._record_error(f"server receive failed: {exc}")
                return
            if not data:
                if not self._stop_reader.is_set():
                    self._record_error("SimulStreaming server closed the connection")
                return
            buffer += data.replace(b"\0", b"")
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    # The raw line can be caption text; record only its shape, never its content.
                    self._record_error(f"Invalid SimulStreaming JSON ({len(line)} chars)")
                    continue
                if isinstance(payload, dict):
                    self._messages.put(payload)

    def _record_error(self, message: str) -> None:
        self._last_error = message
        self._errors.put(message)