"""Rise exchange agent.

This module owns Rise-specific behavior for the /trade stack.

Current scope:
- Credential discovery from ``RISE_<ALIAS>_WALLET`` and
  ``RISE_<ALIAS>_APISIGNERPRIVATE`` in the live environment or
  ``$HERMES_HOME/.env``.
- Read-only portfolio retrieval through Rise's documented REST endpoints.
- Canonical conversion into the exchange-agnostic TradeDesk / wizard contract.
- Limit-order submission for the generic New Order flow.

TradeDesk and the Telegram wizard MUST remain exchange-agnostic and must not
parse ``RISE_*`` environment variables or Rise-native payloads.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from eth_abi.abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

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

logger = logging.getLogger(__name__)

name = "rise"

DEFAULT_API_BASE = "https://api.rise.trade"
API_TIMEOUT_SECONDS = 20
RISE_ORDER_TYPE_LIMIT = "limit"
RISE_ORDER_TYPE_MARKET = "market"
RISE_ORDER_TYPE_TO_INT = {RISE_ORDER_TYPE_MARKET: 0, RISE_ORDER_TYPE_LIMIT: 1}
RISE_SIDE_TO_INT = {"buy": 0, "sell": 1}
RISE_TIF_TO_INT = {"GTC": 0, "GTT": 1, "FOK": 2, "IOC": 3}
RISE_TPSL_SIDE_TO_INT = {"BUY": 0, "SELL": 1}
RISE_TPSL_STOP_TYPE_TO_INT = {"TAKE_PROFIT": 0, "STOP_LOSS": 1, "STOP_TYPE_NONE": 2}
RISE_TPSL_ORDER_TYPE_TO_INT = {"MARKET": 0, "LIMIT": 1}
RISE_TPSL_STOP_PRICE_OPTION_TO_INT = {"LAST_TRADED_PRICE": 0, "MARK_PRICE": 1, "PRICE_OPTION_NONE": 2}
RISE_STP_DEFAULT = 0
RISE_ORDER_DEADLINE_SECONDS = 300
RISE_EIP712_NAME = "RISEx"
RISE_EIP712_VERSION = "1"
RISE_CHAIN_ID = 4153
RISE_HEADER_VERSION = 1
ACTION_PLACE_ORDER = "RISE_PERPS_PLACE_ORDER_V1"
ACTION_CANCEL_ORDER = "RISE_PERPS_CANCEL_ORDER_V1"
RISE_EIP712_VERIFYING_CONTRACT = "0x0d919daa3f12ae715744eb648c00066c5dbd66f0"
RISE_ROUTER_ADDRESS = "0xaadde0cea454f2bcb26f46ed54c5709b7bb34a7e"

_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_WALLET_HEX_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")
_DEPRECATED_SUFFIX_RE = re.compile(r"\s*\[[^\]]*\]\s*$")


class _RiseHTTPError(RuntimeError):
    def __init__(self, *, status: int, path: str, body: str):
        self.status = status
        self.path = path
        self.body = body
        super().__init__(f"HTTP {status} on {path}: {body[:200]}")


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


def _read_env(name: str) -> str:
    live = os.environ.get(name, "").strip()
    if live:
        return live
    return _load_dotenv_values(_hermes_home() / ".env").get(name, "").strip()


def _discover_accounts() -> List[str]:
    env_values: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith("RISE_"):
            env_values[key] = (value or "").strip()
    for key, value in _load_dotenv_values(_hermes_home() / ".env").items():
        if key.startswith("RISE_"):
            env_values.setdefault(key, (value or "").strip())

    wallets: Dict[str, str] = {}
    signer_keys: Dict[str, str] = {}
    for key, value in env_values.items():
        if not (key.startswith("RISE_") and key.count("_") >= 2):
            continue
        remainder = key[len("RISE_"):]
        if remainder.endswith("_WALLET"):
            alias = remainder[:-len("_WALLET")]
            field = "WALLET"
        elif remainder.endswith("_APISIGNERPRIVATE"):
            alias = remainder[:-len("_APISIGNERPRIVATE")]
            field = "APISIGNERPRIVATE"
        else:
            continue
        if not alias or not _ALIAS_PATTERN.match(alias) or not value:
            continue
        if field == "WALLET":
            wallets[alias] = value
        else:
            signer_keys[alias] = value

    valid: List[str] = []
    for alias in sorted(wallets.keys() & signer_keys.keys()):
        if _WALLET_HEX_PATTERN.match(wallets[alias]):
            valid.append(alias)
    return valid


def _lookup_credentials(alias: str) -> Tuple[Optional[str], Optional[str]]:
    alias_upper = (alias or "").strip().upper()
    if not _ALIAS_PATTERN.match(alias_upper):
        return (None, None)
    wallet = _read_env(f"RISE_{alias_upper}_WALLET")
    signer_private = _read_env(f"RISE_{alias_upper}_APISIGNERPRIVATE")
    if not wallet or not signer_private or not _WALLET_HEX_PATTERN.match(wallet):
        return (None, None)
    return (wallet, signer_private)


def list_accounts() -> List[str]:
    return _discover_accounts()


def capabilities() -> List[str]:
    return ["balance", "positions_orders", "positions_management", "new_order", "ladder", "cancel_orders"]


def _api_base() -> str:
    return (_read_env("RISE_API_BASE") or DEFAULT_API_BASE).rstrip("/")


def _http_request(url: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Any:
    headers = {"User-Agent": "curl/8.5.0"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            text = resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = ""
        raise _RiseHTTPError(
            status=int(exc.code),
            path=urllib.parse.urlparse(url).path,
            body=body or str(exc.reason),
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Rise API unreachable: {exc.reason}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Rise API returned invalid JSON") from exc


def _get_json(url: str) -> Any:
    return _http_request(url, method="GET")


def _post_json(url: str, payload: Dict[str, Any]) -> Any:
    return _http_request(url, method="POST", payload=payload)


def _require_string(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RuntimeError(f"Rise API response missing {field_name}")
    return text


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except Exception:  # noqa: BLE001
        return None


def _decimal_text(value: Any) -> str:
    decimal_value = _decimal_or_none(value)
    if decimal_value is None:
        return "0"
    rendered = format(decimal_value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".") or "0"
    return rendered


def _format_decimal_places(value: Decimal, places: int) -> str:
    quantum = Decimal("1").scaleb(-places)
    quantized = value.quantize(quantum, rounding=ROUND_HALF_UP)
    return format(quantized, "f")


def _step_precision(step_value: Any) -> int:
    decimal_value = _decimal_or_none(step_value)
    if decimal_value is None:
        return 0
    exponent = decimal_value.normalize().as_tuple().exponent
    if not isinstance(exponent, int):
        return 0
    return max(0, -exponent)


def _nonzero_decimal_text(value: Any) -> Optional[str]:
    decimal_value = _decimal_or_none(value)
    if decimal_value is None or decimal_value == 0:
        return None
    return _decimal_text(decimal_value)


def _normalized_money_value(value: Any, field_name: str) -> str:
    return normalize_balance(_require_string(value, field_name), "USDC").value


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"", "0", "false", "no", "off", "none", "null"}:
        return False
    return bool(value)


def _extract_portfolio_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Rise portfolio response missing data")
    return data


def _extract_portfolio_summary(data: Dict[str, Any]) -> CanonicalPortfolioSummary:
    summary = data.get("summary")
    if not isinstance(summary, dict):
        raise RuntimeError("Rise portfolio response missing data.summary")
    return CanonicalPortfolioSummary(
        account_value=_normalized_money_value(summary.get("total_account_value"), "summary.total_account_value"),
        withdrawable=_normalized_money_value(summary.get("free_collateral"), "summary.free_collateral"),
        margin_used=_normalized_money_value(summary.get("total_initial_margin"), "summary.total_initial_margin"),
        total_position_value=_normalized_money_value(summary.get("total_notional"), "summary.total_notional"),
        unit="USDC",
    )


def _rise_side(raw_side: Any, size: Any) -> str:
    side_text = str(raw_side).strip().lower()
    if side_text in {"1", "short", "sell", "s"}:
        return "short"
    if side_text in {"0", "long", "buy", "b"}:
        return "long"
    decimal_size = _decimal_or_none(size)
    if decimal_size is not None and decimal_size < 0:
        return "short"
    return "long"


def _rise_order_side(raw_side: Any) -> str:
    side_text = str(raw_side).strip().lower()
    if side_text in {"1", "sell", "s"}:
        return "sell"
    return "buy"


def _rise_symbol(raw_market_name: Any) -> str:
    market_name = _DEPRECATED_SUFFIX_RE.sub("", str(raw_market_name or "").strip())
    if "/" in market_name:
        market_name = market_name.split("/", 1)[0]
    return market_name.strip().upper() or "UNKNOWN"


def _tpsl_closing_side_for_position(side: str) -> str:
    return "SELL" if side == "long" else "BUY"


def _extract_tpsl_orders(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    orders = payload.get("orders")
    if not isinstance(orders, list):
        data = payload.get("data")
        if isinstance(data, dict):
            orders = data.get("orders")
    if not isinstance(orders, list):
        return []
    rows: List[Dict[str, Any]] = []
    for item in orders:
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _accepted_tpsl_orders(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        order
        for order in _extract_tpsl_orders(payload)
        if str(order.get("status") or "").strip().upper() == "TPSL_ORDER_STATUS_ACCEPTED"
    ]


def _index_tpsl_orders(
    payload: Dict[str, Any],
    market_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[Tuple[str, str], Dict[str, List[Dict[str, Any]]]]:
    indexed: Dict[Tuple[str, str], Dict[str, List[Dict[str, Any]]]] = {}
    for order in _accepted_tpsl_orders(payload):
        market_id = str(order.get("market_id") or "").strip()
        side = str(order.get("side") or "").strip().upper()
        if not market_id or side not in {"BUY", "SELL"}:
            continue
        bucket = indexed.setdefault((market_id, side), {"tp": [], "sl": []})
        stop_type = str(order.get("stop_type") or "").strip().upper()
        market = market_cache.get(market_id) if isinstance(market_cache, dict) else None
        precision = _step_precision(market.get("step_price")) if isinstance(market, dict) else 0
        stop_price = _decimal_or_none(order.get("stop_price"))
        normalized = dict(order)
        if stop_price is not None:
            normalized["_normalized_stop_price"] = _format_decimal_places(stop_price, precision) if precision > 0 else _decimal_text(stop_price)
        if stop_type == "TAKE_PROFIT":
            bucket["tp"].append(normalized)
        elif stop_type == "STOP_LOSS":
            bucket["sl"].append(normalized)
    return indexed


def _normalize_positions(
    data: Dict[str, Any],
    market_cache: Optional[Dict[str, Dict[str, Any]]] = None,
    tpsl_index: Optional[Dict[Tuple[str, str], Dict[str, List[Dict[str, Any]]]]] = None,
) -> List[CanonicalPosition]:
    raw_positions = data.get("positions")
    if not isinstance(raw_positions, list):
        return []
    positions: List[CanonicalPosition] = []
    for item in raw_positions:
        if not isinstance(item, dict):
            continue
        raw_size = _decimal_or_none(item.get("size"))
        if raw_size is None or raw_size == 0:
            continue
        market = None
        if isinstance(market_cache, dict):
            market = market_cache.get(str(item.get("market_id") or "").strip())
        size_precision = _step_precision(market.get("step_size")) if isinstance(market, dict) else 0
        price_precision = _step_precision(market.get("step_price")) if isinstance(market, dict) else 0
        size_text = _format_decimal_places(abs(raw_size), size_precision) if size_precision > 0 else _decimal_text(abs(raw_size))
        entry_price = _decimal_or_none(item.get("avg_entry_price"))
        entry_price_text = _format_decimal_places(entry_price, price_precision) if entry_price is not None and price_precision > 0 else str(item.get("avg_entry_price") or "0")
        pnl_value = _decimal_or_none(item.get("unrealized_pnl"))
        pnl_text = _format_decimal_places(pnl_value, 2) if pnl_value is not None else str(item.get("unrealized_pnl") or "0")
        side = _rise_side(item.get("side"), item.get("size"))
        protections = {"tp": [], "sl": []}
        if isinstance(tpsl_index, dict):
            key = (str(item.get("market_id") or "").strip(), _tpsl_closing_side_for_position(side))
            protections = tpsl_index.get(key, protections)
        tp_orders = list(protections.get("tp") or [])
        sl_orders = list(protections.get("sl") or [])
        tp_price = tp_orders[0].get("_normalized_stop_price") if tp_orders else None
        sl_price = sl_orders[0].get("_normalized_stop_price") if sl_orders else None
        positions.append(
            CanonicalPosition(
                symbol=_rise_symbol(item.get("market_name")),
                side=side,
                size=size_text,
                entry_price=entry_price_text,
                pnl=pnl_text,
                tp=str(tp_price) if tp_price not in (None, "") else None,
                sl=str(sl_price) if sl_price not in (None, "") else None,
                tp_count=len(tp_orders) or None,
                sl_count=len(sl_orders) or None,
            )
        )
    positions.sort(key=lambda item: (item.symbol, item.side))
    return positions


def _fetch_portfolio(wallet: str) -> Dict[str, Any]:
    query = urllib.parse.urlencode({"account": wallet})
    payload = _get_json(f"{_api_base()}/v1/portfolio/details?{query}")
    if not isinstance(payload, dict):
        raise RuntimeError("Rise portfolio response was not an object")
    return payload


def _fetch_markets_payload() -> Dict[str, Any]:
    payload = _get_json(f"{_api_base()}/v1/markets")
    if not isinstance(payload, dict):
        raise RuntimeError("Rise markets response was not an object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Rise markets response missing data")
    return data


def _fetch_system_config() -> Dict[str, Any]:
    payload = _get_json(f"{_api_base()}/v1/system/config")
    if not isinstance(payload, dict):
        raise RuntimeError("Rise system config response was not an object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Rise system config missing data")
    return data


def _fetch_eip712_domain() -> Dict[str, Any]:
    payload = _get_json(f"{_api_base()}/v1/auth/eip712-domain")
    if not isinstance(payload, dict):
        raise RuntimeError("Rise EIP-712 domain response was not an object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Rise EIP-712 domain missing data")
    return data


def _fetch_nonce_state(wallet: str) -> Dict[str, Any]:
    payload = _get_json(f"{_api_base()}/v1/nonce-state/{wallet}")
    if not isinstance(payload, dict):
        raise RuntimeError("Rise nonce-state response was not an object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Rise nonce-state response missing data")
    return data


def _fetch_open_orders_payload(wallet: str) -> Dict[str, Any]:
    query = urllib.parse.urlencode({"account": wallet})
    payload = _get_json(f"{_api_base()}/v1/orders/open?{query}")
    if not isinstance(payload, dict):
        raise RuntimeError("Rise open orders response was not an object")
    return payload


def _fetch_tpsl_orders_payload(wallet: str, market_id: Optional[str] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "account": wallet,
        "limit": 1000,
        "statuses": ["TPSL_ORDER_STATUS_ACCEPTED"],
    }
    if market_id:
        params["market_id"] = market_id
    query = urllib.parse.urlencode(params, doseq=True)
    payload = _get_json(f"{_api_base()}/v1/orders/tpsl?{query}")
    if not isinstance(payload, dict):
        raise RuntimeError("Rise TP/SL orders response was not an object")
    return payload


def _market_cache(markets_payload: Dict[str, Any], portfolio_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    cache: Dict[str, Dict[str, Any]] = {}
    raw_markets = markets_payload.get("markets")
    if isinstance(raw_markets, list):
        for market in raw_markets:
            if not isinstance(market, dict):
                continue
            market_id = str(market.get("market_id") or "").strip()
            raw_config = market.get("config")
            config: Dict[str, Any] = raw_config if isinstance(raw_config, dict) else {}
            if not market_id:
                continue
            cache[market_id] = {
                "market_id": market_id,
                "symbol": _rise_symbol(config.get("name") or market.get("display_name") or market.get("base_asset_symbol")),
                "raw_name": str(config.get("name") or market.get("display_name") or market.get("base_asset_symbol") or "").strip(),
                "step_size": _require_string(config.get("step_size") or "0", f"markets[{market_id}].config.step_size"),
                "step_price": _require_string(config.get("step_price") or "0", f"markets[{market_id}].config.step_price"),
                "min_order_size": str(config.get("min_order_size") or "0").strip() or "0",
                "active": bool(market.get("active", True)),
            }
    raw_positions = portfolio_data.get("positions")
    if isinstance(raw_positions, list):
        for position in raw_positions:
            if not isinstance(position, dict):
                continue
            market_id = str(position.get("market_id") or "").strip()
            if not market_id or market_id in cache:
                continue
            cache[market_id] = {
                "market_id": market_id,
                "symbol": _rise_symbol(position.get("market_name")),
                "raw_name": str(position.get("market_name") or "").strip(),
                "step_size": "1",
                "step_price": "1",
                "min_order_size": "0",
                "active": True,
            }
    return cache


def _resolve_market_by_symbol(symbol: str, market_cache: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    requested = (symbol or "").strip().upper()
    if not requested:
        return None
    matches: List[Dict[str, Any]] = []
    for market in market_cache.values():
        if str(market.get("symbol") or "").strip().upper() != requested:
            continue
        matches.append(market)
    if not matches:
        return None
    matches.sort(
        key=lambda market: (
            0 if bool(market.get("active")) else 1,
            1 if "deprecated" in str(market.get("raw_name") or "").lower() else 0,
            int(str(market.get("market_id") or "0") or "0"),
        )
    )
    return matches[0]


def _price_precision(step_price: Any) -> int:
    decimal_value = _decimal_or_none(step_price)
    if decimal_value is None:
        return 0
    exponent = decimal_value.normalize().as_tuple().exponent
    if not isinstance(exponent, int):
        return 0
    return max(0, -exponent)


def _normalize_open_orders(open_orders_payload: Dict[str, Any], market_cache: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    data = open_orders_payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Rise open orders response missing data")
    orders = data.get("orders")
    if not isinstance(orders, list):
        return []
    rows: List[Dict[str, Any]] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        market_id = str(order.get("market_id") or "").strip()
        market = market_cache.get(market_id)
        if not market:
            continue
        step_size = _decimal_or_none(market.get("step_size"))
        step_price = _decimal_or_none(market.get("step_price"))
        size_steps = _decimal_or_none(order.get("size_steps"))
        price_ticks = _decimal_or_none(order.get("price_ticks"))
        if step_size is None or step_price is None or size_steps is None or price_ticks is None:
            continue
        size = size_steps * step_size
        price = price_ticks * step_price
        if size <= 0 or price <= 0:
            continue
        rows.append(
            {
                "order_id": str(order.get("order_id") or "").strip(),
                "wide_order_id": str(order.get("wide_order_id") or "").strip(),
                "resting_order_id": str(order.get("resting_order_id") or "").strip(),
                "market_id": market_id,
                "symbol": str(market.get("symbol") or "UNKNOWN"),
                "side": _rise_order_side(order.get("side")),
                "side_int": int(str(order.get("side") or 0)),
                "size": size,
                "price": price,
                "size_steps": int(size_steps),
                "price_ticks": int(price_ticks),
                "price_precision": _price_precision(market.get("step_price")),
                "reduce_only": bool(order.get("reduce_only")),
                "post_only": bool(order.get("post_only")),
                "order_type": str(order.get("order_type") or "").strip(),
                "time_in_force": str(order.get("time_in_force") or "").strip(),
            }
        )
    return rows


def _aggregate_open_orders(orders: List[Dict[str, Any]]) -> List[CanonicalOrderGroup]:
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for order in orders:
        key = (order["symbol"], order["side"])
        group = grouped.setdefault(
            key,
            {
                "symbol": order["symbol"],
                "side": order["side"],
                "order_count": 0,
                "total_size": Decimal("0"),
                "notional": Decimal("0"),
                "min_price": None,
                "max_price": None,
                "price_precision": int(order.get("price_precision") or 0),
            },
        )
        group["order_count"] += 1
        group["total_size"] += order["size"]
        group["notional"] += order["price"] * order["size"]
        if group["min_price"] is None or order["price"] < group["min_price"]:
            group["min_price"] = order["price"]
        if group["max_price"] is None or order["price"] > group["max_price"]:
            group["max_price"] = order["price"]

    rows: List[CanonicalOrderGroup] = []
    for group in grouped.values():
        total_size: Decimal = group["total_size"]
        notional: Decimal = group["notional"]
        vwap = notional / total_size if total_size != 0 else Decimal("0")
        precision = int(group.get("price_precision") or 0)
        rows.append(
            CanonicalOrderGroup(
                symbol=group["symbol"],
                side=group["side"],
                order_count=group["order_count"],
                total_size=_decimal_text(total_size),
                vwap=_format_decimal_places(vwap, precision),
                min_price=_decimal_text(group["min_price"]),
                max_price=_decimal_text(group["max_price"]),
            )
        )
    rows.sort(key=lambda item: (item.symbol, item.side))
    return rows


def _rise_quantize_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("Rise step must be positive.")
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def _rise_steps(value: Decimal, step: Decimal) -> int:
    if step <= 0:
        raise ValueError("Rise step must be positive.")
    return int((value / step).to_integral_value(rounding=ROUND_DOWN))


def _rise_order_deadline() -> int:
    return int(time.time()) + RISE_ORDER_DEADLINE_SECONDS


def _rise_to_checksum_address(value: str) -> str:
    return to_checksum_address(value)


def _rise_eip712_domain_verifying_contract() -> str:
    return RISE_EIP712_VERIFYING_CONTRACT


def _rise_router_address() -> str:
    return RISE_ROUTER_ADDRESS


def _rise_encode_order_action_hash(
    *,
    market_id: int,
    size_steps: int,
    price_ticks: int,
    side: int,
    order_type: int,
    time_in_force: int,
    post_only: bool,
    reduce_only: bool,
    stp_mode: int,
) -> bytes:
    action_hash_id = keccak(ACTION_PLACE_ORDER.encode("utf-8"))
    order_flags = 0
    if int(side) & 1:
        order_flags |= 1
    if post_only:
        order_flags |= 2
    if reduce_only:
        order_flags |= 4
    order_flags |= (int(stp_mode) & 3) << 3
    order_flags |= (int(order_type) & 1) << 5
    order_flags |= (int(time_in_force) & 3) << 6

    data = 0
    data |= (int(market_id) & 0xFFFF) << 70
    data |= (int(size_steps) & 0xFFFFFFFF) << 38
    data |= (int(price_ticks) & 0xFFFFFF) << 14
    data |= (order_flags & 0xFF) << 6
    data |= (RISE_HEADER_VERSION & 0x1F) << 1

    encoded = abi_encode(
        ["bytes32", "uint8", "uint256", "uint16", "uint64", "uint16"],
        [
            action_hash_id,
            1,
            data,
            0,
            0,
            0,
        ],
    )
    return keccak(encoded)


def _rise_sign_eip712_verify_witness(
    *,
    signer_private_key: str,
    domain_separator: bytes,
    account: str,
    target: str,
    action_hash: bytes,
    nonce_anchor: int,
    nonce_bitmap_index: int,
    deadline: int,
) -> bytes:
    _unused_domain_separator = domain_separator
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "VerifyWitness": [
                {"name": "account", "type": "address"},
                {"name": "target", "type": "address"},
                {"name": "hash", "type": "bytes32"},
                {"name": "nonceAnchor", "type": "uint48"},
                {"name": "nonceBitmap", "type": "uint8"},
                {"name": "deadline", "type": "uint32"},
            ],
        },
        "primaryType": "VerifyWitness",
        "domain": {
            "name": RISE_EIP712_NAME,
            "version": RISE_EIP712_VERSION,
            "chainId": RISE_CHAIN_ID,
            "verifyingContract": _rise_to_checksum_address(_rise_eip712_domain_verifying_contract()),
        },
        "message": {
            "account": _rise_to_checksum_address(account),
            "target": _rise_to_checksum_address(target),
            "hash": "0x" + action_hash.hex(),
            "nonceAnchor": int(nonce_anchor),
            "nonceBitmap": int(nonce_bitmap_index),
            "deadline": int(deadline),
        },
    }
    signable = encode_typed_data(full_message=typed)
    signed = Account.sign_message(signable, private_key=signer_private_key)
    return bytes(signed.signature)


def _rise_sig_to_base64(signature: bytes) -> str:
    return base64.b64encode(signature).decode("ascii")


def _extract_exchange_order_id(order: Dict[str, Any]) -> Optional[int]:
    for key in ("resting_order_id", "wide_order_id", "order_id"):
        raw = str(order.get(key) or "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except Exception:  # noqa: BLE001
            continue
    return None


def _rise_sign_place_tpsl_order(
    *,
    signer_private_key: str,
    account: str,
    market_id: int,
    side: str,
    size: str,
    stop_type: str,
    stop_price: str,
    limit_price: str,
    order_type: str,
    stop_price_option: str,
    tif: str,
    deadline: int,
    size_percent_bps: int,
) -> bytes:
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "PlaceTpslOrder": [
                {"name": "account", "type": "address"},
                {"name": "marketId", "type": "uint64"},
                {"name": "side", "type": "uint8"},
                {"name": "size", "type": "string"},
                {"name": "stopType", "type": "uint8"},
                {"name": "stopPrice", "type": "string"},
                {"name": "limitPrice", "type": "string"},
                {"name": "orderType", "type": "uint8"},
                {"name": "stopPriceOption", "type": "uint8"},
                {"name": "tif", "type": "uint8"},
                {"name": "deadline", "type": "uint32"},
                {"name": "sizePercentBps", "type": "uint32"},
            ],
        },
        "primaryType": "PlaceTpslOrder",
        "domain": {
            "name": RISE_EIP712_NAME,
            "version": RISE_EIP712_VERSION,
            "chainId": RISE_CHAIN_ID,
            "verifyingContract": _rise_to_checksum_address(_rise_eip712_domain_verifying_contract()),
        },
        "message": {
            "account": _rise_to_checksum_address(account),
            "marketId": int(market_id),
            "side": int(RISE_TPSL_SIDE_TO_INT[side]),
            "size": size,
            "stopType": int(RISE_TPSL_STOP_TYPE_TO_INT[stop_type]),
            "stopPrice": stop_price,
            "limitPrice": limit_price,
            "orderType": int(RISE_TPSL_ORDER_TYPE_TO_INT[order_type]),
            "stopPriceOption": int(RISE_TPSL_STOP_PRICE_OPTION_TO_INT[stop_price_option]),
            "tif": int(RISE_TIF_TO_INT[tif]),
            "deadline": int(deadline),
            "sizePercentBps": int(size_percent_bps),
        },
    }
    signable = encode_typed_data(full_message=typed)
    signed = Account.sign_message(signable, private_key=signer_private_key)
    return bytes(signed.signature)


def _rise_sign_cancel_tpsl_order(*, signer_private_key: str, account: str, order_id: str, deadline: int) -> bytes:
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "CancelTpslOrder": [
                {"name": "account", "type": "address"},
                {"name": "orderId", "type": "string"},
                {"name": "deadline", "type": "uint32"},
            ],
        },
        "primaryType": "CancelTpslOrder",
        "domain": {
            "name": RISE_EIP712_NAME,
            "version": RISE_EIP712_VERSION,
            "chainId": RISE_CHAIN_ID,
            "verifyingContract": _rise_to_checksum_address(_rise_eip712_domain_verifying_contract()),
        },
        "message": {
            "account": _rise_to_checksum_address(account),
            "orderId": order_id,
            "deadline": int(deadline),
        },
    }
    signable = encode_typed_data(full_message=typed)
    signed = Account.sign_message(signable, private_key=signer_private_key)
    return bytes(signed.signature)


def _rise_position_action_result(
    *,
    operation: str,
    symbol: str,
    verified: bool,
    price: Optional[str] = None,
    removed: Optional[bool] = None,
    status: str = "success",
    current_side: Optional[str] = None,
    current_size: Optional[str] = None,
    message: Optional[str] = None,
) -> CanonicalPositionActionResult:
    return CanonicalPositionActionResult(
        operation=operation,
        symbol=symbol,
        verified=verified,
        price=price,
        removed=removed,
        status=status,
        current_side=current_side,
        current_size=current_size,
        message=message,
    )


def _find_rise_position_context(
    *,
    wallet: str,
    requested_symbol: str,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any], Dict[Tuple[str, str], Dict[str, List[Dict[str, Any]]]], Dict[str, Any], str, str]:
    portfolio_payload = _fetch_portfolio(wallet)
    portfolio_data = _extract_portfolio_payload(portfolio_payload)
    markets_payload = _fetch_markets_payload()
    cache = _market_cache(markets_payload, portfolio_data)
    market = _resolve_market_by_symbol(requested_symbol, cache)
    if market is None:
        raise ValueError("INSTRUMENT_NOT_FOUND")
    market_id = str(market.get("market_id") or "").strip()
    raw_positions = portfolio_data.get("positions") if isinstance(portfolio_data.get("positions"), list) else []
    current_position = None
    for item in raw_positions:
        if not isinstance(item, dict):
            continue
        if str(item.get("market_id") or "").strip() != market_id:
            continue
        raw_size = _decimal_or_none(item.get("size"))
        if raw_size is None or raw_size == 0:
            continue
        current_position = item
        break
    if current_position is None:
        raise ValueError("POSITION_NOT_FOUND")
    current_side = _rise_side(current_position.get("side"), current_position.get("size"))
    current_size_raw = _decimal_or_none(current_position.get("size"))
    size_precision = _step_precision(market.get("step_size"))
    if current_size_raw is None:
        raise RuntimeError("Rise position size missing")
    current_size = _format_decimal_places(abs(current_size_raw), size_precision) if size_precision > 0 else _decimal_text(abs(current_size_raw))
    tpsl_payload = _fetch_tpsl_orders_payload(wallet, market_id)
    tpsl_index = _index_tpsl_orders(tpsl_payload, cache)
    return cache, market, tpsl_index, current_position, current_side, current_size


def _verify_rise_tpsl_state(
    *,
    wallet: str,
    market_id: str,
    closing_side: str,
    stop_type: str,
    expected_price: Optional[str],
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    payload = _fetch_tpsl_orders_payload(wallet, market_id)
    indexed = _index_tpsl_orders(payload)
    bucket = indexed.get((market_id, closing_side), {"tp": [], "sl": []})
    target_key = "tp" if stop_type == "TAKE_PROFIT" else "sl"
    orders = list(bucket.get(target_key) or [])
    if expected_price is None:
        return len(orders) == 0, None
    expected_decimal = _decimal_or_none(expected_price)
    for order in orders:
        order_price_text = str(order.get("_normalized_stop_price") or order.get("stop_price") or "").strip()
        if order_price_text == expected_price:
            return True, order
        if expected_decimal is not None and _decimal_or_none(order_price_text) == expected_decimal:
            return True, order
    return False, None


def _execute_set_tpsl(account: str, request: Dict[str, Any], *, stop_type: str, operation: str) -> CanonicalResponse:
    wallet, signer_private = _lookup_credentials(account)
    if not wallet or not signer_private:
        return make_failure(operation=operation, exchange=name, account=account, code="UNKNOWN_ACCOUNT", message="Unknown or incomplete Rise account credentials.")
    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_price = _decimal_or_none(request.get("price"))
    if not requested_symbol:
        return make_failure(operation=operation, exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_price is None or requested_price < 0:
        return make_failure(operation=operation, exchange=name, account=account, code=("INVALID_TP_PRICE" if operation == "set_tp" else "INVALID_SL_PRICE"), message=("TP price must be numeric and non-negative." if operation == "set_tp" else "SL price must be numeric and non-negative."))
    try:
        cache, market, tpsl_index, _current_position, current_side, current_size = _find_rise_position_context(wallet=wallet, requested_symbol=requested_symbol)
        market_id = str(market.get("market_id") or "").strip()
        price_precision = _step_precision(market.get("step_price"))
        closing_side = _tpsl_closing_side_for_position(current_side)
        bucket = tpsl_index.get((market_id, closing_side), {"tp": [], "sl": []})
        target_key = "tp" if stop_type == "TAKE_PROFIT" else "sl"
        existing_orders = list(bucket.get(target_key) or [])
        if len(existing_orders) > 1:
            return make_failure(operation=operation, exchange=name, account=account, code="AMBIGUOUS_PROTECTION_STATE", message="Multiple matching TP/SL orders were found.")
        existing_order = existing_orders[0] if existing_orders else None
        signer_address = Account.from_key(signer_private).address

        if requested_price == 0:
            if existing_order is None:
                return make_success(operation=operation, exchange=name, account=account, position_action=_rise_position_action_result(operation=operation, symbol=requested_symbol, verified=True, removed=False, current_side=current_side, current_size=current_size, message=("No Take Profit was set." if operation == "set_tp" else "No Stop Loss was set.")))
            deadline = _rise_order_deadline()
            cancel_signature = _rise_sign_cancel_tpsl_order(signer_private_key=signer_private, account=wallet, order_id=str(existing_order.get("order_id") or ""), deadline=deadline)
            cancel_payload = {"order_id": str(existing_order.get("order_id") or ""), "account": wallet, "signer": signer_address, "signature": _rise_sig_to_base64(cancel_signature), "deadline": int(deadline)}
            _post_json(f"{_api_base()}/v1/orders/tpsl/cancel", cancel_payload)
            verified, _order = _verify_rise_tpsl_state(wallet=wallet, market_id=market_id, closing_side=closing_side, stop_type=stop_type, expected_price=None)
            action = _rise_position_action_result(operation=operation, symbol=requested_symbol, verified=verified, removed=True, current_side=current_side, current_size=current_size, message=("Take Profit removed." if operation == "set_tp" else "Stop Loss removed."), status="success" if verified else "failed")
            if verified:
                return make_success(operation=operation, exchange=name, account=account, position_action=action)
            return make_failure(operation=operation, exchange=name, account=account, code="VERIFICATION_FAILED", message="TP/SL removal could not be verified.", position_action=action)

        formatted_price = _format_decimal_places(requested_price, price_precision) if price_precision > 0 else _decimal_text(requested_price)
        if existing_order is not None:
            deadline = _rise_order_deadline()
            cancel_signature = _rise_sign_cancel_tpsl_order(signer_private_key=signer_private, account=wallet, order_id=str(existing_order.get("order_id") or ""), deadline=deadline)
            cancel_payload = {"order_id": str(existing_order.get("order_id") or ""), "account": wallet, "signer": signer_address, "signature": _rise_sig_to_base64(cancel_signature), "deadline": int(deadline)}
            _post_json(f"{_api_base()}/v1/orders/tpsl/cancel", cancel_payload)

        deadline = _rise_order_deadline()
        place_signature = _rise_sign_place_tpsl_order(
            signer_private_key=signer_private,
            account=wallet,
            market_id=int(market_id),
            side=closing_side,
            size=current_size,
            stop_type=stop_type,
            stop_price=formatted_price,
            limit_price="0",
            order_type="MARKET",
            stop_price_option="LAST_TRADED_PRICE",
            tif="GTC",
            deadline=deadline,
            size_percent_bps=10000,
        )
        place_payload = {
            "account": wallet,
            "market_id": market_id,
            "side": closing_side,
            "size": current_size,
            "stop_type": stop_type,
            "order_type": "MARKET",
            "stop_price": formatted_price,
            "limit_price": "0",
            "stop_price_option": "LAST_TRADED_PRICE",
            "tif": "GTC",
            "signer": signer_address,
            "signature": _rise_sig_to_base64(place_signature),
            "deadline": int(deadline),
            "size_percent_bps": 10000,
        }
        _post_json(f"{_api_base()}/v1/orders/tpsl", place_payload)
        verified, _order = _verify_rise_tpsl_state(wallet=wallet, market_id=market_id, closing_side=closing_side, stop_type=stop_type, expected_price=formatted_price)
        action = _rise_position_action_result(operation=operation, symbol=requested_symbol, verified=verified, price=formatted_price, removed=False, current_side=current_side, current_size=current_size, message=("Take Profit updated." if operation == "set_tp" else "Stop Loss updated."), status="success" if verified else "failed")
        if verified:
            return make_success(operation=operation, exchange=name, account=account, position_action=action)
        return make_failure(operation=operation, exchange=name, account=account, code="VERIFICATION_FAILED", message="TP/SL update could not be verified.", position_action=action)
    except ValueError as exc:
        code = str(exc)
        if code == "INSTRUMENT_NOT_FOUND":
            return make_failure(operation=operation, exchange=name, account=account, code=code, message="Instrument not found.")
        if code == "POSITION_NOT_FOUND":
            return make_failure(operation=operation, exchange=name, account=account, code=code, message="No open position was found for that symbol.")
        return make_failure(operation=operation, exchange=name, account=account, code="POSITION_ACTION_FAILED", message=sanitize_error_message(code))
    except _RiseHTTPError as exc:
        return make_failure(operation=operation, exchange=name, account=account, code="POSITION_ACTION_FAILED", message=f"HTTP {exc.status} on {exc.path}: {exc.body[:200]}")
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation=operation, exchange=name, account=account, code="POSITION_ACTION_FAILED", message=sanitize_error_message(str(exc)))


def _verify_new_order_submission(
    *,
    wallet: str,
    market_cache: Dict[str, Dict[str, Any]],
    market_id: str,
    side_int: int,
    size_steps: int,
    price_ticks: int,
    response_order_id: Optional[str],
) -> Tuple[bool, Optional[int]]:
    post_payload = _fetch_open_orders_payload(wallet)
    post_orders = _normalize_open_orders(post_payload, market_cache)
    for order in post_orders:
        if str(order.get("market_id") or "") != market_id:
            continue
        if int(str(order.get("side_int") if order.get("side_int") is not None else -1)) != side_int:
            continue
        if int(order.get("size_steps") or -1) != size_steps:
            continue
        if int(order.get("price_ticks") or -1) != price_ticks:
            continue
        if response_order_id and str(order.get("order_id") or "").strip() != response_order_id:
            continue
        return True, _extract_exchange_order_id(order)
    return False, None


def _extract_response_order_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("order_id", "resting_order_id", "wide_order_id"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    for key in ("order_id", "resting_order_id", "wide_order_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return None


def _ladder_distribution_weights(order_count: int, distribution: str) -> List[Decimal]:
    if order_count <= 0:
        return []
    distribution_key = str(distribution or "").strip().lower()
    if distribution_key == "uniform":
        return [Decimal("1")] * order_count
    if distribution_key != "half_gaussian":
        raise ValueError("UNSUPPORTED_DISTRIBUTION")
    if order_count == 1:
        return [Decimal("1")]
    import math

    weights: List[Decimal] = []
    span = Decimal(order_count - 1)
    for index in range(order_count):
        z = Decimal("3") * (span - Decimal(index)) / span
        weights.append(Decimal(str(math.exp(-(float(z) ** 2) / 2.0))))
    return weights


def _build_ladder_prices(start_price: Decimal, end_price: Decimal, order_count: int, step_price: Decimal) -> List[Decimal]:
    if order_count <= 0:
        return []
    if order_count == 1:
        return [_rise_quantize_to_step((start_price + end_price) / Decimal("2"), step_price)]
    step = (end_price - start_price) / Decimal(order_count - 1)
    prices = [_rise_quantize_to_step(start_price + (step * Decimal(index)), step_price) for index in range(order_count)]
    if start_price <= end_price:
        for index in range(1, len(prices)):
            if prices[index] < prices[index - 1]:
                prices[index] = prices[index - 1]
    else:
        for index in range(1, len(prices)):
            if prices[index] > prices[index - 1]:
                prices[index] = prices[index - 1]
    return prices


def _allocate_ladder_sizes(total_volume: Decimal, order_count: int, step_size: Decimal, distribution: str) -> Tuple[List[Decimal], Decimal]:
    if step_size <= 0:
        raise ValueError("INVALID_INCREMENT")
    total_units = int((total_volume / step_size).to_integral_value(rounding=ROUND_DOWN))
    if total_units < order_count:
        raise ValueError("INSUFFICIENT_VOLUME_FOR_ORDER_COUNT")
    weights = _ladder_distribution_weights(order_count, distribution)
    if not weights:
        raise ValueError("INVALID_ORDER_COUNT")
    total_weight = sum(weights, Decimal("0"))
    if total_weight <= 0:
        raise ValueError("INVALID_DISTRIBUTION")
    raw_units = [Decimal(total_units) * weight / total_weight for weight in weights]
    base_units = [int(unit.to_integral_value(rounding=ROUND_DOWN)) for unit in raw_units]
    residual = total_units - sum(base_units)
    remainders = [raw_units[index] - Decimal(base_units[index]) for index in range(order_count)]
    allocation = list(base_units)
    if residual > 0:
        order_indices = sorted(range(order_count), key=lambda index: (remainders[index], -index), reverse=True)
        for index in order_indices[:residual]:
            allocation[index] += 1
    sizes = [Decimal(units) * step_size for units in allocation]
    return sizes, Decimal(total_units) * step_size


def _build_rise_ladder_children(
    *,
    side: str,
    distribution: str,
    order_count: int,
    total_volume: Decimal,
    start_price: Decimal,
    end_price: Decimal,
    step_size: Decimal,
    step_price: Decimal,
    min_order_size: Decimal,
) -> Tuple[List[Dict[str, Any]], Decimal, int]:
    prices = _build_ladder_prices(start_price, end_price, order_count, step_price)
    sizes, submitted_volume = _allocate_ladder_sizes(total_volume, order_count, step_size, distribution)
    rows: List[Dict[str, Any]] = []
    omitted_below_minimum = 0
    for price, size in zip(prices, sizes):
        if size <= 0:
            continue
        if size < min_order_size:
            omitted_below_minimum += 1
            continue
        if rows and rows[-1]["price"] == price:
            rows[-1]["size"] += size
            continue
        rows.append({"side": side, "price": price, "size": size})
    rows = [row for row in rows if row["size"] > 0]
    rows.sort(key=lambda row: row["price"], reverse=(side == "buy"))
    return rows, submitted_volume, omitted_below_minimum


def _submit_rise_limit_order(
    *,
    wallet: str,
    signer_private: str,
    market_cache: Dict[str, Dict[str, Any]],
    market: Dict[str, Any],
    requested_symbol: str,
    requested_side: str,
    requested_volume: Decimal,
    requested_price: Decimal,
    requested_tif: str,
    reduce_only: bool,
    operation: str,
    account: str,
    verify_after_submit: bool = True,
) -> Tuple[CanonicalResponse, Dict[str, Any], Decimal, Decimal]:
    step_price = _decimal_or_none(market.get("step_price"))
    step_size = _decimal_or_none(market.get("step_size"))
    min_order_size = _decimal_or_none(market.get("min_order_size")) or Decimal("0")
    if step_price is None or step_price <= 0 or step_size is None or step_size <= 0:
        raise RuntimeError("Rise market metadata missing step sizes")

    submitted_price = _rise_quantize_to_step(requested_price, step_price)
    submitted_volume = _rise_quantize_to_step(requested_volume, step_size)
    order_type = RISE_ORDER_TYPE_LIMIT
    if submitted_volume <= 0:
        return make_failure(operation=operation, exchange=name, account=account, code="INVALID_VOLUME", message="Volume must be positive after quantization."), {}, submitted_volume, submitted_price
    if submitted_price <= 0:
        return make_failure(operation=operation, exchange=name, account=account, code="INVALID_PRICE", message="Price must be positive after quantization."), {}, submitted_volume, submitted_price
    if submitted_volume < min_order_size:
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="INVALID_VOLUME",
            message="Order size is below the market minimum after quantization.",
            order=CanonicalOrderResult(
                symbol=requested_symbol,
                side=requested_side,
                order_type=order_type,
                requested_volume=_decimal_text(requested_volume),
                requested_price=_decimal_text(requested_price),
                submitted_volume=_decimal_text(submitted_volume),
                submitted_price=_decimal_text(submitted_price),
                verified=False,
                status="failed",
            ),
        ), {}, submitted_volume, submitted_price

    market_id = int(str(market.get("market_id") or "0"))
    side_int = RISE_SIDE_TO_INT[requested_side]
    order_type_int = RISE_ORDER_TYPE_TO_INT[order_type]
    tif_int = RISE_TIF_TO_INT[requested_tif]
    post_only = False
    size_steps = _rise_steps(submitted_volume, step_size)
    price_ticks = _rise_steps(submitted_price, step_price)

    nonce_state = _fetch_nonce_state(wallet)
    nonce_anchor = int(str(nonce_state.get("nonce_anchor") or 0))
    nonce_bitmap_index = int(str(nonce_state.get("current_bitmap_index") or 0))
    if nonce_bitmap_index > 207:
        nonce_anchor += 1
        nonce_bitmap_index = 0
    deadline = _rise_order_deadline()

    action_hash = _rise_encode_order_action_hash(
        market_id=market_id,
        size_steps=size_steps,
        price_ticks=price_ticks,
        side=side_int,
        order_type=order_type_int,
        time_in_force=tif_int,
        post_only=post_only,
        reduce_only=reduce_only,
        stp_mode=RISE_STP_DEFAULT,
    )
    signature = _rise_sign_eip712_verify_witness(
        signer_private_key=signer_private,
        domain_separator=b"",
        account=wallet,
        target=_rise_router_address(),
        action_hash=action_hash,
        nonce_anchor=nonce_anchor,
        nonce_bitmap_index=nonce_bitmap_index,
        deadline=deadline,
    )
    permit = {
        "account": wallet,
        "signer": Account.from_key(signer_private).address,
        "deadline": int(deadline),
        "nonce_anchor": str(nonce_anchor),
        "nonce_bitmap_index": int(nonce_bitmap_index),
        "signature": _rise_sig_to_base64(signature),
    }
    payload = {
        "market_id": market_id,
        "account": wallet,
        "side": side_int,
        "price_ticks": int(price_ticks),
        "size_steps": int(size_steps),
        "order_type": order_type_int,
        "time_in_force": tif_int,
        "post_only": post_only,
        "reduce_only": reduce_only,
        "stp_mode": RISE_STP_DEFAULT,
        "ttl_units": 0,
        "client_order_id": "0",
        "builder_id": 0,
        "permit": permit,
    }
    try:
        submission_payload = _post_json(f"{_api_base()}/v1/orders/place", payload)
    except _RiseHTTPError as exc:
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="ORDER_SUBMISSION_FAILED",
            message=f"HTTP {exc.status} on {exc.path}: {exc.body[:200]}",
            order=CanonicalOrderResult(
                symbol=requested_symbol,
                side=requested_side,
                order_type=order_type,
                requested_volume=_decimal_text(requested_volume),
                requested_price=_decimal_text(requested_price),
                submitted_volume=_decimal_text(submitted_volume),
                submitted_price=_decimal_text(submitted_price),
                verified=False,
                status="failed",
            ),
        ), payload, submitted_volume, submitted_price

    response_order_id = _extract_response_order_id(submission_payload)
    if verify_after_submit:
        verified, exchange_order_id = _verify_new_order_submission(
            wallet=wallet,
            market_cache=market_cache,
            market_id=str(market_id),
            side_int=side_int,
            size_steps=size_steps,
            price_ticks=price_ticks,
            response_order_id=response_order_id,
        )
    else:
        verified, exchange_order_id = True, None
    result = CanonicalOrderResult(
        symbol=requested_symbol,
        side=requested_side,
        order_type=order_type,
        requested_volume=_decimal_text(requested_volume),
        requested_price=_decimal_text(requested_price),
        submitted_volume=_decimal_text(submitted_volume),
        submitted_price=_decimal_text(submitted_price),
        verified=verified,
        status="success" if verified else "partial",
        exchange_order_id=exchange_order_id,
    )
    if verified:
        return make_success(operation=operation, exchange=name, account=account, order=result), payload, submitted_volume, submitted_price
    return make_failure(operation=operation, exchange=name, account=account, code="VERIFICATION_FAILED", message="Order submission could not be verified.", order=result), payload, submitted_volume, submitted_price


def _verify_rise_ladder_submission(
    *,
    wallet: str,
    market_cache: Dict[str, Dict[str, Any]],
    market_id: str,
    side_int: int,
    expected_payloads: List[Dict[str, Any]],
) -> Tuple[bool, List[int]]:
    post_payload = _fetch_open_orders_payload(wallet)
    post_orders = _normalize_open_orders(post_payload, market_cache)
    matched_ids: List[int] = []
    used_positions: set[int] = set()
    for expected in expected_payloads:
        matched = False
        for index, order in enumerate(post_orders):
            if index in used_positions:
                continue
            if str(order.get("market_id") or "") != market_id:
                continue
            if int(str(order.get("side_int") if order.get("side_int") is not None else -1)) != side_int:
                continue
            if int(order.get("size_steps") or -1) != int(expected.get("size_steps") or -1):
                continue
            if int(order.get("price_ticks") or -1) != int(expected.get("price_ticks") or -1):
                continue
            used_positions.add(index)
            order_id = _extract_exchange_order_id(order)
            if order_id is not None:
                matched_ids.append(order_id)
            matched = True
            break
        if not matched:
            return False, matched_ids
    return True, matched_ids


def _rise_encode_cancel_action_hash(*, market_id: int, resting_order_id: int) -> bytes:
    action_hash_id = keccak(ACTION_CANCEL_ORDER.encode("utf-8"))
    encoded = abi_encode(
        ["bytes32", "uint256", "uint256"],
        [action_hash_id, int(market_id), int(resting_order_id)],
    )
    return keccak(encoded)


def _verify_rise_cancel_submission(
    *,
    wallet: str,
    market_cache: Dict[str, Dict[str, Any]],
    expected_targets: List[Dict[str, Any]],
    non_target_order_ids: List[str],
) -> Tuple[bool, int, int]:
    post_payload = _fetch_open_orders_payload(wallet)
    post_orders = _normalize_open_orders(post_payload, market_cache)
    target_keys = {
        (
            str(order.get("market_id") or ""),
            str(order.get("order_id") or "").strip(),
            str(order.get("resting_order_id") or "").strip(),
        )
        for order in expected_targets
    }
    remaining_target_count = 0
    for order in post_orders:
        key = (
            str(order.get("market_id") or ""),
            str(order.get("order_id") or "").strip(),
            str(order.get("resting_order_id") or "").strip(),
        )
        if key in target_keys:
            remaining_target_count += 1
    confirmed_absent_count = max(0, len(expected_targets) - remaining_target_count)
    post_order_ids = {str(order.get("order_id") or "").strip() for order in post_orders}
    non_target_preserved = all(order_id in post_order_ids for order_id in non_target_order_ids)
    return non_target_preserved and remaining_target_count == 0, confirmed_absent_count, remaining_target_count


def _execute_cancel_order_group(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    wallet, signer_private = _lookup_credentials(account)
    if not wallet or not signer_private:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Rise account credentials.",
        )

    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_side = str(request.get("side") or "").strip().lower()
    if not requested_symbol:
        return make_failure(operation="cancel_order_group", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in {"buy", "sell"}:
        return make_failure(operation="cancel_order_group", exchange=name, account=account, code="INVALID_SIDE", message="Side must be buy or sell.")

    try:
        markets_payload = _fetch_markets_payload()
        cache = _market_cache(markets_payload, {})
        pre_open_payload = _fetch_open_orders_payload(wallet)
        pre_orders = _normalize_open_orders(pre_open_payload, cache)
        target_orders = [
            order
            for order in pre_orders
            if str(order.get("symbol") or "").strip().upper() == requested_symbol
            and str(order.get("side") or "").strip().lower() == requested_side
        ]
        if not target_orders:
            return make_failure(
                operation="cancel_order_group",
                exchange=name,
                account=account,
                code="NO_TARGET_ORDERS",
                message="No matching orders were found.",
                cancel_group=CanonicalCancelGroupResult(
                    symbol=requested_symbol,
                    side=requested_side,
                    targeted_order_count=0,
                    cancelled_order_count=0,
                    confirmed_absent_count=0,
                    remaining_target_count=0,
                    verified=False,
                    partial=False,
                    status="failed",
                    batch_count=0,
                ),
            )

        target_order_ids = {str(order.get("order_id") or "").strip() for order in target_orders}
        non_target_order_ids = [
            str(order.get("order_id") or "").strip()
            for order in pre_orders
            if str(order.get("order_id") or "").strip() not in target_order_ids
        ]
        cancelled_count = 0
        partial = False
        status_code = ""
        status_message = ""
        batches: List[Dict[str, Any]] = []

        for order in target_orders:
            market_id = int(str(order.get("market_id") or 0))
            order_id = str(order.get("order_id") or "").strip()
            resting_order_id = str(order.get("resting_order_id") or "").strip()
            if market_id <= 0 or not order_id or not resting_order_id:
                partial = True
                status_code = "MISSING_ORDER_METADATA"
                status_message = "Rise open orders are missing cancel metadata."
                batches.append({"submitted": 1, "accepted": 0, "ok": False, "reason": status_code})
                break

            nonce_state = _fetch_nonce_state(wallet)
            nonce_anchor = int(str(nonce_state.get("nonce_anchor") or 0))
            nonce_bitmap_index = int(str(nonce_state.get("current_bitmap_index") or 0))
            if nonce_bitmap_index > 207:
                nonce_anchor += 1
                nonce_bitmap_index = 0
            deadline = _rise_order_deadline()
            action_hash = _rise_encode_cancel_action_hash(market_id=market_id, resting_order_id=int(resting_order_id))
            signature = _rise_sign_eip712_verify_witness(
                signer_private_key=signer_private,
                domain_separator=b"",
                account=wallet,
                target=_rise_router_address(),
                action_hash=action_hash,
                nonce_anchor=nonce_anchor,
                nonce_bitmap_index=nonce_bitmap_index,
                deadline=deadline,
            )
            signer_address = Account.from_key(signer_private).address
            permit = {
                "account": wallet,
                "signer": signer_address,
                "deadline": int(deadline),
                "nonce_anchor": str(nonce_anchor),
                "nonce_bitmap_index": int(nonce_bitmap_index),
                "signature": _rise_sig_to_base64(signature),
            }
            payload = {
                "market_id": market_id,
                "order_id": order_id,
                "permit": permit,
            }
            try:
                _post_json(f"{_api_base()}/v1/orders/cancel", payload)
            except _RiseHTTPError as exc:
                partial = cancelled_count > 0
                status_code = "CANCEL_FAILED"
                status_message = f"HTTP {exc.status} on {exc.path}: {exc.body[:200]}"
                batches.append({"submitted": 1, "accepted": 0, "ok": False, "reason": status_code})
                break
            except Exception as exc:  # noqa: BLE001
                partial = cancelled_count > 0
                status_code = "CANCEL_FAILED"
                status_message = sanitize_error_message(str(exc))
                batches.append({"submitted": 1, "accepted": 0, "ok": False, "reason": status_code})
                break
            cancelled_count += 1
            batches.append({"submitted": 1, "accepted": 1, "ok": True})

        verified, confirmed_absent_count, remaining_target_count = _verify_rise_cancel_submission(
            wallet=wallet,
            market_cache=cache,
            expected_targets=target_orders,
            non_target_order_ids=non_target_order_ids,
        )
        cancel_result = CanonicalCancelGroupResult(
            symbol=requested_symbol,
            side=requested_side,
            targeted_order_count=len(target_orders),
            cancelled_order_count=cancelled_count,
            confirmed_absent_count=confirmed_absent_count,
            remaining_target_count=remaining_target_count,
            verified=verified and not partial and cancelled_count == len(target_orders),
            partial=partial or not (verified and cancelled_count == len(target_orders)),
            status="success" if (verified and not partial and cancelled_count == len(target_orders)) else ("partial" if cancelled_count else "failed"),
            batch_count=len(batches),
            batches=batches or None,
        )
        if cancel_result.verified:
            return make_success(
                operation="cancel_order_group",
                exchange=name,
                account=account,
                cancel_group=cancel_result,
            )
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code=status_code or ("VERIFICATION_FAILED" if cancelled_count else "NO_TARGET_ORDERS"),
            message=status_message or "Cancellation was only partially completed.",
            cancel_group=cancel_result,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="CANCEL_FAILED",
            message=sanitize_error_message(str(exc)),
        )


def _normalize_new_order_request(request: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(request, dict):
        return {}
    normalized = dict(request)
    child = normalized.get("child_order")
    if str(normalized.get("operation") or "").strip() == "order" and str(normalized.get("parent_operation") or "").strip() == "place_order" and isinstance(child, dict):
        merged = dict(normalized)
        merged.update(child)
        merged["operation"] = "new_order"
        if "volume" not in merged and "size" in merged:
            merged["volume"] = merged.get("size")
        return merged
    if str(normalized.get("operation") or "").strip() == "place_order":
        normalized["operation"] = "new_order"
    if "volume" not in normalized and "size" in normalized:
        normalized["volume"] = normalized.get("size")
    return normalized


def _execute_balance(account: str) -> CanonicalResponse:
    wallet, _signer_private = _lookup_credentials(account)
    if not wallet:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Rise account credentials.",
        )
    try:
        payload = _fetch_portfolio(wallet)
        data = _extract_portfolio_payload(payload)
        markets_payload = _fetch_markets_payload()
        cache = _market_cache(markets_payload, data)
        tpsl_payload = _fetch_tpsl_orders_payload(wallet)
        tpsl_index = _index_tpsl_orders(tpsl_payload, cache)
        portfolio_summary = _extract_portfolio_summary(data)
        positions = _normalize_positions(data, cache, tpsl_index)
        return make_success(
            operation="balance",
            exchange=name,
            account=account,
            balance=normalize_balance(portfolio_summary.account_value, portfolio_summary.unit),
            portfolio_summary=portfolio_summary,
            positions=positions,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="BALANCE_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )


def _execute_positions_orders(account: str) -> CanonicalResponse:
    wallet, _signer_private = _lookup_credentials(account)
    if not wallet:
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Rise account credentials.",
        )
    try:
        portfolio_payload = _fetch_portfolio(wallet)
        portfolio_data = _extract_portfolio_payload(portfolio_payload)
        markets_payload = _fetch_markets_payload()
        cache = _market_cache(markets_payload, portfolio_data)
        tpsl_payload = _fetch_tpsl_orders_payload(wallet)
        tpsl_index = _index_tpsl_orders(tpsl_payload, cache)
        positions = _normalize_positions(portfolio_data, cache, tpsl_index)
        open_orders_payload = _fetch_open_orders_payload(wallet)
        normalized_orders = _normalize_open_orders(open_orders_payload, cache)
        order_groups = _aggregate_open_orders(normalized_orders)
        return make_success(
            operation="positions_orders",
            exchange=name,
            account=account,
            positions=positions,
            open_order_count=len(normalized_orders),
            order_groups=order_groups,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=account,
            code="POSITIONS_ORDERS_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )


def _execute_new_order(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    wallet, signer_private = _lookup_credentials(account)
    if not wallet or not signer_private:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Rise account credentials.",
        )

    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_side = str(request.get("side") or "").strip().lower()
    order_type = str(request.get("order_type") or RISE_ORDER_TYPE_LIMIT).strip().lower() or RISE_ORDER_TYPE_LIMIT
    requested_volume_text = str(request.get("volume") or request.get("size") or "").strip()
    requested_price_text = str(request.get("price") or "").strip()
    requested_tif = str(request.get("time_in_force") or "GTC").strip().upper() or "GTC"
    reduce_only = _coerce_bool(request.get("reduce_only"))

    if not requested_symbol:
        return make_failure(operation="new_order", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in RISE_SIDE_TO_INT:
        return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_SIDE", message="Side must be buy or sell.")
    if order_type == RISE_ORDER_TYPE_MARKET:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="INVALID_ORDER_TYPE",
            message="Rise market orders are currently disabled.",
        )
    if order_type != RISE_ORDER_TYPE_LIMIT:
        return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_ORDER_TYPE", message="Only limit orders are supported.")
    if requested_tif not in RISE_TIF_TO_INT:
        return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_TIME_IN_FORCE", message="Time in force must be one of GTC, GTT, FOK, or IOC.")

    requested_volume = _decimal_or_none(requested_volume_text)
    requested_price = _decimal_or_none(requested_price_text)
    if requested_volume is None or requested_volume <= 0:
        return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_VOLUME", message="Volume must be positive.")
    if requested_price is None or requested_price <= 0:
        return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_PRICE", message="Price must be positive.")

    try:
        markets_payload = _fetch_markets_payload()
        cache = _market_cache(markets_payload, {})
        market = _resolve_market_by_symbol(requested_symbol, cache)
        if market is None:
            return make_failure(operation="new_order", exchange=name, account=account, code="INSTRUMENT_NOT_FOUND", message="Instrument not found.")

        step_price = _decimal_or_none(market.get("step_price"))
        step_size = _decimal_or_none(market.get("step_size"))
        min_order_size = _decimal_or_none(market.get("min_order_size")) or Decimal("0")
        if step_price is None or step_price <= 0 or step_size is None or step_size <= 0:
            raise RuntimeError("Rise market metadata missing step sizes")

        submitted_price = _rise_quantize_to_step(requested_price, step_price)
        submitted_volume = _rise_quantize_to_step(requested_volume, step_size)
        if submitted_volume <= 0:
            return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_VOLUME", message="Volume must be positive after quantization.")
        if submitted_price <= 0:
            return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_PRICE", message="Price must be positive after quantization.")
        if submitted_volume < min_order_size:
            return make_failure(
                operation="new_order",
                exchange=name,
                account=account,
                code="INVALID_VOLUME",
                message="Order size is below the market minimum after quantization.",
                order=CanonicalOrderResult(
                    symbol=requested_symbol,
                    side=requested_side,
                    order_type=order_type,
                    requested_volume=_decimal_text(requested_volume),
                    requested_price=_decimal_text(requested_price),
                    submitted_volume=_decimal_text(submitted_volume),
                    submitted_price=_decimal_text(submitted_price),
                    verified=False,
                    status="failed",
                ),
            )

        market_id = int(str(market.get("market_id") or "0"))
        side_int = RISE_SIDE_TO_INT[requested_side]
        order_type_int = RISE_ORDER_TYPE_TO_INT[order_type]
        tif_int = RISE_TIF_TO_INT[requested_tif]
        post_only = False
        size_steps = _rise_steps(submitted_volume, step_size)
        price_ticks = _rise_steps(submitted_price, step_price)

        nonce_state = _fetch_nonce_state(wallet)
        nonce_anchor = int(str(nonce_state.get("nonce_anchor") or 0))
        nonce_bitmap_index = int(str(nonce_state.get("current_bitmap_index") or 0))
        if nonce_bitmap_index > 207:
            nonce_anchor += 1
            nonce_bitmap_index = 0
        deadline = _rise_order_deadline()

        action_hash = _rise_encode_order_action_hash(
            market_id=market_id,
            size_steps=size_steps,
            price_ticks=price_ticks,
            side=side_int,
            order_type=order_type_int,
            time_in_force=tif_int,
            post_only=post_only,
            reduce_only=reduce_only,
            stp_mode=RISE_STP_DEFAULT,
        )
        target = _rise_router_address()
        signature = _rise_sign_eip712_verify_witness(
            signer_private_key=signer_private,
            domain_separator=b"",
            account=wallet,
            target=target,
            action_hash=action_hash,
            nonce_anchor=nonce_anchor,
            nonce_bitmap_index=nonce_bitmap_index,
            deadline=deadline,
        )
        signer_address = Account.from_key(signer_private).address
        permit = {
            "account": wallet,
            "signer": signer_address,
            "deadline": int(deadline),
            "nonce_anchor": str(nonce_anchor),
            "nonce_bitmap_index": int(nonce_bitmap_index),
            "signature": _rise_sig_to_base64(signature),
        }
        payload = {
            "market_id": market_id,
            "account": wallet,
            "side": side_int,
            "price_ticks": int(price_ticks),
            "size_steps": int(size_steps),
            "order_type": order_type_int,
            "time_in_force": tif_int,
            "post_only": post_only,
            "reduce_only": reduce_only,
            "stp_mode": RISE_STP_DEFAULT,
            "ttl_units": 0,
            "client_order_id": "0",
            "builder_id": 0,
            "permit": permit,
        }
        _pre_open_orders_payload = _fetch_open_orders_payload(wallet)
        try:
            submission_payload = _post_json(f"{_api_base()}/v1/orders/place", payload)
        except _RiseHTTPError as exc:
            return make_failure(
                operation="new_order",
                exchange=name,
                account=account,
                code="ORDER_SUBMISSION_FAILED",
                message=f"HTTP {exc.status} on {exc.path}: {exc.body[:200]}",
                order=CanonicalOrderResult(
                    symbol=requested_symbol,
                    side=requested_side,
                    order_type=order_type,
                    requested_volume=_decimal_text(requested_volume),
                    requested_price=_decimal_text(requested_price),
                    submitted_volume=_decimal_text(submitted_volume),
                    submitted_price=_decimal_text(submitted_price),
                    verified=False,
                    status="failed",
                ),
            )
        response_order_id = _extract_response_order_id(submission_payload)
        verified, exchange_order_id = _verify_new_order_submission(
            wallet=wallet,
            market_cache=cache,
            market_id=str(market_id),
            side_int=side_int,
            size_steps=size_steps,
            price_ticks=price_ticks,
            response_order_id=response_order_id,
        )
        result = CanonicalOrderResult(
            symbol=requested_symbol,
            side=requested_side,
            order_type=order_type,
            requested_volume=_decimal_text(requested_volume),
            requested_price=_decimal_text(requested_price),
            submitted_volume=_decimal_text(submitted_volume),
            submitted_price=_decimal_text(submitted_price),
            verified=verified,
            status="success" if verified else "partial",
            exchange_order_id=exchange_order_id,
        )
        if verified:
            return make_success(operation="new_order", exchange=name, account=account, order=result)
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="VERIFICATION_FAILED",
            message="Order submission could not be verified.",
            order=result,
        )
    except Exception as exc:  # noqa: BLE001
        submitted_volume = requested_volume if requested_volume is not None else Decimal("0")
        submitted_price = requested_price if requested_price is not None else Decimal("0")
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="ORDER_SUBMISSION_FAILED",
            message=sanitize_error_message(str(exc)),
            order=CanonicalOrderResult(
                symbol=requested_symbol,
                side=requested_side,
                order_type=order_type,
                requested_volume=_decimal_text(requested_volume),
                requested_price=_decimal_text(requested_price),
                submitted_volume=_decimal_text(submitted_volume),
                submitted_price=_decimal_text(submitted_price),
                verified=False,
                status="failed",
            ),
        )


def _execute_ladder(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    wallet, signer_private = _lookup_credentials(account)
    if not wallet or not signer_private:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Rise account credentials.",
        )

    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_side = str(request.get("side") or "").strip().lower()
    distribution = str(request.get("distribution") or "").strip().lower()
    requested_tif = str(request.get("time_in_force") or "GTC").strip().upper() or "GTC"
    try:
        order_count = int(str(request.get("order_count") or "").strip())
    except Exception:
        order_count = 0
    total_volume = _decimal_or_none(request.get("total_volume"))
    start_price = _decimal_or_none(request.get("start_price"))
    end_price = _decimal_or_none(request.get("end_price"))

    if not requested_symbol:
        return make_failure(operation="ladder", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in RISE_SIDE_TO_INT:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_SIDE", message="Side must be buy or sell.")
    if distribution not in {"uniform", "half_gaussian"}:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_DISTRIBUTION", message="Distribution must be uniform or half_gaussian.")
    if requested_tif not in RISE_TIF_TO_INT:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_TIME_IN_FORCE", message="Time in force must be one of GTC, GTT, FOK, or IOC.")
    if order_count <= 0:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_ORDER_COUNT", message="Order count must be positive.")
    if total_volume is None or total_volume <= 0:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_VOLUME", message="Total volume must be positive.")
    if start_price is None or start_price <= 0 or end_price is None or end_price <= 0:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_PRICE", message="Start and end price must be positive.")
    if requested_side == "buy" and end_price >= start_price:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_LADDER_DIRECTION", message="BUY ladders require end price below start price.")
    if requested_side == "sell" and end_price <= start_price:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_LADDER_DIRECTION", message="SELL ladders require end price above start price.")

    try:
        markets_payload = _fetch_markets_payload()
        cache = _market_cache(markets_payload, {})
        market = _resolve_market_by_symbol(requested_symbol, cache)
        if market is None:
            return make_failure(operation="ladder", exchange=name, account=account, code="INSTRUMENT_NOT_FOUND", message="Instrument not found.")
        step_price = _decimal_or_none(market.get("step_price"))
        step_size = _decimal_or_none(market.get("step_size"))
        min_order_size = _decimal_or_none(market.get("min_order_size")) or Decimal("0")
        if step_price is None or step_price <= 0 or step_size is None or step_size <= 0:
            raise RuntimeError("Rise market metadata missing step sizes")

        children, planned_submitted_volume, omitted_below_minimum = _build_rise_ladder_children(
            side=requested_side,
            distribution=distribution,
            order_count=order_count,
            total_volume=total_volume,
            start_price=start_price,
            end_price=end_price,
            step_size=step_size,
            step_price=step_price,
            min_order_size=min_order_size,
        )
        if not children:
            return make_failure(operation="ladder", exchange=name, account=account, code="NO_VALID_CHILDREN", message="No valid ladder children remained after quantization.")

        accepted_payloads: List[Dict[str, Any]] = []
        accepted_count = 0
        accepted_volume = Decimal("0")
        child_order_ids: List[int] = []
        for child in children:
            child_response, payload, child_volume, _child_price = _submit_rise_limit_order(
                wallet=wallet,
                signer_private=signer_private,
                market_cache=cache,
                market=market,
                requested_symbol=requested_symbol,
                requested_side=requested_side,
                requested_volume=child["size"],
                requested_price=child["price"],
                requested_tif=requested_tif,
                reduce_only=False,
                operation="ladder",
                account=account,
                verify_after_submit=False,
            )
            if child_response.success:
                accepted_payloads.append(payload)
                accepted_count += 1
                accepted_volume += child_volume
                if child_response.order and child_response.order.exchange_order_id is not None:
                    child_order_ids.append(child_response.order.exchange_order_id)
                continue
            ladder = CanonicalLadderResult(
                symbol=requested_symbol,
                side=requested_side,
                distribution=distribution,
                requested_order_count=order_count,
                submitted_order_count=accepted_count,
                requested_volume=_decimal_text(total_volume),
                submitted_volume=_decimal_text(accepted_volume),
                batch_count=1,
                verified=False,
                partial=accepted_count > 0,
                status="partial" if accepted_count > 0 else "failed",
                accepted_child_count=accepted_count,
                omitted_order_count=order_count - accepted_count,
                omitted_below_minimum=omitted_below_minimum,
                child_order_ids=child_order_ids or None,
                batches=[{"batch_index": 1, "submitted_order_count": accepted_count, "accepted_child_count": accepted_count, "child_order_ids": child_order_ids}],
            )
            return make_failure(
                operation="ladder",
                exchange=name,
                account=account,
                code=child_response.error.code if child_response.error else "ORDER_SUBMISSION_FAILED",
                message=child_response.error.message if child_response.error else "Ladder submission failed.",
                ladder=ladder,
            )

        verified, verified_order_ids = _verify_rise_ladder_submission(
            wallet=wallet,
            market_cache=cache,
            market_id=str(market.get("market_id") or ""),
            side_int=RISE_SIDE_TO_INT[requested_side],
            expected_payloads=accepted_payloads,
        )
        ladder = CanonicalLadderResult(
            symbol=requested_symbol,
            side=requested_side,
            distribution=distribution,
            requested_order_count=order_count,
            submitted_order_count=accepted_count,
            requested_volume=_decimal_text(total_volume),
            submitted_volume=_decimal_text(accepted_volume if accepted_count else planned_submitted_volume),
            batch_count=1,
            verified=verified,
            partial=(accepted_count != order_count) or not verified,
            status="success" if verified and accepted_count == order_count else "partial",
            accepted_child_count=accepted_count,
            omitted_order_count=order_count - accepted_count,
            omitted_below_minimum=omitted_below_minimum,
            child_order_ids=verified_order_ids or child_order_ids or None,
            batches=[{"batch_index": 1, "submitted_order_count": accepted_count, "accepted_child_count": accepted_count, "child_order_ids": verified_order_ids or child_order_ids}],
        )
        if verified and accepted_count == len(children):
            return make_success(operation="ladder", exchange=name, account=account, ladder=ladder)
        return make_failure(operation="ladder", exchange=name, account=account, code="VERIFICATION_FAILED", message="Ladder submission could not be verified.", ladder=ladder)
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="ladder", exchange=name, account=account, code="ORDER_SUBMISSION_FAILED", message=sanitize_error_message(str(exc)))


def _execute_positions_management(account: str) -> CanonicalResponse:
    response = _execute_positions_orders(account)
    if not response.success:
        return response
    return make_success(
        operation="positions_management",
        exchange=name,
        account=account,
        positions=response.positions,
    )


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    if not isinstance(request, dict):
        return make_failure(
            operation="",
            exchange=name,
            account="",
            code="INVALID_REQUEST",
            message="Request must be a dict.",
        )

    normalized_request = _normalize_new_order_request(request)
    operation = str(normalized_request.get("operation") or "").strip()
    account = str(normalized_request.get("account") or "").strip()

    if not operation:
        return make_failure(
            operation="",
            exchange=name,
            account=account,
            code="INVALID_REQUEST",
            message="Missing 'operation'.",
        )
    if not account:
        return make_failure(
            operation=operation,
            exchange=name,
            account="",
            code="MISSING_ACCOUNT",
            message="Missing 'account'.",
        )
    if operation == "balance":
        return _execute_balance(account)
    if operation == "positions_orders":
        return _execute_positions_orders(account)
    if operation == "positions_management":
        return _execute_positions_management(account)
    if operation == "set_tp":
        return _execute_set_tpsl(account, normalized_request, stop_type="TAKE_PROFIT", operation="set_tp")
    if operation == "set_sl":
        return _execute_set_tpsl(account, normalized_request, stop_type="STOP_LOSS", operation="set_sl")
    if operation == "new_order":
        return _execute_new_order(account, normalized_request)
    if operation == "ladder":
        return _execute_ladder(account, normalized_request)
    if operation == "cancel_order_group":
        return _execute_cancel_order_group(account, normalized_request)
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code="NOT_IMPLEMENTED",
        message="Not implemented yet.",
    )
