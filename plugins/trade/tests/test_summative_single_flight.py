"""Single-flight refresh coordination tests.

Two simultaneous Summative Report refreshes must share ONE underlying
snapshot fetch rather than each launching a full account fan-out.  These
tests are deterministic: a barrier releases every worker at the same
moment, and the producer blocks until the test releases it, so the
interleaving is arranged rather than raced for.

Covered:
  * two simultaneous refreshes -> one fetch
  * many simultaneous refreshes -> one fetch
  * all callers receive an equivalent snapshot
  * an exception propagates to every waiter (and is never cached)
  * a refresh after a failure works and refetches
  * distinct TradeDesk instances never share in-flight work
  * per-user filters stay independent and do not mutate the shared snapshot
  * the in-flight registry is empty afterwards (no stale entries / deadlock)
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock
from decimal import Decimal
from typing import Any, List, Optional

from plugins.trade import portfolio_report as portfolio
from plugins.trade import summative_report_ui as ui


def _clear_inflight() -> None:
    """Reset the shared registry so these tests never leak state."""
    with ui._REFRESH_LOCK:
        ui._INFLIGHT.clear()


class _State:
    """Minimal stand-in for the wizard report state."""

    def __init__(self) -> None:
        self.report: dict = {}


class _Desk:
    """Weak-referenceable desk stand-in (real TradeDesk is a normal object)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()


def _snapshot(tag: str = "S") -> portfolio.PortfolioSnapshot:
    return portfolio.PortfolioSnapshot(fetched_at=0.0, accounts=[], positions=[], errors=[])


_BARRIER_TIMEOUT = 15.0
_JOIN_TIMEOUT = 30.0


def _release_barrier(barrier: Any) -> None:
    """Force a barrier open so no thread can stay parked on it.

    An untimed ``Barrier.wait()`` is an unbounded block: if one worker dies
    before reaching it, the others hang forever and, being non-daemon, keep
    the interpreter alive at exit.  Cleanup always breaks the barrier.
    """
    try:
        barrier.abort()
    except Exception:  # noqa: BLE001
        pass


def _join_all(threads: List[threading.Thread], timeout: float = _JOIN_TIMEOUT) -> None:
    """Bounded join; assert every worker actually terminated."""
    deadline = time.monotonic() + timeout
    for t in threads:
        remaining = max(0.0, deadline - time.monotonic())
        t.join(timeout=remaining)
    stuck = [t for t in threads if t.is_alive()]
    if stuck:
        raise AssertionError(f"{len(stuck)} worker thread(s) failed to terminate")


