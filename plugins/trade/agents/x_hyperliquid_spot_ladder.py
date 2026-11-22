"""Hyperliquid Spot ladder implementation for ``/tradespot``.

This module owns every Hyperliquid-specific ladder concern:

* venue-native instrument/asset resolution (spot universe ``@<index>`` keys);
* price/size precision and minimum-notional validation;
* the durable execution record (unique execution id, per-child durable
  rows, exact native order ids);
* partial-success reporting, timeout/uncertain-status handling, and the
  no-blind-retry rule;
* read-only reconciliation.

The exchange-agnostic planner (``plugins.trade.spot_ladder``) is reused
verbatim; the exchange-agnostic wizard is reused verbatim. Nothing here
knows about Telegram, and nothing here mutates a perpetual instrument.

Safety model (mirrors the MEXC spot ladder):
  * A durable record is written BEFORE the first network write.
  * Every child is validated before ANY child is submitted.
  * A balance re-read happens immediately before the first write.
  * An uncertain outcome (timeout / 5xx / malformed) marks the affected
    children UNKNOWN and STOPS the ladder. It never retries.
  * Reconciliation is read-only: it never POSTs, never DELETEs, never
    re-submits.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..canonical import (
    CanonicalLadderResult,
    make_failure,
    make_success,
    sanitize_error_message,
)
from .x_hyperliquid_agent import _post_info
from .x_hyperliquid_spot_agent import (
    _available,
    _build_exchange_client,
    _dec,
    _display_name,
    _error,
    _fetch_spot_balances,
    _fetch_spot_meta,
    _lookup_credentials,
    _market,
    _normalize_order,
    _normalize_spot_price,
    _parse_spot_meta,
    _spot_markets_by_coin,
    _text,
    name,
)

logger = logging.getLogger(__name__)

# Hyperliquid caps a single ``bulk_orders`` action. We keep batches small
# so a partial failure cannot silently swallow a large plan, and so the
# per-batch result mapping stays unambiguous.
HL_SPOT_LADDER_BATCH_MAX = 20

# Durable execution record naming. The directory is exchange-qualified so
# a MEXC execution and a Hyperliquid execution can never collide even when
# they use the same execution id.
_HL_SPOT_LADDER_DIRNAME = "hyperliquid-spot"

_LADDER_EXEC_ID_HEX_LEN = 16
_LADDER_INDEX_DEC_LEN = 4

# Hyperliquid client order ids are a 16-byte hex string ("0x" + 32 hex
# chars). We derive them deterministically from the execution id + child
# index so a restart can rebuild the identical id set and reconcile
# against the venue by cloid.
_CLOID_PREFIX = "0x"

_RECONCILE_CLASSIFICATIONS = (
    "FOUND_OPEN",
    "FOUND_FILLED",
    "FOUND_CANCELED",
    "FOUND_OTHER_TERMINAL",
    "NOT_FOUND",
    "QUERY_UNKNOWN",
)


# --------------------------------------------------------------------------
# Durable record storage
# --------------------------------------------------------------------------
def _ladder_record_dir(account: str) -> Path:
    """Return the durable-record directory for ``account``."""
    base = Path(os.environ.get("HERMES_HOME") or "/root/.hermes") / "cache" / "tradespot" / _HL_SPOT_LADDER_DIRNAME
    safe_account = re.sub(r"[^A-Za-z0-9_.-]", "_", account or "unknown") or "unknown"
    return base / safe_account


def _ladder_record_path(account: str, execution_id: str) -> Path:
    """Return the durable-record JSON path for ``account/execution_id``."""
    return _ladder_record_dir(account) / f"{execution_id}.json"


def _json_safe(value: Any) -> Any:
    """Return ``value`` as JSON-serializable primitives."""
    if isinstance(value, Decimal):
        return _text(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _ladder_persist_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically (tmp + fsync + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(_json_safe(payload), handle, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _ladder_load_record(account: str, execution_id: str) -> Optional[Dict[str, Any]]:
    """Load the durable record for ``account/execution_id`` (None if absent)."""
    path = _ladder_record_path(account, execution_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except Exception:  # noqa: BLE001
        return None
    return record if isinstance(record, dict) else None


def _ladder_new_execution_id() -> str:
    """Return a fresh unique execution id."""
    return secrets.token_hex(_LADDER_EXEC_ID_HEX_LEN // 2)[:_LADDER_EXEC_ID_HEX_LEN]


def _ladder_new_client_order_id(execution_id: str, child_index: int) -> str:
    """Return a deterministic Hyperliquid-valid client order id (cloid).

    Hyperliquid requires ``0x`` followed by exactly 32 hex characters. We
    derive the value from the execution id and the child index so the same
    execution always produces the same cloid set, which is what makes
    read-only reconciliation by cloid possible after a restart.
    """
    if not (isinstance(execution_id, str) and len(execution_id) == _LADDER_EXEC_ID_HEX_LEN):
        raise ValueError(f"execution_id must be {_LADDER_EXEC_ID_HEX_LEN} hex chars")
    seed = f"{execution_id}:{int(child_index):0{_LADDER_INDEX_DEC_LEN}d}"
    # Deterministic derivation keeps the cloid stable across restarts
    # without reusing the execution id verbatim.
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return _CLOID_PREFIX + digest[:32]


def _cloid_from_client_order_id(client_order_id: str) -> Optional[Any]:
    """Return an SDK ``Cloid`` for ``client_order_id`` (None if invalid)."""
    raw = str(client_order_id or "").strip()
    if not raw.startswith(_CLOID_PREFIX) or len(raw) != 34:
        return None
    try:
        from hyperliquid.utils.types import Cloid

        return Cloid(raw)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
def plan_ladder(request: Mapping[str, Any]) -> Any:
    """Validate and plan a Hyperliquid Spot ladder without placing orders."""
    from ..spot_ladder import compute_ladder_with_min_notional, plan_as_dict

    account = str(request.get("account") or "")
    try:
        instrument = request.get("instrument")
        if not isinstance(instrument, Mapping):
            raise ValueError("MISSING_INSTRUMENT")
        total_volume_raw = request.get("total_volume")
        budget_raw = request.get("total_quote_budget")
        total_volume = Decimal(str(total_volume_raw)) if total_volume_raw is not None else None
        budget = Decimal(str(budget_raw)) if budget_raw is not None else None
        plan = compute_ladder_with_min_notional(
            side=str(request.get("side") or ""),
            distribution=str(request.get("distribution") or ""),
            start_price=Decimal(str(request.get("start_price"))),
            end_price=Decimal(str(request.get("end_price"))),
            order_count=int(request.get("order_count") or 0),
            instrument=instrument,
            # Hyperliquid documents MinTradeSpotNtl as 10 quote-token units.
            min_notional=Decimal(str(instrument.get("min_notional") or instrument.get("minimum_notional") or 0)),
            total_volume=total_volume,
            total_quote_budget=budget,
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        return make_failure(
            operation="plan_ladder",
            exchange=name,
            account=account,
            code="INVALID_LADDER_PLAN",
            message=sanitize_error_message(str(exc)),
        )
    return make_success(
        operation="plan_ladder",
        exchange=name,
        account=account,
        data={"plan": plan_as_dict(plan)},
    )


# --------------------------------------------------------------------------
# Child validation
# --------------------------------------------------------------------------
def _price_exceeds_significant_figures(price: Decimal, limit: int = 5,
                                       max_decimals: int = 5) -> bool:
    """True when the VENUE would refuse ``price``.

    Hyperliquid caps a spot LIMIT price at 5 significant figures AND at
    most ``8 - szDecimals`` decimal places. The decimal-place limit is
    the tick lattice; the significant-figure limit is the rule that
    catches a price like 58.96551, which sits on a 1e-5 lattice yet is
    still refused by the venue.

    Mirror the venue's own validity check exactly, so preflight refuses
    precisely what the venue refuses -- no more and no less.
    """
    if price is None or price <= 0:
        return False
    # An integral price is accepted by the venue without a significant-
    # figure test, so mirror that exactly: 60 and 1000 are fine even
    # though 1000 has four digits.
    if price == price.to_integral_value():
        return False
    normalized = price.normalize()
    decimal_places = max(0, -normalized.as_tuple().exponent)
    if decimal_places > max_decimals:
        return True
    digits = normalized.as_tuple().digits
    # Trailing zeros are not significant: 60.50 is 4 figures, not 5.
    significant = len(digits)
    exponent = normalized.as_tuple().exponent
    if exponent > 0:
        significant += exponent
    return significant > limit


def _ladder_validate_child(
    child: Mapping[str, Any],
    *,
    expected_symbol: Optional[str] = None,
    expected_side: Optional[str] = None,
    instrument: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Return an error code for an invalid child, or None when valid."""
    if not isinstance(child, Mapping):
        return "CHILD_NOT_MAPPING"
    required = ("symbol", "side", "quantity", "price", "client_order_id")
    for key in required:
        if not str(child.get(key) or "").strip():
            return f"MISSING_{key.upper()}"
    symbol = str(child.get("symbol") or "").strip().upper().replace("/", "")
    if expected_symbol and symbol != expected_symbol:
        return "MIXED_SYMBOLS"
    side = str(child.get("side") or "").strip().upper()
    if expected_side and side != expected_side:
        return "MIXED_SIDES"
    if side not in {"BUY", "SELL"}:
        return "INVALID_SIDE"
    order_type = str(child.get("type") or child.get("order_type") or "LIMIT").strip().upper()
    if order_type != "LIMIT":
        return "MARKET_NOT_ALLOWED"
    try:
        quantity = Decimal(str(child.get("quantity")))
        price = Decimal(str(child.get("price")))
    except (InvalidOperation, ValueError, TypeError):
        return "INVALID_NUMERIC_VALUE"
    if not quantity.is_finite() or not price.is_finite() or quantity <= 0 or price <= 0:
        return "NON_POSITIVE_NUMERIC_VALUE"
    constraints = instrument or {}
    size_step = _dec(constraints.get("size_step") or constraints.get("size_increment"))
    price_tick = _dec(constraints.get("price_tick") or constraints.get("price_increment"))
    min_qty = _dec(constraints.get("min_qty") or constraints.get("minimum_size"))
    max_qty = _dec(constraints.get("max_qty"))
    min_notional = _dec(constraints.get("min_notional") or constraints.get("minimum_notional"))
    if size_step > 0 and quantity % size_step != 0:
        return "QUANTITY_PRECISION_INVALID"
    if price_tick > 0 and price % price_tick != 0:
        return "PRICE_PRECISION_INVALID"
    # Hyperliquid enforces a SECOND price rule that the tick lattice
    # alone does not express: at most 5 SIGNIFICANT FIGURES. A price
    # like 58.96551 sits on the 1e-5 lattice yet carries 7 significant
    # figures, and the venue rejects it ("Price must be divisible by
    # tick size") -- and because the venue rejects the whole
    # ``bulk_orders`` ACTION, one over-precise child kills the entire
    # ladder and every sibling comes back with no per-child status.
    #
    # Preflight must therefore refuse what the venue refuses, rather
    # than relying on the venue to discover it after the fact.
    # The venue's decimal-place limit is the tick lattice the instrument
    # publishes, so derive the significant-figure check's decimal budget
    # from it rather than hard-coding a number.
    max_decimals = 5
    if price_tick > 0:
        max_decimals = max(0, -price_tick.normalize().as_tuple().exponent)
    if _price_exceeds_significant_figures(price, limit=5, max_decimals=max_decimals):
        return "PRICE_SIGNIFICANT_FIGURES_INVALID"
    if min_qty > 0 and quantity < min_qty:
        return "QUANTITY_BELOW_MINIMUM"
    if max_qty > 0 and quantity > max_qty:
        return "QUANTITY_ABOVE_MAXIMUM"
    if min_notional > 0 and quantity * price < min_notional:
        return "NOTIONAL_BELOW_MINIMUM"
    return None


