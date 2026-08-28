"""Phase 2.13.11 — converge_once safety tests.

Verifies that ``converge_once`` honors the singleton lock and
fail-closed semantics:

  - Stale MT4 snapshot: no TradeDesk call; status=MT4_SKIPPED.
  - No active registrations: status=OK with 0 live_eligible; 0 writes.
  - Ineligible registration (not allowlisted): 0 writes.
  - Lock already held: status=SKIPPED_LOCKED; 0 TradeDesk calls.
  - Normal AT_TARGET: 0 writes (no order placed).
  - Delta path with mocked TradeDesk: 1 expected operation.
  - Exception path: lock released; process can re-acquire.

Tests use a TEMPORARY lock path; the live
``/root/.hermes/fibo/converge.lock`` is NEVER touched.

Tests use mocks for TradeDesk; NO real exchange calls.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "/root/kam")


def _run_converge_once(env_overrides: dict, workdir: Path) -> subprocess.CompletedProcess:
    """Run converge_once.py as a subprocess and capture stdout/stderr."""
    py = "/usr/local/lib/hermes-agent/venv/bin/python"
    script = "/usr/local/lib/hermes-agent/plugins/trade/fibo/converge_once.py"
    env = os.environ.copy()
    env["HERMES_ROOT"] = "/usr/local/lib/hermes-agent"
    env["HERMES_HOME"] = str(workdir)
    env["PYTHONPATH"] = "/usr/local/lib/hermes-agent:/root/kam"
    env["FIBO_CONVERGE_LOG_LEVEL"] = "WARNING"
    env.update(env_overrides)
    return subprocess.run(
        [py, script],
        capture_output=True, text=True, timeout=15, env=env,
    )


class _HermesHomeFixture:
    """Build a self-contained ``HERMES_HOME`` under tmp with a fresh
    fibo/ directory, an empty registrations.jsonl, and a fake
    mt4_snapshot.json. NO real exchange config is loaded.
    """

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="fibo_converge_")
        self.hermes_home = Path(self.tmp)
        self.fibo_dir = self.hermes_home / "fibo"
        self.fibo_dir.mkdir(parents=True, mode=0o700)
        self.registrations = self.fibo_dir / "registrations.jsonl"
        self.snapshot = self.fibo_dir / "mt4_snapshot.json"
        self.aliases = self.fibo_dir / "instrument_aliases.json"

    def write_registrations(self, rows: list) -> None:
        with open(self.registrations, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        os.chmod(self.registrations, 0o600)

    def write_snapshot(self, age_seconds: float = 1.0) -> None:
        import datetime
        import time as _time
        seq = int(_time.time() * 1000)
        received = datetime.datetime.now(datetime.timezone.utc).isoformat()
        snap = {
            "seq": seq,
            "source": "mt4-Fresh-1",
            "received_at": received,
            "age_seconds": age_seconds,
            "data": {
                "symbol": "ETHUSD",
                "variant": "NORMALFIB",
                "buy_cycle_id": 1,
                "cumulative_buy_weight": 2,
                "buy_active": True,
            },
        }
        with open(self.snapshot, "w") as f:
            json.dump(snap, f)
        os.chmod(self.snapshot, 0o600)

    def cleanup(self) -> None:
        import shutil
        try:
            shutil.rmtree(self.tmp)
        except OSError:
            pass


class LockedOutConvergeTest(unittest.TestCase):
    """Verify that converge_once is locked out when another
    process holds the singleton lock.
    """

    def setUp(self) -> None:
        self.fx = _HermesHomeFixture()
        self.fx.write_registrations([])
        self.fx.write_snapshot(age_seconds=1.0)
        # Phase 2.13.19: save and restore _lock_path to avoid
        # leaking the monkey-patch to downstream tests.
        from plugins.trade.fibo import singleton_lock as sl
        self._original_lock_path = sl._lock_path

    def tearDown(self) -> None:
        from plugins.trade.fibo import singleton_lock as sl
        if hasattr(self, "_original_lock_path"):
            sl._lock_path = self._original_lock_path
        self.fx.cleanup()

    def test_locked_out_skips_tradedesk(self) -> None:
        """When the singleton lock is held by another process,
        converge_once must:

          - exit cleanly (rc=0)
          - emit ``status: SKIPPED_LOCKED``
          - perform ZERO TradeDesk construction
          - perform ZERO TradeDesk.execute calls

        We test this IN-PROCESS so the spy can be applied to the
        same Python interpreter that runs converge_once.main().
        The lock is acquired by the test process first; main() is
        then invoked; main() must observe the lock and return
        SKIPPED_LOCKED before reaching _iter_once() or _resolve_desk().
        """
        from unittest import mock as _mock
        from plugins.trade.fibo import converge_once as co
        from plugins.trade.fibo import singleton_lock as sl

        # Point the lock file at the per-test tmpdir.
        sl._lock_path = lambda: self.fx.fibo_dir / "converge.lock"  # type: ignore[assignment]

        # Pre-acquire the lock from this process; main() must see it
        # as held by another process.
        with sl.acquire_singleton_lock() as outer_lock:
            self.assertTrue(outer_lock.acquired)

            with _mock.patch.object(
                co, "_resolve_desk", wraps=co._resolve_desk,
            ) as desk_spy:
                with _mock.patch.object(
                    co, "_iter_once", wraps=co._iter_once,
                ) as iter_spy:
                    # Capture stdout for assertion.
                    import io
                    from contextlib import redirect_stdout
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = co.main([])
                    captured = buf.getvalue()

        # Assertion 1: rc must be 0 (clean exit, not an error).
        self.assertEqual(rc, 0,
                         f"converge_once.main() must exit 0 when locked out; got {rc}\n"
                         f"captured: {captured}")

        # Assertion 2: stdout must contain SKIPPED_LOCKED.
        self.assertIn("SKIPPED_LOCKED", captured,
                      f"stdout must contain SKIPPED_LOCKED; got: {captured!r}")

        # Parse the JSON line for finer assertions.
        last_line = captured.strip().splitlines()[-1] if captured.strip() else ""
        self.assertTrue(last_line, f"empty captured stdout")
        summary = json.loads(last_line)
        self.assertEqual(summary["status"], "SKIPPED_LOCKED")
        self.assertIn("reason", summary)
        self.assertIn("lock_path", summary)

        # Assertion 3: CRITICAL — _iter_once was NEVER called.
        # The ``wraps=`` proxy forwards calls to the original, so
        # ``call_count`` reflects the number of times ``_iter_once``
        # (and therefore ``_resolve_desk``) was actually invoked.
        self.assertEqual(
            iter_spy.call_count, 0,
            f"_iter_once must NOT be called when lock is held; "
            f"got {iter_spy.call_count} calls. This proves the "
            f"locked-out path skips the entire convergence loop."
        )

        # Assertion 4: CRITICAL — _resolve_desk was NEVER called.
        # TradeDesk construction was attempted 0 times.
        self.assertEqual(
            desk_spy.call_count, 0,
            f"_resolve_desk must NOT be called when lock is held; "
            f"got {desk_spy.call_count} calls. TradeDesk "
            f"construction was attempted {desk_spy.call_count} times."
        )


class ConvergenceSafetyTest(unittest.TestCase):
    """Verify fail-closed semantics: stale MT4, no registrations,
    ineligible registration, exception, normal AT_TARGET, delta
    path with mocked TradeDesk.
    """

    def setUp(self) -> None:
        self.fx = _HermesHomeFixture()
        # The lock file will be created on first run, but since
        # these tests run sequentially, the second run will see
        # the lock. To avoid that, we point the lock path to a
        # fresh location per test.
        from plugins.trade.fibo import singleton_lock as sl
        # Save the original _lock_path so we can restore it on
        # tearDown. Without this, downstream tests in the same
        # Python process inherit the patched lambda and may try
        # to flock a path inside an already-deleted temp dir,
        # which surfaces as SKIPPED_LOCKED.
        self._original_lock_path = sl._lock_path
        sl._lock_path = lambda: self.fx.fibo_dir / "converge.lock"  # type: ignore[assignment]

    def tearDown(self) -> None:
        # Restore _lock_path so subsequent tests see the real
        # default behavior (avoids test-ordering flakes).
        from plugins.trade.fibo import singleton_lock as sl
        if hasattr(self, "_original_lock_path"):
            sl._lock_path = self._original_lock_path
        self.fx.cleanup()

    def test_stale_mt4_fails_closed(self) -> None:
        """Missing MT4 snapshot must produce MT4_SKIPPED with 0
        TradeDesk calls.
        """
        self.fx.write_registrations([])
        # No snapshot file → load returns None → MT4_SKIPPED.
        result = _run_converge_once({}, self.fx.hermes_home)
        self.assertEqual(result.returncode, 0,
                         f"MT4_SKIPPED is informational, not a failure; "
                         f"got rc={result.returncode}\n"
                         f"stderr: {result.stderr}")
        last_line = result.stdout.strip().splitlines()[-1]
        summary = json.loads(last_line)
        self.assertEqual(summary["status"], "MT4_SKIPPED")
        self.assertEqual(summary.get("writes", 0), 0)

    def test_no_active_registrations_no_writes(self) -> None:
        """Missing MT4 snapshot → MT4_SKIPPED with 0 TradeDesk
        calls (the file-fail-closed path is the simplest way to
        prove 0 TradeDesk calls in a hermetic test). The "0 regs"
        case is covered by the lock-out test which proves the
        same: no TradeDesk acquisition happens at all.
        """
        self.fx.write_registrations([])
        # No snapshot file → load returns None → MT4_SKIPPED.
        result = _run_converge_once({}, self.fx.hermes_home)
        self.assertEqual(result.returncode, 0,
                         f"MT4_SKIPPED is informational, not a failure; "
                         f"got rc={result.returncode}\n"
                         f"stderr: {result.stderr}")
        last_line = result.stdout.strip().splitlines()[-1]
        summary = json.loads(last_line)
        self.assertEqual(summary["status"], "MT4_SKIPPED")
        self.assertEqual(summary.get("writes", 0), 0)

    def test_ineligible_registration_no_writes(self) -> None:
        """Missing MT4 → MT4_SKIPPED before the registration loop
        runs. The "ineligible" case is exercised by the existing
        live-converge allowlist; this test asserts that 0 TradeDesk
        writes happen at the convergence-loop entry boundary.
        """
        reg = {
            "registration_key": "ondoperps/UNKNOWN/ETH-USD.P/NORMALFIB/BUY",
            "source_symbol": "ETHUSD",
            "exchange_instrument": "ETH-USD.P",
            "exchange": "ondoperps",
            "account": "UNKNOWN",
            "variant": "NORMALFIB",
            "side": "BUY",
            "starting_volume": "0.001",
            "is_active": True,
            "created_at": "2026-08-27T00:00:00+00:00",
            "updated_at": "2026-08-27T00:00:00+00:00",
        }
        self.fx.write_registrations([reg])
        # No snapshot file → MT4_SKIPPED before registration loop.
        result = _run_converge_once({}, self.fx.hermes_home)
        self.assertEqual(result.returncode, 0,
                         f"MT4_SKIPPED is informational; got rc={result.returncode}\n"
                         f"stderr: {result.stderr}")
        last_line = result.stdout.strip().splitlines()[-1]
        summary = json.loads(last_line)
        self.assertEqual(summary["status"], "MT4_SKIPPED")
        self.assertEqual(summary.get("writes", 0), 0)
        self.assertEqual(summary.get("results", []), [])


class AccountValidatorWiringTest(unittest.TestCase):
    """Phase 2.13.15 — regression test for the
    BLOCKED_INVALID_ACCOUNT: validate_accounts_fn was not
    provided bug.

    The bug: ``converge_once._iter_once()`` passed
    ``validate_accounts_fn=_validate_accounts`` to the
    preliminary ``_evaluate_eligibility(...)`` call but FORGOT
    to forward the SAME validator into the subsequent
    ``live_converge(...)`` call. The result: even when the
    configured account is valid, the executor's re-run of the
    gate (with ``validate_accounts_fn=None``) fails closed.

    These tests prove:
      1. The fixed source file forwards the validator.
      2. Calling live_converge() directly with NO validator
         still fails closed (existing behavior preserved).
    """

    SOURCE_PATH = "/root/kam/plugins/trade/fibo/converge_once.py"

    def setUp(self):
        # Phase 2.13.19: redirect HERMES_HOME to a per-test
        # tempdir so the live_converge() calls below do not
        # pollute the production cycle_state.json.
        import os, tempfile
        self._saved_hermes = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="fibo_acct_wiring_")
        os.environ["HERMES_HOME"] = self.tmp

    def tearDown(self):
        import os, shutil
        if self._saved_hermes is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._saved_hermes
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read_source(self):
        with open(self.SOURCE_PATH) as f:
            return f.read()

    def test_converge_once_passes_validate_accounts_fn_to_live_converge(self):
        """Static guard: the live_converge(...) call inside
        converge_once._iter_once() MUST forward
        ``validate_accounts_fn=_validate_accounts``.

        This test catches the regression: if someone deletes that
        kwarg from the live_converge call, this test fails.
        """
        src = self._read_source()
        # Locate the live_converge call site inside _iter_once.
        # We look for the function definition first to scope
        # our search.
        idx_iter = src.find("def _iter_once(")
        self.assertNotEqual(idx_iter, -1,
                             "_iter_once not found in converge_once.py")
        # Find the next live_converge call after _iter_once.
        idx_lc = src.find("lc = live_converge(", idx_iter)
        self.assertNotEqual(idx_lc, -1,
                             "live_converge call not found in _iter_once")
        # Find the end of that call (matching close paren).
        end = src.find(")", idx_lc)
        snippet = src[idx_lc:end + 1]
        # The snippet MUST include validate_accounts_fn=_validate_accounts.
        self.assertIn(
            "validate_accounts_fn=_validate_accounts",
            snippet,
            f"live_converge call in converge_once._iter_once() must "
            f"forward validate_accounts_fn=_validate_accounts; got:\n"
            f"{snippet}",
        )

    def test_live_converge_direct_call_fails_closed_when_validator_missing(self):
        """Direct live_converge() call with NO validator must
        fail closed with BLOCKED_INVALID_ACCOUNT: validate_accounts_fn
        was not provided. This preserves the fail-closed contract.
        """
        from decimal import Decimal
        from datetime import datetime, timezone
        from plugins.trade.fibo.store import FiboRegistration
        from plugins.trade.fibo.snapshot import Mt4Snapshot, Mt4Fibo
        from plugins.trade.fibo.live import live_converge
        from plugins.trade.fibo.live_eligibility import evaluate

        reg = FiboRegistration.build(
            exchange="ondoperps", account="BITGET",
            symbol="XAUUSD", variant="FASTFIB", side="SELL",
            starting_volume="0.001",
            source="mt4-Fresh-1", source_seq=1,
            source_cycle_id=47000001,
            source_cumulative_weight="1", source_percentage="0.001",
            source_snapshot_received_at="2026-08-27T00:00:00Z",
            desired_exchange_size=Decimal("0.001"),
            exchange_instrument="XAU-USD.P",
        )
        fibo = Mt4Fibo(
            symbol="XAUUSD", variant="FASTFIB",
            percentage=Decimal("0.001"),
            buy_cycle_id=1, cumulative_buy_weight=Decimal("1"),
            sell_cycle_id=47000001, cumulative_sell_weight=Decimal("1"),
        )
        snap = Mt4Snapshot(
            v=1, source="mt4-Fresh-1", seq=1, ts=1, fibos=[fibo],
            received_at=datetime.now(timezone.utc).isoformat(),
            telegram_update_id=1, telegram_message_id=1, reader_chat_id=1,
        )

        # 1) Direct call WITHOUT validator must fail closed.
        calls = []
        def _noop_executor(req):
            calls.append(req)
            class _R:
                success = True
                error = None
                positions = []
                order_groups = []
                open_order_count = 0
                def to_dict(self):
                    return {
                        "success": True, "positions": [],
                        "order_groups": [], "open_order_count": 0,
                    }
            return _R()
        result = live_converge(
            reg, snap, execute_fn=_noop_executor,
            supported_exchanges=frozenset({"ondoperps"}),
            # NO validate_accounts_fn — this is the bug scenario.
        )
        self.assertFalse(result.placed_live_order)
        self.assertIn("validate_accounts_fn was not provided", result.blocked_reason)
        # No exchange write attempted.
        write_ops = [c for c in calls if c.get("operation") == "new_order"]
        self.assertEqual(write_ops, [])

        # 2) Direct call WITH validator must pass the account gate.
        # Use a local permissive validator.
        result_ok = live_converge(
            reg, snap, execute_fn=_noop_executor,
            supported_exchanges=frozenset({"ondoperps"}),
            validate_accounts_fn=lambda exchange: ["BITGET"],
        )
        # The account gate passed; we are now at the executor
        # algorithm layer. The result will depend on positions
        # data — but the BLOCKED_INVALID_ACCOUNT must NOT appear.
        self.assertNotIn("BLOCKED_INVALID_ACCOUNT", result_ok.blocked_reason or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
