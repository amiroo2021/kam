"""Rise limit-order result normalization tests.

GoldenFibo ``RiseGoldenFiboAdapter.place_limit`` must receive a usable
canonical result whenever Rise accepted the order, even if immediate venue
state is OPEN / PARTIALLY_FILLED / FILLED-quickly / order-disappeared. The raw
Rise exchange_order_id must always be preserved. Never infer FILLED from
disappearance alone.

These tests patch only low-level reads (markets, portfolio, open-orders,
nonce, post_json) and drive the REAL ``x_rise_agent.execute()`` dispatch
(including the gated reconcile path) plus the real adapter ``place_limit``.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest import mock

from plugins.trade.agents import x_rise_agent as rise
from plugins.trade.golden_fibo.rise_adapter import RiseGoldenFiboAdapter


IDENT = "0x" + "ab" * 20
SIGNER = "0x" + "11" * 32
MARKET_ID = "5"

OID_OPEN = "0x0000000000000000000000000000000000000005aa11"
OID_PART = "0x0000000000000000000000000000000000000005bb22"
OID_FILL = "0x0000000000000000000000000000000000000005cc33"
OID_UNK = "0x0000000000000000000000000000000000000005dd44"


def _markets_payload() -> Dict[str, Any]:
    return {
        "markets": [
            {
                "market_id": MARKET_ID,
                "display_name": "HYPE/USDC",
                "base_asset_symbol": "HYPE",
                "active": True,
                "config": {
                    "name": "HYPE/USDC",
                    "step_size": "0.01",
                    "step_price": "0.001",
                    "min_order_size": "0.01",
                },
                "last_price": "69.8",
            }
        ]
    }


def _open_row(*, order_id: str, remaining: str = "0.02") -> Dict[str, Any]:
    return {
        "market_id": MARKET_ID,
        "order_id": order_id,
        "resting_order_id": str(int(order_id, 16) % 1000000),
        "wide_order_id": "wid",
        "symbol": "HYPE",
        "side": 0,          # normalize expects raw side as int
        "size": remaining,
        "size_steps": int(Decimal(remaining) / Decimal("0.01")),
        "price_ticks": 69700,
        "reduce_only": False,
        "post_only": False,
        "order_type": 1,
        "time_in_force": "GTC",
    }


def _post_success(order_id: str) -> Dict[str, Any]:
    return {"data": {"order_id": order_id, "side_int": 0, "market_id": MARKET_ID}}


def _open_payload(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"data": {"orders": rows}}


def _flat_pos() -> Dict[str, Any]:
    return {"side": None, "size": "0", "entry_price": None}


def _snap(pre: Dict[str, Any], post: Dict[str, Any]):
    calls = {"n": 0}

    def snap(wallet, symbol):
        calls["n"] += 1
        return post if calls["n"] > 1 else pre

    return snap


def _run_new_order(
    *,
    reconcile: bool = True,
    order_id: str = OID_OPEN,
    open_rows: Optional[List[Dict[str, Any]]] = None,
    pre: Optional[Dict[str, Any]] = None,
    post: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if pre is None:
        pre = _flat_pos()
    if post is None:
        post = _flat_pos()
    if open_rows is None:
        open_rows = [_open_row(order_id=order_id)]
    with mock.patch.object(rise, "_fetch_markets_payload", return_value=_markets_payload()), \
         mock.patch.object(rise, "_fetch_nonce_state",
                           return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
         mock.patch.object(rise, "_fetch_portfolio", return_value={"data": {"positions": []}}), \
         mock.patch.object(rise, "_fetch_open_orders_payload", return_value=_open_payload(open_rows)), \
         mock.patch.object(rise, "_rise_position_snapshot", side_effect=_snap(pre, post)), \
         mock.patch.object(rise, "_post_json", return_value=_post_success(order_id)), \
         mock.patch.object(rise, "_lookup_credentials", return_value=(IDENT, SIGNER)):
        req: Dict[str, Any] = {
            "operation": "new_order",
            "account": "BASED",
            "symbol": "HYPE",
            "side": "buy",
            "order_type": "limit",
            "volume": "0.02",
            "price": "69.7",
            "time_in_force": "GTC",
            "reduce_only": False,
            "reconcile_on_unverified": bool(reconcile),
        }
        return rise.execute(req).to_dict()


def _snap(pre: Dict[str, Any], post: Dict[str, Any]):
    calls = {"n": 0}

    def snap(wallet, symbol):
        calls["n"] += 1
        return post if calls["n"] > 1 else pre

    return snap


class LimitResultReconciliationTests(unittest.TestCase):
    """GATED classification through the real new_order dispatch."""

    def test_resting_open_returns_success_with_oid(self):
        d = _run_new_order(
            order_id=OID_OPEN,
            open_rows=[_open_row(order_id=OID_OPEN)],
            pre=_flat_pos(), post=_flat_pos(),
        )
        self.assertTrue(d["success"])
        self.assertEqual(d["order"]["exchange_order_id"], OID_OPEN)

    def test_partial_fill_then_disappears(self):
        # Order gone from openOrders; position grew 0.01 < requested 0.02.
        d = _run_new_order(
            order_id=OID_PART,
            open_rows=[],
            pre=_flat_pos(), post={"side": "long", "size": "0.01", "entry_price": "69.7"},
        )
        self.assertTrue(d["success"])
        self.assertEqual(d["order_state"]["classification"], "PARTIALLY_FILLED")
        self.assertEqual(d["order"]["exchange_order_id"], OID_PART)

    def test_full_fill_then_disappears(self):
        # Order gone from openOrders; position grew 0.02 == requested.
        d = _run_new_order(
            order_id=OID_FILL,
            open_rows=[],
            pre=_flat_pos(), post={"side": "long", "size": "0.02", "entry_price": "69.7"},
        )
        self.assertEqual(d["order_state"]["classification"], "FILLED")
        self.assertEqual(d["order"]["exchange_order_id"], OID_FILL)

    def test_disappear_no_growth_unknown_not_filled(self):
        d = _run_new_order(
            order_id=OID_UNK,
            open_rows=[{"market_id": "5", "order_id": OID_UNK, "side": 0,
                        "size_steps": 0, "price_ticks": 0}],
            pre=_flat_pos(), post=_flat_pos(),
        )
        self.assertEqual(d["order_state"]["classification"], "UNKNOWN")
        self.assertNotEqual(d["order"]["status"], "filled")

    def test_default_no_reconcile_unchanged(self):
        # Without the gated flag, an unverifiable order stays VERIFICATION_FAILED.
        d = _run_new_order(
            reconcile=False,
            order_id=OID_UNK,
            open_rows=[{"market_id": "5", "order_id": OID_UNK, "side": 0,
                        "size_steps": 0, "price_ticks": 0}],
            pre=_flat_pos(), post=_flat_pos(),
        )
        self.assertFalse(d["success"])
        self.assertEqual(d["error"]["code"], "VERIFICATION_FAILED")

    def test_raw_oid_preserved_even_when_verifier_misses(self):
        # Response carries the oid but openOrders doesn't. Gated path classifies
        # UNKNOWN but MUST still surface the raw exchange_order_id.
        d = _run_new_order(
            order_id=OID_UNK,
            open_rows=[],
            pre=_flat_pos(), post=_flat_pos(),
        )
        self.assertTrue(d["success"])
        self.assertEqual(d["order"]["exchange_order_id"], OID_UNK)


class LimitResultExtendedTests(unittest.TestCase):
    """Resting-partial, position-delta FILLED, no-duplicate, /trade default."""

    def test_partial_fill_remains_active_returns_open(self):
        # Order still resting with remaining 0.01 -> OPEN (not fabricated fill).
        d = _run_new_order(
            order_id=OID_PART,
            open_rows=[_open_row(order_id=OID_PART, remaining="0.01")],
            pre=_flat_pos(), post={"side": "long", "size": "0.01", "entry_price": "69.7"},
        )
        self.assertEqual(d["order_state"]["classification"], "OPEN")

    def test_filled_via_position_delta_with_disappearance(self):
        # No open order; exact position growth => FILLED.
        d = _run_new_order(
            order_id=OID_FILL, open_rows=[],
            pre=_flat_pos(), post={"side": "long", "size": "0.02", "entry_price": "69.7"},
        )
        self.assertEqual(d["order_state"]["classification"], "FILLED")
        self.assertEqual(d["order"]["exchange_order_id"], OID_FILL)

    def test_no_duplicate_submit_resting_open(self):
        # A resting OPEN order must not be re-submitted by a subsequent call.
        first = _run_new_order(
            order_id=OID_OPEN,
            open_rows=[_open_row(order_id=OID_OPEN)],
            pre=_flat_pos(), post=_flat_pos(),
        )
        self.assertTrue(first["success"])
        self.assertEqual(first["order"]["exchange_order_id"], OID_OPEN)

    def test_trade_new_order_default_unchanged(self):
        # Without the gated flag and no open-row match, /trade stays failure.
        d = _run_new_order(
            reconcile=False,
            order_id=OID_OPEN,
            open_rows=[_open_row(order_id="0x0000000000000000000000000000000000000000ffeeff")],
            pre=_flat_pos(), post=_flat_pos(),
        )
        self.assertFalse(d["success"])
        self.assertEqual(d["error"]["code"], "VERIFICATION_FAILED")


class AdapterPlaceLimitThroughRealDispatchTests(unittest.TestCase):
    """GoldenFibo adapter -> real x_rise_agent.execute() (no execute mock).

    Patches use STRING paths (``plugins.trade.agents.x_rise_agent.<fn>``) so
    they always target the same ``sys.modules`` object ``_get_rise_agent()``
    resolves at call time — immune to full-suite module re-imports.
    """

    _P = "plugins.trade.agents.x_rise_agent."

    def test_full_fill_through_adapter(self):
        ad = RiseGoldenFiboAdapter()
        order_id = OID_FILL
        with mock.patch(self._P + "_fetch_markets_payload",
                        return_value=_markets_payload()), \
             mock.patch(self._P + "_fetch_nonce_state",
                        return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
             mock.patch(self._P + "_fetch_portfolio",
                        return_value={"data": {"positions": []}}), \
             mock.patch(self._P + "_fetch_open_orders_payload",
                        return_value=_open_payload([])), \
             mock.patch(self._P + "_rise_position_snapshot",
                        side_effect=_snap(_flat_pos(), {"side": "long", "size": "0.02", "entry_price": "69.7"})), \
             mock.patch(self._P + "_post_json", return_value=_post_success(order_id)), \
             mock.patch(self._P + "_lookup_credentials", return_value=(IDENT, SIGNER)):
            out = ad.place_limit(
                account="BASED", instrument="HYPE", side="buy",
                size=Decimal("0.02"), price=Decimal("69.7"),
                client_order_id=333, reduce_only=False,
            )
        self.assertEqual(out["exchange_order_id_hex"], order_id)
        self.assertTrue(out["verified"])
        self.assertEqual(out["status"], "filled")

    def test_partial_fill_through_adapter(self):
        ad = RiseGoldenFiboAdapter()
        order_id = OID_PART
        with mock.patch(self._P + "_fetch_markets_payload",
                            return_value=_markets_payload()), \
             mock.patch(self._P + "_fetch_nonce_state",
                        return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
             mock.patch(self._P + "_fetch_portfolio",
                        return_value={"data": {"positions": []}}), \
             mock.patch(self._P + "_fetch_open_orders_payload",
                        return_value=_open_payload([])), \
             mock.patch(self._P + "_rise_position_snapshot",
                        side_effect=_snap(_flat_pos(), {"side": "long", "size": "0.01", "entry_price": "69.7"})), \
             mock.patch(self._P + "_post_json", return_value=_post_success(order_id)), \
             mock.patch(self._P + "_lookup_credentials", return_value=(IDENT, SIGNER)):
            out = ad.place_limit(
                account="BASED", instrument="HYPE", side="buy",
                size=Decimal("0.02"), price=Decimal("69.7"),
                client_order_id=334, reduce_only=False,
            )
        self.assertEqual(out["exchange_order_id_hex"], order_id)
        self.assertEqual(out["status"], "partially_filled")
        self.assertTrue(out["verified"])


if __name__ == "__main__":
    unittest.main()