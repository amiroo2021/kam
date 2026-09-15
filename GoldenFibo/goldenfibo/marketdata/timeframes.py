"""Timeframe helpers and supported intervals."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import FrozenSet

# Phase 3 fully validated first; others accepted by API/downloader wiring.
SUPPORTED_INTERVALS: FrozenSet[str] = frozenset(
    {"1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"}
)

_INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def validate_interval(interval: str) -> str:
    iv = (interval or "").strip()
    if iv not in SUPPORTED_INTERVALS:
        raise ValueError(f"unsupported timeframe {interval!r}; allowed={sorted(SUPPORTED_INTERVALS)}")
    return iv


def interval_ms(interval: str) -> int:
    return _INTERVAL_MS[validate_interval(interval)]


def align_time_ms(ts_ms: int, interval: str, *, mode: str = "floor") -> int:
    """Align UTC epoch ms to timeframe boundary."""
    step = interval_ms(interval)
    if mode == "floor":
        return (ts_ms // step) * step
    if mode == "ceil":
        q, r = divmod(ts_ms, step)
        return ts_ms if r == 0 else (q + 1) * step
    raise ValueError(mode)


def require_aligned(ts_ms: int, interval: str, label: str = "timestamp") -> int:
    """Reject non-aligned times (Phase 3 preference B)."""
    aligned = align_time_ms(ts_ms, interval, mode="floor")
    if aligned != ts_ms:
        raise ValueError(
            f"{label} must be aligned to {interval} boundary (UTC); "
            f"got {ms_to_iso(ts_ms)}, expected {ms_to_iso(aligned)}"
        )
    return ts_ms


def parse_iso_to_ms(value: str) -> int:
    s = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ms_to_iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
