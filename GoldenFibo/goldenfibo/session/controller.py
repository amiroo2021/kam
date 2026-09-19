"""SessionController — LIVE / BACKTEST / REPLAY_TO_LIVE over one GoldenFiboEngine."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from ..api import schemas
from ..engine.config import EngineConfig, OhlcResolveMode, Side
from ..engine.engine import GoldenFiboEngine
from ..engine.events import DomainEvent, MarketEvent, MarketEventKind
from ..live.price_path import apply_price_to_engine
from ..marketdata import binance_public as bn
from ..marketdata.aggtrade_cache import AggTradeCache
from ..marketdata.symbols import canonical_binance_symbol
from ..marketdata.binance_klines_range import inclusive_open_range_fetch_end
from ..marketdata.kline_cache import CachePolicy, CachedBinanceKlineSource, KlineCache, fetch_range_cached
from ..marketdata.timeframes import (
    interval_ms,
    ms_to_iso,
    parse_iso_to_ms,
    require_aligned,
    validate_interval,
)
from ..metrics import OhlcvBar, bar_from_binance_kline
from ..metrics.trade_store import TradeMetricStore
from ..marketdata.aggtrade_cache import AggTradeCache
from ..marketdata.binance_agg_trades import fetch_agg_trades_range
from ..metrics.backfill_scheduler import (
    DesiredWindow,
    LatestWinsScheduler,
)
from .event_log import EventLog
from .runner import apply_ohlc_page, new_engine_for_run, run_ohlc_on_engine
from .types import SessionMode, SessionPhase

CHART_CANDLE_LIMIT = 2500  # browser chart window; engine reconstructs full history

logger = logging.getLogger("goldenfibo.controller")


def _normalize_market(market: Optional[str]) -> str:
    m = str(market or "spot").lower()
    if m in {"futures", "future", "usdm"}:
        return "futures"
    return "spot"


class SessionController:
    """Backend-owned multi-mode session. One engine object for REPLAY→LIVE handoff."""

    def __init__(self) -> None:
        self.mode = SessionMode.LIVE
        self.phase = SessionPhase.IDLE
        self.symbol = "BTCUSDT"
        self.timeframe = "1m"
        self.market = "spot"
        self.side = Side.BUY
        self.percentage = Decimal("0.001")
        self.ohlc_mode = OhlcResolveMode.LEGACY
        self.run_id = ""
        self.symbol = canonical_binance_symbol(self.symbol)
        self.engine = GoldenFiboEngine(
            EngineConfig(side=self.side, percentage=self.percentage, symbol=self.symbol)
        )
        self.event_log = EventLog()
        self.klines: List[list] = []
        self.bars: List[OhlcvBar] = []
        self.chart_candles: List[Dict[str, Any]] = []
        self.last_price: Optional[str] = None
        self.recent_domain: List[DomainEvent] = []
        self.ambiguity_count = 0
        self.bars_processed = 0
        self.progress: Dict[str, Any] = {}
        self.error: Optional[str] = None
        self.start_ms: Optional[int] = None
        self.end_ms: Optional[int] = None
        self.fence_ms: Optional[int] = None
        self.last_hist_open_ms: Optional[int] = None
        self._p0_seeded = False
        self._ws_task: Optional[asyncio.Task] = None
        self._run_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._clients: Set[Any] = set()
        self._lock = asyncio.Lock()
        self._trade_buffer: List[Tuple[int, Decimal]] = []
        self._buffering = False
        self._live_enabled = False
        self.feed_status = "idle"
        self.kline_cache = KlineCache()
        self.cache_policy = CachePolicy.AUTO
        self.kline_source = CachedBinanceKlineSource(cache=self.kline_cache, policy=self.cache_policy)
        self.cache_stats: Dict[str, Any] = {}
        self.history_limit_live = 300
        self.trade_store = TradeMetricStore(tick_size=0.01)
        self.aggtrade_cache = AggTradeCache()
        self._trade_backfill_task: Optional[asyncio.Task] = None
        self._last_metric_broadcast_ms: int = 0
        self._metric_broadcast_min_interval_ms: int = 500
        # Full aggTrade buffer while hist replay (id, price, qty, ts)
        self._agg_trade_buffer: List[Dict[str, Any]] = []
        self._ws_coverage_edge_ms: Optional[int] = None
        # Explicit enable for LIVE AGGTRADE metrics (avoids REST in unit tests)
        self._aggtrade_metrics_enabled: bool = False
        # Injectable fetches for tests
        self._agg_trades_fetch = fetch_agg_trades_range
        self._agg_archive_fetch = self.aggtrade_cache.fetch_archive_range
        # Latest-wins/coalescing backfill scheduler. Built lazily on first
        # use so that ``self._stream_backfill_for_request`` (a bound method)
        # is available when the worker closure is constructed.
        self._backfill_scheduler: Optional[LatestWinsScheduler] = None
        # Counter for tracked number of streaming backfill requests via
        # the scheduler (kept for compatibility with existing tests that
        # read controller state).
        self._stream_gen_token: Optional[Tuple[Any, ...]] = None

    def register_client(self, ws: Any) -> None:
        self._clients.add(ws)

    def unregister_client(self, ws: Any) -> None:
        self._clients.discard(ws)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        dead = []
        data = json.dumps(message, default=str)
        for ws in list(self._clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def broadcast_snapshot(self) -> None:
        await self.broadcast(self.snapshot_dict())

    async def _set_phase(self, phase: SessionPhase, **progress: Any) -> None:
        self.phase = phase
        if progress:
            self.progress.update(progress)
        await self.broadcast(
            {
                "v": 1,
                "type": "phase",
                "phase": phase.value,
                "mode": self.mode.value,
                "progress": dict(self.progress),
                "ambiguity_count": self.ambiguity_count,
                "bars_processed": self.bars_processed,
            }
        )

    def snapshot_dict(self) -> Dict[str, Any]:
        prefer = self._prefer_aggtrade_metrics()
        payload = schemas.build_state_payload(
            mode=self.mode.value,
            symbol=self.symbol,
            timeframe=self.timeframe,
            side=self.side.value,
            percentage=str(self.percentage),
            price=self.last_price,
            engine=self.engine,
            candles=list(self.chart_candles),
            bars=list(self.bars),
            recent_domain=list(self.recent_domain[-80:]),
            connected=self.phase
            in (
                SessionPhase.LIVE,
                SessionPhase.BACKTEST_DONE,
                SessionPhase.REPLAYING,
                SessionPhase.DOWNLOADING,
                SessionPhase.LOADING,
                SessionPhase.CATCHING_UP,
            )
            or self.feed_status in ("live", "seeded", "buffering"),
            metric_display=None,
            prefer_aggtrade=False,
        )
        payload.update(
            {
                "phase": self.phase.value,
                "run_id": self.run_id,
                "market": self.market,
                "progress": dict(self.progress),
                "ambiguity_count": self.ambiguity_count,
                "bars_processed": self.bars_processed,
                "event_summary": self.event_log.summary_counts(),
                "ohlc_resolver": self.ohlc_mode.value,
                "cache": dict(self.cache_stats),
                "start_ms": self.start_ms,
                "end_ms": self.end_ms,
                "fence_ms": self.fence_ms,
                "error": self.error,
                "feed_status": self.feed_status,
                "note_historical": (
                    "LEGACY OHLC uses deterministic intrabar assumptions; "
                    "not tick-perfect vs continuous aggTrade."
                    if not prefer
                    else "LIVE metrics: aggTrade VAP POC + trade VWAP when coverage COMPLETE."
                ),
            }
        )
        if prefer:
            payload.update(self._metric_fragment())
        return payload

    def _prefer_aggtrade_metrics(self) -> bool:
        """LIVE chart path prefers AGGTRADE; BACKTEST/historical replay use OHLC."""
        if not self._aggtrade_metrics_enabled:
            return False
        if self.mode is SessionMode.BACKTEST:
            return False
        if self.phase is SessionPhase.LIVE:
            return True
        if self.mode is SessionMode.LIVE and self._p0_seeded:
            return True
        if self.mode is SessionMode.REPLAY_TO_LIVE and self._live_enabled:
            return True
        return False

    def _current_engine_window(self) -> Optional[DesiredWindow]:
        return self._build_desired_window_from_engine(kind="full")

    def _installed_streamed_window(self) -> Optional[DesiredWindow]:
        identity = self.trade_store.streamed_identity()
        if identity is None:
            return None
        market, symbol, ladder_start_ms, step_start_ms, endpoint_ts_ms = identity
        return DesiredWindow(
            market=str(market),
            symbol=str(symbol),
            ladder_start_ms=int(ladder_start_ms),
            step_start_ms=int(step_start_ms),
            endpoint_ts_ms=int(endpoint_ts_ms),
            session_id=str(self.run_id) if self.run_id else "unknown",
        )

    def _request_kind_for_window(self, latest: Optional[DesiredWindow], installed: Optional[DesiredWindow]) -> Optional[str]:
        if latest is None:
            return None
        if installed is not None:
            same_session = latest.session_id == installed.session_id
            same_market = latest.market == installed.market
            same_symbol = latest.symbol == installed.symbol
            same_ladder = latest.ladder_start_ms == installed.ladder_start_ms
            same_step = latest.step_start_ms == installed.step_start_ms
            if same_session and same_market and same_symbol and same_ladder and same_step:
                return None
            if same_session and same_market and same_symbol and same_ladder:
                return "step"
        return "full"

    def _sync_trade_windows_from_engine(self, *, schedule_backfill: bool = False) -> bool:
        """Update trade-store windows from engine legs and request backfill.

        FULL is requested when the current installed streamed identity is
        absent or diverges in market, symbol, ladder, or session. STEP_ONLY
        is requested only when the same session/market/symbol/ladder is
        installed and only step_start moved forward.
        """
        st = self.engine.state
        if not st.legs:
            return False
        ladder = int(st.legs[0].ts_ms)
        step = int(st.legs[-1].ts_ms)
        prev_ladder = self.trade_store.ladder_start_ms
        prev_step = self.trade_store.step_start_ms
        new_p0 = prev_ladder is None or ladder > int(prev_ladder)
        new_step = prev_step is None or step > int(prev_step)
        self.trade_store.set_windows(ladder_start_ms=ladder, step_start_ms=step)
        if not self._prefer_aggtrade_metrics():
            return new_p0
        latest = self._current_engine_window()
        installed = self._installed_streamed_window()
        kind = self._request_kind_for_window(latest, installed)
        if kind is None:
            return new_p0
        if schedule_backfill and kind == "full":
            self._request_backfill_now(kind="full")
            return new_p0
        if kind == "step" and new_step:
            self._request_backfill_now(kind="step")
            return new_p0
        if kind == "full" and (new_p0 or installed is None or not self.trade_store.has_streaming_snapshot()):
            self._request_backfill_now(kind="full")
            return new_p0
        return new_p0

    async def _drive_scheduled_backfill(self, request: DesiredWindow, *, kind: str) -> None:
        scheduler = self._get_backfill_scheduler()
        await scheduler.request_window(request, kind=kind)
        await scheduler.wait_idle()

    def _request_backfill_now(self, *, kind: str) -> None:
        """Liveness helper: ask the scheduler to rebuild for current window.

        The controller keeps a task handle so existing run paths can wait
        for backfill convergence in tests and during startup.
        """
        request = self._build_desired_window_from_engine(kind=kind)
        if request is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            asyncio.run(self._stream_backfill_for_request(request))
            return
        self._trade_backfill_task = loop.create_task(
            self._drive_scheduled_backfill(request, kind=kind),
            name="gf-trade-backfill",
        )

    async def _request_backfill_now_async(self, *, kind: str) -> None:
        """Async variant of ``_request_backfill_now``.

        Used from live price paths where a coroutine is already
        running and we can simply ``await`` the scheduler.
        """
        request = self._build_desired_window_from_engine(kind=kind)
        if request is None:
            return
        scheduler = self._get_backfill_scheduler()
        await scheduler.request_window(request, kind=kind)

    def _schedule_trade_backfill(self) -> None:
        """Schedule a backfill for the current engine window.

        Delegates to the latest-wins scheduler. The scheduler enforces:

        - At most one worker in flight at any time (coalescing all
          intermediate window changes into the latest pending request).
        - P0 transitions supersede any pending step-only work.
        - Step-only transitions preserve the existing valid ladder
          accumulator.

        This method is fire-and-forget; it does not block the caller.
        The full orchestrator (gen_token check, apply_streaming_backfill,
        catchup_from_persistent) lives inside the worker coroutine and
        runs on a thread via ``asyncio.to_thread``. Stale-worker
        rejection (invariant #1) is preserved there.
        """
        request = self._build_desired_window_from_engine(kind="full")
        if request is None:
            return
        scheduler = self._get_backfill_scheduler()
        # Fire-and-forget: launch the coroutine on the running loop.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            # No loop: caller is sync. Fall back to a direct worker call.
            asyncio.run(self._stream_backfill_for_request(request))
            return
        loop.create_task(scheduler.request_window(request, kind="full"))

    async def _run_trade_backfill(self) -> None:
        """Worker coroutine invoked by the latest-wins scheduler.

        Each invocation is tied to a captured DesiredWindow (see
        ``self._build_desired_window_from_engine``). The worker streams
        the current ladder/step window from the persistent cache up to
        the captured endpoint and atomically installs. After every
        worker outcome (success, stale, or failure) the liveness
        invariant requires the controller to call
        ``scheduler.reevaluate_after_worker(...)``.

        Strict stale-worker protection (invariant #1) lives here:
        ``apply_streaming_backfill`` will silently no-op if the
        trade_store's installed identity no longer matches the
        request; we then drop the accumulator without re-using it.
        """
        # Backwards-compat: if invoked directly (not via the scheduler),
        # derive a DesiredWindow from the engine state, run, then exit.
        st = self.engine.state
        if not st.legs:
            return
        request = self._build_desired_window_from_engine(kind="full")
        if request is None:
            return
        await self._stream_backfill_for_request(request)

    def _build_desired_window_from_engine(
        self, *, kind: str = "full"
    ) -> Optional[DesiredWindow]:
        """Build a DesiredWindow from the current engine state.

        Returns ``None`` if the engine has no legs yet (no cycle seeded).
        Uses ``self.run_id`` as the session id; this is set during
        ``start_run`` and changes on every new session, ensuring
        inv11 (session invalidation works correctly).
        """
        st = self.engine.state
        if not st.legs:
            return None
        ladder = int(st.legs[0].ts_ms)
        step = int(st.legs[-1].ts_ms)
        endpoint_ms = int(time.time() * 1000)
        return DesiredWindow(
            market=str(self.market),
            symbol=str(self.symbol),
            ladder_start_ms=ladder,
            step_start_ms=step,
            endpoint_ts_ms=endpoint_ms,
            session_id=str(self.run_id) if self.run_id else "unknown",
        )

    def _get_backfill_scheduler(self) -> LatestWinsScheduler:
        """Lazily build the latest-wins scheduler."""
        sched = self._backfill_scheduler
        if sched is not None:
            return sched
        sched = LatestWinsScheduler(
            worker_fn=self._scheduler_worker_fn,
            on_idle=self._scheduler_on_idle,
        )
        self._backfill_scheduler = sched
        return sched

    async def _scheduler_worker_fn(self, request: DesiredWindow) -> None:
        """Bound worker coroutine invoked by the scheduler.

        Runs ensure_coverage, the streaming pass, install (or
        stale-rejection), and the persistent catch-up — exactly the
        same logic as the legacy ``_run_trade_backfill`` but driven
        by a captured ``DesiredWindow`` rather than the live engine
        state.
        """
        try:
            await self._stream_backfill_for_request(request)
        except Exception as exc:
            logger.warning("backfill worker failed: %s", exc)

    async def _scheduler_on_idle(self) -> None:
        """Called by the scheduler when it has no work to do.

        Triggers a metric fragment broadcast so the UI sees the latest
        state when churn stops. This keeps the displayed source in
        sync with what the trade_store holds.
        """
        # No-op in tests; in production the broadcast is owned by the
        # session loop. We only touch this from the liveness invariant
        # path; broadcast happens via _apply_live_price on the next
        # tick.
        return None

    async def _stream_backfill_for_request(self, request: DesiredWindow) -> None:
        """Run the streaming backfill for a captured DesiredWindow.

        - Lazily ensures coverage from the cache (REST 2-day window +
          archive; existing semantics — invariant preserved).
        - Streams deduped trades via iter_deduped_trades_range over
          ``[request.ladder_start_ms, request.endpoint_ts_ms]`` (full)
          or ``[request.step_start_ms, request.endpoint_ts_ms]``
          (step-only). The persistent cache is the authoritative
          source; no in-memory live buffer is reintroduced.
        - Captures a ``gen_token`` from the captured request (NOT
          the live engine state — that would defeat the whole
          point of scheduling).
        - Calls ``apply_streaming_backfill`` which enforces a strict
          5-tuple identity check and silently rejects any
          window-mismatched accumulator. Stale rejection is the
          source of the liveness loop.
        - On stale reject, drops the accumulator without
          ``catchup_from_persistent`` (because that would smear the
          wrong-window trades in).
        - Calls ``scheduler.reevaluate_after_worker(...)`` once to
          satisfy the liveness invariant (max 1 worker, latest wins,
          coalescing preserved).
        """
        if self._stopped():
            return
        ladder_start_ms = int(request.ladder_start_ms)
        step_start_ms = int(request.step_start_ms)
        endpoint_ms = int(request.endpoint_ts_ms)
        installed_identity = self._installed_streamed_window()
        full_rebuild = (
            ladder_start_ms == step_start_ms
            or self.trade_store._streamed_acc is None
            or installed_identity is None
            or installed_identity.session_id != request.session_id
            or installed_identity.market != request.market
            or installed_identity.symbol != request.symbol
            or installed_identity.ladder_start_ms != ladder_start_ms
        )
        start_ms = ladder_start_ms if full_rebuild else step_start_ms
        # Capture identity used for stale-worker protection. This is
        # the captured-request identity, NOT the live engine state.
        # The apply_streaming_backfill method enforces strict
        # 5-tuple matching against trade_store.{market,symbol,
        # ladder_start_ms,step_start_ms}.
        gen_token = (
            str(request.market),
            str(request.symbol),
            ladder_start_ms,
            step_start_ms,
            endpoint_ms,
        )
        self._stream_gen_token = gen_token
        # Pre-sync trade_store windows so apply_streaming_backfill has
        # something to match against. Existing full-rebuild code path
        # uses set_windows here; step-only path leaves ladder_start
        # unchanged.
        self.trade_store.market = request.market
        self.trade_store.symbol = request.symbol
        self.trade_store.set_windows(
            ladder_start_ms=ladder_start_ms,
            step_start_ms=step_start_ms,
        )
        self.trade_store.handoff_status = "backfilling"
        self.trade_store.backfill_complete = False
        try:
            ensure_result = await asyncio.to_thread(
                self.aggtrade_cache.ensure_coverage,
                request.market,
                request.symbol,
                start_ms,
                endpoint_ms,
                rest_fetcher=self._agg_trades_fetch,
                archive_fetcher=self._agg_archive_fetch,
                now_ms=endpoint_ms,
                pause_s=0.03,
            )
            if self._stopped():
                return
            if ensure_result.covered:
                self.trade_store.mark_ws_attached()
            latest_before_install = self._current_engine_window()
            installed_before_install = self._installed_streamed_window()
            # Streaming pass: ladder+step or step-only depending on the
            # request kind. The streaming internal pass already dedupes
            # via the cache iterator; _stream_metric_backfill runs the
            # full-ladder variant. Step-only re-stream delegates to
            # trade_store.reconcile_step_window which preserves the
            # installed ladder accumulator.
            if full_rebuild:
                # Treat as full rebuild (the original cycle anchor).
                streamed = await asyncio.to_thread(
                    self._stream_metric_backfill,
                    ladder_start_ms,
                    endpoint_ms,
                )
                if self._stopped():
                    return
                latest_now = self._current_engine_window()
                if latest_now is None or latest_now.session_id != request.session_id:
                    return
                if (
                    latest_before_install is not None
                    and latest_now is not None
                    and latest_before_install.same_identity(latest_now)
                    is False
                    and latest_now.same_identity(request) is False
                ):
                    return
                self.trade_store.apply_streaming_backfill(
                    streamed,
                    market=request.market,
                    symbol=request.symbol,
                    ladder_start_ms=start_ms,
                    step_start_ms=step_start_ms,
                    endpoint_ts_ms=endpoint_ms,
                )
            else:
                # Step-only re-stream: preserves the valid ladder
                # accumulator. reconcile_step_window returns False if
                # no snapshot is installed yet (worker should fall back
                # to a full rebuild by re-requesting via the
                # scheduler's reevaluate path).
                latest_now = self._current_engine_window()
                if latest_now is None or latest_now.session_id != request.session_id:
                    return
                if (
                    latest_now.market != request.market
                    or latest_now.symbol != request.symbol
                    or latest_now.ladder_start_ms != request.ladder_start_ms
                    or latest_now.step_start_ms != request.step_start_ms
                ):
                    return
                ok = await asyncio.to_thread(
                    self.trade_store.reconcile_step_window,
                    self.aggtrade_cache,
                    new_step_start_ms=step_start_ms,
                    endpoint_ts_ms=endpoint_ms,
                )
                if not ok:
                    pass
            # Lossless catch-up: pull anything persisted into SQLite
            # between endpoint_ms and now via (ts, storage_id) keyset.
            # The persistent cache is the authoritative source; no
            # in-memory buffer is reintroduced.
            catchup_now = int(time.time() * 1000)
            await asyncio.to_thread(
                self.trade_store.catchup_from_persistent,
                self.aggtrade_cache,
                max_ts_ms=catchup_now,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("aggTrade backfill worker failed: %s", exc)
            try:
                self.trade_store.note_backfill_failure(str(exc))
            except Exception:
                pass
        finally:
            scheduler = self._backfill_scheduler
            if scheduler is not None:
                await scheduler.reevaluate_after_worker(
                    latest_window_provider=self._current_engine_window,
                    installed_window_provider=self._installed_streamed_window,
                )

    def _stream_metric_backfill(self, start_ms: int, end_ms: int):
        """One-pass streaming ladder+step VWAP/VAP computation.

        Streams deduped trades from the cache and accumulates into scalar
        VWAP + per-window price bins. Memory is bounded by raw-window size
        (small in production) + aggregate interval list + price bin count.
        No full result list is materialized.

        For small datasets (count below ``materialize_threshold``) we also
        populate the legacy in-memory ``TradeStore._by_id`` for backward
        compatibility with unit tests and tools that introspect the store.

        Returns the ``StreamMetricsAccumulator`` (NOT a finalized result).
        The caller is responsible for calling ``apply_streaming_backfill``
        with proper identity and stale-worker protection.
        """
        from ..metrics import trade_vap

        ladder_start = int(start_ms)
        step_start = int(self.trade_store.step_start_ms or ladder_start)
        acc = trade_vap.StreamMetricsAccumulator(
            ladder_start_ms=ladder_start,
            step_start_ms=step_start,
            bin_size=self.trade_store.tick_size,
        )
        first_ts_ms: Optional[int] = None
        small_buffer: List[trade_vap.AggTrade] = []
        materialize_threshold = 100_000
        # Use add_trusted: the cache iter has already deduped at the SQL
        # level, so we don't need per-trade identity dedupe here. This keeps
        # the in-memory _seen_keys set bounded to the live-edge size, not
        # the full historical 1.85M trades.
        for trade in self.aggtrade_cache.iter_deduped_trades_range(
            self.market, self.symbol, ladder_start, end_ms
        ):
            if first_ts_ms is None:
                first_ts_ms = int(trade.ts_ms)
            acc.add_trusted(trade)
            if len(small_buffer) < materialize_threshold:
                small_buffer.append(trade)
        if len(small_buffer) < materialize_threshold:
            for t in small_buffer:
                self.trade_store.ingest(t)
        # Persist the streaming accumulator so future step filter changes can
        # be re-finalized without re-streaming the full window.
        self._streamed_acc = acc
        return acc

    def _metric_fragment(self) -> Dict[str, Any]:
        st = self.engine.state
        ohlc_md = schemas.ohlc_metric_display(
            self.bars,
            ladder_ts=st.legs[0].ts_ms if st.legs else None,
            step_ts=st.legs[-1].ts_ms if st.legs else None,
        )
        ohlc_fields = ohlc_md.as_payload_fields()
        if not self._prefer_aggtrade_metrics():
            # Historical / BACKTEST / REPLAY: use canonical OHLC display exactly as before.
            return ohlc_fields

        live_md = self.trade_store.compute(now_ms=int(time.time() * 1000))
        live_fields = live_md.as_payload_fields()

        def choose(key: str, source_key: str, status_key: str, agg_status_key: str):
            # Prefer complete aggTrade; otherwise preserve visible OHLC approximation.
            if live_fields.get(status_key) == "COMPLETE" and live_fields.get(key) is not None:
                ohlc_fields[key] = live_fields[key]
                ohlc_fields[source_key] = "AGGTRADE"
                ohlc_fields[status_key] = "COMPLETE"
                ohlc_fields[agg_status_key] = live_fields.get(agg_status_key, "COMPLETE")
            else:
                ohlc_fields[source_key] = "OHLC_APPROXIMATION"
                ohlc_fields[status_key] = "COMPLETE"
                ohlc_fields[agg_status_key] = live_fields.get(agg_status_key, "INCOMPLETE_TRADE_HISTORY")

        # Ladder window: VWAP/POC/VAL/VAH. Step window: VWAP/POC only.
        choose("ladder_vwap", "ladder_metric_source", "ladder_metric_status", "ladder_aggtrade_status")
        choose("ladder_poc", "ladder_metric_source", "ladder_metric_status", "ladder_aggtrade_status")
        choose("ladder_val", "ladder_metric_source", "ladder_metric_status", "ladder_aggtrade_status")
        choose("ladder_vah", "ladder_metric_source", "ladder_metric_status", "ladder_aggtrade_status")
        choose("active_step_vwap", "step_metric_source", "step_metric_status", "step_aggtrade_status")
        choose("active_step_poc", "step_metric_source", "step_metric_status", "step_aggtrade_status")
        ladder_src = ohlc_fields.get("ladder_metric_source")
        step_src = ohlc_fields.get("step_metric_source")
        ohlc_fields["metric_source"] = ladder_src if ladder_src == step_src else "MIXED"
        ohlc_fields["metrics_handoff_status"] = live_fields.get("metrics_handoff_status", self.trade_store.handoff_status)
        ohlc_fields["metrics_gap_count"] = live_fields.get("metrics_gap_count", 0)
        ohlc_fields["ladder_trade_count"] = live_fields.get("ladder_trade_count", 0)
        ohlc_fields["step_trade_count"] = live_fields.get("step_trade_count", 0)
        ohlc_fields["ladder_total_qty"] = live_fields.get("ladder_total_qty", 0.0)
        ohlc_fields["step_total_qty"] = live_fields.get("step_total_qty", 0.0)
        ohlc_fields["metrics_detail"] = live_fields.get("metrics_detail", ohlc_fields.get("metrics_detail", "OHLC approximation"))
        return ohlc_fields

    async def stop(self) -> None:
        self._stop.set()
        self._live_enabled = False
        self._buffering = False
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):
                pass
            self._run_task = None
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ws_task = None
        self.feed_status = "stopped"
        # Always mark stopped when stop() is invoked (new runs set LOADING next).
        if self.phase != SessionPhase.ERROR:
            self.phase = SessionPhase.STOPPED

    async def start_run(
        self,
        *,
        mode: SessionMode = SessionMode.LIVE,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        side: Optional[Side] = None,
        market: Optional[str] = None,
        percentage: Optional[Decimal] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> Dict[str, Any]:
        await self.stop()
        self._stop.clear()
        self.error = None
        self.progress = {}
        self.event_log.clear()
        self.recent_domain.clear()
        self._trade_buffer.clear()
        self._agg_trade_buffer.clear()
        self.trade_store.clear()
        self._aggtrade_metrics_enabled = False
        self.last_price = None
        if self._trade_backfill_task and not self._trade_backfill_task.done():
            self._trade_backfill_task.cancel()
            self._trade_backfill_task = None
        if self._backfill_scheduler is not None:
            try:
                await self._backfill_scheduler.invalidate_session(self.run_id or "")
            except Exception:
                logger.exception("failed to invalidate old backfill session")
        self.ambiguity_count = 0
        self.bars_processed = 0
        self.fence_ms = None
        self.last_hist_open_ms = None
        self.klines.clear()
        self.bars.clear()
        self.chart_candles.clear()
        self._p0_seeded = False
        self._live_enabled = False
        self._buffering = False
        self._stream_gen_token = None

        if symbol:
            self.symbol = canonical_binance_symbol(symbol)
        if timeframe:
            self.timeframe = validate_interval(timeframe)
        if market is not None:
            self.market = _normalize_market(market)
        if side is not None:
            self.side = side
        if percentage is not None:
            self.percentage = percentage
        self.mode = mode
        self.run_id = uuid.uuid4().hex[:12]
        self.phase = SessionPhase.LOADING
        self.symbol = canonical_binance_symbol(self.symbol)
        self.engine = GoldenFiboEngine(
            EngineConfig(side=self.side, percentage=self.percentage, symbol=self.symbol)
        )

        if mode is SessionMode.LIVE:
            self.start_ms = None
            self.end_ms = None
            self._run_task = asyncio.create_task(self._run_live_now(), name="gf-live")
        elif mode is SessionMode.BACKTEST:
            if not start_time or not end_time:
                raise ValueError("BACKTEST requires start_time and end_time (UTC, timeframe-aligned)")
            self.start_ms = require_aligned(parse_iso_to_ms(start_time), self.timeframe, "start_time")
            self.end_ms = require_aligned(parse_iso_to_ms(end_time), self.timeframe, "end_time")
            if self.end_ms <= self.start_ms:
                raise ValueError("end_time must be after start_time")
            self._run_task = asyncio.create_task(self._run_backtest(), name="gf-backtest")
        elif mode is SessionMode.REPLAY_TO_LIVE:
            if not start_time:
                raise ValueError("REPLAY_TO_LIVE requires start_time (UTC, timeframe-aligned)")
            self.start_ms = require_aligned(parse_iso_to_ms(start_time), self.timeframe, "start_time")
            self.end_ms = None
            self._run_task = asyncio.create_task(self._run_replay_to_live(), name="gf-replay-live")
        else:
            raise ValueError(str(mode))
        return self.snapshot_dict()

    async def start(self) -> None:
        await self.start_run(mode=SessionMode.LIVE)

    async def reconfigure(self, **kwargs: Any) -> None:
        side = kwargs.get("side")
        pct = kwargs.get("percentage")
        await self.start_run(
            mode=SessionMode.LIVE,
            side=side if isinstance(side, Side) else (Side(side) if side else None),
            percentage=pct
            if isinstance(pct, Decimal)
            else (Decimal(str(pct)) if pct is not None else None),
            symbol=kwargs.get("symbol"),
            timeframe=kwargs.get("timeframe"),
            market=kwargs.get("market"),
        )
        await self.broadcast_snapshot()


    def _stopped(self) -> bool:
        return self._stop.is_set()

    def _trim_chart(self) -> None:
        if len(self.chart_candles) > CHART_CANDLE_LIMIT:
            self.chart_candles = self.chart_candles[-CHART_CANDLE_LIMIT:]
        if len(self.klines) > CHART_CANDLE_LIMIT:
            self.klines = self.klines[-CHART_CANDLE_LIMIT:]

    def _trim_metric_bars(self) -> None:
        """Keep bars needed for current-cycle ladder/step metrics only."""
        st = self.engine.state
        if not st.legs:
            # keep a modest tail while seeding
            if len(self.bars) > CHART_CANDLE_LIMIT:
                self.bars = self.bars[-CHART_CANDLE_LIMIT:]
            return
        start_ms = int(st.legs[0].ts_ms)
        self.bars = [b for b in self.bars if int(getattr(b, "ts_ms", 0) or 0) >= start_ms]

    async def _hist_download_and_replay(
        self,
        *,
        start_ms: int,
        end_ms: int,
        closed_only_before_ms: int | None,
    ) -> tuple[int, int, int | None]:
        """Cache-first range fetch, then chunked apply to one engine.

        Returns (bars, ambiguity, last_open_ms).
        """
        step = interval_ms(self.timeframe)
        est = max(1, (int(end_ms) - int(start_ms)) // step)
        amb_total = 0
        last_open: int | None = None
        bars_done = 0

        await self._set_phase(
            SessionPhase.DOWNLOADING,
            from_t=ms_to_iso(start_ms),
            to_t=ms_to_iso(end_ms),
            bars_done=0,
            bars_est=est,
            bars_total=est,
            pages=0,
            pct=0.0,
            stage="loading_cache",
        )

        progress_box: dict = {"last": {}}

        def on_progress(info: dict) -> None:
            progress_box["last"] = dict(info)

        def do_fetch():
            return fetch_range_cached(
                canonical_binance_symbol(self.symbol),
                self.timeframe,
                start_ms,
                end_ms,
                cache=self.kline_cache,
                policy=self.cache_policy,
                closed_only_before_ms=closed_only_before_ms,
                fetch=getattr(self.kline_source, "fetch", None),
                base_url=getattr(self.kline_source, "base_url", None) or "https://api.binance.com",
                market=self.market,
                on_progress=on_progress,
            )

        fetch_fut = asyncio.get_event_loop().run_in_executor(None, do_fetch)
        while not fetch_fut.done():
            if self._stopped():
                # cannot cancel executor easily; wait briefly
                await asyncio.sleep(0.1)
                if not fetch_fut.done():
                    pass
                break
            info = progress_box.get("last") or {}
            stg = info.get("stage") or "loading_cache"
            st = info.get("stats") or {}
            done_hint = int(st.get("bars_from_cache", 0) or 0) + int(st.get("bars_downloaded", 0) or 0)
            await self._set_phase(
                SessionPhase.DOWNLOADING,
                from_t=ms_to_iso(start_ms),
                to_t=ms_to_iso(end_ms),
                bars_done=done_hint,
                bars_est=est,
                bars_total=est,
                pages=int(st.get("rest_pages", 0) or 0),
                pct=min(40.0, 40.0 * done_hint / max(1, est)),
                stage=stg,
                cache=st,
            )
            await asyncio.sleep(0.25)

        if self._stopped() and not fetch_fut.done():
            # still running — wait for completion to avoid thread leak, discard
            try:
                await asyncio.wait_for(asyncio.shield(fetch_fut), timeout=600)
            except Exception:
                pass
            return bars_done, amb_total, last_open

        result = await fetch_fut
        klines = result.klines
        self.cache_stats = result.stats.as_dict()
        if self._stopped():
            return 0, 0, None

        await self._set_phase(
            SessionPhase.DOWNLOADING,
            from_t=ms_to_iso(start_ms),
            to_t=ms_to_iso(end_ms),
            bars_done=len(klines),
            bars_est=est,
            bars_total=len(klines),
            pages=self.cache_stats.get("rest_pages", 0),
            pct=40.0,
            stage="cache_ready",
            cache=self.cache_stats,
        )

        await self._set_phase(
            SessionPhase.REPLAYING,
            from_t=ms_to_iso(start_ms),
            to_t=ms_to_iso(end_ms),
            bars_done=0,
            bars_est=len(klines),
            bars_total=len(klines),
            pct=40.0,
            stage="replaying",
            cache=self.cache_stats,
        )

        chunk = 1000
        total = len(klines)
        for i in range(0, total, chunk):
            if self._stopped():
                break
            page_rows = klines[i : i + chunk]

            def apply_page(rows=page_rows):
                r = apply_ohlc_page(
                    self.engine,
                    rows,
                    mode=self.ohlc_mode,
                    event_log=self.event_log,
                )
                return r.ambiguity_count, r.domain, r.bars_processed

            a_count, domain, nbar = await asyncio.to_thread(apply_page)
            amb_total += a_count
            bars_done += nbar
            if page_rows:
                last_open = int(page_rows[-1][0])

            async with self._lock:
                for k in page_rows:
                    self.klines.append(k)
                    self.bars.append(bar_from_binance_kline(k))
                new_cc = bn.bars_to_chart_candles(page_rows)
                self.chart_candles.extend(new_cc)
                self._trim_chart()
                self._trim_metric_bars()
                self.ambiguity_count = amb_total
                self.bars_processed = bars_done
                self.recent_domain.extend(domain[-50:])
                self._p0_seeded = self.engine.state.active
                if page_rows:
                    self.last_price = str(page_rows[-1][4])

            frac = bars_done / max(1, total)
            pct = 40.0 + 59.0 * frac
            await self._set_phase(
                SessionPhase.REPLAYING,
                from_t=ms_to_iso(start_ms),
                to_t=ms_to_iso(end_ms),
                bars_done=bars_done,
                bars_est=total,
                bars_total=total,
                pages=self.cache_stats.get("rest_pages", 0),
                pct=round(pct, 2),
                stage="replaying",
                cache=self.cache_stats,
                last_open_ms=last_open,
            )
            if i == 0 or (i // chunk) % 5 == 0 or bars_done >= total:
                await self.broadcast_snapshot()
            await asyncio.sleep(0)

        if self._stopped():
            return bars_done, amb_total, last_open

        await self._set_phase(
            SessionPhase.REPLAYING,
            from_t=ms_to_iso(start_ms),
            to_t=ms_to_iso(end_ms),
            bars_done=bars_done,
            bars_est=bars_done,
            bars_total=bars_done,
            pages=self.cache_stats.get("rest_pages", 0),
            pct=99.5,
            stage="replay_complete",
            cache=self.cache_stats,
        )
        return bars_done, amb_total, last_open

    async def _run_live_now(self) -> None:
        try:
            await self._set_phase(SessionPhase.LOADING)
            step = interval_ms(self.timeframe)
            now = int(time.time() * 1000)
            start = now - self.history_limit_live * step
            klines = await asyncio.to_thread(
                lambda: self.kline_source.fetch_range(
                    symbol=canonical_binance_symbol(self.symbol),
                    interval=self.timeframe,
                    start_ms=start,
                    end_ms=now + step,
                    market=self.market,
                )
            )
            async with self._lock:
                self.klines = klines
                self.bars = [bar_from_binance_kline(k) for k in klines]
                self.chart_candles = bn.bars_to_chart_candles(klines)
                if klines and not self._p0_seeded:
                    open_px = Decimal(str(klines[-1][1]))
                    ts_ms = int(klines[-1][0])
                    r = self.engine.on_event(
                        MarketEvent(kind=MarketEventKind.SEED_P0, ts_ms=ts_ms, price=open_px)
                    )
                    self.recent_domain.extend(r.events)
                    self.event_log.extend(r.events)
                    self._p0_seeded = True
                    self.last_price = str(klines[-1][4])
                self.feed_status = "seeded"
            # Open WS early so trades buffer during REST backfill
            self._buffering = True
            await self._ensure_ws()
            self._aggtrade_metrics_enabled = True
            self._sync_trade_windows_from_engine(schedule_backfill=True)
            if self._trade_backfill_task:
                try:
                    await asyncio.wait_for(asyncio.shield(self._trade_backfill_task), timeout=120)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
            await self.broadcast_snapshot()
            self._live_enabled = True
            self._buffering = False
            self.fence_ms = int(klines[-1][0]) if klines else now
            await self._set_phase(SessionPhase.LIVE)
            await self.broadcast_snapshot()
        except Exception as exc:
            logger.exception("live start failed")
            self.error = str(exc)
            await self._set_phase(SessionPhase.ERROR, error=str(exc))

    async def _run_backtest(self) -> None:
        assert self.start_ms is not None and self.end_ms is not None
        try:
            start_ms, end_open_ms = self.start_ms, self.end_ms
            # BACKTEST user End is inclusive by candle open → fetch [start, end+tf)
            fetch_end_ms = inclusive_open_range_fetch_end(end_open_ms, self.timeframe)
            bars_done, amb, _last = await self._hist_download_and_replay(
                start_ms=start_ms,
                end_ms=fetch_end_ms,
                closed_only_before_ms=fetch_end_ms,
            )
            if self._stopped():
                await self._set_phase(SessionPhase.STOPPED)
                return
            self.ambiguity_count = amb
            self.bars_processed = bars_done
            await self.broadcast_snapshot()
            await self._set_phase(
                SessionPhase.BACKTEST_DONE,
                pct=100.0,
                bars_done=self.bars_processed,
                bars_total=self.bars_processed,
                ambiguity_count=self.ambiguity_count,
            )
            await self.broadcast(
                {
                    "v": 1,
                    "type": "run_complete",
                    "mode": "BACKTEST",
                    "reason": "end_time_reached",
                    "ambiguity_count": self.ambiguity_count,
                    "bars_processed": self.bars_processed,
                    "event_summary": self.event_log.summary_counts(),
                }
            )
        except Exception as exc:
            logger.exception("backtest failed")
            self.error = str(exc)
            await self._set_phase(SessionPhase.ERROR, error=str(exc))

    async def _run_replay_to_live(self) -> None:
        assert self.start_ms is not None
        engine_id = id(self.engine)
        try:
            step = interval_ms(self.timeframe)
            now = int(time.time() * 1000)
            hist_end = (now // step) * step
            start_ms = self.start_ms
            self._buffering = True
            self._live_enabled = False
            await self._ensure_ws()

            bars_done, amb, last_open = await self._hist_download_and_replay(
                start_ms=start_ms,
                end_ms=hist_end,
                closed_only_before_ms=hist_end,
            )
            if self._stopped():
                self._buffering = False
                await self._set_phase(SessionPhase.STOPPED)
                return

            self.ambiguity_count = amb
            self.bars_processed = bars_done
            if last_open is not None:
                self.last_hist_open_ms = last_open
                self.fence_ms = last_open + step
            else:
                self.fence_ms = hist_end

            if id(self.engine) != engine_id:
                raise RuntimeError("engine object must survive handoff")

            await self._set_phase(SessionPhase.CATCHING_UP, pct=99.0)
            await self._flush_trade_buffer()
            self._buffering = False
            self._live_enabled = True
            self.feed_status = "live"
            self._aggtrade_metrics_enabled = True
            self._sync_trade_windows_from_engine(schedule_backfill=True)
            if self._trade_backfill_task:
                try:
                    await asyncio.wait_for(asyncio.shield(self._trade_backfill_task), timeout=180)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
            await self.broadcast(
                {
                    "v": 1,
                    "type": "handoff",
                    "phase": "live",
                    "last_hist_open_ms": self.last_hist_open_ms,
                    "fence_ms": self.fence_ms,
                    "engine_id": engine_id,
                    "ambiguity_count": self.ambiguity_count,
                    "metrics_handoff_status": self.trade_store.handoff_status,
                }
            )
            await self._set_phase(SessionPhase.LIVE, pct=100.0)
            await self.broadcast_snapshot()
        except Exception as exc:
            logger.exception("replay_to_live failed")
            self.error = str(exc)
            self._buffering = False
            await self._set_phase(SessionPhase.ERROR, error=str(exc))

    async def _flush_trade_buffer(self) -> None:
        fence = self.fence_ms or 0
        async with self._lock:
            buf = sorted(self._trade_buffer, key=lambda x: x[0])
            self._trade_buffer.clear()
        for ts_ms, price in buf:
            if ts_ms < fence:
                continue
            await self._apply_live_price(price, ts_ms, broadcast=False)
        await self.broadcast_snapshot()

    async def _ensure_ws(self) -> None:
        if self._ws_task and not self._ws_task.done():
            return
        self._ws_task = asyncio.create_task(self._run_binance_ws(), name="gf-binance-ws")

    async def _run_binance_ws(self) -> None:
        try:
            import websockets
        except ImportError:
            self.feed_status = "no_websockets_pkg"
            return
        url = bn.combined_stream_url(canonical_binance_symbol(self.symbol), self.timeframe, market=self.market)
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    self.feed_status = "live" if self._live_enabled else "buffering"
                    self._ws_coverage_edge_ms = None
                    backoff = 1.0
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        await self._on_ws_raw(str(raw))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("ws error: %s", exc)
                self.feed_status = "live_degraded"
                self._ws_coverage_edge_ms = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _record_ws_observation(self, ts_ms: int) -> None:
        """Record verified live observation coverage while the WS is connected.

        A connected stream verifies elapsed time even if no trade row exists in
        every millisecond. The edge is reset on reconnect so disconnected time is
        not falsely covered; REST must repair that gap.
        """
        ts = int(ts_ms)
        prev = self._ws_coverage_edge_ms
        start = ts if prev is None else min(ts, int(prev) + 1)
        self.aggtrade_cache.record_coverage(
            self.market,
            self.symbol,
            start,
            ts,
            source="ws",
            note="ws-observed",
        )
        self._ws_coverage_edge_ms = max(ts, int(prev or ts))

    async def _on_ws_raw(self, raw: str) -> None:
        parsed = bn.parse_combined_message(raw)
        if not parsed:
            return
        data = parsed["data"]
        event = data.get("e")
        obs_ts = int(data.get("T") or data.get("E") or 0)
        if obs_ts > 0:
            self._record_ws_observation(obs_ts)
        if event in {"aggTrade", "trade"}:
            price = Decimal(str(data["p"]))
            ts_ms = int(data.get("T") or data.get("E") or 0)
            # Always retain full trade for metric store when LIVE metrics desired
            if self._buffering and not self._live_enabled:
                async with self._lock:
                    self._trade_buffer.append((ts_ms, price))
                    self._agg_trade_buffer.append(dict(data))
                    if len(self._trade_buffer) > 50_000:
                        self._trade_buffer = self._trade_buffer[-30_000:]
                    if len(self._agg_trade_buffer) > 50_000:
                        self._agg_trade_buffer = self._agg_trade_buffer[-30_000:]
                # Also feed store if windows already set (live seed backfill overlap)
                if self.trade_store.ladder_start_ms is not None:
                    self.aggtrade_cache.ingest_ws_message(self.market, self.symbol, data)
                    self.trade_store.ingest_ws_message(data)
                return
            if not self._live_enabled:
                if self.trade_store.ladder_start_ms is not None:
                    self.aggtrade_cache.ingest_ws_message(self.market, self.symbol, data)
                    self.trade_store.ingest_ws_message(data)
                return
            fence = self.fence_ms or 0
            # Metrics accumulate regardless of fence (fence is for engine path only)
            self.aggtrade_cache.ingest_ws_message(self.market, self.symbol, data)
            self.trade_store.ingest_ws_message(data)
            # Incremental streaming-snapshot update for new live trades.
            # Live trades are persisted to SQLite first (above), so even if
            # this incremental call is dropped for any reason the persistent
            # catchup_from_persistent pass will recover the trade.
            self._incremental_streaming_update(data)
            if ts_ms < fence:
                return
            await self._apply_live_price(price, ts_ms, broadcast=True)
        elif event == "kline":
            await self._on_kline(data)

    def _buffer_live_for_streaming(self, data: Dict[str, Any]) -> None:
        """Buffer a WS trade for later merge into the streaming snapshot."""
        try:
            ts = int(data.get("T") or data.get("E") or 0)
            if ts <= 0:
                return
            raw_id = int(data["a"] if "a" in data else data["t"])
            if "a" in data:
                # aggregate row passed via WS — store under positive agg ID.
                storage_id = raw_id
                id_domain = "aggtrade"
                real_trade_id = raw_id
            else:
                # raw @trade row — store under negative internal key.
                storage_id = -abs(raw_id)
                id_domain = "trade"
                real_trade_id = raw_id
            self.trade_store.buffer_live_trade(
                ts_ms=ts,
                price=float(data["p"]),
                qty=float(data["q"]),
                storage_id=storage_id,
                real_trade_id=real_trade_id,
                id_domain=id_domain,
            )
        except (KeyError, TypeError, ValueError):
            return

    def _incremental_streaming_update(self, data: Dict[str, Any]) -> bool:
        """Incrementally update the streaming snapshot for a NEW live trade."""
        if not self.trade_store.has_streaming_snapshot():
            return False
        try:
            ts = int(data.get("T") or data.get("E") or 0)
            if ts <= 0:
                return False
            raw_id = int(data["a"] if "a" in data else data["t"])
            if "a" in data:
                storage_id = raw_id
                id_domain = "aggtrade"
                real_trade_id = raw_id
            else:
                storage_id = -abs(raw_id)
                id_domain = "trade"
                real_trade_id = raw_id
            return self.trade_store.ingest_live_trade(
                ts_ms=ts,
                price=float(data["p"]),
                qty=float(data["q"]),
                storage_id=storage_id,
                real_trade_id=real_trade_id,
                id_domain=id_domain,
            )
        except (KeyError, TypeError, ValueError):
            return False

    async def _apply_live_price(self, price: Decimal, ts_ms: int, *, broadcast: bool) -> None:
        from ..metrics import fmt_price

        async with self._lock:
            self.last_price = str(price)
            domain = apply_price_to_engine(self.engine, price, ts_ms)
            windows_changed = False
            if domain:
                self.recent_domain.extend(domain)
                self.event_log.extend(domain)
                if len(self.recent_domain) > 300:
                    self.recent_domain = self.recent_domain[-300:]
                prev_l = self.trade_store.ladder_start_ms
                prev_s = self.trade_store.step_start_ms
                self._sync_trade_windows_from_engine(schedule_backfill=False)
                windows_changed = (
                    self.trade_store.ladder_start_ms != prev_l
                    or self.trade_store.step_start_ms != prev_s
                )
                frag = {
                    "cycle_id": self.engine.state.cycle_id,
                    "n": self.engine.state.highest_filled,
                    "p0": str(self.engine.state.p0) if self.engine.state.p0 is not None else None,
                    "current_p": str(self.engine.state.current_p())
                    if self.engine.state.current_p()
                    else None,
                    "shared_tp": str(self.engine.state.shared_tp)
                    if self.engine.state.shared_tp
                    else None,
                    "next_p": str(self.engine.state.next_p()) if self.engine.state.next_p() else None,
                    "further_p": str(self.engine.state.further_p())
                    if self.engine.state.further_p()
                    else None,
                    "levels": schemas.levels_for_render(self.engine.state),
                    "legs": [
                        {
                            "step": leg.step,
                            "entry": fmt_price(leg.entry),
                            "ts_ms": leg.ts_ms,
                            "time": leg.ts_ms // 1000,
                        }
                        for leg in self.engine.state.legs
                    ],
                    "last_candle_time": self.chart_candles[-1]["time"] if self.chart_candles else None,
                    "metric_windows": {
                        "ladder_start_ts_ms": self.engine.state.legs[0].ts_ms if self.engine.state.legs else None,
                        "step_start_ts_ms": self.engine.state.legs[-1].ts_ms if self.engine.state.legs else None,
                        "latest_candle_time": self.chart_candles[-1]["time"] if self.chart_candles else None,
                    },
                    "markers": schemas.markers_from_domain(domain),
                    "ambiguity_count": self.ambiguity_count,
                }
                frag.update(self._metric_fragment())
                msg: Dict[str, Any] = schemas.engine_event_msg(domain, frag)
            else:
                metrics = None
                # Throttle metric broadcasts on pure price ticks
                if ts_ms - self._last_metric_broadcast_ms >= self._metric_broadcast_min_interval_ms:
                    metrics = self._metric_fragment()
                    self._last_metric_broadcast_ms = ts_ms
                msg = schemas.price_update_msg(str(price), ts_ms, metrics=metrics)
        if broadcast:
            await self.broadcast(msg)

    async def _on_kline(self, data: Dict[str, Any]) -> None:
        k = data.get("k") or {}
        o_time = int(k["t"])
        if self.last_hist_open_ms is not None and o_time <= self.last_hist_open_ms:
            return
        arr = [
            o_time,
            k["o"],
            k["h"],
            k["l"],
            k["c"],
            k["v"],
            int(k.get("T") or 0),
            k.get("q") or "0",
        ]
        candle = bn.kline_to_chart_candle(arr)
        final = bool(k.get("x"))
        async with self._lock:
            if self.chart_candles and self.chart_candles[-1]["time"] == candle["time"]:
                self.chart_candles[-1] = candle
                if self.klines:
                    self.klines[-1] = arr
                    self.bars[-1] = bar_from_binance_kline(arr)
            else:
                self.chart_candles.append(candle)
                self.klines.append(arr)
                self.bars.append(bar_from_binance_kline(arr))
            self.last_price = str(k["c"])
        if self._live_enabled or self.mode is SessionMode.LIVE:
            await self.broadcast(schemas.candle_update_msg(candle, final=final))


_CONTROLLER: Optional[SessionController] = None


def get_session() -> SessionController:
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = SessionController()
    return _CONTROLLER


def reset_session_for_tests() -> SessionController:
    global _CONTROLLER
    _CONTROLLER = SessionController()
    return _CONTROLLER
