"""EdgeX V2 exchange agent for the /trade wizard.

Discovers complete ``EDGEX_<ACCOUNT>_ACCOUNTID/APIKEY/APISECRET/APIPASSPHRASE``
credential groups from the live environment and ``$HERMES_HOME/.env``. The
optional ``SIGNERKEY`` is required for signed write operations
(new order, ladder, cancel, set TP, set SL, close position); read-only
menus (balance, positions, open orders) work without it.

All EdgeX write operations are executed by the official EdgeX V2
Python SDK running **in-process** inside the shared Hermes venv —
no separate venv, no subprocess, no JSON/IPC plumbing. Compatibility
was verified end-to-end with the shared-venv package set.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Tuple

from edgex_sdk import (
    Client,
    OrderSide,
    CancelOrderParams,
    CreateOrderParams,
    OrderType,
)
from edgex_sdk.quote.client import PriceType

from ..canonical import (
    CanonicalCancelGroupResult,
    CanonicalLadderResult,
    CanonicalOrderGroup,
    CanonicalOrderResult,
    CanonicalPortfolioSummary,
    CanonicalPosition,
    CanonicalPositionActionResult,
    CanonicalResponse,
    make_failure,
    make_success,
    normalize_balance,
    sanitize_error_message,
)

name = "edgex"
BASE_URL = "https://edgex-prod-v2.edgex.exchange"
_REQUIRED = ("ACCOUNTID", "APIKEY", "APISECRET", "APIPASSPHRASE")
_FIELDS = {
    "ACCOUNTID": "account_id", "APIKEY": "api_key", "APISECRET": "api_secret",
    "APIPASSPHRASE": "passphrase", "SIGNERKEY": "signer_key",
}
_ALIAS = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _env() -> Dict[str, str]:
    values: Dict[str, str] = {}
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    try:
        with open(os.path.join(home, ".env"), encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
                if m:
                    values[m.group(1).upper()] = m.group(2).strip().strip('"').strip("'")
    except OSError:
        pass
    for key, value in os.environ.items():
        values[key.upper()] = value
    return values


def list_accounts() -> List[str]:
    env = _env()
    found = set()
    for key in env:
        m = re.fullmatch(r"EDGEX_(.+)_ACCOUNTID", key)
        if not m or not _ALIAS.match(m.group(1)):
            continue
        alias = m.group(1)
        if all(env.get(f"EDGEX_{alias}_{s}", "").strip() for s in _REQUIRED):
            found.add(alias.lower())
    return sorted(found)


def _credentials(account: str) -> Optional[Dict[str, str]]:
    alias = str(account or "").strip().upper()
    if not _ALIAS.match(alias):
        return None
    env = _env()
    out: Dict[str, str] = {"account": alias.lower()}
    for suffix, field in _FIELDS.items():
        value = env.get(f"EDGEX_{alias}_{suffix}", "").strip()
        if suffix in _REQUIRED and not value:
            return None
        if value:
            out[field] = value
    if not out.get("account_id", "").isdigit():
        return None
    return out


def capabilities() -> List[str]:
    return [
        "balance", "positions_orders", "positions_management",
        "new_order", "ladder",
        "cancel_orders", "cancel_order_group",
        "set_tp", "set_sl", "close_position",
    ]


def _query(params: Mapping[str, Any]) -> str:
    return "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v is not None and str(v) != "")


def _request(creds: Mapping[str, str], path: str, params: Mapping[str, Any]) -> Any:
    """Authenticated GET against the EdgeX V2 REST API using HMAC-SHA256."""
    query = _query(params)
    timestamp = str(int(time.time() * 1000))
    signing_key = base64.b64encode(creds["api_secret"].encode())
    signature = hmac.new(signing_key, (timestamp + "GET" + path + query).encode(), hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        BASE_URL + path + ("?" + query if query else ""),
        headers={
            "X-edgeX-Api-Key": creds["api_key"],
            "X-edgeX-Passphrase": creds["passphrase"],
            "X-edgeX-Timestamp": timestamp,
            "X-edgeX-Signature": signature,
            "Accept": "application/json",
            "User-Agent": "curl/8.0",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read())
    if payload.get("code") != "SUCCESS":
        raise RuntimeError(f"EdgeX API {payload.get('code')}: {payload.get('msg') or 'request failed'}")
    return payload.get("data")


def _metadata() -> Dict[str, str]:
    """Public metadata — returns contractId -> native symbol."""
    try:
        request = urllib.request.Request(
            BASE_URL + "/api/v2/public/meta/getMetaData",
            headers={"User-Agent": "curl/8.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=20) as r:
            data = json.loads(r.read()).get("data") or {}
    except Exception:
        return {}
    return {
        str(x.get("contractId")): str(x.get("contractName") or x.get("contractId"))
        for x in data.get("contractList", [])
    }


def _metadata_full() -> Dict[str, Any]:
    request = urllib.request.Request(
        BASE_URL + "/api/v2/public/meta/getMetaData",
        headers={"User-Agent": "curl/8.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read())
    if payload.get("code") != "SUCCESS":
        raise RuntimeError(payload.get("msg") or "metadata unavailable")
    return payload.get("data") or {}


def _resolve_contract(symbol: Any) -> Optional[Tuple[str, str]]:
    """Map a wizard symbol (e.g. 'SOL', 'SOLUSDC', 'BTC-USDC') to (contractId, native)."""
    requested = str(symbol or "").strip().upper().replace("/", "").replace("-", "")
    if not requested:
        return None
    metadata = _metadata()
    exact = [(cid, native) for cid, native in metadata.items() if native.upper() == requested]
    if exact:
        return exact[0]
    candidates = [(cid, native) for cid, native in metadata.items()
                  if native.upper() in {requested + "USDC", requested + "USDT"}]
    return candidates[0] if len(candidates) == 1 else None


def _contract_rules(contract_id: str) -> Tuple[Decimal, Decimal, Decimal]:
    for row in _metadata_full().get("contractList", []):
        if str(row.get("contractId")) == str(contract_id):
            return (
                Decimal(str(row.get("tickSize") or "0.01")),
                Decimal(str(row.get("stepSize") or "0.1")),
                Decimal(str(row.get("minOrderSize") or row.get("stepSize") or "0.1")),
            )
    return Decimal("0.01"), Decimal("0.1"), Decimal("0.1")


def _money(value: Any) -> str:
    try:
        return normalize_balance(str(value or "0"), "USDT").value
    except Exception:
        return "0.00"


def _balance(account: str) -> CanonicalResponse:
    creds = _credentials(account)
    if not creds:
        return make_failure(
            operation="balance", exchange=name, account=account, code="ACCOUNT_NOT_FOUND",
            message="Set EDGEX_<account>_ACCOUNTID, APIKEY, APISECRET and APIPASSPHRASE.",
        )
    try:
        data = _request(creds, "/api/v2/private/account/getAccountAsset",
                        {"accountId": creds["account_id"]}) or {}
        assets = data.get("collateralAssetModelList") or []
        asset = assets[0] if assets else {}
        summary = CanonicalPortfolioSummary(
            account_value=_money(asset.get("totalEquity")),
            withdrawable=_money(asset.get("availableAmount")),
            margin_used=_money(asset.get("initialMarginRequirement")),
            total_position_value=_money(asset.get("totalPositionValueAbs")),
            unit="USDT",
        )
        return make_success(
            operation="balance", exchange=name, account=creds["account"],
            balance=normalize_balance(summary.account_value, "USDT"),
            portfolio_summary=summary,
        )
    except Exception as exc:
        return make_failure(
            operation="balance", exchange=name, account=creds["account"],
            code="BALANCE_UNAVAILABLE", message=sanitize_error_message(str(exc)),
        )


def _active_orders(creds: Mapping[str, str]) -> List[Dict[str, Any]]:
    data = _request(creds, "/api/v2/private/order/getActiveOrderPage",
                   {"accountId": creds["account_id"], "size": "200"}) or {}
    return [x for x in (data.get("dataList") or []) if isinstance(x, dict)]


def _trigger_time_in_force() -> str:
    """EdgeX trigger orders require GOOD_TIL_CANCEL."""
    return "GOOD_TIL_CANCEL"


def _trigger_order_price(trigger_price: str) -> str:
    """Use the trigger price as the SDK order price so L2 value is non-zero."""
    return str(trigger_price)


def _protection_order_ids(rows: List[Dict[str, Any]], contract_id: str, operation: str) -> List[str]:
    wanted = "TAKE_PROFIT" if operation == "set_tp" else "STOP_"
    return [
        str(row.get("id"))
        for row in rows
        if str(row.get("contractId")) == str(contract_id)
        and row.get("isPositionTpsl")
        and str(row.get("type") or "").upper().startswith(wanted)
    ]


def _protection_prices(rows: List[Dict[str, Any]], contract_id: str) -> Tuple[Optional[str], Optional[str]]:
    tp = sl = None
    for row in rows:
        if str(row.get("contractId")) != str(contract_id) or not row.get("isPositionTpsl"):
            continue
        kind = str(row.get("type") or "").upper()
        price = str(row.get("triggerPrice") or "")
        if kind.startswith("TAKE_PROFIT") and price:
            tp = price
        elif kind.startswith("STOP_") and price:
            sl = price
    return tp, sl


def _positions_orders(account: str) -> CanonicalResponse:
    """Return positions, open orders, and live TP/SL protection orders."""
    creds = _credentials(account)
    if not creds:
        return make_failure(
            operation="positions_orders", exchange=name, account=account,
            code="ACCOUNT_NOT_FOUND", message="EdgeX account is not configured.",
        )
    try:
        aid = creds["account_id"]
        data = _request(creds, "/api/v2/private/account/getAccountAsset", {"accountId": aid}) or {}
        orders_data = _request(creds, "/api/v2/private/order/getActiveOrderPage",
                              {"accountId": aid, "size": "200"}) or {}
        symbols = _metadata()
        positions: List[CanonicalPosition] = []
        asset_by_contract = {str(x.get("contractId")): x for x in (data.get("positionAssetList") or [])}
        contract_ids = {str(x.get("contractId")) for x in (data.get("positionList") or [])}
        protection_by_contract = {cid: _protection_prices(orders_data.get("dataList") or [], cid) for cid in contract_ids}
        for row in data.get("positionList", []) or []:
            size = Decimal(str(row.get("openSize") or "0"))
            if size == 0:
                continue
            cid = str(row.get("contractId"))
            detail = asset_by_contract.get(cid, {})
            tp, sl = protection_by_contract.get(cid, (None, None))
            positions.append(CanonicalPosition(
                symbol=symbols.get(cid, cid),
                side="long" if size > 0 else "short",
                size=str(abs(size)),
                entry_price=str(detail.get("avgEntryPrice") or "0"),
                pnl=str(detail.get("unrealizePnl") or "0"),
                tp=tp, sl=sl,
            ))
        groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for row in orders_data.get("dataList", []) or []:
            cid = str(row.get("contractId"))
            symbol = symbols.get(cid, cid)
            side = str(row.get("side") or "").lower()
            groups.setdefault((symbol, side), []).append(row)
        order_groups: List[CanonicalOrderGroup] = []
        for (symbol, side), rows in groups.items():
            total = sum((Decimal(str(x.get("size") or "0")) for x in rows), Decimal(0))
            weighted = sum(
                (Decimal(str(x.get("size") or "0")) * Decimal(str(x.get("price") or "0")) for x in rows),
                Decimal(0),
            )
            prices = [Decimal(str(x.get("price") or "0")) for x in rows]
            order_groups.append(CanonicalOrderGroup(
                symbol=symbol, side=side, order_count=len(rows), total_size=str(total),
                vwap=str(weighted / total if total else 0),
                min_price=str(min(prices)), max_price=str(max(prices)),
            ))
        return make_success(
            operation="positions_orders", exchange=name, account=creds["account"],
            positions=positions, order_groups=order_groups,
        )
    except Exception as exc:
        return make_failure(
            operation="positions_orders", exchange=name, account=creds["account"],
            code="POSITIONS_ORDERS_UNAVAILABLE", message=sanitize_error_message(str(exc)),
        )


# ---------------------------------------------------------------------------
# SDK-backed write operations (in-process; no subprocess, no dedicated EdgeX venv)
# ---------------------------------------------------------------------------

def _run_async(coro: Any) -> Any:
    """Run an SDK coroutine without nesting an event loop in the gateway thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=_runner, name="edgex-sdk-async", daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _build_client(creds: Mapping[str, str]) -> Client:
    return Client(
        base_url=BASE_URL,
        account_id=int(creds["account_id"]),
        api_key=creds["api_key"],
        api_passphrase=creds["passphrase"],
        api_secret=creds["api_secret"],
        trading_private_key=creds.get("signer_key") or "",
    )


