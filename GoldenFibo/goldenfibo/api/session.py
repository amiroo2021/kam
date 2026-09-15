"""In-memory LIVE session: owns GoldenFiboEngine + candle buffer + Binance public feed."""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Set

from ..engine.config import EngineConfig, Side
from ..engine.engine import GoldenFiboEngine
from ..engine.events import DomainEvent, MarketEvent, MarketEventKind
from ..live.price_path import apply_price_to_engine
from ..marketdata import binance_public as bn
from ..metrics import OhlcvBar, bar_from_binance_kline
from . import schemas

logger = logging.getLogger("goldenfibo.session")

BroadcastFn = Callable[[Dict[str, Any]], None]


class LiveSession:
    """Backend-owned live paper visualization session (no exchange orders)."""

    def __init__(
        self,
        *,
        symbol: str = "BTCUSDT",
        timeframe: str = "1m",
        side: Side = Side.BUY,
        percentage: Decimal = Decimal("0.001"),
        history_limit: int = 300,
    ) -> None:
        self.symbol = symbol.upper()
        self.timeframe = timeframe
        self.side = side
        self.percentage = percentage
        self.history_limit = history_limit
        self.mode = "LIVE"

        self.engine = GoldenFiboEngine(
            EngineConfig(side=side, percentage=percentage, symbol=self.symbol)
        )
        self.klines: List[list] = []
        self.bars: List[OhlcvBar] = []
        self.chart_candles: List[Dict[str, Any]] = []
        self.last_price: Optional[str] = None
        self.recent_domain: List[DomainEvent] = []
        self._p0_seeded = False
        self._ws_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._clients: Set[Any] = set()
        self._lock = asyncio.Lock()
        self.feed_status = "idle"
        self.session_id = f"{self.symbol}-{self.timeframe}-{side.value}"

    # ----- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        await self._seed_history_and_p0()
        if self._ws_task is None or self._ws_task.done():
            self._stop.clear()
            self._ws_task = asyncio.create_task(self._run_binance_ws(), name="gf-binance-ws")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ws_task = None
        self.feed_status = "stopped"

    async def reconfigure(
        self,
        *,
        side: Optional[Side] = None,
        percentage: Optional[Decimal] = None,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
    ) -> None:
        """Replace engine config — starts a new paper cycle (explicit user action)."""
        async with self._lock:
            if symbol:
                self.symbol = symbol.upper()
            if timeframe:
                self.timeframe = timeframe
            if side is not None:
                self.side = side
            if percentage is not None:
                self.percentage = percentage
            self.engine = GoldenFiboEngine(
                EngineConfig(side=self.side, percentage=self.percentage, symbol=self.symbol)
            )
            self.recent_domain.clear()
            self._p0_seeded = False
            self.klines.clear()
            self.bars.clear()
            self.chart_candles.clear()
        await self.stop()
        await self.start()
        await self.broadcast_snapshot()

    # ----- P0 rule -----------------------------------------------------------
    # Live session P0 = open of the *current forming* 1m candle at session start
    # (last REST kline open). We do NOT replay historical path into the engine;
    # the chart shows history, the ladder monitors forward from that P0.
    # Reconnect does not reseed while this process lives (_p0_seeded stays True).

    async def _seed_history_and_p0(self) -> None:
        def _fetch():
            return bn.fetch_klines(self.symbol, self.timeframe, limit=self.history_limit)

        try:
            klines = await asyncio.to_thread(_fetch)
        except Exception as exc:
            logger.exception("kline seed failed: %s", exc)
            self.feed_status = f"seed_error:{exc}"
            return

        async with self._lock:
            self.klines = list(klines)
            self.bars = [bar_from_binance_kline(k) for k in klines]
            self.chart_candles = bn.bars_to_chart_candles(klines)
            if klines and not self._p0_seeded:
                open_px = Decimal(str(klines[-1][1]))
                ts_ms = int(klines[-1][0])
                result = self.engine.on_event(
                    MarketEvent(kind=MarketEventKind.SEED_P0, ts_ms=ts_ms, price=open_px)
                )
                self.recent_domain.extend(result.events)
                self._p0_seeded = True
                self.last_price = str(klines[-1][4])
            self.feed_status = "seeded"

    # ----- clients -----------------------------------------------------------

    def register_client(self, ws: Any) -> None:
        self._clients.add(ws)

    def unregister_client(self, ws: Any) -> None:
        self._clients.discard(ws)

    def snapshot_dict(self) -> Dict[str, Any]:
        return schemas.build_state_payload(
            mode=self.mode,
            symbol=self.symbol,
            timeframe=self.timeframe,
            side=self.side.value,
            percentage=str(self.percentage),
            price=self.last_price,
            engine=self.engine,
            candles=list(self.chart_candles),
            bars=list(self.bars),
            recent_domain=list(self.recent_domain[-50:]),
            connected=self.feed_status in ("seeded", "live", "live_degraded"),
        )

    async def broadcast(self, message: Dict[str, Any]) -> None:
        dead = []
        data = json.dumps(message)
        for ws in list(self._clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def broadcast_snapshot(self) -> None:
        await self.broadcast(self.snapshot_dict())

    # ----- binance ws --------------------------------------------------------

    async def _run_binance_ws(self) -> None:
        try:
            import websockets  # type: ignore
        except ImportError:
            logger.error("websockets package missing")
            self.feed_status = "no_websockets_pkg"
            return

        url = bn.combined_stream_url(self.symbol, self.timeframe)
        self.feed_status = "connecting"
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    self.feed_status = "live"
                    backoff = 1.0
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        await self._on_ws_raw(str(raw))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("binance ws error: %s", exc)
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
            await self._on_agg_trade(data)
        elif event == "kline":
            await self._on_kline(data)

    async def _on_agg_trade(self, data: Dict[str, Any]) -> None:
        price = Decimal(str(data["p"]))
        ts_ms = int(data.get("T") or data.get("E") or 0)
        async with self._lock:
            self.last_price = str(price)
            domain = apply_price_to_engine(self.engine, price, ts_ms)
            if domain:
                self.recent_domain.extend(domain)
                # trim
                if len(self.recent_domain) > 200:
                    self.recent_domain = self.recent_domain[-200:]
                frag = {
                    "cycle_id": self.engine.state.cycle_id,
                    "n": self.engine.state.highest_filled,
                    "p0": str(self.engine.state.p0) if self.engine.state.p0 is not None else None,
                    "current_p": str(self.engine.state.current_p()) if self.engine.state.current_p() else None,
                    "shared_tp": str(self.engine.state.shared_tp) if self.engine.state.shared_tp else None,
                    "next_p": str(self.engine.state.next_p()) if self.engine.state.next_p() else None,
                    "further_p": str(self.engine.state.further_p()) if self.engine.state.further_p() else None,
                    "levels": schemas.levels_for_render(self.engine.state),
                    "legs": [
                        {"step": leg.step, "entry": str(leg.entry), "ts_ms": leg.ts_ms, "qty": str(leg.qty)}
                        for leg in self.engine.state.legs
                    ],
                    "markers": schemas.markers_from_domain(domain),
                }
                # metrics refresh
                st = self.engine.state
                from ..metrics import metrics_for_legs, fmt_metric

                lv, sv, lp, sp = metrics_for_legs(
                    self.bars,
                    ladder_start_ts_ms=st.legs[0].ts_ms if st.legs else None,
                    step_start_ts_ms=st.legs[-1].ts_ms if st.legs else None,
                )
                frag["ladder_vwap"] = fmt_metric(lv)
                frag["active_step_vwap"] = fmt_metric(sv)
                frag["ladder_poc"] = fmt_metric(lp)
                frag["active_step_poc"] = fmt_metric(sp)
                msg = schemas.engine_event_msg(domain, frag)
            else:
                msg = schemas.price_update_msg(str(price), ts_ms)
        await self.broadcast(msg)

    async def _on_kline(self, data: Dict[str, Any]) -> None:
        k = data.get("k") or {}
        # Convert WS kline object to array-like candle
        o_time = int(k["t"])
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
            # update / append chart candle
            if self.chart_candles and self.chart_candles[-1]["time"] == candle["time"]:
                self.chart_candles[-1] = candle
                self.klines[-1] = arr
                self.bars[-1] = bar_from_binance_kline(arr)
            else:
                self.chart_candles.append(candle)
                self.klines.append(arr)
                self.bars.append(bar_from_binance_kline(arr))
                if len(self.chart_candles) > self.history_limit + 5:
                    self.chart_candles = self.chart_candles[-self.history_limit :]
                    self.klines = self.klines[-self.history_limit :]
                    self.bars = self.bars[-self.history_limit :]
            self.last_price = str(k["c"])
        await self.broadcast(schemas.candle_update_msg(candle, final=final))


# process-wide default session
_SESSION: Optional[LiveSession] = None


def get_session() -> LiveSession:
    global _SESSION
    if _SESSION is None:
        _SESSION = LiveSession()
    return _SESSION


def reset_session_for_tests() -> LiveSession:
    global _SESSION
    _SESSION = LiveSession()
    return _SESSION
