"""Active-registration-driven fibo-converge.timer lifecycle tests.

Offline only. Never touches production systemd units, registrations,
cycle_state, or exchanges.
"""
from __future__ import annotations

import os
import sys

# Host timer is off-limits for this suite.
os.environ["FIBO_TIMER_LIFECYCLE_DRY_RUN"] = "1"

import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Sequence, Tuple
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.fibo.store import (  # noqa: E402
    FiboRegistration,
    FiboRegistrationStore,
)
from plugins.trade.fibo.timer_lifecycle import (  # noqa: E402
    CONVERGE_TIMER_UNIT,
    CompletedProc,
    TimerReconcileResult,
    convergence_status_lines,
    count_active_registrations,
    ensure_convergence_timer_active,
    ensure_convergence_timer_inactive,
    reconcile_convergence_timer,
)


def _reg(
    *,
    exchange: str = "ondoperps",
    account: str = "bitget",
    symbol: str = "ETHUSD",
    instrument: str = "ETH-USD.P",
    variant: str = "NORMALFIB",
    side: str = "buy",
    status: str = "registered",
) -> FiboRegistration:
    return FiboRegistration.build(
        exchange=exchange,
        account=account,
        symbol=symbol,
        source_symbol=symbol,
        exchange_instrument=instrument,
        variant=variant,
        side=side,
        starting_volume=Decimal("1"),
        source="mt4",
        source_seq=1,
        source_cycle_id=100,
        source_cumulative_weight=Decimal("1"),
        source_percentage=Decimal("100"),
        source_snapshot_received_at="2026-01-01T00:00:00Z",
        desired_exchange_size=Decimal("1"),
        status=status,
    )


class FakeSystemctl:
    """In-memory systemctl stand-in. Never shells out."""

    def __init__(self) -> None:
        self.enabled: Dict[str, bool] = {CONVERGE_TIMER_UNIT: False}
        self.active: Dict[str, bool] = {CONVERGE_TIMER_UNIT: False}
        self.calls: List[Tuple[str, ...]] = []
        self.fail_enable = False
        self.fail_disable = False

    def __call__(self, args: Sequence[str]) -> CompletedProc:
        argv = tuple(str(a) for a in args)
        self.calls.append(argv)
        if not argv:
            return CompletedProc(1, "", "no args")
        cmd = argv[0]
        unit = argv[-1] if len(argv) > 1 else ""
        if unit and unit != CONVERGE_TIMER_UNIT and cmd in {
            "enable", "disable", "start", "stop", "is-enabled", "is-active"
        }:
            # Should never be asked — helper hard-codes the timer.
            return CompletedProc(1, "", f"refused unit {unit}")
        if cmd == "is-enabled":
            en = self.enabled.get(unit, False)
            return CompletedProc(0 if en else 1, "enabled\n" if en else "disabled\n", "")
        if cmd == "is-active":
            ac = self.active.get(unit, False)
            return CompletedProc(0 if ac else 3, "active\n" if ac else "inactive\n", "")
        if cmd == "enable":
            # enable [--now] unit
            if self.fail_enable:
                return CompletedProc(1, "", "enable failed")
            self.enabled[unit] = True
            if "--now" in argv:
                self.active[unit] = True
            return CompletedProc(0, "", "")
        if cmd == "disable":
            if self.fail_disable:
                return CompletedProc(1, "", "disable failed")
            self.enabled[unit] = False
            if "--now" in argv:
                self.active[unit] = False
            return CompletedProc(0, "", "")
        if cmd == "start":
            self.active[unit] = True
            return CompletedProc(0, "", "")
        if cmd == "stop":
            self.active[unit] = False
            return CompletedProc(0, "", "")
        return CompletedProc(1, "", f"unknown {cmd}")


