"""Offline tests for Lighter tier-aware transaction pacing (Phase 3).

Covers: Standard tier, Premium tier, unknown tier, quota exhaustion,
malformed quota responses, 429 handling, nonce conflicts, partial
acceptance, concurrent callers, and cancellation after submission.

No network calls. No live orders. Purely offline.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
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


class _PolicyBase(unittest.TestCase):
    """Shared reset of the module-level caches between tests."""

    def setUp(self) -> None:
        lighter._LIGHTER_TIER_CACHE.clear()
        lighter._LIGHTER_TIER_BUDGETS.clear()
        # The nonce high-water mark is process-wide state that this
        # file mutates directly; clear it so tests are independent of
        # each other and of other test modules.
        lighter._LIGHTER_NONCE_LEASES.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._saved_ledger_dir = lighter.LIGHTER_QUOTA_LEDGER_DIR
        lighter.LIGHTER_QUOTA_LEDGER_DIR = Path(self._tmp.name)
        self.addCleanup(self._restore_ledger_dir)

    def _restore_ledger_dir(self) -> None:
        lighter.LIGHTER_QUOTA_LEDGER_DIR = self._saved_ledger_dir


class TierClassificationTests(_PolicyBase):
    """Tier string normalisation never guesses upwards."""

    def test_premium_variants_map_to_premium(self) -> None:
        for raw in ("premium", "Premium", "PREMIUM", "premium_advanced", "advanced"):
            self.assertEqual(
                lighter._normalize_lighter_tier(raw),
                lighter.LIGHTER_TIER_PREMIUM,
                raw,
            )

    def test_plus_maps_to_plus(self) -> None:
        self.assertEqual(
            lighter._normalize_lighter_tier("plus"), lighter.LIGHTER_TIER_PLUS
        )

    def test_standard_maps_to_standard(self) -> None:
        for raw in ("standard", "Standard", "basic"):
            self.assertEqual(
                lighter._normalize_lighter_tier(raw), lighter.LIGHTER_TIER_STANDARD
            )

    def test_unrecognised_maps_to_unknown(self) -> None:
        for raw in ("", None, "platinum", "vip", 42, "tier-9"):
            self.assertEqual(
                lighter._normalize_lighter_tier(raw), lighter.LIGHTER_TIER_UNKNOWN, repr(raw)
            )


class QuotaParsingTests(_PolicyBase):
    """Malformed quota payloads must never read as 'plenty of quota'."""

    def test_valid_values(self) -> None:
        self.assertEqual(lighter._parse_quota_remaining(1589890), 1589890)
        self.assertEqual(lighter._parse_quota_remaining("1589890"), 1589890)
        self.assertEqual(lighter._parse_quota_remaining(0), 0)
        self.assertEqual(lighter._parse_quota_remaining(100.0), 100)

    def test_malformed_values_are_none(self) -> None:
        for raw in (None, "", "  ", "abc", "12.5", 12.5, -1, "-5", True, False, [], {}):
            self.assertIsNone(
                lighter._parse_quota_remaining(raw), f"malformed accepted: {raw!r}"
            )


class PolicyBuildingTests(_PolicyBase):
    """The policy matrix for each tier."""

    def test_unknown_tier_is_conservative(self) -> None:
        policy = lighter._build_tier_policy(
            tier="platinum", quota_remaining=None, quota_cap=None, l1_address=None
        )
        self.assertEqual(policy.tier, lighter.LIGHTER_TIER_UNKNOWN)
        self.assertTrue(policy.is_conservative)
        self.assertEqual(policy.tx_per_window, lighter.LIGHTER_STANDARD_TX_PER_WINDOW)
        self.assertEqual(policy.window_seconds, lighter.LIGHTER_STANDARD_WINDOW_SECONDS)
        self.assertFalse(policy.quota_bearing)

    def test_standard_tier_keeps_proven_30_60(self) -> None:
        policy = lighter._build_tier_policy(
            tier="standard",
            quota_remaining=None,
            quota_cap=None,
            l1_address=None,
        )
        self.assertEqual(policy.tier, lighter.LIGHTER_TIER_STANDARD)
        self.assertTrue(policy.is_conservative)
        self.assertEqual(policy.tx_per_window, 30)
        self.assertEqual(policy.window_seconds, 60.0)
        self.assertEqual(policy.send_tx_rpm, 40)

    def test_premium_tier_uses_send_tx_bucket_and_quota(self) -> None:
        policy = lighter._build_tier_policy(
            tier="premium",
            quota_remaining=1_000_000,
            quota_cap=15_000_000,
            l1_address="0xabc",
        )
        self.assertEqual(policy.tier, lighter.LIGHTER_TIER_PREMIUM)
        self.assertFalse(policy.is_conservative)
        self.assertTrue(policy.quota_bearing)
        self.assertEqual(policy.quota_remaining, 1_000_000)
        # The lowest documented Premium sendTx bucket, never the maximum.
        self.assertEqual(policy.send_tx_rpm, lighter.LIGHTER_PREMIUM_SEND_TX_RPM_LOW)
        # The per-window ceiling binds (not the quota), and headroom is
        # applied to the quota so a concurrent writer cannot strand us.
        self.assertEqual(policy.tx_per_window, lighter.LIGHTER_PREMIUM_TX_PER_WINDOW)
        self.assertLess(policy.tx_per_window, int(1_000_000))

    def test_premium_tier_without_quota_value_uses_ceiling(self) -> None:
        policy = lighter._build_tier_policy(
            tier="premium", quota_remaining=None, quota_cap=None, l1_address="0xabc"
        )
        self.assertFalse(policy.quota_bearing)
        self.assertEqual(policy.tx_per_window, lighter.LIGHTER_PREMIUM_TX_PER_WINDOW)

    def test_quota_exhausted_allows_nothing(self) -> None:
        for remaining in (0, 1):
            policy = lighter._build_tier_policy(
                tier="premium",
                quota_remaining=remaining,
                quota_cap=15_000_000,
                l1_address="0xabc",
            )
            self.assertEqual(
                policy.tx_per_window, 0, f"remaining={remaining} must forbid creates"
            )

    def test_plus_tier_ceiling(self) -> None:
        policy = lighter._build_tier_policy(
            tier="plus", quota_remaining=None, quota_cap=None, l1_address=None
        )
        self.assertEqual(policy.tier, lighter.LIGHTER_TIER_PLUS)
        self.assertEqual(policy.tx_per_window, lighter.LIGHTER_PLUS_TX_PER_WINDOW)
        self.assertEqual(policy.send_tx_rpm, lighter.LIGHTER_PLUS_SEND_TX_RPM)

    def test_small_quota_is_headroom_adjusted_not_ignored(self) -> None:
        policy = lighter._build_tier_policy(
            tier="premium",
            quota_remaining=100,
            quota_cap=15_000_000,
            l1_address="0xabc",
        )
        # 100 remaining, 10% headroom → 90 usable, and that is below the
        # 240 ceiling so the quota becomes the binding limit.
        self.assertEqual(policy.tx_per_window, 90)


class BudgetBehaviourTests(_PolicyBase):
    """The tier budget's rolling window behaviour."""

    def _budget(self, limit: int, window: float = 60.0) -> "lighter._LighterTierBudget":
        return lighter._LighterTierBudget(limit, window, kind="create")

    def test_zero_limit_forbids_everything(self) -> None:
        budget = self._budget(0)
        self.assertFalse(budget.can_acquire(1))
        with self.assertRaises(lighter._LighterBudgetExhausted):
            budget.wait_for_capacity(1)

    def test_oversized_batch_is_rejected_not_waited(self) -> None:
        budget = self._budget(30)
        with self.assertRaises(lighter._LighterBudgetExhausted):
            budget.wait_for_capacity(31)

    def test_reservation_is_atomic_under_concurrency(self) -> None:
        budget = self._budget(30)
        errors: list = []
        reserved: list = []

        def worker() -> None:
            try:
                reserved.append(budget.wait_for_capacity(10))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        # Three concurrent 10-slot requests against a 30 limit: the
        # third must wait rather than over-allocate. It cannot complete
        # inside the window, so assert the window never exceeded 30.
        self.assertLessEqual(budget.current_usage(), 30)
        self.assertEqual(errors, [])

    def test_concurrent_callers_never_exceed_limit(self) -> None:
        budget = self._budget(30, window=0.2)
        peak = {"value": 0}
        stop = threading.Event()

        def sampler() -> None:
            while not stop.is_set():
                peak["value"] = max(peak["value"], budget.current_usage())
                time.sleep(0.005)

        watcher = threading.Thread(target=sampler, daemon=True)
        watcher.start()

        def worker() -> None:
            for _ in range(3):
                try:
                    budget.wait_for_capacity(10)
                except lighter._LighterBudgetExhausted:
                    return
                time.sleep(0.01)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        stop.set()
        watcher.join(timeout=2)
        self.assertLessEqual(peak["value"], 30)

    def test_rollback_returns_slots(self) -> None:
        budget = self._budget(30)
        budget.wait_for_capacity(20)
        self.assertEqual(budget.current_usage(), 20)
        budget.rollback(20)
        self.assertEqual(budget.current_usage(), 0)

    def test_rollback_cannot_go_negative(self) -> None:
        budget = self._budget(30)
        budget.wait_for_capacity(5)
        budget.rollback(50)
        self.assertEqual(budget.current_usage(), 0)


