"""Arcus rate-limit hardening tests (offline, no live HTTP)."""

from __future__ import annotations

import time
import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest import mock

from plugins.trade.agents import x_arcus_agent as A
from plugins.trade.fibo_service import (
    SHUTDOWN_MODE_EMERGENCY,
    STATUS_NEEDS_RECOVERY,
    STATUS_RUNNING,
    STATUS_STOPPING,
    PersistentFiboService,
)
from plugins.trade.golden_fibo.state import GoldenFiboState
from plugins.trade.golden_fibo.arcus_adapter import ArcusGoldenFiboAdapter


class _Resp:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 429:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class ArcusGetGateTests(unittest.TestCase):
    def setUp(self):
        A.arcus_clear_get_cache()
        A._ARCUS_GET_GATE._backoff_until = 0.0
        A._ARCUS_GET_GATE._backoff_seconds = 0.0
        self.creds = {
            "base_url": "https://api.arcus.xyz",
            "wallet": "0xabc",
            "account_index": 0,
        }

    def test_cache_coalesces_identical_gets(self):
        calls = {"n": 0}

        def fake_get(url, params=None, timeout=None):
            calls["n"] += 1
            return _Resp(200, {"orders": [], "n": calls["n"]})

        with mock.patch.object(A.requests, "get", side_effect=fake_get):
            a = A._public_get(self.creds, "/v1/openOrders")
            b = A._public_get(self.creds, "/v1/openOrders")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(a, b)

    def test_retry_after_respected(self):
        def fake_get(url, params=None, timeout=None):
            return _Resp(429, {"error": "rate"}, headers={"Retry-After": "2"})

        with mock.patch.object(A.requests, "get", side_effect=fake_get):
            with self.assertRaises(RuntimeError) as ctx:
                A._public_get(self.creds, "/v1/account", allow_stale_on_backoff=False)
        self.assertIn("429", str(ctx.exception))
        self.assertGreaterEqual(A.arcus_http_backoff_remaining(), 1.0)

    def test_exponential_backoff_grows(self):
        def fake_get(url, params=None, timeout=None):
            return _Resp(429, {})

        with mock.patch.object(A.requests, "get", side_effect=fake_get):
            with self.assertRaises(RuntimeError):
                A._public_get(self.creds, "/v1/account", allow_stale_on_backoff=False)
            first = A._ARCUS_GET_GATE._backoff_seconds
            A._ARCUS_GET_GATE._backoff_until = 0.0  # allow next attempt to run
            with self.assertRaises(RuntimeError):
                A._public_get(self.creds, "/v1/account", allow_stale_on_backoff=False)
            second = A._ARCUS_GET_GATE._backoff_seconds
        self.assertGreater(second, first)

    def test_stale_cache_served_during_backoff(self):
        seq = [
            _Resp(200, {"ok": True, "v": 1}),
            _Resp(429, {}, headers={"Retry-After": "5"}),
        ]

        def fake_get(url, params=None, timeout=None):
            return seq.pop(0) if seq else _Resp(429, {})

        with mock.patch.object(A.requests, "get", side_effect=fake_get):
            first = A._public_get(self.creds, "/v1/account", force_refresh=True)
            # expire TTL
            A._ARCUS_GET_GATE._cache[A._ARCUS_GET_GATE._key(self.creds, "/v1/account")] = (
                time.time() - 10,
                first,
            )
            second = A._public_get(self.creds, "/v1/account", allow_stale_on_backoff=True)
        self.assertEqual(second.get("v"), 1)


