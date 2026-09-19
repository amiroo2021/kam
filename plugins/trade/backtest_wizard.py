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
from goldenfibo.marketdata.kline_cache import CachePolicy, KlineCache, fetch_range_cached  # noqa: E402
from plugins.trade.tradedesk import TradeDesk  # noqa: E402

_BACKTEST_CACHE = KlineCache(Path("/root/kam/GoldenFibo/data/backtest_klines.sqlite"))
_BACKTEST_TIMEFRAME = "1m"
_BACKTEST_REFRESH_TAIL_MS = 0

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
    return int(state.legs[0].ts)


def _active_step_start_ts(state) -> int:
    """Return the timestamp when the current active step P(n) became filled.

    ``state.legs[-1]`` is the highest filled step for the open cycle. Active
    Step VWAP and Active Step POC MUST share this exact start timestamp.
    When only P0 is open, this equals the ladder (P0) start.
    """
    if not getattr(state, "legs", None):
        raise ValueError("active ladder has no open step leg")
    return int(state.legs[-1].ts)


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
        f"Ladder VWAP: {_fmt(float(ladder_vwap)) if ladder_vwap is not None else 'nan'}",
        f"Step VWAP: {_fmt(float(step_vwap)) if step_vwap is not None else 'nan'}",
        f"Ladder POC: {_fmt(float(ladder_poc)) if ladder_poc is not None else 'nan'}",
        f"Ladder Value Area: {_fmt(float(ladder_val)) if ladder_val is not None else 'nan'} → {_fmt(float(ladder_vah)) if ladder_vah is not None else 'nan'}",
        f"Step POC: {_fmt(float(step_poc)) if step_poc is not None else 'nan'}",
        f"Last close: {_fmt(last)}",
    ]
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
    )
    return {"text": "\n".join(lines), "svg": jpg}


def _draw_jpg(symbol, market, side, levels, n, ladder_vwap, step_vwap, ladder_poc, step_poc, current, *, ladder_value_area=None):
    """Draw visual ladder summary directly to JPEG for Telegram photo delivery."""
    from PIL import Image, ImageDraw, ImageFont

    W,H=1200,1600; left,right=170,1100; top,bottom=110,1440
    va_prices=[]
    if ladder_value_area:
        va_prices=[float(ladder_value_area.get('val', float('nan'))), float(ladder_value_area.get('vah', float('nan')))]
        va_prices=[p for p in va_prices if p == p]
    prices=[float(x['price']) for x in levels]+[ladder_vwap,step_vwap,ladder_poc,step_poc,current]+va_prices
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
    for item in levels:
        i=int(str(item['level'])[1:]); p=float(item['price']); yy=y(p); is_active=i==n
        col=active if is_active else line_color
        d.line([left,yy,right,yy],fill=col,width=5 if is_active else 2)
        d.ellipse([xmid-7,yy-7,xmid+7,yy+7],fill=col,outline='white',width=2)
        role=item.get('role') or ''
        txt=f"{item['level']} {_fmt(p)}" + (f"  {role}" if role else '')
        f=fmb if is_active else fm
        bbox=d.textbbox((0,0),txt,font=f)
        d.text((right-12-bbox[2],yy-25),txt,fill=active if is_active else '#933',font=f)
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
    pn=float(levels[n]['price']); tp=float(levels[n-1]['price']) if n>=1 else float(levels[0]['price'])
    pn1=float(levels[n+1]['price']) if n+1 < len(levels) else pn
    pn2=float(levels[n+2]['price']) if n+2 < len(levels) else pn1
    d.text((60,1500),f'Active: P{n}={_fmt(pn)} · TP=P{max(n-1,0)}={_fmt(tp)} · Next P{n+1}={_fmt(pn1)} · P{n+2}={_fmt(pn2)}',fill='#222',font=fmb)
    d.text((60,1532),f'VWAP: ladder={_fmt(ladder_vwap)} · active step={_fmt(step_vwap)} · POC: ladder={_fmt(ladder_poc)} · step={_fmt(step_poc)} · current={_fmt(current)}',fill='#222',font=fmb)
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
    chat=getattr(msg,"chat",None); chat_id=getattr(chat,"id",None) if chat is not None else None; thread=getattr(msg,"message_thread_id",None)
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
            from plugins.platforms.telegram.adapter import InlineKeyboardButton, InlineKeyboardMarkup
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
        from plugins.platforms.telegram.adapter import InlineKeyboardButton, InlineKeyboardMarkup
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
    key=_chat_key_from_message(msg); screen=await asyncio.to_thread(_WIZARD.handle_text,key,getattr(msg,"text","") or "")
    if screen is None: return False
    cid=_chat_id_from_message(msg)
    if cid: await _send_screen(adapter,cid,screen,metadata=_metadata_from_message(msg))
    return True

__all__=["handle_backtest_command","handle_backtest_callback","handle_backtest_text","BacktestWizard"]