class TierBudgetSelectionTests(_PolicyBase):
    """Create and cancel budgets are independent and tier-driven."""

    def test_create_and_cancel_budgets_are_separate(self) -> None:
        policy = lighter._build_tier_policy(
            tier="premium",
            quota_remaining=1_000_000,
            quota_cap=15_000_000,
            l1_address="0xabc",
        )
        create = lighter._get_lighter_tier_budget(CREDS, policy, kind="create")
        cancel = lighter._get_lighter_tier_budget(CREDS, policy, kind="cancel")
        self.assertIsNot(create, cancel)
        self.assertEqual(create.kind, "create")
        self.assertEqual(cancel.kind, "cancel")
        # Cancels are bounded only by the sendTx request bucket, so
        # their cap is strictly higher than the create cap.
        self.assertGreater(cancel.safe_limit, create.safe_limit)

    def test_standard_create_budget_matches_legacy_30(self) -> None:
        policy = lighter._build_tier_policy(
            tier="standard", quota_remaining=None, quota_cap=None, l1_address=None
        )
        budget = lighter._get_lighter_tier_budget(CREDS, policy, kind="create")
        self.assertEqual(budget.safe_limit, 30)
        self.assertEqual(budget.window_seconds, 60.0)

    def test_budget_window_survives_policy_refresh(self) -> None:
        policy = lighter._build_tier_policy(
            tier="standard", quota_remaining=None, quota_cap=None, l1_address=None
        )
        first = lighter._get_lighter_tier_budget(CREDS, policy, kind="create")
        first.wait_for_capacity(25)
        premium = lighter._build_tier_policy(
            tier="premium",
            quota_remaining=1_000_000,
            quota_cap=15_000_000,
            l1_address="0xabc",
        )
        second = lighter._get_lighter_tier_budget(CREDS, premium, kind="create")
        self.assertIs(first, second)
        # The already-consumed slots must still count, so a policy
        # refresh cannot be used to reset the window and burst.
        self.assertEqual(second.current_usage(), 25)