def _run_concurrently(n: int, desk: Any, state_factory, produce) -> List[Any]:
    """Start n threads that all call _report_snapshot at the same moment.

    ``produce`` stands in for portfolio.collect_portfolio_snapshot, which is
    the seam _report_snapshot actually calls.  Exceptions raised inside a
    worker are captured and re-raised here so a thread can never fail
    silently.
    """
    barrier = threading.Barrier(n, timeout=_BARRIER_TIMEOUT)
    results: List[Any] = []
    errors: List[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            state = state_factory()
            # A broken barrier must not deadlock the worker: it turns the
            # simultaneity guarantee into a hard failure we can report.
            barrier.wait(timeout=_BARRIER_TIMEOUT)
            snap = ui._report_snapshot(state=state, desk=desk, refresh=True)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
        else:
            with lock:
                results.append(snap)

    # Patch ONCE around the whole concurrent section. Patching inside a worker
    # thread races: one thread's `with` can exit while another is still inside,
    # leaving a leaked mock that corrupts unrelated later tests.
    with mock.patch.object(
        portfolio, "collect_portfolio_snapshot", side_effect=produce
    ):
        threads = [
            threading.Thread(target=worker, name=f"sf-worker-{i}", daemon=True)
            for i in range(n)
        ]
        try:
            for t in threads:
                t.start()
            _join_all(threads)
            active = portfolio.collect_portfolio_snapshot
            if not isinstance(active, mock.MagicMock):
                raise AssertionError("mock was restored early")
        finally:
            # Daemon threads are a last-resort safety net, never the cleanup
            # strategy: break the barrier, then join, so nothing is left parked.
            _release_barrier(barrier)
            _join_all(threads, timeout=5.0)
    if errors:
        raise errors[0]
    return results


class TestSharedFetch(unittest.TestCase):
    def setUp(self) -> None:
        _clear_inflight()
        self.desk = _Desk()
        self.addCleanup(_clear_inflight)
        self.calls = 0
        self.gate = threading.Event()
        self.lock = threading.Lock()
        self.snap = _snapshot()

        def produce(desk: Any = None, **kwargs: Any) -> portfolio.PortfolioSnapshot:
            with self.lock:
                self.calls += 1
            self.gate.wait(20)  # hold the fetch open until the test releases it
            return self.snap

        self.produce = produce

    def test_two_simultaneous_refreshes_use_one_fetch(self) -> None:
        def state_factory() -> _State:
            return _State()

        def run() -> None:
            _run_concurrently(2, self.desk, state_factory, self.produce)

        t = threading.Thread(target=run, name="sf-two", daemon=True)
        t.start()
        time.sleep(0.5)  # let both threads reach the barrier
        self.gate.set()
        _join_all([t])
        self.assertEqual(self.calls, 1, "two refreshes must share one fetch")

    def test_many_simultaneous_refreshes_use_one_fetch(self) -> None:
        results: List[Any] = []
        errors: List[str] = []

        def run() -> None:
            try:
                results.extend(
                    _run_concurrently(8, self.desk, _State, self.produce)
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(str(exc))

        t = threading.Thread(target=run, name="sf-many", daemon=True)
        t.start()
        time.sleep(0.5)
        self.gate.set()
        _join_all([t])
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(self.calls, 1, "8 refreshes must share one fetch")

    def test_all_callers_receive_equivalent_snapshot(self) -> None:
        results: List[Any] = []

        def run() -> None:
            results.extend(_run_concurrently(4, self.desk, _State, self.produce))

        t = threading.Thread(target=run, name="sf-equiv", daemon=True)
        t.start()
        time.sleep(0.5)
        self.gate.set()
        _join_all([t])
        self.assertEqual(self.calls, 1)
        for snap in results:
            self.assertIs(snap, self.snap, "every caller gets the same object")

    def test_registry_is_empty_after_success(self) -> None:
        def run() -> None:
            _run_concurrently(3, self.desk, _State, self.produce)

        t = threading.Thread(target=run, name="sf-equiv", daemon=True)
        t.start()
        time.sleep(0.5)
        self.gate.set()
        _join_all([t])
        self.assertEqual(len(list(ui._INFLIGHT.items())), 0, "no stale entry")


class TestFailurePropagation(unittest.TestCase):
    def setUp(self) -> None:
        _clear_inflight()
        self.desk = _Desk()
        self.addCleanup(_clear_inflight)
        self.calls = 0
        self.gate = threading.Event()
        self.lock = threading.Lock()

    def test_exception_reaches_every_waiter_and_is_not_cached(self) -> None:
        def produce(desk: Any = None, **kwargs: Any) -> Any:
            with self.lock:
                self.calls += 1
            self.gate.wait(20)
            raise RuntimeError("upstream unavailable")

        seen: List[str] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            try:
                ui._report_snapshot(state=_State(), desk=self.desk, refresh=True)
            except RuntimeError as exc:
                with lock:
                    seen.append(str(exc))
            else:
                with lock:
                    seen.append("NO-ERROR")

        barrier = threading.Barrier(4, timeout=_BARRIER_TIMEOUT)
        with mock.patch.object(
            portfolio, "collect_portfolio_snapshot", side_effect=produce
        ):
            threads = [
                threading.Thread(target=worker, name=f"sf-fail-{i}", daemon=True)
                for i in range(4)
            ]
            try:
                for t in threads:
                    t.start()
                time.sleep(0.5)
                self.gate.set()
                _join_all(threads)
            finally:
                _release_barrier(barrier)
                _join_all(threads, timeout=5.0)

        self.assertEqual(self.calls, 1, "one failed fetch shared by all waiters")
        self.assertEqual(
            seen, ["upstream unavailable"] * 4,
            "every caller must see the same failure",
        )
        self.assertEqual(len(list(ui._INFLIGHT.items())), 0, "failure must not stay cached")

    def test_refresh_after_failure_refetches(self) -> None:
        calls = {"n": 0}

        def failing(desk: Any = None, **kwargs: Any) -> Any:
            calls["n"] += 1
            raise RuntimeError("first attempt fails")

        state = _State()
        with mock.patch.object(
            portfolio, "collect_portfolio_snapshot", side_effect=failing
        ):
            with self.assertRaises(RuntimeError):
                ui._report_snapshot(state=state, desk=self.desk, refresh=True)
        self.assertIsNone(state.report.get("snapshot"), "failure must not cache a snapshot")

        good = _snapshot("SECOND")
        state2 = _State()
        with mock.patch.object(portfolio, "collect_portfolio_snapshot", return_value=good):
            snap = ui._report_snapshot(state=state2, desk=self.desk, refresh=True)
        self.assertIs(snap, good, "a later refresh must succeed normally")


class TestDeskIsolation(unittest.TestCase):
    def setUp(self) -> None:
        _clear_inflight()
        self.addCleanup(_clear_inflight)

    def test_distinct_desks_do_not_share_in_flight_work(self) -> None:
        desk_a, desk_b = _Desk(), _Desk()
        calls = {"a": 0, "b": 0}
        gate_a = threading.Event()
        gate_b = threading.Event()

        def produce_a(desk: Any = None, **kwargs: Any) -> Any:
            # Both threads patch the SAME module attribute concurrently, so
            # dispatching on `desk` is what makes this test meaningful: a
            # thread that received the other side's mock still records against
            # the right counter instead of mis-attributing the call.
            calls["a"] += 1
            gate_a.wait(20)
            return _snapshot("A")

        def produce_b(desk: Any = None, **kwargs: Any) -> Any:
            calls["b"] += 1
            gate_b.wait(20)
            return _snapshot("B")

        def produce_any(target: Any = None, **kwargs: Any) -> Any:
            return produce_a(target, **kwargs) if target is desk_a else produce_b(target, **kwargs)

        out: dict = {}

        def run_a() -> None:
            with mock.patch.object(
                portfolio, "collect_portfolio_snapshot", side_effect=produce_any
            ):
                out["a"] = _run_concurrently(1, desk_a, _State, produce_any)

        def run_b() -> None:
            with mock.patch.object(
                portfolio, "collect_portfolio_snapshot", side_effect=produce_any
            ):
                out["b"] = _run_concurrently(1, desk_b, _State, produce_any)

        ta = threading.Thread(target=run_a, name="sf-deskA", daemon=True)
        tb = threading.Thread(target=run_b, name="sf-deskB", daemon=True)
        ta.start(); tb.start()
        time.sleep(0.3)
        # A's fetch is still open; B must be free to start its own.
        gate_b.set()
        try:
            _join_all([tb])
            self.assertEqual(calls["b"], 1)
            self.assertEqual(calls["a"], 1, "desk B must not join desk A's fetch")
        finally:
            gate_a.set()
            gate_b.set()
            _join_all([ta, tb], timeout=10.0)
        self.assertEqual(out["a"][0].errors, [])
        self.assertEqual(len(list(ui._INFLIGHT.items())), 0)


class TestFilterIndependenceAndNoMutation(unittest.TestCase):
    def setUp(self) -> None:
        _clear_inflight()
        self.addCleanup(_clear_inflight)

    def _snap_with_accounts(self) -> portfolio.PortfolioSnapshot:
        def acct(ex: str, name: str, positions: int = 0) -> portfolio.AccountSnapshot:
            a = portfolio.AccountSnapshot(
                exchange=ex, account=name, account_label=name,
                identity=f"{ex}|{name}", ok=True, balance=None,
                balance_unit=None, equity=Decimal("100"), available=None,
                balance_ok=True, positions_ok=True,
            )
            a.positions = [
                portfolio.OpenPositionRow(
                    exchange=ex, account=name, account_label=name,
                    symbol=f"{ex.upper()}-1", instrument_key=f"{ex.upper()}-1",
                    side="BUY", size=Decimal("1"), entry_price=Decimal("10"),
                    mark=None, pnl=None, pnl_unit="USD", identity=f"{name}|1",
                )
                for _ in range(positions)
            ]
            return a

        return portfolio.PortfolioSnapshot(
            fetched_at=0.0,
            accounts=[acct("alpha", "A", 2), acct("beta", "B", 1)],
            positions=[], errors=[],
        )

    def test_filtering_does_not_mutate_the_shared_snapshot(self) -> None:
        snap = self._snap_with_accounts()
        before = (len(snap.accounts), len(snap.positions), snap.accounts_seen)

        scoped = snap.filtered("alpha")
        self.assertEqual(len(scoped.accounts), 1)
        self.assertEqual(scoped.accounts[0].exchange, "alpha")

        # The shared object is untouched by one user's filter.
        self.assertEqual(
            (len(snap.accounts), len(snap.positions), snap.accounts_seen), before,
            "per-user filtering must not mutate the shared snapshot",
        )

    def test_two_states_keep_independent_filters(self) -> None:
        snap = self._snap_with_accounts()
        state_a, state_b = _State(), _State()
        state_a.report["snapshot"] = snap
        state_b.report["snapshot"] = snap
        state_a.report["filter_exchange"] = "alpha"
        state_b.report["filter_exchange"] = "beta"

        a = ui._scope(state_a, state_a.report["snapshot"])
        b = ui._scope(state_b, state_b.report["snapshot"])
        self.assertEqual([x.exchange for x in a.accounts], ["alpha"])
        self.assertEqual([x.exchange for x in b.accounts], ["beta"])
        # Each user still sees only their own scope.
        self.assertEqual(len(snap.accounts), 2, "shared snapshot still holds all accounts")


class TestCachedReuseUnaffected(unittest.TestCase):
    def setUp(self) -> None:
        _clear_inflight()
        self.addCleanup(_clear_inflight)

    def test_refresh_false_reuses_cached_snapshot(self) -> None:
        desk = _Desk()
        state = _State()
        cached = _snapshot("CACHED")
        state.report["snapshot"] = cached

        calls = {"n": 0}

        def produce(desk: Any = None, **kwargs: Any) -> Any:
            calls["n"] += 1
            return _snapshot("NEW")

        with mock.patch.object(
            portfolio, "collect_portfolio_snapshot", side_effect=produce
        ):
            got = ui._report_snapshot(state=state, desk=desk, refresh=False)
        self.assertIs(got, cached, "refresh=False must reuse the cached snapshot")
        self.assertEqual(calls["n"], 0, "no fetch on a non-refresh navigation")

    def test_non_weakrefable_desk_still_works(self) -> None:
        # A desk that cannot be weak-referenced must degrade to no coordination,
        # not crash.
        class NoWeakref:
            __slots__ = ()

        good = _snapshot("OK")
        with mock.patch.object(portfolio, "collect_portfolio_snapshot", return_value=good):
            got = ui._report_snapshot(state=_State(), desk=NoWeakref(), refresh=True)
        self.assertIs(got, good)


class TestLeaderFailureModes(unittest.TestCase):
    """Leader-side failure paths must always release waiters and clean up."""

    def setUp(self) -> None:
        _clear_inflight()
        self.desk = _Desk()
        self.addCleanup(_clear_inflight)

    def test_failure_before_snapshot_creation_releases_waiters(self) -> None:
        # Producer raises before producing anything - the waiter must still be
        # woken and see the same error, not block forever.
        seen: List[str] = []
        lock = threading.Lock()
        gate = threading.Event()
        barrier = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)

        def produce(desk: Any = None, **kwargs: Any) -> Any:
            gate.wait(20)
            raise ValueError("failed before snapshot")

        def worker() -> None:
            barrier.wait(timeout=_BARRIER_TIMEOUT)
            try:
                ui._report_snapshot(state=_State(), desk=self.desk, refresh=True)
            except ValueError as exc:
                with lock:
                    seen.append(str(exc))

        with mock.patch.object(
            portfolio, "collect_portfolio_snapshot", side_effect=produce
        ):
            threads = [
                threading.Thread(target=worker, name=f"sf-pre-{i}", daemon=True)
                for i in range(2)
            ]
            try:
                for t in threads:
                    t.start()
                time.sleep(0.3)
                gate.set()
                _join_all(threads)
            finally:
                _release_barrier(barrier)
                _join_all(threads, timeout=5.0)

        self.assertEqual(seen, ["failed before snapshot"] * 2)
        self.assertEqual(len(list(ui._INFLIGHT.items())), 0)

    def test_unexpected_exception_type_still_propagates(self) -> None:
        def produce(desk: Any = None, **kwargs: Any) -> Any:
            raise KeyboardInterrupt("simulated interrupt")

        with mock.patch.object(
            portfolio, "collect_portfolio_snapshot", side_effect=produce
        ):
            with self.assertRaises(KeyboardInterrupt):
                ui._report_snapshot(state=_State(), desk=self.desk, refresh=True)
        self.assertEqual(
            len(list(ui._INFLIGHT.items())), 0,
            "an unexpected exception must still clear the registry",
        )

    def test_cleanup_error_does_not_mask_registry_removal(self) -> None:
        # If the registry cleanup itself raises, the entry must not be left
        # behind to poison the next refresh.
        real_del = type(ui._INFLIGHT).__delitem__
        boom = {"once": True}

        class _Exploding(dict):
            pass

        def exploding_del(self, key):
            if boom["once"]:
                boom["once"] = False
                raise RuntimeError("cleanup exploded")
            return real_del(self, key)

        good = _snapshot("OK")
        original = ui._INFLIGHT.__class__.__delitem__
        try:
            ui._INFLIGHT.__class__.__delitem__ = exploding_del
            with mock.patch.object(
                portfolio, "collect_portfolio_snapshot", return_value=good
            ):
                snap = ui._report_snapshot(
                    state=_State(), desk=self.desk, refresh=True
                )
        finally:
            ui._INFLIGHT.__class__.__delitem__ = original
        self.assertIs(snap, good, "cleanup error must not break the result")

    def test_waiter_terminates_after_leader_failure(self) -> None:
        gate = threading.Event()
        finished = threading.Event()

        def produce(desk: Any = None, **kwargs: Any) -> Any:
            gate.wait(20)
            raise RuntimeError("leader down")

        def waiter() -> None:
            try:
                ui._report_snapshot(state=_State(), desk=self.desk, refresh=True)
            except RuntimeError:
                pass
            finally:
                finished.set()

        with mock.patch.object(
            portfolio, "collect_portfolio_snapshot", side_effect=produce
        ):
            leader = threading.Thread(
                target=lambda: _guard(
                    lambda: ui._report_snapshot(
                        state=_State(), desk=self.desk, refresh=True
                    )
                ),
                name="sf-leader", daemon=True,
            )
            leader.start()
            time.sleep(0.2)
            w = threading.Thread(target=waiter, name="sf-waiter", daemon=True)
            w.start()
            time.sleep(0.2)
            gate.set()
            _join_all([leader, w])
        self.assertTrue(
            finished.is_set(), "waiter must terminate after the leader fails"
        )


def _guard(fn):
    try:
        fn()
    except BaseException:  # noqa: BLE001
        pass


class TestRefreshGenerations(unittest.TestCase):
    def setUp(self) -> None:
        _clear_inflight()
        self.desk = _Desk()
        self.addCleanup(_clear_inflight)

    def test_consecutive_generations_each_refetch(self) -> None:
        calls = {"n": 0}

        def produce(desk: Any = None, **kwargs: Any) -> Any:
            calls["n"] += 1
            return _snapshot(f"gen{calls['n']}")

        with mock.patch.object(
            portfolio, "collect_portfolio_snapshot", side_effect=produce
        ):
            first = ui._report_snapshot(state=_State(), desk=self.desk, refresh=True)
            second = ui._report_snapshot(state=_State(), desk=self.desk, refresh=True)
            third = ui._report_snapshot(state=_State(), desk=self.desk, refresh=True)

        self.assertEqual(calls["n"], 3, "each refresh generation must refetch")
        self.assertIsNot(first, second)
        self.assertIsNot(second, third)
        self.assertEqual(len(list(ui._INFLIGHT.items())), 0)

    def test_registry_clean_after_every_outcome(self) -> None:
        good, bad = _snapshot("G"), None

        def ok(desk: Any = None, **kwargs: Any) -> Any:
            return good

        def boom(desk: Any = None, **kwargs: Any) -> Any:
            raise RuntimeError("nope")

        for name, fn in (("success", ok), ("failure", boom)):
            with mock.patch.object(
                portfolio, "collect_portfolio_snapshot", side_effect=fn
            ):
                try:
                    ui._report_snapshot(state=_State(), desk=self.desk, refresh=True)
                except RuntimeError:
                    pass
            self.assertEqual(
                len(list(ui._INFLIGHT.items())), 0,
                f"registry not clean after {name}",
            )
        _ = bad

    def test_waiter_timeout_does_not_start_a_second_fetch(self) -> None:
        # A bounded waiter must RAISE, never silently launch another fan-out:
        # auto-retrying here would multiply exactly the request volume this
        # coordinator exists to reduce.
        calls = {"n": 0}
        release = threading.Event()

        def produce(desk: Any = None, **kwargs: Any) -> Any:
            calls["n"] += 1
            release.wait(30)
            return _snapshot("SLOW")

        with mock.patch.object(
            portfolio, "collect_portfolio_snapshot", side_effect=produce
        ):
            leader = threading.Thread(
                target=_guard,
                args=(lambda: ui._report_snapshot(
                    state=_State(), desk=self.desk, refresh=True
                ),),
                name="sf-slow-leader", daemon=True,
            )
            leader.start()
            time.sleep(0.3)

            entry = ui._InFlightFetch()
            ui._INFLIGHT[self.desk] = entry

            errors: List[str] = []

            def waiter() -> None:
                try:
                    entry.wait(timeout=0.5)
                except TimeoutError as exc:
                    errors.append(str(exc))

            w = threading.Thread(target=waiter, name="sf-timeout-waiter", daemon=True)
            try:
                w.start()
                _join_all([w])
            finally:
                release.set()
                _join_all([leader], timeout=10.0)
                _clear_inflight()

        self.assertEqual(len(errors), 1, "bounded wait must raise TimeoutError")
        self.assertIn("in-progress", errors[0])
        self.assertEqual(
            calls["n"], 1,
            "a waiter timeout must never trigger another fetch",
        )


if __name__ == "__main__":
    unittest.main()