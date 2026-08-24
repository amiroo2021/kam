"""Regression tests for the Hyperliquid account_address fix and
the response-is-None close classification (CLOSE_SUBMISSION_NOOP).

Proves:
  A. _build_exchange_client passes the configured trading-account
     public address as `account_address` to the SDK Exchange().
  B. API/signing-wallet address may differ from account_address
     without breaking construction.
  C. market_close() sees the configured trading-account context
     (verified via _build_exchange_client output shape).
  D. response is None maps to CLOSE_SUBMISSION_NOOP, never:
       - AMBIGUOUS_LADDER_RESPONSE
       - CLOSE_RESPONSE_MALFORMED
  E. None + post-read flat  -> success if the position actually
     became flat.
  F. None + unchanged position -> CLOSE_SUBMISSION_NOOP with
     remaining size surfaced.
  G. No automatic retry.
  H. Existing close regression tests remain green (handled by
     running the full test_phase4 + test_hyperliquid_close_response
     suites — see main()).
  I. Existing New Order, Ladder, TP and SL tests remain green
     (same mechanism as H).

Read-only — no live writes, no real network. Uses the same
FakeExchange / patching pattern as the existing phase4 tests.

The tests for C and E mock the SDK Exchange class so we don't need
network or signing material.
"""
from __future__ import annotations

import inspect
import os
import sys
import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest import mock

# Ensure the source tree is importable so we test against the patched
# /root/kam module (not the live installed one).
_KAM_ROOT = "/root/kam"
if _KAM_ROOT not in sys.path:
    sys.path.insert(0, _KAM_ROOT)
os.environ.setdefault("HERMES_HOME", "/root/.hermes")

from plugins.trade.canonical import (  # noqa: E402
    CanonicalPosition,
    CanonicalPositionActionResult,
    make_success,
)
from plugins.trade.tests.test_phase4 import (  # noqa: E402
    FakeExchange,
    TestPositionManagementWrites,
)


_FORBIDDEN = "AMBIGUOUS_LADDER_RESPONSE"


