from __future__ import annotations

import collections
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from core.config import user_path
from engines.simulstreaming.availability import resolve_runtime

SAMPLE_RATE = 16000

# engines/simulstreaming/server.py exit codes -> failure reasons (privacy-safe categories).
STARTUP_EXIT_REASONS = {4: "no_cuda", 5: "model_missing", 6: "runtime_missing", 7: "out_of_memory"}
RUNNING_EXIT_REASONS = {7: "out_of_memory", 8: "stalled", 9: "crashed"}


class SimulStreamingError(RuntimeError):
    pass


class SimulStreamingClient:
    """Owns one SimulStreaming server process and its TCP stream.

    The server (engines/simulstreaming/server.py) runs in the optional CUDA runtime, owns
    its VAC/streaming state and answers with JSON lines: text increments (append-only),
    `is_final` at the end of each voiced segment and a heartbeat every second. Nothing in
    this class blocks the caller for long: the connection is made by a background thread
    while the model loads, audio goes through a sender thread, and `failure()` turns the
    process exit code, a silent server or a growing backlog into a reason string.
    """

    name = "simulstreaming"

    def __init__(
        self,
        config: dict[str, Any],
        *,
        model_path: str | Path | None = None,
        log_path: str | Path | None = None,
        command: list[str] | None = None,
    ) -> None:
        self.config = config
        settings = config.get("simulstreaming", {}) or {}
        runtime = resolve_runtime(config)
        self.repo_dir = Path(runtime["repo_dir"])
        self.server_script = Path(runtime["server_script"])
        self.python_executable = str(runtime["python"] or sys.executable)
        self.torch_home = runtime["torch_home"]
        self.silero_jit = runtime.get("silero_jit")
        default_model = next(iter(runtime["model_files"].values()), None)
        self.model_path = str(model_path or default_model or settings.get("model_path") or "")
        self.host = str(settings.get("host", "127.0.0.1"))
        self.port = int(settings.get("port", 0) or 0)
        self.start_server = bool(settings.get("start_server", True))
        self.min_chunk_size = float(settings.get("min_chunk_size", 0.5))
        self.vac_chunk_size = float(settings.get("vac_chunk_size", 0.04))
        self.audio_max_len = float(settings.get("audio_max_len", 30.0))
        self.beams = int(settings.get("beams", 1))
        self.frame_threshold = int(settings.get("frame_threshold", 40))
        self.use_vac = bool(settings.get("use_vac", True))
        self.never_fire = bool(settings.get("never_fire", False))
        self.start_timeout_sec = float(settings.get("connect_timeout_sec", 180.0))
        self.stall_timeout_sec = float(settings.get("stall_timeout_sec", 15.0))
        self.max_lag_sec = float(settings.get("max_lag_sec", 10.0))
        self.lag_grace_sec = 5.0
        self.max_backlog_sec = max(self.stall_timeout_sec * 2, 30.0)
        self._command_override = list(command) if command else None
        self._log_path = Path(log_path) if log_path else None
        self._port_file: Path | None = None  # the server writes the port it bound (port 0 = any)

        self._process: subprocess.Popen[bytes] | None = None
        self._server_log: Any | None = None
        self._socket: socket.socket | None = None
        self._lock = threading.Condition()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._errors: queue.Queue[str] = queue.Queue()
        self._send_queue: collections.deque[bytes] = collections.deque()
        self._queued_bytes = 0
        self._failure = ""
        self._last_error = ""
        self._launched_at = 0.0
        self._connected_at = 0.0
        self._last_message_at = 0.0
        self._last_audio_at = 0.0
        self._lag_since: float | None = None
        self._enqueued_samples = 0
        self._server_audio_sec = 0.0
        self._server_iter_sec = 0.0
        self._server_max_iter_sec = 0.0
        self._server_guards = 0
        self._hello: dict[str, Any] = {}
        self._exit_code: int | None = None
        self._connection_closed = False

    # ------------------------------------------------------------------ lifecycle

    def server_command(self) -> list[str]:
        if self._command_override is not None:
            command = list(self._command_override)
            if self._port_file is not None:
                command += ["--port", "0", "--port-file", str(self._port_file)]
            return command
        if not (self.repo_dir / "simulstreaming_whisper.py").exists():
            raise SimulStreamingError("SimulStreaming source was not found")
        if not self.model_path:
            raise SimulStreamingError("no SimulStreaming model file")
        command = [
            self.python_executable,
            "-u",
            str(self.server_script),
            "--repo", str(self.repo_dir),
            "--host", self.host,
            "--port", str(self.port),
            *(["--port-file", str(self._port_file)] if self._port_file is not None else []),
            "--parent-pid", str(os.getpid()),
            "--stall-exit-sec", str(max(20.0, self.stall_timeout_sec * 2)),
            "--accept-timeout-sec", str(max(60.0, self.start_timeout_sec)),
            "-l", "WARNING",
            "--lan", str(self.config.get("whisper", {}).get("language", "ko") or "ko"),
            "--task", "transcribe",
            "--min-chunk-size", str(self.min_chunk_size),
            "--audio_max_len", str(self.audio_max_len),
            "--beams", str(self.beams),
            "--frame_threshold", str(self.frame_threshold),
            "--model_path", self.model_path,
        ]
        if self.silero_jit is not None:
            command.extend(["--silero-jit", str(self.silero_jit)])
        if self.never_fire:
            command.append("--never_fire")
        if self.use_vac:
            command.extend(["--vac", "--vac-chunk-size", str(self.vac_chunk_size)])
        base_prompt = str(self.config.get("whisper", {}).get("initial_prompt", "") or "").strip()
        if base_prompt:
            command.extend(["--static_init_prompt", base_prompt])
        return command

    def launch(self) -> None:
        """Start the server (if configured) and connect in the background."""
        if self._launched_at:
            return
        self._launched_at = time.monotonic()
        if self.start_server:
            log_path = self._log_path or (
                user_path(self.config.get("log", {}).get("dir", "logs"))
                / f"simulstreaming_server_{time.strftime('%Y%m%d_%H%M%S')}.log"
            )
            if self.port <= 0:
                # The server binds any free port and names it in this file once it listens.
                self._port_file = log_path.parent / f"simulstreaming_{os.getpid()}_{id(self)}.port"
                try:
                    self._port_file.unlink()
                except OSError:
                    pass
            try:
                command = self.server_command()
            except SimulStreamingError as exc:
                self._fail("model_missing" if "model" in str(exc) else "source_missing", str(exc))
                return
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                self._server_log = log_path.open("ab")
                self._server_log.write(f"--- start {time.strftime('%Y-%m-%d %H:%M:%S')} port={self.port}\n".encode())
                self._server_log.flush()
            except OSError as exc:
                self._fail("start_failed", f"server log not writable: {type(exc).__name__}")
                return
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            if self.torch_home is not None:
                env["TORCH_HOME"] = str(self.torch_home)
            try:
                self._process = subprocess.Popen(
                    command,
                    cwd=str(self.repo_dir if self.repo_dir.is_dir() else Path(__file__).resolve().parent),
                    # No stdin pipe: a thread blocked reading it stalled `import torch` on Windows.
                    stdin=subprocess.DEVNULL,
                    stdout=self._server_log,
                    stderr=subprocess.STDOUT,
                    env=env,
                    # Share the app's console (CREATE_NO_WINDOW would make the venv launcher's
                    # child open a console window of its own) but not its Ctrl+C: the app stops
                    # the server itself (closed connection, or kill while it is still loading).
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
            except OSError as exc:
                self._fail("runtime_missing", f"server could not start: {type(exc).__name__}")
                return
        elif self.port <= 0:
            self._fail("start_failed", "an external server needs simulstreaming.port")
            return
        self._spawn(self._connect_loop, "simulstreaming-connect")

    def start(self) -> None:
        """Blocking start for tools: raises SimulStreamingError with the failure reason."""
        self.launch()
        while not self.connected:
            reason = self.failure()
            if reason:
                raise SimulStreamingError(f"SimulStreaming did not start: {reason}")
            time.sleep(0.1)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            sock = self._socket
            self._socket = None
            self._lock.notify_all()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        process = self._process
        if process is not None:
            # A connected server returns when its connection closes; one still loading the model
            # has nothing to finish and is stopped at once.
            try:
                process.wait(timeout=5.0 if sock is not None else 0.2)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass
            self._exit_code = process.returncode
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2.0)
        self._threads = []
        if self._port_file is not None:
            try:
                self._port_file.unlink()
            except OSError:
                pass
        if self._server_log is not None:
            try:
                self._server_log.close()
            except OSError:
                pass
            self._server_log = None

    def kill(self) -> None:
        """Stop a misbehaving server right away (stall, lag); close() still cleans up."""
        process = self._process
        if process is not None and process.poll() is None:
            process.kill()

    # ------------------------------------------------------------------ streaming

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._socket is not None and not self._failure

    def send_audio(self, samples: Any) -> None:
        import numpy as np

        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            return
        payload = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2", copy=False).tobytes()
        with self._lock:
            if self._socket is None or self._failure:
                return
            self._send_queue.append(payload)
            self._queued_bytes += len(payload)
            self._enqueued_samples += audio.size
            self._last_audio_at = time.monotonic()
            self._lock.notify_all()

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

    def failure(self) -> str:
        """"" while healthy; otherwise a reason category (never caption text)."""
        now = time.monotonic()
        with self._lock:
            if self._failure:
                return self._failure
            connected = self._socket is not None
            queued_sec = self._queued_bytes / 2 / SAMPLE_RATE
            last_message_at = self._last_message_at
            lag = self._enqueued_samples / SAMPLE_RATE - self._server_audio_sec
        process = self._process
        if process is not None:
            code = process.poll()
            if code is not None:
                self._exit_code = code
                if not connected and not self._connected_at:
                    self._fail(STARTUP_EXIT_REASONS.get(code, "start_failed"), f"server exited with code {code}")
                else:
                    self._fail(RUNNING_EXIT_REASONS.get(code, "crashed"), f"server exited with code {code}")
                return self._failure
        if not connected:
            if not self._connected_at and now - self._launched_at > self.start_timeout_sec:
                self._fail("start_timeout", f"server not ready after {self.start_timeout_sec:.0f}s")
                self.kill()
            return self._failure
        if self._connection_closed:
            return ""  # _connection_ended() is waiting for the exit code to name the cause
        if now - last_message_at > self.stall_timeout_sec:
            self._fail("stalled", f"no server heartbeat for {now - last_message_at:.1f}s")
            self.kill()
            return self._failure
        if queued_sec > self.max_backlog_sec:
            self._fail("stalled", f"server stopped reading audio ({queued_sec:.0f}s queued)")
            self.kill()
            return self._failure
        if lag > self.max_lag_sec:
            if self._lag_since is None:
                self._lag_since = now
            elif now - self._lag_since > self.lag_grace_sec:
                self._fail("too_slow", f"server {lag:.1f}s behind the audio")
                self.kill()
                return self._failure
        else:
            self._lag_since = None
        return ""

    def snapshot(self) -> dict[str, Any]:
        process = self._process
        now = time.monotonic()
        with self._lock:
            lag = self._enqueued_samples / SAMPLE_RATE - self._server_audio_sec
            return {
                "engine": self.name,
                "connected": self._socket is not None,
                "server_started_by_app": self.start_server,
                "server_process_alive": process is not None and process.poll() is None,
                "server_pid": process.pid if process is not None else None,
                "port": self.port,
                "model": Path(self.model_path).stem if self.model_path else "",
                "elapsed_sec": round(now - self._launched_at, 1) if self._launched_at else 0.0,
                "heartbeat_age_sec": round(now - self._last_message_at, 1) if self._last_message_at else None,
                "audio_sent_sec": round(self._enqueued_samples / SAMPLE_RATE, 1),
                "server_audio_sec": round(self._server_audio_sec, 1),
                "lag_sec": round(max(0.0, lag), 2),
                "server_iter_sec": self._server_iter_sec,
                "server_max_iter_sec": self._server_max_iter_sec,
                "server_guard_triggers": self._server_guards,
                "exit_code": self._exit_code,
                "failure": self._failure,
                "last_error": self._last_error,
            }

    # ------------------------------------------------------------------ threads

    def _spawn(self, target: Any, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _fail(self, reason: str, detail: str) -> None:
        with self._lock:
            if self._failure:
                return
            self._failure = reason
            self._last_error = detail
            self._lock.notify_all()
        self._errors.put(f"{reason}: {detail}")

    def _connection_ended(self, detail: str) -> None:
        """A send or receive failed: the exit code, if the server is gone, names the cause."""
        if self._stop.is_set():
            return
        self._connection_closed = True
        process = self._process
        code = None
        if process is not None:
            try:
                code = process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                code = None
        if code is not None:
            self._exit_code = code
            self._fail(RUNNING_EXIT_REASONS.get(code, "crashed"), f"server exited with code {code}")
        else:
            self._fail("connection_lost", detail)

    def _connect_loop(self) -> None:
        deadline = self._launched_at + self.start_timeout_sec
        while not self._stop.is_set() and time.monotonic() < deadline:
            process = self._process
            if process is not None and process.poll() is not None:
                return  # failure() classifies the exit code
            if self.port <= 0:
                try:
                    self.port = int(self._port_file.read_text(encoding="ascii").strip()) if self._port_file else 0
                except (OSError, ValueError):
                    self.port = 0
                if self.port <= 0:
                    self._stop.wait(0.25)  # still loading the model
                    continue
            try:
                sock = socket.create_connection((self.host, self.port), timeout=1.0)
            except OSError:
                self._stop.wait(0.25)
                continue
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                if self._stop.is_set():
                    sock.close()
                    return
                self._socket = sock
                now = time.monotonic()
                self._connected_at = now
                self._last_message_at = now
                self._lock.notify_all()
            self._spawn(lambda: self._read_loop(sock), "simulstreaming-reader")
            self._spawn(lambda: self._send_loop(sock), "simulstreaming-sender")
            return

    def _send_loop(self, sock: socket.socket) -> None:
        while True:
            with self._lock:
                while not self._send_queue and not self._stop.is_set() and self._socket is sock and not self._failure:
                    self._lock.wait(timeout=0.5)
                if self._stop.is_set() or self._socket is not sock or self._failure:
                    return
                payload = self._send_queue.popleft()
            try:
                sock.sendall(payload)
            except OSError as exc:
                self._connection_ended(f"audio send failed: {type(exc).__name__}")
                return
            with self._lock:
                self._queued_bytes -= len(payload)

    def _read_loop(self, sock: socket.socket) -> None:
        buffer = b""
        while not self._stop.is_set():
            try:
                data = sock.recv(65536)
            except OSError as exc:
                self._connection_ended(f"receive failed: {type(exc).__name__}")
                return
            if not data:
                self._connection_ended("server closed the connection")
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
                    # The line can be caption text; record only its size, never its content.
                    self._errors.put(f"Invalid SimulStreaming JSON ({len(line)} chars)")
                    continue
                if not isinstance(payload, dict):
                    continue
                now = time.monotonic()
                kind = payload.get("type")
                with self._lock:
                    self._last_message_at = now
                    if kind == "heartbeat":
                        self._server_audio_sec = float(payload.get("audio_sec", self._server_audio_sec) or 0.0)
                        self._server_iter_sec = float(payload.get("iter_sec", 0.0) or 0.0)
                        self._server_max_iter_sec = float(payload.get("max_iter_sec", 0.0) or 0.0)
                        self._server_guards = int(payload.get("guards", 0) or 0)
                        continue
                    if kind == "hello":
                        self._hello = payload
                        continue
                self._messages.put(payload)
