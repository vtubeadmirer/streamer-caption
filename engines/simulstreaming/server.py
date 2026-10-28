"""Streamer Caption's SimulStreaming server.

This script runs inside the optional SimulStreaming runtime (a separate Python with CUDA
PyTorch), never inside the app's own runtime, so it imports only the standard library,
numpy, torch and the vendored upstream source. The upstream source under
experiments/vendor/SimulStreaming stays byte-identical to ufal/SimulStreaming 077ea37; the
fixes found in the 2026-10 lab runs are applied here at import time instead:

* split_tokens_on_unicode bounds check: upstream raised IndexError on Korean after a few
  minutes (a Hangul syllable split across byte tokens) and the exception killed the server.
* hide_incomplete_unicode: upstream held back only the last byte token, which cut the
  syllable before it and showed broken characters; the whole incomplete tail is held now.
* runaway/duplicate guard for the greedy decoder: a repetition loop (" . . . .") ran for
  over 50 s in the end-of-utterance flush and stalled the stream; the flush could also
  re-transcribe the whole utterance.
* Silero VAC is loaded from a local copy only (a model file shipped with the runtime, the
  silero-vad package or an existing torch.hub cache): no trust prompt, no network.
* Model weights are used as present: upstream hashed the 3 GB file on every start and
  downloaded it again when the hash differed. The file names must stay large-v3.pt /
  large-v3-turbo.pt because the alignment heads are chosen by name.

The server loop itself is ours as well: one client per process, PCM16 alignment kept
across reads, a heartbeat line every second, no caption text in the log, and distinct exit
codes so the app can tell a missing runtime from a crash, a stall or a full GPU.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import select
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

# Exit codes read by engines/simulstreaming/client.py.
EXIT_OK = 0
EXIT_STARTUP = 3          # bad arguments, incompatible upstream source, port in use
EXIT_NO_CUDA = 4          # torch has no usable CUDA device
EXIT_MODEL_MISSING = 5    # the .pt file is not there (upstream would download 3 GB)
EXIT_RUNTIME_MISSING = 6  # torch / librosa / upstream modules cannot be imported
EXIT_OUT_OF_MEMORY = 7    # CUDA out of memory, at load time or while streaming
EXIT_STALLED = 8          # one model update ran longer than --stall-exit-sec
EXIT_CRASHED = 9          # any other exception while streaming
EXIT_PARENT_GONE = 10     # the app process ended (--parent-pid)

SAMPLE_RATE = 16000
LOG = logging.getLogger("streamer_caption.simul_server")
GUARD_COUNTS: dict[str, int] = {}
UNICODE_FALLBACKS = [0]


def _strip_own_dir_from_path() -> None:
    """Running as a script puts engines/simulstreaming first on sys.path; nothing here should
    shadow upstream module names (client.py, route.py ...)."""
    here = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))
    sys.path[:] = [p for p in sys.path if os.path.normcase(os.path.abspath(p or ".")) != here]


def is_out_of_memory(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return (
        type(exc).__name__ == "OutOfMemoryError"
        or "out of memory" in text
        or "cublas_status_alloc_failed" in text
        or "cuda_error_out_of_memory" in text
    )


def _describe(exc: BaseException) -> str:
    message = " ".join(str(exc).split())[:300]
    frames = traceback.extract_tb(exc.__traceback__)[-3:]
    where = " <- ".join(f"{Path(frame.filename).name}:{frame.lineno}" for frame in reversed(frames))
    return f"{type(exc).__name__}: {message} [{where}]"


# --------------------------------------------------------------------------- upstream fixes


def patch_unicode_split() -> None:
    from simulstreaming.whisper.simul_whisper.whisper import tokenizer as tk

    if not hasattr(tk.Tokenizer, "split_tokens_on_unicode"):
        raise RuntimeError("upstream Tokenizer.split_tokens_on_unicode not found")

    def split_tokens_on_unicode(self, tokens):
        decoded_full = self.decode_with_timestamps(tokens)
        replacement_char = "�"
        words, word_tokens, current_tokens = [], [], []
        unicode_offset = 0
        for token in tokens:
            current_tokens.append(token)
            decoded = self.decode_with_timestamps(current_tokens)
            if replacement_char not in decoded:
                boundary = True
            else:
                position = unicode_offset + decoded.index(replacement_char)
                if position < len(decoded_full):
                    boundary = decoded_full[position] == replacement_char
                else:
                    boundary = True  # upstream raised IndexError here
                    UNICODE_FALLBACKS[0] += 1
            if boundary:
                words.append(decoded)
                word_tokens.append(current_tokens)
                current_tokens = []
                unicode_offset += len(decoded)
        return words, word_tokens

    tk.Tokenizer.split_tokens_on_unicode = split_tokens_on_unicode


def patch_hide_incomplete_unicode() -> None:
    import simulstreaming_whisper as sw

    online = sw.SimulWhisperOnline
    if not hasattr(online, "hide_incomplete_unicode") or not hasattr(online, "timestamped_text"):
        raise RuntimeError("upstream SimulWhisperOnline hooks not found")

    def hide_incomplete_unicode(self, tokens):
        if self.unicode_buffer != []:
            tokens = self.unicode_buffer + tokens
            self.unicode_buffer = []
        tokens = list(tokens)
        enc = self.model.tokenizer.encoding
        eot = self.model.tokenizer.eot

        def complete(k: int) -> bool:
            data = b"".join(enc.decode_single_token_bytes(t) for t in tokens[:k] if t < eot)
            try:
                data.decode("utf-8")
                return True
            except UnicodeDecodeError:
                return False

        n = len(tokens)
        last = getattr(self, "is_last", False)
        # The longest prefix that ends on a character boundary; hold back at most 4 tokens.
        # At the end of an utterance nothing can complete the piece any more: drop it.
        for k in range(n, max(n - 4, 0) - 1, -1):
            if complete(k):
                if k < n and not last:
                    self.unicode_buffer = tokens[k:]
                return tokens[:k]
        return tokens  # malformed bytes: show them rather than stall

    def timestamped_text(self, tokens, generation):
        if not generation or not tokens:
            return []
        frames = [p["most_attended_frames"][0] for p in generation["progress"]] or [0]
        n = len(tokens)
        if len(frames) < n:  # tokens held back from the previous update come first
            frames = [frames[0]] * (n - len(frames)) + frames
        frames = frames[:n]
        split_words, split_tokens = self.model.tokenizer.split_to_word_tokens(list(tokens))
        out = []
        i = 0
        for word, word_tokens in zip(split_words, split_tokens):
            b = frames[min(i, n - 1)]
            i += len(word_tokens)
            e = frames[min(i - 1, n - 1)]
            out.append({"start": b * 0.02 + self.audio_bufer_offset, "end": e * 0.02 + self.audio_bufer_offset,
                        "text": word, "tokens": word_tokens})
        return out

    online.hide_incomplete_unicode = hide_incomplete_unicode
    online.timestamped_text = timestamped_text


def patch_runaway_guard(max_new: int = 120) -> None:
    """Stop the greedy decoder for this update when it starts repeating itself or restarts the
    utterance it already committed. Never changes normal output (lab runs S1g/S5/L1)."""
    import torch
    from simulstreaming.whisper.simul_whisper import simul_whisper as swm
    from simulstreaming.whisper.simul_whisper.whisper.decoding import GreedyDecoder

    if not hasattr(GreedyDecoder, "update") or not hasattr(swm.PaddedAlignAttWhisper, "infer"):
        raise RuntimeError("upstream decoder hooks not found")
    original_update = GreedyDecoder.update

    def update(self, tokens, logits, sum_logprobs):
        if getattr(self, "_guard_start", None) is None:
            self._guard_start = tokens.shape[1]
        tokens, completed = original_update(self, tokens, logits, sum_logprobs)
        if completed:
            return tokens, completed
        new = tokens[0, self._guard_start:].tolist()
        n = len(new)
        length = tokens.shape[1]
        reason = None
        cut = None  # tokens to keep before the decoder loop strips one more
        if n >= max_new:
            reason, cut = "max_new_tokens", length
        else:
            for period, reps in ((1, 6), (2, 4), (3, 4), (4, 4)):
                if n >= period * reps:
                    tail = new[-period * reps:]
                    if all(tail[i] == tail[i % period] for i in range(len(tail))):
                        reason = f"repeat_p{period}"
                        cut = length - period * (reps - 1) + 1  # keep one copy of the pattern
                        break
            committed = getattr(self, "_guard_committed", None)
            if reason is None and committed and n >= 4 and len(committed) >= n and new == committed[:n]:
                reason = "utterance_restart"
                cut = length - n + 1  # drop the re-transcription entirely
        if reason is None:
            return tokens, completed
        GUARD_COUNTS[reason] = GUARD_COUNTS.get(reason, 0) + 1
        LOG.info("runaway guard: %s (new_tokens=%d, counts=%s)", reason, n, GUARD_COUNTS)
        return tokens[:, :cut], True

    GreedyDecoder.update = update
    original_infer = swm.PaddedAlignAttWhisper.infer

    def infer(self, is_last=False):
        decoder = self.token_decoder
        decoder._guard_start = None
        decoder._guard_committed = (
            torch.cat(self.tokens[1:], dim=1)[0].tolist() if len(self.tokens) > 1 else []
        )
        return original_infer(self, is_last)

    swm.PaddedAlignAttWhisper.infer = infer


class OfflineFileMissing(RuntimeError):
    """A file the server would otherwise fetch from the internet (model weights, Silero VAD)."""


def patch_silero_loading(jit_path: str = "") -> None:
    """Upstream VAC calls torch.hub.load('snakers4/silero-vad', 'silero_vad') without
    trust_repo: on torch 2.14 the trust prompt with a closed stdin ends the server at start,
    and every start asks GitHub for an unpinned master. Load a local copy only: the model file
    shipped with the runtime, the silero-vad package, or an existing torch.hub cache."""
    import torch

    original_load = torch.hub.load

    def load(repo_or_dir, model, *args, **kwargs):
        if str(repo_or_dir).strip("/").lower() != "snakers4/silero-vad" or model != "silero_vad":
            return original_load(repo_or_dir, model, *args, **kwargs)
        if jit_path and Path(jit_path).is_file():
            LOG.info("silero VAC: model file shipped with the runtime")
            vad = torch.jit.load(jit_path, map_location="cpu")
            vad.eval()
            return vad, None
        try:
            from silero_vad import load_silero_vad

            LOG.info("silero VAC: silero-vad package")
            return load_silero_vad(), None
        except ImportError:
            pass
        hub_dir = Path(torch.hub.get_dir())
        for ref in ("master", "main"):
            cached = hub_dir / f"snakers4_silero-vad_{ref}"
            if (cached / "hubconf.py").is_file():
                LOG.info("silero VAC: local torch.hub cache")
                return original_load(str(cached), model, source="local", trust_repo=True)
        raise OfflineFileMissing("Silero VAD is not part of the runtime; the server does not download it")

    torch.hub.load = load


def patch_model_download() -> None:
    """Upstream's whisper._download re-reads the whole .pt for a SHA-256 on every start (3 GB in
    RAM, seconds of CPU) and downloads it again from the internet when the hash differs. The
    app's downloader checks the hash once; here a present file is used as is, never fetched."""
    import os as _os

    from simulstreaming.whisper.simul_whisper import whisper as whisper_module

    if not hasattr(whisper_module, "_download"):
        raise RuntimeError("upstream whisper._download not found")

    def _download(url: str, root: str, in_memory: bool):
        target = _os.path.join(root, _os.path.basename(url))
        if not _os.path.isfile(target):
            raise OfflineFileMissing(f"model file missing: {_os.path.basename(url)}")
        if in_memory:
            with open(target, "rb") as handle:
                return handle.read()
        return target

    whisper_module._download = _download