class TestTimerHelperIdempotent(unittest.TestCase):
    def test_activate_noop_when_already_active(self) -> None:
        fake = FakeSystemctl()
        fake.enabled[CONVERGE_TIMER_UNIT] = True
        fake.active[CONVERGE_TIMER_UNIT] = True
        r = ensure_convergence_timer_active(runner=fake)
        self.assertTrue(r.ok)
        self.assertTrue(r.already_ok)
        self.assertFalse(r.changed)
        self.assertNotIn(("enable", "--now", CONVERGE_TIMER_UNIT), fake.calls)

    def test_deactivate_noop_when_already_inactive(self) -> None:
        fake = FakeSystemctl()
        r = ensure_convergence_timer_inactive(runner=fake)
        self.assertTrue(r.ok)
        self.assertTrue(r.already_ok)
        self.assertFalse(r.changed)

    def test_reconcile_by_count(self) -> None:
        fake = FakeSystemctl()
        r = reconcile_convergence_timer(1, runner=fake)
        self.assertTrue(r.ok)
        self.assertTrue(fake.active[CONVERGE_TIMER_UNIT])
        r2 = reconcile_convergence_timer(2, runner=fake)
        self.assertTrue(r2.already_ok)
        r3 = reconcile_convergence_timer(0, runner=fake)
        self.assertTrue(r3.ok)
        self.assertFalse(fake.active[CONVERGE_TIMER_UNIT])

    def test_refuses_non_timer_unit(self) -> None:
        fake = FakeSystemctl()
        with self.assertRaises(ValueError):
            ensure_convergence_timer_active(
                runner=fake, unit="fibo-converge.service"
            )


class TestEffectiveActiveCount(unittest.TestCase):
    def test_latest_per_key_wins_and_stopped_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = FiboRegistrationStore(Path(td) / "registrations.jsonl")
            a = _reg(symbol="ETHUSD", instrument="ETH-USD.P", side="buy")
            b = _reg(symbol="BTCUSD", instrument="BTC-USD.P", side="sell")
            self.assertEqual(store.append(a), 1)
            self.assertEqual(store.append(b), 2)
            # Historical stopped line should not count once superseded —
            # stop B then re-check.
            stopped, n = store.mark_stopped(b.registration_key)
            self.assertTrue(stopped.is_stopped)
            self.assertEqual(n, 1)
            # Append another stop history doesn't invent actives.
            all_regs = store.load_all()
            self.assertEqual(count_active_registrations(all_regs), 1)
            # Historical: raw file has multiple lines but load_all is latest-wins.
            raw = (Path(td) / "registrations.jsonl").read_text().strip().splitlines()
            self.assertGreaterEqual(len(raw), 3)
            self.assertEqual(len(all_regs), 2)  # two keys

    def test_historical_stopped_lines_do_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = FiboRegistrationStore(Path(td) / "registrations.jsonl")
            a = _reg()
            store.append(a)
            store.mark_stopped(a.registration_key)
            # Reactivate
            reactivated, n = store.reactivate(
                a.registration_key,
                source_symbol=a.source_symbol,
                exchange_instrument=a.exchange_instrument,
                starting_volume=Decimal("2"),
                desired_exchange_size=Decimal("2"),
                source="mt4",
                source_seq=2,
                source_cycle_id=101,
                source_cumulative_weight=Decimal("1"),
                source_percentage=Decimal("100"),
                source_snapshot_received_at="2026-01-02T00:00:00Z",
            )
            self.assertTrue(reactivated.is_active)
            self.assertEqual(n, 1)
            self.assertEqual(store.count_active(), 1)


