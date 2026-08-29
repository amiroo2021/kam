"""Phase 2.13.20 \u2014 Refresh-button freshness regression tests.

The user reported that pressing the wizard's Refresh button on
the confirmation screen did not update the displayed snapshot
diagnostics. Investigation showed the wizard already re-loads
``Mt4SnapshotStore.load()`` on every callback; these tests
prove the contract explicitly and guard against future
regressions.

Properties verified:

  [1] confirmation initially renders snapshot A
  [2] disk snapshot advances to B
  [3] Refresh renders B, not A
  [4] displayed seq/ts/cycle/weight/age all come from B
  [5] user selections survive Refresh
  [6] calculated target updates if weight changed
  [7] final Agree re-loads latest snapshot
  [8] stale final snapshot fails closed
  [9] symbol/variant disappearing before Agree fails closed
 10] no registration/exchange writes occur during Refresh
 11] existing Start Fibo 13-pair discovery remains intact
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from plugins.trade.fibo.flow import (
    CB_ACCT, CB_AGREE, CB_CANCEL, CB_CREATE, CB_EX, CB_REFRESH,
    CB_SIDE, CB_SYM, SIDE_TOKEN_BUY, SIDE_TOKEN_SELL, StartFiboFlow,
)
from plugins.trade.fibo.snapshot import (
    Mt4Fibo, Mt4Snapshot, Mt4SnapshotStore, parse_snapshot_payload,
)
from plugins.trade.fibo.store import FiboRegistrationStore


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def _utc_iso(seconds_ago: float = 0.0) -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        - timedelta(seconds=seconds_ago)
    ).isoformat().replace("+00:00", "Z")


def _good_fibo(
    *,
    symbol: str = "SOLUSD",
    variant: str = "FASTFib",
    percentage: str = "0.001",
    buy_cycle_id: int = 42,
    cumulative_buy_weight: str = "1.0",
    sell_cycle_id: int = 7,
    cumulative_sell_weight: str = "1.0",
) -> Mt4Fibo:
    return Mt4Fibo(
        symbol=symbol, variant=variant,
        percentage=Decimal(percentage),
        buy_cycle_id=buy_cycle_id,
        cumulative_buy_weight=Decimal(cumulative_buy_weight),
        sell_cycle_id=sell_cycle_id,
        cumulative_sell_weight=Decimal(cumulative_sell_weight),
    )


def _snapshot_payload(
    fibos: List[Mt4Fibo],
    *,
    source: str = "obs-1",
    seq: int = 42,
    ts: int = 1700000000,
    received_at: Optional[str] = None,
) -> Dict[str, Any]:
    body = {
        "v": 1, "source": source, "seq": seq, "ts": ts,
        "fibos": [
            {
                "symbol": f.symbol, "variant": f.variant,
                "percentage": str(f.percentage),
                "buy_cycle_id": f.buy_cycle_id,
                "cumulative_buy_weight": str(f.cumulative_buy_weight),
                "sell_cycle_id": f.sell_cycle_id,
                "cumulative_sell_weight": str(f.cumulative_sell_weight),
            }
            for f in fibos
        ],
        "received_at": received_at or _utc_iso(0.0),
        "telegram_update_id": seq,
        "telegram_message_id": seq,
        "reader_chat_id": -1004351200469,
    }
    return body


def _write_snapshot(path: Path, fibos: List[Mt4Fibo], **kwargs: Any) -> Mt4Snapshot:
    body = _snapshot_payload(fibos, **kwargs)
    path.write_text(json.dumps(body))
    return Mt4SnapshotStore(path).load()


# -----------------------------------------------------------------------
# Test fixture
# -----------------------------------------------------------------------


class RefreshTestBase(unittest.TestCase):
    """Sets up a deterministic StartFiboFlow with a writable
    snapshot file. ``self.set_disk_snapshot`` updates the on-disk
    snapshot between operations without recreating the flow."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snap_path = self.root / "mt4_snapshot.json"
        self.reg_path = self.root / "registrations.jsonl"
        self.exchange_writes: List[Any] = []

        # Default SOLUSD FASTFib SELL entry.
        self.sol_a = _good_fibo(
            symbol="SOLUSD", variant="FASTFib",
            sell_cycle_id=47034392, cumulative_sell_weight="1",
        )
        _write_snapshot(
            self.snap_path, [self.sol_a],
            source="mt4-Fresh542468-1", seq=2416, ts=1787976800,
            received_at=_utc_iso(0.0),
        )

        snap_store = Mt4SnapshotStore(self.snap_path)
        reg_store = FiboRegistrationStore(self.reg_path)

        def list_exchanges() -> List[str]:
            return ["ondoperps"]

        def list_accounts(exchange: str) -> List[Any]:
            return ["BITGET"]

        def list_instruments(exchange: str, account: str) -> List[str]:
            return ["XAU-USD.P"]

        def resolve_instrument(exchange: str, account: str, symbol: str):
            return ("XAU-USD.P", None)

        def now_fn() -> datetime:
            return datetime.now(timezone.utc)

        self.flow = StartFiboFlow(
            snapshot_store=snap_store,
            registration_store=reg_store,
            list_exchanges_fn=list_exchanges,
            list_accounts_fn=list_accounts,
            list_instruments_fn=list_instruments,
            resolve_instrument_fn=resolve_instrument,
            now_fn=now_fn,
        )

    def drive_to_confirm(self, *, volume: str = "0.001") -> None:
        """Drive the flow from open() through to AWAITING_CONFIRM
        so we can immediately call Refresh/Create."""
        self.flow.open("chat-1", "user-1")
        self.flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        self.flow.handle_callback("chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_SELL}")
        self.flow.handle_callback("chat-1", "user-1", f"{CB_EX}0")
        self.flow.handle_callback("chat-1", "user-1", f"{CB_ACCT}0")
        self.flow.handle_callback("chat-1", "user-1", CB_AGREE)
        self.flow.handle_text("chat-1", "user-1", volume)

    def set_disk_snapshot(
        self, fibos: List[Mt4Fibo], **kwargs: Any
    ) -> Mt4Snapshot:
        return _write_snapshot(self.snap_path, fibos, **kwargs)


