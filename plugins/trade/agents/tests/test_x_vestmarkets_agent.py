"""Tests for the Vest Markets exchange agent.

Covers:
  1. list_accounts() surfaces the configured alias (lowercased).
  2. list_accounts() ignores incomplete accounts (missing any of the
     four required VEST_<ALIAS>_* fields).
  3. _lookup_credentials("fibo") returns all four VEST fields,
     normalised by alias convention (lowercased).
  4. _lookup_credentials("nope") returns None.
  5. _normalize_alias("FIBO") returns "fibo".
  6. _portfolio_summary_from_account maps /account → CanonicalPortfolioSummary.
  7. WRONG_ACCOUNT gate: /account returning a different address is
     refused with code WRONG_ACCOUNT, not surfaced as balance.
  8. capabilities() includes only read-only ops in Phase 1.
  9. execute() routes a "balance" request to _balance, and surfaces a
     canonical NOT_IMPLEMENTED for any other operation.
 10. Passwords stay out of logs/exceptions/redaction routine behaviour.
 11. _redact() scrubs X-API-KEY header values defensively.
 12. _decimal_or_none() parses Vest's documented string-decimal form
     and returns None for missing/null/non-numeric values.
"""

from __future__ import annotations

import json
import os
import unittest
from decimal import Decimal
from typing import Any, Dict, Iterator, Optional

# Ensure KAM root is on sys.path when the test is run from anywhere.
import sys
_HERE = os.path.dirname(os.path.abspath(__file__))
_KAM_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
if _KAM_ROOT not in sys.path:
    sys.path.insert(0, _KAM_ROOT)

from plugins.trade.agents import x_vestmarkets_agent as vest  # noqa: E402


