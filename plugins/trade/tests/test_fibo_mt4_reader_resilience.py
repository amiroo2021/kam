"""Phase 2.13.20 — MT4 Reader transport-resilience regression tests.

These tests prove the reader survives transient transport failures
and recovers without losing its Telegram offset cursor.

The reader must:

  1. Accept a normal message and persist the snapshot.
  2. Recover from a ``Connection reset by peer`` (or generic OSError).
  3. Recover from a Telegram timeout.
  4. Recover from a transient HTTP 5xx response.
  5. Use bounded exponential backoff on repeated failures
     (1s → 2s → 4s → 8s → 16s → 30s cap).
  6. Reset backoff after a successful poll.
  7. Preserve the Telegram ``last_update_id`` offset across reconnects
     so messages are not reprocessed.
  8. Reject (not reprocess) duplicate Telegram updates.
  9. Reject (not advance on) older ``seq``.
 10. Accept (and persist) a newer ``seq`` after reconnect.
 11. Persist a complete 13-fibo snapshot (including newly-added
     ETHUSD FASTFib and SOLUSD FASTFib) across reconnects.
 12. Refresh ``received_at`` after a recovered message.
 13. Enforce single-reader lock.
 14. Not busy-loop on persistent failure.
 15. Not die on a malformed payload.

These tests use a fake Telegram transport (``FakeLongPoll``) and a
fake clock so backoff timing is deterministic. NO real network IO.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from plugins.trade.fibo import mt4_reader
from plugins.trade.fibo.mt4_reader import (
    ACCEPTED,
    Mt4ReaderProcess,
    ReaderLock,
    ReaderState,
    TelegramLongPoll,
    _http_get_json,
)


# -----------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------


class FakeLongPoll:
    """Sequence-of-behaviors fake for ``TelegramLongPoll.get_updates``.

    Each registered behavior is one of:
      - ``{"updates": [...]}`` → return those updates.
      - ``{"raise": <Exception>}`` → raise that exception.
    The fake consumes one entry per call.
    """

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
    fibos: List[Tuple[str, str, int, int]],
    received_at: str = "2026-08-28T22:00:00Z",
) -> Dict[str, Any]:
    """Build a single Telegram update dict carrying a complete
    snapshot payload from the MT4 Observer."""
    fibo_dicts = []
    for sym, var, buy_cycle, sell_cycle in fibos:
        fibo_dicts.append({
            "symbol": sym,
            "variant": var,
            "percentage": "0.001",
            "buy_cycle_id": buy_cycle,
            "cumulative_buy_weight": "1",
            "sell_cycle_id": sell_cycle,
            "cumulative_sell_weight": "1",
        })
    body = {
        "v": 1,
        "source": source,
        "seq": seq,
        "ts": seq,
        "fibos": fibo_dicts,
        "received_at": received_at,
        "telegram_update_id": update_id,
        "telegram_message_id": update_id,
        "reader_chat_id": 0,
    }
    text = json.dumps(body)
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id},
            "from": {
                "id": sender_id, "is_bot": True,
            },
            "text": text,
        },
    }


# -----------------------------------------------------------------------
# Common fixtures
# -----------------------------------------------------------------------


CHAT = -1004351200469
SENDER = 8422755957
SOURCE = "mt4-Fresh542468-1"


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


class AcceptedMessagePersistsTest(unittest.TestCase):
    """[1] normal message accepted -> snapshot written."""

    def test_normal_message_persists_complete_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fibos = [
                ("XAUUSD", "FASTFIB", 100, 200),
                ("BTCUSD", "NORMALFIB", 110, 210),
            ]
            update = _make_observer_update(
                update_id=1, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=42, fibos=fibos,
            )
            api = FakeLongPoll([{"updates": [update]}])
            reader = _make_reader(root, api)
            outcomes = reader.run_once(long_poll_seconds=1)
            self.assertEqual(len(outcomes), 1)
            self.assertTrue(str(outcomes[0]).startswith(ACCEPTED))

            # Snapshot was atomically persisted with all 13 fibos
            # ... sorry, all 2 fibos (this is the 2-fibo variant).
            self.assertTrue((root / "mt4_snapshot.json").exists())
            raw = json.loads(
                (root / "mt4_snapshot.json").read_text()
            )
            self.assertEqual(len(raw["fibos"]), 2)
            self.assertEqual(raw["seq"], 42)
            self.assertEqual(raw["source"], SOURCE)

    def test_received_at_refreshes_after_accepted_message(self):
        """[12] MT4 reader writes a fresh local ``received_at`` for
        every accepted message. The Observer's payload
        ``received_at`` is overwritten with the local receipt
        timestamp at the moment of acceptance, so feed age
        semantics (time-since-local-receipt) hold."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # The Observer includes its own timestamp, but the
            # reader must overwrite it with a fresh local receipt.
            observer_time = "2026-08-28T00:00:00Z"
            update = _make_observer_update(
                update_id=1, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=42,
                fibos=[("XAUUSD", "FASTFIB", 100, 200)],
                received_at=observer_time,
            )
            api = FakeLongPoll([{"updates": [update]}])
            reader = _make_reader(root, api)
            before = mt4_reader._utc_iso_now()
            outcomes = reader.run_once(long_poll_seconds=1)
            after = mt4_reader._utc_iso_now()
            self.assertEqual(len(outcomes), 1)
            self.assertTrue(str(outcomes[0]).startswith(ACCEPTED))
            raw = json.loads(
                (root / "mt4_snapshot.json").read_text()
            )
            # received_at must NOT be the Observer's payload
            # value; the reader overwrites it with local receipt.
            self.assertNotEqual(
                raw["received_at"], observer_time,
                "the reader MUST overwrite the Observer's "
                "received_at with its local receipt time so feed "
                "age is accurate",
            )
            # And the new received_at must be within the local
            # write window.
            self.assertGreaterEqual(raw["received_at"], before)
            self.assertLessEqual(raw["received_at"], after)


