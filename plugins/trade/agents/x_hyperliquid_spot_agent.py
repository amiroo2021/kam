"""Dedicated Hyperliquid spot agent for ``/tradespot``.

Spot metadata, balances, and orders are intentionally isolated from the
perpetual agent. Credential discovery and the Hyperliquid signing client are
reused from ``x_hyperliquid_agent``; this module never routes a spot request
through perpetual market discovery.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .x_hyperliquid_agent import (
    _build_exchange_client,
    _lookup_credentials,
    _normalize_account_alias,
    _normalize_hyperliquid_order_price,
    _post_info,
)
from ..canonical import (
    CanonicalBalance,
    CanonicalCancelGroupResult,
    CanonicalInstrument,
    CanonicalMarketPrice,
    CanonicalOrderGroup,
    CanonicalOrderResult,
    CanonicalResponse,
    make_failure,
    make_success,
    normalize_balance,
    sanitize_error_message,
)

name = "hyperliquid"

_SPOT_META_CACHE: Optional[Dict[str, Any]] = None

# HyperCore token names are not always the display names used by the official
# frontend.  These are validated display aliases, not permanent market IDs;
# resolution still requires a matching live row in spotMeta.
_FRONTEND_ALIAS_BY_NATIVE = {
    "USOL": "SOL",
    "UBTC": "BTC",
    "UETH": "ETH",
    "UZEC": "ZEC",
}
_FRONTEND_ALIAS_BY_FULL_NAME = {
    "UNIT SOLANA": "SOL",
    "UNIT BITCOIN": "BTC",
    "UNIT ETHEREUM": "ETH",
    "UNIT ZCASH": "ZEC",
}


class SpotInstrumentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def list_accounts() -> List[str]:
    """Return aliases with the shared Hyperliquid credential convention."""
    from .x_hyperliquid_agent import list_accounts as _list_accounts
    return _list_accounts()


def capabilities() -> List[str]:
    return [
        "balance", "orders", "open_orders", "list_instruments", "resolve_instrument",
        "market_price", "preview_order", "new_order", "cancel_orders", "order_history",
        "plan_ladder", "ladder", "ladder_reconcile", "ladder_list_unresolved",
    ]


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _text(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _step(decimals: Any) -> Decimal:
    try:
        n = int(decimals)
    except Exception:
        return Decimal("0")
    return Decimal("1").scaleb(-n) if n >= 0 else Decimal("0")


def _floor(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def _parse_pair(value: Any) -> Tuple[str, str]:
    raw = str(value or "").strip().upper().replace("-", "/").replace(":", "/")
    if "/" not in raw:
        return "", ""
    base, quote = raw.split("/", 1)
    return base.strip(), quote.strip()


def _frontend_base(native_base: str, token: Mapping[str, Any]) -> str:
    full_name = str(token.get("fullName") or "").strip().upper()
    return _FRONTEND_ALIAS_BY_FULL_NAME.get(
        full_name, _FRONTEND_ALIAS_BY_NATIVE.get(native_base, native_base)
    )


def _native_pair(market: Mapping[str, Any]) -> str:
    return f"{market['base']}/{market['quote']}"


def _display_name(market: Mapping[str, Any]) -> str:
    return f"{market.get('display_base', market['base'])}/{market.get('display_quote', market['quote'])}"


def _parse_spot_meta(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    tokens = payload.get("tokens") if isinstance(payload, Mapping) else None
    universe = payload.get("universe") if isinstance(payload, Mapping) else None
    if not isinstance(tokens, list) or not isinstance(universe, list):
        raise SpotInstrumentError("SPOT_METADATA_INVALID", "Hyperliquid spot metadata is malformed.")
    token_by_index: Dict[int, Mapping[str, Any]] = {}
    for position, token in enumerate(tokens):
        if not isinstance(token, Mapping):
            continue
        try:
            token_by_index[int(token.get("index", position))] = token
        except (KeyError, TypeError, ValueError):
            continue
    rows: List[Dict[str, Any]] = []
    for fallback_index, item in enumerate(universe):
        if not isinstance(item, Mapping):
            continue
        raw_tokens = item.get("tokens")
        if not isinstance(raw_tokens, (list, tuple)) or len(raw_tokens) != 2:
            continue
        try:
            base_idx, quote_idx = int(raw_tokens[0]), int(raw_tokens[1])
            base_token, quote_token = token_by_index[base_idx], token_by_index[quote_idx]
        except (KeyError, TypeError, ValueError):
            continue
        if not isinstance(base_token, Mapping) or not isinstance(quote_token, Mapping):
            continue
        base, quote = str(base_token.get("name") or "").upper(), str(quote_token.get("name") or "").upper()
        if not base or not quote or base == quote:
            continue
        try:
            index = int(item.get("index", fallback_index))
        except (TypeError, ValueError):
            index = fallback_index
        sz_decimals = int(base_token.get("szDecimals", 0))
        price_decimals = max(0, 8 - sz_decimals)
        native_coin = str(item.get("name") or "").upper()
        if native_coin != "PURR/USDC":
            native_coin = f"@{index}"
        rows.append({
            "name": f"{base}/{quote}", "base": base, "quote": quote,
            "display_base": _frontend_base(base, base_token), "display_quote": quote,
            "base_token_index": base_idx, "quote_token_index": quote_idx,
            "universe_index": index, "asset_id": 10000 + index,
            "coin": native_coin, "is_canonical": bool(item.get("isCanonical")),
            "size_increment": _step(sz_decimals),
            "price_increment": _step(price_decimals),
            "minimum_size": _step(sz_decimals),
            # Hyperliquid documents MinTradeSpotNtl as 10 quote-token units.
            "minimum_notional": Decimal("10"),
            # Ladder-planner aliases. The exchange-agnostic planner reads
            # ``size_step``/``price_tick``/``min_notional``/``min_qty``
            # first and falls back to the increment names. Publishing both
            # keeps the ladder planner venue-neutral without teaching the
            # shared planner about Hyperliquid field names.
            "size_step": _step(sz_decimals),
            "price_tick": _step(price_decimals),
            "min_notional": Decimal("10"),
            "min_qty": _step(sz_decimals),
            "max_qty": Decimal("0"),
        })
    if not rows:
        raise SpotInstrumentError("SPOT_MARKETS_EMPTY", "Hyperliquid published no usable spot markets.")
    return rows


def _resolve_market(symbol: str, markets: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    requested = str(symbol or "").strip().upper()
    if requested.startswith("@"):
        try:
            index = int(requested[1:])
        except ValueError as exc:
            raise SpotInstrumentError("INSTRUMENT_NOT_FOUND", "Invalid Hyperliquid spot market index.") from exc
        matches = [dict(row) for row in markets if int(row.get("universe_index", -1)) == index]
    else:
        base, quote = _parse_pair(requested)
        if not base and requested:
            matches = [
                dict(row) for row in markets
                if f"{row.get('base', '')}{row.get('quote', '')}" == requested
                or f"{row.get('display_base', row.get('base', ''))}{row.get('display_quote', row.get('quote', ''))}" == requested
            ]
            if not matches:
                matches = [
                    dict(row) for row in markets
                    if row.get("base") == requested or row.get("display_base", row.get("base")) == requested
                ]
        else:
            matches = [
                dict(row) for row in markets
                if (row.get("base") == base and row.get("quote") == quote)
                or (row.get("display_base", row.get("base")) == base and row.get("display_quote", row.get("quote")) == quote)
            ]
        canonical = [row for row in matches if row.get("is_canonical")]
        if len(canonical) == 1:
            matches = canonical
    if not matches:
        raise SpotInstrumentError("INSTRUMENT_NOT_FOUND", "Hyperliquid spot market not found.")
    if len(matches) != 1:
        raise SpotInstrumentError("INSTRUMENT_AMBIGUOUS", "Multiple Hyperliquid spot markets match this symbol.")
    return matches[0]


def _fetch_spot_meta() -> Mapping[str, Any]:
    global _SPOT_META_CACHE
    if _SPOT_META_CACHE is None:
        payload = _post_info({"type": "spotMeta"})
        if not isinstance(payload, Mapping):
            raise RuntimeError("Hyperliquid spotMeta returned an invalid response")
        _SPOT_META_CACHE = dict(payload)
    return _SPOT_META_CACHE


def _market(account: str, symbol: str) -> Dict[str, Any]:
    return _resolve_market(symbol, _parse_spot_meta(_fetch_spot_meta()))


def _fetch_spot_price(market: Mapping[str, Any]) -> Optional[str]:
    payload = _post_info({"type": "spotMetaAndAssetCtxs"})
    if not isinstance(payload, list) or len(payload) != 2:
        return None
    meta, contexts = payload
    universe = meta.get("universe") if isinstance(meta, Mapping) else None
    if not isinstance(universe, list) or not isinstance(contexts, list):
        return None
    target = int(market["universe_index"])
    context = next(
        (item for item in contexts if isinstance(item, Mapping) and str(item.get("coin") or "") == str(market["coin"])),
        None,
    )
    if context is None:
        for position, item in enumerate(universe):
            if not isinstance(item, Mapping) or int(item.get("index", -1)) != target:
                continue
            if position >= len(contexts) or not isinstance(contexts[position], Mapping):
                return None
            context = contexts[position]
            break
    if context is not None:
        for key in ("midPx", "markPx", "oraclePx"):
            value = context.get(key)
            if value is not None and str(value).strip():
                return _text(_dec(value))
    return None


def _market_price(account: str, symbol: str) -> CanonicalResponse:
    try:
        market = _market(account, symbol)
        price = _fetch_spot_price(market)
        if not price:
            return _error("market_price", account, "PRICE_UNAVAILABLE", "Hyperliquid returned no spot price.")
        instrument = _canonical_instrument(market, symbol)
        return make_success(
            "market_price", name, account,
            market_price=CanonicalMarketPrice(
                requested_symbol=symbol, market=str(market["coin"]),
                symbol=instrument.symbol, display_name=instrument.display_name,
                native_symbol=_native_pair(market), display_symbol=instrument.display_name,
                base=instrument.base, quote=instrument.quote, market_type="spot",
                price_increment=instrument.price_increment, size_increment=instrument.size_increment,
                minimum_size=instrument.minimum_size, minimum_notional=instrument.minimum_notional,
                mark_price=price, price=price, last_updated_time=str(__import__("time").time()),
            ),
            data={"price": price, "source": "hyperliquid_spot_meta_and_ctxs"},
        )
    except SpotInstrumentError as exc:
        return _error("market_price", account, exc.code, str(exc))
    except Exception as exc:
        return _error("market_price", account, "PRICE_UNAVAILABLE", str(exc))


def _preview_order(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    side = str(request.get("side") or "").upper()
    quantity = str(request.get("quantity") or request.get("volume") or "")
    price = str(request.get("price") or request.get("limit_price") or "")
    try:
        market = _market(account, str(request.get("symbol") or ""))
        order = _build_order_request(market, side, quantity, price)
        wallet, _secret = _lookup_credentials(account)
        if not wallet:
            return _error("preview_order", account, "ACCOUNT_NOT_CONFIGURED", "Account is not configured.")
        balances = _fetch_spot_balances(wallet)
        required_coin = str(market["quote"] if side == "BUY" else market["base"])
        normalized_qty = _dec(order["sz"])
        normalized_price = _dec(order["limit_px"])
        required = normalized_qty * normalized_price if side == "BUY" else normalized_qty
        available = _available(balances, required_coin)
        if available < required:
            return _error("preview_order", account, "INSUFFICIENT_BALANCE", f"Insufficient {required_coin} balance.")
        return make_success(
            "preview_order", name, account,
            instrument=_canonical_instrument(market, str(request.get("symbol") or "")),
            data={
                "validation_status": "valid", "quantity": _text(normalized_qty),
                "limit_price": _text(normalized_price),
                "estimated_notional": _text(normalized_qty * normalized_price),
                "required_asset": required_coin, "required": _text(required),
                "available": _text(available), "minimum_notional": _text(_dec(market["minimum_notional"])),
            },
        )
    except SpotInstrumentError as exc:
        return _error("preview_order", account, exc.code, str(exc))
    except Exception as exc:
        return _error("preview_order", account, "PREVIEW_FAILED", str(exc))


def _fetch_spot_balances(wallet: str) -> List[Dict[str, Any]]:
    payload = _post_info({"type": "spotClearinghouseState", "user": wallet})
    rows = payload.get("balances") if isinstance(payload, Mapping) else None
    return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _available(rows: Sequence[Mapping[str, Any]], coin: str) -> Decimal:
    for row in rows:
        if str(row.get("coin") or row.get("token") or "").upper() == coin.upper():
            return max(Decimal("0"), _dec(row.get("total")) - _dec(row.get("hold")))
    return Decimal("0")


def _build_order_request(market: Mapping[str, Any], side: str, quantity: str, price: str) -> Dict[str, Any]:
    side_u = str(side).upper()
    if side_u not in {"BUY", "SELL"}:
        raise SpotInstrumentError("INVALID_SIDE", "Side must be BUY or SELL.")
    qty = _floor(_dec(quantity), _dec(market["size_increment"]))
    minimum_size = _dec(market.get("minimum_size"))
    minimum_notional = _dec(market.get("minimum_notional"))
    if qty <= 0 or (minimum_size > 0 and qty < minimum_size):
        raise SpotInstrumentError("INVALID_ORDER_PRECISION", "Quantity and price must remain positive after normalization.")
    px = _normalize_spot_price(_dec(price), _dec(market["size_increment"]))
    if px <= 0:
        raise SpotInstrumentError("INVALID_ORDER_PRECISION", "Quantity and price must remain positive after normalization.")
    if minimum_notional > 0 and qty * px < minimum_notional:
        raise SpotInstrumentError("MINIMUM_NOTIONAL", "Order value is below the Hyperliquid spot minimum notional.")
    return {
        "coin": str(market["coin"]), "is_buy": side_u == "BUY", "sz": float(qty),
        "limit_px": float(px), "order_type": {"limit": {"tif": "Gtc"}},
        "reduce_only": False,
    }


def _normalize_spot_price(price: Decimal, size_increment: Decimal) -> Decimal:
    """Return a price Hyperliquid will accept for a spot LIMIT order.

    Hyperliquid enforces two price rules for spot (MAX_DECIMALS = 8):
    at most ``8 - szDecimals`` decimal places, and at most 5 significant
    figures. A price with too many significant figures is REJECTED by the
    venue (tick error), not rounded for us, so it must be normalized before
    it reaches the wire. ``szDecimals`` is recovered from the size
    increment, which is derived from the same ``szDecimals`` field in
    ``spotMeta``.

    The shared perp helper implements exactly this rule and already accepts
    an ``is_spot`` flag, so spot and perpetual orders use one code path.
    """
    if price <= 0:
        raise SpotInstrumentError("INVALID_ORDER_PRECISION", "Price must be positive.")
    sz_decimals = 0
    if size_increment > 0:
        exponent = size_increment.normalize().as_tuple().exponent
        sz_decimals = max(0, -int(exponent))
    try:
        normalized = _normalize_hyperliquid_order_price(price, sz_decimals, is_spot=True)
    except Exception as exc:  # noqa: BLE001 - ValueError("INVALID_PRICE") and friends
        raise SpotInstrumentError(
            "INVALID_ORDER_PRECISION",
            "Price exceeds the Hyperliquid spot price precision limit.",
        ) from exc
    if normalized <= 0:
        raise SpotInstrumentError("INVALID_ORDER_PRECISION", "Price must be positive.")
    return normalized


def _canonical_instrument(market: Mapping[str, Any], requested: str) -> CanonicalInstrument:
    display_name = _display_name(market)
    return CanonicalInstrument(
        requested_symbol=requested, symbol=display_name,
        display_name=display_name, native_symbol=_native_pair(market),
        display_symbol=display_name, base=str(market.get("display_base", market["base"])),
        quote=str(market["quote"]), market_type="spot",
        price_increment=_text(_dec(market["price_increment"])),
        size_increment=_text(_dec(market["size_increment"])),
        minimum_size=_text(_dec(market["minimum_size"])),
        minimum_notional=_text(_dec(market["minimum_notional"])),
    )


def _error(operation: str, account: str, code: str, message: str) -> CanonicalResponse:
    return make_failure(operation, name, account, code, sanitize_error_message(message))


def _balance(account: str) -> CanonicalResponse:
    wallet, _secret = _lookup_credentials(account)
    if not wallet:
        return _error("balance", account, "ACCOUNT_NOT_CONFIGURED", "Account is not configured.")
    try:
        rows = _fetch_spot_balances(wallet)
        assets = []
        for row in rows:
            coin = str(row.get("coin") or row.get("token") or "").upper()
            total, hold = _dec(row.get("total")), _dec(row.get("hold"))
            if coin and total > 0:
                asset = {"asset": coin, "total": _text(total), "free": _text(total - hold), "locked": _text(hold)}
                # Some venues report balances under the native token name
                # while the instrument is presented under a display alias
                # (e.g. native USOL shown as SOL). Expose the alias so a
                # caller can match a display asset to its native balance
                # without hard-coding venue-specific names.
                display_asset = _FRONTEND_ALIAS_BY_NATIVE.get(coin)
                if display_asset:
                    asset["display_asset"] = display_asset
                assets.append(asset)
        usdc = next((_dec(row["total"]) for row in assets if row["asset"] == "USDC"), Decimal("0"))
        return make_success("balance", name, account, balance=normalize_balance(usdc, "USDC"), data={"assets": assets, "source": "hyperliquid_spot"})
    except Exception as exc:
        return _error("balance", account, "SPOT_BALANCE_FAILED", str(exc))


def _spot_native_coins() -> set[str]:
    return set(_spot_markets_by_coin())


def _spot_markets_by_coin() -> Dict[str, Dict[str, Any]]:
    return {str(row["coin"]): row for row in _parse_spot_meta(_fetch_spot_meta())}


def _normalize_order(row: Mapping[str, Any], market: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    side = "BUY" if str(row.get("side") or "").upper() in {"B", "BUY"} else "SELL"
    remaining = _dec(row.get("sz"))
    original = _dec(row.get("origSz")) if row.get("origSz") is not None else remaining
    executed = max(Decimal("0"), original - remaining)
    native_coin = str(row.get("coin") or "")
    display_symbol = _display_name(market) if market is not None else native_coin
    base_asset = str(market.get("display_base") or market.get("base") or "") if market is not None else ""
    quote_asset = str(market.get("display_quote") or market.get("quote") or "") if market is not None else ""
    return {
        "symbol": display_symbol, "pair": display_symbol, "display_symbol": display_symbol,
        "base_asset": base_asset, "quote_asset": quote_asset, "native_coin": native_coin,
        "quantity_unit": base_asset, "price_unit": quote_asset, "side": side,
        "type": "LIMIT", "price": _text(_dec(row.get("limitPx") or row.get("px"))),
        "orig_qty": _text(original),
        "executed_qty": _text(executed),
        "remaining_qty": _text(remaining),
        "status": str(row.get("status") or "NEW"), "order_id": str(row.get("oid") or ""),
    }


def _fetch_orders(wallet: str, history: bool = False) -> List[Dict[str, Any]]:
    kind = "historicalOrders" if history else "openOrders"
    payload = _post_info({"type": kind, "user": wallet})
    raw_rows = payload if isinstance(payload, list) else payload.get("orders", []) if isinstance(payload, Mapping) else []
    markets_by_coin = _spot_markets_by_coin()
    rows: List[Dict[str, Any]] = []
    if history:
        # Hyperliquid returns one record per status transition, so a single
        # order can appear several times with conflicting statuses (the
        # original "open" placement record plus a later "canceled"/"filled"
        # transition). Keep only the newest record per oid, otherwise the
        # same order is reported twice with contradictory state.
        latest: Dict[str, Dict[str, Any]] = {}
        for raw in raw_rows:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw.get("order") or raw)
            if raw.get("status") is not None:
                row["status"] = raw.get("status")
            oid = str(row.get("oid") or "")
            if not oid:
                continue
            timestamp = raw.get("statusTimestamp")
            try:
                ts = int(timestamp)
            except (TypeError, ValueError):
                ts = 0
            previous = latest.get(oid)
            if previous is None or ts >= int(previous.get("_status_ts", -1)):
                row["_status_ts"] = ts
                latest[oid] = row
        candidates = list(latest.values())
    else:
        candidates = [dict(raw) for raw in raw_rows if isinstance(raw, Mapping)]
    for row in candidates:
        row.pop("_status_ts", None)
        market = markets_by_coin.get(str(row.get("coin") or ""))
        if market is not None:
            rows.append(_normalize_order(row, market))
    return rows


def _orders(account: str, history: bool = False, operation: str = "") -> CanonicalResponse:
    wallet, _secret = _lookup_credentials(account)
    operation = operation or ("order_history" if history else "orders")
    if not wallet:
        return _error(operation, account, "ACCOUNT_NOT_CONFIGURED", "Account is not configured.")
    try:
        rows = _fetch_orders(wallet, history)
        groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault((row["display_symbol"], row["side"]), []).append(row)
        order_groups = []
        for (symbol, side), items in groups.items():
            qty = sum((_dec(item["remaining_qty"]) for item in items), Decimal("0"))
            notional = sum((_dec(item["price"]) * _dec(item["remaining_qty"]) for item in items), Decimal("0"))
            order_groups.append(CanonicalOrderGroup(symbol=symbol, side=side, order_count=len(items), total_size=_text(qty), vwap=_text(notional / qty) if qty else "", min_price="", max_price="", display_type="LIMIT"))
        return make_success(operation, name, account, open_order_count=len(rows) if not history else None, order_groups=order_groups if not history else None, data={"orders": rows, "source": "hyperliquid_spot"})
    except Exception as exc:
        return _error(operation, account, "SPOT_ORDERS_FAILED", str(exc))


def _new_order(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    wallet, _secret = _lookup_credentials(account)
    if not wallet:
        return _error("new_order", account, "ACCOUNT_NOT_CONFIGURED", "Account is not configured.")
    side = str(request.get("side") or "").upper()
    quantity, price = str(request.get("quantity") or request.get("volume") or ""), str(request.get("price") or "")
    try:
        market = _market(account, str(request.get("symbol") or ""))
        order = _build_order_request(market, side, quantity, price)
        balances = _fetch_spot_balances(wallet)
        required_coin = str(market["quote"] if side == "BUY" else market["base"])
        required = _dec(order["sz"]) * _dec(order["limit_px"]) if side == "BUY" else _dec(order["sz"])
        if _available(balances, required_coin) < required:
            return _error("new_order", account, "INSUFFICIENT_BALANCE", f"Insufficient {required_coin} balance.")
        client, _wallet, _secret = _build_exchange_client(account)
        if client is None:
            return _error("new_order", account, "ACCOUNT_NOT_CONFIGURED", "Account signing client is unavailable.")
        response = client.bulk_orders([order])
        statuses = (((response or {}).get("response") or {}).get("data") or {}).get("statuses", [])
        status = statuses[0] if statuses else {}
        if not isinstance(status, Mapping) or not (status.get("resting") or status.get("filled")):
            return _error("new_order", account, "ORDER_REJECTED", "Hyperliquid rejected the spot order.")
        state = status.get("resting") or status.get("filled") or {}
        oid = state.get("oid")
        submitted_qty, submitted_px = _text(_dec(order["sz"])), _text(_dec(order["limit_px"]))
        return make_success("new_order", name, account, order=CanonicalOrderResult(symbol=str(market["name"]), side=side, order_type="LIMIT", requested_volume=quantity, requested_price=price, submitted_volume=submitted_qty, submitted_price=submitted_px, verified=True, status="filled" if status.get("filled") else "resting", exchange_order_id=oid), data={"asset_id": market["asset_id"], "coin": market["coin"]})
    except SpotInstrumentError as exc:
        return _error("new_order", account, exc.code, str(exc))
    except Exception as exc:
        return _error("new_order", account, "ORDER_SUBMISSION_FAILED", str(exc))


def _cancel_orders(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    wallet, _secret = _lookup_credentials(account)
    if not wallet:
        return _error("cancel_orders", account, "ACCOUNT_NOT_CONFIGURED", "Account is not configured.")
    try:
        market = _market(account, str(request.get("symbol") or ""))
        wanted = {int(value) for value in request.get("order_ids", [])}
        orders = [
            row for row in _fetch_orders(wallet)
            if (not wanted or int(row["order_id"]) in wanted)
            and str(row.get("native_coin") or row.get("symbol")) == market["coin"]
            and row["side"].lower() == str(request.get("side") or "").lower()
        ]
        if not orders:
            return _error("cancel_orders", account, "NO_TARGET_ORDERS", "No matching spot orders were found.")
        client, _wallet, _secret = _build_exchange_client(account)
        if client is None:
            return _error("cancel_orders", account, "ACCOUNT_NOT_CONFIGURED", "Account signing client is unavailable.")
        requests = [{"coin": market["coin"], "oid": int(row["order_id"])} for row in orders]
        response = client.bulk_cancel(requests)
        statuses = (((response or {}).get("response") or {}).get("data") or {}).get("statuses", [])
        cancelled = sum(1 for item in statuses if item == "success")
        verified = len(statuses) == len(requests) and cancelled == len(requests)
        result = CanonicalCancelGroupResult(symbol=_display_name(market), side=str(request.get("side") or "").lower(), targeted_order_count=len(requests), cancelled_order_count=cancelled, confirmed_absent_count=cancelled, remaining_target_count=len(requests) - cancelled, verified=verified, partial=not verified, status="success" if verified else "partial")
        return make_success("cancel_orders", name, account, cancel_group=result, data={"cancelled": cancelled, "remaining": len(requests) - cancelled}) if verified else make_failure("cancel_orders", name, account, "CANCEL_STATUS_UNKNOWN", "Cancellation could not be fully verified.", cancel_group=result, data={"cancelled": cancelled, "remaining": len(requests) - cancelled})
    except SpotInstrumentError as exc:
        return _error("cancel_orders", account, exc.code, str(exc))
    except Exception as exc:
        return _error("cancel_orders", account, "CANCEL_STATUS_UNKNOWN", str(exc))


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    if not isinstance(request, dict):
        return _error("", "", "INVALID_REQUEST", "Request must be a dict.")
    operation, account = str(request.get("operation") or ""), str(request.get("account") or "").strip().upper()
    if not operation:
        return _error("", account, "INVALID_REQUEST", "Missing operation.")
    if not account:
        return _error(operation, account, "MISSING_ACCOUNT", "Account alias is required.")
    if operation == "balance":
        return _balance(account)
    if operation in {"orders", "open_orders"}:
        return _orders(account, False, operation)
    if operation == "order_history":
        return _orders(account, True, operation)
    if operation == "list_instruments":
        try:
            rows = _parse_spot_meta(_fetch_spot_meta())
            query = str(request.get("query") or "").upper()
            items = [
                dict(row) for row in rows
                if not query
                or query in row["name"]
                or query in row["base"]
                or query in f"{row['base']}{row['quote']}"
                or query in _display_name(row)
                or query in f"{row.get('display_base', row['base'])}{row.get('display_quote', row['quote'])}"
            ]
            instruments = []
            for row in items:
                instruments.append({
                    **row,
                    "symbol": f"{row.get('display_base', row['base'])}{row.get('display_quote', row['quote'])}",
                    "baseAsset": row.get("display_base", row["base"]), "quoteAsset": row["quote"],
                    "display_name": _display_name(row), "native_symbol": row["name"],
                    "native_coin": row["coin"], "status": "TRADING",
                    "isSpotTradingAllowed": True, "api_eligible": True,
                    "size_increment": _text(row["size_increment"]),
                    "price_increment": _text(row["price_increment"]),
                    "minimum_size": _text(row["minimum_size"]),
                    # Ladder-planner aliases (see _parse_spot_meta).
                    "size_step": _text(row["size_step"]),
                    "price_tick": _text(row["price_tick"]),
                    "min_notional": _text(row["min_notional"]),
                    "min_qty": _text(row["min_qty"]),
                    "max_qty": _text(row["max_qty"]),
                    "minimum_notional": _text(row["minimum_notional"]),
                })
            return make_success(operation, name, account, data={"instruments": instruments, "count": len(instruments)})
        except Exception as exc:
            return _error(operation, account, "SPOT_MARKETS_FAILED", str(exc))
    if operation == "resolve_instrument":
        try:
            requested = str(request.get("symbol") or "")
            market = _market(account, requested)
            instrument = _canonical_instrument(market, requested)
            return make_success(operation, name, account, instrument=instrument, data={
                "asset_id": market["asset_id"], "coin": market["coin"],
                "base": market["base"], "quote": market["quote"],
                "native_symbol": _native_pair(market), "display_symbol": instrument.display_symbol,
                "base_token": market["base"], "quote_token": market["quote"],
                # Ladder-planner aliases (see _parse_spot_meta). The wizard
                # re-resolves the instrument at confirmation time and
                # requires ``size_step`` to be present before it will
                # submit anything.
                "size_step": _text(market["size_step"]),
                "price_tick": _text(market["price_tick"]),
                "min_notional": _text(market["min_notional"]),
                "min_qty": _text(market["min_qty"]),
                "max_qty": _text(market["max_qty"]),
                "size_increment": _text(market["size_increment"]),
                "price_increment": _text(market["price_increment"]),
                "minimum_size": _text(market["minimum_size"]),
                "minimum_notional": _text(market["minimum_notional"]),
            })
        except SpotInstrumentError as exc:
            return _error(operation, account, exc.code, str(exc))
        except Exception as exc:
            return _error(operation, account, "SPOT_MARKETS_FAILED", str(exc))
    if operation == "new_order":
        return _new_order(account, request)
    if operation == "market_price":
        return _market_price(account, str(request.get("symbol") or ""))
    if operation == "preview_order":
        return _preview_order(account, request)
    if operation == "cancel_orders":
        return _cancel_orders(account, request)
    if operation in {"plan_ladder", "ladder", "ladder_reconcile", "ladder_list_unresolved"}:
        # The ladder implementation lives in a sibling module so the
        # per-exchange ladder rules stay separable from the core spot
        # surface. Import lazily to avoid a circular import at module load.
        from . import x_hyperliquid_spot_ladder as _ladder_module

        if operation == "plan_ladder":
            return _ladder_module.plan_ladder(request)
        if operation == "ladder":
            return _ladder_module._ladder(account, request)
        if operation == "ladder_reconcile":
            return _ladder_module._execute_reconcile_batch(account, request)
        return _ladder_module._execute_list_unresolved(account)
    return _error(operation, account, "NOT_IMPLEMENTED", "Unsupported Hyperliquid spot operation.")


__all__ = ["name", "list_accounts", "capabilities", "execute", "SpotInstrumentError", "_parse_spot_meta", "_resolve_market", "_build_order_request"]