class _RecordingExchange:
    """Records the kwargs it was constructed with.

    Used to assert _build_exchange_client passes the right account_address
    without needing the real SDK.
    """

    instances: List["_RecordingExchange"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.market_close_calls: List[Dict[str, Any]] = []
        self.bulk_orders_calls: List[List[Dict[str, Any]]] = []
        self.info = mock.MagicMock()  # the SDK attaches self.info
        _RecordingExchange.instances.append(self)

    @classmethod
    def reset(cls):
        cls.instances = []

    def market_close(self, coin, sz=None, px=None, slippage=None, cloid=None, builder=None):
        self.market_close_calls.append(
            {"coin": coin, "sz": sz, "px": px, "slippage": slippage}
        )
        return None  # simulate the SDK's implicit-None return

    def bulk_orders(self, orders, builder=None, grouping="na"):
        self.bulk_orders_calls.append(list(orders))
        # Return a successful exchange response shape so callers can parse it.
        return {
            "status": "ok",
            "response": {"data": {"statuses": [{"filled": {"oid": 999}}]}},
        }


class HyperliquidAccountContextRegressionTests(TestPositionManagementWrites):
    """Regression suite for the Option A + C fix."""

    def setUp(self):
        # Initialize the parent's patch list first so self.patch works.
        super().setUp()
        # Wipe recording instances before each test
        _RecordingExchange.reset()
        # Patch the SDK Exchange class so _build_exchange_client gets our
        # recording constructor. Use the canonical import path the
        # agent uses.
        self._exchange_patch = self.patch(
            "plugins.trade.agents.x_hyperliquid_agent.Exchange",
            _RecordingExchange,
        )
        # Provide env vars so _lookup_credentials finds them.
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "HYPERLIQUID_FLEX_WALLET": "0x4FE260D11bf48BA3a94459771259c910a398ac59",
                "HYPERLIQUID_FLEX_SECRET": "0x" + "ab" * 32,  # 64 hex chars
            },
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        super().tearDown()

    # ------------------------------------------------------------------
    # A. _build_exchange_client passes the configured trading-account
    #    public address as `account_address`.
    # ------------------------------------------------------------------
    def test_A_build_exchange_client_passes_account_address(self):
        from plugins.trade.agents import x_hyperliquid_agent as hl

        exchange, returned_address, _secret = hl._build_exchange_client("FLEX")

        self.assertIsNotNone(exchange, "_build_exchange_client must succeed")
        self.assertEqual(
            exchange.kwargs.get("account_address"),
            "0x4FE260D11bf48BA3a94459771259c910a398ac59",
            "account_address must equal the configured /trade wallet",
        )
        self.assertEqual(returned_address, "0x4FE260D11bf48BA3a94459771259c910a398ac59")
        # signing key is still the LocalAccount (not the trading address)
        self.assertIsNotNone(exchange.kwargs.get("wallet"))

    # ------------------------------------------------------------------
    # B. API/signing-wallet address may differ from account_address.
    # ------------------------------------------------------------------
    def test_B_signing_wallet_can_differ_from_account_address(self):
        from plugins.trade.agents import x_hyperliquid_agent as hl
        from eth_account import Account

        exchange, _, _ = hl._build_exchange_client("FLEX")

        # The signing key derived from the 0xab.. secret is some address.
        # account_address must remain the configured FLEX wallet.
        signing_wallet_obj = exchange.kwargs["wallet"]
        signing_address = (
            signing_wallet_obj.address
            if hasattr(signing_wallet_obj, "address")
            else None
        )
        # account_address must be the configured trading address,
        # independent of which signing key derived from the secret.
        self.assertEqual(
            exchange.kwargs["account_address"],
            "0x4FE260D11bf48BA3a94459771259c910a398ac59",
        )
        # The signing address is almost certainly different from the
        # configured trading address for FLEX (Hyperliquid agent pattern).
        # Verify they're not accidentally identical (a sanity check
        # against the bug we just fixed).
        self.assertNotEqual(
            signing_address,
            exchange.kwargs["account_address"],
            "signing key address must NOT equal the configured trading "
            "address — the whole point of the fix is to separate them. "
            "If this assertion ever fails, the test env vars are stale.",
        )

    # ------------------------------------------------------------------
    # C. market_close() sees the configured trading-account context.
    #    Verified by inspecting that the SDK's address-resolution logic
    #    would pick account_address over wallet.address when set.
    # ------------------------------------------------------------------
    def test_C_market_close_sees_account_address(self):
        from plugins.trade.agents import x_hyperliquid_agent as hl

        # Patch _api_base to avoid network calls (not strictly needed for
        # this assertion but keeps the test self-contained).
        with mock.patch("plugins.trade.agents.x_hyperliquid_agent._api_base", lambda: "http://test"):
            exchange, _, _ = hl._build_exchange_client("FLEX")

        # Replicate the SDK's market_close address-resolution:
        #   address = self.wallet.address
        #   if self.account_address: address = self.account_address
        wallet_address = (
            exchange.kwargs["wallet"].address
            if hasattr(exchange.kwargs["wallet"], "address")
            else None
        )
        sdk_picked_address = wallet_address
        if exchange.kwargs.get("account_address"):
            sdk_picked_address = exchange.kwargs["account_address"]

        self.assertEqual(
            sdk_picked_address,
            "0x4FE260D11bf48BA3a94459771259c910a398ac59",
            "The SDK must query the configured trading-account address, "
            "not the signing key address.",
        )

    # ------------------------------------------------------------------
    # D. response is None maps to CLOSE_SUBMISSION_NOOP, never
    #    AMBIGUOUS_LADDER_RESPONSE nor CLOSE_RESPONSE_MALFORMED.
    # ------------------------------------------------------------------
    def test_D_None_maps_to_CLOSE_SUBMISSION_NOOP(self):
        from plugins.trade.agents import x_hyperliquid_agent as hl

        verdict = hl._classify_close_response(None)
        self.assertEqual(verdict.kind, "unconfirmed")
        self.assertEqual(verdict.code, "CLOSE_SUBMISSION_NOOP")
        self.assertNotIn(
            _FORBIDDEN,
            verdict.code + verdict.message,
            "CLOSE_SUBMISSION_NOOP must NEVER carry AMBIGUOUS_LADDER_RESPONSE",
        )
        self.assertNotEqual(
            verdict.code, "CLOSE_RESPONSE_MALFORMED",
            "response is None is a distinct outcome from a malformed envelope",
        )
        # Message must be evidence-based, not asserting root cause
        self.assertIn("No matching position", verdict.message)
        self.assertIn("no automatic retry", verdict.message)
        # Should NOT claim "account configuration is wrong"
        self.assertNotIn("account configuration", verdict.message.lower())

    # ------------------------------------------------------------------
    # E. None + post-read flat -> success if the position actually
    #    became flat.
    # ------------------------------------------------------------------
    def test_E_None_with_flat_position_is_success(self):
        hl = _hl_module()

        # Build a recording exchange that returns None from market_close.
        rec_exchange = _RecordingExchange(
            wallet=mock.MagicMock(),
            base_url="http://test",
            account_address="0x4FE260D11bf48BA3a94459771259c910a398ac59",
        )
        rec_exchange.info = mock.MagicMock()  # SDK attaches self.info
        _RecordingExchange.instances.append(rec_exchange)

        # Pre-read sees a SHORT, post-read is flat -> success.
        pre_positions = [CanonicalPosition(
            symbol="HYPE", side="short", size="250",
            entry_price="71.075", pnl="+1", tp=None, sl=None,
        )]
        post_positions = []  # genuinely flat after close
        self._patch_context(
            [self._position_response(pre_positions), self._position_response(post_positions)],
            [[], []],
            rec_exchange,  # _build_exchange_client will return this
        )
        # _build_exchange_client calls Exchange(...) which we've patched
        # to _RecordingExchange — but _patch_context already patches
        # _build_exchange_client to return a tuple. Override that:
        with mock.patch(
            "plugins.trade.agents.x_hyperliquid_agent._build_exchange_client",
            lambda account: (rec_exchange, "0x4FE2...", "secret"),
        ):
            response = hl.execute({
                "operation": "close_position",
                "exchange": "hyperliquid",
                "account": "TRADE",
                "symbol": "HYPE",
            })

        self.assertTrue(
            response.success,
            f"None + flat post-position must succeed. Got: {response.error}",
        )
        self.assertEqual(response.position_action.status, "success")
        # market_close was called exactly once
        self.assertEqual(len(rec_exchange.market_close_calls), 1)

    # ------------------------------------------------------------------
    # F. None + unchanged position -> CLOSE_SUBMISSION_NOOP with
    #    remaining size surfaced.
    # ------------------------------------------------------------------
    def test_F_None_with_unchanged_position_is_noop_with_size(self):
        hl = _hl_module()

        rec_exchange = _RecordingExchange(
            wallet=mock.MagicMock(),
            base_url="http://test",
            account_address="0x4FE260D11bf48BA3a94459771259c910a398ac59",
        )
        _RecordingExchange.instances.append(rec_exchange)

        pre_positions = [CanonicalPosition(
            symbol="HYPE", side="short", size="250",
            entry_price="71.075", pnl="+1", tp=None, sl=None,
        )]
        # Post-read shows the SAME position — close did not execute.
        post_positions = [CanonicalPosition(
            symbol="HYPE", side="short", size="250",
            entry_price="71.075", pnl="+1", tp=None, sl=None,
        )]
        self._patch_context(
            [self._position_response(pre_positions), self._position_response(post_positions)],
            [[], []],
            None,
        )
        with mock.patch(
            "plugins.trade.agents.x_hyperliquid_agent._build_exchange_client",
            lambda account: (rec_exchange, "0x4FE2...", "secret"),
        ):
            response = hl.execute({
                "operation": "close_position",
                "exchange": "hyperliquid",
                "account": "TRADE",
                "symbol": "HYPE",
            })

        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "CLOSE_SUBMISSION_NOOP")
        # remaining position surfaced
        self.assertEqual(response.position_action.status, "unchanged")
        self.assertEqual(
            Decimal(str(response.position_action.current_size)),
            Decimal("250"),
        )
        # exactly one submission (no retry)
        self.assertEqual(len(rec_exchange.market_close_calls), 1)
        # Forbidden codes do NOT appear
        self.assertNotEqual(response.error.code, _FORBIDDEN)
        self.assertNotEqual(response.error.code, "CLOSE_RESPONSE_MALFORMED")

    # ------------------------------------------------------------------
    # G. No automatic retry.
    # ------------------------------------------------------------------
    def test_G_no_automatic_retry(self):
        # Same scenario as F: None + unchanged position.
        # market_close was called exactly once; no second call.
        # (Already asserted in F; this test is explicit and named for the
        # G requirement.)
        self.test_F_None_with_unchanged_position_is_noop_with_size()

    # ------------------------------------------------------------------
    # H + I. Existing close / New Order / Ladder / TP / SL tests
    #    remain green.
    #    Verified by running the full phase4 + hyperliquid_close_response
    #    suites in __main__.
    # ------------------------------------------------------------------


