"""Regression tests for the generic GoldenFibo Lighter preflight.

Covers the approved requirements:
- 0.100 SOL configuration is rejected before Step0
- rejection explains the $10 Step1 minimum-notional problem
- calculated minimum volume is correctly quantized upward
- a sufficiently large volume passes
- BUY and SELL both work
- no MARKET order occurs when preflight fails
- TP set_tp path remains unchanged
- Step1 remains normal LIMIT
- /trade behavior remains unchanged
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


_EDITABLE_FINDER = "__editable___hermes_agent_0_20_0_finder"
_KNOWN_EDITABLE_FINDERS = (_EDITABLE_FINDER,)
if any(name in repr(h) for h in sys.path_hooks for name in _KNOWN_EDITABLE_FINDERS):
    sys.path_hooks[:] = [
        h for h in sys.path_hooks
        if not any(name in repr(h) for name in _KNOWN_EDITABLE_FINDERS)
    ]

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
for _cached in [k for k in list(sys.modules)
              if k.startswith("plugins.trade")
              and not k.startswith("plugins.trade.tests")]:
    sys.modules.pop(_cached, None)


from plugins.trade.golden_fibo.preflight import (
    golden_fibo_lighter_preflight,
    compute_safe_min_step0_volume,
)
from plugins.trade.fibo_service import PersistentFiboService


# Real SOL-like venue rules captured from Lighter mainnet metadata.
SOL_MARK = Decimal("76.259")
SOL_MIN_BASE = Decimal("0.100")
SOL_MIN_QUOTE = Decimal("10.000000")
SOL_SIZE_DEC = 3
SOL_PRICE_DEC = 3


def _pf(direction, v0, mark=SOL_MARK, min_quote=SOL_MIN_QUOTE):
    return golden_fibo_lighter_preflight(
        direction=direction,
        percentage=Decimal("0.01"),
        step0_volume=Decimal(str(v0)),
        estimated_p0=Decimal(str(mark)),
        min_base_amount=SOL_MIN_BASE,
        min_quote_amount=min_quote,
        size_decimals=SOL_SIZE_DEC,
        price_decimals=SOL_PRICE_DEC,
    )


class TestPreflightMath(unittest.TestCase):
    def test_buy_0_100_rejected_for_step1_min_quote(self):
        """0.100 SOL BUY is rejected before Step0: Step1 notional < $10."""
        r = _pf("BUY", "0.100")
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "STEP1_BELOW_MIN_QUOTE")
        self.assertEqual(r.failing_step, 1)
        # The rejection explains the $10 Step1 minimum-notional problem.
        self.assertIn("Step1", r.detail)
        self.assertIn("10", r.detail)
        # estimated P1 below P0 for BUY.
        self.assertLess(r.estimated_p1, r.estimated_p0)

    def test_sell_0_100_rejected_for_step1_min_quote(self):
        """0.100 SOL SELL is rejected before Step0: Step1 notional < $10."""
        r = _pf("SELL", "0.100")
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "STEP1_BELOW_MIN_QUOTE")
        self.assertEqual(r.failing_step, 1)
        # estimated P1 above P0 for SELL.
        self.assertGreater(r.estimated_p1, r.estimated_p0)

    def test_safe_minimum_quantized_upward_buy(self):
        """The reported safe minimum rounds UP to the size increment and
        actually satisfies the min-quote at the estimated Step1 price."""
        r = _pf("BUY", "0.100")
        safe = r.safe_min_step0_volume
        self.assertIsNotNone(safe)
        # Notional at safe volume meets min_quote.
        notional = safe * r.estimated_p1.quantize(Decimal("0.001"))
        self.assertGreaterEqual(notional, SOL_MIN_QUOTE)
        # Safe min is a clean multiple of the size increment (0.001).
        self.assertEqual(safe, safe.quantize(Decimal("0.001")))

    def test_sufficiently_large_volume_passes_buy(self):
        """A volume at/above the safe minimum passes Step1 min-quote (BUY).
        Note: at 1% the BUY ladder still goes non-positive by Step8, so this
        config is rejected for a different reason — Step1 min-quote is NOT
        the cause."""
        r = _pf("BUY", "0.136")
        self.assertNotEqual(r.error, "STEP1_BELOW_MIN_QUOTE")

    def test_sufficiently_large_volume_passes_sell(self):
        r = _pf("SELL", "0.132")
        self.assertTrue(r.ok)

    def test_compute_safe_min_matches_preflight(self):
        safe = compute_safe_min_step0_volume(
            direction="BUY", percentage=Decimal("0.01"),
            estimated_p0=SOL_MARK, min_quote_amount=SOL_MIN_QUOTE,
            size_decimals=SOL_SIZE_DEC, price_decimals=SOL_PRICE_DEC,
        )
        self.assertIsNotNone(safe)
        r = _pf("BUY", str(safe))
        # The safe minimum itself passes Step1.
        self.assertNotEqual(r.error, "STEP1_BELOW_MIN_QUOTE")

    def test_buy_valid_step1_accepted_despite_hypothetical_step8(self):
        """ONE-STEP-AHEAD: a BUY config with a valid Step1 is ACCEPTED even
        though a hypothetical Step8 would go non-positive. Deeper steps are
        validated at placement time, not at START. (0.136 SOL, 1%, valid
        Step1 notional >= $10.)"""
        r = golden_fibo_lighter_preflight(
            direction="BUY", percentage=Decimal("0.01"),
            step0_volume=Decimal("0.136"), estimated_p0=Decimal("76.259"),
            min_base_amount=Decimal("0.100"), min_quote_amount=Decimal("10"),
            size_decimals=3, price_decimals=3,
        )
        self.assertTrue(r.ok, f"expected ok, got {r.error}: {r.detail}")

    def test_buy_full_positive_ladder_through_step20_accepted(self):
        """A BUY ladder whose every Step1..20 price is positive is accepted.
        Uses a percentage small enough to keep the full ladder positive."""
        # ~0.002% keeps all 20 steps positive at mark 76.259 (verified by
        # compute_max_positive_ladder_percentage below).
        tiny_pct = Decimal("0.00002")
        r = golden_fibo_lighter_preflight(
            direction="BUY", percentage=tiny_pct,
            step0_volume=Decimal("0.136"), estimated_p0=Decimal("76.259"),
            min_base_amount=Decimal("0.100"), min_quote_amount=Decimal("10"),
            size_decimals=3, price_decimals=3,
        )
        self.assertTrue(r.ok, f"expected ok, got {r.error}: {r.detail}")

    def test_max_positive_percentage_helper_buy(self):
        """compute_max_positive_ladder_percentage returns a small positive
        bound for BUY on SOL, and a config at/below it stays positive."""
        from plugins.trade.golden_fibo.preflight import compute_max_positive_ladder_percentage
        max_pct = compute_max_positive_ladder_percentage(
            direction="BUY", estimated_p0=Decimal("76.259")
        )
        self.assertGreater(max_pct, 0)
        # 1% exceeds the bound (Step8 non-positive).
        self.assertLess(max_pct, Decimal("0.01"))
        # A config at half the bound stays positive through Step20.
        r = golden_fibo_lighter_preflight(
            direction="BUY", percentage=max_pct / 2,
            step0_volume=Decimal("0.136"), estimated_p0=Decimal("76.259"),
            min_base_amount=Decimal("0.100"), min_quote_amount=Decimal("10"),
            size_decimals=3, price_decimals=3,
        )
        self.assertTrue(r.ok, f"expected ok at half max_pct, got {r.error}")

    def test_sell_full_ladder_remains_valid(self):
        """SELL full ladder stays positive and valid at a normal percentage."""
        r = golden_fibo_lighter_preflight(
            direction="SELL", percentage=Decimal("0.01"),
            step0_volume=Decimal("0.132"), estimated_p0=Decimal("76.259"),
            min_base_amount=Decimal("0.100"), min_quote_amount=Decimal("10"),
            size_decimals=3, price_decimals=3,
        )
        self.assertTrue(r.ok, f"expected ok, got {r.error}: {r.detail}")

    def test_sell_step1_implies_later_steps(self):
        """For SELL, prices rise and volumes double, so notional increases
        monotonically. If Step1 passes min-quote, all later steps pass too."""
        r = _pf("SELL", "0.132")
        self.assertTrue(r.ok)
        # Verify every reported step meets min quote.
        for rep in r.step_reports:
            self.assertTrue(rep["meets_min_quote"], f"step {rep['step']} below min quote")

    def test_step0_below_min_base_rejected(self):
        r = _pf("BUY", "0.050")
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "STEP0_BELOW_MIN_BASE")


# ---------------------------------------------------------------------------
# Service-level: START is rejected before any exchange mutation
# ---------------------------------------------------------------------------
class _PreflightAdapter:
    """Adapter stub exposing SOL-like venue constraints and a market price,
    and tracking whether any mutation was attempted."""

    def __init__(self, direction="BUY"):
        self.mutations = []
        self.position = {"symbol": "SOL", "side": None, "size": "0", "sl": None, "tp": None}
        self._dir = direction

    def resolve_instrument(self, account, instrument):
        return {"symbol": instrument, "market_id": 2, "size_decimals": 3,
                "price_decimals": 3, "min_base_amount": "0.100", "minimum_size": "0.100"}

    def get_venue_constraints(self, account, instrument):
        return {"symbol": instrument, "market_id": 2, "size_decimals": 3,
                "price_decimals": 3, "min_base_amount": "0.100",
                "min_quote_amount": "10.000000", "tick_size": ""}

    def market_price(self, account, instrument):
        return {"mark_price": "76.259", "last_external_price": "76.261"}

    def position_state(self, account, instrument):
        return dict(self.position)

    def place_market(self, **kw):
        self.mutations.append(("place_market", kw))
        return {"client_order_id": kw.get("client_order_id"), "exchange_order_id": None,
                "submitted_price": None, "submitted_volume": str(kw.get("size")),
                "status": "filled", "verified": True, "role": "entry"}

    def place_limit(self, **kw):
        self.mutations.append(("place_limit", kw))
        return {"client_order_id": kw.get("client_order_id"), "exchange_order_id": 1,
                "submitted_price": str(kw.get("price")), "submitted_volume": str(kw.get("size")),
                "status": "submitted", "verified": True}

    def set_shared_tp(self, **kw):
        self.mutations.append(("set_shared_tp", kw))
        return {"verified": True, "submitted_price": str(kw.get("price")),
                "exchange_order_id": 2, "current_side": None, "current_size": "0", "role": "tp"}

    def get_order_state(self, account, order_index):
        return {}

    def get_order_state_by_client_id(self, account, instrument, client_order_index):
        return {}

    def cancel_order(self, *, account, order_index):
        return True


class TestPreflightBlocksStart(unittest.TestCase):
    def _svc(self, tmp):
        return PersistentFiboService(
            state_path=Path(tmp) / "service_state.json",
            ledger_path=Path(tmp) / "service_ledger.jsonl",
            event_log_path=Path(tmp) / "service-events.log",
            start_thread=False,
        )

    def test_start_rejected_for_0_100_sol_before_any_market_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            key = "lighter/amiroo/SOL/BUY"
            adapter = _PreflightAdapter("BUY")
            svc._adapters[key] = adapter
            resp = svc.execute_command({
                "op": "start", "exchange": "lighter", "account": "amiroo",
                "instrument": "SOL", "direction": "BUY",
                "percentage": "0.01", "step0_volume": "0.100",
            })
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"], "STEP1_BELOW_MIN_QUOTE")
            self.assertIn("safe_min_step0_volume", resp)
            # NO market order / TP / limit mutation occurred.
            self.assertEqual(adapter.mutations, [])
            # No registration was persisted.
            self.assertNotIn(key, svc._states)

    def test_start_accepted_for_large_enough_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            key = "lighter/amiroo/SOL/BUY"
            adapter = _PreflightAdapter("BUY")
            svc._adapters[key] = adapter
            # Tiny percentage keeps the full BUY ladder positive through Step20.
            resp = svc.execute_command({
                "op": "start", "exchange": "lighter", "account": "amiroo",
                "instrument": "SOL", "direction": "BUY",
                "percentage": "0.00002", "step0_volume": "0.136",
            })
            self.assertTrue(resp["ok"], resp)
            self.assertIn(key, svc._states)

    def test_start_accepted_when_only_step1_valid(self):
        """ONE-STEP-AHEAD: START is accepted when the immediate initial
        sequence (Step0/TP0/Step1) is valid, even if a hypothetical future
        step would be invalid. No market order is sent by the preflight
        itself (START only registers; the engine ticks separately)."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            key = "lighter/amiroo/SOL/BUY"
            adapter = _PreflightAdapter("BUY")
            svc._adapters[key] = adapter
            resp = svc.execute_command({
                "op": "start", "exchange": "lighter", "account": "amiroo",
                "instrument": "SOL", "direction": "BUY",
                "percentage": "0.01", "step0_volume": "0.136",
            })
            self.assertTrue(resp["ok"], resp)
            self.assertIn(key, svc._states)
            # START itself sent no orders (registration only).
            self.assertEqual(adapter.mutations, [])

    def test_start_rejected_sell_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            key = "lighter/amiroo/SOL/SELL"
            adapter = _PreflightAdapter("SELL")
            svc._adapters[key] = adapter
            resp = svc.execute_command({
                "op": "start", "exchange": "lighter", "account": "amiroo",
                "instrument": "SOL", "direction": "SELL",
                "percentage": "0.01", "step0_volume": "0.100",
            })
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"], "STEP1_BELOW_MIN_QUOTE")
            self.assertEqual(adapter.mutations, [])


