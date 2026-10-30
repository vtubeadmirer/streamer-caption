"""내 화면 자막: a click-through, always-on-top caption strip drawn straight on the desktop.

For streamers without OBS. The app runs this as its own process (DesktopOverlayKeeper) while
desktop_overlay.enabled is on. It reads captions from the caption WebSocket and its settings from
the admin API, so a change on the admin page shows within a second. Windows only; it needs
nothing beyond ctypes and numpy, which the portable runtime already ships.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CREATE_NO_WINDOW = 0x08000000
# How long the overlay keeps running without an answer from the admin API (the app was closed).
ADMIN_GONE_SEC = 6.0
PENDING_RGB = (220, 226, 235)
PENDING_ALPHA = 0.8


class DesktopOverlayKeeper:
    """Keeps the overlay process running exactly while desktop_overlay.enabled is on."""

    def __init__(
        self,
        load_config: Callable[[], dict[str, Any]],
        admin_url: str,
        log_path: Path | None = None,
        interval_sec: float = 2.0,
        command: list[str] | None = None,
    ) -> None:
        self._load_config = load_config
        self._command = command or [sys.executable, "-m", "core.desktop_overlay", "--admin", admin_url]
        self._log_path = log_path
        self._interval = interval_sec
        self._process: subprocess.Popen[bytes] | None = None
        self._started_at = 0.0
        self._quick_exits = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="desktop-overlay", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._terminate()

    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def sync(self) -> None:
        enabled = bool(self._load_config().get("desktop_overlay", {}).get("enabled", False))
        if not enabled:
            self._quick_exits = 0
            self._terminate()
            return
        if self.running():
            return
        if self._process is not None:  # it exited on its own
            self._quick_exits = self._quick_exits + 1 if time.monotonic() - self._started_at < 10 else 0
            self._process = None
        if self._quick_exits >= 3:
            self.last_error = "desktop overlay exited three times right after starting; not restarting"
            return
        stderr: Any = subprocess.DEVNULL
        if self._log_path is not None:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            stderr = self._log_path.open("ab")
        try:
            self._process = subprocess.Popen(
                self._command,
                cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        finally:
            if stderr is not subprocess.DEVNULL:
                stderr.close()
        self._started_at = time.monotonic()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync()
            except Exception as exc:  # a broken overlay must never touch the captions
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(self._interval)

    def _terminate(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


# --------------------------------------------------------------------------- caption state


@dataclass(frozen=True)
class OverlayLine:
    text: str
    agreed: int  # leading characters drawn in the caption colour (the rest of a preview is gray)
    kind: str  # "current", "previous" or "pending"


@dataclass
class OverlaySettings:
    font_size: float = 34
    max_lines: int = 2
    position: str = "bottom"
    margin_percent: float = 6
    width_percent: float = 80
    opacity: float = 1.0
    background: float = 0.35
    prev_opacity: float = 0.55
    font: str = "Malgun Gothic"
    hold_sec: float = 6.0
    stale_sec: float = 3.0
    # 저스트 채팅 모드: lines stay and scroll up one at a time instead of clearing after hold_sec.
    just_chatting: bool = False
    ws_port: int = 8765
    labels: dict[str, str] | None = None  # shown only when both sources are captioned
    enabled: bool = True
    # The box the streamer dragged and sized, in screen pixels; width 0 = not placed yet.
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    adjusting: bool = False

    @property
    def rect(self) -> tuple[int, int, int, int] | None:
        return (self.x, self.y, self.width, self.height) if self.width > 0 and self.height > 0 else None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "OverlaySettings":
        overlay = config.get("desktop_overlay", {})
        caption = config.get("caption", {})
        streaming = config.get("streaming", {})
        both = config.get("audio", {}).get("source") == "both" and caption.get("show_source_labels", True)
        return cls(
            font_size=float(overlay.get("font_size", 34)),
            max_lines=int(overlay.get("max_lines", 2)),
            position=str(overlay.get("position", "bottom")),
            margin_percent=float(overlay.get("margin_percent", 6)),
            width_percent=float(overlay.get("width_percent", 80)),
            opacity=float(overlay.get("opacity", 1.0)),
            background=float(overlay.get("background", 0.35)),
            prev_opacity=float(caption.get("prev_opacity", 0.55)),
            font=str(caption.get("font") or "Malgun Gothic"),
            hold_sec=float(streaming.get("confirmed_hold_sec", 6.0)),
            stale_sec=float(streaming.get("pending_stale_sec", 3.0)),
            just_chatting=bool(caption.get("just_chatting_mode", False)),
            ws_port=int(config.get("server", {}).get("port", 8765)),
            labels={
                "microphone": str(caption.get("microphone_label") or "방송자"),
                "system": str(caption.get("system_label") or "컴퓨터"),
            } if both else None,
            enabled=bool(overlay.get("enabled", False)),
            x=int(overlay.get("x", 0)),
            y=int(overlay.get("y", 0)),
            width=int(overlay.get("width", 0)),
            height=int(overlay.get("height", 0)),
            adjusting=bool(overlay.get("adjusting", False)),
        )


def _source(message: dict[str, Any]) -> str:
    return "system" if str((message.get("metadata") or {}).get("audio_source", "")).lower() == "system" else "microphone"


class CaptionState:
    """The same rules as overlay/caption.html."""

    def __init__(self) -> None:
        self.confirmed: list[str] = []
        self.last_confirmed_at = 0.0
        self.pending = ""
        self.pending_agreed = 0
        self.pending_stable = False
        self.pending_source = ""
        self.pending_offset = -1.0
        self.pending_at = 0.0
        self.last_confirmed_offset = {"microphone": -1.0, "system": -1.0}
        self.last_pending_offset = {"microphone": -1.0, "system": -1.0}
        self._seen: list[str] = []

    def apply(self, message: dict[str, Any], now: float, settings: OverlaySettings) -> None:
        kind = message.get("type")
        if kind in {"caption_confirmed", "caption"}:
            self._confirmed(message, now, settings)
        elif kind == "caption_pending":
            self._pending(message, now, settings)
        elif kind == "caption_hidden":
            source = _source(message)
            offset = float(message.get("audio_offset_sec") or 0.0)
            if self.pending_source == source and offset >= self.last_pending_offset[source]:
                self._clear_pending()
        # "sync" replays captions from before the overlay started; old lines would only flash.

    def lines(self, now: float, settings: OverlaySettings) -> list[OverlayLine]:
        if self.confirmed and not settings.just_chatting and now - self.last_confirmed_at > settings.hold_sec:
            self.confirmed.clear()
            if not self.pending_stable:
                self._clear_pending()
        if self.pending:
            stale = max(settings.stale_sec, settings.hold_sec) if self.pending_stable else settings.stale_sec
            if now - self.pending_at > stale:
                self._clear_pending()
        shown = self.confirmed[-max(1, settings.max_lines):]
        out = [
            OverlayLine(text, len(text), "current" if index == len(shown) - 1 else "previous")
            for index, text in enumerate(shown)
        ]
        if self.pending:
            agreed = len(self.pending) if self.pending_stable else min(self.pending_agreed, len(self.pending))
            out.append(OverlayLine(self.pending, agreed, "pending"))
        return out

    def _label(self, message: dict[str, Any], settings: OverlaySettings) -> str:
        if not settings.labels:
            return ""
        source = _source(message)
        label = (message.get("metadata") or {}).get("source_label") or settings.labels.get(source, source)
        return f"[{label}] "

    def _confirmed(self, message: dict[str, Any], now: float, settings: OverlaySettings) -> None:
        text = str(message.get("text") or "").strip()
        if not text:
            return
        source = _source(message)
        raw_offset = message.get("audio_offset_sec")
        offset = float(raw_offset) if raw_offset is not None else self.last_confirmed_offset[source]
        if offset < self.last_confirmed_offset[source]:
            return
        split_index = (message.get("metadata") or {}).get("split_index", "")
        key = f"{source}:{offset:.3f}:{split_index}:{text}"
        if key in self._seen:
            return
        self._seen = (self._seen + [key])[-200:]
        self.last_confirmed_offset[source] = offset
        if self.pending_source == source and (not self.pending_stable or offset >= self.pending_offset):
            self._clear_pending()
        self.confirmed = (self.confirmed + [self._label(message, settings) + text])[-50:]
        self.last_confirmed_at = now

    def _pending(self, message: dict[str, Any], now: float, settings: OverlaySettings) -> None:
        raw = str(message.get("text") or "")
        text = raw.strip()
        source = _source(message)
        offset = float(message.get("audio_offset_sec") or 0.0)
        if not text or offset < self.last_pending_offset[source] or offset <= self.last_confirmed_offset[source]:
            return
        metadata = message.get("metadata") or {}
        label = self._label(message, settings)
        leading = len(raw) - len(raw.lstrip())
        self.last_pending_offset[source] = offset
        self.pending = label + text
        self.pending_agreed = len(label) + max(0, int(metadata.get("stable_chars") or 0) - leading)
        self.pending_stable = bool(metadata.get("stable"))
        self.pending_source = source
        self.pending_offset = offset
        self.pending_at = now

    def _clear_pending(self) -> None:
        self.pending = ""
        self.pending_agreed = 0
        self.pending_stable = False
        self.pending_source = ""


def wrap_spans(text: str, measure: Callable[[str], int], max_width: int) -> list[tuple[int, int]]:
    """Greedy word wrap; returns (start, end) indices into text. Spaces at a break are dropped."""
    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        while start < len(text) and text[start] == " ":
            start += 1
        if start >= len(text):
            break
        end = start
        best = -1
        while end < len(text):
            nxt = text.find(" ", end + 1)
            nxt = len(text) if nxt == -1 else nxt
            if measure(text[start:nxt]) > max_width:
                break
            best = end = nxt
        if best == -1:  # one word wider than the strip: break it by characters
            end = start + 1
            while end < len(text) and measure(text[start:end + 1]) <= max_width:
                end += 1
            best = end
        spans.append((start, best))
        start = best
    return spans


# --------------------------------------------------------------------------- Win32 drawing


@functools.lru_cache(maxsize=None)
def _win32() -> Any:
    """One set of declarations: argtypes name these exact Structure classes."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    H = wintypes.HANDLE

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
            ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
        ]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_byte)]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int), ("hInstance", H), ("hIcon", H), ("hCursor", H),
            ("hbrBackground", H), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
        ]

    signatures = {
        (user32, "GetDC"): (H, [wintypes.HWND]),
        (user32, "ReleaseDC"): (ctypes.c_int, [wintypes.HWND, H]),
        (user32, "RegisterClassW"): (wintypes.ATOM, [ctypes.POINTER(WNDCLASSW)]),
        (user32, "CreateWindowExW"): (wintypes.HWND, [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.HWND, H, H, ctypes.c_void_p]),
        (user32, "DestroyWindow"): (wintypes.BOOL, [wintypes.HWND]),
        (user32, "DefWindowProcW"): (ctypes.c_ssize_t, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
        (user32, "GetWindowRect"): (wintypes.BOOL, [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]),
        (user32, "SetWindowLongPtrW"): (ctypes.c_ssize_t, [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]),
        (user32, "ShowWindow"): (wintypes.BOOL, [wintypes.HWND, ctypes.c_int]),
        (user32, "SetWindowPos"): (wintypes.BOOL, [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]),
        (user32, "UpdateLayeredWindow"): (wintypes.BOOL, [
            wintypes.HWND, H, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE), H,
            ctypes.POINTER(wintypes.POINT), wintypes.DWORD, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]),
        (user32, "SystemParametersInfoW"): (wintypes.BOOL, [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]),
        (user32, "PeekMessageW"): (wintypes.BOOL, [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]),
        (user32, "TranslateMessage"): (wintypes.BOOL, [ctypes.POINTER(wintypes.MSG)]),
        (user32, "DispatchMessageW"): (ctypes.c_ssize_t, [ctypes.POINTER(wintypes.MSG)]),
        (gdi32, "CreateCompatibleDC"): (H, [H]),
        (gdi32, "DeleteDC"): (wintypes.BOOL, [H]),
        (gdi32, "SelectObject"): (H, [H, H]),
        (gdi32, "DeleteObject"): (wintypes.BOOL, [H]),
        (gdi32, "CreateFontW"): (H, [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]),
        (gdi32, "CreateDIBSection"): (H, [
            H, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), H, wintypes.DWORD]),
        (gdi32, "SetBkMode"): (ctypes.c_int, [H, ctypes.c_int]),
        (gdi32, "SetTextColor"): (wintypes.DWORD, [H, wintypes.DWORD]),
        (gdi32, "TextOutW"): (wintypes.BOOL, [H, ctypes.c_int, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int]),
        (gdi32, "GetTextExtentPoint32W"): (wintypes.BOOL, [H, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.SIZE)]),
        (kernel32, "GetModuleHandleW"): (H, [wintypes.LPCWSTR]),
    }
    for (dll, name), (restype, argtypes) in signatures.items():
        function = getattr(dll, name)
        function.restype = restype
        function.argtypes = argtypes
    return ctypes, wintypes, user32, gdi32, kernel32, BITMAPINFOHEADER, BLENDFUNCTION, WNDCLASSW


