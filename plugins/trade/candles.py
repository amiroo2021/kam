"""Exchange OHLCV fetchers for TradeDesk ``candles`` operations.

WebTrade must not grow per-exchange if/elif chains. Agents either call these
helpers (when their candle path is plain REST) or return explicit UNSUPPORTED /
UNAVAILABLE errors with a documented reason.

Contract:
- input: canonical native symbol from the exchange resolver
- output: normalized OHLCV list
- no exchange-specific symbol rewriting in WebTrade UI code
- no Binance fallback unless a venue explicitly uses it as its official source
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence

from .canonical import make_failure, make_success


def _http_json(url: str, timeout: int = 20) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Hermes-KAM/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 public market data
        data = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(data)
    except Exception:
        return data


def _extract_ts_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = int(value)
        if ts > 10_000_000_000_000_000:  # ns
            return ts // 1_000_000
        if ts > 10_000_000_000_000:  # µs
            return ts // 1000
        if ts > 10_000_000_000:  # ms
            return ts
        return ts * 1000
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def normalize_candle(time_ms: Any, open_: Any, high: Any, low: Any, close: Any, volume: Any = 0) -> Dict[str, Any]:
    ts = _extract_ts_ms(time_ms)
    if ts is None:
        raise ValueError("invalid candle timestamp")
    return {
        "time": int(ts),
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume or 0),
    }


def finalize_candles(rows: Sequence[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            candle = normalize_candle(
                row.get("time"), row.get("open"), row.get("high"), row.get("low"), row.get("close"), row.get("volume", 0)
            )
        except Exception:
            continue
        if candle["time"] in seen:
            continue
        seen.add(candle["time"])
        out.append(candle)
    out.sort(key=lambda x: x["time"])
    if limit > 0:
        out = out[-limit:]
    return out


SUPPORTED_TFS = {"1m", "5m", "15m", "30m", "1h", "4h", "1D"}
_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1D": 86400}


def _resample_1m(rows: Sequence[Dict[str, Any]], target_sec: int, limit: int) -> List[Dict[str, Any]]:
    if target_sec <= 60:
        return finalize_candles(rows, limit)
    bucket_ms = target_sec * 1000
    buckets: Dict[int, Dict[str, Any]] = {}
    for raw in rows:
        try:
            c = normalize_candle(raw.get("time"), raw.get("open"), raw.get("high"), raw.get("low"), raw.get("close"), raw.get("volume", 0))
        except Exception:
            continue
        b = (c["time"] // bucket_ms) * bucket_ms
        existing = buckets.get(b)
        if existing is None:
            buckets[b] = {"time": b, "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "volume": c["volume"]}
        else:
            existing["high"] = max(existing["high"], c["high"])
            existing["low"] = min(existing["low"], c["low"])
            existing["close"] = c["close"]
            existing["volume"] += c["volume"]
    return finalize_candles(list(buckets.values()), limit)


# ---------------------------------------------------------------------------
# Apex
# ---------------------------------------------------------------------------

def _apex_wire_symbol(symbol: str) -> str:
    sym = str(symbol or "").strip().upper()
    return sym.replace("-", "").replace("/", "").replace("_", "")


def fetch_apex(symbol: str, tf: str, limit: int = 300, *, base: str = "https://omni.apex.exchange") -> List[Dict[str, Any]]:
    sym = _apex_wire_symbol(symbol)
    if tf not in _TF_SECONDS:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    interval = str(_TF_SECONDS[tf] // 60 if tf != "1m" else 1)
    url = f"{base.rstrip('/')}/api/v3/klines?{urllib.parse.urlencode({'symbol': sym, 'interval': interval, 'limit': str(max(limit, 1))})}"
    data = _http_json(url, timeout=20)
    rows = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for k in ("data", "result", "rows", "candles"):
            v = data.get(k)
            if isinstance(v, list):
                rows = v
                break
            if isinstance(v, dict):
                rows = v.get("rows") or v.get("candles") or []
                if rows:
                    break
    out: List[Dict[str, Any]] = []
    for row in rows:
        if isinstance(row, list) and len(row) >= 6:
            out.append(normalize_candle(row[0], row[1], row[2], row[3], row[4], row[5]))
        elif isinstance(row, dict):
            out.append(normalize_candle(row.get("time") or row.get("t") or row.get("openTime"), row.get("open") or row.get("o"), row.get("high") or row.get("h"), row.get("low") or row.get("l"), row.get("close") or row.get("c"), row.get("volume") or row.get("v") or 0))
    if not out:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Apex returned no klines for {symbol}")
    return finalize_candles(out, limit)


# ---------------------------------------------------------------------------
# QFEX
# ---------------------------------------------------------------------------

def _qfex_resolution(tf: str) -> str:
    return {"1m": "1MIN", "5m": "1MIN", "15m": "1MIN", "30m": "1MIN", "1h": "1HOUR", "4h": "1HOUR", "1D": "1DAY"}[tf]


def _qfex_resample(rows: Sequence[Dict[str, Any]], tf: str, limit: int) -> List[Dict[str, Any]]:
    return _resample_1m(rows, _TF_SECONDS[tf], limit)


def fetch_qfex(symbol: str, tf: str, limit: int = 300, *, base: str = "https://api.qfex.com") -> List[Dict[str, Any]]:
    sym = str(symbol or "").strip().upper()
    if tf not in _TF_SECONDS:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    res = _qfex_resolution(tf)
    if res == "1MIN":
        secs = _TF_SECONDS[tf]
        rows_req = max(limit * max(secs // 60, 1) * 2, limit * 2, 120)
        end = int(time.time())
        start = end - rows_req * 60
        url = f"{base.rstrip('/')}/candles/{urllib.parse.quote(sym)}?{urllib.parse.urlencode({'resolution': res, 'from': str(start), 'to': str(end)})}"
        data = _http_json(url, timeout=20)
        rows = []
        if isinstance(data, dict):
            for k in ("data", "candles", "rows", "result"):
                v = data.get(k)
                if isinstance(v, list):
                    rows = v
                    break
                if isinstance(v, dict):
                    rows = v.get("candles") or v.get("rows") or []
                    if rows:
                        break
        elif isinstance(data, list):
            rows = data
        out = []
        for row in rows:
            if isinstance(row, list) and len(row) >= 6:
                out.append(normalize_candle(row[0], row[1], row[2], row[3], row[4], row[5]))
            elif isinstance(row, dict):
                out.append(normalize_candle(row.get("time") or row.get("t") or row.get("openTime"), row.get("open") or row.get("o"), row.get("high") or row.get("h"), row.get("low") or row.get("l"), row.get("close") or row.get("c"), row.get("volume") or row.get("v") or 0))
        if tf == "1m":
            return finalize_candles(out, limit)
        return _qfex_resample(out, tf, limit)
    url = f"{base.rstrip('/')}/candles/{urllib.parse.quote(sym)}?{urllib.parse.urlencode({'resolution': res})}"
    data = _http_json(url, timeout=20)
    rows = []
    if isinstance(data, dict):
        for k in ("data", "candles", "rows", "result"):
            v = data.get(k)
            if isinstance(v, list):
                rows = v
                break
            if isinstance(v, dict):
                rows = v.get("candles") or v.get("rows") or []
                if rows:
                    break
    elif isinstance(data, list):
        rows = data
    out = []
    for row in rows:
        if isinstance(row, list) and len(row) >= 6:
            out.append(normalize_candle(row[0], row[1], row[2], row[3], row[4], row[5]))
        elif isinstance(row, dict):
            out.append(normalize_candle(row.get("time") or row.get("t") or row.get("openTime"), row.get("open") or row.get("o"), row.get("high") or row.get("h"), row.get("low") or row.get("l"), row.get("close") or row.get("c"), row.get("volume") or row.get("v") or 0))
    if not out:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: QFEX returned no candles for {symbol}")
    return finalize_candles(out, limit)


# ---------------------------------------------------------------------------
# Rise
# ---------------------------------------------------------------------------

_RISE_MARKET_CACHE: Dict[str, Any] = {"ts": 0.0, "by_sym": {}}


def _rise_market_id(symbol: str, *, api_base: str) -> Optional[str]:
    text = str(symbol or "").strip().upper()
    if not text:
        return None
    if text.isdigit():
        return text
    now = time.time()
    if now - float(_RISE_MARKET_CACHE.get("ts") or 0) > 300 or not _RISE_MARKET_CACHE.get("by_sym"):
        data = _http_json(f"{api_base.rstrip('/')}/v1/markets", timeout=20)
        markets: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, dict):
                markets = inner.get("markets") or []
            elif isinstance(inner, list):
                markets = inner
            else:
                markets = data.get("markets") or []
        by_sym: Dict[str, str] = {}
        for m in markets:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("market_id") or "").strip()
            if not mid:
                continue
            cfg = m.get("config") if isinstance(m.get("config"), dict) else {}
            names = [cfg.get("name"), m.get("display_name"), m.get("base_asset_symbol"), m.get("underlying")]
            for n in names:
                key = str(n or "").strip().upper()
                if not key:
                    continue
                by_sym[key] = mid
                base = key.split("/")[0].split("-")[0].split("_")[0]
                if base and base not in by_sym:
                    by_sym[base] = mid
                compact = key.replace("/", "").replace("-", "").replace("_", "")
                by_sym[compact] = mid
                if compact.endswith("USDC"):
                    by_sym[compact[: -len("USDC")]] = mid
                if compact.endswith("USD"):
                    by_sym[compact[: -len("USD")]] = mid
        _RISE_MARKET_CACHE["ts"] = now
        _RISE_MARKET_CACHE["by_sym"] = by_sym
    by_sym = _RISE_MARKET_CACHE.get("by_sym") or {}
    if text in by_sym:
        return by_sym[text]
    compact = text.replace("/", "").replace("-", "").replace("_", "")
    if compact in by_sym:
        return by_sym[compact]
    for suffix in ("USDC", "USDT", "USD"):
        if compact.endswith(suffix) and compact[: -len(suffix)] in by_sym:
            return by_sym[compact[: -len(suffix)]]
    base = text.split("/")[0].split("-")[0]
    return by_sym.get(base)


def fetch_rise(symbol: str, tf: str, limit: int = 300, *, api_base: str = "https://api.rise.trade") -> List[Dict[str, Any]]:
    sec = _TF_SECONDS.get(tf)
    if not sec:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    market_id = _rise_market_id(symbol, api_base=api_base)
    if market_id is None:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Rise market not found for {symbol!r}")
    # Rise returns 1m candles from trading-view-data; aggregate locally for
    # all higher timeframes.
    interval_sec = 60
    now_s = int(time.time())
    target_bars = max(int(limit) * max(_TF_SECONDS[tf] // 60, 1) * 4 + 200, 400)
    from_s = now_s - interval_sec * target_bars
    qs = urllib.parse.urlencode({"interval": str(interval_sec), "from": str(from_s), "to": str(now_s)})
    url = f"{api_base.rstrip('/')}/v1/markets/id/{market_id}/trading-view-data?{qs}"
    data = _http_json(url, timeout=30)
    rows: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, list):
            rows = inner
        elif isinstance(inner, dict):
            for k in ("data", "candles", "bars"):
                v = inner.get(k)
                if isinstance(v, list):
                    rows = v
                    break
    if not rows:
        raise RuntimeError(f"Empty Rise candles for {market_id} symbol={symbol}")
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts_ms = _extract_ts_ms(row.get("time") or row.get("t") or row.get("startTime"))
        if ts_ms is None or ts_ms <= 0:
            continue
        try:
            out.append(normalize_candle(ts_ms, row.get("open"), row.get("high"), row.get("low"), row.get("close"), row.get("volume") or 0))
        except Exception:
            continue
    if not out:
        raise RuntimeError(f"Empty Rise candles after normalize for {market_id} symbol={symbol}")
    return _resample_1m(out, sec, limit)


# ---------------------------------------------------------------------------
# Previously verified adapters (delegates to WebTrade marketdata helpers)
# ---------------------------------------------------------------------------

def fetch_phemex(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    from plugins.trade.webtrade import marketdata as md
    return md.fetch_phemex_candles(symbol, tf, limit=limit)


def fetch_mexc(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    from plugins.trade.webtrade import marketdata as md
    return md.fetch_mexc_candles(symbol, tf, limit=limit)


def fetch_raydium(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    from plugins.trade.webtrade import marketdata as md
    return md.fetch_raydium_candles(symbol, tf, limit=limit)


def fetch_arcus(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    from plugins.trade.webtrade import marketdata as md
    return md.fetch_arcus_candles(symbol, tf, limit=limit)


def fetch_pacifica(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    from plugins.trade.webtrade import marketdata as md
    return md.fetch_pacifica_candles(symbol, tf, limit=limit)


def fetch_hyperliquid(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    from plugins.trade.webtrade import marketdata as md
    return md.fetch_hyperliquid_candles(symbol, tf, limit=limit)


def fetch_binance(symbol: str, tf: str, limit: int = 300, *, market: str = "spot") -> List[Dict[str, Any]]:
    from plugins.trade.webtrade import marketdata as md
    return md.fetch_binance_candles(symbol, tf, limit=limit, market=market)


def fetch_ondoperps(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    from plugins.trade.webtrade import marketdata as md
    return md.fetch_ondoperps_candles(symbol, tf, limit=limit)


def fetch_edgex(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    raise RuntimeError(
        "UNSUPPORTED_CANDLES: Public /api/v1/public/quote/getKline returns empty dataList for the probed contract IDs at multiple intervals."
    )


def fetch_lighter(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    raise RuntimeError(
        "UNSUPPORTED_CANDLES: Public candlesticks endpoint returns 403 without a session and no public historical OHLCV was verified."
    )


def fetch_hibachi(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    raise RuntimeError(
        "UNSUPPORTED_CANDLES: The visible /v1/candles REST surface is gRPC-Web or 429 rate-limited, not a stable JSON OHLCV feed."
    )


FETCHERS: Dict[str, Callable[..., List[Dict[str, Any]]]] = {
    "binance": fetch_binance,
    "hyperliquid": fetch_hyperliquid,
    "phemex": fetch_phemex,
    "mexc": fetch_mexc,
    "raydium": fetch_raydium,
    "arcus": fetch_arcus,
    "rise": fetch_rise,
    "pacifica": fetch_pacifica,
    "apex": fetch_apex,
    "hibachi": fetch_hibachi,
    "qfex": fetch_qfex,
    "ondoperps": fetch_ondoperps,
    "edgex": fetch_edgex,
    "lighter": fetch_lighter,
}

UNSUPPORTED_NATIVE_CANDLES: Dict[str, str] = {
    "perpl": (
        "Public REST hosts api.perpl.xyz (DNS NXDOMAIN), app.perpl.xyz (SPA HTML), perpl.xyz (marketing page). "
        "Authenticated SDK path exposes /v1/market-data/{id}/candles/{sec}/{from}-{to}, but the public REST surface is not reachable for candle data."
    ),
    "nado": (
        "Gateway /v1/query type=candlesticks (or any historical aggregate variant) rejects with 'unknown variant'. No public OHLCV endpoint found on gateway/indexer."
    ),
}


def has_native_candles(exchange: str) -> bool:
    ex = str(exchange or "").strip().lower()
    return ex in FETCHERS or ex == "ondoperps"


def fetch_for_exchange(exchange: str, symbol: str, tf: str, limit: int = 300, *, account: str = "") -> List[Dict[str, Any]]:
    ex = str(exchange or "").strip().lower()
    fn = FETCHERS.get(ex)
    if fn is None:
        raise RuntimeError(f"UNSUPPORTED_CANDLES: {UNSUPPORTED_NATIVE_CANDLES.get(ex, 'No native candle adapter registered.')}")
    if ex == "binance":
        market = "futures" if str(account).lower() in {"futures", "future", "perp", "perps"} else "spot"
        return fn(symbol, tf, limit=limit, market=market)
    return fn(symbol, tf, limit=limit)


def candle_error_class(error_message: str) -> str:
    msg = (error_message or "").upper()
    if msg.startswith("UNSUPPORTED_CANDLES"):
        return "UNSUPPORTED_CANDLES"
    if msg.startswith("UNSUPPORTED_TIMEFRAME"):
        return "UNSUPPORTED_TIMEFRAME"
    if msg.startswith("CANDLES_UNAVAILABLE"):
        return "CANDLES_UNAVAILABLE"
    return "CANDLES_UPSTREAM_ERROR"


def handle_candles_operation(exchange_name: str, account: str, request: Dict[str, Any]):
    symbol = str(request.get("symbol") or request.get("native_symbol") or "").strip()
    interval = str(request.get("interval") or request.get("tf") or request.get("timeframe") or "15m").strip()
    try:
        limit = int(request.get("limit") or 300)
    except (TypeError, ValueError):
        limit = 300
    if not symbol:
        return make_failure(operation="candles", exchange=exchange_name, account=account, code="MISSING_SYMBOL", message="symbol is required for candles.")
    if interval not in SUPPORTED_TFS:
        return make_failure(operation="candles", exchange=exchange_name, account=account, code="UNSUPPORTED_TIMEFRAME", message=f"Timeframe {interval!r} is not supported.")
    try:
        candles = fetch_for_exchange(exchange_name, symbol, interval, limit, account=account)
    except ValueError as exc:
        if str(exc) == "UNSUPPORTED_TIMEFRAME":
            return make_failure(operation="candles", exchange=exchange_name, account=account, code="UNSUPPORTED_TIMEFRAME", message=f"Timeframe {interval!r} is not supported on {exchange_name}.")
        return make_failure(operation="candles", exchange=exchange_name, account=account, code="CANDLE_ERROR", message=str(exc)[:240])
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if msg.startswith("UNSUPPORTED_CANDLES:"):
            code = "UNSUPPORTED_CANDLES"
            user_msg = msg.split(":", 1)[1].strip()
        elif msg.startswith("CANDLES_UNAVAILABLE:"):
            code = "CANDLES_UNAVAILABLE"
            user_msg = msg.split(":", 1)[1].strip()
        else:
            code = "CANDLES_UPSTREAM_ERROR"
            user_msg = msg[:240]
        if "HTTP Error" in user_msg or "url:" in user_msg.lower():
            user_msg = f"Candles unavailable for {symbol} on {exchange_name}."
        return make_failure(operation="candles", exchange=exchange_name, account=account, code=code, message=user_msg or f"Candles unavailable for {symbol} on {exchange_name}.")
    return make_success(operation="candles", exchange=exchange_name, account=account, data={"candles": candles, "symbol": symbol, "interval": interval, "source": "native", "count": len(candles)})
