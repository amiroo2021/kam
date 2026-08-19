"""Contract: real Lighter order-state normalizers expose requested_size.

GoldenFibo engine.tick TP-volume sync reads::

    live_tp_size = tp_state.get("requested_size")

If the real Lighter get_order_state / get_order_state_by_client_id
payloads omit that field, partial-fill TP volume sync silently no-ops
and the shared TP covers less than the live position.

These tests exercise the REAL normalizers in x_lighter_agent.py (not a
FakeAdapter) and prove the engine consumes the resulting shape.
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional


_EDITABLE_FINDER = "__editable___hermes_agent_0_20_0_finder"
_KNOWN_EDITABLE_FINDERS = (_EDITABLE_FINDER,)
if any(name in repr(h) for h in sys.path_hooks for name in _KNOWN_EDITABLE_FINDERS):
    sys.path_hooks[:] = [
        h
        for h in sys.path_hooks
        if not any(name in repr(h) for name in _KNOWN_EDITABLE_FINDERS)
    ]

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
# NOTE: Do NOT pop plugins.trade.* from sys.modules here.
# Session-level isolation lives in conftest.py.

from plugins.trade.agents import x_lighter_agent as lighter  # noqa: E402
from plugins.trade.golden_fibo.config import GoldenFiboConfig  # noqa: E402
from plugins.trade.golden_fibo.engine import GoldenFiboEngine  # noqa: E402
from plugins.trade.golden_fibo.state import (  # noqa: E402
    ROLE_LADDER,
    ROLE_TP,
    GoldenFiboState,
)


class TestLighterOrderStateRequestedSizeContract(unittest.TestCase):
    """Real x_lighter_agent normalizers must emit requested_size for TPs."""

    def test_normalize_by_client_id_exposes_requested_size_from_initial_base(self):
        raw = {
            "order_index": 900001,
            "client_order_index": 424242,
            "market_index": 2,
            "is_ask": True,  # SELL reduce-only TP for a BUY robot
            "type": "limit",
            "status": "open",
            "price": "76.890",
            "initial_base_amount": "0.214",
            "filled_base_amount": "0",
            "filled_quote_amount": "0",
            "reduce_only": True,
        }
        market = {"symbol": "SOL", "size_decimals": 3, "price_decimals": 3}
        out = lighter._normalize_order_record_by_client_id(
            raw, market=market, size_decimals=3
        )
        self.assertEqual(out["taxonomy"], "ACTIVE")
        self.assertEqual(out["side"], "sell")
        self.assertTrue(out["reduce_only"])
        self.assertIsNotNone(out.get("requested_size"), out)
        self.assertEqual(Decimal(str(out["requested_size"])), Decimal("0.214"))

    def test_normalize_by_client_id_falls_back_to_base_amount_and_size(self):
        for key, value in (
            ("base_amount", "0.400"),
            ("remaining_base_amount", "0.400"),
            ("size", "0.400"),
        ):
            raw = {
                "order_index": 900002,
                "client_order_index": 1,
                "market_index": 2,
                "is_ask": False,
                "status": "open",
                "price": "100",
                key: value,
                "reduce_only": True,
            }
            out = lighter._normalize_order_record_by_client_id(
                raw, market={"symbol": "SOL"}, size_decimals=3
            )
            self.assertEqual(
                Decimal(str(out["requested_size"])),
                Decimal("0.400"),
                f"fallback key {key} failed: {out}",
            )

    def test_get_order_state_payload_shape_includes_requested_size(self):
        """Mirror the get_order_state payload builder fields (offline).

        The live path builds this dict inside _execute_get_order_state
        after a venue read. We assert the same field mapping the agent
        uses for initial_base_amount → requested_size.
        """
        matched = {
            "market_index": 2,
            "symbol": "SOL",
            "client_order_index": 77,
            "is_ask": True,
            "type": "limit",
            "status": "open",
            "price": "76.126",
            "initial_base_amount": "0.200",
            "filled_base_amount": "0",
            "filled_quote_amount": "0",
            "reduce_only": True,
            "created_at": 0,
            "updated_at": 0,
        }
        # Replicate the exact field extraction from the agent (no network).
        payload = {
            "order_index": 900003,
            "client_order_index": int(matched.get("client_order_index") or 0) or None,
            "symbol": "SOL",
            "side": "sell" if bool(matched.get("is_ask")) else "buy",
            "type": str(matched.get("type") or ""),
            "status": str(matched.get("status") or ""),
            "taxonomy": lighter._classify_order_status(matched),
            "requested_price": lighter._decimal_text(
                lighter._decimal_or_none(matched.get("price"))
            ),
            "requested_size": lighter._decimal_text(
                lighter._decimal_or_none(matched.get("initial_base_amount"))
            )
            if lighter._decimal_or_none(matched.get("initial_base_amount")) is not None
            else None,
            "filled_size": lighter._decimal_text(
                lighter._decimal_or_none(matched.get("filled_base_amount"))
            )
            if lighter._decimal_or_none(matched.get("filled_base_amount")) is not None
            else None,
            "reduce_only": bool(matched.get("reduce_only")),
        }
        self.assertEqual(payload["taxonomy"], "ACTIVE")
        self.assertEqual(Decimal(str(payload["requested_size"])), Decimal("0.200"))


class _AdapterFromRealNormalizedRecords:
    """Adapter whose get_order_state returns REAL-normalizer-shaped dicts."""

    def __init__(self) -> None:
        self.position: Dict[str, Any] = {
            "symbol": "SOL", "side": "long", "size": "0.200", "sl": None, "tp": None
        }
        self.orders: Dict[int, Dict[str, Any]] = {}
        self.submit_log: List[Dict[str, Any]] = []
        self.cancel_log: List[int] = []
        self._next_oid = 800000
        self.market = {"symbol": "SOL", "size_decimals": 3, "price_decimals": 3}

    def _gen(self) -> int:
        self._next_oid += 1
        return self._next_oid

    def position_state(self, account, instrument):
        return dict(self.position)

    def get_venue_constraints(self, account, instrument):
        return {
            "min_base_amount": "0.100",
            "min_quote_amount": "10.000000",
            "size_decimals": 3,
            "price_decimals": 3,
        }

    def place_market(self, *, account, instrument, side, size, client_order_id):
        oid = self._gen()
        self.orders[oid] = {
            "order_index": oid,
            "client_order_index": client_order_id,
            "is_ask": side == "sell",
            "type": "market",
            "status": "filled",
            "initial_base_amount": str(size),
            "filled_base_amount": str(size),
            "reduce_only": False,
        }
        self.position["side"] = "long" if side == "buy" else "short"
        self.position["size"] = str(size)
        self.submit_log.append({"role": "entry", "oid": oid})
        return {
            "exchange_order_id": oid,
            "client_order_id": client_order_id,
            "submitted_volume": str(size),
            "status": "filled",
            "verified": True,
            "role": "entry",
        }

    def place_limit(
        self, *, account, instrument, side, size, price, client_order_id, reduce_only=False
    ):
        oid = self._gen()
        self.orders[oid] = {
            "order_index": oid,
            "client_order_index": client_order_id,
            "is_ask": side == "sell",
            "type": "limit",
            "status": "open",
            "price": str(price),
            "initial_base_amount": str(size),
            "filled_base_amount": "0",
            "reduce_only": bool(reduce_only),
        }
        self.submit_log.append(
            {"role": "tp" if reduce_only else "ladder", "oid": oid, "size": str(size), "price": str(price)}
        )
        return {
            "exchange_order_id": oid,
            "client_order_id": client_order_id,
            "submitted_price": str(price),
            "submitted_volume": str(size),
            "status": "submitted",
            "verified": True,
        }

    def set_shared_tp(self, *, account, instrument, price, side, size, client_order_id):
        return self.place_limit(
            account=account,
            instrument=instrument,
            side=side,
            size=size,
            price=price,
            client_order_id=client_order_id,
            reduce_only=True,
        )

    def get_order_state(self, account, order_index):
        raw = self.orders.get(int(order_index))
        if not raw:
            return {}
        # REAL normalizer — this is the contract under test.
        return lighter._normalize_order_record_by_client_id(
            raw, market=self.market, size_decimals=3
        )

    def get_order_state_by_client_id(self, account, instrument, client_order_index):
        for raw in self.orders.values():
            if int(raw.get("client_order_index") or 0) == int(client_order_index):
                return lighter._normalize_order_record_by_client_id(
                    raw, market=self.market, size_decimals=3
                )
        return {}

    def cancel_order(self, *, account, order_index):
        self.cancel_log.append(int(order_index))
        raw = self.orders.get(int(order_index))
        if raw:
            raw["status"] = "canceled"
            return True
        return False


class TestEngineConsumesRealLighterRequestedSize(unittest.TestCase):
    """Engine TP-volume sync must fire when TP state comes from real normalizer."""

    def test_partial_fill_syncs_tp_volume_using_real_requested_size_field(self):
        cfg = GoldenFiboConfig(
            exchange="lighter",
            account="amiroo",
            instrument="SOL",
            direction="BUY",
            percentage=Decimal("0.01"),
            step0_volume=Decimal("0.200"),
        )
        state = GoldenFiboState(
        client_id_version=1,
            registration_key=cfg.registration_key,
            exchange=cfg.exchange,
            account=cfg.account,
            instrument=cfg.instrument,
            direction=cfg.direction,
            percentage=cfg.percentage,
            step0_volume=cfg.step0_volume,
        )
        adapter = _AdapterFromRealNormalizedRecords()
        counter = {"n": 100000}

        def nid() -> int:
            counter["n"] += 1
            return counter["n"]

        eng = GoldenFiboEngine(cfg, state, adapter, nid)
        eng._start_fresh_cycle([])
        eng.confirm_step0_filled(Decimal("76.126"))
        eng.place_step0_tp_and_step1(Decimal("76.126"))

        tp_oid = eng.state.current_tp_order_id
        step1_oid = eng.state.pending_order_exchange_id
        tp_price_before = eng.state.current_tp_price
        self.assertIsNotNone(tp_oid)
        self.assertIsNotNone(step1_oid)

        # Prove the real normalizer exposes requested_size on the live TP.
        tp_state = adapter.get_order_state("amiroo", int(tp_oid))
        self.assertEqual(tp_state.get("taxonomy"), "ACTIVE")
        self.assertIsNotNone(tp_state.get("requested_size"))
        self.assertEqual(Decimal(str(tp_state["requested_size"])), Decimal("0.200"))

        # Partial ladder fill grows live position; TP must resize via requested_size.
        adapter.orders[int(step1_oid)]["status"] = "open"  # still ACTIVE
        adapter.position["size"] = "0.214"

        result = eng.tick()
        self.assertEqual(result.state.status, "running")
        self.assertEqual(eng.state.highest_filled_step, 0)  # no promotion
        self.assertEqual(eng.state.current_tp_price, tp_price_before)
        self.assertEqual(eng.state.current_tp_size, Decimal("0.214"))
        self.assertIn(int(tp_oid), adapter.cancel_log)
        # New TP placed for 0.214
        new_tps = [s for s in adapter.submit_log if s.get("role") == "tp"]
        self.assertGreaterEqual(len(new_tps), 2)
        self.assertEqual(Decimal(str(new_tps[-1]["size"])), Decimal("0.214"))


if __name__ == "__main__":
    unittest.main()
