from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence


@dataclass(frozen=True)
class HistoricalCandle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    quote_volume: float | None = None
    volume_kind: str = "unknown"
    price_side: str | None = None

    def as_replay_row(self) -> list:
        vol = 0.0 if self.volume is None else float(self.volume)
        qvol = vol if self.quote_volume is None else float(self.quote_volume)
        return [self.open_time_ms, self.open, self.high, self.low, self.close, vol,
                self.open_time_ms + 59_999, qvol, 0, 0.0, 0.0, "0"]


@dataclass(frozen=True)
class HistoricalRequest:
    instrument: str
    start_ms: int
    end_ms: int
    price_side: str | None = None


class HistoricalProvider(Protocol):
    provider_id: str

    def fetch_range(self, request: HistoricalRequest, *, on_progress: Callable[[dict], None] | None = None) -> Sequence[list]: ...


def check_disk_budget(
    path: Path | str,
    estimated_bytes: int,
    *,
    threshold_bytes: int,
    journal_reserve_bytes: int = 0,
) -> None:
    """Raise before a write if projected DB/journal growth crosses the reserve."""
    target = Path(path)
    usage = shutil.disk_usage(target.parent if target.parent.exists() else Path("/"))
    projected = max(0, int(estimated_bytes)) + max(0, int(journal_reserve_bytes))
    if usage.free - projected < int(threshold_bytes):
        raise RuntimeError(
            f"historical cache disk safety threshold would be crossed: free={usage.free} "
            f"estimated={estimated_bytes} journal_reserve={journal_reserve_bytes} "
            f"threshold={threshold_bytes}"
        )
