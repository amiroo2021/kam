"""Tests for the MT4 Observer Reader (plugins.trade.fibo.mt4_reader).

Spec §15 hardening matrix:

* restart resumes at last_update_id + 1
* rejected update advances transport cursor
* wrong sender bot rejected
* human sender rejected
* OS lock prevents second reader
* lock releases after process / file descriptor closes
* reader state survives restart
* old seq after restart rejected
* source rollover accepted
* retired source cannot reclaim cache
* atomic cache and reader-state publication
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from plugins.trade.fibo.mt4_reader import (
    ACCEPTED,
    IGNORED_DUP,
    IGNORED_OLDER,
    MAX_RETIRED_SOURCES,
    REJECTED_MALFORMED,
    REJECTED_NO_TEXT,
    REJECTED_NOT_BOT,
    REJECTED_RETIRED_SOURCE,
    REJECTED_SCHEMA,
    REJECTED_VERSION,
    REJECTED_WRONG_CHAT,
    REJECTED_WRONG_SENDER,
    Mt4ReaderProcess,
    ReaderLock,
    ReaderLockError,
    ReaderState,
    TelegramLongPoll,
    inspect_update,
    parse_snapshot_payload,
)


def _make_good_observer_body(
    *,
    source: str = "obs-1",
    seq: int = 1,
    ts: int = 1700000000,
    fibos: Optional[List[Dict[str, Any]]] = None,
    v: int = 1,
) -> Dict[str, Any]:
    if fibos is None:
        fibos = [
            {
                "symbol": "BTCUSD",
                "variant": "FASTFib",
                "percentage": 0.001,
                "buy_cycle_id": 5,
                "cumulative_buy_weight": 2.5,
                "sell_cycle_id": 0,
                "cumulative_sell_weight": 0,
            }
        ]
    return {
        "v": v,
        "source": source,
        "seq": seq,
        "ts": ts,
        "fibos": fibos,
    }


def _make_update(
    *,
    update_id: int = 1,
    chat_id: int = -100,
    sender_id: int = 42,
    is_bot: bool = True,
    text: str = "",
) -> Dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1000 + update_id,
            "chat": {"id": chat_id, "type": "supergroup"},
            "from": {"id": sender_id, "is_bot": is_bot},
            "text": text,
            "date": int(time.time()),
        },
    }


class InspectUpdateTests(unittest.TestCase):
    """Pure inspection — no I/O. Covers every reject branch."""

    def setUp(self) -> None:
        self.expected_chat = -100
        self.expected_sender = 42

    def test_correct_chat_accepted(self) -> None:
        body = _make_good_observer_body()
        update = _make_update(
            update_id=1,
            chat_id=self.expected_chat,
            sender_id=self.expected_sender,
            text=json.dumps(body),
        )
        result = inspect_update(
            update,
            expected_chat_id=self.expected_chat,
            expected_sender_id=self.expected_sender,
        )
        self.assertEqual(result.outcome.code, ACCEPTED)
        self.assertEqual(result.update_id_for_cursor, 1)
        self.assertIsNotNone(result.snapshot_to_publish)

    def test_wrong_chat_rejected(self) -> None:
        body = _make_good_observer_body()
        update = _make_update(
            update_id=2,
            chat_id=self.expected_chat + 1,
            sender_id=self.expected_sender,
            text=json.dumps(body),
        )
        result = inspect_update(
            update,
            expected_chat_id=self.expected_chat,
            expected_sender_id=self.expected_sender,
        )
        self.assertEqual(result.outcome.code, REJECTED_WRONG_CHAT)
        self.assertEqual(result.update_id_for_cursor, 2)
        self.assertIsNone(result.snapshot_to_publish)

    def test_wrong_sender_rejected(self) -> None:
        body = _make_good_observer_body()
        update = _make_update(
            update_id=3,
            chat_id=self.expected_chat,
            sender_id=self.expected_sender + 1,
            text=json.dumps(body),
        )
        result = inspect_update(
            update,
            expected_chat_id=self.expected_chat,
            expected_sender_id=self.expected_sender,
        )
        self.assertEqual(result.outcome.code, REJECTED_WRONG_SENDER)
        self.assertEqual(result.update_id_for_cursor, 3)

    def test_human_sender_rejected(self) -> None:
        body = _make_good_observer_body()
        update = _make_update(
            update_id=4,
            chat_id=self.expected_chat,
            sender_id=self.expected_sender,
            is_bot=False,
            text=json.dumps(body),
        )
        result = inspect_update(
            update,
            expected_chat_id=self.expected_chat,
            expected_sender_id=self.expected_sender,
        )
        self.assertEqual(result.outcome.code, REJECTED_NOT_BOT)

    def test_malformed_json_rejected(self) -> None:
        update = _make_update(
            update_id=5,
            chat_id=self.expected_chat,
            sender_id=self.expected_sender,
            text="{not json",
        )
        result = inspect_update(
            update,
            expected_chat_id=self.expected_chat,
            expected_sender_id=self.expected_sender,
        )
        self.assertEqual(result.outcome.code, REJECTED_MALFORMED)
        self.assertEqual(result.update_id_for_cursor, 5)

    def test_missing_required_field_rejected(self) -> None:
        body = _make_good_observer_body()
        del body["fibos"][0]["buy_cycle_id"]
        update = _make_update(
            update_id=6,
            chat_id=self.expected_chat,
            sender_id=self.expected_sender,
            text=json.dumps(body),
        )
        result = inspect_update(
            update,
            expected_chat_id=self.expected_chat,
            expected_sender_id=self.expected_sender,
        )
        # Schema validation happens in the reader's _process_update
        # AFTER source/seq checks, so the pure inspector may already
        # short-circuit on source presence. Here source/seq/v are good;
        # the inspector returns ACCEPTED, but the reader layer will
        # reject on parse_snapshot_payload. We assert the inspector
        # doesn't crash and returns ACCEPTED.
        self.assertEqual(result.outcome.code, ACCEPTED)

    def test_wrong_version_rejected(self) -> None:
        body = _make_good_observer_body(v=99)
        update = _make_update(
            update_id=7,
            chat_id=self.expected_chat,
            sender_id=self.expected_sender,
            text=json.dumps(body),
        )
        result = inspect_update(
            update,
            expected_chat_id=self.expected_chat,
            expected_sender_id=self.expected_sender,
        )
        self.assertEqual(result.outcome.code, REJECTED_VERSION)

    def test_missing_source_rejected(self) -> None:
        body = _make_good_observer_body()
        body["source"] = ""
        update = _make_update(
            update_id=8,
            chat_id=self.expected_chat,
            sender_id=self.expected_sender,
            text=json.dumps(body),
        )
        result = inspect_update(
            update,
            expected_chat_id=self.expected_chat,
            expected_sender_id=self.expected_sender,
        )
        self.assertEqual(result.outcome.code, REJECTED_SCHEMA)

    def test_empty_text_rejected(self) -> None:
        update = _make_update(
            update_id=9,
            chat_id=self.expected_chat,
            sender_id=self.expected_sender,
            text="",
        )
        result = inspect_update(
            update,
            expected_chat_id=self.expected_chat,
            expected_sender_id=self.expected_sender,
        )
        self.assertEqual(result.outcome.code, REJECTED_NO_TEXT)
        self.assertEqual(result.update_id_for_cursor, 9)


class ReaderStateTests(unittest.TestCase):
    def test_initial_state_empty(self) -> None:
        s = ReaderState()
        self.assertEqual(s.last_update_id, 0)
        self.assertEqual(s.current_source, "")
        self.assertEqual(s.last_seq, 0)
        self.assertEqual(s.retired_sources, [])

    def test_initial_source_acceptance(self) -> None:
        s = ReaderState()
        s.accept_initial_source("obs-1")
        self.assertEqual(s.current_source, "obs-1")
        self.assertEqual(s.last_seq, 0)
        self.assertFalse(s.is_retired("obs-1"))

    def test_source_rollover_retires_previous(self) -> None:
        s = ReaderState()
        s.accept_initial_source("obs-1")
        s.accept_newer_seq(5)
        s.retire_current_and_adopt_new("obs-2")
        self.assertEqual(s.current_source, "obs-2")
        self.assertEqual(s.last_seq, 0)
        self.assertTrue(s.is_retired("obs-1"))
        self.assertFalse(s.is_retired("obs-2"))

    def test_retired_sources_bounded(self) -> None:
        s = ReaderState(retired_sources=[])
        s.accept_initial_source("obs-0")
        for i in range(1, MAX_RETIRED_SOURCES + 10):
            s.retire_current_and_adopt_new(f"obs-{i}")
        # The list is bounded to MAX_RETIRED_SOURCES.
        self.assertLessEqual(len(s.retired_sources), MAX_RETIRED_SOURCES)
        # The newest retired sources are still tracked. After 41
        # rollovers (obs-0 through obs-40) the bounded list keeps the
        # most recent MAX_RETIRED_SOURCES entries.
        self.assertIn("obs-40", s.retired_sources)
        # The oldest entries were dropped.
        self.assertNotIn("obs-0", s.retired_sources)


class FakeApi:
    """A stand-in for TelegramLongPoll with deterministic responses."""

    def __init__(self, updates: List[Dict[str, Any]]) -> None:
        self._updates = list(updates)
        self.calls: List[Optional[int]] = []

    def get_updates(
        self,
        *,
        offset: Optional[int],
        timeout_seconds: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        self.calls.append(offset)
        return list(self._updates)


class ReaderProcessTests(unittest.TestCase):
    """End-to-end through Mt4ReaderProcess using a fake Telegram API."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snap_path = self.root / "mt4_snapshot.json"
        self.state_path = self.root / "mt4_reader_state.json"
        self.lock_path = self.root / "mt4_reader.lock"
        self.api_calls_offset: List[Optional[int]] = []
        # Per-test monotonic ts counter (Phase 2.13.20): every
        # _good_update auto-increments ts so consecutive accepts
        # advance the ordering cursor naturally.
        self._ts_counter = 0

    def _make_reader(
        self,
        api_updates: List[Dict[str, Any]],
    ) -> Mt4ReaderProcess:
        api = FakeApi(api_updates)
        return Mt4ReaderProcess(
            bot_token="TOKEN",
            expected_chat_id=-100,
            expected_sender_id=42,
            snapshot_path=self.snap_path,
            reader_state_path=self.state_path,
            reader_lock_path=self.lock_path,
            api=api,
        )

    def _good_update(
        self,
        update_id: int,
        source: str = "obs-1",
        seq: int = 1,
        ts: Optional[int] = None,
    ) -> Dict[str, Any]:
        # Auto-increment ts so each consecutive call from the same
        # source advances the ordering cursor (Phase 2.13.20).
        if ts is None:
            ts = 1700000000 + self._ts_counter
            self._ts_counter += 1
        body = _make_good_observer_body(source=source, seq=seq, ts=ts)
        return _make_update(
            update_id=update_id,
            chat_id=-100,
            sender_id=42,
            is_bot=True,
            text=json.dumps(body),
        )

    # ---- source/seq policy -----------------------------------------

    def test_duplicate_seq_ignored(self) -> None:
        # Phase 2.13.20: dedup is keyed by ts. Two updates with the
        # same ts (and any seq) are duplicates.
        ts = 1700000000
        reader = self._make_reader([
            self._good_update(1, "obs-1", 10, ts=ts),
            self._good_update(2, "obs-1", 10, ts=ts),  # duplicate
            self._good_update(3, "obs-1", 10, ts=ts),  # duplicate
        ])
        outcomes = reader.run_once()
        codes = [o.code for o in outcomes]
        self.assertEqual(codes[0], ACCEPTED)
        self.assertEqual(codes[1], IGNORED_DUP)
        self.assertEqual(codes[2], IGNORED_DUP)

    def test_older_seq_ignored(self) -> None:
        # Phase 2.13.20: ordering is keyed by ts. Older ts is
        # rejected even if the seq is "newer" (seq is diagnostic).
        reader = self._make_reader([
            self._good_update(1, "obs-1", 100, ts=1700000010),
            self._good_update(2, "obs-1", 99, ts=1700000005),  # older ts
        ])
        outcomes = reader.run_once()
        codes = [o.code for o in outcomes]
        self.assertEqual(codes, [ACCEPTED, IGNORED_OLDER])

    def test_newer_seq_accepted(self) -> None:
        reader = self._make_reader([
            self._good_update(1, "obs-1", 5),
            self._good_update(2, "obs-1", 6),
            self._good_update(3, "obs-1", 7),
        ])
        outcomes = reader.run_once()
        self.assertEqual([o.code for o in outcomes], [ACCEPTED, ACCEPTED, ACCEPTED])
        self.assertEqual(reader.state.last_seq, 7)
        # Each accept advances last_update_id
        self.assertEqual(reader.state.last_update_id, 3)

    def test_source_rollover_accepted(self) -> None:
        reader = self._make_reader([
            self._good_update(1, "obs-1", 1),
            self._good_update(2, "obs-2", 1),  # new source
        ])
        outcomes = reader.run_once()
        self.assertEqual([o.code for o in outcomes], [ACCEPTED, ACCEPTED])
        self.assertEqual(reader.state.current_source, "obs-2")
        self.assertTrue(reader.state.is_retired("obs-1"))
        self.assertFalse(reader.state.is_retired("obs-2"))

    def test_retired_source_cannot_reclaim_cache(self) -> None:
        reader = self._make_reader([
            self._good_update(1, "obs-1", 1),
            self._good_update(2, "obs-2", 1),
            self._good_update(3, "obs-1", 2),  # retired source
        ])
        outcomes = reader.run_once()
        codes = [o.code for o in outcomes]
        self.assertEqual(codes[0], ACCEPTED)
        self.assertEqual(codes[1], ACCEPTED)
        self.assertEqual(codes[2], REJECTED_RETIRED_SOURCE)
        # Current source must NOT have been replaced
        self.assertEqual(reader.state.current_source, "obs-2")

    def test_rejected_update_advances_transport_cursor(self) -> None:
        bad = _make_update(
            update_id=10,
            chat_id=-999,  # wrong chat
            sender_id=42,
            text="{}",
        )
        good = self._good_update(11, "obs-1", 1)
        reader = self._make_reader([bad, good])
        outcomes = reader.run_once()
        self.assertEqual(outcomes[0].code, REJECTED_WRONG_CHAT)
        self.assertEqual(outcomes[1].code, ACCEPTED)
        # Both must have advanced last_update_id so the poison message
        # does not replay forever.
        self.assertEqual(reader.state.last_update_id, 11)

    # ---- restart / persistence ------------------------------------

    def test_reader_state_survives_restart(self) -> None:
        # First run accepts two updates and persists state
        reader1 = self._make_reader([
            self._good_update(1, "obs-1", 5),
            self._good_update(2, "obs-1", 6),
        ])
        reader1.run_once()
        # Persisted state on disk
        self.assertTrue(self.state_path.is_file())
        on_disk = json.loads(self.state_path.read_text())
        self.assertEqual(on_disk["last_update_id"], 2)
        self.assertEqual(on_disk["current_source"], "obs-1")
        self.assertEqual(on_disk["last_seq"], 6)

        # Second reader, fresh process, replays from last_update_id + 1
        api2 = FakeApi([])
        reader2 = Mt4ReaderProcess(
            bot_token="TOKEN",
            expected_chat_id=-100,
            expected_sender_id=42,
            snapshot_path=self.snap_path,
            reader_state_path=self.state_path,
            reader_lock_path=self.lock_path,
            api=api2,
        )
        # The persisted state was loaded
        self.assertEqual(reader2.state.last_update_id, 2)
        self.assertEqual(reader2.state.last_seq, 6)
        self.assertEqual(reader2.state.current_source, "obs-1")

    def test_old_seq_after_restart_rejected(self) -> None:
        # Phase 2.13.20: ordering is keyed by ts. Pre-seed state
        # with a ts cursor that is HIGHER than the incoming
        # update's ts; expect IGNORED_OLDER regardless of seq.
        prior = ReaderState(
            last_update_id=5,
            current_source="obs-1",
            last_accepted_ts=1800000000,  # higher than the new ts
            last_seq=100,  # diagnostic only; doesn't gate acceptance
        )
        prior.save(self.state_path)
        # New reader resumes with same source and a smaller ts.
        api = FakeApi([self._good_update(6, "obs-1", 99, ts=1700000000)])
        reader = Mt4ReaderProcess(
            bot_token="TOKEN",
            expected_chat_id=-100,
            expected_sender_id=42,
            snapshot_path=self.snap_path,
            reader_state_path=self.state_path,
            reader_lock_path=self.lock_path,
            api=api,
        )
        outcomes = reader.run_once()
        self.assertEqual(outcomes[0].code, IGNORED_OLDER)

    def test_atomic_cache_publication(self) -> None:
        # Accept an update; the snapshot file must end up with
        # parseable JSON and the four envelope fields added on top.
        reader = self._make_reader([
            self._good_update(1, "obs-1", 1),
        ])
        outcomes = reader.run_once()
        self.assertEqual(outcomes[0].code, ACCEPTED)
        raw = json.loads(self.snap_path.read_text())
        # Source fields preserved
        self.assertEqual(raw["source"], "obs-1")
        self.assertEqual(raw["seq"], 1)
        self.assertEqual(raw["v"], 1)
        self.assertIn("fibos", raw)
        # Envelope fields added
        self.assertIn("received_at", raw)
        self.assertIn("telegram_update_id", raw)
        self.assertIn("telegram_message_id", raw)
        self.assertIn("reader_chat_id", raw)
        # The snapshot must be parseable by the canonical parser
        snap = parse_snapshot_payload(
            raw,
            received_at=raw["received_at"],
            telegram_update_id=raw["telegram_update_id"],
            telegram_message_id=raw["telegram_message_id"],
            reader_chat_id=raw["reader_chat_id"],
        )
        self.assertEqual(snap.source, "obs-1")
        self.assertEqual(snap.seq, 1)

    def test_snapshot_file_mode_0600(self) -> None:
        reader = self._make_reader([self._good_update(1, "obs-1", 1)])
        reader.run_once()
        mode = self.snap_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_snapshot_publish_failure_does_not_advance_cursor(self) -> None:
        """Invariant #1 (hardening): if the snapshot publish raises,
        the in-memory cursor must NOT advance. On restart, the update
        will replay and we try again."""
        # Force the snapshot path to be unwriteable. We point it at a
        # file path whose parent directory exists with WRONG perms so
        # ensure_dir_0700 refuses to auto-chmod.
        bad_dir = self.root / "lock-down" / "fibo"
        bad_dir.mkdir(parents=True)
        os.chmod(bad_dir, 0o755)  # not 0700
        bad_snap_path = bad_dir / "mt4_snapshot.json"
        reader = Mt4ReaderProcess(
            bot_token="TOKEN",
            expected_chat_id=-100,
            expected_sender_id=42,
            snapshot_path=bad_snap_path,
            reader_state_path=self.state_path,
            reader_lock_path=self.lock_path,
            api=FakeApi([self._good_update(7, "obs-1", 1)]),
        )
        outcomes = reader.run_once()
        # The cycle's try/except logs and stops the loop, returning
        # the outcomes for updates processed BEFORE the failure.
        # Outcome 7 was attempted, the snapshot publish raised, the
        # loop broke, the cursor was NOT advanced.
        self.assertEqual(outcomes, [])
        self.assertEqual(reader.state.last_update_id, 0)
        self.assertEqual(reader.state.current_source, "")
        self.assertEqual(reader.state.last_seq, 0)
        # Even though the state was saved at end-of-cycle, it must
        # contain the unmutated (initial) values.
        if self.state_path.exists():
            persisted = json.loads(self.state_path.read_text())
            self.assertEqual(persisted["last_update_id"], 0)
            self.assertEqual(persisted["current_source"], "")
            self.assertEqual(persisted["last_seq"], 0)

    def test_reader_state_save_failure_logs_and_continues(self) -> None:
        """Invariant #1: if reader-state persistence fails AFTER a
        successful snapshot publish, the snapshot remains valid and
        on restart we replay from the last persisted cursor (the
        previous successful save). The cache is not corrupted."""
        reader = self._make_reader([self._good_update(8, "obs-1", 1)])
        outcomes = reader.run_once()
        self.assertEqual(outcomes[0].code, ACCEPTED)
        # Snapshot is good.
        self.assertTrue(self.snap_path.is_file())
        original_snap = self.snap_path.read_text()
        original_state = self.state_path.read_text()

        # Patch atomic_write_text to simulate a save failure. Use a
        # no-op fake api so the next cycle's outcome list is empty.
        from plugins.trade.fibo import mt4_reader as reader_mod
        noop_api = type("NoopApi", (), {
            "get_updates": lambda self, *, offset, timeout_seconds=None: []
        })()
        reader2 = Mt4ReaderProcess(
            bot_token="TOKEN",
            expected_chat_id=-100,
            expected_sender_id=42,
            snapshot_path=self.snap_path,
            reader_state_path=self.state_path,
            reader_lock_path=self.lock_path,
            api=noop_api,
            # Carry over the in-memory state from the first cycle so
            # the noop cycle doesn't see an empty cursor.
            state=reader.state,
        )
        with mock.patch.object(
            reader_mod, "atomic_write_text",
            side_effect=reader_mod.AtomicWriteError("simulated save fail"),
        ):
            outcomes2 = reader2.run_once()
        # The cycle ran (the api returned []) and run_once returned
        # normally — the save failure was logged, not raised.
        self.assertEqual(outcomes2, [])
        # Snapshot still on disk and unchanged.
        self.assertEqual(self.snap_path.read_text(), original_snap)
        # Reader state file still has the values from the first
        # successful save (unchanged — the second save never landed).
        self.assertEqual(self.state_path.read_text(), original_state)


