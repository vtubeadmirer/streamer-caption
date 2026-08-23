from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AudioWindow:
    samples: Any
    sample_rate: int
    start_sample: int
    end_sample: int

    @property
    def start_offset_sec(self) -> float:
        return self.start_sample / self.sample_rate

    @property
    def end_offset_sec(self) -> float:
        return self.end_sample / self.sample_rate

    @property
    def duration_sec(self) -> float:
        return max(0.0, (self.end_sample - self.start_sample) / self.sample_rate)


class AudioRingBuffer:
    def __init__(self, sample_rate: int, max_seconds: float) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if max_seconds <= 0:
            raise ValueError("max_seconds must be positive")
        self.sample_rate = int(sample_rate)
        self.capacity = max(1, int(sample_rate * max_seconds))
        self._start_sample = 0
        self._end_sample = 0
        self._samples: Any | None = None

    def append(self, samples: Any) -> AudioWindow:
        import numpy as np

        arr = np.asarray(samples, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return self.latest(0.0)
        if self._samples is None:
            self._samples = arr.copy()
        else:
            self._samples = np.concatenate([self._samples, arr], axis=0)
        self._end_sample += int(arr.size)
        overflow = int(self._samples.size) - self.capacity
        if overflow > 0:
            self._samples = self._samples[overflow:]
            self._start_sample += overflow
        return self.window(self._end_sample - int(arr.size), self._end_sample)

    def latest(self, seconds: float) -> AudioWindow:
        if seconds <= 0:
            return self.window(self._end_sample, self._end_sample)
        sample_count = int(seconds * self.sample_rate)
        start = max(self._start_sample, self._end_sample - sample_count)
        return self.window(start, self._end_sample)

    def window(self, start_sample: int, end_sample: int) -> AudioWindow:
        import numpy as np

        start = max(self._start_sample, int(start_sample))
        end = min(self._end_sample, int(end_sample))
        if end < start:
            end = start
        if self._samples is None or start == end:
            data = np.zeros(0, dtype=np.float32)
        else:
            local_start = start - self._start_sample
            local_end = end - self._start_sample
            data = self._samples[local_start:local_end].copy()
        return AudioWindow(
            samples=data,
            sample_rate=self.sample_rate,
            start_sample=start,
            end_sample=end,
        )

    @property
    def start_sample(self) -> int:
        return self._start_sample

    @property
    def end_sample(self) -> int:
        return self._end_sample

    @property
    def duration_sec(self) -> float:
        return max(0.0, (self._end_sample - self._start_sample) / self.sample_rate)