def _create_limit_order(creds: Mapping[str, str], contract_id: str, size: str, price: str, side: str) -> Dict[str, Any]:
    client = _build_client(creds)
    try:
        return _run_async(client.create_limit_order(
            contract_id=contract_id,
            size=size,
            price=price,
            side=OrderSide.SELL if side == "sell" else OrderSide.BUY,
        ))
    finally:
        _run_async(client.close())


def _create_market_order(creds: Mapping[str, str], contract_id: str, size: str, side: str) -> Dict[str, Any]:
    client = _build_client(creds)
    try:
        return _run_async(client.create_market_order(
            contract_id=contract_id,
            size=size,
            side=OrderSide.SELL if side == "sell" else OrderSide.BUY,
            reduce_only=True,
        ))
    finally:
        _run_async(client.close())


def _cancel_order_by_id(creds: Mapping[str, str], order_id: str) -> Dict[str, Any]:
    client = _build_client(creds)
    try:
        return _run_async(client.cancel_order(CancelOrderParams(order_id=str(order_id))))
    finally:
        _run_async(client.close())


def _create_trigger_order(creds: Mapping[str, str], contract_id: str, size: str, side: str,
                          price: str, kind: str) -> Dict[str, Any]:
    """kind is 'tp' (Take-Profit) or 'sl' (Stop-Loss)."""
    client = _build_client(creds)
    try:
        order_type = OrderType.TAKE_PROFIT_MARKET if kind == "tp" else OrderType.STOP_MARKET
        return _run_async(client.create_order(CreateOrderParams(
            contract_id=contract_id,
            price=_trigger_order_price(price),
            size=size,
            type=order_type,
            side=OrderSide.SELL if side == "sell" else OrderSide.BUY,
            time_in_force=_trigger_time_in_force(),
            reduce_only=True,
            trigger_price=price,
            trigger_price_type=PriceType.ORACLE_PRICE,
            is_position_tpsl=True,
        )))
    finally:
        _run_async(client.close())