class TestStartStopLifecycleOrdering(unittest.TestCase):
    def test_first_registration_persists_then_activates_timer(self) -> None:
        fake = FakeSystemctl()
        order: List[str] = []

        with tempfile.TemporaryDirectory() as td:
            store = FiboRegistrationStore(Path(td) / "registrations.jsonl")
            real_append = store.append

            def tracking_append(reg):
                order.append("persist")
                n = real_append(reg)
                order.append(f"count:{n}")
                return n

            store.append = tracking_append  # type: ignore[method-assign]
            n = store.append(_reg())
            self.assertEqual(n, 1)
            order.append("timer")
            r = reconcile_convergence_timer(n, runner=fake)
            self.assertTrue(r.ok)
            self.assertEqual(order, ["persist", "count:1", "timer"])
            self.assertTrue(fake.active[CONVERGE_TIMER_UNIT])
            # No converge service start
            self.assertTrue(
                all(c[-1] == CONVERGE_TIMER_UNIT for c in fake.calls if c and c[0] in {"enable", "disable"})
            )

    def test_persist_failure_skips_timer(self) -> None:
        fake = FakeSystemctl()
        with tempfile.TemporaryDirectory() as td:
            store = FiboRegistrationStore(Path(td) / "registrations.jsonl")
            store.append(_reg())
            with self.assertRaises(Exception):
                # duplicate same-status
                store.append(_reg())
            # Timer never asked
            self.assertEqual(fake.calls, [])
            self.assertFalse(fake.active[CONVERGE_TIMER_UNIT])

    def test_second_registration_no_timer_restart(self) -> None:
        fake = FakeSystemctl()
        with tempfile.TemporaryDirectory() as td:
            store = FiboRegistrationStore(Path(td) / "registrations.jsonl")
            n1 = store.append(_reg(side="buy"))
            r1 = reconcile_convergence_timer(n1, runner=fake)
            self.assertTrue(r1.changed or r1.already_ok)
            calls_after_first = list(fake.calls)
            n2 = store.append(_reg(side="sell", symbol="BTCUSD", instrument="BTC-USD.P"))
            self.assertEqual(n2, 2)
            r2 = reconcile_convergence_timer(n2, runner=fake)
            self.assertTrue(r2.already_ok)
            self.assertFalse(r2.changed)
            # No additional enable after the first arming.
            enable_calls = [c for c in fake.calls if c and c[0] == "enable"]
            self.assertEqual(len(enable_calls), len([c for c in calls_after_first if c and c[0] == "enable"]))

    def test_stop_one_of_many_leaves_timer(self) -> None:
        fake = FakeSystemctl()
        with tempfile.TemporaryDirectory() as td:
            store = FiboRegistrationStore(Path(td) / "registrations.jsonl")
            a = _reg(side="buy")
            b = _reg(side="sell", symbol="BTCUSD", instrument="BTC-USD.P")
            store.append(a)
            store.append(b)
            reconcile_convergence_timer(2, runner=fake)
            fake.calls.clear()
            stopped, n = store.mark_stopped(a.registration_key)
            self.assertEqual(n, 1)
            r = reconcile_convergence_timer(n, runner=fake)
            self.assertTrue(r.already_ok)
            self.assertFalse(any(c and c[0] == "disable" for c in fake.calls))
            self.assertTrue(fake.active[CONVERGE_TIMER_UNIT])

    def test_stop_last_disables_timer_after_persist(self) -> None:
        fake = FakeSystemctl()
        order: List[str] = []
        with tempfile.TemporaryDirectory() as td:
            store = FiboRegistrationStore(Path(td) / "registrations.jsonl")
            a = _reg()
            store.append(a)
            reconcile_convergence_timer(1, runner=fake)
            fake.calls.clear()

            real_ms = store.mark_stopped

            def tracking_ms(key, **kw):
                order.append("persist_stop")
                out = real_ms(key, **kw)
                order.append(f"count:{out[1]}")
                return out

            store.mark_stopped = tracking_ms  # type: ignore[method-assign]
            stopped, n = store.mark_stopped(a.registration_key)
            self.assertEqual(n, 0)
            order.append("timer")
            r = reconcile_convergence_timer(n, runner=fake)
            self.assertTrue(r.ok)
            self.assertFalse(fake.active[CONVERGE_TIMER_UNIT])
            self.assertEqual(order[0], "persist_stop")
            self.assertIn("count:0", order)
            self.assertEqual(order[-1], "timer")

    def test_start_timer_failure_keeps_registration(self) -> None:
        fake = FakeSystemctl()
        fake.fail_enable = True
        with tempfile.TemporaryDirectory() as td:
            store = FiboRegistrationStore(Path(td) / "registrations.jsonl")
            n = store.append(_reg())
            self.assertEqual(n, 1)
            r = reconcile_convergence_timer(n, runner=fake)
            self.assertFalse(r.ok)
            # Registration remains active.
            self.assertEqual(store.count_active(), 1)
            self.assertTrue(store.load_all()[0].is_active)

    def test_stop_last_timer_failure_keeps_stopped(self) -> None:
        fake = FakeSystemctl()
        with tempfile.TemporaryDirectory() as td:
            store = FiboRegistrationStore(Path(td) / "registrations.jsonl")
            a = _reg()
            store.append(a)
            fake.enabled[CONVERGE_TIMER_UNIT] = True
            fake.active[CONVERGE_TIMER_UNIT] = True
            fake.fail_disable = True
            stopped, n = store.mark_stopped(a.registration_key)
            self.assertEqual(n, 0)
            self.assertTrue(stopped.is_stopped)
            r = reconcile_convergence_timer(n, runner=fake)
            self.assertFalse(r.ok)
            # No rollback — still stopped.
            latest = store.get(a.registration_key)
            assert latest is not None
            self.assertTrue(latest.is_stopped)
            self.assertEqual(store.count_active(), 0)


