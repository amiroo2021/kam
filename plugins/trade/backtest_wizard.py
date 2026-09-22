"""Telegram /backtest wizard for GoldenFibo Binance Spot/Futures historical replay."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# GoldenFibo legacy engine lives in the repo checkout.
_GF_LEGACY_ROOT = Path("/root/kam/GoldenFibo/reference/legacy_research")
if str(_GF_LEGACY_ROOT) not in sys.path:
    sys.path.insert(0, str(_GF_LEGACY_ROOT))

# GoldenFibo marketdata/cache code lives in the modern package tree.
_GF_ROOT = Path("/root/kam/GoldenFibo")
if str(_GF_ROOT) not in sys.path:
    sys.path.insert(0, str(_GF_ROOT))

from golden_fibo.constants import Side  # noqa: E402
from goldenfibo.backtest import run_finite_backtest  # noqa: E402
from goldenfibo.marketdata.aggtrade_cache import AggTradeCache  # noqa: E402
from goldenfibo.marketdata.kline_cache import CachePolicy, KlineCache, fetch_range_cached  # noqa: E402
from plugins.trade.tradedesk import TradeDesk  # noqa: E402

_BACKTEST_CACHE = KlineCache(Path("/root/kam/GoldenFibo/data/backtest_klines.sqlite"))
_BACKTEST_AGGTRADE_CACHE = AggTradeCache(Path("/root/kam/GoldenFibo/data/aggtrades.sqlite"))
_BACKTEST_TIMEFRAME = "1m"
_BACKTEST_REFRESH_TAIL_MS = 0
INCOMPLETE_AGGTRADE_COVERAGE = "INCOMPLETE_AGGTRADE_COVERAGE"

logger = logging.getLogger(__name__)

_SPOT = "https://api.binance.com"
_FUTURES = "https://fapi.binance.com"
_TIMEOUT = 30
_LIMIT_SPOT = 1000
_LIMIT_FUTURES = 1500
_OUT = Path("/root/golden_fibo/outputs/backtest_wizard")
_OUT.mkdir(parents=True, exist_ok=True)


def _button(text: str, cb: str) -> Dict[str, str]:
    return {"text": text, "callback_data": cb}


@dataclass
class Screen:
    text: str
    buttons: List[List[Dict[str, str]]] = field(default_factory=list)
    state: str = ""
    attachments: List[str] = field(default_factory=list)


@dataclass
class State:
    step: str = "mode"
    ladder: str = ""
    market: str = ""
    requested: str = ""
    symbol: str = ""
    display: str = ""
    price: str = ""
    percentage: float = 0.001
    day: Optional[int] = None
    month: Optional[int] = None
    year: Optional[int] = None
    awaiting_text: Optional[str] = None


class BacktestWizard:
    def __init__(self) -> None:
        self._states: Dict[Tuple[Any, ...], State] = {}
        self._desk = TradeDesk()

    def _state(self, key: Tuple[Any, ...]) -> State:
        return self._states.setdefault(key, State())

    @staticmethod
    def _progress_bar(pct: float, *, width: int = 16) -> str:
        pct = max(0.0, min(100.0, float(pct)))
        filled = int(round((pct / 100.0) * width))
        filled = max(0, min(width, filled))
        return "[" + ("█" * filled) + ("░" * (width - filled)) + f"] {pct:5.1f}%"

    @staticmethod
    def _progress_text(phase: str, pct: float, detail: str = "") -> str:
        labels = {
            "loading_cache": "Checking Historical Cache",
            "downloading_gaps": "Downloading Gaps",
            "cache_ready": "Historical Data Ready",
            "backtest": "Running Backtest",
        }
        label = labels.get(phase, phase.replace("_", " ").title())
        bar = BacktestWizard._progress_bar(pct)
        suffix = f"\n{detail}" if detail else ""
        return f"⏳ {label}\n{bar}{suffix}"

    @staticmethod
    def _progress_from_cache(stats: Dict[str, Any]) -> Tuple[str, float, str]:
        bars_total = int(stats.get("bars_total") or 0)
        bars_from_cache = int(stats.get("bars_from_cache") or 0)
        bars_downloaded = int(stats.get("bars_downloaded") or 0)
        bars_done = bars_from_cache + bars_downloaded
        pct = 100.0 if bars_total <= 0 else min(100.0, 100.0 * bars_done / bars_total)
        detail = ""
        cache_path = stats.get("path")
        if bars_total > 0:
            if bars_downloaded > 0:
                detail = f"Final candles: {bars_total:,}\nFrom cache: {bars_from_cache:,}\nDownloaded: {bars_downloaded:,}"
            else:
                detail = f"{bars_from_cache:,} candles loaded from cache\nDownloaded: 0"
        elif bars_from_cache > 0 or bars_downloaded > 0:
            detail = f"{bars_done:,} candles loaded from cache"
            if bars_downloaded > 0:
                detail += f"\nDownloaded: {bars_downloaded:,}"
        if cache_path:
            detail += ("\n" if detail else "") + f"Cache: {cache_path}"
        return ("cache_ready", pct, detail)

    @staticmethod
    def _progress_from_payload(payload: Dict[str, Any]) -> Tuple[str, float, str]:
        stats = dict(payload.get("stats") or {})
        phase = str(payload.get("stage") or "download")
        requested = int(payload.get("requested_candles") or stats.get("requested_candles") or 0)
        cached = int(payload.get("cached_candles") or stats.get("bars_from_cache") or 0)
        downloaded = int(payload.get("download_done") or payload.get("downloaded_candles") or stats.get("bars_downloaded") or 0)
        download_total = int(payload.get("download_total") or payload.get("missing_candles") or stats.get("download_total") or stats.get("gaps_remaining") or 0)
        final_candles = int(payload.get("final_candles") or stats.get("bars_total") or 0)
        backtest_done = int(payload.get("backtest_done") or 0)
        backtest_total = int(payload.get("backtest_total") or 0)
        cache_path = stats.get("path")

        if phase == "loading_cache":
            if requested > 0:
                pct = min(100.0, 100.0 * cached / requested)
                detail = f"{cached:,} / {requested:,} cached"
                if requested > cached:
                    detail += f"\nMissing: {requested - cached:,}"
            else:
                pct = 0.0
                detail = "Preparing historical request..."
            if cache_path:
                detail += f"\nCache: {cache_path}"
            return phase, pct, detail

        if phase == "downloading_gaps":
            if download_total <= 0:
                detail = "Preparing missing ranges..."
                if cache_path:
                    detail += f"\nCache: {cache_path}"
                return phase, 0.0, detail
            pct = min(100.0, 100.0 * downloaded / download_total)
            detail = f"{downloaded:,} / {download_total:,} missing candles"
            if cached > 0:
                detail += f"\nCached: {cached:,}"
            if cache_path:
                detail += f"\nCache: {cache_path}"
            return phase, pct, detail

        if phase == "cache_ready":
            pct = 100.0
            if final_candles > 0:
                detail = f"Final candles: {final_candles:,}"
                if cached > 0 or downloaded > 0:
                    detail += f"\nFrom cache: {cached:,}\nDownloaded: {downloaded:,}"
            else:
                detail = "Historical Data Ready"
            if cache_path:
                detail += f"\nCache: {cache_path}"
            return phase, pct, detail

        if phase == "backtest":
            if backtest_total > 0:
                pct = min(100.0, 100.0 * backtest_done / backtest_total)
                detail = f"{backtest_done:,} / {backtest_total:,} candles"
            else:
                pct = 0.0
                detail = "Running backtest..."
            return phase, pct, detail

        pct = 100.0 if final_candles else 0.0
        return phase, pct, str(payload.get("detail") or "")

    def reset(self, key: Tuple[Any, ...]) -> None:
        self._states.pop(key, None)

    def _nav(self, back: str | None = None) -> List[Dict[str, str]]:
        row: List[Dict[str, str]] = []
        if back:
            row.append(_button("⬅️ Back", f"back:{back}"))
        row.append(_button("✖️ Exit", "exit"))
        return row

    def open(self, key: Tuple[Any, ...]) -> Screen:
        self._states[key] = State()
        return self._screen_ladder()

    def _screen_ladder(self) -> Screen:
        return Screen(
            "Choose GoldenFibo backtest side:",
            [
                [_button("🔵 Buy Ladder", "ladder:buy")],
                [_button("🔴 Sell Ladder", "ladder:sell")],
                [_button("⚪ Both", "ladder:both")],
                self._nav(),
            ],
            "ladder",
        )

    def _screen_market(self) -> Screen:
        return Screen("Choose Binance market:", [[_button("Spot", "market:spot"), _button("Futures", "market:futures")], self._nav("ladder")], "market")

    def _screen_symbol(self) -> Screen:
        return Screen(
            "Choose instrument:",
            [[_button("BTC", "symbol:BTC"), _button("ETH", "symbol:ETH"), _button("SOL", "symbol:SOL")], [_button("HYPE", "symbol:HYPE"), _button("Other", "symbol:other")], self._nav("market")],
            "symbol",
        )

    def _resolve_and_price(self, st: State, requested: str) -> Screen:
        st.requested = requested.strip().upper()
        resp = self._desk.execute({"operation": "resolve_instrument", "exchange": "binance", "account": st.market, "market_type": st.market, "symbol": st.requested})
        if not getattr(resp, "success", False) or getattr(resp, "instrument", None) is None:
            msg = getattr(getattr(resp, "error", None), "message", None) or "Instrument not found."
            return Screen(f"Instrument not found: {st.requested}\n{msg}", [self._nav("symbol")], "symbol_error")
        inst = resp.instrument
        st.symbol = inst.symbol
        st.display = inst.display_name
        px_resp = self._desk.execute({"operation": "market_price", "exchange": "binance", "account": st.market, "market_type": st.market, "symbol": st.symbol})
        px = None
        if getattr(px_resp, "success", False):
            mp = getattr(px_resp, "market_price", None)
            px = getattr(mp, "price", None) or getattr(mp, "mark_price", None) if mp is not None else None
            if not px and isinstance(getattr(px_resp, "data", None), dict):
                px = px_resp.data.get("price")
        st.price = str(px or "?")
        return Screen(
            f"Confirm instrument:\n\n{st.display}\nPrice: {st.price}\nMarket: {st.market}",
            [[_button(f"✅ {st.symbol} @ {st.price}", "confirm:instrument")], self._nav("symbol")],
            "confirm_instrument",
        )

    def _screen_percentage(self) -> Screen:
        return Screen(
            "Choose ladder percentage (step-0 TP distance):",
            [
                [_button("Default: 0.001", "pct:0.001")],
                [_button("0.0001", "pct:0.0001"), _button("0.001", "pct:0.001"), _button("0.01", "pct:0.01")],
                [_button("0.1", "pct:0.1"), _button("1", "pct:1"), _button("Other", "pct:other")],
                self._nav("instrument"),
            ],
            "percentage",
        )

    def handle_callback(self, key: Tuple[Any, ...], suffix: str) -> Screen:
        st = self._state(key)
        if suffix == "exit":
            self.reset(key); return Screen("Backtest closed.", [], "closed")
        if suffix.startswith("back:"):
            target = suffix.split(":", 1)[1]
            if target == "symbol": return self._screen_symbol()
            if target == "instrument" and st.symbol:
                return Screen(
                    f"Confirm instrument:\n\n{st.display}\nPrice: {st.price}\nMarket: {st.market}",
                    [[_button(f"✅ {st.symbol} @ {st.price}", "confirm:instrument")], self._nav("symbol")],
                    "confirm_instrument",
                )
            if target == "pct": return self._screen_percentage()
            if target == "day":
                st.awaiting_text = "day"
                return Screen("Enter start day of month (1–31):", [self._nav("pct")], "await_day")
            if target == "market": return self._screen_market()
            return self._screen_ladder()
        if suffix.startswith("ladder:"):
            st.ladder = suffix.split(":",1)[1]
            return self._screen_market()
        if suffix.startswith("market:"):
            st.market = suffix.split(":",1)[1]
            return self._screen_symbol()
        if suffix.startswith("symbol:"):
            sym = suffix.split(":",1)[1]
            if sym == "other":
                st.awaiting_text = "symbol"
                return Screen("Type the Binance instrument symbol/base (example: BTC, BTCUSDT, ETH).", [self._nav("symbol")], "await_symbol")
            return self._resolve_and_price(st, sym)
        if suffix == "confirm:instrument":
            return self._screen_percentage()
        if suffix.startswith("pct:"):
            raw = suffix.split(":",1)[1]
            if raw == "other":
                st.awaiting_text = "percentage"
                return Screen("Type custom ladder percentage (example: 0.001):", [self._nav("pct")], "await_percentage")
            try:
                st.percentage = float(raw)
                if st.percentage <= 0:
                    raise ValueError
            except Exception:
                return self._screen_percentage()
            st.awaiting_text = "day"
            return Screen(f"Percentage set to {st.percentage:g}.\n\nEnter start day of month (1–31):", [self._nav("pct")], "await_day")
        if suffix.startswith("month:"):
            st.month = int(suffix.split(":",1)[1])
            st.awaiting_text = "year"
            return Screen("Enter start year (example: 2026):", [self._nav("day")], "await_year")
        if suffix == "run":
            return self._run_backtest(st)
        return self._screen_ladder()

    def handle_text(self, key: Tuple[Any, ...], text: str) -> Optional[Screen]:
        st = self._state(key)
        what = st.awaiting_text
        if not what:
            return None
        text = (text or "").strip()
        if what == "symbol":
            st.awaiting_text = None
            return self._resolve_and_price(st, text)
        if what == "percentage":
            try:
                st.percentage = float(text)
                if st.percentage <= 0:
                    raise ValueError
            except Exception:
                return Screen("Invalid percentage. Type a positive number like 0.001:", [self._nav("pct")], "await_percentage")
            st.awaiting_text = "day"
            return Screen(f"Percentage set to {st.percentage:g}.\n\nEnter start day of month (1–31):", [self._nav("pct")], "await_day")
        if what == "day":
            try:
                day = int(text)
                if not 1 <= day <= 31: raise ValueError
            except Exception:
                return Screen("Invalid day. Enter 1–31:", [self._nav("pct")], "await_day")
            st.day = day; st.awaiting_text = None
            rows=[]
            for a in range(1,13,4):
                rows.append([_button(str(m), f"month:{m}") for m in range(a,a+4)])
            rows.append(self._nav("day"))
            return Screen("Choose start month:", rows, "month")
        if what == "year":
            try:
                year = int(text)
                if not 2017 <= year <= datetime.now(timezone.utc).year: raise ValueError
                datetime(year, st.month or 1, st.day or 1, 0, 1, tzinfo=timezone.utc)
            except Exception:
                return Screen("Invalid year/date. Enter a valid year (example: 2026):", [self._nav("day")], "await_year")
            st.year = year; st.awaiting_text = None
            start = datetime(st.year, st.month or 1, st.day or 1, 0, 1, tzinfo=timezone.utc).isoformat().replace("+00:00","Z")
            return Screen(f"Run {st.ladder.upper()} backtest?\n{st.symbol} {st.market}\nPercentage: {st.percentage:g}\nStart: {start}", [[_button("▶️ Run backtest", "run")], self._nav("day")], "confirm_run")
        return None

    def _run_backtest(self, st: State, *, on_progress=None) -> Screen:
        start_dt = datetime(st.year or 2026, st.month or 1, st.day or 1, 0, 1, tzinfo=timezone.utc)
        sides = [Side.BUY, Side.SELL] if st.ladder == "both" else ([Side.BUY] if st.ladder == "buy" else [Side.SELL])
        total_sides = max(1, len(sides))
        if on_progress:
            on_progress(
                {
                    "stage": "download",
                    "pct": 0.0,
                    "bars_done": 0,
                    "bars_est": 1,
                    "stats": {"path": str(_BACKTEST_CACHE.path), "bars_total": 0},
                    "detail": "Loading cached candles",
                }
            )
        candles = _fetch_klines(st.symbol, st.market, int(start_dt.timestamp() * 1000), on_progress=on_progress)
        if not candles:
            return Screen("No Binance candles returned for that request.", [self._nav("symbol")], "error")
        stats = _BACKTEST_CACHE.stats_for(st.symbol, _BACKTEST_TIMEFRAME, market="futures" if st.market == "futures" else "spot")
        if on_progress:
            on_progress(
                {
                    "stage": "backtest",
                    "pct": 0.0,
                    "bars_done": 0,
                    "bars_est": total_sides,
                    "stats": stats,
                    "detail": "Running replay",
                }
            )
        start_iso = datetime.fromtimestamp(int(candles[0][0]) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        end_iso = datetime.fromtimestamp(int(candles[-1][0]) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        parts=[f"Backtest complete: {st.symbol} {st.market}\nPercentage: {st.percentage:g}\nData: {start_iso} → {end_iso}\nCandles: {len(candles):,}\nCache: {stats['path']}\nCached bars: {stats['bars']}\n"]
        attachments=[]
        for idx, side in enumerate(sides, start=1):
            if on_progress:
                on_progress(
                    {
                        "stage": "backtest",
                        "pct": min(100.0, 100.0 * idx / total_sides),
                        "bars_done": idx,
                        "bars_est": total_sides,
                        "stats": stats,
                        "detail": f"Running {idx}/{total_sides} sides",
                    }
                )
            result = _summarize_side(candles, side, st.symbol, st.market, st.percentage)
            parts.append(result["text"])
            attachments.append(result["svg"])
        self.reset(_DUMMY_KEY) if False else None
        return Screen("\n\n".join(parts), [[_button("New backtest", "restart"), _button("Exit", "exit")]], "done", attachments)


_DUMMY_KEY=("dummy",)
_WIZARD = BacktestWizard()


def _fetch_json(base: str, path: str, params: Dict[str, Any]):
    qs=urllib.parse.urlencode({k:v for k,v in params.items() if v is not None})
    req=urllib.request.Request(f"{base}{path}?{qs}", headers={"User-Agent":"HermesBacktest/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 public HTTPS API
        return json.loads(resp.read().decode())


def _fetch_klines(symbol: str, market: str, start_ms: int, *, on_progress=None):
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    source_market = "futures" if market == "futures" else "spot"
    if on_progress:
        on_progress(
            {
                "stage": "download",
                "bars_done": 0,
                "bars_est": max(1, (end - int(start_ms)) // 60_000),
                "stats": {"path": str(_BACKTEST_CACHE.path), "bars_total": 0},
                "detail": "Loading cached candles",
            }
        )
    base_url = _FUTURES if source_market == "futures" else _SPOT
    result = fetch_range_cached(
        symbol,
        _BACKTEST_TIMEFRAME,
        int(start_ms),
        end,
        cache=_BACKTEST_CACHE,
        policy=CachePolicy.AUTO,
        refresh_tail_ms=_BACKTEST_REFRESH_TAIL_MS,
        base_url=base_url,
        fetch=None,
        sleep_s=0.015,
        closed_only_before_ms=end,
        market=source_market,
        on_progress=on_progress,
    )
    return result.klines


def _vwap(candles, ts):
    rel=[k for k in candles if int(k[0])>=ts]
    base=sum(float(k[5]) for k in rel); quote=sum(float(k[7]) for k in rel)
    return quote/base if base else float("nan")


LIGHTBLUE = "#7ec8ff"
DARKBLUE = "#004c99"
LIGHTRED = "#ff9a9a"
DARKRED = "#990000"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _aggressor_metrics_from_trades(trades, start_ts: int, end_ts: int | None = None) -> Dict[str, float | str | None]:
    """Return aggressor metrics from Binance aggTrade side, never OHLC.

    Binance aggTrade ``m`` means buyer is maker. Therefore:
    - m=False: buyer is taker/aggressor => BUY aggressor
    - m=True: seller is taker/aggressor => SELL aggressor
    If any trade lacks side, mark the window unavailable instead of guessing.
    """
    buy_base = buy_quote = sell_base = sell_quote = all_base = all_quote = 0.0
    start = int(start_ts)
    end = None if end_ts is None else int(end_ts)
    count = 0
    missing_side = 0
    for t in trades:
        ts = int(getattr(t, "ts_ms"))
        if ts < start or (end is not None and ts >= end):
            continue
        q = max(0.0, _safe_float(getattr(t, "qty", 0.0)))
        if q <= 0:
            continue
        p = _safe_float(getattr(t, "price", 0.0))
        count += 1
        all_base += q
        all_quote += p * q
        side = getattr(t, "buyer_is_maker", None)
        if side is None:
            missing_side += 1
            continue
        if bool(side):
            sell_base += q
            sell_quote += p * q
        else:
            buy_base += q
            buy_quote += p * q
    total = buy_base + sell_base
    status = "COMPLETE" if count and missing_side == 0 else "UNAVAILABLE"
    return {
        "status": status,
        "trade_count": float(count),
        "missing_side_count": float(missing_side),
        "total_volume": all_base,
        "buy_base": buy_base,
        "sell_base": sell_base,
        "buy_volume": buy_base,
        "sell_volume": sell_base,
        "all_vwap": (all_quote / all_base) if all_base > 0 else None,
        "buy_vwap": (buy_quote / buy_base) if buy_base > 0 and status == "COMPLETE" else None,
        "sell_vwap": (sell_quote / sell_base) if sell_base > 0 and status == "COMPLETE" else None,
        "delta": buy_base - sell_base,
        "delta_ratio": ((buy_base - sell_base) / total) if total > 0 and status == "COMPLETE" else None,
    }


def _unavailable_aggressor(status: str = INCOMPLETE_AGGTRADE_COVERAGE) -> Dict[str, Any]:
    return {
        "status": status,
        "trade_count": 0.0,
        "missing_side_count": 0.0,
        "total_volume": 0.0,
        "buy_base": 0.0,
        "sell_base": 0.0,
        "buy_volume": 0.0,
        "sell_volume": 0.0,
        "all_vwap": None,
        "buy_vwap": None,
        "sell_vwap": None,
        "delta": None,
        "delta_ratio": None,
    }


def _ensure_required_aggtrade_coverage(
    cache: Any,
    market: str,
    symbol: str,
    required_start_ms: int,
    required_end_ms: int,
    **ensure_kwargs: Any,
) -> Tuple[bool, Any]:
    """Ensure exactly the aggressive-metric window, not the full candle range.

    ``AggTradeCache.ensure_coverage`` is idempotent: verified intervals are
    reused and only missing ranges are fetched. Keep the completeness gate here
    strict; callers must not calculate aggressive-flow metrics from partial
    coverage.
    """
    start = int(required_start_ms)
    end = int(required_end_ms)
    result = cache.ensure_coverage(market, symbol, start, end, **ensure_kwargs)
    covered = bool(getattr(result, "covered", False)) and bool(
        cache.coverage_covers(market, symbol, start, end)
    )
    return covered, result


def _aggtrade_aggressor_metrics_for_display(
    cache: Any,
    market: str,
    symbol: str,
    ladder_start_ms: int,
    step_start_ms: int,
    metric_end_ms: int,
    **ensure_kwargs: Any,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Return ladder and current-step aggTrade metrics for the display window.

    For a SELL ladder the requested displayed metrics are L-B-VWAP and
    S-B-VWAP. Their union is [current ladder P0, metric end]. Step metrics are a
    subwindow [current step start, metric end]. The same strict coverage gate is
    used for BUY too so the helper stays side-neutral.
    """
    required_start = min(int(ladder_start_ms), int(step_start_ms))
    required_end = int(metric_end_ms)
    meta: Dict[str, Any] = {
        "required_start_ms": required_start,
        "required_end_ms": required_end,
        "covered": False,
        "ensure_result": None,
    }
    try:
        covered, ensure_result = _ensure_required_aggtrade_coverage(
            cache, market, symbol, required_start, required_end, **ensure_kwargs
        )
        meta["ensure_result"] = ensure_result
        meta["covered"] = covered
        if not covered:
            return _unavailable_aggressor(), _unavailable_aggressor(), meta
        trades = cache.query_trades_range(market, symbol, required_start, required_end)
        ladder = _aggressor_metrics_from_trades(trades, int(ladder_start_ms), required_end + 1)
        step = _aggressor_metrics_from_trades(trades, int(step_start_ms), required_end + 1)
        return ladder, step, meta
    except Exception as exc:
        logger.warning("aggTrade coverage/metrics unavailable for %s %s: %s", market, symbol, exc)
        meta["error"] = str(exc)
        return _unavailable_aggressor(), _unavailable_aggressor(), meta


