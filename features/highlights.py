from __future__ import annotations

import re
from typing import Any

from features.summarizer import extract_keywords, tokenize


LAUGHTER_RE = re.compile(r"(?:ㅋ{2,}|ㅎ{2,}|하하|웃겨)")
DEFAULT_WEIGHTS = {
    "strong_expression": 0.45,
    "laughter": 0.65,
    "keyword": 0.18,
    "topic_change": 0.4,
}


def _normalized_weights(
    weights: dict[str, Any] | None,
) -> dict[str, float]:
    result = dict(DEFAULT_WEIGHTS)
    if not isinstance(weights, dict):
        return result
    for key in result:
        value = weights.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        result[key] = max(0.0, min(float(value), 3.0))
    return result


def find_highlights(
    captions: list[dict[str, Any]],
    topics: list[dict[str, Any]] | None = None,
    *,
    limit: int = 10,
    weights: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not captions:
        return []
    weight = _normalized_weights(weights)
    common = {
        item["term"]
        for item in extract_keywords(captions, limit=12)
    }
    topic_offsets = [
        float(item.get("offset_sec", 0.0))
        for item in (topics or [])
    ]
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for caption in captions:
        text = str(caption.get("text", "")).strip()
        normalized = " ".join(text.lower().split())
        if len(text) < 5 or normalized in seen:
            continue
        seen.add(normalized)
        reasons: list[str] = []
        score_parts: dict[str, float] = {}

        length_score = min(len(text) / 60.0, 1.0) * 0.7
        score_parts["length"] = round(length_score, 4)
        score = length_score

        if ("!" in text or "?" in text) and weight["strong_expression"] > 0:
            expression_score = weight["strong_expression"]
            score += expression_score
            score_parts["strong_expression"] = round(
                expression_score,
                4,
            )
            reasons.append("strong_expression")

        if LAUGHTER_RE.search(text) and weight["laughter"] > 0:
            laughter_score = weight["laughter"]
            score += laughter_score
            score_parts["laughter"] = round(laughter_score, 4)
            reasons.append("laughter")

        keyword_hits = len(set(tokenize(text)) & common)
        if keyword_hits and weight["keyword"] > 0:
            keyword_score = min(
                keyword_hits * weight["keyword"],
                weight["keyword"] * 4.0,
            )
            score += keyword_score
            score_parts["key_topic"] = round(keyword_score, 4)
            reasons.append("key_topic")

        offset = float(caption.get("offset_sec", 0.0))
        if weight["topic_change"] > 0 and any(
            abs(offset - topic_offset) <= 15.0
            for topic_offset in topic_offsets[1:]
        ):
            topic_score = weight["topic_change"]
            score += topic_score
            score_parts["topic_change"] = round(topic_score, 4)
            reasons.append("topic_change")

        confidence = max(
            0.0,
            min(float(caption.get("confidence", 0.0)), 1.0),
        )
        confidence_score = confidence * 0.15
        score += confidence_score
        score_parts["confidence"] = round(confidence_score, 4)

        candidates.append(
            {
                "offset_sec": offset,
                "text": text,
                "score": round(score, 4),
                "score_parts": score_parts,
                "reasons": reasons or ["representative_caption"],
            }
        )
    candidates.sort(
        key=lambda item: (-item["score"], item["offset_sec"])
    )
    return candidates[: max(1, min(int(limit), 50))]