class PositionContextHardeningTests(unittest.TestCase):
    def setUp(self):
        A.arcus_clear_get_cache()
        A._ARCUS_GET_GATE._backoff_until = 0.0
        self.creds = {
            "base_url": "https://api.arcus.xyz",
            "wallet": "0xabc",
            "account_index": 0,
            "account": "metamask",
        }

    def test_account_ok_openorders_429_still_returns_position(self):
        def fake_get(url, params=None, timeout=None):
            if url.endswith("/v1/account"):
                return _Resp(
                    200,
                    {
                        "positions": {
                            "3": {
                                "marketDisplayName": "SOL-USD",
                                "side": "long",
                                "size": "0.2",
                                "markPx": "78.5",
                                "averageEntryPrice": "78.4",
                            }
                        }
                    },
                )
            return _Resp(429, {}, headers={"Retry-After": "3"})

        with mock.patch.object(A, "_lookup_credentials", return_value=self.creds):
            with mock.patch.object(A.requests, "get", side_effect=fake_get):
                A.arcus_clear_get_cache()
                A._ARCUS_GET_GATE._backoff_until = 0.0
                ctx = A._arcus_position_context("metamask", "SOL-USD")
        self.assertIsNotNone(ctx)
        side, size, mark, orders = ctx
        self.assertEqual(side, "long")
        self.assertEqual(size, Decimal("0.2"))
        self.assertEqual(orders, [])  # unavailable, not invented clean identity

    def test_position_state_op_does_not_require_open_orders(self):
        def fake_get(url, params=None, timeout=None):
            if url.endswith("/v1/account"):
                return _Resp(
                    200,
                    {
                        "positions": {
                            "3": {
                                "marketDisplayName": "SOL-USD",
                                "side": "LONG",
                                "size": "0.2",
                                "averageEntryPrice": "78.4",
                                "unrealizedPnl": "0",
                            }
                        }
                    },
                )
            if url.endswith("/v1/markets"):
                return _Resp(
                    200,
                    {
                        "markets": [
                            {
                                "marketId": 3,
                                "marketDisplayName": "SOL-USD",
                                "baseAsset": "SOL",
                                "tickSize": "0.001",
                                "stepSize": "0.000001",
                                "minOrderNotional": "10",
                            }
                        ]
                    },
                )
            return _Resp(429, {})

        with mock.patch.object(A, "_lookup_credentials", return_value=self.creds):
            with mock.patch.object(A.requests, "get", side_effect=fake_get):
                A.arcus_clear_get_cache()
                A._ARCUS_GET_GATE._backoff_until = 0.0
                resp = A.execute(
                    {
                        "operation": "position_state",
                        "account": "metamask",
                        "symbol": "SOL-USD",
                    }
                )
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.positions or []), 1)
        self.assertEqual(resp.positions[0].side, "long")