def _new_order(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "")
    creds = _credentials(account)
    if not creds or not creds.get("signer_key"):
        return make_failure(
            operation="new_order", exchange=name, account=account, code="ACCOUNT_NOT_FOUND",
            message="A complete EdgeX account including EDGEX_<account>_SIGNERKEY is required.",
        )
    symbol = str(request.get("symbol") or "").upper().replace("/", "").replace("-", "")
    side = str(request.get("side") or "").lower()
    size = str(request.get("volume") or request.get("size") or "")
    price = str(request.get("price") or "")
    if side not in {"buy", "sell"} or not symbol or not size or not price:
        return make_failure(
            operation="new_order", exchange=name, account=creds["account"],
            code="INVALID_REQUEST",
            message="Symbol, buy/sell side, volume and price are required.",
        )
    try:
        resolved = _resolve_contract(symbol)
        if not resolved:
            raise RuntimeError(f"Unknown EdgeX symbol {symbol}")
        contract_id, native_symbol = resolved
        raw = _create_limit_order(creds, contract_id, size, price, side)
        if raw.get("code") not in (None, "SUCCESS"):
            raise RuntimeError(raw.get("msg") or raw.get("code") or "SDK error")
        order_id = (raw.get("data") or {}).get("orderId")
        result = CanonicalOrderResult(
            symbol=native_symbol, side=side, order_type="limit",
            requested_volume=size, requested_price=price,
            submitted_volume=size, submitted_price=price,
            verified=bool(order_id),
            exchange_order_id=int(order_id) if order_id else None,
        )
        return make_success(
            operation="new_order", exchange=name, account=creds["account"], order=result,
        )
    except Exception as exc:
        return make_failure(
            operation="new_order", exchange=name, account=creds["account"],
            code="ORDER_FAILED", message=sanitize_error_message(str(exc)),
        )