def _ladder_plan_batches(children: List[Mapping[str, Any]]) -> List[List[Mapping[str, Any]]]:
    """Split children into deterministic <=N-child batches, oldest-first."""
    if not children:
        return []
    return [
        list(children[i : i + HL_SPOT_LADDER_BATCH_MAX])
        for i in range(0, len(children), HL_SPOT_LADDER_BATCH_MAX)
    ]


def _ladder_child_status(child_result: Any) -> str:
    """Classify one bulk_orders child result as ACCEPTED/REJECTED/UNKNOWN."""
    if not isinstance(child_result, Mapping):
        return "UNKNOWN"
    resting = child_result.get("resting")
    filled = child_result.get("filled")
    if isinstance(resting, Mapping) or isinstance(filled, Mapping):
        return "ACCEPTED"
    error = child_result.get("error")
    if error is not None:
        return "REJECTED"
    return "UNKNOWN"


def _vwap(children: Sequence[Mapping[str, Any]], qty_key: str, price_key: str) -> Optional[Decimal]:
    """Decimal VWAP across accepted children. None if no accepted quantity."""
    total_qty = Decimal("0")
    total_notional = Decimal("0")
    for child in children:
        try:
            quantity = Decimal(str(child.get(qty_key) or "0"))
            price = Decimal(str(child.get(price_key) or "0"))
        except Exception:  # noqa: BLE001
            continue
        if quantity <= 0 or price <= 0:
            continue
        total_qty += quantity
        total_notional += quantity * price
    if total_qty <= 0:
        return None
    return total_notional / total_qty


