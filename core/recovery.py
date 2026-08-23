from __future__ import annotations

import threading
from typing import Any


class ConfirmedRangeLedger:
    """Claims confirmed STT results once by job id and sample range."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job_ids: set[int] = set()
        self._ranges: set[tuple[int, int]] = set()
        self._last_committed_end_sample = 0
        self._committed = 0
        self._duplicates = 0

    def claim(
        self,
        job_id: int,
        metadata: dict[str, Any],
    ) -> bool:
        sample_range = self._sample_range(metadata)
        with self._lock:
            duplicate = job_id in self._job_ids
            if sample_range is not None and sample_range in self._ranges:
                duplicate = True
            if duplicate:
                self._duplicates += 1
                return False

            self._job_ids.add(job_id)
            if sample_range is not None:
                self._ranges.add(sample_range)
                self._last_committed_end_sample = max(
                    self._last_committed_end_sample,
                    sample_range[1],
                )
            self._committed += 1
            return True

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "committed_confirmed_jobs": self._committed,
                "duplicate_confirmed_results": self._duplicates,
                "last_committed_end_sample": (
                    self._last_committed_end_sample
                ),
            }

    @staticmethod
    def _sample_range(
        metadata: dict[str, Any],
    ) -> tuple[int, int] | None:
        try:
            start = int(metadata["segment_start_sample"])
            end = int(metadata["segment_end_sample"])
        except (KeyError, TypeError, ValueError):
            return None
        if start < 0 or end <= start:
            return None
        return start, end