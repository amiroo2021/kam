"""Verification suite for x_hibachi_agent.py.

This is a Phase 7 test file dedicated to the Hibachi exchange agent.
It is intentionally self-contained: every HIBACHI_* environment
variable is wiped in ``setUp`` / ``tearDown`` so the persistent
``~/.hermes/.env`` on the development machine cannot leak into
discovery assertions. The suite is structured around the 8 Phase 1
deliverables — discovery, credential lookup, dispatcher wiring,
balance canonicalization, error paths, instrument resolution, secret
redaction, and isolation invariants.

Run with::

    python3 plugins/trade/tests/test_phase7_hibachi.py
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from unittest import mock

import eth_keys.datatypes
from eth_keys.datatypes import PrivateKey as EthPrivateKey

# Make the package importable when invoked as a script.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent.parent  # /root/kam
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Use a clean, empty HERMES_HOME for every test so the persistent
# ``~/.hermes/.env`` from this machine cannot leak into discovery.
EMPTY_HOME = Path(tempfile.mkdtemp(prefix="hibachi_empty_home_"))
(EMPTY_HOME / ".env").write_text("")
os.environ["HERMES_HOME"] = str(EMPTY_HOME)

from plugins.trade.agents import x_hibachi_agent as hibachi  # noqa: E402
from plugins.trade import tradedesk  # noqa: E402
from plugins.trade.canonical import CanonicalCancelGroupResult, CanonicalLadderResult, CanonicalPositionActionResult  # noqa: E402


SAMPLE_ACCOUNT_INFO = {
    "balance": "1998766.543274",
    "maximalWithdraw": "500000.123456",
    "initialMargin": "12000.000000",
    "maintenanceMargin": "8000.000000",
    "totalPositionNotional": "24000.000000",
    "totalOrderNotional": "5000.000000",
    "assets": [{"symbol": "USDT", "quantity": "1998766.543274"}],
    "positions": [],
    "leverages": [],
    "numFreeTransfersRemaining": 96,
}
SAMPLE_EXCHANGE_INFO = {
    "status": "NORMAL",
    "maintenanceWindow": [],
    "feeConfig": {"tradeMakerFeeRate": "0.00015000", "tradeTakerFeeRate": "0.00045000"},
    "futureContracts": [
        {"id": 2, "symbol": "BTC/USDT-P", "displayName": "BTC/USDT Perps",
         "underlyingSymbol": "BTC", "settlementSymbol": "USDT",
         "tickSize": "0.00001", "stepSize": "0.0000000001",
         "minOrderSize": "0.0000000001", "minNotional": "1",
         "underlyingDecimals": 10, "settlementDecimals": 6, "live": True},
        {"id": 1, "symbol": "ETH/USDT-P", "displayName": "ETH/USDT Perps",
         "underlyingSymbol": "ETH", "settlementSymbol": "USDT",
         "tickSize": "0.000001", "stepSize": "0.000000001",
         "minOrderSize": "0.000000001", "minNotional": "1",
         "underlyingDecimals": 9, "settlementDecimals": 6, "live": True},
        {"id": 3, "symbol": "SOL/USDT-P", "displayName": "SOL/USDT Perps",
         "underlyingSymbol": "SOL", "settlementSymbol": "USDT",
         "tickSize": "0.0000001", "stepSize": "0.00000001",
         "minOrderSize": "0.00000001", "minNotional": "1",
         "underlyingDecimals": 8, "settlementDecimals": 6, "live": True},
        {"id": 99, "symbol": "HYPE/USDT-P", "displayName": "HYPE/USDT Perps",
         "underlyingSymbol": "HYPE", "settlementSymbol": "USDT",
         "tickSize": "0.0001", "stepSize": "0.0001",
         "minOrderSize": "0.01", "minNotional": "1",
         "underlyingDecimals": 8, "settlementDecimals": 6, "live": True},
        {"id": 100, "symbol": "DEPRECATED/USDT-P", "displayName": "Dead",
         "underlyingSymbol": "ZZZ", "settlementSymbol": "USDT",
         "tickSize": "0.01", "stepSize": "0.01",
         "minOrderSize": "0.01", "minNotional": "1",
         "underlyingDecimals": 8, "settlementDecimals": 6, "live": False},
    ],
}


def _set_account(alias_upper, *, account_id="128", api_key="AK" + "X" * 40,
                 private_key="PK" + "Y" * 60):
    os.environ[f"HIBACHI_{alias_upper}_ACCOUNTID"] = account_id
    os.environ[f"HIBACHI_{alias_upper}_APIKEY"] = api_key
    os.environ[f"HIBACHI_{alias_upper}_PRIVATEKEY"] = private_key


class _HibachiEnvTest(unittest.TestCase):
    """Base class that wipes HIBACHI_* env around every test."""

    def setUp(self):
        for k in list(os.environ):
            if k.startswith("HIBACHI_"):
                os.environ.pop(k, None)
        hibachi._MarketCache.invalidate()

    def tearDown(self):
        for k in list(os.environ):
            if k.startswith("HIBACHI_"):
                os.environ.pop(k, None)
        hibachi._MarketCache.invalidate()


class TestDiscovery(_HibachiEnvTest):
    def test_1_no_env_no_accounts(self):
        self.assertEqual(hibachi.list_accounts(), [])

    def test_2_two_complete_accounts(self):
        _set_account("BITGET")
        _set_account("MAIN", account_id="200")
        self.assertEqual(hibachi.list_accounts(), ["bitget", "main"])

    def test_3_alias_case_insensitive(self):
        # KAM convention: uppercase prefix, case-insensitive alias.
        # ``HIBACHI_BITGET_*`` and ``HIBACHI_Bitget_*`` both surface
        # the ``bitget`` alias.
        os.environ["HIBACHI_BITGET_APIKEY"] = "k1"
        os.environ["HIBACHI_BITGET_PRIVATEKEY"] = "k2"
        os.environ["HIBACHI_BITGET_ACCOUNTID"] = "128"
        os.environ["HIBACHI_Bitget_APIKEY"] = "kk1"
        os.environ["HIBACHI_Bitget_PRIVATEKEY"] = "kk2"
        os.environ["HIBACHI_Bitget_ACCOUNTID"] = "128"
        self.assertEqual(hibachi.list_accounts(), ["bitget"])

    def test_4_incomplete_account_excluded(self):
        os.environ["HIBACHI_HALF_APIKEY"] = "k1"
        self.assertEqual(hibachi.list_accounts(), [])

    def test_5_dotenv_discovery(self):
        tmp = Path(tempfile.mkdtemp(prefix="hibachi_test_"))
        os.environ["HERMES_HOME"] = str(tmp)
        (tmp / ".env").write_text(
            'HIBACHI_DOTENV_ACCOUNTID=42\n'
            'HIBACHI_DOTENV_APIKEY=ak\n'
            'HIBACHI_DOTENV_PRIVATEKEY=sk\n'
        )
        try:
            self.assertEqual(hibachi.list_accounts(), ["dotenv"])
            creds = hibachi._lookup_credentials("dotenv")
            self.assertEqual(creds["account_id"], "42")
        finally:
            os.environ["HERMES_HOME"] = str(EMPTY_HOME)

    def test_6_duplicate_aliases_dedup(self):
        _set_account("BITGET")
        _set_account("BITGET2", account_id="2")
        self.assertEqual(hibachi.list_accounts(), ["bitget", "bitget2"])

    def test_7_alphabetical_ordering(self):
        for alias in ("ZULU", "ALPHA", "MIKE"):
            _set_account(alias, account_id="1")
        self.assertEqual(hibachi.list_accounts(), ["alpha", "mike", "zulu"])


class TestLookupCredentials(_HibachiEnvTest):
    def test_missing_returns_none(self):
        self.assertIsNone(hibachi._lookup_credentials("nope"))

    def test_valid_returns_dict(self):
        _set_account("MAIN", account_id="999", api_key="mykey", private_key="mypriv")
        creds = hibachi._lookup_credentials("main")
        self.assertEqual(creds["account"], "main")
        self.assertEqual(creds["account_id"], "999")
        self.assertEqual(creds["api_key"], "mykey")
        self.assertEqual(creds["private_key"], "mypriv")

    def test_accountid_must_be_numeric(self):
        _set_account("BAD", account_id="not-a-number")
        self.assertIsNone(hibachi._lookup_credentials("bad"))

    def test_alias_must_match_pattern(self):
        self.assertIsNone(hibachi._lookup_credentials("123starts_with_digit"))
        self.assertIsNone(hibachi._lookup_credentials(""))
        self.assertIsNone(hibachi._lookup_credentials(None))

    def test_uppercase_lookup(self):
        _set_account("MAIN", account_id="42")
        for case in ("main", "MAIN", "Main"):
            with self.subTest(case=case):
                self.assertIsNotNone(hibachi._lookup_credentials(case))


class TestTradeDeskDiscovery(_HibachiEnvTest):
    def test_tradedesk_picks_up_hibachi(self):
        desk = tradedesk.TradeDesk()
        desk._agents = {}
        desk._loaded = False
        exchanges = desk.list_exchanges()
        self.assertIn("hibachi", exchanges)
        agent = desk._agents["hibachi"]
        self.assertEqual(agent.name, "hibachi")
        for attr in ("name", "list_accounts", "capabilities", "execute"):
            self.assertTrue(hasattr(agent, attr))


class TestBalanceDispatch(_HibachiEnvTest):
    def setUp(self):
        super().setUp()
        _set_account("MAIN", account_id="128")

    def test_balance_success(self):
        with mock.patch.object(hibachi, "_request_json", side_effect=lambda *a, **kw: (
            SAMPLE_ACCOUNT_INFO if "/trade/account/info" in a[1] else SAMPLE_EXCHANGE_INFO
        )):
            resp = hibachi.execute({
                "operation": "balance", "account": "main", "exchange": "hibachi",
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.exchange, "hibachi")
        self.assertEqual(resp.account, "main")
        self.assertEqual(resp.balance.value, "1998766.54")
        self.assertEqual(resp.balance.unit, "USDT")
        self.assertEqual(resp.portfolio_summary.account_value, "1998766.54")
        self.assertEqual(resp.portfolio_summary.withdrawable, "500000.12")
        self.assertEqual(resp.portfolio_summary.margin_used, "12000.00")
        self.assertEqual(resp.portfolio_summary.total_position_value, "24000.00")
        self.assertEqual(resp.portfolio_summary.unit, "USDT")

    def test_balance_account_not_found(self):
        resp = hibachi.execute({
            "operation": "balance", "account": "ghost", "exchange": "hibachi",
        })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "ACCOUNT_NOT_FOUND")
        for hint in ("HIBACHI_<alias>_APIKEY", "HIBACHI_<alias>_PRIVATEKEY",
                     "HIBACHI_<alias>_ACCOUNTID"):
            self.assertIn(hint, resp.error.message)

    def test_balance_missing_account(self):
        resp = hibachi.execute({
            "operation": "balance", "account": "", "exchange": "hibachi",
        })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "MISSING_ACCOUNT")

    def test_balance_missing_operation(self):
        resp = hibachi.execute({
            "operation": "", "account": "main", "exchange": "hibachi",
        })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "INVALID_REQUEST")

    def test_balance_network_error_redacted(self):
        def boom(*a, **kw):
            raise RuntimeError(
                "HTTP 500 on /trade/account/info: api_key=AKXXXXXXXXXXXX leaked"
            )
        with mock.patch.object(hibachi, "_request_json", side_effect=boom):
            resp = hibachi.execute({
                "operation": "balance", "account": "main", "exchange": "hibachi",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "BALANCE_UNAVAILABLE")
        self.assertNotIn("AKXXXXXXXXXXXX", resp.error.message)
        self.assertIn("api_key=***", resp.error.message)

    def test_balance_secret_in_trace_scrubbed(self):
        leak = "PK" + "Y" * 60
        def boom(*a, **kw):
            raise RuntimeError(f"internal error: private_key={leak}")
        with mock.patch.object(hibachi, "_request_json", side_effect=boom):
            resp = hibachi.execute({
                "operation": "balance", "account": "main", "exchange": "hibachi",
            })
        self.assertFalse(resp.success)
        self.assertNotIn(leak, resp.error.message)
        self.assertIn("private_key=***", resp.error.message)

    def test_balance_response_missing_field(self):
        with mock.patch.object(hibachi, "_request_json", side_effect=lambda *a, **kw: (
            {"maximalWithdraw": "0"} if "/trade/account/info" in a[1] else SAMPLE_EXCHANGE_INFO
        )):
            resp = hibachi.execute({
                "operation": "balance", "account": "main", "exchange": "hibachi",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "BALANCE_UNAVAILABLE")
        self.assertIn("balance", resp.error.message)

    def test_balance_non_dict_response(self):
        with mock.patch.object(hibachi, "_request_json", return_value=["not", "a", "dict"]):
            resp = hibachi.execute({
                "operation": "balance", "account": "main", "exchange": "hibachi",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "BALANCE_UNAVAILABLE")

    def test_non_balance_returns_not_implemented(self):
        for op in ("cancel_orders", "close_position"):
            with self.subTest(op=op):
                resp = hibachi.execute({
                    "operation": op, "account": "main", "exchange": "hibachi",
                })
                self.assertFalse(resp.success)
                self.assertEqual(resp.error.code, "NOT_IMPLEMENTED")
                self.assertIn(op, resp.error.message)

    def test_request_exception_caught(self):
        with mock.patch.object(hibachi, "_fetch_account_info",
                               side_effect=Exception("boom!")):
            resp = hibachi.execute({
                "operation": "balance", "account": "main", "exchange": "hibachi",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "BALANCE_UNAVAILABLE")

    def test_authorization_header_carries_api_key(self):
        captured = {}
        def fake(method, url, *, headers=None, body=None, timeout=None):
            captured[url] = dict(headers or {})
            if "/trade/account/info" in url:
                return SAMPLE_ACCOUNT_INFO
            return SAMPLE_EXCHANGE_INFO
        with mock.patch.object(hibachi, "_request_json", side_effect=fake):
            hibachi.execute({
                "operation": "balance", "account": "main", "exchange": "hibachi",
            })
        account_info_urls = [u for u in captured if "/trade/account/info" in u]
        self.assertEqual(len(account_info_urls), 1)
        headers = captured[account_info_urls[0]]
        self.assertIn("Authorization", headers)
        self.assertEqual(headers["Authorization"], os.environ["HIBACHI_MAIN_APIKEY"])
        self.assertIn("accountId=128", account_info_urls[0])

    def test_market_metadata_preload_failure_is_nonfatal(self):
        def fake(method, url, *, headers=None, body=None, timeout=None):
            if "/market/exchange-info" in url:
                raise RuntimeError("503 Service Unavailable")
            return SAMPLE_ACCOUNT_INFO
        with mock.patch.object(hibachi, "_request_json", side_effect=fake):
            resp = hibachi.execute({
                "operation": "balance", "account": "main", "exchange": "hibachi",
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.balance.value, "1998766.54")


class TestInstrumentResolver(_HibachiEnvTest):
    def test_canonical_aliases(self):
        cases = [
            ("BTC", "BTC"), ("btc", "BTC"), ("BTCUSDT", "BTC"), ("BTC/USDT", "BTC"),
            ("BTC-USDT-P", "BTC"), ("BTC-PERP", "BTC"), ("WBTC", "BTC"),
            ("ETH", "ETH"), ("ETHUSDC", "ETH"), ("HYPE", "HYPE"),
            ("HYPEUSDT", "HYPE"), ("HYPE/USDT-P", "HYPE"),
            ("SOL", "SOL"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(hibachi._canonical_symbol_from_request(raw), expected)

    def test_canonical_unknown_passthrough(self):
        self.assertEqual(hibachi._canonical_symbol_from_request("XRP"), "XRP")
        self.assertEqual(hibachi._canonical_symbol_from_request(""), "")
        self.assertEqual(hibachi._canonical_symbol_from_request(None), "")

    def test_resolve_btc(self):
        with mock.patch.object(hibachi, "_request_json", return_value=SAMPLE_EXCHANGE_INFO):
            d = hibachi._resolve_canonical_instrument("BTC")
        self.assertEqual(d["symbol"], "BTC/USDT-P")
        self.assertEqual(d["id"], 2)
        self.assertEqual(d["underlying_symbol"], "BTC")
        self.assertEqual(d["settlement_symbol"], "USDT")
        self.assertEqual(d["tick_size"], "0.00001")

    def test_resolve_with_quote_suffix(self):
        with mock.patch.object(hibachi, "_request_json", return_value=SAMPLE_EXCHANGE_INFO):
            d = hibachi._resolve_canonical_instrument("ETH/USDT-P")
        self.assertIsNotNone(d)
        self.assertEqual(d["symbol"], "ETH/USDT-P")

    def test_resolve_unknown_returns_none(self):
        with mock.patch.object(hibachi, "_request_json", return_value=SAMPLE_EXCHANGE_INFO):
            self.assertIsNone(hibachi._resolve_canonical_instrument("NOPE"))

    def test_live_false_excluded(self):
        with mock.patch.object(hibachi, "_request_json", return_value=SAMPLE_EXCHANGE_INFO):
            self.assertIsNone(hibachi._resolve_canonical_instrument("ZZZ"))

    def test_market_cache_reused(self):
        call_count = {"n": 0}
        def counter(*a, **kw):
            call_count["n"] += 1
            return SAMPLE_EXCHANGE_INFO
        with mock.patch.object(hibachi, "_request_json", side_effect=counter):
            hibachi._resolve_canonical_instrument("BTC")
            hibachi._resolve_canonical_instrument("ETH")
            hibachi._resolve_canonical_instrument("SOL")
        self.assertEqual(call_count["n"], 1)

    def test_resolve_instrument_endpoint(self):
        with mock.patch.object(hibachi, "_request_json", return_value=SAMPLE_EXCHANGE_INFO):
            resp = hibachi.execute({
                "operation": "resolve_instrument", "account": "main",
                "exchange": "hibachi", "symbol": "BTC/USDT-P",
            })
        self.assertTrue(resp.success)
        self.assertIsNotNone(resp.instrument)
        self.assertEqual(resp.instrument.symbol, "BTC")
        self.assertEqual(resp.instrument.display_name, "BTC/USDT Perps")
        self.assertEqual(resp.instrument.price_increment, "0.00001")
        self.assertEqual(resp.instrument.size_increment, "0.0000000001")

    def test_resolve_instrument_unknown(self):
        with mock.patch.object(hibachi, "_request_json", return_value=SAMPLE_EXCHANGE_INFO):
            resp = hibachi.execute({
                "operation": "resolve_instrument", "account": "main",
                "exchange": "hibachi", "symbol": "NOPE",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "INSTRUMENT_NOT_FOUND")

    def test_resolve_instrument_missing_symbol(self):
        resp = hibachi.execute({
            "operation": "resolve_instrument", "account": "main",
            "exchange": "hibachi",
        })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "MISSING_SYMBOL")

    def test_duplicate_underlying_keeps_lowest_id(self):
        payload = {"futureContracts": [
            {"id": 50, "symbol": "BTC-OLD", "displayName": "Old",
             "underlyingSymbol": "BTC", "settlementSymbol": "USDT",
             "tickSize": "0.01", "stepSize": "0.01", "minOrderSize": "0.01",
             "minNotional": "1", "underlyingDecimals": 8, "settlementDecimals": 6,
             "live": True},
            {"id": 2, "symbol": "BTC/USDT-P", "displayName": "New",
             "underlyingSymbol": "BTC", "settlementSymbol": "USDT",
             "tickSize": "0.00001", "stepSize": "0.0000000001",
             "minOrderSize": "0.0000000001", "minNotional": "1",
             "underlyingDecimals": 10, "settlementDecimals": 6, "live": True},
        ]}
        with mock.patch.object(hibachi, "_request_json", return_value=payload):
            d = hibachi._resolve_canonical_instrument("BTC")
        self.assertEqual(d["id"], 2)


class TestRedaction(unittest.TestCase):
    def test_redacts_known_markers(self):
        text = "Authorization: Bearer abc123; api_key=zzz; private_key=qqq"
        out = hibachi._redact(text)
        self.assertNotIn("abc123", out)
        self.assertNotIn("api_key=zzz", out)
        self.assertNotIn("private_key=qqq", out)
        self.assertIn("Bearer ***", out)
        self.assertIn("api_key=***", out)
        self.assertIn("private_key=***", out)
        self.assertIn("Authorization:", out)

    def test_redact_empty_input(self):
        self.assertEqual(hibachi._redact(""), "")
        self.assertEqual(hibachi._redact(None), "")

    def test_redacts_authorization_bearer_token_after_space(self):
        out = hibachi._redact("Authorization: Bearer secret_token_value")
        self.assertNotIn("secret_token_value", out)
        self.assertIn("Bearer ***", out)

    def test_redacts_authorization_basic(self):
        out = hibachi._redact("Authorization: Basic dXNlcjpwYXNz")
        self.assertNotIn("dXNlcjpwYXNz", out)
        self.assertIn("Basic ***", out)

    def test_redacts_case_insensitive_marker(self):
        out = hibachi._redact("API_KEY=secretvalue")
        self.assertNotIn("secretvalue", out)
        self.assertIn("api_key=***", out)

    def test_passthrough_non_sensitive(self):
        text = "Everything is fine; status=ok; code=200"
        self.assertEqual(hibachi._redact(text), text)


class TestPositionsOrders(_HibachiEnvTest):
    """Phase 2 tests for the combined positions + open-orders read.

    Every test in this class mocks out both Hibachi HTTP endpoints
    (the live integration test in test_phase7_live_positions.py is
    the source of truth for end-to-end behaviour)."""

    POSITIONS_PAYLOAD = {
        "balance": "20000.000000",
        "maximalWithdraw": "19976.600000",
        "initialMargin": "23.400000",
        "maintenanceMargin": "18.720000",
        "totalPositionNotional": "46.800000",
        "totalOrderNotional": "0.000000",
        "assets": [{"symbol": "USDT", "quantity": "19976.600000"}],
        "leverages": [],
        "numFreeTransfersRemaining": 100,
        "positions": [
            {
                "direction": "Short",
                "entryNotional": "10.302213",
                "notionalValue": "10.225008",
                "quantity": "0.004310550",
                "symbol": "ETH/USDT-P",
                "unrealizedFundingPnl": "-0.000500",
                "unrealizedTradingPnl": "0.077204",
            },
            {
                "direction": "Long",
                "entryNotional": "2.000015",
                "notionalValue": "2.022384",
                "quantity": "0.01470600",
                "symbol": "SOL/USDT-P",
                "unrealizedFundingPnl": "0.000100",
                "unrealizedTradingPnl": "-0.022369",
            },
        ],
    }
    OPEN_ORDERS_PAYLOAD = [
        {
            "orderId": "582870002718998528",
            "clientId": "my-order-1",
            "accountId": 126,
            "contractId": 1,
            "symbol": "ETH/USDT-P",
            "side": "BID",
            "orderType": "LIMIT",
            "price": "2344.032723",
            "totalQuantity": "0.100000000",
            "availableQuantity": "0.100000000",
            "status": "PLACED",
            "creationTime": 1731609679,
        },
        {
            "orderId": "582869893300355075",
            "clientId": None,
            "accountId": 126,
            "contractId": 1,
            "symbol": "ETH/USDT-P",
            "side": "ASK",
            "orderType": "LIMIT",
            "price": "2400.000000",
            "totalQuantity": "0.050000000",
            "availableQuantity": "0.050000000",
            "status": "PLACED",
            "creationTime": 1731609680,
        },
        {
            "orderId": "582870002718998529",
            "clientId": None,
            "accountId": 126,
            "contractId": 3,
            "symbol": "SOL/USDT-P",
            "side": "BID",
            "orderType": "LIMIT",
            "price": "150.000000",
            "totalQuantity": "0.500000000",
            "availableQuantity": "0.500000000",
            "status": "PLACED",
            "creationTime": 1731609681,
        },
    ]

    def setUp(self):
        super().setUp()
        _set_account("MAIN", account_id="30352")

    def _patched_fetch(self, *, account_info=None, open_orders=None,
                       account_info_error=None, open_orders_error=None):
        """Wire up ``_request_json`` to return deterministic fixtures.

        Either ``account_info`` / ``open_orders`` supply a payload, or
        ``account_info_error`` / ``open_orders_error`` raise — both
        paths are exercised in this class.
        """
        account_info = (account_info if account_info is not None
                        else self.POSITIONS_PAYLOAD)
        open_orders = (open_orders if open_orders is not None
                       else self.OPEN_ORDERS_PAYLOAD)

        def fake(method, url, *, headers=None, body=None, timeout=None):
            if "/trade/account/info" in url:
                if account_info_error is not None:
                    raise account_info_error
                return account_info
            if "/trade/orders" in url:
                if open_orders_error is not None:
                    raise open_orders_error
                return open_orders
            if "/market/exchange-info" in url:
                return SAMPLE_EXCHANGE_INFO
            raise AssertionError(f"unexpected URL: {method} {url}")
        return mock.patch.object(hibachi, "_request_json", side_effect=fake)

    def test_positions_orders_success(self):
        with self._patched_fetch():
            resp = hibachi.execute({
                "operation": "positions_orders",
                "account": "main", "exchange": "hibachi",
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.exchange, "hibachi")
        self.assertEqual(resp.account, "main")
        # Open-order count is the raw row count (3 orders across 3 buckets)
        self.assertEqual(resp.open_order_count, 3)
        # Two positions: ETH short, SOL long
        self.assertEqual(len(resp.positions), 2)
        by_symbol = {p.symbol: p for p in resp.positions}
        eth = by_symbol["ETH"]
        sol = by_symbol["SOL"]
        self.assertEqual(eth.side, "short")
        # absolute quantity, formatted to the precision in the source
        self.assertEqual(eth.size, "0.00431055")
        # entry_price = entryNotional / quantity = 10.302213 / 0.004310550
        # ~ 2389.96...; we test to a tolerance
        self.assertTrue(eth.entry_price.startswith("2389"))
        # pnl = trading + funding = 0.077204 + (-0.000500) = 0.076704
        self.assertEqual(eth.pnl, "0.076704")
        # tp/sl are None for Hibachi until the trigger-order surface is wired
        self.assertIsNone(eth.tp)
        self.assertIsNone(eth.sl)
        self.assertEqual(sol.side, "long")
        self.assertEqual(sol.size, "0.014706")
        # pnl = -0.022369 + 0.000100 = -0.022269
        self.assertEqual(sol.pnl, "-0.022269")

    def test_positions_orders_buckets_open_orders(self):
        with self._patched_fetch():
            resp = hibachi.execute({
                "operation": "positions_orders",
                "account": "main", "exchange": "hibachi",
            })
        self.assertTrue(resp.success)
        # Three rows collapse into three (canonical_symbol, side) buckets
        self.assertEqual(len(resp.order_groups), 3)
        by_key = {(g.symbol, g.side): g for g in resp.order_groups}
        eth_buy = by_key[("ETH", "buy")]
        eth_sell = by_key[("ETH", "sell")]
        sol_buy = by_key[("SOL", "buy")]
        # ETH buy side has a single order so vwap == price
        self.assertEqual(eth_buy.order_count, 1)
        self.assertEqual(eth_buy.vwap, "2344.032723")
        self.assertEqual(eth_buy.min_price, "2344.032723")
        self.assertEqual(eth_buy.max_price, "2344.032723")
        # ETH sell side has a single order
        self.assertEqual(eth_sell.order_count, 1)
        self.assertEqual(eth_sell.vwap, "2400")
        # SOL buy side has a single order
        self.assertEqual(sol_buy.order_count, 1)
        self.assertEqual(sol_buy.vwap, "150")

    def test_positions_orders_empty_orders(self):
        with self._patched_fetch(open_orders=[]):
            resp = hibachi.execute({
                "operation": "positions_orders",
                "account": "main", "exchange": "hibachi",
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.open_order_count, 0)
        self.assertEqual(resp.order_groups, [])
        # Positions still surface
        self.assertEqual(len(resp.positions), 2)

    def test_positions_orders_no_positions(self):
        with self._patched_fetch(account_info={
            "balance": "20000.000000", "maximalWithdraw": "20000.000000",
            "initialMargin": "0.000000", "maintenanceMargin": "0.000000",
            "totalPositionNotional": "0.000000", "totalOrderNotional": "0.000000",
            "assets": [], "leverages": [], "numFreeTransfersRemaining": 100,
            "positions": [],
        }):
            resp = hibachi.execute({
                "operation": "positions_orders",
                "account": "main", "exchange": "hibachi",
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.positions, [])
        self.assertEqual(resp.open_order_count, 3)

    def test_positions_orders_account_info_error(self):
        with self._patched_fetch(
            account_info_error=RuntimeError("HTTP 500 on /trade/account/info"),
        ):
            resp = hibachi.execute({
                "operation": "positions_orders",
                "account": "main", "exchange": "hibachi",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "POSITIONS_ORDERS_UNAVAILABLE")
        self.assertIn("/trade/account/info", resp.error.message)

    def test_positions_orders_open_orders_error(self):
        with self._patched_fetch(
            open_orders_error=RuntimeError("HTTP 502 on /trade/orders"),
        ):
            resp = hibachi.execute({
                "operation": "positions_orders",
                "account": "main", "exchange": "hibachi",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "POSITIONS_ORDERS_UNAVAILABLE")
        self.assertIn("/trade/orders", resp.error.message)

    def test_positions_orders_account_not_found(self):
        resp = hibachi.execute({
            "operation": "positions_orders",
            "account": "ghost", "exchange": "hibachi",
        })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "ACCOUNT_NOT_FOUND")
        for hint in ("HIBACHI_<alias>_APIKEY", "HIBACHI_<alias>_PRIVATEKEY",
                     "HIBACHI_<alias>_ACCOUNTID"):
            self.assertIn(hint, resp.error.message)

    def test_positions_orders_drops_unknown_side(self):
        with self._patched_fetch(open_orders=[
            {"orderId": "1", "symbol": "ETH/USDT-P", "side": "GARBAGE",
             "price": "2000", "totalQuantity": "0.1", "availableQuantity": "0.1",
             "orderType": "LIMIT", "status": "PLACED"},
            {"orderId": "2", "symbol": "ETH/USDT-P", "side": "ASK",
             "price": "2100", "totalQuantity": "0.2", "availableQuantity": "0.2",
             "orderType": "LIMIT", "status": "PLACED"},
        ]):
            resp = hibachi.execute({
                "operation": "positions_orders",
                "account": "main", "exchange": "hibachi",
            })
        self.assertTrue(resp.success)
        # The bad-side row is dropped: only the ASK row contributes.
        self.assertEqual(resp.open_order_count, 2)  # raw row count preserved
        self.assertEqual(len(resp.order_groups), 1)
        self.assertEqual(resp.order_groups[0].side, "sell")
        self.assertEqual(resp.order_groups[0].total_size, "0.2")

    def test_positions_orders_dict_wrapped_orders(self):
        # Hibachi's documented success shape is a bare list, but the
        # agent also tolerates ``{"orders": [...]}`` wrappers.
        wrapped = {"orders": self.OPEN_ORDERS_PAYLOAD}
        with self._patched_fetch(open_orders=wrapped):
            resp = hibachi.execute({
                "operation": "positions_orders",
                "account": "main", "exchange": "hibachi",
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.open_order_count, 3)
        self.assertEqual(len(resp.order_groups), 3)

    def test_positions_orders_zero_quantity_drops_row(self):
        with self._patched_fetch(open_orders=[
            {"orderId": "1", "symbol": "ETH/USDT-P", "side": "BID",
             "price": "2000", "totalQuantity": "0", "availableQuantity": "0",
             "orderType": "LIMIT", "status": "PLACED"},
        ]):
            resp = hibachi.execute({
                "operation": "positions_orders",
                "account": "main", "exchange": "hibachi",
            })
        self.assertTrue(resp.success)
        # Row dropped, so 0 buckets, 0 open orders
        self.assertEqual(resp.open_order_count, 1)  # raw row count
        self.assertEqual(resp.order_groups, [])

    def test_positions_orders_vwap_with_multiple_orders_same_side(self):
        with self._patched_fetch(open_orders=[
            {"orderId": "1", "symbol": "ETH/USDT-P", "side": "BID",
             "price": "2000.5", "totalQuantity": "0.5", "availableQuantity": "0.5",
             "orderType": "LIMIT", "status": "PLACED"},
            {"orderId": "2", "symbol": "ETH/USDT-P", "side": "BID",
             "price": "3000.5", "totalQuantity": "0.5", "availableQuantity": "0.5",
             "orderType": "LIMIT", "status": "PLACED"},
        ]):
            resp = hibachi.execute({
                "operation": "positions_orders",
                "account": "main", "exchange": "hibachi",
            })
        self.assertTrue(resp.success)
        g = resp.order_groups[0]
        self.assertEqual(g.order_count, 2)
        self.assertEqual(g.total_size, "1")
        # (2000.5 + 3000.5) / 2 = 2500.5; precision 1 from the source
        self.assertEqual(g.vwap, "2500.5")
        self.assertEqual(g.min_price, "2000.5")
        self.assertEqual(g.max_price, "3000.5")


class TestNewOrder(_HibachiEnvTest):
    def setUp(self):
        super().setUp()
        _set_account("MAIN", account_id="30352", api_key="my-api-key", private_key="my-secret-key")

    def test_build_place_order_buffer_matches_doc_example(self):
        buffer = hibachi._build_hibachi_place_order_buffer(
            nonce=1714701600000000,
            contract_id=2,
            quantity=Decimal("1"),
            price=Decimal("100000"),
            side="sell",
            underlying_decimals=10,
            settlement_decimals=6,
            max_fees_percent=Decimal("0.0005"),
        )
        self.assertEqual(
            buffer.hex(),
            "0006178313c388000000000200000002540be400000000000000000a00000000000000000000c350",
        )

    def test_hibachi_signature_mode_detection(self):
        self.assertTrue(hibachi._looks_like_hibachi_ecdsa_private_key("0x" + "11" * 32))
        self.assertFalse(hibachi._looks_like_hibachi_ecdsa_private_key("my-secret-key"))

    def test_hibachi_hmac_signature_length(self):
        sig = hibachi._sign_hibachi_place_order(
            private_key="my-secret-key",
            nonce=1714701600000000,
            contract_id=2,
            quantity=Decimal("1"),
            price=Decimal("100000"),
            side="sell",
            underlying_decimals=10,
            settlement_decimals=6,
            max_fees_percent=Decimal("0.0005"),
        )
        self.assertEqual(len(sig), 64)
        self.assertRegex(sig, r"^[0-9a-f]{64}$")

    def test_hibachi_ecdsa_signature_length(self):
        sig = hibachi._sign_hibachi_place_order(
            private_key="0x4c0883a69102937d6231471b5dbb6204fe5129617082798ff7b1c0a1f4a9f9b8",
            nonce=1714701600000000,
            contract_id=2,
            quantity=Decimal("1"),
            price=Decimal("100000"),
            side="sell",
            underlying_decimals=10,
            settlement_decimals=6,
            max_fees_percent=Decimal("0.0005"),
        )
        self.assertEqual(len(sig), 130)
        self.assertRegex(sig, r"^[0-9a-f]{130}$")
        self.assertIn(sig[-2:], {"00", "01"})

    def test_hibachi_ecdsa_signature_matches_sdk_serialization(self):
        private_key = "0x4c0883a69102937d6231471b5dbb6204fe5129617082798ff7b1c0a1f4a9f9b8"
        payload = hibachi._build_hibachi_place_order_buffer(
            nonce=1714701600000000,
            contract_id=2,
            quantity=Decimal("1"),
            price=Decimal("100000"),
            side="sell",
            underlying_decimals=10,
            settlement_decimals=6,
            max_fees_percent=Decimal("0.0005"),
        )
        digest = hashlib.sha256(payload).digest()
        key = EthPrivateKey(bytes.fromhex(private_key[2:]))
        signed = key.sign_msg_hash(digest)
        sdk_style = (
            signed.r.to_bytes(32, "big").hex()
            + signed.s.to_bytes(32, "big").hex()
            + signed.v.to_bytes(1, "big").hex()
        )
        kam_style = hibachi._sign_hibachi_place_order(
            private_key=private_key,
            nonce=1714701600000000,
            contract_id=2,
            quantity=Decimal("1"),
            price=Decimal("100000"),
            side="sell",
            underlying_decimals=10,
            settlement_decimals=6,
            max_fees_percent=Decimal("0.0005"),
        )
        self.assertEqual(kam_style, sdk_style)

    def test_hibachi_offline_digest_matches_failed_eth_server_digest(self):
        server_expected_hex = (
            "000658042719c276000000010000000005f5e100"
            "000000000000000200000000000000000000afc8"
        )
        nonce = int.from_bytes(bytes.fromhex(server_expected_hex)[:8], "big", signed=False)
        digest = hibachi._build_hibachi_place_order_buffer(
            nonce=nonce,
            contract_id=1,
            quantity=Decimal("0.1"),
            price=Decimal("2000"),
            side="sell",
            underlying_decimals=9,
            settlement_decimals=6,
            max_fees_percent=Decimal("0.00045"),
        )
        self.assertEqual(digest.hex(), server_expected_hex)

    def test_hibachi_offline_digest_matches_failed_eth_2560_server_digest(self):
        server_expected_hex = (
            "00065804be87c855000000010000000005f5e100"
            "00000000000000028f5c28f5000000000000afc8"
        )
        nonce = int.from_bytes(bytes.fromhex(server_expected_hex)[:8], "big", signed=False)
        digest = hibachi._build_hibachi_place_order_buffer(
            nonce=nonce,
            contract_id=1,
            quantity=Decimal("0.1"),
            price=Decimal("2560"),
            side="sell",
            underlying_decimals=9,
            settlement_decimals=6,
            max_fees_percent=Decimal("0.00045"),
        )
        self.assertEqual(digest.hex(), server_expected_hex)
        self.assertEqual(digest[24:32].hex(), "000000028f5c28f5")

    def test_hibachi_offline_digest_2560_matches_official_sdk(self):
        server_expected_hex = (
            "00065804be87c855000000010000000005f5e100"
            "00000000000000028f5c28f5000000000000afc8"
        )
        sdk_python = Path("/tmp/hibachi_sdk_venv/bin/python")
        if not sdk_python.is_file():
            self.skipTest("hibachi SDK venv fixture not present at /tmp/hibachi_sdk_venv/bin/python")
        script = r'''
import typing
if not hasattr(typing, 'override'):
    def override(func): return func
    typing.override = override
from decimal import Decimal
import json, sys
sys.path.insert(0, '/tmp/hibachi_sdk_repo/python')
from hibachi_xyz.api import HibachiApiClient
from hibachi_xyz.types import Side, FutureContract
server_hex = "00065804be87c855000000010000000005f5e10000000000000000028f5c28f5000000000000afc8"
nonce = int.from_bytes(bytes.fromhex(server_hex)[:8], 'big', signed=False)
contract = FutureContract(
    displayName='ETH/USDT Perps',
    id=1,
    minNotional='1',
    minOrderSize='0.000000001',
    orderbookGranularities=['0.01'],
    initialMarginRate='0.01',
    maintenanceMarginRate='0.005',
    settlementDecimals=6,
    settlementSymbol='USDT',
    status='LIVE',
    stepSize='0.000000001',
    symbol='ETH/USDT-P',
    tickSize='0.01',
    underlyingDecimals=9,
    underlyingSymbol='ETH',
)
client = HibachiApiClient(account_id=30352, api_key='[REDACTED]', private_key='0x' + '11'*32)
payload = client._HibachiApiClient__create_or_update_order_payload(contract, nonce, Decimal('0.1'), Side.ASK, Decimal('0.00045'), Decimal('2560'))
print(json.dumps({'sdk_hex': payload.hex(), 'server_match': payload.hex() == server_hex}))
'''
        result = subprocess.run([sdk_python, "-c", script], capture_output=True, text=True, check=True)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["server_match"])
        nonce = int.from_bytes(bytes.fromhex(server_expected_hex)[:8], "big", signed=False)
        digest = hibachi._build_hibachi_place_order_buffer(
            nonce=nonce,
            contract_id=1,
            quantity=Decimal("0.1"),
            price=Decimal("2560"),
            side="sell",
            underlying_decimals=9,
            settlement_decimals=6,
            max_fees_percent=Decimal("0.00045"),
        )
        self.assertEqual(digest.hex(), payload["sdk_hex"])

    def test_hibachi_price_encoding_2000_exact_integer(self):
        encoded = hibachi._encode_hibachi_price(
            Decimal("2000"),
            underlying_decimals=9,
            settlement_decimals=6,
        )
        self.assertEqual(encoded, 8589934592)
        self.assertEqual(format(encoded, "016x"), "0000000200000000")

    def test_hibachi_price_encoding_2560_truncates_fractional(self):
        exact_scaled = Decimal("2560") * (Decimal(2) ** 32) * (Decimal(10) ** -3)
        self.assertEqual(str(exact_scaled), "10995116277.760")
        encoded = hibachi._encode_hibachi_price(
            Decimal("2560"),
            underlying_decimals=9,
            settlement_decimals=6,
        )
        self.assertEqual(encoded, 10995116277)
        self.assertEqual(format(encoded, "016x"), "000000028f5c28f5")

    def test_hibachi_price_encoding_truncates_below_point_five(self):
        exact_scaled = Decimal("0.26") * (Decimal(2) ** 32) * (Decimal(10) ** -3)
        self.assertEqual(str(exact_scaled), "1116691.49696")
        encoded = hibachi._encode_hibachi_price(
            Decimal("0.26"),
            underlying_decimals=9,
            settlement_decimals=6,
        )
        self.assertEqual(encoded, 1116691)

    def test_hibachi_price_encoding_truncates_above_point_five(self):
        exact_scaled = Decimal("0.29") * (Decimal(2) ** 32) * (Decimal(10) ** -3)
        self.assertEqual(str(exact_scaled), "1245540.51584")
        encoded = hibachi._encode_hibachi_price(
            Decimal("0.29"),
            underlying_decimals=9,
            settlement_decimals=6,
        )
        self.assertEqual(encoded, 1245540)

    def test_hibachi_price_encoding_boundary_around_integer_point(self):
        just_below = hibachi._encode_hibachi_price(
            Decimal("0.33554432"),
            underlying_decimals=9,
            settlement_decimals=6,
        )
        just_above = hibachi._encode_hibachi_price(
            Decimal("0.33554433"),
            underlying_decimals=9,
            settlement_decimals=6,
        )
        self.assertEqual(just_below, 1441151)
        self.assertEqual(just_above, 1441151)

    def test_hibachi_signing_path_uses_no_float_arithmetic(self):
        source = inspect.getsource(hibachi._encode_hibachi_price)
        self.assertNotIn("float(", source)
        self.assertNotIn("round(", source)
        self.assertIn("_decimal_to_uint_truncated", source)

    def test_hibachi_submit_failed_row_is_order_rejected_without_verification(self):
        seen_get = []

        def fake(method, url, *, headers=None, body=None, timeout=None):
            if "/market/exchange-info" in url:
                return SAMPLE_EXCHANGE_INFO
            if method == "POST" and "/trade/orders" in url:
                return {"orders": [{"errorCode": 9, "message": "Invalid signature", "status": "failed"}]}
            if method == "GET":
                seen_get.append(url)
                raise AssertionError(f"verification should not run after explicit rejection: {url}")
            raise AssertionError(f"unexpected request: {method} {url}")

        with mock.patch.object(hibachi, "_request_json", side_effect=fake), \
             mock.patch.object(hibachi, "_next_hibachi_nonce", return_value=1714701600000006):
            resp = hibachi.execute({
                "operation": "new_order",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "SOL",
                "side": "sell",
                "volume": "1",
                "price": "140",
            })
        self.assertFalse(resp.success)
        self.assertIsNotNone(resp.error)
        self.assertIsNotNone(resp.order)
        error = cast(Any, resp.error)
        order = cast(Any, resp.order)
        self.assertEqual(error.code, "ORDER_REJECTED")
        self.assertEqual(order.status, "rejected")
        self.assertFalse(order.verified)
        self.assertIsNotNone(error.exchange_reason)
        self.assertIn("status=failed", error.exchange_reason)
        self.assertIn("message=Invalid signature", error.exchange_reason)
        self.assertEqual(seen_get, [])

    def test_hibachi_offline_signature_verifies_against_derived_public_key(self):
        private_key = "0x4c0883a69102937d6231471b5dbb6204fe5129617082798ff7b1c0a1f4a9f9b8"
        nonce = 1785624719377014
        signature = hibachi._sign_hibachi_place_order(
            private_key=private_key,
            nonce=nonce,
            contract_id=1,
            quantity=Decimal("0.1"),
            price=Decimal("2000"),
            side="sell",
            underlying_decimals=9,
            settlement_decimals=6,
            max_fees_percent=Decimal("0.00045"),
        )
        payload = hibachi._build_hibachi_place_order_buffer(
            nonce=nonce,
            contract_id=1,
            quantity=Decimal("0.1"),
            price=Decimal("2000"),
            side="sell",
            underlying_decimals=9,
            settlement_decimals=6,
            max_fees_percent=Decimal("0.00045"),
        )
        digest = hashlib.sha256(payload).digest()
        key = EthPrivateKey(bytes.fromhex(private_key[2:]))
        self.assertTrue(eth_keys.datatypes.Signature(bytes.fromhex(signature)).verify_msg_hash(digest, key.public_key))

    def test_hibachi_fee_rate_live_eth_case(self):
        self.assertEqual(
            hibachi._encode_hibachi_fee_rate(Decimal("0.00045")),
            45000,
        )

    def test_encode_hibachi_price_rounds_valid_eth_price(self):
        # Live ETH metadata: underlyingDecimals=9, settlementDecimals=6,
        # tickSize=0.01. A valid price of 50.00 maps to 214748364.800 in
        # Hibachi's 2^32 lattice. The official SDK uses ``int(...)`` on the
        # scaled Decimal, so KAM must truncate/floor to 214748364 rather than
        # round half-up.
        encoded = hibachi._encode_hibachi_price(
            Decimal("50"),
            underlying_decimals=9,
            settlement_decimals=6,
        )
        self.assertEqual(encoded, 214748364)

    def test_new_order_success_uses_batch_endpoint(self):
        captured = {}

        def fake(method, url, *, headers=None, body=None, timeout=None):
            captured.setdefault("calls", []).append((method, url, body))
            if "/market/exchange-info" in url:
                return SAMPLE_EXCHANGE_INFO
            if method == "POST" and "/trade/orders" in url:
                captured["submit_body"] = body
                return [{"orderId": "578721673790138368"}]
            if method == "GET" and "/trade/order" in url:
                return {
                    "accountId": 30352,
                    "orderId": "578721673790138368",
                    "symbol": "SOL/USDT-P",
                    "side": "ASK",
                    "price": "125",
                    "totalQuantity": "1",
                    "availableQuantity": "1",
                    "status": "PLACED",
                    "orderType": "LIMIT",
                }
            raise AssertionError(f"unexpected request: {method} {url}")

        with mock.patch.object(hibachi, "_request_json", side_effect=fake), \
             mock.patch.object(hibachi, "_next_hibachi_nonce", return_value=1714701600000000):
            resp = hibachi.execute({
                "operation": "new_order",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "SOL",
                "side": "sell",
                "volume": "1",
                "price": "125",
            })

        self.assertTrue(resp.success)
        self.assertIsNotNone(resp.order)
        self.assertTrue(resp.order.verified)
        self.assertEqual(resp.order.exchange_order_id, 578721673790138368)
        self.assertEqual(resp.order.symbol, "SOL")
        self.assertEqual(resp.order.side, "sell")
        self.assertEqual(resp.order.submitted_volume, "1")
        self.assertEqual(resp.order.submitted_price, "125")

        body = captured["submit_body"]
        self.assertEqual(body["accountId"], 30352)
        self.assertIsInstance(body["accountId"], int)
        self.assertEqual(len(body["orders"]), 1)
        child = body["orders"][0]
        self.assertEqual(child["action"], "place")
        self.assertEqual(child["symbol"], "SOL/USDT-P")
        self.assertEqual(child["side"], "ASK")
        self.assertEqual(child["orderType"], "LIMIT")
        self.assertEqual(child["quantity"], "1")
        self.assertEqual(child["price"], "125")
        self.assertEqual(child["maxFeesPercent"], "0.00045")
        self.assertTrue(isinstance(child["signature"], str) and len(child["signature"]) == 64)

    def test_new_order_verification_falls_back_to_open_orders(self):
        def fake(method, url, *, headers=None, body=None, timeout=None):
            if "/market/exchange-info" in url:
                return SAMPLE_EXCHANGE_INFO
            if method == "POST" and "/trade/orders" in url:
                return [{"orderId": "578721673790138368"}]
            if method == "GET" and "/trade/orders" in url:
                return [{
                    "accountId": 30352,
                    "orderId": "578721673790138368",
                    "symbol": "SOL/USDT-P",
                    "side": "ASK",
                    "price": "125",
                    "totalQuantity": "1",
                    "availableQuantity": "1",
                    "status": "PLACED",
                    "orderType": "LIMIT",
                }]
            if method == "GET" and "/trade/order" in url:
                raise RuntimeError("temporary readback failure")
            raise AssertionError(f"unexpected request: {method} {url}")

        with mock.patch.object(hibachi, "_request_json", side_effect=fake), \
             mock.patch.object(hibachi, "_next_hibachi_nonce", return_value=1714701600000001):
            resp = hibachi.execute({
                "operation": "new_order",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "SOL",
                "side": "sell",
                "volume": "1",
                "price": "125",
            })
        self.assertTrue(resp.success)
        self.assertTrue(resp.order.verified)

    def test_new_order_verification_retries_until_order_visible(self):
        state = {"by_id": 0, "open_orders": 0}

        def fake(method, url, *, headers=None, body=None, timeout=None):
            if "/market/exchange-info" in url:
                return SAMPLE_EXCHANGE_INFO
            if method == "POST" and "/trade/orders" in url:
                return [{"orderId": "578721673790138368"}]
            if method == "GET" and "/trade/order" in url:
                state["by_id"] += 1
                if state["by_id"] < 3:
                    raise RuntimeError("not visible yet")
                return {
                    "accountId": 30352,
                    "orderId": "578721673790138368",
                    "symbol": "SOL/USDT-P",
                    "side": "ASK",
                    "price": "125",
                    "totalQuantity": "1",
                    "availableQuantity": "1",
                    "status": "PLACED",
                    "orderType": "LIMIT",
                }
            if method == "GET" and "/trade/orders" in url:
                state["open_orders"] += 1
                return []
            raise AssertionError(f"unexpected request: {method} {url}")

        with mock.patch.object(hibachi, "_request_json", side_effect=fake), \
             mock.patch.object(hibachi, "_next_hibachi_nonce", return_value=1714701600000002), \
             mock.patch.object(hibachi, "_ORDER_VERIFICATION_ATTEMPTS", 4), \
             mock.patch.object(hibachi.time, "sleep", return_value=None):
            resp = hibachi.execute({
                "operation": "new_order",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "SOL",
                "side": "sell",
                "volume": "1",
                "price": "125",
            })
        self.assertTrue(resp.success)
        self.assertTrue(resp.order.verified)
        self.assertGreaterEqual(state["by_id"], 3)

    def test_new_order_verification_uses_history_fallback(self):
        def fake(method, url, *, headers=None, body=None, timeout=None):
            if "/market/exchange-info" in url:
                return SAMPLE_EXCHANGE_INFO
            if method == "POST" and "/trade/orders" in url:
                return [{"orderId": "578721673790138368"}]
            if method == "GET" and "/trade/orders/history" in url:
                return {
                    "hasMore": False,
                    "orders": [{
                        "accountId": 30352,
                        "orderId": "578721673790138368",
                        "symbol": "SOL/USDT-P",
                        "side": "ASK",
                        "price": "140",
                        "totalQuantity": "1",
                        "filledQuantity": "0",
                        "status": "Placed",
                        "orderType": "LIMIT",
                    }],
                }
            if method == "GET" and "/trade/order" in url:
                raise RuntimeError("not visible by id")
            if method == "GET" and "/trade/orders?" in url:
                return []
            raise AssertionError(f"unexpected request: {method} {url}")

        with mock.patch.object(hibachi, "_request_json", side_effect=fake), \
             mock.patch.object(hibachi, "_next_hibachi_nonce", return_value=1714701600000003), \
             mock.patch.object(hibachi, "_ORDER_VERIFICATION_ATTEMPTS", 2), \
             mock.patch.object(hibachi.time, "sleep", return_value=None):
            resp = hibachi.execute({
                "operation": "new_order",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "SOL",
                "side": "sell",
                "volume": "1",
                "price": "140",
            })
        self.assertTrue(resp.success)
        self.assertTrue(resp.order.verified)

    def test_new_order_verification_failure_surfaces_submit_response_reason(self):
        def fake(method, url, *, headers=None, body=None, timeout=None):
            if "/market/exchange-info" in url:
                return SAMPLE_EXCHANGE_INFO
            if method == "POST" and "/trade/orders" in url:
                return {"orders": [{"orderId": "578721673790138368", "status": "accepted"}]}
            if method == "GET" and "/trade/orders/history" in url:
                return {"hasMore": False, "orders": []}
            if method == "GET" and "/trade/order" in url:
                raise RuntimeError("not visible by id")
            if method == "GET" and "/trade/orders?" in url:
                return []
            raise AssertionError(f"unexpected request: {method} {url}")

        with mock.patch.object(hibachi, "_request_json", side_effect=fake), \
             mock.patch.object(hibachi, "_next_hibachi_nonce", return_value=1714701600000004), \
             mock.patch.object(hibachi, "_ORDER_VERIFICATION_ATTEMPTS", 1), \
             mock.patch.object(hibachi.time, "sleep", return_value=None):
            resp = hibachi.execute({
                "operation": "new_order",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "SOL",
                "side": "sell",
                "volume": "1",
                "price": "140",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "VERIFICATION_FAILED")
        self.assertIn("orderId=578721673790138368", resp.error.exchange_reason)
        self.assertIn("status=accepted", resp.error.exchange_reason)
        self.assertIn("shape=dict.orders", resp.error.exchange_reason)

    def test_new_order_success_with_dict_wrapped_submit_response(self):
        def fake(method, url, *, headers=None, body=None, timeout=None):
            if "/market/exchange-info" in url:
                return SAMPLE_EXCHANGE_INFO
            if method == "POST" and "/trade/orders" in url:
                return {"orders": [{"orderId": "578721673790138368"}]}
            if method == "GET" and "/trade/order" in url:
                return {
                    "accountId": 30352,
                    "orderId": "578721673790138368",
                    "symbol": "SOL/USDT-P",
                    "side": "ASK",
                    "price": "140",
                    "totalQuantity": "1",
                    "availableQuantity": "1",
                    "status": "PLACED",
                    "orderType": "LIMIT",
                }
            raise AssertionError(f"unexpected request: {method} {url}")

        with mock.patch.object(hibachi, "_request_json", side_effect=fake), \
             mock.patch.object(hibachi, "_next_hibachi_nonce", return_value=1714701600000005):
            resp = hibachi.execute({
                "operation": "new_order",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "SOL",
                "side": "sell",
                "volume": "1",
                "price": "140",
            })
        self.assertTrue(resp.success)
        self.assertTrue(resp.order.verified)
        self.assertEqual(resp.order.exchange_order_id, 578721673790138368)

    def test_new_order_invalid_client_id(self):
        with mock.patch.object(hibachi, "_request_json", return_value=SAMPLE_EXCHANGE_INFO):
            resp = hibachi.execute({
                "operation": "new_order",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "SOL",
                "side": "sell",
                "volume": "1",
                "price": "125",
                "client_id": "bad id with spaces",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "INVALID_CLIENT_ID")

    def test_new_order_instrument_not_found(self):
        with mock.patch.object(hibachi, "_request_json", return_value={**SAMPLE_EXCHANGE_INFO, "futureContracts": []}):
            resp = hibachi.execute({
                "operation": "new_order",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "DOGE",
                "side": "buy",
                "volume": "1",
                "price": "1",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "INSTRUMENT_NOT_FOUND")

    def test_new_order_only_limit_supported(self):
        resp = hibachi.execute({
            "operation": "new_order",
            "exchange": "hibachi",
            "account": "main",
            "symbol": "SOL",
            "side": "buy",
            "order_type": "market",
            "volume": "1",
            "price": "125",
        })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "INVALID_ORDER_TYPE")


class TestPositionManagement(_HibachiEnvTest):
    def setUp(self):
        super().setUp()
        _set_account("MAIN", account_id="30352", api_key="my-api-key", private_key="my-secret-key")

    def _btc_account_info(self):
        return {
            **SAMPLE_ACCOUNT_INFO,
            "positions": [{
                "symbol": "BTC/USDT-P",
                "direction": "Long",
                "quantity": "0.0100000000",
                "entryNotional": "650.000000",
                "unrealizedTradingPnl": "5.000000",
                "unrealizedFundingPnl": "-0.200000",
                "notionalValue": "660.000000",
            }],
        }

    def _btc_trigger_orders(self):
        return [
            {
                "orderId": "9101",
                "accountId": 30352,
                "symbol": "BTC/USDT-P",
                "side": "ASK",
                "orderType": "MARKET",
                "status": "PLACED",
                "totalQuantity": "0.0100000000",
                "availableQuantity": "0.0100000000",
                "triggerPrice": "70000",
                "triggerDirection": "HIGH",
                "orderFlags": "REDUCE_ONLY",
            },
            {
                "orderId": "9102",
                "accountId": 30352,
                "symbol": "BTC/USDT-P",
                "side": "ASK",
                "orderType": "MARKET",
                "status": "PLACED",
                "totalQuantity": "0.0100000000",
                "availableQuantity": "0.0100000000",
                "triggerPrice": "50000",
                "triggerDirection": "LOW",
                "orderFlags": "REDUCE_ONLY",
            },
        ]

    def test_positions_management_surfaces_tp_sl(self):
        def fake(method, url, *, headers=None, body=None, timeout=None):
            if "/trade/account/info" in url:
                return self._btc_account_info()
            if method == "GET" and "/trade/orders" in url:
                return self._btc_trigger_orders()
            if "/market/exchange-info" in url:
                return SAMPLE_EXCHANGE_INFO
            raise AssertionError(f"unexpected request: {method} {url}")

        with mock.patch.object(hibachi, "_request_json", side_effect=fake):
            resp = hibachi.execute({
                "operation": "positions_management",
                "exchange": "hibachi",
                "account": "main",
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.operation, "positions_management")
        self.assertEqual(len(resp.positions or []), 1)
        position = cast(Any, (resp.positions or [])[0])
        self.assertEqual(position.symbol, "BTC")
        self.assertEqual(position.tp, "70000")
        self.assertEqual(position.sl, "50000")
        self.assertEqual(position.tp_count, 1)
        self.assertEqual(position.sl_count, 1)

    def test_set_tp_replaces_existing_tp(self):
        captured = {"delete": [], "post": []}
        state = {"phase": "before"}

        def fake(method, url, *, headers=None, body=None, timeout=None):
            if "/market/exchange-info" in url:
                return SAMPLE_EXCHANGE_INFO
            if "/trade/account/info" in url:
                return self._btc_account_info()
            if method == "GET" and "/trade/orders/history" in url:
                return {"orders": []}
            if method == "GET" and "/trade/orders" in url:
                if state["phase"] == "before":
                    return self._btc_trigger_orders()
                return [{
                    "orderId": "9201",
                    "accountId": 30352,
                    "symbol": "BTC/USDT-P",
                    "side": "ASK",
                    "orderType": "MARKET",
                    "status": "PLACED",
                    "totalQuantity": "0.0100000000",
                    "availableQuantity": "0.0100000000",
                    "triggerPrice": "70000",
                    "triggerDirection": "HIGH",
                    "orderFlags": "REDUCE_ONLY",
                }, {
                    "orderId": "9202",
                    "accountId": 30352,
                    "symbol": "BTC/USDT-P",
                    "side": "ASK",
                    "orderType": "MARKET",
                    "status": "PLACED",
                    "totalQuantity": "0.0100000000",
                    "availableQuantity": "0.0100000000",
                    "triggerPrice": "50000",
                    "triggerDirection": "LOW",
                    "orderFlags": "REDUCE_ONLY",
                }]
            if method == "DELETE" and "/trade/order" in url:
                captured["delete"].append(body)
                state["phase"] = "after_cancel"
                return {}
            if method == "POST" and "/trade/order" in url:
                captured["post"].append(body)
                state["phase"] = "after_submit"
                return {"orderId": "9201"}
            if method == "GET" and "/trade/order" in url:
                if "orderId=9201" in url:
                    return {
                        "orderId": "9201",
                        "accountId": 30352,
                        "symbol": "BTC/USDT-P",
                        "side": "ASK",
                        "orderType": "MARKET",
                        "status": "PLACED",
                        "totalQuantity": "0.0100000000",
                        "availableQuantity": "0.0100000000",
                        "triggerPrice": "70000",
                        "triggerDirection": "HIGH",
                        "orderFlags": "REDUCE_ONLY",
                    }
                raise RuntimeError("not visible by id")
            raise AssertionError(f"unexpected request: {method} {url}")

        with mock.patch.object(hibachi, "_request_json", side_effect=fake), \
             mock.patch.object(hibachi, "_next_hibachi_nonce", return_value=1714701600010000):
            resp = hibachi.execute({
                "operation": "set_tp",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "BTC",
                "price": "70000",
            })
        self.assertTrue(resp.success)
        self.assertEqual(len(captured["delete"]), 1)
        self.assertEqual(len(captured["post"]), 1)
        self.assertIsNotNone(resp.position_action)
        action = cast(Any, resp.position_action)
        self.assertIsInstance(action, CanonicalPositionActionResult)
        self.assertEqual(action.operation, "set_tp")
        self.assertEqual(action.symbol, "BTC")
        self.assertEqual(action.price, "70000")
        self.assertEqual(action.removed, False)
        self.assertTrue(action.verified)
        self.assertEqual(captured["delete"][0]["orderId"], "9101")
        self.assertEqual(captured["post"][0]["orderFlags"], "REDUCE_ONLY")
        self.assertEqual(captured["post"][0]["triggerPrice"], "70000")
        self.assertEqual(captured["post"][0]["triggerDirection"], "HIGH")
        self.assertEqual(captured["post"][0]["side"], "ASK")

    def test_set_tp_zero_removes_existing_tp(self):
        captured = {"delete": []}

        def fake(method, url, *, headers=None, body=None, timeout=None):
            if "/market/exchange-info" in url:
                return SAMPLE_EXCHANGE_INFO
            if "/trade/account/info" in url:
                return self._btc_account_info()
            if method == "GET" and "/trade/orders" in url:
                if captured["delete"]:
                    return [{
                        "orderId": "9102",
                        "accountId": 30352,
                        "symbol": "BTC/USDT-P",
                        "side": "ASK",
                        "orderType": "MARKET",
                        "status": "PLACED",
                        "totalQuantity": "0.0100000000",
                        "availableQuantity": "0.0100000000",
                        "triggerPrice": "50000",
                        "triggerDirection": "LOW",
                        "orderFlags": "REDUCE_ONLY",
                    }]
                return self._btc_trigger_orders()
            if method == "DELETE" and "/trade/order" in url:
                captured["delete"].append(body)
                return {}
            raise AssertionError(f"unexpected request: {method} {url}")

        with mock.patch.object(hibachi, "_request_json", side_effect=fake):
            resp = hibachi.execute({
                "operation": "set_tp",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "BTC",
                "price": "0",
            })
        self.assertTrue(resp.success)
        self.assertEqual(len(captured["delete"]), 1)
        action = cast(Any, resp.position_action)
        self.assertTrue(action.removed)
        self.assertTrue(action.verified)
        self.assertEqual(action.operation, "set_tp")

    def test_set_sl_creates_new_trigger_order(self):
        captured = {"post": []}

        def fake(method, url, *, headers=None, body=None, timeout=None):
            if "/market/exchange-info" in url:
                return SAMPLE_EXCHANGE_INFO
            if "/trade/account/info" in url:
                return self._btc_account_info()
            if method == "GET" and "/trade/orders" in url:
                if captured["post"]:
                    return [{
                        "orderId": "9301",
                        "accountId": 30352,
                        "symbol": "BTC/USDT-P",
                        "side": "ASK",
                        "orderType": "MARKET",
                        "status": "PLACED",
                        "totalQuantity": "0.0100000000",
                        "availableQuantity": "0.0100000000",
                        "triggerPrice": "50000",
                        "triggerDirection": "LOW",
                        "orderFlags": "REDUCE_ONLY",
                    }]
                return []
            if method == "POST" and "/trade/order" in url:
                captured["post"].append(body)
                return {"orderId": "9301"}
            if method == "GET" and "/trade/order" in url:
                return {
                    "orderId": "9301",
                    "accountId": 30352,
                    "symbol": "BTC/USDT-P",
                    "side": "ASK",
                    "orderType": "MARKET",
                    "status": "PLACED",
                    "totalQuantity": "0.0100000000",
                    "availableQuantity": "0.0100000000",
                    "triggerPrice": "50000",
                    "triggerDirection": "LOW",
                    "orderFlags": "REDUCE_ONLY",
                }
            if method == "GET" and "/trade/orders/history" in url:
                return {"orders": []}
            raise AssertionError(f"unexpected request: {method} {url}")

        with mock.patch.object(hibachi, "_request_json", side_effect=fake), \
             mock.patch.object(hibachi, "_next_hibachi_nonce", return_value=1714701600011000):
            resp = hibachi.execute({
                "operation": "set_sl",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "BTC",
                "price": "50000",
            })
        self.assertTrue(resp.success)
        self.assertEqual(len(captured["post"]), 1)
        body = captured["post"][0]
        self.assertEqual(body["orderType"], "MARKET")
        self.assertEqual(body["triggerPrice"], "50000")
        self.assertEqual(body["triggerDirection"], "LOW")
        self.assertEqual(body["orderFlags"], "REDUCE_ONLY")
        self.assertEqual(body["side"], "ASK")
        self.assertIsNone(body.get("price"))

    def test_set_tp_invalid_direction_for_long(self):
        with mock.patch.object(hibachi, "_request_json", side_effect=lambda method, url, **kw: self._btc_account_info() if "/trade/account/info" in url else SAMPLE_EXCHANGE_INFO):
            resp = hibachi.execute({
                "operation": "set_tp",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "BTC",
                "price": "60000",
            })
        self.assertFalse(resp.success)
        self.assertIsNotNone(resp.error)
        self.assertEqual(cast(Any, resp.error).code, "INVALID_TP_PRICE")


class TestLadder(_HibachiEnvTest):
    def setUp(self):
        super().setUp()
        _set_account("MAIN", account_id="30352", api_key="my-api-key", private_key="my-secret-key")

    def test_ladder_success_uses_batch_endpoint(self):
        captured = {}

        def fake(method, url, *, headers=None, body=None, timeout=None):
            captured.setdefault("calls", []).append((method, url, body))
            if "/market/exchange-info" in url:
                return SAMPLE_EXCHANGE_INFO
            if method == "POST" and "/trade/orders" in url:
                captured["submit_body"] = body
                return {"orders": [
                    {"orderId": "7001"},
                    {"orderId": "7002"},
                    {"orderId": "7003"},
                ]}
            if method == "GET" and "/trade/order" in url:
                if "orderId=7001" in url:
                    return {
                        "accountId": 30352,
                        "orderId": "7001",
                        "symbol": "SOL/USDT-P",
                        "side": "ASK",
                        "price": "100",
                        "totalQuantity": "1",
                        "availableQuantity": "1",
                        "status": "PLACED",
                        "orderType": "LIMIT",
                    }
                if "orderId=7002" in url:
                    return {
                        "accountId": 30352,
                        "orderId": "7002",
                        "symbol": "SOL/USDT-P",
                        "side": "ASK",
                        "price": "110",
                        "totalQuantity": "1",
                        "availableQuantity": "1",
                        "status": "PLACED",
                        "orderType": "LIMIT",
                    }
                if "orderId=7003" in url:
                    return {
                        "accountId": 30352,
                        "orderId": "7003",
                        "symbol": "SOL/USDT-P",
                        "side": "ASK",
                        "price": "120",
                        "totalQuantity": "1",
                        "availableQuantity": "1",
                        "status": "PLACED",
                        "orderType": "LIMIT",
                    }
            if method == "GET" and "/trade/orders?" in url:
                return []
            if method == "GET" and "/trade/orders/history" in url:
                return {"orders": []}
            raise AssertionError(f"unexpected request: {method} {url}")

        with mock.patch.object(hibachi, "_request_json", side_effect=fake), \
             mock.patch.object(hibachi, "_next_hibachi_nonce", return_value=1714701600001000):
            resp = hibachi.execute({
                "operation": "ladder",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "SOL",
                "side": "sell",
                "distribution": "uniform",
                "order_count": "3",
                "total_volume": "3",
                "start_price": "100",
                "end_price": "120",
            })

        self.assertTrue(resp.success)
        self.assertIsNotNone(resp.ladder)
        result = cast(Any, resp.ladder)
        self.assertIsInstance(result, CanonicalLadderResult)
        self.assertTrue(result.verified)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.requested_order_count, 3)
        self.assertEqual(result.submitted_order_count, 3)
        self.assertEqual(result.accepted_child_count, 3)
        self.assertEqual(result.batch_count, 1)
        self.assertEqual(result.child_order_ids, [7001, 7002, 7003])

        body = captured["submit_body"]
        self.assertEqual(body["accountId"], 30352)
        self.assertEqual(len(body["orders"]), 3)
        prices = [child["price"] for child in body["orders"]]
        quantities = [child["quantity"] for child in body["orders"]]
        sides = [child["side"] for child in body["orders"]]
        self.assertEqual(prices, ["100", "110", "120"])
        self.assertEqual(quantities, ["1", "1", "1"])
        self.assertEqual(sides, ["ASK", "ASK", "ASK"])
        self.assertEqual([child["nonce"] for child in body["orders"]], [1714701600001000, 1714701600001001, 1714701600001002])

    def test_ladder_explicit_failed_row_is_order_rejected_without_verification(self):
        def fake(method, url, *, headers=None, body=None, timeout=None):
            if "/market/exchange-info" in url:
                return SAMPLE_EXCHANGE_INFO
            if method == "POST" and "/trade/orders" in url:
                return {"orders": [
                    {"orderId": "7001"},
                    {"status": "failed", "message": "Invalid signature"},
                    {"orderId": "7003"},
                ]}
            raise AssertionError(f"unexpected request: {method} {url}")

        with mock.patch.object(hibachi, "_request_json", side_effect=fake), \
             mock.patch.object(hibachi, "_next_hibachi_nonce", return_value=1714701600002000), \
             mock.patch.object(hibachi, "_verify_submitted_order", side_effect=AssertionError("verification must not run after explicit rejection")):
            resp = hibachi.execute({
                "operation": "ladder",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "SOL",
                "side": "sell",
                "distribution": "uniform",
                "order_count": "3",
                "total_volume": "3",
                "start_price": "100",
                "end_price": "120",
            })
        self.assertFalse(resp.success)
        self.assertIsNotNone(resp.error)
        self.assertEqual(cast(Any, resp.error).code, "ORDER_REJECTED")
        self.assertIn("status=failed", cast(Any, resp.error).exchange_reason)
        self.assertIsNotNone(resp.ladder)
        result = cast(Any, resp.ladder)
        self.assertEqual(result.accepted_child_count, 2)
        self.assertEqual(result.submitted_order_count, 2)
        self.assertTrue(result.partial)

    def test_ladder_invalid_direction(self):
        resp = hibachi.execute({
            "operation": "ladder",
            "exchange": "hibachi",
            "account": "main",
            "symbol": "SOL",
            "side": "sell",
            "distribution": "uniform",
            "order_count": "3",
            "total_volume": "3",
            "start_price": "120",
            "end_price": "100",
        })
        self.assertFalse(resp.success)
        self.assertIsNotNone(resp.error)
        self.assertEqual(cast(Any, resp.error).code, "INVALID_LADDER_DIRECTION")

    def test_ladder_reconciles_children_below_min_notional(self):
        descriptor = {
            "symbol": "SOL/USDT-P",
            "tick_size": "0.01",
            "step_size": "0.01",
            "min_order_size": "0.01",
            "min_notional": "1",
        }
        requests, submitted_volume = hibachi._build_hibachi_ladder_order_requests(
            descriptor=descriptor,
            side="sell",
            distribution="uniform",
            order_count=4,
            total_volume=Decimal("0.23"),
            start_price=Decimal("10"),
            end_price=Decimal("40"),
        )
        final_requests, omitted_below_minimum, reason = hibachi._reconcile_hibachi_ladder_children(
            requests,
            min_order_size=Decimal("0.01"),
            min_notional=Decimal("1"),
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.01"),
        )
        self.assertEqual(reason, "")
        self.assertEqual(omitted_below_minimum, 1)
        self.assertEqual(len(final_requests), 3)
        self.assertEqual(sum(Decimal(str(r["quantity"])) for r in final_requests), submitted_volume)
        for request in final_requests:
            self.assertGreaterEqual(Decimal(str(request["quantity"])) * Decimal(str(request["price"])), Decimal("1"))

    def test_ladder_unsupported_distribution(self):
        with mock.patch.object(hibachi, "_request_json", return_value=SAMPLE_EXCHANGE_INFO):
            resp = hibachi.execute({
                "operation": "ladder",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "SOL",
                "side": "sell",
                "distribution": "triangle",
                "order_count": "3",
                "total_volume": "3",
                "start_price": "100",
                "end_price": "120",
            })
        self.assertFalse(resp.success)
        self.assertIsNotNone(resp.error)
        self.assertEqual(cast(Any, resp.error).code, "UNSUPPORTED_DISTRIBUTION")


class TestCancelOrderGroup(_HibachiEnvTest):
    def setUp(self):
        super().setUp()
        _set_account("MAIN", account_id="30352", api_key="my-api-key", private_key="my-secret-key")

    def test_build_cancel_order_request_matches_sdk_shape(self):
        request = hibachi._build_hibachi_cancel_request_data(
            private_key="my-secret-key",
            order_id=597841472116490240,
            nonce=None,
        )
        self.assertEqual(request["orderId"], "597841472116490240")
        expected_sig = hmac.new(
            b"my-secret-key",
            int(597841472116490240).to_bytes(8, "big", signed=False),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(request["signature"], expected_sig)

    def test_cancel_order_group_success_exact_scope(self):
        pre_orders = [
            {
                "orderId": "11",
                "symbol": "ETH/USDT-P",
                "side": "ASK",
                "price": "2300.000000",
                "totalQuantity": "0.050000000",
                "availableQuantity": "0.050000000",
                "status": "PLACED",
            },
            {
                "orderId": "12",
                "symbol": "ETH/USDT-P",
                "side": "ASK",
                "price": "2300.000000",
                "totalQuantity": "0.050000000",
                "availableQuantity": "0.050000000",
                "status": "PLACED",
            },
            {
                "orderId": "13",
                "symbol": "ETH/USDT-P",
                "side": "BID",
                "price": "2200.000000",
                "totalQuantity": "0.100000000",
                "availableQuantity": "0.100000000",
                "status": "PLACED",
            },
            {
                "orderId": "14",
                "symbol": "SOL/USDT-P",
                "side": "ASK",
                "price": "120.0000000",
                "totalQuantity": "1.00000000",
                "availableQuantity": "1.00000000",
                "status": "PLACED",
            },
        ]
        post_orders = [pre_orders[2], pre_orders[3]]
        with mock.patch.object(hibachi, "_fetch_open_orders", side_effect=[pre_orders, post_orders]), \
             mock.patch.object(hibachi, "_submit_cancel_order", side_effect=[{}, {}]) as submit_cancel:
            resp = hibachi.execute({
                "operation": "cancel_order_group",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "ETH",
                "side": "sell",
            })
        self.assertTrue(resp.success)
        self.assertIsNotNone(resp.cancel_group)
        result = cast(Any, resp.cancel_group)
        self.assertIsInstance(result, CanonicalCancelGroupResult)
        self.assertEqual(result.symbol, "ETH")
        self.assertEqual(result.side, "sell")
        self.assertEqual(result.targeted_order_count, 2)
        self.assertEqual(result.cancelled_order_count, 2)
        self.assertEqual(result.confirmed_absent_count, 2)
        self.assertEqual(result.remaining_target_count, 0)
        self.assertTrue(result.verified)
        self.assertFalse(result.partial)
        self.assertEqual(result.batch_count, 2)
        self.assertEqual(
            [call.kwargs["order_id"] for call in submit_cancel.call_args_list],
            [11, 12],
        )

    def test_cancel_order_group_no_target_orders(self):
        pre_orders = [
            {
                "orderId": "14",
                "symbol": "SOL/USDT-P",
                "side": "ASK",
                "price": "120.0000000",
                "totalQuantity": "1.00000000",
                "availableQuantity": "1.00000000",
                "status": "PLACED",
            },
        ]
        with mock.patch.object(hibachi, "_fetch_open_orders", return_value=pre_orders):
            resp = hibachi.execute({
                "operation": "cancel_order_group",
                "exchange": "hibachi",
                "account": "main",
                "symbol": "ETH",
                "side": "sell",
            })
        self.assertFalse(resp.success)
        self.assertIsNotNone(resp.error)
        self.assertEqual(cast(Any, resp.error).code, "NO_TARGET_ORDERS")


class TestCapabilities(unittest.TestCase):
    def test_capabilities_advertise_balance_only(self):
        self.assertEqual(
            hibachi.capabilities(),
            [
                "balance",
                "positions_orders",
                "positions_management",
                "new_order",
                "cancel_order_group",
                "ladder",
                "set_tp",
                "set_sl",
                "resolve_instrument",
            ],
        )


class TestIsolation(unittest.TestCase):
    def test_no_imports_from_other_agents(self):
        text = (_REPO / "plugins/trade/agents/x_hibachi_agent.py").read_text()
        for other in ("x_hyperliquid_agent", "x_arcus_agent", "x_apex_agent",
                      "x_rise_agent", "x_lighter_agent", "x_raydium_agent"):
            self.assertNotIn(other, text,
                             f"hibachi agent must not reference {other}")

    def test_no_tradedesk_modification(self):
        text = (_REPO / "plugins/trade/tradedesk.py").read_text()
        self.assertNotIn("hibachi", text)

    def test_no_canonical_modification(self):
        text = (_REPO / "plugins/trade/canonical.py").read_text()
        self.assertNotIn("hibachi", text.lower())

    def test_wizard_untouched(self):
        text = (_REPO / "plugins/trade/wizard.py").read_text()
        self.assertNotIn("hibachi", text.lower())

    def test_only_one_file_added(self):
        added = []
        for p in (_REPO / "plugins/trade/agents").iterdir():
            if "hibachi" in p.name.lower():
                added.append(p.name)
        self.assertEqual(added, ["x_hibachi_agent.py"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
