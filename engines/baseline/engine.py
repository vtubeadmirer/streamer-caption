from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from core.engine_contract import CaptionEngine
from core.events import CaptionEvent
from features.slang import SlangDictionary


def _package_dir(module: str, fallback: Path) -> Path:
    # The portable runtime is an embedded CPython whose sys.prefix is .venv\Scripts, so a
    # prefix-relative guess misses its site-packages; ask the import system first.
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        spec = None
    locations = list(spec.submodule_search_locations or []) if spec is not None else []
    return Path(locations[0]) if locations else fallback


def _prepend_to_path(directories: list[Path]) -> None:
    current = os.environ.get("PATH", "")
    known = {
        os.path.normcase(os.path.normpath(entry))
        for entry in current.split(os.pathsep)
        if entry
    }
    fresh = [
        str(directory)
        for directory in directories
        if os.path.normcase(os.path.normpath(str(directory))) not in known
    ]
    if fresh:
        os.environ["PATH"] = os.pathsep.join(fresh + ([current] if current else []))


class BaselineEngine(CaptionEngine):
    """Document-based v1-compatible segment STT adapter.

    It uses faster-whisper when available. Model loading is lazy so utility
    scripts can import the engine without immediately allocating GPU memory.
    """

    name = "baseline"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.model_name = config["whisper"]["model"]
        self.device = config["whisper"]["device"]
        self.compute_type = config["whisper"]["compute_type"]
        self.language = config["whisper"]["language"]
        self.task = config["whisper"]["task"]
        whisper = config["whisper"]
        self.beam_size = int(whisper.get("beam_size", 5))
        self.temperature = float(whisper.get("temperature", 0.0))
        self.compression_ratio_threshold = float(
            whisper.get("compression_ratio_threshold", 2.4)
        )
        self.log_prob_threshold = float(whisper.get("log_prob_threshold", -1.0))
        self.no_speech_threshold = float(whisper.get("no_speech_threshold", 0.6))
        self.condition_on_previous_text = bool(
            whisper.get("condition_on_previous_text", False)
        )
        self.repetition_penalty = float(
            whisper.get("repetition_penalty", 1.05)
        )
        self.no_repeat_ngram_size = int(
            whisper.get("no_repeat_ngram_size", 3)
        )
        self.initial_prompt = self._build_initial_prompt(config)
        self._model: Any | None = None
        self._degraded_reason: str | None = None
        self._dll_handles: list[Any] = []
        self._registered_dll_dirs: list[str] = []

    def _register_cuda_dll_dirs(self) -> list[str]:
        if os.name != "nt":
            return []
        site_packages = Path(sys.prefix) / "Lib" / "site-packages"
        nvidia_bins = [
            _package_dir(f"nvidia.{name}", site_packages / "nvidia" / name) / "bin"
            for name in ("cublas", "cudnn")
        ]
        candidates = [
            *nvidia_bins,
            _package_dir("ctranslate2", site_packages / "ctranslate2"),
        ]
        registered: list[str] = []
        for path in candidates:
            if path.is_dir():
                handle = os.add_dll_directory(str(path))
                self._dll_handles.append(handle)
                registered.append(str(path))
        # ctranslate2 opens cublas64_12.dll with a bare LoadLibrary, which searches PATH but
        # ignores add_dll_directory(); without this only a system-wide CUDA Toolkit is found.
        _prepend_to_path([path for path in nvidia_bins if path.is_dir()])
        self._registered_dll_dirs = registered
        return registered

    def _load_model(self) -> None:
        if self._model is not None:
            return

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Install requirements before STT use."
            ) from exc

        self._register_cuda_dll_dirs()
        try:
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
            )
            return
        except Exception as exc:
            fallback = self.config["streaming"].get("gpu_fallback_on_fail")
            if self.device != "cuda" or fallback != "degrade_to_cpu":
                raise

            self._degraded_reason = str(exc)
            self.device = "cpu"
            self.compute_type = "int8"
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
            )

    def warm_up(self) -> CaptionEvent:
        try:
            self._load_model()
            return CaptionEvent(
                event_type="engine_status",
                text="BaselineEngine model loaded.",
                engine=self.name,
                metadata=self._metadata(self.config["audio"]["sample_rate"]),
            )
        except Exception as exc:
            return CaptionEvent(
                event_type="engine_error",
                text=str(exc),
                engine=self.name,
                metadata=self._metadata(self.config["audio"]["sample_rate"]),
            )

    def on_audio_segment(
        self,
        audio: object,
        sample_rate: int,
        audio_offset_sec: float,
    ) -> Iterable[CaptionEvent]:
        if audio is None:
            yield CaptionEvent(
                event_type="engine_status",
                text="BaselineEngine is ready for audio input.",
                audio_offset_sec=audio_offset_sec,
                engine=self.name,
                metadata=self._metadata(sample_rate),
            )
            return

        try:
            self._load_model()
        except Exception as exc:
            yield CaptionEvent(
                event_type="engine_error",
                text=str(exc),
                audio_offset_sec=audio_offset_sec,
                engine=self.name,
                metadata=self._metadata(sample_rate),
            )
            return

        segments, info = self._model.transcribe(
            audio,
            language=self.language,
            task=self.task,
            initial_prompt=self.initial_prompt,
            beam_size=self.beam_size,
            temperature=self.temperature,
            compression_ratio_threshold=self.compression_ratio_threshold,
            log_prob_threshold=self.log_prob_threshold,
            no_speech_threshold=self.no_speech_threshold,
            condition_on_previous_text=self.condition_on_previous_text,
            repetition_penalty=self.repetition_penalty,
            no_repeat_ngram_size=self.no_repeat_ngram_size,
            vad_filter=False,
        )
        texts: list[str] = []
        confidences: list[float] = []
        no_speech_probs: list[float] = []
        avg_logprobs: list[float] = []
        compression_ratios: list[float] = []
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            texts.append(text)
            confidences.append(self._segment_confidence(segment))
            no_speech_probs.append(
                float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
            )
            avg_logprob = getattr(segment, "avg_logprob", None)
            if avg_logprob is not None:
                avg_logprobs.append(float(avg_logprob))
            compression_ratio = getattr(segment, "compression_ratio", None)
            if compression_ratio is not None:
                compression_ratios.append(float(compression_ratio))

        text = " ".join(texts).strip()
        if not text:
            yield CaptionEvent(
                event_type="caption_hidden",
                text="",
                confidence=0.0,
                audio_offset_sec=audio_offset_sec,
                engine=self.name,
                metadata={
                    **self._metadata(sample_rate),
                    "reason": "empty_transcript",
                },
            )
            return

        confidence = sum(confidences) / len(confidences) if confidences else 0.0
        yield CaptionEvent(
            event_type="caption_confirmed",
            text=text,
            confidence=confidence,
            audio_offset_sec=audio_offset_sec + getattr(info, "duration", 0.0),
            engine=self.name,
            metadata={
                **self._metadata(sample_rate),
                "no_speech_prob": (
                    sum(no_speech_probs) / len(no_speech_probs)
                    if no_speech_probs
                    else 0.0
                ),
                "avg_logprob": (
                    sum(avg_logprobs) / len(avg_logprobs)
                    if avg_logprobs
                    else None
                ),
                "compression_ratio": (
                    max(compression_ratios)
                    if compression_ratios
                    else None
                ),
                "transcript_segment_count": len(texts),
            },
        )

    def _segment_confidence(self, segment: Any) -> float:
        avg_logprob = getattr(segment, "avg_logprob", None)
        no_speech_prob = getattr(segment, "no_speech_prob", 0.0) or 0.0
        if avg_logprob is None:
            return max(0.0, min(1.0, 1.0 - no_speech_prob))
        logprob_score = max(0.0, min(1.0, (avg_logprob + 1.0)))
        speech_score = max(0.0, min(1.0, 1.0 - no_speech_prob))
        return (logprob_score + speech_score) / 2.0

    def _metadata(self, sample_rate: int) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "device": self.device,
            "compute_type": self.compute_type,
            "sample_rate": sample_rate,
            "degraded_reason": self._degraded_reason,
            "registered_dll_dirs": self._registered_dll_dirs,
        }

    def _build_initial_prompt(self, config: dict[str, Any]) -> str | None:
        parts = []
        base_prompt = config["whisper"].get("initial_prompt")
        if base_prompt:
            parts.append(str(base_prompt))
        # Slang mappings are applied after transcription. Injecting the full
        # dictionary into Whisper's prompt can make it echo a registered term
        # during noisy or non-speech audio, so prompt injection is opt-in.
        if config.get("slang", {}).get("prompt_mode") == "terms":
            addition = SlangDictionary(config).initial_prompt_addition()
            if addition:
                parts.append(addition)
        return "\n".join(parts) if parts else None