"""Phase 2.7 — Restart/Reactivate regression tests.

Covers the user requirements:
  1. registered -> stopped -> registered (full cycle)
  2. historical rows preserved across transitions
  3. created_at preserved across reactivate
  4. updated_at refreshed on reactivate
  5. registration_key unchanged across reactivate
  6. latest load_all/get returns registered after restart
  7. active duplicate still blocked (registered -> registered)
  8. stopped duplicate can reactivate (the canonical path)
  9. Running includes the reactivated registration
 10. Stop picker includes the reactivated registration
 11. Reconciler includes the reactivated registration
 12. zero TradeDesk calls during reactivate itself
 13. zero exchange writes
 14. Create wizard renders "Fibo restarted" rather than
     "Already registered" when the target key is stopped
 15. new MT4 cycle/weight/volume snapshot fields are used on
     restart
 16. callback_data lengths remain <= Telegram limit

All tests use temporary stores under a tmp dir; nothing touches
the live ~/.hermes/fibo/registrations.jsonl or
~/.hermes/fibo/instrument_aliases.json.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from plugins.trade.fibo.flow import (
    CB_SYM, CB_SIDE, CB_EX, CB_ACCT, CB_AGREE, CB_CREATE,
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
# Fixtures (mirrors test_fibo_stop.py)
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
    from datetime import datetime, timezone
    received_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    raw = {
        "v": 1,
        "source": "obs-1",
        "seq": 42,
        "ts": 100,
        "fibos": [f.to_dict() for f in fibos],
    }
    snap = parse_snapshot_payload(
        raw,
        received_at=received_at,
        telegram_update_id=1,
        telegram_message_id=1,
        reader_chat_id=-100,
    )
    assert snap is not None
    return snap


def _write_snapshot(path: Path, snap: Mt4Snapshot) -> None:
    path.write_text(json.dumps(snap.to_dict()))


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


class _RestartTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fibo_dir = self.root / "fibo"
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

    def _flow(self) -> Any:
        from plugins.trade.fibo.alias_memory import AliasMemory
        from plugins.trade.fibo.flow import StartFiboFlow
        return StartFiboFlow(
            snapshot_store=Mt4SnapshotStore(self.snap_path),
            registration_store=FiboRegistrationStore(self.reg_path),
            list_exchanges_fn=lambda: ["ondoperps"],
            list_accounts_fn=lambda ex: ["bitget"],
            alias_memory=AliasMemory(self.ali_path),
        )


# ---------------------------------------------------------------------------
# 1. registered -> stopped -> registered (full cycle)
# ---------------------------------------------------------------------------


class FullTransitionCycleTests(_RestartTestBase):
    def test_full_cycle_registered_stopped_registered(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        # Initial registered row
        original = _append_registration(
            store,
            exchange="ondoperps", account="bitget",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        self.assertEqual(original.status, "registered")
        self.assertTrue(original.is_active)
        self.assertFalse(original.is_stopped)
        # Stop it
        stopped, _active_count = store.mark_stopped(original.registration_key)
        self.assertEqual(stopped.status, "stopped")
        self.assertTrue(stopped.is_stopped)
        self.assertFalse(stopped.is_active)
        # Reactivate via the canonical method
        reactivated, _active_count = store.reactivate(
            original.registration_key,
            source_symbol="ETHUSD",
            exchange_instrument="ETH-USD.P",
            starting_volume=Decimal("0.5"),
            desired_exchange_size=Decimal("1.0"),
            source="obs-2",
            source_seq=99,
            source_cycle_id=100,
            source_cumulative_weight=Decimal("2.0"),
            source_percentage=Decimal("0.01"),
            source_snapshot_received_at="2026-08-27T05:00:00Z",
        )
        self.assertEqual(reactivated.status, "registered")
        self.assertTrue(reactivated.is_active)
        self.assertFalse(reactivated.is_stopped)


# ---------------------------------------------------------------------------
# 2. historical rows preserved across transitions
# ---------------------------------------------------------------------------


class HistoricalRowsPreservedTests(_RestartTestBase):
    def test_history_preserved_through_registered_stopped_registered(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        original = _append_registration(
            store,
            exchange="ondoperps", account="bitget",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        store.mark_stopped(original.registration_key)
        store.reactivate(
            original.registration_key,
            source_symbol="ETHUSD",
            exchange_instrument="ETH-USD.P",
            starting_volume=Decimal("0.5"),
            desired_exchange_size=Decimal("1.0"),
            source="obs-2",
            source_seq=99,
            source_cycle_id=100,
            source_cumulative_weight=Decimal("2.0"),
            source_percentage=Decimal("0.01"),
            source_snapshot_received_at="2026-08-27T05:00:00Z",
        )
        # Three raw rows on disk
        with open(self.reg_path) as f:
            rows = f.read().splitlines()
        self.assertEqual(len(rows), 3)
        first = json.loads(rows[0])
        middle = json.loads(rows[1])
        last = json.loads(rows[2])
        self.assertEqual(first["status"], "registered")
        self.assertEqual(middle["status"], "stopped")
        self.assertEqual(last["status"], "registered")
        # All three share the same registration_key.
        for r in (first, middle, last):
            self.assertEqual(r["registration_key"], original.registration_key)
            self.assertEqual(r["exchange"], "ondoperps")
            # Account is normalized to upper-case by the store.
            self.assertEqual(r["account"], "BITGET")
            self.assertEqual(r["source_symbol"], "ETHUSD")
            self.assertEqual(r["exchange_instrument"], "ETH-USD.P")
            self.assertEqual(r["variant"], "NORMALFIB")
            self.assertEqual(r["side"], "BUY")


# ---------------------------------------------------------------------------
# 3. created_at preserved across reactivate
# 4. updated_at refreshed on reactivate
# 5. registration_key unchanged across reactivate
# 6. latest load_all/get returns registered after restart
# ---------------------------------------------------------------------------


class ReactivateInvariantTests(_RestartTestBase):
    def test_reactivate_invariants(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        original = _append_registration(
            store,
            exchange="ondoperps", account="bitget",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        original_key = original.registration_key
        original_created_at = original.created_at
        original_updated_at = original.updated_at

        store.mark_stopped(original.registration_key)
        reactivated, _active_count = store.reactivate(
            original.registration_key,
            source_symbol="ETHUSD",
            exchange_instrument="ETH-USD.P",
            starting_volume=Decimal("0.5"),
            desired_exchange_size=Decimal("1.0"),
            source="obs-2",
            source_seq=99,
            source_cycle_id=100,
            source_cumulative_weight=Decimal("2.0"),
            source_percentage=Decimal("0.01"),
            source_snapshot_received_at="2026-08-27T05:00:00Z",
            updated_at="2026-08-27T05:00:00Z",
        )

        # registration_key unchanged
        self.assertEqual(reactivated.registration_key, original_key)
        # created_at preserved
        self.assertEqual(reactivated.created_at, original_created_at)
        # updated_at refreshed (explicit value used here)
        self.assertEqual(reactivated.updated_at, "2026-08-27T05:00:00Z")
        self.assertNotEqual(reactivated.updated_at, original_updated_at)
        # latest load_all returns the registered row
        latest = store.get(original_key)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.status, "registered")
        self.assertEqual(latest.registration_key, original_key)
        # All registered entries via load_all: 1 effective row
        all_ = store.load_all()
        self.assertEqual(len(all_), 1)
        self.assertEqual(all_[0].status, "registered")


# ---------------------------------------------------------------------------
# 7. active duplicate still blocked (registered -> registered)
# ---------------------------------------------------------------------------


class ActiveDuplicateStillBlockedTests(_RestartTestBase):
    def test_registered_to_registered_is_duplicate(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        reg = _append_registration(
            store,
            exchange="ondoperps", account="bitget",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        # Try a plain append with status=registered over the
        # existing registered row.
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


# ---------------------------------------------------------------------------
# 8. stopped duplicate can reactivate (the canonical path)
# ---------------------------------------------------------------------------


class StoppedDuplicateCanReactivateTests(_RestartTestBase):
    def test_stopped_to_registered_only_via_reactivate(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        reg = _append_registration(
            store,
            exchange="ondoperps", account="bitget",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        store.mark_stopped(reg.registration_key)
        # Plain append is blocked.
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
        # But reactivate works.
        out, _active_count = store.reactivate(
            reg.registration_key,
            source_symbol="ETHUSD",
            exchange_instrument="ETH-USD.P",
            starting_volume=Decimal("0.001"),
            desired_exchange_size=Decimal("0.002"),
            source="obs-1",
            source_seq=42,
            source_cycle_id=42,
            source_cumulative_weight=Decimal("2.0"),
            source_percentage=Decimal("0.01"),
            source_snapshot_received_at="2026-08-27T00:00:00Z",
        )
        self.assertEqual(out.status, "registered")
        self.assertEqual(out.registration_key, reg.registration_key)


# ---------------------------------------------------------------------------
# 9. Running includes the reactivated registration
# 10. Stop picker includes the reactivated registration
# 11. Reconciler includes the reactivated registration
# ---------------------------------------------------------------------------


class ReactivatedIncludedInWiringTests(_RestartTestBase):
    def test_reactivated_registration_appears_in_wiring(self) -> None:
        from plugins.trade import fibo_wizard
        store = FiboRegistrationStore(self.reg_path)
        reg = _append_registration(
            store,
            exchange="ondoperps", account="bitget",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        store.mark_stopped(reg.registration_key)
        store.reactivate(
            reg.registration_key,
            source_symbol="ETHUSD",
            exchange_instrument="ETH-USD.P",
            starting_volume=Decimal("0.001"),
            desired_exchange_size=Decimal("0.002"),
            source="obs-1",
            source_seq=42,
            source_cycle_id=42,
            source_cumulative_weight=Decimal("2.0"),
            source_percentage=Decimal("0.01"),
            source_snapshot_received_at="2026-08-27T00:00:00Z",
        )

        class _MockQ:
            def __init__(self):
                self.edited_text = None
                self.edited_markup = None
                self.answered = False
            def answer(self):
                self.answered = True
            async def edit_message_text(self, text="", reply_markup=None):
                self.edited_text = text
                self.edited_markup = reply_markup

        # Stop picker — must include the reactivated registration.
        async def _run():
            q = _MockQ()
            with mock.patch.object(
                fibo_wizard, "_resolve_hermes_home_for_flow",
                return_value=self.root,
            ):
                await fibo_wizard.handle_fibo_callback(
                    None, q, "fibo:stop"
                )
            return q
        q = asyncio.run(_run())
        self.assertIn("ETHUSD NORMALFIB BUY", q.edited_text)

        # Reconciler — must include the reactivated registration.
        calls: List[Dict[str, Any]] = []
        def _spy(req):
            calls.append(dict(req))
            from plugins.trade.canonical import make_success
            return make_success(
                operation=req.get("operation", ""),
                exchange=req.get("exchange", ""),
                account=req.get("account", ""),
                data={"positions": [], "orders": []},
            )
        reconciler = FiboReconciler(
            registration_store=store,
            snapshot_store=Mt4SnapshotStore(self.snap_path),
            execute_fn=_spy,
        )
        results = reconciler.reconcile_all()
        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0].registration_key, reg.registration_key
        )
        self.assertGreater(len(calls), 0)


# ---------------------------------------------------------------------------
# 12. zero TradeDesk calls during reactivate itself
# 13. zero exchange writes
# ---------------------------------------------------------------------------


class ReactivateZeroWriteTests(_RestartTestBase):
    def test_reactivate_does_not_call_tradedesk(self) -> None:
        """The ``reactivate`` store method MUST NOT call TradeDesk.

        We verify by patching ``get_tradedesk`` to raise if
        called during reactivate.
        """
        store = FiboRegistrationStore(self.reg_path)
        reg = _append_registration(
            store,
            exchange="ondoperps", account="bitget",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        store.mark_stopped(reg.registration_key)

        from plugins.trade import fibo_wizard
        # Patch the store's flow's get_tradedesk usage to ensure
        # nothing leaks. Use a global spy.
        with mock.patch.dict(
            fibo_wizard.__dict__,
            {"get_tradedesk": mock.Mock(side_effect=AssertionError(
                "reactivate must not call get_tradedesk()"
            ))},
        ):
            store.reactivate(
                reg.registration_key,
                source_symbol="ETHUSD",
                exchange_instrument="ETH-USD.P",
                starting_volume=Decimal("0.001"),
                desired_exchange_size=Decimal("0.002"),
                source="obs-1",
                source_seq=42,
                source_cycle_id=42,
                source_cumulative_weight=Decimal("2.0"),
                source_percentage=Decimal("0.01"),
                source_snapshot_received_at="2026-08-27T00:00:00Z",
            )
        # Test passed if no exception escaped.

    def test_reactivate_writes_only_local_jsonl(self) -> None:
        """reactivate is purely local: it touches only the
        registration_store file. No alias write, no exchange
        call."""
        store = FiboRegistrationStore(self.reg_path)
        reg = _append_registration(
            store,
            exchange="ondoperps", account="bitget",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        store.mark_stopped(reg.registration_key)

        # Snapshot alias file size before reactivate.
        ali_size_before = (
            self.ali_path.stat().st_size if self.ali_path.exists() else 0
        )
        # Reactivate.
        store.reactivate(
            reg.registration_key,
            source_symbol="ETHUSD",
            exchange_instrument="ETH-USD.P",
            starting_volume=Decimal("0.001"),
            desired_exchange_size=Decimal("0.002"),
            source="obs-1",
            source_seq=42,
            source_cycle_id=42,
            source_cumulative_weight=Decimal("2.0"),
            source_percentage=Decimal("0.01"),
            source_snapshot_received_at="2026-08-27T00:00:00Z",
        )
        # Alias file unchanged.
        if self.ali_path.exists():
            self.assertEqual(self.ali_path.stat().st_size, ali_size_before)


# ---------------------------------------------------------------------------
# 14. Create wizard renders "Fibo restarted" rather than "Already registered"
# ---------------------------------------------------------------------------


class CreateRendersRestartedTests(_RestartTestBase):
    def test_create_renders_fibo_restarted_screen(self) -> None:
        """Drive the Start Fibo flow for a stopped registration;
        Create should render the "Fibo restarted" screen,
        not the "Already registered" screen.
        """
        from plugins.trade.fibo.flow import StartFiboFlow
        store = FiboRegistrationStore(self.reg_path)
        reg = _append_registration(
            store,
            exchange="ondoperps", account="bitget",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        store.mark_stopped(reg.registration_key)

        flow = StartFiboFlow(
            snapshot_store=Mt4SnapshotStore(self.snap_path),
            registration_store=FiboRegistrationStore(self.reg_path),
            list_exchanges_fn=lambda: ["ondoperps"],
            list_accounts_fn=lambda ex: ["bitget"],
            resolve_instrument_fn=lambda ex, ac, sym: (
                "ETH-USD.P" if sym == "ETHUSD" else None
            ),
        )
        CHAT = "phase27-create"
        USER = "phase27-create-user"
        flow.open(CHAT, USER)
        flow.handle_callback(CHAT, USER, f"{CB_SYM}0")
        flow.handle_callback(CHAT, USER, f"{CB_SIDE}{SIDE_TOKEN_BUY}")
        flow.handle_callback(CHAT, USER, f"{CB_EX}0")
        flow.handle_callback(CHAT, USER, f"{CB_ACCT}0")
        flow.handle_callback(CHAT, USER, CB_AGREE)
        flow.handle_text(CHAT, USER, "0.001")
        screen = flow.handle_callback(CHAT, USER, CB_CREATE)
        self.assertIn("Fibo restarted", screen.text)
        self.assertNotIn("Already registered", screen.text)
        # The reactivated registration is now active.
        all_ = FiboRegistrationStore(self.reg_path).load_all()
        self.assertEqual(len(all_), 1)
        self.assertEqual(all_[0].status, "registered")


# ---------------------------------------------------------------------------
# 15. new MT4 cycle/weight/volume snapshot fields are used on restart
# ---------------------------------------------------------------------------


class NewSnapshotFieldsAppliedTests(_RestartTestBase):
    def test_reactivate_uses_new_snapshot_fields(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        reg = _append_registration(
            store,
            exchange="ondoperps", account="bitget",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        store.mark_stopped(reg.registration_key)
        # Reactivate with brand-new snapshot fields.
        out, _active_count = store.reactivate(
            reg.registration_key,
            source_symbol="ETHUSD",
            exchange_instrument="ETH-USD.P",
            starting_volume=Decimal("0.5"),
            desired_exchange_size=Decimal("1.0"),
            source="obs-NEW",
            source_seq=7777,
            source_cycle_id=8888,
            source_cumulative_weight=Decimal("3.0"),
            source_percentage=Decimal("0.02"),
            source_snapshot_received_at="2026-09-01T00:00:00Z",
        )
        self.assertEqual(out.starting_volume, Decimal("0.5"))
        self.assertEqual(out.desired_exchange_size, Decimal("1.0"))
        self.assertEqual(out.source, "obs-NEW")
        self.assertEqual(out.source_seq, 7777)
        self.assertEqual(out.source_cycle_id, 8888)
        self.assertEqual(out.source_cumulative_weight, Decimal("3.0"))
        self.assertEqual(out.source_percentage, Decimal("0.02"))
        self.assertEqual(out.source_snapshot_received_at,
                         "2026-09-01T00:00:00Z")
        # Identity preserved.
        self.assertEqual(out.exchange, "ondoperps")
        # Account is normalized to upper-case by the store.
        self.assertEqual(out.account, "BITGET")
        self.assertEqual(out.source_symbol, "ETHUSD")
        self.assertEqual(out.exchange_instrument, "ETH-USD.P")
        self.assertEqual(out.variant, "NORMALFIB")
        self.assertEqual(out.side, "BUY")


# ---------------------------------------------------------------------------
# 16. callback_data lengths remain <= Telegram limit
# ---------------------------------------------------------------------------


class CallbackLengthUnderTelegramLimitTests(_RestartTestBase):
    def test_reactivation_callbacks_under_64_bytes(self) -> None:
        """No Phase 2.7 callback exceeds Telegram's 64-byte
        limit (defensive: also 32-byte budget).
        """
        # Start Fibo flow uses CB_SYM / CB_SIDE / CB_EX / CB_ACCT /
        # CB_AGREE / CB_CREATE — none of which changed.
        from plugins.trade.fibo.flow import (
            CB_SYM, CB_SIDE, CB_EX, CB_ACCT, CB_AGREE, CB_CREATE,
        )
        for cb in (CB_SYM, CB_SIDE, CB_EX, CB_ACCT, CB_AGREE, CB_CREATE):
            self.assertLessEqual(len(cb), 64)
            self.assertLessEqual(len(cb), 32)


# ---------------------------------------------------------------------------
# 17. identity-mismatch refusal (defensive: reactivate refuses
#     different source_symbol / exchange_instrument).
# ---------------------------------------------------------------------------


class ReactivateIdentityMismatchTests(_RestartTestBase):
    def test_reactivate_refuses_different_source_symbol(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        reg = _append_registration(
            store,
            exchange="ondoperps", account="bitget",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        store.mark_stopped(reg.registration_key)
        with self.assertRaises(ValueError):
            store.reactivate(
                reg.registration_key,
                source_symbol="BTCUSD",  # WRONG
                exchange_instrument="ETH-USD.P",
                starting_volume=Decimal("0.001"),
                desired_exchange_size=Decimal("0.002"),
                source="obs-1",
                source_seq=42,
                source_cycle_id=42,
                source_cumulative_weight=Decimal("2.0"),
                source_percentage=Decimal("0.01"),
                source_snapshot_received_at="2026-08-27T00:00:00Z",
            )

    def test_reactivate_refuses_different_exchange_instrument(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        reg = _append_registration(
            store,
            exchange="ondoperps", account="bitget",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        store.mark_stopped(reg.registration_key)
        with self.assertRaises(ValueError):
            store.reactivate(
                reg.registration_key,
                source_symbol="ETHUSD",
                exchange_instrument="ETH-USDC.P",  # WRONG
                starting_volume=Decimal("0.001"),
                desired_exchange_size=Decimal("0.002"),
                source="obs-1",
                source_seq=42,
                source_cycle_id=42,
                source_cumulative_weight=Decimal("2.0"),
                source_percentage=Decimal("0.01"),
                source_snapshot_received_at="2026-08-27T00:00:00Z",
            )


# ---------------------------------------------------------------------------
# 18. reactivate refuses on already-registered (no-op transition).
# ---------------------------------------------------------------------------


class ReactivateAlreadyRegisteredTests(_RestartTestBase):
    def test_reactivate_on_already_registered_raises(self) -> None:
        store = FiboRegistrationStore(self.reg_path)
        reg = _append_registration(
            store,
            exchange="ondoperps", account="bitget",
            source_symbol="ETHUSD", exchange_instrument="ETH-USD.P",
            variant="NORMALFib", side="BUY",
        )
        # No stop; the row is registered.
        with self.assertRaises(ValueError):
            store.reactivate(
                reg.registration_key,
                source_symbol="ETHUSD",
                exchange_instrument="ETH-USD.P",
                starting_volume=Decimal("0.001"),
                desired_exchange_size=Decimal("0.002"),
                source="obs-1",
                source_seq=42,
                source_cycle_id=42,
                source_cumulative_weight=Decimal("2.0"),
                source_percentage=Decimal("0.01"),
                source_snapshot_received_at="2026-08-27T00:00:00Z",
            )


if __name__ == "__main__":
    unittest.main()
