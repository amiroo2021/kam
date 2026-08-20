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

logger = logging.getLogger(__name__)

name = "rise"

# GoldenFibo V2 client_order_id canonical format.
# The V2 integer is 48 bits, packed as (magic | version | direction | role | cycle_uid | step | seq).
# We accept caller-supplied IDs as decimal strings within [0, 2**48 - 1].
RISE_V2_MAX_INT = (1 << 48) - 1
RISE_V2_DEFAULT_ID = "0"


def _rise_normalize_v2_client_order_id(candidate):
    """Validate and normalize a caller-supplied client_order_id for Rise.

    Live evidence (Phase 3): the Rise on-chain PlaceOrderWithPermitV2
    reverts on any non-zero ``client_order_id`` value. Therefore this
    helper either:
      * returns ``"0"`` when caller opted in with the literal "0"
      * returns ``None`` when caller did not supply any client_order_id
      * raises ``ValueError`` when caller supplied any other value
        (rejected before any HTTP mutation)
    """
    if candidate is None:
        return None
    if isinstance(candidate, bool):
        raise ValueError(
            "Rise rejects non-zero client_order_id values "
            "(venue reverts PlaceOrderWithPermitV2)"
        )
    raw = str(candidate).strip()
    if raw == "":
        return None
    if raw == "0":
        return "0"
    raise ValueError(
        f"Rise rejects non-zero client_order_id values; got {raw!r}. "
        "On Rise, the wire client_order_id must remain '0'."
    )


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
    # Phase 1: market_immediate is bounded-limit IOC fill.
    return [
        "balance",
        "positions_orders",
        "positions_management",
        "new_order",
        "ladder",
        "cancel_order_group",
        "cancel_orders",
        "cancel_order",
        "set_tp",
        "set_sl",
        "market_immediate",
        "resolve_instrument",
        "market_constraints",
        "market_price",
        "position_state",
        "get_order_state",
        "get_order_state_by_client_id",
        "close_position",
    ]


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


def _canonical_order_id(order: Dict[str, Any]) -> Optional[str]:
    raw = str(order.get("order_id") or "").strip()
    return raw or None


def _find_matching_open_order(
    *,
    post_orders: List[Dict[str, Any]],
    market_id: str,
    side_int: int,
    size_steps: int,
    price_ticks: int,
    response_order_id: Optional[str],
    used_positions: Optional[set[int]] = None,
    require_response_order_id: bool = True,
) -> Optional[Tuple[int, Dict[str, Any]]]:
    normalized_response_order_id = str(response_order_id or "").strip()
    if require_response_order_id and not normalized_response_order_id:
        return None
    for index, order in enumerate(post_orders):
        if used_positions is not None and index in used_positions:
            continue
        if str(order.get("market_id") or "") != market_id:
            continue
        if int(str(order.get("side_int") if order.get("side_int") is not None else -1)) != side_int:
            continue
        if int(order.get("size_steps") or -1) != size_steps:
            continue
        if int(order.get("price_ticks") or -1) != price_ticks:
            continue
        canonical_id = _canonical_order_id(order)
        if require_response_order_id and canonical_id != normalized_response_order_id:
            continue
        if not require_response_order_id and normalized_response_order_id and canonical_id != normalized_response_order_id:
            continue
        return index, order
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
            tif="IOC",
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
            "tif": "IOC",
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
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    if not str(response_order_id or "").strip():
        return False, None, None
    post_payload = _fetch_open_orders_payload(wallet)
    post_orders = _normalize_open_orders(post_payload, market_cache)
    matched = _find_matching_open_order(
        post_orders=post_orders,
        market_id=market_id,
        side_int=side_int,
        size_steps=size_steps,
        price_ticks=price_ticks,
        response_order_id=response_order_id,
        require_response_order_id=True,
    )
    if matched is None:
        return False, None, None
    _index, order = matched
    return True, _canonical_order_id(order), order


def _classify_rise_limit_result(
    *,
    requested_volume: Decimal,
    submitted_volume: Decimal,
    side_int: int,
    pre_position_size: Decimal,
    post_position_size: Decimal,
    pre_position_side: str,
    post_position_side: str,
    remaining_evidence: Optional[Decimal] = None,
    still_in_open_orders: bool = False,
) -> str:
    """Classify a Rise limit submission from position/or order evidence.

    Returns one of ``OPEN`` / ``PARTIALLY_FILLED`` / ``FILLED`` /
    ``UNKNOWN``. Never infers FILLED from disappearance alone.

    * OPEN: order still active with non-zero remaining and no contradictory
      state.
    * FILLED: confirmed position growth equals the requested quantity (or
        equivalent evidence proves full execution).
    * PARTIALLY_FILLED: confirmed fill delta > 0 and < requested quantity.
    * UNKNOWN: order disappeared with no sufficient position evidence, or the
        evidence is conflicting.
    """
    requested = abs(requested_volume)
    submitted = abs(submitted_volume) if submitted_volume is not None else requested
    # Expected growth sign per side.
    growth = abs(post_position_size) - abs(pre_position_size)
    # For BUY (0), a correctly-placed buy grows the long position.
    pre_open = pre_position_size > 0 and pre_position_side in ("long", "short")

    if still_in_open_orders:
        rem = remaining_evidence
        if rem is not None and rem > 0:
            return "OPEN"
        # Present but no remaining evidence; treat as open (defensive).
        return "OPEN"

    # Not in openOrders — never conclude FILLED from disappearance alone.
    if growth is None:
        return "UNKNOWN"

    # Confirmed position growth vs requested.
    if growth <= 0:
        return "UNKNOWN"

    fill_ratio = growth / submitted if submitted > 0 else Decimal("0")
    if fill_ratio >= Decimal("0.999"):
        return "FILLED"
    if fill_ratio > 0:
        if growth < requested:
            return "PARTIALLY_FILLED"
        return "FILLED"
    return "UNKNOWN"


def _order_id_in_open_orders(
    wallet: str,
    market_cache: Dict[str, Dict[str, Any]],
    market_id: str,
    target_order_id: Optional[str],
) -> bool:
    """Return True if *target_order_id* is still present in openOrders.

    Used by the gated limit-result reconciliation to distinguish an order that
    still rests (OPEN) from one that disappeared (filled / partial / unknown).
    """
    target = str(target_order_id or "").strip()
    if not target:
        return False
    try:
        open_payload = _fetch_open_orders_payload(wallet)
        rows = _normalize_open_orders(open_payload, market_cache)
    except Exception:
        return False
    for row in rows:
        if str(row.get("order_id") or "").strip() == target:
            return True
    return False


def _extract_response_order_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        value = str(data.get("order_id") or "").strip()
        if value:
            return value
    value = str(payload.get("order_id") or "").strip()
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
    client_order_id: Optional[str] = None,
) -> Tuple[CanonicalResponse, Dict[str, Any], Decimal, Decimal]:
    # Caller-supplied GoldenFibo V2 client ID; None ⇒ /trade default.
    _submitted_client_id = (
        client_order_id if client_order_id is not None else RISE_V2_DEFAULT_ID
    )
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
        




