from __future__ import annotations

import re


CONNECTORS = ("그리고", "근데", "그런데", "하지만", "그래서", "그러면", "아니", "그러니까")


def split_sentences(text: str, max_chars: int = 50) -> list[str]:
    value = re.sub(r"\s+", " ", text).strip()
    if not value:
        return []
    if len(value) <= 25:
        return [value]

    parts = _split_by_punctuation(value)
    parts = _split_by_connectors(parts)
    result: list[str] = []
    for part in parts:
        result.extend(_split_long_chunk(part, max_chars))
    return [part.strip() for part in result if part.strip()]


def _split_by_punctuation(text: str) -> list[str]:
    pieces = re.split(r"(?<=[.!?,…])\s+", text)
    return [piece.strip() for piece in pieces if piece.strip()]


def _split_by_connectors(parts: list[str]) -> list[str]:
    result: list[str] = []
    for part in parts:
        split_at = None
        for connector in CONNECTORS:
            index = part.find(f" {connector} ")
            if index >= 8:
                split_at = index
                break
        if split_at is None:
            result.append(part)
        else:
            result.append(part[:split_at].strip())
            result.append(part[split_at:].strip())
    return result


def _split_long_chunk(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    words = text.split(" ")
    if len(words) <= 1:
        return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]
    midpoint = len(text) / 2
    best_index = 0
    best_distance = len(text)
    cursor = 0
    for i, word in enumerate(words[:-1]):
        cursor += len(word) + 1
        distance = abs(cursor - midpoint)
        if distance < best_distance:
            best_index = i + 1
            best_distance = distance
    left = " ".join(words[:best_index]).strip()
    right = " ".join(words[best_index:]).strip()
    return [part for part in (left, right) if part]

