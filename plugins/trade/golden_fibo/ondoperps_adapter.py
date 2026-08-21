"""Ondo Perps adapter for GoldenFibo.

Mirrors ``RiseGoldenFiboAdapter`` — pure I/O over :mod:`x_ondoperps_agent`,
no strategy logic.

Identity model
==============

GoldenFibo V2 allocates an integer ``client_order_index``. Ondo requires a
string ``clientOrderId`` matching ``[A-Za-z0-9_-]{1,64}``. The adapter
converts with :func:`encode_gf_client_order_id` (decimal representation of
the integer) at this venue boundary only. ``/trade`` is unchanged.

Ondo exchange order ids are alphanumeric strings (32-char base32-ish ids
such as ``EDGPTJP3FMDFIMSCPF4W4G5XRH4G6EFV``). They are stored opaquely as
the venue's native string form — the engine never inspects them as numbers
and never coerces them with ``int(...)``. Cancels look them up via
:func:`_oid_to_str` and pass the string straight through to the venue.

Cancellation
============

True single-order cancel: ``DELETE /v1/perps/orders/{orderId}`` via the
agent's ``cancel_order`` op. Never uses ``cancel_order_group``.

TP/SL
=====

Ondo stop orders are net-position scoped (one TP per market+direction).
``set_shared_tp`` maps to ``set_tp`` and does not claim a per-fill
exchange_order_id.
"""

from __future__ import annotations

import importlib
import sys
from decimal import Decimal
from typing import Any, Dict, Optional


def _get_ondo_agent() -> Any:
    name = "plugins.trade.agents.x_ondoperps_agent"
    mod = sys.modules.get(name)
    if mod is None:
        mod = importlib.import_module(name)
    return mod


def encode_gf_client_order_id(client_order_index: int) -> str:
    """Deterministic GoldenFibo V2 int → Ondo ``clientOrderId`` string."""
    n = int(client_order_index)
    if n < 0:
        raise ValueError("client_order_index must be non-negative")
    text = str(n)
    if len(text) > 64:
        raise ValueError("client_order_index decimal form exceeds 64 characters")
    return text


def _oid_to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text, 10)
    except (TypeError, ValueError):
        return None


def _oid_to_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value == 0:
            return None
        return str(int(value))
    text = str(value).strip()
    return text or None


def _is_success(resp: Any) -> bool:
    return bool(getattr(resp, "success", False)) and getattr(resp, "error", None) is None


def _flatten(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "to_dict") and callable(getattr(value, "to_dict")):
        try:
            return value.to_dict()
        except Exception:
            return None
    if isinstance(value, list):
        return [_flatten(v) for v in value]
    return value


def _get_payload(resp: Any) -> Dict[str, Any]:
    if resp is None:
        return {}
    out: Dict[str, Any] = {}
    for key in (
        "instrument",
        "market_price",
        "order",
        "position",
        "order_state",
        "order_history",
        "order_states",
        "ladder",
        "cancel_group",
        "balance",
        "portfolio_summary",
        "positions",
        "order_groups",
        "position_action",
    ):
        val = getattr(resp, key, None)
        if val is None:
            continue
        flat = _flatten(val)
        if flat is not None:
            out[key] = flat
    payload = getattr(resp, "payload", None)
    if isinstance(payload, dict):
        out.update(payload)
    return out


def _normalize_position_row(pos: Dict[str, Any], instrument: str) -> Dict[str, Any]:
    raw_side = pos.get("side")
    side_norm = str(raw_side or "").lower() if raw_side is not None else None
    if side_norm in {"buy", "long"}:
        side = "long"
    elif side_norm in {"sell", "short"}:
        side = "short"
    else:
        side = None
    try:
        size = abs(Decimal(str(pos.get("size") or "0")))
    except Exception:
        size = Decimal("0")
    if size <= 0:
        side = None
        size = Decimal("0")
    try:
        entry = Decimal(str(pos.get("entry_price") or "0"))
    except Exception:
        entry = Decimal("0")
    return {
        "symbol": pos.get("symbol") or instrument,
        "side": side,
        "size": str(size),
        "entry_price": str(entry) if entry > 0 else None,
        "pnl": pos.get("pnl"),
        "tp": pos.get("tp"),
        "sl": pos.get("sl"),
    }


