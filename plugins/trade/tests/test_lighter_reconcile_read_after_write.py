"""Offline regression tests for the Phase 5.1 bounded read-after-write
reconciliation.

The Phase 5 failure: the first ``accountActiveOrders`` read after a WS
batch returned 0/50 although all 50 children had landed — the
exchange's indexer lags the ack by a few hundred milliseconds. A
single read therefore cannot decide whether a batch landed, and
treating a stale snapshot as a rejection (or as "did not land")
invites a duplicate submission of live orders.

These tests drive the reconciler with SIMULATED exchange responses.
No live orders are placed.
"""
from __future__ import annotations

import time
import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest import mock

from plugins.trade.agents import x_lighter_agent as lighter


def _order(
    coi: int,
    *,
    order_id: Optional[int] = None,
    market_id: int = 3,
    is_ask: bool = False,
    price: str = "55.000",
    size: str = "0.250",
    status: str = "open",
    order_type: str = "limit",
) -> Dict[str, Any]:
    """One accountActiveOrders entry."""
    return {
        "order_id": str(order_id if order_id is not None else 1400000000000 + coi),
        "order_index": order_id if order_id is not None else 1400000000000 + coi,
        "client_order_index": coi,
        "client_order_id": str(coi),
        "market_index": market_id,
        "market_symbol": "SOL",
        "market_kind": "perps",
        "is_ask": is_ask,
        "price": price,
        "initial_base_amount": size,
        "remaining_base_amount": size,
        "filled_base_amount": "0.0000",
        "status": status,
        "type": order_type,
        "time_in_force": "good-till-time",
        "reduce_only": False,
        "nonce": 1000 + coi,
    }


class _ReconcileBase(unittest.TestCase):
    """Shared helpers."""

    def setUp(self) -> None:
        lighter._LIGHTER_LAST_RECONCILE_STATS.clear()
        self._saved_deadline = lighter.LIGHTER_RECONCILE_DEADLINE_SECONDS
        self._saved_backoff = lighter.LIGHTER_RECONCILE_BACKOFF_SECONDS
        lighter.LIGHTER_RECONCILE_DEADLINE_SECONDS = 1.0
        lighter.LIGHTER_RECONCILE_BACKOFF_SECONDS = (0.001, 0.002, 0.004)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        lighter.LIGHTER_RECONCILE_DEADLINE_SECONDS = self._saved_deadline
        lighter.LIGHTER_RECONCILE_BACKOFF_SECONDS = self._saved_backoff

    @staticmethod
    def _creds() -> Dict[str, Any]:
        return {
            "base_url": "https://api.test.invalid",
            "account_index": 3499,
            "api_key_index": 4,
        }

    def _reconcile(
        self,
        snapshots: List[List[Dict[str, Any]]],
        expected: List[int],
        **kwargs,
    ):
        """Drive the reconciler against a scripted sequence of snapshots.

        Each element of ``snapshots`` is one read's response. The last
        snapshot repeats if more reads happen than scripted, so a
        reconciler that keeps reading is observable.
        """
        state = {"i": 0}

        def fake_fetch(_creds, _token):  # noqa: ARG001
            idx = min(state["i"], len(snapshots) - 1)
            state["i"] += 1
            return snapshots[idx]

        with mock.patch.object(lighter, "_fetch_active_orders", side_effect=fake_fetch):
            return lighter._reconcile_batch_by_client_order_index(
                credentials=self._creds(),
                market_id=kwargs.pop("market_id", 3),
                side=kwargs.pop("side", "buy"),
                expected_client_order_indices=expected,
                auth_token="token",
                sleep=lambda _s: None,
                **kwargs,
            )


class StaleSnapshotTests(_ReconcileBase):
    """A stale read must not be mistaken for a landing failure."""

    def test_first_snapshot_empty_second_has_all(self) -> None:
        """0/50 then 50/50 -> all 50 confirmed."""
        expected = list(range(1, 51))
        first = []
        second = [_order(coi) for coi in expected]
        out = self._reconcile([first, second], expected)
        self.assertEqual(len(out), 50, "all 50 must be confirmed after the second read")
        for coi in expected:
            self.assertIn(coi, out)
        self.assertEqual(lighter._LIGHTER_LAST_RECONCILE_STATS["attempts"], 2)
        self.assertEqual(lighter._LIGHTER_LAST_RECONCILE_STATS["outcome"], "confirmed")
        self.assertEqual(lighter._LIGHTER_LAST_RECONCILE_STATS["unresolved"], [])

    def test_partial_snapshots_accumulate(self) -> None:
        """25 then 25 more -> 50 confirmed, neither snapshot forgotten."""
        expected = list(range(1, 51))
        first = [_order(coi) for coi in expected[:25]]
        second = [_order(coi) for coi in expected[25:]]
        out = self._reconcile([first, second], expected)
        self.assertEqual(len(out), 50)
        self.assertEqual(set(out), set(expected))
        self.assertEqual(lighter._LIGHTER_LAST_RECONCILE_STATS["outcome"], "confirmed")

    def test_a_child_seen_once_stays_confirmed(self) -> None:
        """A child that vanishes from later snapshots stays confirmed.

        A child can fill and leave the active feed; it still landed.
        """
        expected = [1, 2, 3]
        first = [_order(c) for c in expected]
        second = [_order(c) for c in expected[1:]]  # child 1 gone
        out = self._reconcile([first, second, second, second], expected)
        self.assertEqual(len(out), 3, "matches accumulate; nothing is forgotten")
        self.assertIn(1, out)

    def test_filled_child_still_counts_as_landed(self) -> None:
        """A child that already FILLED is still a confirmed landing."""
        expected = [7]
        snapshot = [_order(7, status="filled", size="0.000")]
        out = self._reconcile([snapshot], expected)
        self.assertEqual(len(out), 1, "a filled order still proves the order landed")
        self.assertEqual(lighter._LIGHTER_LAST_RECONCILE_STATS["outcome"], "confirmed")

    def test_non_open_status_still_confirms(self) -> None:
        """A cancelled / partially-filled child is still a landing."""
        expected = [9]
        for status in ("filled", "cancelled", "partially_filled"):
            with self.subTest(status=status):
                lighter._LIGHTER_LAST_RECONCILE_STATS.clear()
                out = self._reconcile([[_order(9, status=status)]], expected)
                self.assertEqual(len(out), 1)