# -----------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------


class RefreshReloadsLatestSnapshotTest(RefreshTestBase):
    """[1][2][3][4][5][6] Refresh renders the latest disk snapshot
    while preserving user selections and updating the calc target."""

    def test_initial_confirmation_renders_snapshot_a(self):
        # Sanity: initial render shows seq=2416, cycle=47034392, weight=1.
        self.drive_to_confirm()
        screen = self.flow.handle_callback(
            "chat-1", "user-1", CB_REFRESH
        )
        self.assertIn("seq 2416", screen.text)
        self.assertIn("MT4 cycle:           47034392", screen.text)
        self.assertIn("MT4 weight:          1", screen.text)
        self.assertIn("Calc target:         0.001", screen.text)

    def test_disk_snapshot_advances_to_b_and_refresh_renders_b(self):
        # [2] Advance the on-disk snapshot to B (seq 2500, cycle 47099999, weight 2).
        self.sol_b = _good_fibo(
            symbol="SOLUSD", variant="FASTFib",
            sell_cycle_id=47099999, cumulative_sell_weight="2",
        )
        self.set_disk_snapshot(
            [self.sol_b],
            source="mt4-Fresh542468-1", seq=2500, ts=1787976900,
            received_at=_utc_iso(0.0),
        )
        # [3] Refresh renders B, not A.
        self.drive_to_confirm()
        screen = self.flow.handle_callback(
            "chat-1", "user-1", CB_REFRESH
        )
        self.assertIn("seq 2500", screen.text)
        self.assertNotIn("seq 2416", screen.text)
        self.assertIn("MT4 cycle:           47099999", screen.text)
        self.assertNotIn("MT4 cycle:           47034392", screen.text)
        self.assertIn("MT4 weight:          2", screen.text)
        self.assertNotIn("MT4 weight:          1", screen.text)
        # [6] Calc target updates to 0.001 * 2 = 0.002.
        self.assertIn("Calc target:         0.002", screen.text)

    def test_user_selections_survive_refresh(self):
        # [5] symbol, variant, side, exchange, account, volume are
        # user-controlled and must NOT be altered by Refresh.
        self.drive_to_confirm(volume="0.005")
        sess = self.flow.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.symbol, "SOLUSD")
        self.assertEqual(sess.variant, "FASTFib")
        self.assertEqual(sess.side, "SELL")
        self.assertEqual(sess.exchange, "ondoperps")
        self.assertEqual(sess.account, "BITGET")
        self.assertEqual(sess.starting_volume, Decimal("0.005"))
        # Advance the disk snapshot to a different cycle/weight.
        self.sol_b = _good_fibo(
            symbol="SOLUSD", variant="FASTFib",
            sell_cycle_id=47099999, cumulative_sell_weight="3",
        )
        self.set_disk_snapshot(
            [self.sol_b], seq=2500, ts=1787976900,
            received_at=_utc_iso(0.0),
        )
        screen = self.flow.handle_callback(
            "chat-1", "user-1", CB_REFRESH
        )
        # Selections still in session state.
        sess = self.flow.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.symbol, "SOLUSD")
        self.assertEqual(sess.variant, "FASTFib")
        self.assertEqual(sess.side, "SELL")
        self.assertEqual(sess.exchange, "ondoperps")
        self.assertEqual(sess.account, "BITGET")
        self.assertEqual(sess.starting_volume, Decimal("0.005"))
        # And the confirmation text shows those exact selections.
        self.assertIn("Source symbol:       SOLUSD", screen.text)
        self.assertIn("Variant:             FASTFib", screen.text)
        self.assertIn("Side:                SELL", screen.text)
        self.assertIn("Exchange:            ondoperps", screen.text)
        self.assertIn("Account:             BITGET", screen.text)
        self.assertIn("Volume:              0.005", screen.text)

    def test_refresh_does_not_persist_registration(self):
        # [10] Refresh must not write to the registration store.
        self.drive_to_confirm()
        self.flow.handle_callback("chat-1", "user-1", CB_REFRESH)
        store = FiboRegistrationStore(self.reg_path)
        self.assertEqual(len(store.load_all()), 0)

    def test_refresh_repeated_advances_each_time(self):
        """Pressing Refresh twice, with the disk snapshot advancing
        between presses, shows the latest values each time."""
        self.drive_to_confirm()
        # Initial refresh: shows seq=2416.
        s1 = self.flow.handle_callback("chat-1", "user-1", CB_REFRESH)
        self.assertIn("seq 2416", s1.text)
        # Advance disk: seq=2500.
        self.set_disk_snapshot(
            [_good_fibo(sell_cycle_id=47099999, cumulative_sell_weight="2")],
            seq=2500, ts=1787976900, received_at=_utc_iso(0.0),
        )
        s2 = self.flow.handle_callback("chat-1", "user-1", CB_REFRESH)
        self.assertIn("seq 2500", s2.text)
        # Advance again: seq=2600.
        self.set_disk_snapshot(
            [_good_fibo(sell_cycle_id=47100100, cumulative_sell_weight="3")],
            seq=2600, ts=1787977000, received_at=_utc_iso(0.0),
        )
        s3 = self.flow.handle_callback("chat-1", "user-1", CB_REFRESH)
        self.assertIn("seq 2600", s3.text)
        self.assertIn("MT4 weight:          3", s3.text)


