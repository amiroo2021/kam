"""Arcus adapter for GoldenFibo.

Mirrors LighterGoldenFiboAdapter: pure I/O over x_arcus_agent, no strategy
logic. Shared TP uses Arcus position-level set_tp (full live size); ladder
and Step0 use new_order limit/market with V2 client ids as clientId strings.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional

from plugins.trade.agents import x_arcus_agent as arcus_agent


def _is_success(resp: Any) -> bool:
    return bool(getattr(resp, "success", False)) and getattr(resp, "error", None) is None


def _get_payload(resp: Any) -> Dict[str, Any]:
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


def _cid_str(client_order_id: int) -> str:
    return str(int(client_order_id))


class ArcusGoldenFiboAdapter:
    """Thin adapter that drives the Arcus agent for GoldenFibo."""

    def __init__(self, name: str = "golden_fibo_arcus") -> None:
        self.name = name

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def resolve_instrument(self, account: str, instrument: str) -> Dict[str, Any]:
        resp = arcus_agent.execute(
            {
                "operation": "resolve_instrument",
                "account": account,
                "symbol": instrument,
            }
        )
        if not _is_success(resp):
            raise RuntimeError(f"resolve_instrument failed: {getattr(resp, 'error', None)}")
        inst = _get_payload(resp).get("instrument") or {}
        # Merge constraints for engine/preflight consumers.
        cons = self.get_venue_constraints(account, instrument)
        out = dict(inst)
        out.setdefault("symbol", inst.get("symbol") or instrument)
        out.setdefault("size_decimals", cons.get("size_decimals"))
        out.setdefault("price_decimals", cons.get("price_decimals"))
        out.setdefault("min_base_amount", cons.get("min_base_amount") or inst.get("minimum_size"))
        out.setdefault("min_quote_amount", cons.get("min_quote_amount"))
        out.setdefault("tick_size", cons.get("tick_size") or inst.get("price_increment"))
        out.setdefault("step_size", cons.get("step_size") or inst.get("size_increment"))
        out.setdefault("market_id", cons.get("market_id"))
        return out

    def position_state(self, account: str, instrument: str) -> Dict[str, Any]:
        resp = arcus_agent.execute(
            {
                "operation": "position_state",
                "account": account,
                "symbol": instrument,
            }
        )
        if not _is_success(resp):
            raise RuntimeError(f"position_state failed: {getattr(resp, 'error', None)}")
        positions = _get_payload(resp).get("positions") or []
        if not positions:
            return {"symbol": instrument, "side": None, "size": "0"}
        pos = positions[0] if isinstance(positions[0], dict) else {}
        side = str(pos.get("side") or "").lower()
        if side in {"buy", "long"}:
            side = "long"
        elif side in {"sell", "short"}:
            side = "short"
        return {
            "symbol": pos.get("symbol") or instrument,
            "side": side if side in {"long", "short"} else None,
            "size": str(pos.get("size") or "0"),
            "entry_price": pos.get("entry_price"),
            "tp": pos.get("tp"),
            "sl": pos.get("sl"),
        }

    def get_venue_constraints(self, account: str, instrument: str) -> Dict[str, Any]:
        resp = arcus_agent.execute(
            {
                "operation": "market_constraints",
                "account": account,
                "symbol": instrument,
            }
        )
        if not _is_success(resp):
            return {}
        return _get_payload(resp).get("order_state") or {}

    def market_price(self, account: str, instrument: str) -> Dict[str, Any]:
        resp = arcus_agent.execute(
            {
                "operation": "market_price",
                "account": account,
                "symbol": instrument,
            }
        )
        if not _is_success(resp):
            return {}
        return _get_payload(resp).get("market_price") or {}

    def market_constraints(self, account: str, instrument: str) -> Dict[str, Any]:
        return self.get_venue_constraints(account, instrument)

    def get_order_state(self, account: str, order_index: int) -> Dict[str, Any]:
        resp = arcus_agent.execute(
            {
                "operation": "get_order_state",
                "account": account,
                "order_index": order_index,
            }
        )
        if not _is_success(resp):
            return {}
        return _get_payload(resp).get("order_state") or {}

    def get_order_state_by_client_id(
        self, account: str, instrument: str, client_order_index: int
    ) -> Dict[str, Any]:
        resp = arcus_agent.execute(
            {
                "operation": "get_order_state_by_client_id",
                "account": account,
                "symbol": instrument,
                "client_order_index": int(client_order_index),
                "client_order_id": int(client_order_index),
            }
        )
        if not _is_success(resp):
            return {}
        return _get_payload(resp).get("order_state") or {}

    # ------------------------------------------------------------------
    # Writes
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
        resp = arcus_agent.execute(
            {
                "operation": "new_order",
                "account": account,
                "symbol": instrument,
                "side": side,
                "order_type": "market",
                "volume": str(size),
                "reduce_only": False,
                "client_order_id": int(client_order_id),
                "client_order_index": int(client_order_id),
            }
        )
        return self._parse_submit(resp, role="entry", client_order_id=client_order_id)

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
        resp = arcus_agent.execute(
            {
                "operation": "new_order",
                "account": account,
                "symbol": instrument,
                "side": side,
                "order_type": "limit",
                "volume": str(size),
                "price": str(price),
                "reduce_only": bool(reduce_only),
                "client_order_id": int(client_order_id),
                "client_order_index": int(client_order_id),
            }
        )
        return self._parse_submit(
            resp, role="tp" if reduce_only else "ladder", client_order_id=client_order_id
        )

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
        """Set ONE position-level Arcus TP (full live size on venue).

        Arcus ``set_tp`` cancels any existing position TP then places a new
        position-level take-profit. Quantity is the live position (engine
        still passes size for parity / logging). V2 client id is forwarded
        when the batch path accepts clientId (set_tp currently mints its own
        id for the batch element; we still record the GF V2 id locally).
        """
        resp = arcus_agent.execute(
            {
                "operation": "set_tp",
                "account": account,
                "symbol": instrument,
                "price": str(price),
                "client_order_id": int(client_order_id),
                "client_order_index": int(client_order_id),
                "size": str(size),
                "side": side,
            }
        )
        payload = _get_payload(resp)
        if not _is_success(resp):
            raise RuntimeError(f"set_shared_tp failed: {payload}")
        action = payload.get("position_action") or {}
        return {
            "exchange_order_id": action.get("exchange_order_id"),
            "client_order_id": int(client_order_id),
            "submitted_price": action.get("price") or str(price),
            "submitted_volume": action.get("current_size") or str(size),
            "status": action.get("status") or "submitted",
            "verified": bool(action.get("verified")),
            "role": "tp",
        }

    def cancel_order(self, *, account: str, order_index: int) -> bool:
        resp = arcus_agent.execute(
            {
                "operation": "cancel_order",
                "account": account,
                "order_index": order_index,
            }
        )
        if _is_success(resp):
            return True
        # Treat already-gone as success for engine cancel-before-replace.
        err = getattr(resp, "error", None)
        code = getattr(err, "code", None) if err is not None else None
        st = self.get_order_state(account, int(order_index))
        if not st:
            return True
        tax = str(st.get("taxonomy") or "").upper()
        status = str(st.get("status") or "").upper()
        if tax in {"CANCELED", "FILLED", "EXPIRED", "REJECTED"}:
            return True
        if status in {"CANCELED", "CANCELLED", "FILLED", "CLOSED", "DONE"}:
            return True
        if code in {"ORDER_NOT_FOUND", "VERIFICATION_FAILED"} and tax not in {"ACTIVE"}:
            return True
        return False

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
            req["client_order_id"] = int(client_order_id)
            req["client_order_index"] = int(client_order_id)
        resp = arcus_agent.execute(req)
        payload = _get_payload(resp)
        action = payload.get("position_action") or {}
        return {
            "success": _is_success(resp),
            "verified": bool(action.get("verified")) if isinstance(action, dict) else False,
            "status": (action.get("status") if isinstance(action, dict) else None),
            "message": (action.get("message") if isinstance(action, dict) else None),
            "error": getattr(getattr(resp, "error", None), "code", None),
            "client_order_id": client_order_id,
            "raw": payload,
        }

    def _parse_submit(
        self, resp: Any, *, role: str, client_order_id: int
    ) -> Dict[str, Any]:
        if not _is_success(resp):
            raise RuntimeError(f"new_order({role}) failed: {getattr(resp, 'error', None)}")
        order = _get_payload(resp).get("order") or {}
        return {
            "client_order_id": int(
                order.get("client_order_id") or client_order_id
            ),
            "exchange_order_id": order.get("exchange_order_id"),
            "submitted_price": order.get("submitted_price"),
            "submitted_volume": order.get("submitted_volume"),
            "status": str(order.get("status") or "submitted"),
            "verified": bool(order.get("verified", False)),
            "role": role,
        }
