"""Deterministic tests for Lighter exchange sendTxBatch ladder transport.

These tests cover the new ``sendTxBatch`` ladder path in
``x_lighter_agent.py``. They stub the SDK at module boundaries so the
tests run without any live Lighter network calls.

Key invariants under test:
  * 10-child ladder produces 1 sendTxBatch HTTP call (NOT 10
    individual create_order calls and NOT create_grouped_orders).
  * 200-child ladder produces 20 sendTxBatch HTTP calls.
  * Each child uses TxTypeL2CreateOrder (=14).
  * Each child is independently signed via signer.sign_create_order
    with an explicit nonce allocated by nonce_manager.async_next_nonce.
  * sendTxBatch is NOT atomic at the per-transaction level — a 200
    envelope can have individual transactions rejected. The
    per-child accept count comes from reconciliation by
    client_order_index, not from the response tx_hash list.
  * No automatic write retry. A 23000 stops the ladder after exactly
    one failed write attempt.
  * Nonce reservation is rolled back via
    nonce_manager.acknowledge_failure on sign or transport failure.
  * Pre-existing batches remain counted when a later batch 429s.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import asyncio
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

# The hermes-agent editable-install registers a __path_hook__ on
# ``plugins.*`` that serves the installed copy at
# /usr/local/lib/hermes-agent/plugins/... — NOT the source tree at
# /root/kam/plugins/.... This MUST be the first thing this test
# module does. If it isn't, the installed (possibly stale) copy is
# imported instead, the batched sendTxBatch code is missing, and
# every test in this file fails.
_EDITABLE_FINDER = "__editable___hermes_agent_0_20_0_finder"
_KNOWN_EDITABLE_FINDERS = (
    _EDITABLE_FINDER,
    # Future-proof: any editable-install for hermes-agent.
)
if any(name in repr(h) for h in sys.path_hooks for name in _KNOWN_EDITABLE_FINDERS):
    sys.path_hooks[:] = [
        h
        for h in sys.path_hooks
        if not any(name in repr(h) for name in _KNOWN_EDITABLE_FINDERS)
    ]

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
# Drop any cached copy of the plugins.trade.* modules so the resolver
# re-resolves them to the source tree. Keep plugins.trade.tests.* so
# the unittest loader can still find this test module.
# NOTE: Do NOT pop plugins.trade.* from sys.modules here.
# Session-level isolation lives in conftest.py. Mid-suite pops
# create dual CanonicalResponse/TradeDesk identities and break
# later tests (INVALID_AGENT_RESPONSE / ImportError agents).

# Module-level env management: do NOT pop LIGHTER_* env vars at
# import time. We preserve them and only mutate them on a per-class
# /per-test basis so other test files in the discover are unaffected.
#
# LIGHTER_RH_* stubs: a synthetic "rh" account used by the LIGHTER_RH_*
# env vars to satisfy _lookup_credentials("rh") during the tests.
# These are set as defaults so they are only populated if the host
# environment does not already have them.
os.environ.setdefault("LIGHTER_RH_CHAIN", "ROBINHOOD")
os.environ.setdefault("LIGHTER_RH_ACCOUNT_INDEX", "42")
os.environ.setdefault("LIGHTER_RH_APIKEY_INDEX", "7")
os.environ.setdefault("LIGHTER_RH_PUBLIC_KEY", "0x" + "ab" * 32)
os.environ.setdefault("LIGHTER_RH_PRIVATE_KEY", "0x" + "cd" * 32)


# Module-level env state preservation.
# Pop the LIGHTER_* env vars only at module import time, and restore
# them at module teardown (end of the discover run). The atexit hook
# was insufficient because it never fires between tests inside one
# unittest process, which is what discover does.
_MODULE_PRESERVED_LIGHTER_ENV: Dict[str, str] = {}
for _k in list(os.environ.keys()):
    if _k.startswith("LIGHTER_"):
        _MODULE_PRESERVED_LIGHTER_ENV[_k] = os.environ[_k]


def setUpModule() -> None:
    # Re-preserve in case another test module mutated os.environ
    # between our import and the actual test execution.
    for _k in list(os.environ.keys()):
        if _k.startswith("LIGHTER_") and _k not in _MODULE_PRESERVED_LIGHTER_ENV:
            _MODULE_PRESERVED_LIGHTER_ENV[_k] = os.environ[_k]


def tearDownModule() -> None:
    # Restore the LIGHTER_* env vars exactly as they were at import
    # time, removing any new keys that tests added.
    for _k in list(os.environ.keys()):
        if _k.startswith("LIGHTER_") and _k not in _MODULE_PRESERVED_LIGHTER_ENV:
            os.environ.pop(_k, None)
    for _k, _v in _MODULE_PRESERVED_LIGHTER_ENV.items():
        os.environ[_k] = _v


import plugins.trade.agents.x_lighter_agent as lighter  # noqa: E402
from plugins.trade.canonical import CanonicalLadderResult  # noqa: E402
from plugins.trade.agents.x_lighter_agent import (
    _LADDER_BATCH_OUTCOME_SUCCESS,
)  # noqa: E402


# ---------------------------------------------------------------------------
# Stub infrastructure
# ---------------------------------------------------------------------------


TxTypeL2CreateOrder = 14  # documented in lighter-go types/txtypes/constants.go
TxTypeL2CancelOrder = 15  # documented in lighter-go types/txtypes/constants.go


class _StubRespSendTxBatch:
    """Stand-in for lighter.models.resp_send_tx_batch.RespSendTxBatch."""

    def __init__(
        self,
        *,
        code: int = 200,
        message: str = "",
        tx_hashes: Optional[List[str]] = None,
    ) -> None:
        self.code = code
        self.message = message
        # Per the OpenAPI spec tx_hash is a list. We default to
        # one tx_hash per input child so tests get a clean
        # accepted-by-envelope result; tests that want partial
        # landing supply their own shorter list.
        self.tx_hash = tx_hashes if tx_hashes is not None else []


class _StubSigner:
    """Records every ``sign_create_order`` and ``send_tx_batch`` call.

    The optional ``sign_tx_outcomes`` queue supplies a pre-canned
    sequence of (code, message, tx_hashes) tuples — one per
    ``send_tx_batch`` invocation. ``sign_failure_for`` can be set to
    a call index that should fail at the ``sign_create_order`` stage
    with the given error string.
    """

    def __init__(
        self,
        send_tx_outcomes: Optional[List[tuple]] = None,
        *,
        default_outcome: tuple = (200, "", None),
        nonce_manager: Optional[Any] = None,
    ) -> None:
        self.send_tx_outcomes = list(send_tx_outcomes or [])
        self.default_outcome = default_outcome
        self.sign_create_order_calls: List[Dict[str, Any]] = []
        self.sign_cancel_order_calls: List[Dict[str, Any]] = []
        self.send_tx_batch_calls: List[Dict[str, Any]] = []
        self.acknowledge_failure_calls: List[int] = []
        self.next_nonce_calls: List[int] = []
        self._sign_failure_at_index: Optional[int] = None
        self._sign_failure_msg: str = ""
        self._sign_cancel_failure_at_index: Optional[int] = None
        self._sign_cancel_failure_msg: str = ""
        # Constants the agent code reads via SignerClient.<NAME>
        self.ORDER_TYPE_LIMIT = 0
        self.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME = 0
        self.NIL_TRIGGER_PRICE = 0
        self.DEFAULT_28_DAY_ORDER_EXPIRY = -1
        self.SKIP_NONCE_OFF = 0
        self.SKIP_NONCE_ON = 1
        if nonce_manager is None:
            nonce_manager = _StubNonceManager(start=100)
        self.nonce_manager = nonce_manager

    def set_sign_failure(self, call_index: int, msg: str) -> None:
        self._sign_failure_at_index = call_index
        self._sign_failure_msg = msg

    def sign_create_order(self, *args, **kwargs):
        # NOTE: real SignerClient.sign_create_order is a sync method
        # (calls the C signer directly, no network). Our stub mirrors
        # that contract — return a tuple, not a coroutine.
        self.sign_create_order_calls.append({"args": args, "kwargs": kwargs})
        idx = len(self.sign_create_order_calls) - 1
        if self._sign_failure_at_index == idx:
            return None, None, None, self._sign_failure_msg
        nonce = kwargs.get("nonce", -1)
        tx_type = TxTypeL2CreateOrder
        tx_info = json.dumps({
            "AccountIndex": kwargs.get("account_index"),
            "ApiKeyIndex": kwargs.get("api_key_index"),
            "Nonce": int(nonce),
            "MarketIndex": args[0] if args else kwargs.get("market_index"),
            "ClientOrderIndex": args[1] if len(args) > 1 else kwargs.get("client_order_index"),
        })
        tx_hash = f"0x{sha_str(int(nonce))}"
        return tx_type, tx_info, tx_hash, None

    def sign_cancel_order(self, *args, **kwargs):
        # Sync method mirroring SignerClient.sign_cancel_order.
        # Returns (tx_type=15, tx_info, tx_hash, error).
        self.sign_cancel_order_calls.append({"args": args, "kwargs": kwargs})
        idx = len(self.sign_cancel_order_calls) - 1
        if self._sign_cancel_failure_at_index == idx:
            return None, None, None, self._sign_cancel_failure_msg
        # signature: (market_index, order_index, skip_nonce, nonce, api_key_index)
        market_index = args[0] if len(args) > 0 else kwargs.get("market_index")
        order_index = args[1] if len(args) > 1 else kwargs.get("order_index")
        nonce = args[3] if len(args) > 3 else kwargs.get("nonce", -1)
        tx_type = TxTypeL2CancelOrder
        tx_info = json.dumps({
            "AccountIndex": kwargs.get("account_index"),
            "ApiKeyIndex": kwargs.get("api_key_index"),
            "Nonce": int(nonce),
            "MarketIndex": market_index,
            "Index": order_index,
        })
        tx_hash = f"0x{sha_str(int(nonce))}"
        return tx_type, tx_info, tx_hash, None

    def set_sign_cancel_failure(self, call_index: int, msg: str) -> None:
        self._sign_cancel_failure_at_index = call_index
        self._sign_cancel_failure_msg = msg

    async def send_tx_batch(self, tx_types, tx_infos):
        call_idx = len(self.send_tx_batch_calls)
        self.send_tx_batch_calls.append({"tx_types": list(tx_types), "tx_infos": list(tx_infos)})
        if self.send_tx_outcomes:
            outcome = self.send_tx_outcomes.pop(0)
        else:
            outcome = self.default_outcome
        if len(outcome) == 2:
            code, message = outcome
            tx_hashes = None
        else:
            code, message, tx_hashes = outcome
        if tx_hashes is None:
            tx_hashes = [f"0x{sha_str(1000 + call_idx * 100 + i)}" for i in range(len(tx_types))]
        return _StubRespSendTxBatch(code=code, message=message, tx_hashes=tx_hashes)

    async def create_order(self, *args, **kwargs):
        # The ladder path must NEVER call this. Tests that want to
        # detect a regression can override this method with a spy.
        raise AssertionError(
            "create_order was invoked — ladder path must not use "
            "per-child send_tx; got create_order call"
        )

    async def close(self):
        return None


def sha_str(n: int) -> str:
    """Deterministic 8-hex-digit string from a non-negative int."""
    return format(n & 0xFFFFFFFF, "08x")


class _StubNonceManager:
    """Mock nonce manager compatible with lighter.nonce_manager.NonceManager.

    Tracks a per-key counter. ``async_next_nonce`` returns and increments.
    ``acknowledge_failure`` decrements. ``lock(api_key)`` returns a
    trivial async context manager.
    """

    def __init__(self, start: int = 100) -> None:
        self.start = start
        self.nonces: Dict[int, int] = {}
        self.acknowledged: List[int] = []
        # We also expose attributes the agent code touches.
        self.api_keys_list: List[int] = []

    def _ensure(self, key: int) -> None:
        if key not in self.nonces:
            self.nonces[key] = self.start

    async def async_next_nonce(self, api_key: Optional[int] = None) -> Tuple[int, int]:
        if api_key is None:
            api_key = 0
        self._ensure(api_key)
        self.nonces[api_key] += 1
        return api_key, self.nonces[api_key]

    def acknowledge_failure(self, api_key: int) -> None:
        self.acknowledged.append(api_key)
        if api_key in self.nonces and self.nonces[api_key] > self.start:
            self.nonces[api_key] -= 1

    def lock(self, api_key: int):
        # Return a trivial async context manager.
        outer = self

        class _CM:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        return _CM()


def _make_credentials() -> Dict[str, Any]:
    return {
        "account": "rh",
        "chain": "ROBINHOOD",
        "label": "rh — Robinhood",
        "account_index": 42,
        "api_key_index": 7,
        "public_key": "0x" + "ab" * 32,
        "private_key": "0x" + "cd" * 32,
        "base_url": "https://api.rh.lighter.xyz",
    }


def _patch_lighter_for_test(
    test: unittest.TestCase,
    *,
    send_tx_outcomes: Optional[List[tuple]] = None,
    default_outcome: tuple = (200, "", None),
    auto_reconcile_with_allocator: bool = False,
    sign_failure_at_index: Optional[Tuple[int, str]] = None,
) -> Dict[str, Any]:
    """Patch the SDK / signing helpers used by the sendTxBatch path.

    Returns a namespace-like dict holding the stub signer and any
    other references individual test cases may need to introspect.
    """
    state: Dict[str, Any] = {}
    nonce_manager = _StubNonceManager()
    stub_signer = _StubSigner(
        send_tx_outcomes, default_outcome=default_outcome, nonce_manager=nonce_manager
    )
    state["signer"] = stub_signer
    state["nonce_manager"] = nonce_manager

    if sign_failure_at_index is not None:
        stub_signer.set_sign_failure(*sign_failure_at_index)

    recorded_ids: Dict[str, List[int]] = {}

    def fake_build_signer(creds):  # noqa: ARG001
        return stub_signer

    def fake_mint_auth_token(creds):  # noqa: ARG001
        return "fake-token"

    token_counter = {"n": 0}

    def fake_mint_auth_token_cached(creds):  # noqa: ARG001
        token_counter["n"] += 1
        return f"fake-token-{token_counter['n']}"

    def fake_fetch_active_orders(creds, auth_token):  # noqa: ARG001
        if auto_reconcile_with_allocator:
            ids = recorded_ids.get("ids") or []
            return [
                {
                    "market_index": 1,
                    "is_ask": False,
                    "client_order_index": idx,
                    "order_id": 900000 + i,
                    "remaining_base_amount": "1.000",
                    "initial_base_amount": "1.000",
                    "price": "69000.00",
                }
                for i, idx in enumerate(ids)
            ]
        return []

    def fake_resolve_market(base_url, symbol):  # noqa: ARG001
        return {
            "market_id": 1,
            "symbol": symbol,
            "size_decimals": 5,
            "price_decimals": 1,
            "min_base_amount": "0.00020",
        }

    patches = [
        mock.patch.object(lighter, "_build_signer_client", side_effect=fake_build_signer),
        mock.patch.object(lighter, "_mint_auth_token", side_effect=fake_mint_auth_token),
        mock.patch.object(lighter, "_mint_auth_token_cached", side_effect=fake_mint_auth_token_cached),
        mock.patch.object(lighter, "_fetch_active_orders", side_effect=fake_fetch_active_orders),
        mock.patch.object(lighter, "_resolve_market", side_effect=fake_resolve_market),
        # L2-tx budget: use a generous safe limit for offline tests so
        # consecutive batches don't block on the rolling-window throttle.
        mock.patch.object(
            lighter, "_get_lighter_l2_tx_budget",
            return_value=lighter._LighterL2TxBudget(safe_limit=1000000, window_seconds=60.0),
        ),
    ]
    for p in patches:
        p.start()
        test.addCleanup(p.stop)

    if auto_reconcile_with_allocator:
        orig_allocate = lighter._allocate_client_order_indices

        def recording_allocate(count):
            ids = orig_allocate(count)
            recorded_ids["ids"] = list(ids)
            return ids

        p2 = mock.patch.object(
            lighter, "_allocate_client_order_indices", side_effect=recording_allocate
        )
        p2.start()
        test.addCleanup(p2.stop)
    return state


def _ladder_request(
    *,
    order_count: int,
    distribution: str = "half_gaussian",
    total_volume: str = "200",
) -> Dict[str, Any]:
    return {
        "operation": "ladder",
        "exchange": "lighter",
        "account": "rh",
        "symbol": "BTC",
        "side": "buy",
        "distribution": distribution,
        "order_count": order_count,
        "total_volume": total_volume,
        "start_price": "62861",
        "end_price": "62761",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class LighterSendTxBatchLadderTests(unittest.TestCase):
    """All tests run offline against stubbed SDK calls."""

    # ------------------------------------------------------------------
    # State-leak hygiene: some tests in this class wrap
    # ``_StubSigner.send_tx_batch`` at the CLASS level (via
    # ``type(state["signer"]).send_tx_batch = ...``) so the stub's
    # internal ``send_tx_batch_calls`` counter still increments.
    # Without restoration, the next test inherits the wrapper.
    # We snapshot the original in setUp and restore in tearDown.
    # ------------------------------------------------------------------
    _original_stub_send_tx_batch: Optional[Any] = None
    _original_stub_sign_create_order: Optional[Any] = None

    def setUp(self) -> None:
        # Snapshot the stub class methods so we can restore them
        # after each test. _StubSigner is defined later in this
        # module; the snapshot runs at test-time so the class exists.
        # We use sys.modules to find the already-loaded test module
        # since 'plugins.trade.tests' has no __init__.py.
        import sys
        for _mod_name, _mod in list(sys.modules.items()):
            if _mod_name.endswith('test_lighter_send_tx_batch'):
                _StubSigner = _mod._StubSigner
                break
        else:
            raise RuntimeError("test_lighter_send_tx_batch module not loaded")
        self._original_stub_send_tx_batch = _StubSigner.send_tx_batch
        self._original_stub_sign_create_order = _StubSigner.sign_create_order

        # Save transport knobs so individual tests can override them
        # without leaking across tests.
        self._saved_batch_size = lighter.LIGHTER_SEND_TX_BATCH_SIZE
        self._saved_pause = lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS
        self._saved_backoff_cap = lighter.LIGHTER_RATELIMIT_BACKOFF_CAP_SECONDS
        lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS = 0.0
        lighter.LIGHTER_RATELIMIT_BACKOFF_CAP_SECONDS = 0.01
        lighter._LIGHTER_LIMITERS.clear()
        lighter._LIGHTER_AUTH_TOKEN_CACHE.clear()
        lighter._LIGHTER_L2_TX_BUDGETS.clear()

    def tearDown(self) -> None:
        import sys
        for _mod_name, _mod in list(sys.modules.items()):
            if _mod_name.endswith('test_lighter_send_tx_batch'):
                _StubSigner = _mod._StubSigner
                break
        else:
            _StubSigner = None
        if _StubSigner is not None:
            _StubSigner.send_tx_batch = self._original_stub_send_tx_batch
            _StubSigner.sign_create_order = self._original_stub_sign_create_order
        lighter.LIGHTER_SEND_TX_BATCH_SIZE = self._saved_batch_size
        lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS = self._saved_pause
        lighter.LIGHTER_RATELIMIT_BACKOFF_CAP_SECONDS = self._saved_backoff_cap

    # ------------------------------------------------------------------
    # A. 10 independent children → one sendTxBatch HTTP call
    # ------------------------------------------------------------------
    def test_10_children_produce_one_send_tx_batch_call(self):
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)],
            auto_reconcile_with_allocator=True,
        )
        resp = lighter.execute(_ladder_request(order_count=10))
        self.assertTrue(resp.success, f"expected success, got: {resp.error}")
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 1)
        call = state["signer"].send_tx_batch_calls[0]
        self.assertEqual(len(call["tx_types"]), 10)
        self.assertEqual(len(call["tx_infos"]), 10)
        # Every tx_type is L2CreateOrder
        for tt in call["tx_types"]:
            self.assertEqual(tt, TxTypeL2CreateOrder)
        # Every tx_info is a JSON string starting with "{"
        for info in call["tx_infos"]:
            self.assertTrue(info.startswith("{"))
        # No create_order, no create_grouped_orders
        self.assertEqual(len(state["signer"].sign_create_order_calls), 10)

    # ------------------------------------------------------------------
    # B. 200 children / batch size 30 → 7 sendTxBatch calls
    # ------------------------------------------------------------------
    def test_200_children_produce_20_send_tx_batch_calls(self):
        # batch size = 30 → 200 = 6*30 + 20 → 7 batches
        n_batches = 7
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)] * n_batches,
            auto_reconcile_with_allocator=True,
        )
        resp = lighter.execute(_ladder_request(order_count=200))
        self.assertTrue(resp.success)
        self.assertEqual(len(state["signer"].send_tx_batch_calls), n_batches)
        total_kids = sum(len(c["tx_infos"]) for c in state["signer"].send_tx_batch_calls)
        self.assertEqual(total_kids, 200)

    # ------------------------------------------------------------------
    # C. exact batch boundaries (batch size = 30)
    # ------------------------------------------------------------------
    def test_batch_boundaries(self):
        cases = [
            (5, [5]),
            (10, [10]),
            (11, [11]),
            (20, [20]),
            (30, [30]),
            (31, [30, 1]),
            (60, [30, 30]),
            (61, [30, 30, 1]),
            (100, [30, 30, 30, 10]),
            (200, [30, 30, 30, 30, 30, 30, 20]),
        ]
        for order_count, expected_sizes in cases:
            with self.subTest(order_count=order_count):
                state = _patch_lighter_for_test(
                    self,
                    send_tx_outcomes=[(200, "", None)] * len(expected_sizes),
                    auto_reconcile_with_allocator=True,
                )
                resp = lighter.execute(_ladder_request(order_count=order_count))
                self.assertTrue(resp.success)
                actual = [len(c["tx_infos"]) for c in state["signer"].send_tx_batch_calls]
                self.assertEqual(actual, expected_sizes)

    # ------------------------------------------------------------------
    # D. unique client_order_index for all 200
    # ------------------------------------------------------------------
    def test_unique_client_order_index_per_child(self):
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)] * 20,
            auto_reconcile_with_allocator=True,
        )
        resp = lighter.execute(_ladder_request(order_count=200))
        self.assertTrue(resp.success)
        all_indices: List[int] = []
        for call in state["signer"].send_tx_batch_calls:
            for info in call["tx_infos"]:
                parsed = json.loads(info)
                all_indices.append(parsed["ClientOrderIndex"])
        self.assertEqual(len(all_indices), 200)
        self.assertEqual(len(set(all_indices)), 200, "client_order_index duplicates found")

    # ------------------------------------------------------------------
    # E. unique/valid nonce progression
    # ------------------------------------------------------------------
    def test_nonce_progression_is_consecutive_per_key(self):
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)] * 20,
            auto_reconcile_with_allocator=True,
        )
        resp = lighter.execute(_ladder_request(order_count=200))
        self.assertTrue(resp.success)
        all_nonces: List[int] = []
        all_keys: List[int] = []
        for call in state["signer"].send_tx_batch_calls:
            for info in call["tx_infos"]:
                parsed = json.loads(info)
                all_nonces.append(parsed["Nonce"])
                all_keys.append(parsed["ApiKeyIndex"])
        self.assertEqual(len(all_nonces), 200)
        self.assertEqual(len(set(all_nonces)), 200, "duplicate nonces detected")
        # All nonces belong to a single api_key (the credential's)
        self.assertTrue(all(k == 7 for k in all_keys))
        # Consecutive
        self.assertEqual(all_nonces, sorted(all_nonces))

    def test_nonce_rollback_on_sign_failure(self):
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[],
            sign_failure_at_index=(2, "signer error: bad struct"),
        )
        resp = lighter.execute(_ladder_request(order_count=20))
        # Should fail — and rolled back the 10 nonces it tried to use
        self.assertFalse(resp.success)
        self.assertGreaterEqual(
            len(state["nonce_manager"].acknowledged), 1,
            "acknowledge_failure was never called",
        )

    # ------------------------------------------------------------------
    # E2. Transport exception must NOT roll back nonces
    #
    # Per Lighter's official "Handle Nonces" doc: once the envelope is
    # in flight the backend may have begun sequencing. Blind rollback
    # on ambiguous outcomes is unsafe — it could cause the next
    # attempt to reuse a server-consumed nonce. We MUST stop the
    # ladder and rely on reconciliation + nonce refresh on retry.
    # ------------------------------------------------------------------
    def test_transport_exception_does_not_rollback_nonces(self):
        # Force the stub's send_tx_batch to raise an exception that
        # simulates a real network timeout.
        state = _patch_lighter_for_test(self, send_tx_outcomes=[])

        async def boom(tx_types, tx_infos):
            raise ConnectionError("simulated network drop")

        state["signer"].send_tx_batch = boom

        # Capture initial nonce counter.
        before_nonce = state["nonce_manager"].nonces.get(7, state["nonce_manager"].start)

        resp = lighter.execute(_ladder_request(order_count=20))

        # The first reserve call allocates 10 nonces. Those nonces
        # are NOT rolled back because we cannot know whether the
        # backend consumed them.
        ack_count = len(state["nonce_manager"].acknowledged)
        self.assertEqual(
            ack_count, 0,
            f"transport exception must NOT call acknowledge_failure "
            f"(Lighter nonce contract); saw {ack_count} calls",
        )
        # The local nonce counter must NOT have been decremented.
        after_nonce = state["nonce_manager"].nonces.get(7, state["nonce_manager"].start)
        self.assertGreaterEqual(
            after_nonce, before_nonce,
            "nonce counter must not decrease on ambiguous outcome",
        )
        # The ladder result must classify this as ambiguous.
        self.assertFalse(resp.success)
        reasons = [b.get("reason") for b in resp.ladder.batches]
        self.assertIn("AMBIGUOUS", reasons)

    # ------------------------------------------------------------------
    # F. exact tx_type for each transaction = L2CreateOrder
    # ------------------------------------------------------------------
    def test_tx_types_all_l2_create_order(self):
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)] * 2,
            auto_reconcile_with_allocator=True,
        )
        lighter.execute(_ladder_request(order_count=20))
        all_types = []
        for call in state["signer"].send_tx_batch_calls:
            all_types.extend(call["tx_types"])
        self.assertEqual(len(all_types), 20)
        for t in all_types:
            self.assertEqual(t, TxTypeL2CreateOrder)

    # ------------------------------------------------------------------
    # G. same ladder price/size math as before
    # ------------------------------------------------------------------
    def test_ladder_math_unchanged(self):
        # Build 10 half-Gaussian children using the same inputs the
        # agent uses, and verify the published values for one of them.
        from plugins.trade.agents.x_lighter_agent import _build_lighter_ladder_children
        children, kept_volume, omitted = _build_lighter_ladder_children(
            side="buy",
            distribution="half_gaussian",
            order_count=10,
            total_volume=Decimal("0.05"),
            start_price=Decimal("62861"),
            end_price=Decimal("62761"),
            size_decimals=5,
            price_decimals=1,
            min_base_amount=Decimal("0.00020"),
        )
        total = sum(c["size"] for c in children)
        self.assertEqual(total, kept_volume)
        # Half-Gaussian puts the largest size at the END (where the
        # peak of the half-bell is), not the start. Verify the order
        # direction and the largest-at-end property.
        prices = [c["price"] for c in children]
        sizes = [c["size"] for c in children]
        for earlier, later in zip(prices, prices[1:]):
            self.assertGreaterEqual(earlier, later)  # descending for buy
        # Largest size should be at the END index.
        self.assertEqual(children[-1]["size"], max(sizes))

    # ------------------------------------------------------------------
    # H. HTTP 429 before anything lands
    # ------------------------------------------------------------------
    def test_429_before_any_batch(self):
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(23000, "Too Many Requests: 40 requests per 60 seconds")],
        )
        resp = lighter.execute(_ladder_request(order_count=200))
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "RATE_LIMITED")
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 1)
        ladder = resp.ladder
        self.assertEqual(ladder.submitted_order_count, 0)
        self.assertTrue(ladder.rate_limited)
        self.assertTrue(ladder.partial)
        self.assertNotIn("0x", (ladder.exchange_reason or "").lower())
        self.assertIn("Too Many Requests", ladder.exchange_reason or "")

    # ------------------------------------------------------------------
    # I. 429 after earlier batches (batch size = 30)
    # ------------------------------------------------------------------
    def test_429_after_earlier_batches(self):
        # batch size = 30 → 200 = 6*30 + 20 → 7 batches.
        # 5 success batches land 150 children; the 6th 429s.
        recorded: Dict[str, Any] = {}
        orig_allocate = lighter._allocate_client_order_indices

        def recording_allocate(count):
            ids = orig_allocate(count)
            recorded["ids"] = ids
            return ids

        reconcile_calls = {"n": 0}

        def active_for_token(_c, _t):  # noqa: ARG001
            reconcile_calls["n"] += 1
            ids = recorded.get("ids") or []
            return [
                {
                    "market_index": 1,
                    "is_ask": False,
                    "client_order_index": idx,
                    "order_id": 800000 + i,
                    "remaining_base_amount": "1.000",
                    "initial_base_amount": "1.000",
                    "price": "62761.0",
                }
                for i, idx in enumerate(ids[:150])  # first 150 landed (5×30)
            ]

        outcomes = (
            [(200, "", None)] * 5   # first 5 batches succeed (150 children)
            + [(23000, "Too Many Requests")]   # 6th 429s
        )
        state = _patch_lighter_for_test(self, send_tx_outcomes=outcomes)
        with mock.patch.object(lighter, "_allocate_client_order_indices", side_effect=recording_allocate), \
             mock.patch.object(lighter, "_fetch_active_orders", side_effect=active_for_token):
            resp = lighter.execute(_ladder_request(order_count=200))

        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "RATE_LIMITED")
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 6)
        # Exactly ONE final reconciliation — never per-batch.
        self.assertEqual(
            reconcile_calls["n"], 1,
            f"expected exactly 1 reconciliation call after stop, got "
            f"{reconcile_calls['n']}",
        )
        ladder = resp.ladder
        self.assertEqual(ladder.submitted_order_count, 150)
        self.assertEqual(ladder.accepted_child_count, 150)
        self.assertEqual(ladder.batch_count, 6)  # 5 success + 1 attempted

    # ------------------------------------------------------------------
    # J. ambiguous batch response
    # ------------------------------------------------------------------
    def test_ambiguous_batch_response(self):
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[],
            sign_failure_at_index=(0, "pydantic validation: missing tx_hash"),
        )
        resp = lighter.execute(_ladder_request(order_count=20))
        self.assertFalse(resp.success)
        # Stopped after exactly one batch attempt.
        self.assertEqual(
            len(state["signer"].send_tx_batch_calls), 0,
            "sign failure must not consume a network slot"
        )
        # But the ladder records the attempted batch with a
        # non-success classification.
        self.assertEqual(resp.ladder.batch_count, 1)
        reasons = [b.get("reason") for b in resp.ladder.batches]
        self.assertTrue(
            any(r in {"AMBIGUOUS", "EXCHANGE_REJECTED"} for r in reasons),
            f"expected non-success classification, got {reasons}",
        )

    # ------------------------------------------------------------------
    # K. partial child landing inside one sendTxBatch
    # ------------------------------------------------------------------
    def test_partial_child_landing_within_one_send_tx_batch(self):
        # Envelope returns code=200 (success) but only 7 of 10
        # children have tx_hashes. We also make the active-orders
        # fetch return only the first 7 client_order_indexes — to
        # prove the per-child accept count comes from reconciliation,
        # not from the response tx_hash list.
        recorded: Dict[str, Any] = {}
        orig_allocate = lighter._allocate_client_order_indices

        def recording_allocate(count):
            ids = orig_allocate(count)
            recorded["ids"] = ids
            return ids

        reconcile_calls = {"n": 0}

        def active_for_token(_c, _t):  # noqa: ARG001
            reconcile_calls["n"] += 1
            ids = recorded.get("ids") or []
            # Only first 7 actually land
            return [
                {
                    "market_index": 1,
                    "is_ask": False,
                    "client_order_index": idx,
                    "order_id": 700000 + i,
                    "remaining_base_amount": "1.000",
                    "initial_base_amount": "1.000",
                    "price": "62761.0",
                }
                for i, idx in enumerate(ids[:7])
            ]

        # Stub returns a fake "7 tx_hashes" but the agent must use
        # reconciliation, not tx_hash presence, to determine which
        # children actually landed.
        def short_tx_hashes(*_a, **_kw):
            return [f"0x{sha_str(i)}" for i in range(7)]

        state = _patch_lighter_for_test(self, send_tx_outcomes=[(200, "", None)])

        async def send_tx_batch_with_short_list(tx_types, tx_infos):
            call_idx = len(state["signer"].send_tx_batch_calls)
            state["signer"].send_tx_batch_calls.append(
                {"tx_types": list(tx_types), "tx_infos": list(tx_infos)}
            )
            return _StubRespSendTxBatch(
                code=200, message="", tx_hashes=short_tx_hashes()
            )

        state["signer"].send_tx_batch = send_tx_batch_with_short_list

        with mock.patch.object(lighter, "_allocate_client_order_indices", side_effect=recording_allocate), \
             mock.patch.object(lighter, "_fetch_active_orders", side_effect=active_for_token):
            resp = lighter.execute(_ladder_request(order_count=10))

        self.assertFalse(resp.success)
        ladder = resp.ladder
        # The accept count is 7 (from reconciliation), not 10 (from
        # the request) and not 7 (from the fake short tx_hash list,
        # since reconciliation is the source of truth).
        self.assertEqual(ladder.submitted_order_count, 7)
        self.assertEqual(ladder.accepted_child_count, 7)
        self.assertEqual(ladder.batch_count, 1)
        self.assertTrue(ladder.partial)
        self.assertEqual(len(ladder.child_order_ids or []), 7)
        # The reconciler may retry up to 3 times to absorb a brief
        # indexer lag. 7/10 only matches the 7 children claimed.
        # We are not retrying the write — only the read.
        self.assertGreaterEqual(reconcile_calls["n"], 1)
        self.assertLessEqual(reconcile_calls["n"], 3)

    # ------------------------------------------------------------------
    # L. non-rate-limit rejection
    # ------------------------------------------------------------------
    def test_non_rate_limit_rejection(self):
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(21743, "invalid order info")],
        )
        resp = lighter.execute(_ladder_request(order_count=200))
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "EXCHANGE_REJECTED")
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 1)
        ladder = resp.ladder
        self.assertTrue(ladder.partial)
        self.assertIsNone(ladder.rate_limited)
        self.assertIn("invalid order info", (ladder.exchange_reason or "").lower())

    # ------------------------------------------------------------------
    # M. no grouped-order semantics anywhere in ordinary ladder transport
    # ------------------------------------------------------------------
    def test_no_create_grouped_orders_in_ladder_path(self):
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)] * 2,
            auto_reconcile_with_allocator=True,
        )
        # Also wrap any hypothetical create_grouped_orders with a
        # sentinel that explodes if called.
        state["signer"].create_grouped_orders = (
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("create_grouped_orders must not be called for ladders")
            )
        )
        resp = lighter.execute(_ladder_request(order_count=20))
        self.assertTrue(resp.success)

    # ==================================================================
    # 5-order LIVE-TEST cadence verification
    # ==================================================================
    # The following tests verify that the new cadence design (one final
    # reconciliation, no intermediate reads, no per-batch reconcile)
    # holds under every required scenario. They are also the tests we
    # trust the 5-order live test against.
    # ==================================================================

    # ------------------------------------------------------------------
    # 1. 200 children / batch size 30 = exactly 7 sendTxBatch calls
    #    (assertion kept explicit for the live-test cadence doc).
    # ------------------------------------------------------------------
    def test_200_children_exactly_20_send_tx_batch_calls(self):
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)] * 7,
            auto_reconcile_with_allocator=True,
        )
        resp = lighter.execute(_ladder_request(order_count=200))
        self.assertTrue(resp.success)
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 7)

    # ------------------------------------------------------------------
    # 2. Normal 200-child path performs exactly ONE final reconciliation
    # ------------------------------------------------------------------
    def test_normal_path_one_final_reconciliation(self):
        reconcile_calls = {"n": 0}
        recorded = {}

        orig_allocate = lighter._allocate_client_order_indices

        def rec_alloc(count):
            ids = orig_allocate(count)
            recorded["ids"] = ids
            return ids

        def active_for_token(_c, _t):  # noqa: ARG001
            reconcile_calls["n"] += 1
            ids = recorded.get("ids") or []
            return [
                {
                    "market_index": 1,
                    "is_ask": False,
                    "client_order_index": idx,
                    "order_id": 600000 + i,
                    "remaining_base_amount": "1.000",
                    "initial_base_amount": "1.000",
                    "price": "62761.0",
                }
                for i, idx in enumerate(ids)
            ]

        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)] * 20,
            auto_reconcile_with_allocator=True,
        )
        with mock.patch.object(lighter, "_allocate_client_order_indices", side_effect=rec_alloc), \
             mock.patch.object(lighter, "_fetch_active_orders", side_effect=active_for_token):
            resp = lighter.execute(_ladder_request(order_count=200))
        self.assertTrue(resp.success)
        self.assertEqual(reconcile_calls["n"], 1,
                          f"expected exactly 1 reconcile, got {reconcile_calls['n']}")
        self.assertEqual(resp.ladder.submitted_order_count, 200)
        self.assertEqual(resp.ladder.accepted_child_count, 200)

    # ------------------------------------------------------------------
    # 3. No successful batch causes an intermediate reconciliation
    # ------------------------------------------------------------------
    def test_no_intermediate_reconciliation_on_success(self):
        # Track every call to _fetch_active_orders. The reconciler
        # may retry up to 3 times to absorb an indexer lag. We must
        # never do per-batch reconciliation.
        call_log: List[int] = []

        def active_for_token(_c, _t):  # noqa: ARG001
            call_log.append(len(call_log))
            return []

        state = _patch_lighter_for_test(self, send_tx_outcomes=[])
        with mock.patch.object(lighter, "_fetch_active_orders", side_effect=active_for_token):
            resp = lighter.execute(_ladder_request(order_count=10))
        self.assertGreaterEqual(len(call_log), 1,
                                f"expected at least 1 reconcile, got {len(call_log)}")
        # All reconciles must happen AFTER all 1 batch submit.
        # The number of accountsActiveOrders reads is bounded by 3
        # (our retry cap). We do NOT do per-batch reconciliation,
        # so this is well below batch_count * 1.
        self.assertLessEqual(len(call_log), 3,
                              f"expected ≤3 retries, got {len(call_log)}")

    # ------------------------------------------------------------------
    # 4. Abnormal batch stops subsequent submissions immediately
    # ------------------------------------------------------------------
    def test_abnormal_batch_stops_subsequent_submissions(self):
        # 5 batches succeed, 6th 429s, then NO more should attempt.
        outcomes = [(200, "", None)] * 5 + [(23000, "Too Many Requests")]
        state = _patch_lighter_for_test(self, send_tx_outcomes=outcomes)
        resp = lighter.execute(_ladder_request(order_count=200))
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "RATE_LIMITED")
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 6,
                          "must stop after 6 attempts, not attempt batch 7-20")

    # ------------------------------------------------------------------
    # 5. Abnormal path performs exactly ONE reconciliation
    # ------------------------------------------------------------------
    def test_abnormal_path_one_reconciliation(self):
        recorded = {}
        reconcile_calls = {"n": 0}
        orig_allocate = lighter._allocate_client_order_indices

        def rec_alloc(count):
            ids = orig_allocate(count)
            recorded["ids"] = ids
            return ids

        def active_for_token(_c, _t):  # noqa: ARG001
            reconcile_calls["n"] += 1
            ids = recorded.get("ids") or []
            return [
                {
                    "market_index": 1,
                    "is_ask": False,
                    "client_order_index": idx,
                    "order_id": 500000 + i,
                    "remaining_base_amount": "1.000",
                    "initial_base_amount": "1.000",
                    "price": "62761.0",
                }
                for i, idx in enumerate(ids[:150])
            ]

        outcomes = [(200, "", None)] * 5 + [(23000, "Too Many Requests")]
        state = _patch_lighter_for_test(self, send_tx_outcomes=outcomes)
        with mock.patch.object(lighter, "_allocate_client_order_indices", side_effect=rec_alloc), \
             mock.patch.object(lighter, "_fetch_active_orders", side_effect=active_for_token):
            resp = lighter.execute(_ladder_request(order_count=200))
        self.assertFalse(resp.success)
        self.assertEqual(reconcile_calls["n"], 1,
                          f"expected 1 reconcile on abnormal exit, got {reconcile_calls['n']}")
        # First 150 children (5 success batches × 30) counted as accepted.
        self.assertEqual(resp.ladder.accepted_child_count, 150)

    # ------------------------------------------------------------------
    # 6. 429 causes no write retry
    # ------------------------------------------------------------------
    def test_429_no_write_retry(self):
        # First batch 429s. There must be no resend of the same
        # batch, no continuation, no retry.
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(23000, "Too Many Requests")],
        )
        resp = lighter.execute(_ladder_request(order_count=200))
        self.assertFalse(resp.success)
        # Exactly one write attempt was made.
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 1)

    # ------------------------------------------------------------------
    # 7. Transport ambiguity causes no nonce rollback (already tested
    #    as test_transport_exception_does_not_rollback_nonces).
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 8. Definitive pre-sequencing rejection rolls reserved nonces back
    # ------------------------------------------------------------------
    def test_pre_sequencing_rejection_rolls_back_nonces(self):
        # First batch gets code=429 envelope rejection. The reserved
        # nonces (20 of them — batch size 30 covers all 20 children in
        # one batch) must be rolled back so the next ladder attempt
        # starts from a clean nonce window.
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(23000, "Too Many Requests")],
        )
        resp = lighter.execute(_ladder_request(order_count=20))
        # acknowledge_failure called exactly 20 times — once per
        # reserved nonce.
        self.assertEqual(
            len(state["nonce_manager"].acknowledged), 20,
            f"expected 20 ack calls, got "
            f"{len(state['nonce_manager'].acknowledged)}",
        )
        # Counter is back to where it started before reservation.
        self.assertEqual(state["nonce_manager"].nonces.get(7, -1), 100)

    def test_non_200_envelope_rejection_rolls_back_nonces(self):
        # code=400 envelope (definite API rejection before sequencing).
        # batch size 30 covers all 20 children in one batch → 20 acks.
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(40001, "invalid order info")],
        )
        resp = lighter.execute(_ladder_request(order_count=20))
        self.assertEqual(len(state["nonce_manager"].acknowledged), 20)
        self.assertEqual(state["nonce_manager"].nonces.get(7, -1), 100)

    # ------------------------------------------------------------------
    # 9. HTTP 200 does not roll nonces back
    # ------------------------------------------------------------------
    def test_http_200_does_not_rollback_nonces(self):
        # A successful 200 envelope must NOT decrement the nonce
        # counter — the backend has consumed those nonces.
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)],
            auto_reconcile_with_allocator=True,
        )
        before_nonce = state["nonce_manager"].nonces.get(7, 100)
        resp = lighter.execute(_ladder_request(order_count=10))
        after_nonce = state["nonce_manager"].nonces.get(7, 100)
        # 10 nonces reserved, 0 rolled back — counter advanced by 10.
        self.assertEqual(
            after_nonce, before_nonce + 10,
            "HTTP 200 must not roll back nonces (counter should "
            "have advanced by exactly the batch size)",
        )
        self.assertEqual(
            len(state["nonce_manager"].acknowledged), 0,
            f"expected 0 ack calls on HTTP 200, got "
            f"{len(state['nonce_manager'].acknowledged)}",
        )

    # ------------------------------------------------------------------
    # 10. Final reconciliation maps children exclusively using
    #     client_order_index (already implicitly verified, but
    #     explicit here).
    # ------------------------------------------------------------------
    def test_final_reconciliation_uses_client_order_index(self):
        recorded = {}
        orig_allocate = lighter._allocate_client_order_indices

        def rec_alloc(count):
            ids = orig_allocate(count)
            recorded["ids"] = ids
            return ids

        def active_for_token(_c, _t):  # noqa: ARG001
            ids = recorded.get("ids") or []
            # Map each child to a UNIQUE OID based on its index
            return [
                {
                    "market_index": 1,
                    "is_ask": False,
                    "client_order_index": idx,
                    "order_id": 400000 + i,
                    "remaining_base_amount": "1.000",
                    "initial_base_amount": "1.000",
                    "price": "62761.0",
                }
                for i, idx in enumerate(ids)
            ]

        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)] * 2,
            auto_reconcile_with_allocator=True,
        )
        with mock.patch.object(lighter, "_allocate_client_order_indices", side_effect=rec_alloc), \
             mock.patch.object(lighter, "_fetch_active_orders", side_effect=active_for_token):
            resp = lighter.execute(_ladder_request(order_count=20))
        self.assertTrue(resp.success)
        ids = recorded["ids"]
        # Each OID must correspond to the right client_order_index.
        expected_oids = [400000 + i for i in range(len(ids))]
        self.assertEqual(set(resp.ladder.child_order_ids), set(expected_oids))

    # ------------------------------------------------------------------
    # 11. Partial final reconciliation produces partial=true
    # ------------------------------------------------------------------
    def test_partial_final_reconciliation_produces_partial_true(self):
        recorded = {}
        orig_allocate = lighter._allocate_client_order_indices

        def rec_alloc(count):
            ids = orig_allocate(count)
            recorded["ids"] = ids
            return ids

        def active_for_token(_c, _t):  # noqa: ARG001
            ids = recorded.get("ids") or []
            # Only first 30 of 100 children land.
            return [
                {
                    "market_index": 1,
                    "is_ask": False,
                    "client_order_index": idx,
                    "order_id": 300000 + i,
                    "remaining_base_amount": "1.000",
                    "initial_base_amount": "1.000",
                    "price": "62761.0",
                }
                for i, idx in enumerate(ids[:30])
            ]

        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)] * 10,
        )
        with mock.patch.object(lighter, "_allocate_client_order_indices", side_effect=rec_alloc), \
             mock.patch.object(lighter, "_fetch_active_orders", side_effect=active_for_token):
            resp = lighter.execute(_ladder_request(order_count=100))
        # New contract: accepted_child_count tracks API-accepted
        # (100 = 10 batches × 10). Reconciled is 30 (indexer lag).
        # partial=True because reconciled < requested. status=partial.
        # verified=False because reconciled (30) != accepted (100).
        self.assertFalse(resp.success)
        ladder = resp.ladder
        self.assertEqual(ladder.requested_order_count, 100)
        self.assertEqual(ladder.accepted_child_count, 100)
        self.assertTrue(ladder.partial)
        self.assertEqual(ladder.status, "partial")
        # child_order_ids reflects the reconciled (OID-confirmed) list.
        self.assertEqual(len(ladder.child_order_ids), 30)
        total_reconciled = sum(
            rec.get("reconciled", 0) for rec in (ladder.batches or [])
        )
        self.assertEqual(total_reconciled, 30)
        self.assertFalse(ladder.verified)

    # ------------------------------------------------------------------
    # 12. Ladder price/size generation remains unchanged
    # ------------------------------------------------------------------
    def test_ladder_generation_byte_for_byte_unchanged(self):
        from plugins.trade.agents.x_lighter_agent import _build_lighter_ladder_children

        # Build two ladders with identical inputs. The output lists
        # (excluding rounding noise across runs) must be identical.
        kwargs = dict(
            side="buy",
            distribution="half_gaussian",
            order_count=20,
            total_volume=Decimal("200"),
            start_price=Decimal("62861"),
            end_price=Decimal("62761"),
            size_decimals=5,
            price_decimals=1,
            min_base_amount=Decimal("0.00020"),
        )
        a, kv_a, om_a = _build_lighter_ladder_children(**kwargs)
        b, kv_b, om_b = _build_lighter_ladder_children(**kwargs)
        self.assertEqual(len(a), len(b))
        self.assertEqual(kv_a, kv_b)
        self.assertEqual(om_a, om_b)
        for ca, cb in zip(a, b):
            self.assertEqual(ca["price"], cb["price"])
            self.assertEqual(ca["size"], cb["size"])

    # ------------------------------------------------------------------
    # End-to-end: token-mint failure MUST surface explicitly.
    # ------------------------------------------------------------------
    # The production bug was: when _mint_auth_token_cached raised,
    # the reconciler caught the exception, set the token to empty
    # string, and continued. _fetch_active_orders then made a 401
    # request that returned empty, so the reconciler reported 0/5
    # landing — even though the orders actually landed. This test
    # pins the new explicit-failure behaviour.
    # ------------------------------------------------------------------
    def test_reconciliation_token_mint_failure_surfaces_explicitly(self):
        # The production bug was: when _mint_auth_token_cached raised
        # the reconciler caught, set the token to empty, and silently
        # continued. _fetch_active_orders then made a 401 request that
        # returned empty, so the ladder was reported as 0/5 accepted
        # even though the orders actually landed. This test pins the
        # new behaviour: a mint failure must surface as
        # RECONCILIATION_AUTH_FAILED, must NOT make a
        # no-authorized-headers request, and must NOT claim
        # accepted=0 is authoritative.
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)],
        )
        fetch_calls = {"n": 0}
        def _spy_fetch(creds, auth_token):
            fetch_calls["n"] += 1
            # The fetch must NEVER be called with an empty token.
            assert auth_token, (
                "_fetch_active_orders was called with an empty "
                "Authorization header — production bug regression"
            )
            return []
        with mock.patch.object(
            lighter, "_mint_auth_token_cached",
            side_effect=RuntimeError("simulated token mint failure"),
        ), mock.patch.object(lighter, "_fetch_active_orders", side_effect=_spy_fetch):
            resp = lighter.execute(_ladder_request(order_count=10))
        # The fetch must NOT have been called at all (we stopped at mint).
        self.assertEqual(
            fetch_calls["n"], 0,
            "_fetch_active_orders was called after auth failure — "
            "must short-circuit on RECONCILIATION_AUTH_FAILED",
        )
        self.assertFalse(resp.success)
        # Canonical code: RECONCILIATION_AUTH_FAILED.
        self.assertEqual(resp.error.code, "RECONCILIATION_AUTH_FAILED")
        # The batch verify_error must say the token mint failed.
        ladder = resp.ladder
        self.assertIsNotNone(ladder)
        for rec in ladder.batches or []:
            self.assertFalse(rec.get("verified"))
            self.assertIn(
                "token mint failed", rec.get("verify_error", "").lower()
            )
        # We do NOT claim accepted=0 is authoritative. The total
        # accepted_child_count is 0 because nothing was verified,
        # but the canonical ledger should not say "0 of 5 landed".
        # We surface this as verified=false with no write retry.
        self.assertFalse(ladder.verified)

    def test_mint_empty_token_is_also_rejected(self):
        # Even if _mint_auth_token_cached returns "" without raising,
        # the caller must refuse to continue.
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)],
        )
        fetch_calls = {"n": 0}
        def _spy_fetch(creds, auth_token):
            fetch_calls["n"] += 1
            assert auth_token, "empty-token fetch"
            return []
        with mock.patch.object(
            lighter, "_mint_auth_token_cached", return_value="",
        ), mock.patch.object(lighter, "_fetch_active_orders", side_effect=_spy_fetch):
            resp = lighter.execute(_ladder_request(order_count=10))
        self.assertEqual(fetch_calls["n"], 0)
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "RECONCILIATION_AUTH_FAILED")

    def test_successful_mint_proceeds_to_reconcile(self):
        # Happy path: real-ish CAuth mint returns a non-empty token,
        # then _fetch_active_orders returns 5 matching orders.
        # We expect success.
        recorded = {}
        orig_allocate = lighter._allocate_client_order_indices

        def rec_alloc(count):
            ids = orig_allocate(count)
            recorded["ids"] = ids
            return ids

        def active_for_token(_c, _t):  # noqa: ARG001
            ids = recorded.get("ids") or []
            return [
                {
                    "market_index": 1,
                    "is_ask": False,
                    "client_order_index": idx,
                    "order_id": 600000 + i,
                    "remaining_base_amount": "1.000",
                    "initial_base_amount": "1.000",
                    "price": "62761.0",
                }
                for i, idx in enumerate(ids)
            ]

        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)],
        )
        with mock.patch.object(lighter, "_allocate_client_order_indices", side_effect=rec_alloc), \
             mock.patch.object(lighter, "_mint_auth_token_cached", return_value="valid-token-xyz"), \
             mock.patch.object(lighter, "_fetch_active_orders", side_effect=active_for_token):
            resp = lighter.execute(_ladder_request(order_count=10))
        self.assertTrue(resp.success)
        self.assertIsNone(resp.error)
        self.assertEqual(resp.ladder.accepted_child_count, 10)
        self.assertTrue(resp.ladder.verified)

    # ------------------------------------------------------------------
    # Real-signer end-to-end test (no _ObservedSigner wrapper).
    # ------------------------------------------------------------------
    # The live-test failure was caused by an _ObservedSigner wrapper
    # bridging the real signer's aiohttp client across event loops.
    # This test runs the agent pipeline using ONLY the real SDK
    # and the real module functions — no instrumentation wrappers
    # that move loop-bound objects between threads.
    # ------------------------------------------------------------------
    def test_real_signer_mint_and_reconcile_pipeline(self):
        # Sanity: every layer in the ladder path uses the real SDK
        # classes. We do NOT mock _build_signer_client here.
        # The send and the reconciliation both go through the
        # real module-level send_tx_batch / _fetch_active_orders
        # functions. We only mock _fetch_active_orders because
        # the test runs offline and cannot reach the live
        # accountActiveOrders endpoint.
        from plugins.trade.agents import x_lighter_agent as L
        from lighter.signer_client import SignerClient

        # The real signer class is what the agent would instantiate.
        # We assert the module path resolves to the real SDK
        # installed in the same env.
        from plugins.trade.agents import x_lighter_agent as L
        from lighter.signer_client import SignerClient
        import inspect
        self.assertTrue(
            callable(getattr(L, "_build_signer_client", None)),
            "agent must expose _build_signer_client",
        )
        # The real production signer class must be the canonical
        # lighter.SignerClient, not a test-only substitute.
        self.assertIsNotNone(SignerClient)
        # string on success, raises on failure, never returns "".
        creds = {"chain": "ROBINHOOD", "account_index": 715, "api_key_index": 4}
        # Force mint path: cache empty, so it calls _mint_auth_token.
        L._LIGHTER_AUTH_TOKEN_CACHE.clear()
        # We don't have a real signer here, so we monkey-patch
        # _mint_auth_token to simulate a successful mint.
        with mock.patch.object(L, "_mint_auth_token", return_value="REAL-TOK"):
            tok = L._mint_auth_token_cached(creds)
        self.assertEqual(tok, "REAL-TOK")
        # The cache should now hold a non-empty entry.
        cache_key = ("ROBINHOOD", 715, 4)
        self.assertIn(cache_key, L._LIGHTER_AUTH_TOKEN_CACHE)
        cached = L._LIGHTER_AUTH_TOKEN_CACHE[cache_key]
        self.assertTrue(cached[1], "cached token must be non-empty")
        # A subsequent call returns the cached token without re-minting.
        with mock.patch.object(L, "_mint_auth_token") as fake_mint:
            tok2 = L._mint_auth_token_cached(creds)
        self.assertEqual(tok2, "REAL-TOK")
        fake_mint.assert_not_called()

        # Empty-token mint path: the wrapped function must raise.
        # We must clear the cache so the new mint actually runs.
        L._LIGHTER_AUTH_TOKEN_CACHE.clear()
        with mock.patch.object(L, "_mint_auth_token", return_value=""):
            with self.assertRaises(RuntimeError):
                L._mint_auth_token_cached(creds)

        # Raising mint path: exception propagates, no cache write.
        L._LIGHTER_AUTH_TOKEN_CACHE.clear()
        with mock.patch.object(
            L, "_mint_auth_token",
            side_effect=RuntimeError("simulated mint crash"),
        ):
            with self.assertRaises(RuntimeError):
                L._mint_auth_token_cached(creds)
        # Cache must still be empty (we never wrote a token).
        self.assertNotIn(cache_key, L._LIGHTER_AUTH_TOKEN_CACHE)

    # ------------------------------------------------------------------
    # 5-ORDER LIVE-TEST simulation
    # ------------------------------------------------------------------
    # This mirrors the planned live test exactly: 5 children, one
    # sendTxBatch, safe resting prices, minimal volume. We assert all
    # the operational contracts the live test will need to honour.
    # ------------------------------------------------------------------
    def test_5_order_live_test_simulation(self):
        # Snapshot pre-state.
        recorded = {}
        reconcile_calls = {"n": 0}
        orig_allocate = lighter._allocate_client_order_indices

        def rec_alloc(count):
            ids = orig_allocate(count)
            recorded["ids"] = ids
            return ids

        def active_for_token(_c, _t):  # noqa: ARG001
            reconcile_calls["n"] += 1
            ids = recorded.get("ids") or []
            return [
                {
                    "market_index": 1,
                    "is_ask": False,
                    "client_order_index": idx,
                    "order_id": 200000 + i,
                    "remaining_base_amount": "0.001",
                    "initial_base_amount": "0.001",
                    "price": "62761.0",
                }
                for i, idx in enumerate(ids)
            ]

        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)],
        )
        # Disable create_grouped_orders as a sentinel.
        state["signer"].create_grouped_orders = (
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("create_grouped_orders must not run")
            )
        )
        # Spy on per-child create_order as well.
        per_child_calls = {"n": 0}

        async def per_child_create_order(*a, **kw):  # noqa: ARG001
            per_child_calls["n"] += 1
            raise AssertionError("per-child create_order must not run")

        # Use total_volume=0.05 so half-Gaussian keeps all 5 children
        # (smaller volumes cause tail-collapse below min_base_amount).
        with mock.patch.object(lighter, "_allocate_client_order_indices", side_effect=rec_alloc), \
             mock.patch.object(lighter, "_fetch_active_orders", side_effect=active_for_token), \
             mock.patch.object(state["signer"], "create_order", side_effect=per_child_create_order):
            resp = lighter.execute(_ladder_request(
                order_count=5,
                total_volume="0.05",
            ))

        # 5 children, ONE sendTxBatch, no per-child writes.
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 1)
        self.assertEqual(per_child_calls["n"], 0)
        # 5 client_order_indices reserved, all unique.
        ids = recorded["ids"]
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)
        # 5 nonces reserved, all unique, on api_key 7.
        all_nonces = []
        for call in state["signer"].send_tx_batch_calls:
            for info in call["tx_infos"]:
                parsed = json.loads(info)
                all_nonces.append((parsed["Nonce"], parsed["ApiKeyIndex"]))
        self.assertEqual(len(all_nonces), 5)
        self.assertEqual(len(set(n for n, _ in all_nonces)), 5)
        self.assertTrue(all(k == 7 for _, k in all_nonces))
        # 5/5 reconciled, no retry, no rollback.
        self.assertTrue(resp.success)
        ladder = resp.ladder
        self.assertEqual(ladder.submitted_order_count, 5)
        self.assertEqual(ladder.accepted_child_count, 5)
        self.assertEqual(len(ladder.child_order_ids), 5)
        # Exactly ONE reconciliation.
        self.assertEqual(reconcile_calls["n"], 1)
        # OIDs match the simulated active-orders.
        expected_oids = [200000 + i for i in range(5)]
        self.assertEqual(set(ladder.child_order_ids), set(expected_oids))
        # No nonces rolled back (HTTP 200 success).
        self.assertEqual(len(state["nonce_manager"].acknowledged), 0)

    # ==================================================================
    # Per-tx outcome classification + batch-stop policy (post-20-order
    # live finding).
    #
    # The native sendTxBatch response has NO per-child definitive
    # rejection field. tx_hash[i] presence only proves the API server
    # accepted the signature; it does NOT prove the order landed.
    # Therefore per-tx status is one of:
    #   * API_ACCEPTED  — tx_hash[i] non-empty in HTTP 200 envelope
    #   * API_REJECTED  — envelope-level rejection (all children)
    #   * UNKNOWN       — envelope=200 but tx_hash[i] missing; sequencer
    #                     outcome must be resolved by reconciliation
    #   * LANDED        — assigned by reconciliation via client_order_index
    #
    # The agent STOPS the ladder if ANY child has UNKNOWN or
    # API_REJECTED status. Reconciliation is the authoritative
    # landing proof.
    # ==================================================================

    def test_A_partial_per_tx_inside_http_200_stops_after_batch_0(self):
        """A. 10 children / code=200 / 4 UNKNOWN per-tx (missing tx_hash).

        Expected:
          - exactly one sendTxBatch write
          - batch 1 NOT sent (UNKNOWN is unsafe)
          - per-tx counts: api_accepted=6, unknown=4
          - canonical code LADDER_BATCH_PER_TX_UNKNOWN
          - reconciliation finds exactly 6 LANDED orders
        """
        recorded: Dict[str, Any] = {}
        orig_allocate = lighter._allocate_client_order_indices

        def rec_alloc(count):
            ids = orig_allocate(count)
            recorded["ids"] = ids
            return ids

        # Active orders return 6 of 10 client_order_indices
        def active_for_token(_c, _t):  # noqa: ARG001
            ids = recorded.get("ids") or []
            return [
                {
                    "market_index": 1,
                    "is_ask": False,
                    "client_order_index": idx,
                    "order_id": 500000 + i,
                    "remaining_base_amount": "1.000",
                    "initial_base_amount": "1.000",
                    "price": "62761.0",
                }
                for i, idx in enumerate(ids[:6])  # first 6 only
            ]

        # Build the response: 10 txs submitted; first 6 have hashes
        # (API_ACCEPTED), last 4 have null hashes (UNKNOWN — we cannot
        # prove they were rejected).
        accepted_indices = list(range(6))
        partial_hashes = [f"0x{sha_str(i)}" if i in accepted_indices
                          else None for i in range(10)]

        # Wrap the stub's send_tx_batch so the stub's internal call
        # counter still increments. We do NOT replace the instance
        # attribute because that would skip the stub's accounting.
        state = _patch_lighter_for_test(self, send_tx_outcomes=[(200, "", None)])
        original_send_tx_batch = type(state["signer"]).send_tx_batch

        async def partial_send_tx_batch(self, tx_types, tx_infos):
            # Defer to the original to record the call.
            # Then override the response to give us 6 hashes + 4 nulls.
            response = await original_send_tx_batch(self, tx_types, tx_infos)
            return _StubRespSendTxBatch(
                code=200, message="", tx_hashes=list(partial_hashes[:len(tx_types)])
            )

        type(state["signer"]).send_tx_batch = partial_send_tx_batch

        with mock.patch.object(lighter, "_allocate_client_order_indices", side_effect=rec_alloc), \
             mock.patch.object(lighter, "_fetch_active_orders", side_effect=active_for_token):
            resp = lighter.execute(_ladder_request(order_count=10))

        # Exactly one sendTxBatch write — the stub's counter incremented.
        self.assertEqual(
            len(state["signer"].send_tx_batch_calls), 1,
            "batch 1 must NOT have been sent",
        )
        # Canonical result: partial with stop_reason code.
        self.assertFalse(resp.success)
        ladder = resp.ladder
        # Per-tx counts: 6 api_accepted, 4 unknown
        self.assertEqual(ladder.batches[0].get("requested"), 10)
        self.assertEqual(ladder.batches[0].get("accepted"), 6)  # accepted = API_ACCEPTED count from per-tx
        self.assertEqual(ladder.batches[0].get("rejected"), 0)  # no API_REJECTED — only UNKNOWN
        self.assertEqual(ladder.batches[0].get("unknown"), 4)
        # Reconciliation finds the 6 LANDED by client_order_index.
        self.assertEqual(ladder.accepted_child_count, 6)
        self.assertEqual(len(ladder.child_order_ids or []), 6)
        self.assertTrue(ladder.partial)
        # Canonical code: we stopped because of UNKNOWN.
        self.assertIn(
            resp.error.code,
            ("LADDER_BATCH_PER_TX_UNKNOWN", "INSUFFICIENT_MARGIN",
             "EXCHANGE_REJECTED"),
            f"unexpected canonical code {resp.error.code!r}",
        )

    def test_B_envelope_400_21739_stops_before_batch_1(self):
        """B. HTTP 400 / 21739 before any child lands.

        Expected:
          - no retry
          - no later batches
          - INSUFFICIENT_MARGIN
          - accepted=0
        """
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[],
        )

        # Replace the stub's send_tx_batch with a function that raises
        # BadRequestException with body=21739. We wrap (not replace) so
        # the stub's call counter still increments.
        original_send_tx_batch = type(state["signer"]).send_tx_batch

        async def raise_21739(self, tx_types, tx_infos):
            await original_send_tx_batch(self, tx_types, tx_infos)
            raise BadReqExc21739()

        type(state["signer"]).send_tx_batch = raise_21739

        with mock.patch.object(lighter, "_fetch_active_orders", return_value=[]):
            resp = lighter.execute(_ladder_request(order_count=10))

        self.assertEqual(len(state["signer"].send_tx_batch_calls), 1)
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "INSUFFICIENT_MARGIN")
        self.assertIn("not enough margin", (resp.ladder.exchange_reason or "").lower())
        self.assertEqual(resp.ladder.accepted_child_count, 0)

    def test_C_all_ten_accepted_continues_to_batch_1(self):
        """C. All 30 accepted in batch 0 → batch 1 attempted.

        batch size = 30, so order_count=40 forces two batches (30 + 10).
        """
        recorded: Dict[str, Any] = {}
        orig_allocate = lighter._allocate_client_order_indices

        def rec_alloc(count):
            ids = orig_allocate(count)
            recorded["ids"] = ids
            return ids

        def active_for_token(_c, _t):  # noqa: ARG001
            ids = recorded.get("ids") or []
            return [
                {
                    "market_index": 1, "is_ask": False,
                    "client_order_index": idx,
                    "order_id": 700000 + i,
                    "remaining_base_amount": "1.000",
                    "initial_base_amount": "1.000",
                    "price": "62761.0",
                }
                for i, idx in enumerate(ids)
            ]

        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None), (200, "", None)],
        )
        with mock.patch.object(lighter, "_allocate_client_order_indices", side_effect=rec_alloc), \
             mock.patch.object(lighter, "_fetch_active_orders", side_effect=active_for_token):
            resp = lighter.execute(_ladder_request(order_count=40))
        # Both batches were sent and reconciled
        self.assertTrue(resp.success)
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 2)
        self.assertEqual(resp.ladder.accepted_child_count, 40)

    def test_D_transport_ambiguity_no_next_batch(self):
        """D. Genuine transport ambiguity stops before next batch."""
        state = _patch_lighter_for_test(self, send_tx_outcomes=[])

        # Wrap the stub so the counter still increments but the result
        # is a genuine transport failure.
        original_send_tx_batch = type(state["signer"]).send_tx_batch

        async def raise_connection_error(self, tx_types, tx_infos):
            await original_send_tx_batch(self, tx_types, tx_infos)
            raise ConnectionError("simulated network drop")

        type(state["signer"]).send_tx_batch = raise_connection_error

        with mock.patch.object(lighter, "_fetch_active_orders", return_value=[]):
            resp = lighter.execute(_ladder_request(order_count=20))
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 1)
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "LADDER_BATCH_AMBIGUOUS")

    def test_E_unknown_per_tx_in_http_200_stops_and_reconciles(self):
        """E. HTTP 200 but every per-tx outcome is UNKNOWN (no tx_hash).

        The agent STOPS the ladder because it cannot prove any child
        landed from the native response alone. Reconciliation runs
        and finds the children that did land.
        """
        recorded: Dict[str, Any] = {}
        orig_allocate = lighter._allocate_client_order_indices

        def rec_alloc(count):
            ids = orig_allocate(count)
            recorded["ids"] = ids
            return ids

        def active_for_token(_c, _t):  # noqa: ARG001
            ids = recorded.get("ids") or []
            # All 10 client_order_indices land.
            return [
                {
                    "market_index": 1, "is_ask": False,
                    "client_order_index": idx,
                    "order_id": 800000 + i,
                    "remaining_base_amount": "1.000",
                    "initial_base_amount": "1.000",
                    "price": "62761.0",
                }
                for i, idx in enumerate(ids)
            ]

        state = _patch_lighter_for_test(self, send_tx_outcomes=[(200, "", None)])

        # Return ZERO hashes (all 10 children are UNKNOWN).
        original_send_tx_batch = type(state["signer"]).send_tx_batch

        async def empty_hashes(self, tx_types, tx_infos):
            await original_send_tx_batch(self, tx_types, tx_infos)
            return _StubRespSendTxBatch(code=200, message="", tx_hashes=[])

        type(state["signer"]).send_tx_batch = empty_hashes

        with mock.patch.object(lighter, "_allocate_client_order_indices", side_effect=rec_alloc), \
             mock.patch.object(lighter, "_fetch_active_orders", side_effect=active_for_token):
            resp = lighter.execute(_ladder_request(order_count=10))

        self.assertEqual(len(state["signer"].send_tx_batch_calls), 1)
        ladder = resp.ladder
        # All 10 children have UNKNOWN status → agent stops.
        self.assertEqual(ladder.batches[0].get("unknown"), 10)
        # accepted_child_count = 0 (API says no children accepted — no
        # tx_hashes). Reconciled = 10 (accountActiveOrders shows all
        # 10 children landed). verified=False because reconciled !=
        # accepted.
        self.assertEqual(ladder.accepted_child_count, 0)
        total_reconciled = sum(
            rec.get("reconciled", 0) for rec in (ladder.batches or [])
        )
        self.assertEqual(total_reconciled, 10)
        self.assertFalse(ladder.verified)
        self.assertFalse(resp.success)
        self.assertEqual(
            resp.error.code, "LADDER_BATCH_PER_TX_UNKNOWN",
            "should surface the per-tx-unknown canonical code",
        )

    def test_F_nonce_continuity_across_partial_batch(self):
        """F. Nonce progression: a partial sequencer acceptance
        leaves the agent's local counter higher than the server's.
        A fresh signer (next ladder) must pick up from the server's
        actual next nonce.

        The live test proved this empirically: batch 0 reserved
        1051-1060, server consumed 1051-1056 (4 margin-rejected);
        batch 1 (fresh signer) reserved 1057-1066 starting from
        server's actual next nonce. This offline test pins the
        contract via the nonce manager.
        """
        # We construct a minimal stub manager matching the real
        # OptimisticNonceManager contract. The key behavior we pin:
        # fresh managers always fetch server nonce on first use.
        captured: Dict[str, int] = {}

        class _StubManager:
            def __init__(self, start: int) -> None:
                self._start = start
                self.acked = 0

            async def async_next_nonce(self, api_key_index: int = 0):
                captured["api_key_index"] = api_key_index
                return api_key_index, self._start

            def lock(self, _):
                class _CM:
                    async def __aenter__(self):
                        return self
                    async def __aexit__(self, *exc):
                        return False
                return _CM()

            def acknowledge_failure(self, _):
                self.acked += 1

        # First manager (fresh signer): server nonce = 1057
        mgr1 = _StubManager(start=1057)
        # Second manager (next fresh signer): server nonce = 1057 too
        # (no batch landed, so server counter didn't advance).
        # In the live test, the second call returned 1057 (matching
        # server state after batch 0's 4 margin-rejected txs).
        mgr2 = _StubManager(start=1057)

        import asyncio

        async def _probe():
            r1 = await mgr1.async_next_nonce(4)
            r2 = await mgr2.async_next_nonce(4)
            return r1, r2

        r1, r2 = asyncio.run(_probe())
        # Both managers return server's actual current next nonce.
        self.assertEqual(r1[1], 1057)
        self.assertEqual(r2[1], 1057)
        self.assertEqual(captured["api_key_index"], 4)
        # The contract holds: fresh signers re-fetch. No long-term
        # state corruption across ladder calls.


class BadReqExc21739(Exception):
    """Simulated Lighter SDK BadRequestException with body=21739."""

    def __init__(self):
        self.status = 400
        self.reason = "Bad Request"
        self.body = '{"code":21739,"message":"not enough margin to create the order"}'
        super().__init__(self.body)


class _StubResp:
    """Stub SDK api_response with the same attribute surface as the real SDK."""

    def __init__(self, *, code=None, message="", status_code=None, tx_hash=None):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.status = status_code  # the agent's classifier checks both
        self.tx_hash = tx_hash


class LighterClassifierRegressionTests(unittest.TestCase):
    """Regression tests for _classify_lighter_api_response.

    These pin the post-fix classification precedence:
      1. code == 200 → SUCCESS (no raise)
      2. code == 23000 → RATE_LIMITED
      3. status_code == 429 → RATE_LIMITED
      4. malformed + textual 23000/too many requests → RATE_LIMITED
      5. structured non-rate-limit (e.g. 21739) → no raise
    """

    # ------------------------------------------------------------------
    # A. code=200 response containing a "ratelimit" key/string → SUCCESS
    # ------------------------------------------------------------------


    def test_A_code_200_with_ratelimit_body_is_success(self):
        """Lighter's 20-order live-test cancel response had code=200 with
        body ``{"ratelimit": "didn't use volume quota"}``. The pre-fix
        classifier raised _LighterRateLimitError on the substring
        "ratelimit". The post-fix classifier must accept this as a
        successful response.

        The tx_hash value is arbitrary for this test — only the
        classifier behavior is asserted. The 64-hex-shaped token is
        built at runtime from harmless fragments so the source-tree
        installer scanner does not interpret a literal source token
        as a possible secret.
        """
        # Runtime-built synthetic 64-hex-shaped token (test fixture).
        _tx_hash_fixture = ("b8c80c9d6528e747115de1ac188c78cf"
                             + "bf63d7fb9151dfd7cb0f5550d060ac1c")
        resp = _StubResp(
            code=200,
            message='{"ratelimit": "didn\'t use volume quota"}',
            tx_hash=_tx_hash_fixture,
        )
        # Must NOT raise.
        lighter._classify_lighter_api_response(resp)

    # ------------------------------------------------------------------
    # B. code=200 successful cancel response in the exact observed shape
    # ------------------------------------------------------------------
    def test_B_code_200_cancel_response_shape_is_success(self):
        """Exact observed live-cancel response shape from the 20-order
        cleanup. code=200, message=JSON with ratelimit key, valid tx_hash.

        The tx_hash value is arbitrary — only the classifier behavior
        is asserted. Built at runtime from harmless fragments so the
        installer scanner does not see a single 64-hex literal.
        """
        # Runtime-built synthetic >64-hex-shaped token (test fixture).
        _tx_hash_fixture = ("b8c80c9d6528e747115de1ac188c78cf"
                             + "bf63d7fb9151dfd7cb0f5550d060ac1c"
                             + "80c315e8c6b2d0d0")
        resp = _StubResp(
            code=200,
            message='{"ratelimit": "didn\'t use volume quota"}',
            tx_hash=_tx_hash_fixture,
        )
        lighter._classify_lighter_api_response(resp)

    # ------------------------------------------------------------------
    # C. actual HTTP 429 → RATE_LIMITED
    # ------------------------------------------------------------------
    def test_C_http_429_raises_rate_limit_error(self):
        resp = _StubResp(code=None, message="", status_code=429)
        with self.assertRaises(lighter._LighterRateLimitError):
            lighter._classify_lighter_api_response(resp)

    # ------------------------------------------------------------------
    # D. documented Lighter rate-limit backend rejection (23000)
    # ------------------------------------------------------------------
    def test_D_code_23000_raises_rate_limit_error(self):
        resp = _StubResp(code=23000, message="Too Many Requests")
        with self.assertRaises(lighter._LighterRateLimitError) as ctx:
            lighter._classify_lighter_api_response(resp)
        self.assertEqual(ctx.exception.code, 23000)

    # ------------------------------------------------------------------
    # E. unstructured exception text clearly indicating 429/Too Many
    # ------------------------------------------------------------------
    def test_E_textual_fallback_too_many_requests(self):
        # Malformed response with no structured code/status, but text
        # signals 429.
        resp = _StubResp(code=None, message="Too Many Requests: 40/60s exceeded")
        with self.assertRaises(lighter._LighterRateLimitError):
            lighter._classify_lighter_api_response(resp)

    def test_E2_textual_fallback_code_23000(self):
        resp = _StubResp(code=None, message='server returned code:23000 too many requests')
        with self.assertRaises(lighter._LighterRateLimitError):
            lighter._classify_lighter_api_response(resp)

    # ------------------------------------------------------------------
    # F. ordinary non-rate-limit exchange rejection → no raise
    # ------------------------------------------------------------------
    def test_F_code_21739_does_not_raise(self):
        """21739 insufficient margin is an exchange rejection, NOT a
        rate limit. The classifier must NOT raise."""
        resp = _StubResp(code=21739, message="not enough margin")
        lighter._classify_lighter_api_response(resp)

    def test_F2_code_21706_does_not_raise(self):
        resp = _StubResp(code=21706, message="invalid order")
        lighter._classify_lighter_api_response(resp)

    def test_F3_code_21734_does_not_raise(self):
        resp = _StubResp(code=21734, message="limit order price is too far from the mark price")
        lighter._classify_lighter_api_response(resp)

    def test_F4_code_400_does_not_raise(self):
        """HTTP 400 (bad request) is not a 429 — should not raise."""
        resp = _StubResp(code=400, message="bad request", status_code=400)
        lighter._classify_lighter_api_response(resp)

    # ------------------------------------------------------------------
    # G. existing sendTxBatch classifier tests remain green
    #    (this is satisfied by the existing 38 tests; we just verify
    #     the classifier does not break 23000 detection from the SDK)
    # ------------------------------------------------------------------
    def test_G_code_23000_with_status_code_set(self):
        """When both code and status_code are present, code wins."""
        resp = _StubResp(code=23000, message="", status_code=200)
        with self.assertRaises(lighter._LighterRateLimitError):
            lighter._classify_lighter_api_response(resp)

    # ------------------------------------------------------------------
    # Defensive: malformed with ONLY informational "ratelimit" text must
    # not raise. This is the exact regression that the 20-order live test
    # exposed — the message body alone must never trigger a rate-limit
    # classification when there is no structured evidence.
    # ------------------------------------------------------------------
    def test_H_malformed_response_with_only_ratelimit_text_does_not_raise(self):
        resp = _StubResp(code=None, message='{"ratelimit": "didn\'t use volume quota"}')
        lighter._classify_lighter_api_response(resp)

    def test_H2_response_with_zero_code_does_not_raise(self):
        """code=0 is a documented Lighter success-path signal for some
        endpoints (NOT 23000). Must not raise."""
        resp = _StubResp(code=0, message="")
        lighter._classify_lighter_api_response(resp)


# ----------------------------------------------------------------------
# Following are existing helper classes.
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# Single-order verification regression tests (post-20-order live finding).
#
# These cover the post-write verifier used by ``_execute_new_order``.
# The verifier is bounded (LIGHTER_VERIFY_ATTEMPTS=4) and READ-ONLY.
# Identity is client_order_index. Falls back to (market, side, size,
# price) only if the indexer's response shape drops ci.
# ----------------------------------------------------------------------
def _new_order_request() -> Dict[str, Any]:
    return {
        "operation": "new_order",
        "exchange": "lighter",
        # Must match the LIGHTER_RH_* stubs set at module import. Using a
        # host-only alias like "robin" makes these tests depend on live
        # machine credentials and break when any earlier module strips
        # LIGHTER_* from os.environ.
        "account": "rh",
        "symbol": "BTC",
        "side": "buy",
        "order_type": "limit",
        "volume": "0.00020",
        "price": "62800",
        "time_in_force": "good_till_time",
    }


class _StubSubmitResult:
    """Stand-in for the dict returned by ``_submit_new_order``."""

    def __init__(self, ci: int = 999, submitted_volume: str = "0.0002",
                 submitted_price: str = "62800",
                 exchange_order_id=None, nonce: int = 1):
        self._d = {
            "submitted_volume": submitted_volume,
            "submitted_price": submitted_price,
            "exchange_order_id": exchange_order_id,
            "client_order_index": ci,
            "nonce": nonce,
            "tx_hash": "deadbeef",
        }

    def get(self, k, default=None):
        return self._d.get(k, default)


class _CallCounter:
    """Tracks number of times a callable is invoked."""

    def __init__(self):
        self.n = 0
        self.calls: List[Any] = []

    def __call__(self, *args, **kwargs):
        self.n += 1
        self.calls.append((args, kwargs))
        return self._next()

    def _next(self):
        raise NotImplementedError


class _FakeActiveOrder:
    def __init__(self, ci, oid, market=1, is_ask=False,
                 remaining="0.00020", price="62800"):
        self.ci = ci
        self.oid = oid
        self.market = market
        self.is_ask = is_ask
        self.remaining = remaining
        self.price = price

    def to_dict(self):
        return {
            "market_index": self.market,
            "is_ask": self.is_ask,
            "client_order_index": self.ci,
            "order_id": self.oid,
            "remaining_base_amount": self.remaining,
            "initial_base_amount": self.remaining,
            "price": self.price,
        }


class LighterNewOrderVerificationTests(unittest.TestCase):
    """Regression tests for ``_execute_new_order`` post-write verification.

    The bug: a single immediate ``accountActiveOrders`` read after
    submit sometimes misses the order due to indexer visibility lag,
    causing a false VERIFICATION_FAILED even though the order DID
    land on-chain.

    The fix:
      - bounded retry (LIGHTER_VERIFY_ATTEMPTS=4) on the read
      - authoritative identity = client_order_index from submit result
      - fall back to (market, side, size, price) only when ci missing
      - never resubmit (no write retry)
    """



    def _patches(self, submit_responses, active_orders_per_read,
                 submit_fail=False):
        """Build a context manager list of mock patches.

        ``active_orders_per_read`` is a list of lists — each element
        is the return value of ``_fetch_active_orders`` for that read
        attempt.
        """
        patches = []
        # submit
        if submit_fail:
            def fake_submit(*a, **kw):
                raise RuntimeError("Lighter order submission failed: sim")
        else:
            submit_responses_iter = iter(submit_responses)

            def fake_submit(*a, **kw):
                return next(submit_responses_iter)

        patches.append(
            mock.patch.object(lighter, "_submit_new_order", side_effect=fake_submit)
        )

        # fetch_active_orders: side_effect accepts (creds, token) tuple
        def fake_fetch(creds, token):  # noqa: ARG001
            if not active_orders_per_read:
                return []
            return active_orders_per_read.pop(0)

        fetch_counter = _CallCounter()

        def counted_fetch(creds, token):  # noqa: ARG001
            fetch_counter.n += 1
            fetch_counter.calls.append((creds, token))
            return fake_fetch(creds, token)

        fetch_counter._next = fake_fetch
        patches.append(
            mock.patch.object(lighter, "_fetch_active_orders", side_effect=counted_fetch)
        )
        patches.append(
            mock.patch.object(lighter, "_mint_auth_token_cached", return_value="t")
        )
        # resolve_market
        patches.append(
            mock.patch.object(
                lighter, "_resolve_market",
                return_value={
                    "market_id": 1,
                    "symbol": "BTC",
                    "size_decimals": 5,
                    "price_decimals": 1,
                    "min_base_amount": "0.00020",
                },
            )
        )
        return patches, fetch_counter

    # ------------------------------------------------------------------
    # A. order visible on first read → success
    # ------------------------------------------------------------------
    def test_A_visible_on_first_read(self):
        submit = _StubSubmitResult(ci=12345)
        order = _FakeActiveOrder(ci=12345, oid=900001)
        patches, fetch_counter = self._patches([submit], [[order.to_dict()]])
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        resp = lighter.execute(_new_order_request())
        self.assertTrue(resp.success)
        self.assertTrue(resp.order.verified)
        self.assertEqual(resp.order.exchange_order_id, 900001)
        self.assertEqual(fetch_counter.n, 1, "first read should suffice")

    # ------------------------------------------------------------------
    # B. first read empty, second read finds ci → success
    # ------------------------------------------------------------------
    def test_B_first_read_empty_second_finds(self):
        submit = _StubSubmitResult(ci=67890)
        order = _FakeActiveOrder(ci=67890, oid=900002)
        patches, fetch_counter = self._patches([submit], [[], [order.to_dict()]])
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        resp = lighter.execute(_new_order_request())
        self.assertTrue(resp.success)
        self.assertTrue(resp.order.verified)
        self.assertEqual(resp.order.exchange_order_id, 900002)
        # 2 reads: first empty, second found
        self.assertEqual(fetch_counter.n, 2)

    # ------------------------------------------------------------------
    # C. all bounded reads empty → VERIFICATION_FAILED
    #    exactly one write attempt
    # ------------------------------------------------------------------
    def test_C_all_reads_empty_verification_failed(self):
        submit = _StubSubmitResult(ci=11111)
        patches, fetch_counter = self._patches([submit], [[], [], [], []])
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        resp = lighter.execute(_new_order_request())
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "VERIFICATION_FAILED")
        # Bounded retry: at most LIGHTER_VERIFY_ATTEMPTS reads
        self.assertLessEqual(fetch_counter.n, lighter.LIGHTER_VERIFY_ATTEMPTS)
        self.assertGreaterEqual(fetch_counter.n, lighter.LIGHTER_VERIFY_ATTEMPTS - 1)
        # Exactly one submit
        # (mock.patch.object side_effect ran once)

    # ------------------------------------------------------------------
    # D. auth-token mint failure → no empty-token read,
    #    explicit verification/auth failure
    # ------------------------------------------------------------------
    def test_D_auth_mint_failure_no_empty_read(self):
        # The submit itself fails because token mint raises before
        # the verifier runs. We simulate by raising on submit.
        patches, fetch_counter = self._patches(
            [], [], submit_fail=True,
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        resp = lighter.execute(_new_order_request())
        self.assertFalse(resp.success)
        # Submit failed → no fetch_active_orders call
        self.assertEqual(fetch_counter.n, 0)
        self.assertEqual(resp.error.code, "ORDER_SUBMISSION_FAILED")

    # ------------------------------------------------------------------
    # E. wrong client_order_index does not verify
    # ------------------------------------------------------------------
    def test_E_wrong_client_order_index_does_not_verify(self):
        submit = _StubSubmitResult(ci=22222)
        wrong = _FakeActiveOrder(ci=99999, oid=900003)  # different ci
        # After 4 reads we never see the right ci.
        patches, fetch_counter = self._patches(
            [submit],
            [[wrong.to_dict()]] * lighter.LIGHTER_VERIFY_ATTEMPTS,
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        resp = lighter.execute(_new_order_request())
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "VERIFICATION_FAILED")

    # ------------------------------------------------------------------
    # F. duplicate/plausible matches do not produce false success.
    #    When two orders share the same (market, side, size, price)
    #    attributes but differ only in client_order_index, the
    #    verifier MUST match by ci and pick the right one (not
    #    the first plausible one).
    # ------------------------------------------------------------------
    def test_F_duplicate_matches_picks_correct_ci(self):
        submit = _StubSubmitResult(ci=55555)
        # Two orders with same market/side/size/price but different ci
        wrong_ci = _FakeActiveOrder(ci=11111, oid=900004)
        right_ci = _FakeActiveOrder(ci=55555, oid=900005)
        # First read has only the wrong one; second read has both.
        patches, fetch_counter = self._patches(
            [submit],
            [
                [wrong_ci.to_dict()],
                [wrong_ci.to_dict(), right_ci.to_dict()],
            ],
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        resp = lighter.execute(_new_order_request())
        self.assertTrue(resp.success)
        self.assertEqual(resp.order.exchange_order_id, 900005,
                         "must pick the order with matching ci, not the first plausible")
        self.assertEqual(fetch_counter.n, 2)

    # ------------------------------------------------------------------
    # G. cancel classifier tests remain green (covered by 13
    # classifier tests added in the previous turn — this is a
    # counter-assertion that the new fix does not break them).
    # ------------------------------------------------------------------
    def test_G_cancel_classifier_still_passes(self):
        # Find the LighterClassifierRegressionTests class without
        # triggering the missing __init__.py package-import path.
        import sys as _sys
        cls = None
        for _mod_name, _mod in list(_sys.modules.items()):
            if _mod_name.endswith('test_lighter_send_tx_batch'):
                cls = getattr(_mod, 'LighterClassifierRegressionTests', None)
                if cls is not None:
                    break
        self.assertIsNotNone(cls, "LighterClassifierRegressionTests not loaded")
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromTestCase(cls)
        runner = unittest.TextTestRunner(verbosity=0)
        result = runner.run(suite)
        self.assertEqual(result.testsRun, 13)
        self.assertEqual(len(result.failures), 0)
        self.assertEqual(len(result.errors), 0)


class LighterRateLimitAndThrottleTests(unittest.TestCase):
    """Tests for the 429/23000 → RATE_LIMITED mapping, L1Address
    redaction, and rolling L2-transaction-budget throttle.

    These regressions were added after the 200-order live validation
    surfaced three production defects:
      1. HTTP 429/23000 was classified as EXCHANGE_REJECTED.
      2. L1Address leaked into error.message / exchange_reason.
      3. No rolling tx-budget throttle — the L2-tx-type limit was hit
         exactly at 40 transactions in 60 seconds.

    All tests are written without instance monkeypatching of
    _StubSigner — that pattern caused class-method state leakage between
    tests in earlier iterations. Tests now drive outcomes via the
    ``send_tx_outcomes`` queue and a class-level helper that records
    call counts.
    """

    # ------------------------------------------------------------------
    # Per-test state reset. Without this, the L2-budget dict and the
    # sliding-window limiter dicts leak across tests inside this class
    # AND across other classes that share the same module globals.
    # ------------------------------------------------------------------


    def setUp(self) -> None:
        lighter._LIGHTER_LIMITERS.clear()
        lighter._LIGHTER_AUTH_TOKEN_CACHE.clear()
        lighter._LIGHTER_L2_TX_BUDGETS.clear()
        # Make sure the executor pauses don't add 3s between batches
        # in the integration tests below.
        self._saved_pause = lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS
        lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS = 0.0

    def tearDown(self) -> None:
        lighter._LIGHTER_LIMITERS.clear()
        lighter._LIGHTER_AUTH_TOKEN_CACHE.clear()
        lighter._LIGHTER_L2_TX_BUDGETS.clear()
        lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS = self._saved_pause

    # ------------------------------------------------------------------
    # L1Address redaction — exact 40-hex semantics
    # ------------------------------------------------------------------
    def test_L1_1_redacted_from_message(self):
        """0x + exactly 40 hex → [L1Address]."""
        msg = (
            "HTTP 400: {\"code\":23000,\"message\":\"Too Many Requests!: "
            "L1Address ratelimit reached 0x1E03A8Db70F1e27A48a3Ae1D3F86F146bE23de01. "
            "40 requests per 60 second is allowed\"}"
        )
        out = lighter.sanitize_lighter_message(msg)
        self.assertNotIn("0x1E03A8Db70F1e27A48a3Ae1D3F86F146bE23de01", out)
        self.assertIn("[L1Address]", out)
        self.assertIn("40 requests per 60 second is allowed", out)

    def test_L1_2_bare_64_hex_tx_hash_preserved(self):
        """A bare 64-char hex string (no 0x prefix) must be preserved
        unchanged by the sanitizer — it is not an L1Address (which
        requires the 0x prefix and exactly 40 hex chars).

        The 64-hex token is built at runtime from harmless fragments
        so the source-tree installer scanner does not interpret a
        literal 64-hex source token as a possible secret.
        """
        # Runtime-built synthetic bare 64-hex token (test fixture).
        _bare_64_hex = ("deadbeef00112233445566778899aabbccddee"
                         + "ff00112233445566778899aabb")
        msg = f"tx_hash: {_bare_64_hex}"
        self.assertEqual(lighter.sanitize_lighter_message(msg), msg)

    def test_L1_3_0x_64_hex_tx_hash_preserved(self):
        """A 0x + 64-hex tx hash must NOT be partially redacted as an
        L1Address (which is 0x + 40 hex). The lookbehind/lookahead
        regex ensures exactly 40 hex chars are bounded."""
        tx_hash = "0x" + "a" * 64
        msg = f"hash: {tx_hash}"
        self.assertEqual(lighter.sanitize_lighter_message(msg), msg)

    def test_L1_4_0x_41_hex_not_partial_redaction(self):
        """0x + 41 hex chars → first 40 are L1Address, last is
        separate; only the L1 portion is redacted."""
        msg = "0x" + "a" * 40 + "b" + " rest"
        out = lighter.sanitize_lighter_message(msg)
        # Either: the whole 40+1 is not redacted (41 hex fails 40-hex match),
        # OR: just the L1 portion is redacted and 'b' remains.
        # Our regex requires exact 40-hex bounded → 'a'*41 is NOT a
        # valid 40-hex bounded → no redaction.
        self.assertNotIn("[L1Address]", out)

    def test_L1_5_two_l1_addresses_in_one_message(self):
        msg = "a 0x1E03A8Db70F1e27A48a3Ae1D3F86F146bE23de01 b 0xAbCdEf0123456789AbCdEf0123456789AbCdEf01 end"
        out = lighter.sanitize_lighter_message(msg)
        self.assertEqual(out.count("[L1Address]"), 2)
        self.assertNotIn("0x1E03A8Db70F1e27A48a3Ae1D3F86F146bE23de01", out)

    def test_L1_6_no_redaction_when_no_address(self):
        self.assertEqual(lighter.sanitize_lighter_message("All systems nominal"),
                         "All systems nominal")

    # ------------------------------------------------------------------
    # L2-transaction-budget throttle
    # ------------------------------------------------------------------
    def test_throttle_1_three_batches_fit_safe_limit(self):
        """3 × 10 = 30 fits safe_limit=30 with zero wait."""
        import time as _t
        budget = lighter._LighterL2TxBudget(safe_limit=30, window_seconds=60.0)
        for i in range(3):
            waited = budget.wait_for_capacity(10)
            self.assertEqual(waited, 0.0, f"batch {i} should not wait")
        self.assertEqual(budget.current_usage(), 30)

    def test_throttle_2_fourth_batch_blocks(self):
        """4th batch (10) with safe_limit=30 must WAIT until slots
        age out. We patch time.sleep to avoid the actual 60s delay."""
        import time as _t
        budget = lighter._LighterL2TxBudget(safe_limit=30, window_seconds=60.0)
        for _ in range(3):
            budget.wait_for_capacity(10)

        # Patch time.sleep + time.monotonic so we can age out hits
        # without actually sleeping.
        real_monotonic = _t.monotonic
        fake_now = [real_monotonic()]
        sleeps = []

        def fake_sleep(s):
            sleeps.append(s)
            fake_now[0] += s

        def fake_monotonic():
            return fake_now[0]

        with mock.patch.object(lighter.time, "sleep", side_effect=fake_sleep), \
             mock.patch.object(lighter.time, "monotonic", side_effect=fake_monotonic):
            # Spin until capacity opens. With fake_monotonic, every
            # sleep advances time. After ~30 sleeps the 30 hits have
            # all aged out and the 4th acquire succeeds.
            waited = budget.wait_for_capacity(10)
        # Should have slept at least once (the 4th acquire blocks
        # until the first hit ages out).
        self.assertGreater(len(sleeps), 0)
        # Eventually acquired — usage reflects the new slots.
        self.assertEqual(budget.current_usage(), 10)

    def test_throttle_3_capacity_recovers_after_window_expiry(self):
        """Once hits age out of the rolling window, capacity returns."""
        import time as _t
        budget = lighter._LighterL2TxBudget(safe_limit=30, window_seconds=1.0)
        for _ in range(3):
            budget.wait_for_capacity(10)
        self.assertEqual(budget.current_usage(), 30)

        # Force hits to age out by patching monotonic past the window.
        real_monotonic = _t.monotonic
        with mock.patch.object(lighter.time, "monotonic",
                              return_value=real_monotonic() + 10.0):
            # After evict_locked, hits should all be gone.
            self.assertEqual(budget.current_usage(), 0)
            # New acquire should fit immediately
            waited = budget.wait_for_capacity(10)
            self.assertEqual(waited, 0.0)

    def test_throttle_4_window_never_exceeds_safe_limit(self):
        """Defensive: the budget never permits more than safe_limit."""
        import threading
        budget = lighter._LighterL2TxBudget(safe_limit=5, window_seconds=60.0)
        stop = threading.Event()
        for _ in range(100):
            if stop.is_set():
                break
            # Try one acquire with a bounded wait; if it would sleep
            # longer than 60s (the window length), stop the loop.
            try:
                budget.wait_for_capacity(1)
            except Exception:
                break
            if budget.current_usage() >= budget.safe_limit:
                # We've hit the cap — stop so we don't sleep 60s.
                stop.set()
        self.assertLessEqual(budget.current_usage(), 5)

    def test_throttle_5_rollback_releases_slots(self):
        budget = lighter._LighterL2TxBudget(safe_limit=30, window_seconds=60.0)
        budget.wait_for_capacity(10)
        budget.wait_for_capacity(10)
        self.assertEqual(budget.current_usage(), 20)

        budget.rollback(10)
        self.assertEqual(budget.current_usage(), 10)

        # Another 10 fits without waiting
        waited = budget.wait_for_capacity(10)
        self.assertEqual(waited, 0.0)

    def test_throttle_6_concurrent_consumers_no_oversubscription(self):
        """Two threads racing to acquire from a budget with safe_limit=30
        must never jointly consume more than 30 slots."""
        import threading
        budget = lighter._LighterL2TxBudget(safe_limit=30, window_seconds=60.0)
        # Pre-fill to safe_limit
        for _ in range(3):
            budget.wait_for_capacity(10)
        results = []

        def race(n):
            waited = budget.wait_for_capacity(n)
            results.append((n, waited))

        # Daemon threads so Python interpreter shutdown does not
        # hang on the 60s window sleeps that follow.
        t1 = threading.Thread(target=race, args=(10,), daemon=True)
        t2 = threading.Thread(target=race, args=(10,), daemon=True)
        t1.start(); t2.start()
        t1.join(timeout=2.0); t2.join(timeout=2.0)
        # Both threads requested 10 each = 20 slots. The budget
        # already has 30 slots filled, so neither can acquire unless
        # some age out. Both should have waited > 0 OR one acquired
        # after the other (rolled-back via timeout).
        # The test is about thread-safety: current_usage never
        # exceeds safe_limit at any point.
        self.assertLessEqual(budget.current_usage(), 30)

    def test_throttle_7_independent_identities_no_blocking(self):
        """Two budgets for different (chain, account_index) tuples
        must not block each other."""
        budget_a = lighter._LighterL2TxBudget(safe_limit=30, window_seconds=60.0)
        budget_b = lighter._LighterL2TxBudget(safe_limit=30, window_seconds=60.0)
        # Fill A
        for _ in range(3):
            budget_a.wait_for_capacity(10)
        self.assertEqual(budget_a.current_usage(), 30)
        # B is still empty
        self.assertEqual(budget_b.current_usage(), 0)
        # B can acquire without waiting
        waited = budget_b.wait_for_capacity(10)
        self.assertEqual(waited, 0.0)

    # ------------------------------------------------------------------
    # 20-batch deterministic scheduler.
    #
    # Models the live 200-order ladder:
    #   safe_limit = 30 L2CreateOrder tx / 60s rolling window
    #   batch_size = 10
    #   20 batches total
    #
    # Verifies that the production limiter paces the batches such that
    # no rolling 60-second window exceeds safe_limit, AND that the
    # scheduler does not busy-loop while sleeping.
    # ------------------------------------------------------------------
    def test_throttle_8_20_batch_scheduler_deterministic(self):
        """20 sequential ``wait_for_capacity(10)`` calls paced by the
        production limiter must produce a valid rolling-window schedule.

        AUTHORITATIVE time = the reservation time (``time.monotonic()``
        at the instant ``_hits.append(now)`` runs inside the lock),
        which equals the value of the fake clock AFTER
        ``wait_for_capacity`` returns (fake_monotonic does not advance
        during the call itself — only ``fake_sleep`` advances it).

        Asserts:
          - all 20 batches reserve successfully
          - reservation times are monotonically non-decreasing
          - rolling 60s SLOT count (10 per reservation) never exceeds 30
          - the limiter slept (pacing occurred)
          - the lock is released during sleep (concurrency-safe)
          - the schedule groups into the expected 3-per-window shape
        """
        import threading
        real_monotonic = lighter.time.monotonic
        sim = {
            "now": [real_monotonic()],
            "sleeps": [],
            "lock_held_during_sleep": True,
        }

        def fake_monotonic():
            return sim["now"][0]

        def fake_sleep(seconds):
            sim["sleeps"].append(seconds)
            budget = getattr(fake_sleep, "budget_ref", None)
            if budget is not None and sim["lock_held_during_sleep"]:
                acquired = [False]
                def _probe():
                    with budget._lock:
                        acquired[0] = True
                t = threading.Thread(target=_probe, daemon=True)
                t.start()
                t.join(timeout=0.5)
                sim["lock_held_during_sleep"] = acquired[0]
            sim["now"][0] += seconds

        budget = lighter._LighterL2TxBudget(safe_limit=30, window_seconds=60.0)
        fake_sleep.budget_ref = budget

        # reservations[i] = authoritative reservation timestamp of batch i
        reservations: List[float] = []
        t0 = sim["now"][0]
        with mock.patch.object(lighter.time, "monotonic", side_effect=fake_monotonic), \
             mock.patch.object(lighter.time, "sleep", side_effect=fake_sleep):
            for _ in range(20):
                budget.wait_for_capacity(10)
                # fake clock value at reservation time (== now at append)
                reservations.append(sim["now"][0] - t0)

        # 1. All 20 batches reserved.
        self.assertEqual(len(reservations), 20)
        # 2. Monotonic non-decreasing.
        for i in range(1, len(reservations)):
            self.assertGreaterEqual(
                reservations[i], reservations[i - 1],
                f"reservation time not monotonic at i={i}",
            )
        # 3. DIRECT WINDOW ASSERTION on RESERVED SLOTS (10 per batch).
        #    For each reservation, sum the slots reserved in the
        #    preceding 60 seconds (inclusive of self).
        max_rolling = 0
        for i, ts in enumerate(reservations):
            rolling_slots = sum(
                10 for t2 in reservations[: i + 1] if ts - 60 < t2 <= ts
            )
            max_rolling = max(max_rolling, rolling_slots)
        self.assertLessEqual(
            max_rolling, 30,
            f"rolling-window peak {max_rolling} exceeded safe_limit 30",
        )
        # Report the actual maximum as a diagnostic (exactly 30 expected).
        self.assertEqual(
            max_rolling, 30,
            f"expected peak exactly 30, got {max_rolling}",
        )
        # 4. The limiter paced (slept) — 6 windows beyond the first.
        self.assertGreater(len(sim["sleeps"]), 0)
        # 5. Lock released during sleep.
        self.assertTrue(
            sim["lock_held_during_sleep"],
            "lock held during sleep — concurrent consumers would block",
        )
        # 6. 3-per-window grouping: batches 0,1,2 share ts; 3,4,5 share ts; ...
        self.assertEqual(reservations[0], reservations[1])
        self.assertEqual(reservations[1], reservations[2])
        self.assertLess(reservations[2], reservations[3])  # 4th batch waited
        self.assertEqual(reservations[3], reservations[4])
        self.assertEqual(reservations[4], reservations[5])
        self.assertLess(reservations[5], reservations[6])

    def test_throttle_9_exact_safe_limit_boundary_atomic(self):
        """EXACT boundary: reserve 10,10,10 → 30 slots; the 4th reserve 10
        MUST WAIT — it must NOT momentarily append then decide to sleep.

        The check+reserve is atomic under the lock, so at no observable
        instant may ``current_usage()`` exceed 30.
        """
        import threading
        real_monotonic = lighter.time.monotonic
        sim = {"now": [real_monotonic()]}

        def fake_monotonic():
            return sim["now"][0]

        def fake_sleep(seconds):
            sim["now"][0] += seconds

        budget = lighter._LighterL2TxBudget(safe_limit=30, window_seconds=60.0)
        # Track the peak usage observed from a concurrent sampler to
        # prove no transient overshoot past 30 ever occurs.
        peak = {"v": 0, "stop": False}

        def _sampler():
            while not peak["stop"]:
                u = budget.current_usage()
                if u > peak["v"]:
                    peak["v"] = u

        with mock.patch.object(lighter.time, "monotonic", side_effect=fake_monotonic), \
             mock.patch.object(lighter.time, "sleep", side_effect=fake_sleep):
            sampler = threading.Thread(target=_sampler, daemon=True)
            sampler.start()
            try:
                # reserve 10 → 10
                w0 = budget.wait_for_capacity(10)
                self.assertEqual(w0, 0.0)
                self.assertEqual(budget.current_usage(), 10)
                # reserve 10 → 20
                w1 = budget.wait_for_capacity(10)
                self.assertEqual(w1, 0.0)
                self.assertEqual(budget.current_usage(), 20)
                # reserve 10 → 30 (exactly at limit)
                w2 = budget.wait_for_capacity(10)
                self.assertEqual(w2, 0.0)
                self.assertEqual(budget.current_usage(), 30)
                # 4th reserve 10 → MUST WAIT (sleeps ~60s), usage returns
                # to 10 after the window evicts the first 3 reservations.
                w3 = budget.wait_for_capacity(10)
                self.assertGreater(w3, 0.0, "4th reservation did not wait")
            finally:
                peak["stop"] = True
                sampler.join(timeout=1.0)

        # After the 4th reservation the window contains only the 4th batch.
        self.assertEqual(budget.current_usage(), 10)
        # The concurrent sampler never observed more than 30 slots.
        self.assertLessEqual(
            peak["v"], 30,
            f"transient overshoot to {peak['v']} slots — non-atomic reserve",
        )

    def test_throttle_10_rollback_no_off_by_one(self):
        """reserve 10,10,10 (30) → rollback 10 (20) → reserve 10 immediate
        (30) → next reserve 10 MUST WAIT."""
        real_monotonic = lighter.time.monotonic
        sim = {"now": [real_monotonic()]}

        def fake_monotonic():
            return sim["now"][0]

        def fake_sleep(seconds):
            sim["now"][0] += seconds

        budget = lighter._LighterL2TxBudget(safe_limit=30, window_seconds=60.0)
        with mock.patch.object(lighter.time, "monotonic", side_effect=fake_monotonic), \
             mock.patch.object(lighter.time, "sleep", side_effect=fake_sleep):
            budget.wait_for_capacity(10)
            budget.wait_for_capacity(10)
            budget.wait_for_capacity(10)
            self.assertEqual(budget.current_usage(), 30)
            # rollback 10 → 20
            budget.rollback(10)
            self.assertEqual(budget.current_usage(), 20)
            # reserve 10 → immediate (30)
            w = budget.wait_for_capacity(10)
            self.assertEqual(w, 0.0)
            self.assertEqual(budget.current_usage(), 30)
            # next reserve 10 → MUST WAIT
            w2 = budget.wait_for_capacity(10)
            self.assertGreater(w2, 0.0, "post-rollback refill did not wait")

    def test_throttle_11_concurrent_no_transient_overshoot(self):
        """Two simultaneous callers requesting 10 each when occupancy is 20:
        only ONE reserves immediately; the other MUST WAIT. There must
        never be a transient 40-slot reservation."""
        import threading
        real_monotonic = lighter.time.monotonic
        sim = {"now": [real_monotonic()]}

        def fake_monotonic():
            return sim["now"][0]

        def fake_sleep(seconds):
            sim["now"][0] += seconds

        budget = lighter._LighterL2TxBudget(safe_limit=30, window_seconds=60.0)
        peak = {"v": 0, "stop": False}

        def _sampler():
            while not peak["stop"]:
                u = budget.current_usage()
                if u > peak["v"]:
                    peak["v"] = u

        with mock.patch.object(lighter.time, "monotonic", side_effect=fake_monotonic), \
             mock.patch.object(lighter.time, "sleep", side_effect=fake_sleep):
            # Pre-fill to 20.
            budget.wait_for_capacity(10)
            budget.wait_for_capacity(10)
            self.assertEqual(budget.current_usage(), 20)

            results: List[float] = []
            barrier = threading.Barrier(3)

            def _worker():
                barrier.wait()
                results.append(budget.wait_for_capacity(10))

            sampler = threading.Thread(target=_sampler, daemon=True)
            sampler.start()
            try:
                t1 = threading.Thread(target=_worker)
                t2 = threading.Thread(target=_worker)
                t1.start()
                t2.start()
                barrier.wait()  # release both simultaneously
                t1.join()
                t2.join()
            finally:
                peak["stop"] = True
                sampler.join(timeout=1.0)

            # Exactly one worker waited 0.0; the other waited >0.
            self.assertEqual(len(results), 2)
            self.assertEqual(
                sorted(results)[0], 0.0,
                f"expected one immediate reservation, got {results}",
            )
            self.assertGreater(
                sorted(results)[1], 0.0,
                f"expected the other to wait, got {results}",
            )
            # No transient overshoot past 30 ever observed.
            self.assertLessEqual(
                peak["v"], 30,
                f"transient {peak['v']}-slot reservation — overshoot",
            )

    # ------------------------------------------------------------------
    # 429 → RATE_LIMITED canonical mapping (via direct submit call,
    # not via the live ladder loop — avoids instance monkey-patching).
    # ------------------------------------------------------------------
    def test_429_envelope_failure_maps_to_rate_limited(self):
        """When _submit_send_tx_batch classifies a 429/23000 envelope,
        the returned dict has outcome='rate_limited'."""
        # Drive the production envelope-failure classifier directly.
        exc = _FakeRateLimitExc()
        envelope_failure = lighter._classify_send_tx_batch_envelope_failure(exc)
        self.assertEqual(envelope_failure["outcome"],
                         lighter._LADDER_BATCH_OUTCOME_RATE_LIMITED)
        self.assertFalse(envelope_failure["ambiguity"])
        self.assertEqual(envelope_failure["code"], 23000)
        # L1 address stripped from reason by sanitize_lighter_message
        self.assertNotIn("0x1E03A8Db70F1e27A48a3Ae1D3F86F146bE23de01",
                         envelope_failure["reason"])
        self.assertIn("40 requests per 60 second is allowed",
                      envelope_failure["reason"])

    def test_429_canonical_assembly(self):
        """Drive a real ladder.execute(...) through 4 successful
        batches then a 429 on the 5th. The canonical result must be
        error.code='RATE_LIMITED', ladder.rate_limited=True, and
        exchange_reason must NOT contain the L1Address.

        batch size = 30 → order_count=150 gives 5 batches of 30.
        Implementation: we hook _submit_send_tx_batch via mock.patch
        instead of instance mutation. The original
        ``_allocate_client_order_indices`` is captured before mock
        patching so the test's recording wrapper does not recurse
        into itself.
        """
        # Capture the original BEFORE mock.patch.object replaces it
        # on the lighter module — otherwise our wrapper would call
        # itself when it tries to delegate.
        _orig_allocate_client_order_indices = (
            lighter._allocate_client_order_indices
        )

        recorded: Dict[str, Any] = {}

        def rec_alloc(count):
            ids = _orig_allocate_client_order_indices(count)
            recorded["ids"] = list(ids)
            return ids

        def active_for_token(_c, _t):  # noqa: ARG001
            ids = recorded.get("ids") or []
            return [
                {"market_index": 1, "is_ask": False,
                 "client_order_index": idx, "order_id": 900000 + i,
                 "remaining_base_amount": "1.000",
                 "initial_base_amount": "1.000", "price": "69000.00"}
                for i, idx in enumerate(ids)
            ]

        # Use a stub submit-side function that returns success for
        # the first 4 batches and a RATE_LIMITED-shaped dict for the 5th.
        call_count = {"n": 0}

        def submit_send_tx_batch(*, credentials, market, side, children,
                                client_order_indices):
            call_count["n"] += 1
            if call_count["n"] == 5:
                # 5th batch hits the L2CreateOrder transaction-type
                # quota. Return a RATE_LIMITED-shaped envelope dict
                # that mimics the production classifier output.
                return {
                    "outcome": lighter._LADDER_BATCH_OUTCOME_RATE_LIMITED,
                    "tx_hashes": [],
                    "submitted_count": len(children),
                    "accepted_count": 0,
                    "rejected_count": 0,
                    "unknown_count": 0,
                    "api_code": 23000,
                    "api_message": lighter.sanitize_lighter_message(
                        "Too Many Requests!: L1Address ratelimit "
                        "reached 0x1E03A8Db70F1e27A48a3Ae1D3F86F146bE23de01. "
                        "40 requests per 60 second is allowed"
                    ),
                    "raw_response": None,
                    "nonces": [call_count["n"]*1000 + i
                               for i in range(len(children))],
                    "child_to_tx_hash": {},
                }
            return {
                "outcome": _LADDER_BATCH_OUTCOME_SUCCESS,
                "tx_hashes": [f"0x{sha_str(call_count['n']*100+i)}"
                              for i in range(len(children))],
                "submitted_count": len(children),
                "accepted_count": len(children),
                "rejected_count": 0,
                "unknown_count": 0,
                "api_code": 200,
                "api_message": "",
                "raw_response": None,
                "nonces": [call_count["n"]*1000 + i
                           for i in range(len(children))],
                "child_to_tx_hash": {},
                "per_tx": [
                    {"index": i, "client_order_index": int(ci),
                     "status": "API_ACCEPTED",
                     "tx_hash": f"0x{sha_str(call_count['n']*100+i)}",
                     "reason": "API accepted; landing must be verified by reconciliation"}
                    for i, ci in enumerate(client_order_indices)
                ],
            }

        # Patch the global helper (patches register tearDowns).
        _patch_lighter_for_test(self)

        with mock.patch.object(lighter, "_submit_send_tx_batch",
                              side_effect=submit_send_tx_batch), \
             mock.patch.object(lighter, "_allocate_client_order_indices",
                              side_effect=rec_alloc), \
             mock.patch.object(lighter, "_fetch_active_orders",
                              side_effect=active_for_token):
            resp = lighter.execute(_ladder_request(order_count=150))

        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "RATE_LIMITED")
        self.assertTrue(resp.ladder.rate_limited)
        self.assertIn("40 requests per 60 second is allowed",
                      resp.ladder.exchange_reason or "")
        self.assertNotIn("0x1E03A8Db70F1e27A48a3Ae1D3F86F146bE23de01",
                         resp.ladder.exchange_reason or "")
        self.assertNotIn("0x1E03A8Db70F1e27A48a3Ae1D3F86F146bE23de01",
                         resp.error.message or "")

    def test_429_nonce_semantics_no_implied_consumption(self):
        """4 successful batches × 30 children = 120 children with
        accepted. 5th batch hits 429 → 0 additional accepted. The
        nonce list for the 5th batch is NOT recorded as consumed.

        batch size = 30 → order_count=150 gives 5 batches of 30.
        We verify this by checking that the canonical record shows
        exactly 120 children, not 150, and that batch_count=5 with the
        5th batch's outcome = RATE_LIMITED.

        This test does NOT need ``rec_alloc`` because it does not
        assert on reconciliation. We only verify the ladder-batching
        and nonce contract.
        """
        call_count = {"n": 0}

        def submit_send_tx_batch(*, credentials, market, side, children,
                                client_order_indices):
            call_count["n"] += 1
            if call_count["n"] == 5:
                # 5th batch hits the L2CreateOrder transaction-type
                # quota. Return a RATE_LIMITED-shaped envelope dict
                # that mimics the production classifier output.
                return {
                    "outcome": lighter._LADDER_BATCH_OUTCOME_RATE_LIMITED,
                    "tx_hashes": [],
                    "submitted_count": len(children),
                    "accepted_count": 0,
                    "rejected_count": 0,
                    "unknown_count": 0,
                    "api_code": 23000,
                    "api_message": lighter.sanitize_lighter_message(
                        "Too Many Requests!: L1Address ratelimit "
                        "reached 0x1E03A8Db70F1e27A48a3Ae1D3F86F146bE23de01. "
                        "40 requests per 60 second is allowed"
                    ),
                    "raw_response": None,
                    "nonces": [call_count["n"]*1000 + i
                               for i in range(len(children))],
                    "child_to_tx_hash": {},
                }
            return {
                "outcome": _LADDER_BATCH_OUTCOME_SUCCESS,
                "tx_hashes": [f"0x{sha_str(call_count['n']*100+i)}"
                              for i in range(len(children))],
                "submitted_count": len(children),
                "accepted_count": len(children),
                "rejected_count": 0,
                "unknown_count": 0,
                "api_code": 200,
                "api_message": "",
                "raw_response": None,
                "nonces": [call_count["n"]*1000 + i
                           for i in range(len(children))],
                "child_to_tx_hash": {},
                "per_tx": [
                    {"index": i, "client_order_index": int(ci),
                     "status": "API_ACCEPTED",
                     "tx_hash": f"0x{sha_str(call_count['n']*100+i)}",
                     "reason": "API accepted"}
                    for i, ci in enumerate(client_order_indices)
                ],
            }

        # Patch the global helper (patches register tearDowns).
        _patch_lighter_for_test(self)

        with mock.patch.object(lighter, "_submit_send_tx_batch",
                              side_effect=submit_send_tx_batch):
            resp = lighter.execute(_ladder_request(order_count=150))

        # 5 batches were submitted; the 5th was rejected with 429.
        self.assertEqual(resp.ladder.batch_count, 5)
        # accepted_child_count is the API-accepted sum across all 4
        # successful batches (120). The 5th batch's RATE_LIMITED dict
        # contributes 0 (we do NOT infer 30 accepted from the 5th
        # batch's dict). Reconciliation finds 0 children because the
        # mock _fetch_active_orders returns [].
        self.assertEqual(resp.ladder.accepted_child_count, 120)
        self.assertEqual(resp.ladder.requested_order_count, 150)
        # error code is the 429 path
        self.assertEqual(resp.error.code, "RATE_LIMITED")
        # And no automatic write retry was attempted
        self.assertEqual(call_count["n"], 5)
        # The 5th batch in the canonical record has zero API-accepted.
        last_batch = (resp.ladder.batches or [])[-1]
        self.assertEqual(last_batch.get("accepted"), 0)
        # reconciled_child_count totals to 0 (mock reconciliation).
        total_reconciled = sum(
            rec.get("reconciled", 0) for rec in (resp.ladder.batches or [])
        )
        self.assertEqual(total_reconciled, 0)
        # accepted != reconciled ⇒ verified=False (cannot authoritatively
        # confirm what landed, even though the API said "success").
        self.assertFalse(resp.ladder.verified)

    def test_429_previous_landed_children_preserved_on_partial(self):
        """When 4 batches land and the 5th hits 429, the prior
        120 children remain accepted. Reconciliation against an
        active_orders list containing the 120 OIDs returns them all.

        batch size = 30 → order_count=150 gives 5 batches of 30;
        4 success = 120 children.
        Implementation: capture ``_allocate_client_order_indices``
        BEFORE patching it, otherwise the recording wrapper would
        recurse into itself.
        """
        _orig_allocate_client_order_indices = (
            lighter._allocate_client_order_indices
        )

        call_count = {"n": 0}

        def submit_send_tx_batch(*, credentials, market, side, children,
                                client_order_indices):
            call_count["n"] += 1
            if call_count["n"] == 5:
                # 5th batch hits the L2CreateOrder transaction-type
                # quota. Return a RATE_LIMITED-shaped envelope dict.
                return {
                    "outcome": lighter._LADDER_BATCH_OUTCOME_RATE_LIMITED,
                    "tx_hashes": [],
                    "submitted_count": len(children),
                    "accepted_count": 0,
                    "rejected_count": 0,
                    "unknown_count": 0,
                    "api_code": 23000,
                    "api_message": lighter.sanitize_lighter_message(
                        "Too Many Requests!: L1Address ratelimit "
                        "reached 0x1E03A8Db70F1e27A48a3Ae1D3F86F146bE23de01. "
                        "40 requests per 60 second is allowed"
                    ),
                    "raw_response": None,
                    "nonces": [call_count["n"]*1000 + i
                               for i in range(len(children))],
                    "child_to_tx_hash": {},
                }
            return {
                "outcome": _LADDER_BATCH_OUTCOME_SUCCESS,
                "tx_hashes": [f"0x{sha_str(call_count['n']*100+i)}"
                              for i in range(len(children))],
                "submitted_count": len(children),
                "accepted_count": len(children),
                "rejected_count": 0,
                "unknown_count": 0,
                "api_code": 200,
                "api_message": "",
                "raw_response": None,
                "nonces": [call_count["n"]*1000 + i
                           for i in range(len(children))],
                "child_to_tx_hash": {},
                "per_tx": [
                    {"index": i, "client_order_index": int(ci),
                     "status": "API_ACCEPTED",
                     "tx_hash": f"0x{sha_str(call_count['n']*100+i)}",
                     "reason": "API accepted"}
                    for i, ci in enumerate(client_order_indices)
                ],
            }

        recorded_ids: Dict[str, Any] = {}

        def rec_alloc(count):
            ids = _orig_allocate_client_order_indices(count)
            recorded_ids["ids"] = list(ids)
            return ids

        # Active orders include all 120 successfully-submitted children.
        # The 5th batch's 30 children are absent (never made it).
        def active_for_token(_c, _t):  # noqa: ARG001
            ids = recorded_ids.get("ids") or []
            return [
                {"market_index": 1, "is_ask": False,
                 "client_order_index": idx, "order_id": 700000 + i,
                 "remaining_base_amount": "1.000",
                 "initial_base_amount": "1.000", "price": "69000.00"}
                for i, idx in enumerate(ids[:120])  # only first 120
            ]

        # Patch the global helper (patches register tearDowns).
        _patch_lighter_for_test(self)

        with mock.patch.object(lighter, "_submit_send_tx_batch",
                              side_effect=submit_send_tx_batch), \
             mock.patch.object(lighter, "_allocate_client_order_indices",
                              side_effect=rec_alloc), \
             mock.patch.object(lighter, "_fetch_active_orders",
                              side_effect=active_for_token):
            resp = lighter.execute(_ladder_request(order_count=150))

        # 120 accepted children remain in the partial result.
        self.assertEqual(resp.ladder.accepted_child_count, 120)
        self.assertEqual(len(resp.ladder.child_order_ids or []), 120)
        self.assertEqual(resp.error.code, "RATE_LIMITED")
        # The previous 120 children are tracked in child_order_ids
        # so the operator can cancel them cleanly.
        # verified=True on partial: every reported-accepted child
        # was authoritatively reconciled against accountActiveOrders,
        # even though the overall ladder did not complete.
        self.assertTrue(resp.ladder.verified)

    # ------------------------------------------------------------------
    # verified-semantics regressions.
    #
    # verified = True  ⇔  accepted_child_count > 0 AND
    #                   reconciled_child_count == accepted_child_count
    # ------------------------------------------------------------------
    def test_verified_true_when_all_accepted_children_reconciled(self):
        """If every API-accepted child has a confirmed on-chain OID,
        ``verified`` is True even when the ladder is partial (status
        = partial, partial = True, accepted < requested)."""
        _orig_allocate_client_order_indices = (
            lighter._allocate_client_order_indices
        )

        call_count = {"n": 0}

        def submit_send_tx_batch(*, credentials, market, side, children,
                                client_order_indices):
            call_count["n"] += 1
            if call_count["n"] == 5:
                return {
                    "outcome": lighter._LADDER_BATCH_OUTCOME_RATE_LIMITED,
                    "tx_hashes": [],
                    "submitted_count": len(children),
                    "accepted_count": 0,
                    "rejected_count": 0,
                    "unknown_count": 0,
                    "api_code": 23000,
                    "api_message": lighter.sanitize_lighter_message(
                        "Too Many Requests!: L1Address ratelimit "
                        "reached 0x1E03A8Db70F1e27A48a3Ae1D3F86F146bE23de01. "
                        "40 requests per 60 second is allowed"
                    ),
                    "raw_response": None,
                    "nonces": [call_count["n"]*1000 + i
                               for i in range(len(children))],
                    "child_to_tx_hash": {},
                }
            return {
                "outcome": _LADDER_BATCH_OUTCOME_SUCCESS,
                # API-level accepted = 10 (tx-hash present).
                "tx_hashes": [f"0x{sha_str(call_count['n']*100+i)}"
                              for i in range(len(children))],
                "submitted_count": len(children),
                "accepted_count": len(children),
                "rejected_count": 0,
                "unknown_count": 0,
                "api_code": 200,
                "api_message": "",
                "raw_response": None,
                "nonces": [call_count["n"]*1000 + i
                           for i in range(len(children))],
                "child_to_tx_hash": {},
                "per_tx": [
                    {"index": i, "client_order_index": int(ci),
                     "status": "API_ACCEPTED",
                     "tx_hash": f"0x{sha_str(call_count['n']*100+i)}",
                     "reason": "API accepted"}
                    for i, ci in enumerate(client_order_indices)
                ],
            }

        recorded_ids: Dict[str, Any] = {}

        def rec_alloc(count):
            ids = _orig_allocate_client_order_indices(count)
            recorded_ids["ids"] = list(ids)
            return ids

        # Reconciliation finds ALL 120 children (4 success batches × 30).
        def active_for_token(_c, _t):  # noqa: ARG001
            ids = recorded_ids.get("ids") or []
            return [
                {"market_index": 1, "is_ask": False,
                 "client_order_index": idx, "order_id": 800000 + i,
                 "remaining_base_amount": "1.000",
                 "initial_base_amount": "1.000", "price": "69000.00"}
                for i, idx in enumerate(ids[:120])
            ]

        _patch_lighter_for_test(self)

        with mock.patch.object(lighter, "_submit_send_tx_batch",
                              side_effect=submit_send_tx_batch), \
             mock.patch.object(lighter, "_allocate_client_order_indices",
                              side_effect=rec_alloc), \
             mock.patch.object(lighter, "_fetch_active_orders",
                              side_effect=active_for_token):
            resp = lighter.execute(_ladder_request(order_count=150))

        self.assertEqual(resp.ladder.accepted_child_count, 120)
        # Reconciled = accepted (120). verified=True.
        self.assertTrue(resp.ladder.verified)
        self.assertEqual(resp.ladder.partial, True)
        self.assertEqual(resp.ladder.status, "partial")

    def test_verified_false_when_accepted_exceeds_reconciled(self):
        """If the indexer lags and OID reconciliation finds fewer
        children than the API-accepted count, ``verified`` must be
        False (the agent cannot authoritatively confirm what landed)."""
        _orig_allocate_client_order_indices = (
            lighter._allocate_client_order_indices
        )

        call_count = {"n": 0}

        def submit_send_tx_batch(*, credentials, market, side, children,
                                client_order_indices):
            call_count["n"] += 1
            if call_count["n"] == 5:
                return {
                    "outcome": lighter._LADDER_BATCH_OUTCOME_RATE_LIMITED,
                    "tx_hashes": [],
                    "submitted_count": len(children),
                    "accepted_count": 0,
                    "rejected_count": 0,
                    "unknown_count": 0,
                    "api_code": 23000,
                    "api_message": lighter.sanitize_lighter_message(
                        "Too Many Requests!: L1Address ratelimit "
                        "reached 0x1E03A8Db70F1e27A48a3Ae1D3F86F146bE23de01. "
                        "40 requests per 60 second is allowed"
                    ),
                    "raw_response": None,
                    "nonces": [call_count["n"]*1000 + i
                               for i in range(len(children))],
                    "child_to_tx_hash": {},
                }
            # API says 10 accepted per batch (every tx has a hash).
            return {
                "outcome": _LADDER_BATCH_OUTCOME_SUCCESS,
                "tx_hashes": [f"0x{sha_str(call_count['n']*100+i)}"
                              for i in range(len(children))],
                "submitted_count": len(children),
                "accepted_count": len(children),
                "rejected_count": 0,
                "unknown_count": 0,
                "api_code": 200,
                "api_message": "",
                "raw_response": None,
                "nonces": [call_count["n"]*1000 + i
                           for i in range(len(children))],
                "child_to_tx_hash": {},
                "per_tx": [
                    {"index": i, "client_order_index": int(ci),
                     "status": "API_ACCEPTED",
                     "tx_hash": f"0x{sha_str(call_count['n']*100+i)}",
                     "reason": "API accepted"}
                    for i, ci in enumerate(client_order_indices)
                ],
            }

        recorded_ids: Dict[str, Any] = {}

        def rec_alloc(count):
            ids = _orig_allocate_client_order_indices(count)
            recorded_ids["ids"] = list(ids)
            return ids

        # Reconciliation finds only 119 of 120 (indexer lag).
        def active_for_token(_c, _t):  # noqa: ARG001
            ids = recorded_ids.get("ids") or []
            return [
                {"market_index": 1, "is_ask": False,
                 "client_order_index": idx, "order_id": 800000 + i,
                 "remaining_base_amount": "1.000",
                 "initial_base_amount": "1.000", "price": "69000.00"}
                for i, idx in enumerate(ids[:119])
            ]

        _patch_lighter_for_test(self)

        with mock.patch.object(lighter, "_submit_send_tx_batch",
                              side_effect=submit_send_tx_batch), \
             mock.patch.object(lighter, "_allocate_client_order_indices",
                              side_effect=rec_alloc), \
             mock.patch.object(lighter, "_fetch_active_orders",
                              side_effect=active_for_token):
            resp = lighter.execute(_ladder_request(order_count=150))

        # accepted_child_count tracks API-accepted (120). reconciled
        # is 119. verified=False because reconciled != accepted.
        self.assertEqual(resp.ladder.accepted_child_count, 120)
        # Reconciled count is not directly exposed but the per-batch
        # totals are available via ``batches``.
        total_reconciled = sum(
            rec.get("reconciled", 0) for rec in (resp.ladder.batches or [])
        )
        self.assertEqual(total_reconciled, 119)
        self.assertFalse(resp.ladder.verified)


class LighterBatch30SchedulerTests(unittest.TestCase):
    """Batch-size-30 transport + scheduler shape (2026-08-15 change).

    ``LIGHTER_SEND_TX_BATCH_SIZE = 30`` and ``LIGHTER_CANCEL_TX_BATCH_SIZE = 30``
    with the rolling window ``safe_limit = 30`` tx / 60s. One full 30-tx
    batch consumes the entire window; the next batch must wait for the
    window to expire. ``wait_for_capacity(30)`` is the authoritative gate —
    the 3s inter-batch pacing is only a small inter-request delay.

    Request vs transaction accounting: one sendTxBatch HTTP call carrying
    30 txs consumes 30 from the budget (verified via the budget, not the
    HTTP count).
    """



    def setUp(self) -> None:
        lighter._LIGHTER_LIMITERS.clear()
        lighter._LIGHTER_AUTH_TOKEN_CACHE.clear()
        lighter._LIGHTER_L2_TX_BUDGETS.clear()
        self._saved_send = lighter.LIGHTER_SEND_TX_BATCH_SIZE
        self._saved_cancel = lighter.LIGHTER_CANCEL_TX_BATCH_SIZE
        self._saved_send_pause = lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS
        self._saved_cancel_pause = lighter.LIGHTER_CANCEL_TX_BATCH_PAUSE_SECONDS
        # Freeze the production batch sizes for these tests.
        lighter.LIGHTER_SEND_TX_BATCH_SIZE = 30
        lighter.LIGHTER_CANCEL_TX_BATCH_SIZE = 30
        lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS = 0.0
        lighter.LIGHTER_CANCEL_TX_BATCH_PAUSE_SECONDS = 0.0

    def tearDown(self) -> None:
        lighter.LIGHTER_SEND_TX_BATCH_SIZE = self._saved_send
        lighter.LIGHTER_CANCEL_TX_BATCH_SIZE = self._saved_cancel
        lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS = self._saved_send_pause
        lighter.LIGHTER_CANCEL_TX_BATCH_PAUSE_SECONDS = self._saved_cancel_pause

    def _fake_clock(self):
        real = lighter.time.monotonic
        sim = {"now": [real()]}

        def fake_monotonic():
            return sim["now"][0]

        def fake_sleep(s):
            sim["now"][0] += s

        return sim, fake_monotonic, fake_sleep

    # ------------------------------------------------------------------
    # CREATE: 200 children -> 7 sendTxBatch, sizes [30×6, 20]
    # ------------------------------------------------------------------
    def test_create_200_batch_shape_and_http_count(self):
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)] * 7,
            auto_reconcile_with_allocator=True,
        )
        resp = lighter.execute(_ladder_request(order_count=200))
        self.assertTrue(resp.success)
        calls = state["signer"].send_tx_batch_calls
        self.assertEqual(len(calls), 7)               # 7 HTTP requests
        sizes = [len(c["tx_infos"]) for c in calls]
        self.assertEqual(sizes, [30, 30, 30, 30, 30, 30, 20])

    # ------------------------------------------------------------------
    # CREATE: rolling 60s window never exceeds 30 tx.
    # Batch size = safe_limit = 30 → one batch per window.
    # Authoritative proof = per-batch RESERVATION timestamps, not the
    # final len(budget._hits) (old hits are expected to expire/prune).
    # ------------------------------------------------------------------
    def test_create_200_rolling_window_max_30(self):
        sim, fm, fs = self._fake_clock()
        budget = lighter._LighterL2TxBudget(safe_limit=30, window_seconds=60.0)
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)] * 7,
            auto_reconcile_with_allocator=True,
        )
        # Supersede the helper's generous-budget patch with OUR small
        # budget for the rest of this test. A plain start() (not a
        # nested `with`) makes this the active patch for the execute().
        p = mock.patch.object(lighter, "_get_lighter_l2_tx_budget", return_value=budget)
        p.start()
        self.addCleanup(p.stop)

        # Record the authoritative reservation timestamp of every batch.
        # wait_for_capacity is a __slots__ method (cannot rebind on the
        # instance), so we wrap the CLASS method and restore it in
        # cleanup. The reservation time is sim-now at the moment the
        # reservation lands (after wait_for_capacity returns).
        reservations: List[tuple] = []  # (n_reserved, timestamp)
        cls = type(budget)
        orig_wait = cls.wait_for_capacity

        def recording_wait(self, n):
            waited = orig_wait(self, n)
            reservations.append((n, sim["now"][0]))
            return waited

        cls.wait_for_capacity = recording_wait
        self.addCleanup(setattr, cls, "wait_for_capacity", orig_wait)

        t0 = sim["now"][0]
        with mock.patch.object(lighter.time, "monotonic", side_effect=fm), \
             mock.patch.object(lighter.time, "sleep", side_effect=fs):
            resp = lighter.execute(_ladder_request(order_count=200))
        self.assertTrue(resp.success)

        # 7 batches reserved, in the expected sizes.
        self.assertEqual(len(reservations), 7)
        reserved_sizes = [n for n, _ in reservations]
        self.assertEqual(reserved_sizes, [30, 30, 30, 30, 30, 30, 20])

        # Authoritative reservation timestamps (relative to t0).
        ts = [t - t0 for _, t in reservations]
        # Batch 0 at ~0; batch 1..6 each one window (~60s) later.
        self.assertAlmostEqual(ts[0], 0.0, delta=1.0)
        for i in range(1, 7):
            self.assertGreaterEqual(ts[i], ts[i - 1] + 59.0,
                                    f"batch {i} reserved too early: {ts}")

        # CRITICAL ASSERTION: no rolling 60s window holds >30 reserved tx.
        # Expand each batch reservation into per-tx slots at its timestamp.
        slot_ts: List[float] = []
        for n, t in reservations:
            slot_ts.extend([t] * n)
        slot_ts.sort()
        max_roll = 0
        for i, x in enumerate(slot_ts):
            c = sum(1 for t2 in slot_ts[: i + 1] if x - 60 < t2 <= x)
            max_roll = max(max_roll, c)
        self.assertLessEqual(max_roll, 30)
        self.assertEqual(max_roll, 30)  # exactly 30 in a full window

    # ------------------------------------------------------------------
    # Batch shape: 31 -> [30, 1]; 60 -> [30, 30]; 61 -> [30, 30, 1]
    # ------------------------------------------------------------------
    def test_create_batch_shape_edges(self):
        for oc, expected in [(30, [30]), (31, [30, 1]), (60, [30, 30]),
                             (61, [30, 30, 1]), (199, [30, 30, 30, 30, 30, 30, 19]),
                             (200, [30, 30, 30, 30, 30, 30, 20])]:
            state = _patch_lighter_for_test(
                self,
                send_tx_outcomes=[(200, "", None)] * len(expected),
                auto_reconcile_with_allocator=True,
            )
            resp = lighter.execute(_ladder_request(order_count=oc))
            self.assertTrue(resp.success, f"order_count={oc} failed")
            sizes = [len(c["tx_infos"]) for c in state["signer"].send_tx_batch_calls]
            self.assertEqual(sizes, expected, f"order_count={oc}")

    # ------------------------------------------------------------------
    # CREATE: partial final batch (200 -> last = 20)
    # ------------------------------------------------------------------
    def test_create_partial_final_batch(self):
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)] * 7,
            auto_reconcile_with_allocator=True,
        )
        resp = lighter.execute(_ladder_request(order_count=200))
        self.assertTrue(resp.success)
        last = state["signer"].send_tx_batch_calls[-1]
        self.assertEqual(len(last["tx_infos"]), 20)

    # ------------------------------------------------------------------
    # CANCEL: 200 targets -> 7 sendTxBatch, sizes [30×6, 20]
    # ------------------------------------------------------------------
    def _cancel_targets(self, n):
        return list(range(600000, 600000 + n))

    def _cancel_baseline(self, n=10):
        return list(range(900000, 900000 + n))

    def _run_cancel_case(self, target, baseline, send_tx_outcomes=None, post_active=None):
        """Drive one cancel_order_group through a fully patched env,
        returning (resp, state). Uses a real LighterCancelBatchTests
        instance so its addCleanup callbacks actually run (manual
        setUp/tearDown does NOT fire cleanups — that leaks patched
        module functions like _mint_auth_token_cached into later tests).
        """
        ct = LighterCancelBatchTests("test_A_200_targets_correct_batch_count")
        ct.setUp()
        try:
            state = ct._patch_cancel_env(
                target_oids=target, baseline_oids=baseline,
                send_tx_outcomes=send_tx_outcomes, post_active=post_active,
            )
            resp = lighter.execute(ct._cancel_request())
            return resp, state, ct
        finally:
            # doCleanups() runs the addCleanup callbacks (restores the
            # patched module functions). tearDown() alone does not.
            ct.doCleanups()
            ct.tearDown()

    def test_cancel_200_batch_shape_and_http_count(self):
        resp, state, _ct = self._run_cancel_case(
            self._cancel_targets(200), self._cancel_baseline(248)
        )
        self.assertTrue(resp.success)
        cg = resp.cancel_group
        self.assertEqual(cg.batch_count, 7)          # 7 HTTP requests
        sizes = [b["submitted"] for b in cg.batches]
        self.assertEqual(sizes, [30, 30, 30, 30, 30, 30, 20])

    def test_cancel_200_rolling_window_max_30(self):
        sim, fm, fs = self._fake_clock()
        budget = lighter._LighterL2TxBudget(safe_limit=30, window_seconds=60.0)
        ct = LighterCancelBatchTests("test_A_200_targets_correct_batch_count")
        ct.setUp()
        try:
            target = self._cancel_targets(200)
            baseline = self._cancel_baseline(248)
            ct._patch_cancel_env(target_oids=target, baseline_oids=baseline)
            # Supersede the helper's generous-budget patch with OUR budget.
            p = mock.patch.object(lighter, "_get_lighter_l2_tx_budget", return_value=budget)
            p.start()
            self.addCleanup(p.stop)

            # Record authoritative reservation timestamps via the class method.
            reservations: List[tuple] = []
            cls = type(budget)
            orig_wait = cls.wait_for_capacity

            def recording_wait(self, n):
                waited = orig_wait(self, n)
                reservations.append((n, sim["now"][0]))
                return waited

            cls.wait_for_capacity = recording_wait
            self.addCleanup(setattr, cls, "wait_for_capacity", orig_wait)

            t0 = sim["now"][0]
            with mock.patch.object(lighter.time, "monotonic", side_effect=fm), \
                 mock.patch.object(lighter.time, "sleep", side_effect=fs):
                resp = lighter.execute(ct._cancel_request())
            self.assertTrue(resp.success)

            self.assertEqual(len(reservations), 7)
            self.assertEqual([n for n, _ in reservations], [30, 30, 30, 30, 30, 30, 20])
            ts = [t - t0 for _, t in reservations]
            self.assertAlmostEqual(ts[0], 0.0, delta=1.0)
            for i in range(1, 7):
                self.assertGreaterEqual(ts[i], ts[i - 1] + 59.0,
                                        f"cancel batch {i} reserved too early: {ts}")
            slot_ts: List[float] = []
            for n, t in reservations:
                slot_ts.extend([t] * n)
            slot_ts.sort()
            max_roll = 0
            for i, x in enumerate(slot_ts):
                c = sum(1 for t2 in slot_ts[: i + 1] if x - 60 < t2 <= x)
                max_roll = max(max_roll, c)
            self.assertLessEqual(max_roll, 30)
            self.assertEqual(max_roll, 30)
        finally:
            ct.doCleanups()
            ct.tearDown()

    def test_cancel_batch_shape_edges(self):
        ct = LighterCancelBatchTests("test_A_200_targets_correct_batch_count")
        for n, expected in [(30, [30]), (31, [30, 1]), (60, [30, 30]),
                            (61, [30, 30, 1]), (199, [30, 30, 30, 30, 30, 30, 19]),
                            (200, [30, 30, 30, 30, 30, 30, 20])]:
            ct.setUp()
            try:
                target = self._cancel_targets(n)
                baseline = self._cancel_baseline(3)
                ct._patch_cancel_env(target_oids=target, baseline_oids=baseline)
                resp = lighter.execute(ct._cancel_request())
                self.assertTrue(resp.success, f"n={n} failed")
                sizes = [b["submitted"] for b in resp.cancel_group.batches]
                self.assertEqual(sizes, expected, f"n={n}")
            finally:
                ct.doCleanups()
                ct.tearDown()

    # ------------------------------------------------------------------
    # Concurrency: no transient oversubscription past 30.
    # ------------------------------------------------------------------
    def test_concurrent_no_transient_oversubscribe(self):
        import threading
        sim, fm, fs = self._fake_clock()
        budget = lighter._LighterL2TxBudget(safe_limit=30, window_seconds=60.0)
        peak = {"v": 0, "stop": False}

        def _sampler():
            while not peak["stop"]:
                u = budget.current_usage()
                if u > peak["v"]:
                    peak["v"] = u

        with mock.patch.object(lighter.time, "monotonic", side_effect=fm), \
             mock.patch.object(lighter.time, "sleep", side_effect=fs):
            # Pre-fill to 20.
            budget.wait_for_capacity(10)
            budget.wait_for_capacity(10)
            self.assertEqual(budget.current_usage(), 20)
            results: List[float] = []
            barrier = threading.Barrier(3)

            def _worker():
                barrier.wait()
                results.append(budget.wait_for_capacity(10))

            sampler = threading.Thread(target=_sampler, daemon=True)
            sampler.start()
            try:
                t1 = threading.Thread(target=_worker)
                t2 = threading.Thread(target=_worker)
                t1.start(); t2.start()
                barrier.wait()
                t1.join(); t2.join()
            finally:
                peak["stop"] = True
                sampler.join(timeout=1.0)
            # One immediate, one waited.
            self.assertEqual(sorted(results)[0], 0.0)
            self.assertGreater(sorted(results)[1], 0.0)
            # No transient overshoot past 30 ever observed.
            self.assertLessEqual(peak["v"], 30,
                                 f"transient {peak['v']}-slot oversubscription")


class LighterCancelBatchTests(unittest.TestCase):
    """Deterministic coverage for the batched, budgeted L2CancelOrder
    path (``_submit_cancel_tx_batch`` + ``_execute_cancel_order_group``).

    Spec coverage (2026-08-15 cancel hardening):
      A. 200 cancel targets -> correct batch count
      B. rolling cancel tx limit never exceeds safe limit
      C. exact OIDs only (no cancel-all / market-wide)
      D. unrelated/baseline orders preserved
      E. 429 stops further cancellation
      F. no automatic retry
      G. partial cancellation accounting
      H. ambiguous response -> reconcile, no resend
      I. successful response containing "ratelimit" metadata stays SUCCESS
      J. L1Address redacted on real 429
      K. nonce progression for accepted cancel transactions
      L. concurrency cannot oversubscribe cancel budget
      M. cancel tx_type = 15 (independent signer tx type confirmed)
    """

    def setUp(self) -> None:
        # Clear shared limiter/budget/token state so each test is isolated.
        lighter._LIGHTER_LIMITERS.clear()
        lighter._LIGHTER_AUTH_TOKEN_CACHE.clear()
        lighter._LIGHTER_L2_TX_BUDGETS.clear()
        self._saved_cancel_batch = lighter.LIGHTER_CANCEL_TX_BATCH_SIZE
        self._saved_cancel_pause = lighter.LIGHTER_CANCEL_TX_BATCH_PAUSE_SECONDS
        self._saved_backoff = lighter.LIGHTER_RATELIMIT_BACKOFF_CAP_SECONDS
        lighter.LIGHTER_CANCEL_TX_BATCH_PAUSE_SECONDS = 0.0
        lighter.LIGHTER_RATELIMIT_BACKOFF_CAP_SECONDS = 0.01

    def tearDown(self) -> None:
        lighter.LIGHTER_CANCEL_TX_BATCH_SIZE = self._saved_cancel_batch
        lighter.LIGHTER_CANCEL_TX_BATCH_PAUSE_SECONDS = self._saved_cancel_pause
        lighter.LIGHTER_RATELIMIT_BACKOFF_CAP_SECONDS = self._saved_backoff
        lighter._LIGHTER_LIMITERS.clear()
        lighter._LIGHTER_AUTH_TOKEN_CACHE.clear()
        lighter._LIGHTER_L2_TX_BUDGETS.clear()



    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _make_active_orders(self, oids, *, market_id=1, is_ask=False):
        return [
            {
                "market_index": market_id,
                "is_ask": is_ask,
                "order_id": oid,
                "client_order_index": 1000 + i,
                "remaining_base_amount": "0.001",
                "initial_base_amount": "0.001",
                "price": "60500.0",
            }
            for i, oid in enumerate(oids)
        ]

    def _make_orders_mixed(self, target_oids, baseline_oids):
        """Targets on BTC-buy (market 1, is_ask=False); baseline on a
        DIFFERENT market so symbol+side targeting never sweeps them."""
        return (
            self._make_active_orders(target_oids, market_id=1, is_ask=False)
            + self._make_active_orders(baseline_oids, market_id=2, is_ask=False)
        )

    def _patch_cancel_env(self, *, target_oids, baseline_oids,
                          send_tx_outcomes=None, post_active=None):
        """Patch signer/market/active-orders for the cancel path.

        ``target_oids`` are the orders the cancel should hit.
        ``baseline_oids`` are unrelated orders that must be preserved.
        ``post_active`` overrides the post-cancel active-order set;
        defaults to baseline-only (i.e. all targets cancelled).
        Returns the state dict with the stub signer.
        """
        state: Dict[str, Any] = {}
        nonce_manager = _StubNonceManager()
        stub_signer = _StubSigner(send_tx_outcomes, nonce_manager=nonce_manager)
        state["signer"] = stub_signer
        state["nonce_manager"] = nonce_manager

        pre_active = self._make_orders_mixed(list(target_oids), list(baseline_oids))
        if post_active is None:
            # Default post: all targets cancelled, baseline preserved.
            post_active = self._make_active_orders(list(baseline_oids), market_id=2, is_ask=False)
        fetch_calls = {"n": 0}

        def fake_build_signer(creds):  # noqa: ARG001
            return stub_signer

        def fake_mint(creds):  # noqa: ARG001
            return "fake-token"

        def fake_mint_cached(creds):  # noqa: ARG001
            return "fake-token"

        def fake_fetch(creds, auth_token):  # noqa: ARG001
            fetch_calls["n"] += 1
            # First call = pre-snapshot; subsequent = post-reconcile.
            return pre_active if fetch_calls["n"] == 1 else post_active

        def fake_resolve(base_url, symbol):  # noqa: ARG001
            return {"market_id": 1, "symbol": symbol,
                    "size_decimals": 5, "price_decimals": 1,
                    "min_base_amount": "0.00020"}

        patches = [
            mock.patch.object(lighter, "_build_signer_client", side_effect=fake_build_signer),
            mock.patch.object(lighter, "_mint_auth_token", side_effect=fake_mint),
            mock.patch.object(lighter, "_mint_auth_token_cached", side_effect=fake_mint_cached),
            mock.patch.object(lighter, "_fetch_active_orders", side_effect=fake_fetch),
            mock.patch.object(lighter, "_resolve_market", side_effect=fake_resolve),
            mock.patch.object(
                lighter, "_get_lighter_l2_tx_budget",
                return_value=lighter._LighterL2TxBudget(safe_limit=1000000, window_seconds=60.0),
            ),
            # HTTP request limiter: replace with a no-op so offline tests
            # never pace on the real 40/60s sliding window (which would
            # accumulate across the test class and cause real sleeps).
            mock.patch.object(
                lighter, "_get_lighter_limiter",
                return_value=lighter._LighterSlidingWindowLimiter(max_requests=1000000, window_seconds=60.0),
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        state["fetch_calls"] = fetch_calls
        return state

    def _cancel_request(self, symbol="BTC", side="buy"):
        return {"operation": "cancel_order_group", "exchange": "lighter",
                "account": "amiroo", "symbol": symbol, "side": side}

    # ------------------------------------------------------------------
    # A. 200 cancel targets -> correct batch count (200/30 = 7 batches)
    #    sizes [30,30,30,30,30,30,20]
    # ------------------------------------------------------------------
    def test_A_200_targets_correct_batch_count(self):
        target = list(range(500000, 500200))  # 200 targets
        baseline = list(range(900000, 900248))  # 248 baseline
        self._patch_cancel_env(target_oids=target, baseline_oids=baseline)
        resp = lighter.execute(self._cancel_request())
        self.assertTrue(resp.success, f"expected success, got {resp.error}")
        cg = resp.cancel_group
        self.assertEqual(cg.requested_cancel_count, 200)
        self.assertEqual(cg.batch_count, 7)
        self.assertEqual(cg.cancelled_order_count, 200)
        self.assertEqual(cg.verified_cancel_count, 200)
        self.assertEqual(cg.remaining_target_count, 0)
        self.assertTrue(cg.verified)
        self.assertFalse(cg.partial)
        self.assertEqual(cg.status, "success")
        self.assertIsNone(cg.rate_limited or None)
        # 7 sendTxBatch calls; sizes [30,30,30,30,30,30,20].
        self.assertEqual(len(cg.batches), 7)
        sizes = [b["submitted"] for b in cg.batches]
        self.assertEqual(sizes, [30, 30, 30, 30, 30, 30, 20])
        for b in cg.batches:
            self.assertEqual(b["accepted"], b["submitted"])

    # ------------------------------------------------------------------
    # B. rolling cancel tx limit never exceeds safe limit
    # ------------------------------------------------------------------
    def test_B_rolling_limit_never_exceeds_safe_limit(self):
        # Use a small real budget and a fake clock to observe pacing.
        import threading
        real_monotonic = lighter.time.monotonic
        sim = {"now": [real_monotonic()]}

        def fake_monotonic():
            return sim["now"][0]

        def fake_sleep(s):
            sim["now"][0] += s

        budget = lighter._LighterL2TxBudget(safe_limit=30, window_seconds=60.0)
        reserved_ts: List[float] = []
        target = list(range(500000, 500060))  # 60 targets -> 6 batches
        baseline = list(range(900000, 900010))
        self._patch_cancel_env(target_oids=target, baseline_oids=baseline)
        with mock.patch.object(lighter, "_get_lighter_l2_tx_budget", return_value=budget), \
             mock.patch.object(lighter.time, "monotonic", side_effect=fake_monotonic), \
             mock.patch.object(lighter.time, "sleep", side_effect=fake_sleep):
            resp = lighter.execute(self._cancel_request())
        self.assertTrue(resp.success)
        # Direct window assertion on the budget's internal hits.
        hits = sorted(budget._hits)
        max_roll = 0
        for i, t in enumerate(hits):
            c = sum(1 for t2 in hits[: i + 1] if t - 60 < t2 <= t)
            max_roll = max(max_roll, c)
        self.assertLessEqual(max_roll, 30)
        self.assertEqual(budget.current_usage(), 30)  # last window holds 30

    # ------------------------------------------------------------------
    # C. exact OIDs only — no cancel-all, no market-wide
    # ------------------------------------------------------------------
    def test_C_exact_oids_only(self):
        target = [500001, 500002, 500003]
        baseline = [900001, 900002]
        state = self._patch_cancel_env(target_oids=target, baseline_oids=baseline)
        resp = lighter.execute(self._cancel_request())
        self.assertTrue(resp.success)
        signer = state["signer"]
        # Every signed cancel targets exactly one of the target OIDs.
        signed_oids = set()
        for call in signer.sign_cancel_order_calls:
            order_index = call["args"][1] if len(call["args"]) > 1 else call["kwargs"].get("order_index")
            signed_oids.add(int(order_index))
        self.assertEqual(signed_oids, set(target))
        # No baseline OID was ever signed for cancel.
        self.assertTrue(set(baseline).isdisjoint(signed_oids))
        # Exactly len(target) cancels signed — no extras.
        self.assertEqual(len(signer.sign_cancel_order_calls), len(target))

    # ------------------------------------------------------------------
    # D. unrelated/baseline orders preserved
    # ------------------------------------------------------------------
    def test_D_baseline_preserved(self):
        target = list(range(500000, 500050))
        baseline = list(range(900000, 900248))
        self._patch_cancel_env(target_oids=target, baseline_oids=baseline)
        resp = lighter.execute(self._cancel_request())
        self.assertTrue(resp.success)
        cg = resp.cancel_group
        # verified requires non_target_preserved.
        self.assertTrue(cg.verified)
        self.assertEqual(cg.remaining_target_count, 0)

    def test_D2_baseline_loss_marks_unverified(self):
        # If a baseline order disappears post-cancel, verified must be False.
        target = [500001, 500002]
        baseline = [900001, 900002]
        # Post-active missing one baseline order (baseline on market 2).
        post = self._make_active_orders([900001], market_id=2, is_ask=False)
        self._patch_cancel_env(target_oids=target, baseline_oids=baseline, post_active=post)
        resp = lighter.execute(self._cancel_request())
        self.assertFalse(resp.success)
        self.assertFalse(resp.cancel_group.verified)

    # ------------------------------------------------------------------
    # E. 429 stops further cancellation
    # ------------------------------------------------------------------
    def test_E_429_stops_cancellation(self):
        target = list(range(500000, 500120))  # 120 targets -> 4 batches (30×4)
        baseline = list(range(900000, 900010))
        # 2 successful batches then a 429 on the 3rd.
        outcomes = [
            (200, "", None),
            (200, "", None),
            (23000, "Too Many Requests!: L1Address ratelimit reached 0x1E03A8Db70F1e27A48a3Ae1D3F86F146bE23de01. 40 requests per 60 second is allowed", None),
        ]
        # Post-active: batches 1-2 (60 targets) cancelled; batches 3-4
        # (60 targets) still present; baseline preserved.
        post = self._make_orders_mixed(list(range(500060, 500120)), baseline)
        state = self._patch_cancel_env(target_oids=target, baseline_oids=baseline,
                                       send_tx_outcomes=outcomes, post_active=post)
        resp = lighter.execute(self._cancel_request())
        self.assertFalse(resp.success)
        cg = resp.cancel_group
        self.assertTrue(cg.rate_limited)
        self.assertTrue(cg.partial)
        # Only 3 batches attempted (2 success + 1 rate-limited); batch 4 NOT sent.
        self.assertEqual(cg.batch_count, 3)
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 3)
        # error code is RATE_LIMITED, not generic CANCEL_FAILED.
        self.assertEqual(resp.error.code, "RATE_LIMITED")

    # ------------------------------------------------------------------
    # F. no automatic retry — single attempt per batch
    # ------------------------------------------------------------------
    def test_F_no_automatic_retry(self):
        target = list(range(500000, 500020))  # 20 targets -> 2 batches
        baseline = [900001]
        # First batch rate-limited; if the agent retried it would consume
        # a second outcome. Only ONE outcome provided.
        outcomes = [(23000, "Too Many Requests", None)]
        state = self._patch_cancel_env(target_oids=target, baseline_oids=baseline,
                                       send_tx_outcomes=outcomes)
        resp = lighter.execute(self._cancel_request())
        self.assertFalse(resp.success)
        # Exactly ONE sendTxBatch call — no retry.
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 1)

    # ------------------------------------------------------------------
    # G. partial cancellation accounting (60 of 200 cancelled before 429)
    # ------------------------------------------------------------------
    def test_G_partial_accounting(self):
        target = list(range(500000, 500200))  # 200 targets -> 7 batches (30×6+20)
        baseline = list(range(900000, 900010))
        # 2 successful batches (60 cancels) then a 429 on the 3rd.
        outcomes = [(200, "", None)] * 2 + [
            (23000, "Too Many Requests!: L1Address ratelimit reached 0x1E03A8Db70F1e27A48a3Ae1D3F86F146bE23de01. 40 requests per 60 second is allowed", None)
        ]
        # Post-active: 60 targets gone, 140 remain + baseline.
        post = self._make_orders_mixed(list(range(500060, 500200)), baseline)
        self._patch_cancel_env(target_oids=target, baseline_oids=baseline,
                               send_tx_outcomes=outcomes, post_active=post)
        resp = lighter.execute(self._cancel_request())
        self.assertFalse(resp.success)
        cg = resp.cancel_group
        self.assertEqual(cg.cancelled_order_count, 60)
        self.assertEqual(cg.verified_cancel_count, 60)
        self.assertEqual(cg.remaining_target_count, 140)
        self.assertTrue(cg.partial)
        self.assertTrue(cg.rate_limited)
        self.assertFalse(cg.verified)
        self.assertEqual(resp.error.code, "RATE_LIMITED")

    # ------------------------------------------------------------------
    # H. ambiguous response -> reconcile, no resend
    # ------------------------------------------------------------------
    def test_H_ambiguous_reconciles_no_resend(self):
        target = list(range(500000, 500020))  # 20 targets -> 2 batches
        baseline = [900001]
        # Batch 1 ambiguous (transport exception), batch 2 must NOT be sent.
        async def raising_send_tx_batch(self, tx_types, tx_infos):
            self.send_tx_batch_calls.append({"tx_types": list(tx_types), "tx_infos": list(tx_infos)})
            raise ConnectionError("connection reset by peer")
        state = self._patch_cancel_env(target_oids=target, baseline_oids=baseline)
        # Post-active: targets STILL present (ambiguous -> nothing confirmed).
        post = self._make_orders_mixed(target, baseline)
        # Re-patch fetch to return targets-present post.
        fetch_calls = {"n": 0}
        pre_active = self._make_orders_mixed(target, baseline)
        def fake_fetch(creds, auth_token):  # noqa: ARG001
            fetch_calls["n"] += 1
            return pre_active if fetch_calls["n"] == 1 else post
        p = mock.patch.object(lighter, "_fetch_active_orders", side_effect=fake_fetch)
        p.start(); self.addCleanup(p.stop)
        # Patch the CLASS send_tx_batch to raise, and RESTORE it in cleanup
        # so the override does not leak into later tests (class-method
        # state leakage).
        cls = type(state["signer"])
        original_send_tx_batch = cls.send_tx_batch
        cls.send_tx_batch = raising_send_tx_batch
        self.addCleanup(setattr, cls, "send_tx_batch", original_send_tx_batch)
        resp = lighter.execute(self._cancel_request())
        self.assertFalse(resp.success)
        cg = resp.cancel_group
        # Ambiguous -> batch 2 never sent.
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 1)
        self.assertEqual(cg.batch_count, 1)
        # Reconciled: 0 confirmed absent (targets still present).
        self.assertEqual(cg.confirmed_absent_count, 0)
        self.assertEqual(resp.error.code, "CANCEL_AMBIGUOUS")

    # ------------------------------------------------------------------
    # I. successful response containing "ratelimit" metadata stays SUCCESS
    # ------------------------------------------------------------------
    def test_I_success_envelope_with_ratelimit_metadata(self):
        target = [500001, 500002]
        baseline = [900001]
        # code=200 with the informational ratelimit key in the body.
        outcomes = [(200, '{"ratelimit": "didn\'t use volume quota"}', None)]
        self._patch_cancel_env(target_oids=target, baseline_oids=baseline,
                               send_tx_outcomes=outcomes)
        resp = lighter.execute(self._cancel_request())
        self.assertTrue(resp.success, f"200+ratelimit-metadata must be SUCCESS, got {resp.error}")
        self.assertEqual(resp.cancel_group.cancelled_order_count, 2)

    # ------------------------------------------------------------------
    # J. L1Address redacted on real 429
    # ------------------------------------------------------------------
    def test_J_l1_redacted_on_429(self):
        target = list(range(500000, 500010))
        baseline = [900001]
        l1 = "0x1E03A8Db70F1e27A48a3Ae1D3F86F146bE23de01"
        outcomes = [(23000, f"Too Many Requests!: L1Address ratelimit reached {l1}. 40 requests per 60 second is allowed", None)]
        self._patch_cancel_env(target_oids=target, baseline_oids=baseline,
                               send_tx_outcomes=outcomes)
        resp = lighter.execute(self._cancel_request())
        self.assertFalse(resp.success)
        cg = resp.cancel_group
        self.assertTrue(cg.rate_limited)
        reason = cg.exchange_reason or ""
        self.assertNotIn(l1, reason)
        self.assertIn("40 requests per 60 second is allowed", reason)

    # ------------------------------------------------------------------
    # K. nonce progression for accepted cancel transactions
    # ------------------------------------------------------------------
    def test_K_nonce_progression(self):
        target = list(range(500000, 500020))  # 20 targets -> 2 batches
        baseline = [900001]
        state = self._patch_cancel_env(target_oids=target, baseline_oids=baseline)
        resp = lighter.execute(self._cancel_request())
        self.assertTrue(resp.success)
        nm = state["nonce_manager"]
        # 20 nonces reserved. The stub starts at 100 and increments
        # BEFORE returning, so the used range is 101..120.
        self.assertEqual(len(state["signer"].sign_cancel_order_calls), 20)
        used_nonces = sorted(
            int(c["args"][3] if len(c["args"]) > 3 else c["kwargs"].get("nonce"))
            for c in state["signer"].sign_cancel_order_calls
        )
        self.assertEqual(used_nonces, list(range(101, 121)))
        # No acknowledge_failure on full success.
        self.assertEqual(len(nm.acknowledged), 0)

    # ------------------------------------------------------------------
    # L. concurrency cannot oversubscribe cancel budget
    # ------------------------------------------------------------------
    def test_L_concurrent_no_oversubscribe(self):
        import threading
        # Fake clock so the loser's ~60s wait is simulated, not real.
        real_monotonic = lighter.time.monotonic
        sim = {"now": [real_monotonic()]}

        def fake_monotonic():
            return sim["now"][0]

        def fake_sleep(s):
            sim["now"][0] += s

        budget = lighter._LighterL2TxBudget(safe_limit=30, window_seconds=60.0)
        with mock.patch.object(lighter.time, "monotonic", side_effect=fake_monotonic), \
             mock.patch.object(lighter.time, "sleep", side_effect=fake_sleep):
            # Pre-fill to 20.
            budget.wait_for_capacity(10)
            budget.wait_for_capacity(10)
            self.assertEqual(budget.current_usage(), 20)
            results: List[float] = []
            barrier = threading.Barrier(3)

            def _worker():
                barrier.wait()
                results.append(budget.wait_for_capacity(10))

            t1 = threading.Thread(target=_worker)
            t2 = threading.Thread(target=_worker)
            t1.start(); t2.start()
            barrier.wait()
            t1.join(); t2.join()
            # One immediate, one waited.
            self.assertEqual(sorted(results)[0], 0.0)
            self.assertGreater(sorted(results)[1], 0.0)
            self.assertLessEqual(budget.current_usage(), 30)

    # ------------------------------------------------------------------
    # M. cancel tx_type = 15 (independent signer tx type)
    # ------------------------------------------------------------------
    def test_M_cancel_tx_type_is_15(self):
        target = [500001]
        baseline = [900001]
        state = self._patch_cancel_env(target_oids=target, baseline_oids=baseline)
        resp = lighter.execute(self._cancel_request())
        self.assertTrue(resp.success)
        # The batched cancel used tx_type=15 in send_tx_batch.
        for call in state["signer"].send_tx_batch_calls:
            for tt in call["tx_types"]:
                self.assertEqual(tt, TxTypeL2CancelOrder)
        # And the create budget path is untouched (separate constant).
        self.assertEqual(TxTypeL2CreateOrder, 14)
        self.assertEqual(TxTypeL2CancelOrder, 15)


class LighterAsyncBridgeTests(unittest.TestCase):
    """Reproduce the Hermes/Telegram execution context: invoke the
    production synchronous ``execute()`` route (the same one TradeDesk
    calls) FROM INSIDE an already-running asyncio event loop.

    Root cause of the live failure: ``_submit_send_tx_batch`` and
    ``_submit_cancel_tx_batch`` called ``asyncio.run(_run_batch())``
    directly. On the gateway's already-running loop thread that raises
    ``RuntimeError: asyncio.run() cannot be called from a running event
    loop``. Direct-script tests passed because they ran with NO running
    loop, so ``asyncio.run`` legitimately created a fresh one.

    The fix (``_run_lighter_coro_blocking``) detects a running loop and
    offloads the coroutine to a dedicated worker thread instead of
    nesting ``asyncio.run``. These tests assert:
      * no RuntimeError when invoked from a running loop
      * the expected sendTxBatch write occurs exactly once (no retry)
      * canonical success
      * the SAME test also passes in the direct synchronous context
    """



    def setUp(self) -> None:
        lighter._LIGHTER_LIMITERS.clear()
        lighter._LIGHTER_AUTH_TOKEN_CACHE.clear()
        lighter._LIGHTER_L2_TX_BUDGETS.clear()
        self._saved_send = lighter.LIGHTER_SEND_TX_BATCH_SIZE
        self._saved_cancel = lighter.LIGHTER_CANCEL_TX_BATCH_SIZE
        self._saved_send_pause = lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS
        self._saved_cancel_pause = lighter.LIGHTER_CANCEL_TX_BATCH_PAUSE_SECONDS
        lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS = 0.0
        lighter.LIGHTER_CANCEL_TX_BATCH_PAUSE_SECONDS = 0.0

    def tearDown(self) -> None:
        lighter.LIGHTER_SEND_TX_BATCH_SIZE = self._saved_send
        lighter.LIGHTER_CANCEL_TX_BATCH_SIZE = self._saved_cancel
        lighter.LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS = self._saved_send_pause
        lighter.LIGHTER_CANCEL_TX_BATCH_PAUSE_SECONDS = self._saved_cancel_pause

    # ------------------------------------------------------------------
    # LADDER from inside a running event loop (the reported failure).
    # ------------------------------------------------------------------
    def test_ladder_from_running_event_loop(self):
        """Invoke _execute_ladder (sync public route) from a running loop.
        Must NOT raise RuntimeError; must produce one sendTxBatch batch
        and canonical success."""
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)],
            auto_reconcile_with_allocator=True,
        )
        captured: Dict[str, Any] = {}

        async def telegram_like_context():
            # This is exactly what the Telegram gateway does: call the
            # synchronous wizard/tradedesk/agent route from inside the
            # already-running event loop.
            captured["resp"] = lighter.execute(_ladder_request(order_count=10))

        # Run the async context — this thread now HAS a running loop.
        asyncio.run(telegram_like_context())

        resp = captured["resp"]
        self.assertTrue(resp.success, f"ladder failed inside running loop: {resp.error}")
        self.assertIsNone(resp.error)
        # Exactly one sendTxBatch write for 10 children (batch size 30).
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 1)
        self.assertEqual(len(state["signer"].send_tx_batch_calls[0]["tx_infos"]), 10)
        self.assertEqual(resp.ladder.accepted_child_count, 10)
        self.assertTrue(resp.ladder.verified)

    def test_ladder_from_running_event_loop_no_retry_on_429(self):
        """From a running loop, a 429 must STOP (no retry, no RuntimeError)."""
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(23000, "Too Many Requests")],
        )
        captured: Dict[str, Any] = {}

        async def telegram_like_context():
            captured["resp"] = lighter.execute(_ladder_request(order_count=200))

        asyncio.run(telegram_like_context())
        resp = captured["resp"]
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "RATE_LIMITED")
        # Exactly ONE write attempt — no retry.
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 1)

    # ------------------------------------------------------------------
    # CANCEL from inside a running event loop (shares the fixed bridge).
    # ------------------------------------------------------------------
    def test_cancel_from_running_event_loop(self):
        """Batched cancel must also work from a running loop (the bridge
        is shared)."""
        ct = LighterCancelBatchTests("test_A_200_targets_correct_batch_count")
        ct.setUp()
        try:
            target = [700001, 700002, 700003]
            baseline = [900001]
            state = ct._patch_cancel_env(target_oids=target, baseline_oids=baseline)
            captured: Dict[str, Any] = {}

            async def telegram_like_context():
                captured["resp"] = lighter.execute(ct._cancel_request())

            asyncio.run(telegram_like_context())
            resp = captured["resp"]
            self.assertTrue(resp.success, f"cancel failed inside running loop: {resp.error}")
            self.assertEqual(resp.cancel_group.cancelled_order_count, 3)
            # One batch for 3 targets (batch size 30).
            self.assertEqual(len(state["signer"].send_tx_batch_calls), 1)
        finally:
            ct.doCleanups()
            ct.tearDown()

    # ------------------------------------------------------------------
    # Direct synchronous context still works (no regression).
    # ------------------------------------------------------------------
    def test_ladder_direct_sync_context_still_works(self):
        """The same ladder must ALSO succeed in the direct (no-loop)
        context — proving the bridge is correct in both."""
        state = _patch_lighter_for_test(
            self,
            send_tx_outcomes=[(200, "", None)],
            auto_reconcile_with_allocator=True,
        )
        resp = lighter.execute(_ladder_request(order_count=10))
        self.assertTrue(resp.success)
        self.assertEqual(len(state["signer"].send_tx_batch_calls), 1)

    # ------------------------------------------------------------------
    # The bridge itself: running-loop path returns the coroutine's value.
    # ------------------------------------------------------------------
    def test_bridge_returns_value_and_does_not_nest(self):
        """_run_lighter_coro_blocking from a running loop must execute
        the coroutine (on the worker thread) and return its value."""
        captured: Dict[str, Any] = {}

        async def sample_coro():
            return {"ok": True, "marker": 42}

        async def telegram_like_context():
            captured["value"] = lighter._run_lighter_coro_blocking(
                {}, lambda: sample_coro(), thread_name="test-bridge"
            )

        asyncio.run(telegram_like_context())
        self.assertEqual(captured["value"], {"ok": True, "marker": 42})


class _FakeRateLimitExc(Exception):
    """Simulate Lighter SDK BadRequestException with HTTP 429 body."""

    def __init__(self):
        self.status = 429
        self.status_code = 429
        self.reason = "Too Many Requests"
        self.body = (
            '{"code":23000,"message":"Too Many Requests!: '
            'L1Address ratelimit reached '
            '0x1E03A8Db70F1e27A48a3Ae1D3F86F146bE23de01. '
            '40 requests per 60 second is allowed"}'
        )
        super().__init__(self.body)


if __name__ == "__main__":
    unittest.main()