# --------------------------------------------------------------------------
# Submission
# --------------------------------------------------------------------------
def _submit_batch(client: Any, batch: List[Mapping[str, Any]], market: Mapping[str, Any]) -> Dict[str, Any]:
    """Submit one batch of LIMIT spot orders. Returns a classified outcome.

    Outcome kinds:
      OK              - the venue answered with a per-child status list
      HTTP_REJECTED   - the venue rejected the action (4xx)
      AMBIGUOUS_ERROR - the venue answered 5xx / timed out / malformed;
                        the request MAY have been processed, so the caller
                        must treat it as UNKNOWN and stop.
    """
    orders: List[Dict[str, Any]] = []
    size_increment = _dec(market.get("size_increment") or market.get("size_step"))
    for child in batch:
        coin = str(market["coin"])
        side_u = str(child.get("side") or "").upper()
        quantity = _dec(child.get("quantity"))
        # Hyperliquid enforces TWO rules for a spot LIMIT price: at most
        # ``8 - szDecimals`` decimal places AND at most 5 significant
        # figures. A price with too many significant figures is REJECTED
        # by the venue ("Price must be divisible by tick size"), and the
        # venue rejects the WHOLE ``bulk_orders`` action -- every other
        # child in the batch comes back with no per-child status.
        #
        # The single-order path normalizes through
        # ``_normalize_spot_price``; the ladder path used to send the raw
        # planner price, so a 30-child ladder died on its first child.
        # Normalize here so the ladder and the single order obey ONE
        # rule.
        price = _normalize_spot_price(_dec(child.get("price")), size_increment)
        cloid = _cloid_from_client_order_id(str(child.get("client_order_id") or ""))
        order: Dict[str, Any] = {
            "coin": coin,
            "is_buy": side_u == "BUY",
            "sz": float(quantity),
            "limit_px": float(price),
            "order_type": {"limit": {"tif": "Gtc"}},
            "reduce_only": False,
        }
        if cloid is not None:
            order["cloid"] = cloid
        orders.append(order)

    try:
        response = client.bulk_orders(orders)
    except Exception as exc:  # noqa: BLE001 - transport failure is uncertain
        return {
            "kind": "AMBIGUOUS_ERROR",
            "error_code": "TRANSPORT_ERROR",
            "message": sanitize_error_message(str(exc)),
            "statuses": None,
            "http_status": None,
        }

    statuses = (((response or {}).get("response") or {}).get("data") or {}).get("statuses")
    if not isinstance(statuses, list):
        return {
            "kind": "AMBIGUOUS_ERROR",
            "error_code": "MALFORMED_RESPONSE",
            "message": "Hyperliquid returned no per-child status list.",
            "statuses": None,
            "http_status": None,
        }
    return {
        "kind": "OK",
        "error_code": None,
        "message": None,
        "statuses": statuses,
        "http_status": None,
    }