class OndoPerpsGoldenFiboAdapter:
    """Thin adapter that drives the Ondo Perps agent for GoldenFibo."""

    def __init__(self, name: str = "golden_fibo_ondoperps") -> None:
        self.name = name

    def resolve_instrument(self, account: str, instrument: str) -> Dict[str, Any]:
        resp = _get_ondo_agent().execute({
            "operation": "resolve_instrument",
            "account": account,
            "symbol": instrument,
        })
        if not _is_success(resp):
            raise RuntimeError(
                f"resolve_instrument failed: {getattr(resp, 'error', None)}"
            )
        inst = _get_payload(resp).get("instrument") or {}
        cons = self.get_venue_constraints(account, instrument)
        out = dict(inst)
        out.setdefault("symbol", inst.get("symbol") or instrument)
        out.setdefault("tick_size", cons.get("tick_size") or inst.get("price_increment"))
        out.setdefault("step_size", cons.get("step_size") or inst.get("size_increment"))
        out.setdefault("min_base_amount", cons.get("min_base_amount") or inst.get("minimum_size"))
        out.setdefault("size_decimals", cons.get("size_decimals"))
        out.setdefault("price_decimals", cons.get("price_decimals"))
        out.setdefault("market", cons.get("market"))
        return out

    def position_state(self, account: str, instrument: str) -> Dict[str, Any]:
        resp = _get_ondo_agent().execute({
            "operation": "position_state",
            "account": account,
            "symbol": instrument,
        })
        if not _is_success(resp):
            raise RuntimeError(
                f"position_state failed: {getattr(resp, 'error', None)}"
            )
        positions = _get_payload(resp).get("positions") or []
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            return _normalize_position_row(pos, instrument)
        return {"symbol": instrument, "side": None, "size": "0", "entry_price": None}

    def get_venue_constraints(self, account: str, instrument: str) -> Dict[str, Any]:
        resp = _get_ondo_agent().execute({
            "operation": "market_constraints",
            "account": account,
            "symbol": instrument,
        })
        if not _is_success(resp):
            return {}
        out = dict(_get_payload(resp).get("order_state") or {})
        # Drop explicit Nones so callers using `in` match Rise fail-open.
        return {k: v for k, v in out.items() if v is not None}

    def market_price(self, account: str, instrument: str) -> Dict[str, Any]:
        resp = _get_ondo_agent().execute({
            "operation": "market_price",
            "account": account,
            "symbol": instrument,
        })
        if not _is_success(resp):
            return {}
        return _get_payload(resp).get("market_price") or {}

    def market_constraints(self, account: str, instrument: str) -> Dict[str, Any]:
        return self.get_venue_constraints(account, instrument)

    def get_order_state(self, account: str, order_index: int) -> Dict[str, Any]:
        oid = _oid_to_str(order_index)
        if oid is None:
            return {}
        resp = _get_ondo_agent().execute({
            "operation": "get_order_state",
            "account": account,
            "order_id": oid,
            "order_index": order_index if isinstance(order_index, int) else None,
        })
        if not _is_success(resp):
            return {}
        return _get_payload(resp).get("order_state") or {}

    def get_order_state_by_client_id(
        self,
        account: str,
        instrument: str,
        client_order_index: int,
    ) -> Dict[str, Any]:
        try:
            cid = encode_gf_client_order_id(int(client_order_index))
        except (TypeError, ValueError):
            return {}
        resp = _get_ondo_agent().execute({
            "operation": "get_order_state_by_client_id",
            "account": account,
            "symbol": instrument,
            "client_order_id": cid,
            "client_order_index": int(client_order_index),
        })
        if not _is_success(resp):
            return {}
        return _get_payload(resp).get("order_state") or {}

    def place_market(
        self,
        *,
        account: str,
        instrument: str,
        side: str,
        size: Decimal,
        client_order_id: int,
    ) -> Dict[str, Any]:
        cid = encode_gf_client_order_id(int(client_order_id))
        resp = _get_ondo_agent().execute({
            "operation": "new_order",
            "account": account,
            "symbol": instrument,
            "side": side,
            "order_type": "market",
            "volume": str(size),
            "client_order_id": cid,
        })
        payload = _get_payload(resp)
        if not _is_success(resp):
            raise RuntimeError(f"place_market failed: {payload}")
        order = payload.get("order") or {}
        # Ondo's orderId is an alphanumeric string. Pass it through opaquely
        # to the engine; do NOT coerce it via ``int(...)`` (would raise and
        # silently null out the exchange identity).
        eoid_raw = order.get("exchange_order_id")
        if eoid_raw is None or eoid_raw == "":
            eoid_out: Optional[Any] = None
        elif isinstance(eoid_raw, bool):
            eoid_out = None
        elif isinstance(eoid_raw, int):
            eoid_out = int(eoid_raw)
        else:
            text = str(eoid_raw).strip()
            eoid_out = text or None
        return {
            "client_order_id": int(client_order_id),
            "exchange_order_id": eoid_out,
            "submitted_price": order.get("submitted_price"),
            "submitted_volume": order.get("submitted_volume"),
            "status": str(order.get("status") or "filled"),
            "verified": bool(order.get("verified", False)),
            "role": "entry",
            "raw": payload,
        }

    def place_limit(
        self,
        *,
        account: str,
        instrument: str,
        side: str,
        size: Decimal,
        price: Decimal,
        client_order_id: int,
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        cid = encode_gf_client_order_id(int(client_order_id))
        req: Dict[str, Any] = {
            "operation": "new_order",
            "account": account,
            "symbol": instrument,
            "side": side,
            "order_type": "limit",
            "volume": str(size),
            "price": str(price),
            "client_order_id": cid,
        }
        if reduce_only:
            req["reduce_only"] = True
        resp = _get_ondo_agent().execute(req)
        payload = _get_payload(resp)
        order = payload.get("order") or {}
        # Alphanumeric exchange_order_id tolerance — see place_market.
        eoid_raw = order.get("exchange_order_id")
        if eoid_raw is None or eoid_raw == "":
            eoid: Optional[Any] = None
        elif isinstance(eoid_raw, bool):
            eoid = None
        elif isinstance(eoid_raw, int):
            eoid = int(eoid_raw)
        else:
            text = str(eoid_raw).strip()
            eoid = text or None
        if not _is_success(resp) and eoid is None:
            raise RuntimeError(f"place_limit failed: {payload}")

        classification = "OPEN" if _is_success(resp) else None
        remaining = None
        filled_size = None
        if not _is_success(resp) and eoid is not None:
            st = self.get_order_state(account, eoid)
            classification = (
                st.get("classification") or st.get("status") or "UNKNOWN"
            )
            remaining = st.get("remaining_size")
            filled_size = st.get("filled_size")
            if str(classification).upper() in {"", "NONE"}:
                classification = "UNKNOWN"
        if classification is None:
            classification = "OPEN"

        status = str(classification).lower()
        verified = str(classification).upper() in {"OPEN", "FILLED", "PARTIALLY_FILLED"}
        return {
            "client_order_id": int(client_order_id),
            "exchange_order_id": eoid,
            "submitted_price": order.get("submitted_price") or str(price),
            "submitted_volume": order.get("submitted_volume") or str(size),
            "status": status,
            "verified": verified or bool(order.get("verified")),
            "filled_size": filled_size,
            "remaining_size": remaining,
            "role": "tp" if reduce_only else "ladder",
            "raw": payload,
        }

    def set_shared_tp(
        self,
        *,
        account: str,
        instrument: str,
        price: Decimal,
        side: str,
        size: Decimal,
        client_order_id: int,
    ) -> Dict[str, Any]:
        """Net-position scoped TP. Ondo has one TP per market+direction."""
        resp = _get_ondo_agent().execute({
            "operation": "set_tp",
            "account": account,
            "symbol": instrument,
            "price": str(price),
            "size": str(size),
            "side": side,
        })
        payload = _get_payload(resp)
        if not _is_success(resp):
            raise RuntimeError(f"set_shared_tp failed: {payload}")
        action = payload.get("position_action") or {}
        # Alphanumeric exchange_order_id tolerance — see place_market.
        eoid_raw = action.get("exchange_order_id")
        if eoid_raw is None or eoid_raw == "":
            tp_eoid: Optional[Any] = None
        elif isinstance(eoid_raw, bool):
            tp_eoid = None
        elif isinstance(eoid_raw, int):
            tp_eoid = int(eoid_raw)
        else:
            text = str(eoid_raw).strip()
            tp_eoid = text or None
        return {
            "client_order_id": int(client_order_id),
            "exchange_order_id": tp_eoid,
            "submitted_price": action.get("price") or str(price),
            "submitted_volume": action.get("current_size") or str(size),
            "status": action.get("status") or "submitted",
            "verified": bool(action.get("verified")),
            "role": "tp",
            "raw": payload,
        }

    def cancel_order(self, *, account: str, order_index: Any) -> bool:
        # Ondo's orderId is an alphanumeric string; accept it opaquely.
        oid = _oid_to_str(order_index)
        if oid is None:
            return False
        # ``order_index`` is forwarded to the agent so the URL path segment
        # is built from the native string form (not coerced through int).
        resp = _get_ondo_agent().execute({
            "operation": "cancel_order",
            "account": account,
            "order_id": oid,
            "order_index": order_index,
        })
        if _is_success(resp):
            return True
        os_field = getattr(resp, "order_state", None) or {}
        outcome = (os_field or {}).get("outcome") if isinstance(os_field, dict) else None
        return outcome == "ALREADY_TERMINAL"

    def close_position(
        self,
        *,
        account: str,
        instrument: str,
        client_order_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        req: Dict[str, Any] = {
            "operation": "close_position",
            "account": account,
            "symbol": instrument,
        }
        if client_order_id is not None:
            req["client_order_id"] = encode_gf_client_order_id(int(client_order_id))
        resp = _get_ondo_agent().execute(req)
        payload = _get_payload(resp)
        action = payload.get("position_action") or {}
        success = _is_success(resp)
        verified = bool(action.get("verified")) if isinstance(action, dict) else False
        status = action.get("status") if isinstance(action, dict) else None
        # Alphanumeric exchange_order_id tolerance — see place_market.
        close_eoid_raw = action.get("exchange_order_id") if isinstance(action, dict) else None
        if close_eoid_raw is None or close_eoid_raw == "":
            close_eoid: Optional[Any] = None
        elif isinstance(close_eoid_raw, bool):
            close_eoid = None
        elif isinstance(close_eoid_raw, int):
            close_eoid = int(close_eoid_raw)
        else:
            text = str(close_eoid_raw).strip()
            close_eoid = text or None
        return {
            "success": success,
            "verified": verified,
            "outcome": "CLOSED" if verified else (status or ("failed" if not success else "ok")),
            "status": status or ("failed" if not success else "ok"),
            "message": action.get("message") if isinstance(action, dict) else None,
            "error": getattr(getattr(resp, "error", None), "code", None),
            "client_order_id": client_order_id,
            "exchange_order_id": close_eoid,
            "raw": payload,
        }
