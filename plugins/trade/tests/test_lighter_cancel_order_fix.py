"""Regression tests for the Lighter exact-order cancellation fix.

Three live bugs were found and fixed in x_lighter_agent._execute_cancel_order:

A. The signer client was constructed OUTSIDE the async coroutine, so its
   aiohttp session bound to no running event loop -> "no running event loop".
   Fix: build the signer INSIDE the coroutine (_do_cancel).

B. The SDK cancel_order requires market_index as well as order_index; the
   call passed only order_index -> "missing a required argument: 'market_index'".
   Fix: derive market_index from the target order record and pass both.

C. The success path called make_success(..., cancel_order={...}) with a field
   that does not exist on CanonicalResponse -> "make_success() got an
   unexpected keyword argument 'cancel_order'".
   Fix: return the valid order_state field instead.

These tests are offline and do not touch the venue.
"""

from __future__ import annotations

import inspect
import sys
import unittest
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
# NOTE: Do NOT pop plugins.trade.* from sys.modules here.
# Session-level isolation lives in conftest.py. Mid-suite pops
# create dual CanonicalResponse/TradeDesk identities and break
# later tests (INVALID_AGENT_RESPONSE / ImportError agents).


import plugins.trade.agents.x_lighter_agent as L


class TestCancelOrderFixRegression(unittest.TestCase):
    """The three live cancel_order bugs are regression-covered in source."""

    def _src(self):
        return inspect.getsource(L._execute_cancel_order)

    def test_A_signer_built_inside_coroutine(self):
        """A: signer construction must be inside the async coroutine."""
        src = self._src()
        # There must be an async coroutine that builds the signer.
        self.assertIn("async def _do_cancel", src)
        # The signer is built inside the coroutine, not in the sync wrapper.
        # i.e. `_build_signer_client` appears AFTER `async def _do_cancel`.
        idx_coro = src.find("async def _do_cancel")
        idx_signer = src.find("_build_signer_client(credentials)", idx_coro)
        self.assertGreater(idx_signer, idx_coro,
                          "signer must be built inside the coroutine")
        # The sync _submit wrapper must NOT build the signer directly.
        submit_idx = src.find("def _submit()")
        self.assertGreater(submit_idx, idx_coro)
        # In the sync wrapper region, there should be no direct
        # _build_signer_client call (it is inside the coroutine).
        wrapper_region = src[submit_idx:]
        self.assertNotIn("_build_signer_client(credentials)", wrapper_region,
                        "sync wrapper must not build the signer")

    def test_B_market_index_passed_with_order_index(self):
        """B: cancel passes market_index together with order_index."""
        src = self._src()
        # market_index is derived from the target order record.
        self.assertIn('target.get("market_index")', src)
        # The SDK call passes both market_index and order_index positionally.
        self.assertIn("signer.cancel_order(market_index, order_index)", src)
        # There is a guard rejecting when market_index cannot be determined.
        self.assertIn("MARKET_INDEX_UNAVAILABLE", src)

    def test_C_success_uses_order_state_not_cancel_order(self):
        """C: success path uses order_state, not a nonexistent cancel_order."""
        src = self._src()
        # The success return uses order_state.
        self.assertIn("order_state={", src)
        # It must NOT call make_success with a cancel_order kwarg.
        self.assertNotIn("cancel_order={", src,
                        "make_success must not receive a cancel_order kwarg")

    def test_make_success_has_no_cancel_order_field(self):
        """CanonicalResponse/make_success genuinely lack a cancel_order field."""
        from plugins.trade.canonical import make_success
        sig = inspect.signature(make_success)
        self.assertNotIn("cancel_order", sig.parameters,
                         "make_success must not accept cancel_order")
        self.assertIn("order_state", sig.parameters,
                      "make_success must accept order_state")

    def test_cancel_order_success_path_constructs_valid_response(self):
        """Smoke: a simulated successful cancel builds a valid CanonicalResponse."""
        from plugins.trade.canonical import make_success
        # Mirror the fixed success payload shape.
        resp = make_success(
            operation="cancel_order",
            exchange="lighter",
            account="amiroo",
            order_state={
                "order_index": 1125898831127248,
                "client_order_index": 1100002,
                "status": "canceled",
                "taxonomy": "CANCELED",
                "verified": True,
            },
        )
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["status"], "canceled")
        self.assertEqual(resp.order_state["taxonomy"], "CANCELED")
        self.assertTrue(resp.order_state["verified"])


if __name__ == "__main__":
    unittest.main()