def _step_delta_ratios_from_trades(trades, state) -> Dict[int, float | None]:
    """Delta ratio per active ladder step window from aggTrade side."""
    ratios: Dict[int, float | None] = {}
    legs = list(getattr(state, "legs", None) or [])
    for idx, leg in enumerate(legs):
        step = int(getattr(leg, "step", idx))
        start = _leg_ts_ms(leg)
        end = _leg_ts_ms(legs[idx + 1]) if idx + 1 < len(legs) else None
        ratios[step] = _aggressor_metrics_from_trades(trades, start, end).get("delta_ratio")
    return ratios


def _per_step_aggressor_table_from_trades(trades, state, metric_end_ms: int) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    legs = sorted(list(getattr(state, "legs", None) or []), key=_leg_ts_ms)
    if not legs:
        return out
    p0_ts = _leg_ts_ms(legs[0])
    final_end = int(metric_end_ms) + 1
    for idx, leg in enumerate(legs):
        step_n = int(getattr(leg, "step", idx))
        start = _leg_ts_ms(leg)
        end = _leg_ts_ms(legs[idx + 1]) if idx + 1 < len(legs) else final_end
        step_snap = _aggressor_metrics_from_trades(trades, start, end)
        ladder_snap = _aggressor_metrics_from_trades(trades, p0_ts, end)
        row = dict(step_snap)
        row["ladder_buy_vwap"] = ladder_snap.get("buy_vwap")
        row["ladder_sell_vwap"] = ladder_snap.get("sell_vwap")
        row["ladder_delta_ratio"] = ladder_snap.get("delta_ratio")
        row["ladder_buy_base"] = ladder_snap.get("buy_base")
        row["ladder_sell_base"] = ladder_snap.get("sell_base")
        row["is_active"] = idx == len(legs) - 1
        out[step_n] = row
    return out


