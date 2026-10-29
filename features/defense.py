from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import user_path
from features.phantom_phrases import is_phantom_line, is_subtitle_credit


@dataclass(frozen=True)
class DefenseResult:
    visible: bool
    text: str
    opacity: float
    reason: str = ""
    risk_score: float = 0.0
    signals: tuple[str, ...] = ()


def _load_terms(path: Path) -> list[str]:
    if not path.exists():
        return []
    terms: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        terms.append(value)
    return terms


class DefenseFilter:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        defense = config["defense"]
        self.min_confidence = float(defense["min_confidence_show"])
        self.dim_confidence = float(defense["dim_confidence_threshold"])
        self.filter_single_char = bool(defense["filter_single_char"])
        self.filter_repeat_char = bool(defense["filter_repeat_char"])
        self.filter_hallucination = bool(
            defense["filter_hallucination_patterns"]
        )
        self.min_segment_voice_ratio = float(
            defense.get("min_segment_voice_ratio", 0.0)
        )
        self.max_no_speech_prob = float(
            defense.get("max_no_speech_prob", 1.0)
        )
        self.hallucination_score_threshold = float(
            defense.get("hallucination_score_threshold", 0.72)
        )
        self.min_avg_logprob = float(
            defense.get("min_avg_logprob", -0.95)
        )
        self.max_compression_ratio = float(
            defense.get("max_compression_ratio", 2.6)
        )
        self.short_utterance_max_chars = int(
            defense.get("short_utterance_max_chars", 12)
        )
        self.short_utterance_min_confidence = float(
            defense.get("short_utterance_min_confidence", 0.72)
        )
        self.short_utterance_voice_ratio_floor = float(
            defense.get("short_utterance_voice_ratio_floor", 0.02)
        )
        self.repeat_window_sec = float(
            defense.get("repeat_window_sec", 45.0)
        )
        self.repeat_limit = int(defense.get("repeat_limit", 2))
        # A whole line like "감사합니다." is hidden when less than this share of its audio was
        # speech to the app's voice detector (metadata speech_share, else segment_voice_ratio
        # unless short_speech_rescue applies).
        self.phantom_max_speech_share = float(
            defense.get("phantom_max_speech_share", 0.3)
        )
        self._recent_visible: deque[tuple[float, str]] = deque()
        self.blocklist = _load_terms(user_path(defense["blocklist_path"]))
        self.whitelist = set(
            _load_terms(user_path(defense["whitelist_path"]))
        )

    def apply(
        self,
        text: str,
        confidence: float,
        metadata: dict[str, Any] | None = None,
        *,
        remember: bool = True,
        allow_single_char: bool = False,
    ) -> DefenseResult:
        """allow_single_char: the 정확 우선 engine's one-syllable words ("네") are real speech;
        every other rule still applies to them."""
        normalized = text.strip()
        if not normalized:
            return DefenseResult(False, "", 0.0, "empty")

        evidence = metadata or {}
        voice_ratio = self._float_metadata(
            evidence,
            "segment_voice_ratio",
            "voice_ratio",
        )
        no_speech_prob = self._float_metadata(
            evidence,
            "no_speech_prob",
        )
        avg_logprob = self._float_metadata(evidence, "avg_logprob")
        compression_ratio = self._float_metadata(
            evidence,
            "compression_ratio",
        )
        audio_offset = self._float_metadata(evidence, "audio_offset_sec")
        # Share of this line's audio the app's own voice detector heard as speech. The 정확 우선
        # engine reports no Whisper confidence values, so this is its main acoustic evidence.
        speech_share = self._float_metadata(evidence, "speech_share")
        compact_length = len(re.sub(r"\s+", "", normalized))
        short_speech_rescue = (
            compact_length <= self.short_utterance_max_chars
            and confidence >= self.short_utterance_min_confidence
            and voice_ratio is not None
            and voice_ratio >= self.short_utterance_voice_ratio_floor
            and (
                no_speech_prob is None
                or no_speech_prob < self.max_no_speech_prob
            )
        )

        if (
            voice_ratio is not None
            and voice_ratio < self.min_segment_voice_ratio
            and not short_speech_rescue
        ):
            return DefenseResult(
                False,
                normalized,
                0.0,
                "low_voice_ratio",
                1.0,
                ("low_voice_ratio",),
            )

        if (
            no_speech_prob is not None
            and no_speech_prob >= self.max_no_speech_prob
        ):
            return DefenseResult(
                False,
                normalized,
                0.0,
                "no_speech_probability",
                1.0,
                ("high_no_speech_probability",),
            )

        # Protected terms bypass text heuristics, but never strong evidence
        # that the captured segment was not speech.
        if normalized in self.whitelist or any(
            term and term in normalized for term in self.whitelist
        ):
            self._remember(normalized, audio_offset, remember)
            return DefenseResult(True, normalized, 1.0, "whitelist")

        if self.filter_hallucination and is_phantom_line(normalized):
            # faster-whisper lines have no speech_share; the segment's voice ratio stands in, but
            # not for a short line Whisper is sure of (short_speech_rescue, as for low_voice_ratio):
            # the pre-roll and every chunk the detector missed under game sound weigh heavily in
            # a short segment, so a donor's "감사합니다!" could be hidden.
            share = speech_share
            if share is None and not short_speech_rescue:
                share = voice_ratio
            if share is not None and share < self.phantom_max_speech_share:
                return DefenseResult(
                    False,
                    normalized,
                    0.0,
                    "phantom_phrase",
                    1.0,
                    ("phantom_phrase", "low_speech_share"),
                )

        risk_score, signals = self._hallucination_risk(
            normalized,
            confidence,
            voice_ratio,
            no_speech_prob,
            avg_logprob,
            compression_ratio,
            audio_offset,
            short_speech_rescue,
            speech_share,
        )
        if risk_score >= self.hallucination_score_threshold:
            return DefenseResult(
                False,
                normalized,
                0.0,
                "hallucination_score",
                risk_score,
                tuple(signals),
            )

        if confidence < self.min_confidence:
            return DefenseResult(
                False,
                normalized,
                0.0,
                "low_confidence",
                max(risk_score, 0.6),
                tuple((*signals, "low_confidence")),
            )

        if (
            self.filter_single_char
            and not allow_single_char
            and len(normalized.replace(" ", "")) <= 1
        ):
            return DefenseResult(False, normalized, 0.0, "single_char")

        if self.filter_repeat_char and re.search(r"(.)\1{5,}", normalized):
            return DefenseResult(False, normalized, 0.0, "repeat_char")

        if (
            self.filter_hallucination
            and self._looks_like_hallucination(normalized)
        ):
            return DefenseResult(
                False,
                normalized,
                0.0,
                "hallucination_pattern",
                1.0,
                tuple((*signals, "hallucination_pattern")),
            )

        masked = self._mask_blocklist(normalized)
        opacity = 0.55 if confidence < self.dim_confidence else 1.0
        reason = "dim_confidence" if opacity < 1.0 else ""
        self._remember(normalized, audio_offset, remember)
        return DefenseResult(
            True,
            masked,
            opacity,
            reason,
            risk_score,
            tuple(signals),
        )

    def _hallucination_risk(
        self,
        text: str,
        confidence: float,
        voice_ratio: float | None,
        no_speech_prob: float | None,
        avg_logprob: float | None,
        compression_ratio: float | None,
        audio_offset: float | None,
        short_speech_rescue: bool,
        speech_share: float | None = None,
    ) -> tuple[float, list[str]]:
        score = 0.0
        signals: list[str] = []

        low_speech_share = (
            speech_share is not None
            and speech_share < self.phantom_max_speech_share
        )
        if low_speech_share:
            score += 0.25
            signals.append("low_speech_share")

        weak_voice_limit = max(
            self.min_segment_voice_ratio * 2.0,
            0.12,
        )
        if short_speech_rescue:
            signals.append("short_speech_rescue")
        elif voice_ratio is not None and voice_ratio < weak_voice_limit:
            score += 0.16
            signals.append("weak_voice_ratio")

        moderate_no_speech = self.max_no_speech_prob * 0.6
        if (
            no_speech_prob is not None
            and no_speech_prob >= moderate_no_speech
        ):
            span = max(
                0.01,
                self.max_no_speech_prob - moderate_no_speech,
            )
            severity = min(
                1.0,
                max(
                    0.0,
                    (no_speech_prob - moderate_no_speech) / span,
                ),
            )
            score += 0.3 * max(0.25, severity)
            signals.append("elevated_no_speech_probability")

        if avg_logprob is not None and avg_logprob < self.min_avg_logprob:
            severity = min(
                1.0,
                max(
                    0.5,
                    (self.min_avg_logprob - avg_logprob) / 1.0,
                ),
            )
            score += 0.3 * severity
            signals.append("low_avg_logprob")

        if (
            compression_ratio is not None
            and compression_ratio > self.max_compression_ratio
        ):
            severity = min(
                1.0,
                max(
                    0.5,
                    (compression_ratio - self.max_compression_ratio)
                    / max(1.0, self.max_compression_ratio),
                ),
            )
            score += 0.3 * severity
            signals.append("high_compression_ratio")

        if confidence < self.dim_confidence:
            score += 0.14
            signals.append("weak_confidence")

        repeated = self._recent_repeat_count(text, audio_offset)
        # A low speech_share is not weak evidence here: a short "네" or "응" spans too little
        # audio for the voice detector and often scores under 0.3, so every third one said
        # within repeat_window_sec would be hidden. Outro phrases have their own rule (apply).
        weak_acoustic_evidence = (
            confidence < self.dim_confidence
            or (
                no_speech_prob is not None
                and no_speech_prob >= moderate_no_speech
            )
            or (
                voice_ratio is not None
                and voice_ratio < weak_voice_limit
            )
            or (
                avg_logprob is not None
                and avg_logprob < self.min_avg_logprob
            )
        )
        if repeated >= self.repeat_limit and weak_acoustic_evidence:
            score += 0.7
            signals.append("repeated_weak_caption")

        return min(1.0, round(score, 4)), signals

    def _recent_repeat_count(
        self,
        text: str,
        audio_offset: float | None,
    ) -> int:
        now = audio_offset if audio_offset is not None else time.monotonic()
        self._prune_recent(now)
        return sum(
            1
            for _, previous in self._recent_visible
            if previous == text
        )

    def _remember(
        self,
        text: str,
        audio_offset: float | None,
        remember: bool,
    ) -> None:
        if not remember:
            return
        now = audio_offset if audio_offset is not None else time.monotonic()
        self._prune_recent(now)
        self._recent_visible.append((now, text))

    def _prune_recent(self, now: float) -> None:
        while (
            self._recent_visible
            and now - self._recent_visible[0][0] > self.repeat_window_sec
        ):
            self._recent_visible.popleft()

    @staticmethod
    def _float_metadata(
        metadata: dict[str, Any],
        *keys: str,
    ) -> float | None:
        for key in keys:
            if key not in metadata:
                continue
            try:
                return float(metadata[key])
            except (TypeError, ValueError):
                return None
        return None

    def _mask_blocklist(self, text: str) -> str:
        result = text
        for term in self.blocklist:
            result = result.replace(term, "\u25a0" * max(2, len(term)))
        return result

    def _looks_like_hallucination(self, text: str) -> bool:
        """Subtitle credits ("\ud55c\uae00\uc790\ub9c9 by \u2026", "\uc790\ub9c9 \uc81c\uacf5 \u2026") and long runs of two or three characters.
        A sentence that merely mentions \uc790\ub9c9, \ud3b8\uc9d1 or "by" is real speech."""
        if is_subtitle_credit(text):
            return True
        compact_plain = re.sub(
            "[^0-9A-Za-z\uac00-\ud7a3]",
            "",
            text,
        )
        if len(compact_plain) >= 16 and len(set(compact_plain)) <= 3:
            return True
        return False