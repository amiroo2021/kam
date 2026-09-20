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

# Orderly Network (Raydium Perps white-label) public candles intervals.
_ORDERLY_INTERVAL = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1D": "1d",
}

# Arcus public OHLCV (GET /v1/candles). Docs: timeframe enum uses lowercase day.
_ARCUS_INTERVAL = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1D": "1d",
}
_ARCUS_API_BASE = "https://api.arcus.xyz"


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
    # Normalize to Phemex market-data symbol.
    # Linear USDT-m perps (PerpetualV2) use BTCUSDT with Rp (human) prices.
    # Inverse BTCUSD uses scaled Ev prices; spot uses sBTCUSDT with priceScale 8.
    raw = str(symbol or "").upper().replace("-", "").replace("_", "").replace("/", "")
    if not raw:
        raise RuntimeError("Empty Phemex candle symbol")
    if raw.startswith("S") and len(raw) > 1 and raw[1:].endswith("USDT"):
        # already spot-style sBTCUSDT
        kline_symbol = raw if raw.startswith("s") else "s" + raw[1:]
        # keep as provided lower-s convention: sBTCUSDT
        if not kline_symbol.startswith("s"):
            kline_symbol = "s" + kline_symbol.lstrip("S")
    elif raw.endswith("USDT") or raw.endswith("USDC"):
        kline_symbol = raw
    elif raw.endswith("USD"):
        # Prefer linear USDT-m klines for TradeMenu (matches USDT positions).
        kline_symbol = raw[:-3] + "USDT"
    else:
        kline_symbol = f"{raw}USDT"

    limit_n = max(1, min(int(limit), 1000))
    rows: Any = None
    last_err: Optional[BaseException] = None

    # 1) kline/last — works for PerpetualV2 BTCUSDT with human Rp OHLC
    try:
        qs = urllib.parse.urlencode(
            {"symbol": kline_symbol, "resolution": res, "limit": limit_n}
        )
        data = _http_json(f"https://api.phemex.com/exchange/public/md/v2/kline/last?{qs}")
        if isinstance(data, dict) and data.get("code") in (0, "0", None):
            payload = data.get("data")
            if isinstance(payload, dict):
                rows = payload.get("rows")
            elif isinstance(payload, list):
                rows = payload
    except Exception as exc:  # noqa: BLE001
        last_err = exc

    # 2) kline/list with from/to window
    if not isinstance(rows, list) or not rows:
        try:
            to_ts = int(time.time())
            fr_ts = to_ts - int(res) * max(limit_n + 5, 20)
            qs2 = urllib.parse.urlencode(
                {
                    "symbol": kline_symbol,
                    "resolution": res,
                    "from": fr_ts,
                    "to": to_ts,
                }
            )
            data = _http_json(f"https://api.phemex.com/exchange/public/md/v2/kline/list?{qs2}")
            if isinstance(data, dict) and data.get("code") in (0, "0", None):
                payload = data.get("data")
                if isinstance(payload, dict):
                    rows = payload.get("rows")
                elif isinstance(payload, list):
                    rows = payload
        except Exception as exc:  # noqa: BLE001
            last_err = exc

    if not isinstance(rows, list) or not rows:
        msg = f"Unexpected Phemex candle response for {kline_symbol}"
        if last_err is not None:
            msg = f"{msg}: {last_err}"
        raise RuntimeError(msg)

    # Detect scaled Ev prices (spot s* or inverse) vs human Rp (linear USDT).
    # Human BTCUSDT sample close ~76000; scaled sBTCUSDT ~7.5e12; inverse ~7.5e8.
    def _scale_for_symbol(sym: str, sample_close: float) -> float:
        if sym.startswith("s") and sample_close > 1e9:
            return 1e8  # spot priceScale 8
        if (sym.endswith("USD") and not sym.endswith(("USDT", "USDC"))) and sample_close > 1e5:
            return 1e4  # classic inverse Ev
        return 1.0

    sample_close = 0.0
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) >= 7:
            try:
                sample_close = abs(float(row[6]))
                if sample_close > 0:
                    break
            except Exception:  # noqa: BLE001
                continue
    scale = _scale_for_symbol(kline_symbol, sample_close)

    out: List[Dict[str, Any]] = []
    for row in rows:
        # [timestamp, interval, lastClose, open, high, low, close, volume, turnover, symbol?]
        if isinstance(row, (list, tuple)) and len(row) >= 7:
            ts = int(row[0])
            ts_ms = ts * 1000 if ts < 10_000_000_000 else ts
            o = float(row[3]) / scale
            h = float(row[4]) / scale
            l = float(row[5]) / scale
            c = float(row[6]) / scale
            v = float(row[7] if len(row) > 7 else 0)
            out.append(_normalize_candle(ts_ms, o, h, l, c, v))
    out.sort(key=lambda c: c["time"])
    # de-dupe by time
    dedup: Dict[int, Dict[str, Any]] = {}
    for c in out:
        dedup[int(c["time"])] = c
    ordered = [dedup[k] for k in sorted(dedup.keys())]
    return ordered[-limit_n:]


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