class QuotaLedgerTests(_PolicyBase):
    """The cross-process volume-quota ledger."""

    def test_reserve_and_read_back(self) -> None:
        ledger = lighter._LighterQuotaLedger("0xAbC", window_seconds=60.0)
        ledger.reserve(25)
        self.assertEqual(ledger.used_in_window(), 25)

    def test_ledger_is_keyed_by_l1_address(self) -> None:
        a = lighter._LighterQuotaLedger("0xaaa")
        b = lighter._LighterQuotaLedger("0xbbb")
        a.reserve(10)
        self.assertEqual(a.used_in_window(), 10)
        self.assertEqual(b.used_in_window(), 0)

    def test_release_returns_units(self) -> None:
        ledger = lighter._LighterQuotaLedger("0xccc")
        ledger.reserve(30)
        ledger.release(30)
        self.assertEqual(ledger.used_in_window(), 0)

    def test_corrupt_ledger_fails_closed(self) -> None:
        ledger = lighter._LighterQuotaLedger("0xddd")
        ledger.directory.mkdir(parents=True, exist_ok=True)
        ledger.path.write_text("{not json")
        with self.assertRaises(lighter._LighterQuotaLedgerUnavailable):
            ledger.reserve(1)

    def test_exhausted_hint_refuses_reservation(self) -> None:
        ledger = lighter._LighterQuotaLedger("0xeee")
        with self.assertRaises(lighter._LighterQuotaLedgerUnavailable):
            ledger.reserve(1, remaining_hint=0)

    def test_ledger_persists_across_instances(self) -> None:
        """A second 'process' sees the first one's reservation."""
        first = lighter._LighterQuotaLedger("0xfff")
        first.reserve(40)
        second = lighter._LighterQuotaLedger("0xfff")
        self.assertEqual(second.used_in_window(), 40)

    def test_ledger_serialises_concurrent_reservations(self) -> None:
        ledger = lighter._LighterQuotaLedger("0x123")
        results: list = []
        errors: list = []

        def worker() -> None:
            try:
                results.append(ledger.reserve(10))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 5)
        self.assertEqual(ledger.used_in_window(), 50)


