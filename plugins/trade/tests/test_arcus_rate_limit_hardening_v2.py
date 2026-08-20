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

    def test_batch_cancel_builds_cancels_array(self):
        hl = arcus()
        captured: Dict[str, Any] = {}

        def fake_post(url, headers=None, data=None, timeout=20):
            captured["data"] = data.decode("utf-8") if isinstance(data, bytes) else data
            captured["headers"] = headers
            return Resp(text='{"responses":[]}')

        with mock.patch.object(hl, "_ARCUS_WRITE_GATE") as gate, \
             mock.patch("requests.post", side_effect=fake_post):
            gate.wait_for_slot.return_value = 0.0
            hl._submit_batch_cancel(_creds(), 1, [
                {"orderId": "ord-1"}, {"orderId": "ord-2"},
            ], operation="cancel_order_group")
        body = json.loads(captured["data"])
        self.assertIn("cancels", body)
        self.assertEqual(len(body["cancels"]), 2)
        self.assertEqual(body["cancels"][0]["kind"], "orderId")
        self.assertIn("signature", body["cancels"][0])
        self.assertIn("X-Signature", captured["headers"])

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