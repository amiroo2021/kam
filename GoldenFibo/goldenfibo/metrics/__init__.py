"""VWAP / POC / Value Area metrics.

VWAP & POC histogram semantics from plugins/trade/backtest_wizard.py:
  - VWAP = sum(quote_vol) / sum(base_vol) for bars with open_time >= window_start
  - POC = max-volume bin center after spreading each bar's base volume uniformly
    across bins covering [bar.low, bar.high] (default 160 bins over window range)

Value Area (standard market-profile style, 70% of profile volume):
  - Build the same histogram as POC
  - Start at the POC (max-volume) bin
  - Repeatedly expand to the adjacent bin with greater volume (ties: expand both
    sides when possible, right then left) until cumulative volume >= 70% of total
  - VAL = low edge of leftmost included bin; VAH = high edge of rightmost bin

Note on Hermes2 display parity (2026-09-15 diagnostic):
  Hermes2 "Ladder Value Area" matched this VA algorithm exactly.
  Hermes2 "Ladder POC" equaled (VAL+VAH)/2 (VA midpoint), NOT max-volume POC.
  We keep true volume POC and expose VA separately — do not relabel VA mid as POC.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isnan
from typing import Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class OhlcvBar:
    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    base_volume: float
    quote_volume: float


@dataclass(frozen=True)
class VolumeProfile:
    lo: float
    hi: float
    width: float
    vols: List[float]
    poc_index: int
    poc: float
    val: Optional[float]
    vah: Optional[float]


def _f(x) -> float:
    return float(x)


def bar_from_binance_kline(k: Sequence) -> OhlcvBar:
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
    return ladder_vwap(bars, since_ts_ms)


def _build_histogram(rel: Sequence[OhlcvBar], bins: int = 160) -> Optional[Tuple[float, float, float, List[float]]]:
    if not rel:
        return None
    lo = min(b.low for b in rel)
    hi = max(b.high for b in rel)
    if hi <= lo:
        return lo, hi, 0.0, []
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
    return lo, hi, width, vols


def build_volume_profile(
    bars: Iterable[OhlcvBar],
    since_ts_ms: int,
    *,
    bins: int = 160,
    value_area_pct: float = 0.70,
) -> Optional[VolumeProfile]:
    rel = [b for b in bars if b.ts_ms >= since_ts_ms]
    if not rel:
        return None
    built = _build_histogram(rel, bins=bins)
    assert built is not None
    lo, hi, width, vols = built
    if not vols or width <= 0:
        c = rel[-1].close
        return VolumeProfile(lo, hi, 0.0, [], 0, c, c, c)
    poc_index = max(range(len(vols)), key=lambda i: vols[i])
    poc = lo + (poc_index + 0.5) * width
    val, vah = _value_area_bounds(lo, width, vols, poc_index, value_area_pct)
    return VolumeProfile(lo, hi, width, vols, poc_index, poc, val, vah)


def _value_area_bounds(
    lo: float,
    width: float,
    vols: List[float],
    poc_index: int,
    value_area_pct: float,
) -> Tuple[float, float]:
    """Expand from POC bin until cumulative volume >= value_area_pct of total."""
    bins = len(vols)
    total = sum(vols)
    if total <= 0:
        return lo, lo + bins * width
    target = total * float(value_area_pct)
    left = right = poc_index
    acc = vols[poc_index]
    while acc + 1e-15 < target:
        lval = vols[left - 1] if left > 0 else -1.0
        rval = vols[right + 1] if right < bins - 1 else -1.0
        if lval < 0 and rval < 0:
            break
        if rval > lval:
            right += 1
            acc += vols[right]
        elif lval > rval:
            left -= 1
            acc += vols[left]
        else:
            # tie: expand both when possible (right then left)
            expanded = False
            if right < bins - 1:
                right += 1
                acc += vols[right]
                expanded = True
            if acc + 1e-15 < target and left > 0:
                left -= 1
                acc += vols[left]
                expanded = True
            if not expanded:
                break
    val = lo + left * width
    vah = lo + (right + 1) * width
    return val, vah


def volume_profile_poc(bars: Iterable[OhlcvBar], since_ts_ms: int, bins: int = 160) -> Optional[float]:
    """Max-volume bin center (wizard ``_poc``)."""
    prof = build_volume_profile(bars, since_ts_ms, bins=bins)
    return None if prof is None else prof.poc


def volume_profile_value_area(
    bars: Iterable[OhlcvBar],
    since_ts_ms: int,
    *,
    bins: int = 160,
    value_area_pct: float = 0.70,
) -> Tuple[Optional[float], Optional[float]]:
    """Return (VAL, VAH) or (None, None)."""
    prof = build_volume_profile(bars, since_ts_ms, bins=bins, value_area_pct=value_area_pct)
    if prof is None:
        return None, None
    return prof.val, prof.vah


def metrics_for_legs(
    bars: Sequence[OhlcvBar],
    *,
    ladder_start_ts_ms: Optional[int],
    step_start_ts_ms: Optional[int],
    bins: int = 160,
) -> Tuple[
    Optional[float],
    Optional[float],
    Optional[float],
    Optional[float],
    Optional[float],
    Optional[float],
]:
    """Return (ladder_vwap, step_vwap, ladder_poc, step_poc, ladder_val, ladder_vah)."""
    if ladder_start_ts_ms is None:
        return None, None, None, None, None, None
    lv = ladder_vwap(bars, ladder_start_ts_ms)
    lp = volume_profile_poc(bars, ladder_start_ts_ms, bins=bins)
    l_val, l_vah = volume_profile_value_area(bars, ladder_start_ts_ms, bins=bins)
    if step_start_ts_ms is None:
        return lv, None, lp, None, l_val, l_vah
    sv = step_vwap(bars, step_start_ts_ms)
    sp = volume_profile_poc(bars, step_start_ts_ms, bins=bins)
    return lv, sv, lp, sp, l_val, l_vah


def fmt_metric(x: Optional[float], *, decimals: int = 2) -> Optional[str]:
    """Display metrics/prices with fixed double-digit decimals."""
    if x is None or (isinstance(x, float) and isnan(x)):
        return None
    return f"{float(x):.{decimals}f}"


def fmt_price(x) -> Optional[str]:
    if x is None:
        return None
    try:
        return f"{float(x):.2f}"
    except (TypeError, ValueError):
        return str(x)


# Trade-based VAP / VWAP (aggTrade) — optional analytics path; does not replace OHLC defaults.
from .trade_vap import (  # noqa: E402
    BTCUSDT_TICK_SIZE,
    INCOMPLETE_TRADE_HISTORY,
    AggTrade,
    TradeHistoryCoverage,
    TradeVapProfile,
    TradeWindowMetrics,
    assess_trade_history_coverage,
    bin_price,
    poc_sensitivity,
    trade_metrics_for_windows,
    trade_poc,
    trade_value_area,
    trade_vap_profile,
    trade_vwap,
)