class EmergencyPollIsolationTests(unittest.TestCase):
    def test_tick_skips_emergency_shutdown_mode(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            svc = PersistentFiboService(
                state_path=Path(tmp) / "s.json",
                ledger_path=Path(tmp) / "l.jsonl",
                event_log_path=Path(tmp) / "e.log",
                start_thread=False,
            )
            key = "arcus/metamask/SOL-USD/BUY"
            st = GoldenFiboState(
                registration_key=key,
                exchange="arcus",
                account="metamask",
                instrument="SOL-USD",
                direction="BUY",
                status=STATUS_NEEDS_RECOVERY,
                shutdown_mode=SHUTDOWN_MODE_EMERGENCY,
            )
            svc._states[key] = st
            called = {"n": 0}

            def boom(k):
                called["n"] += 1

            svc._drive_one = boom  # type: ignore
            svc._tick_once()
            self.assertEqual(called["n"], 0)

    def test_tick_skips_arcus_needs_recovery_while_backoff(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            svc = PersistentFiboService(
                state_path=Path(tmp) / "s.json",
                ledger_path=Path(tmp) / "l.jsonl",
                event_log_path=Path(tmp) / "e.log",
                start_thread=False,
            )
            key = "arcus/metamask/SOL-USD/BUY"
            st = GoldenFiboState(
                registration_key=key,
                exchange="arcus",
                account="metamask",
                instrument="SOL-USD",
                direction="BUY",
                status=STATUS_NEEDS_RECOVERY,
                shutdown_mode="",
            )
            svc._states[key] = st
            called = {"n": 0}

            def boom(k):
                called["n"] += 1

            svc._drive_one = boom  # type: ignore
            with mock.patch.object(A, "arcus_http_backoff_remaining", return_value=5.0):
                svc._tick_once()
            self.assertEqual(called["n"], 0)


class EmergencyStopNoDuplicateTests(unittest.TestCase):
    def test_emergency_bounded_read_no_duplicate_close_on_persistent_429(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            svc = PersistentFiboService(
                state_path=Path(tmp) / "s.json",
                ledger_path=Path(tmp) / "l.jsonl",
                event_log_path=Path(tmp) / "e.log",
                start_thread=False,
            )
            key = "arcus/metamask/SOL-USD/BUY"
            st = GoldenFiboState(
                registration_key=key,
                exchange="arcus",
                account="metamask",
                instrument="SOL-USD",
                direction="BUY",
                status=STATUS_RUNNING,
                pending_order_exchange_id=111,
                current_tp_order_id=222,
                cycle_uid=1,
                client_id_version=2,
            )
            svc._states[key] = st

            class BadAdapter:
                name = "golden_fibo_arcus"
                closes = 0
                cancels = 0

                def position_state(self, account, instrument):
                    raise RuntimeError("429 Client Error: Too Many Requests")

                def cancel_order(self, **kw):
                    self.cancels += 1
                    return True

                def close_position(self, **kw):
                    self.closes += 1
                    return {"success": True, "verified": True}

            ad = BadAdapter()
            svc._adapters[key] = ad
            # speed up sleeps
            with mock.patch("plugins.trade.fibo_service.time.sleep", return_value=None):
                with mock.patch.object(A, "arcus_http_backoff_remaining", return_value=0.0):
                    resp = svc.execute_command({"op": "emergency_stop", "registration_key": key})
            self.assertFalse(resp.get("ok"))
            self.assertEqual(ad.closes, 0)  # no close without position read
            self.assertEqual(ad.cancels, 0)
            st2 = svc._states[key]
            self.assertEqual(st2.shutdown_mode, SHUTDOWN_MODE_EMERGENCY)
            self.assertEqual(st2.status, STATUS_STOPPING)

    def test_emergency_succeeds_after_transient_429(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            svc = PersistentFiboService(
                state_path=Path(tmp) / "s.json",
                ledger_path=Path(tmp) / "l.jsonl",
                event_log_path=Path(tmp) / "e.log",
                start_thread=False,
            )
            key = "arcus/metamask/SOL-USD/BUY"
            st = GoldenFiboState(
                registration_key=key,
                exchange="arcus",
                account="metamask",
                instrument="SOL-USD",
                direction="BUY",
                status=STATUS_RUNNING,
                pending_order_exchange_id=111,
                current_tp_order_id=222,
                cycle_uid=9,
                highest_filled_step=0,
                client_id_version=2,
            )
            svc._states[key] = st

            class FlakyAdapter:
                name = "golden_fibo_arcus"

                def __init__(self):
                    self.pos_calls = 0
                    self.closes = 0
                    self.cancels = []

                def position_state(self, account, instrument):
                    self.pos_calls += 1
                    if self.pos_calls == 1:
                        raise RuntimeError("429 Client Error: Too Many Requests")
                    if self.closes:
                        return {"symbol": "SOL-USD", "side": None, "size": "0"}
                    return {"symbol": "SOL-USD", "side": "long", "size": "0.2"}

                def cancel_order(self, **kw):
                    self.cancels.append(kw.get("order_index"))
                    return True

                def close_position(self, **kw):
                    self.closes += 1
                    return {
                        "success": True,
                        "verified": True,
                        "client_order_id": kw.get("client_order_id"),
                    }

                def get_order_state(self, account, order_index):
                    return {}

            ad = FlakyAdapter()
            svc._adapters[key] = ad
            with mock.patch("plugins.trade.fibo_service.time.sleep", return_value=None):
                with mock.patch.object(A, "arcus_http_backoff_remaining", return_value=0.0):
                    resp = svc.execute_command({"op": "emergency_stop", "registration_key": key})
            self.assertTrue(resp.get("ok"), resp)
            self.assertEqual(ad.closes, 1)
            self.assertIn(111, ad.cancels)
            self.assertNotIn(key, svc._states)


if __name__ == "__main__":
    unittest.main()
