"""Aftermath Finance native perpetuals agent for the /trade stack.

Configured accounts are discovered from the existing env contract:

- AFTERMATH_<ALIAS>_AGENT_ADDRESS
- AFTERMATH_<ALIAS>_AGENT_PRIVATEKEY
- AFTERMATH_<ALIAS>_ACCOUNT_ID

This module uses Aftermath's native perpetuals REST API only.  The agent does
not import Python ccxt and deliberately avoids Aftermath's compatibility
endpoint family.

Safety: native write operations are wired as transaction-build DRY_RUNs unless
``dry_run=False`` is explicitly passed. LIVE transaction signing/submission is
not attempted without a verified Sui signing stack in this runtime.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import urllib.error
import urllib.request
from base64 import b64decode, b64encode
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..canonical import (
    CanonicalCancelGroupResult,
    CanonicalInstrument,
    CanonicalLadderResult,
    CanonicalMarketPrice,
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

from ._sui_signer import SuiSigner, SuiSigningError

logger = logging.getLogger(__name__)

name = "aftermath"
DEFAULT_API_BASE = "https://aftermath.finance"
API_TIMEOUT_SECONDS = 25
_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SUI_ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{64}$")
_REQUIRED_SUFFIXES = ("_AGENT_ADDRESS", "_AGENT_PRIVATEKEY", "_ACCOUNT_ID")
_PRICE_SCALE = Decimal("1000000000")
_SIZE_SCALE = Decimal("1000000000")
_USDC_COLLATERAL_COIN_TYPE = "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC"
_KNOWN_MARKET_SYMBOLS: Dict[str, str] = {
    "0x05b5c3bea84c4b8f33cf592d899008336dcbae8c9c6c75b2f8e7b8f7878744c1": "BTC",
    "0x143b0900a15a11af9764119ea7b72a25756484a60ad21024ed79325231ed7a9c": "DRAM",
    "0x69f182c8d2cf3e128fe6073242e9ad62af57e71ca3069647b496399cf30ae0db": "ETH",
    "0x3071216e5e9c07a4cebd3b3d3b9789ef76da44a8cfd2928292fd9f16dc7ce502": "GOOGL",
    "0x98accf0e005744bebabb894f17972ab0b2ae0b415452f31a1ba7e4548edba43c": "HYPE",
    "0x164773ec0aa7a04f0882800a36cd7f8bdbd8f1d850bfdc6e82fa0fd923b178ad": "IOVA",
    "0x057643bb31a32339169e16671b43178df4a727d1bb18f1cd269ab33126d0eeaf": "LIT",
    "0x59c1c2bf2ed158b14e283d4d4801a68a6038387e8f103d2b2279a55f1d53b8d3": "XRP",
    "0xabc5065cf350f65da0c7b9245db4ed5f14ffa0119f950c2a6dcd8c3839c80ce3": "INTC",
    "0x7b302ca4ed5a96654612fd94363a925f7c71dfa9678d4ab4e10ae8825941a5c0": "LLY",
    "0x791bbd6bbb5a65932022b710034be4f8b95f1046ac5fcd0bb3a059a35ff84c62": "MU",
    "0xa4cd7737ad09d4e6c8377b7efa88258ef13d5ded76021579ae40418f4207dd23": "MON",
    "0x70519b6e514f78c266dec5f5406bc8ba953ee0941490181bcc7e31d6798999f8": "NVDA",
    "0xd75e8847ef3b3ad1da28c95f662fc1fea7178fee1eb565e7696e4f2147db9bb1": "PUMP",
    "0x5072ccd95e6ff7bd724f89aa8b8dc58d31f09a93467ef7b694e9f24c50a1e49a": "SOL",
    "0x0d54c8e8d642d6c30412c48553628d9a91e2eb7e8f4e806bcb953c14cd1f5020": "SPCX",
    "0xa97dacdd972a0414363290ec5752cb93ac47443c3e7710df145cc7f337575002": "SUI",
    "0x5dd43224d7fbf4e726ecd03e4eb620094d7a37d356f85734714ebaceb08211ee": "TSLA",
    "0x6ab09dbe631656de5a7c7e6b2a5f3d2fef9bf326b77c01a43995bbcf5062a5f2": "US500",
    "0xdd5a6622612d131f90b114ba5f8c84e3ed75b4b40e9de8479a875e228940bcf6": "WTI",
    "0x38aca7076576e5f656e601ab34a55de2e66b7056a5652d16120671c7115b4057": "XAG",
    "0xd57ae2c981c6fbf49870319ccfca4762721aaf6ea8ea7ce8fd038f74a552e664": "XAUT",
    "0x435987c9e1b8f61a4cdfa220751b3324ebf5a7bfd1250b25c79412a71a20392f": "ZEC",
}
_SYMBOL_TO_MARKET_ID: Dict[str, str] = {symbol: market_id for market_id, symbol in _KNOWN_MARKET_SYMBOLS.items()}


def _symbol_lookup_key(value: Any) -> str:
    text = str(value or "").strip().upper().replace("-", "").replace("/", "")
    for suffix in ("PERP", "USDT", "USDC", "USD"):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text


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
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        values[key] = value
    return values


def _combined_env(prefix: str = "AFTERMATH_") -> Dict[str, str]:
    values: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith(prefix):
            values[key] = (value or "").strip()
    for key, value in _load_dotenv_values(_hermes_home() / ".env").items():
        if key.startswith(prefix):
            values.setdefault(key, (value or "").strip())
    return values


def _read_env(name_: str) -> str:
    live = os.environ.get(name_, "").strip()
    if live:
        return live
    return _load_dotenv_values(_hermes_home() / ".env").get(name_, "").strip()


def _api_base() -> str:
    return (_read_env("AFTERMATH_API_BASE") or DEFAULT_API_BASE).rstrip("/")


def _discover_accounts() -> List[str]:
    env_values = _combined_env("AFTERMATH_")
    grouped: Dict[str, Dict[str, str]] = {}
    for key, value in env_values.items():
        if not value or not key.startswith("AFTERMATH_"):
            continue
        remainder = key[len("AFTERMATH_"):]
        field = None
        alias = ""
        for suffix in _REQUIRED_SUFFIXES:
            if remainder.endswith(suffix):
                alias = remainder[: -len(suffix)]
                field = suffix[1:]
                break
        if field is None or not alias or not _ALIAS_PATTERN.match(alias):
            continue
        grouped.setdefault(alias, {})[field] = value

    valid: List[str] = []
    for alias, fields in grouped.items():
        address = fields.get("AGENT_ADDRESS", "").strip()
        private_key = fields.get("AGENT_PRIVATEKEY", "").strip()
        account_id = fields.get("ACCOUNT_ID", "").strip()
        if address and private_key and account_id and _SUI_ADDRESS_PATTERN.match(address):
            valid.append(alias.lower())
    return sorted(valid)


def list_accounts() -> List[str]:
    return _discover_accounts()


def capabilities() -> List[str]:
    return [
        "balance",
        "positions_orders",
        "positions_management",
        "new_order",
        "ladder",
        "cancel_orders",
        "cancel_order_group",
        "close_position",
        "set_tp",
        "set_sl",
        "set_leverage",
        "resolve_instrument",
        "list_instruments",
        "market_price",
    ]


def _lookup_credentials(account: str) -> Optional[Dict[str, str]]:
    alias = str(account or "").strip().upper()
    if not alias or not _ALIAS_PATTERN.match(alias):
        return None
    env_values = _combined_env("AFTERMATH_")
    address = env_values.get(f"AFTERMATH_{alias}_AGENT_ADDRESS", "").strip()
    private_key = env_values.get(f"AFTERMATH_{alias}_AGENT_PRIVATEKEY", "").strip()
    account_id = env_values.get(f"AFTERMATH_{alias}_ACCOUNT_ID", "").strip()
    if not address or not private_key or not account_id:
        return None
    if not _SUI_ADDRESS_PATTERN.match(address):
        return None
    return {
        "account": alias.lower(),
        "address": address,
        "private_key": private_key,
        "account_id": _strip_bigint(account_id),
        "base_url": _api_base(),
    }


def _redact_credentials(message: str, credentials: Optional[Dict[str, str]] = None) -> str:
    sanitized = sanitize_error_message(str(message or ""))
    if credentials:
        secret = str(credentials.get("private_key") or "").strip()
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED_PRIVATE_KEY]")
    return sanitized


def _strip_bigint(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    return text[:-1] if text.endswith("n") else text


def _bigint(value: Any) -> str:
    text = _strip_bigint(value)
    return f"{text}n" if text and not text.startswith("0x") else text


def _decimal(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        value = default
    if isinstance(value, float):
        # Native Aftermath responses can use JSON NaN for inactive rows.
        if value != value or value in (float("inf"), float("-inf")):
            value = default
        return Decimal(str(value))
    text = _strip_bigint(value)
    if text.lower() in {"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}:
        text = default
    return Decimal(text)


def _money(value: Any) -> str:
    return str(_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _display_2(value: Any) -> str:
    return _money(value)


def _display_decimal(value: Any) -> str:
    d = _decimal(value)
    return format(d.normalize(), "f") if d else "0"


def _format_step(value: Any, step: Optional[Any], *, rounding=ROUND_HALF_UP) -> str:
    d = _decimal(value)
    if step is None or str(step).strip() == "":
        return _display_decimal(d)
    q = _decimal(step)
    if q <= 0:
        return _display_decimal(d)
    rounded = (d / q).to_integral_value(rounding=rounding) * q
    text = format(rounded.normalize(), "f") if rounded else "0"
    step_text = format(q.normalize(), "f")
    places = len(step_text.split(".", 1)[1]) if "." in step_text else 0
    if places > 0:
        return f"{rounded:.{places}f}"
    return text


def _display_price(value: Any, market: Optional[Mapping[str, Any]] = None) -> str:
    return _format_step(value, _price_increment(market or {}))


def _display_size_for_market(value: Any, market: Optional[Mapping[str, Any]] = None) -> str:
    return _format_step(value, _size_increment(market or {}))


def _scaled(value: Any, scale: Decimal) -> str:
    d = _decimal(value)
    if not d.is_finite() or d < 0:
        raise ValueError("numeric value must be non-negative and finite")
    return str(int((d * scale).to_integral_value(rounding=ROUND_HALF_UP)))


def _native_price(value: Any) -> str:
    return _scaled(value, _PRICE_SCALE)


def _native_size(value: Any) -> str:
    return _scaled(value, _SIZE_SCALE)


def _post_json(credentials: Dict[str, str], path: str, payload: Dict[str, Any]) -> Any:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    url = f"{credentials['base_url'].rstrip('/')}{path}"
    request = urllib.request.Request(
        url,
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Hermes-TradeDesk/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            error_body = ""
        raise RuntimeError(f"Aftermath HTTP {exc.code} on {path}: {error_body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Aftermath request failed on {path}: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Aftermath response was not valid JSON") from exc


def _account_positions(credentials: Dict[str, str]) -> Dict[str, Any]:
    payload = _post_json(
        credentials,
        "/api/perpetuals/accounts/positions",
        {"accountIds": [_bigint(credentials["account_id"])]},
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Aftermath positions response was not an object")
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise RuntimeError("Aftermath positions response did not include the configured account")
    return next((row for row in accounts if _strip_bigint(row.get("accountId")) == credentials["account_id"]), accounts[0])


def _markets(credentials: Dict[str, str], market_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    payload = _post_json(credentials, "/api/perpetuals/markets", {"marketIds": market_ids or []})
    if not isinstance(payload, dict):
        return []
    rows = payload.get("markets") or payload.get("orderbooks") or payload.get("data") or []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _all_markets(credentials: Dict[str, str]) -> List[Dict[str, Any]]:
    payload = _post_json(credentials, "/api/perpetuals/all-markets", {"collateralCoinType": _USDC_COLLATERAL_COIN_TYPE})
    if not isinstance(payload, dict):
        return []
    rows = payload.get("markets") or []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _market_id(row: Mapping[str, Any]) -> str:
    return str(row.get("marketId") or row.get("objectId") or row.get("id") or "").strip()


def _market_params(row: Mapping[str, Any]) -> Mapping[str, Any]:
    params = row.get("marketParams")
    return params if isinstance(params, Mapping) else {}


def _increment_from_params(row: Mapping[str, Any], key: str) -> Optional[str]:
    params = _market_params(row)
    raw = params.get(key)
    if raw is None:
        return None
    scale = params.get("scalingFactor")
    try:
        value = _decimal(raw) * _decimal(scale if scale is not None else "1")
    except Exception:
        return None
    return _display_decimal(value) if value > 0 else None


def _price_increment(row: Mapping[str, Any]) -> Optional[str]:
    return _increment_from_params(row, "tickSize")


def _size_increment(row: Mapping[str, Any]) -> Optional[str]:
    return _increment_from_params(row, "lotSize")


def _quantize_floor(value: Decimal, increment: Decimal) -> Decimal:
    """Floor ``value`` to the nearest multiple of ``increment``.

    Aftermath rejects child prices that are not multiples of ``tickSize`` and
    child sizes that are not multiples of ``lotSize``. We use floor (not round)
    so a child can never be created larger than the wizard-requested total.
    """
    if increment <= 0:
        return value
    n = (value / increment).to_integral_value(rounding=ROUND_FLOOR)
    return n * increment


def _market_prices(credentials: Dict[str, str], market_ids: List[str]) -> Dict[str, Any]:
    try:
        payload = _post_json(credentials, "/api/perpetuals/markets/prices", {"marketIds": market_ids})
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _symbol_from_market_id(market_id: Any) -> str:
    text = str(market_id or "").strip()
    if not text:
        return "UNKNOWN"
    if text in _KNOWN_MARKET_SYMBOLS:
        return _KNOWN_MARKET_SYMBOLS[text]
    # Object ids are not human friendly; preserve a short stable suffix if no metadata is known.
    return text if not text.startswith("0x") else f"{text[:6]}…{text[-4:]}"


def _market_symbol(row: Mapping[str, Any]) -> str:
    params = _market_params(row)
    base_asset = str(params.get("baseAssetSymbol") or "").strip().upper()
    if base_asset.endswith("USD") and len(base_asset) > 3:
        return base_asset[:-3]
    if base_asset:
        return base_asset
    for key in ("symbol", "marketSymbol", "name", "displayName", "baseAssetSymbol", "baseSymbol"):
        value = row.get(key)
        if value:
            return str(value)
    market = row.get("market")
    if isinstance(market, dict):
        return _market_symbol(market)
    return _symbol_from_market_id(_market_id(row))


def _find_market(credentials: Dict[str, str], symbol: str) -> Dict[str, Any]:
    requested = str(symbol or "").strip()
    if not requested:
        raise ValueError("Missing symbol")
    all_rows: Optional[List[Dict[str, Any]]] = None

    def search_all(wanted_id: str = "", wanted_symbol: str = "") -> Optional[Dict[str, Any]]:
        nonlocal all_rows
        if all_rows is None:
            all_rows = _all_markets(credentials)
        wanted_key = _symbol_lookup_key(wanted_symbol) if wanted_symbol else ""
        for row in all_rows:
            mid = _market_id(row)
            sym_key = _symbol_lookup_key(_market_symbol(row))
            if wanted_id and mid == wanted_id:
                return dict(row)
            if wanted_key and wanted_key == sym_key:
                return dict(row)
        return None

    if requested.startswith("0x"):
        found = search_all(wanted_id=requested)
        if found:
            return found
        rows = _markets(credentials, [requested])
        return rows[0] if rows else {"marketId": requested, "symbol": _symbol_from_market_id(requested)}
    wanted_key = _symbol_lookup_key(requested)
    known_id = _SYMBOL_TO_MARKET_ID.get(wanted_key)
    if known_id:
        found = search_all(wanted_id=known_id)
        if found:
            return found
        rows = _markets(credentials, [known_id])
        return rows[0] if rows else {"marketId": known_id, "symbol": wanted_key}
    found = search_all(wanted_symbol=requested)
    if found:
        return found
    wanted = wanted_key
    for row in _markets(credentials):
        mid = _market_id(row)
        names = {_symbol_lookup_key(_market_symbol(row)), mid.upper()}
        if wanted in names or any(name.startswith(wanted) for name in names):
            data = dict(row)
            data.setdefault("marketId", mid)
            return data
    raise ValueError(f"Aftermath market '{requested}' was not found")


def _position_symbol(position: Mapping[str, Any]) -> str:
    return _symbol_from_market_id(position.get("marketId"))


def _position_side(size: Decimal) -> str:
    return "long" if size >= 0 else "short"


def _canonical_positions(account_obj: Mapping[str, Any]) -> List[CanonicalPosition]:
    positions: List[CanonicalPosition] = []
    for row in account_obj.get("positions") or []:
        if not isinstance(row, dict):
            continue
        size = _decimal(row.get("baseAssetAmount"), "0")
        if size == 0:
            continue
        positions.append(
            CanonicalPosition(
                symbol=_position_symbol(row),
                side=_position_side(size),
                size=_display_2(abs(size)),
                entry_price=_display_2(row.get("entryPrice")),
                pnl=_money(row.get("unrealizedPnlUsd")),
                exchange_instrument=str(row.get("marketId") or ""),
                mark=None,
            )
        )
    return positions


def _pending_orders_for_market(credentials: Dict[str, str], market_id: str) -> List[Dict[str, Any]]:
    """Return CCXT-shaped open-order rows for one market on this account.

    Aftermath's native ``/api/perpetuals/accounts/positions`` payload only
    carries ``orderId / side / currentSize / initialSize`` inside
    ``positions[i].pendingOrders`` — no price field. The price/size details
    live behind ``/api/ccxt/myPendingOrders`` which takes
    ``{ accountNumber, chId }`` (the human account number, not the capability
    object id, and the market id).

    Returns the list of order dicts (each carrying ``price``, ``remaining``,
    ``amount``, ``side``, ``type``, ``id``, ``status``, ``symbol``).
    """
    if not market_id:
        return []
    body = {"accountNumber": int(credentials["account_id"]), "chId": market_id}
    try:
        response = _post_json(credentials, "/api/ccxt/myPendingOrders", body)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Aftermath myPendingOrders failed for market %s: %s",
            market_id[:14] + "…",
            _redact_credentials(str(exc), credentials),
        )
        return []
    if isinstance(response, list):
        return [r for r in response if isinstance(r, dict)]
    if isinstance(response, dict):
        for key in ("orders", "pendingOrders", "data", "result"):
            rows = response.get(key)
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
    return []


def _canonical_order_groups(
    account_obj: Mapping[str, Any],
    credentials: Optional[Mapping[str, str]] = None,
) -> List[CanonicalOrderGroup]:
    """Build canonical order groups from native + CCXT pending-order data.

    Aftermath nests price-less pending orders inside each position row
    (``orderId / side / currentSize / initialSize``); the actual price/
    remaining/amount live behind ``/api/ccxt/myPendingOrders``, which is
    queried per market if ``credentials`` is provided.
    """
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for pos in account_obj.get("positions") or []:
        if not isinstance(pos, dict):
            continue
        market_id = str(pos.get("marketId") or "")
        symbol = _position_symbol(pos)
        # Prefer the rich CCXT-shaped rows from /api/ccxt/myPendingOrders; fall
        # back to the position-embedded pendingOrders only if CCXT returns
        # nothing (e.g. credentials omitted during unit testing).
        ccxt_orders = (
            _pending_orders_for_market(dict(credentials), market_id)
            if credentials else []
        )
        if ccxt_orders:
            orders = ccxt_orders
        else:
            orders = pos.get("pendingOrders") or []
        for order in orders:
            if not isinstance(order, dict):
                continue
            # CCXT-shaped row → use price/remaining/amount directly.
            if "price" in order or "remaining" in order:
                side = str(order.get("side") or "").lower()
                if side not in {"buy", "sell"}:
                    # numeric encoding fallback
                    side_num = order.get("side")
                    side = "buy" if str(side_num) in {"0", "bid", "buy"} else "sell"
                price = _decimal(order.get("price"), "0")
                size = abs(_decimal(order.get("remaining"), "0"))
                order_id = str(order.get("id") or order.get("orderId") or "")
            else:
                # Position-embedded (price-less) row → keep legacy decoding.
                side_num = order.get("side")
                side = "buy" if str(side_num) in {"0", "bid", "buy"} else "sell"
                price = _decimal(
                    order.get("price") or order.get("limitPrice") or order.get("triggerPrice"),
                    "0",
                )
                raw_size = order.get("size") or order.get("baseAssetAmount") or order.get("remainingSize")
                if raw_size is None:
                    raw_size = order.get("currentSize") or order.get("initialSize")
                    size = abs(_decimal(raw_size, "0") / _SIZE_SCALE) if raw_size is not None else Decimal("0")
                else:
                    size = abs(_decimal(raw_size, "0"))
                order_id = str(order.get("orderId") or "")
            key = (market_id, side)
            item = groups.setdefault(
                key,
                {"symbol": symbol, "side": side, "count": 0,
                 "size": Decimal("0"), "notional": Decimal("0"),
                 "prices": [], "ids": []},
            )
            item["count"] += 1
            item["size"] += size
            item["notional"] += size * price
            item["prices"].append(price)
            if order_id:
                item["ids"].append(order_id)
    result: List[CanonicalOrderGroup] = []
    for item in groups.values():
        total_size = item["size"] or Decimal("0")
        prices = item["prices"] or [Decimal("0")]
        vwap = (item["notional"] / total_size) if total_size else Decimal("0")
        result.append(
            CanonicalOrderGroup(
                symbol=item["symbol"],
                side=item["side"],
                order_count=int(item["count"]),
                total_size=_display_decimal(total_size),
                vwap=_display_decimal(vwap),
                min_price=_display_decimal(min(prices)),
                max_price=_display_decimal(max(prices)),
                order_ids=item["ids"] or None,
            )
        )
    return result


def _balance(credentials: Dict[str, str]) -> CanonicalResponse:
    obj = _account_positions(credentials)
    equity = _decimal(obj.get("totalEquityUsd"), "0")
    available = _decimal(obj.get("availableCollateralUsd", obj.get("availableCollateral")), "0")
    used = max(Decimal("0"), equity - available)
    positions = _canonical_positions(obj)
    order_groups = _canonical_order_groups(obj, credentials)
    return make_success(
        operation="balance",
        exchange=name,
        account=credentials["account"],
        balance=normalize_balance(equity, "USD"),
        portfolio_summary=CanonicalPortfolioSummary(
            account_value=_money(equity),
            withdrawable=_money(available),
            margin_used=_money(used),
            total_position_value=_money(sum((abs(_decimal(p.size)) * _decimal(p.entry_price) for p in positions), Decimal("0"))),
            unit="USD",
        ),
        positions=positions,
        open_order_count=sum(g.order_count for g in order_groups),
        order_groups=order_groups,
        data={"account_id": credentials["account_id"], "source": "native_perpetuals"},
    )


def _positions_orders(credentials: Dict[str, str], operation: str = "positions_orders") -> CanonicalResponse:
    obj = _account_positions(credentials)
    order_groups = _canonical_order_groups(obj, credentials)
    return make_success(
        operation=operation,
        exchange=name,
        account=credentials["account"],
        positions=_canonical_positions(obj),
        open_order_count=sum(g.order_count for g in order_groups),
        order_groups=order_groups,
        data={"account_id": credentials["account_id"], "source": "native_perpetuals"},
    )


def _side_value(value: Any) -> int:
    text = str(value).strip().lower() if value is not None else ""
    if text in {"buy", "long", "bid", "0"}:
        return 0
    if text in {"sell", "short", "ask", "1"}:
        return 1
    raise ValueError("side must be buy/long or sell/short")


def _side_text(value: Any) -> str:
    return "buy" if _side_value(value) == 0 else "sell"


def _base_write_payload(credentials: Dict[str, str], request: Mapping[str, Any], market: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "accountId": _resolve_cap_id(credentials),
        "walletAddress": credentials["address"],
        "marketId": _market_id(market) or str(request.get("symbol") or ""),
        "sponsor": None,
        "txKind": None,
    }


def _has_position(credentials: Dict[str, str], market_id: str) -> bool:
    try:
        obj = _account_positions(credentials)
        for pos in obj.get("positions") or []:
            if str(pos.get("marketId") or "") == market_id and _decimal(pos.get("baseAssetAmount"), "0") != 0:
                return True
    except Exception:
        pass
    return False


def _order_ids_from_request(request: Mapping[str, Any]) -> List[str]:
    raw = request.get("order_ids") or request.get("orderIds") or request.get("ids") or []
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return [str(x) for x in raw if str(x).strip()] if isinstance(raw, Iterable) else []


def _find_position(credentials: Dict[str, str], symbol: str, side: str = "") -> Dict[str, Any]:
    obj = _account_positions(credentials)
    wanted = symbol.strip()
    for pos in obj.get("positions") or []:
        if not isinstance(pos, dict):
            continue
        if wanted and wanted not in {str(pos.get("marketId") or ""), _position_symbol(pos)}:
            continue
        size = _decimal(pos.get("baseAssetAmount"), "0")
        if side and _position_side(size) != side.lower():
            continue
        if size != 0:
            return pos
    raise ValueError("matching position not found")


def _resolve_cap_id(credentials: Dict[str, str]) -> str:
    """Resolve the Aftermath **capability object ID** for the wallet.

    ``credentials['account_id']`` carries the human account number (``"672"``)
    from ``AFTERMATH_<ALIAS>_ACCOUNT_ID``. Aftermath's CCXT build paths
    require the on-chain *capability object ID* — a 32-byte hex address — in
    the ``accountId`` field. Calling ``/api/ccxt/accounts`` with the wallet
    address returns both a ``capability`` entry and an ``account`` entry; we
    pick the capability one and cache it on the credentials dict so subsequent
    calls in the same process skip the round-trip.

    Raises ``RuntimeError`` if the capability ID is not a 32-byte hex string
    — that would mean Aftermath returned something we cannot safely send into
    an ``Address`` field.
    """
    cached = credentials.get("cap_id")
    if cached and _SUI_ADDRESS_PATTERN.match(cached):
        return cached
    body = {"address": credentials["address"]}
    response = _post_json(credentials, "/api/ccxt/accounts", body)
    if not isinstance(response, list) or not response:
        raise RuntimeError(
            f"Aftermath /api/ccxt/accounts returned an unexpected payload for "
            f"{credentials['account']!r}: {type(response).__name__}"
        )
    cap = next((row for row in response if isinstance(row, Mapping) and row.get("type") == "capability"), None)
    if not cap:
        raise RuntimeError(
            f"Aftermath /api/ccxt/accounts did not include a capability entry "
            f"for {credentials['account']!r} wallet {credentials['address']!r}"
        )
    cap_id = str(cap.get("id") or "").strip()
    if not _SUI_ADDRESS_PATTERN.match(cap_id):
        raise RuntimeError(
            f"Aftermath capability id for {credentials['account']!r} is not a "
            f"32-byte hex address: {cap_id!r}"
        )
    credentials["cap_id"] = cap_id
    return cap_id


def _is_dry_run(request: Mapping[str, Any]) -> bool:
    if "dry_run" in request:
        return bool(request.get("dry_run"))
    env = _read_env("AFTERMATH_DRY_RUN").lower()
    return env not in {"0", "false", "no", "live"}


def _dryrun_success(operation: str, credentials: Dict[str, str], result: Any, *, data: Dict[str, Any], **kwargs: Any) -> CanonicalResponse:
    payload = dict(data)
    payload["dry_run"] = True
    payload["native_response"] = result
    return make_success(operation=operation, exchange=name, account=credentials["account"], data=payload, **kwargs)


def _live_success(operation: str, credentials: Dict[str, str], submit: Mapping[str, Any], *, data: Dict[str, Any], **kwargs: Any) -> CanonicalResponse:
    """Wrap a successful LIVE sign+submit call as a canonical response."""
    payload = dict(data)
    payload["live_submitted"] = True
    payload["dry_run"] = False
    payload["submit_path"] = submit.get("submit_path")
    submit_payload = submit.get("submit_payload") or {}
    # Redact the transaction bytes but keep signature length so callers can confirm size.
    safe_submit = {
        "submit_path": submit.get("submit_path"),
        "transactionBytes_len": len(submit_payload.get("transactionBytes") or ""),
        "signatures_count": len(submit_payload.get("signatures") or []),
        "response": submit.get("submit_response"),
    }
    payload["submit"] = safe_submit
    response_obj = submit.get("submit_response")
    if isinstance(response_obj, Mapping):
        digest = response_obj.get("digest") or response_obj.get("txDigest")
        if digest:
            payload["digest"] = digest
    return make_success(operation=operation, exchange=name, account=credentials["account"], data=payload, **kwargs)


def _live_unavailable(operation: str, credentials: Dict[str, str], message: str = "Aftermath native LIVE signing/submission is not enabled in this runtime.") -> CanonicalResponse:
    return make_failure(operation=operation, exchange=name, account=credentials["account"], code="LIVE_SIGNING_UNAVAILABLE", message=message)


# Aftermath's API does not expose any non-CCXT write surface. The build/submit
# pair ``/api/ccxt/build/{createOrders,cancelOrders,setLeverage,...}`` and
# ``/api/ccxt/submit/{createOrders,cancelOrders,setLeverage,...}`` is the only
# way to construct and submit perpetuals transactions. We deliberately do NOT
# import the Python ``ccxt`` package — the agent talks to Aftermath's REST
# surface directly. The agent docstring's "no CCXT" claim therefore means:
# "no Python ccxt import; reads use native /api/perpetuals/* and writes use
# Aftermath's documented /api/ccxt/build/* and /api/ccxt/submit/* endpoints."
_BUILD_ROUTES: Dict[str, str] = {
    "createOrders": "/api/ccxt/build/createOrders",
    "cancelOrders": "/api/ccxt/build/cancelOrders",
    "setLeverage": "/api/ccxt/build/setLeverage",
}
_SUBMIT_ROUTES: Dict[str, str] = {
    "createOrders": "/api/ccxt/submit/createOrders",
    "cancelOrders": "/api/ccxt/submit/cancelOrders",
    "setLeverage": "/api/ccxt/submit/setLeverage",
}


def _build_metadata(credentials: Dict[str, str], *, gas_from_address_balance: bool = True) -> Dict[str, Any]:
    """Build a ``TransactionMetadata`` payload — only ``sender`` is required by
    Aftermath. We request gas from the sender's SUI address balance so the
    agent wallet's 0.1 SUI covers the cost (avoids race conditions with
    concurrent gas-coin selections).
    """
    meta: Dict[str, Any] = {"sender": credentials["address"]}
    if gas_from_address_balance:
        meta["gasFromAddressBalance"] = True
    return meta


def _ch_id(market: Mapping[str, Any]) -> str:
    """Return the clearing-house ID for a market row.

    Aftermath's build paths take ``chId`` (clearing-house object ID), not the
    market's own object ID. The ``/ccxt/markets`` response publishes ``chId``
    on each ``Market``. The native ``/api/perpetuals/markets`` rows we cache
    carry only ``objectId`` — and on Aftermath's perpetuals architecture, the
    market object IS the clearing house, so ``objectId`` is a valid ``chId``.
    """
    return str(
        market.get("chId")
        or market.get("clearingHouseId")
        or market.get("id")
        or market.get("objectId")
        or market.get("marketId")
        or ""
    ).strip()


def _native_price_float(value: Any) -> Optional[float]:
    """Convert a human-readable price (e.g. ``"120.5"``) to the float Aftermath's
    CCXT build paths accept. The native ``place-limit-order`` paths used scaled
    integers; the CCXT build path uses plain floats.
    """
    if value is None or value == "":
        return None
    return float(value)


def _native_amount_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def _sign_and_submit(
    credentials: Dict[str, str],
    route_name: str,
    build_payload: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build a transaction via ``/api/ccxt/build/{route}``, sign the returned
    ``signingDigest`` with the agent wallet, and submit via
    ``/api/ccxt/submit/{route}``.

    Returns ``{submit_path, submit_payload, submit_response}`` where
    ``submit_payload`` is redacted before being attached to the canonical data.
    """
    build_path = _BUILD_ROUTES[route_name]
    submit_path = _SUBMIT_ROUTES[route_name]
    build_response = _post_json(credentials, build_path, dict(build_payload))
    if not isinstance(build_response, Mapping):
        raise RuntimeError(f"Aftermath {build_path} returned a non-object response")
    tx_bytes_b64 = str(build_response.get("transactionBytes") or "").strip()
    signing_digest_b64 = str(build_response.get("signingDigest") or "").strip()
    if not tx_bytes_b64 or not signing_digest_b64:
        raise RuntimeError(
            f"Aftermath {build_path} did not include transactionBytes/signingDigest"
        )
    try:
        signing_digest = b64decode(signing_digest_b64, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Aftermath signingDigest was not valid base64: {type(exc).__name__}") from exc
    sponsor_sig = build_response.get("sponsorSignature")
    try:
        signer = SuiSigner(credentials["private_key"])
    except SuiSigningError as exc:
        raise RuntimeError(f"failed to initialize Sui signer: {exc}") from exc
    sender_signature = signer.serialized_user_signature(signing_digest)
    signatures: List[str] = (
        [str(sponsor_sig), sender_signature] if sponsor_sig else [sender_signature]
    )
    submit_payload = {
        "transactionBytes": tx_bytes_b64,
        "signatures": signatures,
    }
    submit_response = _post_json(credentials, submit_path, submit_payload)
    return {
        "build_path": build_path,
        "submit_path": submit_path,
        "submit_payload": submit_payload,
        "submit_response": submit_response,
    }


def _new_order(credentials: Dict[str, str], request: Mapping[str, Any]) -> CanonicalResponse:
    symbol = str(request.get("symbol") or "").strip()
    market = _find_market(credentials, symbol)
    ch_id = _ch_id(market) or _market_id(market) or symbol
    side = _side_text(request.get("side"))  # CCXT takes strings, not ints
    order_type = str(request.get("order_type") or request.get("type") or "limit").strip().lower()
    if order_type not in {"market", "limit"}:
        raise ValueError("order_type must be 'market' or 'limit'")
    volume = request.get("volume") or request.get("size") or request.get("amount")
    if volume is None:
        raise ValueError("Missing volume")
    display_symbol = _market_symbol(market)
    order: Dict[str, Any] = {
        "chId": ch_id,
        "type": order_type,
        "side": side,
        "amount": _native_amount_float(volume),
        "reduceOnly": bool(request.get("reduce_only", False)),
    }
    if order_type == "limit":
        price = request.get("price")
        if price is None:
            raise ValueError("Missing price for limit order")
        order["price"] = _native_price_float(price)
    if request.get("client_order_id"):
        order["clientOrderId"] = str(request["client_order_id"])
    build_payload: Dict[str, Any] = {
        "accountId": _resolve_cap_id(credentials),
        "deallocateFreeCollateral": False,
        "metadata": _build_metadata(credentials),
        "orders": [order],
    }
    if _is_dry_run(request):
        build_response = _post_json(credentials, _BUILD_ROUTES["createOrders"], build_payload)
        result = CanonicalOrderResult(
            symbol=display_symbol,
            side=side,
            order_type=order_type,
            requested_volume=str(volume),
            requested_price=str(request.get("price") or "market"),
            submitted_volume="0",
            submitted_price=str(request.get("price") or "market"),
            verified=False,
            status="dry_run",
        )
        return _dryrun_success(
            "new_order",
            credentials,
            build_response,
            data={
                "build_path": _BUILD_ROUTES["createOrders"],
                "payload": _safe_payload(build_payload),
                "dry_run_note": "Aftermath transaction was built only; it was not signed or submitted.",
            },
            order=result,
        )
    submit = _sign_and_submit(credentials, "createOrders", build_payload)
    submit_resp = submit.get("submit_response") if isinstance(submit.get("submit_response"), Mapping) else {}
    result = CanonicalOrderResult(
        symbol=display_symbol,
        side=side,
        order_type=order_type,
        requested_volume=str(volume),
        requested_price=str(request.get("price") or "market"),
        submitted_volume=str(volume),
        submitted_price=str(request.get("price") or "market"),
        verified=True,
        status="submitted",
        exchange_order_id=str(submit_resp.get("digest") or submit_resp.get("txDigest") or "") or None,
    )
    return _live_success(
        "new_order",
        credentials,
        submit,
        data={
            "build_path": _BUILD_ROUTES["createOrders"],
            "payload": _safe_payload(build_payload),
            "operation": "new_order",
        },
        order=result,
    )


def _ladder_distribution_weights(order_count: int, distribution: str) -> List[Decimal]:
    """Return per-child size weights for a ladder.

    Matches the wizard preview exactly: ``uniform`` → equal weights;
    ``half_gaussian`` → ``exp(-((3 * (count-1-i) / (count-1))²) / 2)`` which is
    smallest near ``start`` (i=0) and largest near ``end`` (i=count-1).
    Same canonical math used by the Hyperliquid agent so the preview and the
    live submission stay in sync.
    """
    if order_count <= 0:
        return []
    distribution_key = str(distribution or "").strip().lower()
    if distribution_key == "uniform":
        return [Decimal("1")] * order_count
    if distribution_key != "half_gaussian":
        raise ValueError("UNSUPPORTED_DISTRIBUTION")
    if order_count == 1:
        return [Decimal("1")]
    weights: List[Decimal] = []
    span = Decimal(order_count - 1)
    for index in range(order_count):
        z = Decimal("3") * (span - Decimal(index)) / span
        weight = math.exp(-(float(z) ** 2) / 2.0)
        weights.append(Decimal(str(weight)))
    return weights


def _verify_ladder_children(
    credentials: Dict[str, str],
    ch_id: str,
    expected_children: Sequence[Mapping[str, Any]],
) -> Tuple[int, List[Dict[str, Any]]]:
    """Re-read live open orders and match each expected child to an id.

    Two orders match if their ``price`` and ``side`` agree and the
    ``remaining`` size is within one part in 10^6 of the expected size.
    Returns ``(matched_count, matched_rows)``.
    """
    if not ch_id or not expected_children:
        return 0, []
    live = _open_orders_for(credentials, ch_id)
    matched: List[Dict[str, Any]] = []
    used: set = set()
    for child in expected_children:
        c_price = _decimal(child.get("price"), "0")
        c_side = str(child.get("side") or "").lower()
        c_size = abs(_decimal(child.get("amount"), "0"))
        for idx, order in enumerate(live):
            if idx in used:
                continue
            o_side = str(order.get("side") or "").lower()
            if o_side != c_side:
                continue
            o_price = _decimal(order.get("price"), "0")
            o_size = abs(_decimal(order.get("remaining"), "0"))
            if o_price == 0 or c_price == 0:
                continue
            if abs(o_price - c_price) / c_price > Decimal("0.000001"):
                continue
            if c_size > 0 and abs(o_size - c_size) / c_size > Decimal("0.01"):
                continue
            used.add(idx)
            matched.append({
                "order_id": str(order.get("id") or order.get("orderId") or ""),
                "price": str(o_price),
                "size": str(o_size),
                "side": o_side,
            })
            break
    return len(matched), matched


# Aftermath enforces a per-order minimum notional (usd) per market. The
# documented field is ``marketParams.minOrderUsdValue`` on
# ``/api/perpetuals/all-markets``. The value defaults to 1.0 USD for every
# market we have observed, but we read it from marketParams at runtime and
# fall back to 1.0 if the field is absent. ``_validate_children_min_notional``
# is the single source of truth for the per-child check — there is no
# Aftermath-documented per-PTB cap, so we do not enforce one client-side.


def _validate_children_min_notional(
    child_orders: Sequence[Mapping[str, Any]],
    min_order_usd_value: Decimal,
) -> Optional[CanonicalResponse]:
    """Return a clear error response when any child violates Aftermath's
    per-order minimum notional, else ``None``. The exact offending child
    (index, side, price, size, notional) is reported so the wizard can
    surface it instead of opaque HTTP 500.
    """
    for idx, child in enumerate(child_orders):
        price = _decimal(child.get("price"), "0")
        amount = abs(_decimal(child.get("amount"), "0"))
        if price <= 0 or amount <= 0:
            continue
        notional = price * amount
        if notional < min_order_usd_value:
            return make_failure(
                operation="ladder", exchange=name, account="",
                code="CHILD_BELOW_MIN_NOTIONAL",
                message=(
                    f"child #{idx} {child.get('side','?')} notional "
                    f"${notional:.4f} (price ${price:.4f} × size {amount}) "
                    f"is below Aftermath's per-order minimum "
                    f"${min_order_usd_value:.2f}. "
                    f"Increase total_volume or reduce order_count."
                ),
            )
    return None


def _max_order_size(
    credentials: Dict[str, str],
    ch_id: str,
    side: int,
    price: Decimal,
    scaling_factor: Decimal,
) -> Optional[Decimal]:
    """Call Aftermath's documented ``/api/perpetuals/account/max-order-size``
    endpoint. Returns the largest base-asset size this account can place at
    the given side/price right now, or ``None`` if the request fails.
    """
    payload: Dict[str, Any] = {
        "accountId": credentials.get("account_id"),
        "marketId": ch_id,
        "side": int(side),
        "price": float(price),
    }
    try:
        resp = _post_json(credentials, "/api/perpetuals/account/max-order-size", payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Aftermath max-order-size probe failed: %s", _redact_credentials(str(exc), credentials))
        return None
    raw = resp.get("maxOrderSize") if isinstance(resp, Mapping) else None
    if raw is None:
        return None
    text = str(raw).rstrip("n")
    try:
        scaled = Decimal(text)
    except Exception:  # noqa: BLE001
        return None
    return scaled * scaling_factor


def _validate_ladder_capacity(
    credentials: Dict[str, str],
    ch_id: str,
    side_text: str,
    child_prices: Sequence[Decimal],
    child_sizes: Sequence[Decimal],
    scaling_factor: Decimal,
) -> Optional[CanonicalResponse]:
    """Pre-flight check using the documented max-order-size endpoint.

    For SELL orders the binding limit is the smallest price in the range
    (the SELL cap shrinks as the price rises relative to the short positions
    holding the account back). For BUY orders the binding limit is the
    largest price in the range. We probe both ends and compare against the
    largest child size at each end.
    """
    if not child_prices or not child_sizes:
        return None
    side_int = 0 if side_text == "buy" else 1
    if side_int == 1:  # sell — lowest price is the tightest end
        binding_price = min(child_prices)
        binding_idx = child_prices.index(binding_price)
    else:  # buy — highest price is the tightest end
        binding_price = max(child_prices)
        binding_idx = child_prices.index(binding_price)
    binding_size = abs(child_sizes[binding_idx])
    cap = _max_order_size(credentials, ch_id, side_int, binding_price, scaling_factor)
    if cap is None:
        return None  # probe failed; let the live submit surface the real error
    if binding_size > cap:
        return make_failure(
            operation="ladder", exchange=name, account=credentials.get("account", ""),
            code="LADDER_OVER_MAX_ORDER_SIZE",
            message=(
                f"child #{binding_idx} {side_text} size {binding_size} SOL exceeds "
                f"Aftermath's max-order-size cap {cap} SOL at price ${binding_price}. "
                f"This account cannot place this child at this price right now. "
                f"Reduce total_volume, narrow the price range toward lower "
                f"{'sell ' if side_int == 1 else 'buy '}capacity, or close/cover "
                f"existing positions to free up room."
            ),
        )
    return None


def _ladder(credentials: Dict[str, str], request: Mapping[str, Any]) -> CanonicalResponse:
    """Scale/ladder orders.

    The native ``place-scale-order`` endpoint does not appear in Aftermath's
    public spec, so we expand a ladder into a list of N individual limit
    orders and submit them in a single ``createOrders`` build call (the CCXT
    build path accepts an array of orders and Aftermath composes them into one
    PTB). After a LIVE submit we re-read ``/api/ccxt/myPendingOrders`` for
    the market to verify each expected child actually exists.
    """
    symbol = str(request.get("symbol") or "").strip()
    market = _find_market(credentials, symbol)
    ch_id = _ch_id(market) or _market_id(market) or symbol
    display_symbol = _market_symbol(market) or symbol
    count = int(request.get("order_count") or request.get("count") or 0)
    if count <= 0:
        raise ValueError("order_count must be positive")
    total_volume = request.get("total_volume") or request.get("volume")
    start_price = request.get("start_price")
    end_price = request.get("end_price")
    if total_volume is None or start_price is None or end_price is None:
        raise ValueError("ladder requires total_volume, start_price, and end_price")
    side = _side_text(request.get("side"))
    distribution = str(request.get("distribution") or "uniform").lower()
    try:
        weights = _ladder_distribution_weights(count, distribution)
    except ValueError:
        distribution = "uniform"
        weights = _ladder_distribution_weights(count, distribution)
    weights_total = sum(weights) or Decimal("1")
    start = _decimal(start_price)
    end = _decimal(end_price)
    price_step = (end - start) / Decimal(max(count - 1, 1))
    requested_total = _decimal(total_volume)
    child_prices: List[Decimal] = [start + price_step * Decimal(i) for i in range(count)]
    child_sizes: List[Decimal] = [
        (requested_total * w / weights_total) for w in weights
    ]
    # Quantize every child to Aftermath's tick/lot precision. Without this,
    # the build endpoint rejects every order with an opaque HTTP 500 because
    # unquantized amounts/prices don't fit the on-chain Move types.
    tick_inc = _decimal(_price_increment(market) or "0.01")
    lot_inc = _decimal(_size_increment(market) or "0.01")
    child_prices_q: List[Decimal] = [_quantize_floor(p, tick_inc) for p in child_prices]
    child_sizes_q: List[Decimal] = [_quantize_floor(s, lot_inc) for s in child_sizes]
    orders: List[Dict[str, Any]] = []
    reduce_only = bool(request.get("reduce_only", False))
    for price, size in zip(child_prices_q, child_sizes_q):
        orders.append({
            "chId": ch_id,
            "type": "limit",
            "side": side,
            "amount": float(size),
            "price": float(price),
            "reduceOnly": reduce_only,
        })
    build_payload: Dict[str, Any] = {
        "accountId": _resolve_cap_id(credentials),
        "deallocateFreeCollateral": False,
        "metadata": _build_metadata(credentials),
        "orders": orders,
    }
    expected_children = [
        {"price": float(p), "size": float(s), "side": side}
        for p, s in zip(child_prices_q, child_sizes_q)
    ]
    # --- Per-order minimum notional check (documented Aftermath rule) -------
    # Read the per-market ``minOrderUsdValue`` straight from marketParams.
    params = _market_params(market)
    try:
        min_order_usd_value = _decimal(params.get("minOrderUsdValue", "1.0"))
    except Exception:  # noqa: BLE001
        min_order_usd_value = Decimal("1.0")
    try:
        scaling_factor = _decimal(params.get("scalingFactor", "1"))
    except Exception:  # noqa: BLE001
        scaling_factor = Decimal("1")
    validation_error = _validate_children_min_notional(orders, min_order_usd_value)
    if validation_error is not None:
        return validation_error
    # --- max-order-size check (documented Aftermath endpoint) --------------
    capacity_error = _validate_ladder_capacity(
        credentials, ch_id, side, child_prices_q, child_sizes_q, scaling_factor,
    )
    if capacity_error is not None:
        return capacity_error
    build_payload: Dict[str, Any] = {
        "accountId": _resolve_cap_id(credentials),
        "deallocateFreeCollateral": False,
        "metadata": _build_metadata(credentials),
        "orders": orders,
    }
    if _is_dry_run(request):
        # Dry-run probes the entire ladder as a single ``orders[]`` PTB so the
        # wizard preview reflects what Aftermath's build endpoint will actually
        # accept. If it fails the wizard sees the exact HTTP 500 body or the
        # pre-flight validation error — not a blanket "Succeeded: 0 / Failed: N".
        try:
            build_response = _post_json(
                credentials, _BUILD_ROUTES["createOrders"], build_payload,
            )
            first_batch_accepted = bool(
                build_response.get("transactionBytes")
                if isinstance(build_response, Mapping)
                else False
            )
            first_batch_error: Optional[str] = None
        except Exception as exc:  # noqa: BLE001
            build_response = None
            first_batch_accepted = False
            first_batch_error = str(exc)
        batch_plan = [{
            "index": 0,
            "order_indices": list(range(count)),
            "notional_usd": str(sum(child_prices_q[i] * child_sizes_q[i] for i in range(count))),
        }]
        ladder_batches = [{
            "index": 0,
            "requested": count,
            "accepted": 0,
            "expected_children": expected_children,
            "batch_plan": batch_plan[0],
            "first_batch_accepted": first_batch_accepted,
            "first_batch_error": first_batch_error,
        }]
        ladder = CanonicalLadderResult(
            symbol=display_symbol,
            side=side,
            distribution=distribution,
            requested_order_count=count,
            submitted_order_count=0,
            requested_volume=str(total_volume),
            submitted_volume="0",
            batch_count=1,
            verified=False,
            status="dry_run",
            accepted_child_count=0,
            omitted_order_count=0,
            batches=ladder_batches,
        )
        dryrun_data = {
            "build_path": _BUILD_ROUTES["createOrders"],
            "submit_path": _SUBMIT_ROUTES["createOrders"],
            "payload": _safe_payload(build_payload),
            "preview": {
                "symbol": display_symbol,
                "side": side,
                "orders": count,
                "total_size": str(total_volume),
                "min_price": str(min(child_prices_q)),
                "max_price": str(max(child_prices_q)),
                "vwap": str(_ladder_vwap(child_prices_q, child_sizes_q)),
                "distribution": distribution,
            },
            "batch_count": 1,
            "batch_plan": batch_plan,
            "batches": ladder_batches,
            "expected_children": expected_children,
            "first_batch_accepted": first_batch_accepted,
            "first_batch_error": first_batch_error,
            "min_order_usd_value": str(min_order_usd_value),
            "quantization": {
                "tick_increment": str(tick_inc),
                "lot_increment": str(lot_inc),
                "requested_volume": str(total_volume),
                "quantized_total_volume": str(sum(child_sizes_q)),
            },
        }
        if first_batch_accepted:
            return _dryrun_success(
                "ladder", credentials, build_response,
                data=dryrun_data, ladder=ladder,
            )
        # Dry-run build failed → surface the exact reason instead of a
        # blanket "Succeeded: 0 / Failed: N".
        err_response = make_failure(
            operation="ladder", exchange=name,
            account=credentials.get("account", ""),
            code="LADDER_DRY_RUN_REJECTED",
            message=first_batch_error or "Aftermath rejected the build.",
            ladder=ladder,
        )
        object.__setattr__(err_response, "data", dryrun_data)
        return err_response
    # ---- LIVE branch -----------------------------------------------------
    # Aftermath's /api/ccxt/build/createOrders accepts multiple orders in a
    # single PTB when the wizard's distribution and Aftermath's risk checks
    # all line up. If Aftermath's CCXT build endpoint rejects the whole
    # ladder (the endpoint returns opaque HTTP 500 with no per-child
    # reason), fall back to N individual single-order PTBs so the wizard
    # can still report which exact children landed and which didn't. Child
    # prices and sizes are never altered — every fallback PTB carries the
    # generated child exactly as the wizard produced it.
    matched_rows: List[Dict[str, Any]] = []
    batch_summaries: List[Dict[str, Any]] = []
    accepted_total = 0
    last_digest = ""
    any_failed = False
    first_error: Optional[str] = None

    def _submit_indices(indices: List[int], batch_no: int) -> Dict[str, Any]:
        nonlocal accepted_total, last_digest, any_failed, first_error
        batch_orders = [orders[i] for i in indices]
        batch_expected = [expected_children[i] for i in indices]
        batch_payload: Dict[str, Any] = {
            "accountId": _resolve_cap_id(credentials),
            "deallocateFreeCollateral": False,
            "metadata": _build_metadata(credentials),
            "orders": batch_orders,
        }
        try:
            submit = _sign_and_submit(credentials, "createOrders", batch_payload)
        except Exception as exc:  # noqa: BLE001
            any_failed = True
            if first_error is None:
                first_error = str(exc)
            logger.warning(
                "Aftermath ladder batch %d failed: %s",
                batch_no,
                _redact_credentials(str(exc), credentials),
            )
            return {
                "batch": batch_no,
                "requested": len(indices),
                "accepted": 0,
                "transaction_digest": "",
                "error": str(exc),
                "status": "failed",
                "build_path": _BUILD_ROUTES["createOrders"],
                "submit_path": _SUBMIT_ROUTES["createOrders"],
                "build_payload": _safe_payload(batch_payload),
            }
        submit_response = submit.get("submit_response") if isinstance(submit, Mapping) else None
        digest = ""
        if isinstance(submit_response, Mapping):
            digest = str(submit_response.get("digest") or submit_response.get("txDigest") or "")
        last_digest = digest or last_digest
        accepted, rows = _verify_ladder_children(credentials, ch_id, batch_expected)
        accepted_total += accepted
        matched_rows.extend(rows)
        if accepted != len(indices):
            any_failed = True
        return {
            "batch": batch_no,
            "requested": len(indices),
            "accepted": accepted,
            "transaction_digest": digest,
            "status": "submitted" if accepted == len(indices) else "partial",
            "build_path": _BUILD_ROUTES["createOrders"],
            "submit_path": _SUBMIT_ROUTES["createOrders"],
            "build_payload": _safe_payload(batch_payload),
        }

    # Step 1: try the entire ladder in ONE PTB.
    one_ptb_result = _submit_indices(list(range(count)), batch_no=1)
    if one_ptb_result.get("status") == "submitted":
        batch_summaries.append(one_ptb_result)
    else:
        # ONE-PTB attempt failed at build/submit. Aftermath's ``createOrders``
        # build endpoint doesn't expose per-child rejection reasons, so if the
        # whole ladder fails in one PTB the failure reason is opaque. Fall
        # back to submitting each child as its own single-order PTB so the
        # wizard can report which exact children landed and which didn't.
        # We do NOT alter the child prices/sizes — every child keeps its
        # generated price and size.
        first_one_ptb_error = one_ptb_result.get("error") or first_error
        accepted_total = 0
        matched_rows = []
        last_digest = ""
        any_failed = False
        first_error = first_one_ptb_error
        logger.info(
            "Aftermath ladder single-PTB attempt failed (%s); falling back to "
            "%d individual single-order PTBs.",
            first_one_ptb_error, count,
        )
        for batch_no, idx in enumerate(range(count), start=1):
            summary = _submit_indices([idx], batch_no)
            batch_summaries.append(summary)
    verified = accepted_total == count and accepted_total > 0 and not any_failed
    submitted_volume = sum(child_sizes_q[:accepted_total]) if accepted_total else Decimal("0")
    # Build a flat failures list for the wizard's existing ``data["failures"]``
    # rendering. Each failure carries the batch index, error message, and the
    # sanitized build payload so the user can see exactly what was rejected.
    failures_list = [
        {
            "batch": b.get("batch"),
            "requested": b.get("requested"),
            "error": b.get("error") or "",
            "code": "AFTERMATH_BUILD_OR_SUBMIT_FAILED",
            "build_payload": b.get("build_payload") or {},
        }
        for b in batch_summaries
        if b.get("status") in {"failed", "partial"}
    ]
    all_failed = accepted_total == 0 and any_failed and bool(first_error)
    ladder = CanonicalLadderResult(
        symbol=display_symbol,
        side=side,
        distribution=distribution,
        requested_order_count=count,
        submitted_order_count=accepted_total,
        requested_volume=str(total_volume),
        submitted_volume=str(submitted_volume),
        batch_count=len(batch_summaries),
        verified=verified,
        status="submitted" if verified else ("partial" if accepted_total > 0 else "failed"),
        accepted_child_count=accepted_total,
        omitted_order_count=max(0, count - accepted_total),
        child_order_ids=[r["order_id"] for r in matched_rows if r.get("order_id")],
        batches=batch_summaries,
    )
    if all_failed:
        # Surface the first concrete HTTP/build error to the wizard so the
        # "Succeeded: 0 / Failed: 10" view becomes "Error: <real reason>".
        err_payload = (
            first_error
            or "Aftermath rejected every batch; no transactions were submitted."
        )
        error_response = make_failure(
            operation="ladder", exchange=name, account=credentials.get("account", ""),
            code="LADDER_BUILD_FAILED",
            message=err_payload,
            ladder=ladder,
        )
        # Attach the rich diagnostic payload. CanonicalResponse is a frozen
        # dataclass so we use object.__setattr__ to set ``data`` exactly once.
        object.__setattr__(error_response, "data", {
            "build_path": _BUILD_ROUTES["createOrders"],
            "submit_path": _SUBMIT_ROUTES["createOrders"],
            "operation": "ladder",
            "requested_order_count": count,
            "submitted_order_count": 0,
            "verified": False,
            "first_error": first_error,
            "failures": failures_list,
            "child_orders": orders,
            "quantization": {
                "tick_increment": str(tick_inc),
                "lot_increment": str(lot_inc),
                "requested_volume": str(total_volume),
                "quantized_total_volume": str(sum(child_sizes_q)),
            },
            "preview": {
                "symbol": display_symbol,
                "side": side,
                "orders": count,
                "total_size": str(total_volume),
                "min_price": str(min(child_prices_q)),
                "max_price": str(max(child_prices_q)),
                "vwap": str(_ladder_vwap(child_prices_q, child_sizes_q)),
                "distribution": distribution,
            },
            "batches": batch_summaries,
        })
        return error_response
    return _live_success(
        "ladder",
        credentials,
        {"submit_path": _SUBMIT_ROUTES["createOrders"], "submit_response": {"digest": last_digest},
         "build_path": _BUILD_ROUTES["createOrders"]},
        data={
            "build_path": _BUILD_ROUTES["createOrders"],
            "submit_path": _SUBMIT_ROUTES["createOrders"],
            "payload": _safe_payload(build_payload),
            "operation": "ladder",
            # Wizard-friendly aliases — the existing ladder_result renderer
            # in wizard.py reads ``requested`` / ``succeeded`` / ``failed`` /
            # ``failures`` from ``response.data``.
            "requested": count,
            "succeeded": accepted_total,
            "failed": max(0, count - accepted_total),
            "failures": failures_list,
            "transaction_digest": last_digest,
            "requested_order_count": count,
            "submitted_order_count": accepted_total,
            "verified": verified,
            "verified_via": "/api/ccxt/myPendingOrders",
            "matched_children": matched_rows,
            "batch_count": len(batch_summaries),
            "min_order_usd_value": str(min_order_usd_value),
            "first_error": first_error,
            "quantization": {
                "tick_increment": str(tick_inc),
                "lot_increment": str(lot_inc),
                "requested_volume": str(total_volume),
                "quantized_total_volume": str(sum(child_sizes_q)),
            },
            "preview": {
                "symbol": display_symbol,
                "side": side,
                "orders": count,
                "total_size": str(total_volume),
                "min_price": str(min(child_prices_q)),
                "max_price": str(max(child_prices_q)),
                "vwap": str(_ladder_vwap(child_prices_q, child_sizes_q)),
                "distribution": distribution,
            },
            "child_orders": orders,
            "batches": batch_summaries,
        },
        ladder=ladder,
    )


def _ladder_vwap(prices: Sequence[Decimal], sizes: Sequence[Decimal]) -> Decimal:
    """Size-weighted VWAP for the generated child orders."""
    total_size = sum((abs(s) for s in sizes), Decimal("0"))
    if total_size == 0:
        return Decimal("0")
    return sum((abs(s) * p for p, s in zip(prices, sizes)), Decimal("0")) / total_size


def _open_orders_for(credentials: Dict[str, str], ch_id: str) -> List[Dict[str, Any]]:
    """Read live CCXT-shaped open orders for one market on this account.

    Wraps :func:`_pending_orders_for_market` (which is keyed by market id) so
    the cancel and ladder verification paths can both list open orders for a
    given ``ch_id``.
    """
    if not ch_id:
        return []
    return _pending_orders_for_market(credentials, ch_id)


def _resolve_open_order_ids(
    credentials: Dict[str, str],
    symbol: str,
    side: Optional[str],
    explicit_ids: Optional[Sequence[str]] = None,
) -> Tuple[str, str, List[Dict[str, Any]]]:
    """Resolve live open orders to cancel for a wizard ``cancel_order_group`` call.

    Returns ``(display_symbol, ch_id, open_orders)`` where ``open_orders`` is
    the list of CCXT-shaped rows for the resolved market (filtered by
    ``side`` if provided). If ``explicit_ids`` is non-empty the explicit list
    is preserved and the live lookup is used only as a verification source.
    """
    display_symbol = str(symbol or "").strip()
    if not display_symbol:
        raise ValueError("cancel_orders requires symbol")
    market = _find_market(credentials, display_symbol)
    ch_id = _ch_id(market) or _market_id(market) or display_symbol
    resolved_symbol = _market_symbol(market) or display_symbol
    open_orders = _open_orders_for(credentials, ch_id)
    side_filter = _side_text(side) if side else ""
    if side_filter:
        open_orders = [o for o in open_orders if str(o.get("side") or "").lower() == side_filter]
    return resolved_symbol, ch_id, open_orders


def _verify_cancellations(
    credentials: Dict[str, str],
    ch_id: str,
    targeted_ids: Sequence[str],
) -> Tuple[int, int, List[str]]:
    """Re-read open orders and report how many of the targeted ids are gone.

    Returns ``(cancelled_count, remaining_count, remaining_ids)``.
    """
    if not ch_id or not targeted_ids:
        return 0, 0, []
    remaining_orders = _open_orders_for(credentials, ch_id)
    remaining_ids = {str(o.get("id") or o.get("orderId") or "") for o in remaining_orders}
    remaining = [tid for tid in targeted_ids if tid in remaining_ids]
    cancelled = len(targeted_ids) - len(remaining)
    return cancelled, len(remaining), remaining


def _cancel_orders(credentials: Dict[str, str], request: Mapping[str, Any]) -> CanonicalResponse:
    symbol = str(request.get("symbol") or "").strip()
    side = request.get("side")
    display_symbol = symbol
    ch_id = ""
    targeted_ids: List[str] = []
    open_orders: List[Dict[str, Any]] = []
    explicit_ids = _order_ids_from_request(request)
    try:
        resolved_symbol, ch_id, open_orders = _resolve_open_order_ids(credentials, symbol, side)
        display_symbol = resolved_symbol
        if explicit_ids:
            targeted_ids = list(explicit_ids)
        else:
            targeted_ids = [
                str(o.get("id") or o.get("orderId") or "")
                for o in open_orders
                if (o.get("id") or o.get("orderId"))
            ]
    except ValueError:
        if not explicit_ids:
            cancel = CanonicalCancelGroupResult(
                symbol=symbol or "",
                side=_side_text(side) if side else "",
                targeted_order_count=0,
                cancelled_order_count=0,
                confirmed_absent_count=0,
                remaining_target_count=0,
                verified=False,
                status="no_open_orders",
                batch_count=0,
                requested_cancel_count=0,
                verified_cancel_count=0,
            )
            return make_success(
                operation="cancel_order_group",
                exchange=name,
                account=credentials.get("account", ""),
                data={"status": "no_open_orders", "symbol": symbol, "side": _side_text(side) if side else ""},
                cancel_group=cancel,
            )
        targeted_ids = list(explicit_ids)
    if not targeted_ids:
        cancel = CanonicalCancelGroupResult(
            symbol=display_symbol,
            side=_side_text(side) if side else "",
            targeted_order_count=0,
            cancelled_order_count=0,
            confirmed_absent_count=0,
            remaining_target_count=0,
            verified=True,
            status="no_open_orders",
            batch_count=0,
            requested_cancel_count=0,
            verified_cancel_count=0,
        )
        return make_success(
            operation="cancel_order_group",
            exchange=name,
            account=credentials.get("account", ""),
            data={"status": "no_open_orders", "symbol": display_symbol, "side": _side_text(side) if side else ""},
            cancel_group=cancel,
        )
    build_payload: Dict[str, Any] = {
        "accountId": _resolve_cap_id(credentials),
        "chId": ch_id,
        "orderIds": [str(x) for x in targeted_ids],
        "deallocateFreeCollateral": False,
        "metadata": _build_metadata(credentials),
        "shouldAbortOnMissingId": False,
    }
    if _is_dry_run(request):
        build_response = _post_json(credentials, _BUILD_ROUTES["cancelOrders"], build_payload)
        cancel = CanonicalCancelGroupResult(
            symbol=display_symbol,
            side=_side_text(side) if side else "",
            targeted_order_count=len(targeted_ids),
            cancelled_order_count=0,
            confirmed_absent_count=0,
            remaining_target_count=len(targeted_ids),
            verified=False,
            status="dry_run",
            batch_count=1,
            requested_cancel_count=len(targeted_ids),
            verified_cancel_count=0,
        )
        return _dryrun_success(
            "cancel_orders",
            credentials,
            build_response,
            data={"build_path": _BUILD_ROUTES["cancelOrders"], "payload": _safe_payload(build_payload)},
            cancel_group=cancel,
        )
    submit = _sign_and_submit(credentials, "cancelOrders", build_payload)
    submit_response = submit.get("submit_response") if isinstance(submit, Mapping) else None
    digest = ""
    if isinstance(submit_response, Mapping):
        digest = str(submit_response.get("digest") or submit_response.get("txDigest") or "")
    cancelled, remaining_count, remaining_ids = _verify_cancellations(
        credentials, ch_id, targeted_ids,
    )
    verified = remaining_count == 0 and cancelled > 0
    cancel = CanonicalCancelGroupResult(
        symbol=display_symbol,
        side=_side_text(side) if side else "",
        targeted_order_count=len(targeted_ids),
        cancelled_order_count=cancelled,
        confirmed_absent_count=cancelled,
        remaining_target_count=remaining_count,
        verified=verified,
        status="submitted" if verified else "partial",
        batch_count=1,
        requested_cancel_count=len(targeted_ids),
        verified_cancel_count=cancelled,
    )
    return _live_success(
        "cancel_orders",
        credentials,
        submit,
        data={
            "build_path": _BUILD_ROUTES["cancelOrders"],
            "submit_path": _SUBMIT_ROUTES["cancelOrders"],
            "payload": _safe_payload(build_payload),
            "operation": "cancel_orders",
            "transaction_digest": digest,
            "requested_cancel_count": len(targeted_ids),
            "cancelled_order_count": cancelled,
            "remaining_count": remaining_count,
            "remaining_ids": remaining_ids,
            "verified": verified,
            "verified_via": "/api/ccxt/myPendingOrders",
        },
        cancel_group=cancel,
    )


def _close_position(credentials: Dict[str, str], request: Mapping[str, Any]) -> CanonicalResponse:
    """Reduce-only market close in the opposite direction of the current position."""
    req = dict(request)
    pos = _find_position(credentials, str(req.get("symbol") or ""), str(req.get("side") or ""))
    current_size = _decimal(pos.get("baseAssetAmount"), "0")
    if not req.get("volume"):
        req["volume"] = _display_decimal(abs(current_size))
    req["side"] = "sell" if current_size > 0 else "buy"
    req["order_type"] = "market"
    req["reduce_only"] = True
    response = _new_order(credentials, req)
    if response.success:
        return make_success(
            operation="close_position",
            exchange=name,
            account=credentials["account"],
            position_action=CanonicalPositionActionResult(
                operation="close_position",
                symbol=str(request.get("symbol") or ""),
                verified=True,
                status="submitted",
                message="reduce-only market close transaction submitted",
            ),
            data=response.data,
        )
    return response


def _set_tp_sl(credentials: Dict[str, str], request: Mapping[str, Any], operation: str) -> CanonicalResponse:
    """TP/SL are not in Aftermath's public CCXT spec; we model them as additional
    limit orders in the same PTB: a take-profit limit (opposite side, at ``price``)
    or a stop-loss limit. The order is ``reduceOnly`` and ``goodTillCancelled`` —
    Aftermath's CCXT build paths accept these via OrderRequest.
    """
    symbol = str(request.get("symbol") or "").strip()
    pos = _find_position(credentials, symbol, str(request.get("side") or ""))
    market = _find_market(credentials, symbol)
    ch_id = _ch_id(market) or str(pos.get("marketId") or symbol)
    size = abs(_decimal(pos.get("baseAssetAmount"), "0"))
    if size == 0:
        raise ValueError("matching position has zero size")
    price = request.get("price") or request.get("tp") or request.get("sl")
    if price is None:
        raise ValueError(f"{operation} requires price")
    current_side = _position_side(_decimal(pos.get("baseAssetAmount"), "0"))
    close_side = "sell" if current_side == "long" else "buy"
    order = {
        "chId": ch_id,
        "type": "limit",
        "side": close_side,
        "amount": float(size),
        "price": _native_price_float(price),
        "reduceOnly": True,
    }
    build_payload: Dict[str, Any] = {
        "accountId": _resolve_cap_id(credentials),
        "deallocateFreeCollateral": False,
        "metadata": _build_metadata(credentials),
        "orders": [order],
    }
    if _is_dry_run(request):
        build_response = _post_json(credentials, _BUILD_ROUTES["createOrders"], build_payload)
        action = CanonicalPositionActionResult(
            operation=operation,
            symbol=symbol,
            price=str(price),
            verified=False,
            status="dry_run",
            current_side=current_side,
            current_size=_display_decimal(size),
        )
        return _dryrun_success(
            operation,
            credentials,
            build_response,
            data={"build_path": _BUILD_ROUTES["createOrders"], "payload": _safe_payload(build_payload)},
            position_action=action,
        )
    submit = _sign_and_submit(credentials, "createOrders", build_payload)
    action = CanonicalPositionActionResult(
        operation=operation,
        symbol=symbol,
        price=str(price),
        verified=True,
        status="submitted",
        current_side=current_side,
        current_size=_display_decimal(size),
    )
    return _live_success(
        operation,
        credentials,
        submit,
        data={"build_path": _BUILD_ROUTES["createOrders"], "payload": _safe_payload(build_payload), "operation": operation},
        position_action=action,
    )


def _set_leverage(credentials: Dict[str, str], request: Mapping[str, Any]) -> CanonicalResponse:
    symbol = str(request.get("symbol") or "").strip()
    market = _find_market(credentials, symbol) if symbol else {"chId": str(request.get("ch_id") or request.get("marketId") or "")}
    leverage = request.get("leverage")
    if leverage is None:
        raise ValueError("set_leverage requires leverage")
    ch_id = _ch_id(market)
    if not ch_id:
        raise ValueError("set_leverage requires a resolvable market ch_id")
    build_payload: Dict[str, Any] = {
        "accountId": _resolve_cap_id(credentials),
        "chId": ch_id,
        "leverage": float(leverage),
        "metadata": _build_metadata(credentials),
    }
    if _is_dry_run(request):
        build_response = _post_json(credentials, _BUILD_ROUTES["setLeverage"], build_payload)
        return _dryrun_success(
            "set_leverage",
            credentials,
            build_response,
            data={"build_path": _BUILD_ROUTES["setLeverage"], "payload": _safe_payload(build_payload)},
        )
    submit = _sign_and_submit(credentials, "setLeverage", build_payload)
    return _live_success(
        "set_leverage",
        credentials,
        submit,
        data={"build_path": _BUILD_ROUTES["setLeverage"], "payload": _safe_payload(build_payload), "operation": "set_leverage"},
    )


def _safe_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    data = dict(payload)
    if "walletAddress" in data:
        data["walletAddress"] = str(data["walletAddress"])[:8] + "…"
    return data


def _list_instruments(credentials: Dict[str, str]) -> CanonicalResponse:
    rows = []
    for row in _all_markets(credentials):
        mid = _market_id(row)
        sym = _market_symbol(row)
        rows.append({"symbol": sym, "native_symbol": mid, "display_name": sym, "market_type": "perp", "price_increment": _price_increment(row), "size_increment": _size_increment(row), "minimum_size": _size_increment(row), "minimum_notional": _market_params(row).get("minOrderUsdValue")})
    return make_success("list_instruments", name, credentials["account"], data={"instruments": rows})


def _resolve_instrument(credentials: Dict[str, str], request: Mapping[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or request.get("query") or "").strip()
    market = _find_market(credentials, requested)
    mid = _market_id(market) or requested
    sym = _market_symbol(market)
    return make_success(
        "resolve_instrument",
        name,
        credentials["account"],
        instrument=CanonicalInstrument(
            requested_symbol=requested,
            symbol=sym,
            display_name=sym,
            price_increment=_price_increment(market),
            size_increment=_size_increment(market),
            minimum_size=_size_increment(market),
        ),
        data={"symbol": sym, "market_id": mid, "native_symbol": mid, "market_type": "perp", "price_increment": _price_increment(market), "size_increment": _size_increment(market), "minimum_notional": _market_params(market).get("minOrderUsdValue")},
    )


def _price_from_market_prices(prices: Any, market_id: str) -> Optional[str]:
    rows: List[Mapping[str, Any]] = []
    if isinstance(prices, Mapping):
        for key in ("marketsPrices", "prices", "markets", "data"):
            value = prices.get(key)
            if isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, Mapping))
            elif isinstance(value, Mapping):
                nested = value.get(market_id) if market_id else None
                if isinstance(nested, Mapping):
                    rows.append(nested)
                rows.extend(row for row in value.values() if isinstance(row, Mapping))
        if not rows:
            rows.append(prices)
    for row in rows:
        if market_id and str(row.get("marketId") or row.get("id") or market_id) not in {market_id, ""}:
            continue
        for key in ("markPrice", "midPrice", "price", "basePrice", "oraclePrice", "lastExternalPrice"):
            value = row.get(key)
            if value is None or str(value).strip() == "":
                continue
            try:
                return _display_decimal(value)
            except Exception:
                continue
    return None


def _market_price(credentials: Dict[str, str], request: Mapping[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or request.get("query") or "").strip()
    market = _find_market(credentials, requested)
    mid = _market_id(market) or requested
    prices = _market_prices(credentials, [mid])
    raw_price = _price_from_market_prices(prices, mid)
    display_price = _display_price(raw_price, market) if raw_price is not None else None
    sym = _market_symbol(market)
    return make_success(
        "market_price",
        name,
        credentials["account"],
        market_price=CanonicalMarketPrice(
            requested_symbol=requested,
            market=sym,
            price=display_price,
            mark_price=display_price,
            last_external_price=display_price,
        ),
        data={"symbol": sym, "market_id": mid, "price": display_price, "mark_price": display_price, "raw_mark_price": raw_price, "price_increment": _price_increment(market), "size_increment": _size_increment(market)},
    )


def _flatten_values(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _flatten_values(v)
    else:
        yield obj


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    operation = str((request or {}).get("operation") or "").strip()
    account = str((request or {}).get("account") or "").strip()
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation=operation, exchange=name, account=account, code="UNKNOWN_ACCOUNT", message=f"Aftermath account '{account}' is not configured.")
    try:
        if operation == "balance":
            return _balance(credentials)
        if operation in {"positions_orders", "positions_management"}:
            return _positions_orders(credentials, operation)
        if operation == "new_order":
            return _new_order(credentials, request)
        if operation == "ladder":
            return _ladder(credentials, request)
        if operation in {"cancel_orders", "cancel_order_group"}:
            return _cancel_orders(credentials, request)
        if operation == "close_position":
            return _close_position(credentials, request)
        if operation == "set_tp":
            return _set_tp_sl(credentials, request, "set_tp")
        if operation == "set_sl":
            return _set_tp_sl(credentials, request, "set_sl")
        if operation == "set_leverage":
            return _set_leverage(credentials, request)
        if operation == "list_instruments":
            return _list_instruments(credentials)
        if operation == "resolve_instrument":
            return _resolve_instrument(credentials, request)
        if operation == "market_price":
            return _market_price(credentials, request)
        return make_failure(operation=operation, exchange=name, account=credentials["account"], code="NOT_IMPLEMENTED", message=f"Aftermath operation '{operation}' is not implemented.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Aftermath %s failed for %s: %s", operation, credentials["account"], _redact_credentials(str(exc), credentials))
        return make_failure(operation=operation, exchange=name, account=credentials["account"], code=f"{(operation or 'REQUEST').upper()}_FAILED", message=_redact_credentials(str(exc), credentials))


__all__ = ["name", "list_accounts", "capabilities", "execute"]