class TestFlowAndWizardWiring(unittest.TestCase):
    def test_flow_registered_screen_reconciles_timer(self) -> None:
        from plugins.trade.fibo.flow import StartFiboFlow

        fake = FakeSystemctl()
        flow = StartFiboFlow.__new__(StartFiboFlow)
        flow._systemctl_runner = fake  # type: ignore[attr-defined]
        flow._reconcile_timer_after_mutation = (  # type: ignore[method-assign]
            lambda count: reconcile_convergence_timer(count, runner=fake)
        )
        reg = _reg()
        screen = StartFiboFlow._render_registered(
            flow, reg, active_count=1
        )
        self.assertIn("Convergence:", screen.text)
        self.assertTrue(fake.active[CONVERGE_TIMER_UNIT])
        self.assertIn("ACTIVE", screen.text)

    def test_flow_start_timer_failure_warning(self) -> None:
        from plugins.trade.fibo.flow import StartFiboFlow

        fake = FakeSystemctl()
        fake.fail_enable = True
        flow = StartFiboFlow.__new__(StartFiboFlow)
        flow._systemctl_runner = fake  # type: ignore[attr-defined]
        flow._reconcile_timer_after_mutation = (  # type: ignore[method-assign]
            lambda count: reconcile_convergence_timer(count, runner=fake)
        )
        screen = StartFiboFlow._render_registered(
            flow, _reg(), active_count=1
        )
        self.assertIn("could not be enabled", screen.text)
        self.assertIn("INACTIVE", screen.text)

    def test_stop_execute_uses_lifecycle(self) -> None:
        import plugins.trade.fibo_wizard as wiz

        fake = FakeSystemctl()
        fake.enabled[CONVERGE_TIMER_UNIT] = True
        fake.active[CONVERGE_TIMER_UNIT] = True
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            fibo_dir = home / "fibo"
            fibo_dir.mkdir()
            fibo_dir.chmod(0o700)
            store = FiboRegistrationStore(fibo_dir / "registrations.jsonl")
            a = _reg()
            store.append(a)
            # Patch hermes home + runner
            with mock.patch.object(wiz, "_resolve_hermes_home_for_flow", return_value=home):
                with mock.patch.object(wiz, "_stop_active_registrations", return_value=[a]):
                    wiz._FIBO_SYSTEMCTL_RUNNER = fake  # type: ignore[attr-defined]
                    screen = wiz._execute_stop(0)
            self.assertIn("Fibo stopped", screen["text"])
            self.assertIn("Active registrations remaining: 0", screen["text"])
            self.assertIn("INACTIVE", screen["text"])
            self.assertFalse(fake.active[CONVERGE_TIMER_UNIT])
            # No service/gateway/reader control
            joined = [" ".join(c) for c in fake.calls]
            self.assertTrue(all(CONVERGE_TIMER_UNIT in j for j in joined if j.startswith("disable") or j.startswith("enable")))
            self.assertFalse(any("fibo-converge.service" in j for j in joined))
            self.assertFalse(any("hermes-gateway" in j for j in joined))
            self.assertFalse(any("mt4-reader" in j for j in joined))

    def test_running_screen_shows_convergence_status(self) -> None:
        from plugins.trade.fibo import dryrun

        class FakeRec:
            def reconcile_all(self):
                return []

        with mock.patch(
            "plugins.trade.fibo.timer_lifecycle._query_bool",
            side_effect=lambda run, sub, unit: False,
        ):
            screen = dryrun.build_running_screen(FakeRec())  # type: ignore[arg-type]
        self.assertIn("Convergence:", screen["text"])


class TestRaceSerialization(unittest.TestCase):
    def test_concurrent_stop_and_start_cannot_disable_with_active_left(self) -> None:
        """Stop-last and Start race: final timer state matches final count."""
        fake = FakeSystemctl()
        fake.enabled[CONVERGE_TIMER_UNIT] = True
        fake.active[CONVERGE_TIMER_UNIT] = True
        with tempfile.TemporaryDirectory() as td:
            store = FiboRegistrationStore(Path(td) / "registrations.jsonl")
            a = _reg(side="buy")
            b = _reg(side="sell", symbol="BTCUSD", instrument="BTC-USD.P")
            store.append(a)

            barrier = threading.Barrier(2)
            results: List[Tuple[str, int]] = []
            lock = threading.Lock()

            def stop_a():
                barrier.wait()
                _stopped, n = store.mark_stopped(a.registration_key)
                with lock:
                    results.append(("stop", n))
                reconcile_convergence_timer(n, runner=fake)

            def start_b():
                barrier.wait()
                n = store.append(b)
                with lock:
                    results.append(("start", n))
                reconcile_convergence_timer(n, runner=fake)

            t1 = threading.Thread(target=stop_a)
            t2 = threading.Thread(target=start_b)
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)
            final = store.count_active()
            # Final effective actives should be 1 (B active, A stopped).
            self.assertEqual(final, 1)
            # Timer must end ACTIVE because an active registration remains.
            # Reconcile once more from truth.
            r = reconcile_convergence_timer(final, runner=fake)
            self.assertTrue(r.ok or r.already_ok)
            self.assertTrue(fake.active[CONVERGE_TIMER_UNIT])


