"""Trade-based VWAP and volume-at-price (VAP) POC from aggTrades.

Pure analytics: windows are supplied by GoldenFibo state timestamps
(P0 establishment / P(n) fill). No geometry or side logic lives here.

OHLC 160-bin POC remains the default display path until explicitly replaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

INCOMPLETE_TRADE_HISTORY = "INCOMPLETE_TRADE_HISTORY"
COMPLETE = "COMPLETE"

# BTCUSDT spot PRICE_FILTER.tickSize (exchangeInfo). Other symbols pass tick_size.
BTCUSDT_TICK_SIZE = 0.01


@dataclass(frozen=True)
class AggTrade:
    """One Binance compressed aggregate trade."""

    agg_id: int
    price: float
    qty: float
    ts_ms: int


@dataclass(frozen=True)
class TradeHistoryCoverage:
    status: str
    complete: bool
    window_start_ms: int
    window_end_ms: int
    first_trade_ts_ms: Optional[int]
    last_trade_ts_ms: Optional[int]
    trade_count: int
    earliest_available_ts_ms: Optional[int]
    detail: str = ""


@dataclass(frozen=True)
class TradeVapProfile:
    """Volume-at-price histogram over a trade window."""

    bin_size: float
    vols: Dict[float, float]  # bin_low_edge -> base qty
    poc_price: float
    poc_volume: float
    total_volume: float
    raw_price_levels: int
    price_min: float
    price_max: float

    def top_bins(self, n: int = 10) -> List[Tuple[float, float]]:
        """Return up to n (bin_price, volume) pairs, volume desc, price asc on ties."""
        items = sorted(self.vols.items(), key=lambda kv: (-kv[1], kv[0]))
        return items[: max(0, int(n))]


@dataclass(frozen=True)
class TradeWindowMetrics:
    ladder_start_ts_ms: int
    step_start_ts_ms: int
    ladder_vwap: Optional[float]
    step_vwap: Optional[float]
    ladder_poc: Optional[float]
    step_poc: Optional[float]
    ladder_status: str
    step_status: str
    ladder_coverage: TradeHistoryCoverage
    step_coverage: TradeHistoryCoverage
    ladder_profile: Optional[TradeVapProfile] = None
    step_profile: Optional[TradeVapProfile] = None
    ladder_trade_count: int = 0
    step_trade_count: int = 0
    ladder_total_qty: float = 0.0
    step_total_qty: float = 0.0


def bin_price(price: float, bin_size: float) -> float:
    """Map a trade price to a deterministic bin low edge.

    Uses Decimal floor division so results are stable across platforms.
    bin_size must be > 0 (tick size or a multiple of tick).
    """
    bs = Decimal(str(bin_size))
    if bs <= 0:
        raise ValueError("bin_size must be > 0")
    p = Decimal(str(price))
    # floor to multiple of bin_size
    n = (p / bs).to_integral_value(rounding=ROUND_DOWN)
    return float(n * bs)


def _window_trades(trades: Iterable[AggTrade], since_ts_ms: int) -> List[AggTrade]:
    start = int(since_ts_ms)
    return [t for t in trades if int(t.ts_ms) >= start]


def trade_vwap(trades: Iterable[AggTrade], since_ts_ms: int) -> Optional[float]:
    """VWAP = Σ(p × q) / Σ(q) for trades with ts >= since_ts_ms."""
    num = 0.0
    den = 0.0
    start = int(since_ts_ms)
    for t in trades:
        if int(t.ts_ms) < start:
            continue
        q = float(t.qty)
        if q <= 0:
            continue
        num += float(t.price) * q
        den += q
    if den <= 0:
        return None
    return num / den


def trade_vap_profile(
    trades: Iterable[AggTrade],
    since_ts_ms: int,
    *,
    bin_size: float = BTCUSDT_TICK_SIZE,
) -> Optional[TradeVapProfile]:
    """Accumulate actual trade qty into price bins (no OHLC H–L spreading)."""
    rel = _window_trades(trades, since_ts_ms)
    if not rel:
        return None
    vols: Dict[float, float] = {}
    raw_levels: set[float] = set()
    pmin = float("inf")
    pmax = float("-inf")
    total = 0.0
    for t in rel:
        q = float(t.qty)
        if q <= 0:
            continue
        px = float(t.price)
        raw_levels.add(px)
        pmin = min(pmin, px)
        pmax = max(pmax, px)
        b = bin_price(px, bin_size)
        vols[b] = vols.get(b, 0.0) + q
        total += q
    if not vols or total <= 0:
        return None
    # POC: max volume; tie → lower bin price (deterministic)
    poc_price, poc_vol = min(vols.items(), key=lambda kv: (-kv[1], kv[0]))
    return TradeVapProfile(
        bin_size=float(bin_size),
        vols=vols,
        poc_price=float(poc_price),
        poc_volume=float(poc_vol),
        total_volume=float(total),
        raw_price_levels=len(raw_levels),
        price_min=float(pmin),
        price_max=float(pmax),
    )


def trade_poc(
    trades: Iterable[AggTrade],
    since_ts_ms: int,
    *,
    bin_size: float = BTCUSDT_TICK_SIZE,
) -> Optional[float]:
    """True VAP POC = price bin with maximum accumulated trade quantity."""
    prof = trade_vap_profile(trades, since_ts_ms, bin_size=bin_size)
    return None if prof is None else prof.poc_price


def trade_value_area(
    profile: TradeVapProfile,
    *,
    value_area_pct: float = 0.70,
) -> Tuple[float, float]:
    """VAL/VAH edges from a trade VAP profile (same bins as POC).

    Expands from the POC bin among sorted non-empty price bins until cumulative
    volume reaches ``value_area_pct`` of total (default 70%), matching the OHLC
    profile expansion rule: prefer the higher-volume adjacent side; ties expand
    right then left.

    Returns (VAL, VAH) where VAL is the low edge of the leftmost included bin
    and VAH is the high edge of the rightmost included bin (bin + bin_size).
    """
    if not profile.vols or profile.total_volume <= 0:
        p = float(profile.poc_price)
        return p, p + float(profile.bin_size)
    bins = sorted(profile.vols.keys())
    vols = [float(profile.vols[b]) for b in bins]
    total = sum(vols)
    target = total * float(value_area_pct)
    # POC is stored as bin low edge
    try:
        poc_idx = bins.index(float(profile.poc_price))
    except ValueError:
        poc_idx = max(range(len(vols)), key=lambda i: (vols[i], -bins[i]))
    left = right = poc_idx
    acc = vols[poc_idx]
    n = len(vols)
    while acc + 1e-15 < target:
        lval = vols[left - 1] if left > 0 else -1.0
        rval = vols[right + 1] if right < n - 1 else -1.0
        if lval < 0 and rval < 0:
            break
        if rval > lval:
            right += 1
            acc += vols[right]
        elif lval > rval:
            left -= 1
            acc += vols[left]
        else:
            expanded = False
            if right < n - 1:
                right += 1
                acc += vols[right]
                expanded = True
            if acc + 1e-15 < target and left > 0:
                left -= 1
                acc += vols[left]
                expanded = True
            if not expanded:
                break
    width = float(profile.bin_size)
    val = float(bins[left])
    vah = float(bins[right]) + width
    return val, vah


def assess_trade_history_coverage(
    trades: Sequence[AggTrade],
    *,
    window_start_ms: int,
    window_end_ms: int,
    first_fetch_ts_ms: Optional[int] = None,
    max_start_gap_ms: int = 0,
) -> TradeHistoryCoverage:
    """Detect whether trade history covers the analytics window from the start.

    Rules:
    - If ``first_fetch_ts_ms`` (earliest retained/API-available trade time) is
      strictly after ``window_start_ms + max_start_gap_ms``, coverage is
      ``INCOMPLETE_TRADE_HISTORY`` — do not report a ladder POC as "true".
    - Else if the filtered trade list is non-empty and its first trade is within
      the allowed gap of the window start (or first_fetch covers the start),
      status is COMPLETE.
    - Empty window with complete availability still COMPLETE (zero volume).
    """
    w0 = int(window_start_ms)
    w1 = int(window_end_ms)
    rel = [t for t in trades if w0 <= int(t.ts_ms) <= w1]
    first_trade = min((int(t.ts_ms) for t in rel), default=None)
    last_trade = max((int(t.ts_ms) for t in rel), default=None)
    earliest = first_fetch_ts_ms if first_fetch_ts_ms is not None else first_trade

    if earliest is not None and int(earliest) > w0 + int(max_start_gap_ms):
        return TradeHistoryCoverage(
            status=INCOMPLETE_TRADE_HISTORY,
            complete=False,
            window_start_ms=w0,
            window_end_ms=w1,
            first_trade_ts_ms=first_trade,
            last_trade_ts_ms=last_trade,
            trade_count=len(rel),
            earliest_available_ts_ms=int(earliest),
            detail=(
                f"earliest available trade/buffer ts {earliest} is after "
                f"window start {w0} (gap_ms={int(earliest) - w0})"
            ),
        )

    return TradeHistoryCoverage(
        status=COMPLETE,
        complete=True,
        window_start_ms=w0,
        window_end_ms=w1,
        first_trade_ts_ms=first_trade,
        last_trade_ts_ms=last_trade,
        trade_count=len(rel),
        earliest_available_ts_ms=None if earliest is None else int(earliest),
        detail="trade history covers window start",
    )


def trade_metrics_for_windows(
    trades: Sequence[AggTrade],
    *,
    ladder_start_ts_ms: int,
    step_start_ts_ms: int,
    bin_size: float = BTCUSDT_TICK_SIZE,
    earliest_available_ts_ms: Optional[int] = None,
    window_end_ms: Optional[int] = None,
    max_start_gap_ms: int = 0,
) -> TradeWindowMetrics:
    """Compute trade VWAP/POC for ladder and step windows with coverage gates.

    If ladder coverage is incomplete, ladder VWAP/POC are None and status is
    INCOMPLETE_TRADE_HISTORY (same for step independently).
    """
    end = int(window_end_ms) if window_end_ms is not None else (
        max((int(t.ts_ms) for t in trades), default=int(ladder_start_ts_ms))
    )
    ladder_cov = assess_trade_history_coverage(
        trades,
        window_start_ms=int(ladder_start_ts_ms),
        window_end_ms=end,
        first_fetch_ts_ms=earliest_available_ts_ms,
        max_start_gap_ms=max_start_gap_ms,
    )
    step_cov = assess_trade_history_coverage(
        trades,
        window_start_ms=int(step_start_ts_ms),
        window_end_ms=end,
        first_fetch_ts_ms=earliest_available_ts_ms,
        max_start_gap_ms=max_start_gap_ms,
    )

    def _metrics(start: int, cov: TradeHistoryCoverage):
        if not cov.complete:
            return None, None, None, 0, 0.0
        rel = _window_trades(trades, start)
        vwap = trade_vwap(rel, since_ts_ms=start)
        prof = trade_vap_profile(rel, since_ts_ms=start, bin_size=bin_size)
        poc = None if prof is None else prof.poc_price
        qty = 0.0 if prof is None else prof.total_volume
        return vwap, poc, prof, len(rel), qty

    lv, lp, lprof, lcnt, lqty = _metrics(int(ladder_start_ts_ms), ladder_cov)
    sv, sp, sprof, scnt, sqty = _metrics(int(step_start_ts_ms), step_cov)

    return TradeWindowMetrics(
        ladder_start_ts_ms=int(ladder_start_ts_ms),
        step_start_ts_ms=int(step_start_ts_ms),
        ladder_vwap=lv,
        step_vwap=sv,
        ladder_poc=lp,
        step_poc=sp,
        ladder_status=ladder_cov.status,
        step_status=step_cov.status,
        ladder_coverage=ladder_cov,
        step_coverage=step_cov,
        ladder_profile=lprof,
        step_profile=sprof,
        ladder_trade_count=lcnt,
        step_trade_count=scnt,
        ladder_total_qty=lqty,
        step_total_qty=sqty,
    )


def poc_sensitivity(
    trades: Sequence[AggTrade],
    since_ts_ms: int,
    bin_sizes: Sequence[float],
) -> List[Dict[str, float]]:
    """Report POC under several deterministic bin sizes (evaluation helper)."""
    out: List[Dict[str, float]] = []
    for bs in bin_sizes:
        prof = trade_vap_profile(trades, since_ts_ms, bin_size=float(bs))
        if prof is None:
            out.append({"bin_size": float(bs), "poc": float("nan"), "bins": 0.0, "poc_vol": 0.0})
        else:
            out.append(
                {
                    "bin_size": float(bs),
                    "poc": float(prof.poc_price),
                    "bins": float(len(prof.vols)),
                    "poc_vol": float(prof.poc_volume),
                    "raw_levels": float(prof.raw_price_levels),
                    "lo": float(prof.price_min),
                    "hi": float(prof.price_max),
                }
            )
    return out
