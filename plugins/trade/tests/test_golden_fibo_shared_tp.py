"""Regression tests proving GoldenFibo uses x_lighter_agent set_tp for the
shared TP and never constructs Lighter TP payloads itself.

Per the offline adapter-correction approval:
- BUY GoldenFibo calls set_tp with TP0 correctly.
- SELL GoldenFibo calls set_tp correctly.
- GoldenFibo does not construct Lighter TP payload itself.
- TP replacement uses the same x_lighter_agent path.
- existing /trade set_tp behavior is unchanged.
- no old SL/protection logic leaks into GoldenFibo engine.
"""

from __future__ import annotations

import sys
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


from plugins.trade.golden_fibo.config import GoldenFiboConfig
from plugins.trade.golden_fibo.state import GoldenFiboState
from plugins.trade.golden_fibo.engine import GoldenFiboEngine


class _RecordingAdapter:
    """Records which adapter methods are called with what args."""

    def __init__(self, direction: str):
        self.direction = direction
        self.calls = []
        # Live position is the accumulated GoldenFibo position.
        self.position = {
            "symbol": "SOL",
            "side": "long" if direction == "BUY" else "short",
            "size": "0.100",
            "sl": None,
            "tp": None,
        }

    def position_state(self, account, instrument):
        return dict(self.position)

    def set_shared_tp(self, *, account, instrument, price):
        self.calls.append(("set_shared_tp", {
            "account": account, "instrument": instrument, "price": str(price),
        }))
        return {
            "verified": True,
            "submitted_price": str(price),
            "exchange_order_id": 555000 + len(self.calls),
            "current_side": self.position["side"],
            "current_size": self.position["size"],
            "role": "tp",
        }

    # These must NOT be called for TP placement.
    def place_limit(self, **kwargs):
        if kwargs.get("reduce_only"):
            self.calls.append(("place_limit_reduce_only", kwargs))
        return {"exchange_order_id": 1, "submitted_price": str(kwargs.get("price")),
                "submitted_volume": str(kwargs.get("size")), "verified": True}

    def place_market(self, **kwargs):
        self.calls.append(("place_market", kwargs))
        return {"exchange_order_id": 1, "submitted_volume": str(kwargs.get("size")), "verified": True}

    def cancel_order(self, *, account, order_index):
        self.calls.append(("cancel_order", {"order_index": order_index}))
        return True


def _engine(direction: str, p0: str = "76.126"):
    cfg = GoldenFiboConfig(
        exchange="lighter", account="amiroo", instrument="SOL",
        direction=direction, percentage=Decimal("0.01"), step0_volume=Decimal("0.100"),
    )
    state = GoldenFiboState(
        registration_key=cfg.registration_key, exchange=cfg.exchange,
        account=cfg.account, instrument=cfg.instrument, direction=cfg.direction,
        percentage=cfg.percentage, step0_volume=cfg.step0_volume,
    )
    adapter = _RecordingAdapter(direction)
    counter = {"n": 100000}
    def nid():
        counter["n"] += 1
        return counter["n"]
    eng = GoldenFiboEngine(cfg, state, adapter, nid)
    return eng, adapter