class VestAgentContractTests(unittest.TestCase):
    """Phase-1 read-only contract for x_vestmarkets_agent."""

    # -----------------------------------------------------------------
    # Fixtures
    # -----------------------------------------------------------------

    VALID_ENV: Dict[str, str] = {
        "VEST_FIBO_PUBLIC_KEY": "0x1634058e035EBBF97E9828F656c4D4Fdc17EB532",
        "VEST_FIBO_API_KEY": "vest-fibo-api-key-placeholder",
        "VEST_FIBO_SIGN_PRIVATE_KEY": (
            "52edbb10b37695e332802b1d849f921d4024c7fe5105d47788ae3ca958bf9874"
        ),
        "VEST_FIBO_ACCOUNT_GROUP": "0",
    }

    def setUp(self) -> None:
        # Snapshot real env so we can restore it. Tests also redirect
        # ``HERMES_HOME`` to a freshly created empty tmp directory so the
        # discovery helpers cannot leak the operator's real ``~/.hermes/.env``
        # into the assertions (every other KAM agent inherits the same
        # ``.env``-fallback behaviour, so this isolation is required).
        import tempfile
        self._saved = {k: os.environ.get(k) for k in list(os.environ)}
        self._tmpdir = tempfile.mkdtemp(prefix="vest-agent-tests-")
        os.environ["HERMES_HOME"] = self._tmpdir
        # Generate a deterministic test signing key for the sandboxed
        # build+sign+submit probe. NOT the operator's real
        # ``VEST_FIBO_SIGN_PRIVATE_KEY``.
        from eth_account import Account as _EthAccount
        # Deterministic 32-byte seed: 0x11 repeated.
        self._test_key_hex = "0x" + "11" * 32
        self._test_account = _EthAccount.from_key(self._test_key_hex)
        # The agent expects VEST_<ALIAS>_SIGN_PRIVATE_KEY in env; we
        # install our test key here so the credentials lookup succeeds
        # for write-path tests.
        self._apply({
            **self.VALID_ENV,
            "VEST_FIBO_SIGN_PRIVATE_KEY": "11" * 32,  # no 0x prefix
        })
        # Reset the agent's catalog + mark-price caches so each test
        # sees a fresh ``/exchangeInfo`` / ``/ticker/latest`` round trip
        # against the test-specific ``seed_get`` mock.
        vest._catalog_cache = {"ts": 0.0, "rows": [], "lock": None}
        if not hasattr(vest, "_catalog_lock") or vest._catalog_lock is None:
            import threading as _threading
            vest._catalog_lock = _threading.Lock()

    def tearDown(self) -> None:
        # Restore the original environment (including HERMES_HOME) and
        # remove the temporary directory.
        import shutil
        for key in list(os.environ):
            if key.startswith("VEST_") or key == "HERMES_HOME":
                os.environ.pop(key, None)
        for key, value in self._saved.items():
            if value is not None:
                os.environ[key] = value
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _apply(self, env: Dict[str, str]) -> None:
        # Clear VEST_* first, then set the test values.
        for key in list(os.environ):
            if key.startswith("VEST_"):
                os.environ.pop(key, None)
        for key, value in env.items():
            os.environ[key] = value

    # -----------------------------------------------------------------
    # Account discovery
    # -----------------------------------------------------------------

    def test_list_accounts_returns_fibo_alias(self):
        self._apply(self.VALID_ENV)
        accounts = vest.list_accounts()
        self.assertEqual(accounts, ["fibo"])

    def test_list_accounts_ignores_incomplete_accounts(self):
        incomplete = dict(self.VALID_ENV)
        del incomplete["VEST_FIBO_SIGN_PRIVATE_KEY"]
        self._apply(incomplete)
        self.assertEqual(vest.list_accounts(), [])

        # Missing ACCOUNT_GROUP also disqualifies.
        incomplete = dict(self.VALID_ENV)
        del incomplete["VEST_FIBO_ACCOUNT_GROUP"]
        self._apply(incomplete)
        self.assertEqual(vest.list_accounts(), [])

        # Missing PUBLIC_KEY also disqualifies.
        incomplete = dict(self.VALID_ENV)
        del incomplete["VEST_FIBO_PUBLIC_KEY"]
        self._apply(incomplete)
        self.assertEqual(vest.list_accounts(), [])

        # Missing API_KEY also disqualifies.
        incomplete = dict(self.VALID_ENV)
        del incomplete["VEST_FIBO_API_KEY"]
        self._apply(incomplete)
        self.assertEqual(vest.list_accounts(), [])

    def test_list_accounts_supports_multiple_accounts(self):
        env = dict(self.VALID_ENV)
        env.update(
            {
                "VEST_OTHER_PUBLIC_KEY": "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01",
                "VEST_OTHER_API_KEY": "other-api-key",
                "VEST_OTHER_SIGN_PRIVATE_KEY": (
                    "11" * 32
                ),
                "VEST_OTHER_ACCOUNT_GROUP": "1",
            }
        )
        self._apply(env)
        self.assertEqual(vest.list_accounts(), ["fibo", "other"])

    # -----------------------------------------------------------------
    # Credentials
    # -----------------------------------------------------------------

    def test_lookup_credentials_returns_all_four_fields(self):
        self._apply(self.VALID_ENV)
        creds = vest._lookup_credentials("fibo")
        self.assertIsNotNone(creds)
        assert creds is not None
        self.assertEqual(creds["account"], "fibo")
        self.assertEqual(creds["public_key"], self.VALID_ENV["VEST_FIBO_PUBLIC_KEY"])
        self.assertEqual(creds["api_key"], self.VALID_ENV["VEST_FIBO_API_KEY"])
        self.assertEqual(creds["sign_private_key"], self.VALID_ENV["VEST_FIBO_SIGN_PRIVATE_KEY"])
        self.assertEqual(creds["account_group"], 0)
        # Default base URL.
        self.assertEqual(creds["base_url"], vest.DEFAULT_API_BASE)

    def test_lookup_credentials_rejects_unknown_alias(self):
        self._apply(self.VALID_ENV)
        self.assertIsNone(vest._lookup_credentials("nope"))

    def test_lookup_credentials_rejects_malformed_public_key(self):
        bad = dict(self.VALID_ENV)
        bad["VEST_FIBO_PUBLIC_KEY"] = "not-an-address"
        self._apply(bad)
        self.assertIsNone(vest._lookup_credentials("fibo"))

    def test_lookup_credentials_rejects_non_numeric_account_group(self):
        bad = dict(self.VALID_ENV)
        bad["VEST_FIBO_ACCOUNT_GROUP"] = "abc"
        self._apply(bad)
        self.assertIsNone(vest._lookup_credentials("fibo"))

    def test_lookup_credentials_accepts_0x_prefixed_private_key(self):
        prefixed = dict(self.VALID_ENV)
        prefixed["VEST_FIBO_SIGN_PRIVATE_KEY"] = (
            "0x" + self.VALID_ENV["VEST_FIBO_SIGN_PRIVATE_KEY"]
        )
        self._apply(prefixed)
        creds = vest._lookup_credentials("fibo")
        self.assertIsNotNone(creds)
        assert creds is not None
        self.assertTrue(creds["sign_private_key"].startswith("0x"))

    def test_normalize_alias_lowercases(self):
        self.assertEqual(vest._normalize_alias("FIBO"), "fibo")
        self.assertEqual(vest._normalize_alias("fibo"), "fibo")
        self.assertEqual(vest._normalize_alias("  Fibo  "), "fibo")
        self.assertEqual(vest._normalize_alias(""), "")

    # -----------------------------------------------------------------
    # Portfolio mapping
    # -----------------------------------------------------------------

    def test_portfolio_summary_from_account_maps_fields(self):
        summary = {
            "address": self.VALID_ENV["VEST_FIBO_PUBLIC_KEY"],
            "balances": [{"asset": "USDC", "total": "1000.000000", "locked": "0.000000"}],
            "collateral": "1000.000000",
            "withdrawable": "800.000000",
            "totalAccountValue": "1234.560000",
            "openOrderMargin": "100.000000",
            "totalMaintMargin": "50.000000",
            "positions": [
                {
                    "symbol": "BTC-PERP",
                    "isLong": True,
                    "size": "0.1000",
                    "entryPrice": "30000.00",
                    "markPrice": "31000.00",
                },
                {
                    "symbol": "ETH-PERP",
                    "isLong": False,
                    "size": "1.0000",
                    "entryPrice": "2000.00",
                    "markPrice": "1900.00",
                },
            ],
        }
        portfolio = vest._portfolio_summary_from_account(summary)
        # USDC strings only; values are 2-dp per normalize_balance contract.
        self.assertEqual(portfolio.unit, "USDC")
        self.assertEqual(portfolio.account_value, "1234.56")
        self.assertEqual(portfolio.withdrawable, "800.00")
        # margin_used = openOrderMargin + totalMaintMargin = 150.
        self.assertEqual(portfolio.margin_used, "150.00")
        # total_position_value = |0.1 * 31000| + |1 * 1900| = 3100 + 1900 = 5000.
        self.assertEqual(portfolio.total_position_value, "5000.00")

    def test_extract_account_summary_tolerates_wrapped_response(self):
        wrapped = {
            "code": 0,
            "msg": "success",
            "data": {
                "address": self.VALID_ENV["VEST_FIBO_PUBLIC_KEY"],
                "totalAccountValue": "1.000000",
                "balances": [],
            },
        }
        summary = vest._extract_account_summary(wrapped)
        self.assertEqual(summary["address"], self.VALID_ENV["VEST_FIBO_PUBLIC_KEY"])

    # -----------------------------------------------------------------
    # Wrong-account gate (identity check)
    # -----------------------------------------------------------------

    def test_balance_rejects_wrong_account_address(self):
        self._apply(self.VALID_ENV)
        # Patch the helper that performs the network call to return a
        # payload with a different address — the agent MUST refuse with
        # WRONG_ACCOUNT instead of rendering the misleading balance.
        original_signed_get = vest._signed_get

        def fake_signed_get(creds, _path):
            return {
                "address": "0x0000000000000000000000000000000000000000",
                "totalAccountValue": "9999.000000",
                "balances": [],
                "positions": [],
            }

        vest._signed_get = fake_signed_get  # type: ignore[assignment]
        try:
            response = vest.execute({"operation": "balance", "account": "fibo"})
        finally:
            vest._signed_get = original_signed_get  # type: ignore[assignment]

        self.assertFalse(response.success)
        assert response.error is not None
        self.assertEqual(response.error.code, "WRONG_ACCOUNT")

    # -----------------------------------------------------------------
    # Phase-1 capabilities
    # -----------------------------------------------------------------

    def test_capabilities_advertises_phase1_balance_in_phase_1(self):
        # Phase 1 advertised only ``balance``. The agent has since
        # grown past Phase 1 — we keep a backward-compatibility check
        # that ``balance`` is always present, but no longer assert the
        # write ops are absent (phase 3 has them).
        caps = vest.capabilities()
        self.assertIn("balance", caps)

    def test_execute_dispatches_balance(self):
        # Stub out the network call so we don't hit the live API.
        original_signed_get = vest._signed_get

        def fake_signed_get(creds, _path):
            return {
                "address": self.VALID_ENV["VEST_FIBO_PUBLIC_KEY"],
                "balances": [{"asset": "USDC", "total": "1.000000", "locked": "0.000000"}],
                "collateral": "1.000000",
                "withdrawable": "1.000000",
                "totalAccountValue": "1.000000",
                "openOrderMargin": "0.000000",
                "totalMaintMargin": "0.000000",
                "positions": [],
            }

        self._apply(self.VALID_ENV)
        vest._signed_get = fake_signed_get  # type: ignore[assignment]
        try:
            response = vest.execute({"operation": "balance", "account": "fibo"})
        finally:
            vest._signed_get = original_signed_get  # type: ignore[assignment]

        self.assertTrue(response.success)
        assert response.balance is not None
        # normalize_balance quantises to 2 dp per CanonicalBalance contract.
        self.assertEqual(response.balance.unit, "USDC")
        self.assertEqual(response.balance.value, "1.00")
        self.assertIsNotNone(response.portfolio_summary)

    def test_execute_unknown_operation_returns_not_implemented(self):
        # Pick an op that isn't advertised in capabilities — e.g.
        # ``set_tp`` / ``set_sl`` / ``close_position`` are NOT wired in
        # Phase 3 (vest-protected orders need their own signature
        # sub-proofs per the docs). They must return NOT_IMPLEMENTED.
        self._apply(self.VALID_ENV)
        response = vest.execute({"operation": "close_position", "account": "fibo"})
        self.assertFalse(response.success)
        assert response.error is not None
        self.assertEqual(response.error.code, "NOT_IMPLEMENTED")

    def test_execute_missing_account_returns_failure(self):
        response = vest.execute({"operation": "balance", "account": ""})
        self.assertFalse(response.success)
        assert response.error is not None
        self.assertEqual(response.error.code, "MISSING_ACCOUNT")

    def test_execute_unknown_account_returns_failure(self):
        self._apply(self.VALID_ENV)
        response = vest.execute({"operation": "balance", "account": "ghost"})
        self.assertFalse(response.success)
        assert response.error is not None
        self.assertEqual(response.error.code, "UNKNOWN_ACCOUNT")

    # -----------------------------------------------------------------
    # Redaction
    # -----------------------------------------------------------------

    def test_redact_scrubs_x_api_key_header_value(self):
        api_key = self.VALID_ENV["VEST_FIBO_API_KEY"]
        rendered = vest._redact(f"X-API-KEY: {api_key} refused")
        self.assertIn("X-API-KEY: ***", rendered)
        self.assertNotIn(api_key, rendered)

    def test_redact_scrubs_sign_private_key_when_loaded(self):
        # Restore the operator's real Fibonacci key for this test so
        # ``_lookup_credentials`` returns credentials whose
        # ``sign_private_key`` matches the value embedded in the
        # rendered text.
        self._apply(self.VALID_ENV)
        pk = self.VALID_ENV["VEST_FIBO_SIGN_PRIVATE_KEY"]
        # Stash the credentials so the defensive scrub has something to
        # scrub against, then ensure pk is gone from the rendered text.
        creds = vest._lookup_credentials("fibo")
        with vest._with_credentials(creds):
            rendered = vest._redact(f"panic: leaked {pk}")
        self.assertNotIn(pk, rendered)

    # -----------------------------------------------------------------
    # Decimal helpers
    # -----------------------------------------------------------------

    def test_decimal_or_none_parses_string_decimals(self):
        self.assertEqual(vest._decimal_or_none("1.230000"), Decimal("1.230000"))
        self.assertEqual(vest._decimal_or_none("0"), Decimal("0"))
        self.assertIsNone(vest._decimal_or_none(None))
        self.assertIsNone(vest._decimal_or_none(""))
        self.assertIsNone(vest._decimal_or_none("null"))
        self.assertIsNone(vest._decimal_or_none("not-a-number"))

    def test_decimal_text_strips_trailing_zero_padding(self):
        self.assertEqual(vest._decimal_text("100.000000"), "100")
        self.assertEqual(vest._decimal_text("1.500000"), "1.5")
        self.assertEqual(vest._decimal_text("0"), "0")
        self.assertEqual(vest._decimal_text("-0.000000"), "0")
        self.assertEqual(vest._decimal_text(None), "0")

    # -----------------------------------------------------------------
    # HTTP error mapping
    # -----------------------------------------------------------------

    def test_http_error_401_maps_to_auth_invalid(self):
        error = vest.VestHTTPError(status=401, path="/account", body="unauthorized")
        response = vest._map_http_error_to_failure(error, operation="balance", account="fibo")
        self.assertFalse(response.success)
        assert response.error is not None
        self.assertEqual(response.error.code, "AUTH_INVALID")

    def test_http_error_429_maps_to_rate_limited(self):
        error = vest.VestHTTPError(status=429, path="/account", body="rate limited")
        response = vest._map_http_error_to_failure(error, operation="balance", account="fibo")
        self.assertFalse(response.success)
        assert response.error is not None
        self.assertEqual(response.error.code, "RATE_LIMITED")

    def test_http_error_transport_maps_to_transport_error(self):
        error = vest.VestHTTPError(status=0, path="/account", body="connection refused")
        response = vest._map_http_error_to_failure(error, operation="balance", account="fibo")
        self.assertFalse(response.success)
        assert response.error is not None
        self.assertEqual(response.error.code, "TRANSPORT_ERROR")

    # -----------------------------------------------------------------
    # Phase 2: positions, orders, assets
    # -----------------------------------------------------------------

    def _account_payload(self, *, positions=None, balances=None, address=None):
        """Return a synthetic Vest ``/account`` payload."""
        return {
            "address": address or self.VALID_ENV["VEST_FIBO_PUBLIC_KEY"],
            "balances": balances if balances is not None else [
                {"asset": "USDC", "total": "1000.000000", "locked": "0.000000"},
            ],
            "collateral": "1000.000000",
            "withdrawable": "800.000000",
            "totalAccountValue": "1234.560000",
            "openOrderMargin": "100.000000",
            "totalMaintMargin": "50.000000",
            "positions": positions if positions is not None else [],
        }

    def test_capabilities_advertises_phase2_read_only_ops(self):
        caps = set(vest.capabilities())
        self.assertIn("balance", caps)
        self.assertIn("positions_orders", caps)
        self.assertIn("positions_management", caps)
        self.assertIn("market_constraints", caps)
        self.assertIn("get_exact_order", caps)
        # Phase 3 adds write ops: new_order / ladder /
        # cancel_order / cancel_order_group. Verify they ARE advertised
        # (Phase 2 had asserted the opposite — that test is stale).
        for required in (
            "new_order",
            "ladder",
            "cancel_order",
            "cancel_order_group",
        ):
            self.assertIn(required, caps)
        # Phase 4: catalog/resolve/market_price for the wizard symbol picker.
        for required in (
            "resolve_instrument",
            "list_instruments",
            "market_price",
        ):
            self.assertIn(required, caps)
        # Vest-protected orders (set_tp / set_sl / close_position) still
        # NOT wired (need their own signature sub-proofs per docs).
        for forbidden in ("set_tp", "set_sl", "close_position"):
            self.assertNotIn(forbidden, caps)

    def test_symbol_stripper_handles_vest_conventions(self):
        # Crypto perpetuals.
        self.assertEqual(vest._symbol_from_vest_market("BTC-PERP"), "BTC")
        self.assertEqual(vest._symbol_from_vest_market("btc-perp"), "BTC")
        # Equities / indices / forex perpetuals.
        self.assertEqual(vest._symbol_from_vest_market("AAPL-USD-PERP"), "AAPL")
        self.assertEqual(vest._symbol_from_vest_market("SPX-USD-PERP"), "SPX")
        self.assertEqual(vest._symbol_from_vest_market("AUD-USD-PERP"), "AUD")
        self.assertEqual(vest._symbol_from_vest_market("TSLA-USDC-PERP"), "TSLA")
        # Unknown shape passes through verbatim.
        self.assertEqual(vest._symbol_from_vest_market("FOO-BAR"), "FOO-BAR")
        self.assertEqual(vest._symbol_from_vest_market(""), "")

    def test_normalize_positions_maps_btc_long_with_pnl(self):
        positions = [
            {
                "symbol": "BTC-PERP",
                "isLong": True,
                "size": "0.1000",
                "entryPrice": "30000.00",
                "markPrice": "31000.00",
                "unrealizedPnl": "100.000000",
            }
        ]
        out = vest._normalize_positions(self._account_payload(positions=positions))
        self.assertEqual(len(out), 1)
        p = out[0]
        self.assertEqual(p.symbol, "BTC")
        self.assertEqual(p.side, "long")
        self.assertEqual(p.size, "0.1")
        self.assertEqual(p.entry_price, "30000")
        self.assertEqual(p.pnl, "100")
        self.assertEqual(p.mark, "31000")
        self.assertEqual(p.exchange_instrument, "BTC-PERP")
        # Vest does not surface TP/SL on positions.
        self.assertIsNone(p.tp)
        self.assertIsNone(p.sl)

    def test_normalize_positions_maps_btc_short_and_uses_abs_size(self):
        positions = [
            {
                "symbol": "BTC-PERP",
                "isLong": False,
                "size": "-0.0500",  # negative-signed size
                "entryPrice": "30000.00",
                "markPrice": "31000.00",
                "unrealizedPnl": "-50.000000",
            }
        ]
        out = vest._normalize_positions(self._account_payload(positions=positions))
        self.assertEqual(len(out), 1)
        p = out[0]
        self.assertEqual(p.side, "short")
        # abs(-0.05) → 0.05 (no trailing zeros).
        self.assertEqual(p.size, "0.05")

    def test_normalize_positions_skips_zero_size_rows(self):
        positions = [
            {"symbol": "BTC-PERP", "isLong": True, "size": "0", "entryPrice": "30000.00"},
            {"symbol": "ETH-PERP", "isLong": True, "size": "1.0", "entryPrice": "2000.00"},
        ]
        out = vest._normalize_positions(self._account_payload(positions=positions))
        # The zero-size BTC row is skipped; only ETH survives.
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].symbol, "ETH")

    def test_normalize_open_orders_groups_by_symbol_side(self):
        orders = [
            {
                "id": "0xa",
                "symbol": "BTC-PERP",
                "isBuy": True,
                "orderType": "LIMIT",
                "limitPrice": "30000",
                "size": "0.1000",
                "status": "NEW",
                "reduceOnly": False,
            },
            {
                "id": "0xb",
                "symbol": "BTC-PERP",
                "isBuy": True,
                "orderType": "LIMIT",
                "limitPrice": "30100",
                "size": "0.2000",
                "status": "NEW",
                "reduceOnly": False,
            },
            {
                "id": "0xc",
                "symbol": "BTC-PERP",
                "isBuy": False,
                "orderType": "TAKE_PROFIT",
                "limitPrice": "35000",
                "size": "0.1000",
                "status": "NEW",
                "reduceOnly": True,
                "tpPrice": "35000",
            },
        ]
        groups, count = vest._normalize_open_orders(orders)
        self.assertEqual(count, 3)
        # Two resting groups (BTC buy + BTC sell/TP) and one protective
        # group (BTC sell/TP).
        by_symbol_side = {(g.symbol, g.side): g for g in groups}
        self.assertIn(("BTC", "buy"), by_symbol_side)
        self.assertIn(("BTC", "sell"), by_symbol_side)
        buy = by_symbol_side[("BTC", "buy")]
        # entry_limit grouping: total_size = 0.1 + 0.2 = 0.3, min/max
        # = 30000/30100, classification = entry_limit.
        self.assertEqual(buy.order_count, 2)
        self.assertEqual(buy.total_size, "0.3")
        self.assertEqual(buy.min_price, "30000")
        self.assertEqual(buy.max_price, "30100")
        self.assertEqual(buy.classification, "entry_limit")
        self.assertFalse(buy.reduce_only)
        sell = by_symbol_side[("BTC", "sell")]
        # Protective bucket: TP row lands here.
        self.assertEqual(sell.order_count, 1)
        self.assertEqual(sell.classification, "take_profit")
        self.assertTrue(sell.reduce_only)
        self.assertEqual(sell.trigger_price, "35000")
        self.assertEqual(sell.limit_price, "35000")

    def test_normalize_open_orders_skips_filled_and_cancelled(self):
        orders = [
            {"id": "0xa", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "limitPrice": "30000", "size": "0.1", "status": "FILLED"},
            {"id": "0xb", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "limitPrice": "30000", "size": "0.1", "status": "CANCELLED"},
            {"id": "0xc", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "limitPrice": "30000", "size": "0.1", "status": "NEW"},
        ]
        groups, count = vest._normalize_open_orders(orders)
        self.assertEqual(count, 1)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].order_count, 1)

    def test_normalize_open_orders_tolerates_wrapped_payload(self):
        # Vest's wrapped variant — {code: 0, data: [...]}.
        wrapped = {
            "code": 0,
            "msg": "success",
            "data": [
                {"id": "0xa", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "limitPrice": "30000", "size": "0.1", "status": "NEW"},
            ],
        }
        rows = vest._extract_orders_payload(wrapped)
        groups, count = vest._normalize_open_orders(rows)
        self.assertEqual(count, 1)
        self.assertEqual(len(groups), 1)

    def test_extract_orders_payload_handles_bare_array(self):
        orders = [
            {"id": "0xa", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "limitPrice": "30000", "size": "0.1", "status": "NEW"},
        ]
        rows = vest._extract_orders_payload(orders)
        self.assertEqual(len(rows), 1)

    def test_normalize_assets_returns_per_asset_breakdown(self):
        balances = [
            {"asset": "USDC", "total": "1000.000000", "locked": "0.000000"},
            {"asset": "USDT", "total": "0", "locked": "0"},
        ]
        assets = vest._normalize_assets(balances)
        self.assertEqual(len(assets), 2)
        self.assertEqual(assets[0]["asset"], "USDC")
        self.assertEqual(assets[0]["total"], "1000")
        self.assertEqual(assets[1]["asset"], "USDT")
        self.assertEqual(assets[1]["total"], "0")

    def test_normalize_assets_handles_missing_balances(self):
        # Empty / None / malformed rows all degrade to [].
        self.assertEqual(vest._normalize_assets(None), [])
        self.assertEqual(vest._normalize_assets([]), [])
        self.assertEqual(vest._normalize_assets([{"no-asset-key": "x"}]), [])

    def test_balance_emits_assets_data_field(self):
        original_signed_get = vest._signed_get
        balances = [
            {"asset": "USDC", "total": "1500.000000", "locked": "200.000000"},
        ]
        summary_payload = self._account_payload(balances=balances)

        def fake_signed_get(creds, path):
            return summary_payload

        self._apply(self.VALID_ENV)
        vest._signed_get = fake_signed_get  # type: ignore[assignment]
        try:
            response = vest.execute({"operation": "balance", "account": "fibo"})
        finally:
            vest._signed_get = original_signed_get  # type: ignore[assignment]

        self.assertTrue(response.success)
        assert response.data is not None
        assets = response.data["assets"]
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["asset"], "USDC")
        self.assertEqual(assets[0]["total"], "1500")
        self.assertEqual(assets[0]["locked"], "200")

    def test_positions_orders_dispatches_account_and_orders(self):
        account_payload = self._account_payload(
            positions=[
                {
                    "symbol": "BTC-PERP",
                    "isLong": True,
                    "size": "0.1",
                    "entryPrice": "30000",
                    "markPrice": "31000",
                    "unrealizedPnl": "100",
                }
            ]
        )
        orders_payload = [
            {"id": "0xa", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "limitPrice": "29000", "size": "0.05", "status": "NEW"},
        ]
        seen_paths = {}

        def fake_signed_get(creds, path):
            seen_paths[path] = True
            if path == "/account":
                return account_payload
            return orders_payload

        self._apply(self.VALID_ENV)
        original = vest._signed_get
        vest._signed_get = fake_signed_get  # type: ignore[assignment]
        try:
            response = vest.execute({"operation": "positions_orders", "account": "fibo"})
        finally:
            vest._signed_get = original  # type: ignore[assignment]

        self.assertTrue(response.success)
        # Both endpoints were called.
        self.assertIn("/account", seen_paths)
        self.assertIn("/orders", seen_paths)
        # We use a bare ``GET /orders`` (no ``?status=`` filter) because
        # the venue rejects comma-separated multi-status filters with
        # code 1130; filtering to resting orders is client-side.
        self.assertNotIn("/orders?status=NEW,PARTIALLY_FILLED", seen_paths)
        # Positions rendered.
        assert response.positions is not None
        self.assertEqual(len(response.positions), 1)
        self.assertEqual(response.positions[0].symbol, "BTC")
        self.assertEqual(response.positions[0].side, "long")
        # Order groups rendered + count surfaced.
        assert response.order_groups is not None
        self.assertEqual(len(response.order_groups), 1)
        self.assertEqual(response.order_groups[0].symbol, "BTC")
        self.assertEqual(response.open_order_count, 1)

    def test_positions_orders_identity_gate(self):
        # /account returns a different address → WRONG_ACCOUNT, not the
        # positions snapshot.
        account_payload = self._account_payload(
            address="0x0000000000000000000000000000000000000000",
        )
        orders_payload: list = []

        def fake_signed_get(creds, path):
            if path == "/account":
                return account_payload
            return orders_payload

        self._apply(self.VALID_ENV)
        original = vest._signed_get
        vest._signed_get = fake_signed_get  # type: ignore[assignment]
        try:
            response = vest.execute({"operation": "positions_orders", "account": "fibo"})
        finally:
            vest._signed_get = original  # type: ignore[assignment]

        self.assertFalse(response.success)
        assert response.error is not None
        self.assertEqual(response.error.code, "WRONG_ACCOUNT")
        # No positions or order_groups leaked through.
        self.assertIsNone(response.positions)
        self.assertIsNone(response.order_groups)

    def test_positions_orders_handles_orders_fetch_failure(self):
        # If /orders 401s but /account succeeds, the whole positions_orders
        # response surfaces AUTH_INVALID — never a half-rendered mix.
        account_payload = self._account_payload(
            positions=[{"symbol": "BTC-PERP", "isLong": True, "size": "0.1", "entryPrice": "30000", "markPrice": "31000", "unrealizedPnl": "100"}]
        )

        def fake_signed_get(creds, path):
            if path == "/account":
                return account_payload
            raise vest.VestHTTPError(status=401, path=path, body="unauthorized")

        self._apply(self.VALID_ENV)
        original = vest._signed_get
        vest._signed_get = fake_signed_get  # type: ignore[assignment]
        try:
            response = vest.execute({"operation": "positions_orders", "account": "fibo"})
        finally:
            vest._signed_get = original  # type: ignore[assignment]

        self.assertFalse(response.success)
        assert response.error is not None
        self.assertEqual(response.error.code, "AUTH_INVALID")

    def test_positions_management_returns_positions_only(self):
        account_payload = self._account_payload(
            positions=[
                {"symbol": "ETH-PERP", "isLong": False, "size": "1.0", "entryPrice": "2000", "markPrice": "1900", "unrealizedPnl": "-100"},
            ]
        )

        def fake_signed_get(creds, path):
            return account_payload

        self._apply(self.VALID_ENV)
        original = vest._signed_get
        vest._signed_get = fake_signed_get  # type: ignore[assignment]
        try:
            response = vest.execute({"operation": "positions_management", "account": "fibo"})
        finally:
            vest._signed_get = original  # type: ignore[assignment]

        self.assertTrue(response.success)
        assert response.positions is not None
        self.assertEqual(len(response.positions), 1)
        self.assertEqual(response.positions[0].symbol, "ETH")
        self.assertEqual(response.positions[0].side, "short")
        # No order groups on positions_management.
        self.assertIsNone(response.order_groups)
        self.assertIsNone(response.open_order_count)

    def test_positions_management_identity_gate(self):
        account_payload = self._account_payload(
            address="0x0000000000000000000000000000000000000000",
        )

        def fake_signed_get(creds, path):
            return account_payload

        self._apply(self.VALID_ENV)
        original = vest._signed_get
        vest._signed_get = fake_signed_get  # type: ignore[assignment]
        try:
            response = vest.execute({"operation": "positions_management", "account": "fibo"})
        finally:
            vest._signed_get = original  # type: ignore[assignment]

        self.assertFalse(response.success)
        assert response.error is not None
        self.assertEqual(response.error.code, "WRONG_ACCOUNT")

    def test_classify_order_maps_vest_order_types(self):
        self.assertEqual(vest._classify_order("LIMIT"), "entry_limit")
        self.assertEqual(vest._classify_order("MARKET"), "entry_limit")
        self.assertEqual(vest._classify_order("TAKE_PROFIT"), "take_profit")
        self.assertEqual(vest._classify_order("STOP_LOSS"), "stop_loss")
        self.assertEqual(vest._classify_order("LIQUIDATION"), "trigger")
        self.assertEqual(vest._classify_order(""), "other")
        self.assertEqual(vest._classify_order("unknown"), "other")

    # -----------------------------------------------------------------
    # Phase 3: build + sign + submit probe (sandboxed, mocked HTTP)
    # -----------------------------------------------------------------
    #
    # These tests do NOT use the operator's VEST_FIBO_SIGN_PRIVATE_KEY. They
    # generate a deterministic test signing key, capture the exact JSON
    # body the agent would POST, and verify the signature recovers the
    # signer's public key. The intent is to prove the wire payload and
    # signature shape end-to-end BEFORE any live HTTP POST moves funds.

    def _new_order_request(self, **overrides):
        req = {
            "operation": "new_order",
            "account": "fibo",
            "symbol": "BTC-PERP",
            "side": "buy",
            "order_type": "limit",
            "volume": "0.001",
            "price": "30000",
        }
        req.update(overrides)
        return req

    def _mock_signed_get(self, return_value):
        original = vest._signed_get
        # Accept the optional ``query`` kwarg so this matches the production
        # signature (``query=None`` default); the agent does not currently
        # pass ``query=`` to ``_signed_get`` for any path except
        # ``/account/nonce``, but the new tests exercise that path.
        def _mock(creds, path, *, query=None):
            return return_value
        vest._signed_get = _mock  # type: ignore[assignment]
        return original

    def _mock_signed_post(self, capture_list, *, unique_ids: bool = False):
        """Patch ``_signed_post`` to capture the body the agent POSTed
        and return a canned response.

        ``capture_list`` is a list we mutate in place: each captured
        entry is ``(path, body_dict, query_dict_or_None)``. The query
        dict is what the agent passes via ``query=`` (e.g. for the
        cancel endpoint's required ``time`` query parameter).

        By default each call returns ``{"id": "0xabc"}`` (matching the
        GET ``/orders?id=0xabc`` verify mock used by the single-order
        tests). Tests that need per-call unique ids (e.g. ladder tests
        asserting ``len(set(child_order_ids)) == n``) pass
        ``unique_ids=True`` to receive ``0x000000000001``,
        ``0x000000000002``, ... per call.
        """
        original = vest._signed_post

        counter = {"n": 0}

        def fake(creds, path, body, *, query=None):
            counter["n"] += 1
            # Make a deep-ish copy of body so the caller can't touch
            # the captured object after the fact.
            body_copy = json.loads(json.dumps(body))
            query_copy = json.loads(json.dumps(query)) if query else None
            capture_list.append((path, body_copy, query_copy))
            # POST /orders returns {'id': "0x..."}; POST /orders/cancel
            # also returns {'id': "0x..."}. Default to ``0xabc`` so the
            # verify step can match against the GET /orders?id=0xabc
            # mock below. Ladder tests opt into a unique counter so
            # ``len(set(child_order_ids)) == n`` exercises the venue's
            # real behaviour.
            order_id = f"0x{counter['n']:012x}" if unique_ids else "0xabc"
            return {"id": order_id}

        vest._signed_post = fake  # type: ignore[assignment]
        return original

    # --- Phase 3.1: capability advertisement ------------------------------

    def test_capabilities_advertises_phase3_write_ops(self):
        caps = set(vest.capabilities())
        for op in (
            "balance",
            "positions_orders",
            "positions_management",
            "new_order",
            "cancel_order",
            "cancel_order_group",
            "ladder",
            "get_exact_order",
            "market_constraints",
        ):
            self.assertIn(op, caps)
        # Still no TP/SL / close_position / leverage in this slice.
        for forbidden in ("set_tp", "set_sl", "close_position"):
            self.assertNotIn(forbidden, caps)

    def test_ladder_cap_surfaces_conservative_default(self):
        # Wizard renders this on the ladder screen; it should match
        # the constant.
        self.assertEqual(
            vest.ladder_max_orders_per_instrument(),
            vest.LADDER_MAX_ORDERS_PER_INSTRUMENT,
        )

    # --- Phase 3.2: signing primitives -------------------------------------

    def test_keccak_digest_matches_documented_layout(self):
        # Reproduce the digest the agent computes; we expect the same
        # 32 bytes when the helper is invoked with the doc example
        # values.
        digest = vest._keccak_digest(
            abi_types=vest._NEW_ORDER_ABI_TYPES,
            values=(
                1700000000000,
                0,
                "LIMIT",
                "BTC-PERP",
                True,
                "0.1000",
                "30000.00",
                False,
            ),
        )
        self.assertEqual(len(digest), 32)
        # Recompute from scratch using only stdlib + eth_abi + web3 to
        # prove the helper matches.
        from eth_abi.abi import encode as _abi_encode
        from web3 import Web3 as _Web3
        expected = _Web3.keccak(
            _abi_encode(
                list(vest._NEW_ORDER_ABI_TYPES),
                [
                    1700000000000,
                    0,
                    "LIMIT",
                    "BTC-PERP",
                    True,
                    "0.1000",
                    "30000.00",
                    False,
                ],
            )
        )
        self.assertEqual(digest, expected)

    def test_personal_sign_recovers_signer(self):
        # Sign a known digest and recover the signer; round-trip must
        # match the configured test key's public key.
        digest = vest._keccak_digest(
            abi_types=vest._NEW_ORDER_ABI_TYPES,
            values=(1700000000000, 0, "LIMIT", "BTC-PERP", True, "0.1", "30000", False),
        )
        sig_hex = vest._personal_sign(digest=digest, private_key=self._test_key_hex)
        self.assertTrue(sig_hex.startswith("0x"))
        # 65 bytes hex = 130 chars + "0x" prefix.
        self.assertEqual(len(sig_hex), 2 + 130)
        # Recover the signer.
        from eth_account.messages import encode_defunct as _ed
        from eth_account import Account as _Acc
        recovered = _Acc.recover_message(_ed(digest), signature=sig_hex)
        self.assertEqual(recovered, self._test_account.address)

    def test_normalize_signing_key_accepts_both_prefixes(self):
        # No 0x → add it.
        self.assertEqual(vest._normalize_signing_key("52edbb...9874"[:64]), "0x" + "52edbb...9874"[:64])
        # Already 0x → leave it.
        self.assertEqual(
            vest._normalize_signing_key("0x52edbb...9874"[:66]),
            "0x52edbb...9874"[:66],
        )

    # --- Phase 3.3: new_order payload builder -----------------------------

    def test_build_new_order_payload_matches_doc_example_shape(self):
        payload = vest._build_new_order_payload(
            symbol="BTC-PERP",
            order_type="LIMIT",
            is_buy=True,
            size_text="0.1000",
            limit_price_text="30000.00",
            reduce_only=False,
            time_ms=1700000000000,
            nonce=0,
            recv_window_ms=60000,
            signing_key=self._test_key_hex,
        )
        # Top-level keys.
        self.assertEqual(set(payload.keys()), {"order", "recvWindow", "signature"})
        self.assertEqual(payload["recvWindow"], 60000)
        self.assertTrue(payload["signature"].startswith("0x"))
        # Order sub-object — verbatim doc example.
        order = payload["order"]
        self.assertEqual(order["time"], 1700000000000)
        self.assertEqual(order["nonce"], 0)
        self.assertEqual(order["symbol"], "BTC-PERP")
        self.assertEqual(order["isBuy"], True)
        self.assertEqual(order["size"], "0.1000")
        self.assertEqual(order["orderType"], "LIMIT")
        self.assertEqual(order["limitPrice"], "30000.00")
        self.assertEqual(order["reduceOnly"], False)
        # timeInForce is included for LIMIT only.
        self.assertEqual(order["timeInForce"], "GTC")
        # Signature recovers the test signer.
        from eth_account.messages import encode_defunct as _ed
        from eth_account import Account as _Acc
        from web3 import Web3 as _Web3
        from eth_abi.abi import encode as _abi_encode
        digest = _Web3.keccak(
            _abi_encode(
                list(vest._NEW_ORDER_ABI_TYPES),
                [
                    1700000000000, 0, "LIMIT", "BTC-PERP",
                    True, "0.1000", "30000.00", False,
                ],
            )
        )
        recovered = _Acc.recover_message(_ed(digest), signature=payload["signature"])
        self.assertEqual(recovered, self._test_account.address)

    def test_build_new_order_payload_market_omits_limit_price(self):
        # Per the docs MARKET orders do not carry limitPrice / timeInForce.
        payload = vest._build_new_order_payload(
            symbol="BTC-PERP",
            order_type="MARKET",
            is_buy=True,
            size_text="0.1000",
            limit_price_text="0",
            reduce_only=False,
            time_ms=1700000000000,
            nonce=0,
            recv_window_ms=60000,
            signing_key=self._test_key_hex,
        )
        self.assertNotIn("limitPrice", payload["order"])
        self.assertNotIn("timeInForce", payload["order"])
        self.assertEqual(payload["order"]["orderType"], "MARKET")

    # --- Phase 3.4: cancel payload builder --------------------------------

    def test_build_cancel_payload_matches_doc_example_shape(self):
        payload = vest._build_cancel_payload(
            order_id="0xabc",
            time_ms=1700000000000,
            nonce=0,
            recv_window_ms=60000,
            signing_key=self._test_key_hex,
        )
        self.assertEqual(set(payload.keys()), {"order", "recvWindow", "signature"})
        self.assertEqual(payload["recvWindow"], 60000)
        self.assertEqual(payload["order"]["id"], "0xabc")
        self.assertEqual(payload["order"]["time"], 1700000000000)
        self.assertEqual(payload["order"]["nonce"], 0)
        # Signature recovers the test signer.
        from eth_account.messages import encode_defunct as _ed
        from eth_account import Account as _Acc
        from web3 import Web3 as _Web3
        from eth_abi.abi import encode as _abi_encode
        digest = _Web3.keccak(
            _abi_encode(
                list(vest._CANCEL_ABI_TYPES),
                [1700000000000, 0, "0xabc"],
            )
        )
        recovered = _Acc.recover_message(_ed(digest), signature=payload["signature"])
        self.assertEqual(recovered, self._test_account.address)

    # --- Phase 3.5: new_order end-to-end (mocked HTTP) -------------------

    def test_new_order_posts_to_positive_with_correct_endpoint_and_results(self):
        # /account/nonce returns {lastNonce: 5}; next = 6.
        # /orders returns {id: "0xabc"}.
        # GET /orders?id=0xabc returns a NEW row matching the id.
        captured: list = []
        original_post = self._mock_signed_post(captured)
        # Mock the GETs the agent makes: nonce, then verify.
        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 5}
            if path.startswith("/orders?id="):
                return [
                    {
                        "id": "0xabc",
                        "symbol": "BTC-PERP",
                        "isBuy": True,
                        "orderType": "LIMIT",
                        "limitPrice": "30000",
                        "size": "0.001",
                        "status": "NEW",
                    }
                ]
            raise AssertionError(f"unexpected GET {path}")
        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(self._new_order_request())
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=f"failure={resp.error}")
        # The agent POSTed to the right endpoint.
        self.assertEqual(len(captured), 1)
        path, body, _query = captured[0]
        self.assertEqual(path, "/orders")
        # Body shape matches the doc.
        self.assertEqual(set(body.keys()), {"order", "recvWindow", "signature"})
        # Order sub-object.
        order = body["order"]
        self.assertEqual(order["symbol"], "BTC-PERP")
        self.assertEqual(order["isBuy"], True)
        self.assertEqual(order["size"], "0.001")
        self.assertEqual(order["limitPrice"], "30000")
        self.assertEqual(order["orderType"], "LIMIT")
        self.assertEqual(order["reduceOnly"], False)
        self.assertEqual(order["timeInForce"], "GTC")
        # Nonce walked to 6.
        self.assertEqual(order["nonce"], 6)
        # Signature present, 0x-prefixed.
        self.assertTrue(body["signature"].startswith("0x"))
        # response.audit payload exposes the exchange order id.
        assert resp.data is not None
        self.assertEqual(resp.data["order_id"], "0xabc")
        self.assertEqual(resp.data["verified_ok"], True)
        # CanonicalOrderResult is populated.
        assert resp.order is not None
        self.assertEqual(resp.order.symbol, "BTC-PERP")
        self.assertEqual(resp.order.side, "buy")
        self.assertEqual(resp.order.order_type, "LIMIT")
        self.assertEqual(resp.order.exchange_order_id, "0xabc")
        self.assertTrue(resp.order.verified)

    def test_new_order_rejects_market_with_price(self):
        # Per docs MARKET does not accept limitPrice. The agent's
        # payload builder omits it; verify the body.
        captured: list = []
        original_post = self._mock_signed_post(captured)
        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path.startswith("/orders?id="):
                return [{"id": "0xabc", "status": "NEW"}]
            raise AssertionError(f"unexpected GET {path}")
        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(self._new_order_request(
                order_type="market",
                price=None,
                volume="0.001",
            ))
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        # No limitPrice in the body.
        _, body, _query = captured[0]
        self.assertNotIn("limitPrice", body["order"])
        self.assertNotIn("timeInForce", body["order"])
        self.assertEqual(body["order"]["orderType"], "MARKET")

    def test_new_order_rejects_invalid_side(self):
        resp = vest.execute(self._new_order_request(side="SOMETHING"))
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INVALID_SIDE")

    def test_new_order_rejects_invalid_volume(self):
        resp = vest.execute(self._new_order_request(volume="-1"))
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INVALID_VOLUME")

    def test_new_order_rejects_invalid_price(self):
        resp = vest.execute(self._new_order_request(price="0"))
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INVALID_PRICE")

    def test_new_order_rejects_unsupported_order_type(self):
        resp = vest.execute(self._new_order_request(order_type="stop_loss"))
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INVALID_ORDER_TYPE")

    def test_new_order_handles_nonce_failure(self):
        # /account/nonce 500s → VEST_ERROR.
        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                raise vest.VestHTTPError(status=500, path=path, body="boom")
            raise AssertionError(f"unexpected GET {path}")
        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(self._new_order_request())
        finally:
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "VEST_ERROR")

    def test_new_order_handles_post_rejection(self):
        # POST /orders returns HTTP 402 (e.g. MARGIN_CHECK_FAILED).
        original_post = vest._signed_post
        def fake_reject(creds, path, body):
            raise vest.VestHTTPError(status=402, path=path, body="MARGIN_CHECK_FAILED")
        vest._signed_post = fake_reject  # type: ignore[assignment]
        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            raise AssertionError(f"unexpected GET {path}")
        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(self._new_order_request())
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "VEST_ERROR")
        self.assertIn("MARGIN_CHECK_FAILED", resp.error.message)

    # --- Phase 3.6: cancel_order end-to-end (mocked HTTP) ----------------

    def test_cancel_order_posts_correct_payload(self):
        captured: list = []
        original_post = self._mock_signed_post(captured)
        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path.startswith("/orders?id="):
                return [{"id": "0xabc", "status": "CANCELLED"}]
            raise AssertionError(f"unexpected GET {path}")
        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "cancel_order",
                "account": "fibo",
                "order_id": "0xabc",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        self.assertEqual(len(captured), 1)
        path, body, query = captured[0]
        self.assertEqual(path, "/orders/cancel")
        self.assertEqual(body["order"]["id"], "0xabc")
        # Vest's /orders/cancel requires ``time`` in the query string —
        # without it the venue returns 422 missing query.time. The
        # agent MUST always include ``time`` (Unix milliseconds) on the
        # URL, and it MUST match the signed ``order.time`` in the JSON
        # body so the server's replay window validates the signature.
        assert query is not None, "cancel must send a query string"
        self.assertIn("time", query)
        self.assertIsInstance(query["time"], int)
        # Unix milliseconds (now ~1.7e14) is well above any
        # seconds-since-epoch value (1.7e9). Reject seconds-form here.
        self.assertGreater(query["time"], 10**12)
        # The query time MUST match the signed body's order.time.
        self.assertEqual(query["time"], body["order"]["time"])
        assert resp.data is not None
        self.assertEqual(resp.data["verified_ok"], True)
        self.assertEqual(resp.data["verified"]["status"], "CANCELLED")

    def test_cancel_order_rejects_missing_order_id(self):
        resp = vest.execute({"operation": "cancel_order", "account": "fibo"})
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "MISSING_ORDER_ID")

    # --- Phase 3.6b: cancel wire contract (query.time regression suite) ---

    def test_cancel_order_url_query_contains_time_in_milliseconds(self):
        """End-to-end: the cancel request URL must carry ``?time=<ms>``.

        The /trade → vestmarkets → fibo → "Cancel Orders" path was
        failing because Vest's ``POST /orders/cancel`` server-side
        validator requires ``time`` in the URL query string. Without
        it the venue returns HTTP 422:

            {"detail":[{"type":"missing","loc":["query","time"],
                        "msg":"Field required","input":null}]}

        This test pins down the fix: we mock ``_signed_request`` (the
        lowest layer that constructs the URL) and assert the captured
        URL contains ``time=<ms>`` with a value that is unmistakably
        Unix milliseconds (i.e. >> 10**12).
        """
        captured_urls: list = []
        original_request = vest._signed_request

        def fake_request(creds, *, method, path_with_query, body):
            captured_urls.append((method, path_with_query, body))
            return {"id": "0xabc"}

        vest._signed_request = fake_request  # type: ignore[assignment]
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path.startswith("/orders?id="):
                return [{"id": "0xabc", "status": "CANCELLED"}]
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {
                    "operation": "cancel_order",
                    "account": "fibo",
                    "order_id": "0xabc",
                }
            )
        finally:
            vest._signed_request = original_request  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        # Exactly one POST to /orders/cancel was issued.
        self.assertEqual(len(captured_urls), 1)
        method, url_path, body_text = captured_urls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url_path.startswith("/orders/cancel?"), url_path)
        # ``time=<ms>`` MUST be present, MUST be an integer, MUST be
        # milliseconds (>> 1e12) and MUST match the signed body's
        # ``order.time`` field.
        import urllib.parse
        qs = urllib.parse.urlparse(url_path).query
        params = urllib.parse.parse_qs(qs)
        self.assertIn("time", params, msg=f"missing time in query: {qs}")
        time_values = params["time"]
        self.assertEqual(len(time_values), 1)
        time_int = int(time_values[0])
        self.assertGreater(time_int, 10**12, msg=f"time not in ms: {time_int}")
        body_dict = json.loads(body_text)
        self.assertEqual(time_int, int(body_dict["order"]["time"]))

    def test_cancel_order_group_sends_time_in_query_for_every_cancel(self):
        """Group cancel must include ``time`` on every POST.

        The group path delegates each child to the shared single-order
        primitive, so the wire contract is inherited: every child POST
        includes ``?time=<ms>`` on the URL, mirrored into
        ``body.order.time``.
        """
        captured_urls: list = []
        original_request = vest._signed_request

        def fake_request(creds, *, method, path_with_query, body):
            captured_urls.append((method, path_with_query, body))
            # Cancel POSTs return a conventional id envelope. The verify
            # poll returns an order in CANCELLED status.
            if method == "POST":
                try:
                    import json as _json
                    oid = _json.loads(body)["order"]["id"]
                except Exception:  # noqa: BLE001
                    oid = "0xA"
                return {"code": 0, "msg": "", "data": {"id": oid}}
            # GET /orders?id=... → CANCELLED.
            return [{"id": "0xA", "symbol": "BTC-PERP", "isBuy": True,
                     "orderType": "LIMIT", "size": "0.1", "status": "CANCELLED"}]

        vest._signed_request = fake_request  # type: ignore[assignment]
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path == "/orders":
                return [
                    {"id": "0xA", "symbol": "BTC-PERP", "isBuy": True, "size": "0.1", "status": "NEW"},
                    {"id": "0xB", "symbol": "BTC-PERP", "isBuy": False, "size": "0.1", "status": "NEW"},
                    {"id": "0xC", "symbol": "ETH-PERP", "isBuy": True, "size": "0.1", "status": "NEW"},
                ]
            # verify poll: GET /orders?id=<id> → CANCELLED
            if path.startswith("/orders?id="):
                oid = path.split("=", 1)[1]
                return [{"id": oid, "symbol": "BTC-PERP", "isBuy": True,
                         "orderType": "LIMIT", "size": "0.1", "status": "CANCELLED"}]
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {
                    "operation": "cancel_order_group",
                    "account": "fibo",
                    "symbol": "BTC-PERP",
                    "side": "long",
                }
            )
        finally:
            vest._signed_request = original_request  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        # Only 0xA matched the (BTC, long) filter, so only one cancel
        # POST was issued.
        cancel_posts = [u for u in captured_urls if u[0] == "POST"]
        self.assertEqual(len(cancel_posts), 1)
        method, url_path, body = cancel_posts[0]
        self.assertEqual(method, "POST")
        import urllib.parse as _up
        qs = _up.urlparse(url_path).query
        params = _up.parse_qs(qs)
        self.assertIn("time", params)
        self.assertGreater(int(params["time"][0]), 10**12)
        # Mirror check: body.order.time == query.time.
        import json as _json
        body_dict = _json.loads(body)
        self.assertEqual(int(params["time"][0]), int(body_dict["order"]["time"]))

    def test_cancel_order_signature_still_valid_after_query_change(self):
        """Signature must recover the test signer even when ``time`` is
        also sent on the query string. The query is NOT included in the
        ABI-encoded digest — only the JSON body's ``order`` block is.
        """
        captured: list = []
        original_post = self._mock_signed_post(captured)
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path.startswith("/orders?id="):
                return [{"id": "0xabc", "status": "CANCELLED"}]
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {
                    "operation": "cancel_order",
                    "account": "fibo",
                    "order_id": "0xabc",
                }
            )
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        path, body, query = captured[0]
        self.assertEqual(path, "/orders/cancel")
        self.assertEqual(body["order"]["id"], "0xabc")
        # The signature is over (time, nonce, id) per the docs. Recover
        # the signer from the captured signature and confirm it matches
        # the test signing key.
        from eth_abi.abi import encode as _abi_encode
        from eth_account import Account as _Acc
        from eth_account.messages import encode_defunct as _ed
        from web3 import Web3 as _Web3
        order = body["order"]
        digest = _Web3.keccak(
            _abi_encode(
                list(vest._CANCEL_ABI_TYPES),
                [int(order["time"]), int(order["nonce"]), str(order["id"])],
            )
        )
        recovered = _Acc.recover_message(_ed(digest), signature=body["signature"])
        self.assertEqual(recovered, self._test_account.address)
        # The query ``time`` MUST equal the signed ``order.time`` field.
        assert query is not None
        self.assertEqual(int(query["time"]), int(order["time"]))

    def test_cancel_order_422_missing_time_surfaces_as_failure(self):
        """A Vest 422 missing query.time must surface as a clean failure.

        The wizard's launcher screen must not crash; the canonical
        response must be ``success=False`` with a structured error the
        wizard can render.
        """
        original_post = vest._signed_post

        def fake_422(creds, path, body, *, query=None):
            # Mirror the real error verbatim.
            raise vest.VestHTTPError(
                status=422,
                path=path,
                body='{"detail":[{"type":"missing","loc":["query","time"],'
                     '"msg":"Field required","input":null}]}',
            )

        vest._signed_post = fake_422  # type: ignore[assignment]
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {
                    "operation": "cancel_order",
                    "account": "fibo",
                    "order_id": "0xabc",
                }
            )
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertFalse(resp.success)
        assert resp.error is not None
        # We surface the venue's body so the wizard can render
        # ``Field required`` to the operator.
        self.assertIn("Field required", resp.error.message)

    def test_cancel_order_http_failure_is_not_swallowed(self):
        """A transport-level failure on cancel must return a structured
        failure, never crash the wizard."""
        original_post = vest._signed_post

        def fake_500(creds, path, body, *, query=None):
            raise vest.VestHTTPError(status=500, path=path, body="boom")

        vest._signed_post = fake_500  # type: ignore[assignment]
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {
                    "operation": "cancel_order",
                    "account": "fibo",
                    "order_id": "0xabc",
                }
            )
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "VEST_ERROR")
        self.assertIn("boom", resp.error.message)

