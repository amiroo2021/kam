"""Exchange OHLCV fetchers for TradeDesk ``candles`` operations.

TradeMenu must not grow per-exchange if/elif chains. Agents call these
helpers (or ``handle_candles_operation``) and return normalized bars via
``CanonicalResponse.data["candles"]``.

Each fetcher returns a list of:
  {time (unix sec), open, high, low, close, volume}
sorted ascending, de-duplicated by time.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .canonical import make_failure, make_success, sanitize_error_message

SUPPORTED_TFS: Tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h", "4h", "1D")

_TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1D": 86400,
}


def normalize_candle(ts_ms: int, o: float, h: float, l: float, c: float, v: float = 0.0) -> Dict[str, Any]:
    return {
        "time": int(ts_ms // 1000),
        "open": float(o),
        "high": float(h),
        "low": float(l),
        "close": float(c),
        "volume": float(v),
    }


def finalize_candles(rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    rows = [r for r in rows if isinstance(r, dict) and r.get("time") is not None]
    rows.sort(key=lambda c: int(c["time"]))
    dedup: Dict[int, Dict[str, Any]] = {int(c["time"]): c for c in rows}
    ordered = [dedup[k] for k in sorted(dedup.keys())]
    limit_n = max(1, min(int(limit), 1500))
    return ordered[-limit_n:]


def _http_json(url: str, *, method: str = "GET", body: Optional[dict] = None, timeout: int = 25) -> Any:
    data = None
    headers = {"User-Agent": "Hermes-TradeDesk/1.0", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 public market data
        raw = resp.read().decode("utf-8", errors="replace")
    if not raw:
        return None
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Venue fetchers
# ---------------------------------------------------------------------------

def fetch_binance(symbol: str, tf: str, *, market: str = "futures", limit: int = 300) -> List[Dict[str, Any]]:
    interval = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1D": "1d"}.get(tf)
    if not interval:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    s = str(symbol or "").upper().replace("-", "").replace("_", "").replace("/", "")
    if s.endswith("USD") and not s.endswith(("USDT", "USDC")):
        s = s[:-3] + "USDT"
    if not s.endswith(("USDT", "USDC", "BUSD")):
        s = f"{s}USDT"
    if market == "spot":
        base, path = "https://api.binance.com", "/api/v3/klines"
    else:
        base, path = "https://fapi.binance.com", "/fapi/v1/klines"
    qs = urllib.parse.urlencode({"symbol": s, "interval": interval, "limit": min(int(limit), 1000)})
    rows = _http_json(f"{base}{path}?{qs}")
    out = []
    for row in rows or []:
        out.append(normalize_candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])))
    return finalize_candles(out, limit)


def fetch_hyperliquid(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    interval = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1D": "1d"}.get(tf)
    if not interval:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    coin = str(symbol or "").strip()
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
    tf_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[interval]
    start_ms = end_ms - tf_ms * max(int(limit), 50)
    payload = {"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms}}
    try:
        rows = _http_json("https://api.hyperliquid.xyz/info", method="POST", body=payload)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Hyperliquid rejected coin {coin!r} ({exc.code})") from exc
    if not isinstance(rows, list):
        raise RuntimeError("Unexpected Hyperliquid candle response")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        t = int(row.get("t") or 0)
        out.append(normalize_candle(t, float(row["o"]), float(row["h"]), float(row["l"]), float(row["c"]), float(row.get("v") or 0)))
    return finalize_candles(out, limit)


def fetch_phemex(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    # Prefer existing TradeMenu implementation for parity.
    from plugins.trade.trademenu import marketdata as md

    return md.fetch_phemex_candles(symbol, tf, limit=limit)


def fetch_mexc(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    from plugins.trade.trademenu import marketdata as md

    return md.fetch_mexc_candles(symbol, tf, limit=limit)


def fetch_raydium(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    from plugins.trade.trademenu import marketdata as md

    return md.fetch_raydium_candles(symbol, tf, limit=limit)


def fetch_arcus(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    from plugins.trade.trademenu import marketdata as md

    return md.fetch_arcus_candles(symbol, tf, limit=limit)


def fetch_rise(symbol: str, tf: str, limit: int = 300, *, api_base: str = "https://api.rise.trade") -> List[Dict[str, Any]]:
    """Rise trading-view OHLCV by numeric market id.

    Endpoint: GET /v1/markets/id/{market_id}/trading-view-data
    interval/from/to are nanoseconds. Native symbols are BTC, ETH, … (display BTC/USDC).
    """
    sec = _TF_SECONDS.get(tf)
    if not sec:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    market_id = _rise_market_id(symbol, api_base=api_base)
    if market_id is None:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Rise market not found for {symbol!r}")
    interval_ns = int(sec) * 1_000_000_000
    to_ns = int(time.time() * 1_000_000_000)
    limit_n = max(1, min(int(limit), 1000))
    from_ns = to_ns - interval_ns * limit_n
    qs = urllib.parse.urlencode({"interval": str(interval_ns), "from": str(from_ns), "to": str(to_ns)})
    url = f"{api_base.rstrip('/')}/v1/markets/id/{market_id}/trading-view-data?{qs}"
    data = _http_json(url, timeout=25)
    rows = _extract_list(data, keys=("data", "candles", "bars"))
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts_raw = row.get("time") or row.get("t") or 0
        try:
            ts = int(ts_raw)
        except (TypeError, ValueError):
            continue
        # nanoseconds → ms
        if ts > 10_000_000_000_000_000:  # ns
            ts_ms = ts // 1_000_000
        elif ts > 10_000_000_000_000:  # µs
            ts_ms = ts // 1000
        elif ts > 10_000_000_000:  # ms
            ts_ms = ts
        else:
            ts_ms = ts * 1000
        try:
            out.append(
                normalize_candle(
                    ts_ms,
                    float(row.get("open")),
                    float(row.get("high")),
                    float(row.get("low")),
                    float(row.get("close")),
                    float(row.get("volume") or 0),
                )
            )
        except (TypeError, ValueError):
            continue
    if not out:
        raise RuntimeError(f"Empty Rise candles for market_id={market_id} symbol={symbol}")
    return finalize_candles(out, limit)


_RISE_MARKET_CACHE: Dict[str, Any] = {"ts": 0.0, "by_sym": {}}


def _rise_market_id(symbol: str, *, api_base: str) -> Optional[str]:
    text = str(symbol or "").strip().upper()
    if not text:
        return None
    # Already a pure market id?
    if text.isdigit():
        return text
    now = time.time()
    if now - float(_RISE_MARKET_CACHE.get("ts") or 0) > 300 or not _RISE_MARKET_CACHE.get("by_sym"):
        data = _http_json(f"{api_base.rstrip('/')}/v1/markets", timeout=20)
        markets = []
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
            names = [
                cfg.get("name"),
                m.get("display_name"),
                m.get("base_asset_symbol"),
                m.get("underlying"),
            ]
            for n in names:
                key = str(n or "").strip().upper()
                if not key:
                    continue
                by_sym[key] = mid
                # BTC/USDC → BTC, BTC-USDC → BTC
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


def fetch_pacifica(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    interval = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1D": "1d"}.get(tf)
    if not interval:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    sym = str(symbol or "").strip().upper()
    for suffix in ("-USD", "-USDT", "-USDC", "USDT", "USDC", "USD"):
        if sym.endswith(suffix) and len(sym) > len(suffix) and ":" not in sym:
            # Pacifica uses bare BTC/ETH
            if suffix.startswith("-") or suffix in ("USDT", "USDC", "USD"):
                cand = sym[: -len(suffix)] if not suffix.startswith("-") else sym[: -len(suffix)]
                if cand and cand.isalpha():
                    sym = cand
                    break
    # BTCUSD → BTC
    if sym.endswith("USD") and len(sym) > 3 and sym[:-3].isalpha():
        sym = sym[:-3]
    sec = _TF_SECONDS[tf]
    limit_n = max(1, min(int(limit), 1000))
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - sec * 1000 * limit_n
    qs = urllib.parse.urlencode({"symbol": sym, "interval": interval, "start_time": start_ms, "end_time": end_ms})
    data = _http_json(f"https://api.pacifica.fi/api/v1/kline?{qs}", timeout=25)
    if not isinstance(data, dict) or not data.get("success"):
        raise RuntimeError(f"Pacifica candles failed for {sym}")
    rows = data.get("data")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Empty Pacifica candles for {sym}")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        t = int(row.get("t") or row.get("T") or 0)
        if t <= 0:
            continue
        out.append(
            normalize_candle(
                t if t > 10_000_000_000 else t * 1000,
                float(row.get("o") or row.get("open")),
                float(row.get("h") or row.get("high")),
                float(row.get("l") or row.get("low")),
                float(row.get("c") or row.get("close")),
                float(row.get("v") or row.get("volume") or 0),
            )
        )
    return finalize_candles(out, limit)


def _extract_list(data: Any, keys: Sequence[str] = ("data", "candles", "bars", "rows")) -> List[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for k in keys:
        v = data.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for k2 in keys:
                v2 = v.get(k2)
                if isinstance(v2, list):
                    return v2
    return []


# Exchange → fetcher (account may adjust binance spot/futures)
Fetcher = Callable[..., List[Dict[str, Any]]]

FETCHERS: Dict[str, Fetcher] = {
    "binance": fetch_binance,
    "hyperliquid": fetch_hyperliquid,
    "phemex": fetch_phemex,
    "mexc": fetch_mexc,
    "raydium": fetch_raydium,
    "arcus": fetch_arcus,
    "rise": fetch_rise,
    "pacifica": fetch_pacifica,
}

# Documented gaps (no public OHLCV found / auth-walled / empty) — do not fake.
UNSUPPORTED_NATIVE_CANDLES: Dict[str, str] = {
    "lighter": "Public candlesticks endpoint returns 403 without session; no alternate public OHLCV found.",
    "hibachi": "No public OHLCV endpoint discovered on api.hibachi.xyz / data-api.hibachi.xyz.",
    "edgex": "getKline returns empty dataList for probed contract ids; needs confirmed contract mapping.",
    "apex": "api/v3/klines returned empty data for BTC-USDT probes.",
    "nado": "No candlesticks query variant accepted on gateway/archive hosts.",
    "qfex": "Market data endpoints require authentication; no public candle path found.",
    "perpl": "No stable public candle API (TLS/host issues on probed endpoints).",
    "ondoperps": "No /v1/candles (or equivalent) on api.ondoperps.xyz.",
}


def has_native_candles(exchange: str) -> bool:
    return str(exchange or "").strip().lower() in FETCHERS


def fetch_for_exchange(exchange: str, symbol: str, tf: str, limit: int = 300, *, account: str = "") -> List[Dict[str, Any]]:
    ex = str(exchange or "").strip().lower()
    fn = FETCHERS.get(ex)
    if fn is None:
        reason = UNSUPPORTED_NATIVE_CANDLES.get(ex, "No native candle adapter registered.")
        raise RuntimeError(f"UNSUPPORTED_CANDLES: {reason}")
    if ex == "binance":
        market = "futures" if str(account).lower() in {"futures", "future", "perp", "perps"} else "spot"
        return fn(symbol, tf, market=market, limit=limit)
    return fn(symbol, tf, limit=limit)


def handle_candles_operation(exchange_name: str, account: str, request: Dict[str, Any]):
    """Shared agent handler for operation=candles."""
    symbol = str(request.get("symbol") or request.get("native_symbol") or "").strip()
    interval = str(request.get("interval") or request.get("tf") or request.get("timeframe") or "15m").strip()
    try:
        limit = int(request.get("limit") or 300)
    except (TypeError, ValueError):
        limit = 300
    if not symbol:
        return make_failure(
            operation="candles",
            exchange=exchange_name,
            account=account,
            code="MISSING_SYMBOL",
            message="symbol is required for candles.",
        )
    if interval not in SUPPORTED_TFS:
        return make_failure(
            operation="candles",
            exchange=exchange_name,
            account=account,
            code="UNSUPPORTED_TIMEFRAME",
            message=f"Timeframe {interval!r} is not supported.",
        )
    try:
        candles = fetch_for_exchange(exchange_name, symbol, interval, limit, account=account)
    except ValueError as exc:
        code = str(exc)
        if code == "UNSUPPORTED_TIMEFRAME":
            return make_failure(
                operation="candles",
                exchange=exchange_name,
                account=account,
                code="UNSUPPORTED_TIMEFRAME",
                message=f"Timeframe {interval!r} is not supported on {exchange_name}.",
            )
        return make_failure(
            operation="candles",
            exchange=exchange_name,
            account=account,
            code="CANDLE_ERROR",
            message=sanitize_error_message(str(exc)),
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        code = "UNSUPPORTED_CANDLES" if "UNSUPPORTED_CANDLES" in msg else "CANDLES_UNAVAILABLE"
        if msg.startswith("UNSUPPORTED_CANDLES:"):
            msg = msg.split(":", 1)[1].strip()
        elif msg.startswith("CANDLES_UNAVAILABLE:"):
            msg = msg.split(":", 1)[1].strip()
        clean = sanitize_error_message(msg)[:240]
        if "HTTP Error" in clean or "url:" in clean.lower():
            clean = f"Candles unavailable for {symbol} on {exchange_name}."
        return make_failure(
            operation="candles",
            exchange=exchange_name,
            account=account,
            code=code,
            message=clean or f"Candles unavailable for {symbol} on {exchange_name}.",
        )
    return make_success(
        operation="candles",
        exchange=exchange_name,
        account=account,
        data={
            "candles": candles,
            "symbol": symbol,
            "interval": interval,
            "source": "native",
            "count": len(candles),
        },
    )