def _step_delta_ratios(candles, state) -> Dict[int, float | None]:
    return {int(getattr(leg, "step", idx)): None for idx, leg in enumerate(list(getattr(state, "legs", None) or []))}


# ---------------------------------------------------------------------------
# Canonical /backtest aggressive-flow metrics — kline primitive path
# (File 2 definition: BUY = SUM(taker_buy_base), SELL = SUM(volume-taker_buy_base),
# DELTA = BUY - SELL, DELTA_RATIO = (BUY - SELL) / (BUY + SELL), base volume).
#
# Window semantics:
#   Completed step i:    [leg_i.ts_ms, leg_(i+1).ts_ms)         exclusive on right
#   Current active step: [leg_last.ts_ms, last_complete_candle_open_time + 60_000)
#   Whole ladder:        [P0.ts_ms, last_complete_candle_open_time + 60_000)
# Only COMPLETE 1m candles contribute.
# ---------------------------------------------------------------------------


def _aggressor_metrics_from_candles(
    candles, start_ms: int, end_ms_exclusive: int | None = None
) -> Dict[str, Any]:
    """Canonical /backtest aggressive-flow metrics from Binance 1m kline primitives.

    Kline row layout (matches ``goldenfibo.KlineCache.read_range``):
        [open_time, open, high, low, close, volume, close_time,
         quote_volume, trades, taker_buy_base, taker_buy_quote, ignore]
    """
    buy_base = sell_base = buy_quote = sell_quote = 0.0
    start = int(start_ms)
    end = None if end_ms_exclusive is None else int(end_ms_exclusive)
    count = 0
    for k in candles:
        ts = int(k[0])
        if ts < start:
            continue
        if end is not None and ts >= end:
            continue
        base = _safe_float(k[5])
        if base <= 0:
            continue
        tbb = _safe_float(k[9])
        tbq = _safe_float(k[10])
        quote = _safe_float(k[7])
        count += 1
        buy_base += tbb
        sell_base += base - tbb
        buy_quote += tbq
        sell_quote += quote - tbq
    total = buy_base + sell_base
    delta = buy_base - sell_base
    status = "COMPLETE" if total > 0 else ("EMPTY" if count == 0 else "COMPLETE")
    return {
        "status": status,
        "buy_base": buy_base,
        "sell_base": sell_base,
        "total_base": total,
        "buy_quote": buy_quote,
        "sell_quote": sell_quote,
        "buy_vwap": (buy_quote / buy_base) if buy_base > 0 else None,
        "sell_vwap": (sell_quote / sell_base) if sell_base > 0 else None,
        "delta": delta,
        "delta_ratio": (delta / total) if total > 0 else None,
    }


