from __future__ import annotations
from pathlib import Path
from decimal import Decimal

from fibolearn.collector.live_collector import FiboLearnCollector, CollectorSettings
from fibolearn.config.defaults import DEFAULT_SYMBOLS, DEFAULT_PERCENTAGES
from fibolearn.storage.sqlite_store import FiboLearnStore


def main() -> None:
    store = FiboLearnStore(Path.home() / ".hermes" / "fibolearn" / "fibolearn.sqlite")
    settings = CollectorSettings(interval_seconds=60, symbols=DEFAULT_SYMBOLS, percentages=DEFAULT_PERCENTAGES)
    FiboLearnCollector(store, settings=settings).run_forever()


if __name__ == "__main__":
    main()