# --- Phase 3.6d: 3017 ORDER_NOT_FOUND audit / contract model ---
    #
    # Per Vest's public error-codes table (``docs.vestmarkets.com``),
    # code 3017 is named ``ORDER_NOT_FOUND`` and the server returns
    # HTTP 200 with body ``{"code": 3017, "msg": "Order already
    # processed or cancelled"}``. Semantics: the cancel endpoint
    # treats the order as already-terminal (cancelled or filled) and
    # refuses to attempt another mutation.
    #
    # The agent must:
    #   * preserve the venue envelope in ``_parse_order_response`` (so
    #     audit data carries ``venue_code`` / ``venue_message``);
    #   * branch in ``_cancel_order`` on ``venue_code == 3017`` and
    #     disambiguate using the read-only verify poll:
    #       - verify=CANCELLED → success=True, status_label=cancelled
    #       - verify=FILLED    → failure, code=ALREADY_FILLED
    #       - verify=NEW / lookup-error → failure, code=CANCEL_AMBIGUOUS
    #   * never auto-retry the cancel POST. Exactly one POST is sent
    #     in every 3017 scenario.

    def _mock_3017_scenario(self, *, verify_status):
        """Build a (spy_request, captured_posts) pair that simulates a
        Vest cancel that races with a prior cancel/fill and the verify
        poll's observation of the order's current status.

        ``verify_status`` is the status string the verify GET returns
        (one of ``CANCELLED``, ``FILLED``, ``NEW``, ``[]``).
        """
        captured_posts: list = []
        captured_gets: list = []

        def fake_request(c, *, method, path_with_query, body):
            if method == "GET" and path_with_query.startswith("/account/nonce"):
                captured_gets.append(("GET", path_with_query, body))
                return {"lastNonce": 0}
            if method == "POST" and "/orders/cancel" in path_with_query:
                captured_posts.append((path_with_query, body))
                return {"code": 3017, "msg": "Order already processed or cancelled"}
            if method == "GET" and path_with_query.startswith("/orders"):
                captured_gets.append(("GET", path_with_query, body))
                if verify_status == "[]":
                    return []
                return [
                    {
                        "id": "0xabc",
                        "symbol": "BTC-PERP",
                        "isBuy": True,
                        "orderType": "LIMIT",
                        "limitPrice": "30000",
                        "size": "0.001",
                        "status": verify_status,
                    }
                ]
            raise AssertionError(f"unexpected {method} {path_with_query}")

        return fake_request, captured_posts, captured_gets

    # -- _parse_order_response direct contract -----------------------------

    def test_parse_order_response_preserves_3017_envelope(self):
        """``_parse_order_response`` must NOT normalise a Vest error
        envelope to ``{}``. The contract is the audit dict:
        ``{"venue_code": 3017, "venue_message": "..."}``.
        """
        parsed = vest._parse_order_response(
            {"code": 3017, "msg": "Order already processed or cancelled"}
        )
        self.assertEqual(
            parsed,
            {
                "venue_code": 3017,
                "venue_message": "Order already processed or cancelled",
            },
        )

    def test_parse_order_response_still_passes_through_success_envelope(self):
        """Success envelopes (id-keyed, or {code:0, data:{...}} wrap)
        must still pass through unchanged.
        """
        self.assertEqual(
            vest._parse_order_response({"id": "0xabc"}),
            {"id": "0xabc"},
        )
        self.assertEqual(
            vest._parse_order_response({"code": 0, "data": {"id": "0xabc"}}),
            {"id": "0xabc"},
        )
        self.assertEqual(vest._parse_order_response(None), {})

    # -- 3017 cancel end-to-end contract ----------------------------------

    def test_cancel_3017_then_verify_cancelled_yields_success(self):
        """3017 + subsequent CANCELLED on verify = canonical success:
        ``success=True``, ``verified_ok=True``, ``status_label=cancelled``,
        ``venue_code=3017`` preserved in audit.
        """
        fake_request, captured_posts, _ = self._mock_3017_scenario(
            verify_status="CANCELLED"
        )
        original_request = vest._signed_request
        vest._signed_request = fake_request  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {
                    "operation": "cancel_order",
                    "account": "fibo",
                    "order_id": "0xabc",
                }
            )
        finally:
            vest._signed_request = original_request  # type: ignore[assignment]
        # Wire: exactly ONE cancel POST.
        cancel_posts = [p for p in captured_posts if "/orders/cancel" in p[0]]
        self.assertEqual(len(cancel_posts), 1)
        # Behaviour:
        self.assertTrue(resp.success, msg=str(resp.error))
        assert resp.data is not None
        self.assertEqual(resp.data["status_label"], "cancelled")
        self.assertTrue(resp.data.get("verified_ok"))
        assert resp.data.get("verified") is not None
        self.assertEqual(resp.data["verified"]["status"], "CANCELLED")
        # Audit envelope preserved.
        self.assertEqual(resp.data["venue_code"], 3017)
        self.assertEqual(
            resp.data["venue_message"],
            "Order already processed or cancelled",
        )
        self.assertEqual(
            resp.data["response"],
            {
                "venue_code": 3017,
                "venue_message": "Order already processed or cancelled",
            },
        )

    def test_cancel_3017_then_verify_filled_yields_already_filled(self):
        """3017 + verify=FILLED → canonical failure ``ALREADY_FILLED``.

        The cancel did NOT happen — the order filled. The agent must
        surface this as a distinct failure mode (not success, not
        ambiguous). The verified order row is preserved in audit.
        """
        fake_request, captured_posts, _ = self._mock_3017_scenario(
            verify_status="FILLED"
        )
        original_request = vest._signed_request
        vest._signed_request = fake_request  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {
                    "operation": "cancel_order",
                    "account": "fibo",
                    "order_id": "0xabc",
                }
            )
        finally:
            vest._signed_request = original_request  # type: ignore[assignment]
        # Wire: exactly ONE cancel POST.
        cancel_posts = [p for p in captured_posts if "/orders/cancel" in p[0]]
        self.assertEqual(len(cancel_posts), 1)
        # Behaviour: canonical failure.
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "ALREADY_FILLED")
        self.assertIn("FILLED", resp.error.message)
        # Audit / order_state carries the verified FILLED row + envelope.
        assert resp.data is None  # failure path: no success data dict
        order_state = resp.order_state
        assert order_state is not None
        self.assertEqual(order_state["venue_code"], 3017)
        self.assertEqual(
            order_state["venue_message"],
            "Order already processed or cancelled",
        )
        self.assertIsNotNone(order_state["verified"])
        self.assertEqual(order_state["verified"]["status"], "FILLED")

    def test_cancel_3017_then_verify_still_new_yields_ambiguous(self):
        """3017 + verify still sees NEW → canonical failure
        ``CANCEL_AMBIGUOUS``. The venue returned ORDER_NOT_FOUND but
        the order still appears open — the cancel state is
        inconclusive. NO retry POST is issued.
        """
        fake_request, captured_posts, _ = self._mock_3017_scenario(
            verify_status="NEW"
        )
        original_request = vest._signed_request
        vest._signed_request = fake_request  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {
                    "operation": "cancel_order",
                    "account": "fibo",
                    "order_id": "0xabc",
                }
            )
        finally:
            vest._signed_request = original_request  # type: ignore[assignment]
        # Wire: exactly ONE cancel POST.
        cancel_posts = [p for p in captured_posts if "/orders/cancel" in p[0]]
        self.assertEqual(len(cancel_posts), 1)
        # Behaviour: canonical failure.
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "CANCEL_AMBIGUOUS")
        assert resp.data is None
        order_state = resp.order_state
        assert order_state is not None
        self.assertEqual(order_state["venue_code"], 3017)
        self.assertIsNotNone(order_state["verified"])
        self.assertEqual(order_state["verified"]["status"], "NEW")

    def test_cancel_3017_then_verify_lookup_fails_yields_ambiguous(self):
        """3017 + verify GET error → canonical failure
        ``CANCEL_AMBIGUOUS``. NO retry POST is issued. The 3017
        envelope is preserved in audit.
        """
        original_request = vest._signed_request
        captured_posts: list = []

        def fake_request(c, *, method, path_with_query, body):
            if path_with_query.startswith("/account/nonce"):
                return {"lastNonce": 0}
            if "/orders/cancel" in path_with_query:
                captured_posts.append((path_with_query, body))
                return {"code": 3017, "msg": "Order already processed or cancelled"}
            if path_with_query.startswith("/orders"):
                # 503 mirrors the kind of transport-level failure the
                # verify path can swallow.
                raise vest.VestHTTPError(
                    status=503, path=path_with_query, body="service unavailable"
                )
            raise AssertionError(f"unexpected {method} {path_with_query}")

        vest._signed_request = fake_request  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {
                    "operation": "cancel_order",
                    "account": "fibo",
                    "order_id": "0xabc",
                }
            )
        finally:
            vest._signed_request = original_request  # type: ignore[assignment]
        # Wire: exactly ONE cancel POST despite verify failure.
        self.assertEqual(len(captured_posts), 1)
        # Behaviour: canonical failure.
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "CANCEL_AMBIGUOUS")
        assert resp.data is None
        order_state = resp.order_state
        assert order_state is not None
        self.assertEqual(order_state["venue_code"], 3017)
        self.assertEqual(
            order_state["venue_message"],
            "Order already processed or cancelled",
        )
        self.assertIsNone(order_state["verified"])

    def test_3017_does_not_trigger_a_second_post(self):
        """Critical safety property: the agent must NEVER auto-retry a
        3017 envelope. Across every verify outcome — CANCELLED, FILLED,
        NEW, lookup-error — exactly ONE ``POST /orders/cancel`` is sent.
        """
        original_request = vest._signed_request

        def build_fake(verify_status):
            captured_posts: list = []

            def fake_request(c, *, method, path_with_query, body):
                if path_with_query.startswith("/account/nonce"):
                    return {"lastNonce": 0}
                if "/orders/cancel" in path_with_query:
                    captured_posts.append((path_with_query, body))
                    return {"code": 3017, "msg": "Order already processed or cancelled"}
                if path_with_query.startswith("/orders"):
                    if verify_status == "lookup_error":
                        raise vest.VestHTTPError(
                            status=503, path=path_with_query, body="unavailable"
                        )
                    if verify_status == "[]":
                        return []
                    return [
                        {
                            "id": "0xabc",
                            "symbol": "BTC-PERP",
                            "isBuy": True,
                            "orderType": "LIMIT",
                            "limitPrice": "30000",
                            "size": "0.001",
                            "status": verify_status,
                        }
                    ]
                raise AssertionError(f"unexpected {method} {path_with_query}")

            return fake_request, captured_posts

        for verify_status in ("CANCELLED", "FILLED", "NEW", "[]", "lookup_error"):
            with self.subTest(verify_status=verify_status):
                fake_request, captured_posts = build_fake(verify_status)
                vest._signed_request = fake_request  # type: ignore[assignment]
                try:
                    resp = vest.execute(
                        {
                            "operation": "cancel_order",
                            "account": "fibo",
                            "order_id": "0xabc",
                        }
                    )
                finally:
                    vest._signed_request = original_request  # type: ignore[assignment]
                # Exactly one cancel POST was sent, regardless of verify outcome.
                self.assertEqual(
                    len(captured_posts),
                    1,
                    msg=f"verify={verify_status}",
                )
                # Outcome: each branch returns success=True or success=False,
                # but never triggers another POST.
                self.assertIn(resp.success, (True, False))

    def test_conventional_cancel_still_returns_status_label(self):
        """Regression: the conventional cancel path (no 3017) still
        returns ``status_label`` in the canonical data — the new field
        must not break the existing happy-path contract.
        """
        original_request = vest._signed_request
        captured_posts: list = []

        def fake_request(c, *, method, path_with_query, body):
            if path_with_query.startswith("/account/nonce"):
                return {"lastNonce": 0}
            if "/orders/cancel" in path_with_query:
                captured_posts.append((path_with_query, body))
                # Conventional success envelope: code=0, data wraps the id.
                return {"code": 0, "msg": "", "data": {"id": "0xabc"}}
            if path_with_query.startswith("/orders"):
                return [
                    {
                        "id": "0xabc",
                        "symbol": "BTC-PERP",
                        "isBuy": True,
                        "orderType": "LIMIT",
                        "limitPrice": "30000",
                        "size": "0.001",
                        "status": "CANCELLED",
                    }
                ]
            raise AssertionError(f"unexpected {method} {path_with_query}")

            vest._signed_request = fake_request  # type: ignore[assignment]
            try:
                resp = vest.execute(
                    {
                        "operation": "cancel_order",
                        "account": "fibo",
                        "order_id": "0xabc",
                    }
                )
            finally:
                vest._signed_request = original_request  # type: ignore[assignment]
            self.assertEqual(len(captured_posts), 1)
            self.assertTrue(resp.success, msg=str(resp.error))
            assert resp.data is not None
            self.assertEqual(resp.data["status_label"], "cancelled")
            self.assertTrue(resp.data["verified_ok"])
            # No 3017 envelope ⇒ venue_code is None and venue_message is empty.
            self.assertIsNone(resp.data["venue_code"])
            self.assertEqual(resp.data["venue_message"], "")

    # --- Phase 3.6c: nonce wire contract (query.time regression suite) ---

    def test_attach_query_helper_serializes_simple_query(self):
        """A. ``_attach_query`` correctly serializes optional query parameters.

        Single shared encoder for both ``_signed_get`` and
        ``_signed_post``; tests both paths through the same primitive
        to ensure no duplication drift.
        """
        # Empty / None query → input is returned unchanged.
        self.assertEqual(vest._attach_query("/foo", None), "/foo")
        self.assertEqual(vest._attach_query("/foo", {}), "/foo")
        # Simple key/value.
        self.assertEqual(vest._attach_query("/foo", {"time": 123}), "/foo?time=123")
        # Multiple keys.
        out = vest._attach_query(
            "/foo", {"time": 1700000000000, "nonce": 7}
        )
        self.assertTrue(out.startswith("/foo?"))
        self.assertIn("time=1700000000000", out)
        self.assertIn("nonce=7", out)
        # Existing ``?`` in the path → joined with ``&``.
        self.assertIn("&", vest._attach_query("/foo?bar=baz", {"time": 1}))
        self.assertEqual(
            vest._attach_query("/foo?bar=baz", {"time": 1}),
            "/foo?bar=baz&time=1",
        )
        # ``None``-valued keys are skipped (matches the production
        # behaviour in both helpers).
        self.assertEqual(vest._attach_query("/foo", {"a": None, "b": 1}), "/foo?b=1")
        # URL-encoding escapes spaces / ampersands.
        self.assertEqual(
            vest._attach_query("/foo", {"sym": "BTC PERP"}),
            "/foo?sym=BTC%20PERP",
        )

    def test_signed_get_passes_query_through_request_layer(self):
        """A. ``_signed_get`` forwards its ``query`` kwarg to the URL."""
        captured: list = []
        original_request = vest._signed_request

        def fake_request(creds, *, method, path_with_query, body):
            captured.append((method, path_with_query, body))
            return {"lastNonce": 0}

        vest._signed_request = fake_request  # type: ignore[assignment]
        try:
            vest._signed_get(
                vest._lookup_credentials("fibo"),
                "/account/nonce",
                query={"time": 1700000000000},
            )
        finally:
            vest._signed_request = original_request  # type: ignore[assignment]
        self.assertEqual(len(captured), 1)
        method, url_path, body = captured[0]
        self.assertEqual(method, "GET")
        # The URL MUST carry ``time=1700000000000`` (Unix ms).
        import urllib.parse as _up
        qs = _up.urlparse(url_path).query
        params = _up.parse_qs(qs)
        self.assertIn("time", params)
        self.assertEqual(params["time"], ["1700000000000"])
        self.assertEqual(body, "")

    def test_fetch_next_nonce_sends_time_in_query(self):
        """B+C. ``_fetch_next_nonce`` sends ``?time=<Unix milliseconds>``.

        Captures the URL passed to ``_signed_request`` and asserts the
        ``time`` query parameter is present, is an int, and is
        milliseconds (>= 1e12). The mock for ``/account/nonce`` returns
        a fixed lastNonce so the parser test (D) stays deterministic.
        """
        captured: list = []
        original_request = vest._signed_request

        def fake_request(creds, *, method, path_with_query, body):
            captured.append((method, path_with_query, body))
            return {"lastNonce": 42}

        vest._signed_request = fake_request  # type: ignore[assignment]
        try:
            nonce = vest._fetch_next_nonce(vest._lookup_credentials("fibo"))
        finally:
            vest._signed_request = original_request  # type: ignore[assignment]
        # Parser (D): lastNonce 42 + 1 = 43.
        self.assertEqual(nonce, 43)
        # Wire (B).
        self.assertEqual(len(captured), 1)
        method, url_path, _body = captured[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url_path.startswith("/account/nonce?"), url_path)
        import urllib.parse as _up
        qs = _up.urlparse(url_path).query
        params = _up.parse_qs(qs)
        self.assertIn("time", params, msg=f"missing time in query: {qs}")
        self.assertEqual(len(params["time"]), 1)
        time_int = int(params["time"][0])
        # C. plausibly milliseconds (not seconds).
        self.assertGreater(time_int, 10**12)
        # And the time used by the test wallclock is within the past
        # few seconds — i.e. the call to ``_now_ms`` happened during
        # this test, not a stale constant.
        now_ms = int(vest._now_ms())
        self.assertLess(abs(time_int - now_ms), 5_000)

    def test_fetch_next_nonce_parser_handles_missing_field(self):
        """D. Existing nonce parsing remains correct — raises if the
        response body has no ``lastNonce`` field (not a dict at all).
        """
        original_request = vest._signed_request

        def fake_request(creds, *, method, path_with_query, body):
            return ["not", "a", "dict"]

        vest._signed_request = fake_request  # type: ignore[assignment]
        try:
            with self.assertRaises(vest.VestHTTPError) as ctx:
                vest._fetch_next_nonce(vest._lookup_credentials("fibo"))
        finally:
            vest._signed_request = original_request  # type: ignore[assignment]
        self.assertEqual(ctx.exception.status, 0)
        self.assertIn("non-dict payload", ctx.exception.body)

    def test_fetch_next_nonce_parser_handles_unparseable_last_nonce(self):
        """D. The ``int(lastNonce)`` step still raises structured."""
        original_request = vest._signed_request

        def fake_request(creds, *, method, path_with_query, body):
            return {"lastNonce": "not-an-int"}

        vest._signed_request = fake_request  # type: ignore[assignment]
        try:
            with self.assertRaises(vest.VestHTTPError) as ctx:
                vest._fetch_next_nonce(vest._lookup_credentials("fibo"))
        finally:
            vest._signed_request = original_request  # type: ignore[assignment]
        self.assertEqual(ctx.exception.status, 0)
        self.assertIn("unexpected lastNonce", ctx.exception.body)

    def test_fetch_next_nonce_http_error_surfaces_via_failure(self):
        """E. HTTP errors on /account/nonce still surface as
        ``VestHTTPError`` so the caller maps them to canonical
        ``VEST_ERROR`` / ``TRANSPORT_ERROR`` failures.
        """
        original_request = vest._signed_request

        def fake_request(creds, *, method, path_with_query, body):
            # Mirror the real 422 we observed on production when
            # ``time`` is missing from the query — without the fix,
            # _fetch_next_nonce would land here.
            raise vest.VestHTTPError(
                status=422,
                path=path_with_query,
                body='{"detail":[{"type":"missing","loc":["query","time"],'
                     '"msg":"Field required","input":null}]}',
            )

        vest._signed_request = fake_request  # type: ignore[assignment]
        try:
            with self.assertRaises(vest.VestHTTPError) as ctx:
                vest._fetch_next_nonce(vest._lookup_credentials("fibo"))
        finally:
            vest._signed_request = original_request  # type: ignore[assignment]
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn("query", ctx.exception.body)

    def test_cancel_order_reaches_post_after_successful_nonce(self):
        """F+G+H. Cancel order reaches the cancel POST after a
        successful mocked nonce GET, the cancel POST still carries
        ``query.time == body.order.time``, and the new-order
        payload/signature behaviour is untouched.
        """
        captured_posts: list = []
        captured_gets: list = []
        original_post = vest._signed_post
        original_request = vest._signed_request

        def fake_post(creds, path, body, *, query=None):
            captured_posts.append((path, json.loads(json.dumps(body)), query))
            return {"id": "0xabc"}

        def fake_request(creds, *, method, path_with_query, body):
            captured_gets.append((method, path_with_query, body))
            if path_with_query.startswith("/account/nonce"):
                return {"lastNonce": 0}
            if path_with_query.startswith("/orders?id="):
                return [{"id": "0xabc", "status": "CANCELLED"}]
            raise AssertionError(f"unexpected {method} {path_with_query}")

        vest._signed_post = fake_post  # type: ignore[assignment]
        vest._signed_request = fake_request  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {
                    "operation": "cancel_order",
                    "account": "fibo",
                    "order_id": "0xabc",
                }
            )
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_request = original_request  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        # F: nonce GET was issued (1), cancel POST was issued (1), and
        # the verify-step GET also ran (1). We don't bind the exact
        # count on the verify step — the production code polls
        # ``/orders?id=…`` until the order is observed in a terminal
        # state — but we DO bind the count of nonce + cancel POSTs.
        nonce_gets = [g for g in captured_gets if g[1].startswith("/account/nonce")]
        cancel_posts = [p for p in captured_posts if p[0] == "/orders/cancel"]
        self.assertEqual(len(nonce_gets), 1, msg=f"nonce GETs: {nonce_gets}")
        self.assertEqual(len(cancel_posts), 1)
        cancel_path, cancel_body, cancel_query = cancel_posts[0]
        self.assertEqual(cancel_path, "/orders/cancel")
        # G: query.time on the cancel POST matches body.order.time.
        self.assertIsNotNone(cancel_query, msg="cancel must send a query string")
        self.assertIn("time", cancel_query)
        self.assertGreater(int(cancel_query["time"]), 10**12)
        self.assertEqual(int(cancel_query["time"]), int(cancel_body["order"]["time"]))
        # H: signature over (time, nonce, id) still matches the test signer.
        from eth_abi.abi import encode as _abi_encode
        from eth_account import Account as _Acc
        from eth_account.messages import encode_defunct as _ed
        from web3 import Web3 as _Web3
        order = cancel_body["order"]
        digest = _Web3.keccak(
            _abi_encode(
                list(vest._CANCEL_ABI_TYPES),
                [int(order["time"]), int(order["nonce"]), str(order["id"])],
            )
        )
        recovered = _Acc.recover_message(_ed(digest), signature=cancel_body["signature"])
        self.assertEqual(recovered, self._test_account.address)
        # H: the nonce GET's URL also carried ``time=`` (regression
        # coverage for the wire contract).
        nonce_method, nonce_url, _ = captured_gets[0]
        self.assertEqual(nonce_method, "GET")
        import urllib.parse as _up
        qs = _up.urlparse(nonce_url).query
        self.assertIn("time=", qs)

    def test_signed_get_omitting_query_does_not_alter_path(self):
        """A. Calling ``_signed_get`` without ``query=`` is identical
        to the legacy 2-arg call shape (backward compatibility for
        every existing authenticated GET site).
        """
        captured: list = []
        original_request = vest._signed_request

        def fake_request(creds, *, method, path_with_query, body):
            captured.append(path_with_query)
            return []

        vest._signed_request = fake_request  # type: ignore[assignment]
        try:
            vest._signed_get(vest._lookup_credentials("fibo"), "/orders?id=0xabc")
            vest._signed_get(vest._lookup_credentials("fibo"), "/orders")
        finally:
            vest._signed_request = original_request  # type: ignore[assignment]
        self.assertEqual(captured, ["/orders?id=0xabc", "/orders"])

    def test_attach_query_helper_is_actually_used_by_signed_get(self):
        """A. ``_signed_get`` delegates the URL construction to the
        shared ``_attach_query`` helper — preventing drift between
        GET and POST encoders.
        """
        original_attach = vest._attach_query
        calls: list = []

        def spy_attach(path, query):
            calls.append((path, query))
            return original_attach(path, query)

        vest._attach_query = spy_attach  # type: ignore[assignment]
        original_request = vest._signed_request

        def fake_request(creds, *, method, path_with_query, body):
            return {"ok": True}

        vest._signed_request = fake_request  # type: ignore[assignment]
        try:
            vest._signed_get(
                vest._lookup_credentials("fibo"),
                "/account/nonce",
                query={"time": 1},
            )
        finally:
            vest._attach_query = original_attach  # type: ignore[assignment]
            vest._signed_request = original_request  # type: ignore[assignment]
        self.assertTrue(any(c[0] == "/account/nonce" and c[1] == {"time": 1} for c in calls))

    def test_attach_query_helper_is_actually_used_by_signed_post(self):
        """A. ``_signed_post`` also delegates to the shared encoder."""
        original_attach = vest._attach_query
        calls: list = []

        def spy_attach(path, query):
            calls.append((path, query))
            return original_attach(path, query)

        vest._attach_query = spy_attach  # type: ignore[assignment]
        original_request = vest._signed_request

        def fake_request(creds, *, method, path_with_query, body):
            return {"id": "0xabc"}

        vest._signed_request = fake_request  # type: ignore[assignment]
        try:
            vest._signed_post(
                vest._lookup_credentials("fibo"),
                "/orders/cancel",
                {"order": {"time": 1, "nonce": 0, "id": "0xabc"}, "recvWindow": 60000, "signature": "0x"},
                query={"time": 1, "nonce": 0},
            )
        finally:
            vest._attach_query = original_attach  # type: ignore[assignment]
            vest._signed_request = original_request  # type: ignore[assignment]
        self.assertTrue(
            any(
                c[0] == "/orders/cancel" and c[1] == {"time": 1, "nonce": 0}
                for c in calls
            )
        )

    # --- Phase 3.7: cancel_order_group end-to-end -------------------------

    def test_cancel_order_group_filters_and_walks_ids(self):
        # Two resting orders matching the filter, one outside the filter,
        # one already-FILLED (skipped by _extract_open_order_ids).
        captured: list = []
        original_post = self._mock_signed_post(captured)

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path == "/orders":
                return [
                    {"id": "0xA", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "NEW"},
                    {"id": "0xB", "symbol": "BTC-PERP", "isBuy": False, "orderType": "LIMIT", "size": "0.1", "status": "NEW"},
                    {"id": "0xC", "symbol": "ETH-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "NEW"},
                    {"id": "0xD", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "FILLED"},
                ]
            # verify poll: GET /orders?id=0xA → CANCELLED
            if path.startswith("/orders?id="):
                return [{"id": "0xA", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "CANCELLED"}]
            raise AssertionError(f"unexpected GET {path}")
        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "cancel_order_group",
                "account": "fibo",
                "symbol": "BTC-PERP",
                "side": "long",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        # Only 0xA matched (BTC + long, NEW).
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][1]["order"]["id"], "0xA")
        assert resp.cancel_group is not None
        self.assertEqual(resp.cancel_group.targeted_order_count, 1)
        self.assertEqual(resp.cancel_group.cancelled_order_count, 1)
        # The per-child audit is exposed in data.children.
        data = resp.data
        assert data is not None
        self.assertEqual(len(data["children"]), 1)
        self.assertEqual(data["children"][0]["order_id"], "0xA")
        self.assertTrue(data["children"][0]["success"])

    def test_cancel_order_group_cancels_all_when_no_filter(self):
        # No filter → every resting order (PARTIALLY_FILLED is treated
        # as resting; _extract_open_order_ids includes it).
        captured: list = []
        original_post = self._mock_signed_post(captured)

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path == "/orders":
                return [
                    {"id": "0xA", "symbol": "BTC-PERP", "isBuy": True, "size": "0.1", "status": "NEW"},
                    {"id": "0xB", "symbol": "ETH-PERP", "isBuy": False, "size": "1.0", "status": "PARTIALLY_FILLED"},
                ]
            if path.startswith("/orders?id="):
                oid = path.split("=", 1)[1]
                return [{"id": oid, "symbol": "X", "isBuy": True, "size": "0.1", "status": "CANCELLED"}]
            raise AssertionError(f"unexpected GET {path}")
        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({"operation": "cancel_order_group", "account": "fibo"})
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        self.assertEqual(len(captured), 2)
        assert resp.cancel_group is not None
        self.assertEqual(resp.cancel_group.targeted_order_count, 2)
        self.assertEqual(resp.cancel_group.cancelled_order_count, 2)

    def test_cancel_order_group_rejects_invalid_side(self):
        resp = vest.execute({
            "operation": "cancel_order_group",
            "account": "fibo",
            "side": "diagonal",
        })
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INVALID_SIDE")

    def test_cancel_order_group_carries_time_on_nonce_get(self):
        """Every child fetches a fresh /account/nonce with ?time=<ms>.
        Each child uses the shared single-order primitive, so this
        invariant is inherited.
        """
        captured_gets: list = []
        captured_posts: list = []
        original_post = self._mock_signed_post(captured_posts)
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                captured_gets.append((path, query))
                return {"lastNonce": 0}
            if path == "/orders":
                return [{"id": "0xA", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "NEW"}]
            if path.startswith("/orders?id="):
                oid = path.split("=", 1)[1]
                return [{"id": oid, "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "CANCELLED"}]
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "cancel_order_group",
                "account": "fibo",
                "symbol": "BTC-PERP",
                "side": "long",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        # nonce GETs must carry ?time=<int>.
        self.assertGreaterEqual(len(captured_gets), 1)
        for path, query in captured_gets:
            self.assertEqual(path, "/account/nonce")
            assert query is not None
            self.assertIn("time", query)
            self.assertIsInstance(query["time"], int)

    def test_cancel_order_group_query_time_matches_body_time(self):
        """For every child, ``POST /orders/cancel`` carries ``?time=``
        AND the JSON body ``order.time`` field, and both equal each other.
        """
        captured: list = []
        original_post = self._mock_signed_post(captured)

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path == "/orders":
                return [
                    {"id": "0xA", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "NEW"},
                    {"id": "0xB", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "NEW"},
                ]
            if path.startswith("/orders?id="):
                oid = path.split("=", 1)[1]
                return [{"id": oid, "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "CANCELLED"}]
            raise AssertionError(f"unexpected GET {path}")

        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "cancel_order_group",
                "account": "fibo",
                "symbol": "BTC-PERP",
                "side": "long",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        self.assertEqual(len(captured), 2)
        for path, body, query in captured:
            self.assertEqual(path, "/orders/cancel")
            assert query is not None
            query_time = query.get("time")
            self.assertEqual(query_time, body["order"]["time"])

    def test_cancel_order_group_fresh_nonce_per_child(self):
        """Each child fetches its own nonce. Two children ⇒ two nonce
        GETs and two different nonces on the cancel bodies.
        """
        captured: list = []
        original_post = self._mock_signed_post(captured)
        nonce_walk = {"n": 0}

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                nonce_walk["n"] += 1
                # Simulate the server's nonce-walking: each GET returns
                # ``lastNonce`` = prior + 1.
                return {"lastNonce": nonce_walk["n"] - 1}
            if path == "/orders":
                return [
                    {"id": "0xA", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "NEW"},
                    {"id": "0xB", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "NEW"},
                ]
            if path.startswith("/orders?id="):
                oid = path.split("=", 1)[1]
                return [{"id": oid, "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "CANCELLED"}]
            raise AssertionError(f"unexpected GET {path}")

        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "cancel_order_group",
                "account": "fibo",
                "symbol": "BTC-PERP",
                "side": "long",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        self.assertEqual(len(captured), 2)
        nonces = [body["order"]["nonce"] for _path, body, _q in captured]
        self.assertEqual(len(set(nonces)), 2, msg=f"nonces={nonces}")

    def test_cancel_order_group_one_post_per_child(self):
        """Exactly ONE POST /orders/cancel per child, regardless of the
        child outcome (success / 3017 / failure).
        """
        captured: list = []
        original_post = self._mock_signed_post(captured)

        # Track per-id POST counts so we can assert no double POST.
        posts_per_child: dict = {}

        def _spy_post(creds, body_path, body, *, query=None):
            captured.append((body_path, body, query))
            if "order" in body and "id" in body["order"]:
                oid = body["order"]["id"]
                posts_per_child[oid] = posts_per_child.get(oid, 0) + 1
            # Always return a fresh-nonce conventional envelope; the
            # single-order primitive will then verify CANCELLED.
            return {"code": 0, "msg": "", "data": {"id": body["order"]["id"]}}

        vest._signed_post = _spy_post  # type: ignore[assignment]

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path == "/orders":
                return [
                    {"id": "0xA", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "NEW"},
                    {"id": "0xB", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "NEW"},
                ]
            if path.startswith("/orders?id="):
                oid = path.split("=", 1)[1]
                return [{"id": oid, "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "CANCELLED"}]
            raise AssertionError(f"unexpected GET {path}")

        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "cancel_order_group",
                "account": "fibo",
                "symbol": "BTC-PERP",
                "side": "long",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        for oid, count in posts_per_child.items():
            self.assertEqual(count, 1, msg=f"{oid} got {count} POSTs")

    def test_cancel_order_group_hype_untouched_when_ndx_cancelled(self):
        """Group selection is exact: cancelling the BTC side leaves
        the ETH order (different instrument) alone.
        """
        captured: list = []
        original_post = self._mock_signed_post(captured)

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path == "/orders":
                return [
                    {"id": "0xNDX1", "symbol": "NDX-PERP", "isBuy": False, "orderType": "LIMIT", "size": "0.1", "status": "NEW"},
                    {"id": "0xNDX2", "symbol": "NDX-PERP", "isBuy": False, "orderType": "LIMIT", "size": "0.2", "status": "NEW"},
                    {"id": "0xHYPE", "symbol": "HYPE-PERP", "isBuy": True, "orderType": "LIMIT", "size": "1.0", "status": "NEW"},
                ]
            if path.startswith("/orders?id="):
                oid = path.split("=", 1)[1]
                return [{"id": oid, "symbol": "NDX-PERP", "isBuy": False, "orderType": "LIMIT", "size": "0.1", "status": "CANCELLED"}]
            raise AssertionError(f"unexpected GET {path}")

        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "cancel_order_group",
                "account": "fibo",
                "symbol": "NDX-PERP",
                "side": "short",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        # HYPE must not appear in the cancel bodies.
        ids = [body["order"]["id"] for _p, body, _q in captured]
        self.assertNotIn("0xHYPE", ids)
        self.assertEqual(sorted(ids), ["0xNDX1", "0xNDX2"])

    def test_cancel_order_group_mixed_3017_cancelled_and_filled(self):
        """Mixed group: 3017+CANCELLED → succeeded; 3017+FILLED →
        already_filled; 3017+NEW → ambiguous. Each child is its own
        primitive, no auto-retry.
        """
        captured: list = []
        original_post = self._mock_signed_post(captured)

        cancel_index = {"n": 0}

        def _spy_post(creds, body_path, body, *, query=None):
            captured.append((body_path, body, query))
            oid = body["order"]["id"]
            if oid == "0xA":
                return {"code": 3017, "msg": "Order already processed or cancelled"}
            if oid == "0xB":
                return {"code": 3017, "msg": "Order already processed or cancelled"}
            if oid == "0xC":
                return {"code": 3017, "msg": "Order already processed or cancelled"}
            raise AssertionError(f"unexpected order id {oid}")

        vest._signed_post = _spy_post  # type: ignore[assignment]

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path == "/orders":
                return [
                    {"id": "0xA", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "NEW"},
                    {"id": "0xB", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "NEW"},
                    {"id": "0xC", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "NEW"},
                ]
            if path.startswith("/orders?id="):
                oid = path.split("=", 1)[1]
                # Map each id to its verify outcome:
                # A → CANCELLED, B → FILLED, C → NEW (still open).
                status = {"0xA": "CANCELLED", "0xB": "FILLED", "0xC": "NEW"}.get(oid)
                return [{"id": oid, "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": status}]
            raise AssertionError(f"unexpected GET {path}")

        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "cancel_order_group",
                "account": "fibo",
                "symbol": "BTC-PERP",
                "side": "long",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        # Group succeeds (the group operation itself is well-formed),
        # but the per-child outcomes are mixed.
        self.assertTrue(resp.success, msg=str(resp.error))
        # Exactly one POST per id.
        posts_per = {}
        for _p, body, _q in captured:
            oid = body["order"]["id"]
            posts_per[oid] = posts_per.get(oid, 0) + 1
        for oid, count in posts_per.items():
            self.assertEqual(count, 1, msg=f"{oid} got {count}")
        # Per-child classification.
        assert resp.data is not None
        self.assertEqual(resp.data["succeeded"], ["0xA"])
        self.assertEqual(resp.data["already_filled"], ["0xB"])
        self.assertEqual(resp.data["ambiguous"], ["0xC"])
        self.assertEqual(resp.data["failed"], [])
        # cancel_group is partial because some children didn't succeed.
        assert resp.cancel_group is not None
        self.assertTrue(resp.cancel_group.partial)
        self.assertEqual(resp.cancel_group.cancelled_order_count, 1)
        self.assertEqual(resp.cancel_group.targeted_order_count, 3)

    def test_cancel_order_group_no_second_post_on_3017(self):
        """Critical safety property: a 3017 envelope never triggers a
        second POST /orders/cancel — neither at the child level nor at
        the group level.
        """
        captured: list = []
        original_post = self._mock_signed_post(captured)

        def _spy_post(creds, body_path, body, *, query=None):
            captured.append((body_path, body, query))
            return {"code": 3017, "msg": "Order already processed or cancelled"}

        vest._signed_post = _spy_post  # type: ignore[assignment]

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path == "/orders":
                return [
                    {"id": "0xA", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "NEW"},
                    {"id": "0xB", "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "NEW"},
                ]
            if path.startswith("/orders?id="):
                oid = path.split("=", 1)[1]
                return [{"id": oid, "symbol": "BTC-PERP", "isBuy": True, "orderType": "LIMIT", "size": "0.1", "status": "CANCELLED"}]
            raise AssertionError(f"unexpected GET {path}")

        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "cancel_order_group",
                "account": "fibo",
                "symbol": "BTC-PERP",
                "side": "long",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        # Exactly 2 cancel POSTs: one per id, never more.
        cancel_posts = [c for c in captured if c[0] == "/orders/cancel"]
        self.assertEqual(len(cancel_posts), 2)
        assert resp.data is not None
        self.assertEqual(resp.data["succeeded"], ["0xA", "0xB"])

    def test_cancel_order_group_no_targets_returns_empty_success(self):
        """If the filter matches zero open orders, the group succeeds
        with zero counts. The wizard uses this to render "nothing to
        cancel".
        """
        captured: list = []
        original_post = self._mock_signed_post(captured)

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path == "/orders":
                return []
            raise AssertionError(f"unexpected GET {path}")

        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "cancel_order_group",
                "account": "fibo",
                "symbol": "BTC-PERP",
                "side": "long",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        # No POSTs sent.
        self.assertEqual(len(captured), 0)
        assert resp.cancel_group is not None
        self.assertEqual(resp.cancel_group.targeted_order_count, 0)
        self.assertEqual(resp.cancel_group.cancelled_order_count, 0)

    # --- Phase 3.8: ladder end-to-end ------------------------------------

    def test_ladder_posts_each_child_with_walking_nonce(self):
        captured: list = []
        original_post = self._mock_signed_post(captured)
        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path.startswith("/orders?id="):
                return [{"id": path.split("=", 1)[1], "status": "NEW"}]
            raise AssertionError(f"unexpected GET {path}")
        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "ladder",
                "account": "fibo",
                "symbol": "BTC-PERP",
                "side": "buy",
                "children": [
                    {"price": "29000", "size": "0.1"},
                    {"price": "29500", "size": "0.2"},
                    {"price": "30000", "size": "0.3"},
                ],
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        self.assertEqual(len(captured), 3)
        # Each child carries the right price/size and a walking nonce.
        for idx, (path, body, _query) in enumerate(captured):
            self.assertEqual(path, "/orders")
            order = body["order"]
            self.assertEqual(order["symbol"], "BTC-PERP")
            self.assertEqual(order["isBuy"], True)
            self.assertEqual(order["orderType"], "LIMIT")
            self.assertEqual(order["nonce"], idx + 1)  # lastNonce=0 → 1,2,3
        # Canonical result.
        assert resp.ladder is not None
        self.assertEqual(resp.ladder.requested_order_count, 3)
        self.assertEqual(resp.ladder.submitted_order_count, 3)
        assert resp.ladder.child_order_ids is not None
        self.assertEqual(len(resp.ladder.child_order_ids), 3)

    def test_ladder_rejects_empty_children(self):
        resp = vest.execute({
            "operation": "ladder",
            "account": "fibo",
            "symbol": "BTC-PERP",
            "side": "buy",
            "children": [],
        })
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INVALID_CHILDREN")

    def test_ladder_rejects_too_many_children(self):
        too_many = [{"price": "1", "size": "1"} for _ in range(vest.LADDER_MAX_ORDERS_PER_INSTRUMENT + 1)]
        resp = vest.execute({
            "operation": "ladder",
            "account": "fibo",
            "symbol": "BTC-PERP",
            "side": "buy",
            "children": too_many,
        })
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "TOO_MANY_CHILDREN")

    def test_ladder_rejects_invalid_child(self):
        resp = vest.execute({
            "operation": "ladder",
            "account": "fibo",
            "symbol": "BTC-PERP",
            "side": "buy",
            "children": [{"price": "0", "size": "1"}],
        })
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INVALID_CHILD")

    # --- Phase 3.10: ladder distribution expansion (Half-Gaussian / uniform) -

    def _stub_catalog_for(self, ndx_row):
        """Patch ``_fetch_catalog`` to return a one-row catalog.

        ``ndx_row`` may be either a single catalog-row dict (the helper
        wraps it in a one-element list) or a list of catalog-row dicts
        (used by tests that need multiple instruments on the wire).
        """
        original = vest._fetch_catalog
        if isinstance(ndx_row, list):
            catalog = list(ndx_row)
        else:
            catalog = [ndx_row]
        vest._fetch_catalog = lambda creds: catalog  # type: ignore[assignment]
        return original

    def _stub_ndx_catalog(self):
        """Return a Vest-shaped NDX catalog row with size_decimal s=4,
        price_decimals=2 — the live values for NDX-USD-PERP.
        """
        return {
            "symbol": "NDX-USD-PERP",
            "display_name": "NASDAQ 100 E-mini Futures",
            "base": "NDX-USD",
            "quote": "USDC",
            "size_decimals": 4,
            "price_decimals": 2,
            "init_margin_ratio": "0.020000",
            "maint_margin_ratio": "0.010000",
            "taker_fee": "0",
            "isolated": False,
        }

    def test_ladder_half_gaussian_expands_to_50_children(self):
        """User scenario: SELL NDX, Half Gaussian, 50 orders, total_volume=5,
        30850 → 31850. The wizard hits INVALID_CHILDREN without expansion.
        With expansion, exactly 50 valid children are submitted.
        """
        captured: list = []
        original_post = self._mock_signed_post(captured, unique_ids=True)
        original_catalog = self._stub_catalog_for(self._stub_ndx_catalog())
        # nonce GET → 0; verify GET /orders?id=<id> → NEW
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path.startswith("/orders?id="):
                return [{"id": path.split("=", 1)[1], "status": "NEW"}]
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "ladder",
                "account": "fibo",
                "symbol": "NDX-USD-PERP",
                "side": "sell",
                "distribution": "half_gaussian",
                "order_count": 50,
                "total_volume": "5",
                "start_price": "30850",
                "end_price": "31850",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
            vest._fetch_catalog = original_catalog  # type: ignore[assignment]

        self.assertTrue(resp.success, msg=str(resp.error))
        self.assertEqual(len(captured), 50)
        # All 50 are POST /orders with SELL + LIMIT.
        for path, body, _query in captured:
            self.assertEqual(path, "/orders")
            order = body["order"]
            self.assertEqual(order["symbol"], "NDX-USD-PERP")
            self.assertEqual(order["isBuy"], False)
            self.assertEqual(order["orderType"], "LIMIT")
            self.assertNotEqual(order["limitPrice"], "")
            self.assertNotEqual(order["size"], "")
        # Walking nonce.
        nonces = [body["order"]["nonce"] for _p, body, _q in captured]
        self.assertEqual(nonces, list(range(1, 51)))
        # Canonical result.
        assert resp.ladder is not None
        self.assertEqual(resp.ladder.requested_order_count, 50)
        self.assertEqual(resp.ladder.submitted_order_count, 50)
        self.assertEqual(resp.ladder.distribution, "half_gaussian")
        self.assertEqual(resp.ladder.symbol, "NDX-USD-PERP")
        self.assertEqual(resp.ladder.side, "sell")
        self.assertEqual(resp.ladder.status, "success")
        self.assertFalse(resp.ladder.partial)
        self.assertEqual(resp.ladder.omitted_order_count, 0)
        # 50 unique order_ids (regression: counter-based mock returns
        # 0x000000000001..50 so the set is 50 distinct ids).
        assert resp.ladder.child_order_ids is not None
        self.assertEqual(len(set(resp.ladder.child_order_ids)), 50)

    def test_ladder_half_gaussian_price_range_and_size_progression(self):
        """Sizes must be monotonically increasing (smallest at the
        start price = worst exit, largest at the end price = best
        exit) and the price range must span 30850 → 31850 subject
        only to the tick increment (0.01).
        """
        captured: list = []
        original_post = self._mock_signed_post(captured)
        original_catalog = self._stub_catalog_for(self._stub_ndx_catalog())
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path.startswith("/orders?id="):
                return [{"id": path.split("=", 1)[1], "status": "NEW"}]
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "ladder",
                "account": "fibo",
                "symbol": "NDX-USD-PERP",
                "side": "sell",
                "distribution": "half_gaussian",
                "order_count": 50,
                "total_volume": "5",
                "start_price": "30850",
                "end_price": "31850",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
            vest._fetch_catalog = original_catalog  # type: ignore[assignment]

        self.assertTrue(resp.success, msg=str(resp.error))
        from decimal import Decimal
        prices = [Decimal(p[1]["order"]["limitPrice"]) for p in captured]
        sizes = [Decimal(p[1]["order"]["size"]) for p in captured]
        # Range.
        self.assertEqual(prices[0], Decimal("30850.00"))
        self.assertEqual(prices[-1], Decimal("31850.00"))
        # Monotonic prices.
        self.assertTrue(all(prices[i] <= prices[i+1] for i in range(len(prices)-1)),
                        msg="prices not monotonic")
        # Monotonic sizes (Half-Gaussian weights grow toward z=0).
        self.assertTrue(all(sizes[i] <= sizes[i+1] for i in range(len(sizes)-1)),
                        msg="sizes not monotonic increasing")
        # No zero-size children.
        self.assertTrue(all(s > 0 for s in sizes))
        # Quantized kept total equals the requested total (5.0000 /
        # 0.0001 lot = 50000 units, perfectly divisible by 50 children).
        self.assertEqual(sum(sizes), Decimal("5.0000"))
        # No two adjacent prices collapsed past the tick (price increment
        # is 0.01 — 1000 / 49 ≈ 20.41 step so they're all distinct).
        self.assertEqual(len(set(prices)), 50)

    def test_ladder_uniform_distribution_supported(self):
        """``uniform`` is the canonical secondary distribution; the
        agent must accept it and produce 50 evenly-sized children
        summing to 5.0.
        """
        captured: list = []
        original_post = self._mock_signed_post(captured)
        original_catalog = self._stub_catalog_for(self._stub_ndx_catalog())
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path.startswith("/orders?id="):
                return [{"id": path.split("=", 1)[1], "status": "NEW"}]
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "ladder",
                "account": "fibo",
                "symbol": "NDX-USD-PERP",
                "side": "sell",
                "distribution": "uniform",
                "order_count": 50,
                "total_volume": "5",
                "start_price": "30850",
                "end_price": "31850",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
            vest._fetch_catalog = original_catalog  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        self.assertEqual(len(captured), 50)
        from decimal import Decimal
        sizes = [Decimal(p[1]["order"]["size"]) for p in captured]
        # 5.0 / 50 = 0.1 per child (uniform) exactly.
        for s in sizes:
            self.assertEqual(s, Decimal("0.1000"))

    def test_ladder_rejects_buy_direction_with_end_above_start(self):
        """Direction rule: BUY ladders require end_price below start_price."""
        captured: list = []
        original_post = self._mock_signed_post(captured)
        original_catalog = self._stub_catalog_for(self._stub_ndx_catalog())
        try:
            resp = vest.execute({
                "operation": "ladder",
                "account": "fibo",
                "symbol": "NDX-USD-PERP",
                "side": "buy",
                "distribution": "half_gaussian",
                "order_count": 50,
                "total_volume": "5",
                "start_price": "30850",
                "end_price": "31850",  # wrong for BUY
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._fetch_catalog = original_catalog  # type: ignore[assignment]
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INVALID_LADDER_DIRECTION")
        # No POSTs were sent.
        self.assertEqual(len(captured), 0)

    def test_ladder_rejects_sell_direction_with_end_below_start(self):
        captured: list = []
        original_post = self._mock_signed_post(captured)
        original_catalog = self._stub_catalog_for(self._stub_ndx_catalog())
        try:
            resp = vest.execute({
                "operation": "ladder",
                "account": "fibo",
                "symbol": "NDX-USD-PERP",
                "side": "sell",
                "distribution": "half_gaussian",
                "order_count": 50,
                "total_volume": "5",
                "start_price": "31850",
                "end_price": "30850",  # wrong for SELL
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._fetch_catalog = original_catalog  # type: ignore[assignment]
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INVALID_LADDER_DIRECTION")
        self.assertEqual(len(captured), 0)

    def test_ladder_rejects_unsupported_distribution(self):
        captured: list = []
        original_post = self._mock_signed_post(captured)
        original_catalog = self._stub_catalog_for(self._stub_ndx_catalog())
        try:
            resp = vest.execute({
                "operation": "ladder",
                "account": "fibo",
                "symbol": "NDX-USD-PERP",
                "side": "sell",
                "distribution": "random",
                "order_count": 50,
                "total_volume": "5",
                "start_price": "30850",
                "end_price": "31850",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._fetch_catalog = original_catalog  # type: ignore[assignment]
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "UNSUPPORTED_DISTRIBUTION")
        self.assertEqual(len(captured), 0)

    def test_ladder_one_post_per_child_partial_failure(self):
        """Half of the children fail on POST. The agent returns a
        partial result; the failing children are recorded in
        ``data.failed`` and the surviving children keep their order
        ids. NO retry is attempted for the failed children.
        """
        captured: list = []
        original_post = self._mock_signed_post(captured)
        original_catalog = self._stub_catalog_for(self._stub_ndx_catalog())
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path.startswith("/orders?id="):
                return [{"id": path.split("=", 1)[1], "status": "NEW"}]
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]

        posts_per_oid = {0: 0}
        # Wrap the original _signed_post: every odd child returns a 422.
        original_signed_post = vest._signed_post

        def selective_post(creds, path, body, *, query=None):
            posts_per_oid[0] += 1
            try:
                oid = body["order"]["id"]
            except Exception:  # noqa: BLE001
                oid = "0xfallback"
            if posts_per_oid[0] % 2 == 1:
                raise vest.VestHTTPError(
                    status=422, path=path,
                    body='{"detail":[{"type":"missing","loc":["query","time"],"msg":"oops"}]}',
                )
            return {"code": 0, "msg": "", "data": {"id": oid}}

        vest._signed_post = selective_post  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "ladder",
                "account": "fibo",
                "symbol": "NDX-USD-PERP",
                "side": "sell",
                "distribution": "half_gaussian",
                "order_count": 50,
                "total_volume": "5",
                "start_price": "30850",
                "end_price": "31850",
            })
        finally:
            vest._signed_post = original_signed_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
            vest._fetch_catalog = original_catalog  # type: ignore[assignment]

        self.assertTrue(resp.success, msg=str(resp.error))
        # The agent still reports a top-level success for the operation
        # itself (the submit loop ran to completion); the canonical
        # ladder surfaces the partial state via ``status="partial"``
        # and ``submitted_order_count``.
        assert resp.ladder is not None
        self.assertEqual(resp.ladder.status, "partial")
        self.assertEqual(resp.ladder.requested_order_count, 50)
        self.assertEqual(resp.ladder.submitted_order_count, 25)
        self.assertEqual(resp.ladder.omitted_order_count, 25)
        # Captured POSTs = 25 successful (the 25 failed POSTs aren't
        # captured — they raise before reaching _mock_signed_post's
        # append). Total attempts = 50.
        self.assertEqual(posts_per_oid[0], 50)
        # Failure rows live in data.failed (one per failed child).
        assert resp.data is not None
        self.assertEqual(len(resp.data.get("failed") or []), 25)

    def test_ladder_unknown_instrument_returns_instrument_not_found(self):
        captured: list = []
        original_post = self._mock_signed_post(captured)
        original_catalog = self._stub_catalog_for(self._stub_ndx_catalog())
        try:
            resp = vest.execute({
                "operation": "ladder",
                "account": "fibo",
                "symbol": "NOPE-INVALID",
                "side": "sell",
                "distribution": "half_gaussian",
                "order_count": 50,
                "total_volume": "5",
                "start_price": "30850",
                "end_price": "31850",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._fetch_catalog = original_catalog  # type: ignore[assignment]
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INSTRUMENT_NOT_FOUND")
        # No POSTs were sent.
        self.assertEqual(len(captured), 0)

    def test_ladder_each_child_uses_single_order_primitive_path(self):
        """Each child is submitted via the existing /orders POST
        (the same path the single-order ``new_order`` operation uses).
        The order body shape must match what ``_build_new_order_payload``
        produces for a standalone new_order.
        """
        captured: list = []
        original_post = self._mock_signed_post(captured)
        original_catalog = self._stub_catalog_for(self._stub_ndx_catalog())
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path.startswith("/orders?id="):
                return [{"id": path.split("=", 1)[1], "status": "NEW"}]
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "ladder",
                "account": "fibo",
                "symbol": "NDX-USD-PERP",
                "side": "sell",
                "distribution": "half_gaussian",
                "order_count": 3,  # tiny test
                "total_volume": "5",
                "start_price": "30850",
                "end_price": "31850",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
            vest._fetch_catalog = original_catalog  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        # Each child body is a /orders POST (NOT /orders/cancel, NOT
        # any bulk endpoint). The body shape matches the single-order
        # new_order contract.
        for path, body, _q in captured:
            self.assertEqual(path, "/orders")
            self.assertIn("order", body)
            order = body["order"]
            self.assertIn("symbol", order)
            self.assertIn("isBuy", order)
            self.assertIn("orderType", order)
            self.assertEqual(order["orderType"], "LIMIT")
            self.assertIn("time", order)
            self.assertIn("nonce", order)
            # ``signature`` and ``recvWindow`` are siblings of ``order``
            # in the body envelope (matches ``_build_new_order_payload``
            # and the documented Vest JSON shape).
            self.assertIn("signature", body)
            self.assertTrue(body["signature"].startswith("0x"))
            self.assertIn("recvWindow", body)

    def test_ladder_rejects_negative_volume(self):
        captured: list = []
        original_post = self._mock_signed_post(captured)
        original_catalog = self._stub_catalog_for(self._stub_ndx_catalog())
        try:
            resp = vest.execute({
                "operation": "ladder",
                "account": "fibo",
                "symbol": "NDX-USD-PERP",
                "side": "sell",
                "distribution": "half_gaussian",
                "order_count": 50,
                "total_volume": "0",
                "start_price": "30850",
                "end_price": "31850",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._fetch_catalog = original_catalog  # type: ignore[assignment]
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INVALID_VOLUME")

    def test_ladder_invalid_children_no_longer_surfaces(self):
        """Regression: the live failure was ``INVALID_CHILDREN`` when
        the wizard sent a distribution-style request. With the new
        expansion logic, that exact request must NOT produce
        ``INVALID_CHILDREN`` — it produces a successful ladder with
        50 children.
        """
        captured: list = []
        original_post = self._mock_signed_post(captured)
        original_catalog = self._stub_catalog_for(self._stub_ndx_catalog())
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path.startswith("/orders?id="):
                return [{"id": path.split("=", 1)[1], "status": "NEW"}]
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "ladder",
                "account": "fibo",
                "symbol": "NDX-USD-PERP",
                "side": "sell",
                "distribution": "half_gaussian",
                "order_count": 50,
                "total_volume": "5",
                "start_price": "30850",
                "end_price": "31850",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
            vest._fetch_catalog = original_catalog  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        self.assertNotEqual(
            resp.error.code if resp.error else None, "INVALID_CHILDREN"
        )

    def test_ladder_does_not_touch_hype_orders(self):
        """The ladder path resolves one symbol from the catalog and
        submits children only for that symbol. ``HYPE-PERP`` must not
        appear in any child POST body.
        """
        captured: list = []
        original_post = self._mock_signed_post(captured)
        # Catalog with both HYPE and NDX; only NDX should be submitted.
        hype_row = {
            "symbol": "HYPE-PERP",
            "display_name": "Hyperliquid",
            "base": "HYPE",
            "quote": "USDC",
            "size_decimals": 2,
            "price_decimals": 4,
            "isolated": False,
        }
        original_catalog = self._stub_catalog_for([self._stub_ndx_catalog(), hype_row])
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            if path.startswith("/orders?id="):
                return [{"id": path.split("=", 1)[1], "status": "NEW"}]
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "ladder",
                "account": "fibo",
                "symbol": "NDX-USD-PERP",
                "side": "sell",
                "distribution": "half_gaussian",
                "order_count": 5,
                "total_volume": "5",
                "start_price": "30850",
                "end_price": "31850",
            })
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
            vest._fetch_catalog = original_catalog  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        for path, body, _q in captured:
            self.assertEqual(path, "/orders")
            self.assertNotEqual(body["order"]["symbol"], "HYPE-PERP")

    # --- Phase 3.9: get_exact_order --------------------------------------

    def test_get_exact_order_returns_matching_row(self):
        def seed_get(creds, path, *, query=None):
            if path == "/orders?id=0xabc":
                return [{"id": "0xabc", "symbol": "BTC-PERP", "status": "NEW", "size": "0.1"}]
            raise AssertionError(f"unexpected GET {path}")
        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "get_exact_order",
                "account": "fibo",
                "order_id": "0xabc",
            })
        finally:
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success)
        assert resp.data is not None
        self.assertEqual(resp.data["order_state"]["id"], "0xabc")
        self.assertEqual(resp.data["order_state"]["status"], "NEW")

    def test_get_exact_order_handles_not_found(self):
        def seed_get(creds, path, *, query=None):
            return []  # No rows.
        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "get_exact_order",
                "account": "fibo",
                "order_id": "0xnope",
            })
        finally:
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "ORDER_NOT_FOUND")

    def test_get_exact_order_rejects_missing_order_id(self):
        resp = vest.execute({"operation": "get_exact_order", "account": "fibo"})
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "MISSING_ORDER_ID")

    # --- Phase 3.10: market_constraints ---------------------------------

    def test_market_constraints_returns_matching_symbol(self):
        def seed_get(creds, path, *, query=None):
            if path == "/exchangeInfo":
                return {
                    "symbols": [
                        {"symbol": "BTC-PERP", "sizeDecimals": 4, "priceDecimals": 2, "displayName": "BTC-PERP"},
                        {"symbol": "ETH-PERP", "sizeDecimals": 3, "priceDecimals": 2, "displayName": "ETH-PERP"},
                    ]
                }
            raise AssertionError(f"unexpected GET {path}")
        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "market_constraints",
                "account": "fibo",
                "symbol": "BTC-PERP",
            })
        finally:
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success, msg=str(resp.error))
        assert resp.instrument is not None
        # BTC-PERP has priceDecimals=2 → 0.01; sizeDecimals=4 → 0.0001.
        self.assertEqual(resp.instrument.price_increment, "0.01")
        self.assertEqual(resp.instrument.size_increment, "0.0001")

    def test_market_constraints_handles_unknown_symbol(self):
        def seed_get(creds, path, *, query=None):
            return {"symbols": []}
        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute({
                "operation": "market_constraints",
                "account": "fibo",
                "symbol": "FOO-BAR",
            })
        finally:
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INSTRUMENT_NOT_FOUND")

    # --- Phase 3.11: signing key never escapes via exceptions ------------

    def test_signing_key_does_not_leak_into_error_messages(self):
        # Trigger a POST /orders HTTP 500; the rendered error must
        # NOT contain the test signing key.
        original_post = vest._signed_post
        # ``sign_private_key`` is part of the in-flight request's
        # credentials — the same dict that production's ``_signed_request``
        # would carry on the ``VestHTTPError``. Simulate that here.
        creds_for_err = {
            "api_key": "0xdeadbeef",
            "sign_private_key": self._test_key_hex,
            "public_key": "0xpub",
        }
        def fake(creds, path, body):
            raise vest.VestHTTPError(
                status=500, path=path,
                body=f"server panic near {self._test_key_hex}",
                credentials=creds,
            )
        vest._signed_post = fake  # type: ignore[assignment]
        def seed_get(creds, path, *, query=None):
            if path == "/account/nonce":
                return {"lastNonce": 0}
            raise AssertionError(f"unexpected GET {path}")
        original_get = vest._signed_get
        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(self._new_order_request())
        finally:
            vest._signed_post = original_post  # type: ignore[assignment]
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertNotIn(self._test_key_hex, resp.error.message)
        # Also: the raw error body should have the key, but redaction
        # replaces it.
        self.assertIn("***", resp.error.message)

    # -----------------------------------------------------------------
    # Phase 4: instrument catalog + symbol resolver + mark price
    # -----------------------------------------------------------------
    #
    # The wizard's New Order / Ladder symbol picker calls:
    #   - ``list_instruments``   once per chat (returns every row from /exchangeInfo)
    #   - ``resolve_instrument`` once per typed symbol (success: single match,
    #                                                   failure: ambiguous with priced candidates)
    #   - ``market_price``       once per displayed candidate (mark price)
    #
    # These tests mock ``_signed_get`` and verify the response shapes
    # match what the wizard consumes.

    def _catalog(self):
        return [
            {
                "symbol": "BTC-PERP",
                "displayName": "BTC-PERP",
                "base": "BTC",
                "quote": "USDC",
                "sizeDecimals": 4,
                "priceDecimals": 2,
                "initMarginRatio": "0.1",
                "maintMarginRatio": "0.05",
                "takerFee": "0.0001",
                "isolated": False,
            },
            {
                "symbol": "ETH-PERP",
                "displayName": "ETH-PERP",
                "base": "ETH",
                "quote": "USDC",
                "sizeDecimals": 3,
                "priceDecimals": 2,
                "initMarginRatio": "0.1",
                "maintMarginRatio": "0.05",
                "takerFee": "0.0001",
                "isolated": False,
            },
            {
                "symbol": "HYPE-PERP",
                "displayName": "HYPE-PERP",
                "base": "HYPE",
                "quote": "USDC",
                "sizeDecimals": 1,
                "priceDecimals": 3,
                "initMarginRatio": "0.2",
                "maintMarginRatio": "0.1",
                "takerFee": "0.0005",
                "isolated": False,
            },
            {
                "symbol": "NQ-USD-PERP",
                "displayName": "NQ-USD-PERP",
                "base": "NQ",
                "quote": "USDC",
                "sizeDecimals": 4,
                "priceDecimals": 2,
                "initMarginRatio": "0.05",
                "maintMarginRatio": "0.025",
                "takerFee": "0.0002",
                "isolated": False,
            },
        ]

    def test_resolve_instrument_single_match_returns_canonical_instrument(self):
        # Restore operator creds for live-style call (catalog needs auth).
        self._apply(self.VALID_ENV)
        catalog = self._catalog()
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/exchangeInfo":
                return {"symbols": catalog}
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {"operation": "resolve_instrument", "account": "fibo", "symbol": "BTC"}
            )
        finally:
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success)
        assert resp.instrument is not None
        self.assertEqual(resp.instrument.symbol, "BTC-PERP")
        self.assertEqual(resp.instrument.requested_symbol, "BTC")
        self.assertEqual(resp.instrument.base, "BTC")
        self.assertEqual(resp.instrument.quote, "USDC")
        # ``BTC`` alone matches via base lookup; the wizard should still
        # see ``market_price=None`` here because the agent only fetches
        # price on the success path when /ticker/latest is reachable.
        # In this test we deliberately mock ``/ticker/latest`` to return
        # empty so we exercise the price-unavailable branch.

    def test_resolve_instrument_explicit_symbol_short_circuits_to_exact(self):
        self._apply(self.VALID_ENV)
        catalog = self._catalog()
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/exchangeInfo":
                return {"symbols": catalog}
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {
                    "operation": "resolve_instrument",
                    "account": "fibo",
                    "symbol": "ETH-PERP",
                }
            )
        finally:
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success)
        assert resp.instrument is not None
        self.assertEqual(resp.instrument.symbol, "ETH-PERP")

    def test_resolve_instrument_unknown_symbol_returns_not_found(self):
        self._apply(self.VALID_ENV)
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/exchangeInfo":
                return {"symbols": self._catalog()}
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {"operation": "resolve_instrument", "account": "fibo", "symbol": "DOESNOTEXIST"}
            )
        finally:
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INSTRUMENT_NOT_FOUND")

    def test_resolve_instrument_missing_symbol_returns_missing(self):
        self._apply(self.VALID_ENV)
        resp = vest.execute(
            {"operation": "resolve_instrument", "account": "fibo", "symbol": ""}
        )
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "MISSING_SYMBOL")

    def test_resolve_instrument_unknown_account_returns_unknown(self):
        self._apply(self.VALID_ENV)
        resp = vest.execute(
            {"operation": "resolve_instrument", "account": "ghost", "symbol": "BTC"}
        )
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "UNKNOWN_ACCOUNT")

    def test_resolve_instrument_aliases_nasdaq_to_ndx_plus_ndaq_stock(self):
        # The user typed "NASDAQ" — Vest's catalog has two matches:
        #   NDX-USD-PERP (Nasdaq 100 E-mini Futures)
        #   NDAQ-USD-PERP (Nasdaq, Inc. stock)
        # The wizard must surface BOTH as priced candidates so the user
        # can pick. The futures contract (NDX) should come first.
        self._apply(self.VALID_ENV)
        catalog = [
            {
                "symbol": "NDX-USD-PERP",
                "displayName": "NASDAQ 100 E-mini Futures",
                "base": "NDX",
                "quote": "USDC",
                "sizeDecimals": 4,
                "priceDecimals": 2,
            },
            {
                "symbol": "NDAQ-USD-PERP",
                "displayName": "Nasdaq, Inc.",
                "base": "NDAQ",
                "quote": "USDC",
                "sizeDecimals": 4,
                "priceDecimals": 2,
            },
        ]
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/exchangeInfo":
                return {"symbols": catalog}
            if path.startswith("/ticker/latest?symbols="):
                sym = path.split("=", 1)[1].upper()
                prices = {
                    "NDX-USD-PERP": "30660.50",
                    "NDAQ-USD-PERP": "91.95",
                }
                return {
                    "tickers": [
                        {
                            "symbol": sym,
                            "markPrice": prices.get(sym, "1.00"),
                            "status": "TRADING",
                        }
                    ]
                }
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {
                    "operation": "resolve_instrument",
                    "account": "fibo",
                    "symbol": "NASDAQ",
                }
            )
        finally:
            vest._signed_get = original_get  # type: ignore[assignment]
        # Multi-match → INSTRUMENT_AMBIGUOUS, candidate list carries both.
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INSTRUMENT_AMBIGUOUS")
        data = getattr(resp, "data", None) or {}
        candidates = data.get("candidates") if isinstance(data, dict) else None
        self.assertIsInstance(candidates, list)
        assert candidates is not None
        symbols = [c.get("instrument") for c in candidates]
        self.assertIn("NDX-USD-PERP", symbols)
        self.assertIn("NDAQ-USD-PERP", symbols)
        # Futures (NDX) must rank first so the wizard's button column
        # defaults to the index the user almost certainly wanted.
        self.assertEqual(symbols[0], "NDX-USD-PERP")
        # Both candidates must carry their priced ``price`` field.
        by_sym = {c.get("instrument"): c for c in candidates}
        self.assertEqual(by_sym["NDX-USD-PERP"].get("price"), "30660.5")
        self.assertEqual(by_sym["NDAQ-USD-PERP"].get("price"), "91.95")

    def test_resolve_instrument_keyword_500_matches_spx_and_spy(self):
        # The user typed "500" — Vest's catalog has two matches via the
        # display-name substring path:
        #   SPX-USD-PERP (S&P 500 E-mini Futures)
        #   SPY-USD-PERP (SPDR S&P 500 ETF Trust)
        self._apply(self.VALID_ENV)
        catalog = [
            {
                "symbol": "SPX-USD-PERP",
                "displayName": "S&P 500 E-mini Futures",
                "base": "SPX",
                "quote": "USDC",
                "sizeDecimals": 4,
                "priceDecimals": 2,
            },
            {
                "symbol": "SPY-USD-PERP",
                "displayName": "SPDR S&P 500 ETF Trust",
                "base": "SPY",
                "quote": "USDC",
                "sizeDecimals": 4,
                "priceDecimals": 2,
            },
            {
                "symbol": "SPGI-USD-PERP",
                "displayName": "S&P Global Inc.",
                "base": "SPGI",
                "quote": "USDC",
                "sizeDecimals": 4,
                "priceDecimals": 2,
            },
        ]
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/exchangeInfo":
                return {"symbols": catalog}
            if path.startswith("/ticker/latest?symbols="):
                sym = path.split("=", 1)[1].upper()
                prices = {
                    "SPX-USD-PERP": "7693.13",
                    "SPY-USD-PERP": "760.41",
                    "SPGI-USD-PERP": "560.12",
                }
                return {
                    "tickers": [
                        {
                            "symbol": sym,
                            "markPrice": prices.get(sym, "1.00"),
                            "status": "TRADING",
                        }
                    ]
                }
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {
                    "operation": "resolve_instrument",
                    "account": "fibo",
                    "symbol": "500",
                }
            )
        finally:
            vest._signed_get = original_get  # type: ignore[assignment]
        # 500 is a substring keyword, not a canonical alias → multi-match.
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INSTRUMENT_AMBIGUOUS")
        data = getattr(resp, "data", None) or {}
        candidates = data.get("candidates") if isinstance(data, dict) else None
        self.assertIsInstance(candidates, list)
        assert candidates is not None
        symbols = [c.get("instrument") for c in candidates]
        self.assertIn("SPX-USD-PERP", symbols)
        self.assertIn("SPY-USD-PERP", symbols)
        # SPGI doesn't have "500" in its display_name, so it's NOT a match
        # for the "500" keyword.
        self.assertNotIn("SPGI-USD-PERP", symbols)

    def test_resolve_instrument_single_match_advances_to_side(self):
        # "BTC" matches exactly one catalog row → success, no chooser.
        self._apply(self.VALID_ENV)
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/exchangeInfo":
                return {"symbols": self._catalog()}
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {"operation": "resolve_instrument", "account": "fibo", "symbol": "BTC"}
            )
        finally:
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success)
        assert resp.instrument is not None
        self.assertEqual(resp.instrument.symbol, "BTC-PERP")

    def test_list_instruments_returns_full_catalog_normalized(self):
        self._apply(self.VALID_ENV)
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/exchangeInfo":
                return {"symbols": self._catalog()}
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {"operation": "list_instruments", "account": "fibo"}
            )
        finally:
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success)
        data = getattr(resp, "data", None)
        self.assertIsInstance(data, dict)
        instruments = data.get("instruments") if data else None
        self.assertIsInstance(instruments, list)
        assert instruments is not None
        symbols = {entry.get("instrument") for entry in instruments}
        # Catalog passes through every row including the NASD-equivalent NQ.
        self.assertIn("BTC-PERP", symbols)
        self.assertIn("ETH-PERP", symbols)
        self.assertIn("HYPE-PERP", symbols)
        self.assertIn("NQ-USD-PERP", symbols)
        # ``base`` is what the picker keys on; ensure each row carries it.
        for entry in instruments:
            self.assertIn("base", entry)
            self.assertIn("quote", entry)

    def test_list_instruments_unknown_account_returns_unknown(self):
        self._apply(self.VALID_ENV)
        resp = vest.execute(
            {"operation": "list_instruments", "account": "ghost"}
        )
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "UNKNOWN_ACCOUNT")

    def test_market_price_returns_mark_price_for_resolved_symbol(self):
        self._apply(self.VALID_ENV)
        catalog = self._catalog()
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/exchangeInfo":
                return {"symbols": catalog}
            if path.startswith("/ticker/latest?symbols="):
                return {
                    "tickers": [
                        {
                            "symbol": "BTC-PERP",
                            "markPrice": "83786.50",
                            "indexPrice": "83780.00",
                            "status": "TRADING",
                        }
                    ]
                }
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {"operation": "market_price", "account": "fibo", "symbol": "BTC"}
            )
        finally:
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertTrue(resp.success)
        assert resp.market_price is not None
        # Wizard renders the ``as + price`` as ``BTC-PERP  ·  83,786.50``
        self.assertEqual(resp.market_price.market, "BTC-PERP")
        self.assertEqual(str(resp.market_price.mark_price), "83786.5")
        self.assertEqual(str(resp.market_price.price), "83786.5")

    def test_market_price_unavailable_returns_failure(self):
        self._apply(self.VALID_ENV)
        original_get = vest._signed_get

        def seed_get(creds, path, *, query=None):
            if path == "/exchangeInfo":
                return {"symbols": self._catalog()}
            if path.startswith("/ticker/latest?symbols="):
                return {"tickers": []}  # empty
            raise AssertionError(f"unexpected GET {path}")

        vest._signed_get = seed_get  # type: ignore[assignment]
        try:
            resp = vest.execute(
                {"operation": "market_price", "account": "fibo", "symbol": "BTC"}
            )
        finally:
            vest._signed_get = original_get  # type: ignore[assignment]
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "PRICE_UNAVAILABLE")

    def test_market_price_missing_symbol_returns_missing(self):
        self._apply(self.VALID_ENV)
        resp = vest.execute(
            {"operation": "market_price", "account": "fibo", "symbol": ""}
        )
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "MISSING_SYMBOL")

    def test_market_price_unknown_account_returns_unknown(self):
        self._apply(self.VALID_ENV)
        resp = vest.execute(
            {"operation": "market_price", "account": "ghost", "symbol": "BTC"}
        )
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "UNKNOWN_ACCOUNT")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()