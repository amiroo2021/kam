"""Phase 2.7.1 — timestamp semantics regression tests.

The bug (now fixed): ``FiboRegistration.build()`` derived
``now = created_at or _utc_iso_now()`` and then used that same
``now`` as the fallback for ``updated_at``. When a status
transition passes an old ``created_at`` to preserve identity,
``updated_at`` silently inherited the old timestamp instead of
the current transition time.

The fix: capture ``current_time`` once per build and derive
``created_at`` and ``updated_at`` defaults independently from it.

These tests pin the contract:

  1. Initial registration (no created_at, no updated_at):
     created_at == updated_at == current_time.

  2. Transition (created_at=<old>, updated_at=None):
     created_at == <old>
     updated_at == current_time
     updated_at != <old>

  3. Explicit transition (created_at=<old>, updated_at=<explicit>):
     created_at == <old>
     updated_at == <explicit>

  4. mark_stopped preserves original created_at and refreshes
     updated_at to the transition time.

  5. reactivate preserves original created_at and refreshes
     updated_at to the transition time.

  6. Full lifecycle: registered -> stopped -> registered. All
     three rows share created_at; stopped.updated_at and
     reactivated.updated_at are both transition-time values.
     Historical rows remain intact.

  7. / 8. / 9. Existing concurrency / Stop / Restart tests pass
     unchanged (covered by the broader suite, not re-asserted
     here).

  10. JSONL row-count semantics unchanged: each transition
      appends exactly one new row.

We use mock time (freezegun-style helper) so the tests are
deterministic and don't depend on wall-clock sleeps.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from plugins.trade.fibo.store import (
    DuplicateRegistrationError, FiboRegistration, FiboRegistrationStore,
)


# ---------------------------------------------------------------------------
# Mockable clock
# ---------------------------------------------------------------------------


class _MockClock:
    """A deterministic, monotonic, manually-advanced clock for tests.

    Each call to ``utc_iso_now()`` returns the current value of
    ``_t`` formatted as an ISO-8601 UTC string. ``advance(seconds)``
    increments ``_t`` by ``seconds``.
    """

    def __init__(self, start_epoch: float = 1700000000.0) -> None:
        self._t = start_epoch
        self._calls = 0

    def advance(self, seconds: float) -> None:
        self._t += seconds

    def now_epoch(self) -> float:
        return self._t

    def utc_iso_now(self) -> str:
        self._calls += 1
        from datetime import datetime, timezone
        return (
            datetime.fromtimestamp(self._t, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )


_CLOCK = _MockClock()


def _install_mock_clock() -> None:
    """Patch the store module's ``_utc_iso_now`` to use the mock."""
    import plugins.trade.fibo.store as store_mod
    store_mod._utc_iso_now = _CLOCK.utc_iso_now  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _good_args(**overrides: Any) -> dict:
    """Return a dict of valid build() arguments.

    Default fixture includes ``exchange_instrument="ETH-USD.P"``
    so transition tests (mark_stopped / reactivate) can verify
    identity preservation without hitting the empty-string
    identity-mismatch path.
    """
    base = dict(
        exchange="ondoperps",
        account="BITGET",
        symbol="ETHUSD",
        exchange_instrument="ETH-USD.P",
        source_symbol="ETHUSD",
        variant="NORMALFib",
        side="BUY",
        starting_volume="0.001",
        source="obs-1",
        source_seq=1,
        source_cycle_id=42,
        source_cumulative_weight="2.0",
        source_percentage="0.01",
        source_snapshot_received_at="2026-08-27T00:00:00Z",
        desired_exchange_size=Decimal("0.002"),
    )
    base.update(overrides)
    return base


def _new_store() -> FiboRegistrationStore:
    """Build a fresh tempdir-backed store with proper permissions."""
    tmp = tempfile.TemporaryDirectory()
    store = FiboRegistrationStore(Path(tmp.name) / "r.jsonl")
    return store  # caller must clean up


# ---------------------------------------------------------------------------
# 1. Initial registration: created_at == updated_at == current_time
# ---------------------------------------------------------------------------


