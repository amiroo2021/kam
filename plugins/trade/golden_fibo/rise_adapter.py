"""Rise adapter for GoldenFibo.

Mirrors ``ArcusGoldenFiboAdapter`` and ``LighterGoldenFiboAdapter`` — pure I/O
over :mod:`x_rise_agent`, no strategy logic. Shares the engine contract:

* :meth:`place_market` → :func:`x_rise_agent._execute_market_immediate`
* :meth:`place_limit` → :func:`x_rise_agent._execute_new_order` (LIMIT)
* :meth:`set_shared_tp` → :func:`x_rise_agent._execute_set_tp` (position-level TPSL)
* :meth:`cancel_order` → :func:`x_rise_agent._execute_cancel_order` (single order by exchange_order_id)
* :meth:`close_position` → :func:`x_rise_agent._execute_close_position` (Phase 4 reduce-only IOC)
* :meth:`position_state`, :meth:`get_venue_constraints`, :meth:`resolve_instrument` → reads
* :meth:`get_order_state` / :meth:`get_order_state_by_client_id` → return {} (Rise rejects non-zero client_order_id so client-id lookup is meaningless)

Identity model
==============

GoldenFibo V2 client IDs remain INTERNAL state identity only on Rise.

* Persist: ``cycle_uid``, ``step``, ``role``, ``seq`` (V2 integer allocation
  in :mod:`plugins.trade.golden_fibo.client_id_v2`).
* Persist tracked ``pending_order_exchange_id`` etc. as the Rise hex venue
  order id, **stored as** ``int(hex_str, 16)`` to remain compatible with the
  engine's ``Optional[int]`` slot (engine always wraps in ``int(...)``).
* The Rise wire ``client_order_id`` MUST remain the literal string ``"0"``.
  Live evidence: Rise's on-chain ``PlaceOrderWithPermitV2`` reverts on any
  non-zero value. Production normalizer in :mod:`x_rise_agent` enforces this
  before mutation; the adapter does not even attempt V2 transport.

Rise exchange_order_id round-trip
==================================

The engine hands us ``order_index: int``. We translate hex→int on read
and int→hex on write so all venue I/O is hex-string based, while the
in-memory state remains an integer the engine accepts without changes.

History / attribution
=====================

Rise does NOT echo ``client_order_id`` in ``/v1/orders/open`` and rejects
non-zero values on submit. Therefore Rise exchange-only historical
attribution is NOT supported. Identity survives only inside the persisted
GoldenFibo service_state on the same server.

This is documented as a known limitation, NOT as a GoldenFibo-engine
limitation. The engine continues to compute V2 identities; only the
venue-side wire and read-back are restricted.
"""

from __future__ import annotations

import importlib
import sys
from decimal import Decimal
from typing import Any, Dict, Optional


def _get_rise_agent() -> Any:
    """Return the current ``x_rise_agent`` module object.

    Resolved lazily through ``sys.modules`` (falling back to a fresh import)
    so that under test isolation the module referenced here is always the
    same object the test suite patches via
    ``plugins.trade.agents.x_rise_agent``. Binds no stale module identity.
    """
    name = "plugins.trade.agents.x_rise_agent"
    mod = sys.modules.get(name)
    if mod is None:
        mod = importlib.import_module(name)
    return mod


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

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


def _hex_to_int(value: Any) -> Optional[int]:
    """Translate a Rise venue hex order id to the engine's int slot.

    The engine stores pending_order_exchange_id etc. as ``Optional[int]``.
    Rise venue ids are long hex strings (e.g. ``0xc0000024ef...00006f``).
    Round-trip via ``int(value, 16)``. Returns None on unparseable input.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.startswith("0x") or text.startswith("0X"):
            return int(text, 16)
        return int(text)
    except (TypeError, ValueError):
        return None


def _int_to_hex(value: Any) -> Optional[str]:
    """Inverse of :func:`_hex_to_int` for cancel/state reads.

    Always returns canonical hex form (``0x...``) for non-zero integers,
    regardless of whether the input was a hex string or a decimal integer
    string. ``0`` and ``None`` and unparseable strings return ``None``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            n = int(text, 16) if text.startswith(("0x", "0X")) else int(text)
        except (TypeError, ValueError):
            return None
        if n == 0:
            return None
        return "0x{:x}".format(n)
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n == 0:
        return None
    return "0x{:x}".format(n)