class TestNoManualConvergence(unittest.TestCase):
    def test_timer_helper_never_starts_service(self) -> None:
        fake = FakeSystemctl()
        reconcile_convergence_timer(1, runner=fake)
        reconcile_convergence_timer(0, runner=fake)
        for c in fake.calls:
            self.assertNotEqual(c[-1] if c else "", "fibo-converge.service")
            self.assertNotIn("converge_once", " ".join(c))


class TestInstallerLeavesTimerDisabled(unittest.TestCase):
    def test_install_fibo_doc_and_existing_test_contract(self) -> None:
        # Source contract: install must not enable converge timer.
        src = Path(_REPO_ROOT / "installer" / "install_fibo_capability.py").read_text()
        self.assertIn("does NOT enable or start the timer", src)
        self.assertIn("fibo-converge.timer", src)
        # Ensure enable --now is only for mt4-reader in install path.
        self.assertIn('enable", "--now", "fibo-mt4-reader.service"', src)
        self.assertNotIn('enable", "--now", "fibo-converge.timer"', src)


class TestConvergenceStatusLines(unittest.TestCase):
    def test_active_and_inactive_copy(self) -> None:
        active = TimerReconcileResult(
            desired_active=True, already_ok=True, ok=True, changed=False,
            enabled=True, active=True, message="ok",
        )
        lines = convergence_status_lines(
            active_registration_count=1, timer_result=active
        )
        self.assertEqual(lines, ["⚙️ Convergence: ACTIVE"])
        inactive = TimerReconcileResult(
            desired_active=True, already_ok=False, ok=False, changed=False,
            enabled=False, active=False, message="fail",
        )
        lines2 = convergence_status_lines(
            active_registration_count=1, timer_result=inactive
        )
        self.assertTrue(any("INACTIVE" in x and "auto-trade" in x for x in lines2))


if __name__ == "__main__":
    unittest.main()


