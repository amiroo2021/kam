"""Binance public market-data agent for KAM /trade + /backtest.

Public-only: resolves Binance Spot / USD-M Futures symbols and returns latest price.
No credentials, balances, or order operations.
"""
from __future__ import annotations
from plugins.trade.candles import handle_candles_operation, has_native_candles

import json
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping

from ..canonical import CanonicalInstrument, CanonicalMarketPrice, make_failure, make_success, sanitize_error_message

name = "binance"
_SPOT = "https://api.binance.com"
_FUTURES = "https://fapi.binance.com"
_TIMEOUT = 15


def capabilities():
    return [
        "candles",
        "resolve_instrument",
        "market_price",
    ]


def list_accounts():
    # Treat account as market type selector for public-data operations.
    return [
        {"account": "spot", "label": "Spot"},
        {"account": "futures", "label": "Futures"},
    ]


def _market_type(account: str, request: Mapping[str, Any]) -> str:
    raw = str(request.get("market_type") or account or "spot").strip().lower()
    if raw in {"future", "futures", "perp", "perps", "usd-m", "usdm"}:
        return "futures"
    return "spot"


def _get_json(base: str, path: str, params: Mapping[str, Any]) -> Any:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{base}{path}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, headers={"User-Agent": "Hermes-KAM-BinanceAgent/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 public HTTPS API
        return json.loads(resp.read().decode("utf-8"))


def _normalize_symbol(raw: str) -> str:
    s = str(raw or "").strip().upper().replace("/", "").replace("-", "").replace("_", "")
    aliases = {"XBT": "BTC", "HYPERLIQUID": "HYPE"}
    s = aliases.get(s, s)
    if s and not s.endswith("USDT"):
        s = f"{s}USDT"
    return s


def _exchange_info(market_type: str, symbol: str) -> Dict[str, Any] | None:
    base = _FUTURES if market_type == "futures" else _SPOT
    data = _get_json(base, "/fapi/v1/exchangeInfo" if market_type == "futures" else "/api/v3/exchangeInfo", {"symbol": symbol})
    symbols = data.get("symbols") or []
    if not symbols:
        return None
    item = symbols[0]
    status = str(item.get("status") or "").upper()
    if status not in {"TRADING", ""}:
        return None
    return item


def _price(market_type: str, symbol: str) -> str:
    base = _FUTURES if market_type == "futures" else _SPOT
    data = _get_json(base, "/fapi/v1/ticker/price" if market_type == "futures" else "/api/v3/ticker/price", {"symbol": symbol})
    return str(data.get("price") or "")


def _resolve_instrument(account: str, request: Mapping[str, Any]):
    operation = "resolve_instrument"
    mt = _market_type(account, request)
    requested = str(request.get("symbol") or request.get("query") or "").strip()
    symbol = _normalize_symbol(requested)
    try:
        info = _exchange_info(mt, symbol)
        if not info:
            return make_failure(operation, name, mt, "INSTRUMENT_NOT_FOUND", f"Instrument not found on Binance {mt}: {requested}")
        filters = {str(f.get("filterType")): f for f in info.get("filters") or [] if isinstance(f, dict)}
        pf = filters.get("PRICE_FILTER") or {}
        lf = filters.get("LOT_SIZE") or {}
        inst = CanonicalInstrument(
            requested_symbol=requested,
            symbol=symbol,
            display_name=f"{symbol} ({'Binance Futures' if mt == 'futures' else 'Binance Spot'})",
            price_increment=str(pf.get("tickSize") or ""),
            size_increment=str(lf.get("stepSize") or ""),
            minimum_size=str(lf.get("minQty") or ""),
        )
        return make_success(operation, name, mt, instrument=inst, data={"market_type": mt})
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation, name, mt, "BINANCE_ERROR", sanitize_error_message(str(exc)))


def _market_price(account: str, request: Mapping[str, Any]):
    operation = "market_price"
    mt = _market_type(account, request)
    symbol = _normalize_symbol(str(request.get("symbol") or request.get("query") or ""))
    try:
        px = _price(mt, symbol)
        return make_success(
            operation,
            name,
            mt,
            market_price=CanonicalMarketPrice(requested_symbol=symbol, market=mt, price=px, mark_price=px, last_external_price=px),
            data={"market_type": mt, "symbol": symbol, "price": px},
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation, name, mt, "BINANCE_ERROR", sanitize_error_message(str(exc)))


def execute(request: Mapping[str, Any]):
    operation = str(request.get("operation") or "").strip().lower()
    account = str(request.get("account") or "spot")
    if operation == "candles":
        # account selects spot vs futures for public klines
        return handle_candles_operation(name, account, dict(request))

    op = str(request.get("operation") or "").strip()
    account = str(request.get("account") or request.get("market_type") or "spot")
    if op == "resolve_instrument":
        return _resolve_instrument(account, request)
    if op == "market_price":
        return _market_price(account, request)
    return make_failure(op or "unknown", name, account, "NOT_IMPLEMENTED", f"Binance does not implement {op!r}.")