def _step_aggressor_metrics_from_candles(candles, state, step: int) -> Dict[str, Any] | None:
    """Per-completed-step snapshot from candles. Window = [leg_i.ts_ms, leg_(i+1).ts_ms)."""
    legs = list(getattr(state, "legs", None) or [])
    leg = None
    for L in legs:
        if int(getattr(L, "step", -1)) == int(step):
            leg = L
            break
    if leg is None:
        return None
    try:
        start_ts = _leg_ts_ms(leg)
    except AttributeError:
        return None
    # Find the next leg's ts to bound the window exclusive on the right.
    leg_indexes = sorted(range(len(legs)), key=lambda i: _leg_ts_ms(legs[i]))
    end_ts = None
    for idx in leg_indexes:
        if int(getattr(legs[idx], "step", -1)) == int(step):
            nxt = idx + 1
            if nxt < len(leg_indexes):
                end_ts = _leg_ts_ms(legs[leg_indexes[nxt]])
            break
    return _aggressor_metrics_from_candles(candles, start_ts, end_ts)


def _active_step_aggressor_metrics_from_candles(candles, state) -> Dict[str, Any]:
    """Active (current) step window = [leg_last.ts_ms, last_complete_candle_open_time + 60_000)."""
    legs = list(getattr(state, "legs", None) or [])
    if not legs:
        return _aggressor_metrics_from_candles([], 0, None)
    last_leg = max(legs, key=lambda L: _leg_ts_ms(L))
    start_ts = _leg_ts_ms(last_leg)
    if not candles:
        return _aggressor_metrics_from_candles([], start_ts, None)
    last_complete_open = int(candles[-1][0])
    end_ts = last_complete_open + 60_000
    return _aggressor_metrics_from_candles(candles, start_ts, end_ts)


def _ladder_aggressor_metrics_from_candles(candles, state) -> Dict[str, Any]:
    """Ladder window = [P0.ts_ms, last_complete_candle_open_time + 60_000)."""
    legs = list(getattr(state, "legs", None) or [])
    if not legs:
        return _aggressor_metrics_from_candles([], 0, None)
    p0_leg = min(legs, key=lambda L: _leg_ts_ms(L))
    start_ts = _leg_ts_ms(p0_leg)
    if not candles:
        return _aggressor_metrics_from_candles([], start_ts, None)
    end_ts = int(candles[-1][0]) + 60_000
    return _aggressor_metrics_from_candles(candles, start_ts, end_ts)


def _per_step_aggressor_table(candles, state) -> Dict[int, Dict[str, Any]]:
    """Build a dict[step] -> aggressor snapshot for every completed leg, plus
    the current active leg's snapshot using the active-step window.

    Each entry contains both scopes so the per-step history table can show
    both L (ladder cumulative) and S (step window) values:
        - "buy_vwap" / "sell_vwap" / "delta_ratio" / "buy_base" / "sell_base":
              step-window metrics (current step only)
        - "ladder_buy_vwap" / "ladder_sell_vwap" / "ladder_delta_ratio"
          / "ladder_buy_base" / "ladder_sell_base":
              cumulative metrics from P0 through that step's end boundary
    """
    out: Dict[int, Dict[str, Any]] = {}
    legs = list(getattr(state, "legs", None) or [])
    leg_indexes = sorted(range(len(legs)), key=lambda i: _leg_ts_ms(legs[i]))
    # P0 start timestamp (anchor for cumulative ladder metrics).
    p0_ts = _leg_ts_ms(legs[leg_indexes[0]]) if leg_indexes else 0

    for idx_pos, idx in enumerate(leg_indexes):
        leg = legs[idx]
        step_n = int(getattr(leg, "step", -1))
        leg_ts = _leg_ts_ms(leg)
        # Step window: [leg_ts, next_leg_ts) — end boundary is the next leg's ts,
        # or candles[-1][0] + 60_000 for the highest filled (active) leg.
        if idx_pos + 1 < len(leg_indexes):
            end_ts = _leg_ts_ms(legs[leg_indexes[idx_pos + 1]])
            is_active = False
        else:
            end_ts = int(candles[-1][0]) + 60_000 if candles else leg_ts + 60_000
            is_active = True
        step_snap = _aggressor_metrics_from_candles(candles, leg_ts, end_ts)
        # Ladder cumulative metrics from P0 through this step's end boundary.
        ladder_snap = _aggressor_metrics_from_candles(candles, p0_ts, end_ts)
        merged = dict(step_snap)
        merged["ladder_buy_vwap"] = ladder_snap.get("buy_vwap")
        merged["ladder_sell_vwap"] = ladder_snap.get("sell_vwap")
        merged["ladder_delta_ratio"] = ladder_snap.get("delta_ratio")
        merged["ladder_buy_base"] = ladder_snap.get("buy_base")
        merged["ladder_sell_base"] = ladder_snap.get("sell_base")
        merged["is_active"] = is_active
        out[step_n] = merged
    return out


def _aggressor_summary_lines_v2(
    *, ladder: Dict[str, Any], step: Dict[str, Any], side: Side
) -> List[str]:
    """Canonical /backtest aggressive-flow summary lines (File 2 §7).

    Labels are absolute market-aggressor labels and NEVER invert with
    GoldenFibo direction (File 2 §11).
    """
    def _line(label: str, m: Dict[str, Any], key: str) -> str:
        status = m.get("status")
        if status and status != "COMPLETE":
            return f"{label}: unavailable ({status})"
        v = m.get(key)
        if v is None:
            return f"{label}: nan"
        return f"{label}: {_fmt(float(v))}"

    def _ratio_line(label: str, m: Dict[str, Any]) -> str:
        status = m.get("status")
        if status and status != "COMPLETE":
            return f"{label}: unavailable ({status})"
        v = m.get("delta_ratio")
        if v is None:
            return f"{label}: nan"
        return f"{label}: {_fmt_delta(v)}"

    lines = [
        "",
        "AGGRESSIVE FLOW (canonical /backtest, base volume)",
        "",
        "Whole Ladder (P0 -> now)",
        _line("  Buy VWAP", ladder, "buy_vwap"),
        _line("  Sell VWAP", ladder, "sell_vwap"),
        f"  Buy Vol:     {_fmt_metric(ladder.get('buy_base'))}",
        f"  Sell Vol:    {_fmt_metric(ladder.get('sell_base'))}",
        f"  Delta:       {_fmt_metric(ladder.get('delta'))}",
        _ratio_line("  Delta Ratio", ladder),
        "",
        "Current Step",
        _line("  Buy VWAP", step, "buy_vwap"),
        _line("  Sell VWAP", step, "sell_vwap"),
        f"  Buy Vol:     {_fmt_metric(step.get('buy_base'))}",
        f"  Sell Vol:    {_fmt_metric(step.get('sell_base'))}",
        f"  Delta:       {_fmt_metric(step.get('delta'))}",
        _ratio_line("  Delta Ratio", step),
    ]
    return lines


def _step_delta_color(delta_ratio: float | None) -> str:
    if delta_ratio is None:
        return "#666666"
    d = float(delta_ratio)
    if d >= 0.5:
        return DARKBLUE
    if d > 0:
        return LIGHTBLUE
    if d <= -0.5:
        return DARKRED
    if d < 0:
        return LIGHTRED
    return "#666666"


def _fmt_metric(value: float | None) -> str:
    if value is None:
        return "nan"
    try:
        return _fmt(float(value))
    except (TypeError, ValueError):
        return "nan"


def _fmt_delta(value: float | None) -> str:
    if value is None:
        return "nan"
    try:
        return f"{float(value):+.4f}"
    except (TypeError, ValueError):
        return "nan"


def _aggressor_vwap_label(side: Side, prefix: str) -> str:
    return f"{prefix}-B-VWAP" if side is Side.SELL else f"{prefix}-S-VWAP"


def _aggressor_side_vwap(side: Side, metrics: Dict[str, Any]) -> float | None:
    return metrics.get("buy_vwap") if side is Side.SELL else metrics.get("sell_vwap")


