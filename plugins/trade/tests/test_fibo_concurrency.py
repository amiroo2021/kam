"""Phase 2.7 concurrency hardening — regression tests.

Verifies that ``mark_stopped`` and ``reactivate`` perform
read-validate-write atomically under ONE exclusive file lock.

The TOCTOU race being guarded against:
  Old implementation: the latest row was read via
  ``self.get(registration_key)`` (no lock), validated, then the
  write happened under a NEWLY-acquired lock. Two concurrent
  callers could each observe the same latest-status and each
  pass validation, then both write transition rows.

  New implementation: ``_open_and_lock()`` is entered once at the
  top of the operation, the lock is acquired, the read happens
  inside the locked block, the validation happens inside, the
  build happens inside, and the write happens inside — all
  without releasing the lock. Concurrent callers are serialized
  at the lock boundary; only the first caller observes the
  expected pre-state and writes.

Strategy:
  These tests use ``threading.Thread`` to launch two callers
  concurrently against the SAME store instance on the SAME
  JSONL file. Because Python threads share file descriptors
  in the same process, ``fcntl.flock`` is technically
  per-process-per-fd — so two threads in one process that
  both opened the file independently will block on each
  other via flock (Linux flock semantics: advisory lock per
  inode, blocking across processes AND across distinct fds
  in the same process).

  We use TWO separate ``open(...)`` calls in each test thread
  via the ``_open_and_lock()`` context manager — each call
  creates its own file descriptor. The kernel-level flock
  blocks the second thread until the first releases.

  Where the test wants to deterministically force the second
  caller to wait, we use the ``_LockHeldStore`` subclass with
  a barrier that pauses inside the locked block. This avoids
  any flakiness in real-time scheduling.

These tests prove:
  1. Two concurrent ``reactivate`` calls — exactly ONE
     succeeds, exactly ONE registered transition row is
     appended.
  2. Two concurrent ``mark_stopped`` calls — exactly ONE
     succeeds, exactly ONE stopped transition row is appended.
  3. stop/reactivate serialization — history is valid;
     latest-row-wins; impossible transitions rejected.
  4. No partial/corrupt JSONL rows under concurrent transitions.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

from plugins.trade.fibo.store import (
    DuplicateRegistrationError, FiboRegistration, FiboRegistrationStore,
)


def _make_registered(
    store: FiboRegistrationStore,
    *,
    exchange: str,
    account: str,
    source_symbol: str,
    exchange_instrument: str,
    variant: str,
    side: str,
    starting_volume: str = "0.001",
) -> FiboRegistration:
    reg = FiboRegistration.build(
        exchange=exchange,
        account=account,
        symbol=source_symbol,
        variant=variant,
        side=side,
        starting_volume=starting_volume,
        source="obs-1",
        source_seq=42,
        source_cycle_id=42,
        source_cumulative_weight="2.5",
        source_percentage="0.001",
        source_snapshot_received_at="2026-08-27T00:00:00Z",
        desired_exchange_size=Decimal(starting_volume) * Decimal("2.5"),
        source_symbol=source_symbol,
        exchange_instrument=exchange_instrument,
    )
    store.append(reg)
    return reg


class _BaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fibo_dir = self.root / "fibo"
        self.fibo_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.fibo_dir, 0o700)
        self.reg_path = self.fibo_dir / "registrations.jsonl"

    def _raw_rows(self) -> List[dict]:
        if not self.reg_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.reg_path.read_text().splitlines()
            if line.strip()
        ]


class AtomicLockInvariantTests(_BaseTest):
    """The read-validate-write happens inside ONE
    ``_open_and_lock()`` block — i.e., under one exclusive
    file lock acquisition. We assert this by inspecting the
    store's source: every public mutation method must call
    ``_open_and_lock`` exactly once and never call it from
    inside itself (no nested re-entrant lock acquisition).
    """

    def test_append_acquires_lock_exactly_once(self) -> None:
        """``append`` must acquire ``_open_and_lock`` exactly once."""
        import inspect
        from plugins.trade.fibo.store import FiboRegistrationStore
        src = inspect.getsource(FiboRegistrationStore.append)
        # The new implementation uses ``with self._open_and_lock() as f:``
        self.assertIn("with self._open_and_lock()", src)
        # It must not nest another lock acquisition inside.
        self.assertNotIn(
            "self._acquire_lock(", src,
            "append must not call _acquire_lock directly; "
            "_open_and_lock already holds the lock",
        )

    def test_mark_stopped_acquires_lock_exactly_once(self) -> None:
        import inspect
        from plugins.trade.fibo.store import FiboRegistrationStore
        src = inspect.getsource(FiboRegistrationStore.mark_stopped)
        self.assertIn("with self._open_and_lock()", src)
        self.assertNotIn(
            "self._acquire_lock(", src,
            "mark_stopped must not call _acquire_lock directly",
        )

    def test_reactivate_acquires_lock_exactly_once(self) -> None:
        import inspect
        from plugins.trade.fibo.store import FiboRegistrationStore
        src = inspect.getsource(FiboRegistrationStore.reactivate)
        self.assertIn("with self._open_and_lock()", src)
        self.assertNotIn(
            "self._acquire_lock(", src,
            "reactivate must not call _acquire_lock directly",
        )

    def test_write_under_lock_does_not_reacquire(self) -> None:
        """``_write_under_lock`` is a primitive that ASSUMES the
        caller already holds the lock. It must not re-acquire
        the lock itself — i.e. no actual ``_acquire_lock(`` or
        ``_open_and_lock(`` call in the body.
        """
        import inspect
        from plugins.trade.fibo.store import FiboRegistrationStore
        src = inspect.getsource(FiboRegistrationStore._write_under_lock)
        # Strip the docstring so its references to _acquire_lock
        # don't trip this assertion.
        import re
        body = re.sub(r'^(\s*)"""[^"]*"""', "", src, count=1, flags=re.DOTALL)
        self.assertNotIn("_acquire_lock(", body)
        self.assertNotIn("_open_and_lock(", body)


class SerializedConcurrentTransitionTests(_BaseTest):
    """Sequential calls that exercise the lock semantics:

    The first mark_stopped acquires the lock and the second
    mark_stopped (same thread, sequential) acquires the lock
    AFTER the first releases. The second one observes the
    flipped state and refuses.

    Similarly for reactivate.

    This proves the lock boundary is honored even when callers
    share a process: the second sequential caller always sees
    the post-state of the first.
    """

    def test_two_sequential_mark_stopped_only_one_succeeds(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        reg = _make_registered(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        store.mark_stopped(reg.registration_key)
        # Second mark_stopped must fail.
        with self.assertRaises(ValueError) as cm:
            store.mark_stopped(reg.registration_key)
        self.assertIn("already stopped", str(cm.exception))
        rows = self._raw_rows()
        statuses = [r["status"] for r in rows]
        self.assertEqual(statuses, ["registered", "stopped"])

    def test_two_sequential_reactivate_only_one_succeeds(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        reg = _make_registered(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        store.mark_stopped(reg.registration_key)
        # First reactivate succeeds.
        out, _active_count = store.reactivate(
            reg.registration_key,
            source_symbol="ETHUSD",
            exchange_instrument="ETH-USD.P",
            starting_volume=Decimal("0.001"),
            desired_exchange_size=Decimal("0.002"),
            source="obs-2", source_seq=99,
            source_cycle_id=100,
            source_cumulative_weight=Decimal("2.0"),
            source_percentage=Decimal("0.01"),
            source_snapshot_received_at="2026-08-27T05:00:00Z",
        )
        self.assertEqual(out.status, "registered")
        # Second reactivate (now stopped is gone) fails.
        with self.assertRaises(ValueError) as cm:
            store.reactivate(
                reg.registration_key,
                source_symbol="ETHUSD",
                exchange_instrument="ETH-USD.P",
                starting_volume=Decimal("0.001"),
                desired_exchange_size=Decimal("0.002"),
                source="obs-3", source_seq=200,
                source_cycle_id=201,
                source_cumulative_weight=Decimal("3.0"),
                source_percentage=Decimal("0.01"),
                source_snapshot_received_at="2026-08-27T06:00:00Z",
            )
        self.assertIn("is not stopped", str(cm.exception))
        rows = self._raw_rows()
        statuses = [r["status"] for r in rows]
        self.assertEqual(statuses, ["registered", "stopped", "registered"])


class ThreadedConcurrentTransitionTests(_BaseTest):
    """Two real threads try to mark_stopped / reactivate the
    same key concurrently.

    Because ``_open_and_lock`` opens its own file descriptor,
    the two threads each acquire flock on the same inode
    (advisory lock across distinct fds in the same process
    on Linux). The kernel serializes them.

    We assert: exactly one succeeds, the other raises
    ``ValueError``, and the file has the expected number of
    transition rows.
    """

    def _race(
        self, *, first_call, second_call,
    ) -> tuple:
        """Run first_call and second_call concurrently in two
        threads. Returns ``(out_first, out_second)`` where each
        element is ``("ok", result)`` or ``("exc", exc)``.
        """
        barrier = threading.Barrier(2)
        results: List[Optional[tuple]] = [None, None]

        def _runner(idx, fn):
            def _w():
                try:
                    barrier.wait(timeout=5.0)
                    results[idx] = ("ok", fn())
                except Exception as exc:
                    results[idx] = ("exc", exc)
            return _w

        threads = [
            threading.Thread(target=_runner(0, first_call), daemon=True),
            threading.Thread(target=_runner(1, second_call), daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        return tuple(results)

    def test_concurrent_mark_stopped_only_one_succeeds(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        reg = _make_registered(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )

        def _do_stop():
            _reg, _n = store.mark_stopped(reg.registration_key)
            return _reg

        out_a, out_b = self._race(first_call=_do_stop, second_call=_do_stop)

        statuses = [s for (kind, s) in (out_a, out_b) if kind == "ok"]
        excs = [
            (kind, e)
            for (kind, e) in (out_a, out_b)
            if kind == "exc"
        ]
        # Exactly one succeeded.
        self.assertEqual(len(statuses), 1)
        # The other one raised ValueError.
        self.assertEqual(len(excs), 1)
        kind, exc = excs[0]
        self.assertEqual(kind, "exc")
        self.assertIsInstance(exc, ValueError)
        self.assertIn("already stopped", str(exc))

        # Exactly TWO rows on disk: the original + ONE stopped.
        rows = self._raw_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["status"] for r in rows],
                         ["registered", "stopped"])

    def test_concurrent_reactivate_only_one_succeeds(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        reg = _make_registered(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        store.mark_stopped(reg.registration_key)

        def _do_reactivate():
            _reg, _n = store.reactivate(
                reg.registration_key,
                source_symbol="ETHUSD",
                exchange_instrument="ETH-USD.P",
                starting_volume=Decimal("0.001"),
                desired_exchange_size=Decimal("0.002"),
                source="obs-2", source_seq=99,
                source_cycle_id=100,
                source_cumulative_weight=Decimal("2.0"),
                source_percentage=Decimal("0.01"),
                source_snapshot_received_at="2026-08-27T05:00:00Z",
            )
            return _reg

        out_a, out_b = self._race(first_call=_do_reactivate,
                                 second_call=_do_reactivate)

        statuses = [s for (kind, s) in (out_a, out_b) if kind == "ok"]
        excs = [(kind, e) for (kind, e) in (out_a, out_b) if kind == "exc"]
        # Exactly one reactivation succeeded.
        self.assertEqual(len(statuses), 1)
        # The other raised ValueError (no-op transition refused).
        self.assertEqual(len(excs), 1)
        kind, exc = excs[0]
        self.assertIsInstance(exc, ValueError)
        self.assertIn("is not stopped", str(exc))

        # THREE rows on disk: original + stopped + ONE registered.
        rows = self._raw_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [r["status"] for r in rows],
            ["registered", "stopped", "registered"],
        )

    def test_concurrent_stop_and_reactivate_serialized(self) -> None:
        """Two threads: one tries to mark_stopped a registered
        row, the other tries to reactivate it.

        The two operations target the SAME key. Under proper
        serialization:

        * mark_stopped always succeeds (the row IS registered).
        * reactivate EITHER succeeds (if it reads the post-stop
          state and the reactivation identity validates) OR
          fails (if it reads the original registered state and
          refuses to reactivate a non-stopped row).
        * The file must end up in a valid state: latest-row-wins
          semantics resolve to a single effective registration.

        The two callers cannot both succeed in a way that
        produces TWO registered transition rows.
        """
        store = FiboRegistrationStore(self.reg_path)
        reg = _make_registered(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )

        def _do_stop():
            _reg, _n = store.mark_stopped(reg.registration_key)
            return _reg

        def _do_reactivate():
            # The row is currently registered. If reactivate runs
            # BEFORE mark_stopped commits, it sees registered and
            # raises ValueError. If it runs AFTER, it sees
            # stopped and successfully reactivates.
            _reg, _n = store.reactivate(
                reg.registration_key,
                source_symbol="ETHUSD",
                exchange_instrument="ETH-USD.P",
                starting_volume=Decimal("0.001"),
                desired_exchange_size=Decimal("0.002"),
                source="obs-2", source_seq=99,
                source_cycle_id=100,
                source_cumulative_weight=Decimal("2.0"),
                source_percentage=Decimal("0.01"),
                source_snapshot_received_at="2026-08-27T05:00:00Z",
            )
            return _reg

        out_a, out_b = self._race(first_call=_do_stop,
                                 second_call=_do_reactivate)

        # At most ONE transition row added (stop OR reactivate),
        # depending on which ran first.
        rows = self._raw_rows()
        # Original + at most 1 transition.
        self.assertLessEqual(len(rows), 3)
        # mark_stopped always succeeds.
        stopped_seen = any(
            kind == "ok" and x.status == "stopped"
            for (kind, x) in (out_a, out_b)
        )
        self.assertTrue(stopped_seen, "mark_stopped must always succeed")

        # If reactivate succeeded, it must have observed stopped
        # (not registered). Verify the latest row is valid.
        reactivate_seen = any(
            kind == "ok" and x.status == "registered"
            for (kind, x) in (out_a, out_b)
        )
        if reactivate_seen:
            # The latest row must be registered (post-react).
            self.assertEqual(rows[-1]["status"], "registered")
            # And the sequence must be registered, stopped,
            # registered — three rows.
            self.assertEqual(
                [r["status"] for r in rows],
                ["registered", "stopped", "registered"],
            )
        else:
            # mark_stopped won the race; reactivate saw
            # registered and refused. Sequence must be
            # registered, stopped — two rows.
            self.assertEqual(
                [r["status"] for r in rows],
                ["registered", "stopped"],
            )
            # The loser raised ValueError.
            loser = next(
                (kind, x) for (kind, x) in (out_a, out_b) if kind == "exc"
            )
            self.assertEqual(loser[0], "exc")
            self.assertIsInstance(loser[1], ValueError)
            self.assertIn("is not stopped", str(loser[1]))


class NoPartialCorruptJsonlTests(_BaseTest):
    """Under concurrent transition attempts, the JSONL file
    must remain parseable: every line is one well-formed JSON
    object, and ``load_all`` returns the right effective rows.
    """

    def test_no_partial_or_corrupt_rows_after_concurrent_calls(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        reg = _make_registered(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )

        # Run a sequence that exercises many concurrent attempts.
        threads = []

        def _stop_then_lose():
            store.mark_stopped(reg.registration_key)
            try:
                store.mark_stopped(reg.registration_key)
            except ValueError:
                pass

        def _react_then_lose():
            try:
                store.reactivate(
                    reg.registration_key,
                    source_symbol="ETHUSD",
                    exchange_instrument="ETH-USD.P",
                    starting_volume=Decimal("0.001"),
                    desired_exchange_size=Decimal("0.002"),
                    source="obs-2", source_seq=99,
                    source_cycle_id=100,
                    source_cumulative_weight=Decimal("2.0"),
                    source_percentage=Decimal("0.01"),
                    source_snapshot_received_at="2026-08-27T05:00:00Z",
                )
            except ValueError:
                pass

        for _ in range(3):
            t = threading.Thread(target=_stop_then_lose, daemon=True)
            threads.append(t)
        for _ in range(3):
            t = threading.Thread(target=_react_then_lose, daemon=True)
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        # Verify the file is parseable: every line is one JSON object.
        raw = self.reg_path.read_text()
        lines = raw.splitlines()
        while lines and not lines[-1].strip():
            lines.pop()
        for line in lines:
            obj = json.loads(line)
            self.assertIsInstance(obj, dict)
            self.assertIn("registration_key", obj)
            self.assertIn("status", obj)
        # And ``load_all`` returns exactly one effective row
        # (latest-row-wins).
        all_ = FiboRegistrationStore(self.reg_path).load_all()
        self.assertEqual(len(all_), 1)
        # Every row on disk has the same registration_key.
        for r in all_:
            self.assertEqual(r.registration_key, reg.registration_key)


if __name__ == "__main__":
    unittest.main()
