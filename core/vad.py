from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.audio_capture import AudioChunk, AudioSegment, EnergyVadSegmenter


@dataclass(frozen=True)
class VadDecision:
    is_voice: bool
    score: float
    engine: str
    fallback_reason: str | None = None


class EnergyVoiceDetector:
    def __init__(self, config: dict[str, Any]) -> None:
        self.threshold = float(config["audio"]["vad_energy_threshold"])
        self.engine = "energy"

    def decide(self, chunk: AudioChunk) -> VadDecision:
        return VadDecision(
            is_voice=chunk.rms >= self.threshold,
            score=chunk.rms,
            engine=self.engine,
        )


class SileroVoiceDetector:
    def __init__(self, config: dict[str, Any]) -> None:
        self.threshold = float(config["vad"].get("speech_threshold", 0.5))
        self.sample_rate = int(config["audio"]["sample_rate"])
        self.engine = "silero"
        self.energy_threshold = float(config["audio"]["vad_energy_threshold"])
        self.runtime_fallback = str(
            config["vad"].get("fallback_on_runtime_fail", "energy_vad_this_chunk_only")
        )
        self._runtime_failed_permanently = False
        self._torch: Any | None = None
        self._model: Any | None = None
        self._fallback_reason: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            import torch
            from silero_vad import load_silero_vad
        except Exception as exc:
            self._fallback_reason = str(exc)
            return
        try:
            model = load_silero_vad()
            model.eval()
            self._torch = torch
            self._model = model
        except Exception as exc:
            self._fallback_reason = str(exc)

    @property
    def available(self) -> bool:
        return self._model is not None and self._torch is not None

    @property
    def fallback_reason(self) -> str | None:
        return self._fallback_reason

    def decide(self, chunk: AudioChunk) -> VadDecision:
        if not self.available or self._runtime_failed_permanently:
            return self._energy_fallback(
                chunk, self._fallback_reason or "silero_unavailable"
            )
        try:
            prob = self._max_probability(chunk.samples)
        except Exception as exc:
            self._fallback_reason = str(exc)
            if self.runtime_fallback == "energy_vad_permanent":
                self._runtime_failed_permanently = True
            return self._energy_fallback(chunk, str(exc))
        return VadDecision(
            is_voice=prob >= self.threshold,
            score=prob,
            engine=self.engine,
        )

    def reset_states(self) -> None:
        if self.available and hasattr(self._model, "reset_states"):
            self._model.reset_states()

    def _energy_fallback(self, chunk: AudioChunk, reason: str) -> VadDecision:
        return VadDecision(
            is_voice=chunk.rms >= self.energy_threshold,
            score=chunk.rms,
            engine="energy",
            fallback_reason=reason,
        )

    def _max_probability(self, samples: Any) -> float:
        import numpy as np

        arr = np.asarray(samples, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return 0.0
        frame_size = 512 if self.sample_rate == 16000 else 256
        if arr.size < frame_size:
            arr = np.pad(arr, (0, frame_size - arr.size))
        scores: list[float] = []
        assert self._torch is not None
        assert self._model is not None
        with self._torch.no_grad():
            for start in range(0, arr.size, frame_size):
                frame = arr[start : start + frame_size]
                if frame.size < frame_size:
                    frame = np.pad(frame, (0, frame_size - frame.size))
                tensor = self._torch.from_numpy(frame)
                value = self._model(tensor, self.sample_rate)
                scores.append(float(value.item()))
        return max(scores) if scores else 0.0


class DecisionVadSegmenter(EnergyVadSegmenter):
    def __init__(self, config: dict[str, Any], detector: Any) -> None:
        super().__init__(config)
        self.detector = detector
        self.threshold = float(getattr(detector, "threshold", self.threshold))
        self.engine = getattr(detector, "engine", "unknown")
        self._last_decision: VadDecision | None = None

    def push(self, chunk: AudioChunk) -> AudioSegment | None:
        decision = self.detector.decide(chunk)
        self._last_decision = decision
        return self._push_with_voice(chunk, decision.is_voice)

    @property
    def last_decision(self) -> VadDecision | None:
        return self._last_decision

    def _push_with_voice(self, chunk: AudioChunk, is_voice: bool) -> AudioSegment | None:
        return super()._push_with_voice(chunk, is_voice)

    def reset(self) -> None:
        super().reset()
        detector = getattr(self, "detector", None)
        if detector is not None and hasattr(detector, "reset_states"):
            detector.reset_states()


def create_voice_detector(config: dict[str, Any]) -> Any:
    engine = str(config.get("vad", {}).get("engine", "energy")).lower()
    if engine != "silero":
        return EnergyVoiceDetector(config)

    silero = SileroVoiceDetector(config)
    if silero.available:
        return silero
    fallback = str(config.get("vad", {}).get("fallback_on_load_fail", "energy_vad_permanent"))
    if fallback:
        return EnergyVoiceDetector(config)
    return silero


def create_vad_segmenter(config: dict[str, Any]) -> DecisionVadSegmenter:
    return DecisionVadSegmenter(config, create_voice_detector(config))