def _aggressor_summary_line(side: Side, prefix: str, metrics: Dict[str, Any]) -> str:
    label = _aggressor_vwap_label(side, prefix)
    status = metrics.get("status")
    if status and status != "COMPLETE":
        return f"{label}: unavailable ({status})"
    return f"{label}: {_fmt_metric(_aggressor_side_vwap(side, metrics))}"


def _aggressor_summary_lines(side: Side, ladder_window: Dict[str, Any], step_window: Dict[str, Any], ladder_aggressor: Dict[str, Any], step_aggressor: Dict[str, Any]) -> List[str]:
    step_status = step_aggressor.get("status")
    if step_status and step_status != "COMPLETE":
        step_delta = f"Step Delta Ratio: unavailable ({step_status})"
    else:
        step_delta = f"Step Delta Ratio: {_fmt_delta(step_aggressor.get('delta_ratio'))}"
    return [
        f"Ladder VWAP: {_fmt_metric(ladder_window.get('all_vwap'))}",
        _aggressor_summary_line(side, "L", ladder_aggressor),
        f"Step VWAP: {_fmt_metric(step_window.get('all_vwap'))}",
        _aggressor_summary_line(side, "S", step_aggressor),
        step_delta,
    ]


def _leg_ts_ms(leg: Any) -> int:
    ts = getattr(leg, "ts", None)
    if ts is None:
        ts = getattr(leg, "ts_ms", None)
    if ts is None:
        raise AttributeError("leg has neither ts nor ts_ms")
    return int(ts)


def _active_ladder_start_ts(state) -> int:
    """Return the timestamp of the currently open cycle's P0 leg.

    Closed cycles/ladders must not contribute to ladder VWAP/POC or the
    ladder volume area. ``replay_ohlc`` replaces ``state.legs`` whenever a TP
    closes a cycle and chains a new P0, so the first current leg is the active
    P0 open time.

    Ladder VWAP and Ladder POC MUST share this exact start timestamp.
    """
    if not getattr(state, "legs", None):
        raise ValueError("active ladder has no open P0 leg")
    return _leg_ts_ms(state.legs[0])


def _active_step_start_ts(state) -> int:
    """Return the timestamp when the current active step P(n) became filled.

    ``state.legs[-1]`` is the highest filled step for the open cycle. Active
    Step VWAP and Active Step POC MUST share this exact start timestamp.
    When only P0 is open, this equals the ladder (P0) start.
    """
    if not getattr(state, "legs", None):
        raise ValueError("active ladder has no open step leg")
    return _leg_ts_ms(state.legs[-1])


def _volume_profile(candles, ts, bins: int = 160):
    """Approximate volume profile bins from OHLCV candles.

    Binance 1m candles do not expose tick-level volume-at-price, so each
    candle's base volume is distributed across price bins by high-low overlap
    for the requested active window.
    """
    rel=[k for k in candles if int(k[0])>=ts]
    if not rel:
        return None
    lo=min(float(k[3]) for k in rel); hi=max(float(k[2]) for k in rel)
    if hi <= lo:
        close=float(rel[-1][4])
        return {"lo":close, "hi":close, "width":1.0, "vols":[sum(float(k[5]) for k in rel)]}
    bins=max(20, int(bins))
    width=(hi-lo)/bins
    vols=[0.0 for _ in range(bins)]
    for k in rel:
        h=float(k[2]); l=float(k[3]); v=float(k[5])
        if v <= 0:
            continue
        if h <= l:
            idx=min(bins-1,max(0,int((float(k[4])-lo)/width)))
            vols[idx]+=v
            continue
        a=max(0,int((l-lo)/width)); b=min(bins-1,int((h-lo)/width))
        span=h-l
        for idx in range(a,b+1):
            bin_lo=lo+idx*width; bin_hi=bin_lo+width
            overlap=max(0.0, min(h,bin_hi)-max(l,bin_lo))
            if overlap > 0:
                vols[idx]+=v*(overlap/span)
    return {"lo":lo, "hi":hi, "width":width, "vols":vols}


def _poc(candles, ts, bins: int = 160):
    """Approximate volume-profile point of control from active-window OHLCV."""
    profile=_volume_profile(candles, ts, bins=bins)
    if not profile:
        return float("nan")
    vols=profile["vols"]
    if not vols:
        return float("nan")
    idx=max(range(len(vols)), key=lambda i: vols[i])
    return float(profile["lo"]) + (idx + 0.5) * float(profile["width"])


def _value_area(candles, ts, bins: int = 160, ratio: float = 0.70):
    """Approximate active-window Value Area around POC.

    Returns VAL/POC/VAH using the standard profile expansion: start at POC,
    then add the higher-volume adjacent side until ``ratio`` of total profile
    volume is covered.
    """
    profile=_volume_profile(candles, ts, bins=bins)
    if not profile:
        return {"val":float("nan"), "poc":float("nan"), "vah":float("nan"), "covered_volume":0.0, "total_volume":0.0}
    vols=list(profile["vols"])
    total=sum(vols)
    if total <= 0:
        p=float(profile["lo"])
        return {"val":p, "poc":p, "vah":p, "covered_volume":0.0, "total_volume":0.0}
    poc_idx=max(range(len(vols)), key=lambda i: vols[i])
    left=right=poc_idx
    covered=vols[poc_idx]
    target=total*max(0.0,min(1.0,float(ratio)))
    while covered < target and (left > 0 or right < len(vols)-1):
        left_vol=vols[left-1] if left > 0 else -1.0
        right_vol=vols[right+1] if right < len(vols)-1 else -1.0
        if right_vol > left_vol:
            right += 1; covered += vols[right]
        else:
            left -= 1; covered += vols[left]
    lo=float(profile["lo"]); width=float(profile["width"])
    return {
        "val": lo + left * width,
        "poc": lo + (poc_idx + 0.5) * width,
        "vah": lo + (right + 1) * width,
        "covered_volume": covered,
        "total_volume": total,
    }


def _fmt(x: float) -> str:
    return f"{x:,.2f}"


def _summarize_side(candles, side: Side, symbol: str, market: str, percentage: float):
    result = run_finite_backtest(
        candles,
        side=side,
        percentage=percentage,
        symbol=symbol,
        timeframe=_BACKTEST_TIMEFRAME,
    )
    payload = result.state_payload
    n = int(payload.get("n") or 0)
    p0 = result.engine.state.p0
    pn = result.engine.state.current_p()
    pnm1 = result.engine.state.shared_tp if n >= 1 else result.engine.state.shared_tp
    pn1 = result.engine.state.next_p()
    pn2 = result.engine.state.further_p()
    ladder_vwap = result.metric_display.ladder_vwap
    step_vwap = result.metric_display.step_vwap
    ladder_poc = result.metric_display.ladder_poc
    step_poc = result.metric_display.step_poc
    ladder_val = result.metric_display.ladder_val
    ladder_vah = result.metric_display.ladder_vah
    ladder_start_ts = _active_ladder_start_ts(result.engine.state)
    step_start_ts = _active_step_start_ts(result.engine.state)
    aggtrade_end_ts = int(candles[-1][0]) + 60_000 - 1
    ladder_window = {"all_vwap": ladder_vwap}
    step_window = {"all_vwap": step_vwap}
    ladder_aggressor, step_aggressor, agg_meta = _aggtrade_aggressor_metrics_for_display(
        _BACKTEST_AGGTRADE_CACHE,
        market,
        symbol,
        ladder_start_ts,
        step_start_ts,
        aggtrade_end_ts,
    )
    per_step_aggressor: Dict[int, Dict[str, Any]] = {}
    step_delta_ratios: Dict[int, float | None] = {}
    if agg_meta.get("covered"):
        try:
            required_start = int(agg_meta["required_start_ms"])
            required_end = int(agg_meta["required_end_ms"])
            agg_trades = _BACKTEST_AGGTRADE_CACHE.query_trades_range(market, symbol, required_start, required_end)
            per_step_aggressor = _per_step_aggressor_table_from_trades(agg_trades, result.engine.state, aggtrade_end_ts)
            step_delta_ratios = {
                int(s): row.get("delta_ratio") for s, row in per_step_aggressor.items()
            }
        except Exception as exc:
            logger.warning("aggTrade per-step display unavailable for %s %s: %s", market, symbol, exc)
    last = float(candles[-1][4])
    label = "BUY" if side is Side.BUY else "SELL"
    lines = [
        f"{label} ladder",
        f"Cycle: {payload.get('cycle_id')}  Completed: {payload.get('closed_count')}",
        f"P0: {_fmt(float(p0)) if p0 is not None else 'nan'}",
        f"Current step: P{n} = {_fmt(float(pn)) if pn is not None else 'nan'}",
        f"TP/P(n-1): {_fmt(float(pnm1)) if pnm1 is not None else 'nan'}",
        f"Next P{n+1}: {_fmt(float(pn1)) if pn1 is not None else 'nan'}",
        f"P{n+2}: {_fmt(float(pn2)) if pn2 is not None else 'nan'}",
        *_aggressor_summary_lines(side, ladder_window, step_window, ladder_aggressor, step_aggressor),
        *_aggressor_summary_lines_v2(ladder=ladder_aggressor, step=step_aggressor, side=side),
        f"Ladder POC: {_fmt(float(ladder_poc)) if ladder_poc is not None else 'nan'}",
        f"Ladder Value Area: {_fmt(float(ladder_val)) if ladder_val is not None else 'nan'} → {_fmt(float(ladder_vah)) if ladder_vah is not None else 'nan'}",
        f"Step POC: {_fmt(float(step_poc)) if step_poc is not None else 'nan'}",
        f"Last close: {_fmt(last)}",
        "",
        "Per-step aggressive-flow history (L = ladder cumulative, S = step window):",
    ]
    # Append a compact per-step table.
    if per_step_aggressor:
        rows = []
        rows.append("Step | L-BVWAP     | L-SVWAP     | L-ΔR     | S-BVWAP     | S-SVWAP     | S-ΔR")
        for s_idx in sorted(per_step_aggressor.keys()):
            s = per_step_aggressor[s_idx]
            def _v(x):
                return "nan" if x is None else (f"{x:,.2f}" if abs(x) >= 1 else f"{x:.4f}")
            rows.append(
                f"P{s_idx:<3}| {_v(s.get('ladder_buy_vwap')):>11} | {_v(s.get('ladder_sell_vwap')):>11} | "
                f"{_v(s.get('ladder_delta_ratio')):>8} | {_v(s.get('buy_vwap')):>11} | {_v(s.get('sell_vwap')):>11} | "
                f"{_v(s.get('delta_ratio')):>8}"
            )
        lines.extend(rows)
    # Rebuild the chart summary locally; formatting only, values come from canonical result.
    levels = []
    for item in payload.get("levels") or []:
        price = item.get("price")
        levels.append(
            {
                "level": item.get("id") or item.get("level"),
                "price": float(price) if price is not None else float("nan"),
                "role": item.get("label") or item.get("role") or "",
            }
        )
    jpg = _draw_jpg(
        symbol,
        market,
        side,
        levels,
        n,
        ladder_vwap,
        step_vwap,
        ladder_poc,
        step_poc,
        last,
        ladder_value_area={"val": ladder_val, "vah": ladder_vah},
        step_delta_ratios=step_delta_ratios,
        ladder_aggressor=ladder_aggressor,
        step_aggressor=step_aggressor,
        ladder_buy_vwap=ladder_aggressor.get("buy_vwap"),
        ladder_sell_vwap=ladder_aggressor.get("sell_vwap"),
        step_buy_vwap=step_aggressor.get("buy_vwap"),
        step_sell_vwap=step_aggressor.get("sell_vwap"),
        step_aggressor_table=per_step_aggressor,
    )
    return {"text": "\n".join(lines), "svg": jpg}


