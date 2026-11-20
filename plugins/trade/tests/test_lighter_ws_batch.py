"""Offline tests for Lighter WS sendtxbatch ladders and REST-15 fallback.

No live network. Stubs cover jsonapi/sendtxbatch, nonce reuse, halt rules,
canonical child counts, and the wizard in-flight Confirm guard.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

_EDITABLE_FINDER = "__editable___hermes_agent_0_20_0_finder"
if any(name in repr(h) for h in sys.path_hooks for name in (_EDITABLE_FINDER,)):
    sys.path_hooks[:] = [
        h for h in sys.path_hooks if _EDITABLE_FINDER not in repr(h)
    ]

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("LIGHTER_RH_CHAIN", "ROBINHOOD")
os.environ.setdefault("LIGHTER_RH_ACCOUNT_INDEX", "42")
os.environ.setdefault("LIGHTER_RH_APIKEY_INDEX", "7")
os.environ.setdefault("LIGHTER_RH_PUBLIC_KEY", "0x" + "ab" * 32)
os.environ.setdefault("LIGHTER_RH_PRIVATE_KEY", "0x" + "cd" * 32)

import plugins.trade.agents.x_lighter_agent as lighter  # noqa: E402
from plugins.trade.tests.test_lighter_send_tx_batch import (  # noqa: E402
    TxTypeL2CreateOrder,
    _ladder_request,
    _patch_lighter_for_test,
)


class _FakeWs:
    def __init__(self, responses: Optional[List[Any]] = None) -> None:
        self.sent: List[Dict[str, Any]] = []
        self.responses = list(responses or [])
        self.closed = False
        self.recv_error: Optional[BaseException] = None
        self.send_error: Optional[BaseException] = None

    async def send(self, raw: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if self.recv_error is not None:
            raise self.recv_error
        if not self.responses:
            raise TimeoutError("no fake ws response")
        item = self.responses.pop(0)
        return item if isinstance(item, str) else json.dumps(item)

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> "_FakeWs":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        await self.close()
        return False


class LighterWsBatchLadderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_rest = lighter.LIGHTER_REST_SEND_TX_BATCH_SIZE
        self._saved_ws = lighter.LIGHTER_WS_SEND_TX_BATCH_SIZE
        self._saved_pause = lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS
        lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS = 0.0
        lighter._LIGHTER_LIMITERS.clear()
        lighter._LIGHTER_AUTH_TOKEN_CACHE.clear()
        lighter._LIGHTER_L2_TX_BUDGETS.clear()

    def tearDown(self) -> None:
        lighter.LIGHTER_REST_SEND_TX_BATCH_SIZE = self._saved_rest
        lighter.LIGHTER_WS_SEND_TX_BATCH_SIZE = self._saved_ws
        lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS = self._saved_pause
        lighter._LIGHTER_LIMITERS.clear()
        lighter._LIGHTER_AUTH_TOKEN_CACHE.clear()
        lighter._LIGHTER_L2_TX_BUDGETS.clear()

    def _force_rest(self, test_state_owner: unittest.TestCase) -> None:
        p = mock.patch.object(
            lighter,
            "_open_lighter_ladder_ws",
            side_effect=lighter._LighterWsUnavailable("forced-rest"),
        )
        p.start()
        test_state_owner.addCleanup(p.stop)

    def _use_ws(self, fake: _FakeWs) -> None:
        async def open_ws(credentials):  # noqa: ARG001
            return fake

        p = mock.patch.object(lighter, "_open_lighter_ladder_ws", side_effect=open_ws)
        p.start()
        self.addCleanup(p.stop)

    def test_rest_fallback_chunks_15_not_30(self) -> None:
        self._force_rest(self)
        n_batches = (50 + 14) // 15
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)] * n_batches,
            auto_reconcile_with_allocator=True,
        )
        resp = lighter.execute(_ladder_request(order_count=50))
        self.assertTrue(resp.success, resp.error)
        sizes = [len(c["tx_infos"]) for c in state["signer"].send_tx_batch_calls]
        self.assertEqual(sizes, [15, 15, 15, 5])
        self.assertLessEqual(max(sizes), 15)

    def test_ws_50_children_one_message(self) -> None:
        fake = _FakeWs()
        fake.responses.append({
            "type": "jsonapi/sendtxbatch",
            "data": {
                "id": "will-be-overwritten",
                "code": 200,
                "tx_hash": [f"0x{i:064x}" for i in range(50)],
            },
        })
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[],
            auto_reconcile_with_allocator=True,
        )
        self._use_ws(fake)
        resp = lighter.execute(_ladder_request(order_count=50))
        self.assertTrue(resp.success, resp.error)
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 0)
        self.assertEqual(len(fake.sent), 1)
        msg = fake.sent[0]
        self.assertEqual(msg["type"], "jsonapi/sendtxbatch")
        types = json.loads(msg["data"]["tx_types"])
        infos = json.loads(msg["data"]["tx_infos"])
        self.assertEqual(len(types), 50)
        self.assertEqual(len(infos), 50)
        self.assertTrue(all(t == TxTypeL2CreateOrder for t in types))
        self.assertLessEqual(len(json.dumps(msg).encode("utf-8")), lighter.LIGHTER_WS_MAX_MESSAGE_BYTES)
        self.assertEqual(len(state["signer"].sign_create_order_calls), 50)

    def test_ws_100_two_sequential_messages_same_signer(self) -> None:
        fake = _FakeWs()
        for n in (50, 50):
            fake.responses.append({
                "type": "jsonapi/sendtxbatch",
                "data": {"code": 200, "tx_hash": [f"0x{i:064x}" for i in range(n)]},
            })
        builds = {"n": 0}
        orig_build = None

        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[],
            auto_reconcile_with_allocator=True,
        )
        self._use_ws(fake)
        real_build = lighter._build_signer_client

        def counting_build(creds):
            builds["n"] += 1
            return real_build(creds)

        with mock.patch.object(lighter, "_build_signer_client", side_effect=counting_build):
            # _patch already patched build to stub; counting_build calls that stub
            resp = lighter.execute(_ladder_request(order_count=100))
        self.assertTrue(resp.success, resp.error)
        self.assertEqual(len(fake.sent), 2)
        self.assertEqual(builds["n"], 1)
        nonces = [c["kwargs"]["nonce"] for c in state["signer"].sign_create_order_calls]
        self.assertEqual(nonces, list(range(nonces[0], nonces[0] + 100)))

    def test_ws_250_five_messages(self) -> None:
        fake = _FakeWs()
        for _ in range(5):
            fake.responses.append({
                "type": "jsonapi/sendtxbatch",
                "data": {"code": 200, "tx_hash": [f"0x{i:064x}" for i in range(50)]},
            })
        _patch_lighter_for_test(self, send_tx_outcomes=[], auto_reconcile_with_allocator=True)
        self._use_ws(fake)
        resp = lighter.execute(_ladder_request(order_count=250, total_volume="500"))
        self.assertTrue(resp.success, resp.error)
        self.assertEqual(len(fake.sent), 5)
        self.assertEqual(resp.ladder.batch_count, 5)
        self.assertEqual(resp.ladder.accepted_child_count, 250)

    def test_ws_disconnect_before_send_falls_back_to_rest_15(self) -> None:
        async def boom(credentials):  # noqa: ARG001
            raise lighter._LighterWsUnavailable("connect failed")

        p = mock.patch.object(lighter, "_open_lighter_ladder_ws", side_effect=boom)
        p.start()
        self.addCleanup(p.stop)
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)] * 2,
            auto_reconcile_with_allocator=True,
        )
        resp = lighter.execute(_ladder_request(order_count=20))
        self.assertTrue(resp.success, resp.error)
        sizes = [len(c["tx_infos"]) for c in state["signer"].send_tx_batch_calls]
        self.assertEqual(sizes, [15, 5])

    def test_ws_disconnect_after_send_is_ambiguous_no_retry(self) -> None:
        fake = _FakeWs()
        fake.recv_error = ConnectionError("dropped after send")
        state = _patch_lighter_for_test(self, send_tx_outcomes=[(200, "", None)] * 10)
        self._use_ws(fake)
        resp = lighter.execute(_ladder_request(order_count=10))
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "LADDER_BATCH_AMBIGUOUS")
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 0)
        self.assertEqual(len(fake.sent), 1)

    def test_ws_429_halts_without_retry(self) -> None:
        fake = _FakeWs(responses=[{
            "type": "jsonapi/sendtxbatch",
            "data": {"code": 23000, "message": "Too Many Requests: 40 requests per 60 second is allowed"},
        }])
        _patch_lighter_for_test(self, send_tx_outcomes=[(200, "", None)] * 10)
        self._use_ws(fake)
        resp = lighter.execute(_ladder_request(order_count=10))
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "RATE_LIMITED")
        self.assertEqual(len(fake.sent), 1)

    def test_message_over_64kb_is_rejected_without_send(self) -> None:
        fake = _FakeWs(responses=[{"data": {"code": 200, "tx_hash": []}}])
        _patch_lighter_for_test(self, send_tx_outcomes=[(200, "", None)])
        self._use_ws(fake)
        with mock.patch.object(lighter, "LIGHTER_WS_MAX_MESSAGE_BYTES", 200):
            resp = lighter.execute(_ladder_request(order_count=10))
        self.assertFalse(resp.success)
        self.assertEqual(len(fake.sent), 0)

    def test_canonical_child_counts_on_halt(self) -> None:
        self._force_rest(self)
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None), (23000, "Too Many Requests", None)],
            auto_reconcile_with_allocator=True,
        )
        resp = lighter.execute(_ladder_request(order_count=40))
        self.assertFalse(resp.success)
        ladder = resp.ladder
        self.assertIsNotNone(ladder.accepted_child_count)
        self.assertIsNotNone(ladder.rejected_child_count)
        self.assertIsNotNone(ladder.uncertain_child_count)
        self.assertIsNotNone(ladder.not_attempted_child_count)
        self.assertEqual(
            (ladder.accepted_child_count or 0)
            + (ladder.rejected_child_count or 0)
            + (ladder.uncertain_child_count or 0)
            + (ladder.not_attempted_child_count or 0),
            40,
        )
        self.assertGreater(ladder.not_attempted_child_count, 0)
        self.assertLessEqual(len(state["signer"].send_tx_batch_calls), 2)

    def test_l2_budget_still_caps_batch_at_30(self) -> None:
        """WS protocol max is 50, but the retained 30/60s limiter must cap a batch."""
        self.assertEqual(lighter.LIGHTER_L2_TX_SAFE_LIMIT, 30)
        self.assertEqual(
            lighter._lighter_ladder_chunk_size("ws"),
            min(lighter.LIGHTER_WS_SEND_TX_BATCH_SIZE, lighter.LIGHTER_L2_TX_SAFE_LIMIT),
        )

    def test_open_order_cap_standard_lighter_rejects_50(self) -> None:
        caps = lighter._lighter_open_order_caps(chain="ARBITRUM", user_tier="standard")
        self.assertEqual(caps["per_market"], 30)
        self.assertFalse(lighter._lighter_capacity_allows(open_on_market=0, adding=50, caps=caps))

    def test_open_order_cap_premium_allows_250(self) -> None:
        caps = lighter._lighter_open_order_caps(chain="ARBITRUM", user_tier="premium")
        self.assertEqual(caps["per_market"], 1000)
        self.assertTrue(lighter._lighter_capacity_allows(open_on_market=155, adding=250, caps=caps))

    def test_timing_fields_present_and_redacted(self) -> None:
        self._force_rest(self)
        _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)],
            auto_reconcile_with_allocator=True,
        )
        with self.assertLogs(lighter.logger, level="WARNING") as captured:
            resp = lighter.execute(_ladder_request(order_count=5))
        self.assertTrue(resp.success, resp.error)
        joined = "\n".join(captured.output)
        self.assertIn("[LIGHTER-LADDER]", joined)
        self.assertNotIn("cd" * 8, joined)
        self.assertNotIn("PRIVATE", joined.upper())
        batch0 = (resp.ladder.batches or [])[0]
        self.assertIn("timing_ms", batch0)


class LighterWizardInflightTests(unittest.TestCase):
    def test_duplicate_confirm_blocked_while_submitting(self) -> None:
        from plugins.trade.wizard import TradeWizard

        desk = mock.Mock()
        desk.execute.side_effect = RuntimeError("should not run twice")
        w = TradeWizard(tradedesk=desk)
        state = w._state_for(("c",))
        state.exchange = "lighter"
        state.account = "amiroo"
        state.state = "ladder_submitting"
        state.ladder = {
            "symbol": "BTC", "side": "buy", "distribution": "uniform",
            "order_count": 10, "total_volume": "1", "start_price": "2", "end_price": "1",
        }
        screen = w.handle_callback(("c",), "confirm")
        self.assertEqual(screen.state, "ladder_submitting")
        self.assertIn("already submitting", screen.text.lower())
        desk.execute.assert_not_called()
        self.assertEqual(state.state, "ladder_submitting")
        self.assertEqual(desk.execute.call_count, 0)


if __name__ == "__main__":
    unittest.main()