# --------------------------------------------------------------------------- watchdogs


def watch_parent(pid: int) -> None:
    """Exit when the app process ends, so a crashed app never leaves ~10 GB of VRAM held.

    Not done by reading stdin: on Windows a thread blocked in a synchronous read on the stdin
    pipe stalls other calls on that handle (isatty during `import torch` hung the start)."""
    if pid <= 0:
        return
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        synchronize = 0x00100000
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            LOG.warning("cannot watch the app process (%d)", ctypes.get_last_error())
            return

        def wait() -> None:
            kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)  # the handle pins this exact process
    else:
        def wait() -> None:
            while True:  # POSIX only: on Windows os.kill(pid, 0) would terminate the app
                time.sleep(1.0)
                try:
                    os.kill(pid, 0)
                except OSError:
                    return

    def run() -> None:
        wait()
        LOG.info("the app exited; exiting")
        logging.shutdown()
        os._exit(EXIT_PARENT_GONE)

    threading.Thread(target=run, name="parent-watch", daemon=True).start()


class IterationWatchdog:
    """Exits the process when one model update runs far longer than any normal update."""

    def __init__(self, limit_sec: float) -> None:
        self.limit_sec = limit_sec
        self.started_at: float | None = None
        self._lock = threading.Lock()
        if limit_sec > 0:
            threading.Thread(target=self._run, name="iteration-watchdog", daemon=True).start()

    def begin(self) -> None:
        with self._lock:
            self.started_at = time.monotonic()

    def end(self) -> None:
        with self._lock:
            self.started_at = None

    def _run(self) -> None:
        while True:
            time.sleep(0.5)
            with self._lock:
                started = self.started_at
            if started is not None and time.monotonic() - started > self.limit_sec:
                LOG.error("model update ran %.1fs (limit %.1fs); exiting as stalled",
                          time.monotonic() - started, self.limit_sec)
                logging.shutdown()
                os._exit(EXIT_STALLED)