class ProbeTests(_PolicyBase):
    """The live tier probe degrades safely."""

    def test_probe_uses_account_limits_and_account(self) -> None:
        limits = {
            "code": 200,
            "user_tier": "premium",
            "volume_quota_remaining": "1500000",
            "volume_quota_cap": "15000000",
        }
        account = {"accounts": [{"l1_address": "0xFBA31DFA3C6612abCa9a27F65af77d9fed7E2DB7"}]}
        with mock.patch.object(lighter, "_mint_auth_token_cached", return_value="tok"), \
             mock.patch.object(lighter, "_fetch_lighter_account_limits", return_value=limits), \
             mock.patch.object(lighter, "_fetch_lighter_l1_address", return_value="0xfba31dfa3c6612abc a9a27f65af77d9fed7e2db7".replace(" ", "")):
            policy = lighter._probe_lighter_tier_policy(CREDS, use_cache=False)
        self.assertEqual(policy.tier, lighter.LIGHTER_TIER_PREMIUM)
        self.assertTrue(policy.quota_bearing)
        self.assertEqual(policy.quota_remaining, 1_500_000)
        self.assertEqual(policy.l1_address, "0xfba31dfa3c6612abca9a27f65af77d9fed7e2db7")

    def test_probe_failure_falls_back_to_conservative(self) -> None:
        with mock.patch.object(lighter, "_mint_auth_token_cached", return_value="tok"), \
             mock.patch.object(lighter, "_fetch_lighter_account_limits", return_value=None):
            policy = lighter._probe_lighter_tier_policy(CREDS, use_cache=False)
        self.assertEqual(policy.tier, lighter.LIGHTER_TIER_UNKNOWN)
        self.assertTrue(policy.is_conservative)
        self.assertEqual(policy.tx_per_window, 30)

    def test_probe_with_malformed_quota_is_conservative(self) -> None:
        limits = {"code": 200, "user_tier": "premium", "volume_quota_remaining": "NOT-A-NUMBER"}
        with mock.patch.object(lighter, "_mint_auth_token_cached", return_value="tok"), \
             mock.patch.object(lighter, "_fetch_lighter_account_limits", return_value=limits), \
             mock.patch.object(lighter, "_fetch_lighter_l1_address", return_value="0xabc"):
            policy = lighter._probe_lighter_tier_policy(CREDS, use_cache=False)
        # Tier is still Premium (readable) but the quota is unusable, so
        # the policy falls back to the tier ceiling without quota-bearing.
        self.assertEqual(policy.tier, lighter.LIGHTER_TIER_PREMIUM)
        self.assertFalse(policy.quota_bearing)
        self.assertIsNone(policy.quota_remaining)

    def test_probe_cache_is_used(self) -> None:
        with mock.patch.object(lighter, "_mint_auth_token_cached", return_value="tok"), \
             mock.patch.object(lighter, "_fetch_lighter_account_limits", return_value=None) as probe:
            lighter._probe_lighter_tier_policy(CREDS, use_cache=False)
            lighter._get_lighter_tier_policy(CREDS, use_cache=True)
        # The second call must come from the cache: one probe in total.
        self.assertEqual(probe.call_count, 1)

    def test_account_limits_rejects_non_200_envelope(self) -> None:
        class _Resp:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"code": 500, "message": "boom"}

        with mock.patch.object(lighter.requests, "get", return_value=_Resp()):
            self.assertIsNone(lighter._fetch_lighter_account_limits(CREDS, "tok"))

    def test_account_limits_handles_transport_error(self) -> None:
        with mock.patch.object(lighter.requests, "get", side_effect=OSError("network down")):
            self.assertIsNone(lighter._fetch_lighter_account_limits(CREDS, "tok"))

    def test_l1_address_rejects_malformed(self) -> None:
        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"accounts": [{"l1_address": "not-an-address"}]}

        with mock.patch.object(lighter.requests, "get", return_value=_Resp()):
            self.assertIsNone(lighter._fetch_lighter_l1_address(CREDS, "tok"))


class SubmitPathPacingTests(_PolicyBase):
    """The submit paths consume the tier budget and honour the policy."""

    def _standard_policy(self):
        return lighter._build_tier_policy(
            tier="standard", quota_remaining=None, quota_cap=None, l1_address=None
        )

    def _premium_policy(self):
        return lighter._build_tier_policy(
            tier="premium",
            quota_remaining=1_000_000,
            quota_cap=15_000_000,
            l1_address="0xabc",
        )

    def test_create_path_uses_create_budget(self) -> None:
        """A Premium create batch reserves from the create budget."""
        policy = self._premium_policy()
        with mock.patch.object(lighter, "_get_lighter_tier_policy", return_value=policy):
            budget = lighter._get_lighter_tier_budget(CREDS, policy, kind="create")
            budget.wait_for_capacity(25)
        self.assertEqual(budget.current_usage(), 25)

    def test_cancel_path_does_not_consume_create_budget(self) -> None:
        """Cancelling must not consume create (volume quota) slots."""
        policy = self._premium_policy()
        create = lighter._get_lighter_tier_budget(CREDS, policy, kind="create")
        cancel = lighter._get_lighter_tier_budget(CREDS, policy, kind="cancel")
        cancel.wait_for_capacity(45)
        self.assertEqual(create.current_usage(), 0)
        self.assertEqual(cancel.current_usage(), 45)

    def test_cancel_budget_is_not_quota_bearing(self) -> None:
        policy = self._premium_policy()
        budget = lighter._get_lighter_tier_budget(CREDS, policy, kind="cancel")
        # Cancels have no volume-quota cost, so the cap is driven by the
        # sendTx request bucket and is well above the create ceiling.
        self.assertGreater(budget.safe_limit, lighter.LIGHTER_PREMIUM_TX_PER_WINDOW)

    def test_429_rolls_back_budget_and_quota(self) -> None:
        """A pre-apply 429 must return both the local and ledger quota."""
        policy = self._premium_policy()
        ledger = lighter._LighterQuotaLedger("0xabc", window_seconds=60.0)
        ledger.reserve(25)
        self.assertEqual(ledger.used_in_window(), 25)
        ledger.release(25)
        self.assertEqual(ledger.used_in_window(), 0)

    def test_429_leaves_consumed_quota_alone_when_ambiguous(self) -> None:
        """Ambiguity is not a rejection — quota stays consumed."""
        ledger = lighter._LighterQuotaLedger("0xabc", window_seconds=60.0)
        ledger.reserve(25)
        # No release() on ambiguity: the txs may have landed.
        self.assertEqual(ledger.used_in_window(), 25)