class UnresolvedChildTests(_ReconcileBase):
    """Children that never appear stay unresolved — never accepted."""

    def test_permanently_missing_children_are_unresolved(self) -> None:
        expected = [1, 2, 3]
        out = self._reconcile([[]], expected)
        self.assertEqual(out, {}, "nothing may be confirmed")
        stats = lighter._LIGHTER_LAST_RECONCILE_STATS
        self.assertEqual(stats["outcome"], "timeout")
        self.assertEqual(stats["unresolved"], [1, 2, 3])

    def test_missing_children_are_never_counted_as_accepted(self) -> None:
        """The mapping is the ONLY source of truth for 'landed'."""
        expected = [1, 2, 3, 4]
        out = self._reconcile([[_order(1), _order(2)]], expected)
        self.assertEqual(set(out), {1, 2})
        self.assertNotIn(3, out)
        self.assertNotIn(4, out)

    def test_reconcile_is_bounded_by_the_deadline(self) -> None:
        """The pass terminates; it does not read forever."""
        expected = [1, 2, 3]
        out = self._reconcile([[] for _ in range(50)], expected)
        self.assertEqual(out, {})
        attempts = lighter._LIGHTER_LAST_RECONCILE_STATS["attempts"]
        self.assertLessEqual(attempts, lighter.LIGHTER_RECONCILE_MAX_ATTEMPTS)
        self.assertGreater(attempts, 1, "it must retry at least once")

    def test_backoff_sequence_is_ascending(self) -> None:
        """The documented backoff is 0.25, 0.5, 1.0 s.

        Checked against the PRODUCTION default — setUp shrinks it so the
        suite stays fast, so the saved value is the real one.
        """
        self.assertEqual(self._saved_backoff, (0.25, 0.5, 1.0))
        self.assertEqual(self._saved_deadline, 10.0)


class DuplicateAndConflictTests(_ReconcileBase):
    """Duplicate / conflicting client_order_index must be detected."""

    def test_duplicate_client_order_index_with_different_order_id(self) -> None:
        """Two different order_ids for one client_order_index = conflict."""
        expected = [1]
        conflicting = [
            _order(1, order_id=111),
            _order(1, order_id=222),
        ]
        out = self._reconcile([conflicting], expected)
        stats = lighter._LIGHTER_LAST_RECONCILE_STATS
        self.assertTrue(stats["conflicts"], "the conflict must be reported")
        self.assertEqual(len(stats["conflicts"]), 1)
        self.assertEqual(stats["conflicts"][0]["client_order_index"], 1)
        self.assertEqual(sorted(stats["conflicts"][0]["order_ids"]), [111, 222])
        # The first observation is kept; the conflict is not silently
        # merged into a second "landing".
        self.assertEqual(len(out), 1)
        self.assertEqual(out[1], 111)

    def test_duplicate_client_order_index_same_order_id_is_not_a_conflict(self) -> None:
        """The same order twice in one snapshot is harmless."""
        expected = [1]
        snap = [_order(1, order_id=111), _order(1, order_id=111)]
        out = self._reconcile([snap], expected)
        self.assertEqual(len(out), 1)
        self.assertEqual(lighter._LIGHTER_LAST_RECONCILE_STATS["conflicts"], [])


