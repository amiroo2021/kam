"""Aftermath cancel_order_group: live flag, batching, fail-closed verify."""

from __future__ import annotations

import base64
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.trade.agents import x_aftermath_agent as aftermath  # noqa: E402

ZEC_CH = "0x435987c9e1b8f61a4cdfa220751b3324ebf5a7bfd1250b25c79412a71a20392f"


def _pending(ids, side="sell"):
    rows = []
    for oid in ids:
        rows.append(
            {
                "id": str(oid),
                "status": "open",
                "symbol": "ZEC/USD:USDC",
                "type": "limit",
                "side": side,
                "price": 1400.0,
                "amount": 0.1,
                "filled": 0,
                "remaining": 0.1,
            }
        )
    return rows


class AftermathCancelGroupTests(unittest.TestCase):
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
        import secrets
        from pysui_fastcrypto import encode_bech32

        priv = encode_bech32(b"\x00" + secrets.token_bytes(32), "suiprivkey")
        os.environ["AFTERMATH_MAIN_AGENT_ADDRESS"] = "0x" + "1" * 64
        os.environ["AFTERMATH_MAIN_AGENT_PRIVATEKEY"] = priv
        os.environ["AFTERMATH_MAIN_ACCOUNT_ID"] = "12345"

    def tearDown(self) -> None:
        for key in list(os.environ):
            if key.startswith("AFTERMATH_"):
                os.environ.pop(key)
        os.environ.update(self._saved_env)
        if self._saved_hermes_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._saved_hermes_home

    def fake_post_factory(self, *, pending_sequence, submit_bodies=None, submit_error=None, build_error=None):
        submit_calls: list = []
        build_calls: list = []
        pending_calls = {"n": 0}

        def fake_post(credentials, path, payload):
            if path == "/api/perpetuals/all-markets":
                return {
                    "markets": [
                        {
                            "objectId": ZEC_CH,
                            "marketParams": {
                                "baseAssetSymbol": "ZECUSD",
                                "tickSize": "10000n",
                                "lotSize": "10000n",
                                "scalingFactor": 1e-6,
                                "minOrderUsdValue": 1.0,
                            },
                        }
                    ]
                }
            if path == "/api/ccxt/accounts":
                return [
                    {
                        "id": "0x55d70f824bde7d1a5541e5fb4322d1dc53a1ec9c36e827f17469b70b1b310563",
                        "type": "capability",
                        "accountNumber": "12345",
                    }
                ]
            if path == "/api/ccxt/myPendingOrders":
                idx = pending_calls["n"]
                pending_calls["n"] += 1
                if idx >= len(pending_sequence):
                    return pending_sequence[-1]
                return pending_sequence[idx]
            if path.startswith("/api/ccxt/build/"):
                if build_error:
                    raise RuntimeError(build_error)
                build_calls.append((path, dict(payload)))
                return {
                    "transactionBytes": base64.b64encode(b"FAKE-TX-BYTES").decode(),
                    "signingDigest": base64.b64encode(b"\x00" * 32).decode(),
                }
            if path.startswith("/api/ccxt/submit/"):
                if submit_error:
                    raise RuntimeError(submit_error)
                body = {"digest": "0xCANCEL-DIGEST", "txDigest": "0xCANCEL-DIGEST"}
                if submit_bodies is not None:
                    body = submit_bodies[len(submit_calls)] if len(submit_calls) < len(submit_bodies) else submit_bodies[-1]
                submit_calls.append((path, dict(payload)))
                return body
            if path == "/api/perpetuals/markets":
                return {"markets": [{"marketId": ZEC_CH, "symbol": "ZEC"}]}
            raise AssertionError(f"unexpected POST path: {path}")

        return fake_post, submit_calls, build_calls

    def test_confirmed_live_flag_overrides_unset_env_default(self) -> None:
        os.environ.pop("AFTERMATH_DRY_RUN", None)
        ids = ["1", "2"]
        fake_post, submit_calls, _ = self.fake_post_factory(
            pending_sequence=[_pending(ids), []]
        )
        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            resp = aftermath.execute(
                {
                    "operation": "cancel_order_group",
                    "account": "main",
                    "exchange": "aftermath",
                    "symbol": "ZEC",
                    "side": "sell",
                    "dry_run": False,
                }
            )
        self.assertTrue(resp.success)
        self.assertTrue(resp.cancel_group.verified)
        self.assertEqual(len(submit_calls), 1)

    def test_env_default_dry_run_does_not_submit(self) -> None:
        ids = ["1", "2"]
        fake_post, submit_calls, build_calls = self.fake_post_factory(
            pending_sequence=[_pending(ids)]
        )
        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            resp = aftermath.execute(
                {
                    "operation": "cancel_order_group",
                    "account": "main",
                    "exchange": "aftermath",
                    "symbol": "ZEC",
                    "side": "sell",
                }
            )
        self.assertEqual(len(submit_calls), 0)
        self.assertTrue(build_calls)
        self.assertFalse(resp.cancel_group.verified)
        self.assertEqual(resp.cancel_group.status, "dry_run")
        self.assertEqual(resp.cancel_group.cancelled_order_count, 0)

    def test_more_than_20_orders_are_batched(self) -> None:
        ids = [str(i) for i in range(24)]
        fake_post, submit_calls, build_calls = self.fake_post_factory(
            pending_sequence=[_pending(ids), []]
        )
        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            resp = aftermath.execute(
                {
                    "operation": "cancel_order_group",
                    "account": "main",
                    "symbol": "ZEC",
                    "side": "sell",
                    "dry_run": False,
                }
            )
        self.assertEqual(len(submit_calls), 2)
        self.assertEqual(len(build_calls[0][1]["orderIds"]), 20)
        self.assertEqual(len(build_calls[1][1]["orderIds"]), 4)
        self.assertTrue(resp.success)
        self.assertEqual(resp.cancel_group.batch_count, 2)
        self.assertEqual(resp.cancel_group.cancelled_order_count, 24)

    def test_partial_batch_failure_is_not_success(self) -> None:
        ids = [str(i) for i in range(24)]
        n = {"builds": 0}

        def fake_post(credentials, path, payload):
            if path == "/api/perpetuals/all-markets":
                return {
                    "markets": [
                        {"objectId": ZEC_CH, "marketParams": {"baseAssetSymbol": "ZECUSD"}}
                    ]
                }
            if path == "/api/ccxt/accounts":
                return [
                    {
                        "id": "0x55d70f824bde7d1a5541e5fb4322d1dc53a1ec9c36e827f17469b70b1b310563",
                        "type": "capability",
                        "accountNumber": "12345",
                    }
                ]
            if path == "/api/ccxt/myPendingOrders":
                return _pending(ids) if n["builds"] < 2 else _pending(ids[20:])
            if path.startswith("/api/ccxt/build/"):
                n["builds"] += 1
                if n["builds"] == 2:
                    raise RuntimeError("Aftermath HTTP 500 on /api/ccxt/build/cancelOrders")
                return {
                    "transactionBytes": base64.b64encode(b"FAKE").decode(),
                    "signingDigest": base64.b64encode(b"\x00" * 32).decode(),
                }
            if path.startswith("/api/ccxt/submit/"):
                return {"digest": "0xOK", "txDigest": "0xOK"}
            raise AssertionError(path)

        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            resp = aftermath.execute(
                {
                    "operation": "cancel_order_group",
                    "account": "main",
                    "symbol": "ZEC",
                    "side": "sell",
                    "dry_run": False,
                }
            )
        self.assertFalse(resp.success)
        self.assertFalse(resp.cancel_group.verified)
        self.assertGreater(resp.cancel_group.remaining_target_count, 0)

    def test_failed_transaction_submission_is_not_success(self) -> None:
        ids = ["1", "2"]
        fake_post, _, _ = self.fake_post_factory(
            pending_sequence=[_pending(ids), _pending(ids)],
            submit_error="Aftermath HTTP 500 on /api/ccxt/submit/cancelOrders",
        )
        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            resp = aftermath.execute(
                {
                    "operation": "cancel_order_group",
                    "account": "main",
                    "symbol": "ZEC",
                    "side": "sell",
                    "dry_run": False,
                }
            )
        self.assertFalse(resp.success)
        self.assertFalse(resp.cancel_group.verified)
        self.assertEqual(resp.cancel_group.remaining_target_count, 2)

    def test_http_200_unsuccessful_transaction_is_not_success(self) -> None:
        ids = ["1", "2"]
        fake_post, _, _ = self.fake_post_factory(
            pending_sequence=[_pending(ids), _pending(ids)],
            submit_bodies=[{"effects": {"status": {"status": "failure", "error": "MoveAbort"}}}],
        )
        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            resp = aftermath.execute(
                {
                    "operation": "cancel_order_group",
                    "account": "main",
                    "symbol": "ZEC",
                    "side": "sell",
                    "dry_run": False,
                }
            )
        self.assertFalse(resp.success)
        self.assertFalse(resp.cancel_group.verified)

    def test_orders_remaining_after_submission_is_not_success(self) -> None:
        ids = ["1", "2"]
        fake_post, _, _ = self.fake_post_factory(
            pending_sequence=[_pending(ids), _pending(ids)]
        )
        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            resp = aftermath.execute(
                {
                    "operation": "cancel_order_group",
                    "account": "main",
                    "symbol": "ZEC",
                    "side": "sell",
                    "dry_run": False,
                }
            )
        self.assertFalse(resp.success)
        self.assertEqual(resp.cancel_group.cancelled_order_count, 0)
        self.assertEqual(resp.cancel_group.remaining_target_count, 2)

    def test_already_absent_ids_count_as_verified_gone(self) -> None:
        fake_post, submit_calls, _ = self.fake_post_factory(pending_sequence=[[], []])
        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            resp = aftermath.execute(
                {
                    "operation": "cancel_order_group",
                    "account": "main",
                    "symbol": "ZEC",
                    "side": "sell",
                    "dry_run": False,
                    "order_ids": ["gone-1", "gone-2"],
                }
            )
        self.assertEqual(len(submit_calls), 0)
        self.assertTrue(resp.success)
        self.assertTrue(resp.cancel_group.verified)
        self.assertEqual(resp.cancel_group.confirmed_absent_count, 2)
        self.assertEqual(resp.cancel_group.remaining_target_count, 0)

    def test_duplicate_live_request_does_not_resubmit_when_already_gone(self) -> None:
        fake_post, submit_calls, _ = self.fake_post_factory(
            pending_sequence=[_pending(["1", "2"]), [], []]
        )
        req = {
            "operation": "cancel_order_group",
            "account": "main",
            "symbol": "ZEC",
            "side": "sell",
            "dry_run": False,
        }
        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            first = aftermath.execute(req)
            second = aftermath.execute(req)
        self.assertTrue(first.success)
        self.assertTrue(second.cancel_group.verified)
        self.assertEqual(len(submit_calls), 1)

    def test_side_filter_does_not_cancel_buys(self) -> None:
        mixed = _pending(["s1", "s2"], side="sell") + _pending(["b1"], side="buy")
        captured = []

        def fake_post(credentials, path, payload):
            if path == "/api/perpetuals/all-markets":
                return {"markets": [{"objectId": ZEC_CH, "marketParams": {"baseAssetSymbol": "ZECUSD"}}]}
            if path == "/api/ccxt/accounts":
                return [
                    {
                        "id": "0x55d70f824bde7d1a5541e5fb4322d1dc53a1ec9c36e827f17469b70b1b310563",
                        "type": "capability",
                        "accountNumber": "12345",
                    }
                ]
            if path == "/api/ccxt/myPendingOrders":
                return mixed if not captured else [row for row in mixed if row["side"] == "buy"]
            if path.startswith("/api/ccxt/build/"):
                captured.append(list(payload.get("orderIds") or []))
                return {
                    "transactionBytes": base64.b64encode(b"FAKE").decode(),
                    "signingDigest": base64.b64encode(b"\x00" * 32).decode(),
                }
            if path.startswith("/api/ccxt/submit/"):
                return {"digest": "0xOK", "txDigest": "0xOK"}
            raise AssertionError(path)

        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            resp = aftermath.execute(
                {
                    "operation": "cancel_order_group",
                    "account": "main",
                    "symbol": "ZEC",
                    "side": "sell",
                    "dry_run": False,
                }
            )
        self.assertEqual(captured[0], ["s1", "s2"])
        self.assertNotIn("b1", captured[0])
        self.assertTrue(resp.success)

    def test_cancel_logs_counts_without_secrets(self) -> None:
        ids = ["1", "2"]
        fake_post, _, _ = self.fake_post_factory(pending_sequence=[_pending(ids), []])
        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post):
            with self.assertLogs(aftermath.logger, level="WARNING") as captured:
                aftermath.execute(
                    {
                        "operation": "cancel_order_group",
                        "account": "main",
                        "symbol": "ZEC",
                        "side": "sell",
                        "dry_run": False,
                    }
                )
        text = "\n".join(captured.output)
        self.assertIn("account=main", text)
        self.assertIn("symbol=ZEC", text)
        self.assertIn("side=sell", text)
        self.assertIn("targeted=2", text)
        self.assertIn("cancelled=2", text)
        self.assertIn("remaining=0", text)
        self.assertIn("dry_run=False", text)
        self.assertNotIn("suiprivkey", text.lower())
        self.assertNotIn("private", text.lower())

    def test_submit_timeout_does_not_retry_and_is_not_success(self) -> None:
        ids = [str(i) for i in range(24)]
        fake_post, _submits, builds = self.fake_post_factory(pending_sequence=[_pending(ids), _pending(ids)])
        n = {"submit": 0}

        def fake_post_timeout(credentials, path, payload):
            if path.startswith("/api/ccxt/submit/"):
                n["submit"] += 1
                raise TimeoutError("submit timed out")
            return fake_post(credentials, path, payload)

        with mock.patch.object(aftermath, "_post_json", side_effect=fake_post_timeout):
            resp = aftermath.execute(
                {
                    "operation": "cancel_order_group",
                    "account": "main",
                    "symbol": "ZEC",
                    "side": "sell",
                    "dry_run": False,
                }
            )
        self.assertFalse(resp.success)
        assert resp.cancel_group is not None
        self.assertFalse(resp.cancel_group.verified)
        self.assertEqual(n["submit"], 1)
        self.assertEqual(len(builds), 1)

    def test_verify_read_failure_is_not_success(self) -> None:
        ids = ["1", "2"]
        fake_post, _, _ = self.fake_post_factory(pending_sequence=[_pending(ids), _pending(ids)])
        n = {"pending": 0}

        def wrapped(credentials, path, payload):
            if path == "/api/ccxt/myPendingOrders":
                n["pending"] += 1
                if n["pending"] > 1:
                    raise RuntimeError("myPendingOrders down")
            return fake_post(credentials, path, payload)

        with mock.patch.object(aftermath, "_post_json", side_effect=wrapped):
            resp = aftermath.execute(
                {
                    "operation": "cancel_order_group",
                    "account": "main",
                    "symbol": "ZEC",
                    "side": "sell",
                    "dry_run": False,
                }
            )
        self.assertFalse(resp.success)
        self.assertIsNotNone(resp.cancel_group)
        self.assertFalse(resp.cancel_group.verified)


if __name__ == "__main__":
    unittest.main()
