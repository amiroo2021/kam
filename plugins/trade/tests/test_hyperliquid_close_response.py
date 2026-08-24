"""Regression tests for Hyperliquid close-position response handling.

Proves:
  - Explicit exchange rejection → failure with the real exchange
    reason preserved (no ladder error code).
  - Successful filled response + flat post-position → success.
  - Successful resting response + flat post-position → success.
  - Ambiguous/unconfirmed parser response + flat post-position → success
    (post-submit verification is authoritative).
  - Ambiguous/unconfirmed parser response + unchanged post-position →
    CLOSE_OUTCOME_UNCONFIRMED with remaining position surfaced; no
    auto-retry.
  - Ambiguous/unconfirmed parser response + partially reduced post-position
    → CLOSE_PARTIALLY_FILLED with before/after/reduced-by; no auto-retry.
  - Malformed envelope + flat post-position → success.
  - Malformed envelope + still-open post-position → CLOSE_RESPONSE_MALFORMED
    with remaining position; no auto-retry.
  - Sign reversal (position flipped from SHORT to LONG unexpectedly) →
    CLOSE_POSITION_MISMATCH; no auto-retry.
  - Position grew (size > original) → CLOSE_POSITION_MISMATCH.
  - **No single close path may return AMBIGUOUS_LADDER_RESPONSE.**
  - Existing ladder ambiguity test (`test_ladder_is_ambiguous_only_when
    _statuses_are_missing`) is unaffected.

Read-only — no live writes, no real network. Uses the same FakeExchange
helper / patching pattern as the existing phase4 tests.
"""
from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest import mock

# Ensure the source tree is importable so we test against the patched
# /root/kam module (and not the live installed one).
_KAM_ROOT = "/root/kam"
if _KAM_ROOT not in sys.path:
    sys.path.insert(0, _KAM_ROOT)
# Force the editable hermes-agent loader to also resolve to the source
# tree, not /usr/local/lib/hermes-agent.
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


def _ok_response_with_filled(oid: int = 12345) -> Dict[str, Any]:
    """Parser-shape response where the order filled immediately."""
    return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": oid}}]}}}


def _ok_response_with_resting(oid: int = 12346) -> Dict[str, Any]:
    """Parser-shape response where the order is resting."""
    return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}


def _ok_response_missing_statuses() -> Dict[str, Any]:
    """Parser-shape response that hits 'missing_statuses' branch."""
    return {"status": "ok", "response": {"data": {}}}


def _ok_response_status_count_mismatch() -> Dict[str, Any]:
    """Parser-shape response that hits 'status_count_mismatch' branch."""
    return {"status": "ok", "response": {"data": {"statuses": []}}}


def _ok_response_unknown_child() -> Dict[str, Any]:
    """Parser-shape response that hits 'unknown_child' branch."""
    return {"status": "ok", "response": {"data": {"statuses": ["not-a-dict"]}}}


def _malformed_envelope() -> Any:
    """Non-dict payload — hits 'malformed_envelope' branch."""
    return "completely-broken-payload"


def _top_level_error(reason: str = "Insufficient margin to place order.") -> Dict[str, Any]:
    """Parser-shape response with explicit top-level error."""
    return {"status": "err", "response": reason}


def _child_error(reason: str = "Order would immediately match and trade against itself.") -> Dict[str, Any]:
    """Parser-shape response with per-child error."""
    return {"status": "ok", "response": {"data": {"statuses": [{"error": reason}]}}}