def _draw_jpg(
    symbol,
    market,
    side,
    levels,
    n,
    ladder_vwap,
    step_vwap,
    ladder_poc,
    step_poc,
    current,
    *,
    ladder_value_area=None,
    step_delta_ratios=None,
    ladder_aggressor=None,
    step_aggressor=None,
    ladder_buy_vwap=None,
    ladder_sell_vwap=None,
    step_buy_vwap=None,
    step_sell_vwap=None,
    step_aggressor_table=None,
):
    """Draw visual ladder summary directly to JPEG for Telegram photo delivery."""
    from PIL import Image, ImageDraw, ImageFont

    W,H=1200,1600; left,right=170,1100; top,bottom=110,1210
    va_prices=[]
    if ladder_value_area:
        va_prices=[float(ladder_value_area.get('val', float('nan'))), float(ladder_value_area.get('vah', float('nan')))]
        va_prices=[p for p in va_prices if p == p]
    prices=[float(x['price']) for x in levels]+[ladder_vwap,step_vwap,ladder_poc,step_poc,current]+va_prices
    # Include aggressive-flow VWAPs in the price-bounds calculation if numeric.
    for extra in (ladder_buy_vwap, ladder_sell_vwap, step_buy_vwap, step_sell_vwap):
        try:
            f = float(extra)
            if f == f:  # not NaN
                prices.append(f)
        except (TypeError, ValueError):
            pass
    prices=[float(p) for p in prices if p is not None and float(p) == float(p)]
    pmin=min(prices); pmax=max(prices); pad=(pmax-pmin)*0.07 or 1; pmin-=pad; pmax+=pad
    def y(p): return bottom-(float(p)-pmin)/(pmax-pmin)*(bottom-top)
    def font(path, size):
        try: return ImageFont.truetype(path, size)
        except Exception: return ImageFont.load_default()
    fb=font('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',30)
    fm=font('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',18)
    fmb=font('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',18)
    fs=font('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',15)
    fmono=font('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',13)
    img=Image.new('RGB',(W,H),'#fbfbf8'); d=ImageDraw.Draw(img)
    label='BUY' if side is Side.BUY else 'SELL'
    d.text((60,35),f'{symbol} {market.upper()} {label} GoldenFibo Backtest',fill='#111',font=fb)
    d.text((60,72),'Visual summary: P0 at bottom, active ladder value area, active P(n), VWAPs, POCs, and current price',fill='#555',font=fs)
    d.rectangle([left,top,right,bottom],fill='white',outline='#ddd')
    # Active ladder Value Area: shade VAL→VAH for the current open ladder's
    # active-window volume profile. This is not the full displayed ladder range
    # and does not include future P(n+1)/P(n+2) levels unless their prices fall
    # inside the computed value area.
    if ladder_value_area:
        val=float(ladder_value_area.get('val', float('nan')))
        vah=float(ladder_value_area.get('vah', float('nan')))
        if val == val and vah == vah:
            area_top=min(y(val), y(vah)); area_bottom=max(y(val), y(vah))
            d.rectangle([left,area_top,right,area_bottom],fill='#ffe6ef')
            d.rounded_rectangle([left+12,area_top+8,left+330,area_top+34],radius=6,fill='white')
            d.text((left+22,area_top+10),f'Value Area 70%: {_fmt(val)}–{_fmt(vah)}',fill='#c24b75',font=fmb)
    for j in range(8):
        price=pmin+(pmax-pmin)*j/7; yy=y(price)
        d.line([left,yy,right,yy],fill='#eee',width=1)
        txt=_fmt(price); bbox=d.textbbox((0,0),txt,font=fmono); d.text((left-12-bbox[2],yy-7),txt,fill='#888',font=fmono)
    line_color='#77aaff' if side is Side.BUY else '#ff7a7a'; active='#0058ff' if side is Side.BUY else '#d71920'
    xmid=left+65
    d.line([xmid,y(float(levels[0]['price'])),xmid,y(float(levels[-1]['price']))],fill=line_color,width=3)
    step_delta_ratios = dict(step_delta_ratios or {})
    per_step = dict(step_aggressor_table or {})
    level_labels=[]
    for item in levels:
        i=int(str(item['level'])[1:]); p=float(item['price']); yy=y(p); is_active=i==n
        col=active if is_active else line_color
        d.line([left,yy,right,yy],fill=col,width=5 if is_active else 2)
        d.ellipse([xmid-7,yy-7,xmid+7,yy+7],fill=col,outline='white',width=2)
        role=item.get('role') or ''
        ratio = step_delta_ratios.get(i)
        # Keep the price chart focused on prices/roles. Historical per-step
        # aggressive-flow ΔR values are rendered in the dedicated panel below so
        # dense Fibonacci levels cannot collide with flow-history text.
        ratio_txt = ''
        if is_active:
            s_dr = (step_aggressor or {}).get('delta_ratio')
            l_dr = (ladder_aggressor or {}).get('delta_ratio')
            if ratio is not None:
                ratio_txt = f"  ΔR {_fmt_delta(ratio)}"
            if s_dr is not None:
                ratio_txt += f"  S-ΔR {_fmt_delta(s_dr)}"
            if l_dr is not None:
                ratio_txt += f"  L-ΔR {_fmt_delta(l_dr)}"
        txt=f"{item['level']} {_fmt(p)}" + (f"  {role}" if role else '') + ratio_txt
        f=fmb if is_active else fm
        fill=_step_delta_color(ratio) if ratio is not None else (active if is_active else '#933')
        level_labels.append({"txt": txt, "font": f, "fill": fill, "target_y": yy-25, "line_y": yy, "active": is_active})
    # Resolve vertical collisions among Pn labels by shifting label baselines and
    # drawing short leader ticks back to the actual price line. The line itself
    # remains at the exact price.
    min_gap=28
    if level_labels:
        level_labels.sort(key=lambda r: r["target_y"])
        lo=top+6; hi=bottom-26
        for rec in level_labels:
            rec["draw_y"] = max(lo, min(hi, rec["target_y"]))
        for idx in range(1, len(level_labels)):
            if level_labels[idx]["draw_y"] < level_labels[idx-1]["draw_y"] + min_gap:
                level_labels[idx]["draw_y"] = level_labels[idx-1]["draw_y"] + min_gap
        overflow = level_labels[-1]["draw_y"] - hi
        if overflow > 0:
            for rec in level_labels:
                rec["draw_y"] -= overflow
            for idx in range(len(level_labels)-2, -1, -1):
                if level_labels[idx]["draw_y"] > level_labels[idx+1]["draw_y"] - min_gap:
                    level_labels[idx]["draw_y"] = level_labels[idx+1]["draw_y"] - min_gap
            underflow = lo - level_labels[0]["draw_y"]
            if underflow > 0:
                for rec in level_labels:
                    rec["draw_y"] += underflow
        for rec in level_labels:
            bbox=d.textbbox((0,0),rec["txt"],font=rec["font"])
            x=right-12-bbox[2]
            yy=rec["line_y"]; dy=rec["draw_y"]
            if abs(dy-(yy-25)) > 2:
                d.line([right-122, yy, right-92, dy+12], fill='#bbb', width=1)
            d.text((x,dy),rec["txt"],fill=rec["fill"],font=rec["font"])
    def dashed(x1, yy, x2, color, width=4, dash=12, gap=8):
        x=x1
        while x<x2:
            d.line([x,yy,min(x+dash,x2),yy],fill=color,width=width); x+=dash+gap
    for name,p,color,dot in [('Ladder VWAP',ladder_vwap,'#f2c500',False),('Step VWAP',step_vwap,'#f2c500',True),('Current',current,'#111',False)]:
        yy=y(p)
        if dot: dashed(left,yy,right,color)
        else: d.line([left,yy,right,yy],fill=color,width=5)
        d.rounded_rectangle([left+12,yy-30,left+390,yy-4],radius=6,fill='white')
        d.text((left+22,yy-28),f'{name}: {_fmt(p)}',fill='#9a7800' if color=='#f2c500' else color,font=fmb)
    # Point of Control overlays: ladder POC solid green, active-step POC dotted green.
    for name,p,dot in [('Ladder POC',ladder_poc,False),('Step POC',step_poc,True)]:
        yy=y(p)
        if dot:
            dashed(left,yy,right,'#138a36',width=3,dash=8,gap=8)
        else:
            d.line([left,yy,right,yy],fill='#138a36',width=3)
        d.rounded_rectangle([left+410,yy-30,left+750,yy-4],radius=6,fill='white')
        d.text((left+420,yy-28),f'{name}: {_fmt(p)}',fill='#138a36',font=fmb)
    # Aggressive-flow VWAP overlays (File 2). Distinct from ordinary VWAP.
    # Ladder Buy = solid magenta, Ladder Sell = solid teal, Step Buy/Sell = dashed.
    try:
        def _draw_vwap_line(name, price_val, color, is_dashed):
            try:
                fv = float(price_val)
            except (TypeError, ValueError):
                return
            if fv != fv:  # NaN
                return
            yyv = y(fv)
            if is_dashed:
                dashed(left, yyv, right, color, width=2, dash=6, gap=6)
            else:
                d.line([left, yyv, right, yyv], fill=color, width=2)
            d.rounded_rectangle([left+820, yyv-18, left+1170, yyv+4], radius=5, fill='white')
            d.text((left+828, yyv-16), f'{name}: {_fmt(fv)}', fill=color, font=fs)

        _draw_vwap_line('L-B-VWAP', ladder_buy_vwap, '#c050c0', False)
        _draw_vwap_line('L-S-VWAP', ladder_sell_vwap, '#008b8b', False)
        _draw_vwap_line('S-B-VWAP', step_buy_vwap, '#c050c0', True)
        _draw_vwap_line('S-S-VWAP', step_sell_vwap, '#008b8b', True)
    except Exception:
        logger.exception("aggressive-flow VWAP overlay failed")

    # Dedicated aggressive-flow history panel. Historical per-step L-ΔR/S-ΔR
    # snapshots live here instead of beside price lines, preventing collisions
    # when reached Fibonacci levels are vertically dense.
    try:
        panel_top=1230; panel_bottom=1484
        d.rounded_rectangle([60,panel_top,1140,panel_bottom],radius=12,fill='#ffffff',outline='#d6d6d6',width=2)
        d.text((78,panel_top+10),'Current active-step aggressive flow',fill='#222',font=fmb)
        lag = ladder_aggressor or {}
        sag = step_aggressor or {}
        d.text((78,panel_top+38),
               f"L-B-VWAP {_fmt_metric(lag.get('buy_vwap'))}   L-S-VWAP {_fmt_metric(lag.get('sell_vwap'))}   L-ΔR {_fmt_delta(lag.get('delta_ratio'))}",
               fill='#222',font=fm)
        d.text((78,panel_top+64),
               f"S-B-VWAP {_fmt_metric(sag.get('buy_vwap'))}   S-S-VWAP {_fmt_metric(sag.get('sell_vwap'))}   S-ΔR {_fmt_delta(sag.get('delta_ratio'))}",
               fill='#222',font=fm)
        d.line([78,panel_top+94,1122,panel_top+94],fill='#e4e4e4',width=2)
        d.text((78,panel_top+106),'Historical frozen per-step flow snapshots',fill='#222',font=fmb)
        reached_steps=sorted([int(k) for k in per_step.keys() if int(k) <= int(n)])
        cols=[reached_steps[:5], reached_steps[5:10], reached_steps[10:15]]
        col_x=[78,440,802]
        header_y=panel_top+122
        row_h=21
        for ci, steps in enumerate(cols):
            if not steps:
                continue
            x0=col_x[ci]
            d.text((x0,header_y),'Pn',fill='#555',font=fmb)
            d.text((x0+80,header_y),'L-ΔR',fill='#555',font=fmb)
            d.text((x0+210,header_y),'S-ΔR',fill='#555',font=fmb)
            for ri, step_n in enumerate(steps):
                row=per_step.get(step_n) or {}
                yy=header_y+28+ri*row_h
                d.text((x0,yy),f'P{step_n}',fill='#222',font=fm)
                d.text((x0+80,yy),_fmt_delta(row.get('ladder_delta_ratio')),fill=_step_delta_color(row.get('ladder_delta_ratio')),font=fm)
                d.text((x0+210,yy),_fmt_delta(row.get('delta_ratio')),fill=_step_delta_color(row.get('delta_ratio')),font=fm)
    except Exception:
        logger.exception("aggressive-flow history panel failed")

    pn=float(levels[n]['price']); tp=float(levels[n-1]['price']) if n>=1 else float(levels[0]['price'])
    pn1=float(levels[n+1]['price']) if n+1 < len(levels) else pn
    pn2=float(levels[n+2]['price']) if n+2 < len(levels) else pn1
    d.text((60,1500),f'Active: P{n}={_fmt(pn)} · TP=P{max(n-1,0)}={_fmt(tp)} · Next P{n+1}={_fmt(pn1)} · P{n+2}={_fmt(pn2)}',fill='#222',font=fmb)
    d.text((60,1532),f'VWAP: ladder={_fmt(ladder_vwap)} · active step={_fmt(step_vwap)} · POC: ladder={_fmt(ladder_poc)} · step={_fmt(step_poc)} · current={_fmt(current)}',fill='#222',font=fmb)
    if ladder_aggressor or step_aggressor:
        lag = ladder_aggressor or {}
        sag = step_aggressor or {}
        if sag.get("status") and sag.get("status") != "COMPLETE":
            step_delta = f'Step Delta Ratio: unavailable ({sag.get("status")})'
        else:
            step_delta = f'Step Delta Ratio: {_fmt_delta(sag.get("delta_ratio"))}'
        summary = f'{_aggressor_summary_line(side, "L", lag)} · {_aggressor_summary_line(side, "S", sag)} · {step_delta}'
        d.text((60,1560),summary,fill='#222',font=fs)
    # Aggressive-flow summary line (File 2 canonical).
    try:
        lag = ladder_aggressor or {}
        sag = step_aggressor or {}
        aggr_line = (
            f"AGGRESSIVE FLOW  "
            f"L-BVWAP={_fmt_metric(lag.get('buy_vwap'))}  "
            f"L-SVWAP={_fmt_metric(lag.get('sell_vwap'))}  "
            f"L-ΔR={_fmt_delta(lag.get('delta_ratio'))}  "
            f"S-BVWAP={_fmt_metric(sag.get('buy_vwap'))}  "
            f"S-SVWAP={_fmt_metric(sag.get('sell_vwap'))}  "
            f"S-ΔR={_fmt_delta(sag.get('delta_ratio'))}"
        )
        d.text((60,1582), aggr_line, fill='#222', font=fs)
    except Exception:
        logger.exception("aggressive-flow summary line failed")
    out=_OUT/f"backtest_{symbol}_{market}_{side.value.lower()}_{int(time.time()*1000)}.jpg"
    img.save(out, 'JPEG', quality=92, optimize=True)
    return str(out)