def _ladder(account: str, request: Mapping[str, Any]) -> Any:
    """Submit a Hyperliquid Spot ladder."""
    wallet, _secret = _lookup_credentials(account)
    if not wallet:
        return _error("ladder", account, "ACCOUNT_NOT_CONFIGURED", "Account is not configured.")

    children_in = request.get("children")
    if not isinstance(children_in, list) or not children_in:
        return _error("ladder", account, "MISSING_CHILDREN", "Ladder request must contain a non-empty list of FINAL precomputed children.")
    if len(children_in) > 500:
        return _error("ladder", account, "LADDER_TOO_LARGE", f"Ladder of {len(children_in)} children exceeds the 500-child safety cap.")

    expected_symbol = str(children_in[0].get("symbol") or "").strip().upper().replace("/", "")
    expected_side = str(children_in[0].get("side") or "").strip().upper()
    instrument = request.get("instrument") if isinstance(request.get("instrument"), Mapping) else {}

    symbol = str(children_in[0].get("symbol") or "").strip().upper().replace("/", "")
    side = str(children_in[0].get("side") or "").strip().upper()

    # Resolve the NATIVE spot asset. The ladder always trades the spot
    # universe entry (e.g. ``@107``); never a perpetual coin of the same
    # ticker.
    try:
        market = _market(account, str(children_in[0].get("symbol") or ""))
    except Exception as exc:  # noqa: BLE001
        return _error("ladder", account, "INSTRUMENT_NOT_FOUND", sanitize_error_message(str(exc)))

    # Normalize the prices BEFORE preflight. Hyperliquid rejects a spot
    # LIMIT price that carries more than 5 significant figures, and the
    # venue rejects the whole ``bulk_orders`` ACTION when one child is
    # off-grid -- so every sibling comes back with no per-child status.
    # The planner emits full-precision prices; normalizing here means
    # preflight validates exactly the prices that will reach the wire.
    size_increment = _dec(market.get("size_increment") or market.get("size_step"))
    price_tick = _dec(market.get("price_tick") or market.get("price_increment"))
    normalized_children: List[Dict[str, Any]] = []
    for idx, child in enumerate(children_in):
        normalized_child = dict(child)
        raw_price = _dec(child.get("price"))
        # A price that is OFF the tick lattice is a caller error, not a
        # venue-formatting artifact: the planner never produces one, so
        # refuse it rather than silently moving the user's price.
        if price_tick > 0 and raw_price % price_tick != 0:
            return _error(
                "ladder", account, "PRICE_PRECISION_INVALID",
                f"Child {idx}: price is not a multiple of the price tick.")
        try:
            normalized_child["price"] = _text(
                _normalize_spot_price(raw_price, size_increment)
            )
        except Exception as exc:  # noqa: BLE001
            return _error(
                "ladder", account, "PRICE_PRECISION_INVALID",
                f"Child {idx}: {sanitize_error_message(str(exc))}")
        normalized_children.append(normalized_child)

    # Validate EVERY child BEFORE any network write.
    for idx, child in enumerate(normalized_children):
        err = _ladder_validate_child(
            child,
            expected_symbol=expected_symbol,
            expected_side=expected_side,
            instrument=instrument,
        )
        if err == "MARKET_NOT_ALLOWED":
            return _error("ladder", account, "LIMIT_ONLY", f"Child {idx}: MARKET orders are not supported by /tradespot ladder. Use LIMIT only.")
        if err:
            return _error("ladder", account, err, f"Child {idx}: {err.replace('_', ' ').lower()}.")

    # Re-read the exact selected account immediately before any write.
    try:
        balances = _fetch_spot_balances(wallet)
    except Exception as exc:  # noqa: BLE001
        return _error("ladder", account, "BALANCE_UNAVAILABLE", sanitize_error_message(str(exc)))

    base_asset = str(market.get("base") or "").upper()
    quote_asset = str(market.get("quote") or "").upper()
    if not base_asset or not quote_asset:
        return _error("ladder", account, "INSTRUMENT_METADATA_REQUIRED", "Hyperliquid Spot ladder requires base and quote metadata for balance validation.")

    required = sum(
        (
            _dec(child.get("quantity")) * _dec(child.get("price"))
            if side == "BUY"
            else _dec(child.get("quantity"))
            for child in children_in
        ),
        Decimal("0"),
    )
    required_asset = quote_asset if side == "BUY" else base_asset
    available = _available(balances, required_asset)
    if available < required:
        return _error(
            "ladder",
            account,
            "INSUFFICIENT_BALANCE",
            f"Insufficient free {required_asset} for ladder: need {_text(required)}, have {_text(available)}.",
        )

    requested_order_count = len(normalized_children)
    requested_volume = sum((_dec(child.get("quantity")) for child in normalized_children), Decimal("0"))

    # PERSIST BEFORE THE FIRST WRITE.
    upstream_execution_id = str(request.get("execution_id") or "").strip()
    if upstream_execution_id and len(upstream_execution_id) == _LADDER_EXEC_ID_HEX_LEN:
        execution_id = upstream_execution_id
    else:
        execution_id = _ladder_new_execution_id()

    prepared_children: List[Dict[str, Any]] = []
    for idx, child in enumerate(normalized_children):
        prepared = dict(child)
        prepared["client_order_id"] = _ladder_new_client_order_id(execution_id, idx)
        # Normalize the price ONCE, here, so the durable record stores
        # exactly the price that reaches the wire. Reconciliation compares
        # the record against the venue by cloid, so a record that kept the
        # pre-normalization price would never match a venue order.
        prepared["price"] = _text(
            _normalize_spot_price(
                _dec(child.get("price")),
                _dec(market.get("size_increment") or market.get("size_step")),
            )
        )
        prepared_children.append(prepared)

    durable_record: Dict[str, Any] = {
        "version": 1,
        "execution_id": execution_id,
        "exchange": "hyperliquid",
        "account": account,
        "instrument": symbol,
        "exchange_symbol": symbol,
        "native_coin": str(market["coin"]),
        "asset_id": market["asset_id"],
        "side": side,
        "distribution": str(request.get("distribution") or ""),
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
        "submission_state": "PREPARED",
        "planned_vwap": _text(_vwap(
            [{"quantity": c.get("quantity"), "price": c.get("price")} for c in normalized_children],
            "quantity", "price",
        ) or Decimal("0")),
        "children": [
            {
                "index": idx,
                "client_order_id": c["client_order_id"],
                "exchange_order_id": None,
                "quantity": str(c.get("quantity") or ""),
                "price": str(c.get("price") or ""),
                "submission_classification": "NOT_ATTEMPTED",
                "reconciliation_classification": None,
                "batch_index": None,
            }
            for idx, c in enumerate(prepared_children)
        ],
        "last_reconciliation_at": None,
        "last_reconciliation_error": None,
    }
    try:
        _ladder_persist_atomic(_ladder_record_path(account, execution_id), durable_record)
    except Exception as exc:  # noqa: BLE001
        # FAIL CLOSED. The durable record is what makes a submitted
        # ladder reconcilable by cloid. If it cannot be written, orders
        # placed afterwards would have no durable trace, so the ladder
        # must NOT send them. Stop before the first network write.
        logger.error(
            "ladder_persist_pre_failed account=%s execution_id=%s err=%s",
            account, execution_id, sanitize_error_message(str(exc)),
        )
        return _error(
            "ladder", account, "DURABLE_RECORD_WRITE_FAILED",
            "The durable ladder record could not be written, so no orders "
            "were sent. A submitted ladder must be reconcilable.",
        )

    client, _wallet, _secret = _build_exchange_client(account)
    if client is None:
        return _error("ladder", account, "ACCOUNT_NOT_CONFIGURED", "Account signing client is unavailable.")

    batches = _ladder_plan_batches(prepared_children)
    batch_records: List[Dict[str, Any]] = []
    accepted_qty = Decimal("0")
    accepted_notional = Decimal("0")
    accepted_order_ids: List[str] = []
    accepted_count = 0
    rejected_count = 0
    unknown_count = 0
    stopped_early = False
    exchange_reason: Optional[str] = None

    for batch_index, batch in enumerate(batches):
        outcome = _submit_batch(client, batch, market)
        record: Dict[str, Any] = {
            "batch_index": batch_index,
            "children_attempted": [str(c.get("client_order_id") or "") for c in batch],
            "planned_vwap": _text(_vwap(
                [{"quantity": c.get("quantity"), "price": c.get("price")} for c in batch],
                "quantity", "price",
            ) or Decimal("0")),
            "request": {
                "endpoint": "/exchange",
                "method": "POST",
                "action": "order",
                "order_count": len(batch),
                "native_coin": str(market["coin"]),
                "asset_id": market["asset_id"],
                "first_2_order_objects": [
                    {
                        "coin": str(market["coin"]),
                        "is_buy": str(c.get("side")).upper() == "BUY",
                        "sz": str(c.get("quantity")),
                        "limit_px": str(c.get("price")),
                        "order_type": {"limit": {"tif": "Gtc"}},
                        "client_order_id": str(c.get("client_order_id")),
                    }
                    for c in batch[:2]
                ],
            },
            "response": {"kind": outcome["kind"]},
        }

        if outcome["kind"] != "OK":
            # Ambiguous outcome: the request may have reached the venue.
            # Mark the whole batch UNKNOWN and STOP. Never retry.
            record["status"] = "UNKNOWN"
            record["error_code"] = outcome["error_code"]
            record["exchange_reason"] = outcome["message"]
            record["child_results"] = [
                {
                    "client_order_id": str(c.get("client_order_id") or ""),
                    "status": "UNKNOWN",
                    "quantity": str(c.get("quantity") or ""),
                    "price": str(c.get("price") or ""),
                    "message": outcome["message"] or outcome["kind"],
                }
                for c in batch
            ]
            unknown_count += len(batch)
            stopped_early = True
            exchange_reason = outcome["message"] or outcome["kind"]
            batch_records.append(record)
            break

        statuses = outcome["statuses"]
        record["child_results"] = []
        record["exchange_order_ids"] = []
        accepted_qty_batch = Decimal("0")
        accepted_notional_batch = Decimal("0")
        for child_in, child_result in zip(batch, statuses):
            status = _ladder_child_status(child_result)
            child_record: Dict[str, Any] = {
                "client_order_id": str(child_in.get("client_order_id") or ""),
                "status": status,
                "quantity": str(child_in.get("quantity") or ""),
                "price": str(child_in.get("price") or ""),
            }
            if isinstance(child_result, Mapping):
                state = child_result.get("resting") or child_result.get("filled") or {}
                if isinstance(state, Mapping) and state.get("oid") is not None:
                    child_record["exchange_order_id"] = str(state.get("oid"))
                error = child_result.get("error")
                if error is not None:
                    child_record["error"] = sanitize_error_message(str(error))
                    child_record["message"] = sanitize_error_message(str(error))
            record["child_results"].append(child_record)
            if status == "ACCEPTED":
                accepted_count += 1
                quantity = _dec(child_in.get("quantity"))
                price = _dec(child_in.get("price"))
                accepted_qty_batch += quantity
                accepted_notional_batch += quantity * price
                state = child_result.get("resting") or child_result.get("filled") or {}
                oid = state.get("oid") if isinstance(state, Mapping) else None
                if oid is not None:
                    record["exchange_order_ids"].append(str(oid))
                    accepted_order_ids.append(str(oid))
            elif status == "REJECTED":
                rejected_count += 1
                if not exchange_reason:
                    exchange_reason = str(child_record.get("message") or "Hyperliquid rejected a child order.")
            else:
                unknown_count += 1

        # Children the venue did not describe at all are UNKNOWN.
        for child_in in batch[len(statuses) :]:
            record["child_results"].append({
                "client_order_id": str(child_in.get("client_order_id") or ""),
                "status": "UNKNOWN",
                "quantity": str(child_in.get("quantity") or ""),
                "price": str(child_in.get("price") or ""),
                "message": "Hyperliquid returned no per-child result.",
            })
            unknown_count += 1

        record["status"] = "ACCEPTED" if (rejected_count + unknown_count == 0) else "PARTIAL"
        record["accepted_vwap"] = (
            _text(accepted_notional_batch / accepted_qty_batch) if accepted_qty_batch > 0 else None
        )
        batch_records.append(record)
        accepted_qty += accepted_qty_batch
        accepted_notional += accepted_notional_batch

        if rejected_count or unknown_count:
            # A rejected/uncertain child stops the ladder: continuing would
            # place orders the user did not intend to place.
            stopped_early = True
            break

    attempted_total = sum(len(r["children_attempted"]) for r in batch_records)
    not_attempted_count = requested_order_count - attempted_total

    # PERSIST AFTER ALL BATCHES.
    try:
        cid_class: Dict[str, str] = {}
        for rec in batch_records:
            for child_res in rec.get("child_results", []) or []:
                if not isinstance(child_res, Mapping):
                    continue
                cid = str(child_res.get("client_order_id") or "")
                if cid:
                    cid_class[cid] = str(child_res.get("status") or "UNKNOWN")
        persisted = _ladder_load_record(account, execution_id) or dict(durable_record)
        for child in persisted.get("children", []) or []:
            if not isinstance(child, dict):
                continue
            cid = str(child.get("client_order_id") or "")
            child["submission_classification"] = cid_class.get(cid, "NOT_ATTEMPTED")
            if child.get("submission_classification") == "ACCEPTED":
                for rec in batch_records:
                    for child_res in rec.get("child_results", []) or []:
                        if isinstance(child_res, Mapping) and str(child_res.get("client_order_id") or "") == cid:
                            child["exchange_order_id"] = child_res.get("exchange_order_id")
                            child["batch_index"] = rec.get("batch_index")
        persisted["updated_at"] = int(time.time())
        persisted["batches"] = _json_safe(batch_records)
        if stopped_early:
            persisted["submission_state"] = "STOPPED_EARLY"
        elif rejected_count == 0 and unknown_count == 0:
            persisted["submission_state"] = "ACCEPTED"
        elif rejected_count > 0 and unknown_count == 0:
            persisted["submission_state"] = "PARTIAL_REJECTED"
        else:
            persisted["submission_state"] = "PARTIAL_UNKNOWN"
        _ladder_persist_atomic(_ladder_record_path(account, execution_id), persisted)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "ladder_persist_post_failed account=%s execution_id=%s err=%s",
            account, execution_id, sanitize_error_message(str(exc)),
        )

    planned_vwap = _vwap(
        [{"quantity": c.get("quantity"), "price": c.get("price")} for c in normalized_children],
        "quantity", "price",
    )
    accepted_vwap = (accepted_notional / accepted_qty) if accepted_qty > 0 else None

    partial = accepted_qty < requested_volume or rejected_count > 0 or unknown_count > 0 or stopped_early
    success = (not stopped_early) and rejected_count == 0 and unknown_count == 0 and not_attempted_count == 0

    try:
        ladder_result = CanonicalLadderResult(
            symbol=symbol,
            side=side,
            distribution=str(request.get("distribution") or "ladder"),
            requested_order_count=requested_order_count,
            submitted_order_count=accepted_count,
            requested_volume=_text(requested_volume),
            submitted_volume=_text(accepted_qty),
            batch_count=len(batches),
            verified=bool(success and accepted_count == requested_order_count),
            partial=partial,
            status="success" if success else ("partial" if partial else "unknown"),
            accepted_child_count=accepted_count,
            omitted_order_count=rejected_count + unknown_count + not_attempted_count,
            child_order_ids=list(accepted_order_ids),
            batches=batch_records,
            exchange_reason=exchange_reason,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="LADDER_RESULT_CONSTRUCTION_FAILED",
            message=sanitize_error_message(str(exc)),
            data={
                "execution_id": execution_id,
                "batches": _json_safe(batch_records),
                "child_client_order_ids": [
                    str(c.get("client_order_id") or "") for c in prepared_children
                ],
            },
        )

    return make_success(
        operation="ladder",
        exchange=name,
        account=account,
        ladder=ladder_result,
        data={
            "execution_id": execution_id,
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
            "unknown_count": unknown_count,
            "not_attempted_count": not_attempted_count,
            # The MEXC spot agent publishes the bare names and the
            # /tradespot renderer reads those names. The Hyperliquid
            # ladder agent published only the ``*_count`` spellings, so
            # every count the renderer displayed defaulted to zero and a
            # 30-child ladder reported "Accepted: 0, Rejected: 0,
            # Unknown: 0, Not attempted: 0". Publish BOTH spellings so
            # either reader observes the truth.
            "accepted": accepted_count,
            "rejected": rejected_count,
            "unknown": unknown_count,
            "not_attempted": not_attempted_count,
            "planned_vwap": _text(planned_vwap or Decimal("0")),
            "accepted_vwap": _text(accepted_vwap) if accepted_vwap is not None else None,
            "child_client_order_ids": [
                str(c.get("client_order_id") or "") for c in prepared_children
            ],
        },
    )