class InitialRegistrationTimestampTests(unittest.TestCase):
    """Initial registration (no transition) sets both timestamps
    to the same captured instant."""

    def setUp(self) -> None:
        _install_mock_clock()
        _CLOCK._t = 1700000000.0
        _CLOCK._calls = 0

    def test_initial_build_sets_both_timestamps_to_now(self) -> None:
        # Capture the expected current_time before build (so the
        # assertions don't increment _calls beyond the build's
        # single internal call).
        expected = _CLOCK.utc_iso_now()
        _CLOCK._calls = 0

        reg = FiboRegistration.build(**_good_args())

        # Exactly one call to _utc_iso_now (the captured instant).
        self.assertEqual(_CLOCK._calls, 1)
        # Both timestamps equal the captured instant.
        self.assertEqual(reg.created_at, expected)
        self.assertEqual(reg.updated_at, expected)
        self.assertEqual(reg.created_at, reg.updated_at)


# ---------------------------------------------------------------------------
# 2. Transition build (created_at=<old>, updated_at=None):
#    created_at == <old>; updated_at != <old>; updated_at == current_time.
# ---------------------------------------------------------------------------


class TransitionTimestampDefaultTests(unittest.TestCase):
    """Transition build with old created_at and no updated_at."""

    def setUp(self) -> None:
        _install_mock_clock()
        _CLOCK._t = 1700000000.0
        _CLOCK._calls = 0

    def test_old_created_at_preserved_updated_at_refreshed(self) -> None:
        old = "2025-01-01T00:00:00Z"
        # Capture the build-time current_time.
        current_time = _CLOCK.utc_iso_now()
        _CLOCK._calls = 0

        reg = FiboRegistration.build(
            **_good_args(created_at=old, updated_at=None)
        )
        # created_at preserved.
        self.assertEqual(reg.created_at, old)
        # updated_at refreshes to current_time (NOT old).
        self.assertEqual(reg.updated_at, current_time)
        # updated_at != old
        self.assertNotEqual(reg.updated_at, old)
        self.assertNotEqual(reg.created_at, reg.updated_at)
        # Exactly one _utc_iso_now call per build.
        self.assertEqual(_CLOCK._calls, 1)


# ---------------------------------------------------------------------------
# 3. Transition build with explicit updated_at:
#    both fields preserved as supplied.
# ---------------------------------------------------------------------------


class ExplicitUpdatedAtTests(unittest.TestCase):
    """Transition build with old created_at AND explicit updated_at."""

    def setUp(self) -> None:
        _install_mock_clock()
        _CLOCK._t = 1700000000.0
        _CLOCK._calls = 0

    def test_explicit_updated_at_respected(self) -> None:
        old = "2025-01-01T00:00:00Z"
        explicit_updated = "2025-06-15T12:30:45Z"

        # Capture the build-time current_time so the assertion
        # doesn't increment _CLOCK._calls after the build.
        build_time = _CLOCK.utc_iso_now()
        _CLOCK._calls = 0

        reg = FiboRegistration.build(
            **_good_args(created_at=old, updated_at=explicit_updated)
        )
        self.assertEqual(reg.created_at, old)
        self.assertEqual(reg.updated_at, explicit_updated)
        # Both timestamps came from the caller's explicit values,
        # not from build_time — they are different strings.
        self.assertNotEqual(reg.created_at, build_time)
        self.assertNotEqual(reg.updated_at, build_time)


# ---------------------------------------------------------------------------
# 4. mark_stopped preserves original created_at, refreshes updated_at.
# ---------------------------------------------------------------------------


