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
from ..marketdata.binance_klines_range import BinancePublicKlineSource, inclusive_open_range_fetch_end
from ..marketdata.timeframes import (
    interval_ms,
    ms_to_iso,
    parse_iso_to_ms,
    require_aligned,
    validate_interval,
)
from ..metrics import OhlcvBar, bar_from_binance_kline
from .event_log import EventLog
from .runner import apply_ohlc_page, new_engine_for_run, run_ohlc_on_engine
from .types import SessionMode, SessionPhase

CHART_CANDLE_LIMIT = 2500  # browser chart window; engine reconstructs full history

logger = logging.getLogger("goldenfibo.controller")


class SessionController:
    """Backend-owned multi-mode session. One engine object for REPLAY→LIVE handoff."""

    def __init__(self) -> None:
        self.mode = SessionMode.LIVE
        self.phase = SessionPhase.IDLE
        self.symbol = "BTCUSDT"
        self.timeframe = "1m"
        self.side = Side.BUY
        self.percentage = Decimal("0.001")
        self.ohlc_mode = OhlcResolveMode.LEGACY
        self.run_id = ""
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
        self.kline_source = BinancePublicKlineSource()
        self.history_limit_live = 300

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
        )
        payload.update(
            {
                "phase": self.phase.value,
                "run_id": self.run_id,
                "progress": dict(self.progress),
                "ambiguity_count": self.ambiguity_count,
                "bars_processed": self.bars_processed,
                "event_summary": self.event_log.summary_counts(),
                "ohlc_resolver": self.ohlc_mode.value,
                "start_ms": self.start_ms,
                "end_ms": self.end_ms,
                "fence_ms": self.fence_ms,
                "error": self.error,
                "feed_status": self.feed_status,
                "note_historical": (
                    "LEGACY OHLC uses deterministic intrabar assumptions; "
                    "not tick-perfect vs continuous aggTrade."
                ),
            }
        )
        return payload

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

        if symbol:
            self.symbol = symbol.upper().replace("/", "")
        if timeframe:
            self.timeframe = validate_interval(timeframe)
        if side is not None:
            self.side = side
        if percentage is not None:
            self.percentage = percentage
        self.mode = mode
        self.run_id = uuid.uuid4().hex[:12]
        self.phase = SessionPhase.LOADING
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
        """Page fetch → apply to engine → progress. Returns (bars, ambiguity, last_open_ms)."""
        step = interval_ms(self.timeframe)
        est = max(1, (int(end_ms) - int(start_ms)) // step)
        pages = 0
        bars_done = 0
        amb_total = 0
        last_open: int | None = None

        await self._set_phase(
            SessionPhase.DOWNLOADING,
            from_t=ms_to_iso(start_ms),
            to_t=ms_to_iso(end_ms),
            bars_done=0,
            bars_est=est,
            bars_total=est,
            pages=0,
            pct=0.0,
            stage="download_replay",
        )

        # Materialize page iterator in a worker-friendly loop: one page per to_thread
        cursor = start_ms
        seen: set[int] = set()
        from ..marketdata.binance_klines_range import iter_klines_pages

        def next_page_state() -> dict:
            return {"cursor": cursor, "seen": seen}

        # Use source.iter_pages fully in thread would block progress; pull one page at a time.
        from ..marketdata.binance_public import BINANCE_SPOT_REST
        import urllib.parse, json, urllib.request

        base_url = getattr(self.kline_source, "base_url", BINANCE_SPOT_REST)
        custom_fetch = getattr(self.kline_source, "fetch", None)

        while cursor < end_ms and not self._stopped():
            pages += 1
            cur = cursor
            end_bound = end_ms
            cof = closed_only_before_ms
            lim = 1000
            sym = self.symbol
            tf = self.timeframe

            def fetch_one() -> list:
                from ..marketdata.binance_klines_range import _default_fetch
                fetch = custom_fetch or _default_fetch
                qs = urllib.parse.urlencode(
                    {
                        "symbol": sym,
                        "interval": tf,
                        "startTime": int(cur),
                        "endTime": int(end_bound - 1),
                        "limit": lim,
                    }
                )
                url = f"{base_url}/api/v3/klines?{qs}"
                return fetch(url)

            batch = await asyncio.to_thread(fetch_one)
            if self._stopped():
                break
            if not batch:
                break

            page_rows: list = []
            page_last = None
            for row in batch:
                ot = int(row[0])
                if ot < start_ms or ot >= end_ms:
                    continue
                if ot in seen:
                    continue
                if cof is not None and ot + step > cof:
                    continue
                seen.add(ot)
                page_rows.append(row)
                page_last = ot
            page_rows.sort(key=lambda r: int(r[0]))

            if page_rows:
                # Apply page through same engine
                def apply_page() -> tuple:
                    r = apply_ohlc_page(
                        self.engine,
                        page_rows,
                        mode=self.ohlc_mode,
                        event_log=self.event_log,
                    )
                    return r.ambiguity_count, r.domain, r.bars_processed

                a_count, domain, nbar = await asyncio.to_thread(apply_page)
                amb_total += a_count
                bars_done += nbar
                last_open = page_last

                async with self._lock:
                    for k in page_rows:
                        self.klines.append(k)
                        self.bars.append(bar_from_binance_kline(k))
                    # chart candles
                    from ..marketdata import binance_public as bn_mod
                    new_cc = bn_mod.bars_to_chart_candles(page_rows)
                    self.chart_candles.extend(new_cc)
                    self._trim_chart()
                    self._trim_metric_bars()
                    self.ambiguity_count = amb_total
                    self.bars_processed = bars_done
                    self.recent_domain.extend(domain[-50:])
                    self._p0_seeded = self.engine.state.active
                    if page_rows:
                        self.last_price = str(page_rows[-1][4])

            pct = min(99.0, 100.0 * bars_done / est)
            await self._set_phase(
                SessionPhase.DOWNLOADING,
                from_t=ms_to_iso(start_ms),
                to_t=ms_to_iso(end_ms),
                bars_done=bars_done,
                bars_est=est,
                bars_total=est,
                pages=pages,
                pct=round(pct, 2),
                stage="download_replay",
                last_open_ms=last_open,
            )
            # occasional snapshot so chart fills during long runs
            if pages == 1 or pages % 5 == 0 or bars_done >= est:
                await self.broadcast_snapshot()

            if page_last is None:
                break
            nxt = page_last + step
            if nxt <= cursor:
                break
            cursor = nxt
            if len(batch) < lim and (page_last + step >= end_ms or not page_rows):
                break
            # polite yield to event loop (health + WS)
            await asyncio.sleep(0)
            # small pause like original rate limit when full page
            if len(batch) >= lim:
                await asyncio.sleep(0.05)

        if self._stopped():
            return bars_done, amb_total, last_open

        await self._set_phase(
            SessionPhase.REPLAYING,
            from_t=ms_to_iso(start_ms),
            to_t=ms_to_iso(end_ms),
            bars_done=bars_done,
            bars_est=est,
            bars_total=bars_done,
            pages=pages,
            pct=99.5,
            stage="replay_complete",
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
                    symbol=self.symbol,
                    interval=self.timeframe,
                    start_ms=start,
                    end_ms=now + step,
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
            await self.broadcast_snapshot()
            self._live_enabled = True
            self.fence_ms = int(klines[-1][0]) if klines else now
            await self._set_phase(SessionPhase.LIVE)
            await self._ensure_ws()
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
            await self.broadcast(
                {
                    "v": 1,
                    "type": "handoff",
                    "phase": "live",
                    "last_hist_open_ms": self.last_hist_open_ms,
                    "fence_ms": self.fence_ms,
                    "engine_id": engine_id,
                    "ambiguity_count": self.ambiguity_count,
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
        url = bn.combined_stream_url(self.symbol, self.timeframe)
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    self.feed_status = "live" if self._live_enabled else "buffering"
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
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _on_ws_raw(self, raw: str) -> None:
        parsed = bn.parse_combined_message(raw)
        if not parsed:
            return
        data = parsed["data"]
        event = data.get("e")
        if event == "aggTrade":
            price = Decimal(str(data["p"]))
            ts_ms = int(data.get("T") or data.get("E") or 0)
            if self._buffering and not self._live_enabled:
                async with self._lock:
                    self._trade_buffer.append((ts_ms, price))
                    if len(self._trade_buffer) > 50_000:
                        self._trade_buffer = self._trade_buffer[-30_000:]
                return
            if not self._live_enabled:
                return
            fence = self.fence_ms or 0
            if ts_ms < fence:
                return
            await self._apply_live_price(price, ts_ms, broadcast=True)
        elif event == "kline":
            await self._on_kline(data)

    async def _apply_live_price(self, price: Decimal, ts_ms: int, *, broadcast: bool) -> None:
        from ..metrics import fmt_metric, fmt_price, metrics_for_legs

        async with self._lock:
            self.last_price = str(price)
            domain = apply_price_to_engine(self.engine, price, ts_ms)
            if domain:
                self.recent_domain.extend(domain)
                self.event_log.extend(domain)
                if len(self.recent_domain) > 300:
                    self.recent_domain = self.recent_domain[-300:]
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
                st = self.engine.state
                lv, sv, lp, sp, l_val, l_vah = metrics_for_legs(
                    self.bars,
                    ladder_start_ts_ms=st.legs[0].ts_ms if st.legs else None,
                    step_start_ts_ms=st.legs[-1].ts_ms if st.legs else None,
                )
                frag["ladder_vwap"] = fmt_metric(lv)
                frag["active_step_vwap"] = fmt_metric(sv)
                frag["ladder_poc"] = fmt_metric(lp)
                frag["active_step_poc"] = fmt_metric(sp)
                frag["ladder_val"] = fmt_metric(l_val)
                frag["ladder_vah"] = fmt_metric(l_vah)
                msg: Dict[str, Any] = schemas.engine_event_msg(domain, frag)
            else:
                msg = schemas.price_update_msg(str(price), ts_ms)
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