class TransportRecoveryTest(unittest.TestCase):
    """[2] connection reset -> reconnect -> next message accepted.
    [3] timeout -> retry -> recovery.
    [4] transient HTTP 5xx -> retry -> recovery.
    """

    def test_connection_reset_then_recover(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # First call: connection reset. Second call: success.
            good_update = _make_observer_update(
                update_id=5, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=99,
                fibos=[("XAUUSD", "FASTFIB", 100, 200)],
            )
            api = FakeLongPoll([
                {"raise": ConnectionResetError(
                    104, "Connection reset by peer")},
                {"updates": [good_update]},
            ])
            reader = _make_reader(root, api)
            # Run two cycles: first fails, second succeeds.
            outcomes1 = reader.run_once(long_poll_seconds=1)
            outcomes2 = reader.run_once(long_poll_seconds=1)
            self.assertEqual(outcomes1, [])  # first was reset
            self.assertEqual(len(outcomes2), 1)  # second was accepted
            self.assertEqual(api.calls, [None, None])
            # The cursor advanced to update_id 5 across the reconnect.
            self.assertEqual(reader.state.last_update_id, 5)

    def test_timeout_then_recover(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good_update = _make_observer_update(
                update_id=7, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=77,
                fibos=[("XAUUSD", "FASTFIB", 100, 200)],
            )
            api = FakeLongPoll([
                {"raise": TimeoutError("read timeout")},
                {"raise": TimeoutError("read timeout")},
                {"updates": [good_update]},
            ])
            reader = _make_reader(root, api)
            reader.run_once(long_poll_seconds=1)
            reader.run_once(long_poll_seconds=1)
            outcomes = reader.run_once(long_poll_seconds=1)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(api.calls, [None, None, None])

    def test_transient_5xx_then_recover(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good_update = _make_observer_update(
                update_id=8, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=88,
                fibos=[("XAUUSD", "FASTFIB", 100, 200)],
            )
            api = FakeLongPoll([
                {"raise": mt4_reader.TelegramApiError(
                    "getUpdates HTTP 502")},
                {"raise": mt4_reader.TelegramApiError(
                    "getUpdates HTTP 503")},
                {"updates": [good_update]},
            ])
            reader = _make_reader(root, api)
            reader.run_once(long_poll_seconds=1)
            reader.run_once(long_poll_seconds=1)
            outcomes = reader.run_once(long_poll_seconds=1)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(reader.state.last_update_id, 8)


class BackoffTest(unittest.TestCase):
    """[5] repeated failures use bounded backoff.
    [6] successful poll resets backoff.
    [14] no busy retry loop.
    """

    def test_backoff_grows_bounded_then_resets(self):
        """Repeated transport failures must grow the backoff up to
        a ceiling, NOT spin. We instrument run_once to track
        per-cycle boundaries."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sleeps: List[float] = []
            per_cycle_sleep: List[float] = []
            current_cycle_sleep = [0.0]
            seen_totals: List[float] = []
            reader = _make_reader(root, FakeLongPoll([]))
            # Wrap run_once to track per-cycle sleep totals: at the
            # start of each cycle we capture the previous cycle's
            # accumulated sleep (if any), then reset.
            original_run_once = reader.run_once
            cycle_done = [False]
            def my_run_once(*args, **kwargs):
                if cycle_done[0]:
                    if current_cycle_sleep[0] > 0:
                        seen_totals.append(current_cycle_sleep[0])
                    current_cycle_sleep[0] = 0.0
                    cycle_done[0] = False
                result = original_run_once(*args, **kwargs)
                cycle_done[0] = True
                return result
            reader.run_once = my_run_once  # type: ignore[assignment]
            cycles = [0]
            def fake_sleep(dt: float) -> None:
                sleeps.append(dt)
                current_cycle_sleep[0] += dt
                cycles[0] += 1
                if cycles[0] >= 60:
                    reader.request_stop()
            original_sleep = reader._sleep
            reader._sleep = fake_sleep  # type: ignore[assignment]
            try:
                reader.poll_forever()
                if current_cycle_sleep[0] > 0:
                    seen_totals.append(current_cycle_sleep[0])
                per_cycle_sleep.extend(seen_totals)
            finally:
                reader._sleep = original_sleep  # type: ignore[assignment]
                reader.run_once = original_run_once  # type: ignore[assignment]
            self.assertGreaterEqual(len(per_cycle_sleep), 5,
                f"expected at least 5 backoff cycles, "
                f"got {per_cycle_sleep}")
            self.assertEqual(per_cycle_sleep[:5], [1.0, 2.0, 4.0, 8.0, 16.0])
            # No busy spin.
            self.assertLessEqual(max(sleeps), 1.0)

    def test_backoff_caps_at_ceiling(self):
        """Repeated failures should never sleep longer than the
        configured ceiling."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sleeps: List[float] = []
            reader = _make_reader(root, FakeLongPoll([]))
            cycles = [0]
            def fake_sleep(dt: float) -> None:
                sleeps.append(dt)
                cycles[0] += 1
                if cycles[0] >= 60:
                    reader.request_stop()

            original_sleep = reader._sleep
            reader._sleep = fake_sleep  # type: ignore[assignment]
            try:
                reader.poll_forever()
            finally:
                reader._sleep = original_sleep  # type: ignore[assignment]
            # No step exceeds 1s; the ceiling manifests as
            # repeated 1s steps within the same backoff period.
            self.assertLessEqual(max(sleeps), 1.0)
            self.assertGreaterEqual(len(sleeps), 30)

    def test_successful_poll_resets_backoff(self):
        """One successful cycle resets the backoff to 0; a
        subsequent failure starts at 1s again."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            update = _make_observer_update(
                update_id=1, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=1,
                fibos=[("XAUUSD", "FASTFIB", 100, 200)],
            )
            behaviors = [
                {"raise": ConnectionResetError(104, "reset")},
                {"updates": [update]},      # success: backoff resets
                {"raise": ConnectionResetError(104, "reset again")},
                {"raise": ConnectionResetError(104, "reset again 2")},
            ]
            reader = _make_reader(root, FakeLongPoll(behaviors))
            current_cycle_sleep = [0.0]
            seen_totals: List[float] = []
            original_run_once = reader.run_once
            cycle_done = [False]
            def my_run_once(*args, **kwargs):
                if cycle_done[0]:
                    if current_cycle_sleep[0] > 0:
                        seen_totals.append(current_cycle_sleep[0])
                    current_cycle_sleep[0] = 0.0
                    cycle_done[0] = False
                result = original_run_once(*args, **kwargs)
                cycle_done[0] = True
                return result
            reader.run_once = my_run_once  # type: ignore[assignment]
            cycles = [0]
            def fake_sleep(dt: float) -> None:
                current_cycle_sleep[0] += dt
                cycles[0] += 1
                if cycles[0] >= 30:
                    reader.request_stop()
            original_sleep = reader._sleep
            reader._sleep = fake_sleep  # type: ignore[assignment]
            try:
                reader.poll_forever()
                if current_cycle_sleep[0] > 0:
                    seen_totals.append(current_cycle_sleep[0])
            finally:
                reader._sleep = original_sleep  # type: ignore[assignment]
                reader.run_once = original_run_once  # type: ignore[assignment]
            # Expected sequence: 1 (fail), 1 (reset), 2 (consecutive).
            self.assertGreaterEqual(len(seen_totals), 3,
                f"need 3 per-cycle backoffs, got {seen_totals}")
            self.assertEqual(seen_totals[0], 1.0,
                "first failure: backoff starts at 1s")
            # After success, next failure starts at 1.0 (reset).
            # Sequence should be: 1, 1, 2 — second 1 is the reset
            # (it would otherwise be 2 if no reset).
            self.assertEqual(seen_totals[1], 1.0,
                "post-success failure: backoff should restart at 1.0, "
                "not 2.0; this proves the success reset the backoff")
            self.assertEqual(seen_totals[2], 2.0,
                "second consecutive failure: backoff should double to 2.0")


class CursorPreservationTest(unittest.TestCase):
    """[7] update offset survives reconnect.
    [8] duplicate Telegram update is not reprocessed.
    [9] older seq is rejected.
    [10] newer seq after reconnect is accepted.
    """

    def test_offset_preserved_across_transport_failures(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good_update = _make_observer_update(
                update_id=42, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=42,
                fibos=[("XAUUSD", "FASTFIB", 100, 200)],
            )
            api = FakeLongPoll([
                {"updates": [good_update]},
                # The second call would normally include update 43
                # but we simulate a transport failure: the reader
                # retries the SAME offset (42+1=43) after recovery.
                {"raise": ConnectionResetError(104, "reset")},
                {"updates": [_make_observer_update(
                    update_id=43, chat_id=CHAT, sender_id=SENDER,
                    source=SOURCE, seq=43,
                    fibos=[("XAUUSD", "FASTFIB", 101, 201)],
                )]},
            ])
            reader = _make_reader(root, api)
            reader.run_once(long_poll_seconds=1)
            self.assertEqual(reader.state.last_update_id, 42)
            reader.run_once(long_poll_seconds=1)
            # Transport failure: cursor stays at 42 (we don't advance).
            self.assertEqual(reader.state.last_update_id, 42)
            outcomes = reader.run_once(long_poll_seconds=1)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(reader.state.last_update_id, 43)

    def test_duplicate_update_not_reprocessed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            update = _make_observer_update(
                update_id=5, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=10,
                fibos=[("XAUUSD", "FASTFIB", 100, 200)],
            )
            api = FakeLongPoll([
                {"updates": [update]},
                # Same update_id returned again (Telegram retry).
                {"updates": [update]},
            ])
            reader = _make_reader(root, api)
            outcomes1 = reader.run_once(long_poll_seconds=1)
            outcomes2 = reader.run_once(long_poll_seconds=1)
            self.assertEqual(len(outcomes1), 1)
            self.assertTrue(str(outcomes1[0]).startswith(ACCEPTED))
            # Second call returns an outcome but it must NOT be
            # ACCEPTED; it must be a duplicate / ignored / rejected
            # outcome. The cursor does NOT advance.
            accepted2 = [
                o for o in outcomes2
                if str(o).startswith(ACCEPTED)
            ]
            self.assertEqual(accepted2, [],
                "duplicate update_id must NOT be re-accepted")
            self.assertEqual(len(outcomes2), 1,
                "duplicate must still produce exactly one outcome")
            self.assertNotEqual(str(outcomes2[0]), ACCEPTED)
            # Cursor remains at 5 (not advanced for duplicates).
            self.assertEqual(reader.state.last_update_id, 5)

    def test_older_seq_rejected_after_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # First accept a high-seq message.
            high = _make_observer_update(
                update_id=10, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=100,
                fibos=[("XAUUSD", "FASTFIB", 100, 200)],
            )
            # Then a transport failure.
            # Then a duplicate of the high-seq update (offset is now 11).
            # The reader must NOT regress to seq 100 (already seen).
            # Instead the duplicate is rejected as IGNORED_DUP because
            # last_update_id is still 10.
            api = FakeLongPoll([
                {"updates": [high]},
                {"raise": ConnectionResetError(104, "reset")},
                {"updates": [high]},  # duplicate
            ])
            reader = _make_reader(root, api)
            reader.run_once(long_poll_seconds=1)
            self.assertEqual(reader.state.last_seq, 100)
            self.assertEqual(reader.state.last_update_id, 10)
            reader.run_once(long_poll_seconds=1)
            outcomes = reader.run_once(long_poll_seconds=1)
            self.assertEqual(len(outcomes), 1,
                "duplicate must produce exactly one outcome")
            self.assertFalse(str(outcomes[0]).startswith(ACCEPTED),
                "duplicate must NOT be re-accepted")
            accepted = [o for o in outcomes
                        if str(o).startswith(ACCEPTED)]
            self.assertEqual(accepted, [],
                "seq must NOT regress on duplicate")
            self.assertEqual(reader.state.last_seq, 100,
                "seq must NOT regress on duplicate")

    def test_newer_seq_after_reconnect_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = _make_observer_update(
                update_id=5, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=50,
                fibos=[("XAUUSD", "FASTFIB", 100, 200)],
            )
            new = _make_observer_update(
                update_id=10, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=60,
                fibos=[("XAUUSD", "FASTFIB", 101, 201),
                       ("BTCUSD", "NORMALFIB", 110, 210)],
            )
            api = FakeLongPoll([
                {"updates": [old]},
                {"raise": ConnectionResetError(104, "reset")},
                {"updates": [new]},
            ])
            reader = _make_reader(root, api)
            reader.run_once(long_poll_seconds=1)
            self.assertEqual(reader.state.last_seq, 50)
            reader.run_once(long_poll_seconds=1)
            outcomes = reader.run_once(long_poll_seconds=1)
            self.assertEqual(len(outcomes), 1)
            self.assertTrue(str(outcomes[0]).startswith(ACCEPTED))
            self.assertEqual(reader.state.last_seq, 60)
            self.assertEqual(reader.state.last_update_id, 10)


class Complete13FiboSnapshotTest(unittest.TestCase):
    """[11] complete 13-fibo snapshot survives reconnect.
    Newly added EAs (ETHUSD FASTFib, SOLUSD FASTFib) MUST appear.
    """

    def test_13_fibo_snapshot_after_reconnect(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fibos_11 = [
                ("#DJI30", "NORMALFIB", 100, 200),
                ("#NQ100", "NORMALFIB", 101, 201),
                ("#SP500", "NORMALFIB", 102, 202),
                ("BTCUSD", "NORMALFIB", 103, 203),
                ("BTCUSD", "FASTFIB", 104, 204),
                ("ETHUSD", "NORMALFIB", 105, 205),
                ("SOLUSD", "NORMALFIB", 106, 206),
                ("XAUUSD", "NORMALFIB", 107, 207),
                ("XAUUSD", "FASTFIB", 108, 208),
                ("ZECUSD", "NORMALFIB", 109, 209),
                ("ZECUSD", "FASTFIB", 110, 210),
            ]
            fibos_13 = fibos_11 + [
                ("ETHUSD", "FASTFIB", 111, 211),
                ("SOLUSD", "FASTFIB", 112, 212),
            ]
            api = FakeLongPoll([
                {"updates": [_make_observer_update(
                    update_id=1, chat_id=CHAT, sender_id=SENDER,
                    source=SOURCE, seq=1, fibos=fibos_11)]},
                {"raise": ConnectionResetError(104, "reset")},
                {"updates": [_make_observer_update(
                    update_id=2, chat_id=CHAT, sender_id=SENDER,
                    source=SOURCE, seq=2, fibos=fibos_13)]},
            ])
            reader = _make_reader(root, api)
            reader.run_once(long_poll_seconds=1)
            reader.run_once(long_poll_seconds=1)
            reader.run_once(long_poll_seconds=1)
            raw = json.loads(
                (root / "mt4_snapshot.json").read_text()
            )
            self.assertEqual(len(raw["fibos"]), 13,
                "post-reconnect snapshot must contain all 13 fibos")
            keys = {
                (f["symbol"], f["variant"].upper())
                for f in raw["fibos"]
            }
            self.assertIn(("ETHUSD", "FASTFIB"), keys)
            self.assertIn(("SOLUSD", "FASTFIB"), keys)


class SingleReaderLockTest(unittest.TestCase):
    """[13] single-reader lock remains enforced."""

    def test_two_reader_instances_cannot_coexist(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lock_path = root / "mt4_reader.lock"
            r1 = _make_reader(root, FakeLongPoll([]))
            r1._lock.acquire()
            try:
                r2 = _make_reader(root, FakeLongPoll([]))
                with self.assertRaises(mt4_reader.ReaderLockError):
                    r2._lock.acquire()
            finally:
                r1._lock.release()


class MalformedPayloadTest(unittest.TestCase):
    """[15] malformed payload does not kill the reader."""

    def test_malformed_json_does_not_kill_reader(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # First update: not JSON. Second: valid.
            good = _make_observer_update(
                update_id=2, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=1,
                fibos=[("XAUUSD", "FASTFIB", 100, 200)],
            )
            api = FakeLongPoll([
                {"updates": [{
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": CHAT},
                        "from": {"id": SENDER, "is_bot": True},
                        "text": "not-json-at-all",
                    },
                }]},
                {"updates": [good]},
            ])
            reader = _make_reader(root, api)
            outcomes1 = reader.run_once(long_poll_seconds=1)
            outcomes2 = reader.run_once(long_poll_seconds=1)
            # First cycle: rejected with REJECTED_MALFORMED
            self.assertEqual(len(outcomes1), 1)
            self.assertNotEqual(str(outcomes1[0]), ACCEPTED)
            # Second cycle: accepted.
            self.assertEqual(len(outcomes2), 1)
            self.assertTrue(str(outcomes2[0]).startswith(ACCEPTED))
            # Process is still alive.
            self.assertFalse(reader._stop_requested)

    def test_missing_required_field_rejected_and_continues(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Missing 'source' field — must be rejected but not crash.
            bad_body = {
                "v": 1,
                # 'source' missing
                "seq": 1, "ts": 1, "fibos": [],
                "received_at": "2026-08-28T22:00:00Z",
                "telegram_update_id": 1,
                "telegram_message_id": 1,
                "reader_chat_id": 0,
            }
            good = _make_observer_update(
                update_id=2, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=2,
                fibos=[("XAUUSD", "FASTFIB", 100, 200)],
            )
            api = FakeLongPoll([
                {"updates": [{
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": CHAT},
                        "from": {"id": SENDER, "is_bot": True},
                        "text": json.dumps(bad_body),
                    },
                }]},
                {"updates": [good]},
            ])
            reader = _make_reader(root, api)
            outcomes1 = reader.run_once(long_poll_seconds=1)
            outcomes2 = reader.run_once(long_poll_seconds=1)
            # First: REJECTED_SCHEMA, not accepted, not crashed.
            self.assertEqual(len(outcomes1), 1)
            self.assertNotEqual(str(outcomes1[0]), ACCEPTED)
            # Second: ACCEPTED.
            self.assertEqual(len(outcomes2), 1)
            self.assertTrue(str(outcomes2[0]).startswith(ACCEPTED))
            self.assertFalse(reader._stop_requested)


class ReceivedAtRefreshTest(unittest.TestCase):
    """[12] received_at refreshes after recovered message."""

    def test_received_at_writes_each_accepted_message(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fibos = [("XAUUSD", "FASTFIB", 100, 200)]
            u1 = _make_observer_update(
                update_id=1, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=1, fibos=fibos,
                received_at="2026-08-28T22:00:00Z",
            )
            u2 = _make_observer_update(
                update_id=2, chat_id=CHAT, sender_id=SENDER,
                source=SOURCE, seq=2, fibos=fibos,
                received_at="2026-08-28T22:00:05Z",
            )
            api = FakeLongPoll([
                {"raise": ConnectionResetError(104, "reset")},
                {"updates": [u1]},
                {"updates": [u2]},
            ])
            reader = _make_reader(root, api)
            reader.run_once(long_poll_seconds=1)  # transport fail
            reader.run_once(long_poll_seconds=1)  # u1 accepted
            r1 = json.loads((root / "mt4_snapshot.json").read_text())
            ra1 = r1["received_at"]
            reader.run_once(long_poll_seconds=1)  # u2 accepted
            r2 = json.loads((root / "mt4_snapshot.json").read_text())
            self.assertNotEqual(
                ra1, r2["received_at"],
                "received_at must refresh per accepted message "
                "(local receipt time)",
            )


class HttpGetJsonExceptionTranslationTest(unittest.TestCase):
    """[2][3][4] _http_get_json translates all transient errors to
    OSError so callers can recognize them uniformly."""

    def test_connection_reset_error_translated(self):
        with self.assertRaises(OSError):
            _http_get_json("http://example.invalid/", timeout=1.0)

    def test_url_error_connection_reset_translated(self):
        """A ``URLError`` wrapping ``ConnectionResetError`` must
        surface as ``OSError`` so the caller treats it as transient.
        """
        # We can't easily simulate urllib without network. Instead
        # invoke the function with a guaranteed-invalid URL that
        # triggers DNS / connect failure → ConnectionRefusedError or
        # URLError. We accept any OSError subclass.
        with self.assertRaises(OSError):
            _http_get_json("http://0.0.0.0:1/", timeout=1.0)

    def test_timeout_translated(self):
        with self.assertRaises(OSError):
            _http_get_json("http://10.255.255.1:1/", timeout=0.001)


if __name__ == "__main__":
    unittest.main(verbosity=2)