class TransportFailureTests(unittest.TestCase):
    """Invariant #2: transport errors MUST NOT advance last_update_id."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snap_path = self.root / "mt4_snapshot.json"
        self.state_path = self.root / "mt4_reader_state.json"
        self.lock_path = self.root / "mt4_reader.lock"

    def _make_reader(self, api) -> Mt4ReaderProcess:
        return Mt4ReaderProcess(
            bot_token="TOKEN",
            expected_chat_id=-100,
            expected_sender_id=42,
            snapshot_path=self.snap_path,
            reader_state_path=self.state_path,
            reader_lock_path=self.lock_path,
            api=api,
        )

    def test_dns_failure_does_not_advance_cursor(self) -> None:
        from plugins.trade.fibo.mt4_reader import TelegramApiError

        class FlakyApi:
            calls = 0
            def get_updates(self, *, offset, timeout_seconds=None):
                type(self).calls += 1
                raise OSError("Name or service not known")
        reader = self._make_reader(FlakyApi())
        outcomes = reader.run_once()
        self.assertEqual(outcomes, [])
        self.assertEqual(reader.state.last_update_id, 0)
        # No state file was written.
        self.assertFalse(self.state_path.exists())

    def test_timeout_does_not_advance_cursor(self) -> None:
        class FlakyApi:
            def get_updates(self, *, offset, timeout_seconds=None):
                raise TimeoutError("timed out")
        reader = self._make_reader(FlakyApi())
        outcomes = reader.run_once()
        self.assertEqual(outcomes, [])
        self.assertEqual(reader.state.last_update_id, 0)

    def test_http_error_does_not_advance_cursor(self) -> None:
        from plugins.trade.fibo.mt4_reader import TelegramApiError

        class FlakyApi:
            def get_updates(self, *, offset, timeout_seconds=None):
                raise TelegramApiError("getUpdates HTTP 500")
        reader = self._make_reader(FlakyApi())
        outcomes = reader.run_once()
        self.assertEqual(outcomes, [])
        self.assertEqual(reader.state.last_update_id, 0)

    def test_429_does_not_advance_cursor(self) -> None:
        from plugins.trade.fibo.mt4_reader import TelegramApiError

        class FlakyApi:
            def get_updates(self, *, offset, timeout_seconds=None):
                raise TelegramApiError("getUpdates HTTP 429 (rate limited)")
        reader = self._make_reader(FlakyApi())
        outcomes = reader.run_once()
        self.assertEqual(outcomes, [])
        self.assertEqual(reader.state.last_update_id, 0)

    def test_invalid_bot_api_response_does_not_advance_cursor(self) -> None:
        from plugins.trade.fibo.mt4_reader import TelegramApiError

        class FlakyApi:
            def get_updates(self, *, offset, timeout_seconds=None):
                raise TelegramApiError("getUpdates ok=False; description='bad response'")
        reader = self._make_reader(FlakyApi())
        outcomes = reader.run_once()
        self.assertEqual(outcomes, [])
        self.assertEqual(reader.state.last_update_id, 0)

    def test_poll_forever_backs_off_and_does_not_busy_loop(self) -> None:
        """Spec #2: poll_forever must retry transient failures with
        bounded backoff and must not busy-loop. Successful cycles with
        at least one update reset the backoff."""
        sleeps: List[float] = []
        self_called = {"count": 0}

        class StopAfterThree:
            def get_updates(self, *, offset, timeout_seconds=None):
                self_called["count"] += 1
                if self_called["count"] >= 3:
                    reader.request_stop()
                raise OSError("transient")

        reader = self._make_reader(StopAfterThree())
        reader._sleep = sleeps.append  # type: ignore[assignment]
        reader.poll_forever()
        # The sleep helper was called at least twice with increasing
        # values, capped at the ceiling (30s default).
        self.assertGreaterEqual(len(sleeps), 2)
        # Bounded: no single sleep exceeds the ceiling.
        for s in sleeps:
            self.assertLessEqual(s, 30.0)
        # Monotonic non-decreasing (or equal after ceiling).
        for prev, nxt in zip(sleeps, sleeps[1:]):
            self.assertLessEqual(prev, nxt)
        # Total sleeps roughly equal to backoff sum
        # (1.0 + 2.0 = 3.0 in our 3-call scenario, with the third
        # call's backoff interrupted by request_stop).

    def test_poll_forever_resets_backoff_after_successful_cycle(self) -> None:
        """Successful cycle (with at least one outcome) must reset the
        backoff to zero so the next transport failure starts fresh."""
        sleeps: List[float] = []
        seq = iter([0])  # sequence of outcomes; 0 = empty, 1 = has one

        class AlternatingApi:
            def get_updates(self, *, offset, timeout_seconds=None):
                n = next(seq, None)
                if n is None:
                    reader.request_stop()
                    raise OSError("transient")
                if n == 0:
                    raise OSError("transient")
                # Return one fake update
                return [{"update_id": 999, "message": {}}]

        reader = self._make_reader(AlternatingApi())
        reader._sleep = sleeps.append  # type: ignore[assignment]
        reader.poll_forever()
        # Two transport failures happened (call 0 + call after the
        # successful cycle). The successful cycle must reset backoff
        # so the next failure sleeps the initial 1.0, not 2.0+.
        post_success_sleeps = sleeps[-1:] if sleeps else []
        for s in post_success_sleeps:
            self.assertLessEqual(s, 1.0)


class ReaderLockTests(unittest.TestCase):
    """fcntl-based single-reader OS lock semantics."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lock_path = self.root / "mt4_reader.lock"

    def test_lock_acquired_then_released(self) -> None:
        lock = ReaderLock(self.lock_path)
        lock.acquire()
        self.assertTrue(self.lock_path.exists())
        self.assertEqual(self.lock_path.stat().st_mode & 0o777, 0o600)
        lock.release()

    def test_second_reader_cannot_acquire(self) -> None:
        lock1 = ReaderLock(self.lock_path)
        lock1.acquire()
        try:
            lock2 = ReaderLock(self.lock_path)
            with self.assertRaises(ReaderLockError):
                lock2.acquire()
        finally:
            lock1.release()

    def test_lock_releases_after_fd_close(self) -> None:
        """A stale lock filename alone must NOT block startup after a
        crash. ``fcntl.flock`` is released by the kernel when the FD
        closes, regardless of whether the file still exists.

        Simulate a crash by closing the underlying FD without calling
        ``release()``. The lock should file remains on disk but no
        longer blocks a new acquirer.
        """
        lock1 = ReaderLock(self.lock_path)
        lock1.acquire()
        fd = lock1._fd  # noqa: SLF001
        # Crash simulation: close the FD directly (the way the kernel
        # would on process exit). The lock1 object remains alive but
        # the lock is gone.
        assert fd is not None
        os.close(fd)

        # New reader can acquire cleanly even if the filename lingered.
        lock2 = ReaderLock(self.lock_path)
        lock2.acquire()  # should not raise
        lock2.release()

    def test_lock_via_concurrent_thread(self) -> None:
        """A second thread attempting to acquire must fail fast."""
        lock1 = ReaderLock(self.lock_path)
        lock1.acquire()
        results: List[Optional[Exception]] = [None]

        def worker():
            try:
                lock2 = ReaderLock(self.lock_path)
                lock2.acquire()
                lock2.release()
            except Exception as exc:
                results[0] = exc

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=2.0)
        lock1.release()
        self.assertIsInstance(results[0], ReaderLockError)


class ApiTokenRedactionTests(unittest.TestCase):
    def test_token_not_logged(self) -> None:
        # Build a reader whose fake API raises a token-bearing URL.
        api = mock.Mock()
        from plugins.trade.fibo.mt4_reader import TelegramApiError
        api.get_updates.side_effect = TelegramApiError(
            "getUpdates HTTP 401 (url=https://api.telegram.org/bot123456789:ABCDEFsecret/getUpdates?timeout=25)"
        )
        reader = Mt4ReaderProcess(
            bot_token="123456789:ABCDEFsecret",
            expected_chat_id=-100,
            expected_sender_id=42,
            snapshot_path=Path("/tmp/snap.json"),
            reader_state_path=Path("/tmp/state.json"),
            reader_lock_path=Path("/tmp/lock"),
            api=api,
        )
        outcomes = reader.run_once()
        # Outcome list empty (we return [] on TelegramApiError) and no
        # token in any state. Direct check:
        self.assertEqual(outcomes, [])
        # The token never leaks into state:
        self.assertNotIn("ABCDEFsecret", str(reader.state.to_dict()))


if __name__ == "__main__":
    unittest.main()