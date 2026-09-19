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
    streams = f"{s}@aggTrade/{s}@kline_{interval}"
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
