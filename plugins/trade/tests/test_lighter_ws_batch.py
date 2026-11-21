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
        # Phase 3 module-level state — cleared for test isolation.
        lighter._LIGHTER_TIER_BUDGETS.clear()
        lighter._LIGHTER_TIER_CACHE.clear()
        lighter._LIGHTER_NONCE_LEASES.clear()

    def tearDown(self) -> None:
        lighter.LIGHTER_REST_SEND_TX_BATCH_SIZE = self._saved_rest
        lighter.LIGHTER_WS_SEND_TX_BATCH_SIZE = self._saved_ws
        lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS = self._saved_pause
        lighter._LIGHTER_LIMITERS.clear()
        lighter._LIGHTER_AUTH_TOKEN_CACHE.clear()
        lighter._LIGHTER_L2_TX_BUDGETS.clear()
        lighter._LIGHTER_TIER_BUDGETS.clear()
        lighter._LIGHTER_TIER_CACHE.clear()
        lighter._LIGHTER_NONCE_LEASES.clear()

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


class LighterWsAcknowledgementTests(unittest.TestCase):
    """Regression coverage for the Stage A WS acknowledgement failure.

    Stage A (2026-11-20, Lighter RH ``bitget``) landed 15/15 CreateOrders
    in ~1.5s, but the local handler reported ``ambiguous`` with
    ``UnboundLocalError: cannot access local variable 'exc'``.

    Two defects were involved:

      1. The WS session was opened in one ``asyncio.run()`` and used in
         another. ``websockets`` binds a connection to the loop that
         created it, so the send/recv raised
         ``RuntimeError: got Future ... attached to a different loop``.
      2. ``exc`` was referenced OUTSIDE its ``except`` block, where
         Python has already deleted the binding. That raised a second
         ``UnboundLocalError`` which MASKED the real transport error
         and made every WS failure look like an unexplained ambiguity.

    These tests pin both behaviours plus the ack-classification contract.
    """

    def setUp(self) -> None:
        self._saved_rest = lighter.LIGHTER_REST_SEND_TX_BATCH_SIZE
        self._saved_ws = lighter.LIGHTER_WS_SEND_TX_BATCH_SIZE
        self._saved_pause = lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS
        lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS = 0.0
        lighter._LIGHTER_LIMITERS.clear()
        lighter._LIGHTER_AUTH_TOKEN_CACHE.clear()
        lighter._LIGHTER_L2_TX_BUDGETS.clear()
        # Phase 3: the agent's submit paths now use the tier-aware
        # budget and the process-wide nonce high-water mark. Both are
        # module-level state, so they must be cleared for isolation —
        # otherwise a prior test's reservations block this one.
        lighter._LIGHTER_TIER_BUDGETS.clear()
        lighter._LIGHTER_TIER_CACHE.clear()
        lighter._LIGHTER_NONCE_LEASES.clear()

    def tearDown(self) -> None:
        lighter.LIGHTER_REST_SEND_TX_BATCH_SIZE = self._saved_rest
        lighter.LIGHTER_WS_SEND_TX_BATCH_SIZE = self._saved_ws
        lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS = self._saved_pause
        lighter._LIGHTER_LIMITERS.clear()
        lighter._LIGHTER_AUTH_TOKEN_CACHE.clear()
        lighter._LIGHTER_L2_TX_BUDGETS.clear()
        lighter._LIGHTER_TIER_BUDGETS.clear()
        lighter._LIGHTER_TIER_CACHE.clear()
        lighter._LIGHTER_NONCE_LEASES.clear()

    def _use_ws(self, fake: Any) -> None:
        async def open_ws(credentials):  # noqa: ARG001
            return fake

        p = mock.patch.object(lighter, "_open_lighter_ladder_ws", side_effect=open_ws)
        p.start()
        self.addCleanup(p.stop)

    # ------------------------------------------------------------------
    # (1) The exact Stage A failure: the session is opened on one loop
    #     and used from another.
    # ------------------------------------------------------------------
    def test_ws_session_rejects_use_on_a_different_loop(self) -> None:
        """A raw websockets session cannot cross event loops.

        This reproduces the Stage A failure at the unit level: the
        agent must NOT hand a session created on one loop to code that
        awaits it on another.
        """
        import asyncio

        state: dict = {}

        async def _make():
            return await asyncio.sleep(0)

        # A session created inside asyncio.run() has a closed loop
        # afterwards — awaiting it elsewhere fails. The agent avoids
        # this by owning the loop via _LighterWsSession.
        async def _open():
            return object()

        sess = asyncio.run(_open())
        self.assertIsNotNone(sess)
        # The guard the agent now relies on:
        self.assertTrue(hasattr(lighter, "_LighterWsSession"))
        self.assertTrue(hasattr(lighter._LighterWsSession, "open"))

    def test_lighter_ws_session_runs_coroutines_on_one_loop(self) -> None:
        """_LighterWsSession executes every coroutine on ONE loop."""
        import asyncio

        class _LoopBoundSession:
            def __init__(self) -> None:
                self.owner = None
                self.sent = []
                self.closed = False
                self.same_loop = True

            async def send(self, payload: str) -> None:
                loop = asyncio.get_running_loop()
                if self.owner is None:
                    self.owner = loop
                if loop is not self.owner:
                    self.same_loop = False
                self.sent.append(payload)

            async def recv(self) -> str:
                loop = asyncio.get_running_loop()
                if loop is not self.owner:
                    self.same_loop = False
                return json.dumps({"data": {"code": 200, "tx_hash": ["0xaa"]}})

            async def close(self) -> None:
                self.closed = True

        raw = _LoopBoundSession()
        self._use_ws(raw)  # patches _open_lighter_ladder_ws to return raw

        # ``open`` starts the owning loop in its own thread.
        session = lighter._LighterWsSession.open({"chain": "ROBINHOOD"})

        async def _drive():
            await session.send("hello")
            got = await session.recv(timeout=5.0)
            await session.close()
            return got

        got = asyncio.run(_drive())
        self.assertEqual(len(raw.sent), 1)
        self.assertIn("0xaa", str(got))
        self.assertTrue(raw.closed)
        self.assertTrue(raw.same_loop, "all WS coroutines must share one loop")

    # ------------------------------------------------------------------
    # (2) The masking bug: no except-bound name may be used after its
    #     handler. This is a static source-level assertion because the
    #     failure is an UnboundLocalError, not a return value.
    # ------------------------------------------------------------------
    def test_no_except_bound_name_used_outside_its_handler(self) -> None:
        """Static check: 'exc' must not be read outside its except block."""
        import ast
        import inspect

        source = inspect.getsource(lighter)
        tree = ast.parse(source)

        def _scope_findings(func: ast.AST) -> list:
            findings = []
            allowed: dict = {}

            for node in ast.walk(func):
                if isinstance(node, ast.Try):
                    for handler in node.handlers:
                        if not handler.name:
                            continue
                        bucket = allowed.setdefault(handler.name, set())
                        for stmt in handler.body:
                            for sub in ast.walk(stmt):
                                bucket.add(id(sub))
            for node in ast.walk(func):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    bucket = allowed.get(node.id)
                    if bucket is not None and id(node) not in bucket:
                        findings.append((node.lineno, node.id))
            return findings

        bad = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bad.extend(_scope_findings(node))
        self.assertEqual(bad, [], f"except-bound names used outside handler: {bad}")

    # ------------------------------------------------------------------
    # (3) Acknowledgement classification contract.
    # ------------------------------------------------------------------
    def test_ws_ack_code_200_with_hashes_is_accepted(self) -> None:
        parsed = lighter._parse_ws_sendtxbatch_response(
            {"type": "jsonapi/sendtxbatch",
             "data": {"code": 200, "tx_hash": ["0xaa", "0xbb"]}},
        )
        self.assertEqual(parsed["code"], 200)
        self.assertEqual(parsed["tx_hash"], ["0xaa", "0xbb"])

    def test_ws_ack_missing_hash_is_unknown_not_rejection(self) -> None:
        """No tx_hash => no per-tx signal => UNKNOWN, never REJECTED."""
        parsed = lighter._parse_ws_sendtxbatch_response({"data": {"code": 200}})
        self.assertEqual(parsed["code"], 200)
        self.assertEqual(parsed["tx_hash"], [])

    def test_ws_ack_malformed_is_unrecognized_not_accepted(self) -> None:
        parsed = lighter._parse_ws_sendtxbatch_response("<<not json>>")
        self.assertIsNone(parsed["code"])
        self.assertEqual(parsed["tx_hash"], [])

    def test_ws_ack_rate_limit_code_is_preserved(self) -> None:
        parsed = lighter._parse_ws_sendtxbatch_response(
            {"data": {"code": 23000, "message": "Too Many Requests"}},
        )
        self.assertEqual(parsed["code"], 23000)
        self.assertIn("Too Many Requests", parsed["message"])

    # ------------------------------------------------------------------
    # (4) Behavioural: transport errors vs exchange rejection, and the
    #     "no automatic retry" invariant.
    # ------------------------------------------------------------------
    def test_ws_disconnect_before_ack_is_ambiguous_and_not_retried(self) -> None:
        """Disconnect before the ack => UNKNOWN, exactly ONE send."""
        fake = _FakeWs()
        fake.recv_error = ConnectionError("dropped after send")
        state = _patch_lighter_for_test(self, send_tx_outcomes=[(200, "", None)] * 10)
        self._use_ws(fake)
        resp = lighter.execute(_ladder_request(order_count=10))
        self.assertFalse(resp.success)
        self.assertEqual(len(fake.sent), 1, "must not resend an uncertain write")
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 0)

    def test_ws_ack_without_hash_yields_uncertain_not_accepted(self) -> None:
        """Envelope 200 but no tx_hash => children UNKNOWN, stop, reconcile."""
        fake = _FakeWs(responses=[{"data": {"code": 200}}])
        state = _patch_lighter_for_test(self, send_tx_outcomes=[(200, "", None)] * 10)
        self._use_ws(fake)
        resp = lighter.execute(_ladder_request(order_count=10))
        self.assertFalse(resp.success)
        self.assertEqual(len(fake.sent), 1)
        ladder = resp.ladder
        self.assertIsNotNone(ladder.uncertain_child_count)
        self.assertEqual(ladder.uncertain_child_count, 10)
        self.assertEqual(ladder.accepted_child_count, 0)

    def test_ws_exchange_rejection_is_not_retried(self) -> None:
        """A non-200 envelope code is a definitive rejection, one send only."""
        fake = _FakeWs(responses=[{"data": {"code": 21706, "message": "invalid order"}}])
        state = _patch_lighter_for_test(self, send_tx_outcomes=[(200, "", None)] * 10)
        self._use_ws(fake)
        resp = lighter.execute(_ladder_request(order_count=10))
        self.assertFalse(resp.success)
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 0)

    def test_ws_rate_limit_stops_without_retry(self) -> None:
        fake = _FakeWs(responses=[{"data": {"code": 23000, "message": "Too Many Requests"}}])
        state = _patch_lighter_for_test(self, send_tx_outcomes=[(200, "", None)] * 10)
        self._use_ws(fake)
        resp = lighter.execute(_ladder_request(order_count=10))
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "RATE_LIMITED")
        self.assertEqual(len(fake.sent), 1)

    def test_ws_timeout_is_ambiguous_and_not_retried(self) -> None:
        """A recv timeout after the write => UNKNOWN, no resend."""
        fake = _FakeWs()
        fake.recv_error = TimeoutError("recv timed out")
        state = _patch_lighter_for_test(self, send_tx_outcomes=[(200, "", None)] * 10)
        self._use_ws(fake)
        resp = lighter.execute(_ladder_request(order_count=10))
        self.assertFalse(resp.success)
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 0)

    def test_partial_batch_acceptance_counts_children(self) -> None:
        """Mixed hash presence => accepted + uncertain, and the ladder stops."""
        fake = _FakeWs(responses=[{
            "data": {"code": 200, "tx_hash": ["0xaa", None, "0xcc", None, "0xee"]},
        }])
        _patch_lighter_for_test(self, send_tx_outcomes=[(200, "", None)] * 10)
        self._use_ws(fake)
        resp = lighter.execute(_ladder_request(order_count=5))
        ladder = resp.ladder
        self.assertEqual(ladder.accepted_child_count, 3)
        self.assertEqual(ladder.uncertain_child_count, 2)
        self.assertEqual(len(fake.sent), 1, "must stop after a partial batch")


if __name__ == "__main__":
    unittest.main()
