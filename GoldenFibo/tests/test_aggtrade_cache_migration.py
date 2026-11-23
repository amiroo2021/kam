from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from goldenfibo.marketdata.aggtrade_cache import AggTradeCache


class AggTradeCacheMigrationTests(unittest.TestCase):
    def test_legacy_database_migrates_and_records_completion_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "aggtrades.sqlite"
            conn = sqlite3.connect(path)
            conn.execute(
                """
                CREATE TABLE agg_trades (
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    agg_trade_id INTEGER NOT NULL,
                    price TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    first_trade_id INTEGER NOT NULL,
                    last_trade_id INTEGER NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    buyer_is_maker INTEGER NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    inserted_at_ms INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (market, symbol, agg_trade_id)
                )
                """
            )
            conn.execute(
                "INSERT INTO agg_trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("spot", "BTCUSDT", 1, "1", "1", 1, 1, 1, 0, "ws", 0),
            )
            conn.commit()
            conn.close()

            AggTradeCache(path)

            conn = sqlite3.connect(path)
            row = conn.execute("SELECT id_domain FROM agg_trades").fetchone()
            marker = conn.execute(
                "SELECT completed FROM cache_migrations WHERE migration_key=?",
                ("agg_trades_id_domain_v1",),
            ).fetchone()
            conn.close()
            self.assertEqual(("trade",), row)
            self.assertEqual((1,), marker)

    def test_completed_migration_marker_skips_compatibility_updates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "aggtrades.sqlite"
            cache = AggTradeCache(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO agg_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("spot", "BTCUSDT", 1, "1", "1", 1, 1, 1, 0, "aggtrade", "ws", 0),
            )
            conn.execute(
                """
                CREATE TRIGGER reject_agg_trade_updates
                BEFORE UPDATE ON agg_trades
                BEGIN
                    SELECT RAISE(ABORT, 'compatibility UPDATE must be skipped');
                END
                """
            )
            conn.commit()
            conn.close()

            # The marker was written by the first successful initialization.
            AggTradeCache(path)


if __name__ == "__main__":
    unittest.main()