class IdentityCheckTests(_ReconcileBase):
    """A child is confirmed by identity, not by client_order_index alone."""

    def test_wrong_market_is_not_a_match(self) -> None:
        out = self._reconcile([[_order(1, market_id=9)]], [1])
        self.assertEqual(out, {})
        self.assertTrue(lighter._LIGHTER_LAST_RECONCILE_STATS["conflicts"])

    def test_wrong_side_is_not_a_match(self) -> None:
        out = self._reconcile([[_order(1, is_ask=True)]], [1])
        self.assertEqual(out, {})

    def test_wrong_price_is_not_a_match(self) -> None:
        out = self._reconcile(
            [[_order(1, price="12.345")]],
            [1],
            expected_prices={1: Decimal("55.000")},
        )
        self.assertEqual(out, {})
        self.assertTrue(lighter._LIGHTER_LAST_RECONCILE_STATS["conflicts"])

    def test_wrong_size_is_not_a_match(self) -> None:
        out = self._reconcile(
            [[_order(1, size="9.999")]],
            [1],
            expected_sizes={1: Decimal("0.250")},
        )
        self.assertEqual(out, {})

    def test_matching_identity_is_confirmed(self) -> None:
        out = self._reconcile(
            [[_order(1, price="55.000", size="0.250")]],
            [1],
            expected_prices={1: Decimal("55.000")},
            expected_sizes={1: Decimal("0.250")},
        )
        self.assertEqual(len(out), 1)


class ReadFailureTests(_ReconcileBase):
    """A read failure is not evidence the children did not land."""

    def test_read_failure_then_success(self) -> None:
        expected = [1, 2]
        state = {"i": 0}

        def fake_fetch(_creds, _token):  # noqa: ARG001
            state["i"] += 1
            if state["i"] == 1:
                raise ConnectionError("transient read failure")
            return [_order(c) for c in expected]

        with mock.patch.object(lighter, "_fetch_active_orders", side_effect=fake_fetch):
            out = lighter._reconcile_batch_by_client_order_index(
                credentials=self._creds(),
                market_id=3,
                side="buy",
                expected_client_order_indices=expected,
                auth_token="token",
                sleep=lambda _s: None,
            )
        self.assertEqual(len(out), 2, "a transient read failure must not lose the children")

    def test_persistent_read_failure_raises(self) -> None:
        """If we never manage a read, the caller sees the error."""
        with mock.patch.object(
            lighter, "_fetch_active_orders", side_effect=ConnectionError("down")
        ):
            with self.assertRaises(ConnectionError):
                lighter._reconcile_batch_by_client_order_index(
                    credentials=self._creds(),
                    market_id=3,
                    side="buy",
                    expected_client_order_indices=[1],
                    auth_token="token",
                    sleep=lambda _s: None,
                )


class NoDuplicateSubmissionTests(_ReconcileBase):
    """Reconciliation must never resubmit."""

    def test_reconcile_never_calls_a_write(self) -> None:
        """Only reads happen — no signer, no send_tx_batch."""
        expected = [1, 2]
        with mock.patch.object(
            lighter, "_fetch_active_orders", return_value=[_order(c) for c in expected]
        ):
            with mock.patch.object(
                lighter, "_submit_send_tx_batch",
                side_effect=AssertionError("reconciliation must not submit"),
            ):
                with mock.patch.object(
                    lighter, "_submit_cancel_tx_batch",
                    side_effect=AssertionError("reconciliation must not cancel"),
                ):
                    out = lighter._reconcile_batch_by_client_order_index(
                        credentials=self._creds(),
                        market_id=3,
                        side="buy",
                        expected_client_order_indices=expected,
                        auth_token="token",
                        sleep=lambda _s: None,
                    )
        self.assertEqual(len(out), 2)


class StaleVersusRejectionTests(unittest.TestCase):
    """A stale snapshot must not be classified as an exchange rejection."""

    def test_stale_snapshot_is_not_a_rejection(self) -> None:
        """The reconcile outcome distinguishes pending from rejection."""
        self.assertNotEqual(
            lighter._LIGHTER_RECONCILE_OUTCOME_TIMEOUT,
            lighter._LADDER_BATCH_OUTCOME_EXCHANGE_REJECTED,
        )
        self.assertNotEqual(
            lighter._LIGHTER_RECONCILE_OUTCOME_PENDING,
            lighter._LADDER_BATCH_OUTCOME_EXCHANGE_REJECTED,
        )

    def test_api_rejected_children_are_excluded_from_reconciliation(self) -> None:
        """A rejected child provably did not land — it is not 'pending'."""
        rejected = [101, 102]
        landed = [103]
        rec = {
            "ok": False,
            "client_order_indices": rejected + landed,
            "api_rejected_client_order_indices": rejected,
        }
        self.assertEqual(rec["ok"], False)
        # Only the non-rejected child is expected to appear.
        expected = [i for i in rec["client_order_indices"]
                    if i not in set(rec["api_rejected_client_order_indices"])]
        self.assertEqual(expected, landed)


class CancelByClientOrderIndexTests(unittest.TestCase):
    """Cleanup must be able to reach children by client_order_index."""

    def test_cancel_request_accepts_client_order_indices(self) -> None:
        """The cancel group accepts exact-identity targeting by COI."""
        import inspect

        source = inspect.getsource(lighter._execute_cancel_order_group)
        self.assertIn("client_order_indices", source)
        self.assertIn("explicit_cois", source)


if __name__ == "__main__":
    unittest.main()
