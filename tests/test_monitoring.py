from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.monitoring import MemoryMonitor


def main() -> int:
    config = load_config()
    config["monitoring"]["memory_log_interval_min"] = 999
    monitor = MemoryMonitor(config)
    event = monitor.maybe_sample(force=True)
    assert event is not None
    assert event.event_type == "memory_sample"
    assert "rss_mb" in event.metadata
    assert Path(event.metadata["path"]).exists()
    print("monitoring_ok", event.metadata)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

