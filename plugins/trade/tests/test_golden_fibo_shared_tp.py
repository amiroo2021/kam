"""Regression tests for the GoldenFibo shared TP as a resting reduce-only
GTC LIMIT order (replacing the unreliable IOC take-profit trigger).

Locked behavior:
- The shared TP is an ordinary resting reduce-only GTC LIMIT placed via the
  generic x_lighter_agent new_order machinery (order_type="limit",
  reduce_only=True), with the closing side + accumulated size supplied by
  the engine. It is NOT an IOC take-profit trigger (which Lighter cancels
  for slippage after the target is reached — proven live).
- TP notional must satisfy the venue min_quote (no silent resize).
- Replacing the shared TP cancels the old resting TP first.
- The /trade set_tp path is untouched.
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
    """Records adapter calls; provides venue constraints for notional checks."""

    def __init__(self, direction: str, *, min_quote: str = "10.000000"):
        self.direction = direction
        self.calls = []
        self._min_quote = min_quote
        self.position = {
            "symbol": "SOL",
            "side": "long" if direction == "BUY" else "short",
            "size": "0.200",
            "sl": None,
            "tp": None,
        }

    def position_state(self, account, instrument):
        return dict(self.position)

    def get_venue_constraints(self, account, instrument):
        return {
            "min_base_amount": "0.100",
            "min_quote_amount": self._min_quote,
            "size_decimals": 3,
            "price_decimals": 3,
        }

    def set_shared_tp(self, *, account, instrument, price, side, size, client_order_id):
        self.calls.append(("set_shared_tp", {
            "account": account, "instrument": instrument, "price": str(price),
            "side": side, "size": str(size), "client_order_id": client_order_id,
        }))
        return {
            "exchange_order_id": 555000 + len(self.calls),
            "client_order_id": client_order_id,
            "submitted_price": str(price),
            "submitted_volume": str(size),
            "status": "submitted",
            "verified": True,
            "role": "tp",
        }

    def place_limit(self, **kwargs):
        self.calls.append(("place_limit", kwargs))
        return {"exchange_order_id": 1, "submitted_price": str(kwargs.get("price")),
                "submitted_volume": str(kwargs.get("size")), "verified": True}

    def place_market(self, **kwargs):
        self.calls.append(("place_market", kwargs))
        return {"exchange_order_id": 1, "submitted_volume": str(kwargs.get("size")), "verified": True}

    def cancel_order(self, *, account, order_index):
        self.calls.append(("cancel_order", {"order_index": order_index}))
        return True


def _engine(direction: str, p0: str = "76.126", step0: str = "0.200", min_quote: str = "10.000000"):
    cfg = GoldenFiboConfig(
        exchange="lighter", account="amiroo", instrument="SOL",
        direction=direction, percentage=Decimal("0.01"), step0_volume=Decimal(step0),
    )
    state = GoldenFiboState(
        registration_key=cfg.registration_key, exchange=cfg.exchange,
        account=cfg.account, instrument=cfg.instrument, direction=cfg.direction,
        percentage=cfg.percentage, step0_volume=cfg.step0_volume,
    )
    adapter = _RecordingAdapter(direction, min_quote=min_quote)
    counter = {"n": 100000}
    def nid():
        counter["n"] += 1
        return counter["n"]
    eng = GoldenFiboEngine(cfg, state, adapter, nid)
    return eng, adapter


class TestSharedTpRestingLimit(unittest.TestCase):
    """The shared TP is a resting reduce-only GTC LIMIT via new_order."""

    def test_buy_tp0_resting_limit_sell_reduce_only(self):
        eng, adapter = _engine("BUY", "76.126")
        eng.state.highest_filled_step = 0
        eng.state.fill_prices[0] = Decimal("76.126")
        result = eng._rotate_tp(Decimal("76.126"))
        self.assertIsNone(result)
        tp_calls = [c for c in adapter.calls if c[0] == "set_shared_tp"]
        self.assertEqual(len(tp_calls), 1)
        args = tp_calls[0][1]
        # TP0 = P0 * 1.01
        self.assertEqual(Decimal(args["price"]), Decimal("76.126") * Decimal("1.01"))
        # closing side = sell (opposite of BUY)
        self.assertEqual(args["side"], "sell")
        # accumulated size = step0 = 0.200
        self.assertEqual(Decimal(args["size"]), Decimal("0.200"))
        # deterministic client id supplied
        self.assertIsNotNone(args["client_order_id"])

    def test_sell_tp0_resting_limit_buy_reduce_only(self):
        eng, adapter = _engine("SELL", "76.126")
        eng.state.highest_filled_step = 0
        eng.state.fill_prices[0] = Decimal("76.126")
        result = eng._rotate_tp(Decimal("76.126"))
        self.assertIsNone(result)
        tp_calls = [c for c in adapter.calls if c[0] == "set_shared_tp"]
        self.assertEqual(len(tp_calls), 1)
        args = tp_calls[0][1]
        self.assertEqual(Decimal(args["price"]), Decimal("76.126") * Decimal("0.99"))
        self.assertEqual(args["side"], "buy")  # opposite of SELL
        self.assertEqual(Decimal(args["size"]), Decimal("0.200"))

    def test_step1_tp_rotation_uses_p0_and_accumulated_size(self):
        """After Step1 fills, TP = P0 and size = cumulative 0.400."""
        eng, adapter = _engine("BUY", "76.126")
        eng.state.highest_filled_step = 1
        eng.state.fill_prices[0] = Decimal("76.126")
        eng.state.fill_prices[1] = Decimal("74.894")
        result = eng._rotate_tp(Decimal("74.894"))
        self.assertIsNone(result)
        tp_calls = [c for c in adapter.calls if c[0] == "set_shared_tp"]
        self.assertEqual(len(tp_calls), 1)
        args = tp_calls[0][1]
        self.assertEqual(Decimal(args["price"]), Decimal("76.126"))  # P0
        self.assertEqual(Decimal(args["size"]), Decimal("0.400"))  # cumulative

    def test_tp_replacement_cancels_old_tp_first(self):
        """Replacing the shared TP cancels the old resting TP before placing."""
        eng, adapter = _engine("BUY", "76.126")
        eng.state.highest_filled_step = 0
        eng.state.fill_prices[0] = Decimal("76.126")
        eng.state.current_tp_order_id = 999111  # existing TP
        result = eng._rotate_tp(Decimal("76.126"))
        self.assertIsNone(result)
        cancels = [c for c in adapter.calls if c[0] == "cancel_order"]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0][1]["order_index"], 999111)
        # exactly one new TP placed
        tp_calls = [c for c in adapter.calls if c[0] == "set_shared_tp"]
        self.assertEqual(len(tp_calls), 1)

    def test_tp_notional_below_min_quote_freezes_no_placement(self):
        """TP notional < min_quote -> NEEDS_RECOVERY, no TP placed, no resize."""
        eng, adapter = _engine("BUY", "76.126", step0="0.100", min_quote="10.000000")
        eng.state.highest_filled_step = 0
        eng.state.fill_prices[0] = Decimal("76.126")
        result = eng._rotate_tp(Decimal("76.126"))
        # 0.100 * 76.887 = $7.69 < $10 -> freeze
        self.assertIsNotNone(result)
        self.assertEqual(result.state.status, "needs_recovery")
        self.assertIn("below venue minimum", result.state.freeze_reason or "")
        tp_calls = [c for c in adapter.calls if c[0] == "set_shared_tp"]
        self.assertEqual(tp_calls, [])  # never placed

    def test_adapter_delegates_to_new_order_reduce_only_limit(self):
        """The adapter's set_shared_tp calls new_order with reduce_only=True,
        order_type=limit — NOT the set_tp operation, NOT a custom signer."""
        import inspect
        from plugins.trade.golden_fibo import lighter_adapter as la
        src = inspect.getsource(la.LighterGoldenFiboAdapter.set_shared_tp)
        self.assertIn('"operation": "new_order"', src)
        self.assertIn('"order_type": "limit"', src)
        self.assertIn('"reduce_only": True', src)
        # It must NOT use the IOC take-profit trigger path or a custom signer.
        self.assertNotIn('"operation": "set_tp"', src)
        self.assertNotIn("create_tp_order", src)
        self.assertNotIn("signer", src.lower())
        self.assertNotIn("base_amount", src)


class TestTradeSetTpUnchanged(unittest.TestCase):
    """The /trade set_tp path must be untouched by the adapter change."""

    def test_agent_set_tp_operation_exists_and_unchanged(self):
        import inspect
        from plugins.trade.agents import x_lighter_agent as L
        self.assertTrue(hasattr(L, "_execute_set_tpsl"))
        src = inspect.getsource(L._submit_tpsl_order)
        self.assertIn("create_tp_order", src)
        self.assertIn("create_sl_order", src)
        self.assertIn("reduce_only=True", src)


if __name__ == "__main__":
    unittest.main()
