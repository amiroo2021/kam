"""Regression tests for the Apex "displayed size 15.17, fetched size 0.00"
TP/SL/close-position bug.

Incident: a BITGET ETH-USDT SHORT position showed Size: 15.17 on the
Positions & Orders screen, but confirming "Set TP -> 1400" failed with
POSITION_SIZE_UNRESOLVED (fetched='0.00'). Root cause: Apex's account
snapshot can carry more than one row for the same symbol — a residual
zero-size row alongside the real open position — and
``_apex_find_position`` picked whichever row came first in the list,
with no size or side disambiguation. The Positions & Orders screen
never hit this because ``_normalize_positions`` already filters
zero-size rows out before display.

These tests build positions lists with that exact shape (a zero-size
decoy row + a real non-zero row for the same symbol) and verify:

  * ``_apex_find_position`` picks the positive-size row, preferring a
    side match when a hint is supplied.
  * set_tp / set_sl / close_position resolve the REAL fresh size from
    that row and place the order correctly (size=15.17, not 0).
  * None of them ever submit size=0 to the exchange, and none of them
    substitute the wizard's stale displayed size when the position
    truly cannot be verified (every candidate row is zero-size).
  * The behavior holds for both LONG and SHORT positions, for a
    symbol alias that must be resolved to the canonical Apex symbol
    first, and across different accounts (BITGET vs FIBO) without
    cross-account leakage.

Run with::

    python3 -m pytest plugins/trade/tests/test_apex_set_tp_position_size_regression.py -v
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

import plugins.trade.agents.x_apex_agent as apex


def _meta(symbol: str = "ETH-USDT") -> dict:
    return {
        "symbol": symbol,
        "tick_size": Decimal("0.01"),
        "step_size": Decimal("0.01"),
    }


def _patch_common(account: str, positions):
    """Return the stack of patches shared by every scenario below:
    credential resolution, symbol/market lookup, trading-context prep,
    and the live positions snapshot. ``client`` is yielded so callers
    can assert on create_order_v3 / delete_order_v3 calls.
    """
    creds = {"account": account}
    client = mock.MagicMock()
    client.create_order_v3.return_value = {"data": {"id": "123456"}}
    client.open_orders_v3.return_value = {"orders": []}
    return (
        mock.patch.object(apex, "_resolve_credentials", return_value=(creds, None)),
        mock.patch.object(apex, "_client_for_credentials", return_value=client),
        mock.patch.object(apex, "_apex_fetch_supported_markets", return_value=[_meta()]),
        mock.patch.object(apex, "_apex_resolve_symbol", return_value=_meta()),
        mock.patch.object(apex, "_apex_normalize_meta", return_value=_meta()),
        mock.patch.object(apex, "_apex_prepare_trading_context", return_value="primary"),
        mock.patch.object(apex, "_apex_fetch_positions", return_value=positions),
    ), client


class TestApexPositionSizeRegression(unittest.TestCase):
    """Reproduces and guards the 15.17-displayed / 0.00-fetched bug."""

    # -- _apex_find_position disambiguation (unit-level) -----------------

    def test_find_position_prefers_positive_size_row_short(self):
        """Two rows share the symbol; the zero-size decoy must lose even
        though it appears first in the list (the historical bug: first
        match wins unconditionally)."""
        positions = [
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "0"},
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "15.17"},
        ]
        row = apex._apex_find_position(positions, "ETH-USDT")
        self.assertEqual(apex._apex_position_size(row), Decimal("15.17"))

    def test_find_position_prefers_positive_size_row_long(self):
        positions = [
            {"symbol": "ETH-USDT", "side": "LONG", "size": "0"},
            {"symbol": "ETH-USDT", "side": "LONG", "size": "8.5"},
        ]
        row = apex._apex_find_position(positions, "ETH-USDT")
        self.assertEqual(apex._apex_position_size(row), Decimal("8.5"))

    def test_find_position_uses_side_hint_to_break_ties(self):
        """When both candidate rows have positive size (a hedge-mode
        account with simultaneous long and short legs), the side hint
        must pick the leg the caller is actually acting on."""
        positions = [
            {"symbol": "ETH-USDT", "side": "LONG", "size": "3.0"},
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "15.17"},
        ]
        row = apex._apex_find_position(positions, "ETH-USDT", side="short")
        self.assertEqual(apex._apex_position_side(row), "short")
        self.assertEqual(apex._apex_position_size(row), Decimal("15.17"))

        row_long = apex._apex_find_position(positions, "ETH-USDT", side="long")
        self.assertEqual(apex._apex_position_side(row_long), "long")
        self.assertEqual(apex._apex_position_size(row_long), Decimal("3.0"))

    def test_find_position_single_row_unaffected(self):
        """The common case (one row per symbol) must behave exactly as
        before — no regression for the normal path."""
        positions = [{"symbol": "BTC-USDT", "side": "LONG", "size": "1.0"}]
        row = apex._apex_find_position(positions, "BTC-USDT")
        self.assertIs(row, positions[0])

    def test_find_position_all_zero_returns_a_row_not_none(self):
        """If every row for the symbol is genuinely zero (flat position
        with stale residuals, no real open position), disambiguation
        must not invent a positive size — it still returns *a* row
        (callers then correctly fail the positive-size guard)."""
        positions = [
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "0"},
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "0"},
        ]
        row = apex._apex_find_position(positions, "ETH-USDT")
        self.assertEqual(apex._apex_position_size(row), Decimal("0"))

    # -- set_tp: end-to-end regression repro ------------------------------

    def test_set_tp_short_resolves_real_size_not_zero_decoy(self):
        """Exact incident repro: BITGET ETH-USDT SHORT, decoy zero row
        ordered first, real 15.17 row second. TP must place with
        size=15.17, never 0, and never touch the (absent) request size."""
        positions = [
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "0"},
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "15.17"},
        ]
        patches, client = _patch_common("BITGET", positions)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            resp = apex._apex_set_tp({
                "account": "BITGET", "symbol": "ETH-USDT", "price": "1400",
                "side": "sell",  # wizard's request-side convention for a short
            })
        self.assertTrue(resp.success, resp.error)
        self.assertTrue(client.create_order_v3.called)
        kwargs = client.create_order_v3.call_args.kwargs
        self.assertEqual(kwargs["size"], "15.17")
        self.assertEqual(resp.position_action.current_size, "15.17")

    def test_set_tp_long_resolves_real_size_not_zero_decoy(self):
        positions = [
            {"symbol": "ETH-USDT", "side": "LONG", "size": "0"},
            {"symbol": "ETH-USDT", "side": "LONG", "size": "9.4"},
        ]
        patches, client = _patch_common("BITGET", positions)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            resp = apex._apex_set_tp({
                "account": "BITGET", "symbol": "ETH-USDT", "price": "4000",
                "side": "buy",
            })
        self.assertTrue(resp.success, resp.error)
        kwargs = client.create_order_v3.call_args.kwargs
        self.assertEqual(kwargs["size"], "9.4")

    def test_set_tp_never_substitutes_stale_request_size(self):
        """If every fresh candidate row is zero (position cannot be
        verified), set_tp must fail with POSITION_SIZE_UNRESOLVED and
        must NOT fall back to a wizard-supplied stale size, even when
        the request happens to carry one."""
        positions = [{"symbol": "ETH-USDT", "side": "SHORT", "size": "0"}]
        patches, client = _patch_common("BITGET", positions)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            resp = apex._apex_set_tp({
                "account": "BITGET", "symbol": "ETH-USDT", "price": "1400",
                "side": "sell",
                # A stale displayed size the UI might still be carrying —
                # must be ignored, never used as the order size.
                "size": "15.17",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "POSITION_SIZE_UNRESOLVED")
        self.assertFalse(client.create_order_v3.called)

    def test_set_tp_symbol_alias_resolves_before_matching(self):
        """A request using an alias (e.g. 'ETHUSDT') must be normalized
        to the canonical Apex symbol ('ETH-USDT') before the positions
        list is searched — alias handling and size disambiguation must
        compose correctly."""
        positions = [
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "0"},
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "15.17"},
        ]
        creds = {"account": "BITGET"}
        client = mock.MagicMock()
        client.create_order_v3.return_value = {"data": {"id": "123456"}}
        with mock.patch.object(apex, "_resolve_credentials", return_value=(creds, None)), \
             mock.patch.object(apex, "_client_for_credentials", return_value=client), \
             mock.patch.object(apex, "_apex_fetch_supported_markets", return_value=[_meta()]), \
             mock.patch.object(apex, "_apex_resolve_symbol", return_value=_meta()), \
             mock.patch.object(apex, "_apex_normalize_meta", return_value=_meta("ETH-USDT")), \
             mock.patch.object(apex, "_apex_prepare_trading_context", return_value="primary"), \
             mock.patch.object(apex, "_apex_fetch_positions", return_value=positions):
            resp = apex._apex_set_tp({
                "account": "BITGET", "symbol": "ETHUSDT", "price": "1400",
                "side": "sell",
            })
        self.assertTrue(resp.success, resp.error)
        kwargs = client.create_order_v3.call_args.kwargs
        self.assertEqual(kwargs["symbol"], "ETH-USDT")
        self.assertEqual(kwargs["size"], "15.17")

    def test_set_tp_multiple_accounts_do_not_cross_contaminate(self):
        """FIBO's decoy/real rows must never leak into BITGET's
        resolution and vice versa — each call only ever sees the
        positions list returned for its own account."""
        bitget_positions = [
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "0"},
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "15.17"},
        ]
        fibo_positions = [
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "0"},
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "3.33"},
        ]

        for account, positions, expected_size in (
            ("BITGET", bitget_positions, "15.17"),
            ("FIBO", fibo_positions, "3.33"),
        ):
            with self.subTest(account=account):
                creds = {"account": account}
                client = mock.MagicMock()
                client.create_order_v3.return_value = {"data": {"id": "123456"}}
                with mock.patch.object(apex, "_resolve_credentials", return_value=(creds, None)), \
                     mock.patch.object(apex, "_client_for_credentials", return_value=client), \
                     mock.patch.object(apex, "_apex_fetch_supported_markets", return_value=[_meta()]), \
                     mock.patch.object(apex, "_apex_resolve_symbol", return_value=_meta()), \
                     mock.patch.object(apex, "_apex_normalize_meta", return_value=_meta()), \
                     mock.patch.object(apex, "_apex_prepare_trading_context", return_value="primary"), \
                     mock.patch.object(apex, "_apex_fetch_positions", return_value=positions):
                    resp = apex._apex_set_tp({
                        "account": account, "symbol": "ETH-USDT", "price": "1400",
                        "side": "sell",
                    })
                self.assertTrue(resp.success, resp.error)
                kwargs = client.create_order_v3.call_args.kwargs
                self.assertEqual(kwargs["size"], expected_size)
                self.assertEqual(resp.account, account)

    # -- set_sl: same guard, same fallback removal ------------------------

    def test_set_sl_resolves_real_size_not_zero_decoy(self):
        positions = [
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "0"},
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "15.17"},
        ]
        patches, client = _patch_common("BITGET", positions)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            resp = apex._apex_set_sl({
                "account": "BITGET", "symbol": "ETH-USDT", "price": "1600",
                "side": "sell",
            })
        self.assertTrue(resp.success, resp.error)
        kwargs = client.create_order_v3.call_args.kwargs
        self.assertEqual(kwargs["size"], "15.17")

    def test_set_sl_never_substitutes_stale_request_size(self):
        positions = [{"symbol": "ETH-USDT", "side": "SHORT", "size": "0"}]
        patches, client = _patch_common("BITGET", positions)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            resp = apex._apex_set_sl({
                "account": "BITGET", "symbol": "ETH-USDT", "price": "1600",
                "side": "sell", "size": "15.17",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "POSITION_SIZE_UNRESOLVED")
        self.assertFalse(client.create_order_v3.called)

    # -- close_position: disambiguation + new positive-size guard --------

    def test_close_position_resolves_real_size_not_zero_decoy(self):
        positions = [
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "0"},
            {"symbol": "ETH-USDT", "side": "SHORT", "size": "15.17"},
        ]
        creds = {"account": "BITGET"}
        client = mock.MagicMock()
        client.create_order_v3.return_value = {"data": {"id": "123456"}}
        with mock.patch.object(apex, "_resolve_credentials", return_value=(creds, None)), \
             mock.patch.object(apex, "_client_for_credentials", return_value=client), \
             mock.patch.object(apex, "_apex_fetch_supported_markets", return_value=[_meta()]), \
             mock.patch.object(apex, "_apex_resolve_symbol", return_value=_meta()), \
             mock.patch.object(apex, "_apex_normalize_meta", return_value=_meta()), \
             mock.patch.object(apex, "_apex_prepare_trading_context", return_value="primary"), \
             mock.patch.object(apex, "_apex_fetch_positions", return_value=positions), \
             mock.patch.object(apex, "_apex_fetch_mark_price", return_value=Decimal("1500")):
            resp = apex._apex_close_position({
                "account": "BITGET", "symbol": "ETH-USDT", "side": "sell",
            })
        self.assertTrue(resp.success, resp.error)
        kwargs = client.create_order_v3.call_args.kwargs
        self.assertEqual(kwargs["size"], "15.17")
        # Reduce-only + correct closing side (short -> close is BUY) preserved.
        self.assertTrue(kwargs["reduceOnly"])
        self.assertEqual(kwargs["side"], "BUY")

    def test_close_position_never_substitutes_stale_size(self):
        positions = [{"symbol": "ETH-USDT", "side": "SHORT", "size": "0"}]
        creds = {"account": "BITGET"}
        client = mock.MagicMock()
        with mock.patch.object(apex, "_resolve_credentials", return_value=(creds, None)), \
             mock.patch.object(apex, "_client_for_credentials", return_value=client), \
             mock.patch.object(apex, "_apex_fetch_supported_markets", return_value=[_meta()]), \
             mock.patch.object(apex, "_apex_resolve_symbol", return_value=_meta()), \
             mock.patch.object(apex, "_apex_normalize_meta", return_value=_meta()), \
             mock.patch.object(apex, "_apex_prepare_trading_context", return_value="primary"), \
             mock.patch.object(apex, "_apex_fetch_positions", return_value=positions), \
             mock.patch.object(apex, "_apex_fetch_mark_price", return_value=Decimal("1500")):
            resp = apex._apex_close_position({
                "account": "BITGET", "symbol": "ETH-USDT", "side": "sell",
                "size": "15.17",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "POSITION_SIZE_UNRESOLVED")
        self.assertFalse(client.create_order_v3.called)

    # -- _apex_side_hint mapping -------------------------------------------

    def test_side_hint_maps_request_conventions(self):
        self.assertEqual(apex._apex_side_hint({"side": "buy"}), "long")
        self.assertEqual(apex._apex_side_hint({"side": "sell"}), "short")
        self.assertEqual(apex._apex_side_hint({"side": "long"}), "long")
        self.assertEqual(apex._apex_side_hint({"side": "short"}), "short")
        self.assertIsNone(apex._apex_side_hint({"side": ""}))
        self.assertIsNone(apex._apex_side_hint({}))


if __name__ == "__main__":
    unittest.main()
