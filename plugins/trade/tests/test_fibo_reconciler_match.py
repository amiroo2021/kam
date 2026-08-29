"""Phase 2.13.22 \u2014 Running Fibo display canonical-matching regression tests.

The bug: ``reconciler._actual_position_for_symbol`` only compared
``row.symbol`` (e.g. ``\"SOL\"``) to ``reg.exchange_instrument``
(e.g. ``\"SOL-USD.P\"``). For OndoPerps the two differ, so every
match returned None, every active registration reported
``actual=FLAT 0``, and the delta was always a false ``OPEN_*``.

The fix: ``reconciler._actual_position_for_symbol`` now uses
``executor._row_identity`` to match, which checks
``exchange_instrument`` first and then falls back to ``symbol``.

Properties verified here:

  [A] OndoPerps row:
          symbol=\"SOL\", exchange_instrument=\"SOL-USD.P\"
      registration:
          exchange_instrument=\"SOL-USD.P\"
      -> reconciler returns actual SHORT 0.1, not FLAT.

  [B] Same for XAU / XAU-USD.P.

  [C] Running Fibo compact output shows:
          Actual: SHORT ...
          Delta:  NOOP
      when target == actual.

  [D] Existing symbol-only exchanges still match correctly
      through the executor's symbol-fallback path.

  [E] Shadow and reconciler return identical actual side/size
      for the same positions response.

  [F] No change to live convergence behavior \u2014 the executor
      path is unchanged.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, List

# Use a per-test tempdir so we never touch production state.
_TMP = tempfile.mkdtemp(prefix="fibo_reconciler_match_test_")
os.environ["HERMES_HOME"] = _TMP

from plugins.trade.fibo.reconciler import (
    FiboReconciler,
    _actual_position_for_symbol as _rec_match,
)
from plugins.trade.fibo.executor import (
    _actual_position_for_symbol as _exe_match,
    _row_identity,
)
from plugins.trade.fibo.snapshot import (
    Mt4Fibo, Mt4Snapshot, Mt4SnapshotStore,
)
from plugins.trade.fibo.store import FiboRegistration, FiboRegistrationStore


# -----------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------


class FakePosition:
    """Mimics OndoPerps CanonicalPosition: short venue label
    (e.g. ``\"XAU\"``) and canonical contract id
    (e.g. ``\"XAU-USD.P``)."""

    def __init__(
        self, symbol: str, exchange_instrument: str,
        side: str, size: str, entry_price: str = "0",
    ) -> None:
        self.symbol = symbol
        self.exchange_instrument = exchange_instrument
        self.side = side
        self.size = size
        self.entry_price = entry_price


class FakeResponse:
    """Mimics TradeDesk.execute()'s positions_orders response."""

    def __init__(self, positions: List[FakePosition], *, success: bool = True):
        self.success = success
        self.positions = positions
        self.open_order_count = 0


def _ondoperps_3_positions() -> List[FakePosition]:
    return [
        FakePosition("BTC", "BTC-USD.P", "short", "0.135", "80434"),
        FakePosition("SOL", "SOL-USD.P", "short", "0.1",   "104.00"),
        FakePosition("XAU", "XAU-USD.P", "short", "0.002", "4454.49"),
    ]


# -----------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------


class CanonicalMatcherTest(unittest.TestCase):
    """[A][B] OndoPerps: row.symbol short, row.exchange_instrument
    canonical \u2014 the canonical matcher must use exchange_instrument."""

    def test_sol_matches_via_exchange_instrument_not_symbol(self):
        positions = _ondoperps_3_positions()
        # The OLD broken matcher would return None because
        # 'SOL-USD.P' != 'SOL'. The NEW matcher uses
        # _row_identity (exchange_instrument first).
        result = _rec_match(positions, "SOL-USD.P")
        self.assertIsNotNone(
            result,
            "reconciler._actual_position_for_symbol must match "
            "OndoPerps row with symbol='SOL' against "
            "registration.exchange_instrument='SOL-USD.P'",
        )
        self.assertEqual(result.symbol, "SOL")
        self.assertEqual(result.exchange_instrument, "SOL-USD.P")
        self.assertEqual(result.side, "short")
        self.assertEqual(str(result.size), "0.1")

    def test_xau_matches_via_exchange_instrument_not_symbol(self):
        positions = _ondoperps_3_positions()
        result = _rec_match(positions, "XAU-USD.P")
        self.assertIsNotNone(
            result,
            "reconciler._actual_position_for_symbol must match "
            "OndoPerps row with symbol='XAU' against "
            "registration.exchange_instrument='XAU-USD.P'",
        )
        self.assertEqual(result.symbol, "XAU")
        self.assertEqual(result.exchange_instrument, "XAU-USD.P")
        self.assertEqual(result.size, "0.002")

    def test_btc_matches_via_exchange_instrument_not_symbol(self):
        positions = _ondoperps_3_positions()
        result = _rec_match(positions, "BTC-USD.P")
        self.assertIsNotNone(result)
        self.assertEqual(result.symbol, "BTC")
        self.assertEqual(result.size, "0.135")

    def test_no_match_returns_none(self):
        positions = _ondoperps_3_positions()
        # Symbol that doesn't appear in any row.
        self.assertIsNone(_rec_match(positions, "ETH-USD.P"))
        # Empty positions.
        self.assertIsNone(_rec_match([], "XAU-USD.P"))


class SymbolOnlyExchangeMatcherTest(unittest.TestCase):
    """[D] Symbol-only exchanges (where row.exchange_instrument is
    missing or equal to row.symbol) still match through the
    fallback semantics in executor._row_identity."""

    def test_symbol_only_match_via_fallback(self):
        # Some agents return rows where exchange_instrument is
        # missing OR equal to symbol. _row_identity falls back to
        # symbol when exchange_instrument is empty.
        positions = [
            FakePosition("ETH", "ETH", "long", "0.5", "3000"),
            FakePosition("BTC", "BTC", "short", "0.1", "60000"),
        ]
        # Matching by symbol still works.
        result = _rec_match(positions, "ETH")
        self.assertIsNotNone(result)
        self.assertEqual(result.symbol, "ETH")
        self.assertEqual(str(result.size), "0.5")

    def test_missing_exchange_instrument_falls_back_to_symbol(self):
        # exchange_instrument is empty string; symbol is the
        # only available identifier. _row_identity falls back.
        class PosWithoutExi:
            def __init__(self, sym, side, size):
                self.symbol = sym
                self.exchange_instrument = ""
                self.side = side
                self.size = size
                self.entry_price = "0"
        positions = [PosWithoutExi("DOGE", "long", "100")]
        result = _rec_match(positions, "DOGE")
        self.assertIsNotNone(result)
        self.assertEqual(result.symbol, "DOGE")


class RunningFiboCompactBlockTest(unittest.TestCase):
    """[C] The compact_block (the Running Fibo display line) shows
    Actual: SHORT ... and Delta: NOOP when target == actual."""

    def test_xau_compact_block_shows_short_actual_and_noop(self):
        from plugins.trade.fibo.dryrun import _compact_block
        from plugins.trade.fibo.reconciler import ReconciliationResult

        result = ReconciliationResult(
            registration_key="ondoperps/BITGET/XAU-USD.P/FASTFIB/SELL",
            exchange="ondoperps", account="BITGET",
            source_symbol="XAUUSD", exchange_instrument="XAU-USD.P",
            variant="FASTFIB", side="SELL",
            starting_volume="0.001",
            mt4_source="obs-1", mt4_seq=1, mt4_cycle_id=47033879,
            mt4_weight="2", mt4_percentage="0.001",
            mt4_age_seconds=2.0, mt4_active=True,
            previous_cycle_id=47033879, cycle_changed=False,
            desired_side="SHORT", desired_size="0.002",
            actual_side="SHORT", actual_size="0.002",
            actual_entry_price="4454.49",
            delta_action="NONE", delta_size="0",
            safe_to_execute_later=True,
            reason="actual >= target \u2014 no-op",
        )
        block = _compact_block(result)
        self.assertIn("Actual: SHORT 0.002", block)
        self.assertIn("Delta: NONE 0", block)
        # The Mode label is environment-dependent (LIVE only if
        # fibo-converge.timer is active AND cycle_state has this
        # registration). In the test env the cycle_state file is
        # absent, so the label is DRY RUN \u2014 which is correct
        # conservative behavior.

    def test_sol_compact_block_shows_short_actual_and_noop(self):
        from plugins.trade.fibo.dryrun import _compact_block
        from plugins.trade.fibo.reconciler import ReconciliationResult

        result = ReconciliationResult(
            registration_key="ondoperps/BITGET/SOL-USD.P/FASTFIB/SELL",
            exchange="ondoperps", account="BITGET",
            source_symbol="SOLUSD", exchange_instrument="SOL-USD.P",
            variant="FASTFIB", side="SELL",
            starting_volume="0.1",
            mt4_source="obs-1", mt4_seq=1, mt4_cycle_id=47034392,
            mt4_weight="1", mt4_percentage="0.001",
            mt4_age_seconds=2.0, mt4_active=True,
            previous_cycle_id=47034392, cycle_changed=False,
            desired_side="SHORT", desired_size="0.1",
            actual_side="SHORT", actual_size="0.1",
            actual_entry_price="104.00",
            delta_action="NONE", delta_size="0",
            safe_to_execute_later=True,
            reason="actual >= target \u2014 no-op",
        )
        block = _compact_block(result)
        self.assertIn("Actual: SHORT 0.1", block)
        self.assertIn("Delta: NONE 0", block)


def _build_active_reg(
    reg_key: str, source_symbol: str,
    exchange_instrument: str, side: str, starting_volume: str,
    mt4_cycle: int, mt4_weight: str,
) -> FiboRegistration:
    """Build a FiboRegistration. The created_at/updated_at fields
    are required by the dataclass."""
    now = "2026-08-29T00:00:00Z"
    return FiboRegistration(
        exchange="ondoperps",
        account="BITGET",
        source_symbol=source_symbol,
        exchange_instrument=exchange_instrument,
        variant="FASTFIB",
        side=side,
        starting_volume=Decimal(starting_volume),
        source="obs-1",
        source_seq=1,
        source_cycle_id=mt4_cycle,
        source_cumulative_weight=Decimal(mt4_weight),
        source_percentage=Decimal("0.001"),
        source_snapshot_received_at=now,
        desired_exchange_size=Decimal(starting_volume) * Decimal(mt4_weight),
        status="registered",
        symbol=source_symbol,
        created_at=now,
        updated_at=now,
    )


def _build_snap_with(cycle: int, weight: str) -> Mt4Snapshot:
    # Use a recent received_at (within the past 5 seconds) so the
    # reconciler's freshness gate (30s threshold) doesn't classify
    # the snapshot as STALE_MT4 before it can match any position.
    now = datetime.now(timezone.utc)
    recent = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return Mt4Snapshot(
        v=1, source="obs-1", seq=1, ts=1700000000,
        fibos=[
            Mt4Fibo(
                symbol="SOLUSD", variant="FASTFIB",
                percentage=Decimal("0.001"),
                buy_cycle_id=cycle, cumulative_buy_weight=Decimal("1"),
                sell_cycle_id=cycle, cumulative_sell_weight=Decimal(weight),
            ),
        ],
        received_at=recent,
        telegram_update_id=1, telegram_message_id=1,
        reader_chat_id=-1,
    )


class ReconcilerVsShadowTest(unittest.TestCase):
    """[E] Shadow and reconciler return identical actual side/size
    for the same positions response."""

    def test_shadow_and_reconciler_agree_for_sol(self):
        tmp = tempfile.mkdtemp(prefix="fibo_e_test_")
        os.environ["HERMES_HOME"] = tmp
        reg_path = Path(tmp) / "fibo" / "registrations.jsonl"
        snap_path = Path(tmp) / "fibo" / "mt4_snapshot.json"
        # _atomic.ensure_dir_0700 refuses 0o755 parent dirs; create
        # the parent at the expected 0o700 mode.
        reg_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The test writes to reg_path via reg_store.append; that
        # helper enforces the 0o700 mode on the parent.
        # Re-apply 0o700 after our mkdir (umask may have stripped it).
        os.chmod(reg_path.parent, 0o700)
        snap_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(snap_path.parent, 0o700)

        reg = _build_active_reg(
            reg_key="ondoperps/BITGET/SOL-USD.P/FASTFIB/SELL",
            source_symbol="SOLUSD",
            exchange_instrument="SOL-USD.P",
            side="SELL",
            starting_volume="0.1",
            mt4_cycle=47034392, mt4_weight="1",
        )
        reg_store = FiboRegistrationStore(reg_path)
        reg_store.append(reg)

        snap = _build_snap_with(cycle=47034392, weight="1")
        snap_path.write_text(json.dumps(snap.to_dict()))

        positions = _ondoperps_3_positions()
        response = FakeResponse(positions)

        def mock_execute(payload):
            return response

        snap_store = Mt4SnapshotStore(snap_path)

        # Reconciler.
        reconciler = FiboReconciler(
            registration_store=reg_store,
            snapshot_store=snap_store,
            execute_fn=mock_execute,
        )
        recon_results = reconciler.reconcile_all()
        recon_sol = next(
            r for r in recon_results
            if "SOL" in r.registration_key
        )
        # Shadow.
        from plugins.trade.fibo.shadow import shadow_run
        shadow = shadow_run(
            reg, snap, execute_fn=mock_execute,
        )
        # Both should report the same actual (size + normalized
        # side) and the same status (NOOP).
        self.assertEqual(recon_sol.actual_side.upper(), "SHORT")
        self.assertEqual(recon_sol.actual_size, "0.1")
        # Shadow uses the executor's _normalize_actual_side which
        # returns the lowercase venue convention ("sell"). Both
        # expressions of "the position is short" — compare via the
        # common normalizer so case is irrelevant.
        from plugins.trade.fibo.executor import _normalize_actual_side
        long_words = {"long", "buy"}
        short_words = {"short", "sell"}
        def canonical(side):
            s = _normalize_actual_side(side).lower()
            if s in long_words:
                return "LONG"
            if s in short_words:
                return "SHORT"
            return "FLAT"
        self.assertEqual(
            canonical(recon_sol.actual_side),
            canonical(shadow.actual_side),
        )
        self.assertEqual(recon_sol.actual_size, shadow.actual_size)
        self.assertEqual(recon_sol.delta_action, "NONE")
        self.assertEqual(shadow.status, "NOOP")


class LiveConvergenceUnchangedTest(unittest.TestCase):
    """[F] No change to live convergence behavior. The executor's
    canonical matcher is unchanged; only the reconciler's wrapper
    now uses _row_identity. live.py already used the canonical
    matcher via _read_actual_position_from_response, so this
    change is a no-op for live_converge."""

    def test_executor_canonical_matcher_unchanged(self):
        # The executor's _actual_position_for_symbol must still
        # match OndoPerps positions correctly (this is the
        # behaviour live_converge depends on).
        positions = _ondoperps_3_positions()
        for xinst in ("XAU-USD.P", "SOL-USD.P", "BTC-USD.P"):
            result = _exe_match(positions, xinst)
            self.assertFalse(
                result.is_flat,
                f"executor._actual_position_for_symbol must match "
                f"xinst={xinst}",
            )
            self.assertEqual(result.symbol, xinst)

    def test_reconciler_uses_same_row_identity_as_executor(self):
        # Both matchers must return the same result for the same
        # positions.
        positions = _ondoperps_3_positions()
        # The reconciler returns the venue's raw side (e.g.
        # "short"); the executor normalizes to "sell". Compare
        # the underlying trading direction (LONG/SHORT/FLAT) by
        # mapping both through the same normalizer.
        from plugins.trade.fibo.executor import _normalize_actual_side
        long_words = {"long", "buy"}
        short_words = {"short", "sell"}
        def canonical(side):
            s = _normalize_actual_side(side).lower()
            if s in long_words:
                return "LONG"
            if s in short_words:
                return "SHORT"
            return "FLAT"
        for xinst in ("XAU-USD.P", "SOL-USD.P", "BTC-USD.P"):
            rec = _rec_match(positions, xinst)
            exe = _exe_match(positions, xinst)
            self.assertIsNotNone(rec)
            # Both must be non-flat.
            self.assertFalse(exe.is_flat)
            # Same size.
            self.assertEqual(str(rec.size), str(exe.size))
            # Same trading direction.
            self.assertEqual(canonical(rec.side), canonical(exe.side))

    def test_row_identity_prefers_exchange_instrument(self):
        # _row_identity returns the exchange_instrument when
        # present, regardless of row.symbol.
        class Row:
            def __init__(self):
                self.symbol = "XAU"
                self.exchange_instrument = "XAU-USD.P"
        self.assertEqual(_row_identity(Row()), "XAU-USD.P")

    def test_row_identity_falls_back_to_symbol(self):
        # _row_identity returns the symbol when exchange_instrument
        # is empty.
        class Row:
            def __init__(self):
                self.symbol = "ETH"
                self.exchange_instrument = ""
        self.assertEqual(_row_identity(Row()), "ETH")


if __name__ == "__main__":
    unittest.main(verbosity=2)
