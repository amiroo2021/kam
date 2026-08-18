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

import plugins.trade.agents.x_lighter_agent as lighter_agent
from plugins.trade.canonical import make_success


def _is_success(resp: Any) -> bool:
    return bool(getattr(resp, "success", False)) and getattr(resp, "error", None) is None


def _get_payload(resp: Any) -> Dict[str, Any]:
    """Best-effort payload extraction from the canonical response."""
    payload = getattr(resp, "payload", None)
    if isinstance(payload, dict):
        return payload
    # Fallback: look on the response object for matching attributes
    return {}


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
        return _get_payload(resp).get("position") or {}

    def get_order_state(self, account: str, order_index: int) -> Dict[str, Any]:
        resp = lighter_agent.execute({
            "operation": "get_order_state",
            "account": account,
            "order_index": int(order_index),
        })
        if not _is_success(resp):
            # Treat missing order as empty state
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
