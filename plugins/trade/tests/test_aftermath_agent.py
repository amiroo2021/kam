from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.trade import tradedesk  # noqa: E402
from plugins.trade.agents import x_aftermath_agent as aftermath  # noqa: E402
from plugins.trade.canonical import CanonicalPortfolioSummary  # noqa: E402
from plugins.trade.wizard import TradeWizard  # noqa: E402


class AftermathAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = {k: v for k, v in os.environ.items() if k.startswith("AFTERMATH_")}
        self._saved_hermes_home = os.environ.get("HERMES_HOME")
        for key in list(os.environ):
            if key.startswith("AFTERMATH_"):
                os.environ.pop(key)
        self.home = tempfile.mkdtemp()
        os.environ["HERMES_HOME"] = self.home
        os.environ["AFTERMATH_DRY_RUN"] = "1"
        Path(self.home, ".env").write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        for key in list(os.environ):
            if key.startswith("AFTERMATH_"):
                os.environ.pop(key)
        os.environ.update(self._saved_env)
        if self._saved_hermes_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._saved_hermes_home

    def set_account(self, alias: str = "MAIN") -> None:
        # Use a freshly generated suiprivkey so the LIVE sign path can decode it.
        # We don't override the address here — tests that exercise the LIVE path
        # override it themselves with a derived BLAKE2b Sui address.
        import secrets

        from pysui_fastcrypto import encode_bech32

        priv = encode_bech32(b"\x00" + secrets.token_bytes(32), "suiprivkey")
        os.environ[f"AFTERMATH_{alias}_AGENT_ADDRESS"] = "0x" + "1" * 64
        os.environ[f"AFTERMATH_{alias}_AGENT_PRIVATEKEY"] = priv
        os.environ[f"AFTERMATH_{alias}_ACCOUNT_ID"] = "12345"

    def native_positions_payload(self):
        return {
            "accounts": [
                {
                    "accountId": "12345n",
                    "totalEquityUsd": 100.125,
                    "availableCollateral": 80,
                    "availableCollateralUsd": 80,
                    "totalUnrealizedFundingsUsd": 0,
                    "totalUnrealizedPnlUsd": 3.5,
                    "positions": [
                        {
                            "marketId": "0x435987c9e1b8f61a4cdfa220751b3324ebf5a7bfd1250b25c79412a71a20392f",
                            "baseAssetAmount": "2",
                            "quoteAssetNotionalAmount": "40",
                            "entryPrice": "20",
                            "unrealizedPnlUsd": "3.5",
                            # In real Aftermath, positions-embedded pending orders carry
                            # only orderId/side/currentSize/initialSize (no price).
                            # Real price/size/remaining live behind /api/ccxt/myPendingOrders.
                            "pendingOrders": [
                                {"orderId": "11", "side": 0, "currentSize": "1000000n", "initialSize": "1000000n"},
                                {"orderId": "12", "side": 0, "currentSize": "2000000n", "initialSize": "2000000n"},
                            ],
                            "collateral": 1,
                            "cumFundingRateLong": 0,
                            "cumFundingRateShort": 0,
                            "asksQuantity": 0,
                            "bidsQuantity": 0,
                            "leverage": 2,
                            "collateralUsd": 40,
                            "marginRatio": 0.5,
                            "freeMarginUsd": 80,
                            "freeCollateral": 80,
                            "unrealizedFundingsUsd": 0,
                            "liquidationPrice": 10,
                        }
                    ],
                }
            ]
        }

    def ccxt_pending_orders_payload(self, ch_id: str) -> list:
        """Return CCXT-shaped open orders for the requested market.

        Mirrors what real Aftermath returns from ``/api/ccxt/myPendingOrders``
        for a market with two open limit orders at 19 and 18.
        """
        if ch_id == "0x435987c9e1b8f61a4cdfa220751b3324ebf5a7bfd1250b25c79412a71a20392f":
            return [
                {
                    "id": "11",
                    "datetime": "2026-09-28 00:00:00.000 UTC",
                    "timestamp": 1790544000000,
                    "status": "open",
                    "symbol": "ZEC/USD:USDC",
                    "type": "limit",
                    "side": "buy",
                    "price": 19,
                    "amount": 1,
                    "filled": 0,
                    "remaining": 1,
                    "cost": 0,
                    "trades": [],
                    "fee": {},
                },
                {
                    "id": "12",
                    "datetime": "2026-09-28 00:00:00.000 UTC",
                    "timestamp": 1790544000000,
                    "status": "open",
                    "symbol": "ZEC/USD:USDC",
                    "type": "limit",
                    "side": "buy",
                    "price": 18,
                    "amount": 2,
                    "filled": 0,
                    "remaining": 2,
                    "cost": 0,
                    "trades": [],
                    "fee": {},
                },
            ]
        return []

    def fake_native_post(self, credentials, path, payload):
        if path == "/api/perpetuals/accounts/positions":
            return self.native_positions_payload()
        if path == "/api/perpetuals/all-markets":
            return {"markets": [{"objectId": "0x5072ccd95e6ff7bd724f89aa8b8dc58d31f09a93467ef7b694e9f24c50a1e49a", "marketParams": {"baseAssetSymbol": "SOLUSD", "tickSize": "10000n", "lotSize": "10000n", "scalingFactor": 1e-6, "minOrderUsdValue": 1.0}}]}
        if path == "/api/perpetuals/markets":
            return {"markets": [{"marketId": "0x5072ccd95e6ff7bd724f89aa8b8dc58d31f09a93467ef7b694e9f24c50a1e49a", "symbol": "SOL"}]}
        if path == "/api/perpetuals/markets/prices":
            return {"marketsPrices": [{"marketId": "0x5072ccd95e6ff7bd724f89aa8b8dc58d31f09a93467ef7b694e9f24c50a1e49a", "basePrice": 19, "midPrice": 20, "markPrice": 21}]}
        if path == "/api/perpetuals/account/max-order-size":
            # Cap at 100 SOL per order — large enough that no test ladder trips it.
            return {"maxOrderSize": "100000000n"}
        if path == "/api/ccxt/accounts":
            # Capability object ID (the on-chain object Aftermath's build paths want).
            return [
                {"id": "0x55d70f824bde7d1a5541e5fb4322d1dc53a1ec9c36e827f17469b70b1b310563", "type": "capability", "accountNumber": "672"},
                {"id": "0xf65389a94968d9ef054637498c212f154738c4a24ce229314dac9e3ae2681f3d", "type": "account", "accountNumber": "672"},
            ]
        if path == "/api/ccxt/myPendingOrders":
            ch = (payload or {}).get("chId") or ""
            return self.ccxt_pending_orders_payload(ch)
        if path.startswith("/api/ccxt/build/"):
            # Build routes return {transactionBytes, signingDigest}.
            import base64
            return {
                "transactionBytes": base64.b64encode(b"FAKE_TX_BYTES").decode(),
                "signingDigest": base64.b64encode(b"0123456789abcdef0123456789abcdef").decode(),
            }
        if path.startswith("/api/ccxt/submit/"):
            return {"digest": "0xFAKEDIGEST", "txDigest": "0xFAKEDIGEST"}
        raise AssertionError(f"unexpected POST path in fake: {path}")

    def test_discovery_requires_complete_triplet(self) -> None:
        os.environ["AFTERMATH_HALF_AGENT_ADDRESS"] = "0x" + "1" * 64
        os.environ["AFTERMATH_HALF_ACCOUNT_ID"] = "12345"
        self.assertEqual(aftermath.list_accounts(), [])
        self.set_account()
        self.assertEqual(aftermath.list_accounts(), ["main"])

    def test_dotenv_discovery_and_lookup(self) -> None:
        Path(self.home, ".env").write_text(
            "\n".join(
                [
                    "AFTERMATH_TEST_AGENT_ADDRESS=0x" + "2" * 64,
                    "AFTERMATH_TEST_AGENT_PRIVATEKEY=suiprivkey1example",
                    "AFTERMATH_TEST_ACCOUNT_ID=12345",
                ]
            ),
            encoding="utf-8",
        )
        self.assertEqual(aftermath.list_accounts(), ["test"])
        creds = aftermath._lookup_credentials("test")
        self.assertIsNotNone(creds)
        self.assertEqual(creds["account_id"], "12345")

    def test_tradedesk_discovers_agent(self) -> None:
        self.assertIn("aftermath", tradedesk.TradeDesk().list_exchanges())

    def test_capabilities_full_native_surface(self) -> None:
        for operation in (
            "balance", "positions_orders", "new_order", "ladder", "cancel_orders",
            "close_position", "set_tp", "set_sl", "set_leverage",
        ):
            self.assertIn(operation, aftermath.capabilities())

    def test_balance_uses_native_perpetuals_positions(self) -> None:
        self.set_account()
        with mock.patch.object(aftermath, "_post_json", side_effect=self.fake_native_post) as post:
            response = aftermath.execute({"operation": "balance", "exchange": "aftermath", "account": "main"})
        self.assertTrue(response.success)
        self.assertEqual(response.balance.value, "100.13")
        self.assertEqual(response.balance.unit, "USD")
        self.assertEqual(response.portfolio_summary.account_value, "100.13")
        self.assertEqual(response.portfolio_summary.withdrawable, "80.00")
        self.assertEqual(response.positions[0].symbol, "ZEC")
        self.assertEqual(response.positions[0].side, "long")
        self.assertEqual(response.positions[0].size, "2.00")
        self.assertEqual(response.positions[0].entry_price, "20.00")
        self.assertEqual(response.open_order_count, 2)
        self.assertEqual(post.call_args_list[0].args[1], "/api/perpetuals/accounts/positions")

    def test_positions_orders_groups_native_pending_orders(self) -> None:
        """Open orders must be aggregated with real prices from /api/ccxt/myPendingOrders,
        not the price-less position-embedded pendingOrders."""
        self.set_account()
        captured = []

        def trace_post(credentials, path, payload):
            captured.append(path)
            return self.fake_native_post(credentials, path, payload)

        with mock.patch.object(aftermath, "_post_json", side_effect=trace_post):
            response = aftermath.execute({"operation": "positions_orders", "exchange": "aftermath", "account": "main"})
        self.assertTrue(response.success)
        self.assertEqual(len(response.positions), 1)
        self.assertEqual(len(response.order_groups), 1)
        g = response.order_groups[0]
        self.assertEqual(g.symbol, "ZEC")
        self.assertEqual(g.side, "buy")
        self.assertEqual(g.order_count, 2)
        self.assertEqual(g.total_size, "3")
        # size-weighted VWAP = (1*19 + 2*18) / (1+2) = 18.333... (26-digit precision).
        self.assertEqual(g.vwap, "18.33333333333333333333333333")
        self.assertEqual(g.min_price, "18")
        self.assertEqual(g.max_price, "19")
        self.assertIn("/api/ccxt/myPendingOrders", captured)

    def test_no_ccxt_account_build_paths_remain_in_agent_source(self) -> None:
        """Forbid CCXT account endpoints (read or build paths), but permit
        Aftermath's CCXT ``/api/ccxt/submit/*`` family — that is the correct
        native route for submitting signed PTBs.
        """
        source = Path(aftermath.__file__).read_text(encoding="utf-8")
        # The Python ccxt library must not be imported.
        self.assertNotIn("import ccxt", source)
        # CCXT account endpoints (read or build) must be absent.
        forbidden_ccxt = [
            "/api/ccxt/exchangeInfo",
            "/api/ccxt/account/balance",
            "/api/ccxt/account/positions",
            "/api/ccxt/account/orders",
            "/api/ccxt/account/transactions/",
        ]
        for token in forbidden_ccxt:
            self.assertNotIn(token, source, f"unexpected CCXT path {token!r} in agent source")

    def test_resolve_instrument_and_market_price_are_live_constructor_compatible(self) -> None:
        self.set_account()
        with mock.patch.object(aftermath, "_post_json", side_effect=self.fake_native_post):
            resolved = aftermath.execute({"operation": "resolve_instrument", "exchange": "aftermath", "account": "main", "symbol": "SOL"})
            priced = aftermath.execute({"operation": "market_price", "exchange": "aftermath", "account": "main", "symbol": "SOL"})
        self.assertTrue(resolved.success)
        self.assertEqual(resolved.instrument.symbol, "SOL")
        self.assertEqual(resolved.instrument.display_name, "SOL")
        self.assertEqual(resolved.instrument.price_increment, "0.01")
        self.assertEqual(resolved.data["market_id"], "0x5072ccd95e6ff7bd724f89aa8b8dc58d31f09a93467ef7b694e9f24c50a1e49a")
        self.assertTrue(priced.success)
        self.assertEqual(priced.market_price.market, "SOL")
        self.assertEqual(priced.market_price.mark_price, "21.00")

    def test_new_order_dry_run_builds_native_limit_transaction(self) -> None:
        self.set_account()
        calls = []

        def fake(credentials, path, payload):
            calls.append((path, payload))
            return self.fake_native_post(credentials, path, payload)

        with mock.patch.object(aftermath, "_post_json", side_effect=fake):
            response = aftermath.execute({
                "operation": "new_order", "exchange": "aftermath", "account": "main",
                "symbol": "SOL-PERP", "side": "buy", "order_type": "limit", "volume": "1", "price": "20",
                "dry_run": True,
            })
        self.assertTrue(response.success)
        self.assertEqual(response.order.status, "dry_run")
        self.assertIn(("/api/ccxt/build/createOrders"), [c[0] for c in calls])
        # The build payload must use the CCXT ``orders[]`` shape with floats.
        build_call = next(c for c in calls if c[0] == "/api/ccxt/build/createOrders")
        self.assertEqual(build_call[1]["orders"][0]["type"], "limit")
        self.assertEqual(build_call[1]["orders"][0]["side"], "buy")
        self.assertIsInstance(build_call[1]["orders"][0]["amount"], float)
        self.assertIsInstance(build_call[1]["orders"][0]["price"], float)
        self.assertEqual(build_call[1]["metadata"]["sender"], "0x" + "1" * 64)
        # Regression guard: build payload must use the capability object ID
        # (32-byte hex), NOT the human account number "672" or "672n".
        cap_id = build_call[1]["accountId"]
        import re as _re
        self.assertRegex(
            cap_id,
            _re.compile(r"^0x[a-fA-F0-9]{64}$"),
            f"accountId must be the on-chain capability object ID (32-byte hex), got {cap_id!r}",
        )
        self.assertNotIn(cap_id, ("672", "672n"), "accountId must not be the human account number")

    def test_ladder_dry_run_uses_native_scale_order(self) -> None:
        self.set_account()
        calls = []
        with mock.patch.object(aftermath, "_post_json", side_effect=lambda c, p, b: (calls.append((p, b)) or self.fake_native_post(c, p, b))):
            response = aftermath.execute({
                "operation": "ladder", "account": "main", "symbol": "SOL-PERP", "side": "sell",
                "order_count": 3, "total_volume": "6", "start_price": "21", "end_price": "23",
                "distribution": "uniform", "dry_run": True,
            })
        self.assertTrue(response.success)
        self.assertEqual(response.ladder.status, "dry_run")
        self.assertEqual(calls[-1][0], "/api/ccxt/build/createOrders")
        # The dry-run probes the first batch to surface build errors before
        # committing to a multi-batch live submission. The total ladder
        # batch plan is exposed via response.data['batch_count'].
        ladder_call = next(c for c in calls if c[0] == "/api/ccxt/build/createOrders")
        self.assertGreaterEqual(len(ladder_call[1]["orders"]), 1)
        data = response.data if isinstance(response.data, dict) else {}
        self.assertIn("batch_count", data)
        self.assertIn("preview", data)

    def test_ladder_dry_run_uses_head_canonical_signature_and_preserves_diagnostics(self) -> None:
        import dataclasses
        from typing import Any, Dict, Optional

        @dataclasses.dataclass(frozen=True)
        class HeadCanonicalLadderResult:
            symbol: str
            side: str
            distribution: str
            requested_order_count: int
            submitted_order_count: int
            requested_volume: str
            submitted_volume: str
            batch_count: int
            verified: bool
            partial: bool = False
            status: str = "success"
            accepted_child_count: Optional[int] = None
            omitted_order_count: Optional[int] = None
            omitted_below_minimum: Optional[int] = None
            child_order_ids: Optional[list[str | int]] = None
            batches: Optional[list[Dict[str, Any]]] = None
            rate_limited: Optional[bool] = None
            exchange_reason: Optional[str] = None

        self.set_account()
        with mock.patch.object(aftermath, "CanonicalLadderResult", HeadCanonicalLadderResult):
            with mock.patch.object(aftermath, "_post_json", side_effect=self.fake_native_post):
                response = aftermath.execute({
                    "operation": "ladder", "account": "main", "symbol": "SOL-PERP", "side": "sell",
                    "order_count": 3, "total_volume": "6", "start_price": "21", "end_price": "23",
                    "distribution": "uniform", "dry_run": True,
                })
        self.assertTrue(response.success, msg=response)
        ladder = response.ladder
        assert ladder is not None
        self.assertEqual(ladder.status, "dry_run")
        self.assertEqual(ladder.batch_count, 1)
        assert ladder.batches is not None
        self.assertEqual(len(ladder.batches), 1)
        self.assertEqual(len(ladder.batches[0]["expected_children"]), 3)
        data = response.data if isinstance(response.data, dict) else {}
        self.assertEqual(len(data["expected_children"]), 3)
        self.assertEqual(data["batch_plan"][0]["order_indices"], [0, 1, 2])
        self.assertIs(data["first_batch_accepted"], True)
        self.assertIsNone(data["first_batch_error"])

    def test_cancel_close_tp_sl_leverage_dry_run_native_routes(self) -> None:
        self.set_account()
        operations = [
            ({"operation": "cancel_orders", "symbol": "SOL-PERP", "order_ids": ["11"]}, "/api/ccxt/build/cancelOrders"),
            ({"operation": "close_position", "symbol": "0x435987c9e1b8f61a4cdfa220751b3324ebf5a7bfd1250b25c79412a71a20392f"}, "/api/ccxt/build/createOrders"),
            ({"operation": "set_tp", "symbol": "0x435987c9e1b8f61a4cdfa220751b3324ebf5a7bfd1250b25c79412a71a20392f", "price": "25"}, "/api/ccxt/build/createOrders"),
            ({"operation": "set_sl", "symbol": "0x435987c9e1b8f61a4cdfa220751b3324ebf5a7bfd1250b25c79412a71a20392f", "price": "15"}, "/api/ccxt/build/createOrders"),
            ({"operation": "set_leverage", "symbol": "SOL-PERP", "leverage": "3"}, "/api/ccxt/build/setLeverage"),
        ]
        for req, expected_path in operations:
            calls = []
            req = {**req, "account": "main", "dry_run": True}
            with mock.patch.object(aftermath, "_post_json", side_effect=lambda c, p, b: (calls.append((p, b)) or self.fake_native_post(c, p, b))):
                response = aftermath.execute(req)
            self.assertTrue(response.success, msg=req["operation"])
            self.assertEqual(calls[-1][0], expected_path)

    def test_live_write_without_dry_run_attempts_sign_and_submit(self) -> None:
        """When ``dry_run=False`` is explicit, the agent builds a PTB and signs
        the resulting ``txKind`` with the configured Sui key.

        The test fake's ``native_post`` returns a ``MaybeSponsoredTxResponse``
        shape with a non-sponsored ``txKind``. The agent must:
        - NOT raise on the LIVE path.
        - Build the canonical order with ``status="submitted"`` and ``verified=True``.
        - POST a ``SubmitTransactionRequest`` body to the CCXT submit route.
        """
        # Override the address env to match a freshly generated ed25519 pubkey so
        # the signer address-check passes.
        import base64
        import hashlib
        import secrets

        from pysui_fastcrypto import decode_bech32, encode_bech32

        seed = secrets.token_bytes(32)
        priv = encode_bech32(b"\x00" + seed, "suiprivkey")
        scheme, pub, _ = decode_bech32(priv, "suiprivkey")
        # Sui address = BLAKE2b-256(scheme_flag || pubkey), hex with 0x prefix.
        sui_address = "0x" + hashlib.blake2b(
            scheme.to_bytes(1, "big") + bytes(pub), digest_size=32
        ).hexdigest()
        os.environ["AFTERMATH_MAIN_AGENT_PRIVATEKEY"] = priv
        os.environ["AFTERMATH_MAIN_AGENT_ADDRESS"] = sui_address
        os.environ["AFTERMATH_MAIN_ACCOUNT_ID"] = "12345"

        build_calls: list[tuple[str, dict]] = []
        submit_calls: list[tuple[str, dict]] = []

        def fake_post(credentials, path, payload, timeout=15):
            if path == "/api/ccxt/accounts":
                return [
                    {"id": "0x55d70f824bde7d1a5541e5fb4322d1dc53a1ec9c36e827f17469b70b1b310563", "type": "capability", "accountNumber": "672"},
                    {"id": "0xf65389a94968d9ef054637498c212f154738c4a24ce229314dac9e3ae2681f3d", "type": "account", "accountNumber": "672"},
                ]
            if path == "/api/perpetuals/all-markets":
                # _find_market calls this when resolving by symbol.
                return {
                    "markets": [
                        {
                            "objectId": "0x5072ccd95e6ff7bd724f89aa8b8dc58d31f09a93467ef7b694e9f24c50a1e49a",
                            "marketParams": {
                                "baseAssetSymbol": "SOLUSD",
                                "tickSize": "10000n",
                                "lotSize": "10000n",
                                "scalingFactor": 1e-6,
                                "minOrderUsdValue": 1.0,
                            },
                        }
                    ]
                }
            if path.startswith("/api/ccxt/build/"):
                build_calls.append((path, payload))
                import base64 as _b64
                return {
                    "transactionBytes": _b64.b64encode(b"FAKE-TX-BYTES").decode(),
                    "signingDigest": _b64.b64encode(b"deadbeefdeadbeefdeadbeefdeadbeef").decode(),
                }
            if path.startswith("/api/ccxt/submit/"):
                submit_calls.append((path, payload))
                return {"digest": "0xFAKEDIGEST", "txDigest": "0xFAKEDIGEST"}
            raise AssertionError(f"unexpected POST path: {path}")

        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            response = aftermath.execute({
                "operation": "new_order", "account": "main", "symbol": "SOL-PERP",
                "side": "buy", "order_type": "limit", "volume": "1", "price": "20", "dry_run": False,
            })
        self.assertTrue(response.success, msg=str(response))
        self.assertEqual(response.order.status, "submitted")
        self.assertTrue(response.order.verified)
        self.assertEqual(response.order.exchange_order_id, "0xFAKEDIGEST")
        # One build POST + one submit POST.
        self.assertEqual(len(build_calls), 1, "expected exactly one build POST")
        self.assertEqual(len(submit_calls), 1, "expected exactly one submit POST")
        self.assertEqual(build_calls[0][0], "/api/ccxt/build/createOrders")
        submit_path, submit_body = submit_calls[0]
        self.assertEqual(submit_path, "/api/ccxt/submit/createOrders")
        self.assertIn("transactionBytes", submit_body)
        self.assertEqual(len(submit_body["signatures"]), 1, "non-sponsored flow → 1 signature")
        # Decode the signature to confirm Sui ed25519 UserSignature shape:
        # 1-byte flag + 64-byte signature + 32-byte pubkey = 97 bytes.
        sig_bytes = base64.b64decode(submit_body["signatures"][0])
        self.assertEqual(len(sig_bytes), 97, "Aftermath submit requires 97-byte UserSignature")
        self.assertEqual(sig_bytes[0], 0, "ed25519 flag byte must be 0x00")
        embedded_pubkey = sig_bytes[65:]
        # The embedded pubkey, BLAKE2b-256-hashed with the ed25519 scheme flag,
        # must equal the configured agent wallet address.
        import hashlib
        derived_addr = "0x" + hashlib.blake2b(b"\x00" + embedded_pubkey, digest_size=32).hexdigest()
        self.assertEqual(derived_addr.lower(), os.environ["AFTERMATH_MAIN_AGENT_ADDRESS"].lower(),
                         "embedded pubkey must derive to the configured agent wallet address")

    def test_cancel_order_group_reads_live_open_orders_and_verifies(self) -> None:
        """Wizard-style cancel: no explicit order_ids → resolve from
        /api/ccxt/myPendingOrders, build → sign → submit, then re-read to
        confirm the targeted ids are gone.
        """
        self.set_account()
        submit_calls: list = []
        captured_order_ids: list = []

        def fake_post(credentials, path, payload):
            if path == "/api/perpetuals/all-markets":
                return {
                    "markets": [
                        {"objectId": "0x5072ccd95e6ff7bd724f89aa8b8dc58d31f09a93467ef7b694e9f24c50a1e49a",
                         "marketParams": {"baseAssetSymbol": "SOLUSD", "tickSize": "10000n",
                                          "lotSize": "10000n", "scalingFactor": 1e-6,
                                          "minOrderUsdValue": 1.0}}
                    ]
                }
            if path == "/api/ccxt/accounts":
                return [
                    {"id": "0x55d70f824bde7d1a5541e5fb4322d1dc53a1ec9c36e827f17469b70b1b310563",
                     "type": "capability", "accountNumber": "672"},
                ]
            if path == "/api/ccxt/myPendingOrders":
                # First call: there are 2 open sell orders. Second call (verification): gone.
                captured_order_ids.append("read")
                if len(captured_order_ids) == 1:
                    return [
                        {"id": "901", "price": 140.0, "remaining": 0.1, "side": "sell", "status": "open"},
                        {"id": "902", "price": 141.0, "remaining": 0.1, "side": "sell", "status": "open"},
                    ]
                return []
            if path.startswith("/api/ccxt/build/"):
                return {
                    "transactionBytes": base64.b64encode(b"FAKE-TX-BYTES").decode(),
                    "signingDigest": base64.b64encode(b"deadbeefdeadbeefdeadbeefdeadbeef").decode(),
                }
            if path.startswith("/api/ccxt/submit/"):
                submit_calls.append((path, payload))
                return {"digest": "0xCANCEL-DIGEST", "txDigest": "0xCANCEL-DIGEST"}
            raise AssertionError(f"unexpected POST path: {path}")

        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            response = aftermath.execute({
                "operation": "cancel_order_group", "account": "main",
                "exchange": "aftermath", "symbol": "SOL-PERP", "side": "sell",
                "dry_run": False,
            })
        self.assertTrue(response.success, msg=str(response))
        cg = response.cancel_group
        self.assertEqual(cg.symbol, "SOL")
        self.assertEqual(cg.side, "sell")
        self.assertEqual(cg.targeted_order_count, 2)
        self.assertEqual(cg.cancelled_order_count, 2)
        self.assertEqual(cg.remaining_target_count, 0)
        self.assertTrue(cg.verified)
        self.assertEqual(cg.status, "submitted")
        # Exactly one build (cancelOrders) + one submit (cancelOrders).
        self.assertEqual(len(submit_calls), 1)
        self.assertEqual(submit_calls[0][0], "/api/ccxt/submit/cancelOrders")
        # Data carries the digest and verification source.
        data = response.data if isinstance(response.data, dict) else {}
        self.assertEqual(data.get("transaction_digest"), "0xCANCEL-DIGEST")
        self.assertEqual(data.get("verified_via"), "/api/ccxt/myPendingOrders")
        # myPendingOrders must be hit twice: once to read, once to verify.
        self.assertEqual(len(captured_order_ids), 2)

    def test_ladder_live_builds_signs_submits_and_verifies_children(self) -> None:
        """Wizard-style ladder LIVE: child orders are packed into one or more
        build/sign/submit PTBs (after Aftermath's per-batch notional cap)
        and the live open-orders listing is matched against each expected
        child to confirm actual placement.
        """
        self.set_account()
        submit_calls: list = []
        pending_calls: list = []

        def expected_child_price(start: float, end: float, idx: int, count: int) -> float:
            step = (end - start) / max(count - 1, 1)
            return start + step * idx

        def fake_post(credentials, path, payload):
            if path == "/api/perpetuals/all-markets":
                return {
                    "markets": [
                        {"objectId": "0x5072ccd95e6ff7bd724f89aa8b8dc58d31f09a93467ef7b694e9f24c50a1e49a",
                         "marketParams": {"baseAssetSymbol": "SOLUSD", "tickSize": "10000n",
                                          "lotSize": "10000n", "scalingFactor": 1e-6,
                                          "minOrderUsdValue": 1.0}}
                    ]
                }
            if path == "/api/ccxt/accounts":
                return [
                    {"id": "0x55d70f824bde7d1a5541e5fb4322d1dc53a1ec9c36e827f17469b70b1b310563",
                     "type": "capability", "accountNumber": "672"},
                ]
            if path == "/api/ccxt/myPendingOrders":
                pending_calls.append(payload)
                return [
                    {"id": f"L{i}", "price": expected_child_price(20.0, 22.0, i, 3),
                     "remaining": 1.0, "side": "buy", "status": "open"}
                    for i in range(3)
                ]
            if path.startswith("/api/ccxt/build/"):
                return {
                    "transactionBytes": base64.b64encode(b"FAKE-TX-BYTES").decode(),
                    "signingDigest": base64.b64encode(b"deadbeefdeadbeefdeadbeefdeadbeef").decode(),
                }
            if path.startswith("/api/ccxt/submit/"):
                submit_calls.append((path, payload))
                return {"digest": "0xLADDER-DIGEST", "txDigest": "0xLADDER-DIGEST"}
            raise AssertionError(f"unexpected POST path: {path}")
        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            response = aftermath.execute({
                "operation": "ladder", "account": "main", "symbol": "SOL-PERP", "side": "buy",
                "order_count": 3, "total_volume": "0.6", "start_price": "20", "end_price": "22",
                "distribution": "uniform", "dry_run": False,
            })
        self.assertTrue(response.success, msg=str(response))
        ld = response.ladder
        self.assertEqual(ld.symbol, "SOL")
        self.assertEqual(ld.side, "buy")
        self.assertEqual(ld.distribution, "uniform")
        self.assertEqual(ld.requested_order_count, 3)
        self.assertEqual(ld.submitted_order_count, 3)
        self.assertEqual(ld.accepted_child_count, 3)
        self.assertEqual(ld.omitted_order_count, 0)
        self.assertTrue(ld.verified)
        self.assertEqual(ld.status, "submitted")
        # Total orders submitted across all batches equals requested count;
        # the `batches` list carries per-batch details.
        self.assertEqual(ld.batch_count, 1, "small ladder fits in one batch")
        self.assertEqual(len(submit_calls), 1)
        self.assertEqual(submit_calls[0][0], "/api/ccxt/submit/createOrders")
        data = response.data if isinstance(response.data, dict) else {}
        self.assertEqual(data.get("transaction_digest"), "0xLADDER-DIGEST")
        self.assertEqual(data.get("verified_via"), "/api/ccxt/myPendingOrders")
        # Preview block carries the wizard's expected fields.
        preview = data.get("preview") or {}
        self.assertEqual(preview.get("orders"), 3)
        self.assertEqual(preview.get("min_price"), "20")
        self.assertEqual(preview.get("max_price"), "22")
        # Each child must appear in the matched_children list.
        matched = data.get("matched_children") or []
        self.assertEqual(len(matched), 3)

    def test_ladder_packs_into_multiple_batches_when_total_exceeds_cap(self) -> None:
        """When the live withdrawable forces a per-batch cap below the
        ladder's total notional, the agent must split into ≥2 batches while
        preserving every child price and size.
        """
        self.set_account()
        submit_calls: list = []

        def fake_post(credentials, path, payload):
            if path == "/api/perpetuals/all-markets":
                return {
                    "markets": [
                        {"objectId": "0x5072ccd95e6ff7bd724f89aa8b8dc58d31f09a93467ef7b694e9f24c50a1e49a",
                         "marketParams": {"baseAssetSymbol": "SOLUSD", "tickSize": "10000n",
                                          "lotSize": "10000n", "scalingFactor": 1e-6,
                                          "minOrderUsdValue": 1.0}}
                    ]
                }
            if path == "/api/ccxt/accounts":
                return [
                    {"id": "0x55d70f824bde7d1a5541e5fb4322d1dc53a1ec9c36e827f17469b70b1b310563",
                     "type": "capability", "accountNumber": "672"},
                ]
            if path == "/api/ccxt/myPendingOrders":
                # Verification: each expected child exists with matching price/size.
                return [
                    {"id": f"L{i}", "price": 20.0 + i, "remaining": 1.0,
                     "side": "sell", "status": "open"}
                    for i in range(3)
                ]
            if path.startswith("/api/ccxt/build/"):
                # ONE-PTB attempt carries all 3 children → fail to simulate
                # the per-PTB cap; subsequent fallback batches succeed.
                if len(payload.get("orders", [])) == 3:
                    raise RuntimeError(
                        "Aftermath HTTP 500 on /api/ccxt/build/createOrders: "
                        "Error with ID: #simulated_one_ptb_cap"
                    )
                return {
                    "transactionBytes": base64.b64encode(b"FAKE-TX-BYTES").decode(),
                    "signingDigest": base64.b64encode(b"deadbeefdeadbeefdeadbeefdeadbeef").decode(),
                }
            if path.startswith("/api/ccxt/submit/"):
                submit_calls.append((path, payload))
                return {"digest": "0xFAKEDIGEST", "txDigest": "0xFAKEDIGEST"}
            raise AssertionError(f"unexpected POST path: {path}")

        # Withdrawable $10 → cap ≈ $8.50 → ladder 3 orders of $7 each ($21
        # total). ONE-PTB attempt fails (simulated per-PTB cap); fallback
        # packs into 3 single-order batches.
        fake_balance = aftermath.make_success(
            operation="balance", exchange="aftermath", account="main",
            balance=aftermath.normalize_balance("100", "USD"),
            portfolio_summary=CanonicalPortfolioSummary(
                account_value="100", withdrawable="10", margin_used="90",
                total_position_value="0", unit="USD",
            ),
        )

        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post), \
             mock.patch.object(aftermath, "_balance", return_value=fake_balance):
            response = aftermath.execute({
                "operation": "ladder", "account": "main", "symbol": "SOL-PERP", "side": "sell",
                "order_count": 3, "total_volume": "3", "start_price": "20", "end_price": "22",
                "distribution": "uniform", "dry_run": False,
            })
        self.assertTrue(response.success, msg=str(response))
        ld = response.ladder
        self.assertEqual(ld.requested_order_count, 3)
        self.assertEqual(ld.submitted_order_count, 3)
        self.assertEqual(ld.accepted_child_count, 3)
        self.assertEqual(ld.batch_count, 3, "$21 ladder packs into 3 batches of $7 each")
        self.assertEqual(len(submit_calls), 3)
        # Every batch must use the createOrders build/submit path.
        for path, _ in submit_calls:
            self.assertEqual(path, "/api/ccxt/submit/createOrders")

    def test_cancel_build_payload_uses_plain_decimal_order_ids(self) -> None:
        """Aftermath's /api/ccxt/build/cancelOrders rejects order ids with the
        BCS ``"n"`` BigInt suffix — it expects plain decimal strings. Sending
        ``"<id>n"`` makes the server return HTTP 500 with an opaque error id.
        """
        self.set_account()
        captured_build: list = []

        def fake_post(credentials, path, payload):
            if path == "/api/ccxt/build/cancelOrders":
                captured_build.append(payload)
                return {
                    "transactionBytes": base64.b64encode(b"FAKE-TX-BYTES").decode(),
                    "signingDigest": base64.b64encode(b"\x00" * 32).decode(),
                }
            if path.startswith("/api/ccxt/submit/"):
                return {"digest": "0xFAKEDIGEST", "txDigest": "0xFAKEDIGEST"}
            if path == "/api/ccxt/myPendingOrders":
                return [
                    {"id": "12345678901234567890123456789012", "price": 100.0,
                     "remaining": 1.0, "side": "sell", "status": "open"},
                ]
            return self.fake_native_post(credentials, path, payload)

        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            aftermath.execute({
                "operation": "cancel_order_group", "account": "main",
                "exchange": "aftermath", "symbol": "SOL-PERP", "side": "sell",
                "dry_run": False,
            })
        self.assertEqual(len(captured_build), 1)
        ids = captured_build[0]["orderIds"]
        self.assertEqual(ids, ["12345678901234567890123456789012"])
        for value in ids:
            self.assertIsInstance(value, str)
            self.assertFalse(value.endswith("n"),
                             "cancel orderIds must be plain decimal strings, not BCS 'n' BigInt")

    def test_ladder_half_gaussian_smallest_at_start_largest_at_end(self) -> None:
        """The ladder distribution math must match the wizard preview:
        smallest size near start (i=0), largest near end (i=count-1).
        """
        weights = aftermath._ladder_distribution_weights(4, "half_gaussian")
        self.assertGreater(weights[0], Decimal("0"))
        self.assertEqual(weights[-1], Decimal("1"))
        # Weights are strictly increasing as i grows.
        for i in range(len(weights) - 1):
            self.assertLess(weights[i], weights[i + 1])

    def test_ladder_rejects_children_below_per_order_minimum(self) -> None:
        """Ladder children whose notional is below Aftermath's per-order
        minimum ($2.00) must be rejected with a clear error code instead
        of hitting the build endpoint and getting an opaque 500.
        """
        self.set_account()
        with mock.patch.object(aftermath, "_post_json", side_effect=self.fake_native_post):
            response = aftermath.execute({
                "operation": "ladder", "account": "main", "symbol": "SOL-PERP", "side": "sell",
                # 3 orders × $0.30 notional each = below the $2 minimum
                "order_count": 3, "total_volume": "0.045", "start_price": "20", "end_price": "22",
                "distribution": "uniform", "dry_run": False,
            })
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "CHILD_BELOW_MIN_NOTIONAL")
        self.assertIn("per-order minimum", response.error.message)

    def test_ladder_validate_children_min_notional_reports_offender(self) -> None:
        """``_validate_children_min_notional`` must surface the exact child
        index/price/size/notional when any child is below the per-order
        minimum read from ``marketParams.minOrderUsdValue``.
        """
        orders = [
            {"price": 100.0, "amount": 0.05, "side": "buy"},   # $5 ok
            {"price": 100.0, "amount": 0.008, "side": "buy"},  # $0.80 below $1
            {"price": 100.0, "amount": 1.0, "side": "sell"},   # $100 ok
        ]
        response = aftermath._validate_children_min_notional(
            orders, Decimal("1.0"),
        )
        assert response is not None
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "CHILD_BELOW_MIN_NOTIONAL")
        msg = response.error.message
        self.assertIn("child #1", msg)
        self.assertIn("$0.8000", msg)
        self.assertIn("price $100.0000", msg)
        self.assertIn("size 0.008", msg)
        # Higher minimum still rejects the same child.
        response2 = aftermath._validate_children_min_notional(orders, Decimal("2.0"))
        assert response2 is not None
        self.assertIn("child #1", response2.error.message)

    def test_ladder_validate_ladder_capacity_reports_oversized_child(self) -> None:
        """``_validate_ladder_capacity`` must query the documented
        ``/api/perpetuals/account/max-order-size`` endpoint and reject the
        ladder with ``LADDER_OVER_MAX_ORDER_SIZE`` when the largest child
        exceeds the cap at the binding price.
        """
        creds = {"account_id": "672"}
        ch_id = "0x5072ccd95e6ff7bd724f89aa8b8dc58d31f09a93467ef7b694e9f24c50a1e49a"
        # SELL ladder: lowest price is the binding end. Cap is 0.04 SOL there.
        with mock.patch.object(aftermath, "_max_order_size", return_value=Decimal("0.04")):
            resp = aftermath._validate_ladder_capacity(
                credentials=creds, ch_id=ch_id, side_text="sell",
                child_prices=[Decimal("130"), Decimal("135"), Decimal("140")],
                child_sizes=[Decimal("0.05"), Decimal("0.10"), Decimal("0.20")],
                scaling_factor=Decimal("1"),
            )
        assert resp is not None
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "LADDER_OVER_MAX_ORDER_SIZE")
        # The binding price for SELL is the LOWEST price. child #0 sits at $130.
        self.assertIn("child #0", resp.error.message)
        self.assertIn("$130", resp.error.message)
        # BUY ladder: highest price is the binding end.
        with mock.patch.object(aftermath, "_max_order_size", return_value=Decimal("0.04")):
            resp = aftermath._validate_ladder_capacity(
                credentials=creds, ch_id=ch_id, side_text="buy",
                child_prices=[Decimal("130"), Decimal("135"), Decimal("140")],
                child_sizes=[Decimal("0.05"), Decimal("0.10"), Decimal("0.20")],
                scaling_factor=Decimal("1"),
            )
        assert resp is not None
        self.assertIn("child #2", resp.error.message)  # binding = highest price
        self.assertIn("$140", resp.error.message)

    def test_ladder_validate_ladder_capacity_passes_when_under_cap(self) -> None:
        with mock.patch.object(aftermath, "_max_order_size", return_value=Decimal("1000")):
            resp = aftermath._validate_ladder_capacity(
                credentials={"account_id": "672"},
                ch_id="0x5072ccd95e6ff7bd724f89aa8b8dc58d31f09a93467ef7b694e9f24c50a1e49a",
                side_text="sell",
                child_prices=[Decimal("130"), Decimal("140")],
                child_sizes=[Decimal("0.05"), Decimal("0.20")],
                scaling_factor=Decimal("1"),
            )
        self.assertIsNone(resp)

    def test_ladder_validate_ladder_capacity_returns_none_when_probe_fails(self) -> None:
        """When the max-order-size endpoint itself errors out, the capacity
        check must NOT block the ladder — let the live submit surface the
        real error.
        """
        with mock.patch.object(aftermath, "_max_order_size", return_value=None):
            resp = aftermath._validate_ladder_capacity(
                credentials={"account_id": "672"},
                ch_id="0x5072ccd95e6ff7bd724f89aa8b8dc58d31f09a93467ef7b694e9f24c50a1e49a",
                side_text="sell",
                child_prices=[Decimal("130"), Decimal("140")],
                child_sizes=[Decimal("0.05"), Decimal("0.20")],
                scaling_factor=Decimal("1"),
            )
        self.assertIsNone(resp)

    def test_quantize_floor_rounds_to_increment(self) -> None:
        """Child prices and sizes must be floored to Aftermath's tick/lot
        increments. Without quantization the build endpoint returns HTTP 500.
        """
        # 131.1111 floored to 0.01 tick → 131.11.
        self.assertEqual(aftermath._quantize_floor(Decimal("131.1111"), Decimal("0.01")), Decimal("131.11"))
        # 0.1342859 floored to 0.01 lot → 0.13.
        self.assertEqual(aftermath._quantize_floor(Decimal("0.1342859"), Decimal("0.01")), Decimal("0.13"))
        # 0.0 stays 0.
        self.assertEqual(aftermath._quantize_floor(Decimal("0"), Decimal("0.01")), Decimal("0"))
        # 0.01 stays 0.01 (already multiple).
        self.assertEqual(aftermath._quantize_floor(Decimal("0.01"), Decimal("0.01")), Decimal("0.01"))
        # Increment=0 → no quantization.
        self.assertEqual(aftermath._quantize_floor(Decimal("131.1111"), Decimal("0")), Decimal("131.1111"))

    def test_ladder_quantizes_child_prices_and_sizes(self) -> None:
        """Generated child orders must respect tick/lot precision. Without
        quantization the live server returns HTTP 500 with an opaque
        Error ID.
        """
        self.set_account()
        captured_build: list = []

        def fake_post(credentials, path, payload):
            if path == "/api/perpetuals/all-markets":
                return {
                    "markets": [
                        {"objectId": "0x5072ccd95e6ff7bd724f89aa8b8dc58d31f09a93467ef7b694e9f24c50a1e49a",
                         "marketParams": {"baseAssetSymbol": "SOLUSD", "tickSize": "10000n",
                                          "lotSize": "10000n", "scalingFactor": 1e-6,
                                          "minOrderUsdValue": 1.0}}
                    ]
                }
            if path == "/api/ccxt/accounts":
                return [
                    {"id": "0x55d70f824bde7d1a5541e5fb4322d1dc53a1ec9c36e827f17469b70b1b310563",
                     "type": "capability", "accountNumber": "672"},
                ]
            if path.startswith("/api/ccxt/build/"):
                captured_build.append(payload)
                return {
                    "transactionBytes": base64.b64encode(b"FAKE-TX-BYTES").decode(),
                    "signingDigest": base64.b64encode(b"deadbeefdeadbeefdeadbeefdeadbeef").decode(),
                }
            raise AssertionError(f"unexpected POST path: {path}")

        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            response = aftermath.execute({
                "operation": "ladder", "account": "main", "symbol": "SOL-PERP", "side": "sell",
                "order_count": 3, "total_volume": "60", "start_price": "20", "end_price": "22",
                "distribution": "half_gaussian", "dry_run": True,
            })
        self.assertTrue(response.success, msg=str(response))
        # The dry-run probes the first batch; check every child in that
        # batch is quantized to 2-decimal precision.
        first_orders = captured_build[-1]["orders"]
        for o in first_orders:
            # tick 0.01: price must have at most 2 decimals.
            price_str = f"{o['price']:.10f}".rstrip("0").rstrip(".")
            self.assertLessEqual(len(price_str.split(".")[-1] if "." in price_str else ""), 2,
                                f"price {o['price']} not quantized to tick 0.01")
            # lot 0.01: amount must be a multiple of 0.01.
            cents = round(o["amount"] * 100)
            self.assertEqual(o["amount"], cents / 100,
                             f"amount {o['amount']} not quantized to lot 0.01")
        # The data payload must also expose the quantization metadata.
        data = response.data if isinstance(response.data, dict) else {}
        quant = data.get("quantization") or {}
        self.assertEqual(quant.get("tick_increment"), "0.01")
        self.assertEqual(quant.get("lot_increment"), "0.01")

    def test_ladder_all_failed_returns_first_error(self) -> None:
        """When every batch fails the agent must surface a structured
        ``LADDER_BUILD_FAILED`` error with the first concrete reason, not
        just ``Succeeded: 0 / Failed: 10``.
        """
        self.set_account()

        def fake_post(credentials, path, payload):
            if path == "/api/perpetuals/all-markets":
                return {
                    "markets": [
                        {"objectId": "0x5072ccd95e6ff7bd724f89aa8b8dc58d31f09a93467ef7b694e9f24c50a1e49a",
                         "marketParams": {"baseAssetSymbol": "SOLUSD", "tickSize": "10000n",
                                          "lotSize": "10000n", "scalingFactor": 1e-6,
                                          "minOrderUsdValue": 1.0}}
                    ]
                }
            if path == "/api/ccxt/accounts":
                return [
                    {"id": "0x55d70f824bde7d1a5541e5fb4322d1dc53a1ec9c36e827f17469b70b1b310563",
                     "type": "capability", "accountNumber": "672"},
                ]
            if path.startswith("/api/ccxt/build/"):
                raise RuntimeError(
                    "Aftermath HTTP 500 on /api/ccxt/build/createOrders: "
                    "Error with ID: #simulated_build_failure"
                )
            raise AssertionError(f"unexpected POST path: {path}")

        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            response = aftermath.execute({
                "operation": "ladder", "account": "main", "symbol": "SOL-PERP", "side": "sell",
                "order_count": 3, "total_volume": "3", "start_price": "20", "end_price": "22",
                "distribution": "uniform", "dry_run": False,
            })
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "LADDER_BUILD_FAILED")
        self.assertIn("simulated_build_failure", response.error.message)
        # The data dict must carry the rich diagnostic payload so the wizard
        # can show quantization + child orders + per-batch failures.
        data = response.data if isinstance(response.data, dict) else {}
        self.assertIn("first_error", data)
        self.assertIn("simulated_build_failure", data["first_error"])
        self.assertEqual(data.get("requested_order_count"), 3)
        self.assertEqual(data.get("submitted_order_count"), 0)
        self.assertIn("child_orders", data)
        self.assertIn("quantization", data)
        self.assertIn("batches", data)
        # At least one batch in failures.
        self.assertIsInstance(data.get("failures"), list)
        self.assertGreater(len(data["failures"]), 0)

    def test_wizard_can_select_aftermath_and_open_balance(self) -> None:
        class FakeDesk:
            def list_exchanges(self):
                return ["aftermath"]

            def list_accounts(self, exchange):
                return ["main"] if exchange == "aftermath" else []

            def capabilities(self, exchange):
                return aftermath.capabilities() if exchange == "aftermath" else []

            def execute(self, request):
                return aftermath.make_success(  # type: ignore[attr-defined]
                    operation="balance",
                    exchange="aftermath",
                    account="main",
                    balance=aftermath.normalize_balance("42", "USD"),  # type: ignore[attr-defined]
                )

        wizard = TradeWizard(tradedesk=FakeDesk())
        chat_key = ("chat", "aftermath")
        first = wizard.open(chat_key)
        self.assertIn("exchange:aftermath", str(first.buttons))
        accounts = wizard.handle_callback(chat_key, "exchange:aftermath")
        self.assertIn("account:main", str(accounts.buttons))
        action = wizard.handle_callback(chat_key, "account:main")
        self.assertIn("action:balance", str(action.buttons))
        balance = wizard.handle_callback(chat_key, "action:balance")
        self.assertIn("Balance: 42.00 USD", balance.text)

    def test_private_key_not_exposed_in_accounts_or_errors(self) -> None:
        secret = "suiprivkey1thismustnotleak"
        os.environ["AFTERMATH_MAIN_AGENT_ADDRESS"] = "0x" + "1" * 64
        os.environ["AFTERMATH_MAIN_AGENT_PRIVATEKEY"] = secret
        os.environ["AFTERMATH_MAIN_ACCOUNT_ID"] = "12345"
        self.assertEqual(aftermath.list_accounts(), ["main"])
        with mock.patch.object(aftermath, "_post_json", side_effect=RuntimeError(f"boom {secret}")):
            response = aftermath.execute({"operation": "balance", "account": "main"})
        self.assertFalse(response.success)
        self.assertNotIn(secret, response.error.message)
        self.assertNotIn(secret, str(response.to_dict()))


if __name__ == "__main__":
    unittest.main()