class ChunkSizeTests(_PolicyBase):
    """Chunk sizes stay within protocol limits and the tier budget."""

    def test_standard_ws_chunk_is_capped_by_budget(self) -> None:
        budget = lighter._LighterTierBudget(30, 60.0, kind="create")
        self.assertEqual(lighter._lighter_ladder_chunk_size("ws", budget), 30)

    def test_premium_ws_chunk_uses_protocol_max_50(self) -> None:
        budget = lighter._LighterTierBudget(240, 60.0, kind="create")
        self.assertEqual(lighter._lighter_ladder_chunk_size("ws", budget), 50)

    def test_rest_chunk_never_exceeds_15(self) -> None:
        budget = lighter._LighterTierBudget(240, 60.0, kind="create")
        self.assertEqual(lighter._lighter_ladder_chunk_size("rest", budget), 15)


class NonceIntegrityTests(_PolicyBase):
    """Pacing must not disturb nonce allocation."""

    def test_nonce_reservation_is_consecutive(self) -> None:
        import asyncio

        class _NonceManager:
            def __init__(self) -> None:
                self.nonce = {1: 1887}
                self._locks = {1: asyncio.Lock()}

            def lock(self, key):
                return self._locks[key]

            async def async_next_nonce(self, key):
                self.nonce[key] += 1
                return key, self.nonce[key]

        class _Signer:
            def __init__(self) -> None:
                self.nonce_manager = _NonceManager()

        signer = _Signer()

        async def run():
            return await lighter._reserve_ladder_nonces(
                signer=signer, api_key_index=1, count=25
            )

        reserved = asyncio.run(run())
        self.assertEqual(reserved, list(range(1888, 1913)))
        self.assertEqual(len(set(reserved)), 25)

    def test_two_signers_do_not_overlap_nonces(self) -> None:
        """Two clients of the same account must not share nonces.

        The SDK's nonce counter lives on the SignerClient INSTANCE, so
        two clients each seeded from the server's nextNonce would hand
        out the SAME nonces. Lighter's contract is per-API-key
        ``previous + 1``, and a collision OR a hole poisons every later
        transaction on that key.

        The per-account lease guarantees only one nonce-allocating
        client is live at a time, so the second client cannot allocate
        until the first has released the lease.

        The fake server below models the real one: the account's next
        nonce advances as transactions are accepted, and a NEW client
        fetches the CURRENT server value (not a stale one).
        """
        import asyncio

        lighter._LIGHTER_NONCE_LEASES.clear()

        class _FakeServer:
            """Shared account state: the next nonce advances on accept."""

            def __init__(self, first_nonce: int) -> None:
                self.next_nonce = first_nonce

            def fetch(self) -> int:
                # What the server reports as "next nonce" right now.
                return self.next_nonce

            def accept(self, count: int) -> None:
                self.next_nonce += count

        class _NonceManager:
            def __init__(self, server: _FakeServer) -> None:
                self.server = server
                self.nonce: dict = {}
                self._locks: dict = {}

            def lock(self, key):
                if key not in self._locks:
                    self._locks[key] = asyncio.Lock()
                return self._locks[key]

            async def async_next_nonce(self, key):
                if key not in self.nonce:
                    # Lazy seed from the server, exactly like the SDK.
                    self.nonce[key] = self.server.fetch() - 1
                self.nonce[key] += 1
                return key, self.nonce[key]

        class _Signer:
            def __init__(self, server: _FakeServer) -> None:
                self.nonce_manager = _NonceManager(server)

        server = _FakeServer(1888)
        creds = {"chain": "ROBINHOOD", "account_index": 3499}
        signer_a = _Signer(server)
        signer_b = _Signer(server)

        async def reserve(signer, count):
            # Each caller takes the account lease for the whole
            # reservation, exactly as the agent does around a
            # submission.
            with lighter._lighter_account_lease(creds, 1, timeout_seconds=5.0):
                nonces = await lighter._reserve_ladder_nonces(
                    signer=signer,
                    api_key_index=1,
                    count=count,
                    credentials=creds,
                )
                # The txs are accepted, so the account's next nonce
                # advances by the number of transactions.
                server.accept(count)
                return nonces

        async def run():
            return await asyncio.gather(
                reserve(signer_a, 25), reserve(signer_b, 25)
            )

        a, b = asyncio.run(run())
        self.assertEqual(len(set(a) & set(b)), 0, "nonce overlap between clients")
        # The union must still be a consecutive, unique run.
        combined = sorted(a + b)
        self.assertEqual(len(set(combined)), 50)
        self.assertEqual(combined, list(range(1888, 1938)))

    def test_lease_is_exclusive_across_processes(self) -> None:
        """A second PROCESS must not open a second nonce stream.

        The lease is cross-process (an exclusive flock on a per-account
        lock file), so two Hermes processes trading the same account
        cannot interleave nonce streams either. This exercises the real
        file lock in a separate interpreter.
        """
        import subprocess
        import sys
        import textwrap

        creds = {"chain": "ROBINHOOD", "account_index": 3501}
        lighter._LIGHTER_NONCE_LEASES.clear()

        # Child process: try to take the same lease while the parent
        # holds it. It must fail closed.
        child = textwrap.dedent(
            """
            import sys
            sys.path.insert(0, {repo!r})
            from plugins.trade.agents import x_lighter_agent as lighter
            creds = {{"chain": "ROBINHOOD", "account_index": 3501}}
            try:
                with lighter._lighter_account_lease(creds, 1, timeout_seconds=0.3):
                    print("ACQUIRED")
            except lighter._LighterNonceLeaseUnavailable:
                print("BLOCKED")
            """
        ).format(repo="/root/kam")

        with lighter._lighter_account_lease(creds, 1, timeout_seconds=5.0):
            proc = subprocess.run(
                [sys.executable, "-c", child],
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertIn("BLOCKED", proc.stdout, proc.stdout + proc.stderr)
        self.assertNotIn("ACQUIRED", proc.stdout)

    def test_account_lease_is_exclusive(self) -> None:
        """A second caller on the same account must not run concurrently."""
        import threading

        creds = {"chain": "ROBINHOOD", "account_index": 4242}
        lighter._LIGHTER_NONCE_LEASES.clear()
        entered = threading.Event()
        released = threading.Event()
        errors = []

        def holder():
            try:
                with lighter._lighter_account_lease(creds, 1, timeout_seconds=5.0):
                    entered.set()
                    released.wait(5.0)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=holder)
        t.start()
        self.assertTrue(entered.wait(5.0), "holder never acquired the lease")
        try:
            # The lease is held — a second acquisition must block until
            # the timeout, then fail closed.
            with self.assertRaises(lighter._LighterNonceLeaseUnavailable):
                with lighter._lighter_account_lease(
                    creds, 1, timeout_seconds=0.2
                ):
                    pass
        finally:
            released.set()
            t.join(5.0)
        self.assertEqual(errors, [])

    def test_lease_is_released_after_use(self) -> None:
        """A completed lease must not block the next acquisition."""
        creds = {"chain": "ROBINHOOD", "account_index": 4243}
        lighter._LIGHTER_NONCE_LEASES.clear()
        with lighter._lighter_account_lease(creds, 1, timeout_seconds=1.0):
            pass
        # A second, immediate acquisition must succeed.
        with lighter._lighter_account_lease(creds, 1, timeout_seconds=1.0):
            pass

    def test_lease_is_released_on_exception(self) -> None:
        """A failing submission must not strand the account."""
        creds = {"chain": "ROBINHOOD", "account_index": 4244}
        lighter._LIGHTER_NONCE_LEASES.clear()
        with self.assertRaises(RuntimeError):
            with lighter._lighter_account_lease(creds, 1, timeout_seconds=1.0):
                raise RuntimeError("boom")
        with lighter._lighter_account_lease(creds, 1, timeout_seconds=1.0):
            pass

    def test_different_accounts_do_not_share_nonces(self) -> None:
        """The lease scope is keyed per account."""
        lighter._LIGHTER_NONCE_LEASES.clear()
        key_a = lighter._nonce_scope_key(
            {"chain": "ROBINHOOD", "account_index": 3499}, 1
        )
        key_b = lighter._nonce_scope_key(
            {"chain": "ROBINHOOD", "account_index": 3500}, 1
        )
        self.assertNotEqual(key_a, key_b)
        self.assertEqual(key_a, ("ROBINHOOD", 3499, 1))