class RefreshReadsFreshReceivedAtTest(RefreshTestBase):
    """[4] Snapshot age is rebuilt from the fresh received_at, not
    from any stale cached value."""

    def test_age_uses_latest_received_at(self):
        # First confirmation: received_at = now (age ~0s).
        self.drive_to_confirm()
        s1 = self.flow.handle_callback("chat-1", "user-1", CB_REFRESH)
        age1_line = next(
            l for l in s1.text.splitlines()
            if l.startswith("Snapshot age:")
        )
        age1 = float(age1_line.split(":")[1].strip().rstrip("s"))
        self.assertLess(age1, 10.0,
            f"initial age should be small, got {age1}s")

        # Now write a NEW snapshot with received_at 5 seconds ago.
        # The displayed age should reflect ~5s (not the cached 0s).
        self.set_disk_snapshot(
            [self.sol_a], seq=2417, ts=1787976801,
            received_at=_utc_iso(5.0),
        )
        s2 = self.flow.handle_callback("chat-1", "user-1", CB_REFRESH)
        age2_line = next(
            l for l in s2.text.splitlines()
            if l.startswith("Snapshot age:")
        )
        age2 = float(age2_line.split(":")[1].strip().rstrip("s"))
        self.assertGreaterEqual(age2, 4.0,
            f"refreshed age should be ~5s, got {age2}s")
        self.assertLess(age2, 12.0)


