"""Phase 2.13.20 \u2014 ts-only ordering regression tests.

The MT4 reader uses ``ts`` (Observer Unix timestamp) as the
authoritative ordering key for snapshot acceptance. ``seq`` is
captured for diagnostics only and has ZERO effect on whether a
snapshot is accepted.

These tests prove every property the user spec requires:

  1. newer ts + lower seq   -> ACCEPT
  2. newer ts + seq reset 1 -> ACCEPT (Observer restart)
  3. newer ts + any seq     -> ACCEPT
  4. same ts + any seq      -> REJECT
  5. older ts + any seq     -> REJECT
  6. seq alone can never cause acceptance/rejection
  7. received_at refreshes after every accepted newer-ts snapshot
  8. Observer restart requires no special handling
  9. complete 13-fibo snapshot is accepted
 10. ETHUSD FASTFib appears
 11. SOLUSD FASTFib appears
 12. /fibo Start Fibo shows 13 available

Tests use a fake Telegram transport (FakeLongPoll) and a temp
HERMES_HOME so no production state is touched.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Force HERMES_HOME to a tempdir BEFORE importing the fibo modules
# so the snapshot store does not pollute production state.
_TEST_HERMES_HOME = tempfile.mkdtemp(prefix="fibo_ts_test_")
os.environ["HERMES_HOME"] = _TEST_HERMES_HOME

from plugins.trade.fibo import mt4_reader  # noqa: E402
from plugins.trade.fibo.mt4_reader import (  # noqa: E402
    ACCEPTED,
    IGNORED_DUP,
    IGNORED_OLDER,
    Mt4ReaderProcess,
)


CHAT = -1004351200469
SENDER = 8422755957
SOURCE = "mt4-Fresh542468-1"


# -----------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------


class FakeLongPoll:
    """Sequence-of-behaviors fake for ``TelegramLongPoll.get_updates``."""

    def __init__(self, behaviors: List[Dict[str, Any]]) -> None:
        self._behaviors = list(behaviors)
        self.calls: List[Optional[int]] = []

    def get_updates(
        self,
        *,
        offset: Optional[int],
        timeout_seconds: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        self.calls.append(offset)
        if not self._behaviors:
            return []
        behavior = self._behaviors.pop(0)
        if "raise" in behavior:
            raise behavior["raise"]
        if "updates" in behavior:
            return list(behavior["updates"])
        return []


def _make_observer_update(
    *,
    update_id: int,
    chat_id: int,
    sender_id: int,
    source: str,
    seq: int,
    ts: int,
    fibos: Optional[List[Tuple[str, str, int, int]]] = None,
    received_at: str = "2026-08-28T22:00:00Z",
) -> Dict[str, Any]:
    """Build a single Telegram update with explicit ts."""
    if fibos is None:
        fibos = [("XAUUSD", "FASTFIB", 100, 200)]
    fibo_dicts = [
        {
            "symbol": sym,
            "variant": var,
            "percentage": "0.001",
            "buy_cycle_id": bc,
            "cumulative_buy_weight": "1",
            "sell_cycle_id": sc,
            "cumulative_sell_weight": "1",
        }
        for sym, var, bc, sc in fibos
    ]
    body = {
        "v": 1,
        "source": source,
        "seq": seq,
        "ts": ts,
        "fibos": fibo_dicts,
        "received_at": received_at,
        "telegram_update_id": update_id,
        "telegram_message_id": update_id,
        "reader_chat_id": 0,
    }
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id},
            "from": {"id": sender_id, "is_bot": True},
            "text": json.dumps(body),
        },
    }


# -----------------------------------------------------------------------
# Helper
# -----------------------------------------------------------------------


def _make_reader(
    tmpdir: Path,
    api: Any,
) -> Mt4ReaderProcess:
    snap = tmpdir / "mt4_snapshot.json"
    state = tmpdir / "mt4_reader_state.json"
    lock = tmpdir / "mt4_reader.lock"
    health = tmpdir / "mt4_reader_health.json"
    return Mt4ReaderProcess(
        bot_token="TEST",
        expected_chat_id=CHAT,
        expected_sender_id=SENDER,
        snapshot_path=snap,
        reader_state_path=state,
        reader_lock_path=lock,
        health_path=health,
        api=api,
    )


# -----------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------


class NewerTsWithLowerSeqAcceptTest(unittest.TestCase):
    """[1] newer ts + lower seq -> ACCEPT.
    [6] seq alone can never cause acceptance/rejection.
    """

    def test_newer_ts_with_lower_seq_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # First accept a snapshot at ts=2000, seq=500.
            upd_old = _make_observer_update(
                update_id=1, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=500, ts=2000,
            )
            # Now an Observer restart: ts goes up to 2005, but seq
            # resets to 1. This MUST be accepted because ts is the
            # authoritative ordering key.
            upd_restart = _make_observer_update(
                update_id=2, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=1, ts=2005,
            )
            api = FakeLongPoll([
                {"updates": [upd_old]},
                {"updates": [upd_restart]},
            ])
            reader = _make_reader(root, api)
            outcomes1 = reader.run_once(long_poll_seconds=1)
            self.assertTrue(str(outcomes1[0]).startswith(ACCEPTED))
            self.assertEqual(reader.state.last_seq, 500)
            self.assertEqual(reader.state.last_accepted_ts, 2000)
            outcomes2 = reader.run_once(long_poll_seconds=1)
            self.assertTrue(str(outcomes2[0]).startswith(ACCEPTED),
                "newer ts (2005 > 2000) MUST be ACCEPTED regardless "
                "of seq reset (1 < 500)")
            # State advanced to new ts; seq is captured for display.
            self.assertEqual(reader.state.last_seq, 1)
            self.assertEqual(reader.state.last_accepted_ts, 2005)


class ObserverRestartAcceptsAnyTsTest(unittest.TestCase):
    """[2] newer ts + seq reset to 1 -> ACCEPT (Observer restart).
    [8] Observer restart requires no special handling.
    """

    def test_observer_restart_accepted_with_seq_reset(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Pre-seed state with a large last_accepted_ts (e.g.
            # from yesterday). A new Observer process restarts with
            # seq=1 but a fresh ts (within the past few seconds).
            prior = mt4_reader.ReaderState(
                last_update_id=5,
                current_source=SOURCE,
                last_accepted_ts=1700000000,  # yesterday
                last_seq=1000,
            )
            prior.save(root / "mt4_reader_state.json")

            restart_update = _make_observer_update(
                update_id=10, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=1, ts=1700000200,
            )
            api = FakeLongPoll([{"updates": [restart_update]}])
            reader = _make_reader(root, api)
            outcomes = reader.run_once(long_poll_seconds=1)
            self.assertEqual(len(outcomes), 1)
            self.assertTrue(str(outcomes[0]).startswith(ACCEPTED),
                "Observer restart: seq=1 (reset) with newer ts MUST "
                "be ACCEPTED; no special handling required")
            self.assertEqual(reader.state.last_seq, 1)
            self.assertEqual(reader.state.last_accepted_ts, 1700000200)


class NewerTsAnySeqAcceptTest(unittest.TestCase):
    """[3] newer ts + any seq -> ACCEPT."""

    def test_newer_ts_accepted_with_various_seq_values(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Accept the first (initial).
            upd1 = _make_observer_update(
                update_id=1, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=100, ts=1700000000,
            )
            # Subsequent updates: any seq (1, 1000, even decreasing),
            # as long as ts is newer.
            upd2 = _make_observer_update(
                update_id=2, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=1, ts=1700000010,
            )
            upd3 = _make_observer_update(
                update_id=3, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=1000, ts=1700000020,
            )
            api = FakeLongPoll([
                {"updates": [upd1]},
                {"updates": [upd2]},
                {"updates": [upd3]},
            ])
            reader = _make_reader(root, api)
            for i in range(3):
                outcomes = reader.run_once(long_poll_seconds=1)
                self.assertEqual(len(outcomes), 1)
                self.assertTrue(
                    str(outcomes[0]).startswith(ACCEPTED),
                    f"run {i}: newer ts MUST ACCEPT regardless of seq"
                )
            self.assertEqual(reader.state.last_accepted_ts, 1700000020)


class SameTsAnySeqRejectTest(unittest.TestCase):
    """[4] same ts + any seq -> REJECT."""

    def test_same_ts_duplicate_rejected_with_any_seq(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            upd1 = _make_observer_update(
                update_id=1, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=42, ts=1700000000,
            )
            upd2_dup = _make_observer_update(
                update_id=2, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=99, ts=1700000000,  # same ts
            )
            api = FakeLongPoll([
                {"updates": [upd1]},
                {"updates": [upd2_dup]},
            ])
            reader = _make_reader(root, api)
            reader.run_once(long_poll_seconds=1)
            outcomes2 = reader.run_once(long_poll_seconds=1)
            self.assertEqual(len(outcomes2), 1)
            self.assertEqual(
                str(outcomes2[0]), str(
                    mt4_reader.UpdateOutcome(
                        IGNORED_DUP, 2, "ts=1700000000 seq=99"
                    )
                ),
                "same ts MUST be IGNORED_DUP regardless of seq",
            )
            self.assertFalse(str(outcomes2[0]).startswith(ACCEPTED))


class OlderTsAnySeqRejectTest(unittest.TestCase):
    """[5] older ts + any seq -> REJECT."""

    def test_older_ts_rejected_with_any_seq(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            upd_new = _make_observer_update(
                update_id=1, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=10, ts=1700000050,
            )
            upd_older = _make_observer_update(
                update_id=2, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=999, ts=1700000001,  # older ts
            )
            api = FakeLongPoll([
                {"updates": [upd_new]},
                {"updates": [upd_older]},
            ])
            reader = _make_reader(root, api)
            reader.run_once(long_poll_seconds=1)
            outcomes2 = reader.run_once(long_poll_seconds=1)
            self.assertEqual(len(outcomes2), 1)
            self.assertFalse(str(outcomes2[0]).startswith(ACCEPTED))
            self.assertTrue(str(outcomes2[0]).startswith(IGNORED_OLDER))
            self.assertEqual(reader.state.last_accepted_ts, 1700000050)


class SeqAloneNeverAcceptsTest(unittest.TestCase):
    """[6] seq alone can never cause acceptance/rejection.
    A message with the same ts as the current cursor MUST be
    IGNORED_DUP even if seq is "newer".
    """

    def test_seq_advance_with_same_ts_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            upd1 = _make_observer_update(
                update_id=1, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=10, ts=1700000000,
            )
            # Same ts, "newer" seq \u2014 must be REJECTED.
            upd2 = _make_observer_update(
                update_id=2, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=999999, ts=1700000000,
            )
            api = FakeLongPoll([
                {"updates": [upd1]},
                {"updates": [upd2]},
            ])
            reader = _make_reader(root, api)
            reader.run_once(long_poll_seconds=1)
            outcomes2 = reader.run_once(long_poll_seconds=1)
            self.assertEqual(len(outcomes2), 1)
            self.assertFalse(str(outcomes2[0]).startswith(ACCEPTED),
                "seq advance ALONE (same ts) MUST NOT cause ACCEPT")
            self.assertEqual(str(outcomes2[0]), str(
                mt4_reader.UpdateOutcome(
                    IGNORED_DUP, 2, "ts=1700000000 seq=999999"
                )
            ))


class ReceivedAtRefreshesPerAcceptedTsTest(unittest.TestCase):
    """[7] received_at refreshes after every accepted newer-ts
    snapshot.
    """

    def test_received_at_writes_fresh_local_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            upd = _make_observer_update(
                update_id=1, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=1, ts=1700000000,
                received_at="2026-08-28T22:00:00Z",
            )
            api = FakeLongPoll([{"updates": [upd]}])
            reader = _make_reader(root, api)
            before = mt4_reader._utc_iso_now()
            outcomes = reader.run_once(long_poll_seconds=1)
            after = mt4_reader._utc_iso_now()
            self.assertEqual(len(outcomes), 1)
            self.assertTrue(str(outcomes[0]).startswith(ACCEPTED))
            # The persisted snapshot's received_at must be a
            # fresh local receipt time (not the Observer's value).
            raw = json.loads(
                (root / "mt4_snapshot.json").read_text()
            )
            self.assertGreaterEqual(raw["received_at"], before)
            self.assertLessEqual(raw["received_at"], after)


class TsOrderingPersistedTest(unittest.TestCase):
    """Verify that the persisted ``last_accepted_ts`` is the
    authoritative field on disk (alongside the diagnostic
    ``last_seq``)."""

    def test_persisted_state_uses_last_accepted_ts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            upd = _make_observer_update(
                update_id=1, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=42, ts=1700000123,
            )
            api = FakeLongPoll([{"updates": [upd]}])
            reader = _make_reader(root, api)
            reader.run_once(long_poll_seconds=1)
            on_disk = json.loads(
                (root / "mt4_reader_state.json").read_text()
            )
            self.assertEqual(on_disk["last_accepted_ts"], 1700000123)
            self.assertEqual(on_disk["last_seq"], 42)
            self.assertEqual(on_disk["current_source"], SOURCE)

    def test_load_backward_compat_with_old_state_files(self):
        """State files written before Phase 2.13.20 only have
        ``last_seq``. The loader must accept them and start with
        ``last_accepted_ts=0``, so the next newer-ts update is
        accepted."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_path = root / "mt4_reader_state.json"
            # Write a legacy state file (only last_seq).
            state_path.write_text(json.dumps({
                "last_update_id": 5,
                "current_source": SOURCE,
                "last_seq": 67409,
                "retired_sources": [],
            }))
            loaded = mt4_reader.ReaderState.load(state_path)
            self.assertEqual(loaded.last_seq, 67409)
            self.assertEqual(loaded.last_accepted_ts, 0)
            # Next update with ts > 0 (e.g. ts=1700000000) is
            # accepted because last_accepted_ts defaults to 0.
            upd = _make_observer_update(
                update_id=10, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=1, ts=1700000000,
            )
            api = FakeLongPoll([{"updates": [upd]}])
            reader = _make_reader(root, api)
            outcomes = reader.run_once(long_poll_seconds=1)
            self.assertEqual(len(outcomes), 1)
            self.assertTrue(
                str(outcomes[0]).startswith(ACCEPTED),
                "after loading legacy state with no last_accepted_ts, "
                "a newer ts update must be ACCEPTED"
            )