def _draw_svg(symbol, market, side, levels, n, ladder_vwap, step_vwap, current):
    W,H=1200,1600; left,right=170,1100; top,bottom=110,1440
    prices=[float(x['price']) for x in levels]+[ladder_vwap,step_vwap,current]
    pmin=min(prices); pmax=max(prices); pad=(pmax-pmin)*0.07 or 1; pmin-=pad; pmax+=pad
    def y(p): return bottom-(float(p)-pmin)/(pmax-pmin)*(bottom-top)
    title=f"{symbol} {market.upper()} {'BUY' if side is Side.BUY else 'SELL'} GoldenFibo Backtest"
    chunks=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}"><rect width="100%" height="100%" fill="#fbfbf8"/>']
    chunks.append(f'<text x="60" y="55" font-size="30" font-family="Arial" font-weight="800" fill="#111">{title}</text>')
    chunks.append(f'<rect x="{left}" y="{top}" width="{right-left}" height="{bottom-top}" fill="#fff" stroke="#ddd"/>')
    line_color="#77aaff" if side is Side.BUY else "#ff7a7a"; active="#0058ff" if side is Side.BUY else "#d71920"
    for item in levels:
        i=int(str(item['level'])[1:]); p=float(item['price']); yy=y(p); is_active=i==n
        chunks.append(f'<line x1="{left}" x2="{right}" y1="{yy:.2f}" y2="{yy:.2f}" stroke="{active if is_active else line_color}" stroke-width="{5 if is_active else 2}" opacity="{1 if is_active else .62}"/>')
        role=item.get('role') or ''
        fill_color = active if is_active else "#933"
        level_name = item["level"]
        chunks.append(f'<text x="{right-12}" y="{yy-7:.2f}" text-anchor="end" font-family="Arial" font-size="18" font-weight="{800 if is_active else 500}" fill="{fill_color}">{level_name} {_fmt(p)} {role}</text>')
    for name,p,color,dash in [("Ladder VWAP",ladder_vwap,"#f2c500",""),("Step VWAP",step_vwap,"#f2c500","10 8"),("Current",current,"#111","")]:
        yy=y(p); extra=f' stroke-dasharray="{dash}"' if dash else ''
        chunks.append(f'<line x1="{left}" x2="{right}" y1="{yy:.2f}" y2="{yy:.2f}" stroke="{color}" stroke-width="5"{extra}/><text x="{left+20}" y="{yy-8:.2f}" font-family="Arial" font-size="18" font-weight="800" fill="{color}">{name}: {_fmt(p)}</text>')
    chunks.append('</svg>')
    out=_OUT/f"backtest_{symbol}_{market}_{side.value.lower()}_{int(time.time()*1000)}.svg"
    out.write_text("".join(chunks))
    return str(out)


