"""Arcus rate-limit hardening regression tests (offline, no live HTTP).

Covers the cancellation hierarchy, write-gate pacing, 429 policy, batch
sizing, and the safe HTTP diagnostics — without making any live network
call. Every Arcus HTTP call is mocked at the agent boundary.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
os.environ.setdefault("HERMES_HOME", "/root/.hermes")


def arcus():
    return importlib.import_module("plugins.trade.agents.x_arcus_agent")


def _creds() -> Dict[str, Any]:
    return {
        "account": "amiroo",
        "wallet": "0x742d35Cc6634C0532925a3b844Bc454e4438f44e",
        "account_index": 0,
        "base_url": "http://test",
        "api_signing_key": "sign-key",
        "private_key_hex": "00" * 32,
    }


def _market():
    return {"market_id": 1, "display_symbol": "BTC-USD", "tick_size": Decimal("0.1"),
            "step_size": Decimal("0.00000001"), "price_precision": 1, "size_precision": 8,
            "min_notional": Decimal("5")}


_MARKET = _market()


def _env(prefix: str = "AMIROO"):
    return mock.patch.dict(os.environ, {
        f"ARCUS_{prefix}_WALLET": "0x742d35Cc6634C0532925a3b844Bc454e4438f44e",
        f"ARCUS_{prefix}_APISIGNINGKEY": "sign-key",
        f"ARCUS_{prefix}_PRIVATE_KEY": "00" * 32,
    })


class Resp:
    """Minimal requests.Response stand-in with status/headers/body."""

    def __init__(self, status_code: int = 200, text: str = "{}",
                 headers: Optional[Dict[str, str]] = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.content = text.encode("utf-8")

    def json(self):
        try:
            return json.loads(self.text)
        except Exception:
            return {"raw": self.text}


class ArcusHttpDiagTests(unittest.TestCase):
    def test_http_diag_records_safe_fields(self):
        hl = arcus()
        with mock.patch.object(hl, "_HTTP_LOGGER") as mock_logger:
            hl._log_arcus_http(method="POST", endpoint="/v1/cancelAllOrders", status=429,
                               elapsed_s=0.5, gate_wait_s=0.3, retry_after=2.0,
                               operation="cancel_order_group")
            mock_logger.debug.assert_called()
        # Never any secret material in the call kwargs (they aren't passed at all).


class ArcusCancellationHierarchyTests(unittest.TestCase):
    """The cancel hierarchy: cancel-all when single-side, batch when both."""

    def setUp(self):
        self._env = _env()
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_symbol_only_selected_side_uses_cancel_all(self):
        hl = arcus()
        cancel_all_calls: List[Tuple[Any, Any]] = []

        def fake_cancel_all(credentials, market_id=None, *, operation=None):
            cancel_all_calls.append((market_id, operation))
            return {"status": "CANCEL_ALL_ACKNOWLEDGED"}

        def fake_fetch(credentials):
            return [
                {"orderId": "ord-1", "marketDisplayName": "BTC-USD", "side": "BUY"},
                {"orderId": "ord-2", "marketDisplayName": "BTC-USD", "side": "BUY"},
            ]

        with mock.patch.object(hl, "_lookup_credentials", return_value=_creds()), \
             mock.patch.object(hl, "_resolve_market", return_value=_MARKET), \
             mock.patch.object(hl, "_fetch_open_orders_for_account",
                              side_effect=[fake_fetch(None), []]), \
             mock.patch.object(hl, "_submit_cancel_all", side_effect=fake_cancel_all):
            resp = hl.execute({
                "operation": "cancel_order_group", "exchange": "arcus", "account": "amiroo",
                "symbol": "BTC-USD", "side": "buy",
            })
        self.assertTrue(resp.success)
        self.assertEqual(len(cancel_all_calls), 1)  # 1 symbol-wide request, not 2
        self.assertEqual(cancel_all_calls[0][0], 1)  # market_id

    def test_symbol_with_opposite_side_uses_batch(self):
        hl = arcus()
        batch_calls: List[Any] = []

        def fake_batch(credentials, market_id, rows, *, operation=None):
            batch_calls.append((market_id, [r.get("order_id") or r.get("orderId") for r in rows]))
            return {"responses": [{"status": "CANCELED"}] * len(rows)}

        def fake_fetch(credentials):
            return [
                {"orderId": "ord-1", "marketDisplayName": "BTC-USD", "side": "BUY"},
                {"orderId": "ord-2", "marketDisplayName": "BTC-USD", "side": "SELL"},
            ]

        with mock.patch.object(hl, "_lookup_credentials", return_value=_creds()), \
             mock.patch.object(hl, "_resolve_market", return_value=_MARKET), \
             mock.patch.object(hl, "_fetch_open_orders_for_account",
                              side_effect=[fake_fetch(None), []]), \
             mock.patch.object(hl, "_submit_batch_cancel", side_effect=fake_batch):
            resp = hl.execute({
                "operation": "cancel_order_group", "exchange": "arcus", "account": "amiroo",
                "symbol": "BTC-USD", "side": "buy",
            })
        self.assertTrue(resp.success, f"resp: {resp.error}")
        # Only the BUY row is batched; opposite-side SELL is preserved.
        self.assertEqual(len(batch_calls), 1)
        self.assertEqual(len(batch_calls[0][1]), 1)
        self.assertEqual(batch_calls[0][1][0], "ord-1")

    def test_cancel_all_flat_cost_constant(self):
        self.assertEqual(arcus()._ARCUS_CANCEL_ALL_COST, 1_000)


class ArcusBatchSemanticsTests(unittest.TestCase):
    """Batch cancellation body/chunking + ladder placement remains batched."""

    def test_batch_cancel_includes_address_without_changing_signatures_or_body(self):
        hl = arcus()
        captured: Dict[str, Any] = {}
        fixed_timestamp_ns = 1_794_488_000_123_456_789
        rows = [{"orderId": "ord-1"}, {"orderId": "ord-2"}]

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Resp(text='{"responses":[]}')

        with mock.patch.object(hl, "_ARCUS_WRITE_GATE") as gate, \
             mock.patch.object(hl.time, "time_ns", return_value=fixed_timestamp_ns), \
             mock.patch("requests.post", side_effect=fake_post) as post:
            gate.wait_for_slot.return_value = 0.0
            hl._submit_batch_cancel(
                _creds(), 1, rows, operation="cancel_order_group"
            )

        body = json.loads(captured["data"])
        expected_typed = hl._build_arcus_typed_payload_cancel(
            credentials=_creds(),
            market_id=1,
            order_id="ord-1",
            timestamp_ns=fixed_timestamp_ns,
        )
        expected_signature = hl._ed25519_private_key_from_hex(
            _creds()["private_key_hex"]
        ).sign(hl._typed_payload_bytes(expected_typed).encode("utf-8")).hex()

        self.assertEqual(post.call_count, 1)
        self.assertEqual(captured["url"], "http://test/v1/batchCancelOrders")
        self.assertEqual(captured.get("params"), {"address": _creds()["wallet"]})
        self.assertEqual(list(body), ["cancels"])
        self.assertEqual(len(body["cancels"]), 2)
        self.assertEqual(body["cancels"][0]["address"], _creds()["wallet"])
        self.assertEqual(body["cancels"][0]["accountIndex"], 0)
        self.assertEqual(body["cancels"][0]["timestamp"], fixed_timestamp_ns)
        self.assertEqual(body["cancels"][0]["signature"], expected_signature)
        self.assertEqual(captured["headers"]["X-Timestamp"], str(fixed_timestamp_ns))
        self.assertEqual(captured["headers"]["X-Signature"], expected_signature)

    def test_batch_cancel_http_failure_is_not_retried(self):
        hl = arcus()

        with mock.patch.object(hl, "_ARCUS_WRITE_GATE") as gate, \
             mock.patch(
                 "requests.post",
                 return_value=Resp(status_code=401, text='{"error":"Invalid API key"}'),
             ) as post:
            gate.wait_for_slot.return_value = 0.0
            with self.assertRaisesRegex(RuntimeError, "HTTP 401: Invalid API key"):
                hl._submit_batch_cancel(
                    _creds(), 1, [{"orderId": "ord-1"}],
                    operation="cancel_order_group",
                )

        self.assertEqual(post.call_count, 1)

    def test_cancellation_batches_chunk_at_100(self):
        hl = arcus()
        rows = [{"orderId": f"ord-{i}"} for i in range(250)]
        batches = hl._cancellation_batches(rows)
        self.assertEqual([len(b) for b in batches], [100, 100, 50])

    def test_cancel_batch_size_constant_is_100(self):
        self.assertEqual(arcus()._ARCUS_CANCEL_BATCH_SIZE, 100)

    def test_ladder_placement_still_batches_at_10(self):
        self.assertEqual(arcus()._ARCUS_BATCH_SIZE, 10)

    def test_batch_cancel_one_verification_read(self):
        hl = arcus()
        fetch_calls: List[int] = []

        def fake_fetch(credentials):
            fetch_calls.append(1)
            return [
                {"orderId": "ord-1", "marketDisplayName": "BTC-USD", "side": "BUY"},
                {"orderId": "ord-2", "marketDisplayName": "BTC-USD", "side": "SELL"},
            ]

        def fake_batch(credentials, market_id, rows, *, operation=None):
            return {"responses": [{"status": "CANCELED"}] * len(rows)}

        with mock.patch.object(hl, "_lookup_credentials", return_value=_creds()), \
             mock.patch.object(hl, "_resolve_market", return_value=_MARKET), \
             mock.patch.object(hl, "_fetch_open_orders_for_account", side_effect=fake_fetch), \
             mock.patch.object(hl, "_submit_batch_cancel", side_effect=fake_batch):
            hl.execute({
                "operation": "cancel_order_group", "exchange": "arcus", "account": "amiroo",
                "symbol": "BTC-USD", "side": "buy",
            })
        # before-read once + after-verify once = 2 total, not per-child.
        self.assertEqual(len(fetch_calls), 2)


class ArcusWriteGateTests(unittest.TestCase):
    def test_gate_paces_writes(self):
        from plugins.trade.agents import x_arcus_agent as arc
        gate = arc._ArcusWriteGate(arc.ARCUS_POST_MIN_INTERVAL_SECONDS)
        t0 = time.time()
        gate.wait_for_slot()  # first: immediate
        t1 = time.time()
        gate.wait_for_slot()  # second: waits at least min interval
        t2 = time.time()
        self.assertLess(t1 - t0, 0.05)
        self.assertGreaterEqual(t2 - t1, arc.ARCUS_POST_MIN_INTERVAL_SECONDS - 0.02)

    def test_post_min_interval_constant(self):
        from plugins.trade.agents import x_arcus_agent as arc
        self.assertEqual(arc.ARCUS_POST_MIN_INTERVAL_SECONDS, 0.1)


class ArcusMarketCacheTtlTests(unittest.TestCase):
    def test_markets_tl_extended_static_metadata(self):
        from plugins.trade.agents import x_arcus_agent as arc
        self.assertGreaterEqual(arc._ARCUS_MARKETS_CACHE_TTL_SECONDS, 30.0)

    def test_github_volatile_reads_not_long_cached(self):
        from plugins.trade.agents import x_arcus_agent as arc
        gate = arc._ArcusGetGate()
        self.assertLess(gate._ttl_for_path("/v1/account"), 5.0)
        self.assertEqual(gate._ttl_for_path("/v1/markets"), arc._ARCUS_MARKETS_CACHE_TTL_SECONDS)


class ArcusTpslAuthRegressionTests(unittest.TestCase):
    """TP/SL batch auth and diagnostics regressions (offline, no live HTTP)."""

    def test_tpsl_batch_includes_address_query_param_matching_wallet(self):
        hl = arcus()
        captured: Dict[str, Any] = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Resp(text='{"responses":[{"orderId":"abc"}]}')

        with mock.patch.object(hl, "_ARCUS_WRITE_GATE") as gate, \
             mock.patch("requests.post", side_effect=fake_post):
            gate.wait_for_slot.return_value = 0.0
            out = hl._arcus_batch_place_tpsl(
                _creds(), 1, "sell", "0.10000000", 10_000_000,
                [("TAKE_PROFIT", 700_000, "tp-client")],
            )
        self.assertEqual(out, [{"orderId": "abc"}])
        self.assertEqual(captured["url"], "http://test/v1/batchPlaceOrders")
        self.assertEqual(captured.get("params"), {"address": _creds()["wallet"]})

    def test_tpsl_successful_response_body_and_headers_remain_unchanged(self):
        hl = arcus()
        captured: Dict[str, Any] = {}

        def fake_post(url, **kwargs):
            captured.update(kwargs)
            return Resp(text='{"responses":[{"orderId":"abc","status":"OPEN"}]}')

        with mock.patch.object(hl, "_ARCUS_WRITE_GATE") as gate, \
             mock.patch("requests.post", side_effect=fake_post):
            gate.wait_for_slot.return_value = 0.0
            out = hl._arcus_batch_place_tpsl(
                _creds(), 1, "sell", "0.10000000", 10_000_000,
                [("TAKE_PROFIT", 700_000, "tp-client")],
            )
        body = json.loads(captured["data"].decode("utf-8"))
        self.assertEqual(out, [{"orderId": "abc", "status": "OPEN"}])
        self.assertEqual(body["grouping"], "positionTpsl")
        self.assertEqual(body["orders"][0]["accountIndex"], 0)
        self.assertEqual(body["orders"][0]["address"], _creds()["wallet"])
        self.assertIn("signature", body["orders"][0])
        self.assertIn("X-API-Key", captured["headers"])
        self.assertIn("X-Timestamp", captured["headers"])
        self.assertIn("X-Signature", captured["headers"])

    def test_signed_place_and_cancel_posts_include_address_without_changing_payload(self):
        hl = arcus()
        cases = [
            (
                "/v1/placeOrder",
                {
                    "ad": _creds()["wallet"].lower(), "ai": 0, "ct": 123456789,
                    "g": 1234567890000, "m": 1, "op": hl._ARCUS_OP_PLACE,
                    "p": 700000, "q": 10000000, "r": 0, "s": 0, "t": 0, "v": 1,
                },
                {"address": _creds()["wallet"], "accountIndex": 0, "marketId": 1},
            ),
            (
                "/v1/cancelOrder",
                {
                    "ad": _creds()["wallet"].lower(), "ai": 0, "ct": 123456789,
                    "id": "ord-1", "m": 1, "op": hl._ARCUS_OP_CANCEL, "v": 1,
                },
                {
                    "address": _creds()["wallet"], "accountIndex": 0,
                    "marketId": 1, "kind": "orderId", "orderId": "ord-1",
                },
            ),
        ]

        for path, typed_payload, body in cases:
            with self.subTest(path=path):
                captured: Dict[str, Any] = {}

                def fake_post(url, **kwargs):
                    captured["url"] = url
                    captured.update(kwargs)
                    return Resp(text='{"orderId":"abc"}')

                with mock.patch.object(hl, "_ARCUS_WRITE_GATE") as gate, \
                     mock.patch("requests.post", side_effect=fake_post) as post:
                    gate.wait_for_slot.return_value = 0.0
                    out = hl._signed_post(
                        _creds(), path, body,
                        typed_payload=typed_payload,
                        operation="auth_regression",
                    )

                self.assertEqual(out, {"orderId": "abc"})
                self.assertEqual(post.call_count, 1)
                self.assertEqual(captured["url"], f"http://test{path}")
                self.assertEqual(
                    captured.get("params"), {"address": _creds()["wallet"]}
                )
                self.assertEqual(captured["headers"]["X-Timestamp"], "123456789")
                self.assertEqual(json.loads(captured["data"]), body)

    def test_plain_batch_place_includes_address_without_changing_signature(self):
        hl = arcus()
        captured: Dict[str, Any] = {}
        timestamp_ns = 1_794_488_001_123_456_789
        good_til_time_us = 4_102_444_800_000_000
        child = {
            "price": Decimal("70000.0"),
            "size": Decimal("0.10000000"),
            "client_id": "ladder-1",
            "_market_id": 1,
            "_side": "buy",
            "_price_ticks": 700000,
            "_qty_quantums": 10000000,
            "_size_precision": 8,
            "_price_precision": 1,
        }

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Resp(text='{"responses":[{"orderId":"abc"}]}')

        with mock.patch.object(hl, "_ARCUS_WRITE_GATE") as gate, \
             mock.patch("requests.post", side_effect=fake_post) as post:
            gate.wait_for_slot.return_value = 0.0
            out = hl._submit_arcus_ladder_batch(
                _creds(), [child], timestamp_ns=timestamp_ns,
                good_til_time_us=good_til_time_us,
            )

        body = json.loads(captured["data"].decode("utf-8"))
        expected_signature, _ = hl._sign_arcus_ladder_child(
            credentials=_creds(), market_id=1, price_ticks=700000,
            qty_quantums=10000000, side="buy", time_in_force="gtt",
            good_til_time_us=good_til_time_us, timestamp_ns=timestamp_ns,
            reduce_only=False, client_id="ladder-1",
        )
        self.assertEqual(out, {"responses": [{"orderId": "abc"}]})
        self.assertEqual(post.call_count, 1)
        self.assertEqual(captured["url"], "http://test/v1/batchPlaceOrders")
        self.assertEqual(captured.get("params"), {"address": _creds()["wallet"]})
        self.assertEqual(body["orders"][0]["signature"], expected_signature)
        self.assertEqual(captured["headers"]["X-Signature"], expected_signature)

    def test_cancel_all_includes_address_without_changing_body(self):
        hl = arcus()
        captured: Dict[str, Any] = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Resp(status_code=202, text='{"status":"CANCEL_ALL_ACKNOWLEDGED"}')

        with mock.patch.object(hl, "_ARCUS_WRITE_GATE") as gate, \
             mock.patch("requests.post", side_effect=fake_post) as post:
            gate.wait_for_slot.return_value = 0.0
            out = hl._submit_cancel_all(
                _creds(), market_id=1, operation="cancel_order_group"
            )

        self.assertEqual(out, {"status": "CANCEL_ALL_ACKNOWLEDGED"})
        self.assertEqual(post.call_count, 1)
        self.assertEqual(captured["url"], "http://test/v1/cancelAllOrders")
        self.assertEqual(captured.get("params"), {"address": _creds()["wallet"]})
        self.assertEqual(
            json.loads(captured["data"].decode("utf-8")),
            {"address": _creds()["wallet"], "accountIndex": 0, "marketId": 1},
        )

    def test_market_order_direct_post_includes_address(self):
        hl = arcus()
        captured: Dict[str, Any] = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Resp(text='{"orderId":"abc"}')

        with mock.patch.object(hl, "_arcus_fetch_mark_price", return_value=Decimal("70000")), \
             mock.patch("requests.post", side_effect=fake_post) as post:
            out = hl._arcus_place_market_order(
                credentials=_creds(), account="amiroo", market=_MARKET,
                symbol="BTC-USD", side="buy", quantity=Decimal("0.1"),
                reduce_only=False, client_id="market-1",
                requested_volume=Decimal("0.1"),
            )

        self.assertTrue(out.success)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(captured["url"], "http://test/v1/placeOrder")
        self.assertEqual(captured.get("params"), {"address": _creds()["wallet"]})

    def test_close_position_direct_post_includes_address(self):
        hl = arcus()
        captured: Dict[str, Any] = {}
        context = ("long", Decimal("0.1"), Decimal("70000"), [])
        market_meta = {
            "market_id": 1, "tickSize": Decimal("0.1"),
            "stepSize": Decimal("0.00000001"),
            "size_precision": 8, "price_precision": 1,
        }

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Resp(text='{"orderId":"abc"}')

        with mock.patch.object(hl, "_lookup_credentials", return_value=_creds()), \
             mock.patch.object(hl, "_arcus_position_context", side_effect=[context, None]), \
             mock.patch.object(hl, "_arcus_resolve_market_meta", return_value=market_meta), \
             mock.patch.object(hl.time, "sleep", return_value=None), \
             mock.patch("requests.post", side_effect=fake_post) as post:
            out = hl._execute_close_position(
                "amiroo", {"symbol": "BTC-USD", "client_id": "close-1"}
            )

        self.assertTrue(out.success)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(captured["url"], "http://test/v1/placeOrder")
        self.assertEqual(captured.get("params"), {"address": _creds()["wallet"]})

    def test_failed_responses_preserve_http_status_and_log_safely(self):
        hl = arcus()
        records: List[str] = []

        def fake_post(*args, **kwargs):
            return Resp(status_code=401, text='{"error":"Invalid API key"}')

        def fake_warning(msg, *args, **kwargs):
            rendered = msg % args if args else str(msg)
            records.append(rendered)

        with mock.patch.object(hl, "_ARCUS_WRITE_GATE") as gate, \
             mock.patch("requests.post", side_effect=fake_post) as post, \
             mock.patch.object(hl._HTTP_LOGGER, "warning", side_effect=fake_warning):
            gate.wait_for_slot.return_value = 0.0
            with self.assertRaisesRegex(RuntimeError, "HTTP 401: Invalid API key"):
                hl._signed_post(_creds(), "/v1/placeOrder", {"address": _creds()["wallet"]},
                                typed_payload={"ad": _creds()["wallet"].lower(), "ai": 0, "ct": 123,
                                               "g": 456, "m": 1, "op": hl._ARCUS_OP_PLACE,
                                               "p": 1, "q": 1, "r": 0, "s": 0, "t": 0, "v": 1},
                                operation="new_order")
        self.assertEqual(post.call_count, 1)
        joined = "\n".join(records)
        self.assertIn("status=401", joined)
        self.assertIn("endpoint=/v1/placeOrder", joined)
        self.assertIn("account=amiroo", joined)
        self.assertIn("account_index=0", joined)
        self.assertIn("api_key_fp=", joined)
        self.assertIn("wallet_fp=", joined)
        self.assertIn("Invalid API key", joined)
        self.assertNotIn(_creds()["api_signing_key"], joined)
        self.assertNotIn(_creds()["private_key_hex"], joined)
        self.assertNotIn("X-Signature", joined)
        self.assertNotIn("signature", joined.lower())

    def test_tpsl_failed_response_logs_safely_and_does_not_retry(self):
        hl = arcus()
        records: List[str] = []

        def fake_post(*args, **kwargs):
            return Resp(status_code=403, text='{"error":"Invalid API key"}')

        def fake_warning(msg, *args, **kwargs):
            records.append(msg % args if args else str(msg))

        with mock.patch.object(hl, "_ARCUS_WRITE_GATE") as gate, \
             mock.patch("requests.post", side_effect=fake_post) as post, \
             mock.patch.object(hl._HTTP_LOGGER, "warning", side_effect=fake_warning):
            gate.wait_for_slot.return_value = 0.0
            with self.assertRaisesRegex(RuntimeError, "HTTP 403: Invalid API key"):
                hl._arcus_batch_place_tpsl(
                    _creds(), 1, "sell", "0.10000000", 10_000_000,
                    [("TAKE_PROFIT", 700_000, "tp-client")],
                )
        self.assertEqual(post.call_count, 1)
        joined = "\n".join(records)
        self.assertIn("status=403", joined)
        self.assertIn("endpoint=/v1/batchPlaceOrders", joined)
        self.assertIn("operation=tpsl_batch", joined)
        self.assertIn("Invalid API key", joined)
        self.assertNotIn(_creds()["api_signing_key"], joined)
        self.assertNotIn(_creds()["private_key_hex"], joined)
        self.assertNotIn("X-Signature", joined)
        self.assertNotIn("signature", joined.lower())


class ArcusRateLimit429Tests(unittest.TestCase):
    """429 on order-creating ops is NOT blindly retried; Retry-After captured."""

    def test_place_order_429_raises_rate_limited_not_retried(self):
        hl = arcus()
        post_calls: List[int] = []

        def fake_post(*a, **k):
            post_calls.append(1)
            return Resp(status_code=429, text='{"error":"rate limited"}',
                        headers={"Retry-After": "2"})

        with mock.patch.object(hl, "_lookup_credentials", return_value=_creds()), \
             mock.patch.object(hl, "_resolve_market", return_value=_MARKET), \
             mock.patch.object(hl, "_ARCUS_WRITE_GATE") as gate, \
             mock.patch("requests.post", side_effect=fake_post):
            gate.wait_for_slot.return_value = 0.0
            resp = hl.execute({
                "operation": "new_order", "exchange": "arcus", "account": "amiroo",
                "symbol": "BTC-USD", "side": "buy", "order_type": "limit",
                "volume": "0.1", "price": "70000",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "ORDER_SUBMISSION_FAILED")
        # Exactly one submit attempt — no blind auto-retry.
        self.assertEqual(len(post_calls), 1)

    def test_cancel_all_429_surfaces_rate_limited(self):
        hl = arcus()

        def fake_cancel_all(*a, **k):
            raise hl._ArcusRateLimitedError("HTTP 429", retry_after=2.0)

        with mock.patch.object(hl, "_lookup_credentials", return_value=_creds()), \
             mock.patch.object(hl, "_resolve_market", return_value=_MARKET), \
             mock.patch.object(hl, "_fetch_open_orders_for_account",
                              return_value=[{"orderId": "o1", "marketDisplayName": "BTC-USD", "side": "BUY"}]), \
             mock.patch.object(hl, "_submit_cancel_all", side_effect=fake_cancel_all):
            resp = hl.execute({
                "operation": "cancel_order_group", "exchange": "arcus", "account": "amiroo",
                "symbol": "BTC-USD", "side": "buy",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "ARCUS_RATE_LIMITED")
        self.assertIn("2.0", resp.error.message)

    def test_batch_cancel_429_surfaces_rate_limited(self):
        hl = arcus()

        def fake_submit(*a, **k):
            raise hl._ArcusRateLimitedError("HTTP 429", retry_after=3.0)

        with mock.patch.object(hl, "_lookup_credentials", return_value=_creds()), \
             mock.patch.object(hl, "_resolve_market", return_value=_MARKET), \
             mock.patch.object(hl, "_fetch_open_orders_for_account",
                              return_value=[
                                  {"orderId": "o1", "marketDisplayName": "BTC-USD", "side": "BUY"},
                                  {"orderId": "o2", "marketDisplayName": "BTC-USD", "side": "SELL"},
                              ]), \
             mock.patch.object(hl, "_submit_batch_cancel", side_effect=fake_submit):
            resp = hl.execute({
                "operation": "cancel_order_group", "exchange": "arcus", "account": "amiroo",
                "symbol": "BTC-USD", "side": "buy",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "ARCUS_RATE_LIMITED")


if __name__ == "__main__":
    unittest.main()