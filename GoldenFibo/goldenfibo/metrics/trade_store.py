"""Exchange-agnostic trade metric accumulator for LIVE VWAP / VAP POC.

Architecture:
  market trades → TradeMetricStore → VWAP/POC → snapshot/chart

GoldenFiboEngine geometry is independent. Binance is one trade source adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .trade_vap import (
    COMPLETE,
    INCOMPLETE_TRADE_HISTORY,
    BTCUSDT_TICK_SIZE,
    AggTrade,
    TradeVapProfile,
    TradeWindowMetrics,
    assess_trade_history_coverage,
    trade_metrics_for_windows,
    trade_vap_profile,
    trade_value_area,
    trade_vwap,
)

SOURCE_AGGTRADE = "AGGTRADE"
SOURCE_OHLC = "OHLC_APPROXIMATION"
STATUS_LOADING = "loading"
STATUS_COMPLETE = COMPLETE
STATUS_INCOMPLETE = INCOMPLETE_TRADE_HISTORY


@dataclass
class MetricDisplay:
    """Displayed metric fields plus per-window source/status metadata."""

    source: str
    ladder_metric_source: str = SOURCE_OHLC
    step_metric_source: str = SOURCE_OHLC
    ladder_aggtrade_status: str = STATUS_LOADING
    step_aggtrade_status: str = STATUS_LOADING
    ladder_vwap: Optional[float] = None
    step_vwap: Optional[float] = None
    ladder_poc: Optional[float] = None
    step_poc: Optional[float] = None
    ladder_val: Optional[float] = None
    ladder_vah: Optional[float] = None
    ladder_status: str = STATUS_LOADING
    step_status: str = STATUS_LOADING
    ladder_trade_count: int = 0
    step_trade_count: int = 0
    ladder_total_qty: float = 0.0
    step_total_qty: float = 0.0
    handoff_status: str = "idle"
    gap_count: int = 0
    top_ladder_bins: List[Tuple[float, float]] = field(default_factory=list)
    top_step_bins: List[Tuple[float, float]] = field(default_factory=list)
    detail: str = ""

    def as_payload_fields(self) -> Dict[str, object]:
        def fmt(x: Optional[float]) -> Optional[str]:
            if x is None:
                return None
            return f"{float(x):.2f}"

        def status_or_value(status: str, value: Optional[float]) -> Optional[str]:
            if status == STATUS_LOADING:
                return None  # chart shows loading via status field
            if status != STATUS_COMPLETE:
                return None
            return fmt(value)

        return {
            "metric_source": self.source,
            "ladder_metric_source": self.ladder_metric_source,
            "step_metric_source": self.step_metric_source,
            "ladder_aggtrade_status": self.ladder_aggtrade_status,
            "step_aggtrade_status": self.step_aggtrade_status,
            "ladder_vwap": status_or_value(self.ladder_status, self.ladder_vwap),
            "active_step_vwap": status_or_value(self.step_status, self.step_vwap),
            "ladder_poc": status_or_value(self.ladder_status, self.ladder_poc),
            "active_step_poc": status_or_value(self.step_status, self.step_poc),
            # Single chart VAH/VAL = whole ladder only (P0→now), never step VA
            "ladder_val": status_or_value(self.ladder_status, self.ladder_val),
            "ladder_vah": status_or_value(self.ladder_status, self.ladder_vah),
            "ladder_metric_status": self.ladder_status,
            "step_metric_status": self.step_status,
            "metrics_handoff_status": self.handoff_status,
            "metrics_gap_count": self.gap_count,
            "ladder_trade_count": self.ladder_trade_count,
            "step_trade_count": self.step_trade_count,
            "ladder_total_qty": self.ladder_total_qty,
            "step_total_qty": self.step_total_qty,
            "metrics_detail": self.detail,
            "ladder_top_vap_bins": [{"price": p, "qty": q} for p, q in self.top_ladder_bins[:5]],
            "step_top_vap_bins": [{"price": p, "qty": q} for p, q in self.top_step_bins[:5]],
        }


class TradeMetricStore:
    """Deduping trade store with REST→WS handoff and coverage gating.

    One store per live session supports both ladder (P0→now) and step (Pn→now)
    filters. Progression only changes step_start; TP/new P0 resets ladder_start
    and prunes older trades.
    """

    def __init__(self, *, tick_size: float = BTCUSDT_TICK_SIZE) -> None:
        self.tick_size = float(tick_size)
        self._by_id: Dict[int, AggTrade] = {}
        self.ladder_start_ms: Optional[int] = None
        self.step_start_ms: Optional[int] = None
        self.handoff_status: str = "idle"  # idle|backfilling|live_ready|incomplete
        self.backfill_complete: bool = False
        self.coverage_floor_ms: Optional[int] = None  # earliest ts we claim to cover
        self._gap_count: int = 0
        self._known_gaps: List[Tuple[int, int]] = []
        self._ws_attached: bool = False
        self.detail: str = ""

    # --- identity / size ---
    def __len__(self) -> int:
        return len(self._by_id)

    @property
    def trades(self) -> List[AggTrade]:
        return sorted(self._by_id.values(), key=lambda t: (t.ts_ms, t.agg_id))

    def clear(self) -> None:
        self._by_id.clear()
        self.ladder_start_ms = None
        self.step_start_ms = None
        self.handoff_status = "idle"
        self.backfill_complete = False
        self.coverage_floor_ms = None
        self._gap_count = 0
        self._known_gaps.clear()
        self._ws_attached = False
        self.detail = ""

    # --- windows (engine-driven) ---
    def set_windows(
        self,
        *,
        ladder_start_ms: Optional[int],
        step_start_ms: Optional[int],
    ) -> None:
        """Update metric windows from engine legs.

        New P0 (ladder start moves forward): prune trades strictly before new P0
        and require a fresh coverage assessment / backfill.
        Progression (step start moves, ladder unchanged): keep trades; step filter
        only changes.
        """
        new_ladder = int(ladder_start_ms) if ladder_start_ms is not None else None
        new_step = int(step_start_ms) if step_start_ms is not None else new_ladder

        if new_ladder is not None and self.ladder_start_ms is not None and new_ladder > self.ladder_start_ms:
            # TP / new cycle
            self._prune_before(new_ladder)
            self.backfill_complete = False
            self.coverage_floor_ms = None
            self.handoff_status = "backfilling"
            self.detail = "new P0 — coverage reset"
            self._gap_count = 0
            self._known_gaps.clear()

        if self.ladder_start_ms is None and new_ladder is not None:
            self.handoff_status = "backfilling" if not self.backfill_complete else self.handoff_status

        self.ladder_start_ms = new_ladder
        self.step_start_ms = new_step

    def _prune_before(self, ts_ms: int) -> None:
        drop = [i for i, t in self._by_id.items() if int(t.ts_ms) < int(ts_ms)]
        for i in drop:
            del self._by_id[i]

    # --- ingest ---
    def ingest(self, trade: AggTrade) -> bool:
        """Insert trade by agg_id. Returns True if newly stored."""
        aid = int(trade.agg_id)
        if aid in self._by_id:
            return False
        if self.ladder_start_ms is not None and int(trade.ts_ms) < int(self.ladder_start_ms):
            # ignore pre-P0 (closed ladder)
            return False
        self._by_id[aid] = AggTrade(
            agg_id=aid,
            price=float(trade.price),
            qty=float(trade.qty),
            ts_ms=int(trade.ts_ms),
        )
        return True

    def ingest_many(self, trades: Iterable[AggTrade]) -> int:
        n = 0
        for t in trades:
            if self.ingest(t):
                n += 1
        return n

    def ingest_ws_message(self, data: dict) -> bool:
        """Parse Binance aggTrade WS payload and ingest."""
        try:
            t = AggTrade(
                agg_id=int(data["a"]),
                price=float(data["p"]),
                qty=float(data["q"]),
                ts_ms=int(data.get("T") or data.get("E") or 0),
            )
        except (KeyError, TypeError, ValueError):
            return False
        return self.ingest(t)

    def mark_ws_attached(self) -> None:
        self._ws_attached = True

    # --- REST handoff ---
    def apply_rest_backfill(
        self,
        trades: Sequence[AggTrade],
        *,
        requested_start_ms: int,
        requested_end_ms: int,
    ) -> None:
        """Merge REST page results and evaluate start coverage.

        Does not invent trades. If the first returned trade is after the
        requested start (beyond slack), coverage stays incomplete.
        """
        self.handoff_status = "backfilling"
        self.ingest_many(trades)
        self.backfill_complete = True
        earliest = min((int(t.ts_ms) for t in trades), default=None)
        # Also consider anything already in store (WS buffer during backfill)
        if self._by_id:
            store_earliest = min(int(t.ts_ms) for t in self._by_id.values())
            earliest = store_earliest if earliest is None else min(earliest, store_earliest)

        self.coverage_floor_ms = earliest
        self._recompute_gaps()

        slack_ms = 2_000
        if earliest is None:
            # empty market in range — complete empty coverage of the window
            self.coverage_floor_ms = int(requested_start_ms)
            self.handoff_status = "live_ready"
            self.detail = "REST backfill empty — no trades in range"
            return

        if int(earliest) > int(requested_start_ms) + slack_ms:
            self.handoff_status = "incomplete"
            self.detail = (
                f"REST backfill earliest {earliest} after requested start "
                f"{requested_start_ms} (gap_ms={int(earliest) - int(requested_start_ms)})"
            )
            return

        self.coverage_floor_ms = int(requested_start_ms)
        self.handoff_status = "live_ready"
        self.detail = "REST backfill covers P0 start; WS handoff ready"

    def note_backfill_failure(self, error: str) -> None:
        self.backfill_complete = True
        self.handoff_status = "incomplete"
        self.detail = f"REST backfill failed: {error}"

    # --- gaps ---
    def _recompute_gaps(self) -> None:
        ids = sorted(self._by_id.keys())
        self._known_gaps.clear()
        if len(ids) < 2:
            self._gap_count = 0
            return
        gaps = 0
        for a, b in zip(ids, ids[1:]):
            if b > a + 1:
                self._known_gaps.append((a + 1, b - 1))
                gaps += b - a - 1
        self._gap_count = gaps

    def detect_id_gaps(self) -> List[Tuple[int, int]]:
        self._recompute_gaps()
        return list(self._known_gaps)

    @property
    def gap_count(self) -> int:
        return self._gap_count

    # --- coverage + metrics ---
    def _coverage_ok_for(self, window_start_ms: int) -> Tuple[bool, str, str]:
        """Return (ok, status, detail) for a window start."""
        if not self.backfill_complete and self.handoff_status in ("idle", "backfilling"):
            return False, STATUS_LOADING, "backfill in progress"
        if self.handoff_status == "incomplete":
            return False, STATUS_INCOMPLETE, self.detail or INCOMPLETE_TRADE_HISTORY
        if self.coverage_floor_ms is None:
            return False, STATUS_LOADING, "no coverage floor yet"
        # Floor must reach this window start (ladder floor is P0; step is later so OK)
        if int(self.coverage_floor_ms) > int(window_start_ms) + 2_000:
            return False, STATUS_INCOMPLETE, (
                f"coverage floor {self.coverage_floor_ms} after window start {window_start_ms}"
            )
        # Large unresolved id gaps after backfill → incomplete (missing volume)
        if self._gap_count > 0 and self.handoff_status != "live_ready":
            return False, STATUS_INCOMPLETE, f"{self._gap_count} missing agg trade ids"
        # Allow live_ready with minor gaps but flag detail; still complete if floor OK
        # Strict: any gap in the ladder window id span marks incomplete
        if self._gap_count > 0:
            return False, STATUS_INCOMPLETE, f"gapped agg trade ids count={self._gap_count}"
        return True, STATUS_COMPLETE, self.detail or "complete"

    def compute(self, *, now_ms: Optional[int] = None) -> MetricDisplay:
        if self.ladder_start_ms is None:
            return MetricDisplay(
                source=SOURCE_AGGTRADE,
                ladder_metric_source=SOURCE_OHLC,
                step_metric_source=SOURCE_OHLC,
                ladder_aggtrade_status=STATUS_LOADING,
                step_aggtrade_status=STATUS_LOADING,
                ladder_status=STATUS_LOADING,
                step_status=STATUS_LOADING,
                handoff_status=self.handoff_status,
                detail="no P0 window yet",
            )

        ladder_start = int(self.ladder_start_ms)
        step_start = int(self.step_start_ms if self.step_start_ms is not None else ladder_start)
        end = int(now_ms) if now_ms is not None else (
            max((int(t.ts_ms) for t in self._by_id.values()), default=ladder_start)
        )

        self._recompute_gaps()
        ladder_ok, ladder_st, ladder_detail = self._coverage_ok_for(ladder_start)
        step_ok, step_st, step_detail = self._coverage_ok_for(step_start)

        trades = self.trades
        lv = lp = sv = sp = l_val = l_vah = None
        lprof = sprof = None
        lcnt = scnt = 0
        lqty = sqty = 0.0

        if ladder_ok:
            lv = trade_vwap(trades, ladder_start)
            lprof = trade_vap_profile(trades, ladder_start, bin_size=self.tick_size)
            if lprof:
                lp = lprof.poc_price
                lqty = lprof.total_volume
                lcnt = sum(1 for t in trades if int(t.ts_ms) >= ladder_start)
                l_val, l_vah = trade_value_area(lprof, value_area_pct=0.70)
        if step_ok:
            sv = trade_vwap(trades, step_start)
            sprof = trade_vap_profile(trades, step_start, bin_size=self.tick_size)
            if sprof:
                sp = sprof.poc_price
                sqty = sprof.total_volume
                scnt = sum(1 for t in trades if int(t.ts_ms) >= step_start)

        global_source = SOURCE_AGGTRADE if ladder_ok and step_ok else SOURCE_OHLC
        if ladder_ok != step_ok:
            global_source = "MIXED"
        ladder_metric_source = SOURCE_AGGTRADE if ladder_ok else SOURCE_OHLC
        step_metric_source = SOURCE_AGGTRADE if step_ok else SOURCE_OHLC
        return MetricDisplay(
            source=global_source,
            ladder_metric_source=ladder_metric_source,
            step_metric_source=step_metric_source,
            ladder_aggtrade_status=ladder_st,
            step_aggtrade_status=step_st,
            ladder_vwap=lv,
            step_vwap=sv,
            ladder_poc=lp,
            step_poc=sp,
            ladder_val=l_val,
            ladder_vah=l_vah,
            ladder_status=ladder_st,
            step_status=step_st,
            ladder_trade_count=lcnt,
            step_trade_count=scnt,
            ladder_total_qty=lqty,
            step_total_qty=sqty,
            handoff_status=self.handoff_status,
            gap_count=self._gap_count,
            top_ladder_bins=lprof.top_bins(5) if lprof else [],
            top_step_bins=sprof.top_bins(5) if sprof else [],
            detail=ladder_detail if not ladder_ok else step_detail,
        )