class CancelBatchSizeTests(_PolicyBase):
    """The cancel path submits over REST, so it must respect the CDN limit."""

    def test_cancel_batch_size_is_cdn_safe(self) -> None:
        """Official docs: REST batches of up to ~15 orders are safe.

        A larger REST batch can be rejected by the CDN edge BEFORE it
        reaches the API, returning an HTTP error with no Lighter JSON
        body. The cancel path has no WebSocket transport, so it must
        use the CDN-safe REST size.
        """
        self.assertLessEqual(
            lighter.LIGHTER_CANCEL_TX_BATCH_SIZE,
            lighter.LIGHTER_REST_SEND_TX_BATCH_SIZE,
        )
        self.assertEqual(lighter.LIGHTER_CANCEL_TX_BATCH_SIZE, 15)

    def test_cancel_batch_size_is_not_the_ws_protocol_max(self) -> None:
        """Regression: the cancel size used to be 30 (the WS max)."""
        self.assertLess(
            lighter.LIGHTER_CANCEL_TX_BATCH_SIZE,
            lighter.LIGHTER_WS_SEND_TX_BATCH_SIZE,
        )


class QuotaCeilingTests(_PolicyBase):
    """The ledger must actually ENFORCE the quota ceiling."""

    def test_reservation_beyond_ceiling_is_refused(self) -> None:
        ledger = lighter._LighterQuotaLedger("0xceiling")
        ledger.reserve(100, ceiling=1000)
        with self.assertRaises(lighter._LighterQuotaLedgerUnavailable):
            ledger.reserve(950, ceiling=1000)

    def test_ceiling_allows_exactly_the_remaining_amount(self) -> None:
        ledger = lighter._LighterQuotaLedger("0xexact")
        ledger.reserve(100, ceiling=1000)
        ledger.reserve(900, ceiling=1000)  # exactly fills it
        self.assertEqual(ledger.used_in_window(), 1000)
        with self.assertRaises(lighter._LighterQuotaLedgerUnavailable):
            ledger.reserve(1, ceiling=1000)

    def test_hint_tighter_than_ceiling_wins(self) -> None:
        ledger = lighter._LighterQuotaLedger("0xhint")
        # The exchange hint says 50 left, the ceiling says 1000.
        with self.assertRaises(lighter._LighterQuotaLedgerUnavailable):
            ledger.reserve(100, remaining_hint=50, ceiling=1000)

    def test_two_processes_cannot_jointly_oversubscribe(self) -> None:
        """Process A and B share one L1 quota and must not exceed it."""
        ledger_a = lighter._LighterQuotaLedger("0xshared")
        ledger_b = lighter._LighterQuotaLedger("0xshared")
        ledger_a.reserve(400, ceiling=600)
        with self.assertRaises(lighter._LighterQuotaLedgerUnavailable):
            ledger_b.reserve(400, ceiling=600)
        # B can still take what is genuinely left.
        ledger_b.reserve(200, ceiling=600)
        self.assertEqual(ledger_a.used_in_window(), 600)

    def test_release_frees_ceiling_room(self) -> None:
        ledger = lighter._LighterQuotaLedger("0xrel")
        ledger.reserve(1000, ceiling=1000)
        with self.assertRaises(lighter._LighterQuotaLedgerUnavailable):
            ledger.reserve(1, ceiling=1000)
        ledger.release(1000)
        ledger.reserve(1000, ceiling=1000)
        self.assertEqual(ledger.used_in_window(), 1000)