"client_order_id": _submitted_client_id,
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
        verified, exchange_order_id, _matched_order = _verify_new_order_submission(
            wallet=wallet,
            market_cache=market_cache,
            market_id=str(market_id),
            side_int=side_int,
            size_steps=size_steps,
            price_ticks=price_ticks,
            response_order_id=response_order_id,
        )
    else:
        verified, exchange_order_id = bool(response_order_id), response_order_id
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
    if not verify_after_submit:
        return make_success(operation=operation, exchange=name, account=account, order=result), payload, submitted_volume, submitted_price
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
) -> Tuple[bool, List[str], List[Dict[str, Any]]]:
    post_payload = _fetch_open_orders_payload(wallet)
    post_orders = _normalize_open_orders(post_payload, market_cache)
    matched_ids: List[str] = []
    matched_rows: List[Dict[str, Any]] = []
    used_positions: set[int] = set()
    for expected in expected_payloads:
        matched = _find_matching_open_order(
            post_orders=post_orders,
            market_id=market_id,
            side_int=side_int,
            size_steps=int(expected.get("size_steps") or -1),
            price_ticks=int(expected.get("price_ticks") or -1),
            response_order_id=str(expected.get("response_order_id") or "").strip() or None,
            used_positions=used_positions,
            require_response_order_id=True,
        )
        if matched is None:
            return False, matched_ids, matched_rows
        index, order = matched
        used_positions.add(index)
        order_id = _canonical_order_id(order)
        if order_id is None:
            return False, matched_ids, matched_rows
        matched_ids.append(order_id)
        matched_rows.append(order)
    return True, matched_ids, matched_rows


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


# ---------------------------------------------------------------------------
# Phase 1 (market_immediate) / Phase 2 (cancel_order) / GF read-stubs.
# Only market_immediate is fully implemented in Phase 1. The other
# helpers return NOT_IMPLEMENTED on purpose so later phases plug them in
# without bloating this commit.
# ---------------------------------------------------------------------------

import time as _t

RISE_DEFAULT_IMMEDIATE_SLIP_PCT = Decimal("0.01")
RISE_IMMEDIATE_VERIFY_WAIT_SECONDS = 6.0
RISE_IMMEDIATE_VERIFY_POLL_SECONDS = 0.5


def _rise_market_price(account: str, requested_symbol: str) -> Decimal:
    """Read reference price for *requested_symbol*.

    /v1/markets items have ``last_price`` at the top level, not inside
    ``config`` — so we cannot rely on the cache which only flattens config
    keys. Read the raw payload directly.
    """
    try:
        payload = _get_json(f"{_api_base()}/v1/markets")
    except Exception:
        return Decimal("0")
    if not isinstance(payload, dict):
        return Decimal("0")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    raw_markets = data.get("markets") if isinstance(data, dict) else None
    if not isinstance(raw_markets, list):
        return Decimal("0")
    target = (requested_symbol or "").strip().upper()
    for m in raw_markets:
        if not isinstance(m, dict):
            continue
        cfg = m.get("config") if isinstance(m.get("config"), dict) else {}
        name_candidates = [
            str(cfg.get("name") or "").strip(),
            str(m.get("display_name") or "").strip(),
            str(m.get("base_asset_symbol") or "").strip(),
        ]
        if not any(_rise_symbol(n).upper() == target for n in name_candidates if n):
            continue
        # Prefer last → mark → mid → index → oracle
        try:
            for key in (
                "last_price",
                "last_traded_price",
                "mark_price",
                "mid_price",
                "index_price",
                "oracle_price",
            ):
                v = _decimal_or_none(m.get(key))
                if v is not None and v > 0:
                    return v
        except Exception:
            pass
    return Decimal("0")