# --------------------------------------------------------------------------
# Read-only reconciliation
# --------------------------------------------------------------------------
def _raw_spot_orders(wallet: str, history: bool) -> List[Dict[str, Any]]:
    """Return raw venue order rows for ``wallet`` (read-only)."""
    kind = "historicalOrders" if history else "openOrders"
    payload = _post_info({"type": kind, "user": wallet})
    rows: List[Dict[str, Any]] = []
    if isinstance(payload, list):
        rows = [dict(row) for row in payload if isinstance(row, Mapping)]
    elif isinstance(payload, Mapping):
        for key in ("orders", "rows", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = [dict(row) for row in value if isinstance(row, Mapping)]
                break
    return rows


def _row_cloid(row: Mapping[str, Any]) -> str:
    """Return the cloid of a raw venue row (empty string when absent).

    ``openOrders`` returns the order fields at the top level, while
    ``historicalOrders`` nests them under an ``order`` object and puts the
    status alongside it. Both shapes must be handled, otherwise a filled
    or canceled ladder child can never be reconciled by cloid.
    """
    for key in ("cloid", "clientOrderId", "client_order_id"):
        value = row.get(key)
        if value:
            return str(value)
    nested = row.get("order")
    if isinstance(nested, Mapping):
        for key in ("cloid", "clientOrderId", "client_order_id"):
            value = nested.get(key)
            if value:
                return str(value)
    return ""


def reconcile_batch(
    account: str,
    symbol: str,
    expected_client_order_ids: List[str],
    known_exchange_order_ids: Optional[List[str]] = None,
    execution_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Read-only reconciliation against the live Hyperliquid spot account.

    Uses ONLY the read-only ``/info`` endpoints (``openOrders`` and
    ``historicalOrders``). NEVER submits, cancels, or retries.

    Classification (exactly one per expected child):
      FOUND_OPEN              the child is present in openOrders
      FOUND_FILLED            the child's newest history status is filled
      FOUND_CANCELED          the child's newest history status is canceled
      FOUND_OTHER_TERMINAL    the child's newest history status is terminal
                              but neither filled nor canceled
      NOT_FOUND               every applicable read succeeded and the child
                              was absent from both responses
      QUERY_UNKNOWN           a required read failed, so reconciliation is
                              itself incomplete
    """
    wallet, _secret = _lookup_credentials(account)
    if not wallet:
        raise ValueError(f"unknown Hyperliquid account '{account}'")

    remaining = {str(cid): index for index, cid in enumerate(expected_client_order_ids)}
    rows: Dict[int, Dict[str, Any]] = {}
    for index, cid in enumerate(expected_client_order_ids):
        rows[index] = {
            "index": index,
            "client_order_id": str(cid),
            "exchange_order_id": None,
            "classification": "NOT_FOUND",
            "exchange_status": None,
            "executed_qty": None,
            "cumulative_quote_qty": None,
            "query_source": None,
            "query_error": None,
        }
    for kid in known_exchange_order_ids or []:
        kid_s = str(kid or "")
        if kid_s and kid_s in remaining:
            rows[remaining[kid_s]]["exchange_order_id"] = kid_s

    query_failed = False
    query_error_msg: Optional[str] = None

    # --- open orders ---
    try:
        open_rows = _raw_spot_orders(wallet, history=False)
    except Exception as exc:  # noqa: BLE001
        open_rows = []
        query_failed = True
        query_error_msg = sanitize_error_message(str(exc))
    for row in open_rows:
        cloid = _row_cloid(row)
        if not cloid or cloid not in remaining:
            continue
        index = remaining.pop(cloid)
        rows[index]["classification"] = "FOUND_OPEN"
        rows[index]["exchange_status"] = "open"
        rows[index]["query_source"] = "openOrders"
        if row.get("oid") is not None:
            rows[index]["exchange_order_id"] = str(row.get("oid"))
        if row.get("sz") is not None:
            rows[index]["executed_qty"] = _text(_dec(row.get("sz")))

    # --- history (only for children still unresolved) ---
    if remaining:
        try:
            history_rows = _raw_spot_orders(wallet, history=True)
        except Exception as exc:  # noqa: BLE001
            history_rows = []
            query_failed = True
            query_error_msg = sanitize_error_message(str(exc))
        # Newest status per cloid wins.
        newest: Dict[str, Mapping[str, Any]] = {}
        for raw in history_rows:
            cloid = _row_cloid(raw)
            if not cloid:
                continue
            stamp = 0
            try:
                stamp = int(raw.get("statusTimestamp") or 0)
            except (TypeError, ValueError):
                stamp = 0
            previous = newest.get(cloid)
            if previous is None or stamp >= int(previous.get("_ts", 0)):
                raw = dict(raw)
                raw["_ts"] = stamp
                newest[cloid] = raw
        for cloid, index in list(remaining.items()):
            raw = newest.get(cloid)
            if raw is None:
                continue
            status = str(raw.get("status") or "").lower()
            if status == "filled":
                rows[index]["classification"] = "FOUND_FILLED"
            elif status in {"canceled", "cancelled", "expired", "rejected"}:
                rows[index]["classification"] = "FOUND_CANCELED"
            elif status:
                rows[index]["classification"] = "FOUND_OTHER_TERMINAL"
            else:
                continue
            rows[index]["exchange_status"] = str(raw.get("status") or "")
            rows[index]["query_source"] = "historicalOrders"
            order = raw.get("order") if isinstance(raw.get("order"), Mapping) else raw
            if order.get("oid") is not None:
                rows[index]["exchange_order_id"] = str(order.get("oid"))
            if order.get("sz") is not None:
                rows[index]["executed_qty"] = _text(_dec(order.get("sz")))

    if query_failed:
        for index, row in rows.items():
            if row["classification"] == "NOT_FOUND":
                row["classification"] = "QUERY_UNKNOWN"
                row["query_error"] = query_error_msg

    return [rows[index] for index in sorted(rows)]


def update_ladder_reconciliation(
    account: str,
    execution_id: str,
    reconciled: List[Dict[str, Any]],
    query_error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Update the durable record with per-child reconciliation results."""
    record = _ladder_load_record(account, execution_id)
    if record is None:
        return None
    children = record.get("children") or []
    if not isinstance(children, list):
        return record
    by_cid = {c.get("client_order_id"): c for c in children if isinstance(c, dict)}
    now = int(time.time())
    for row in reconciled:
        cid = row.get("client_order_id")
        if cid in by_cid:
            by_cid[cid]["reconciliation_classification"] = row.get("classification")
            by_cid[cid]["exchange_order_id"] = (
                row.get("exchange_order_id") or by_cid[cid].get("exchange_order_id")
            )
            by_cid[cid]["last_reconciled_status"] = row.get("exchange_status")
    record["updated_at"] = now
    record["last_reconciliation_at"] = now
    if query_error:
        record["last_reconciliation_error"] = query_error
    try:
        _ladder_persist_atomic(_ladder_record_path(account, execution_id), record)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "ladder_persist_failed account=%s execution_id=%s err=%s",
            account, execution_id, sanitize_error_message(str(exc)),
        )
    return record


def list_unresolved_ladders(account: str) -> List[Dict[str, Any]]:
    """List durable ladder records whose submission is still unresolved.

    A record is unresolved iff at least one child has a
    ``submission_classification`` of UNKNOWN/NOT_ATTEMPTED, or a
    ``reconciliation_classification`` of QUERY_UNKNOWN.
    """
    base = _ladder_record_dir(account)
    if not base.exists():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(base.glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                record = json.load(handle)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(record, dict):
            continue
        children = record.get("children") or []
        unresolved = any(
            isinstance(child, dict)
            and (
                child.get("submission_classification") in {"UNKNOWN", "NOT_ATTEMPTED"}
                or child.get("reconciliation_classification") == "QUERY_UNKNOWN"
            )
            for child in children
        )
        if unresolved:
            out.append(record)
    return out


def _execute_reconcile_batch(account: str, request: Mapping[str, Any]) -> Any:
    """Read-only bridge: reconcile_batch via the standard execute() entry."""
    symbol = str(request.get("symbol") or "").strip().upper()
    if not symbol:
        return make_failure(
            operation="ladder_reconcile", exchange=name, account=account,
            code="MISSING_SYMBOL", message="reconcile requires `symbol`.",
        )
    expected = list(request.get("expected_client_order_ids") or [])
    if not expected:
        return make_failure(
            operation="ladder_reconcile", exchange=name, account=account,
            code="MISSING_EXPECTED_IDS", message="reconcile requires `expected_client_order_ids`.",
        )
    known = list(request.get("known_exchange_order_ids") or [])
    exec_id = request.get("execution_id")
    try:
        rows = reconcile_batch(
            account=account,
            symbol=symbol,
            expected_client_order_ids=expected,
            known_exchange_order_ids=known,
            execution_id=exec_id,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="ladder_reconcile", exchange=name, account=account,
            code="HYPERLIQUID_SPOT_RECONCILE_FAILED", message=sanitize_error_message(str(exc)),
        )
    by_class: Dict[str, int] = {}
    for row in rows:
        key = str(row.get("classification") or "NOT_FOUND")
        by_class[key] = by_class.get(key, 0) + 1
    if isinstance(exec_id, str) and exec_id:
        try:
            update_ladder_reconciliation(account, exec_id, rows)
        except Exception:  # noqa: BLE001
            logger.exception("ladder_reconcile_update_failed account=%s execution_id=%s", account, exec_id)
    return make_success(
        operation="ladder_reconcile", exchange=name, account=account,
        data={"rows": rows, "summary": by_class, "execution_id": exec_id},
    )


def _execute_list_unresolved(account: str) -> Any:
    """Read-only bridge: list unresolved ladder records for an account."""
    try:
        records = list_unresolved_ladders(account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="ladder_list_unresolved", exchange=name, account=account,
            code="HYPERLIQUID_SPOT_LIST_UNRESOLVED_FAILED", message=sanitize_error_message(str(exc)),
        )
    return make_success(
        operation="ladder_list_unresolved", exchange=name, account=account,
        data={"records": records, "count": len(records)},
    )


def open_orders(symbol: str = "", account: str = "") -> List[Dict[str, Any]]:
    """Return the live spot open orders (read-only).

    The wizard's generic reconciliation buttons call this by name. It
    never submits and never cancels.
    """
    wallet, _secret = _lookup_credentials(account)
    if not wallet:
        return []
    try:
        rows = _raw_spot_orders(wallet, history=False)
    except Exception:  # noqa: BLE001
        return []
    markets = _spot_markets_by_coin()
    out: List[Dict[str, Any]] = []
    for row in rows:
        coin = str(row.get("coin") or "")
        market = markets.get(coin)
        if market is None:
            # Never surface a perpetual row in a spot reconciliation view.
            continue
        normalized = _normalize_order(row, market)
        if symbol and normalized.get("display_symbol") != str(symbol):
            continue
        out.append(normalized)
    return out


def all_orders(symbol: str = "", account: str = "") -> List[Dict[str, Any]]:
    """Return the recent spot order history (read-only).

    The wizard's generic reconciliation buttons call this by name. It
    never submits and never cancels.
    """
    wallet, _secret = _lookup_credentials(account)
    if not wallet:
        return []
    try:
        rows = _raw_spot_orders(wallet, history=True)
    except Exception:  # noqa: BLE001
        return []
    markets = _spot_markets_by_coin()
    out: List[Dict[str, Any]] = []
    for raw in rows:
        order = raw.get("order") if isinstance(raw.get("order"), Mapping) else raw
        if not isinstance(order, Mapping):
            continue
        coin = str(order.get("coin") or "")
        market = markets.get(coin)
        if market is None:
            continue
        merged = dict(order)
        if raw.get("status") is not None:
            merged["status"] = raw.get("status")
        normalized = _normalize_order(merged, market)
        if symbol and normalized.get("display_symbol") != str(symbol):
            continue
        out.append(normalized)
    return out