class TestUnchangedBehavior(unittest.TestCase):
    def test_tp_path_unchanged(self):
        """set_tp remains the dedicated create_tp_order primitive."""
        import inspect
        from plugins.trade.agents import x_lighter_agent as L
        src = inspect.getsource(L._submit_tpsl_order)
        self.assertIn("create_tp_order", src)
        self.assertIn("reduce_only=True", src)

    def test_step1_remains_limit_via_new_order(self):
        """The ladder placement still uses the normal new_order LIMIT path."""
        import inspect
        from plugins.trade.golden_fibo import engine as eng_mod
        src = inspect.getsource(eng_mod.GoldenFiboEngine._place_next_ladder)
        self.assertIn("place_limit", src)
        self.assertIn("reduce_only=False", src)

# ---------------------------------------------------------------------------
# Placement-time (one-step-ahead) next-ladder validation
# ---------------------------------------------------------------------------
class _PlacementAdapter:
    """Adapter stub for engine placement-time validation tests."""

    def __init__(self, *, min_quote="10.000000", size_dec=3, price_dec=3, min_base="0.100"):
        self._c = {"min_base_amount": min_base, "min_quote_amount": min_quote,
                   "size_decimals": size_dec, "price_decimals": price_dec}
        self.limit_calls = []
        self.position = {"symbol": "SOL", "side": "long", "size": "0.136", "sl": None, "tp": None}

    def get_venue_constraints(self, account, instrument):
        return dict(self._c)

    def position_state(self, account, instrument):
        return dict(self.position)

    def place_limit(self, **kw):
        self.limit_calls.append(kw)
        return {"client_order_id": kw.get("client_order_id"), "exchange_order_id": 1,
                "submitted_price": str(kw.get("price")), "submitted_volume": str(kw.get("size")),
                "status": "submitted", "verified": True}

    def cancel_order(self, *, account, order_index):
        return True


