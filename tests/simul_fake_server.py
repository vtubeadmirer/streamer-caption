"""A stand-in for engines/simulstreaming/server.py in tests: same protocol, no torch, no GPU.

Every --word-sec seconds of received audio whose level is above --voice-rms it sends the
next scripted word as an append-only increment; a quieter stretch of --final-sec closes the
segment with is_final (like the VAC). A heartbeat line goes out every --heartbeat-sec.
--mode makes it fail the ways the real server can: exit codes match server.py.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import socket
import sys
import threading
import time

WORDS = ["오늘은", "날씨가", "정말", "좋네요.", "그래서", "산책을", "나가볼까", "합니다."]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--mode", default="normal",
                        choices=["normal", "crash_after", "stall_after", "oom_after", "slow",
                                 "oom_start", "runtime_missing", "no_cuda", "hang_start", "close_after"])
    parser.add_argument("--after-sec", type=float, default=3.0)
    parser.add_argument("--load-sec", type=float, default=0.2)
    parser.add_argument("--word-sec", type=float, default=0.5)
    parser.add_argument("--final-sec", type=float, default=0.6)
    parser.add_argument("--voice-rms", type=float, default=0.02)
    parser.add_argument("--heartbeat-sec", type=float, default=0.25)
    parser.add_argument("--watch-stdin", action="store_true")
    args, _ = parser.parse_known_args()

    if args.watch_stdin:
        def watch() -> None:
            try:
                while sys.stdin.buffer.read(4096):
                    pass
            except Exception:
                pass
            os._exit(10)
        threading.Thread(target=watch, daemon=True).start()

    if args.mode == "oom_start":
        return 7
    if args.mode == "runtime_missing":
        return 6
    if args.mode == "no_cuda":
        return 4
    time.sleep(args.load_sec)
    if args.mode == "hang_start":
        time.sleep(3600)
        return 0

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", args.port))
    listener.listen(1)
    listener.settimeout(30)
    conn, _ = listener.accept()
    listener.close()
    send = lambda payload: conn.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")  # noqa: E731
    send({"type": "hello", "pid": os.getpid(), "model": "fake"})
    pending = bytearray()
    total = 0
    voiced_since_word = 0
    quiet = 0
    word_index = 0
    in_segment = False
    last_heartbeat = 0.0
    began = time.time()
    while True:
        readable, _, _ = select.select([conn], [], [], 0.05)
        if readable:
            data = conn.recv(65536)
            if not data:
                return 0
            pending.extend(data)
        usable = len(pending) - len(pending) % 2
        if usable:
            chunk = bytes(pending[:usable])
            del pending[:usable]
            samples = [int.from_bytes(chunk[i:i + 2], "little", signed=True) / 32768.0 for i in range(0, len(chunk), 2)]
            rms = (sum(value * value for value in samples) / len(samples)) ** 0.5
            total += len(samples)
            audio_sec = total / 16000
            if args.mode in {"crash_after", "stall_after", "oom_after", "close_after"} and audio_sec >= args.after_sec:
                if args.mode == "crash_after":
                    os._exit(9)
                if args.mode == "oom_after":
                    os._exit(7)
                if args.mode == "close_after":
                    conn.close()
                    time.sleep(3600)
                time.sleep(3600)  # stall: no heartbeat, no reads
            if rms >= args.voice_rms:
                in_segment = True
                quiet = 0
                voiced_since_word += len(samples)
                if voiced_since_word >= args.word_sec * 16000:
                    voiced_since_word = 0
                    word = WORDS[word_index % len(WORDS)]
                    word_index += 1
                    text = " " + word
                    send({"start": audio_sec - 0.3, "end": audio_sec - 0.1, "text": text, "is_final": False,
                          "emission_time": time.time() - began,
                          "words": [{"start": audio_sec - 0.3, "end": audio_sec - 0.1, "text": text}]})
            elif in_segment:
                quiet += len(samples)
                if quiet >= args.final_sec * 16000:
                    in_segment = False
                    voiced_since_word = 0
                    send({"is_final": True, "emission_time": time.time() - began, "end": audio_sec})
        now = time.monotonic()
        if now - last_heartbeat >= args.heartbeat_sec:
            last_heartbeat = now
            reported = total / 16000
            if args.mode == "slow":
                reported = reported / 4
            send({"type": "heartbeat", "audio_sec": round(reported, 3), "iter_sec": 0.01})


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
