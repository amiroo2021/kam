"""Public Binance market data (no auth)."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

from ..metrics import OhlcvBar, bar_from_binance_kline
from .symbols import canonical_binance_symbol

BINANCE_SPOT_REST = "https://api.binance.com"
BINANCE_SPOT_WS = "wss://stream.binance.com:9443/ws"


def fetch_klines(
    symbol: str,
    interval: str = "1m",
    limit: int = 500,
    *,
    base_url: str = BINANCE_SPOT_REST,
    timeout: float = 30.0,
) -> List[list]:
    """GET /api/v3/klines — public, no key."""
    symbol = canonical_binance_symbol(symbol)
    qs = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": int(limit)})
    url = f"{base_url}/api/v3/klines?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "GoldenFibo/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 public HTTPS
        data = json.loads(resp.read().decode())
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected klines payload: {type(data)}")
    return data


def klines_to_bars(klines: Sequence[Sequence[Any]]) -> List[OhlcvBar]:
    return [bar_from_binance_kline(k) for k in klines]


def kline_to_chart_candle(k: Sequence[Any]) -> Dict[str, Any]:
    """Lightweight Charts candle: time is unix seconds (UTC)."""
    return {
        "time": int(k[0]) // 1000,
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
    }


def bars_to_chart_candles(klines: Sequence[Sequence[Any]]) -> List[Dict[str, Any]]:
    return [kline_to_chart_candle(k) for k in klines]


def resample_chart_candles(klines: Sequence[Sequence[Any]], timeframe: str) -> List[Dict[str, Any]]:
    """Aggregate 1m klines into display-timeframe candles for the UI.

    Bucket open times are interval-aligned (e.g. 1h uses HH:00). Engine start may
    fall mid-bucket; the first display bar still uses the first available 1m open
    as its OHLC open, but the bar *time* is the aligned bucket so adjacent bars
    are spaced by the display interval after the first partial bucket.
    """
    if not klines:
        return []
    if timeframe == "1m":
        return bars_to_chart_candles(klines)
    from .timeframes import interval_ms

    step = interval_ms(timeframe)
    grouped: List[List[Any]] = []
    current_bucket: List[List[Any]] = []
    current_open = None
    for k in klines:
        open_ms = int(k[0])
        bucket = open_ms - (open_ms % step)
        if current_open is None or bucket != current_open:
            if current_bucket:
                grouped.append(current_bucket)
            current_bucket = [list(k)]
            current_open = bucket
        else:
            current_bucket.append(list(k))
    if current_bucket:
        grouped.append(current_bucket)

    out: List[Dict[str, Any]] = []
    for group in grouped:
        first = group[0]
        last = group[-1]
        bucket_ms = int(first[0]) - (int(first[0]) % step)
        highs = max(float(x[2]) for x in group)
        lows = min(float(x[3]) for x in group)
        volume = sum(float(x[5]) for x in group)
        out.append({
            "time": bucket_ms // 1000,
            "open": float(first[1]),
            "high": highs,
            "low": lows,
            "close": float(last[4]),
            "volume": volume,
        })
    return out


def upsert_display_candle_from_1m(
    chart_candles: List[Dict[str, Any]],
    kline_1m: Sequence[Any],
    timeframe: str,
) -> Dict[str, Any]:
    """Merge one 1m kline into the display-TF series (in-place). Returns candle sent to UI."""
    if timeframe == "1m":
        candle = kline_to_chart_candle(kline_1m)
        if chart_candles and int(chart_candles[-1]["time"]) == int(candle["time"]):
            chart_candles[-1] = candle
        else:
            chart_candles.append(candle)
        return candle

    from .timeframes import interval_ms

    step = interval_ms(timeframe)
    open_ms = int(kline_1m[0])
    bucket_ms = open_ms - (open_ms % step)
    t = bucket_ms // 1000
    o = float(kline_1m[1])
    h = float(kline_1m[2])
    l = float(kline_1m[3])
    c = float(kline_1m[4])
    v = float(kline_1m[5])
    if chart_candles and int(chart_candles[-1]["time"]) == t:
        cur = chart_candles[-1]
        cur["high"] = max(float(cur["high"]), h)
        cur["low"] = min(float(cur["low"]), l)
        cur["close"] = c
        cur["volume"] = float(cur.get("volume") or 0.0) + v
        return cur
    candle = {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v}
    chart_candles.append(candle)
    return candle


def agg_trade_stream_url(symbol: str, *, ws_base: str = BINANCE_SPOT_WS) -> str:
    """Combined stream path for public aggTrade (ordered buyer/seller trades)."""
    s = canonical_binance_symbol(symbol).lower().replace("/", "")
    return f"{ws_base}/{s}@aggTrade"


def kline_stream_url(symbol: str, interval: str = "1m", *, ws_base: str = BINANCE_SPOT_WS) -> str:
    s = canonical_binance_symbol(symbol).lower().replace("/", "")
    return f"{ws_base}/{s}@kline_{interval}"


def combined_stream_url(symbol: str, interval: str = "1m", *, market: str = "spot") -> str:
    """Multiplex aggTrade + kline on one connection."""
    s = canonical_binance_symbol(symbol).lower().replace("/", "")
    base = "wss://stream.binance.com:9443/stream?streams=" if market == "spot" else "wss://fstream.binance.com/stream?streams="
    trade_stream = "aggTrade" if market == "spot" else "trade"
    streams = f"{s}@{trade_stream}/{s}@kline_{interval}"
    return f"{base}{streams}"


def parse_combined_message(raw: str) -> Optional[Dict[str, Any]]:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if "stream" in msg and "data" in msg:
        return {"stream": msg["stream"], "data": msg["data"]}
    # single-stream shape
    if "e" in msg:
        return {"stream": msg.get("e"), "data": msg}
    return None
