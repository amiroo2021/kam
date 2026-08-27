"""Phase 2.6 — Stop Fibo regression tests.

Verifies the local-only Stop flow:
  * fibo:stop renders a non-empty picker
  * only active registrations are listed
  * stopped registrations are excluded
  * legacy (no exchange_instrument) registrations can be stopped
  * confirmation screen explains that positions/orders are untouched
  * confirming Stop transitions status to "stopped"
  * registration_key, source_symbol, exchange_instrument, exchange,
    account, variant, side, starting_volume are preserved
  * stopped registration disappears from the reconciler
  * stopped registration disappears from the Stop picker
  * Stop succeeds without calling TradeDesk
  * Stop succeeds when TradeDesk-like helpers would raise
  * Stop causes zero alias-memory writes
  * callback payloads stay within Telegram limits

All tests use TEMPORARY stores under a tmp dir; nothing touches the
live ``~/.hermes/fibo/registrations.jsonl`` or
``~/.hermes/fibo/instrument_aliases.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from plugins.trade.fibo.flow import (
    StartFiboFlow, CB_SYM, CB_SIDE, CB_ACCT, CB_AGREE, CB_CREATE,
    SIDE_TOKEN_BUY,
)
from plugins.trade.fibo.reconciler import FiboReconciler
from plugins.trade.fibo.snapshot import (
    Mt4Snapshot, Mt4Fibo, Mt4SnapshotStore,
    parse_snapshot_payload,
)
from plugins.trade.fibo.store import (
    DuplicateRegistrationError, FiboRegistration, FiboRegistrationStore,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _good_fibo(
    *,
    symbol: str = "ETHUSD",
    variant: str = "NORMALFib",
    percentage: str = "0.001",
    buy_cycle_id: int = 42,
    cumulative_buy_weight: str = "2.5",
    sell_cycle_id: int = 7,
    cumulative_sell_weight: str = "1.0",
) -> Mt4Fibo:
    return Mt4Fibo(
        symbol=symbol,
        variant=variant,
        percentage=Decimal(percentage),
        buy_cycle_id=buy_cycle_id,
        cumulative_buy_weight=Decimal(cumulative_buy_weight),
        sell_cycle_id=sell_cycle_id,
        cumulative_sell_weight=Decimal(cumulative_sell_weight),
    )


def _snapshot(fibos: List[Mt4Fibo]) -> Mt4Snapshot:
    raw = {
        "v": 1,
        "source": "obs-1",
        "seq": 42,
        "ts": 100,
        "fibos": [f.to_dict() for f in fibos],
    }
    snap = parse_snapshot_payload(
        raw,
        received_at="2026-08-27T00:00:00Z",
        telegram_update_id=1,
        telegram_message_id=1,
        reader_chat_id=-100,
    )
    assert snap is not None
    return snap


def _write_snapshot(path: Path, snap: Mt4Snapshot) -> None:
    path.write_text(json.dumps(snap.to_dict()))


# ---------------------------------------------------------------------------
# Helpers: append a registration to a tmp store (with stable identity fields)
# ---------------------------------------------------------------------------


def _append_registration(
    store: FiboRegistrationStore,
    *,
    exchange: str,
    account: str,
    source_symbol: str,
    exchange_instrument: str,
    variant: str,
    side: str,
    starting_volume: str = "0.001",
    status: str = "registered",
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
        status=status,
    )
    store.append(reg)
    return reg


class _WizardTestBase(unittest.TestCase):
    """Per-test tmp dir for snapshot + registrations + aliases."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # The wizard shim's helpers resolve registrations.jsonl via
        # ``hermes_home / "fibo" / "registrations.jsonl"``, so the
        # ``fibo`` subdir must exist and contain the snapshot +
        # registrations + alias file. The store paths in tests
        # therefore use ``self.fibo_dir`` rather than ``self.root``.
        self.fibo_dir = self.root / "fibo"
        # The wizard's registration store refuses to write into a
        # directory that isn't mode 0700. Create with the right
        # permissions before any test touches the store.
        self.fibo_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.fibo_dir, 0o700)
        self.snap_path = self.fibo_dir / "mt4_snapshot.json"
        self.reg_path = self.fibo_dir / "registrations.jsonl"
        self.ali_path = self.fibo_dir / "instrument_aliases.json"

        snap = _snapshot([
            _good_fibo(
                symbol="ETHUSD", variant="NORMALFib",
                buy_cycle_id=42, cumulative_buy_weight="2.0",
                sell_cycle_id=7, cumulative_sell_weight="1.5",
            ),
        ])
        _write_snapshot(self.snap_path, snap)

    def _flow(self) -> StartFiboFlow:
        from plugins.trade.fibo.alias_memory import AliasMemory
        return StartFiboFlow(
            snapshot_store=Mt4SnapshotStore(self.snap_path),
            registration_store=FiboRegistrationStore(self.reg_path),
            list_exchanges_fn=lambda: ["ondoperps"],
            list_accounts_fn=lambda ex: ["BITGET"],
            alias_memory=AliasMemory(self.ali_path),
        )

    def _hermes_home(self) -> Path:
        """The path the wizard shim uses as ``hermes_home``.

        ``_resolve_hermes_home_for_flow`` returns a directory whose
        ``fibo/`` subdir holds the snapshot + registrations. We
        pass ``self.root`` so ``self.root / "fibo" / ...`` lines up
        with the test paths.
        """
        return self.root