class FinalAgreeReloadsLatestSnapshotTest(RefreshTestBase):
    """[7][8][9] final Agree performs one fresh snapshot load
    immediately before writing the registration; it fails closed
    on stale data or a missing symbol/variant."""

    def test_agree_uses_latest_snapshot_for_registration(self):
        # Drive to confirm, advance the disk snapshot to a NEW cycle,
        # then Create. The new cycle_id must be the one persisted
        # in the registration.
        self.drive_to_confirm()
        self.sol_b = _good_fibo(
            sell_cycle_id=47099999, cumulative_sell_weight="2",
        )
        self.set_disk_snapshot(
            [self.sol_b], seq=2500, ts=1787976900,
            received_at=_utc_iso(0.0),
        )
        # First Refresh captures the new metadata.
        self.flow.handle_callback("chat-1", "user-1", CB_REFRESH)
        screen = self.flow.handle_callback(
            "chat-1", "user-1", CB_CREATE
        )
        self.assertIn("\u2705 Registered", screen.text)
        store = FiboRegistrationStore(self.reg_path)
        regs = store.load_all()
        self.assertEqual(len(regs), 1)
        reg = regs[0]
        # The registration MUST reflect the latest cycle (47099999),
        # not the cycle captured when the user first opened confirm.
        self.assertEqual(reg.source_cycle_id, 47099999)
        self.assertEqual(reg.source_cumulative_weight, Decimal("2"))
        self.assertEqual(reg.source_seq, 2500)

    def test_agree_fails_closed_on_stale_snapshot(self):
        # [8] If the snapshot is stale at the moment of Agree,
        # Create must NOT persist a registration; it must show
        # the stale warning instead.
        # Drive to confirm first with fresh data.
        self.drive_to_confirm()
        # Now write a snapshot whose received_at is far in the past.
        self.set_disk_snapshot(
            [self.sol_a], seq=2416, ts=1787976800,
            received_at=_utc_iso(120.0),  # 2 minutes old, beyond threshold
        )
        screen = self.flow.handle_callback(
            "chat-1", "user-1", CB_CREATE
        )
        self.assertIn("MT4 feed stale", screen.text)
        store = FiboRegistrationStore(self.reg_path)
        self.assertEqual(len(store.load_all()), 0,
            "stale snapshot must NOT create a registration")

    def test_agree_fails_closed_when_symbol_disappears(self):
        # [9] If the symbol/variant disappears before Agree, the
        # wizard must NOT create a registration.
        self.drive_to_confirm()
        # Write a snapshot without SOLUSD/FASTFib.
        self.set_disk_snapshot(
            [_good_fibo(symbol="BTCUSD", variant="FASTFib")],
            seq=2500, ts=1787976900,
            received_at=_utc_iso(0.0),
        )
        screen = self.flow.handle_callback(
            "chat-1", "user-1", CB_CREATE
        )
        self.assertNotIn("\u2705 Registered", screen.text)
        store = FiboRegistrationStore(self.reg_path)
        self.assertEqual(len(store.load_all()), 0)


