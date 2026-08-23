from __future__ import annotations

from typing import Any

from features.summarizer import extract_keywords


LABEL_SEPARATOR = " \u00b7 "
SESSION_START_LABEL = "\ubc29\uc1a1 \uc2dc\uc791"


def _ranked_keywords(
    captions: list[dict[str, Any]],
    limit: int = 8,
) -> list[str]:
    return [
        str(item["term"])
        for item in extract_keywords(captions, limit=limit)
    ]


def detect_topic_transitions(
    captions: list[dict[str, Any]],
    *,
    window_captions: int = 6,
    similarity_threshold: float = 0.22,
    cooldown_sec: float = 30.0,
) -> list[dict[str, Any]]:
    if not captions:
        return []
    size = max(2, int(window_captions))
    blocks = [
        captions[index : index + size]
        for index in range(0, len(captions), size)
    ]
    first_ranked = _ranked_keywords(blocks[0])
    first_keywords = set(first_ranked)
    first_offset = float(blocks[0][0].get("offset_sec", 0.0))
    topics = [
        {
            "offset_sec": first_offset,
            "label": LABEL_SEPARATOR.join(first_ranked[:3])
            or SESSION_START_LABEL,
            "keywords": first_ranked,
            "similarity": 1.0,
            "reason": "session_start",
        }
    ]
    previous_keywords = first_keywords
    last_transition_offset = first_offset
    for block in blocks[1:]:
        if len(block) < 2:
            continue
        current_ranked = _ranked_keywords(block)
        current_keywords = set(current_ranked)
        if not current_keywords:
            continue
        union = previous_keywords | current_keywords
        similarity = (
            len(previous_keywords & current_keywords) / len(union)
            if union
            else 1.0
        )
        offset = float(block[0].get("offset_sec", 0.0))
        if (
            similarity < float(similarity_threshold)
            and offset - last_transition_offset >= float(cooldown_sec)
        ):
            topics.append(
                {
                    "offset_sec": offset,
                    "label": LABEL_SEPARATOR.join(current_ranked[:3]),
                    "keywords": current_ranked,
                    "similarity": round(similarity, 4),
                    "reason": "low_keyword_overlap",
                }
            )
            last_transition_offset = offset
        previous_keywords = current_keywords
    return topics