# --------------------------------------------------------------------------- serving


def _clean_output(output: dict) -> dict:
    """Upstream's dict minus token ids; word timings stay (the app maps them to its clock)."""
    message: dict = {}
    for key in ("start", "end", "text", "is_final", "emission_time"):
        if key in output:
            message[key] = output[key]
    words = output.get("words")
    if isinstance(words, list):
        message["words"] = [
            {"start": word.get("start"), "end": word.get("end"), "text": word.get("text", "")}
            for word in words
            if isinstance(word, dict)
        ]
    return message


def _send_line(conn: socket.socket, payload: dict) -> None:
    conn.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")


def serve(conn: socket.socket, online, args, watchdog: IterationWatchdog) -> int:
    import numpy as np

    min_samples = max(1, int(round(args.vac_chunk_size * SAMPLE_RATE)))
    pending = bytearray()
    total_samples = 0
    iterations = 0
    last_iter_sec = 0.0
    max_iter_sec = 0.0
    began = time.time()
    last_heartbeat = 0.0
    conn.setblocking(True)
    online.init()
    _send_line(conn, {"type": "hello", "pid": os.getpid(), "model": Path(args.model_path).stem,
                      "frame_threshold": args.frame_threshold, "min_chunk_size": args.min_chunk_size})
    while True:
        readable, _, _ = select.select([conn], [], [], 0.1)
        if readable:
            try:
                data = conn.recv(1 << 16)
            except (ConnectionResetError, ConnectionAbortedError, OSError):
                data = b""
            if not data:
                LOG.info("client disconnected after %.1fs of audio", total_samples / SAMPLE_RATE)
                return EXIT_OK
            pending.extend(data)
        usable = len(pending) - (len(pending) % 2)  # a read may end inside a sample
        if usable // 2 >= min_samples:
            audio = np.frombuffer(bytes(pending[:usable]), dtype="<i2").astype(np.float32) / 32768.0
            del pending[:usable]
            watchdog.begin()
            started = time.perf_counter()
            online.insert_audio_chunk(audio)
            output = online.process_iter()
            last_iter_sec = time.perf_counter() - started
            watchdog.end()
            total_samples += audio.size
            iterations += 1
            max_iter_sec = max(max_iter_sec, last_iter_sec)
            if output:
                message = _clean_output(output)
                message["emission_time"] = time.time() - began
                _send_line(conn, message)
        now = time.monotonic()
        if now - last_heartbeat >= args.heartbeat_sec:
            last_heartbeat = now
            _send_line(conn, {"type": "heartbeat", "audio_sec": round(total_samples / SAMPLE_RATE, 3),
                              "iter_sec": round(last_iter_sec, 3), "max_iter_sec": round(max_iter_sec, 3),
                              "iterations": iterations, "guards": sum(GUARD_COUNTS.values()),
                              "unicode_fallbacks": UNICODE_FALLBACKS[0]})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="vendored SimulStreaming source folder")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 with --port-file: any free port")
    parser.add_argument("--port-file", default="", help="write the port actually listened on here")
    parser.add_argument("--heartbeat-sec", type=float, default=1.0)
    parser.add_argument("--stall-exit-sec", type=float, default=30.0)
    parser.add_argument("--accept-timeout-sec", type=float, default=120.0)
    parser.add_argument("--parent-pid", type=int, default=0, help="exit when this process (the app) ends")
    parser.add_argument("--allow-cpu", action="store_true", help="tests only; real-time needs CUDA")
    parser.add_argument("--silero-jit", default="", help="Silero VAD model file shipped with the runtime")
    parser.add_argument("--no-warmup", action="store_true")
    parser.add_argument("--self-check", action="store_true",
                        help="import torch, check CUDA, the model file and the VAC; print JSON and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    _strip_own_dir_from_path()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s\t%(name)s\t%(message)s", stream=sys.stderr)
    LOG.setLevel(logging.INFO)
    own, rest = build_parser().parse_known_args(argv)
    watch_parent(own.parent_pid)
    repo = Path(own.repo).resolve()
    if not (repo / "simulstreaming_whisper.py").is_file():
        LOG.error("SimulStreaming source not found: %s", repo.name)
        return EXIT_STARTUP
    sys.path.insert(0, str(repo))
    began = time.perf_counter()
    try:
        import numpy  # noqa: F401
        import torch
    except Exception as exc:
        LOG.error("runtime import failed: %s", _describe(exc))
        return EXIT_RUNTIME_MISSING
    cuda = bool(torch.cuda.is_available())
    if not cuda and not own.allow_cpu:
        LOG.error("CUDA is not available (torch %s)", torch.__version__)
        return EXIT_NO_CUDA
    try:
        from simulstreaming.whisper.whisper_streaming.whisper_online_main import asr_factory, processor_args
        from simulstreaming_whisper import simul_asr_factory, simulwhisper_args
    except Exception as exc:
        LOG.error("upstream import failed: %s", _describe(exc))
        return EXIT_RUNTIME_MISSING

    parser = build_parser()
    processor_args(parser)
    simulwhisper_args(parser)
    parser.set_defaults(log_level="WARNING")  # upstream's DEBUG default logs every decoded word
    args = parser.parse_args(argv)
    if not own.self_check and not (0 < own.port < 65536 or (own.port == 0 and own.port_file)):
        LOG.error("--port (or --port 0 with --port-file) is required")
        return EXIT_STARTUP
    # Upstream defaults to DEBUG, which logs every decoded word; keep its loggers quiet.
    for name in ("", "simulstreaming", "simul_whisper", "whisper_streaming", "simulstreaming_whisper"):
        logging.getLogger(name).setLevel(getattr(logging, str(args.log_level).upper(), logging.WARNING))
    LOG.setLevel(logging.INFO)
    model_path = Path(args.model_path)
    if not model_path.is_file():
        LOG.error("model file not found: %s", model_path.name)
        return EXIT_MODEL_MISSING
    if model_path.name not in {"large-v3.pt", "large-v3-turbo.pt"}:
        # SimulStreaming picks its alignment heads by this name; a renamed file aligns badly.
        LOG.error("model file must be named large-v3.pt or large-v3-turbo.pt, not %s", model_path.name)
        return EXIT_MODEL_MISSING
    try:
        patch_unicode_split()
        patch_hide_incomplete_unicode()
        patch_runaway_guard()
        patch_silero_loading(own.silero_jit)
        patch_model_download()
    except Exception as exc:
        LOG.error("upstream source is not the expected version: %s", _describe(exc))
        return EXIT_STARTUP

    if own.self_check:
        report = {"torch": torch.__version__, "cuda": cuda, "model": model_path.stem}
        if cuda:
            report["device"] = torch.cuda.get_device_name(0)
            report["total_mib"] = round(torch.cuda.get_device_properties(0).total_memory / 1048576)
        try:
            model, _ = torch.hub.load("snakers4/silero-vad", "silero_vad")
            report["vac"] = model is not None
        except Exception as exc:  # OfflineFileMissing: no local Silero copy
            report["vac"] = False
            report["vac_error"] = _describe(exc)
        print(json.dumps(report), flush=True)
        return EXIT_OK if report["vac"] else EXIT_RUNTIME_MISSING

    loaded = time.perf_counter()
    LOG.info("runtime imported in %.1fs", loaded - began)
    try:
        asr, online = asr_factory(args, simul_asr_factory)
        LOG.info("model and VAC loaded in %.1fs", time.perf_counter() - loaded)
        if not own.no_warmup:
            # One second of quiet noise compiles the CUDA kernels so the first real update is fast.
            import numpy as np

            noise = np.random.default_rng(0).standard_normal(SAMPLE_RATE).astype(np.float32) * 0.01
            asr.warmup(noise)
    except OfflineFileMissing as exc:
        LOG.error("%s", exc)
        return EXIT_RUNTIME_MISSING if "Silero" in str(exc) else EXIT_MODEL_MISSING
    except Exception as exc:
        if is_out_of_memory(exc):
            LOG.error("out of GPU memory while loading %s", model_path.stem)
            return EXIT_OUT_OF_MEMORY
        LOG.error("model load failed: %s", _describe(exc))
        return EXIT_STARTUP
    if cuda:
        LOG.info("model %s ready in %.1fs (reserved %.0f MiB)", model_path.stem, time.perf_counter() - loaded,
                 torch.cuda.memory_reserved() / 1048576)
    else:
        LOG.info("model %s ready in %.1fs (CPU)", model_path.stem, time.perf_counter() - loaded)

    watchdog = IterationWatchdog(own.stall_exit_sec)
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind((own.host, own.port))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        if own.port_file:
            # Bound before it is announced: no other program can take the port in between,
            # which a port picked by the app and bound after a 20 s model load allowed.
            temporary = Path(own.port_file + ".tmp")
            temporary.write_text(str(port), encoding="ascii")
            os.replace(temporary, own.port_file)
    except OSError as exc:
        LOG.error("cannot listen on %s:%s: %s", own.host, own.port, exc)
        return EXIT_STARTUP
    LOG.info("listening on %s:%s", own.host, port)
    listener.settimeout(own.accept_timeout_sec if own.accept_timeout_sec > 0 else None)
    try:
        conn, _ = listener.accept()
    except socket.timeout:
        LOG.error("no client connected within %.0fs", own.accept_timeout_sec)
        return EXIT_STARTUP
    finally:
        listener.close()
    try:
        with conn:
            return serve(conn, online, args, watchdog)
    except Exception as exc:
        watchdog.end()
        if is_out_of_memory(exc):
            LOG.error("out of GPU memory while streaming")
            return EXIT_OUT_OF_MEMORY
        LOG.error("streaming failed: %s", _describe(exc))
        return EXIT_CRASHED
    finally:
        if GUARD_COUNTS or UNICODE_FALLBACKS[0]:
            LOG.info("guards=%s unicode_fallbacks=%d", GUARD_COUNTS, UNICODE_FALLBACKS[0])


if __name__ == "__main__":
    code = main()
    logging.shutdown()
    # os._exit: the parent-watch daemon thread must not keep the interpreter alive.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