class TestPlacementTimeValidation(unittest.TestCase):
    """The engine validates EACH next ladder order at placement time."""

    def _engine(self, adapter):
        from plugins.trade.golden_fibo.config import GoldenFiboConfig
        from plugins.trade.golden_fibo.state import GoldenFiboState
        from plugins.trade.golden_fibo.engine import GoldenFiboEngine
        cfg = GoldenFiboConfig(
            exchange="lighter", account="amiroo", instrument="SOL",
            direction="BUY", percentage=Decimal("0.01"), step0_volume=Decimal("0.136"),
        )
        state = GoldenFiboState(
            registration_key=cfg.registration_key, exchange=cfg.exchange,
            account=cfg.account, instrument=cfg.instrument, direction=cfg.direction,
            percentage=cfg.percentage, step0_volume=cfg.step0_volume,
        )
        state.highest_filled_step = 0
        state.fill_prices[0] = Decimal("76.259")
        state.next_step = 1
        counter = {"n": 100000}
        def nid():
            counter["n"] += 1
            return counter["n"]
        return GoldenFiboEngine(cfg, state, adapter, nid)

    def test_valid_step1_placed(self):
        adapter = _PlacementAdapter()
        eng = self._engine(adapter)
        result = eng._place_next_ladder()
        self.assertIsNone(result)  # success
        self.assertEqual(len(adapter.limit_calls), 1)

    def test_invalid_step1_below_min_quote_not_placed(self):
        # Step0 volume tiny -> Step1 notional below min-quote -> not placed.
        from plugins.trade.golden_fibo.config import GoldenFiboConfig
        from plugins.trade.golden_fibo.state import GoldenFiboState
        from plugins.trade.golden_fibo.engine import GoldenFiboEngine
        adapter = _PlacementAdapter()
        cfg = GoldenFiboConfig(
            exchange="lighter", account="amiroo", instrument="SOL",
            direction="BUY", percentage=Decimal("0.01"), step0_volume=Decimal("0.100"),
        )
        state = GoldenFiboState(
            registration_key=cfg.registration_key, exchange=cfg.exchange,
            account=cfg.account, instrument=cfg.instrument, direction=cfg.direction,
            percentage=cfg.percentage, step0_volume=cfg.step0_volume,
        )
        state.highest_filled_step = 0
        state.fill_prices[0] = Decimal("76.259")
        state.next_step = 1
        counter = {"n": 100000}
        def nid():
            counter["n"] += 1
            return counter["n"]
        eng = GoldenFiboEngine(cfg, state, adapter, nid)
        result = eng._place_next_ladder()
        # Frozen (NEEDS_RECOVERY), and the LIMIT was NOT placed.
        self.assertIsNotNone(result)
        self.assertEqual(result.state.status, "needs_recovery")
        self.assertIn("failed venue validation", result.state.freeze_reason or "")
        self.assertEqual(len(adapter.limit_calls), 0)

    def test_nonpositive_step_price_not_placed(self):
        # Force a config where the next ladder price computes non-positive:
        # set fill_prices so P(k) - TP(k) is a large negative swing.
        adapter = _PlacementAdapter()
        eng = self._engine(adapter)
        # Push P0 very high relative to TP0 by using a huge percentage effect:
        # set next_step=2 with P1 very low and P0 high so P2 goes negative.
        eng.state.highest_filled_step = 1
        eng.state.fill_prices[0] = Decimal("76.259")
        eng.state.fill_prices[1] = Decimal("1.0")
        eng.state.next_step = 2
        result = eng._place_next_ladder()
        # P2 = P1 + 1.618*(P1 - TP1) where TP1 = P0 = 76.259 ->
        # P2 = 1.0 + 1.618*(1.0 - 76.259) = very negative.
        self.assertIsNotNone(result)
        self.assertEqual(result.state.status, "needs_recovery")
        self.assertEqual(len(adapter.limit_calls), 0)


if __name__ == "__main__":
    unittest.main()