def _chat_key_from_message(msg: Any) -> Tuple[Any, ...]:
    chat = getattr(msg, "chat", None)
    chat_id = getattr(chat, "id", None) if chat is not None else None
    thread = getattr(msg, "message_thread_id", None)
    # Canonical Telegram key: always include the string chat id, and include
    # the thread id only when the message actually belongs to a forum thread.
    # This must match the exact shape used by both command/callback/text paths.
    return (str(chat_id), thread) if thread is not None else (str(chat_id),)

def _chat_id_from_message(msg: Any) -> Optional[str]:
    chat=getattr(msg,"chat",None); cid=getattr(chat,"id",None) if chat is not None else None
    return str(cid) if cid is not None else None

def _metadata_from_message(msg: Any) -> Optional[Dict[str, Any]]:
    thread=getattr(msg,"message_thread_id",None)
    return {"thread_id": thread} if thread is not None else None

async def _send_screen(adapter: Any, chat_id: str, screen: Screen, *, metadata: Optional[Dict[str, Any]]=None) -> None:
    send=getattr(adapter,"send_inline_keyboard",None)
    if callable(send):
        await send(chat_id=chat_id, text=screen.text, buttons=screen.buttons, callback_prefix="backtest", metadata=metadata)
    else:
        await adapter.send(chat_id, screen.text, metadata=metadata)
    # Send visual attachments. JPEG/PNG go as native Telegram photos; others as documents.
    for path in screen.attachments:
        lower = str(path).lower()
        if lower.endswith((".jpg", ".jpeg", ".png")):
            send_img = getattr(adapter, "send_image_file", None)
            if callable(send_img):
                await send_img(chat_id=chat_id, image_path=path, caption=Path(path).name, metadata=metadata)
                continue
        doc=getattr(adapter,"send_document",None)
        if callable(doc):
            await doc(chat_id=chat_id, file_path=path, caption=Path(path).name, metadata=metadata)

async def handle_backtest_command(adapter: Any, msg: Any) -> bool:
    text=(getattr(msg,"text","") or "").strip()
    if not text.startswith("/"): return False
    cmd=text.split(None,1)[0].lstrip('/').split('@',1)[0].lower()
    if cmd != "backtest": return False
    key=_chat_key_from_message(msg); screen=_WIZARD.open(key); cid=_chat_id_from_message(msg)
    if cid: await _send_screen(adapter,cid,screen,metadata=_metadata_from_message(msg))
    return True

async def handle_backtest_callback(adapter: Any, query: Any, data: str) -> None:
    try:
        suffix=data[len("backtest:"):] if data.startswith("backtest:") else data
        try: await query.answer()
        except Exception: pass
        msg=getattr(query,"message",None); key=_chat_key_from_message(msg)
        if suffix == "restart":
            screen=_WIZARD.open(key)
        elif suffix == "run":
            try: await query.edit_message_text(_WIZARD._progress_text("loading_cache", 0.0, ""), reply_markup=None)
            except Exception: pass
            try:
                from plugins.platforms.telegram.adapter import InlineKeyboardButton, InlineKeyboardMarkup
            except Exception:
                class InlineKeyboardButton:  # type: ignore
                    def __init__(self, text, callback_data=None):
                        self.text = text
                        self.callback_data = callback_data
                class InlineKeyboardMarkup:  # type: ignore
                    def __init__(self, rows):
                        self.inline_keyboard = rows
            loop = asyncio.get_running_loop()
            progress_lock = asyncio.Lock()
            def progress_cb(payload: dict):
                phase, pct, detail = _WIZARD._progress_from_payload(payload)
                stats = dict(payload.get("stats") or {})
                log_payload = {
                    "phase": phase,
                    "percent": round(pct, 3),
                    "bars_done": payload.get("bars_done"),
                    "bars_est": payload.get("bars_est"),
                    "bars_from_cache": stats.get("bars_from_cache"),
                    "bars_downloaded": stats.get("bars_downloaded"),
                    "bars_total": stats.get("bars_total"),
                    "requested_candles": payload.get("requested_candles") or stats.get("requested_candles"),
                    "download_total": payload.get("download_total") or payload.get("missing_candles") or stats.get("download_total"),
                    "download_done": payload.get("download_done") or payload.get("downloaded_candles") or stats.get("bars_downloaded"),
                    "text": _WIZARD._progress_text(phase, pct, detail),
                }
                logger.info("backtest progress payload=%s", log_payload)
                text = log_payload["text"]
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Working…", callback_data="backtest:run")]])
                async def _update():
                    async with progress_lock:
                        try:
                            await query.edit_message_text(text, reply_markup=kb)
                        except Exception:
                            pass
                asyncio.run_coroutine_threadsafe(_update(), loop)
            screen = await asyncio.to_thread(_WIZARD._run_backtest, _WIZARD._state(key), on_progress=progress_cb)
        else:
            screen=await asyncio.to_thread(_WIZARD.handle_callback,key,suffix)
        try:
            from plugins.platforms.telegram.adapter import InlineKeyboardButton, InlineKeyboardMarkup
        except Exception:
            class InlineKeyboardButton:  # type: ignore
                def __init__(self, text, callback_data=None):
                    self.text = text
                    self.callback_data = callback_data
            class InlineKeyboardMarkup:  # type: ignore
                def __init__(self, rows):
                    self.inline_keyboard = rows
        rows=[]
        for row in screen.buttons:
            br=[]
            for b in row:
                br.append(InlineKeyboardButton(str(b.get('text','')), callback_data=f"backtest:{b.get('callback_data','')}"))
            if br: rows.append(br)
        kb=InlineKeyboardMarkup(rows) if rows else None
        try:
            await query.edit_message_text(screen.text, reply_markup=kb)
        except Exception:
            cid=_chat_id_from_message(msg)
            if cid: await _send_screen(adapter,cid,screen,metadata=_metadata_from_message(msg))
        cid=_chat_id_from_message(msg)
        if cid:
            for path in screen.attachments:
                lower = str(path).lower()
                if lower.endswith((".jpg", ".jpeg", ".png")):
                    send_img = getattr(adapter, "send_image_file", None)
                    if callable(send_img):
                        await send_img(chat_id=cid, image_path=path, caption=Path(path).name, metadata=_metadata_from_message(msg))
                        continue
                doc=getattr(adapter,"send_document",None)
                if callable(doc): await doc(chat_id=cid,file_path=path,caption=Path(path).name,metadata=_metadata_from_message(msg))
    except Exception as exc:
        logger.error("backtest callback failed: %s", exc, exc_info=True)
        try:
            msg = getattr(query, "message", None)
            cid = _chat_id_from_message(msg)
            error_text = f"Backtest failed:\n{type(exc).__name__}: {exc}"
            if getattr(query, "edit_message_text", None):
                try:
                    await query.edit_message_text(error_text, reply_markup=None)
                    return
                except Exception:
                    pass
            if cid:
                await _send_screen(adapter, cid, Screen(error_text, [ _WIZARD._screen_ladder().buttons[-1] ], "error"), metadata=_metadata_from_message(msg))
        except Exception:
            pass

async def handle_backtest_text(adapter: Any, msg: Any) -> bool:
    key = _chat_key_from_message(msg)
    screen = await asyncio.to_thread(_WIZARD.handle_text, key, getattr(msg, "text", "") or "")
    if screen is None:
        return False
    cid = _chat_id_from_message(msg)
    if cid:
        await _send_screen(adapter, cid, screen, metadata=_metadata_from_message(msg))
    return True

__all__=["handle_backtest_command","handle_backtest_callback","handle_backtest_text","BacktestWizard"]
