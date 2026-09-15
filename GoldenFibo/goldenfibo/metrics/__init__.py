"""VWAP / POC metrics — semantics from plugins/trade/backtest_wizard.py."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import isnan
from typing import Iterable, List, Optional, Sequence, Tuple


Candle = Sequence  # [open_time_ms, o, h, l, c, base_vol, ..., quote_vol?]


def _f(x) -> float:
    return float(x)


@dataclass(frozen=True)
class OhlcvBar:
    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    base_volume: float
    quote_volume: float


def bar_from_binance_kline(k: Sequence) -> OhlcvBar:
    """Binance kline array → OhlcvBar (indices match public REST/WS kline)."""
    return OhlcvBar(
        ts_ms=int(k[0]),
        open=_f(k[1]),
        high=_f(k[2]),
        low=_f(k[3]),
        close=_f(k[4]),
        base_volume=_f(k[5]),
        quote_volume=_f(k[7]) if len(k) > 7 else _f(k[5]) * (_f(k[1]) + _f(k[4])) / 2.0,
    )


def ladder_vwap(bars: Iterable[OhlcvBar], since_ts_ms: int) -> Optional[float]:
    """Wizard ``_vwap``: quote_vol/base_vol over bars with ts >= since_ts (leg0 / ladder start)."""
    base = 0.0
    quote = 0.0
    for b in bars:
        if b.ts_ms < since_ts_ms:
            continue
        base += b.base_volume
        quote += b.quote_volume
    if base <= 0:
        return None
    return quote / base


def step_vwap(bars: Iterable[OhlcvBar], since_ts_ms: int) -> Optional[float]:
    """Same formula as ladder VWAP but window starts at active-step leg timestamp."""
    return ladder_vwap(bars, since_ts_ms)


def volume_profile_poc(bars: Iterable[OhlcvBar], since_ts_ms: int, bins: int = 160) -> Optional[float]:
    """Wizard ``_poc``: distribute each bar's base volume uniformly over high–low into bins;
    return center of max-volume bin.
    """
    rel = [b for b in bars if b.ts_ms >= since_ts_ms]
    if not rel:
        return None
    lo = min(b.low for b in rel)
    hi = max(b.high for b in rel)
    if hi <= lo:
        return rel[-1].close
    bins = max(20, int(bins))
    width = (hi - lo) / bins
    vols = [0.0 for _ in range(bins)]
    for b in rel:
        v = b.base_volume
        if v <= 0:
            continue
        h, l = b.high, b.low
        if h <= l:
            idx = min(bins - 1, max(0, int((b.close - lo) / width)))
            vols[idx] += v
            continue
        a = max(0, int((l - lo) / width))
        b_idx = min(bins - 1, int((h - lo) / width))
        count = max(1, b_idx - a + 1)
        share = v / count
        for idx in range(a, b_idx + 1):
            vols[idx] += share
    idx = max(range(bins), key=lambda i: vols[i])
    return lo + (idx + 0.5) * width


def metrics_for_legs(
    bars: Sequence[OhlcvBar],
    *,
    ladder_start_ts_ms: Optional[int],
    step_start_ts_ms: Optional[int],
    bins: int = 160,
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Return (ladder_vwap, step_vwap, ladder_poc, step_poc)."""
    if ladder_start_ts_ms is None:
        return None, None, None, None
    lv = ladder_vwap(bars, ladder_start_ts_ms)
    lp = volume_profile_poc(bars, ladder_start_ts_ms, bins=bins)
    if step_start_ts_ms is None:
        return lv, None, lp, None
    sv = step_vwap(bars, step_start_ts_ms)
    sp = volume_profile_poc(bars, step_start_ts_ms, bins=bins)
    return lv, sv, lp, sp


def fmt_metric(x: Optional[float]) -> Optional[str]:
    if x is None or (isinstance(x, float) and isnan(x)):
        return None
    # stable string for WS JSON (not strategy geometry)
    return f"{x:.8f}".rstrip("0").rstrip(".") if abs(x) < 1e12 else str(x)