class ClosePositionResponseRegressionTests(TestPositionManagementWrites):
    """Regression suite for the close-position response classification fix."""

    def _assert_no_ladder_error(self, response) -> None:
        """Invariant: a single close must NEVER return AMBIGUOUS_LADDER_RESPONSE."""
        if response.error is not None:
            self.assertNotEqual(
                response.error.code,
                _FORBIDDEN,
                "Single close path must never return AMBIGUOUS_LADDER_RESPONSE; "
                f"got code={response.error.code!r} message={response.error.message!r}",
            )

    def _close_with(self, submit_response, post_positions, side="short", size="250"):
        """Run a single close with the given submission response and post-submit positions."""
        hl = _hl_module()
        exchange = FakeExchange([submit_response])
        pre_positions = [CanonicalPosition(
            symbol="HYPE", side=side, size=size,
            entry_price="71.075", pnl="+1", tp=None, sl=None,
        )]
        self._patch_context(
            [self._position_response(pre_positions), self._position_response(post_positions)],
            [[], []],
            exchange,
        )
        response = hl.execute({
            "operation": "close_position",
            "exchange": "hyperliquid",
            "account": "TRADE",
            "symbol": "HYPE",
        })
        return response, exchange

    # ---- success paths ----

    def test_filled_response_with_flat_position_is_success(self):
        response, exchange = self._close_with(_ok_response_with_filled(), post_positions=[])
        self.assertTrue(response.success, f"expected success, got {response.error}")
        self.assertEqual(response.position_action.status, "success")
        self.assertEqual(response.position_action.message, "Position closed.")
        self.assertEqual(len(exchange.requests), 1)
        self.assertEqual(exchange.requests[0][0]["coin"], "HYPE")
        self._assert_no_ladder_error(response)

    def test_resting_response_with_flat_position_is_success(self):
        response, _ = self._close_with(_ok_response_with_resting(), post_positions=[])
        self.assertTrue(response.success)
        self.assertEqual(response.position_action.status, "success")
        self._assert_no_ladder_error(response)

    # ---- ambiguous parser response + evidence-based outcomes ----

    def test_missing_statuses_with_flat_position_is_success(self):
        response, _ = self._close_with(_ok_response_missing_statuses(), post_positions=[])
        self.assertTrue(response.success, f"expected success, got {response.error}")
        self.assertEqual(response.position_action.status, "success")
        self._assert_no_ladder_error(response)

    def test_status_count_mismatch_with_flat_position_is_success(self):
        response, _ = self._close_with(_ok_response_status_count_mismatch(), post_positions=[])
        self.assertTrue(response.success)
        self._assert_no_ladder_error(response)

    def test_unknown_child_with_flat_position_is_success(self):
        response, _ = self._close_with(_ok_response_unknown_child(), post_positions=[])
        self.assertTrue(response.success)
        self._assert_no_ladder_error(response)

    def test_missing_statuses_with_unchanged_position_is_unconfirmed(self):
        # Original was 250; post still 250; no auto-retry.
        response, exchange = self._close_with(
            _ok_response_missing_statuses(),
            post_positions=[CanonicalPosition(
                symbol="HYPE", side="short", size="250",
                entry_price="71.075", pnl="+1", tp=None, sl=None,
            )],
        )
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "CLOSE_OUTCOME_UNCONFIRMED")
        self.assertEqual(response.position_action.status, "unchanged")
        self.assertEqual(Decimal(str(response.position_action.current_size)), Decimal("250"))
        # Exactly one submission was made — no retry.
        self.assertEqual(len(exchange.requests), 1)
        self._assert_no_ladder_error(response)

    def test_missing_statuses_with_partial_position_is_partial_close(self):
        # Original 250 short; post 100 short → CLOSE_PARTIALLY_FILLED.
        response, exchange = self._close_with(
            _ok_response_missing_statuses(),
            post_positions=[CanonicalPosition(
                symbol="HYPE", side="short", size="100",
                entry_price="71.075", pnl="+1", tp=None, sl=None,
            )],
            size="250",
        )
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "CLOSE_PARTIALLY_FILLED")
        self.assertEqual(response.position_action.status, "partial")
        self.assertEqual(Decimal(str(response.position_action.current_size)), Decimal("100"))
        # Message must surface before/after and reduced-by.
        msg = response.error.message
        self.assertIn("250", msg)  # original
        self.assertIn("100", msg)  # remaining
        self.assertIn("reduced_by", msg)
        # No retry.
        self.assertEqual(len(exchange.requests), 1)
        self._assert_no_ladder_error(response)

    def test_missing_statuses_with_reversed_position_is_mismatch(self):
        # Original short 250; post long 250 → sign flipped.
        response, exchange = self._close_with(
            _ok_response_missing_statuses(),
            post_positions=[CanonicalPosition(
                symbol="HYPE", side="long", size="250",
                entry_price="71.075", pnl="+1", tp=None, sl=None,
            )],
            side="short", size="250",
        )
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "CLOSE_POSITION_MISMATCH")
        self.assertEqual(response.position_action.status, "mismatch")
        self.assertEqual(len(exchange.requests), 1)
        self._assert_no_ladder_error(response)

    def test_missing_statuses_with_grown_position_is_mismatch(self):
        # Original short 250; post short 500 → size grew.
        response, exchange = self._close_with(
            _ok_response_missing_statuses(),
            post_positions=[CanonicalPosition(
                symbol="HYPE", side="short", size="500",
                entry_price="71.075", pnl="+1", tp=None, sl=None,
            )],
            side="short", size="250",
        )
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "CLOSE_POSITION_MISMATCH")
        self.assertEqual(len(exchange.requests), 1)
        self._assert_no_ladder_error(response)

    # ---- malformed envelope + evidence-based outcomes ----

    def test_malformed_with_flat_position_is_success(self):
        response, _ = self._close_with(_malformed_envelope(), post_positions=[])
        self.assertTrue(response.success, f"expected success, got {response.error}")
        self.assertEqual(response.position_action.status, "success")
        self._assert_no_ladder_error(response)

    def test_malformed_with_unchanged_position_is_malformed(self):
        response, exchange = self._close_with(
            _malformed_envelope(),
            post_positions=[CanonicalPosition(
                symbol="HYPE", side="short", size="250",
                entry_price="71.075", pnl="+1", tp=None, sl=None,
            )],
        )
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "CLOSE_RESPONSE_MALFORMED")
        # Remaining size surfaced.
        self.assertEqual(Decimal(str(response.position_action.current_size)), Decimal("250"))
        self.assertEqual(len(exchange.requests), 1)
        self._assert_no_ladder_error(response)

    # ---- explicit rejections ----

    def test_top_level_rejection_preserves_exchange_reason(self):
        reason = "Insufficient margin to place order."
        response, _ = self._close_with(_top_level_error(reason), post_positions=[])
        self.assertFalse(response.success)
        # Exact code is preserved (not AMBIGUOUS_LADDER_RESPONSE).
        self.assertEqual(response.error.code, "INSUFFICIENT_MARGIN")
        self.assertEqual(response.error.exchange_reason, reason)
        self.assertEqual(response.position_action.status, "rejected")
        self._assert_no_ladder_error(response)

    def test_child_rejection_preserves_exchange_reason(self):
        reason = "Order would immediately match and trade against itself."
        response, _ = self._close_with(_child_error(reason), post_positions=[])
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "EXCHANGE_REJECTED")
        self.assertEqual(response.error.exchange_reason, reason)
        self.assertEqual(response.position_action.status, "rejected")
        self._assert_no_ladder_error(response)

    # ---- Decimal precision invariants ----

    def test_decimal_precision_residual_within_tolerance_is_flat(self):
        # Original 1.099; post shows 0.0000000001 (well within tolerance).
        # The post position must be for the SAME symbol (HYPE).
        response, _ = self._close_with(
            _ok_response_missing_statuses(),
            post_positions=[CanonicalPosition(
                symbol="HYPE", side="short", size="0.0000000001",
                entry_price="71.075", pnl="+1", tp=None, sl=None,
            )],
            size="1.099",
        )
        self.assertTrue(response.success, f"expected success, got {response.error}")
        self.assertEqual(response.position_action.status, "success")
        self._assert_no_ladder_error(response)

    def test_decimal_precision_residual_above_tolerance_is_not_flat(self):
        # Original 1.099; post shows 0.001 (above the 1e-9 tolerance).
        # The post position must be for the SAME symbol (HYPE) — the
        # agent closes by symbol, not by any position.
        response, _ = self._close_with(
            _ok_response_missing_statuses(),
            post_positions=[CanonicalPosition(
                symbol="HYPE", side="short", size="0.001",
                entry_price="71.075", pnl="+1", tp=None, sl=None,
            )],
            size="1.099",
        )
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "CLOSE_PARTIALLY_FILLED")
        self._assert_no_ladder_error(response)

    # ---- invariant: NEVER AMBIGUOUS_LADDER_RESPONSE ----

    def test_close_never_returns_ladder_error_across_all_response_shapes(self):
        """Exhaustively confirm no single close path emits AMBIGUOUS_LADDER_RESPONSE."""
        shapes = [
            _ok_response_with_filled(),
            _ok_response_with_resting(),
            _ok_response_missing_statuses(),
            _ok_response_status_count_mismatch(),
            _ok_response_unknown_child(),
            _malformed_envelope(),
            _top_level_error(),
            _child_error(),
        ]
        for shape in shapes:
            # post_positions with no matching symbol → "unknown" branch
            response, _ = self._close_with(shape, post_positions=[])
            self._assert_no_ladder_error(response)
            # post_positions with same symbol → various branches
            response, _ = self._close_with(shape, post_positions=[
                CanonicalPosition(symbol="HYPE", side="short", size="250",
                                 entry_price="71.075", pnl="+1", tp=None, sl=None),
            ])
            self._assert_no_ladder_error(response)

    # ---- ladder path untouched ----

    def test_ladder_ambiguity_behavior_unchanged(self):
        """The existing ladder ambiguity contract must still work.

        Re-runs the specific existing test
        ``test_ladder_is_ambiguous_only_when_statuses_are_missing`` and
        proves we haven't broken the ladder path while changing close.
        """
        from plugins.trade.tests.test_phase4 import TestLadderExecution
        suite = unittest.TestSuite()
        suite.addTest(TestLadderExecution(
            "test_ladder_is_ambiguous_only_when_statuses_are_missing"
        ))
        runner = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w"))
        result = runner.run(suite)
        self.assertTrue(
            result.wasSuccessful(),
            f"Ladder regression broke — {len(result.failures)} failures, "
            f"{len(result.errors)} errors",
        )


def _hl_module():
    """Import the patched x_hyperliquid_agent module under test."""
    from plugins.trade.agents import x_hyperliquid_agent as hl  # noqa: F401
    return hl


if __name__ == "__main__":
    unittest.main()