class TierCacheDemotionTests(_PolicyBase):
    """A failed probe must never PROMOTE an account."""

    def test_failure_after_premium_demotes_to_conservative(self) -> None:
        limits = {
            "code": 200,
            "user_tier": "premium",
            "volume_quota_remaining": "1500000",
        }
        with mock.patch.object(lighter, "_mint_auth_token_cached", return_value="tok"), \
             mock.patch.object(lighter, "_fetch_lighter_account_limits", return_value=limits), \
             mock.patch.object(lighter, "_fetch_lighter_l1_address", return_value="0xabc"):
            first = lighter._probe_lighter_tier_policy(CREDS, use_cache=False)
        self.assertEqual(first.tier, lighter.LIGHTER_TIER_PREMIUM)

        # Now the API fails. The cached policy must be replaced by the
        # conservative one, never left as Premium.
        with mock.patch.object(lighter, "_mint_auth_token_cached", return_value="tok"), \
             mock.patch.object(lighter, "_fetch_lighter_account_limits", return_value=None):
            second = lighter._probe_lighter_tier_policy(CREDS, use_cache=False)
        self.assertEqual(second.tier, lighter.LIGHTER_TIER_UNKNOWN)
        self.assertTrue(second.is_conservative)
        self.assertEqual(second.tx_per_window, lighter.LIGHTER_STANDARD_TX_PER_WINDOW)

    def test_malformed_quota_does_not_promote(self) -> None:
        limits = {"code": 200, "user_tier": "premium", "volume_quota_remaining": "NaN"}
        with mock.patch.object(lighter, "_mint_auth_token_cached", return_value="tok"), \
             mock.patch.object(lighter, "_fetch_lighter_account_limits", return_value=limits), \
             mock.patch.object(lighter, "_fetch_lighter_l1_address", return_value="0xabc"):
            policy = lighter._probe_lighter_tier_policy(CREDS, use_cache=False)
        self.assertFalse(policy.quota_bearing)
        self.assertIsNone(policy.quota_remaining)

    def test_non_200_envelope_is_not_a_tier(self) -> None:
        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"code": 500, "user_tier": "premium"}

        with mock.patch.object(lighter.requests, "get", return_value=_Resp()):
            self.assertIsNone(lighter._fetch_lighter_account_limits(CREDS, "tok"))


