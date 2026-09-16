"""Server-side OHLCV fetchers for TradeMenu charts.

Candles always come from the selected exchange's public market data where
available. Unsupported venues return an explicit error — never fake data.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

SUPPORTED_TFS = ("1m", "5m", "15m", "30m", "1h", "4h", "1D")

_BINANCE_INTERVAL = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1D": "1d",
}

_HL_INTERVAL = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1D": "1d",
}

_PHEMEX_RESOLUTION = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1D": 86400,
}

_MEXC_INTERVAL = {
    "1m": "Min1",
    "5m": "Min5",
    "15m": "Min15",
    "30m": "Min30",
    "1h": "Min60",
    "4h": "Hour4",
    "1D": "Day1",
}


def _http_json(url: str, *, method: str = "GET", body: Optional[dict] = None, timeout: int = 20) -> Any:
    data = None
    headers = {"User-Agent": "Hermes-TradeMenu/1.0", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 public market data
        return json.loads(resp.read().decode("utf-8"))


def _normalize_candle(ts_ms: int, o: float, h: float, l: float, c: float, v: float = 0.0) -> Dict[str, Any]:
    return {
        "time": int(ts_ms // 1000),  # lightweight-charts expects seconds for most series
        "open": float(o),
        "high": float(h),
        "low": float(l),
        "close": float(c),
        "volume": float(v),
    }


def _binance_symbol(native: str) -> str:
    s = str(native or "").upper().replace("-", "").replace("_", "").replace("/", "")
    if s.endswith("USD") and not s.endswith("USDT") and not s.endswith("USDC"):
        s = s + "T" if s.endswith("USD") else s
    if s == "BTCUSD":
        s = "BTCUSDT"
    if not s.endswith(("USDT", "USDC", "BUSD", "USD")):
        s = f"{s}USDT"
    if s.endswith("USD") and not s.endswith(("USDT", "USDC")):
        s = s[:-3] + "USDT"
    return s


def fetch_binance_candles(symbol: str, tf: str, *, market: str = "spot", limit: int = 300) -> List[Dict[str, Any]]:
    interval = _BINANCE_INTERVAL.get(tf)
    if not interval:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    native = _binance_symbol(symbol)
    if market == "futures":
        base = "https://fapi.binance.com"
        path = "/fapi/v1/klines"
    else:
        base = "https://api.binance.com"
        path = "/api/v3/klines"
    qs = urllib.parse.urlencode({"symbol": native, "interval": interval, "limit": min(limit, 1000)})
    rows = _http_json(f"{base}{path}?{qs}")
    out = []
    for row in rows:
        out.append(
            _normalize_candle(
                int(row[0]),
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
            )
        )
    return out


def fetch_hyperliquid_candles(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    interval = _HL_INTERVAL.get(tf)
    if not interval:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    coin = str(symbol or "").strip()
    # Preserve HIP-3 / dex-prefixed natives (e.g. xyz:SP500). Only peel quote
    # suffixes from the public tail, never drop a dex route prefix.
    if ":" in coin:
        dex, _, tail = coin.partition(":")
        tail = tail.strip()
        for suffix in ("-USD", "-USDT", "USDT", "USDC", "USD"):
            if tail.upper().endswith(suffix) and len(tail) > len(suffix):
                tail = tail[: -len(suffix)]
                break
        coin = f"{dex}:{tail}" if tail else coin
    else:
        for suffix in ("-USD", "-USDT", "USDT", "USDC", "USD"):
            if coin.upper().endswith(suffix) and len(coin) > len(suffix):
                coin = coin[: -len(suffix)]
                break
    if not coin:
        raise ValueError("MISSING_SYMBOL")
    end_ms = int(time.time() * 1000)
    # rough window by timeframe
    tf_ms = {
        "1m": 60_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
        "4h": 14_400_000,
        "1d": 86_400_000,
    }[interval]
    start_ms = end_ms - tf_ms * max(limit, 50)
    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
    }
    try:
        rows = _http_json("https://api.hyperliquid.xyz/info", method="POST", body=payload)
    except urllib.error.HTTPError as exc:
        # HL returns 500 for unknown coins; surface as structured candle miss.
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Hyperliquid rejected coin {coin!r} ({exc.code})") from exc
    if not isinstance(rows, list):
        raise RuntimeError("Unexpected Hyperliquid candle response")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            _normalize_candle(
                int(row.get("t") or row.get("T") or 0),
                float(row.get("o")),
                float(row.get("h")),
                float(row.get("l")),
                float(row.get("c")),
                float(row.get("v") or 0),
            )
        )
    return out[-limit:]


def fetch_phemex_candles(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    res = _PHEMEX_RESOLUTION.get(tf)
    if res is None:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    sym = str(symbol or "").upper().replace("-", "")
    if not sym.endswith("USDT"):
        if sym.endswith("USD"):
            sym = sym + "T"
        else:
            sym = f"{sym}USDT"
    # public kline
    qs = urllib.parse.urlencode({"symbol": sym, "resolution": res, "limit": min(limit, 1000)})
    data = _http_json(f"https://api.phemex.com/phemex-user/public/md/v2/kline?{qs}")
    # response shapes vary; try common
    rows = data.get("data", {}).get("rows") if isinstance(data, dict) else None
    if rows is None and isinstance(data, dict):
        rows = data.get("data")
    if not isinstance(rows, list):
        # fallback older endpoint
        qs2 = urllib.parse.urlencode({"symbol": sym, "resolution": res, "limit": min(limit, 1000)})
        data = _http_json(f"https://api.phemex.com/exchange/public/md/v2/kline?{qs2}")
        rows = data.get("data", {}).get("rows") if isinstance(data, dict) else None
        if rows is None and isinstance(data, dict):
            rows = data.get("data")
    if not isinstance(rows, list):
        raise RuntimeError("Unexpected Phemex candle response")
    out = []
    for row in rows:
        # [timestamp, interval, lastClose, open, high, low, close, volume, turnover]
        if isinstance(row, (list, tuple)) and len(row) >= 7:
            ts = int(row[0])
            # phemex timestamps sometimes seconds
            ts_ms = ts * 1000 if ts < 10_000_000_000 else ts
            out.append(_normalize_candle(ts_ms, float(row[3]), float(row[4]), float(row[5]), float(row[6]), float(row[7] if len(row) > 7 else 0)))
    out.sort(key=lambda c: c["time"])
    return out[-limit:]


def fetch_mexc_candles(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    interval = _MEXC_INTERVAL.get(tf)
    if not interval:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    sym = str(symbol or "").upper().replace("-", "_")
    if "_" not in sym:
        if sym.endswith("USDC"):
            sym = sym[:-4] + "_USDC"
        elif sym.endswith("USDT"):
            sym = sym[:-4] + "_USDT"
        else:
            sym = f"{sym}_USDT"
    qs = urllib.parse.urlencode({"symbol": sym, "interval": interval, "limit": min(limit, 500)})
    data = _http_json(f"https://contract.mexc.com/api/v1/contract/kline/{sym}?{urllib.parse.urlencode({'interval': interval})}")
    # alt shape
    rows = None
    if isinstance(data, dict):
        d = data.get("data")
        if isinstance(d, dict) and "time" in d:
            times = d.get("time") or []
            opens = d.get("open") or []
            highs = d.get("high") or []
            lows = d.get("low") or []
            closes = d.get("close") or []
            vols = d.get("vol") or d.get("volume") or [0] * len(times)
            out = []
            for i in range(min(len(times), len(opens), len(highs), len(lows), len(closes))):
                ts = int(times[i])
                ts_ms = ts * 1000 if ts < 10_000_000_000 else ts
                out.append(_normalize_candle(ts_ms, float(opens[i]), float(highs[i]), float(lows[i]), float(closes[i]), float(vols[i] if i < len(vols) else 0)))
            return out[-limit:]
        if isinstance(d, list):
            rows = d
    if not rows:
        # spot-style
        data = _http_json(f"https://api.mexc.com/api/v3/klines?{urllib.parse.urlencode({'symbol': sym.replace('_','') , 'interval': {'1m':'1m','5m':'5m','15m':'15m','30m':'30m','1h':'60m','4h':'4h','1D':'1d'}[tf], 'limit': limit})}")
        if not isinstance(data, list):
            raise RuntimeError("Unexpected MEXC candle response")
        out = []
        for row in data:
            out.append(_normalize_candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])))
        return out
    out = []
    for row in rows:
        if isinstance(row, dict):
            ts = int(row.get("time") or row.get("t") or 0)
            ts_ms = ts * 1000 if ts < 10_000_000_000 else ts
            out.append(_normalize_candle(ts_ms, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), float(row.get("vol") or row.get("volume") or 0)))
    return out[-limit:]


def fetch_candles(exchange: str, account: str, symbol: str, tf: str, limit: int = 300) -> Dict[str, Any]:
    """Return {success, candles, ...} or explicit unsupported error."""
    ex = str(exchange or "").strip().lower()
    tf_n = str(tf or "").strip()
    if tf_n not in SUPPORTED_TFS:
        return {
            "success": False,
            "error": {
                "code": "UNSUPPORTED_TIMEFRAME",
                "message": f"Timeframe {tf_n!r} is not supported.",
            },
            "candles": [],
        }
    try:
        if ex == "binance":
            market = "futures" if str(account).lower() in {"futures", "future", "perp", "perps"} else "spot"
            candles = fetch_binance_candles(symbol, tf_n, market=market, limit=limit)
        elif ex == "hyperliquid":
            candles = fetch_hyperliquid_candles(symbol, tf_n, limit=limit)
        elif ex == "phemex":
            candles = fetch_phemex_candles(symbol, tf_n, limit=limit)
        elif ex == "mexc":
            candles = fetch_mexc_candles(symbol, tf_n, limit=limit)
        else:
            return {
                "success": False,
                "error": {
                    "code": "UNSUPPORTED_CANDLES",
                    "message": f"Candles are not implemented for exchange '{ex}' in TradeMenu Phase 1.",
                },
                "candles": [],
                "exchange": ex,
                "symbol": symbol,
                "timeframe": tf_n,
            }
        return {
            "success": True,
            "exchange": ex,
            "account": account,
            "symbol": symbol,
            "timeframe": tf_n,
            "candles": candles,
        }
    except ValueError as exc:
        code = str(exc)
        if code == "UNSUPPORTED_TIMEFRAME":
            return {
                "success": False,
                "error": {"code": "UNSUPPORTED_TIMEFRAME", "message": f"Timeframe {tf_n!r} is not supported on {ex}."},
                "candles": [],
            }
        return {"success": False, "error": {"code": "CANDLE_ERROR", "message": code}, "candles": []}
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)[:300]
        code = "CANDLES_UNAVAILABLE" if "CANDLES_UNAVAILABLE" in msg or "HTTP Error" in msg else "CANDLE_ERROR"
        # Prefer clean user-facing text over raw urllib "HTTP Error 500".
        if msg.startswith("CANDLES_UNAVAILABLE:"):
            msg = msg.split(":", 1)[1].strip()
        elif "HTTP Error" in msg:
            msg = f"Market data unavailable for {symbol} on {ex}."
        return {
            "success": False,
            "error": {"code": code, "message": msg},
            "candles": [],
            "exchange": ex,
            "symbol": symbol,
            "timeframe": tf_n,
        }