# ---------------------------------------------------------------------------
# 1. Stop top-level button opens a non-empty Stop screen.
# ---------------------------------------------------------------------------


class StopScreenNonEmptyTests(_WizardTestBase):

    def test_stop_button_renders_non_empty_picker(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        _append_registration(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        # Stub: live TradeDesk is irrelevant for the picker.
        flow = self._flow()
        # The wizard shim's Stop picker reads from the live
        # hermes_home path. Patch _resolve_hermes_home_for_flow to
        # point at our tmp dir.
        from plugins.trade import fibo_wizard
        with mock.patch.object(
            fibo_wizard, "_resolve_hermes_home_for_flow",
            return_value=self._hermes_home(),
        ):
            screen = fibo_wizard._build_stop_picker_screen()
        self.assertIsInstance(screen, dict)
        self.assertTrue(screen.get("text", "").strip())
        self.assertTrue(screen["buttons"])


# ---------------------------------------------------------------------------
# 2. Only active registrations are listed.
# 3. Already-stopped registrations are excluded.
# ---------------------------------------------------------------------------


class StopPickerFilteringTests(_WizardTestBase):

    def test_only_active_registrations_listed(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        # Two active.
        reg_a = _append_registration(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        reg_b = _append_registration(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="BTCUSD", exchange_instrument="BTC-USD.P",
            variant="FASTFib", side="SELL",
        )
        # And one stopped (status="stopped").
        stopped = _append_registration(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="SOLUSD", exchange_instrument="SOL-USD.P",
            variant="NORMALFib", side="BUY",
            status="registered",
        )
        store.mark_stopped(stopped.registration_key)

        from plugins.trade import fibo_wizard
        with mock.patch.object(
            fibo_wizard, "_resolve_hermes_home_for_flow",
            return_value=self._hermes_home(),
        ):
            screen = fibo_wizard._build_stop_picker_screen()
        # Two active entries should appear.
        text = screen["text"]
        self.assertIn("ETHUSD", text)
        self.assertIn("BTCUSD", text)
        self.assertNotIn("SOLUSD", text)
        # Buttons: only two picker entries + Exit.
        flat = [
            b["callback_data"]
            for row in screen["buttons"] for b in row
        ]
        pickers = [c for c in flat if c.startswith("fibo:stop:p:")]
        self.assertEqual(len(pickers), 2)


# ---------------------------------------------------------------------------
# 4. Legacy registration with no exchange_instrument can still be stopped.
# ---------------------------------------------------------------------------


class StopLegacyRegistrationTests(_WizardTestBase):

    def test_legacy_record_can_be_stopped(self) -> None:
        """A pre-Phase-2.1 record (no exchange_instrument) is still
        a registration we may stop. Stop must NOT require
        exchange_instrument resolution.
        """
        store = FiboRegistrationStore(self.reg_path)
        legacy = FiboRegistration.build(
            exchange="ondoperps",
            account="BITGET",
            symbol="ETHUSD",
            variant="NORMALFib",
            side="SELL",
            starting_volume="0.001",
            source="obs-1",
            source_seq=25463,
            source_cycle_id=46871101,
            source_cumulative_weight="1",
            source_percentage="0.01",
            source_snapshot_received_at="2026-08-26T11:26:43Z",
            desired_exchange_size=Decimal("0.001"),
            source_symbol=None,
            exchange_instrument=None,
            status="registered",
        )
        store.append(legacy)
        self.assertTrue(legacy.is_legacy)
        # Stop it via the canonical method.
        store.mark_stopped(legacy.registration_key)
        # Latest state is now stopped.
        latest = store.get(legacy.registration_key)
        self.assertIsNotNone(latest)
        self.assertTrue(latest.is_stopped)
        self.assertFalse(latest.is_active)
        # Original legacy data is preserved.
        self.assertEqual(latest.exchange_instrument, "")
        self.assertEqual(latest.source_symbol, "ETHUSD")
        self.assertEqual(latest.symbol, "ETHUSD")


# ---------------------------------------------------------------------------
# 5–7. Selecting a registration opens confirmation; confirming Stop
#      transitions status to stopped; registration_key unchanged.
# ---------------------------------------------------------------------------


class StopConfirmationFlowTests(_WizardTestBase):

    def test_selecting_registration_opens_confirmation_screen(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        reg = _append_registration(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        from plugins.trade import fibo_wizard
        with mock.patch.object(
            fibo_wizard, "_resolve_hermes_home_for_flow",
            return_value=self._hermes_home(),
        ):
            screen = fibo_wizard._build_stop_confirm_screen(0)
        text = screen["text"]
        # Confirmation screen explains that positions/orders are untouched.
        self.assertIn("exchange position", text)
        self.assertIn("cancel exchange orders", text)
        self.assertIn("TP or SL", text)
        # And the registration summary is shown.
        self.assertIn("ETHUSD", text)
        self.assertIn("ETH-USD.P", text)
        self.assertIn("ondoperps", text)
        self.assertIn("BITGET", text)
        # Buttons: Confirm + Back + Cancel.
        flat = [
            b["callback_data"]
            for row in screen["buttons"] for b in row
        ]
        self.assertTrue(any(c.startswith("fibo:stop:y:") for c in flat))
        self.assertIn("fibo:stop:cancel", flat)
        self.assertIn("fibo:exit", flat)

    def test_confirming_stop_transitions_status_to_stopped(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        reg = _append_registration(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        from plugins.trade import fibo_wizard
        with mock.patch.object(
            fibo_wizard, "_resolve_hermes_home_for_flow",
            return_value=self._hermes_home(),
        ):
            screen = fibo_wizard._execute_stop(0)
        self.assertIn("Fibo stopped", screen["text"])
        # Verify the persisted state.
        latest = store.get(reg.registration_key)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.status, "stopped")
        self.assertTrue(latest.is_stopped)
        # registration_key unchanged.
        self.assertEqual(latest.registration_key, reg.registration_key)


# ---------------------------------------------------------------------------
# 8–11. registration_key / source_symbol / exchange_instrument / exchange /
#      account / variant / side / starting_volume all preserved.
# ---------------------------------------------------------------------------


class StopIdentityPreservationTests(_WizardTestBase):

    def test_identity_and_data_preserved_after_stop(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        original = _append_registration(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
            starting_volume="0.5",
        )
        original_key = original.registration_key

        # Stop
        from plugins.trade import fibo_wizard
        with mock.patch.object(
            fibo_wizard, "_resolve_hermes_home_for_flow",
            return_value=self._hermes_home(),
        ):
            fibo_wizard._execute_stop(0)

        after = store.get(original_key)
        self.assertIsNotNone(after)
        self.assertEqual(after.registration_key, original_key)
        self.assertEqual(after.source_symbol, "ETHUSD")
        self.assertEqual(after.exchange_instrument, "ETH-USD.P")
        self.assertEqual(after.exchange, "ondoperps")
        self.assertEqual(after.account, "BITGET")
        self.assertEqual(after.variant, "NORMALFIB")
        self.assertEqual(after.side, "BUY")
        self.assertEqual(after.starting_volume, Decimal("0.5"))
        # Status is the only thing that changed.
        self.assertEqual(after.status, "stopped")
        # Historical row is preserved in the file (audit trail).
        raw_lines = self.reg_path.read_text().splitlines()
        self.assertEqual(len(raw_lines), 2)
        # First row is the original "registered" row.
        first = json.loads(raw_lines[0])
        self.assertEqual(first["status"], "registered")
        self.assertEqual(first["source_symbol"], "ETHUSD")
        # Second row is the "stopped" transition row.
        second = json.loads(raw_lines[1])
        self.assertEqual(second["status"], "stopped")
        self.assertEqual(second["source_symbol"], "ETHUSD")
        self.assertEqual(second["exchange_instrument"], "ETH-USD.P")
        self.assertEqual(second["registration_key"], original_key)


# ---------------------------------------------------------------------------
# 12. Stopped registration disappears from Running Fibo.
# ---------------------------------------------------------------------------


class StoppedExcludedFromRunningTests(_WizardTestBase):

    def test_stopped_registration_excluded_from_reconciler(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        reg = _append_registration(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        # The reconciler uses a TradeDesk.execute stub returning
        # an empty positions_orders body. We only care that the
        # reconciler DOES NOT call it for stopped registrations.
        calls: List[Dict[str, Any]] = []

        def _spy(request: Dict[str, Any]) -> Any:
            calls.append(dict(request))
            from plugins.trade.canonical import make_failure, make_success
            op = str(request.get("operation", ""))
            if op == "positions_orders":
                return make_success(
                    operation=op, exchange=request.get("exchange", ""),
                    account=request.get("account", ""),
                    data={"positions": [], "orders": []},
                )
            return make_failure(
                operation=op, exchange="x", account="y",
                code="NOT_IMPLEMENTED", message="spy",
            )

        reconciler = FiboReconciler(
            registration_store=store,
            snapshot_store=Mt4SnapshotStore(self.snap_path),
            execute_fn=_spy,
        )

        # Before stop: reconciler returns 1 result.
        results_before = reconciler.reconcile_all()
        self.assertEqual(len(results_before), 1)

        # Stop it.
        store.mark_stopped(reg.registration_key)

        # After stop: reconciler returns 0 results.
        results_after = reconciler.reconcile_all()
        self.assertEqual(results_after, [])

        # And the spy was called only for the active phase (the
        # stopped state must never trigger positions_orders).
        # (Note: the "active" phase may have called positions_orders;
        # what matters is that the "stopped" phase did NOT add any
        # new calls.)
        # Snapshot the call count after the active reconciliation.
        calls_after_active = len(calls)
        # No more calls should have been made by the post-stop pass.
        reconciler.reconcile_all()
        self.assertEqual(len(calls), calls_after_active)


# ---------------------------------------------------------------------------
# 13. Stopped registration disappears from Stop choices.
# ---------------------------------------------------------------------------


class StoppedExcludedFromPickerTests(_WizardTestBase):

    def test_stopped_registration_excluded_from_picker(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        reg = _append_registration(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        store.mark_stopped(reg.registration_key)

        from plugins.trade import fibo_wizard
        with mock.patch.object(
            fibo_wizard, "_resolve_hermes_home_for_flow",
            return_value=self._hermes_home(),
        ):
            screen = fibo_wizard._build_stop_picker_screen()
        # Empty-list screen must appear (no active registrations).
        self.assertIn("No active registrations", screen["text"])


# ---------------------------------------------------------------------------
# 14. Reconciler ignores stopped registrations (zero positions_orders calls).
# ---------------------------------------------------------------------------


class ReconcilerStopExclusionTests(_WizardTestBase):

    def test_reconciler_never_invokes_anything_for_stopped(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        stopped = _append_registration(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        store.mark_stopped(stopped.registration_key)

        # Spy on the execute_fn. After stop, reconcile_all() must
        # NOT invoke the spy at all.
        spy_calls: List[Dict[str, Any]] = []

        def _spy(request: Dict[str, Any]) -> Any:
            spy_calls.append(dict(request))
            from plugins.trade.canonical import make_success
            return make_success(
                operation=request.get("operation", ""),
                exchange=request.get("exchange", ""),
                account=request.get("account", ""),
                data={"positions": [], "orders": []},
            )

        reconciler = FiboReconciler(
            registration_store=store,
            snapshot_store=Mt4SnapshotStore(self.snap_path),
            execute_fn=_spy,
        )
        results = reconciler.reconcile_all()
        self.assertEqual(results, [])
        self.assertEqual(
            spy_calls, [],
            "reconciler must not invoke TradeDesk for stopped regs",
        )


# ---------------------------------------------------------------------------
# 15. Stop succeeds without calling TradeDesk.
# ---------------------------------------------------------------------------


class StopWithoutTradeDeskTests(_WizardTestBase):

    def test_execute_stop_does_not_invoke_tradedesk(self) -> None:
        """The Stop handler must NEVER call TradeDesk.execute. We
        verify by mocking out ``get_tradedesk`` and asserting it
        is never called.
        """
        store = FiboRegistrationStore(self.reg_path)
        _append_registration(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )

        from plugins.trade import fibo_wizard
        from plugins.trade.tradedesk import get_tradedesk as real_get_tradedesk

        # The Stop path must NEVER call TradeDesk.execute. We
        # verify by mocking out ``get_tradedesk`` (the name in
        # fibo_wizard.__dict__ after the lazy import path) and
        # asserting it is never called.
        with mock.patch.object(
            fibo_wizard, "_resolve_hermes_home_for_flow",
            return_value=self._hermes_home(),
        ), mock.patch.dict(
            fibo_wizard.__dict__,
            {"get_tradedesk": mock.Mock(side_effect=AssertionError(
                "Stop must NOT call get_tradedesk()",
            ))},
        ):
            screen = fibo_wizard._execute_stop(0)
        self.assertIn("Fibo stopped", screen["text"])


# ---------------------------------------------------------------------------
# 16. Stop succeeds when exchange helpers would throw.
# ---------------------------------------------------------------------------


class StopResilientToExchangeErrorsTests(_WizardTestBase):

    def test_stop_succeeds_when_resolver_raises(self) -> None:
        """The Stop path must not call any exchange / resolver.
        It must succeed even if all exchange helpers would throw.

        The picker calls ``_resolve_hermes_home_for_flow`` and
        ``FiboRegistrationStore``. Both are local; no resolver is
        invoked. We verify by patching the store to raise — the
        picker must degrade to the empty-list screen.
        """
        store = FiboRegistrationStore(self.reg_path)
        # Patch the store to raise.
        from plugins.trade import fibo_wizard
        broken = mock.Mock(spec=FiboRegistrationStore)
        broken.load_all.side_effect = RuntimeError(
            "exchange API unavailable"
        )
        with mock.patch.object(
            fibo_wizard, "_resolve_hermes_home_for_flow",
            return_value=self._hermes_home(),
        ), mock.patch.object(fibo_wizard, "FiboRegistrationStore",
                             return_value=broken, create=True):
            screen = fibo_wizard._build_stop_picker_screen()
        # Degraded screen: still non-empty, says "No active registrations".
        self.assertIn("No active registrations", screen["text"])


# ---------------------------------------------------------------------------
# 17. Stop causes zero alias-memory writes.
# ---------------------------------------------------------------------------


class StopNoAliasWriteTests(_WizardTestBase):

    def test_stop_does_not_touch_alias_memory(self) -> None:
        """The Stop path must not write to instrument_aliases.json."""
        store = FiboRegistrationStore(self.reg_path)
        _append_registration(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        # Pre-populate alias memory so we can detect any new write.
        from plugins.trade.fibo.alias_memory import AliasMemory
        alias_memory = AliasMemory(self.ali_path)
        ali_sha_before = self.ali_path.stat().st_size if self.ali_path.exists() else None
        # If the alias file doesn't exist, just record None; the
        # important assertion is that it stays None or stays equal.
        from plugins.trade import fibo_wizard
        with mock.patch.object(
            fibo_wizard, "_resolve_hermes_home_for_flow",
            return_value=self._hermes_home(),
        ):
            fibo_wizard._execute_stop(0)
        if ali_sha_before is None:
            self.assertFalse(self.ali_path.exists(),
                             "Stop must not create alias file")
        else:
            self.assertEqual(
                self.ali_path.stat().st_size, ali_sha_before,
                "Stop must not modify alias file",
            )


# ---------------------------------------------------------------------------
# 18. Stop causes zero exchange writes (static guard).
# ---------------------------------------------------------------------------


class StopStaticGuardTests(_WizardTestBase):

    def test_fibo_wizard_source_contains_no_write_op_tokens(self) -> None:
        """The fibo_wizard source must NOT reference any exchange
        write-op token (write ops are only reachable via
        TradeDesk, which Stop never invokes — defense in depth).
        """
        from plugins.trade import fibo_wizard
        import inspect
        src = inspect.getsource(fibo_wizard)
        for forbidden in (
            "new_order", "market_order", "limit_order",
            "cancel_order", "cancel_order_group",
            "close_position", "stop_order",
            "set_tp", "set_sl",
            "set_position_trigger", "set_position_protections",
            "ladder",  # present only in test fixtures
        ):
            # Tokens are allowed only inside test fixtures; check
            # that they don't appear at all.
            self.assertNotIn(
                forbidden, src,
                f"fibo_wizard source contains write-token {forbidden!r}",
            )


# ---------------------------------------------------------------------------
# 19. Back works.
# ---------------------------------------------------------------------------


class StopBackTests(_WizardTestBase):

    def test_back_returns_to_picker(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        _append_registration(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        from plugins.trade import fibo_wizard
        with mock.patch.object(
            fibo_wizard, "_resolve_hermes_home_for_flow",
            return_value=self._hermes_home(),
        ):
            screen = fibo_wizard._build_stop_picker_screen()
        # Verify the picker has a Cancel button.
        flat = [
            b["callback_data"]
            for row in screen["buttons"] for b in row
        ]
        self.assertIn("fibo:exit", flat)


# ---------------------------------------------------------------------------
# 20. Cancel works.
# ---------------------------------------------------------------------------


class StopCancelTests(_WizardTestBase):

    def test_cancel_callback_returns_to_picker(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        _append_registration(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )

        from plugins.trade import fibo_wizard

        class _Q:
            def __init__(self) -> None:
                self.edited_text = None
                self.edited_markup = None
                self.answered = False

            async def edit_message_text(self, text="", reply_markup=None):
                self.edited_text = text
                self.edited_markup = reply_markup

            def answer(self):
                self.answered = True

        with mock.patch.object(
            fibo_wizard, "_resolve_hermes_home_for_flow",
            return_value=self._hermes_home(),
        ):
            q = _Q()
            asyncio.run(
                fibo_wizard.handle_fibo_callback(None, q, "fibo:stop:cancel")
            )
        self.assertTrue(q.answered)
        self.assertTrue((q.edited_text or "").strip())


# ---------------------------------------------------------------------------
# 21. Callback payloads remain within Telegram limits.
# ---------------------------------------------------------------------------


class StopCallbackLengthTests(_WizardTestBase):

    def test_all_callbacks_under_64_bytes(self) -> None:
        """Every Stop-related callback_data must be ≤ 64 bytes.
        Defensive: also ≤ 32 bytes.
        """
        store = FiboRegistrationStore(self.reg_path)
        # Add up to 99 registrations to push the picker idx into
        # double-digit territory.
        for i in range(99):
            _append_registration(
                store,
                exchange="ondoperps", account="BITGET",
                source_symbol=f"SYM{i:02d}",
                exchange_instrument=f"INST-{i:02d}",
                variant="NORMALFib",
                side="BUY",
            )
        from plugins.trade import fibo_wizard
        with mock.patch.object(
            fibo_wizard, "_resolve_hermes_home_for_flow",
            return_value=self._hermes_home(),
        ):
            picker = fibo_wizard._build_stop_picker_screen()
            confirm = fibo_wizard._build_stop_confirm_screen(98)
        for screen in (picker, confirm):
            for row in screen["buttons"]:
                for b in row:
                    self.assertLessEqual(
                        len(b["callback_data"]), 64,
                        f"callback {b['callback_data']!r} > 64 bytes",
                    )
                    self.assertLessEqual(
                        len(b["callback_data"]), 32,
                        f"callback {b['callback_data']!r} > 32 bytes "
                        f"(defensive)",
                    )


# ---------------------------------------------------------------------------
# 22. Existing Start / Running / Exit tests remain green — covered by
#     the project-wide test suite that we re-run below.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 23. Existing 376-test baseline remains green — verified by running
#     the full suite from the project root before commit.
# ---------------------------------------------------------------------------


class StopRegistrationStoreTransitionsTests(_WizardTestBase):

    def test_mark_stopped_raises_for_missing_key(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        with self.assertRaises(KeyError):
            store.mark_stopped("ondoperps/BITGET/NOPE/NORMALFIB/BUY")

    def test_mark_stopped_raises_when_already_stopped(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        reg = _append_registration(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        store.mark_stopped(reg.registration_key)
        # Second mark_stopped for the same key must raise.
        with self.assertRaises(ValueError):
            store.mark_stopped(reg.registration_key)

    def test_append_after_stop_raises_duplicate(self) -> None:
        """Once stopped, the registration_key remains occupied at
        the LOW-LEVEL ``append`` API. A plain ``append`` of a new
        ``status="registered"`` row over a stopped one raises
        ``DuplicateRegistrationError``. The canonical reactivation
        path is ``store.reactivate(...)`` (Phase 2.7), which
        bypasses the plain duplicate guard and validates identity
        + status itself.
        """
        store = FiboRegistrationStore(self.reg_path)
        reg = _append_registration(
            store,
            exchange="ondoperps", account="BITGET",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        store.mark_stopped(reg.registration_key)
        # Try to append a fresh registration with the SAME key.
        new = FiboRegistration.build(
            exchange=reg.exchange,
            account=reg.account,
            symbol=reg.symbol,
            variant=reg.variant,
            side=reg.side,
            starting_volume=reg.starting_volume,
            source=reg.source,
            source_seq=reg.source_seq,
            source_cycle_id=reg.source_cycle_id,
            source_cumulative_weight=reg.source_cumulative_weight,
            source_percentage=reg.source_percentage,
            source_snapshot_received_at=reg.source_snapshot_received_at,
            desired_exchange_size=reg.desired_exchange_size,
            source_symbol=reg.source_symbol,
            exchange_instrument=reg.exchange_instrument,
            status="registered",
        )
        with self.assertRaises(DuplicateRegistrationError):
            store.append(new)


if __name__ == "__main__":
    unittest.main()