def _hl_module():
    from plugins.trade.agents import x_hyperliquid_agent as hl  # noqa: F401
    return hl


# When this module is run as a script, also execute the existing
# Hyperliquid test suites to confirm H + I are satisfied.
def _run_existing_suites():
    import unittest as _u
    loader = _u.TestLoader()
    runner = _u.TextTestRunner(verbosity=0)

    from plugins.trade.tests.test_phase4 import TestPositionManagementWrites, TestNewOrderExecution
    from plugins.trade.tests.test_hyperliquid_close_response import (
        ClosePositionResponseRegressionTests,
    )

    suites = [
        ("test_phase4 (TestPositionManagementWrites)", TestPositionManagementWrites),
        ("test_phase4 (TestNewOrderExecution)", TestNewOrderExecution),
        ("test_hyperliquid_close_response", ClosePositionResponseRegressionTests),
    ]
    overall_ok = True
    for name, cls in suites:
        suite = loader.loadTestsFromTestCase(cls)
        print(f"\n=== {name} ===")
        result = runner.run(suite)
        if not result.wasSuccessful():
            overall_ok = False
    return overall_ok


if __name__ == "__main__":
    # Run our new tests first
    print("=== HyperliquidAccountContextRegressionTests ===")
    result = unittest.main(exit=False, verbosity=2)

    # Then run the existing suites
    print("\n=== Re-running existing Hyperliquid test suites for regression ===")
    ok = _run_existing_suites()
    if not ok:
        sys.exit(1)