def _cancel_group(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "")
    creds = _credentials(account)
    side = str(request.get("side") or "").lower()
    resolved = _resolve_contract(request.get("symbol"))
    if not creds or not resolved or side not in {"buy", "sell"}:
        return make_failure(
            operation="cancel_order_group", exchange=name, account=account,
            code="INVALID_REQUEST",
            message="Valid account, symbol and side are required.",
        )
    cid, symbol = resolved
    try:
        targets = [o for o in _active_orders(creds)
                   if str(o.get("contractId")) == cid
                   and str(o.get("side") or "").lower() == side]
        for order in targets:
            _cancel_order_by_id(creds, str(order.get("id")))
        remaining = [o for o in _active_orders(creds)
                     if str(o.get("contractId")) == cid
                     and str(o.get("side") or "").lower() == side]
        result = CanonicalCancelGroupResult(
            symbol=symbol, side=side,
            targeted_order_count=len(targets),
            cancelled_order_count=len(targets),
            confirmed_absent_count=len(targets) - len(remaining),
            remaining_target_count=len(remaining),
            verified=not remaining,
            partial=bool(remaining),
            batch_count=len(targets),
        )
        return make_success(
            operation="cancel_order_group", exchange=name, account=creds["account"],
            cancel_group=result,
        )
    except Exception as exc:
        return make_failure(
            operation="cancel_order_group", exchange=name, account=account,
            code="CANCEL_FAILED", message=sanitize_error_message(str(exc)),
        )