# ------------------------------------------------------------------
# Adapter
# ------------------------------------------------------------------

class RiseGoldenFiboAdapter:
    """Thin adapter that drives the Rise agent for GoldenFibo."""

    def __init__(self, name: str = "golden_fibo_rise") -> None:
        self.name = name

    # ----- Generic reads -------------------------------------------------

    def resolve_instrument(self, account: str, instrument: str) -> Dict[str, Any]:
        resp = _get_rise_agent().execute({
            "operation": "resolve_instrument",
            "account": account,
            "symbol": instrument,
        })
        if not _is_success(resp):
            raise RuntimeError(
                f"resolve_instrument failed: {getattr(resp, 'error', None)}"
            )
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
        resp = _get_rise_agent().execute({
            "operation": "position_state",
            "account": account,
            "symbol": instrument,
        })
        if not _is_success(resp):
            raise RuntimeError(
                f"position_state failed: {getattr(resp, 'error', None)}"
            )
        positions = _get_payload(resp).get("positions") or []
        if not positions:
            return {"symbol": instrument, "side": None, "size": "0", "entry_price": None}
        pos = positions[0] if isinstance(positions[0], dict) else {}
        return self._normalize_position_row(pos, instrument)

    def _normalize_position_row(self, pos: Dict[str, Any], instrument: str) -> Dict[str, Any]:
        raw_side = pos.get("side")
        side_norm = str(raw_side or "").lower() if raw_side is not None else None
        if side_norm in {"buy", "long"}:
            side = "long"
        elif side_norm in {"sell", "short"}:
            side = "short"
        elif side_norm in {"flat", "none", "null", ""}:
            side = None
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

    def get_venue_constraints(self, account: str, instrument: str) -> Dict[str, Any]:
        resp = _get_rise_agent().execute({
            "operation": "market_constraints",
            "account": account,
            "symbol": instrument,
        })
        if not _is_success(resp):
            return {}
        return _get_payload(resp).get("order_state") or {}

    def market_price(self, account: str, instrument: str) -> Dict[str, Any]:
        resp = _get_rise_agent().execute({
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

        """Rise does not have a "by exchange order id" surface for fill history.

        We resolve the engine's stored int (which we round-tripped via
        ``int(hex, 16)``) back to a hex string and consult the live
        :func:`positions_orders` + :func:`_fetch_open_orders_payload` to
        determine ACTIVE vs terminal. We never claim FILLED on absence —
        absence is treated as UNKNOWN / NEEDS_RECOVERY signal.
        """
        hex_id = _int_to_hex(order_index)
        if hex_id is None:
            return {}
        # Try openOrders snapshot.
        try:
            import sys
            wallet, _ = _get_rise_agent()._lookup_credentials(account)
            open_payload = _get_rise_agent()._fetch_open_orders_payload(wallet)
            markets_payload = _get_rise_agent()._fetch_markets_payload()
            cache = _get_rise_agent()._market_cache(
                markets_payload,
                {},
            )
            rows = _get_rise_agent()._normalize_open_orders(open_payload, cache)
        except Exception:
            rows = []
        for row in rows:
            if str(row.get("order_id") or "") == hex_id:
                # Resting.
                size = row.get("size")
                try:
                    size_dec = Decimal(str(size))
                except Exception:
                    size_dec = Decimal("0")
                return {
                    "exchange_order_id": hex_id,
                    "status": "OPEN",
                    "taxonomy": "ACTIVE",
                    "side": row.get("side"),
                    "requested_size": str(size_dec),
                    "filled_size": "0",
                    "remaining_size": str(size_dec),
                    "limit_price": row.get("price"),
                    "average_fill_price": None,
                }
        # Not in openOrders. Per Phase 4 contract: unknown terminal state,
        # not a synthesized FILLED. Caller MUST verify against actual position
        # delta before adopting FILLED semantics.
        return {
            "exchange_order_id": hex_id,
            "status": "UNKNOWN",
            "taxonomy": "UNKNOWN",
            "note": "Rise openOrders no longer shows this id; ownership verification required",
        }

    def get_order_state_by_client_id(
        self,
        account: str,
        instrument: str,
        client_order_index: int,
    ) -> Dict[str, Any]:
        """Rise rejects non-zero client_order_id; this lookup is meaningless."""
        return {}

    # ----- Writes --------------------------------------------------------

    def place_market(
        self,
        *,
        account: str,
        instrument: str,
        side: str,
        size: Decimal,
        client_order_id: int,
    ) -> Dict[str, Any]:
        """Step0 immediate fill via bounded LIMIT + IOC.

        The supplied ``client_order_id`` is intentionally NOT forwarded —
        Rise's on-chain PlaceOrderWithPermitV2 reverts on any non-zero
        value. We still record the GF V2 id locally for engine identity.
        """
        resp = _get_rise_agent().execute({
            "operation": "market_immediate",
            "account": account,
            "symbol": instrument,
            "side": side,
            "volume": str(size),
            "slip_pct": "0.005",  # conservative default; engine controls if needed
            "max_wait_seconds": 6,
        })
        payload = _get_payload(resp)
        if not _is_success(resp):
            raise RuntimeError(f"place_market failed: {payload}")
        order = payload.get("order") or {}
        return {
            "client_order_id": int(client_order_id),  # engine V2 id (local only)
            "exchange_order_id": _hex_to_int(order.get("exchange_order_id")),
            "exchange_order_id_hex": order.get("exchange_order_id"),
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
        resp = _get_rise_agent().execute({
            "operation": "new_order",
            "account": account,
            "symbol": instrument,
            "side": side,
            "order_type": "limit",
            "volume": str(size),
            "price": str(price),
            "time_in_force": "GTC",
            "reduce_only": bool(reduce_only),
            "reconcile_on_unverified": True,  # GF gated path: classify via position
        })
        payload = _get_payload(resp)
        order = payload.get("order") or {}
        state = payload.get("order_state") or {}
        classification = state.get("classification")
        # A usable result exists whenever the submit was ACCEPTED (raw
        # exchange_order_id present), even if verified=False / partial / filled.
        if not _is_success(resp):
            # Only raise when there is genuinely no usable order record (e.g.
            # the venue rejected the submission outright).
            if not order.get("exchange_order_id") and not _hex_to_int(order.get("exchange_order_id")):
                raise RuntimeError(f"place_limit failed: {payload}")
        status = str(order.get("status") or "submitted")
        verified = bool(order.get("verified") or classification in ("OPEN", "FILLED", "PARTIALLY_FILLED"))
        return {
            "client_order_id": int(client_order_id),  # engine V2 id (local only)
            "exchange_order_id": _hex_to_int(order.get("exchange_order_id")),
            "exchange_order_id_hex": order.get("exchange_order_id"),
            "submitted_price": order.get("submitted_price"),
            "submitted_volume": order.get("submitted_volume"),
            "status": classification.lower() if classification else status,
            "verified": verified,
            "filled_size": order.get("submitted_volume"),
            "remaining_size": state.get("remaining_size"),
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
        """Position-level TPSL on Rise.

        Rise ``set_tp`` cancels any existing position TP then places a new
        full-live-size take-profit. There is no per-TP ``exchange_order_id``
        we can independently own; verification relies on reading
        ``positions_orders`` and confirming ``pos.tp`` matches the expected
        trigger.
        """
        resp = _get_rise_agent().execute({
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
        return {
            "client_order_id": int(client_order_id),
            # Rise TPSL is position-level; we surface the exchange_order_id of
            # the underlying TPSL order if the response carries one, else None.
            "exchange_order_id": _hex_to_int(action.get("exchange_order_id")),
            "exchange_order_id_hex": action.get("exchange_order_id"),
            "submitted_price": action.get("price") or str(price),
            "submitted_volume": action.get("current_size") or str(size),
            "status": action.get("status") or "submitted",
            "verified": bool(action.get("verified")),
            "role": "tp",
            "raw": payload,
        }

    def cancel_order(self, *, account: str, order_index: int) -> bool:
        """Single-order cancel by venue exchange_order_id.

        The engine passes the int round-tripped via ``int(hex, 16)``; we
        convert back to hex and call the Phase 2 primitive. Outcome is
        idempotent ALREADY_TERMINAL or explicit failure.
        """
        hex_id = _int_to_hex(order_index)
        if hex_id is None:
            # Not a hex int — engine caller passed something else. Refuse.
            return False
        # Look up the symbol so we can cross-check identity (Phase 2 best practice).
        symbol = self._resolve_symbol_for_order(account, hex_id)
        if symbol is None:
            return False
        req: Dict[str, Any] = {
            "operation": "cancel_order",
            "account": account,
            "exchange_order_id": hex_id,
        }
        if symbol:
            req["symbol"] = symbol
        resp = _get_rise_agent().execute(req)
        if _is_success(resp):
            return True
        # Idempotent: ALREADY_TERMINAL counts as success (engine may re-call cancel
        # on an order that already disappeared; safe to ignore).
        os_field = getattr(resp, "order_state", None) or {}
        outcome = (os_field or {}).get("outcome") if isinstance(os_field, dict) else None
        if outcome == "ALREADY_TERMINAL":
            return True
        err = getattr(resp, "error", None)
        return False

    def _resolve_symbol_for_order(self, account: str, hex_id: str) -> Optional[str]:
        """Find which market_id the given exchange_order_id belongs to.

        Returns the bare asset symbol (the form engine stores in
        state.instrument) so the cancel call can cross-check identity.
        """
        try:
            wallet, _ = _get_rise_agent()._lookup_credentials(account)
            open_payload = _get_rise_agent()._fetch_open_orders_payload(wallet)
            markets_payload = _get_rise_agent()._fetch_markets_payload()
            cache = _get_rise_agent()._market_cache(markets_payload, {})
            rows = _get_rise_agent()._normalize_open_orders(open_payload, cache)
        except Exception:
            return None
        for row in rows:
            if str(row.get("order_id") or "") == hex_id:
                return str(row.get("symbol") or "")
        return None

    def close_position(
        self,
        *,
        account: str,
        instrument: str,
        client_order_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Phase 4 reduce-only IOC close of the live Rise position."""
        resp = _get_rise_agent().execute({
            "operation": "close_position",
            "account": account,
            "symbol": instrument,
            "slip_pct": "0.005",
            "max_wait_seconds": 10,
        })
        payload = _get_payload(resp)
        os_field = payload.get("order_state") or {}
        success = _is_success(resp)
        outcome = os_field.get("outcome") if isinstance(os_field, dict) else None
        verified = outcome == "CLOSED" or outcome == "ALREADY_FLAT"
        return {
            "success": success,
            "verified": verified,
            "outcome": outcome,
            "status": outcome or ("failed" if not success else "ok"),
            "message": os_field.get("reason") if isinstance(os_field, dict) else None,
            "error": getattr(getattr(resp, "error", None), "code", None),
            "client_order_id": client_order_id,
            "exchange_order_id": _hex_to_int(os_field.get("exchange_order_id")),
            "raw": payload,
        }