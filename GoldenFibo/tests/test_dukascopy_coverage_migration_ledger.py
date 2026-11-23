"""Regression test for NIT E: the coverage backfill must run only once.

Both independent reviewers flagged that the per-side backfill UPDATE in
dukascopy_cache._init_db runs unconditionally on EVERY initialization,
with no migration ledger. It is idempotent today, but a future rename of
a status value would silently re-run it against every existing row.

The sibling module aggtrade_cache.py already solves this with a
``cache_migrations`` ledger. This test pins the same contract for the
Dukascopy coverage table.

The proof technique is the same one the aggtrade migration test uses: a
SQLite TRIGGER that ABORTs any UPDATE of the backfilled columns. Once the
migration marker is recorded, a second initialization must not attempt the
UPDATE at all — the trigger would fire and raise.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from goldenfibo.marketdata.dukascopy_cache import DukascopyKlineCache


class DukascopyCoverageMigrationLedgerTests(unittest.TestCase):
    MIGRATION_KEY = "coverage_days_side_status_v1"

    def _legacy_db(self, path: Path) -> None:
        """Create a pre-migration database with a legacy coverage row."""
        with sqlite3.connect(path) as db:
            db.executescript(
                """
                CREATE TABLE candles (
                    instrument TEXT NOT NULL,
                    open_time_ms INTEGER NOT NULL,
                    bid_open REAL, bid_high REAL, bid_low REAL, bid_close REAL,
                    ask_open REAL, ask_high REAL, ask_low REAL, ask_close REAL,
                    volume REAL,
                    volume_units TEXT NOT NULL DEFAULT 'provider',
                    source_day TEXT NOT NULL,
                    PRIMARY KEY (instrument, open_time_ms)
                );
                CREATE TABLE coverage_days (
                    instrument TEXT NOT NULL,
                    day TEXT NOT NULL,
                    status TEXT NOT NULL,
                    bid_complete INTEGER NOT NULL DEFAULT 0,
                    ask_complete INTEGER NOT NULL DEFAULT 0,
                    artifact TEXT,
                    verified_at_ms INTEGER NOT NULL,
                    note TEXT,
                    PRIMARY KEY (instrument, day)
                );
                """
            )
            db.execute(
                "INSERT INTO coverage_days"
                "(instrument,day,status,bid_complete,ask_complete,verified_at_ms) "
                "VALUES(?,?,?,?,?,?)",
                ("eurusd", "2024-01-03", "verified_data", 1, 0, 1),
            )
            db.commit()

    def test_legacy_database_backfills_once_and_records_completion_marker(self) -> None:
        """The backfill runs once and records a completion marker."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy.sqlite"
            self._legacy_db(path)

            DukascopyKlineCache(path)

            with sqlite3.connect(path) as db:
                bid_status = db.execute(
                    "SELECT bid_status FROM coverage_days WHERE instrument='eurusd'"
                ).fetchone()[0]
                ask_status = db.execute(
                    "SELECT ask_status FROM coverage_days WHERE instrument='eurusd'"
                ).fetchone()[0]
                marker = db.execute(
                    "SELECT completed FROM cache_migrations WHERE migration_key=?",
                    (self.MIGRATION_KEY,),
                ).fetchone()

            # The legacy row keeps exactly the verification it already had.
            self.assertEqual("verified_data", bid_status)
            # The side it never claimed is still unverified.
            self.assertEqual("", ask_status)
            self.assertEqual((1,), marker)

    def test_completed_marker_skips_the_backfill_on_later_initializations(self) -> None:
        """A second initialization must not re-run the backfill UPDATE."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy.sqlite"
            self._legacy_db(path)

            # First initialization performs the migration.
            DukascopyKlineCache(path)

            # A row that the backfill would still match, written directly so
            # it bypasses the cache API. Only a re-run of the UPDATE can
            # touch it.
            with sqlite3.connect(path) as db:
                db.execute(
                    "INSERT INTO coverage_days"
                    "(instrument,day,status,bid_complete,ask_complete,"
                    "bid_status,ask_status,verified_at_ms) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    ("eurusd", "2024-01-04", "verified_data", 1, 0, "", "", 1),
                )
                db.commit()

            # Any further attempt to backfill must abort loudly. The trigger
            # fires on EVERY UPDATE of the table, not only on a value
            # change, so it detects a re-run even when the backfilled values
            # are already correct.
            with sqlite3.connect(path) as db:
                db.executescript(
                    """
                    CREATE TRIGGER reject_coverage_backfill
                    BEFORE UPDATE ON coverage_days
                    BEGIN
                        SELECT RAISE(ABORT, 'coverage backfill must be skipped');
                    END;
                    """
                )
                db.commit()

            # This must not raise: the migration is already recorded.
            DukascopyKlineCache(path)

            with sqlite3.connect(path) as db:
                rows = dict(
                    db.execute(
                        "SELECT day,bid_status FROM coverage_days WHERE instrument='eurusd'"
                    ).fetchall()
                )
            # The migrated row keeps its verification, and the row the
            # backfill would have matched is left untouched.
            self.assertEqual("verified_data", rows["2024-01-03"])
            self.assertEqual("", rows["2024-01-04"])

    def test_migration_is_idempotent_across_repeated_initializations(self) -> None:
        """Repeated initialization preserves every row's verification."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy.sqlite"
            self._legacy_db(path)

            for _ in range(5):
                DukascopyKlineCache(path)

            with sqlite3.connect(path) as db:
                rows = list(
                    db.execute(
                        "SELECT bid_status,ask_status,bid_complete,ask_complete "
                        "FROM coverage_days WHERE instrument='eurusd'"
                    )
                )
                markers = list(
                    db.execute(
                        "SELECT migration_key,completed FROM cache_migrations"
                    )
                )
            self.assertEqual([("verified_data", "", 1, 0)], rows)
            self.assertEqual(1, len(markers))
            self.assertEqual(self.MIGRATION_KEY, markers[0][0])
            self.assertEqual(1, markers[0][1])


if __name__ == "__main__":
    unittest.main()
