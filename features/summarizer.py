from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Iterable


TOKEN_RE = re.compile(r"[가-힣]{2,}|[A-Za-z0-9][A-Za-z0-9_-]+")
STOPWORDS = {
    "그리고",
    "그런데",
    "그래서",
    "하지만",
    "그러면",
    "이거",
    "저거",
    "그거",
    "여기",
    "저기",
    "지금",
    "진짜",
    "그냥",
    "약간",
    "정말",
    "제가",
    "나는",
    "너무",
    "하는",
    "있는",
    "없는",
    "했는데",
    "합니다",
    "입니다",
    "것은",
    "the",
    "and",
    "that",
    "this",
    "with",
    "from",
    "have",
    "just",
}


EXTRA_STOPWORDS = {
    "\ubc29\uc1a1",
    "\uc624\ub298",
    "\uc9c0\uae08",
    "\ub2e4\uc74c",
    "\uc774\ubc88",
    "\ub9c8\uc9c0\ub9c9",
    "\uba3c\uc800",
    "\uac19\uc740",
    "\uac00\uc7a5",
    "\uc608\uc815",
    "\uc815\ub3c4",
    "\uc870\uae08",
    "\uc774\uc57c\uae30",
    "\ubcf4\uaca0\uc2b5\ub2c8\ub2e4",
    "\ud558\uaca0\uc2b5\ub2c8\ub2e4",
    "\ud569\ub2c8\ub2e4",
    "\ub429\ub2c8\ub2e4",
    "\uc785\ub2c8\ub2e4",
    "\uc788\uc2b5\ub2c8\ub2e4",
    "\uc5c6\uc2b5\ub2c8\ub2e4",
}
KOREAN_TOKEN_RE = re.compile(r"^[\uac00-\ud7a3]+$")
KOREAN_SUFFIXES = (
    "\ud558\uaca0\uc2b5\ub2c8\ub2e4",
    "\ud588\uc2b5\ub2c8\ub2e4",
    "\ud560\uac8c\uc694",
    "\ubcfc\uac8c\uc694",
    "\ud569\ub2c8\ub2e4",
    "\ub429\ub2c8\ub2e4",
    "\uc785\ub2c8\ub2e4",
    "\uc774\uc5d0\uc694",
    "\uc5d0\uc11c\ub294",
    "\uc73c\ub85c",
    "\uc5d0\uc11c",
    "\uc5d0\uac8c",
    "\ud55c\ud14c",
    "\ubd80\ud130",
    "\uae4c\uc9c0",
    "\ucc98\ub7fc",
    "\ud558\uace0",
    "\uc774\ub791",
    "\ub791",
    "\uc608\uc694",
    "\ud574\uc694",
    "\ub124\uc694",
    "\uc5b4\uc694",
    "\uc544\uc694",
    "\uacfc",
    "\uc640",
    "\uc740",
    "\ub294",
    "\uc774",
    "\uac00",
    "\uc744",
    "\ub97c",
    "\uc758",
    "\uc5d0",
    "\ub3c4",
    "\ub9cc",
    "\ub85c",
)


def _normalize_token(token: str) -> str:
    value = token.lower()
    if value in STOPWORDS or value in EXTRA_STOPWORDS:
        return ""
    if KOREAN_TOKEN_RE.fullmatch(value):
        changed = True
        while changed:
            changed = False
            for suffix in KOREAN_SUFFIXES:
                if value.endswith(suffix) and len(value) - len(suffix) >= 2:
                    value = value[: -len(suffix)]
                    changed = True
                    break
    if value in STOPWORDS or value in EXTRA_STOPWORDS:
        return ""
    return value


def tokenize(text: str) -> list[str]:
    normalized = [
        _normalize_token(token)
        for token in TOKEN_RE.findall(str(text))
    ]
    return [token for token in normalized if token]


def extract_keywords(
    captions: Iterable[dict[str, Any]],
    limit: int = 10,
) -> list[dict[str, Any]]:
    frequency: Counter[str] = Counter()
    for caption in captions:
        frequency.update(set(tokenize(str(caption.get("text", "")))))
    return [
        {"term": term, "count": count}
        for term, count in frequency.most_common(max(1, int(limit)))
    ]


def summarize_captions(
    captions: list[dict[str, Any]],
    *,
    bucket_index: int = 0,
    bucket_minutes: int = 30,
    max_bullets: int = 5,
) -> dict[str, Any]:
    bucket_seconds = max(1, int(bucket_minutes)) * 60
    base = {
        "bucket_index": int(bucket_index),
        "window_start_sec": int(bucket_index) * bucket_seconds,
        "window_end_sec": (int(bucket_index) + 1) * bucket_seconds,
        "caption_count": len(captions),
        "headline": "기록 없음",
        "bullets": [],
        "keywords": [],
        "method": "local_extractive_v1",
    }
    if not captions:
        return base

    token_rows = [tokenize(str(row.get("text", ""))) for row in captions]
    frequency: Counter[str] = Counter()
    for tokens in token_rows:
        frequency.update(set(tokens))
    keywords = [
        {"term": term, "count": count}
        for term, count in frequency.most_common(10)
    ]
    max_frequency = max(frequency.values(), default=1)
    candidates: list[tuple[float, int, dict[str, Any], set[str]]] = []
    for index, (caption, tokens) in enumerate(zip(captions, token_rows)):
        text = str(caption.get("text", "")).strip()
        if not text:
            continue
        unique = set(tokens)
        keyword_score = sum(
            frequency[token] / max_frequency for token in unique
        ) / max(math.sqrt(len(tokens) or 1), 1.0)
        length_score = min(len(text) / 45.0, 1.0) * 0.35
        punctuation_score = 0.12 if text.endswith((".", "?", "!", "요", "다")) else 0.0
        confidence_score = max(0.0, min(float(caption.get("confidence", 0.0)), 1.0)) * 0.1
        score = keyword_score + length_score + punctuation_score + confidence_score
        candidates.append((score, index, caption, unique))

    selected: list[tuple[float, int, dict[str, Any], set[str]]] = []
    for candidate in sorted(candidates, key=lambda item: (-item[0], item[1])):
        if len(selected) >= max(1, min(int(max_bullets), 10)):
            break
        candidate_tokens = candidate[3]
        too_similar = False
        for chosen in selected:
            union = candidate_tokens | chosen[3]
            similarity = (
                len(candidate_tokens & chosen[3]) / len(union)
                if union
                else 1.0
            )
            if similarity >= 0.72:
                too_similar = True
                break
        if not too_similar:
            selected.append(candidate)

    selected.sort(key=lambda item: (float(item[2].get("offset_sec", 0.0)), item[1]))
    repeated_terms = [
        item["term"] for item in keywords if int(item["count"]) > 1
    ]
    top_terms = repeated_terms[:3] or [
        item["term"] for item in keywords[:3]
    ]
    return {
        **base,
        "headline": " · ".join(top_terms) if top_terms else "주요 대화",
        "keywords": keywords,
        "bullets": [
            {
                "offset_sec": float(item[2].get("offset_sec", 0.0)),
                "text": str(item[2].get("text", "")),
                "score": round(item[0], 4),
            }
            for item in selected
        ],
    }