class TestSharedTpUsesSetTp(unittest.TestCase):
    """GoldenFibo shared TP must go through adapter.set_shared_tp, never
    through a self-constructed reduce_only LIMIT."""

    def test_buy_tp0_calls_set_shared_tp_at_p0_times_1_01(self):
        eng, adapter = _engine("BUY", "76.126")
        eng.state.highest_filled_step = 0
        eng.state.fill_prices[0] = Decimal("76.126")
        result = eng._rotate_tp(Decimal("76.126"))
        self.assertIsNone(result)  # success
        # set_shared_tp called exactly once with TP0 = P0 * 1.01
        tp_calls = [c for c in adapter.calls if c[0] == "set_shared_tp"]
        self.assertEqual(len(tp_calls), 1)
        called_price = Decimal(tp_calls[0][1]["price"])
        self.assertEqual(called_price, Decimal("76.126") * Decimal("1.01"))
        # Never a reduce_only LIMIT constructed for TP
        bad = [c for c in adapter.calls if c[0] == "place_limit_reduce_only"]
        self.assertEqual(bad, [])

    def test_sell_tp0_calls_set_shared_tp_at_p0_times_0_99(self):
        eng, adapter = _engine("SELL", "76.126")
        eng.state.highest_filled_step = 0
        eng.state.fill_prices[0] = Decimal("76.126")
        result = eng._rotate_tp(Decimal("76.126"))
        self.assertIsNone(result)
        tp_calls = [c for c in adapter.calls if c[0] == "set_shared_tp"]
        self.assertEqual(len(tp_calls), 1)
        called_price = Decimal(tp_calls[0][1]["price"])
        self.assertEqual(called_price, Decimal("76.126") * Decimal("0.99"))

    def test_step1_tp_rotation_uses_p0_price(self):
        """After Step1 fills, TP = P0 (the prior step's price)."""
        eng, adapter = _engine("BUY", "76.126")
        eng.state.highest_filled_step = 1
        eng.state.fill_prices[0] = Decimal("76.126")
        eng.state.fill_prices[1] = Decimal("74.894")
        result = eng._rotate_tp(Decimal("74.894"))
        self.assertIsNone(result)
        tp_calls = [c for c in adapter.calls if c[0] == "set_shared_tp"]
        self.assertEqual(len(tp_calls), 1)
        # TP for step 1 = P0
        self.assertEqual(Decimal(tp_calls[0][1]["price"]), Decimal("76.126"))

    def test_engine_does_not_call_adapter_place_limit_for_tp(self):
        """The engine's TP path must not touch place_limit at all."""
        eng, adapter = _engine("BUY", "76.126")
        eng.state.highest_filled_step = 0
        eng.state.fill_prices[0] = Decimal("76.126")
        eng._rotate_tp(Decimal("76.126"))
        limit_calls = [c for c in adapter.calls if c[0] in ("place_limit", "place_limit_reduce_only")]
        self.assertEqual(limit_calls, [])

    def test_engine_does_not_reference_sl(self):
        """No SL/protection logic in the GoldenFibo engine."""
        import inspect
        from plugins.trade.golden_fibo import engine as eng_mod
        src = inspect.getsource(eng_mod)
        # No stop-loss placement in the strategy.
        self.assertNotIn("place_sl", src)
        self.assertNotIn("set_sl", src)
        self.assertNotIn("stop_loss", src.lower())


class TestSetTpPayloadNotConstructedByAdapter(unittest.TestCase):
    """The thin adapter must not build Lighter TP order bodies itself."""

    def test_adapter_set_shared_tp_only_calls_agent_set_tp(self):
        import inspect
        from plugins.trade.golden_fibo import lighter_adapter as la
        src = inspect.getsource(la.LighterGoldenFiboAdapter.set_shared_tp)
        # The adapter delegates to the generic agent operation.
        self.assertIn('"operation": "set_tp"', src)
        # It must NOT sign, quantize via base_amount, or set reduce_only itself.
        self.assertNotIn("create_tp_order", src)
        self.assertNotIn("base_amount", src)
        self.assertNotIn("ORDER_TYPE", src)
        self.assertNotIn("signer", src.lower())
        # The request payload sent to the agent contains ONLY operation /
        # account / symbol / price — no side, size, reduce_only, order_type,
        # time_in_force, or trigger fields. Strip the docstring before
        # checking so comments don't false-positive.
        body = src.split('resp = lighter_agent.execute({', 1)[1]
        body = body.split('})', 1)[0]
        for forbidden in ('reduce_only', 'side', 'size', 'order_type',
                          'time_in_force', 'trigger_price', 'base_amount',
                          'client_order_index'):
            self.assertNotIn(f'"{forbidden}"', body)


class TestTradeSetTpUnchanged(unittest.TestCase):
    """The /trade set_tp path must be untouched by the adapter change."""

    def test_agent_set_tp_operation_exists_and_unchanged(self):
        import inspect
        from plugins.trade.agents import x_lighter_agent as L
        # The dedicated TP/SL executor still exists.
        self.assertTrue(hasattr(L, "_execute_set_tpsl"))
        src = inspect.getsource(L._submit_tpsl_order)
        # It still uses the dedicated TP/SL signer methods.
        self.assertIn("create_tp_order", src)
        self.assertIn("create_sl_order", src)
        self.assertIn("reduce_only=True", src)


if __name__ == "__main__":
    unittest.main()