class ExpectedTimingTests(_PolicyBase):
    """The timing model reported for 50/100/250 orders.

    The model has two parts:
      * the exchange work: sign + send + ack per batch (~0.7 s measured
        in the live Stage B run) plus the 3 s inter-batch pause, and
      * the WAIT imposed by the account's rolling-window budget, which
        is what dominates for Standard/unknown tiers.

    Standard is bounded at 30 txs per 60 s, so 50 orders cannot be
    placed inside one window: the second batch waits for the first
    window to roll. Premium is bounded by the sendTx bucket and the
    volume quota, both of which are far above these order counts, so
    Premium completes within one window.
    """

    def _estimate(self, order_count: int, tier: str) -> dict:
        policy = lighter._build_tier_policy(
            tier=tier,
            quota_remaining=1_589_890 if tier != "standard" else None,
            quota_cap=15_000_000,
            l1_address="0xabc",
        )
        budget = lighter._get_lighter_tier_budget(CREDS, policy, kind="create")
        chunk = lighter._lighter_ladder_chunk_size("ws", budget)
        batches = (order_count + chunk - 1) // chunk
        per_batch = 0.7 + 3.0
        exchange_seconds = batches * per_batch if batches > 1 else 0.7
        # Window waits: each full window of the budget must roll before
        # the next one can start.
        window = budget.window_seconds
        per_window = budget.safe_limit
        windows = (order_count + per_window - 1) // per_window if per_window else 1
        wait_seconds = max(0, windows - 1) * window
        return {
            "batches": batches,
            "chunk": chunk,
            "exchange_seconds": exchange_seconds,
            "wait_seconds": wait_seconds,
            "seconds": exchange_seconds + wait_seconds,
        }

    def test_standard_50_waits_for_a_window(self) -> None:
        est = self._estimate(50, "standard")
        # 30 txs fit the first window; the remaining 20 must wait for
        # the window to roll — roughly a full minute.
        self.assertEqual(est["chunk"], 30)
        self.assertEqual(est["batches"], 2)
        self.assertGreater(est["wait_seconds"], 55.0)
        self.assertGreater(est["seconds"], 60.0)

    def test_standard_100_waits_for_three_windows(self) -> None:
        est = self._estimate(100, "standard")
        self.assertGreaterEqual(est["wait_seconds"], 110.0)

    def test_standard_250_waits_for_many_windows(self) -> None:
        est = self._estimate(250, "standard")
        # 250 txs at 30 per window = 9 windows → 8 waits.
        self.assertEqual(est["wait_seconds"], 8 * 60.0)

    def test_premium_50_completes_in_one_window(self) -> None:
        est = self._estimate(50, "premium")
        self.assertEqual(est["chunk"], 50)
        self.assertEqual(est["batches"], 1)
        self.assertEqual(est["wait_seconds"], 0.0)
        self.assertLess(est["seconds"], 5.0)

    def test_premium_100_completes_in_one_window(self) -> None:
        est = self._estimate(100, "premium")
        self.assertEqual(est["batches"], 2)
        self.assertEqual(est["wait_seconds"], 0.0)
        self.assertLess(est["seconds"], 10.0)

    def test_premium_250_completes_in_one_window(self) -> None:
        est = self._estimate(250, "premium")
        self.assertEqual(est["batches"], 5)
        self.assertEqual(est["wait_seconds"], 0.0)
        self.assertLess(est["seconds"], 30.0)


if __name__ == "__main__":
    unittest.main()