class CompleteThirteenFiboTest(unittest.TestCase):
    """[9] complete 13-fibo snapshot is accepted (with current
    ETHUSD FASTFib and SOLUSD FASTFib).
    [10] ETHUSD FASTFib appears.
    [11] SOLUSD FASTFib appears.
    [12] /fibo Start Fibo shows 13 available.
    """

    THIRTEEN_PAIRS = [
        ("#DJI30", "NORMALFIB"), ("#NQ100", "NORMALFIB"),
        ("#SP500", "NORMALFIB"),
        ("BTCUSD", "NORMALFIB"), ("BTCUSD", "FASTFIB"),
        ("ETHUSD", "NORMALFIB"), ("ETHUSD", "FASTFIB"),
        ("SOLUSD", "NORMALFIB"), ("SOLUSD", "FASTFIB"),
        ("XAUUSD", "NORMALFIB"), ("XAUUSD", "FASTFIB"),
        ("ZECUSD", "NORMALFIB"), ("ZECUSD", "FASTFIB"),
    ]

    def test_13_fibo_snapshot_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fibos = [
                (sym, var, i * 10, i * 10 + 5)
                for i, (sym, var) in enumerate(self.THIRTEEN_PAIRS)
            ]
            upd = _make_observer_update(
                update_id=1, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=99999, ts=1700000999,
                fibos=fibos,
            )
            api = FakeLongPoll([{"updates": [upd]}])
            reader = _make_reader(root, api)
            outcomes = reader.run_once(long_poll_seconds=1)
            self.assertEqual(len(outcomes), 1)
            self.assertTrue(str(outcomes[0]).startswith(ACCEPTED))
            raw = json.loads(
                (root / "mt4_snapshot.json").read_text()
            )
            self.assertEqual(len(raw["fibos"]), 13,
                "the persisted snapshot must contain all 13 fibos")
            keys = {
                (f["symbol"], f["variant"].upper())
                for f in raw["fibos"]
            }
            # ETHUSD + FASTFib MUST be present.
            self.assertIn(("ETHUSD", "FASTFIB"), keys)
            # SOLUSD + FASTFib MUST be present.
            self.assertIn(("SOLUSD", "FASTFIB"), keys)

    def test_start_fibo_sees_13_available(self):
        """The wizard's Start Fibo screen must produce 13
        unique (symbol, variant) pairs from this snapshot."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fibos = [
                (sym, var, i * 10, i * 10 + 5)
                for i, (sym, var) in enumerate(self.THIRTEEN_PAIRS)
            ]
            upd = _make_observer_update(
                update_id=1, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=99999, ts=1700000999,
                fibos=fibos,
            )
            api = FakeLongPoll([{"updates": [upd]}])
            reader = _make_reader(root, api)
            outcomes = reader.run_once(long_poll_seconds=1)
            self.assertTrue(str(outcomes[0]).startswith(ACCEPTED))

            # Use the same snapshot model the wizard uses.
            from plugins.trade.fibo.snapshot import Mt4SnapshotStore
            store = Mt4SnapshotStore(root / "mt4_snapshot.json")
            snap = store.load()
            self.assertIsNotNone(snap)
            pairs = snap.unique_symbol_variant_pairs()
            self.assertEqual(len(pairs), 13,
                "Start Fibo must see 13 unique pairs")
            keys = {
                (p["symbol"], p["variant"].upper())
                for p in pairs
            }
            self.assertIn(("ETHUSD", "FASTFIB"), keys)
            self.assertIn(("SOLUSD", "FASTFIB"), keys)


class NormalAndFastVariantsNotCollapsedTest(unittest.TestCase):
    """NORMALFib and FASTFib for the same symbol are TWO distinct
    Start Fibo choices \u2014 they must never be collapsed."""

    def test_normal_and_fast_variants_are_separate_pairs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            upd = _make_observer_update(
                update_id=1, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=1, ts=1700000000,
                fibos=[
                    ("XAUUSD", "NORMALFIB", 1, 2),
                    ("XAUUSD", "FASTFIB", 3, 4),
                    ("BTCUSD", "NORMALFIB", 5, 6),
                    ("BTCUSD", "FASTFIB", 7, 8),
                ],
            )
            api = FakeLongPoll([{"updates": [upd]}])
            reader = _make_reader(root, api)
            outcomes = reader.run_once(long_poll_seconds=1)
            self.assertTrue(str(outcomes[0]).startswith(ACCEPTED))
            from plugins.trade.fibo.snapshot import Mt4SnapshotStore
            store = Mt4SnapshotStore(root / "mt4_snapshot.json")
            snap = store.load()
            pairs = snap.unique_symbol_variant_pairs()
            self.assertEqual(len(pairs), 4,
                "NORMALFib and FASTFib must remain 4 distinct pairs")


class TsUnaffectedBySeqJumpTest(unittest.TestCase):
    """A 'seq jump' backward (e.g. due to a clock change) MUST be
    accepted as long as the incoming ts is greater than the
    cursor. seq is irrelevant."""

    def test_backwards_seq_with_forward_ts_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            prior = mt4_reader.ReaderState(
                last_update_id=5,
                current_source=SOURCE,
                last_accepted_ts=1700000100,
                last_seq=1000,
            )
            prior.save(root / "mt4_reader_state.json")
            # A "fresh" Observer restart: ts is HIGHER than the
            # cursor, but seq went down to 1.
            upd = _make_observer_update(
                update_id=10, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=1, ts=1700000200,
            )
            api = FakeLongPoll([{"updates": [upd]}])
            reader = _make_reader(root, api)
            outcomes = reader.run_once(long_poll_seconds=1)
            self.assertEqual(len(outcomes), 1)
            self.assertTrue(
                str(outcomes[0]).startswith(ACCEPTED),
                "ts forward + seq backward MUST be ACCEPTED; "
                "only ts matters"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