class MarkStoppedTimestampTests(unittest.TestCase):
    """mark_stopped preserves the original created_at and refreshes
    updated_at to the transition time."""

    def setUp(self) -> None:
        _install_mock_clock()
        _CLOCK._t = 1700000000.0
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "r.jsonl"
        os.chmod(self.tmp.name, 0o700)
        self.store = FiboRegistrationStore(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_mark_stopped_preserves_created_refreshes_updated(self) -> None:
        original_created = "2025-01-01T00:00:00Z"

        # Phase 1: build the registered row with an explicit
        # old created_at so we can verify preservation.
        _CLOCK._calls = 0
        registered_time = _CLOCK.utc_iso_now()
        reg = FiboRegistration.build(
            **_good_args(created_at=original_created, updated_at=None)
        )
        self.store.append(reg)
        # Capture the post-append time and advance the clock.
        after_append = _CLOCK.utc_iso_now()
        _CLOCK._calls = 0

        # Advance the clock by 1 hour.
        _CLOCK.advance(3600)

        # Phase 2: stop the row.
        stopped = self.store.mark_stopped(reg.registration_key)
        # created_at preserved (historical).
        self.assertEqual(stopped.created_at, original_created)
        # updated_at refreshed to the post-advance current_time.
        self.assertNotEqual(stopped.updated_at, original_created)
        self.assertNotEqual(stopped.updated_at, registered_time)
        # updated_at is strictly > registered_time because we
        # advanced the clock.
        self.assertGreater(stopped.updated_at, registered_time)


# ---------------------------------------------------------------------------
# 5. reactivate preserves original created_at, refreshes updated_at.
# ---------------------------------------------------------------------------


class ReactivateTimestampTests(unittest.TestCase):
    """reactivate preserves the original created_at and refreshes
    updated_at to the reactivation time."""

    def setUp(self) -> None:
        _install_mock_clock()
        _CLOCK._t = 1700000000.0
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "r.jsonl"
        os.chmod(self.tmp.name, 0o700)
        self.store = FiboRegistrationStore(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reactivate_preserves_created_refreshes_updated(self) -> None:
        original_created = "2025-01-01T00:00:00Z"

        # Initial registered row.
        reg = FiboRegistration.build(
            **_good_args(created_at=original_created, updated_at=None)
        )
        self.store.append(reg)

        # Stop the row.
        _CLOCK.advance(3600)  # +1 hour
        self.store.mark_stopped(reg.registration_key)

        # Now advance another hour and reactivate.
        _CLOCK.advance(3600)  # +1 hour more
        stopped_time_before = _CLOCK.utc_iso_now()
        _CLOCK._calls = 0

        reactivated = self.store.reactivate(
            reg.registration_key,
            source_symbol="ETHUSD",
            exchange_instrument="ETH-USD.P",
            starting_volume=Decimal("0.001"),
            desired_exchange_size=Decimal("0.002"),
            source="obs-2", source_seq=2,
            source_cycle_id=100, source_cumulative_weight=Decimal("2.0"),
            source_percentage=Decimal("0.01"),
            source_snapshot_received_at="2026-08-27T05:00:00Z",
        )
        # created_at preserved.
        self.assertEqual(reactivated.created_at, original_created)
        # updated_at refreshed to the reactivation time.
        self.assertNotEqual(reactivated.updated_at, original_created)
        # updated_at is not inherited from stopped.created_at.
        self.assertNotEqual(reactivated.updated_at, reactivated.created_at)
        # updated_at is >= stopped_time_before (advanced clock).
        self.assertGreaterEqual(
            reactivated.updated_at, stopped_time_before
        )


# ---------------------------------------------------------------------------
# 6. Full lifecycle: registered -> stopped -> reactivated.
#    All three rows share created_at; both transitions have
#    transition-time updated_at. Historical rows remain intact.
# ---------------------------------------------------------------------------


class FullLifecycleTimestampTests(unittest.TestCase):
    """End-to-end lifecycle across two transitions."""

    def setUp(self) -> None:
        _install_mock_clock()
        _CLOCK._t = 1700000000.0
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "r.jsonl"
        os.chmod(self.tmp.name, 0o700)
        self.store = FiboRegistrationStore(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_full_lifecycle_three_rows_share_created_at(self) -> None:
        # Phase 1: initial registered row.
        registered_time = _CLOCK.utc_iso_now()
        reg = FiboRegistration.build(
            **_good_args(
                created_at=None, updated_at=None,
            )
        )
        self.store.append(reg)
        # After initial build, both timestamps == registered_time.
        self.assertEqual(reg.created_at, registered_time)
        self.assertEqual(reg.updated_at, registered_time)

        # Advance the clock by 1 hour.
        _CLOCK.advance(3600)

        # Phase 2: stop the row.
        stopped_time = _CLOCK.utc_iso_now()
        stopped = self.store.mark_stopped(reg.registration_key)
        # created_at preserved (== registered_time).
        self.assertEqual(stopped.created_at, registered_time)
        # updated_at refreshed to stopped_time.
        self.assertEqual(stopped.updated_at, stopped_time)
        # updated_at > registered_time (clock advanced).
        self.assertGreater(stopped.updated_at, registered_time)

        # Advance the clock by another hour.
        _CLOCK.advance(3600)

        # Phase 3: reactivate the row.
        react_time = _CLOCK.utc_iso_now()
        reactivated = self.store.reactivate(
            reg.registration_key,
            source_symbol="ETHUSD",
            exchange_instrument="ETH-USD.P",
            starting_volume=Decimal("0.001"),
            desired_exchange_size=Decimal("0.002"),
            source="obs-2", source_seq=2,
            source_cycle_id=100, source_cumulative_weight=Decimal("2.0"),
            source_percentage=Decimal("0.01"),
            source_snapshot_received_at="2026-08-27T05:00:00Z",
        )
        # created_at preserved (== registered_time == stopped.created_at).
        self.assertEqual(reactivated.created_at, registered_time)
        # updated_at refreshed to react_time.
        self.assertEqual(reactivated.updated_at, react_time)
        # All three timestamps chain: registered < stopped < react.
        self.assertLess(registered_time, stopped_time)
        self.assertLess(stopped_time, react_time)
        # All three rows share created_at.
        self.assertEqual(reg.created_at, stopped.created_at)
        self.assertEqual(reg.created_at, reactivated.created_at)
        # updated_at is unique per row.
        ts = {
            "registered": reg.updated_at,
            "stopped": stopped.updated_at,
            "reactivated": reactivated.updated_at,
        }
        self.assertEqual(len(set(ts.values())), 3)
        # Effective latest row is registered.
        all_ = self.store.load_all()
        self.assertEqual(len(all_), 1)
        self.assertEqual(all_[0].status, "registered")
        self.assertTrue(all_[0].is_active)
        self.assertFalse(all_[0].is_stopped)
        # Historical rows remain in the file.
        raw = self.path.read_text().splitlines()
        raw = [r for r in raw if r.strip()]
        self.assertEqual(len(raw), 3)
        statuses = [json.loads(r)["status"] for r in raw]
        self.assertEqual(
            statuses, ["registered", "stopped", "registered"]
        )


# ---------------------------------------------------------------------------
# 10. JSONL row-count semantics: each transition appends exactly one row.
# ---------------------------------------------------------------------------


class RowCountSemanticsTests(unittest.TestCase):
    """JSONL row count: +1 per append / transition; latest-row-wins
    produces one effective registration per key."""

    def setUp(self) -> None:
        _install_mock_clock()
        _CLOCK._t = 1700000000.0
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "r.jsonl"
        os.chmod(self.tmp.name, 0o700)
        self.store = FiboRegistrationStore(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_each_transition_appends_exactly_one_row(self) -> None:
        reg = FiboRegistration.build(**_good_args())
        self.store.append(reg)
        rows_0 = self._raw_count()
        self.assertEqual(rows_0, 1)

        self.store.mark_stopped(reg.registration_key)
        rows_1 = self._raw_count()
        self.assertEqual(rows_1, 2)

        self.store.reactivate(
            reg.registration_key,
            source_symbol="ETHUSD",
            exchange_instrument="ETH-USD.P",
            starting_volume=Decimal("0.001"),
            desired_exchange_size=Decimal("0.002"),
            source="obs-2", source_seq=2,
            source_cycle_id=100, source_cumulative_weight=Decimal("2.0"),
            source_percentage=Decimal("0.01"),
            source_snapshot_received_at="2026-08-27T05:00:00Z",
        )
        rows_2 = self._raw_count()
        self.assertEqual(rows_2, 3)

        # Latest-row-wins: exactly one effective registration.
        self.assertEqual(len(self.store.load_all()), 1)

    def _raw_count(self) -> int:
        if not self.path.exists():
            return 0
        return len([r for r in self.path.read_text().splitlines() if r.strip()])


if __name__ == "__main__":
    unittest.main()