class RefreshNoExchangeWritesTest(RefreshTestBase):
    """[10] Refresh must not invoke any TradeDesk write operation."""

    def test_refresh_does_not_touch_tradedesk(self):
        self.drive_to_confirm()
        # Wrap the tradedesk shims with monitors.
        calls: List[Any] = []
        orig_ex = self.flow._list_exchanges
        orig_acct = self.flow._list_accounts
        self.flow._list_exchanges = lambda: (calls.append(("list_exchanges",)), orig_ex())[1]
        self.flow._list_accounts = lambda ex: (calls.append(("list_accounts", ex)), orig_acct(ex))[1]
        # Also any resolve_instrument calls would surface via the
        # wizard (e.g. Browse markets). Refresh must not invoke them.
        orig_resolve = self.flow._resolve_instrument
        self.flow._resolve_instrument = lambda *a, **k: (calls.append(("resolve", a, k)), orig_resolve(*a, **k))[1]
        # Multiple Refresh presses.
        self.flow.handle_callback("chat-1", "user-1", CB_REFRESH)
        self.set_disk_snapshot(
            [self.sol_a], seq=2500, ts=1787976900,
            received_at=_utc_iso(0.0),
        )
        self.flow.handle_callback("chat-1", "user-1", CB_REFRESH)
        # Only read-only list_exchanges may run as part of the
        # standard symbol picker; the symbol picker must NOT be
        # re-invoked during a confirmation Refresh. We tolerate
        # list_exchanges/list_accounts (both read-only) but flag
        # anything else.
        for c in calls:
            if c[0] not in ("list_exchanges", "list_accounts"):
                self.fail(f"Refresh triggered non-readonly call: {c}")
        # And absolutely no exchange writes (we cannot directly
        # assert this without mocking tradedesk; the absence of
        # a registration record is the strongest indirect signal).
        store = FiboRegistrationStore(self.reg_path)
        self.assertEqual(len(store.load_all()), 0)


class ThirteenPairDiscoveryIntactTest(RefreshTestBase):
    """[11] The existing Start Fibo 13-pair discovery remains
    intact: ETHUSD FASTFib and SOLUSD FASTFib are present."""

    def test_all_thirteen_pairs_visible(self):
        thirteen_pairs = [
            ("#DJI30", "NORMALFib"), ("#NQ100", "NORMALFib"),
            ("#SP500", "NORMALFib"),
            ("BTCUSD", "NORMALFib"), ("BTCUSD", "FASTFib"),
            ("ETHUSD", "NORMALFib"), ("ETHUSD", "FASTFib"),
            ("SOLUSD", "NORMALFib"), ("SOLUSD", "FASTFib"),
            ("XAUUSD", "NORMALFib"), ("XAUUSD", "FASTFib"),
            ("ZECUSD", "NORMALFib"), ("ZECUSD", "FASTFib"),
        ]
        fibos = [
            _good_fibo(
                symbol=s, variant=v,
                sell_cycle_id=47033912 + i,
                cumulative_sell_weight="1",
            )
            for i, (s, v) in enumerate(thirteen_pairs)
        ]
        self.set_disk_snapshot(
            fibos, seq=99999, ts=1787999999,
            received_at=_utc_iso(0.0),
        )
        # Open the flow and inspect the symbol picker.
        screen = self.flow.open("chat-1", "user-1")
        labels = [
            row[0]["text"]
            for row in screen.buttons
            if row and row[0].get("callback_data", "").startswith(CB_SYM)
        ]
        self.assertEqual(len(labels), 13,
            f"expected 13 unique pairs, got {len(labels)}: {labels}")
        for s, v in thirteen_pairs:
            self.assertIn(f"{s} \u2014 {v}", labels,
                f"missing pair {s}/{v}")


class RefreshOnNoneSnapshotTest(RefreshTestBase):
    """Defensive: Refresh must NEVER render stale values from the
    session when the disk snapshot disappears (None). It must
    render the no-data screen."""

    def test_refresh_with_missing_snapshot_renders_no_data(self):
        self.drive_to_confirm()
        # Remove the snapshot file.
        self.snap_path.unlink()
        screen = self.flow.handle_callback(
            "chat-1", "user-1", CB_REFRESH
        )
        self.assertIn("No MT4 data", screen.text)
        # Session is preserved so the user can re-load.
        sess = self.flow.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.symbol, "SOLUSD")
        self.assertEqual(sess.starting_volume, Decimal("0.001"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
