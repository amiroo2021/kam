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

from datetime import datetime, timedelta, timezone
import gzip
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .canonical import make_failure, make_success


_WALLET_HEX_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _load_dotenv_values(path: Path) -> Dict[str, str]:
    if not path.is_file():
        return {}
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        try:
            text = path.read_text(encoding="latin-1")
        except OSError:
            return {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        values[key] = value
    return values


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
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            ts = int(text)
        else:
            try:
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                dt = datetime.fromisoformat(text)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1000)
            except ValueError:
                return None
    if ts > 10_000_000_000_000_000:  # ns
        return ts // 1_000_000
    if ts > 10_000_000_000_000:  # µs
        return ts // 1000
    if ts > 10_000_000_000:  # ms
        return ts
    return ts * 1000


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

def _scaled_float(value: Any, scale: float) -> float:
    return float(value) / scale


# ---------------------------------------------------------------------------
# Apex
# ---------------------------------------------------------------------------

def _apex_interval(tf: str) -> str:
    return {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720", "1D": "D", "1W": "W", "1M": "M"}[tf]


def _apex_symbol_meta(symbol: str, *, base: str = "https://omni.apex.exchange") -> Dict[str, Any] | None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    payload = _http_json(f"{base.rstrip('/')}/api/v3/symbols", timeout=20)
    contracts = (((payload or {}).get("data") or {}).get("contractConfig") or {}).get("perpetualContract")
    if not isinstance(contracts, list):
        return None
    normalized = sym.replace("-", "").replace("/", "").replace("_", "")
    for row in contracts:
        if not isinstance(row, dict):
            continue
        native = str(row.get("symbol") or row.get("symbolDisplayName") or row.get("crossSymbolName") or "").strip().upper()
        native_norm = native.replace("-", "").replace("/", "").replace("_", "")
        if sym == native or normalized == native_norm:
            return row
    return None


def fetch_apex(symbol: str, tf: str, limit: int = 300, *, base: str = "https://omni.apex.exchange") -> List[Dict[str, Any]]:
    if tf not in _TF_SECONDS:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    meta = _apex_symbol_meta(symbol, base=base)
    if not meta:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Apex contract not found for {symbol}")
    wire_symbol = str(meta.get("crossSymbolName") or meta.get("symbolDisplayName") or meta.get("symbol") or "").strip().upper()
    if not wire_symbol:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Apex symbol missing for {symbol}")
    interval = _apex_interval(tf)
    count = max(1, min(int(limit), 200))
    end_s = int(time.time())
    start_s = max(0, end_s - count * _TF_SECONDS[tf])
    params = {
        "symbol": wire_symbol,
        "interval": interval,
        "start": str(start_s),
        "end": str(end_s),
        "limit": str(count),
    }
    data = _http_json(f"{base.rstrip('/')}/api/v3/klines?{urllib.parse.urlencode(params)}", timeout=30)
    rows: List[Any] = []
    if isinstance(data, dict):
        payload = data.get("data") or data.get("klines") or data.get("rows") or {}
        if isinstance(payload, dict):
            rows = (
                payload.get(wire_symbol)
                or payload.get(wire_symbol.replace("-", ""))
                or payload.get("klines")
                or payload.get("dataList")
                or payload.get("rows")
                or payload.get("data")
                or []
            )
        elif isinstance(payload, list):
            rows = payload
    elif isinstance(data, list):
        rows = data
    out: List[Dict[str, Any]] = []
    for row in rows:
        if isinstance(row, list) and len(row) >= 6:
            out.append(normalize_candle(row[0], row[1], row[2], row[3], row[4], row[5]))
        elif isinstance(row, dict):
            out.append(normalize_candle(row.get("start") or row.get("time") or row.get("t") or row.get("openTime"), row.get("open") or row.get("o"), row.get("high") or row.get("h"), row.get("low") or row.get("l"), row.get("close") or row.get("c"), row.get("volume") or row.get("v") or row.get("turnover") or 0))
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
    secs = _TF_SECONDS[tf]
    rows_req = max(limit * max(secs // 60, 1) * 2, limit * 2, 120)
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=rows_req * 60)
    params = {
        "resolution": res,
        "fromISO": start.isoformat().replace("+00:00", "Z"),
        "toISO": end.isoformat().replace("+00:00", "Z"),
    }
    url = f"{base.rstrip('/')}/candles/{urllib.parse.quote(sym)}?{urllib.parse.urlencode(params)}"
    data = _http_json(url, timeout=20)
    rows = []
    if isinstance(data, dict):
        rows = data.get("candles") or []
        if not rows:
            for k in ("data", "rows", "result"):
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
            started = row.get("startedAt") or row.get("time") or row.get("t") or row.get("openTime")
            out.append(normalize_candle(started, row.get("open") or row.get("o"), row.get("high") or row.get("h"), row.get("low") or row.get("l"), row.get("close") or row.get("c"), row.get("baseTokenVolume") or row.get("volume") or row.get("v") or 0))
    if not out:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: QFEX returned no candles for {symbol}")
    out = finalize_candles(out, limit) if tf == "1m" else _qfex_resample(out, tf, limit)
    return out


# ---------------------------------------------------------------------------
# Rise
# ---------------------------------------------------------------------------

_RISE_MARKET_CACHE: Dict[str, Any] = {"ts": 0.0, "by_sym": {}}
_RISE_QUOTE_SUFFIXES = {"USD", "USDT", "USDC", "PERP"}
_RISE_TRADE_CACHE: Dict[str, Any] = {"market_id": "", "ts": 0.0, "rows": []}
_RISE_TRADE_CACHE_TTL_SECONDS = 30


def _rise_symbol(raw_market_name: Any) -> str:
    market_name = str(raw_market_name or "").strip().upper()
    if "/" in market_name:
        market_name = market_name.split("/", 1)[0]
    if "-" in market_name:
        market_name = market_name.split("-", 1)[0]
    if "_" in market_name:
        market_name = market_name.split("_", 1)[0]
    return market_name.strip().upper() or "UNKNOWN"


def _rise_alias_keys(symbol: str) -> List[str]:
    raw = _rise_symbol(symbol)
    keys: List[str] = []
    if raw and raw != "UNKNOWN":
        keys.append(raw)
    if "-" in raw:
        base, rest = raw.split("-", 1)
        if rest in _RISE_QUOTE_SUFFIXES and base:
            keys.append(base)
    compact = raw.replace("-", "").replace("/", "").replace("_", "")
    if compact and compact not in keys:
        keys.append(compact)
    for suffix in ("USDT", "USDC", "USD", "PERP"):
        if compact.endswith(suffix) and len(compact) > len(suffix):
            base = compact[: -len(suffix)]
            if base and base not in keys:
                keys.append(base)
            break
    return keys


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
                for key in _rise_alias_keys(str(n or "")):
                    if key and key not in by_sym:
                        by_sym[key] = mid
        _RISE_MARKET_CACHE["ts"] = now
        _RISE_MARKET_CACHE["by_sym"] = by_sym
    by_sym = _RISE_MARKET_CACHE.get("by_sym") or {}
    for key in _rise_alias_keys(text):
        if key in by_sym:
            return by_sym[key]
    compact = text.replace("/", "").replace("-", "").replace("_", "")
    if compact in by_sym:
        return by_sym[compact]
    for suffix in ("USDC", "USDT", "USD"):
        if compact.endswith(suffix) and compact[: -len(suffix)] in by_sym:
            return by_sym[compact[: -len(suffix)]]
    base = text.split("/")[0].split("-")[0]
    return by_sym.get(base)


def _rise_trade_history_page(market_id: str, *, api_base: str, page: int = 1, limit: int = 1000) -> List[Dict[str, Any]]:
    params = {"market_id": str(market_id), "page": str(page), "limit": str(min(max(int(limit), 1), 1000))}
    url = f"{api_base.rstrip('/')}/v1/markets/id/{market_id}/trade-history?{urllib.parse.urlencode(params)}"
    payload = _http_json(url, timeout=30)
    if not isinstance(payload, dict):
        raise RuntimeError("CANDLES_UNAVAILABLE: Rise trade-history response was not an object")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if not isinstance(data, dict):
        raise RuntimeError("CANDLES_UNAVAILABLE: Rise trade-history missing data object")
    trades = data.get("trades")
    if not isinstance(trades, list):
        raise RuntimeError("CANDLES_UNAVAILABLE: Rise trade-history missing trades list")
    rows: List[Dict[str, Any]] = []
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        ts = _extract_ts_ms(trade.get("time") or trade.get("timestamp"))
        if ts is None:
            continue
        try:
            price = float(trade.get("price"))
            size = float(trade.get("size"))
        except (TypeError, ValueError):
            continue
        if price <= 0 or size <= 0:
            continue
        rows.append({"id": str(trade.get("id") or ""), "time": ts, "open": price, "high": price, "low": price, "close": price, "volume": size})
    return rows


def _rise_trade_history_1m_candles(symbol: str, market_id: str, api_base: str, *, min_history_minutes: int = 0) -> List[Dict[str, Any]]:
    now = time.time()
    cache = _RISE_TRADE_CACHE
    cached_market = str(cache.get("market_id") or "")
    cached_rows = cache.get("rows")
    cached_ts = float(cache.get("ts") or 0)
    if cached_market == market_id and now - cached_ts <= _RISE_TRADE_CACHE_TTL_SECONDS:
        if isinstance(cached_rows, list) and cached_rows:
            if not min_history_minutes:
                return cached_rows
            span_ms = int(min_history_minutes) * 60_000
            latest_ts = int(cached_rows[-1]["time"])
            oldest_ts = int(cached_rows[0]["time"])
            if latest_ts - oldest_ts >= span_ms:
                return cached_rows

    limit = 1000
    seen_ids: set[str] = set()
    rows_by_id: Dict[str, Dict[str, Any]] = {}
    page = 1
    latest_ts: Optional[int] = None
    oldest_ts: Optional[int] = None
    target_start_ms: Optional[int] = None
    max_pages = 100
    while page <= max_pages:
        page_rows = _rise_trade_history_page(market_id, api_base=api_base, page=page, limit=limit)
        if not page_rows:
            break
        if latest_ts is None:
            latest_ts = int(page_rows[0]["time"])
            if min_history_minutes:
                target_start_ms = latest_ts - int(min_history_minutes) * 60_000
        new_rows = 0
        for trade in page_rows:
            trade_id = str(trade.get("id") or "")
            if trade_id and trade_id in seen_ids:
                continue
            if trade_id:
                seen_ids.add(trade_id)
                rows_by_id[trade_id] = trade
            else:
                key = f"{trade['time']}:{trade['open']}:{trade['volume']}:{len(rows_by_id)}"
                rows_by_id[key] = trade
            new_rows += 1
            ts = int(trade["time"])
            oldest_ts = ts if oldest_ts is None else min(oldest_ts, ts)
        if new_rows == 0:
            break
        if target_start_ms is not None and oldest_ts is not None and oldest_ts <= target_start_ms:
            break
        page += 1

    rows = sorted(rows_by_id.values(), key=lambda r: (int(r["time"]), str(r.get("id") or "")))
    if not rows:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Rise trade-history returned no usable trades for market {market_id}")
    buckets: Dict[int, Dict[str, Any]] = {}
    for trade in rows:
        ts = int(trade["time"])
        bucket = (ts // 60_000) * 60_000
        size = float(trade["volume"])
        price = float(trade["close"])
        existing = buckets.get(bucket)
        if existing is None:
            buckets[bucket] = {"time": bucket, "open": price, "high": price, "low": price, "close": price, "volume": size}
        else:
            existing["high"] = max(existing["high"], price)
            existing["low"] = min(existing["low"], price)
            existing["close"] = price
            existing["volume"] += size
    candles = list(buckets.values())
    candles.sort(key=lambda x: x["time"])
    if not candles:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Rise trade-history produced no 1m candles for {symbol}")
    _RISE_TRADE_CACHE.update({"market_id": market_id, "ts": now, "rows": candles})
    return candles


def _rise_fetch_trade_candles(symbol: str, market_id: str, target_sec: int, limit: int, *, api_base: str) -> List[Dict[str, Any]]:
    required_1m = max(limit, 1)
    if target_sec > 60:
        required_1m = max(required_1m, int((target_sec // 60) * limit))
        if target_sec % 60:
            required_1m = max(required_1m, int(((target_sec // 60) + 1) * limit))
    base_1m = _rise_trade_history_1m_candles(symbol, market_id, api_base, min_history_minutes=required_1m)
    if target_sec <= 60:
        return finalize_candles(base_1m, limit)
    return _resample_1m(base_1m, target_sec, limit)


def fetch_rise(symbol: str, tf: str, limit: int = 300, *, api_base: str = "https://api.rise.trade") -> List[Dict[str, Any]]:
    sec = _TF_SECONDS.get(tf)
    if not sec:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    market_id = _rise_market_id(symbol, api_base=api_base)
    if market_id is None:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Rise market not found for {symbol!r}")
    # Rise does not expose a native OHLC endpoint; build candles from
    # historical market trades (1m base candles only) and resample locally.
    return _rise_fetch_trade_candles(symbol, market_id, sec, limit, api_base=api_base)


# ---------------------------------------------------------------------------
# Perpl
# ---------------------------------------------------------------------------

def fetch_perpl(symbol: str, tf: str, limit: int = 300, *, account: str = "") -> List[Dict[str, Any]]:
    sec = _TF_SECONDS.get(tf)
    if not sec:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    if not account:
        raise RuntimeError("CANDLES_UNAVAILABLE: Perpl account is required for candle access")
    from .agents import x_perpl_agent as perpl  # local import to avoid cycle

    creds = perpl._credentials(account)
    if not creds:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Unknown Perpl account {account!r}")
    markets = perpl._markets_index(str(creds["api_url"]))
    matched = perpl._match_market(symbol, markets)
    if matched is None:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Perpl market not found for {symbol!r}")
    mid = int(matched["id"])
    price_decimals = int(matched.get("price_decimals") or 0)
    price_scale = float(10 ** max(price_decimals, 0)) if price_decimals >= 0 else 1.0

    # Keep each authenticated request within a smaller window; Perpl rejects
    # oversized range requests with HTTP 400 on some markets.
    target_1m = max(limit * max(sec // 60, 1) + 50, 100)
    max_chunk_1m = 250
    max_pages = 8
    candles_by_time: Dict[int, Dict[str, Any]] = {}
    remaining = target_1m
    to_ms = int(time.time() * 1000)
    for _page in range(max_pages):
        chunk_1m = min(max_chunk_1m, remaining)
        from_ms = to_ms - chunk_1m * 60_000
        path = f"/v1/market-data/{mid}/candles/60/{from_ms}-{to_ms}"
        status, data, body = perpl._signed_request(creds, "GET", path, timeout=30.0)
        if status != 200 or not isinstance(data, dict):
            detail = ""
            if isinstance(data, dict):
                detail = str(data.get('message') or data.get('error') or data)
            elif isinstance(body, str):
                detail = body[:200]
            raise RuntimeError(f"CANDLES_UNAVAILABLE: Perpl candles request failed HTTP {status}{': ' + detail if detail else ''}")
        rows = data.get("d") or []
        if not isinstance(rows, list) or not rows:
            if candles_by_time:
                break
            raise RuntimeError(f"CANDLES_UNAVAILABLE: Empty Perpl candles for {symbol}")
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                o_raw = row.get("o") or row.get("open")
                h_raw = row.get("h") or row.get("high")
                l_raw = row.get("l") or row.get("low")
                c_raw = row.get("c") or row.get("close")
                candle = normalize_candle(
                    row.get("t") or row.get("time") or row.get("startedAt"),
                    _scaled_float(o_raw, price_scale),
                    _scaled_float(h_raw, price_scale),
                    _scaled_float(l_raw, price_scale),
                    _scaled_float(c_raw, price_scale),
                    row.get("v") or row.get("volume") or 0,
                )
            except Exception:
                continue
            candles_by_time[candle["time"]] = candle
        oldest = min(candles_by_time) if candles_by_time else None
        if oldest is None or oldest <= from_ms:
            break
        remaining = max(0, remaining - chunk_1m)
        to_ms = from_ms
        if remaining <= 0:
            break
    out = sorted(candles_by_time.values(), key=lambda c: c["time"])
    if not out:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Perpl returned no usable candles for {symbol}")
    base = finalize_candles(out, max(limit * max(sec // 60, 1) * 2, limit * 2, 120))
    return finalize_candles(base, limit) if sec <= 60 else _resample_1m(base, sec, limit)


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
    interval = _TF_SECONDS.get(tf)
    if not interval:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise RuntimeError("CANDLES_UNAVAILABLE: Empty Pacifica symbol")
    limit_n = max(1, min(int(limit), 1000))
    start_time = int((time.time() * 1000) - (interval * max(limit_n, 1) * 2 * 1000))
    url = "https://api.pacifica.fi/api/v1/kline"
    qs = urllib.parse.urlencode({"symbol": sym, "interval": tf, "start_time": start_time})
    data = _http_json(f"{url}?{qs}", timeout=20)
    if not isinstance(data, dict):
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Unexpected Pacifica candle response for {sym}")
    rows = data.get("data")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Empty Pacifica candles for {sym}")
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            candle = normalize_candle(
                row.get("t") or row.get("time") or row.get("startedAt"),
                row.get("o") or row.get("open"),
                row.get("h") or row.get("high"),
                row.get("l") or row.get("low"),
                row.get("c") or row.get("close"),
                row.get("v") or row.get("volume") or 0,
            )
        except Exception:
            continue
        out.append(candle)
    if not out:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Pacifica returned no usable candles for {sym}")
    out = finalize_candles(out, max(limit_n * 2, 120))
    if interval > 60:
        out = _resample_1m(out, interval, limit_n)
    return finalize_candles(out, limit_n)


def _nado_gateway_query(payload: Dict[str, Any], *, base: str = "https://api.prod.nado.xyz/gateway/v1") -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base.rstrip('/')}/query",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br",
            "User-Agent": "Hermes-KAM/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 public docs API
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
    if b"\x1f\x8b" in raw[:4] or "gzip" in enc:
        raw = gzip.decompress(raw)
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError("Nado gateway returned a non-object JSON payload.")
    return parsed


def _nado_archive_query(payload: Dict[str, Any], *, base: str = "https://api.prod.nado.xyz/archive/v1") -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip('/'),
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br",
            "User-Agent": "Hermes-KAM/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 public docs API
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
    if b"\x1f\x8b" in raw[:4] or "gzip" in enc:
        raw = gzip.decompress(raw)
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError("Nado archive returned a non-object JSON payload.")
    return parsed


def _nado_resolve_product_id(symbol: str) -> tuple[int, str]:
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise RuntimeError("CANDLES_UNAVAILABLE: Empty Nado symbol")
    payload = _nado_gateway_query({"type": "symbols"})
    if str(payload.get("status") or "").lower() != "success":
        raise RuntimeError(str(payload.get("error") or "symbols query failed"))
    symbols = (payload.get("data") or {}).get("symbols") or {}
    if not isinstance(symbols, dict):
        raise RuntimeError("Nado symbols payload missing symbols map")
    candidates = [sym, f"{sym}-PERP", sym.removesuffix("-PERP")]
    for row in symbols.values():
        if not isinstance(row, dict):
            continue
        key = str(row.get("symbol") or "").upper()
        if key in candidates or str(row.get("display_name") or "").upper() in candidates:
            try:
                return int(row["product_id"]), key
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"CANDLES_UNAVAILABLE: Invalid Nado product for {sym}") from exc
    raise RuntimeError(f"CANDLES_UNAVAILABLE: Nado product not found for {sym}")


def fetch_nado(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    granularity = _TF_SECONDS.get(tf)
    if not granularity:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    pid, _nado_symbol = _nado_resolve_product_id(symbol)
    limit_n = max(1, min(int(limit), 500))
    payload = {"candlesticks": {"product_id": pid, "granularity": int(granularity), "limit": limit_n}}
    data = _nado_archive_query(payload)
    rows = data.get("candlesticks")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Empty Nado candles for {symbol}")
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            ts = int(row.get("timestamp") or 0)
        except Exception:
            continue
        try:
            candle = normalize_candle(
                ts * 1000 if ts < 10_000_000_000 else ts,
                _scaled_float(row.get("open_x18"), 10**18),
                _scaled_float(row.get("high_x18"), 10**18),
                _scaled_float(row.get("low_x18"), 10**18),
                _scaled_float(row.get("close_x18"), 10**18),
                _scaled_float(row.get("volume"), 10**18),
            )
        except Exception:
            continue
        out.append(candle)
    if not out:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Nado returned no usable candles for {symbol}")
    out = finalize_candles(out, limit_n)
    if granularity > 60 and granularity != _TF_SECONDS.get(tf):
        out = _resample_1m(out, granularity, limit_n)
    return finalize_candles(out, limit_n)


def fetch_hyperliquid(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    from plugins.trade.webtrade import marketdata as md
    return md.fetch_hyperliquid_candles(symbol, tf, limit=limit)


def fetch_binance(symbol: str, tf: str, limit: int = 300, *, market: str = "spot") -> List[Dict[str, Any]]:
    from plugins.trade.webtrade import marketdata as md
    return md.fetch_binance_candles(symbol, tf, limit=limit, market=market)


def fetch_ondoperps(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    from plugins.trade.webtrade import marketdata as md
    return md.fetch_ondoperps_candles(symbol, tf, limit=limit)


_EDGEX_KLINE_TYPE = {
    "1m": "MINUTE_1",
    "5m": "MINUTE_5",
    "15m": "MINUTE_15",
    "30m": "MINUTE_30",
    "1h": "HOUR_1",
    "4h": "HOUR_4",
    "1D": "DAY_1",
}


def _edgex_metadata() -> Dict[str, Any]:
    payload = _http_json("https://edgex-prod-v2.edgex.exchange/api/v2/public/meta/getMetaData", timeout=30)
    if not isinstance(payload, dict) or str(payload.get("code") or "").upper() not in {"SUCCESS", "0"}:
        raise RuntimeError("METADATA_UNAVAILABLE")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise RuntimeError("METADATA_UNAVAILABLE")
    return data


def _edgex_resolve_contract(symbol: str) -> Optional[tuple[str, str]]:
    text = str(symbol or "").strip().upper()
    if not text:
        return None
    try:
        rows = _edgex_metadata().get("contractList") or []
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    normalized = text.replace("/", "").replace("-", "").replace("_", "")
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("contractId") or "").strip()
        cname = str(row.get("contractName") or "").strip().upper()
        if not cid or not cname:
            continue
        cname_norm = cname.replace("/", "").replace("-", "").replace("_", "")
        if normalized == cname_norm or normalized == cname_norm.replace("USDT", "USDC") or normalized == cname_norm.replace("USDC", "USDT") or normalized == cname_norm.replace("USD", "USDT"):
            return cid, cname
        if normalized == cname_norm[:-4] if cname_norm.endswith(("USDT", "USDC")) else False:
            return cid, cname
    return None


def fetch_edgex(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    kline_type = _EDGEX_KLINE_TYPE.get(tf)
    if not kline_type:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    resolved = _edgex_resolve_contract(symbol)
    if not resolved:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: EdgeX contract not found for {symbol}")
    contract_id, native = resolved
    count = max(1, min(int(limit), 1000))
    end_ms = int(time.time() * 1000)
    start_ms = max(0, end_ms - count * _TF_SECONDS[tf] * 1000 * 3)
    params = {
        "contractId": contract_id,
        "klineType": kline_type,
        "filterBeginKlineTimeInclusive": str(start_ms),
        "filterEndKlineTimeExclusive": str(end_ms),
        "priceType": "LAST_PRICE",
        "size": str(count),
    }
    payload = _http_json(f"https://edgex-prod-v2.edgex.exchange/api/v2/public/quote/getKline?{urllib.parse.urlencode(params)}", timeout=30)
    if not isinstance(payload, dict):
        raise RuntimeError(f"CANDLES_UNAVAILABLE: EdgeX returned unexpected payload for {symbol}")
    if str(payload.get("code") or "").upper() not in {"SUCCESS", "0"}:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: EdgeX rejected kline request for {symbol}")
    data = payload.get("data") or {}
    rows = data.get("dataList") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Empty EdgeX candles for {symbol}")
    candles: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            candles.append(
                normalize_candle(
                    row.get("klineTime") or row.get("startTime") or row.get("time") or row.get("t"),
                    row.get("open") or row.get("o"),
                    row.get("high") or row.get("h"),
                    row.get("low") or row.get("l"),
                    row.get("close") or row.get("c"),
                    row.get("size") or row.get("value") or row.get("trades") or 0,
                )
            )
        except Exception:
            continue
    if not candles:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Parsed no usable EdgeX candles for {symbol}")
    return finalize_candles(candles, count)


_LIGHTER_MARKET_CACHE: Dict[str, Any] = {"ts": 0.0, "by_sym": {}, "by_id": {}}


def _lighter_api_base() -> str:
    return "https://mainnet.zklighter.elliot.ai"


def _lighter_markets() -> Dict[str, Any]:
    now = time.time()
    cache = _LIGHTER_MARKET_CACHE
    if now - float(cache.get("ts") or 0.0) < 300 and cache.get("by_sym"):
        return cache
    payload = _http_json(f"{_lighter_api_base()}/api/v1/orderBooks")
    by_sym: Dict[str, Any] = {}
    by_id: Dict[str, Any] = {}
    markets = []
    if isinstance(payload, dict):
        for key in ("c", "data", "markets", "orderBooks", "order_books"):
            value = payload.get(key)
            if isinstance(value, list):
                markets = value
                break
    elif isinstance(payload, list):
        markets = payload
    for m in markets:
        if not isinstance(m, dict):
            continue
        mid = m.get("market_id") or m.get("id") or m.get("marketId")
        sym = m.get("symbol") or m.get("ticker") or m.get("market_symbol") or m.get("name")
        if mid is None or not sym:
            continue
        by_sym[str(sym).strip().upper()] = m
        by_id[str(mid).strip()] = m
    cache.update({"ts": now, "by_sym": by_sym, "by_id": by_id})
    return cache


def _lighter_market_id(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    if not text:
        raise RuntimeError("CANDLES_UNAVAILABLE: Empty Lighter symbol")
    cache = _lighter_markets()
    by_sym = cache.get("by_sym") or {}
    if text in by_sym:
        m = by_sym[text]
        mid = m.get("market_id") or m.get("id") or m.get("marketId")
        if mid is not None:
            return str(mid)
    if text in cache.get("by_id") or {}:
        return text
    # Be permissive with BTC/BTC-PERP style display labels.
    for key, m in by_sym.items():
        key_norm = key.replace("-PERP", "").replace("/", "").replace("_", "")
        text_norm = text.replace("-PERP", "").replace("/", "").replace("_", "")
        if key_norm == text_norm:
            mid = m.get("market_id") or m.get("id") or m.get("marketId")
            if mid is not None:
                return str(mid)
    raise RuntimeError(f"CANDLES_UNAVAILABLE: Unknown Lighter market for {symbol}")


def _lighter_candles_json(market_id: str, resolution: str, start_ts_ms: int, end_ts_ms: int, count_back: int) -> Dict[str, Any]:
    params = {
        "market_id": str(market_id),
        "resolution": resolution,
        "start_timestamp": str(int(start_ts_ms)),
        "end_timestamp": str(int(end_ts_ms)),
        "count_back": str(int(count_back)),
        "set_timestamp_to_end": "false",
    }
    url = f"{_lighter_api_base()}/api/v1/candles?{urllib.parse.urlencode(params)}"
    payload = _http_json(url, timeout=30)
    if not isinstance(payload, dict):
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Lighter candles returned unexpected payload for market {market_id}")
    return payload


def fetch_lighter(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    if tf not in _TF_SECONDS:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    sec = _TF_SECONDS[tf]
    market_id = _lighter_market_id(symbol)
    count_back = max(1, min(int(limit), 500))
    end_ts_ms = int(time.time() * 1000)
    start_ts_ms = max(0, end_ts_ms - (count_back * sec * 1000 * 2))
    payload = _lighter_candles_json(market_id, tf, start_ts_ms, end_ts_ms, count_back)
    rows = payload.get("c") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Empty Lighter candles for {symbol}")
    candles: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            candles.append(
                normalize_candle(
                    row.get("t") or row.get("time"),
                    row.get("o") or row.get("O") or row.get("open"),
                    row.get("h") or row.get("H") or row.get("high"),
                    row.get("l") or row.get("L") or row.get("low"),
                    row.get("c") or row.get("C") or row.get("close"),
                    row.get("v") or row.get("volume") or 0,
                )
            )
        except Exception:
            continue
    candles = [c for c in candles if c["time"] and c["close"]]
    if not candles:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Parsed no usable Lighter candles for {symbol}")
    return _resample_1m(candles, sec, limit)


_HIBACHI_INTERVAL = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
    "1D": "1d",
}


def _hibachi_market_symbol(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    if not text:
        raise RuntimeError("CANDLES_UNAVAILABLE: Empty Hibachi symbol")
    if text.endswith("-P"):
        text = text[:-2]
    if ":" in text:
        text = text.split(":", 1)[1]
    if "/" in text:
        return text
    # Preserve native base symbols like HYPE/BTC/ETH and map to Hibachi perp form.
    return f"{text}/USDT-P"


def fetch_hibachi(symbol: str, tf: str, limit: int = 300) -> List[Dict[str, Any]]:
    interval = _HIBACHI_INTERVAL.get(tf)
    if not interval:
        raise ValueError("UNSUPPORTED_TIMEFRAME")
    market_symbol = _hibachi_market_symbol(symbol)
    count = max(1, min(int(limit), 500))
    end_ms = int(time.time() * 1000)
    start_ms = max(0, end_ms - count * _TF_SECONDS[tf] * 1000 * 3)
    params = {
        "symbol": market_symbol,
        "interval": interval,
        "fromMs": str(start_ms),
        "toMs": str(end_ms),
    }
    payload = _http_json(f"https://data-api.hibachi.xyz/market/data/klines?{urllib.parse.urlencode(params)}", timeout=30)
    rows = None
    if isinstance(payload, dict):
        rows = payload.get("klines") or payload.get("candles") or payload.get("data")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Empty Hibachi candles for {symbol}")
    candles: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            candles.append(
                normalize_candle(
                    row.get("timestamp") or row.get("time") or row.get("t"),
                    row.get("open") or row.get("o"),
                    row.get("high") or row.get("h"),
                    row.get("low") or row.get("l"),
                    row.get("close") or row.get("c"),
                    row.get("volumeNotional") or row.get("volume") or row.get("v") or 0,
                )
            )
        except Exception:
            continue
    if not candles:
        raise RuntimeError(f"CANDLES_UNAVAILABLE: Parsed no usable Hibachi candles for {symbol}")
    return finalize_candles(candles, count)


FETCHERS: Dict[str, Callable[..., List[Dict[str, Any]]]] = {
    "binance": fetch_binance,
    "hyperliquid": fetch_hyperliquid,
    "phemex": fetch_phemex,
    "mexc": fetch_mexc,
    "raydium": fetch_raydium,
    "arcus": fetch_arcus,
    "rise": fetch_rise,
    "perpl": fetch_perpl,
    "pacifica": fetch_pacifica,
    "nado": fetch_nado,
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
    if ex == "perpl":
        return fn(symbol, tf, limit=limit, account=account)
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
        elif "HTTP Error" in msg or "url:" in msg.lower() or "forbidden" in msg.lower() or "empty rise candles" in msg.lower():
            code = "CANDLES_UNAVAILABLE"
            user_msg = f"Candles unavailable for {symbol} on {exchange_name}."
        else:
            code = "CANDLES_UPSTREAM_ERROR"
            user_msg = msg[:240]
        if "HTTP Error" in user_msg or "url:" in user_msg.lower():
            user_msg = f"Candles unavailable for {symbol} on {exchange_name}."
        return make_failure(operation="candles", exchange=exchange_name, account=account, code=code, message=user_msg or f"Candles unavailable for {symbol} on {exchange_name}.")
    return make_success(operation="candles", exchange=exchange_name, account=account, data={"candles": candles, "symbol": symbol, "interval": interval, "source": "native", "count": len(candles)})
