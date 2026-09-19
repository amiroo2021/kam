"""Exchange-agnostic trade metric accumulator for LIVE VWAP / VAP POC.

Architecture:
  market trades → TradeMetricStore → VWAP/POC → snapshot/chart

GoldenFiboEngine geometry is independent. Binance is one trade source adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .trade_vap import (
    COMPLETE,
    INCOMPLETE_TRADE_HISTORY,
    BTCUSDT_TICK_SIZE,
    AggTrade,
    StreamMetricsAccumulator,
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

    def __init__(
        self,
        *,
        tick_size: float = BTCUSDT_TICK_SIZE,
        market: str = "futures",
        symbol: str = "",
    ) -> None:
        self.tick_size = float(tick_size)
        self.market = str(market)
        self.symbol = str(symbol)
        self._by_id: Dict[int, AggTrade] = {}
        self.ladder_start_ms: Optional[int] = None
        self.step_start_ms: Optional[int] = None
        self.handoff_status: str = "idle"  # idle|backfilling|live_ready|incomplete
        self.backfill_complete: bool = False
        self.coverage_floor_ms: Optional[int] = None  # earliest ts we claim to cover
        self._gap_count: int = 0
        self._known_gaps: List[Tuple[int, int]] = []
        self._ws_attached: bool = False
        self._strict_id_gaps: bool = True
        self.detail: str = ""
        # Streaming lifecycle
        self._streamed_acc: Optional[StreamMetricsAccumulator] = None
        self._streamed_result: Optional[TradeWindowMetrics] = None
        self._streamed_identity: Optional[Tuple[str, str, int, int, int]] = None
        self._live_buffer: List[Dict[str, Any]] = []
        self._live_buffer_cap: int = 5_000
        self._step_dirty: bool = False

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
        self._strict_id_gaps = True
        self.detail = ""
        self.invalidate_streamed()

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
            # Streaming snapshot is for a previous ladder window.
            self.invalidate_streamed()

        if (
            new_step is not None
            and self.step_start_ms is not None
            and new_step > self.step_start_ms
        ):
            # Step activation only — ladder accumulator stays intact; only
            # the step portion becomes stale.
            self.mark_step_dirty()

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
        """Parse Binance aggregate/raw trade WS payload and ingest."""
        try:
            raw_id = data["a"] if "a" in data else data["t"]
            if "a" not in data and "t" in data:
                self._strict_id_gaps = False
                raw_id = -abs(int(raw_id))
            t = AggTrade(
                agg_id=int(raw_id),
                price=float(data["p"]),
                qty=float(data["q"]),
                ts_ms=int(data.get("T") or data.get("E") or 0),
            )
        except (KeyError, TypeError, ValueError):
            return False
        return self.ingest(t)

    def mark_ws_attached(self) -> None:
        self._ws_attached = True

    # --- streaming lifecycle ---
    def streamed_identity(self) -> Optional[Tuple[str, str, int, int, int]]:
        """Return (market, symbol, ladder_start, step_start, endpoint) of the
        currently installed streamed snapshot, or ``None`` if no snapshot."""
        return getattr(self, "_streamed_identity", None)

    def has_streaming_snapshot(self) -> bool:
        return self._streamed_acc is not None

    def is_step_dirty(self) -> bool:
        """True if the streaming snapshot is stale for the current step window
        but still valid for the ladder window. Set when only the step_start
        has advanced since the snapshot was installed."""
        return bool(getattr(self, "_step_dirty", False))

    def invalidate_streamed(self) -> None:
        """Drop the streaming snapshot. ``compute()`` falls back to OHLC."""
        self._streamed_acc = None
        self._streamed_result = None
        self._streamed_identity = None
        self._step_dirty = False
        self._live_buffer.clear()

    def mark_step_dirty(self) -> None:
        """Mark the streaming snapshot as stale for the current step window.
        The ladder portion remains valid; compute() will return AGGTRADE for
        ladder and OHLC for step until a fresh step-only backfill reinstalls
        step identity."""
        self._step_dirty = True

    def clear_step_dirty(self) -> None:
        self._step_dirty = False

    def apply_streaming_backfill(
        self,
        accumulator,
        *,
        market: str,
        symbol: str,
        ladder_start_ms: int,
        step_start_ms: int,
        endpoint_ts_ms: int,
        preserve_ladder_accumulator: bool = False,
    ) -> None:
        """Atomically install a streaming ladder+step accumulator snapshot.

        Caller must supply the exact window identity the snapshot was built
        for. Subsequent ``compute()`` calls will return streamed metrics
        only while ``streamed_identity() == (market, symbol, ladder_start,
        step_start, endpoint)``.

        Live trades with ``ts > endpoint_ts_ms`` that arrived during backfill
        are stored separately in ``_live_buffer`` and must be merged via
        ``merge_live_buffer`` before they contribute to streamed metrics.
        Live trades with ``ts <= endpoint_ts_ms`` are dropped (already
        covered by the historical pass).

        ``preserve_ladder_accumulator=True`` keeps the existing ladder
        accumulator's bins/counts and replaces only the step portion with
        the supplied accumulator's step bins/counts. Used for step-only
        reinstalls after a step activation.
        """
        if not isinstance(accumulator, StreamMetricsAccumulator):
            raise TypeError("apply_streaming_backfill requires a StreamMetricsAccumulator")
        identity = (
            str(market),
            str(symbol),
            int(ladder_start_ms),
            int(step_start_ms),
            int(endpoint_ts_ms),
        )
        current = (
            str(self.market),
            str(self.symbol),
            int(self.ladder_start_ms) if self.ladder_start_ms is not None else None,
            int(self.step_start_ms) if self.step_start_ms is not None else None,
            None,
        )
        if (
            current[0] != identity[0]
            or current[1] != identity[1]
            or current[2] is None
            or current[3] is None
        ):
            return
        if int(current[2]) != int(identity[2]) or int(current[3]) != int(identity[3]):
            return

        if preserve_ladder_accumulator and self._streamed_acc is not None:
            # Replace step portion only; ladder portion stays intact.
            new_acc = StreamMetricsAccumulator(
                ladder_start_ms=accumulator.ladder_start_ms,
                step_start_ms=accumulator.step_start_ms,
                bin_size=accumulator.bin_size,
            )
            # Copy ladder fields from the existing accumulator.
            old = self._streamed_acc
            new_acc.ladder_num = old.ladder_num
            new_acc.ladder_den = old.ladder_den
            new_acc.ladder_bins = dict(old.ladder_bins)
            new_acc.ladder_pmin = old.ladder_pmin
            new_acc.ladder_pmax = old.ladder_pmax
            new_acc.ladder_levels = set(old.ladder_levels)
            new_acc.ladder_count = old.ladder_count
            # Copy step fields from the new (bounded) accumulator.
            new_acc.step_num = accumulator.step_num
            new_acc.step_den = accumulator.step_den
            new_acc.step_bins = dict(accumulator.step_bins)
            new_acc.step_pmin = accumulator.step_pmin
            new_acc.step_pmax = accumulator.step_pmax
            new_acc.step_levels = set(accumulator.step_levels)
            new_acc.step_count = accumulator.step_count
            # Carry over identity dedupe set from the bounded accumulator
            # (it was populated by the re-stream).
            new_acc._seen_keys = set(accumulator._seen_keys)
            # Earliest/latest observed across old + new.
            new_acc.earliest_observed_ts_ms = (
                old.earliest_observed_ts_ms
                if old.earliest_observed_ts_ms is not None
                else accumulator.earliest_observed_ts_ms
            )
            if (
                accumulator.earliest_observed_ts_ms is not None
                and (
                    new_acc.earliest_observed_ts_ms is None
                    or accumulator.earliest_observed_ts_ms < new_acc.earliest_observed_ts_ms
                )
            ):
                new_acc.earliest_observed_ts_ms = accumulator.earliest_observed_ts_ms
            new_acc.latest_observed_ts_ms = (
                accumulator.latest_observed_ts_ms
                if accumulator.latest_observed_ts_ms is not None
                else old.latest_observed_ts_ms
            )
            if (
                accumulator.latest_observed_ts_ms is not None
                and (
                    new_acc.latest_observed_ts_ms is None
                    or accumulator.latest_observed_ts_ms > new_acc.latest_observed_ts_ms
                )
            ):
                new_acc.latest_observed_ts_ms = accumulator.latest_observed_ts_ms
            new_acc.latest_observed_storage_id = (
                accumulator.latest_observed_storage_id
                if accumulator.latest_observed_storage_id
                > (old.latest_observed_storage_id or 0)
                else old.latest_observed_storage_id
            )
            accumulator = new_acc

        self._streamed_acc = accumulator
        result = accumulator.finalize(window_end_ms=int(endpoint_ts_ms))
        self._streamed_result = result
        self._streamed_identity = identity
        self._step_dirty = False
        self.ladder_start_ms = int(ladder_start_ms)
        self.step_start_ms = int(step_start_ms)
        self.coverage_floor_ms = int(ladder_start_ms)
        ladder_cov_ok = bool(result.ladder_coverage.complete)
        step_cov_ok = bool(result.step_coverage.complete)
        if ladder_cov_ok and step_cov_ok:
            self.handoff_status = "live_ready"
            self.detail = "Streaming backfill covers P0 + step start"
        else:
            self.handoff_status = "incomplete"
            self.detail = (
                "Streaming backfill incomplete: "
                f"ladder_ok={ladder_cov_ok} step_ok={step_cov_ok}"
            )
        self.backfill_complete = True
        self._gap_count = 0
        self._known_gaps.clear()

    def merge_live_buffer(self) -> None:
        """Compatibility stub. The authoritative live trade source is the
        persistent SQLite cache via ``catchup_from_persistent``; the legacy
        in-memory buffer is no longer authoritative. This method is kept
        for backward compatibility and is a no-op for the streaming path.
        """
        # Live trades arriving during backfill are persisted into SQLite
        # by the controller immediately. After the streaming backfill
        # installs, the controller calls catchup_from_persistent() which
        # pulls any trades persisted after the backfill endpoint from
        # SQLite via (ts, agg_trade_id) keyset continuation. No in-memory
        # buffer overflow can lose data.
        self._live_buffer.clear()

    def catchup_from_persistent(
        self,
        aggtrade_cache,
        *,
        max_ts_ms: int,
    ) -> int:
        """Catch up the streaming accumulator with any trades persisted into
        SQLite between the backfill endpoint and now. Uses the (ts,
        storage_id) frontier stored in the accumulator to skip trades
        already incorporated. No buffer overflow possible because the
        authoritative source is the persistent cache.

        Returns the number of trades incorporated.
        """
        acc = self._streamed_acc
        if acc is None:
            return 0
        frontier_ts = acc.latest_observed_ts_ms
        frontier_id = acc.latest_observed_storage_id
        if frontier_ts is None:
            return 0
        n = 0
        for trade in aggtrade_cache.iter_after_frontier(
            self.market,
            self.symbol,
            int(frontier_ts),
            int(frontier_id),
            max_ts_ms=int(max_ts_ms),
        ):
            if acc.live_add(trade):
                n += 1
        # Refresh the materialized snapshot.
        if self._streamed_identity is not None:
            try:
                self._streamed_result = acc.finalize(
                    window_end_ms=int(self._streamed_identity[4])
                )
            except Exception:
                pass
        return n

    def reconcile_step_window(
        self,
        aggtrade_cache,
        *,
        new_step_start_ms: int,
        endpoint_ts_ms: int,
    ) -> bool:
        """Re-stream the new step window from the persistent cache and
        reinstall the streaming snapshot with refreshed step fields. Ladder
        accumulator fields are preserved. Returns True on success.

        Used to bring the step portion of a streaming snapshot back into
        AGGTRADE state after a step activation marked it dirty. The
        re-stream operates on `[new_step_start_ms, endpoint_ts_ms]` only —
        bounded, no full historical rescan.
        """
        if self._streamed_acc is None:
            return False
        ladder_start = int(self.ladder_start_ms) if self.ladder_start_ms is not None else int(new_step_start_ms)
        old_acc = self._streamed_acc
        new_acc = StreamMetricsAccumulator(
            ladder_start_ms=ladder_start,
            step_start_ms=int(new_step_start_ms),
            bin_size=old_acc.bin_size,
        )
        # Carry ladder fields from the existing accumulator.
        new_acc.ladder_num = old_acc.ladder_num
        new_acc.ladder_den = old_acc.ladder_den
        new_acc.ladder_bins = dict(old_acc.ladder_bins)
        new_acc.ladder_pmin = old_acc.ladder_pmin
        new_acc.ladder_pmax = old_acc.ladder_pmax
        new_acc.ladder_levels = set(old_acc.ladder_levels)
        new_acc.ladder_count = old_acc.ladder_count
        new_acc.earliest_observed_ts_ms = old_acc.earliest_observed_ts_ms
        new_acc.latest_observed_ts_ms = old_acc.latest_observed_ts_ms
        new_acc.latest_observed_storage_id = old_acc.latest_observed_storage_id
        # Stream step window through the cache iter and feed the new acc.
        # Note: do NOT carry over _seen_keys from old_acc. The step window
        # is being re-built; trades that were in the historical ladder pass
        # but also fall in the step window MUST be counted in the step
        # accumulators. The dedupe is by the new acc's _seen_keys which
        # starts empty, so the re-stream correctly populates both the
        # aggregate identities that fall in [new_step_start, endpoint] AND
        # the new raw identities. Use add_trusted to skip identity dedupe
        # entirely (the SQL iter is already canonical).
        for trade in aggtrade_cache.iter_deduped_trades_range(
            self.market, self.symbol, int(new_step_start_ms), int(endpoint_ts_ms)
        ):
            new_acc.add_trusted(trade)
        # Install the refreshed snapshot (preserves ladder).
        self.apply_streaming_backfill(
            new_acc,
            market=self.market,
            symbol=self.symbol,
            ladder_start_ms=ladder_start,
            step_start_ms=int(new_step_start_ms),
            endpoint_ts_ms=int(endpoint_ts_ms),
            preserve_ladder_accumulator=True,
        )
        return True

    def buffer_live_trade(
        self,
        *,
        ts_ms: int,
        price: float,
        qty: float,
        storage_id: int,
        real_trade_id: int,
        id_domain: str,
    ) -> None:
        """Buffer a WS trade that arrived during streaming backfill."""
        if len(self._live_buffer) >= self._live_buffer_cap:
            # Drop oldest to keep memory bounded.
            self._live_buffer = self._live_buffer[-self._live_buffer_cap // 2:]
        self._live_buffer.append(
            {
                "ts_ms": int(ts_ms),
                "price": float(price),
                "qty": float(qty),
                "storage_id": int(storage_id),
                "real_trade_id": int(real_trade_id),
                "id_domain": str(id_domain),
            }
        )

    def ingest_live_trade(
        self,
        *,
        ts_ms: int,
        price: float,
        qty: float,
        storage_id: int,
        real_trade_id: int,
        id_domain: str,
    ) -> bool:
        """Incrementally update the streaming snapshot for a NEW live trade.

        Returns True if the trade was incorporated. False if:
          - no streaming snapshot is installed
          - the trade is at or before the snapshot's endpoint
          - the trade does not fall inside the current window identity
        """
        acc = self._streamed_acc
        if acc is None:
            return False
        try:
            agg = AggTrade(
                agg_id=int(storage_id),
                price=float(price),
                qty=float(qty),
                ts_ms=int(ts_ms),
                first_trade_id=int(real_trade_id),
                last_trade_id=int(real_trade_id),
                id_domain=str(id_domain),
            )
        except (TypeError, ValueError):
            return False
        result = acc.live_add(agg)
        if result:
            try:
                self._streamed_result = acc.finalize(
                    window_end_ms=int(self._streamed_identity[4])
                )
            except Exception:
                pass
        return bool(result)

    def _validate_streamed_identity(self, *, allow_step_mismatch: bool = False) -> bool:
        """True iff the persisted streaming snapshot still matches current state.

        ``allow_step_mismatch=True`` is used for the partial-validity case
        after a step activation: the ladder window identity is still valid,
        only the step window is stale (and ``is_step_dirty()`` flags it).
        """
        ident = self._streamed_identity
        if ident is None:
            return False
        if ident[0] != str(self.market):
            return False
        if ident[1] != str(self.symbol):
            return False
        if self.ladder_start_ms is None or self.step_start_ms is None:
            return False
        if int(ident[2]) != int(self.ladder_start_ms):
            return False
        if not allow_step_mismatch:
            if int(ident[3]) != int(self.step_start_ms):
                return False
        return True

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
        if self._strict_id_gaps and self._gap_count > 0 and self.handoff_status != "live_ready":
            return False, STATUS_INCOMPLETE, f"{self._gap_count} missing agg trade ids"
        # Allow live_ready with minor gaps but flag detail; still complete if floor OK
        # Strict: any gap in the ladder window id span marks incomplete
        if self._strict_id_gaps and self._gap_count > 0:
            return False, STATUS_INCOMPLETE, f"gapped agg trade ids count={self._gap_count}"
        return True, STATUS_COMPLETE, self.detail or "complete"

    def compute(self, *, now_ms: Optional[int] = None) -> MetricDisplay:
        # Streaming-backed fast path: validate window identity strictly,
        # but tolerate step mismatch when step_dirty is set (only ladder
        # is valid in that case; step falls back to OHLC).
        allow_step = self.is_step_dirty()
        if (
            getattr(self, "_streamed_result", None) is not None
            and self._validate_streamed_identity(allow_step_mismatch=allow_step)
            and not self._by_id
        ):
            acc = self._streamed_acc
            try:
                if acc is not None and self._streamed_identity is not None:
                    self._streamed_result = acc.finalize(
                        window_end_ms=int(self._streamed_identity[4])
                    )
            except Exception:
                pass
            result = self._streamed_result
            if result is None:
                pass  # fall through to legacy path
            else:

                def _profile_top_bins(prof):
                    if prof is None:
                        return []
                    items = sorted(prof.vols.items(), key=lambda kv: (-kv[1], kv[0]))
                    return items[:5]

                ladder_ok = bool(result.ladder_coverage.complete)
                # If step was marked dirty (new step activation since install),
                # step metrics must come from OHLC, not from the stale snapshot.
                if self.is_step_dirty():
                    step_ok = False
                else:
                    step_ok = bool(result.step_coverage.complete)
                source = SOURCE_AGGTRADE if ladder_ok and step_ok else SOURCE_OHLC
                if ladder_ok != step_ok:
                    source = "MIXED"
                ladder_val = ladder_vah = None
                if ladder_ok and result.ladder_profile is not None:
                    ladder_val, ladder_vah = trade_value_area(
                        result.ladder_profile, value_area_pct=0.70
                    )
                return MetricDisplay(
                    source=source,
                    ladder_metric_source=SOURCE_AGGTRADE if ladder_ok else SOURCE_OHLC,
                    step_metric_source=SOURCE_AGGTRADE if step_ok else SOURCE_OHLC,
                    ladder_aggtrade_status=result.ladder_coverage.status,
                    step_aggtrade_status=(
                        result.step_coverage.status if not self.is_step_dirty()
                        else STATUS_LOADING
                    ),
                    ladder_vwap=result.ladder_vwap,
                    step_vwap=result.step_vwap if step_ok else None,
                    ladder_poc=result.ladder_poc,
                    step_poc=result.step_poc if step_ok else None,
                    ladder_val=ladder_val,
                    ladder_vah=ladder_vah,
                    ladder_status=result.ladder_coverage.status,
                    step_status=(
                        result.step_coverage.status if not self.is_step_dirty()
                        else STATUS_LOADING
                    ),
                    ladder_trade_count=result.ladder_trade_count,
                    step_trade_count=result.step_trade_count if step_ok else 0,
                    ladder_total_qty=result.ladder_total_qty,
                    step_total_qty=result.step_total_qty if step_ok else 0.0,
                    handoff_status=self.handoff_status,
                    gap_count=self._gap_count,
                    top_ladder_bins=_profile_top_bins(result.ladder_profile),
                    top_step_bins=(
                        []
                        if self.is_step_dirty()
                        else _profile_top_bins(result.step_profile)
                    ),
                    detail=result.ladder_coverage.detail,
                )

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

        # If streaming snapshot was invalidated (no _streamed_acc) and the
        # legacy _by_id is empty too, fall back to OHLC rather than report
        # AGGTRADE from an empty store. This happens after a step/P0
        # transition invalidates the snapshot before a fresh backfill.
        if (
            getattr(self, "_streamed_acc", None) is None
            and not self._by_id
            and not getattr(self, "_streamed_result", None)
        ):
            return MetricDisplay(
                source=SOURCE_OHLC,
                ladder_metric_source=SOURCE_OHLC,
                step_metric_source=SOURCE_OHLC,
                ladder_aggtrade_status=STATUS_LOADING,
                step_aggtrade_status=STATUS_LOADING,
                ladder_status=STATUS_LOADING,
                step_status=STATUS_LOADING,
                handoff_status=self.handoff_status or "backfilling",
                detail="OHLC approximation (no actual-trade data)",
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