def _rise_position_snapshot(wallet: str, requested_symbol: str) -> Dict[str, Any]:
    """Return a snapshot of the position row for *requested_symbol*.

    Shape::

        {
          "side": "long" | "short" | "flat",
          "size": Decimal(absolute notional quantity, 0 when flat),
          "entry_price": Decimal(avg_entry_price, 0 when flat),
        }

    * ``size`` is always a non-negative Decimal (we keep the absolute
      quantity because some Rise payloads sign the size with the side).
    * ``entry_price`` is whatever the venue reports; we do not invent
      a value if the row is missing or zero.
    """
    snap = {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")}
    try:
        payload = _fetch_portfolio(wallet)
    except Exception:
        return snap
    if not isinstance(payload, dict):
        return snap
    positions_payload = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    positions = positions_payload.get("positions") if isinstance(positions_payload, dict) else None
    if not isinstance(positions, list):
        positions = payload.get("positions") if isinstance(payload.get("positions"), list) else []
    target = str(requested_symbol or "").strip().upper()
    for item in positions:
        if not isinstance(item, dict):
            continue
        sym = _rise_symbol(item.get("market_name") or item.get("symbol") or item.get("market"))
        if sym.upper() != target:
            continue
        raw_size = _decimal_or_none(item.get("size"))
        side_field = item.get("side")
        side_norm = _rise_side(side_field, raw_size)
        abs_size = abs(raw_size) if raw_size is not None else Decimal("0")
        if abs_size <= 0:
            side_norm = "flat"
        entry = _decimal_or_none(item.get("avg_entry_price"))
        if entry is None:
            entry = Decimal("0")
        return {
            "side": side_norm,
            "size": abs_size,
            "entry_price": entry,
        }
    return snap


def _execute_market_immediate(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Phase 1: bounded-limit IOC buy/sell via existing PlaceOrder.

    Verification path:
      * capture PRE-submit position snapshot
      * submit IOC via existing signed LIMIT primitive
      * capture POST-submit position snapshot
      * SUCCESS iff: order not resting in openOrders AND post-side
        matches expected_fill_side AND post-size increased by at least
        the configured fill_threshold AND avg_entry_price inside the
        slip bound (BUY fill <= bound, SELL fill >= bound)
      * otherwise return FILL_NOT_CONFIRMED / FILL_SIDE_MISMATCH / ...
        Never invent a fill.
    """
    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_side = str(request.get("side") or "").strip().lower()
    if not requested_symbol:
        return make_failure(operation="market_immediate", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in {"buy", "sell"}:
        return make_failure(operation="market_immediate", exchange=name, account=account, code="INVALID_SIDE", message="Side must be buy or sell.")
    requested_volume_text = request.get("volume") or request.get("size")
    requested_volume = _decimal_or_none(requested_volume_text)
    if requested_volume is None or requested_volume <= 0:
        return make_failure(operation="market_immediate", exchange=name, account=account, code="INVALID_VOLUME", message="Volume must be positive.")

    wallet_signer = _lookup_credentials(account)
    if not wallet_signer or not wallet_signer[0] or not wallet_signer[1]:
        return make_failure(operation="market_immediate", exchange=name, account=account, code="UNKNOWN_ACCOUNT", message="Unknown or incomplete Rise account credentials.")
    wallet, signer_private = wallet_signer
    expected_fill_side = "long" if requested_side == "buy" else "short"

    # Phase 3: optional caller-supplied GoldenFibo V2 client_order_id.
    raw_cid_mi = (
        request.get("client_order_id")
        if "client_order_id" in request
        else (
            request.get("client_id")
            or request.get("client_order_index")
        )
    )
    try:
        normalized_cid_mi = _rise_normalize_v2_client_order_id(raw_cid_mi)
    except ValueError as exc:
        return make_failure(
            operation="market_immediate",
            exchange=name,
            account=account,
            code="RISE_CLIENT_ORDER_ID_UNSUPPORTED",
            message=str(exc),
        )
    market_immediate_client_order_id = normalized_cid_mi or RISE_V2_DEFAULT_ID

    if "slip_pct" in request:
        parsed_slip = _decimal_or_none(request.get("slip_pct"))
        if parsed_slip is None:
            return make_failure(operation="market_immediate", exchange=name, account=account, code="INVALID_SLIP_PCT", message="slip_pct could not be parsed.")
        slip_pct = parsed_slip
    else:
        slip_pct = RISE_DEFAULT_IMMEDIATE_SLIP_PCT
    if slip_pct <= 0 or slip_pct > Decimal("0.5"):
        return make_failure(operation="market_immediate", exchange=name, account=account, code="INVALID_SLIP_PCT", message="slip_pct must be in (0, 0.5].")

    requested_threshold = _decimal_or_none(request.get("fill_threshold")) or Decimal("0.95")
    if requested_threshold <= 0 or requested_threshold > Decimal("1"):
        requested_threshold = Decimal("0.95")

    max_wait_text = request.get("max_wait_seconds")
    if max_wait_text is None:
        max_wait = RISE_IMMEDIATE_VERIFY_WAIT_SECONDS
    else:
        try:
            max_wait = float(max_wait_text)
        except Exception:
            max_wait = RISE_IMMEDIATE_VERIFY_WAIT_SECONDS
    if max_wait <= 0 or max_wait > 60:
        max_wait = RISE_IMMEDIATE_VERIFY_WAIT_SECONDS

    try:
        markets_payload = _fetch_markets_payload()
        cache = _market_cache(markets_payload, {})
    except Exception as exc:
        return make_failure(operation="market_immediate", exchange=name, account=account, code="MARKETS_READ_FAILED", message=str(exc))

    market = _resolve_market_by_symbol(requested_symbol, cache)
    if market is None:
        return make_failure(operation="market_immediate", exchange=name, account=account, code="INSTRUMENT_NOT_FOUND", message="Instrument not found.")

    step_price = _decimal_or_none(market.get("step_price"))
    step_size = _decimal_or_none(market.get("step_size"))
    min_order_size = _decimal_or_none(market.get("min_order_size")) or Decimal("0")
    if step_price is None or step_price <= 0 or step_size is None or step_size <= 0:
        return make_failure(operation="market_immediate", exchange=name, account=account, code="INVALID_MARKET_METADATA", message="Missing step sizes.")

    submitted_volume = _rise_quantize_to_step(requested_volume, step_size)
    if submitted_volume <= 0 or submitted_volume < min_order_size:
        return make_failure(operation="market_immediate", exchange=name, account=account, code="INVALID_VOLUME", message="Volume rounds below market minimum after quantization.")

    reference = _rise_market_price(account, requested_symbol)
    if reference <= 0:
        return make_failure(operation="market_immediate", exchange=name, account=account, code="MARK_PRICE_UNAVAILABLE", message="Reference price unavailable; cannot price market-immediate slip bound.")
    if requested_side == "buy":
        slip_price = reference * (Decimal("1") + slip_pct)
    else:
        slip_price = reference * (Decimal("1") - slip_pct)
    slip_price = _rise_quantize_to_step(slip_price, step_price)
    if slip_price <= 0:
        return make_failure(operation="market_immediate", exchange=name, account=account, code="INVALID_SLIP_PRICE", message="Computed slip-price invalid.")

    pre = _rise_position_snapshot(wallet, requested_symbol)

    response, _payload, sub_vol, sub_price = _submit_rise_limit_order(
        wallet=wallet,
        signer_private=signer_private,
        market_cache=cache,
        market=market,
        requested_symbol=requested_symbol,
        requested_side=requested_side,
        requested_volume=submitted_volume,
        requested_price=slip_price,
        requested_tif="IOC",
        reduce_only=False,
        operation="market_immediate",
        account=account,
        verify_after_submit=False,
        client_order_id=market_immediate_client_order_id,
    )
    submitted_order = getattr(response, "order", None)
    raw_exchange_order_id = (
        submitted_order.exchange_order_id if submitted_order is not None else None
    )
    if raw_exchange_order_id is None:
        # Try to recover from raw payload (raw response body).
        try:
            resp_obj = response
        except Exception:
            resp_obj = None
        # _submit_rise_limit_order returned (resp, payload, ...) — already destructured
        # but we can reach in via _payload closure? not directly. Best effort: if the
        # response carries no exchange_order_id we treat as no-fill.
        pass

    deadline = _t.time() + max_wait
    last_post = pre
    while _t.time() < deadline:
        try:
            open_payload = _fetch_open_orders_payload(wallet)
            rows = _normalize_open_orders(open_payload, cache)
        except Exception:
            rows = []
        still_resting = bool(raw_exchange_order_id) and any(
            str(r.get("order_id") or "") == str(raw_exchange_order_id) for r in rows
        )
        post = _rise_position_snapshot(wallet, requested_symbol)
        last_post = post

        if post["side"] == expected_fill_side and pre["side"] == expected_fill_side:
            delta = post["size"] - pre["size"]
        elif post["side"] == expected_fill_side and pre["side"] == "flat":
            delta = post["size"]
        else:
            delta = Decimal("0")

        if (not still_resting) and post["side"] == expected_fill_side and delta >= submitted_volume * requested_threshold:
            fill_price = post["entry_price"]
            if fill_price <= 0:
                return make_failure(
                    operation="market_immediate",
                    exchange=name,
                    account=account,
                    code="FILL_NOT_CONFIRMED",
                    message="Post-fill snapshot is missing avg_entry_price.",
                    order_state={
                        "exchange_order_id": raw_exchange_order_id,
                        "submitted_volume": _decimal_text(submitted_volume),
                        "submitted_price": _decimal_text(slip_price),
                        "reference_price": _decimal_text(reference),
                        "post_position": {
                            "side": post["side"],
                            "size": _decimal_text(post["size"]),
                        },
                    },
                )
            if requested_side == "buy" and fill_price > slip_price:
                return make_failure(
                    operation="market_immediate",
                    exchange=name,
                    account=account,
                    code="FILL_PRICE_OUT_OF_SLIP",
                    message=f"avg_entry_price {fill_price} above BUY slip bound {slip_price}.",
                    order_state={
                        "exchange_order_id": raw_exchange_order_id,
                        "submitted_price": _decimal_text(slip_price),
                        "fill_price": _decimal_text(fill_price),
                    },
                )
            if requested_side == "sell" and fill_price < slip_price:
                return make_failure(
                    operation="market_immediate",
                    exchange=name,
                    account=account,
                    code="FILL_PRICE_OUT_OF_SLIP",
                    message=f"avg_entry_price {fill_price} below SELL slip bound {slip_price}.",
                    order_state={
                        "exchange_order_id": raw_exchange_order_id,
                        "submitted_price": _decimal_text(slip_price),
                        "fill_price": _decimal_text(fill_price),
                    },
                )
            return make_success(
                operation="market_immediate",
                exchange=name,
                account=account,
                order=CanonicalOrderResult(
                    symbol=requested_symbol,
                    side=expected_fill_side,
                    order_type="market",
                    requested_volume=_decimal_text(requested_volume),
                    requested_price=_decimal_text(reference),
                    submitted_volume=_decimal_text(submitted_volume),
                    submitted_price=_decimal_text(slip_price),
                    verified=True,
                    status="filled",
                    exchange_order_id=raw_exchange_order_id,
                    client_order_id="0",
                ),
                order_state={
                    "fill_size": _decimal_text(post["size"]),
                    "fill_price": _decimal_text(fill_price),
                    "delta_size": _decimal_text(delta),
                    "pre_position_size": _decimal_text(pre["size"]),
                    "pre_position_side": pre["side"],
                    "still_resting": False,
                    "submitted_client_order_id": market_immediate_client_order_id,
                    "venue_client_order_id": "",
                    "venue_roundtrip_verified": False,
                },
            )
        _t.sleep(RISE_IMMEDIATE_VERIFY_POLL_SECONDS)

    pre_size = pre["size"]
    pre_side = pre["side"]
    post_side = last_post["side"]
    post_size = last_post["size"]
    if post_side == expected_fill_side and pre_side == expected_fill_side:
        delta = post_size - pre_size
    elif post_side == expected_fill_side and pre_side == "flat":
        delta = post_size
    else:
        delta = Decimal("0")
    note = {
        "exchange_order_id": raw_exchange_order_id,
        "submitted_volume": _decimal_text(submitted_volume),
        "submitted_price": _decimal_text(slip_price),
        "reference_price": _decimal_text(reference),
        "pre_position_side": pre_side,
        "pre_position_size": _decimal_text(pre_size),
        "post_position_side": post_side,
        "post_position_size": _decimal_text(post_size),
        "delta_size": _decimal_text(delta),
    }
    if post_side == ("short" if expected_fill_side == "long" else "long"):
        return make_failure(
            operation="market_immediate",
            exchange=name,
            account=account,
            code="FILL_SIDE_MISMATCH",
            message=f"Post-submit position side {post_side} is opposite to expected {expected_fill_side}.",
            order_state=note,
        )
    if delta <= 0:
        return make_failure(
            operation="market_immediate",
            exchange=name,
            account=account,
            code="FILL_NOT_CONFIRMED",
            message="No position-size increase observed; the IOC may not have filled.",
            order_state=note,
        )
    required = submitted_volume * requested_threshold
    if delta < required:
        return make_failure(
            operation="market_immediate",
            exchange=name,
            account=account,
            code="FILL_NOT_CONFIRMED",
            message=f"Position delta {delta} below required threshold {required}. Partial fill observed.",
            order_state=note,
        )
    return make_failure(
        operation="market_immediate",
        exchange=name,
        account=account,
        code="FILL_NOT_CONFIRMED",
        message="Verification window exhausted.",
        order_state=note,
    )



def _execute_cancel_order(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Phase 2: cancel EXACTLY one Rise order by venue order_id.

    Identifier: ``exchange_order_id`` (canonical venue string such as
    ``"0xc0000024ef...00006f"``). Symbol is optional context only.
    ``resting_order_id`` is accepted because the venue distinguishes the
    canonical ``order_id`` from the integer used in the EIP712 cancel
    signature.

    Outcomes are normalized via ``order_state``:

        CANCELED            — target was active, venue accepted, target
                              gone on post-confirm; unrelated preserved.
        ALREADY_TERMINAL    — target absent pre-submit (already canceled,
                              filled, or never existed).
        NOT_CONFIRMED       — cancel submitted but post-state unclear.
        FAILED              — venue rejected or broader cancellation
                              detected.
    """
    wallet_signer = _lookup_credentials(account)
    if not wallet_signer or not wallet_signer[0] or not wallet_signer[1]:
        return make_failure(
            operation="cancel_order",
            exchange=name, account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Rise account credentials.",
            order_state={"identity_provided": _public_cancel_id_or_none(request)},
        )

    wallet, signer_private = wallet_signer

    raw_order_id = str(
        request.get("exchange_order_id")
        or request.get("order_id")
        or request.get("resting_order_id")
        or ""
    ).strip()
    if not raw_order_id:
        return make_failure(
            operation="cancel_order",
            exchange=name, account=account,
            code="MISSING_ORDER_ID",
            message="exchange_order_id is required.",
        )
    if len(raw_order_id) > 128:
        return make_failure(
            operation="cancel_order",
            exchange=name, account=account,
            code="MALFORMED_ORDER_ID",
            message="exchange_order_id is too long.",
        )
    if not re.match(r"^[0-9a-zA-Z]+$", raw_order_id):
        return make_failure(
            operation="cancel_order",
            exchange=name, account=account,
            code="MALFORMED_ORDER_ID",
            message="exchange_order_id must be alphanumeric only.",
        )

    context_symbol = str(request.get("symbol") or "").strip().upper()

    # PRE: read openOrders so we can both confirm target identity and
    # capture "other" ids to prove later that we did not mutate them.
    try:
        markets_payload = _fetch_markets_payload()
        cache = _market_cache(markets_payload, {})
        pre_open_payload = _fetch_open_orders_payload(wallet)
        pre_open_rows = _normalize_open_orders(pre_open_payload, cache)
    except Exception as exc:
        return make_failure(
            operation="cancel_order",
            exchange=name, account=account,
            code="PRE_READ_FAILED",
            message=f"openOrders read failed: {exc}",
            order_state={"target": raw_order_id},
        )

    target_row = None
    other_target_ids = []
    for row in pre_open_rows:
        oid = str(row.get("order_id") or "").strip()
        if not oid:
            continue
        if oid == raw_order_id:
            if target_row is not None:
                return make_failure(
                    operation="cancel_order",
                    exchange=name, account=account,
                    code="DUPLICATE_TARGET_ID",
                    message="Target id matched multiple active orders; refusing to act.",
                    order_state={"target": raw_order_id},
                )
            target_row = row
        else:
            other_target_ids.append(oid)

    if target_row is None:
        note = {
            "target": raw_order_id,
            "context_symbol": context_symbol or None,
            "active_orders_seen": len(pre_open_rows),
            "unrelated_count": len(other_target_ids),
        }
        return make_success(
            operation="cancel_order",
            exchange=name, account=account,
            order_state={"outcome": "ALREADY_TERMINAL", **note},
        )

    if context_symbol:
        resolved_market_id = str(target_row.get("market_id") or "").strip()
        matched_symbol = None
        for m in (markets_payload.get("markets") or []):
            if not isinstance(m, dict):
                continue
            if str(m.get("market_id") or "") == resolved_market_id:
                cfg = m.get("config") if isinstance(m.get("config"), dict) else {}
                matched_symbol = _rise_symbol(cfg.get("name") or m.get("display_name"))
                break
        if matched_symbol and matched_symbol.upper() != context_symbol:
            return make_failure(
                operation="cancel_order",
                exchange=name, account=account,
                code="IDENTITY_MISMATCH",
                message=(
                    f"Target exchange_order_id belongs to {matched_symbol} but "
                    f"caller supplied context_symbol={context_symbol}."
                ),
                order_state={"target": raw_order_id, "matched_symbol": matched_symbol},
            )

    resting_order_id = str(target_row.get("resting_order_id") or "").strip()
    market_id_int = target_row.get("market_id")
    if not resting_order_id or market_id_int is None:
        return make_failure(
            operation="cancel_order",
            exchange=name, account=account,
            code="MISSING_RISK_METADATA",
            message="Target open-order row lacks required cancel metadata (resting_order_id/market_id).",
            order_state={"target": raw_order_id},
        )
    try:
        market_id_value = int(market_id_int)
        resting_order_id_value = int(resting_order_id)
    except (TypeError, ValueError):
        return make_failure(
            operation="cancel_order",
            exchange=name, account=account,
            code="MALFORMED_RISK_METADATA",
            message="Target open-order metadata is non-integer.",
            order_state={"target": raw_order_id},
        )

    # SUBMIT: exactly one POST /v1/orders/cancel.
    try:
        nonce_state = _fetch_nonce_state(wallet)
        nonce_anchor = int(str(nonce_state.get("nonce_anchor") or 0))
        nonce_bitmap_index = int(str(nonce_state.get("current_bitmap_index") or 0))
        if nonce_bitmap_index > 207:
            nonce_anchor += 1
            nonce_bitmap_index = 0
        deadline = _rise_order_deadline()
        action_hash = _rise_encode_cancel_action_hash(
            market_id=market_id_value,
            resting_order_id=resting_order_id_value,
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
        from eth_account import Account
        signer_address = Account.from_key(signer_private).address
        permit = {
            "account": wallet,
            "signer": signer_address,
            "deadline": int(deadline),
            "nonce_anchor": str(nonce_anchor),
            "nonce_bitmap_index": int(nonce_bitmap_index),
            "signature": _rise_sig_to_base64(signature),
        }
        cancel_body = {
            "market_id": int(market_id_value),
            "order_id": raw_order_id,
            "permit": permit,
        }
        _post_json(f"{_api_base()}/v1/orders/cancel", cancel_body)
    except _RiseHTTPError as exc:
        return make_failure(
            operation="cancel_order",
            exchange=name, account=account,
            code="CANCEL_REJECTED",
            message=f"HTTP {exc.status} on {exc.path}: {exc.body[:200]}",
            order_state={"target": raw_order_id, "outcome": "FAILED"},
        )
    except Exception as exc:
        return make_failure(
            operation="cancel_order",
            exchange=name, account=account,
            code="CANCEL_REJECTED",
            message=sanitize_error_message(str(exc)),
            order_state={"target": raw_order_id, "outcome": "FAILED"},
        )

    # POST: verify target is gone from openOrders AND unrelated orders
    # remained intact.
    try:
        post_open_payload = _fetch_open_orders_payload(wallet)
        post_rows = _normalize_open_orders(post_open_payload, cache)
    except Exception as exc:
        return make_success(
            operation="cancel_order",
            exchange=name, account=account,
            order_state={
                "outcome": "NOT_CONFIRMED",
                "target": raw_order_id,
                "reason": f"post-read failed: {exc}",
            },
        )

    target_post_ids = {
        str(row.get("order_id") or "").strip()
        for row in post_rows
        if str(row.get("order_id") or "").strip()
    }
    target_gone = raw_order_id not in target_post_ids
    unrelated_preserved = all(
        oid in target_post_ids for oid in other_target_ids
    ) if other_target_ids else True

    if target_gone and unrelated_preserved:
        return make_success(
            operation="cancel_order",
            exchange=name, account=account,
            order_state={
                "outcome": "CANCELED",
                "target": raw_order_id,
                "unrelated_preserved": True,
                "unrelated_count": len(other_target_ids),
            },
        )
    if not target_gone:
        return make_success(
            operation="cancel_order",
            exchange=name, account=account,
            order_state={
                "outcome": "NOT_CONFIRMED",
                "target": raw_order_id,
                "reason": "target still active after cancel",
            },
        )
    return make_success(
        operation="cancel_order",
        exchange=name, account=account,
        order_state={
            "outcome": "FAILED",
            "target": raw_order_id,
            "reason": "broader cancellation detected (unrelated orders missing)",
        },
    )


def _execute_close_position(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Phase 4: safe reduce-only close of an existing Rise position.

    Mechanism: PlaceOrder with ``reduce_only=True`` (on-chain enforced via the
    ``order_flags`` bit carried in the EIP712 ``action_hash``). The opposite
    side IOC + exact actual position size guarantees no reversal because the
    venue refuses to overshoot.

    Outcomes are normalized via ``order_state``:
        CLOSED                  - position verified flat post-submit
        ALREADY_FLAT            - pre read showed no position (idempotent)
        NOT_CONFIRMED           - close submitted but flat unverified in window
        FAILED                  - venue rejected, side mismatch, or wider mutation

    Exactly one close submission is ever attempted. Transient 429s retry the
    verification read only — never the close.
    """
    wallet_signer = _lookup_credentials(account)
    if not wallet_signer or not wallet_signer[0] or not wallet_signer[1]:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Rise account credentials.",
        )

    wallet, signer_private = wallet_signer
    requested_symbol = str(request.get("symbol") or "").strip().upper()
    if not requested_symbol:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="symbol is required.",
        )

    # Caller must NOT supply a non-zero client_order_id (Phase 3 evidence).
    raw_cid = (
        request.get("client_order_id")
        if "client_order_id" in request
        else (
            request.get("client_id")
            or request.get("client_order_index")
        )
    )
    try:
        normalized_cid = _rise_normalize_v2_client_order_id(raw_cid)
    except ValueError as exc:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="RISE_CLIENT_ORDER_ID_UNSUPPORTED",
            message=str(exc),
        )
    client_order_id = normalized_cid or RISE_V2_DEFAULT_ID

    # Slip bound, default to 0.5% — must be a tight enough bound that the
    # reduce-only fill is acceptable.
    if "slip_pct" in request:
        parsed_slip = _decimal_or_none(request.get("slip_pct"))
        if parsed_slip is None:
            return make_failure(
                operation="close_position",
                exchange=name,
                account=account,
                code="INVALID_SLIP_PCT",
                message="slip_pct could not be parsed.",
            )
        slip_pct = parsed_slip
    else:
        slip_pct = RISE_DEFAULT_IMMEDIATE_SLIP_PCT
    if slip_pct <= 0 or slip_pct > Decimal("0.5"):
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="INVALID_SLIP_PCT",
            message="slip_pct must be in (0, 0.5].",
        )

    max_wait_text = request.get("max_wait_seconds")
    if max_wait_text is None:
        max_wait = RISE_IMMEDIATE_VERIFY_WAIT_SECONDS
    else:
        try:
            max_wait = float(max_wait_text)
        except Exception:
            max_wait = RISE_IMMEDIATE_VERIFY_WAIT_SECONDS
    if max_wait <= 0 or max_wait > 60:
        max_wait = RISE_IMMEDIATE_VERIFY_WAIT_SECONDS

    # Load markets / cache.
    try:
        markets_payload = _fetch_markets_payload()
        cache = _market_cache(markets_payload, {})
    except Exception as exc:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="MARKETS_READ_FAILED",
            message=str(exc),
        )
    market = _resolve_market_by_symbol(requested_symbol, cache)
    if market is None:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="INSTRUMENT_NOT_FOUND",
            message="Instrument not found.",
        )
    step_price = _decimal_or_none(market.get("step_price"))
    step_size = _decimal_or_none(market.get("step_size"))
    if step_price is None or step_size is None or step_price <= 0 or step_size <= 0:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="INVALID_MARKET_METADATA",
            message="Missing step sizes.",
        )

    # PRE: read live position snapshot for THIS symbol.
    pre = _rise_position_snapshot(wallet, requested_symbol)
    pre_size = pre["size"]
    pre_side = pre["side"]
    pre_entry = pre["entry_price"]

    if pre_size <= 0:
        # Already flat. Idempotent success.
        return make_success(
            operation="close_position",
            exchange=name,
            account=account,
            order_state={
                "outcome": "ALREADY_FLAT",
                "symbol": requested_symbol,
                "pre_position_size": _decimal_text(pre_size),
                "pre_position_side": pre_side,
            },
        )

    # Determine close side + size.
    close_side = "sell" if pre_side == "long" else "buy"
    close_size = _rise_quantize_to_step(pre_size, step_size)
    if close_size <= 0:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="INVALID_CLOSE_SIZE",
            message=f"Pre-close size {pre_size} rounds to zero after quantization.",
            order_state={"pre_position_size": _decimal_text(pre_size)},
        )

    # Compute slip-bound price for the immediate fill.
    reference = _rise_market_price(account, requested_symbol)
    if reference <= 0:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="MARK_PRICE_UNAVAILABLE",
            message="Reference price unavailable; cannot price close slip bound.",
        )
    if close_side == "sell":
        slip_price = reference * (Decimal("1") - slip_pct)
    else:
        slip_price = reference * (Decimal("1") + slip_pct)
    slip_price = _rise_quantize_to_step(slip_price, step_price)
    if slip_price <= 0:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="INVALID_SLIP_PRICE",
            message="Computed slip-price invalid.",
        )

    # SUBMIT: one PlaceOrder with reduce_only=True, IOC, opposite side, exact size.
    response, _payload, sub_vol, sub_price = _submit_rise_limit_order(
        wallet=wallet,
        signer_private=signer_private,
        market_cache=cache,
        market=market,
        requested_symbol=requested_symbol,
        requested_side=close_side,
        requested_volume=close_size,
        requested_price=slip_price,
        requested_tif="IOC",
        reduce_only=True,  # on-chain enforced
        operation="close_position",
        account=account,
        verify_after_submit=False,
        client_order_id=client_order_id,
    )
    submitted_order = getattr(response, "order", None)
    raw_close_order_id = (
        submitted_order.exchange_order_id if submitted_order is not None else None
    )

    if not getattr(response, "success", False):
        # Surface the venue error (e.g. on-chain revert because reduce_only
        # can't be honoured with the current state). No retry, no duplicate close.
        return response

    # POST: poll position until flat, idempotent, or timeout.
    deadline = _t.time() + max_wait
    last_post = pre
    while _t.time() < deadline:
        try:
            post = _rise_position_snapshot(wallet, requested_symbol)
        except Exception:
            post = last_post  # transient read failure: stay with what we know
        last_post = post
        # hard-fail if venue shows the opposite side.
        if post["side"] != "flat" and post["side"] != pre_side:
            return make_success(
                operation="close_position",
                exchange=name,
                account=account,
                order_state={
                    "outcome": "FAILED",
                    "symbol": requested_symbol,
                    "reason": (
                        f"post-side {post['side']} is opposite to pre-side {pre_side};"
                        " the close would have reversed exposure"
                    ),
                    "submitted_size": _decimal_text(sub_vol),
                    "submitted_price": _decimal_text(sub_price),
                    "exchange_order_id": raw_close_order_id,
                    "pre_position_side": pre_side,
                    "pre_position_size": _decimal_text(pre_size),
                    "post_position_side": post["side"],
                    "post_position_size": _decimal_text(post["size"]),
                },
            )
        if post["size"] == 0:
            return make_success(
                operation="close_position",
                exchange=name,
                account=account,
                order_state={
                    "outcome": "CLOSED",
                    "symbol": requested_symbol,
                    "submitted_size": _decimal_text(sub_vol),
                    "submitted_price": _decimal_text(sub_price),
                    "fill_price": _decimal_text(post["entry_price"] or pre_entry),
                    "exchange_order_id": raw_close_order_id,
                    "pre_position_size": _decimal_text(pre_size),
                    "pre_position_side": pre_side,
                    "post_position_size": _decimal_text(post["size"]),
                },
            )
        _t.sleep(RISE_IMMEDIATE_VERIFY_POLL_SECONDS)

    # Window exhausted.
    reduced = last_post["size"] < pre_size
    if reduced and last_post["size"] > 0:
        return make_success(
            operation="close_position",
            exchange=name,
            account=account,
            order_state={
                "outcome": "PARTIALLY_CLOSED",
                "symbol": requested_symbol,
                "submitted_size": _decimal_text(sub_vol),
                "submitted_price": _decimal_text(sub_price),
                "exchange_order_id": raw_close_order_id,
                "pre_position_size": _decimal_text(pre_size),
                "pre_position_side": pre_side,
                "post_position_size": _decimal_text(last_post["size"]),
                "post_position_side": last_post["side"],
                "reason": (
                    "position reduced but not fully flattened within the "
                    "verification window; close was submitted exactly once and "
                    "reduce_only prevents any reversal"
                ),
            },
        )
    return make_success(
        operation="close_position",
        exchange=name,
        account=account,
        order_state={
            "outcome": "NOT_CONFIRMED",
            "symbol": requested_symbol,
            "submitted_size": _decimal_text(sub_vol),
            "submitted_price": _decimal_text(sub_price),
            "exchange_order_id": raw_close_order_id,
            "pre_position_size": _decimal_text(pre_size),
            "pre_position_side": pre_side,
            "post_position_size": _decimal_text(last_post["size"]),
            "post_position_side": last_post["side"],
            "reason": "verification window exhausted; close was submitted exactly once",
        },
    )


def _public_cancel_id_or_none(request: Dict[str, Any]) -> Optional[str]:
    cand = (
        request.get("exchange_order_id")
        or request.get("order_id")
        or request.get("resting_order_id")
    )
    if cand is None:
        return None
    text = str(cand).strip()
    return text or None



def _rise_market_metadata(requested_symbol: str) -> Optional[Dict[str, Any]]:
    """Return normalized market metadata for *requested_symbol*.

    Single source of truth for GoldenFibo resolve_instrument /
    market_constraints / market_price reads. Raises on read failure so the
    caller can map it to a canonical failure.

    Returns::

        {
          "symbol": canonical bare asset symbol,
          "market_id": str,
          "step_size": str,
          "step_price": str,
          "min_order_size": str,
          "active": bool,
        }
    """
    requested = str(requested_symbol or "").strip().upper()
    if not requested:
        return None
    markets_payload = _fetch_markets_payload()
    cache = _market_cache(markets_payload, {})
    market = _resolve_market_by_symbol(requested, cache)
    if market is None:
        return None
    step_price = _decimal_or_none(market.get("step_price"))
    step_size = _decimal_or_none(market.get("step_size"))
    # Do NOT invent missing mandatory steps. Surface them as empty strings so
    # downstream preflight (market_constraints) can fail with
    # INVALID_MARKET_METADATA rather than fabricating values.
    return {
        "market": {
            "symbol": (market.get("symbol") or requested).strip().upper(),
            "market_id": market.get("market_id"),
            "step_size": _decimal_text(step_size) if step_size is not None else "",
            "step_price": _decimal_text(step_price) if step_price is not None else "",
            "min_order_size": _decimal_text(_decimal_or_none(market.get("min_order_size")) or Decimal("0")),
            "active": bool(market.get("active", True)),
        },
        "market_id": market.get("market_id"),
        "symbol": (market.get("symbol") or requested).strip().upper(),
        "size_step": _decimal_text(step_size) if step_size is not None else "",
        "price_tick": _decimal_text(step_price) if step_price is not None else "",
        "min_size": _decimal_text(_decimal_or_none(market.get("min_order_size")) or Decimal("0")),
        "size_precision": _step_precision(step_size),
        "price_precision": _step_precision(step_price),
    }


def _market_constraints_fields(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize GoldenFibo preflight constraint fields from metadata."""
    if not meta:
        return {}
    market = meta.get("market") or {}
    return {
        "symbol": meta.get("symbol"),
        "market_id": market.get("market_id"),
        "price_tick": meta.get("price_tick"),
        "size_step": meta.get("size_step"),
        "min_size": meta.get("min_size"),
        "size_precision": meta.get("size_precision"),
        "price_precision": meta.get("price_precision"),
        "min_notional": None,  # Rise does not expose an explicit min notional.
        "active": market.get("active"),
    }


def _execute_resolve_instrument(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    requested_symbol = str(request.get("symbol") or request.get("instrument") or "").strip().upper()
    if not requested_symbol:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    try:
        meta = _rise_market_metadata(requested_symbol)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="MARKETS_READ_FAILED",
            message=sanitize_error_message(str(exc)),
        )
    if meta is None:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="INSTRUMENT_NOT_FOUND",
            message=f"Instrument not found: {requested_symbol}",
        )
    market = meta.get("market") or {}
    instrument = CanonicalInstrument(
        requested_symbol=requested_symbol,
        symbol=str(market.get("symbol") or requested_symbol),
        display_name=str(market.get("symbol") or requested_symbol),
        price_increment=market.get("step_price"),
        size_increment=market.get("step_size"),
        minimum_size=market.get("min_order_size"),
    )
    return make_success(
        operation="resolve_instrument",
        exchange=name,
        account=account,
        instrument=instrument,
        order_state=_market_constraints_fields(meta),
    )


def _execute_market_constraints(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    requested_symbol = str(request.get("symbol") or request.get("instrument") or "").strip().upper()
    if not requested_symbol:
        return make_failure(
            operation="market_constraints",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    try:
        meta = _rise_market_metadata(requested_symbol)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="market_constraints",
            exchange=name,
            account=account,
            code="MARKETS_READ_FAILED",
            message=sanitize_error_message(str(exc)),
        )
    if meta is None:
        return make_failure(
            operation="market_constraints",
            exchange=name,
            account=account,
            code="INSTRUMENT_NOT_FOUND",
            message=f"Instrument not found: {requested_symbol}",
        )
    fields = _market_constraints_fields(meta)
    # GoldenFibo preflight treats missing mandatory metadata as a hard fail.
    # (Rise's market cache defaults absent steps to "0", so treat non-positive
    # step values as missing rather than fabricating them.)
    step_tick_d = _decimal_or_none(fields.get("price_tick"))
    step_size_d = _decimal_or_none(fields.get("size_step"))
    if (
        not fields.get("price_tick")
        or not fields.get("size_step")
        or step_tick_d is None
        or step_tick_d <= 0
        or step_size_d is None
        or step_size_d <= 0
    ):
        return make_failure(
            operation="market_constraints",
            exchange=name,
            account=account,
            code="INVALID_MARKET_METADATA",
            message="Missing mandatory market step metadata.",
        )
    return make_success(
        operation="market_constraints",
        exchange=name,
        account=account,
        order_state=fields,
    )


def _execute_market_price(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    requested_symbol = str(request.get("symbol") or request.get("instrument") or "").strip().upper()
    if not requested_symbol:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    try:
        meta = _rise_market_metadata(requested_symbol)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account,
            code="MARKETS_READ_FAILED",
            message=sanitize_error_message(str(exc)),
        )
    if meta is None:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account,
            code="INSTRUMENT_NOT_FOUND",
            message=f"Instrument not found: {requested_symbol}",
        )
    # Same reference-price source market_immediate uses for IOC slippage.
    reference_raw = _rise_market_price(account, requested_symbol)
    if reference_raw <= 0:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account,
            code="MARK_PRICE_UNAVAILABLE",
            message="Reference price unavailable.",
        )
    symbol = str(meta.get("symbol") or requested_symbol)
    mark_price = CanonicalMarketPrice(
        requested_symbol=requested_symbol,
        market=str(meta.get("market_id") or ""),
        mark_price=_decimal_text(reference_raw),
        price=_decimal_text(reference_raw),
    )
    return make_success(
        operation="market_price",
        exchange=name,
        account=account,
        market_price=mark_price,
        order_state={"symbol": symbol, "market_id": meta.get("market_id")},
    )


def _execute_position_state(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    wallet, _signer_private = _lookup_credentials(account)
    if not wallet:
        return make_failure(
            operation="position_state",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Rise account credentials.",
        )
    requested_symbol = str(request.get("symbol") or request.get("instrument") or "").strip().upper()
    if not requested_symbol:
        return make_failure(
            operation="position_state",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    try:
        snap = _rise_position_snapshot(wallet, requested_symbol)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="position_state",
            exchange=name,
            account=account,
            code="POSITIONS_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )
    size = snap.get("size")
    if size is not None and size <= 0:
        side = "flat"
    else:
        side = snap.get("side") or "flat"
    if side == "flat":
        position = CanonicalPosition(
            symbol=requested_symbol,
            side="flat",
            size="0",
            entry_price=None,
            pnl="0",
            tp=None,
            sl=None,
        )
    else:
        abs_size = abs(size) if size is not None else Decimal("0")
        entry = snap.get("entry_price")
        # Clamp: Rise portfolio may report the position with the platform's
        # own precision; keep the venue-reported value verbatim (strings).
        position = CanonicalPosition(
            symbol=requested_symbol,
            side=side,
            size=_decimal_text(abs_size) if abs_size is not None else "0",
            entry_price=_decimal_text(entry) if entry is not None and entry > 0 else str(entry or "0"),
            pnl="0",
            tp=None,
            sl=None,
        )
    return make_success(
        operation="position_state",
        exchange=name,
        account=account,
        positions=[position],
    )


def _execute_get_order_state(request: Dict[str, Any]) -> CanonicalResponse:
    return make_failure(
        operation="get_order_state",
        exchange=name,
        account=str(request.get("account") or "").strip(),
        code="NOT_IMPLEMENTED",
        message="Phase 1 wiring only; full adapter lands in GF-attach phase.",
    )


def _execute_get_order_state_by_client_id(request: Dict[str, Any]) -> CanonicalResponse:
    return make_failure(
        operation="get_order_state_by_client_id",
        exchange=name,
        account=str(request.get("account") or "").strip(),
        code="NOT_IMPLEMENTED",
        message="Phase 3 wires client-id reads; Phase 1 only adds market_immediate.",
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

    # Phase 3: optional caller-supplied GoldenFibo V2 client_order_id.
    raw_cid = (
        request.get("client_order_id")
        if "client_order_id" in request
        else (
            request.get("client_id")
            or request.get("client_order_index")
        )
    )
    try:
        normalized_cid = _rise_normalize_v2_client_order_id(raw_cid)
    except ValueError as exc:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="RISE_CLIENT_ORDER_ID_UNSUPPORTED",
            message=str(exc),
        )
    _submitted_client_id = normalized_cid or RISE_V2_DEFAULT_ID

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
            "client_order_id": _submitted_client_id,
            "builder_id": 0,
            "permit": permit,
        }
        _pre_open_orders_payload = _fetch_open_orders_payload(wallet)
        reconcile_on_unverified = _coerce_bool(request.get("reconcile_on_unverified"))
        # For gated reconciliation we need a pre-submission position snapshot.
        pre_position = None
        if reconcile_on_unverified:
            pre_position = _rise_position_snapshot(wallet, requested_symbol)
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
        verified, exchange_order_id, _matched_order = _verify_new_order_submission(
            wallet=wallet,
            market_cache=cache,
            market_id=str(market_id),
            side_int=side_int,
            size_steps=size_steps,
            price_ticks=price_ticks,
            response_order_id=response_order_id,
        )
        # ALWAYS preserve the raw Rise exchange order id across every accepted
        # submit path. Even if a post-submit verifier cannot find the order in
        # openOrders (instant fill / partial / gone), GoldenFibo still needs the
        # raw id for pending ownership, exact cancel, and restart reconciliation.
        if not exchange_order_id and response_order_id:
            exchange_order_id = response_order_id

        classification: Optional[str] = None
        remaining_size: Optional[Decimal] = None
        if verified:
            classification = "OPEN"
        elif reconcile_on_unverified:
            # Gate-only: classify via position evidence so GoldenFibo always
            # receives a usable result instead of a VERIFICATION_FAILED raise.
            post_position = _rise_position_snapshot(wallet, requested_symbol)
            still_active = (
                _order_id_in_open_orders(wallet, cache, str(market_id), exchange_order_id)
                if exchange_order_id
                else False
            )
            pre_size = Decimal(str((pre_position or {}).get("size") or "0"))
            post_size = Decimal(str(post_position.get("size") or "0"))
            classification = _classify_rise_limit_result(
                requested_volume=requested_volume,
                submitted_volume=submitted_volume,
                side_int=side_int,
                pre_position_size=pre_size,
                post_position_size=post_size,
                pre_position_side=str((pre_position or {}).get("side") or "flat"),
                post_position_side=str(post_position.get("side") or "flat"),
                still_in_open_orders=still_active,
            )
            # Remaining = submitted minus confirmed growth (never below zero).
            growth = max(Decimal("0"), post_size - pre_size)
            remaining_size = max(Decimal("0"), submitted_volume - growth)

        result = CanonicalOrderResult(
            symbol=requested_symbol,
            side=requested_side,
            order_type=order_type,
            requested_volume=_decimal_text(requested_volume),
            requested_price=_decimal_text(requested_price),
            submitted_volume=_decimal_text(submitted_volume),
            submitted_price=_decimal_text(submitted_price),
            verified=verified or classification in ("OPEN", "FILLED", "PARTIALLY_FILLED"),
            status=(
                "success"
                if verified
                else (
                    classification.lower() if classification
                    else "partial"
                )
            ),
            exchange_order_id=exchange_order_id,
            client_order_id=_submitted_client_id,
        )
        if verified:
            return make_success(operation="new_order", exchange=name, account=account, order=result)
        if reconcile_on_unverified and classification:
            # GoldenFibo gated path: return a usable success even when the order
            # was not found in openOrders, so reconcile-from-position can proceed.
            return make_success(
                operation="new_order",
                exchange=name,
                account=account,
                order=result,
                order_state={
                    "classification": classification,
                    "verified": result.verified,
                    "requested_size": _decimal_text(requested_volume),
                    "remaining_size": (
                        _decimal_text(remaining_size) if remaining_size is not None else "0"
                    ),
                },
            )
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
        child_order_ids: List[str | int] = []
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
                accepted_payload = dict(payload)
                if child_response.order and child_response.order.exchange_order_id is not None:
                    accepted_payload["response_order_id"] = str(child_response.order.exchange_order_id)
                accepted_payloads.append(accepted_payload)
                accepted_count += 1
                accepted_volume += child_volume
                if child_response.order and child_response.order.exchange_order_id is not None:
                    child_order_ids.append(str(child_response.order.exchange_order_id))
                continue
            reported_child_order_ids: List[str | int] = list(child_order_ids)
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
                child_order_ids=reported_child_order_ids or None,
                batches=[{"batch_index": 1, "submitted_order_count": accepted_count, "accepted_child_count": accepted_count, "child_order_ids": reported_child_order_ids}],
            )
            return make_failure(
                operation="ladder",
                exchange=name,
                account=account,
                code=child_response.error.code if child_response.error else "ORDER_SUBMISSION_FAILED",
                message=child_response.error.message if child_response.error else "Ladder submission failed.",
                ladder=ladder,
            )

        verified, verified_order_ids, _matched_rows = _verify_rise_ladder_submission(
            wallet=wallet,
            market_cache=cache,
            market_id=str(market.get("market_id") or ""),
            side_int=RISE_SIDE_TO_INT[requested_side],
            expected_payloads=accepted_payloads,
        )
        reported_child_order_ids: List[str | int] = list(verified_order_ids or child_order_ids)
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
            child_order_ids=reported_child_order_ids or None,
            batches=[{"batch_index": 1, "submitted_order_count": accepted_count, "accepted_child_count": accepted_count, "child_order_ids": reported_child_order_ids}],
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
    if operation == "cancel_order":
        return _execute_cancel_order(account, normalized_request)
    if operation == "close_position":
        return _execute_close_position(account, normalized_request)
    if operation == "resolve_instrument":
        return _execute_resolve_instrument(normalized_request)
    if operation == "market_constraints":
        return _execute_market_constraints(normalized_request)
    if operation == "market_price":
        return _execute_market_price(normalized_request)
    if operation == "position_state":
        return _execute_position_state(account, normalized_request)
    if operation == "get_order_state":
        return _execute_get_order_state(normalized_request)
    if operation == "get_order_state_by_client_id":
        return _execute_get_order_state_by_client_id(normalized_request)
    if operation == "market_immediate":
        # Phase 1: bounded-limit + IOC slip within existing PlaceOrder.
        return _execute_market_immediate(account, normalized_request)
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code="NOT_IMPLEMENTED",
        message="Not implemented yet.",
    )



# ---------------------------------------------------------------------------
# Phase 1 / 2 / 4 wiring:
# Bounded-limit + IOC slip for immediate execution (market_immediate).
# Single-order cancel-by-id (cancel_order).
# Read-only wrappers required by GoldenFibo adapter contract.
# ---------------------------------------------------------------------------

RISE_DEFAULT_IMMEDIATE_SLIP_PCT = Decimal("0.01")  # 1.00% of reference mark
RISE_IMMEDIATE_VERIFY_WAIT_SECONDS = 6.0
RISE_IMMEDIATE_VERIFY_POLL_SECONDS = 0.5