def _orderly_symbol_from_any(symbol: str) -> str:
    raw = str(symbol or "").strip().upper().replace("-", "_").replace("/", "_")
    if not raw:
        return ""
    if raw.startswith("PERP_"):
        return raw
    for q in ("USDC", "USDT", "USD"):
        if raw.endswith(q) and len(raw) > len(q):
            base = raw[: -len(q)].rstrip("_")
            return f"PERP_{base}_USDC"
    return f"PERP_{raw}_USDC"


def fetch_raydium_candles(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    """Orderly Network public candles (Raydium Perps white-label on Orderly)."""
    interval = _ORDERLY_INTERVAL.get(tf)
    if not interval:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    orderly = _orderly_symbol_from_any(symbol)
    if not orderly:
        raise RuntimeError("Empty Raydium/Orderly candle symbol")
    limit_n = max(1, min(int(limit), 1000))
    body = {
        "type": "candles",
        "symbol": orderly,
        "interval": interval,
        "limit": limit_n,
    }
    data = _http_json(
        "https://api.orderly.org/v1/public/query",
        method="POST",
        body=body,
        timeout=25,
    )
    if not isinstance(data, dict) or not data.get("success"):
        raise RuntimeError(f"Orderly candles failed for {orderly}")
    payload = data.get("data")
    rows = None
    if isinstance(payload, dict):
        rows = payload.get("rows")
    elif isinstance(payload, list):
        rows = payload
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Empty Orderly candles for {orderly}")
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = int(row.get("timestamp") or row.get("start_timestamp") or row.get("t") or 0)
        if ts <= 0:
            continue
        ts_ms = ts if ts > 10_000_000_000 else ts * 1000
        out.append(
            _normalize_candle(
                ts_ms,
                float(row.get("open")),
                float(row.get("high")),
                float(row.get("low")),
                float(row.get("close")),
                float(row.get("volume") or 0),
            )
        )
    out.sort(key=lambda c: c["time"])
    dedup: Dict[int, Dict[str, Any]] = {int(c["time"]): c for c in out}
    ordered = [dedup[k] for k in sorted(dedup.keys())]
    return ordered[-limit_n:]


def _arcus_market_symbol(symbol: str) -> str:
    """Preserve Arcus display natives (BTC-USD). Do not strip to BTCUSD."""
    text = str(symbol or "").strip().upper().replace("_", "-").replace("/", "-")
    if not text:
        raise RuntimeError("Empty Arcus candle market")
    return text


def fetch_arcus_candles(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    """Arcus public OHLCV via GET /v1/candles (oracle-priced bars + trade volume)."""
    timeframe = _ARCUS_INTERVAL.get(tf)
    if not timeframe:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    market = _arcus_market_symbol(symbol)
    limit_n = max(1, min(int(limit), 1500))
    # Server requires Unix microseconds for `to` (min 1e14).
    to_us = int(time.time() * 1_000_000)
    qs = urllib.parse.urlencode(
        {
            "market": market,
            "timeframe": timeframe,
            "to": to_us,
            "countback": limit_n,
        }
    )
    data = _http_json(f"{_ARCUS_API_BASE}/v1/candles?{qs}", timeout=25)
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected Arcus candles response for {market}")
    rows = data.get("candles")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Empty Arcus candles for {market}")
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        open_time = row.get("openTime") or row.get("open_time") or row.get("t") or 0
        try:
            ts = int(open_time)
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        # Arcus openTime is microseconds; other adapters feed ms into _normalize_candle.
        if ts > 10_000_000_000_000:  # > ~year 2286 in ms → treat as µs
            ts_ms = ts // 1000
        elif ts > 10_000_000_000:  # ms
            ts_ms = ts
        else:  # seconds
            ts_ms = ts * 1000
        try:
            o = float(row.get("open"))
            h = float(row.get("high"))
            l = float(row.get("low"))
            c = float(row.get("close"))
            v = float(row.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        out.append(_normalize_candle(ts_ms, o, h, l, c, v))
    out.sort(key=lambda c: c["time"])
    dedup: Dict[int, Dict[str, Any]] = {int(c["time"]): c for c in out}
    ordered = [dedup[k] for k in sorted(dedup.keys())]
    if not ordered:
        raise RuntimeError(f"Empty Arcus candles after normalize for {market}")
    return ordered[-limit_n:]


def fetch_candles(exchange: str, account: str, symbol: str, tf: str, limit: int = 300) -> Dict[str, Any]:
    """Return {success, candles, ...} via TradeDesk agent ``candles`` operation.

    Exchange-specific HTTP/symbol logic lives in agents + ``plugins.trade.candles``.
    TradeMenu must not grow a per-venue if/elif switch here.
    """
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
        from plugins.trade.tradedesk import TradeDesk

        desk = TradeDesk()
        caps = desk.capabilities(ex) if ex in desk.list_exchanges() else []
        if "candles" not in (caps or []):
            return {
                "success": False,
                "error": {
                    "code": "UNSUPPORTED_CANDLES",
                    "message": f"Candles unavailable for {symbol} on {ex}.",
                },
                "candles": [],
                "exchange": ex,
                "symbol": symbol,
                "timeframe": tf_n,
            }
        resp = desk.execute(
            {
                "operation": "candles",
                "exchange": ex,
                "account": account,
                "symbol": symbol,
                "interval": tf_n,
                "limit": limit,
            }
        )
        if not getattr(resp, "success", False):
            err = getattr(resp, "error", None)
            code = getattr(err, "code", None) or "CANDLES_UNAVAILABLE"
            msg = getattr(err, "message", None) or f"Candles unavailable for {symbol} on {ex}."
            msg_s = str(msg)
            # Never surface Phase-1 leftovers or raw HTTP URLs.
            if "Phase 1" in msg_s:
                msg_s = f"Candles unavailable for {symbol} on {ex}."
            if "url:" in msg_s.lower() or "HTTP Error" in msg_s or "forbidden" in msg_s.lower() or "empty rise candles" in msg_s.lower() or "python-multipart" in msg_s.lower() or "form data requires" in msg_s.lower():
                code = "CANDLES_UNAVAILABLE"
                msg_s = f"Candles unavailable for {symbol} on {ex}."
            elif str(code) == "CANDLES_UPSTREAM_ERROR":
                code = "CANDLES_UNAVAILABLE"
                msg_s = f"Candles unavailable for {symbol} on {ex}."
            return {
                "success": False,
                "error": {"code": str(code), "message": msg_s[:300]},
                "candles": [],
                "exchange": ex,
                "symbol": symbol,
                "timeframe": tf_n,
            }
        data = getattr(resp, "data", None) or {}
        candles = data.get("candles") if isinstance(data, dict) else None
        if not isinstance(candles, list):
            candles = []
        return {
            "success": True,
            "exchange": ex,
            "account": account,
            "symbol": symbol,
            "timeframe": tf_n,
            "candles": candles,
            "source": (data.get("source") if isinstance(data, dict) else None) or "native",
        }
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)[:300]
        if msg.startswith("CANDLES_UNAVAILABLE:"):
            code = "CANDLES_UNAVAILABLE"
            msg = msg.split(":", 1)[1].strip()
        elif "HTTP Error" in msg or "url:" in msg.lower() or "forbidden" in msg.lower() or "empty rise candles" in msg.lower() or "python-multipart" in msg.lower() or "form data requires" in msg.lower():
            code = "CANDLES_UNAVAILABLE"
            msg = f"Candles unavailable for {symbol} on {ex}."
        else:
            code = "CANDLE_ERROR"
        return {
            "success": False,
            "error": {"code": code, "message": msg},
            "candles": [],
            "exchange": ex,
            "symbol": symbol,
            "timeframe": tf_n,
        }
