"""Lighter adapter for GoldenFibo v1.

Uses Generic goldenFibo-required ops on x_lighter_agent:
- resolve_instrument
- position_state
- new_order (market + limit)
- get_order_state
- cancel_order

Always passes deterministic client_order_id so the engine can
reconcile the expected order across restart.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from plugins.trade.agents import x_lighter_agent as lighter_agent
from plugins.trade.canonical import make_success


def _is_success(resp: Any) -> bool:
    return bool(getattr(resp, "success", False)) and getattr(resp, "error", None) is None


def _get_payload(resp: Any) -> Dict[str, Any]:
    """Best-effort payload extraction from the canonical response.

    The canonical response stores payload fields directly on the
    response object (instrument, order, position, etc.). This helper
    returns a dict with the keys callers expect, populated from the
    response attributes when present.

    Objects that expose a ``to_dict()`` method are flattened into a
    dict (preserving nested structure) so callers can use uniform
    dict-style access. Lists (positions, order_groups, order_states)
    are similarly flattened element-wise.
    """
    if resp is None:
        return {}
    out: Dict[str, Any] = {}

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


class LighterGoldenFiboAdapter:
    """Thin adapter that drives the Lighter agent for GoldenFibo.

    Pure I/O. No state machine logic. The engine uses these methods.
    """

    def __init__(self, name: str = "golden_fibo_lighter") -> None:
        self.name = name

    # ------------------------------------------------------------------
    # Generic venue reads
    # ------------------------------------------------------------------
    def resolve_instrument(self, account: str, instrument: str) -> Dict[str, Any]:
        resp = lighter_agent.execute({
            "operation": "resolve_instrument",
            "account": account,
            "symbol": instrument,
        })
        if not _is_success(resp):
            raise RuntimeError(
                f"resolve_instrument failed: {getattr(resp, 'error', None)}"
            )
        return _get_payload(resp).get("instrument") or {}

    def position_state(self, account: str, instrument: str) -> Dict[str, Any]:
        resp = lighter_agent.execute({
            "operation": "position_state",
            "account": account,
            "symbol": instrument,
        })
        if not _is_success(resp):
            raise RuntimeError(
                f"position_state failed: {getattr(resp, 'error', None)}"
            )
        positions = _get_payload(resp).get("positions") or []
        return positions[0] if positions else {}

    def get_venue_constraints(self, account: str, instrument: str) -> Dict[str, Any]:
        """Thin read of full venue constraints for preflight validation.

        Returns min_base_amount, min_quote_amount, size_decimals,
        price_decimals, tick_size. Read-only.
        """
        resp = lighter_agent.execute({
            "operation": "market_constraints",
            "account": account,
            "symbol": instrument,
        })
        if not _is_success(resp):
            return {}
        return _get_payload(resp).get("order_state") or {}

    def get_order_state(self, account: str, order_index: int) -> Dict[str, Any]:
        resp = lighter_agent.execute({
            "operation": "get_order_state",
            "account": account,
            "order_index": int(order_index),
        })
        if not _is_success(resp):
            # Treat missing order as empty state
            return {}
        return _get_payload(resp).get("order_state") or _get_payload(resp).get("order") or {}

    def get_order_state_by_client_id(
        self, account: str, instrument: str, client_order_index: int
    ) -> Dict[str, Any]:
        """Look up an order by its deterministic client_order_index.

        Returns the normalized record (with actual_fill_price derived
        from filled_quote/filled_base when the native field is absent),
        or {} when not found. Read-only.
        """
        resp = lighter_agent.execute({
            "operation": "get_order_state_by_client_id",
            "account": account,
            "symbol": instrument,
            "client_order_index": int(client_order_index),
        })
        if not _is_success(resp):
            return {}
        return _get_payload(resp).get("order_state") or {}

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------
    def place_market(
        self,
        *,
        account: str,
        instrument: str,
        side: str,
        size: Decimal,
        client_order_id: int,
    ) -> Dict[str, Any]:
        resp = lighter_agent.execute({
            "operation": "new_order",
            "account": account,
            "symbol": instrument,
            "side": side,
            "order_type": "market",
            "volume": str(size),
            "reduce_only": False,
            "client_order_id": int(client_order_id),
        })
        return self._parse_submit(resp, role="entry")

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
        resp = lighter_agent.execute({
            "operation": "new_order",
            "account": account,
            "symbol": instrument,
            "side": side,
            "order_type": "limit",
            "volume": str(size),
            "price": str(price),
            "reduce_only": bool(reduce_only),
            "client_order_id": int(client_order_id),
        })
        return self._parse_submit(resp, role="tp" if reduce_only else "ladder")

    def set_shared_tp(
        self,
        *,
        account: str,
        instrument: str,
        price: Decimal,
    ) -> Dict[str, Any]:
        """Set/replace the ONE shared TP on the accumulated position.

        Thin wrapper over x_lighter_agent.execute(operation="set_tp").
        The agent derives the position size, closing side, quantization,
        reduce_only, TP trigger semantics, and verification itself —
        the adapter never constructs Lighter TP payloads.
        """
        resp = lighter_agent.execute({
            "operation": "set_tp",
            "account": account,
            "symbol": instrument,
            "price": str(price),
        })
        state = _get_payload(resp).get("position_action") or {}
        if not _is_success(resp):
            raise RuntimeError(
                f"set_shared_tp failed: {getattr(resp, 'error', None)}"
            )
        # Normalize the agent's verified result into the adapter shape.
        return {
            "verified": bool(state.get("verified")),
            "submitted_price": state.get("price"),
            "exchange_order_id": state.get("exchange_order_id"),
            "current_side": state.get("current_side"),
            "current_size": state.get("current_size"),
            "role": "tp",
        }

    def cancel_order(self, *, account: str, order_index: int) -> bool:
        resp = lighter_agent.execute({
            "operation": "cancel_order",
            "account": account,
            "order_index": int(order_index),
        })
        return _is_success(resp)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _parse_submit(self, resp: Any, *, role: str) -> Dict[str, Any]:
        if not _is_success(resp):
            raise RuntimeError(
                f"new_order({role}) failed: {getattr(resp, 'error', None)}"
            )
        order = _get_payload(resp).get("order") or {}
        return {
            "client_order_id": int(client_order_id_unwrap(order.get("client_order_id"))),
            "exchange_order_id": order.get("exchange_order_id"),
            "submitted_price": order.get("submitted_price"),
            "submitted_volume": order.get("submitted_volume"),
            "status": str(order.get("status") or "submitted"),
            "verified": bool(order.get("verified", False)),
            "role": role,
        }


def client_order_id_unwrap(value: Any) -> int:
    """Best-effort unwrap of client_order_id from various shapes."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    if isinstance(value, dict):
        # {"client_order_id": 12345}
        inner = value.get("client_order_id")
        if isinstance(inner, int):
            return inner
        if isinstance(inner, str):
            try:
                return int(inner)
            except ValueError:
                return 0
    return 0