def _position_context(creds: Mapping[str, str], symbol: Any) -> Tuple[str, str, Decimal]:
    resolved = _resolve_contract(symbol)
    if not resolved:
        raise RuntimeError(f"Unknown EdgeX symbol {symbol}")
    cid, native = resolved
    data = _request(creds, "/api/v2/private/account/getAccountAsset",
                   {"accountId": creds["account_id"]}) or {}
    row = next(
        (x for x in (data.get("positionList") or [])
         if str(x.get("contractId")) == cid
         and Decimal(str(x.get("openSize") or 0)) != 0),
        None,
    )
    if not row:
        raise RuntimeError(f"No open position for {native}")
    return cid, native, Decimal(str(row.get("openSize")))


def _position_action(request: Dict[str, Any]) -> CanonicalResponse:
    op = str(request.get("operation"))
    account = str(request.get("account") or "")
    creds = _credentials(account)
    if not creds or not creds.get("signer_key"):
        return make_failure(
            operation=op, exchange=name, account=account, code="ACCOUNT_NOT_FOUND",
            message="Complete EdgeX signer credentials are required.",
        )
    try:
        cid, symbol, size = _position_context(creds, request.get("symbol"))
        close_side = "sell" if size > 0 else "buy"
        amount = str(abs(size))
        price = str(request.get("price") or "0")
        if op in ("set_tp", "set_sl") and Decimal(price) <= 0:
            # price == 0 is the wizard's "remove existing protection" intent.
            targets = _protection_order_ids(_active_orders(creds), cid, op)
            if not targets:
                raise RuntimeError("No existing protection order to remove")
            for order_id in targets:
                _cancel_order_by_id(creds, order_id)
            result = CanonicalPositionActionResult(
                operation=op, symbol=symbol, verified=True, removed=True,
                current_side="long" if size > 0 else "short", current_size=amount,
            )
            return make_success(
                operation=op, exchange=name, account=creds["account"], position_action=result,
            )
        if op == "close_position":
            raw = _create_market_order(creds, cid, amount, close_side)
        else:
            raw = _create_trigger_order(creds, cid, amount, close_side, price,
                                        "tp" if op == "set_tp" else "sl")
        oid = (raw.get("data") or {}).get("orderId")
        result = CanonicalPositionActionResult(
            operation=op, symbol=symbol,
            verified=bool(oid),
            price=None if op == "close_position" else price,
            exchange_order_id=int(oid) if oid else None,
            current_side="long" if size > 0 else "short",
            current_size=amount,
        )
        return make_success(
            operation=op, exchange=name, account=creds["account"], position_action=result,
        )
    except Exception as exc:
        return make_failure(
            operation=op, exchange=name, account=account,
            code="POSITION_ACTION_FAILED", message=sanitize_error_message(str(exc)),
        )