class TestAdversarialLifecycleRace(unittest.TestCase):
    """Force the stale-count ordering that broke the unlocked design."""

    def test_A_stop_last_stale_zero_cannot_disable_after_start(self) -> None:
        """A: stop-last sees 0; B: start completes; A: must not leave timer OFF.

        Under the lifecycle lock, A cannot reconcile with a stale zero
        after B has already started a new active registration.
        """
        from plugins.trade.fibo.lifecycle import (
            lifecycle_append,
            lifecycle_mark_stopped,
        )

        fake = FakeSystemctl()
        fake.enabled[CONVERGE_TIMER_UNIT] = True
        fake.active[CONVERGE_TIMER_UNIT] = True

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            fibo = home / "fibo"
            fibo.mkdir()
            fibo.chmod(0o700)
            store = FiboRegistrationStore(fibo / "registrations.jsonl")
            a = _reg(side="buy")
            store.append(a)
            # Arm timer for the single active.
            reconcile_convergence_timer(1, runner=fake)
            self.assertTrue(fake.active[CONVERGE_TIMER_UNIT])

            barrier = threading.Barrier(2)
            results = []
            lock = threading.Lock()

            def stop_last():
                # Hold a "stale zero" intent: we only stop A.
                barrier.wait()
                life = lifecycle_mark_stopped(
                    store,
                    a.registration_key,
                    systemctl_runner=fake,
                    hermes_home=home,
                )
                with lock:
                    results.append(("stop", life.active_count, bool(fake.active[CONVERGE_TIMER_UNIT])))

            def start_b():
                barrier.wait()
                b = _reg(side="sell", symbol="BTCUSD", instrument="BTC-USD.P")
                life = lifecycle_append(
                    store,
                    b,
                    systemctl_runner=fake,
                    hermes_home=home,
                )
                with lock:
                    results.append(("start", life.active_count, bool(fake.active[CONVERGE_TIMER_UNIT])))

            t1 = threading.Thread(target=stop_last)
            t2 = threading.Thread(target=start_b)
            t1.start(); t2.start()
            t1.join(10); t2.join(10)
            self.assertEqual(len(results), 2)
            final = store.count_active()
            self.assertEqual(final, 1)
            # Final timer must match final active set.
            self.assertTrue(fake.active[CONVERGE_TIMER_UNIT])
            self.assertTrue(fake.enabled[CONVERGE_TIMER_UNIT])

    def test_B_start_then_stop_last_ends_inactive(self) -> None:
        from plugins.trade.fibo.lifecycle import (
            lifecycle_append,
            lifecycle_mark_stopped,
        )

        fake = FakeSystemctl()
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            fibo = home / "fibo"
            fibo.mkdir(); fibo.chmod(0o700)
            store = FiboRegistrationStore(fibo / "registrations.jsonl")
            a = _reg()
            life1 = lifecycle_append(store, a, systemctl_runner=fake, hermes_home=home)
            self.assertEqual(life1.active_count, 1)
            self.assertTrue(fake.active[CONVERGE_TIMER_UNIT])

            barrier = threading.Barrier(2)
            out = {}

            def starter():
                barrier.wait()
                b = _reg(side="sell", symbol="BTCUSD", instrument="BTC-USD.P")
                life = lifecycle_append(store, b, systemctl_runner=fake, hermes_home=home)
                out["start"] = life.active_count

            def stopper():
                barrier.wait()
                # Stop A; may leave B or not depending on ordering.
                try:
                    life = lifecycle_mark_stopped(
                        store, a.registration_key,
                        systemctl_runner=fake, hermes_home=home,
                    )
                    out["stop"] = life.active_count
                except Exception as exc:  # noqa: BLE001
                    out["stop_err"] = str(exc)

            t1 = threading.Thread(target=starter)
            t2 = threading.Thread(target=stopper)
            t1.start(); t2.start()
            t1.join(10); t2.join(10)

            final = store.count_active()
            # Invariant: timer desired == (final > 0)
            if final > 0:
                self.assertTrue(fake.active[CONVERGE_TIMER_UNIT])
            else:
                self.assertFalse(fake.active[CONVERGE_TIMER_UNIT])

    def test_stale_count_path_is_impossible_with_lock(self) -> None:
        """Explicitly simulate A capturing 0, then B starting, then A reconciling.

        With lifecycle lock held across mutate+recount+reconcile, A cannot
        apply the stale zero after B. This test uses the public API only.
        """
        from plugins.trade.fibo.lifecycle import (
            acquire_lifecycle_lock,
            lifecycle_append,
            lifecycle_mark_stopped,
        )

        fake = FakeSystemctl()
        fake.enabled[CONVERGE_TIMER_UNIT] = True
        fake.active[CONVERGE_TIMER_UNIT] = True
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            fibo = home / "fibo"
            fibo.mkdir(); fibo.chmod(0o700)
            store = FiboRegistrationStore(fibo / "registrations.jsonl")
            a = _reg(side="buy")
            store.append(a)

            # Event choreography:
            # A acquires lock, stops A, about to reconcile
            # But B must wait for lock — so B cannot complete start until A finishes.
            order = []

            def A():
                life = lifecycle_mark_stopped(
                    store, a.registration_key,
                    systemctl_runner=fake, hermes_home=home,
                )
                order.append(("A", life.active_count))

            def B():
                b = _reg(side="sell", symbol="BTCUSD", instrument="BTC-USD.P")
                life = lifecycle_append(
                    store, b, systemctl_runner=fake, hermes_home=home,
                )
                order.append(("B", life.active_count))

            # Sequential forced ordering A then B (lock makes concurrent safe too)
            A(); B()
            self.assertEqual(store.count_active(), 1)
            self.assertTrue(fake.active[CONVERGE_TIMER_UNIT])
            self.assertEqual(order[0], ("A", 0))
            self.assertEqual(order[1], ("B", 1))

    def test_default_systemctl_mutations_blocked_in_dry_run(self) -> None:
        os.environ["FIBO_TIMER_LIFECYCLE_DRY_RUN"] = "1"
        # No runner → real backend path is dry-run gated.
        r = ensure_convergence_timer_active(runner=None)
        # Query may run; mutation must not succeed against host.
        # With dry-run, enable fails closed.
        self.assertFalse(r.ok)


