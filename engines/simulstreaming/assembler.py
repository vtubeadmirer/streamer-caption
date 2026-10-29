"""Turns SimulStreaming's append-only text increments into caption lines.

The server never revises text it has sent: every message adds a few words (" 오늘",
"은 날씨", ...) and `is_final` closes a voiced segment. Captions therefore need no gray
preview: the words of the current line are shown as they arrive (a *stable* pending line
that only grows) and the line is committed as a confirmed caption at a sentence end, at the
end of the voiced segment, or when it would grow past one caption line.
"""
from __future__ import annotations

from dataclasses import dataclass

SENTENCE_END = ".?!…。？！"


def has_words(text: str) -> bool:
    """False for punctuation-only leftovers (a "." that arrives after its line was committed)."""
    return any(char.isalnum() for char in text)


@dataclass(frozen=True)
class AssembledText:
    kind: str  # "pending" (the growing line) or "confirmed"
    text: str
    end_sec: float  # server stream clock: end of the last word in `text`
    segment: int  # voiced-segment counter (one per server is_final)
    start_sec: float | None = None  # server stream clock: start of the first word, when known


class CaptionAssembler:
    def __init__(self, max_chars: int = 28) -> None:
        self.max_chars = max(8, int(max_chars))
        self.segment = 0
        self._buffer = ""
        self._marks: list[tuple[int, float]] = []  # (buffer index after a word, its end time)
        self._starts: list[tuple[int, float]] = []  # (buffer index where a word begins, its start time)
        self._last_end = 0.0
        self._last_pending = ""

    @property
    def text(self) -> str:
        return self._buffer.strip()

    @property
    def last_end(self) -> float:
        return self._last_end

    def push(self, message: dict) -> list[AssembledText]:
        text = str(message.get("text") or "")
        is_final = bool(message.get("is_final"))
        if text:
            self._append(text, message)
        out = self._cut(is_final)
        if is_final:
            out.extend(self.flush())
            self.segment += 1
        pending = self._buffer.strip()
        if pending and has_words(pending) and pending != self._last_pending:
            out.append(
                AssembledText("pending", pending, self._end_at(len(self._buffer)), self.segment, self._start_before(len(self._buffer)))
            )
            self._last_pending = pending
        elif not pending:
            self._last_pending = ""
        return out

    def flush(self) -> list[AssembledText]:
        """Commit whatever is on the growing line (end of segment, fallback, shutdown)."""
        if not has_words(self._buffer):
            self._buffer = ""
            self._marks = []
            self._starts = []
            self._last_pending = ""
            return []
        return [self._commit(len(self._buffer))]

    # ------------------------------------------------------------------ internals

    def _append(self, text: str, message: dict) -> None:
        base = len(self._buffer)
        self._buffer += text
        words = message.get("words")
        marks: list[tuple[int, float]] = []
        starts: list[tuple[int, float]] = []
        if isinstance(words, list) and words and "".join(str(w.get("text", "")) for w in words if isinstance(w, dict)) == text:
            cursor = base
            for word in words:
                word_text = str(word.get("text", ""))
                start = _number(word.get("start"))
                if start is not None and word_text.strip():
                    starts.append((cursor, start))
                cursor += len(word_text)
                end = _number(word.get("end"))
                if end is not None:
                    marks.append((cursor, end))
        if not marks:
            end = _number(message.get("end"))
            if end is not None:
                marks.append((len(self._buffer), end))
        if not starts and text.strip():
            start = _number(message.get("start"))
            if start is not None:
                starts.append((base, start))
        for index, end in marks:
            end = max(end, self._last_end)
            self._marks.append((index, end))
            self._last_end = end
        self._starts.extend(starts)

    def _start_before(self, index: int) -> float | None:
        """Start time of the first word on the line, if it begins before `index`."""
        if self._starts and self._starts[0][0] < index:
            return self._starts[0][1]
        return None

    def _end_at(self, index: int) -> float:
        end = None
        for mark_index, mark_end in self._marks:
            if mark_index <= index:
                end = mark_end
            else:
                break
        if end is None:
            end = self._marks[0][1] if self._marks else self._last_end
        return end

    def _commit(self, index: int) -> AssembledText:
        piece = self._buffer[:index].strip()
        end = self._end_at(index)
        start = self._start_before(index)
        self._buffer = self._buffer[index:]
        self._marks = [(mark - index, mark_end) for mark, mark_end in self._marks if mark > index]
        self._starts = [(mark - index, mark_start) for mark, mark_start in self._starts if mark >= index]
        self._last_pending = ""
        return AssembledText("confirmed", piece, end, self.segment, start)

    def _cut(self, is_final: bool) -> list[AssembledText]:
        out: list[AssembledText] = []
        while self._buffer.strip():
            index = self._sentence_cut()
            if index is None and len(self._buffer.strip()) > self.max_chars:
                index = self._length_cut()
            if index is None:
                break
            piece = self._commit(index)
            if has_words(piece.text):
                out.append(piece)
        return out

    def _sentence_cut(self) -> int | None:
        """Index just past a sentence end that is followed by more words. A sentence end at the
        very end of the buffer waits: "3." may still become "3.5", and is_final commits it."""
        buffer = self._buffer
        for index, char in enumerate(buffer):
            if char not in SENTENCE_END:
                continue
            after = index + 1
            while after < len(buffer) and buffer[after] in SENTENCE_END:
                after += 1
            if after < len(buffer) and buffer[after].isspace() and buffer[:after].strip():
                return after
        return None

    def _length_cut(self) -> int | None:
        """The longest prefix that ends between words and fits on one caption line."""
        buffer = self._buffer
        best = None
        for index, char in enumerate(buffer):
            if char.isspace() and buffer[:index].strip():
                if len(buffer[:index].strip()) <= self.max_chars:
                    best = index
                else:
                    break
        if best is not None:
            return best
        # One very long word: cut at the first word boundary after it, else keep growing.
        for index, char in enumerate(buffer):
            if char.isspace() and len(buffer[:index].strip()) > self.max_chars:
                return index
        return None


def _number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number