# ---------------------------------------------------------------------------
# Ladder
# ---------------------------------------------------------------------------

def _ladder_prices(start: Decimal, end: Decimal, count: int, tick: Decimal) -> List[Decimal]:
    if count == 1:
        raw = [start]
    else:
        raw = [start + (end - start) * Decimal(i) / Decimal(count - 1) for i in range(count)]
    return [value.quantize(tick) for value in raw]


def _ladder_sizes(total: Decimal, count: int, increment: Decimal, distribution: str,
                  min_size: Decimal = Decimal("0")) -> List[Decimal]:
    key = str(distribution or "").strip().lower().replace(" ", "_")
    if key == "uniform":
        weights = [Decimal(1)] * count
    elif key == "half_gaussian":
        if count == 1:
            weights = [Decimal(1)]
        else:
            span = Decimal(count - 1)
            weights = [
                Decimal(str(math.exp(-(float(Decimal("3") * (span - Decimal(i)) / span) ** 2) / 2)))
                for i in range(count)
            ]
    else:
        raise ValueError(f"Unsupported ladder distribution: {distribution}")
    minimum_units = int((min_size / increment).to_integral_value())
    total_units = int((total / increment).to_integral_value())
    if total_units < minimum_units * count:
        raise ValueError("Total volume is too small for the exchange minimum on every ladder child")
    distributable = total_units - minimum_units * count
    raw = [Decimal(distributable) * w / sum(weights) for w in weights]
    allocated = [minimum_units + int(x) for x in raw]
    residual = total_units - sum(allocated)
    remainders = [raw[i] - int(raw[i]) for i in range(count)]
    order = sorted(range(count), key=lambda i: (remainders[i], -i), reverse=True)
    for i in order[:residual]:
        allocated[i] += 1
    return [Decimal(x) * increment for x in allocated]


def _ladder(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "")
    creds = _credentials(account)
    resolved = _resolve_contract(request.get("symbol"))
    side = str(request.get("side") or "").lower()
    try:
        count = int(request.get("order_count"))
        total = Decimal(str(request.get("total_volume")))
        start = Decimal(str(request.get("start_price")))
        end = Decimal(str(request.get("end_price")))
        if not creds or not creds.get("signer_key") or not resolved or side not in {"buy", "sell"} or count < 1:
            raise ValueError("Invalid ladder parameters")
        cid, symbol = resolved
        tick, size_increment, min_size = _contract_rules(cid)
        prices = _ladder_prices(start, end, count, tick)
        distribution = str(request.get("distribution") or "uniform")
        sizes = _ladder_sizes(total, count, size_increment, distribution, min_size)
        ids: List[int] = []
        for price, size in zip(prices, sizes):
            raw = _create_limit_order(creds, cid, format(size, "f"), format(price, "f"), side)
            oid = (raw.get("data") or {}).get("orderId")
            if oid:
                ids.append(int(oid))
        submitted = sum(sizes[: len(ids)], Decimal(0))
        result = CanonicalLadderResult(
            symbol=symbol, side=side, distribution=distribution,
            requested_order_count=count, submitted_order_count=len(ids),
            requested_volume=str(total), submitted_volume=str(submitted),
            batch_count=count, verified=len(ids) == count, partial=len(ids) != count,
            child_order_ids=ids,
        )
        return make_success(
            operation="ladder", exchange=name, account=creds["account"], ladder=result,
        )
    except Exception as exc:
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="LADDER_FAILED", message=sanitize_error_message(str(exc)),
        )


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    operation = str((request or {}).get("operation") or "")
    account = str((request or {}).get("account") or "")
    if operation == "balance":
        return _balance(account)
    if operation in ("positions_orders", "positions_management"):
        return _positions_orders(account)
    if operation == "new_order":
        return _new_order(request)
    if operation == "ladder":
        return _ladder(request)
    if operation in ("cancel_orders", "cancel_order_group"):
        return _cancel_group(request)
    if operation in ("set_tp", "set_sl", "close_position"):
        return _position_action(request)
    return make_failure(
        operation=operation, exchange=name, account=account, code="NOT_IMPLEMENTED",
        message=f"EdgeX does not implement '{operation}' yet.",
    )
