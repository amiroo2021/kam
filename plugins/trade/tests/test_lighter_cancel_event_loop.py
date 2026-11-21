"""Offline regression tests for the Phase 4.1 cancellation event-loop fix.

The live Phase 4 failure: ``_execute_cancel_order_group`` built the
SignerClient in the CALLER's (synchronous) context, so a caller with no
running event loop raised

    Lighter SignerClient initialization failed: no running event loop

and — worse — the failure was classified as ``ambiguous``, which tells
the operator the cancel MAY have landed when in fact nothing was sent.

These tests use a REALISTIC SDK-compatible fake:

  * ``_FakeSignerClient`` creates an ``aiohttp.ClientSession`` at
    construction time, exactly like the real SDK, so it raises if
    constructed outside a running loop.
  * It records the loop it was built on and refuses to be used from a
    different loop, so a cross-loop client cannot silently work.
  * It tracks a per-key nonce counter so consecutive allocation and
    rollback are observable.

No network calls. No live orders. Purely offline.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.trade.agents import x_lighter_agent as lighter  # noqa: E402


CREDS = {
    "account": "bitget",
    "chain": "ROBINHOOD",
    "account_index": 3499,
    "api_key_index": 1,
    "base_url": "https://fake.example",
}


class _FakeNonceManager:
    """Per-key nonce counter, mirroring the SDK's contract.

    Nonces are strictly ``previous + 1`` per API key, and a rollback
    decrements the counter so the next allocation reuses the nonce.
    """

    def __init__(self, start: int = 1000) -> None:
        self.nonce: dict = {}
        self._start = start
        self._locks: dict = {}

    def lock(self, key: int) -> "asyncio.Lock":
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def async_next_nonce(self, key: int):
        if key not in self.nonce:
            self.nonce[key] = self._start
        self.nonce[key] += 1
        return key, self.nonce[key]

    def acknowledge_failure(self, key: int) -> None:
        if key in self.nonce:
            self.nonce[key] -= 1


class _FakeSignerClient:
    """SDK-compatible fake: aiohttp session at construction, loop-bound.

    Mirrors the real ``SignerClient`` in the two ways that matter:

      1. constructing it outside a running event loop raises (the real
         SDK creates an ``aiohttp.ClientSession`` and aiohttp needs a
         live loop); and
      2. using it from a DIFFERENT loop than the one that built it
         raises (the session is bound to its creating loop).
    """

    SKIP_NONCE_OFF = 0

    #: every instance built during the process, for assertions
    instances: list = []

    #: instances that were used to SIGN cancels (the cancel client)
    cancel_clients: list = []

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()
        cls.cancel_clients.clear()

    def __init__(self, base_url=None, account_index=None, keys=None, *, fail=False):
        try:
            asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "error occurred when creating TxClient. err: no running event loop"
            ) from exc
        self._loop = asyncio.get_running_loop()
        self.nonce_manager = _FakeNonceManager()
        self.closed = False
        self.closed_on_loop = None
        self.send_calls: list = []
        self.signed: list = []
        self.fail = fail
        type(self).instances.append(self)

    # -- loop affinity ------------------------------------------------
    def _assert_own_loop(self) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError("Event loop is closed")
        if running is not self._loop:
            raise RuntimeError(
                f"aiohttp session bound to a different loop: {self._loop} vs {running}"
            )

    # -- SDK surface --------------------------------------------------
    def sign_cancel_order(self, market_id, order_id, nonce_off, nonce, api_key_index):
        self._assert_own_loop()
        if self not in type(self).cancel_clients:
            type(self).cancel_clients.append(self)
        self.signed.append({"order_id": order_id, "nonce": nonce})
        return 15, f"txinfo-{market_id}-{order_id}-{nonce}", f"txhash-{nonce}", None

    async def send_tx_batch(self, tx_types, tx_infos):
        self._assert_own_loop()
        self.send_calls.append({"types": list(tx_types), "infos": list(tx_infos)})
        return _FakeRespSendTxBatch()

    async def close(self):
        self._assert_own_loop()
        self.closed = True
        self.closed_on_loop = asyncio.get_running_loop()


class _FakeRespSendTxBatch:
    code = 200
    message = ""
    tx_hash: list = []
    additional_properties: dict = {}


def _reset() -> None:
    lighter._LIGHTER_TIER_CACHE.clear()
    lighter._LIGHTER_TIER_BUDGETS.clear()
    lighter._LIGHTER_NONCE_LEASES.clear()
    lighter._LIGHTER_LIMITERS.clear()
    lighter._LIGHTER_AUTH_TOKEN_CACHE.clear()
    _FakeSignerClient.reset()


class CancelEventLoopTests(unittest.TestCase):
    """The cancel path must build and use its client on ONE loop."""

    def setUp(self) -> None:
        _reset()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._saved_ledger = lighter.LIGHTER_QUOTA_LEDGER_DIR
        lighter.LIGHTER_QUOTA_LEDGER_DIR = Path(self._tmp.name)
        self.addCleanup(self._restore)
        self._saved_pause = lighter.LIGHTER_CANCEL_TX_BATCH_PAUSE_SECONDS
        lighter.LIGHTER_CANCEL_TX_BATCH_PAUSE_SECONDS = 0.0
        self.addCleanup(self._restore_pause)
        # The production cancel budget is conservative (40/60s for an
        # unknown tier). Stub it so the offline tests never sleep on a
        # rolling window.
        self._budget_patch = mock.patch.object(
            lighter, "_get_lighter_tier_budget",
            return_value=lighter._LighterTierBudget(
                safe_limit=1000000, window_seconds=60.0
            ),
        )
        self._budget_patch.start()
        self.addCleanup(self._budget_patch.stop)

    def _restore(self) -> None:
        lighter.LIGHTER_QUOTA_LEDGER_DIR = self._saved_ledger

    def _restore_pause(self) -> None:
        lighter.LIGHTER_CANCEL_TX_BATCH_PAUSE_SECONDS = self._saved_pause

    # ------------------------------------------------------------------
    # 1. Construction and use on the same loop.
    # ------------------------------------------------------------------
    def test_group_builds_client_on_the_loop_that_uses_it(self) -> None:
        """One client, built and used on the SAME loop.

        The fake raises on cross-loop use, so this fails loudly if the
        group hands the client to a different loop.
        """
        with mock.patch.object(
            lighter, "_build_signer_client", side_effect=lambda c: _FakeSignerClient()
        ):
            group = lighter._run_cancel_tx_group(
                credentials=CREDS,
                market_id=3,
                target_ids=[101, 102, 103],
            )

        self.assertEqual(len(group["batches"]), 1)
        self.assertEqual(group["stop_reason"], None)
        self.assertEqual(len(_FakeSignerClient.cancel_clients), 1)
        client = _FakeSignerClient.cancel_clients[0]
        self.assertTrue(client.closed, "client must be closed")
        self.assertIs(
            client.closed_on_loop, client._loop,
            "client must be closed on the loop that built it",
        )

    def test_client_closed_on_its_owning_loop(self) -> None:
        """Closing happens on the owning loop, and always happens."""
        with mock.patch.object(
            lighter, "_build_signer_client", side_effect=lambda c: _FakeSignerClient()
        ):
            lighter._run_cancel_tx_group(
                credentials=CREDS, market_id=3, target_ids=[201],
            )
        self.assertEqual(len(_FakeSignerClient.cancel_clients), 1)
        self.assertTrue(_FakeSignerClient.cancel_clients[0].closed)

    def test_client_closed_even_when_a_batch_fails(self) -> None:
        """A failing batch must still close the client.

        The batch failure surfaces as a stop_reason (the group records
        it and stops); the client must be closed either way.
        """
        class _FailingSend(_FakeSignerClient):
            async def send_tx_batch(self, tx_types, tx_infos):
                self._assert_own_loop()
                raise ConnectionError("reset after send")

        def _build_failing(creds):
            return _FailingSend()

        with mock.patch.object(
            lighter, "_build_signer_client", side_effect=_build_failing
        ):
            group = lighter._run_cancel_tx_group(
                credentials=CREDS, market_id=3, target_ids=[301],
            )

        self.assertIsNotNone(group["stop_reason"])
        self.assertEqual(
            len(_FakeSignerClient.cancel_clients), 1,
            "the cancel client must still have been created",
        )
        self.assertTrue(
            _FakeSignerClient.cancel_clients[0].closed,
            "the client must be closed even when the batch failed",
        )

    # ------------------------------------------------------------------
    # 2. Multiple REST batches sharing ONE signer.
    # ------------------------------------------------------------------
    def test_multiple_batches_share_one_signer(self) -> None:
        """50 targets at chunk size 15 -> 4 batches, ONE client."""
        saved = lighter.LIGHTER_CANCEL_TX_BATCH_SIZE
        lighter.LIGHTER_CANCEL_TX_BATCH_SIZE = 15
        self.addCleanup(lambda: setattr(lighter, "LIGHTER_CANCEL_TX_BATCH_SIZE", saved))

        with mock.patch.object(
            lighter, "_build_signer_client", side_effect=lambda c: _FakeSignerClient()
        ):
            group = lighter._run_cancel_tx_group(
                credentials=CREDS,
                market_id=3,
                target_ids=list(range(1000, 1050)),
            )

        self.assertEqual(len(group["batches"]), 4, "50 targets / 15 = 4 batches")
        self.assertEqual(
            [len(b["order_ids"]) for b in group["batches"]], [15, 15, 15, 5]
        )
        self.assertEqual(
            len(_FakeSignerClient.cancel_clients), 1,
            "all batches must share ONE client",
        )

    # ------------------------------------------------------------------
    # 3. Consecutive nonce allocation across batches.
    # ------------------------------------------------------------------
    def test_nonces_are_consecutive_across_batches(self) -> None:
        """One client -> the SDK counter continues across batches."""
        saved = lighter.LIGHTER_CANCEL_TX_BATCH_SIZE
        lighter.LIGHTER_CANCEL_TX_BATCH_SIZE = 15
        self.addCleanup(lambda: setattr(lighter, "LIGHTER_CANCEL_TX_BATCH_SIZE", saved))

        with mock.patch.object(
            lighter, "_build_signer_client", side_effect=lambda c: _FakeSignerClient()
        ):
            lighter._run_cancel_tx_group(
                credentials=CREDS,
                market_id=3,
                target_ids=list(range(2000, 2050)),
            )

        client = _FakeSignerClient.cancel_clients[0]
        nonces = [s["nonce"] for s in client.signed]
        self.assertEqual(len(nonces), 50)
        self.assertEqual(
            nonces, list(range(nonces[0], nonces[0] + 50)),
            "nonces must be strictly consecutive across batch boundaries",
        )

    def test_nonce_rollback_is_exact_on_sign_failure(self) -> None:
        """A sign failure rolls the counter back with no hole.

        The SDK counter must return to its pre-reservation value so the
        next allocation reuses the nonce rather than leaving a gap.
        """
        class _FailingSigner(_FakeSignerClient):
            def sign_cancel_order(self, *args, **kwargs):
                raise RuntimeError("sign failed")

        counter: dict = {}

        async def _run():
            # Build the client INSIDE the loop that uses it, exactly as
            # the production group runner does.
            client = _FailingSigner()
            counter["client"] = client
            return await lighter._submit_cancel_tx_batch_async(
                credentials=CREDS,
                market_id=3,
                order_ids=[1, 2, 3],
                signer=client,
            )

        # The fake's counter starts at 1000; three reservations take it
        # to 1003, and the rollback must return it to 1000.
        result = asyncio.run(_run())
        self.assertEqual(result["outcome"], lighter._LADDER_BATCH_OUTCOME_EXCHANGE_REJECTED)
        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(
            counter["client"].nonce_manager.nonce.get(1), 1000,
            "the nonce counter must be rolled back exactly (no hole)",
        )

    # ------------------------------------------------------------------
    # 4. Synchronous caller with NO running event loop.
    # ------------------------------------------------------------------
    def test_group_runs_from_a_thread_with_no_running_loop(self) -> None:
        """A plain script thread (no loop) must work.

        This is the exact Phase 4 failure mode: the caller has no loop,
        so the client must be built INSIDE the bridged coroutine.
        """
        result: dict = {}

        def _worker():
            with mock.patch.object(
                lighter, "_build_signer_client", side_effect=lambda c: _FakeSignerClient()
            ):
                result["group"] = lighter._run_cancel_tx_group(
                    credentials=CREDS, market_id=3, target_ids=[401, 402],
                )

        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result["group"]["batches"]), 1)
        self.assertEqual(result["group"]["stop_reason"], None)

    def test_group_works_from_inside_a_running_loop(self) -> None:
        """The gateway path: the caller thread already has a loop."""
        async def _caller():
            with mock.patch.object(
                lighter, "_build_signer_client", side_effect=lambda c: _FakeSignerClient()
            ):
                return lighter._run_cancel_tx_group(
                    credentials=CREDS, market_id=3, target_ids=[501],
                )

        group = asyncio.run(_caller())
        self.assertEqual(len(group["batches"]), 1)
        self.assertEqual(group["stop_reason"], None)

    def test_no_running_loop_is_not_required_at_the_call_site(self) -> None:
        """The group must not demand a loop from its caller."""
        # If the group called asyncio.run() itself on a thread that
        # already had a loop, this would raise "asyncio.run() cannot be
        # called from a running event loop".
        async def _caller():
            with mock.patch.object(
                lighter, "_build_signer_client", side_effect=lambda c: _FakeSignerClient()
            ):
                return lighter._run_cancel_tx_group(
                    credentials=CREDS, market_id=3, target_ids=[601],
                )

        group = asyncio.run(_caller())
        self.assertEqual(len(group["batches"]), 1)

    # ------------------------------------------------------------------
    # 5. Error classification.
    # ------------------------------------------------------------------
    def test_initialisation_failure_is_local_not_ambiguous(self) -> None:
        """A client that cannot be built is a DEFINITE local failure.

        Nothing was signed and nothing was sent, so the outcome must not
        be ``ambiguous`` — that verdict wrongly tells the operator the
        cancel may have landed.
        """
        def _fail_build(creds):
            raise RuntimeError("no running event loop")

        with mock.patch.object(lighter, "_build_signer_client", side_effect=_fail_build):
            group = lighter._run_cancel_tx_group(
                credentials=CREDS, market_id=3, target_ids=[701, 702],
            )

        self.assertEqual(group["batches"], [], "no batch may be attempted")
        reason = group["stop_reason"]
        self.assertIsNotNone(reason)
        self.assertEqual(
            reason["outcome"], lighter._LADDER_BATCH_OUTCOME_LOCAL_FAILURE
        )
        self.assertNotEqual(reason["outcome"], lighter._LADDER_BATCH_OUTCOME_AMBIGUOUS)
        self.assertEqual(reason["batches_attempted"], 0)

    def test_transport_failure_after_possible_transmission_is_ambiguous(self) -> None:
        """A failure AFTER the write may have reached the exchange.

        That stays ``ambiguous`` — the operator must reconcile, and must
        NOT be told the cancel definitely did not happen.
        """
        # Drive the real batch body: the fake client's send raises after
        # the request is considered in flight.
        class _ResetAfterSend(_FakeSignerClient):
            async def send_tx_batch(self, tx_types, tx_infos):
                self._assert_own_loop()
                raise ConnectionError("connection reset after send")

        async def _run():
            client = _ResetAfterSend()
            return await lighter._submit_cancel_tx_batch_async(
                credentials=CREDS,
                market_id=3,
                order_ids=[801],
                signer=client,
            )

        result = asyncio.run(_run())
        self.assertEqual(result["outcome"], lighter._LADDER_BATCH_OUTCOME_AMBIGUOUS)
        self.assertNotEqual(result["outcome"], lighter._LADDER_BATCH_OUTCOME_LOCAL_FAILURE)
        self.assertIn("ambiguity_reason", result)

    def test_batches_attempted_is_reported(self) -> None:
        """The operator must be able to see how many batches were tried."""
        saved = lighter.LIGHTER_CANCEL_TX_BATCH_SIZE
        lighter.LIGHTER_CANCEL_TX_BATCH_SIZE = 15
        self.addCleanup(lambda: setattr(lighter, "LIGHTER_CANCEL_TX_BATCH_SIZE", saved))

        seen: list = []

        async def _capture(**kwargs):
            seen.append(kwargs)
            return {
                "outcome": lighter._LADDER_BATCH_OUTCOME_EXCHANGE_REJECTED,
                "submitted_count": len(kwargs["order_ids"]),
                "accepted_count": 0,
                "rejected_count": len(kwargs["order_ids"]),
                "unknown_count": 0,
                "api_code": 400,
                "api_message": "rejected",
            }

        with mock.patch.object(
            lighter, "_build_signer_client", side_effect=lambda c: _FakeSignerClient()
        ):
            with mock.patch.object(
                lighter, "_submit_cancel_tx_batch_async", side_effect=_capture
            ):
                group = lighter._run_cancel_tx_group(
                    credentials=CREDS, market_id=3, target_ids=list(range(900, 930)),
                )

        self.assertEqual(len(group["batches"]), 1, "stop after the first failure")
        self.assertEqual(len(seen), 1, "exactly one batch attempted")
        self.assertEqual(
            group["stop_reason"]["outcome"],
            lighter._LADDER_BATCH_OUTCOME_EXCHANGE_REJECTED,
        )

    # ------------------------------------------------------------------
    # 6. The cancel-group entry point classifies a local failure.
    # ------------------------------------------------------------------
    def test_execute_cancel_group_reports_local_failure(self) -> None:
        """``execute()`` must surface CANCEL_LOCAL_FAILURE, not AMBIGUOUS."""
        orders = [
            {
                "order_id": str(1000 + i),
                "order_index": 1000 + i,
                "market_index": 3,
                "market_symbol": "SOL",
                "is_ask": False,
                "status": "open",
                "price": "55.0",
                "filled_base_amount": "0",
                "remaining_base_amount": "0.25",
                "client_order_index": 500000 + i,
            }
            for i in range(3)
        ]

        def _fail_build(creds):
            raise RuntimeError("no running event loop")

        with mock.patch.object(lighter, "_lookup_credentials", return_value=dict(CREDS)):
            with mock.patch.object(
                lighter, "_resolve_market",
                return_value={"market_id": 3, "market_symbol": "SOL",
                              "last_trade_price": "68.0"},
            ):
                with mock.patch.object(
                    lighter, "_mint_auth_token_cached", return_value="tok"
                ):
                    with mock.patch.object(
                        lighter, "_fetch_active_orders", return_value=orders
                    ):
                        with mock.patch.object(
                            lighter, "_build_signer_client", side_effect=_fail_build
                        ):
                            resp = lighter.execute({
                                "operation": "cancel_order_group",
                                "exchange": lighter.name,
                                "account": "bitget",
                                "symbol": "SOL",
                                "side": "buy",
                                "dry_run": False,
                                "order_ids": [1000, 1001, 1002],
                            })

        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "CANCEL_LOCAL_FAILURE")
        self.assertNotEqual(resp.error.code, "CANCEL_AMBIGUOUS")
        cg = resp.cancel_group
        self.assertEqual(cg.cancelled_order_count, 0)
        self.assertEqual(cg.batch_count, 0)
        self.assertEqual(cg.remaining_target_count, 3, "all three still open")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
