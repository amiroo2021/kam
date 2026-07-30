"""Lighter exchange agent.

This module owns Lighter-specific behavior for the /trade stack.

Current scope:
- Credential discovery from ``LIGHTER_<ACCOUNT>_*`` variables in the live
  environment or ``$HERMES_HOME/.env``.
- Dynamic account discovery across the Arbitrum and Robinhood deployments.
- Authenticated account balance retrieval through Lighter's account endpoint.
- Canonical conversion into the exchange-agnostic TradeDesk / wizard contract.

TradeDesk and the Telegram wizard MUST remain exchange-agnostic and must not
parse ``LIGHTER_*`` environment variables or Lighter-native payloads.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import math
import os
import re
import threading
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

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

LIGHTER_MAX_CLIENT_ORDER_INDEX = (1 << 48) - 1
LIGHTER_VERIFY_ATTEMPTS = 4
LIGHTER_VERIFY_DELAY_SECONDS = 0.25
LIGHTER_CLOSE_MAX_SLIPPAGE = 0.05

name = "lighter"

LIGHTER_REQUIRED_FIELDS = (
    "CHAIN",
    "ACCOUNT_INDEX",
    "APIKEY_INDEX",
    "PUBLIC_KEY",
    "PRIVATE_KEY",
)
LIGHTER_CHAIN_URLS = {
    "ARBITRUM": "https://mainnet.zklighter.elliot.ai",
    "ROBINHOOD": "https://api.rh.lighter.xyz",
}
_LIGHTER_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_LIGHTER_INT_PATTERN = re.compile(r"^[0-9]+$")
_LIGHTER_HEX_PATTERN = re.compile(r"^(0x)?[0-9a-fA-F]+$")


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


def _combined_lighter_env() -> Dict[str, str]:
    values: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith("LIGHTER_"):
            values[key] = (value or "").strip()
    for key, value in _load_dotenv_values(_hermes_home() / ".env").items():
        if key.startswith("LIGHTER_"):
            values.setdefault(key, (value or "").strip())
    return values


def _normalize_chain(value: Any) -> Optional[str]:
    chain = str(value or "").strip().upper()
    return chain if chain in LIGHTER_CHAIN_URLS else None


def _is_strict_int(value: Any) -> bool:
    return bool(_LIGHTER_INT_PATTERN.match(str(value or "").strip()))


def _is_hex(value: Any) -> bool:
    text = str(value or "").strip()
    if not text or not _LIGHTER_HEX_PATTERN.match(text):
        return False
    try:
        int(text[2:] if text.lower().startswith("0x") else text, 16)
    except ValueError:
        return False
    return True


def _display_chain(chain: str) -> str:
    lowered = str(chain or "").strip().lower()
    if lowered == "arbitrum":
        return "Arbitrum"
    if lowered == "robinhood":
        return "Robinhood"
    return str(chain or "").strip().title()


def _discover_account_entries() -> List[Dict[str, str]]:
    grouped: Dict[str, Dict[str, str]] = {}
    for key, value in _combined_lighter_env().items():
        if not key.startswith("LIGHTER_"):
            continue
        remainder = key[len("LIGHTER_"):]
        for field in LIGHTER_REQUIRED_FIELDS:
            suffix = f"_{field}"
            if not remainder.endswith(suffix):
                continue
            alias = remainder[: -len(suffix)]
            if not alias or not _LIGHTER_ALIAS_PATTERN.match(alias) or not value:
                break
            grouped.setdefault(alias, {})[field] = value
            break

    discovered: List[Dict[str, str]] = []
    for alias in sorted(grouped.keys()):
        entry = grouped[alias]
        if any(not entry.get(field) for field in LIGHTER_REQUIRED_FIELDS):
            continue
        chain = _normalize_chain(entry.get("CHAIN"))
        if not chain:
            continue
        if not _is_strict_int(entry.get("ACCOUNT_INDEX")):
            continue
        if not _is_strict_int(entry.get("APIKEY_INDEX")):
            continue
        if not _is_hex(entry.get("PUBLIC_KEY")) or not _is_hex(entry.get("PRIVATE_KEY")):
            continue
        account = alias.lower()
        discovered.append(
            {
                "account": account,
                "chain": chain,
                "label": f"{account} — {_display_chain(chain)}",
            }
        )
    return discovered


def _lookup_credentials(account: str) -> Optional[Dict[str, Any]]:
    alias = str(account or "").strip().upper()
    if not alias or not _LIGHTER_ALIAS_PATTERN.match(alias):
        return None
    env = _combined_lighter_env()
    values = {field: str(env.get(f"LIGHTER_{alias}_{field}", "")).strip() for field in LIGHTER_REQUIRED_FIELDS}
    if any(not values[field] for field in LIGHTER_REQUIRED_FIELDS):
        return None
    chain = _normalize_chain(values["CHAIN"])
    if not chain or not _is_strict_int(values["ACCOUNT_INDEX"]) or not _is_strict_int(values["APIKEY_INDEX"]):
        return None
    if not _is_hex(values["PUBLIC_KEY"]) or not _is_hex(values["PRIVATE_KEY"]):
        return None
    return {
        "account": alias.lower(),
        "chain": chain,
        "label": f"{alias.lower()} — {_display_chain(chain)}",
        "account_index": int(values["ACCOUNT_INDEX"]),
        "api_key_index": int(values["APIKEY_INDEX"]),
        "public_key": values["PUBLIC_KEY"],
        "private_key": values["PRIVATE_KEY"],
        "base_url": LIGHTER_CHAIN_URLS[chain].rstrip("/"),
    }


def list_accounts() -> List[Dict[str, str]]:
    return _discover_account_entries()


def capabilities() -> List[str]:
    return ["balance", "positions_orders", "positions_management", "new_order", "ladder", "cancel_orders"]


def _build_signer_client(credentials: Dict[str, Any]) -> Any:
    candidates = [
        ("lighter", "SignerClient"),
        ("lighter_sdk", "SignerClient"),
        ("zklighter", "SignerClient"),
    ]
    last_error: Optional[Exception] = None
    for module_name, attr in candidates:
        try:
            module = importlib.import_module(module_name)
            signer_cls = getattr(module, attr)
            return signer_cls(
                credentials["base_url"],
                credentials["account_index"],
                {credentials["api_key_index"]: credentials["private_key"]},
            )
        except ModuleNotFoundError as exc:
            last_error = exc
            continue
        except AttributeError as exc:
            last_error = exc
            continue
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Lighter SignerClient initialization failed: {exc}") from exc
    raise RuntimeError(
        "Lighter SignerClient is unavailable. Install the official Lighter SDK before using the lighter agent."
    ) from last_error


def _mint_auth_token(credentials: Dict[str, Any]) -> str:
    async def _create_token() -> str:
        signer = _build_signer_client(credentials)
        try:
            result = signer.create_auth_token_with_expiry(api_key_index=credentials["api_key_index"])
            if isinstance(result, tuple):
                token = result[0] if len(result) >= 1 else None
                err = result[1] if len(result) >= 2 else None
            else:
                token = result
                err = None
            if err:
                raise RuntimeError(f"Lighter auth token generation failed: {err}")
            token_text = str(token or "").strip()
            if not token_text:
                raise RuntimeError("Lighter auth token generation returned an empty token")
            return token_text
        finally:
            api_client = getattr(signer, "api_client", None)
            if api_client is not None and hasattr(api_client, "close"):
                try:
                    await api_client.close()
                except Exception:
                    pass

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_create_token())

    result: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["token"] = asyncio.run(_create_token())
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=_runner, name="lighter-auth-token", daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return str(result.get("token") or "")


def _fetch_account_entry(request: Dict[str, Any]) -> Dict[str, Any]:
    account_name = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account_name)
    if not credentials:
        raise RuntimeError("Unknown or invalid Lighter account configuration")

    auth_token = _mint_auth_token(credentials)
    response = requests.get(
        f"{credentials['base_url']}/api/v1/account",
        params={
            "by": "index",
            "value": str(credentials["account_index"]),
            "auth": auth_token,
        },
        headers={"Accept": "application/json"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise RuntimeError("Lighter account response missing accounts[]")

    target = None
    for entry in accounts:
        if not isinstance(entry, dict):
            continue
        raw_entry_index = entry.get("account_index")
        if raw_entry_index is None:
            continue
        try:
            entry_index = int(str(raw_entry_index))
        except Exception:  # noqa: BLE001
            continue
        if entry_index == credentials["account_index"]:
            target = entry
            break
    if target is None:
        raise RuntimeError("Configured Lighter account_index was not present in the account response")

    return {
        "_raw": payload,
        "target": target,
        "account": credentials["account"],
        "chain": credentials["chain"],
        "label": credentials["label"],
        "credentials": credentials,
        "auth_token": auth_token,
    }


def _quantize_2(value: Any) -> str:
    decimal_value = Decimal(str(value or "0"))
    return format(decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


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
    if places <= 0:
        return format(quantized, ".0f")
    return format(quantized, f".{places}f")


def _decimal_places(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        decimal_value = Decimal(text)
    except Exception:
        return 0
    sign, digits, exponent = decimal_value.as_tuple()
    exponent = int(exponent)
    if exponent >= 0:
        return max(0, -exponent)
    return -exponent


def _fetch_market_symbol_map(base_url: str) -> Dict[int, Dict[str, Any]]:
    response = requests.get(
        f"{base_url}/api/v1/orderBookDetails",
        headers={"Accept": "application/json"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    combined: List[Dict[str, Any]] = []
    if isinstance(payload.get("order_book_details"), list):
        combined.extend(item for item in payload.get("order_book_details") or [] if isinstance(item, dict))
    if isinstance(payload.get("spot_order_book_details"), list):
        combined.extend(item for item in payload.get("spot_order_book_details") or [] if isinstance(item, dict))
    market_map: Dict[int, Dict[str, Any]] = {}
    for entry in combined:
        try:
            market_id = int(entry.get("market_id"))
        except Exception:  # noqa: BLE001
            continue
        symbol = str(entry.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        market_map[market_id] = {
            "symbol": symbol,
            "price_precision": entry.get("price_decimals") or entry.get("supported_price_decimals"),
        }
    return market_map


def _fetch_market_catalog(base_url: str) -> List[Dict[str, Any]]:
    response = requests.get(
        f"{base_url}/api/v1/orderBookDetails",
        headers={"Accept": "application/json"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    combined: List[Dict[str, Any]] = []
    if isinstance(payload.get("order_book_details"), list):
        combined.extend(item for item in payload.get("order_book_details") or [] if isinstance(item, dict))
    if isinstance(payload.get("spot_order_book_details"), list):
        combined.extend(item for item in payload.get("spot_order_book_details") or [] if isinstance(item, dict))
    return combined


def _resolve_market(base_url: str, requested_symbol: str) -> Optional[Dict[str, Any]]:
    symbol = str(requested_symbol or "").strip().upper()
    if not symbol:
        return None
    candidates = []
    for entry in _fetch_market_catalog(base_url):
        entry_symbol = str(entry.get("symbol") or "").strip().upper()
        if entry_symbol != symbol:
            continue
        candidates.append(entry)
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            0 if str(item.get("market_type") or "").strip().lower() == "perp" else 1,
            0 if str(item.get("status") or "").strip().lower() == "active" else 1,
            int(item.get("market_id") or 0),
        )
    )
    chosen = dict(candidates[0])
    try:
        chosen["market_id"] = int(chosen.get("market_id"))
    except Exception:  # noqa: BLE001
        return None
    return chosen


def _quantize_down(value: Decimal, places: int) -> Decimal:
    quantum = Decimal("1").scaleb(-max(0, places))
    return value.quantize(quantum, rounding=ROUND_DOWN)


def _to_scaled_int(value: Decimal, places: int) -> int:
    scale = Decimal(10) ** max(0, places)
    scaled = (value * scale).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return int(scaled)


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
    weights: List[Decimal] = []
    span = Decimal(order_count - 1)
    for index in range(order_count):
        z = Decimal("3") * (span - Decimal(index)) / span
        weight = math.exp(-(float(z) ** 2) / 2.0)
        weights.append(Decimal(str(weight)))
    return weights


def _build_ladder_prices(start_price: Decimal, end_price: Decimal, order_count: int, price_decimals: int) -> List[Decimal]:
    if order_count <= 0:
        return []
    if order_count == 1:
        return [_quantize_down((start_price + end_price) / Decimal("2"), price_decimals)]
    step = (end_price - start_price) / Decimal(order_count - 1)
    prices = [_quantize_down(start_price + (step * Decimal(index)), price_decimals) for index in range(order_count)]
    if start_price <= end_price:
        for index in range(1, len(prices)):
            if prices[index] < prices[index - 1]:
                prices[index] = prices[index - 1]
    else:
        for index in range(1, len(prices)):
            if prices[index] > prices[index - 1]:
                prices[index] = prices[index - 1]
    return prices


def _allocate_ladder_sizes(total_volume: Decimal, order_count: int, size_decimals: int, distribution: str) -> tuple[List[Decimal], Decimal]:
    increment = Decimal("1").scaleb(-max(0, size_decimals))
    total_units = int((total_volume / increment).to_integral_value(rounding=ROUND_HALF_UP))
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
    sizes = [Decimal(units) * increment for units in allocation]
    return sizes, Decimal(total_units) * increment


def _build_lighter_ladder_children(*, side: str, distribution: str, order_count: int, total_volume: Decimal, start_price: Decimal, end_price: Decimal, size_decimals: int, price_decimals: int, min_base_amount: Optional[Decimal]) -> tuple[List[Dict[str, Decimal]], Decimal, int]:
    prices = _build_ladder_prices(start_price, end_price, order_count, price_decimals)
    sizes, submitted_volume = _allocate_ladder_sizes(total_volume, order_count, size_decimals, distribution)
    children: List[Dict[str, Decimal]] = []
    omitted_below_minimum = 0
    for price, size in zip(prices, sizes):
        if size <= 0:
            continue
        if min_base_amount is not None and size < min_base_amount:
            omitted_below_minimum += 1
            continue
        if children and children[-1]["price"] == price:
            children[-1]["size"] = children[-1]["size"] + size
            continue
        children.append({"price": price, "size": size})
    kept_volume = sum((child["size"] for child in children), Decimal("0"))
    return children, kept_volume, omitted_below_minimum


def _verify_ladder_orders(*, orders: List[Dict[str, Any]], market_id: int, side: str, children: List[Dict[str, Decimal]]) -> tuple[bool, int, List[int]]:
    matched_ids: List[int] = []
    for child in children:
        matched = None
        for order in orders:
            if not isinstance(order, dict):
                continue
            try:
                order_market_id = int(order.get("market_index"))
            except Exception:  # noqa: BLE001
                continue
            if order_market_id != market_id:
                continue
            if ("sell" if bool(order.get("is_ask")) else "buy") != side:
                continue
            if _decimal_text(order.get("remaining_base_amount") or order.get("initial_base_amount")) != _decimal_text(child["size"]):
                continue
            if _decimal_text(order.get("price")) != _decimal_text(child["price"]):
                continue
            matched = order
            break
        if matched is None:
            return False, len(matched_ids), matched_ids
        raw_order_id = str(matched.get("order_id") or "").strip()
        try:
            matched_ids.append(int(raw_order_id))
        except Exception:  # noqa: BLE001
            pass
    return True, len(children), matched_ids


def _submit_cancel_order(
    credentials: Dict[str, Any],
    order: Optional[Dict[str, Any]] = None,
    *,
    market_id: Optional[int] = None,
    order_id: Optional[int] = None,
    reason: str = "cancel",
) -> Dict[str, Any]:
    resolved_market_id = market_id
    resolved_order_id = order_id
    if order is not None:
        try:
            resolved_market_id = int(str(order.get("market_index") or resolved_market_id or 0))
        except Exception:  # noqa: BLE001
            pass
        try:
            resolved_order_id = int(str(order.get("order_index") or order.get("order_id") or resolved_order_id or 0))
        except Exception:  # noqa: BLE001
            pass
    if int(resolved_market_id or 0) <= 0 or int(resolved_order_id or 0) <= 0:
        raise RuntimeError(f"Lighter order cancellation failed: missing order metadata for {reason}")

    async def _run_cancel() -> Dict[str, Any]:
        signer = _build_signer_client(credentials)
        try:
            tx, api_response, error = await signer.cancel_order(
                int(resolved_market_id),
                int(resolved_order_id),
                api_key_index=credentials["api_key_index"],
            )
            if error:
                raise RuntimeError(f"Lighter order cancellation failed: {error}")
            return {
                "exchange_order_id": int(resolved_order_id),
                "nonce": getattr(tx, "nonce", None),
                "tx_hash": getattr(api_response, "tx_hash", None),
            }
        finally:
            api_client = getattr(signer, "api_client", None)
            if api_client is not None and hasattr(api_client, "close"):
                try:
                    await api_client.close()
                except Exception:
                    pass

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run_cancel())

    result: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(_run_cancel())
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=_runner, name="lighter-cancel-order", daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return dict(result.get("value") or {})


def _execute_cancel_order_group(request: Dict[str, Any]) -> CanonicalResponse:
    account_name = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account_name)
    if not credentials:
        return make_failure(operation="cancel_order_group", exchange=name, account=account_name, code="UNKNOWN_ACCOUNT", message="Unknown or invalid Lighter account configuration")

    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_side = str(request.get("side") or "").strip().lower()
    if not requested_symbol:
        return make_failure(operation="cancel_order_group", exchange=name, account=account_name, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in {"buy", "sell"}:
        return make_failure(operation="cancel_order_group", exchange=name, account=account_name, code="INVALID_SIDE", message="Side must be buy or sell.")

    market = _resolve_market(credentials["base_url"], requested_symbol)
    if market is None:
        return make_failure(operation="cancel_order_group", exchange=name, account=account_name, code="INSTRUMENT_NOT_FOUND", message="Instrument not found.")
    market_id_raw = market.get("market_id")
    market_id = int(str(market_id_raw or 0))

    try:
        pre_orders = _fetch_active_orders(credentials, _mint_auth_token(credentials))
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="cancel_order_group", exchange=name, account=account_name, code="OPEN_ORDERS_UNAVAILABLE", message=sanitize_error_message(str(exc)))

    target_orders: List[Dict[str, Any]] = []
    target_ids: List[int] = []
    non_target_ids: List[int] = []
    for order in pre_orders:
        if not isinstance(order, dict):
            continue
        try:
            order_market_id = int(str(order.get("market_index") or 0))
        except Exception:  # noqa: BLE001
            continue
        raw_order_id = str(order.get("order_id") or "").strip()
        try:
            parsed_order_id = int(raw_order_id)
        except Exception:  # noqa: BLE001
            continue
        side = "sell" if bool(order.get("is_ask")) else "buy"
        if order_market_id == market_id and side == requested_side:
            target_orders.append(order)
            target_ids.append(parsed_order_id)
        else:
            non_target_ids.append(parsed_order_id)

    if not target_orders:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account_name,
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

    cancelled_count = 0
    partial = False
    status_code = ""
    status_message = ""
    batches: List[Dict[str, Any]] = []
    for order_id in target_ids:
        try:
            _submit_cancel_order(credentials, market_id=market_id, order_id=order_id)
        except Exception as exc:  # noqa: BLE001
            partial = True
            status_code = "CANCEL_FAILED"
            status_message = sanitize_error_message(str(exc))
            batches.append({"submitted": 1, "accepted": 0, "ok": False, "reason": status_code})
            break
        cancelled_count += 1
        batches.append({"submitted": 1, "accepted": 1, "ok": True})

    try:
        post_orders = _fetch_active_orders(credentials, _mint_auth_token(credentials))
    except Exception as exc:  # noqa: BLE001
        post_orders = []
        partial = True
        if not status_code:
            status_code = "VERIFY_UNAVAILABLE"
            status_message = sanitize_error_message(str(exc))

    post_ids: set[int] = set()
    for order in post_orders:
        if not isinstance(order, dict):
            continue
        raw_order_id = str(order.get("order_id") or "").strip()
        try:
            post_ids.add(int(raw_order_id))
        except Exception:  # noqa: BLE001
            continue
    remaining_target_count = sum(1 for oid in target_ids if oid in post_ids)
    confirmed_absent_count = len(target_ids) - remaining_target_count
    non_target_preserved = all(oid in post_ids for oid in non_target_ids)
    verified = remaining_target_count == 0 and non_target_preserved and cancelled_count == len(target_ids) and not partial

    cancel_result = CanonicalCancelGroupResult(
        symbol=requested_symbol,
        side=requested_side,
        targeted_order_count=len(target_ids),
        cancelled_order_count=cancelled_count,
        confirmed_absent_count=confirmed_absent_count,
        remaining_target_count=remaining_target_count,
        verified=verified,
        partial=partial or not verified,
        status="success" if verified else ("partial" if cancelled_count else "failed"),
        batch_count=len(batches),
        batches=batches or None,
    )
    if verified:
        return make_success(operation="cancel_order_group", exchange=name, account=account_name, cancel_group=cancel_result)
    return make_failure(
        operation="cancel_order_group",
        exchange=name,
        account=account_name,
        code=status_code or ("VERIFICATION_FAILED" if cancelled_count else "NO_TARGET_ORDERS"),
        message=status_message or "Cancellation was only partially completed.",
        cancel_group=cancel_result,
    )


def _execute_ladder(request: Dict[str, Any]) -> CanonicalResponse:
    account_name = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account_name)
    if not credentials:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="UNKNOWN_ACCOUNT", message="Unknown or invalid Lighter account configuration")

    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_side = str(request.get("side") or "").strip().lower()
    distribution = str(request.get("distribution") or "").strip().lower()
    try:
        order_count = int(str(request.get("order_count") or "").strip())
    except Exception:  # noqa: BLE001
        order_count = 0
    total_volume = _decimal_or_none(request.get("total_volume"))
    start_price = _decimal_or_none(request.get("start_price"))
    end_price = _decimal_or_none(request.get("end_price"))

    if not requested_symbol:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in {"buy", "sell"}:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="INVALID_SIDE", message="Side must be buy or sell.")
    if order_count <= 0:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="INVALID_ORDER_COUNT", message="Order count must be positive.")
    if total_volume is None or total_volume <= 0:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="INVALID_VOLUME", message="Total volume must be positive.")
    if start_price is None or end_price is None:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="INVALID_PRICE", message="Start and end price are required.")
    if requested_side == "buy" and end_price >= start_price:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="INVALID_LADDER_DIRECTION", message="BUY ladders require end price below start price.")
    if requested_side == "sell" and end_price <= start_price:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="INVALID_LADDER_DIRECTION", message="SELL ladders require end price above start price.")

    market = _resolve_market(credentials["base_url"], requested_symbol)
    if market is None:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="INSTRUMENT_NOT_FOUND", message="Instrument not found.")

    size_decimals = int(market.get("size_decimals") or market.get("supported_size_decimals") or 0)
    price_decimals = int(market.get("price_decimals") or market.get("supported_price_decimals") or 0)
    min_base_amount = _decimal_or_none(market.get("min_base_amount"))

    try:
        children, submitted_volume, omitted_below_minimum = _build_lighter_ladder_children(
            side=requested_side,
            distribution=distribution,
            order_count=order_count,
            total_volume=total_volume,
            start_price=start_price,
            end_price=end_price,
            size_decimals=size_decimals,
            price_decimals=price_decimals,
            min_base_amount=min_base_amount,
        )
    except ValueError as exc:
        code = str(exc) or "INVALID_LADDER_REQUEST"
        return make_failure(operation="ladder", exchange=name, account=account_name, code=code, message=sanitize_error_message(code.replace("_", " ").title()))

    def _ladder_result(*, verified: bool, partial: bool, status: str, submitted_count: int, accepted_child_count: int, child_order_ids: List[int], batches: List[Dict[str, Any]]) -> CanonicalLadderResult:
        return CanonicalLadderResult(
            symbol=requested_symbol,
            side=requested_side,
            distribution=distribution,
            requested_order_count=order_count,
            submitted_order_count=submitted_count,
            requested_volume=_decimal_text(total_volume),
            submitted_volume=_decimal_text(submitted_volume),
            batch_count=1 if submitted_count else 0,
            verified=verified,
            partial=partial,
            status=status,
            accepted_child_count=accepted_child_count,
            omitted_order_count=max(0, order_count - submitted_count) or None,
            omitted_below_minimum=omitted_below_minimum or None,
            child_order_ids=child_order_ids or None,
            batches=batches or None,
        )

    if len(children) < 2:
        result = _ladder_result(verified=False, partial=False, status="failed", submitted_count=0, accepted_child_count=0, child_order_ids=[], batches=[])
        return make_failure(operation="ladder", exchange=name, account=account_name, code="LADDER_TOO_FEW_VALID_CHILDREN", message="Fewer than two valid ladder children remain after preflight.", ladder=result)

    accepted = 0
    child_order_ids: List[int] = []
    batches: List[Dict[str, Any]] = []
    for child in children:
        try:
            result = _submit_new_order(
                credentials,
                market,
                side=requested_side,
                order_type="limit",
                requested_volume=child["size"],
                requested_price=child["price"],
                reduce_only=False,
            )
        except Exception as exc:  # noqa: BLE001
            ladder = _ladder_result(verified=False, partial=accepted > 0, status="partial" if accepted > 0 else "failed", submitted_count=accepted, accepted_child_count=accepted, child_order_ids=child_order_ids, batches=batches)
            return make_failure(operation="ladder", exchange=name, account=account_name, code="ORDER_SUBMISSION_FAILED", message=sanitize_error_message(str(exc)), ladder=ladder)
        accepted += 1
        raw_oid = result.get("exchange_order_id")
        if isinstance(raw_oid, int):
            child_order_ids.append(raw_oid)
        batches.append({"submitted": 1, "accepted": 1, "ok": True})

    post_orders: List[Dict[str, Any]] = []
    verified = False
    verified_count = 0
    verified_ids: List[int] = []
    last_verify_error: Optional[Exception] = None
    for attempt in range(LIGHTER_VERIFY_ATTEMPTS):
        try:
            post_orders = _fetch_active_orders(credentials, _mint_auth_token(credentials))
            verified, verified_count, verified_ids = _verify_ladder_orders(
                orders=post_orders,
                market_id=int(str(market.get("market_id") or 0)),
                side=requested_side,
                children=children,
            )
            if verified:
                break
        except Exception as exc:  # noqa: BLE001
            last_verify_error = exc
        if attempt < LIGHTER_VERIFY_ATTEMPTS - 1:
            time.sleep(LIGHTER_VERIFY_DELAY_SECONDS)

    if last_verify_error is not None and not verified and not post_orders:
        ladder = _ladder_result(verified=False, partial=True, status="partial", submitted_count=accepted, accepted_child_count=accepted, child_order_ids=child_order_ids, batches=batches)
        return make_failure(operation="ladder", exchange=name, account=account_name, code="VERIFICATION_FAILED", message=sanitize_error_message(str(last_verify_error)), ladder=ladder)
    ladder = _ladder_result(verified=verified, partial=not verified, status="success" if verified else "partial", submitted_count=accepted, accepted_child_count=verified_count if not verified else accepted, child_order_ids=verified_ids or child_order_ids, batches=batches)
    if verified:
        return make_success(operation="ladder", exchange=name, account=account_name, ladder=ladder)
    return make_failure(operation="ladder", exchange=name, account=account_name, code="VERIFICATION_FAILED", message="Ladder submission could not be verified.", ladder=ladder)


def _submit_new_order(
    credentials: Dict[str, Any],
    market: Dict[str, Any],
    *,
    side: str,
    order_type: str,
    requested_volume: Decimal,
    requested_price: Decimal,
    reduce_only: bool,
) -> Dict[str, Any]:
    async def _run_submit() -> Dict[str, Any]:
        signer = _build_signer_client(credentials)
        try:
            size_decimals = int(market.get("size_decimals") or market.get("supported_size_decimals") or 0)
            price_decimals = int(market.get("price_decimals") or market.get("supported_price_decimals") or 0)
            submitted_volume = _quantize_down(requested_volume, size_decimals)
            submitted_price = _quantize_down(requested_price, price_decimals)
            base_amount = _to_scaled_int(submitted_volume, size_decimals)
            price_int = _to_scaled_int(submitted_price, price_decimals)
            if base_amount <= 0:
                raise RuntimeError("Volume must be positive after Lighter size quantization")
            if price_int <= 0:
                raise RuntimeError("Price must be positive after Lighter price quantization")
            client_order_index = int(time.time_ns() % LIGHTER_MAX_CLIENT_ORDER_INDEX)
            if client_order_index <= 0:
                client_order_index = 1
            tx, api_response, error = await signer.create_order(
                int(market["market_id"]),
                client_order_index,
                base_amount,
                price_int,
                side == "sell",
                signer.ORDER_TYPE_LIMIT,
                signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                reduce_only=reduce_only,
                api_key_index=credentials["api_key_index"],
            )
            if error:
                raise RuntimeError(f"Lighter order submission failed: {error}")
            exchange_order_id = None
            tx_nonce = getattr(tx, "nonce", None)
            response_hash = getattr(api_response, "tx_hash", None)
            return {
                "submitted_volume": _decimal_text(submitted_volume),
                "submitted_price": _decimal_text(submitted_price),
                "exchange_order_id": exchange_order_id,
                "nonce": tx_nonce,
                "tx_hash": response_hash,
            }
        finally:
            api_client = getattr(signer, "api_client", None)
            if api_client is not None and hasattr(api_client, "close"):
                try:
                    await api_client.close()
                except Exception:
                    pass

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run_submit())

    result: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(_run_submit())
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=_runner, name="lighter-new-order", daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return dict(result.get("value") or {})


def _execute_new_order(request: Dict[str, Any]) -> CanonicalResponse:
    account_name = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account_name)
    if not credentials:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="UNKNOWN_ACCOUNT", message="Unknown or invalid Lighter account configuration")

    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_side = str(request.get("side") or "").strip().lower()
    order_type = str(request.get("order_type") or "limit").strip().lower() or "limit"
    reduce_only = bool(request.get("reduce_only") or False)
    requested_volume = _decimal_or_none(request.get("volume"))
    requested_price = _decimal_or_none(request.get("price"))

    if not requested_symbol:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in {"buy", "sell"}:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="INVALID_SIDE", message="Side must be buy or sell.")
    if order_type != "limit":
        return make_failure(operation="new_order", exchange=name, account=account_name, code="INVALID_ORDER_TYPE", message="Only limit orders are currently supported for Lighter.")
    if requested_volume is None or requested_volume <= 0:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="INVALID_VOLUME", message="Volume must be positive.")
    if requested_price is None or requested_price <= 0:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="INVALID_PRICE", message="Price must be positive.")

    market = _resolve_market(credentials["base_url"], requested_symbol)
    if market is None:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="INSTRUMENT_NOT_FOUND", message="Instrument not found.")

    size_decimals = int(market.get("size_decimals") or market.get("supported_size_decimals") or 0)
    price_decimals = int(market.get("price_decimals") or market.get("supported_price_decimals") or 0)
    submitted_volume_decimal = _quantize_down(requested_volume, size_decimals)
    submitted_price_decimal = _quantize_down(requested_price, price_decimals)
    min_base_amount = _decimal_or_none(market.get("min_base_amount"))
    if submitted_volume_decimal <= 0:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="INVALID_VOLUME", message="Volume must be positive after Lighter size quantization.")
    if min_base_amount is not None and submitted_volume_decimal < min_base_amount:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="INVALID_VOLUME", message="Volume is below Lighter minimum size.")
    if submitted_price_decimal <= 0:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="INVALID_PRICE", message="Price must be positive after Lighter price quantization.")

    try:
        _pre_orders = _fetch_active_orders(credentials, _mint_auth_token(credentials))
        submit_result = _submit_new_order(
            credentials,
            market,
            side=requested_side,
            order_type=order_type,
            requested_volume=submitted_volume_decimal,
            requested_price=submitted_price_decimal,
            reduce_only=reduce_only,
        )
        post_orders = _fetch_active_orders(credentials, _mint_auth_token(credentials))
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account_name,
            code="ORDER_SUBMISSION_FAILED",
            message=sanitize_error_message(str(exc)),
            order=CanonicalOrderResult(
                symbol=requested_symbol,
                side=requested_side,
                order_type=order_type,
                requested_volume=_decimal_text(requested_volume),
                requested_price=_decimal_text(requested_price),
                submitted_volume=_decimal_text(submitted_volume_decimal),
                submitted_price=_decimal_text(submitted_price_decimal),
                verified=False,
                status="failed",
            ),
        )

    matched_order = None
    for order in post_orders:
        if not isinstance(order, dict):
            continue
        try:
            market_id = int(order.get("market_index"))
        except Exception:  # noqa: BLE001
            continue
        if market_id != int(market["market_id"]):
            continue
        if ("sell" if bool(order.get("is_ask")) else "buy") != requested_side:
            continue
        if _decimal_text(order.get("remaining_base_amount") or order.get("initial_base_amount")) != _decimal_text(submitted_volume_decimal):
            continue
        if _decimal_text(order.get("price")) != _decimal_text(submitted_price_decimal):
            continue
        matched_order = order
        break

    exchange_order_id = submit_result.get("exchange_order_id")
    if exchange_order_id is None and isinstance(matched_order, dict):
        raw_order_id = str(matched_order.get("order_id") or "").strip()
        try:
            exchange_order_id = int(raw_order_id) if raw_order_id else None
        except Exception:  # noqa: BLE001
            exchange_order_id = None

    order_result = CanonicalOrderResult(
        symbol=requested_symbol,
        side=requested_side,
        order_type=order_type,
        requested_volume=_decimal_text(requested_volume),
        requested_price=_decimal_text(requested_price),
        submitted_volume=str(submit_result.get("submitted_volume") or _decimal_text(submitted_volume_decimal)),
        submitted_price=str(submit_result.get("submitted_price") or _decimal_text(submitted_price_decimal)),
        verified=matched_order is not None,
        status="success" if matched_order is not None else "partial",
        exchange_order_id=exchange_order_id,
    )
    if matched_order is not None:
        return make_success(operation="new_order", exchange=name, account=account_name, order=order_result)
    return make_failure(
        operation="new_order",
        exchange=name,
        account=account_name,
        code="VERIFICATION_FAILED",
        message="Order submission could not be verified.",
        order=order_result,
    )


def _fetch_active_orders(credentials: Dict[str, Any], auth_token: str) -> List[Dict[str, Any]]:
    response = requests.get(
        f"{credentials['base_url']}/api/v1/accountActiveOrders",
        params={"account_index": str(credentials["account_index"])},
        headers={"Accept": "application/json", "authorization": auth_token},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    orders = payload.get("orders")
    return orders if isinstance(orders, list) else []


def _normalize_positions(
    target: Dict[str, Any],
    market_map: Optional[Dict[int, Dict[str, Any]]] = None,
    price_precisions: Optional[Dict[int, int]] = None,
) -> List[CanonicalPosition]:
    positions_raw = target.get("positions")
    if not isinstance(positions_raw, list):
        return []
    rows: List[CanonicalPosition] = []
    for item in positions_raw:
        if not isinstance(item, dict):
            continue
        size_value = _decimal_or_none(item.get("position"))
        if size_value is None or size_value == 0:
            continue
        sign_value = int(item.get("sign") or 0)
        side = "long" if sign_value >= 0 else "short"
        try:
            market_id = int(item.get("market_id"))
        except Exception:  # noqa: BLE001
            market_id = -1
        market_info = (market_map or {}).get(market_id, {})
        symbol = str(item.get("symbol") or market_info.get("symbol") or f"MARKET-{market_id}").strip().upper()
        precision_value = market_info.get("price_precision")
        try:
            price_precision = int(precision_value) if precision_value is not None else int((price_precisions or {}).get(market_id, 1))
        except Exception:  # noqa: BLE001
            price_precision = int((price_precisions or {}).get(market_id, 1))
        entry_price_value = _decimal_or_none(item.get("avg_entry_price")) or Decimal("0")
        pnl_value = (_decimal_or_none(item.get("unrealized_pnl")) or Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        pnl_text = _decimal_text(pnl_value)
        if pnl_value > 0:
            pnl_text = f"+{pnl_text}"
        rows.append(
            CanonicalPosition(
                symbol=symbol,
                side=side,
                size=_decimal_text(abs(size_value)),
                entry_price=_format_decimal_places(entry_price_value, max(0, price_precision)),
                pnl=pnl_text,
            )
        )
    rows.sort(key=lambda item: (item.symbol, item.side))
    return rows


def _position_action_result(
    *,
    operation: str,
    symbol: str,
    verified: bool,
    price: Optional[str] = None,
    removed: Optional[bool] = None,
    current_side: Optional[str] = None,
    current_size: Optional[str] = None,
    message: Optional[str] = None,
    exchange_order_id: Optional[int] = None,
    status: str = "success",
) -> CanonicalPositionActionResult:
    return CanonicalPositionActionResult(
        operation=operation,
        symbol=symbol,
        verified=verified,
        price=price,
        removed=removed,
        status=status,
        exchange_order_id=exchange_order_id,
        current_side=current_side,
        current_size=current_size,
        message=message,
    )


def _classify_protection_orders(*, orders: List[Dict[str, Any]], market_id: int, closing_side: str) -> Dict[str, List[Dict[str, Any]]]:
    bucket: Dict[str, List[Dict[str, Any]]] = {"tp": [], "sl": []}
    for order in orders:
        if not isinstance(order, dict):
            continue
        status = str(order.get("status") or "").strip().lower()
        if status and status not in {"open", "pending", "in-progress"}:
            continue
        try:
            order_market_id = int(str(order.get("market_index") or 0))
        except Exception:  # noqa: BLE001
            continue
        if order_market_id != market_id:
            continue
        side = "sell" if bool(order.get("is_ask")) else "buy"
        if side != closing_side:
            continue
        if not bool(order.get("reduce_only")):
            continue
        order_type = str(order.get("type") or "").strip().lower()
        if order_type in {"take-profit", "take-profit-limit"}:
            bucket["tp"].append(order)
        elif order_type in {"stop-loss", "stop-loss-limit"}:
            bucket["sl"].append(order)
    return bucket


def _augment_positions_with_protection(
    positions: List[CanonicalPosition],
    *,
    target: Dict[str, Any],
    active_orders: List[Dict[str, Any]],
    market_map: Optional[Dict[int, Dict[str, Any]]] = None,
) -> List[CanonicalPosition]:
    raw_positions = target.get("positions") if isinstance(target, dict) else None
    if not isinstance(raw_positions, list):
        return positions
    raw_by_key: Dict[tuple[str, str], Dict[str, Any]] = {}
    for item in raw_positions:
        if not isinstance(item, dict):
            continue
        size_value = _decimal_or_none(item.get("position"))
        if size_value is None or size_value == 0:
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        side = "long" if int(item.get("sign") or 0) >= 0 else "short"
        raw_by_key[(symbol, side)] = item
    enriched: List[CanonicalPosition] = []
    for position in positions:
        raw = raw_by_key.get((position.symbol, position.side))
        if not raw:
            enriched.append(position)
            continue
        try:
            market_id = int(str(raw.get("market_id") or 0))
        except Exception:  # noqa: BLE001
            market_id = 0
        closing_side = "sell" if position.side == "long" else "buy"
        protection = _classify_protection_orders(orders=active_orders, market_id=market_id, closing_side=closing_side)
        tp_orders = protection["tp"]
        sl_orders = protection["sl"]
        market_info = (market_map or {}).get(market_id, {})
        price_precision = int(market_info.get("price_precision") or market_info.get("price_decimals") or market_info.get("supported_price_decimals") or 0)
        tp_price = _format_decimal_places(_decimal_or_none(tp_orders[0].get("trigger_price") or tp_orders[0].get("price")) or Decimal("0"), price_precision) if tp_orders else None
        sl_price = _format_decimal_places(_decimal_or_none(sl_orders[0].get("trigger_price") or sl_orders[0].get("price")) or Decimal("0"), price_precision) if sl_orders else None
        enriched.append(CanonicalPosition(
            symbol=position.symbol,
            side=position.side,
            size=position.size,
            entry_price=position.entry_price,
            pnl=position.pnl,
            tp=tp_price,
            sl=sl_price,
            tp_count=len(tp_orders) or None,
            sl_count=len(sl_orders) or None,
        ))
    return enriched


def _aggregate_open_orders(
    orders: List[Dict[str, Any]],
    market_map: Optional[Dict[int, Dict[str, Any]]] = None,
    fallback_symbols: Optional[Dict[int, str]] = None,
) -> List[CanonicalOrderGroup]:
    grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
    for order in orders:
        if not isinstance(order, dict):
            continue
        status = str(order.get("status") or "").strip().lower()
        if status and status not in {"open", "pending", "in-progress"}:
            continue
        try:
            market_id = int(order.get("market_index"))
        except Exception:  # noqa: BLE001
            continue
        market_info = (market_map or {}).get(market_id, {})
        symbol = str(market_info.get("symbol") or (fallback_symbols or {}).get(market_id) or f"MARKET-{market_id}").strip().upper()
        side = "sell" if bool(order.get("is_ask")) else "buy"
        size = _decimal_or_none(order.get("remaining_base_amount"))
        if size is None or size <= 0:
            size = _decimal_or_none(order.get("initial_base_amount")) or Decimal("0")
        if size <= 0:
            continue
        price = _decimal_or_none(order.get("price")) or Decimal("0")
        precision_value = market_info.get("price_precision")
        try:
            price_precision = int(precision_value) if precision_value is not None else _decimal_places(order.get("price"))
        except Exception:  # noqa: BLE001
            price_precision = _decimal_places(order.get("price"))
        key = (symbol, side)
        group = grouped.setdefault(
            key,
            {
                "symbol": symbol,
                "side": side,
                "order_count": 0,
                "total_size": Decimal("0"),
                "notional": Decimal("0"),
                "min_price": None,
                "max_price": None,
                "price_precision": price_precision,
            },
        )
        group["order_count"] += 1
        group["total_size"] += size
        group["notional"] += price * size
        group["price_precision"] = max(int(group.get("price_precision") or 0), price_precision)
        if group["min_price"] is None or price < group["min_price"]:
            group["min_price"] = price
        if group["max_price"] is None or price > group["max_price"]:
            group["max_price"] = price

    rows: List[CanonicalOrderGroup] = []
    for group in grouped.values():
        total_size: Decimal = group["total_size"]
        vwap = (group["notional"] / total_size) if total_size != 0 else Decimal("0")
        rows.append(
            CanonicalOrderGroup(
                symbol=group["symbol"],
                side=group["side"],
                order_count=int(group["order_count"]),
                total_size=_decimal_text(total_size),
                vwap=_format_decimal_places(vwap, int(group.get("price_precision") or 0)),
                min_price=_decimal_text(group["min_price"]),
                max_price=_decimal_text(group["max_price"]),
            )
        )
    rows.sort(key=lambda item: (item.symbol, item.side))
    return rows


def _to_portfolio_summary(target: Dict[str, Any]) -> CanonicalPortfolioSummary:
    return CanonicalPortfolioSummary(
        account_value=_quantize_2(target.get("total_asset_value", "0")),
        withdrawable=_quantize_2(target.get("available_balance", "0")),
        margin_used=_quantize_2(target.get("cross_initial_margin_requirement", "0")),
        total_position_value=_quantize_2(target.get("cross_asset_value", "0")),
        unit="USDC",
    )


def _balance(request: Dict[str, Any]) -> CanonicalResponse:
    fetched = _fetch_account_entry(request)
    target = fetched["target"]
    portfolio_summary = _to_portfolio_summary(target)
    balance = normalize_balance(target.get("total_asset_value", target.get("collateral", "0")), "USDC")
    return make_success(
        operation="balance",
        exchange=name,
        account=fetched["account"],
        balance=balance,
        portfolio_summary=portfolio_summary,
        positions=[],
    )


def _positions_orders(request: Dict[str, Any]) -> CanonicalResponse:
    fetched = _fetch_account_entry(request)
    target = fetched["target"]
    credentials = fetched["credentials"]
    auth_token = str(fetched["auth_token"] or "")
    active_orders = _fetch_active_orders(credentials, auth_token)
    market_map: Dict[int, Dict[str, Any]] = {}
    try:
        market_map = _fetch_market_symbol_map(credentials["base_url"])
    except Exception as exc:  # noqa: BLE001
        logger.info("Lighter orderBookDetails lookup failed; falling back to account symbols: %s", exc)
    fallback_symbols: Dict[int, str] = {}
    observed_price_precisions: Dict[int, int] = {}
    positions_raw = target.get("positions")
    if isinstance(positions_raw, list):
        for item in positions_raw:
            if not isinstance(item, dict):
                continue
            try:
                market_id = int(item.get("market_id"))
            except Exception:  # noqa: BLE001
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            if symbol:
                fallback_symbols[market_id] = symbol
    for order in active_orders:
        if not isinstance(order, dict):
            continue
        try:
            market_id = int(order.get("market_index"))
        except Exception:  # noqa: BLE001
            continue
        observed_price_precisions[market_id] = max(observed_price_precisions.get(market_id, 0), _decimal_places(order.get("price")))
    positions = _normalize_positions(target, market_map=market_map, price_precisions=observed_price_precisions)
    order_groups = _aggregate_open_orders(active_orders, market_map=market_map, fallback_symbols=fallback_symbols)
    return make_success(
        operation="positions_orders",
        exchange=name,
        account=fetched["account"],
        positions=positions,
        open_order_count=len(active_orders),
        order_groups=order_groups,
    )


def _positions_management(request: Dict[str, Any]) -> CanonicalResponse:
    fetched = _fetch_account_entry(request)
    target = fetched["target"]
    credentials = fetched["credentials"]
    auth_token = str(fetched["auth_token"] or "")
    active_orders = _fetch_active_orders(credentials, auth_token)
    market_map: Dict[int, Dict[str, Any]] = {}
    try:
        market_map = _fetch_market_symbol_map(credentials["base_url"])
    except Exception as exc:  # noqa: BLE001
        logger.info("Lighter orderBookDetails lookup failed during positions_management; falling back to account symbols: %s", exc)
    observed_price_precisions: Dict[int, int] = {}
    for order in active_orders:
        if not isinstance(order, dict):
            continue
        try:
            market_id = int(str(order.get("market_index") or 0))
        except Exception:  # noqa: BLE001
            continue
        observed_price_precisions[market_id] = max(observed_price_precisions.get(market_id, 0), _decimal_places(order.get("price")))
    positions = _normalize_positions(target, market_map=market_map, price_precisions=observed_price_precisions)
    positions = _augment_positions_with_protection(positions, target=target, active_orders=active_orders, market_map=market_map)
    return make_success(operation="positions_management", exchange=name, account=fetched["account"], positions=positions)


def _find_position_management_context(request: Dict[str, Any], *, include_active_orders: bool = True) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any], List[Dict[str, Any]], str, Decimal, str, str]:
    fetched = _fetch_account_entry(request)
    credentials = fetched["credentials"]
    target = fetched["target"]
    requested_symbol = str(request.get("symbol") or "").strip().upper()
    if not requested_symbol:
        raise ValueError("MISSING_SYMBOL")
    raw_positions = target.get("positions") if isinstance(target, dict) else None
    if not isinstance(raw_positions, list):
        raise LookupError("POSITION_NOT_FOUND")
    current_raw = None
    for item in raw_positions:
        if not isinstance(item, dict):
            continue
        if str(item.get("symbol") or "").strip().upper() != requested_symbol:
            continue
        size_value = _decimal_or_none(item.get("position"))
        if size_value in (None, Decimal("0")):
            continue
        current_raw = item
        break
    if current_raw is None:
        raise LookupError("POSITION_NOT_FOUND")
    market_map = _fetch_market_symbol_map(credentials["base_url"])
    market_id = int(str(current_raw.get("market_id") or 0))
    market = dict(market_map.get(market_id) or {})
    if not market.get("market_id") or market.get("size_decimals") is None or market.get("price_decimals") is None:
        resolved_market = _resolve_market(credentials["base_url"], requested_symbol)
        if resolved_market and int(str(resolved_market.get("market_id") or 0)) == market_id:
            market = dict(resolved_market)
    if not market:
        raise LookupError("INSTRUMENT_NOT_FOUND")
    if not market.get("market_id"):
        market["market_id"] = market_id
    sign_value = int(current_raw.get("sign") or 0)
    current_side = "long" if sign_value >= 0 else "short"
    closing_side = "sell" if current_side == "long" else "buy"
    size_decimals = int(market.get("size_decimals") or market.get("supported_size_decimals") or 0)
    current_size = _quantize_down(abs(_decimal_or_none(current_raw.get("position")) or Decimal("0")), size_decimals)
    auth_token = str(fetched.get("auth_token") or "")
    active_orders = _fetch_active_orders(credentials, auth_token) if include_active_orders else []
    return fetched, credentials, target, market, active_orders, current_side, current_size, closing_side, auth_token


def _submit_tpsl_order(credentials: Dict[str, Any], market: Dict[str, Any], *, operation: str, current_size: Decimal, closing_side: str, trigger_price: Decimal) -> Dict[str, Any]:
    async def _run_submit() -> Dict[str, Any]:
        signer = _build_signer_client(credentials)
        try:
            size_decimals = int(market.get("size_decimals") or market.get("supported_size_decimals") or 0)
            price_decimals = int(market.get("price_decimals") or market.get("supported_price_decimals") or 0)
            submitted_volume = _quantize_down(current_size, size_decimals)
            submitted_price = _quantize_down(trigger_price, price_decimals)
            base_amount = _to_scaled_int(submitted_volume, size_decimals)
            trigger_price_int = _to_scaled_int(submitted_price, price_decimals)
            client_order_index = int(time.time_ns() % LIGHTER_MAX_CLIENT_ORDER_INDEX) or 1
            submitter = signer.create_tp_order if operation == "set_tp" else signer.create_sl_order
            tx, api_response, error = await submitter(
                int(market["market_id"]),
                client_order_index,
                base_amount,
                trigger_price_int,
                trigger_price_int,
                closing_side == "sell",
                reduce_only=True,
                api_key_index=credentials["api_key_index"],
            )
            if error:
                raise RuntimeError(f"Lighter position action failed: {error}")
            return {"exchange_order_id": getattr(tx, "order_index", None), "submitted_price": _decimal_text(submitted_price), "submitted_volume": _decimal_text(submitted_volume), "tx_hash": getattr(api_response, "tx_hash", None)}
        finally:
            api_client = getattr(signer, "api_client", None)
            if api_client is not None and hasattr(api_client, "close"):
                try:
                    await api_client.close()
                except Exception:
                    pass
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run_submit())
    result: Dict[str, Any] = {}
    def _runner() -> None:
        try:
            result["value"] = asyncio.run(_run_submit())
        except Exception as exc:
            result["error"] = exc
    thread = threading.Thread(target=_runner, name=f"lighter-{operation}", daemon=True)
    thread.start(); thread.join()
    if "error" in result:
        raise result["error"]
    return dict(result.get("value") or {})


def _submit_close_position(credentials: Dict[str, Any], market: Dict[str, Any], *, current_size: Decimal, closing_side: str) -> Dict[str, Any]:
    async def _run_submit() -> Dict[str, Any]:
        signer = _build_signer_client(credentials)
        try:
            size_decimals = int(market.get("size_decimals") or market.get("supported_size_decimals") or 0)
            submitted_volume = _quantize_down(current_size, size_decimals)
            base_amount = _to_scaled_int(submitted_volume, size_decimals)
            client_order_index = int(time.time_ns() % LIGHTER_MAX_CLIENT_ORDER_INDEX) or 1
            tx, api_response, error = await signer.create_market_order_limited_slippage(
                int(market["market_id"]),
                client_order_index,
                base_amount,
                LIGHTER_CLOSE_MAX_SLIPPAGE,
                closing_side == "sell",
                reduce_only=True,
                api_key_index=credentials["api_key_index"],
            )
            if error:
                raise RuntimeError(f"Lighter close position failed: {error}")
            return {"exchange_order_id": getattr(tx, "order_index", None), "submitted_volume": _decimal_text(submitted_volume), "tx_hash": getattr(api_response, "tx_hash", None)}
        finally:
            api_client = getattr(signer, "api_client", None)
            if api_client is not None and hasattr(api_client, "close"):
                try:
                    await api_client.close()
                except Exception:
                    pass
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run_submit())
    result: Dict[str, Any] = {}
    def _runner() -> None:
        try:
            result["value"] = asyncio.run(_run_submit())
        except Exception as exc:
            result["error"] = exc
    thread = threading.Thread(target=_runner, name='lighter-close-position', daemon=True)
    thread.start(); thread.join()
    if "error" in result:
        raise result["error"]
    return dict(result.get("value") or {})


def _verify_tpsl_state(*, credentials: Dict[str, Any], auth_token: str, market_id: int, closing_side: str, operation: str, expected_price: Optional[str]) -> tuple[bool, Optional[int]]:
    target_key = 'tp' if operation == 'set_tp' else 'sl'
    matched_order_id: Optional[int] = None
    for attempt in range(LIGHTER_VERIFY_ATTEMPTS):
        orders = _fetch_active_orders(credentials, auth_token)
        bucket = _classify_protection_orders(orders=orders, market_id=market_id, closing_side=closing_side)
        current = bucket[target_key]
        if expected_price is None:
            if not current:
                return True, None
        else:
            for order in current:
                trigger_text = _decimal_text(_decimal_or_none(order.get('trigger_price') or order.get('price')))
                if trigger_text == _decimal_text(expected_price):
                    try:
                        matched_order_id = int(str(order.get('order_index') or order.get('order_id') or 0))
                    except Exception:
                        matched_order_id = None
                    return True, matched_order_id
        if attempt < LIGHTER_VERIFY_ATTEMPTS - 1:
            time.sleep(LIGHTER_VERIFY_DELAY_SECONDS)
    return False, matched_order_id


def _verify_position_closed(request: Dict[str, Any], *, symbol: str) -> bool:
    expected = str(symbol or '').strip().upper()
    for attempt in range(LIGHTER_VERIFY_ATTEMPTS):
        fetched = _fetch_account_entry(request)
        target = fetched.get('target') if isinstance(fetched, dict) else None
        raw_positions = target.get('positions') if isinstance(target, dict) else None
        still_open = False
        if isinstance(raw_positions, list):
            for item in raw_positions:
                if not isinstance(item, dict):
                    continue
                if str(item.get('symbol') or '').strip().upper() != expected:
                    continue
                size_value = _decimal_or_none(item.get('position'))
                if size_value not in (None, Decimal('0')):
                    still_open = True
                    break
        if not still_open:
            return True
        if attempt < LIGHTER_VERIFY_ATTEMPTS - 1:
            time.sleep(LIGHTER_VERIFY_DELAY_SECONDS)
    return False


def _execute_set_tpsl(request: Dict[str, Any], *, operation: str) -> CanonicalResponse:
    account_name = str(request.get('account') or '').strip()
    requested_symbol = str(request.get('symbol') or '').strip().upper()
    requested_price = _decimal_or_none(request.get('price'))
    if not requested_symbol:
        return make_failure(operation=operation, exchange=name, account=account_name, code='MISSING_SYMBOL', message='Symbol is required.')
    if requested_price is None or requested_price < 0:
        return make_failure(operation=operation, exchange=name, account=account_name, code=('INVALID_TP_PRICE' if operation == 'set_tp' else 'INVALID_SL_PRICE'), message=('TP price must be numeric and non-negative.' if operation == 'set_tp' else 'SL price must be numeric and non-negative.'))
    try:
        _fetched, credentials, _target, market, active_orders, current_side, current_size, closing_side, auth_token = _find_position_management_context(request)
    except ValueError:
        return make_failure(operation=operation, exchange=name, account=account_name, code='MISSING_SYMBOL', message='Symbol is required.')
    except LookupError as exc:
        code = str(exc) or 'POSITION_NOT_FOUND'
        return make_failure(operation=operation, exchange=name, account=account_name, code=code, message=('Open position not found.' if code == 'POSITION_NOT_FOUND' else 'Instrument not found.'))
    except Exception as exc:
        return make_failure(operation=operation, exchange=name, account=account_name, code='POSITION_CONTEXT_UNAVAILABLE', message=sanitize_error_message(str(exc)))
    market_id = int(str(market.get('market_id') or 0))
    bucket = _classify_protection_orders(orders=active_orders, market_id=market_id, closing_side=closing_side)
    target_key = 'tp' if operation == 'set_tp' else 'sl'
    existing_orders = list(bucket[target_key])
    if len(existing_orders) > 1:
        return make_failure(operation=operation, exchange=name, account=account_name, code='AMBIGUOUS_PROTECTION_STATE', message='Multiple matching TP/SL orders were found.')
    existing_order = existing_orders[0] if existing_orders else None
    if requested_price == 0:
        if existing_order is None:
            action = _position_action_result(operation=operation, symbol=requested_symbol, verified=True, removed=False, current_side=current_side, current_size=_decimal_text(current_size), message=('No Take Profit was set.' if operation == 'set_tp' else 'No Stop Loss was set.'))
            return make_success(operation=operation, exchange=name, account=account_name, position_action=action)
        try:
            _submit_cancel_order(credentials, existing_order, reason='remove')
            verified, _matched = _verify_tpsl_state(credentials=credentials, auth_token=auth_token, market_id=market_id, closing_side=closing_side, operation=operation, expected_price=None)
        except Exception as exc:
            action = _position_action_result(operation=operation, symbol=requested_symbol, verified=False, removed=True, current_side=current_side, current_size=_decimal_text(current_size), status='failed')
            return make_failure(operation=operation, exchange=name, account=account_name, code=('TP_REMOVAL_FAILED' if operation == 'set_tp' else 'SL_REMOVAL_FAILED'), message=sanitize_error_message(str(exc)), position_action=action)
        action = _position_action_result(operation=operation, symbol=requested_symbol, verified=verified, removed=True, current_side=current_side, current_size=_decimal_text(current_size), message=('Take Profit removed.' if operation == 'set_tp' else 'Stop Loss removed.'), status=('success' if verified else 'failed'))
        if verified:
            return make_success(operation=operation, exchange=name, account=account_name, position_action=action)
        return make_failure(operation=operation, exchange=name, account=account_name, code='VERIFICATION_FAILED', message='TP/SL removal could not be verified.', position_action=action)
    try:
        submitted_price = _quantize_down(requested_price, int(market.get('price_decimals') or market.get('supported_price_decimals') or 0))
        if existing_order is not None:
            _submit_cancel_order(credentials, existing_order, reason='replace')
        submit_result = _submit_tpsl_order(credentials, market, operation=operation, current_size=current_size, closing_side=closing_side, trigger_price=submitted_price)
        verified, verified_oid = _verify_tpsl_state(credentials=credentials, auth_token=auth_token, market_id=market_id, closing_side=closing_side, operation=operation, expected_price=_decimal_text(submitted_price))
    except Exception as exc:
        action = _position_action_result(operation=operation, symbol=requested_symbol, verified=False, price=_decimal_text(requested_price), current_side=current_side, current_size=_decimal_text(current_size), status='failed')
        return make_failure(operation=operation, exchange=name, account=account_name, code='ORDER_SUBMISSION_FAILED', message=sanitize_error_message(str(exc)), position_action=action)
    action = _position_action_result(operation=operation, symbol=requested_symbol, verified=verified, price=str(submit_result.get('submitted_price') or _decimal_text(submitted_price)), current_side=current_side, current_size=_decimal_text(current_size), exchange_order_id=verified_oid or submit_result.get('exchange_order_id'), message=('Take Profit updated.' if operation == 'set_tp' else 'Stop Loss updated.'), status=('success' if verified else 'failed'))
    if verified:
        return make_success(operation=operation, exchange=name, account=account_name, position_action=action)
    return make_failure(operation=operation, exchange=name, account=account_name, code='VERIFICATION_FAILED', message='TP/SL update could not be verified.', position_action=action)


def _execute_close_position(request: Dict[str, Any]) -> CanonicalResponse:
    account_name = str(request.get('account') or '').strip()
    requested_symbol = str(request.get('symbol') or '').strip().upper()
    if not requested_symbol:
        return make_failure(operation='close_position', exchange=name, account=account_name, code='MISSING_SYMBOL', message='Symbol is required.')
    try:
        _fetched, credentials, _target, market, _active_orders, current_side, current_size, closing_side, _auth_token = _find_position_management_context(request, include_active_orders=False)
        submit_result = _submit_close_position(credentials, market, current_size=current_size, closing_side=closing_side)
        verified = _verify_position_closed(request, symbol=requested_symbol)
    except LookupError as exc:
        code = str(exc) or 'POSITION_NOT_FOUND'
        return make_failure(operation='close_position', exchange=name, account=account_name, code=code, message=('Open position not found.' if code == 'POSITION_NOT_FOUND' else 'Instrument not found.'))
    except Exception as exc:
        action = _position_action_result(operation='close_position', symbol=requested_symbol, verified=False, status='failed')
        return make_failure(operation='close_position', exchange=name, account=account_name, code='ORDER_SUBMISSION_FAILED', message=sanitize_error_message(str(exc)), position_action=action)
    action = _position_action_result(operation='close_position', symbol=requested_symbol, verified=verified, current_side=current_side, current_size=_decimal_text(current_size), exchange_order_id=submit_result.get('exchange_order_id'), message='Position closed.', status=('success' if verified else 'failed'))
    if verified:
        return make_success(operation='close_position', exchange=name, account=account_name, position_action=action)
    return make_failure(operation='close_position', exchange=name, account=account_name, code='VERIFICATION_FAILED', message='Position close could not be verified.', position_action=action)


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    operation = str(request.get("operation") or "").strip()
    account = str(request.get("account") or "").strip()
    if not operation:
        return make_failure(
            operation="",
            exchange=name,
            account=account,
            code="INVALID_REQUEST",
            message="Missing operation.",
        )
    if operation not in {"balance", "positions_orders", "positions_management", "set_tp", "set_sl", "close_position", "new_order", "ladder", "cancel_order_group"}:
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="NOT_IMPLEMENTED",
            message="This Lighter operation is not implemented yet.",
        )
    if not account:
        return make_failure(
            operation=operation,
            exchange=name,
            account="",
            code="MISSING_ACCOUNT",
            message="Missing account.",
        )
    try:
        if operation == "balance":
            return _balance(request)
        if operation == "positions_orders":
            return _positions_orders(request)
        if operation == "positions_management":
            return _positions_management(request)
        if operation == "set_tp":
            return _execute_set_tpsl(request, operation="set_tp")
        if operation == "set_sl":
            return _execute_set_tpsl(request, operation="set_sl")
        if operation == "close_position":
            return _execute_close_position(request)
        if operation == "new_order":
            return _execute_new_order(request)
        if operation == "cancel_order_group":
            return _execute_cancel_order_group(request)
        return _execute_ladder(request)
    except requests.HTTPError as exc:
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="LIGHTER_HTTP_ERROR",
            message=sanitize_error_message(f"Lighter HTTP error: {exc}"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lighter %s failed: %s", operation, exc)
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="LIGHTER_ERROR",
            message=sanitize_error_message(str(exc) or "Lighter request failed."),
        )