def _set_dpi_aware() -> float:
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except (AttributeError, OSError):
        return 1.0


class TextRenderer:
    """Draws caption lines into a premultiplied BGRA image: white text, dark outline, optional box."""

    def __init__(self, scale: float = 1.0) -> None:
        (self.ctypes, self.wintypes, self.user32, self.gdi32, _kernel32,
         self.BITMAPINFOHEADER, _blend, _wndclass) = _win32()
        self.scale = scale
        self.dc = self.gdi32.CreateCompatibleDC(None)
        self.gdi32.SetBkMode(self.dc, 1)  # TRANSPARENT
        self._font = None
        self._font_key: tuple[str, int] | None = None

    def close(self) -> None:
        if self._font:
            self.gdi32.DeleteObject(self._font)
        self.gdi32.DeleteDC(self.dc)

    def _use_font(self, name: str, px: int) -> None:
        if self._font_key == (name, px):
            return
        font = self.gdi32.CreateFontW(-px, 0, 0, 0, 800, 0, 0, 0, 1, 0, 0, 4, 0, name)  # 4 = grayscale AA
        self.gdi32.SelectObject(self.dc, font)
        if self._font:
            self.gdi32.DeleteObject(self._font)
        self._font, self._font_key = font, (name, px)

    def measure(self, text: str) -> int:
        size = self.wintypes.SIZE()
        self.gdi32.GetTextExtentPoint32W(self.dc, text, len(text), self.ctypes.byref(size))
        return size.cx

    def render(self, lines: list[OverlayLine], settings: OverlaySettings, max_width: int) -> Any:
        import numpy as np

        px = max(8, round(settings.font_size * self.scale))
        self._use_font(settings.font, px)
        outline = max(2, round(px / 14))
        pad = outline + (round(px * 0.4) if settings.background > 0 else 0)
        line_h = round(px * 1.3)
        rows: list[tuple[str, int, str]] = []
        for line in lines:
            for start, end in wrap_spans(line.text, self.measure, max(px, max_width - 2 * pad)):
                rows.append((line.text[start:end], min(max(line.agreed - start, 0), end - start), line.kind))
        rows = rows[-(max(1, settings.max_lines) + 1) * 2:]
        widths = [self.measure(text) for text, _agreed, _kind in rows]
        width = max(widths) + 2 * pad
        height = len(rows) * line_h + 2 * pad
        header = self.BITMAPINFOHEADER(
            biSize=self.ctypes.sizeof(self.BITMAPINFOHEADER), biWidth=width, biHeight=-height,
            biPlanes=1, biBitCount=32, biCompression=0,
        )
        bits = self.ctypes.c_void_p()
        bitmap = self.gdi32.CreateDIBSection(self.dc, self.ctypes.byref(header), 0, self.ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            raise OSError("CreateDIBSection failed")
        previous = self.gdi32.SelectObject(self.dc, bitmap)
        try:
            pixels = np.ctypeslib.as_array(
                (self.ctypes.c_uint8 * (width * height * 4)).from_address(bits.value)
            ).reshape(height, width, 4)
            pixels[:] = 0
            # One pass, three masks: GDI writes each class into its own colour channel (BGR order
            # in memory) and grayscale anti-aliasing scales each channel by glyph coverage.
            colours = {"current": 0x0000FF, "previous": 0x00FF00, "pending": 0xFF0000}
            for index, ((text, agreed, kind), row_width) in enumerate(zip(rows, widths)):
                x = (width - row_width) // 2
                y = pad + index * line_h + (line_h - px) // 2
                head, tail = text[:agreed], text[agreed:]
                if kind == "pending":
                    if head:
                        self.gdi32.SetTextColor(self.dc, colours["current"])
                        self.gdi32.TextOutW(self.dc, x, y, head, len(head))
                    if tail:
                        self.gdi32.SetTextColor(self.dc, colours["pending"])
                        self.gdi32.TextOutW(self.dc, x + self.measure(head), y, tail, len(tail))
                else:
                    self.gdi32.SetTextColor(self.dc, colours[kind])
                    self.gdi32.TextOutW(self.dc, x, y, text, len(text))
            image = compose(pixels.astype(np.float32) / 255.0, outline, settings)
            return image, width, height
        finally:
            self.gdi32.SelectObject(self.dc, previous)
            self.gdi32.DeleteObject(bitmap)


def compose(masks: Any, outline: int, settings: OverlaySettings) -> Any:
    """masks[..., 2/1/0] = current/previous/pending glyph coverage -> premultiplied BGRA uint8."""
    import numpy as np

    current = masks[..., 2]
    previous = masks[..., 1] * settings.prev_opacity
    pending = masks[..., 0] * PENDING_ALPHA
    coverage = np.maximum(np.maximum(masks[..., 2], masks[..., 1]), masks[..., 0])
    height, width = coverage.shape
    padded = np.pad(coverage, outline)
    ring = np.zeros_like(coverage)
    for dy in range(-outline, outline + 1):
        for dx in range(-outline, outline + 1):
            if dx * dx + dy * dy <= outline * outline + outline:
                np.maximum(ring, padded[outline + dy:outline + dy + height, outline + dx:outline + dx + width], out=ring)
    text_alpha = np.clip(current + previous + pending, 0.0, 1.0)
    under = ring * 0.92
    under = under + (1.0 - under) * settings.background
    alpha = text_alpha + (1.0 - text_alpha) * under
    out = np.empty((height, width, 4), dtype=np.uint8)
    white = current + previous
    for channel, value in zip((0, 1, 2), (PENDING_RGB[2], PENDING_RGB[1], PENDING_RGB[0])):
        out[..., channel] = np.clip((white + pending * value / 255.0) * 255.0, 0, 255).astype(np.uint8)
    out[..., 3] = np.clip(alpha * 255.0, 0, 255).astype(np.uint8)
    np.minimum(out[..., :3], out[..., 3:4], out=out[..., :3])  # premultiplied: colour never above alpha
    return out


ACCENT_BGR = (0x97, 0xC9, 0x20)  # #20c997, the admin page accent
GUIDE_TEXT = "끌어서 옮기기 · 가장자리를 끌어 크기 조절 · 더블클릭하면 고정"
SAMPLE_LINES = (
    OverlayLine("자막이 이 상자 안에 표시됩니다", 16, "previous"),
    OverlayLine("상자를 원하는 곳에 놓고 크기를 맞춰 주세요", 23, "current"),
)
MIN_BOX = (200, 60)
EDGE = 10  # pixels from the border that resize instead of move
HT = {"move": 2, "left": 10, "right": 11, "top": 12, "topleft": 13, "topright": 14,
      "bottom": 15, "bottomleft": 16, "bottomright": 17}


def hit_test(px: int, py: int, rect: tuple[int, int, int, int], edge: int = EDGE) -> int:
    """WM_NCHITTEST answer while adjusting: borders resize, the rest drags the box."""
    left, top, right, bottom = rect
    vertical = "top" if py < top + edge else "bottom" if py >= bottom - edge else ""
    horizontal = "left" if px < left + edge else "right" if px >= right - edge else ""
    return HT[(vertical + horizontal) or "move"]


def over(dst: Any, src: Any) -> Any:
    """Premultiplied BGRA src drawn over dst (same shape)."""
    import numpy as np

    alpha = src[..., 3:4].astype(np.float32) / 255.0
    return np.clip(src.astype(np.float32) + dst.astype(np.float32) * (1.0 - alpha), 0, 255).astype(np.uint8)


def place(image: Any, width: int, height: int, top: bool = False) -> Any:
    """Caption image centred in a width x height canvas, against its bottom (or top) edge.
    Rows that do not fit leave from the far edge, so the newest line always shows."""
    import numpy as np

    canvas = np.zeros((height, width, 4), dtype=np.uint8)
    if image is None:
        return canvas
    rows, cols = image.shape[:2]
    if rows > height:
        image = image[:height] if top else image[rows - height:]
        rows = height
    if cols > width:
        cut = (cols - width) // 2
        image = image[:, cut:cut + width]
        cols = width
    x = (width - cols) // 2
    y = 0 if top else height - rows
    canvas[y:y + rows, x:x + cols] = image
    return canvas


def adjust_frame(width: int, height: int, guide: Any) -> Any:
    """The box shown while placing: dim fill, accent border, how-to line at the top."""
    import numpy as np

    frame = np.zeros((height, width, 4), dtype=np.uint8)
    frame[..., 3] = 115
    border = 3
    for part in (frame[:border], frame[-border:], frame[:, :border], frame[:, -border:]):
        part[..., 0], part[..., 1], part[..., 2], part[..., 3] = (*ACCENT_BGR, 255)
    if guide is not None:
        gh, gw = guide.shape[:2]
        gh, gw = min(gh, height - 2 * border), min(gw, width - 2 * border)
        if gh > 0 and gw > 0:
            x, y = (width - gw) // 2, border + 2
            frame[y:y + gh, x:x + gw] = over(frame[y:y + gh, x:x + gw], guide[:gh, :gw])
    return frame


class OverlayWindow:
    WS_EX_PLACED = 0x00080000 | 0x00000008 | 0x00000080 | 0x08000000  # layered, topmost, tool window
    # (no taskbar button), never takes focus
    WS_EX_TRANSPARENT = 0x00000020  # clicks go to the window behind; off only while adjusting

    def __init__(self, on_moved: Callable[[tuple[int, int, int, int]], None], on_done: Callable[[], None]) -> None:
        scale = _set_dpi_aware()
        (self.ctypes, self.wintypes, self.user32, self.gdi32, kernel32,
         _bmi, self.BLENDFUNCTION, WNDCLASSW) = _win32()
        ctypes, wintypes = self.ctypes, self.wintypes
        self.renderer = TextRenderer(scale)
        self.scale = scale
        self.on_moved, self.on_done = on_moved, on_done
        self.adjusting = False
        self.rect: tuple[int, int, int, int] | None = None  # x, y, width, height of the placed box
        self.visible = False
        self._last: tuple[list[OverlayLine], OverlaySettings] | None = None
        self.hwnd = None
        wndproc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        self._wndproc = wndproc_type(self._handle)  # kept alive for as long as the window
        instance = kernel32.GetModuleHandleW(None)
        self._class = WNDCLASSW(
            style=0x0008,  # CS_DBLCLKS
            lpfnWndProc=ctypes.cast(self._wndproc, ctypes.c_void_p).value,
            hInstance=instance,
            lpszClassName="StreamerCaptionDesktop",
        )
        self.user32.RegisterClassW(ctypes.byref(self._class))
        self.hwnd = self.user32.CreateWindowExW(
            self.WS_EX_PLACED | self.WS_EX_TRANSPARENT, "StreamerCaptionDesktop", "Streamer Caption",
            0x80000000, 0, 0, 1, 1, None, None, instance, None,
        )
        if not self.hwnd:
            raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()})")

    # -- window messages

    def _handle(self, hwnd: Any, msg: int, wparam: int, lparam: int) -> int:
        try:
            if self.adjusting and hwnd == self.hwnd:
                if msg == 0x0084:  # WM_NCHITTEST
                    px = self.ctypes.c_short(lparam & 0xFFFF).value
                    py = self.ctypes.c_short((lparam >> 16) & 0xFFFF).value
                    return hit_test(px, py, self._window_rect_ltrb())
                if msg == 0x0024:  # WM_GETMINMAXINFO: ptMinTrackSize lives at offset 24
                    info = self.ctypes.cast(lparam, self.ctypes.POINTER(self.ctypes.c_long * 10)).contents
                    info[6], info[7] = (round(MIN_BOX[0] * self.scale), round(MIN_BOX[1] * self.scale))
                    return 0
                if msg == 0x0005:  # WM_SIZE: the box changed size under the mouse; redraw it now
                    self.rect = self._window_rect()
                    self._redraw()
                elif msg == 0x0232:  # WM_EXITSIZEMOVE
                    self.rect = self._window_rect()
                    self.on_moved(self.rect)
                elif msg == 0x00A3:  # WM_NCLBUTTONDBLCLK
                    self.rect = self._window_rect()
                    self.on_moved(self.rect)
                    self.on_done()
                    return 0
        except Exception:  # an exception must never escape into Windows' message loop
            pass
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _window_rect_ltrb(self) -> tuple[int, int, int, int]:
        rect = self.wintypes.RECT()
        self.user32.GetWindowRect(self.hwnd, self.ctypes.byref(rect))
        return rect.left, rect.top, rect.right, rect.bottom

    def _window_rect(self) -> tuple[int, int, int, int]:
        left, top, right, bottom = self._window_rect_ltrb()
        return left, top, right - left, bottom - top

    # -- drawing

    def work_area(self) -> tuple[int, int, int, int]:
        rect = self.wintypes.RECT()
        self.user32.SystemParametersInfoW(0x0030, 0, self.ctypes.byref(rect), 0)  # SPI_GETWORKAREA
        return rect.left, rect.top, rect.right, rect.bottom

    def default_rect(self, settings: OverlaySettings) -> tuple[int, int, int, int]:
        left, top, right, bottom = self.work_area()
        width = int((right - left) * 0.6)
        height = round(settings.font_size * self.scale * 1.3 * (settings.max_lines + 1) + settings.font_size * self.scale)
        return left + (right - left - width) // 2, bottom - height - int((bottom - top) * 0.06), width, height

    def set_mode(self, settings: OverlaySettings) -> None:
        # The saved box is read once; after that the box on screen is the truth, because a poll
        # can still carry the old position while the new one is being saved.
        if self.rect is None:
            self.rect = settings.rect
        if settings.adjusting and self.rect is None:
            self.rect = self.default_rect(settings)
        if settings.adjusting != self.adjusting:
            self.adjusting = settings.adjusting
            style = self.WS_EX_PLACED | (0 if self.adjusting else self.WS_EX_TRANSPARENT)
            self.user32.SetWindowLongPtrW(self.hwnd, -20, style)  # GWL_EXSTYLE

    def show(self, lines: list[OverlayLine], settings: OverlaySettings) -> None:
        self._last = (lines, settings)
        self.set_mode(settings)
        self._redraw()

    def _redraw(self) -> None:
        if self._last is None:
            return
        lines, settings = self._last
        if self.adjusting:
            lines = lines or list(SAMPLE_LINES)
        if not lines:
            self._hide()
            return
        if self.rect is None:  # never placed: a strip just above the taskbar, sized to the text
            left, top, right, bottom = self.work_area()
            image, width, height = self.renderer.render(lines, settings, int((right - left) * settings.width_percent / 100))
            margin = int((bottom - top) * settings.margin_percent / 100)
            x = left + (right - left - width) // 2
            y = top + margin if settings.position == "top" else bottom - margin - height
        else:
            x, y, width, height = self.rect
            width, height = max(width, MIN_BOX[0]), max(height, MIN_BOX[1])
            caption, _w, _h = self.renderer.render(lines, settings, width)
            image = place(caption, width, height, top=settings.position == "top" and not self.adjusting)
            if self.adjusting:
                guide_settings = replace(settings, font_size=max(12.0, settings.font_size * 0.42), background=0.0)
                guide, _gw, _gh = self.renderer.render([OverlayLine(GUIDE_TEXT, len(GUIDE_TEXT), "current")],
                                                       guide_settings, width - 8)
                image = over(adjust_frame(width, height, guide), image)
        self._blit(image, x, y, width, height, 1.0 if self.adjusting else settings.opacity)
        if not self.visible:
            self.user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
            self.visible = True

    def _hide(self) -> None:
        if self.visible:
            self.user32.ShowWindow(self.hwnd, 0)
            self.visible = False

    def _blit(self, image: Any, x: int, y: int, width: int, height: int, opacity: float) -> None:
        import numpy as np

        ctypes, wintypes, gdi32 = self.ctypes, self.wintypes, self.gdi32
        image = np.ascontiguousarray(image)
        screen = self.user32.GetDC(None)
        dc = gdi32.CreateCompatibleDC(screen)
        header = self.renderer.BITMAPINFOHEADER(
            biSize=ctypes.sizeof(self.renderer.BITMAPINFOHEADER), biWidth=width, biHeight=-height,
            biPlanes=1, biBitCount=32, biCompression=0,
        )
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        previous = gdi32.SelectObject(dc, bitmap)
        try:
            ctypes.memmove(bits.value, image.ctypes.data, width * height * 4)
            blend = self.BLENDFUNCTION(0, 0, max(0, min(255, round(opacity * 255))), 1)  # AC_SRC_ALPHA
            self.user32.UpdateLayeredWindow(
                self.hwnd, screen, ctypes.byref(wintypes.POINT(x, y)), ctypes.byref(wintypes.SIZE(width, height)),
                dc, ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), 2,  # ULW_ALPHA
            )
        finally:
            gdi32.SelectObject(dc, previous)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(dc)
            self.user32.ReleaseDC(None, screen)

    def keep_on_top(self) -> None:
        # Games and full-screen video can push other windows above; claim the top again.
        self.user32.SetWindowPos(self.hwnd, self.wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

    def pump(self) -> None:
        msg = self.wintypes.MSG()
        while self.user32.PeekMessageW(self.ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
            self.user32.TranslateMessage(self.ctypes.byref(msg))
            self.user32.DispatchMessageW(self.ctypes.byref(msg))

    def close(self) -> None:
        self.user32.DestroyWindow(self.hwnd)
        self.renderer.close()


# --------------------------------------------------------------------------- process


def _poll_settings(admin_url: str, holder: dict[str, Any], stop: threading.Event) -> None:
    last_ok = time.monotonic()
    while not stop.is_set():
        try:
            with urllib.request.urlopen(f"{admin_url}/api/config", timeout=2) as response:
                settings = OverlaySettings.from_config(json.loads(response.read().decode("utf-8")))
            if settings.adjusting and time.monotonic() < holder.get("adjust_off_until", 0.0):
                settings = replace(settings, adjusting=False)
            holder["settings"] = settings
            last_ok = time.monotonic()
            if not settings.enabled:
                stop.set()
        except (OSError, ValueError):
            if time.monotonic() - last_ok > ADMIN_GONE_SEC:
                stop.set()
        stop.wait(1.0)


def _save(admin_url: str, patch: dict[str, Any]) -> None:
    """POST a desktop_overlay patch to the admin API (no Origin header: a local tool)."""
    def send() -> None:
        request = urllib.request.Request(
            f"{admin_url}/api/config",
            data=json.dumps({"desktop_overlay": patch}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=3).close()
        except OSError:
            pass

    threading.Thread(target=send, daemon=True).start()


def _read_captions(holder: dict[str, Any], inbox: "queue.Queue[dict[str, Any]]", stop: threading.Event) -> None:
    from websockets.sync.client import connect

    while not stop.is_set():
        port = holder["settings"].ws_port
        try:
            with connect(f"ws://127.0.0.1:{port}", open_timeout=3) as socket:
                while not stop.is_set():
                    try:
                        raw = socket.recv(timeout=0.5)
                    except TimeoutError:
                        continue
                    message = json.loads(raw)
                    if isinstance(message, dict):
                        inbox.put(message)
        except Exception:
            stop.wait(1.0)


def run(admin_url: str) -> int:
    stop = threading.Event()
    holder: dict[str, Any] = {"settings": OverlaySettings()}
    inbox: queue.Queue[dict[str, Any]] = queue.Queue()
    threading.Thread(target=_poll_settings, args=(admin_url, holder, stop), daemon=True).start()
    threading.Thread(target=_read_captions, args=(holder, inbox, stop), daemon=True).start()

    def moved(rect: tuple[int, int, int, int]) -> None:
        x, y, width, height = rect
        _save(admin_url, {"x": x, "y": y, "width": width, "height": height})

    def done() -> None:
        # Fixed at once on screen; polls that still say "adjusting" are ignored for a moment.
        holder["adjust_off_until"] = time.monotonic() + 3.0
        holder["settings"] = replace(holder["settings"], adjusting=False)
        _save(admin_url, {"adjusting": False})

    window = OverlayWindow(moved, done)
    state = CaptionState()
    drawn: Any = None
    last_top = 0.0
    try:
        while not stop.is_set():
            window.pump()
            now = time.monotonic()
            settings = holder["settings"]
            while True:
                try:
                    state.apply(inbox.get_nowait(), now, settings)
                except queue.Empty:
                    break
            lines = state.lines(now, settings)
            signature = (tuple(lines), replace(settings, x=0, y=0, width=0, height=0))
            if signature != drawn:
                window.show(lines, settings)
                drawn = signature
            if now - last_top > 2.0:
                window.keep_on_top()
                last_top = now
            time.sleep(0.03)
    finally:
        window.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Streamer Caption desktop caption overlay")
    parser.add_argument("--admin", default="http://127.0.0.1:8766")
    args = parser.parse_args()
    return run(args.admin.rstrip("/"))


if __name__ == "__main__":
    raise SystemExit(main())
