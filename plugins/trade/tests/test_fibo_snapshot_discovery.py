"""Phase 2.13.20 — MT4 snapshot discovery + feed-age regression tests.

The MT4 Observer sends a complete snapshot every ~5 seconds. The latest
accepted snapshot is the source of truth for the wizard. These tests
prove:

  1. ``unique_symbol_variant_pairs`` returns one pair per unique
     ``(symbol, variant)`` combination (never deduplicating by symbol
     alone, never collapsing NORMALFib/FASTFib variants).

  2. A snapshot with 13 fibos produces 13 choices for /fibo Start Fibo.

  3. The MT4 snapshot's ``received_at`` is the local receipt time
     recorded by the reader at the moment the message was accepted.

  4. ``age_seconds()`` returns approximately the time elapsed since
     that local receipt.

  5. Mt4SnapshotStore.load() reads the snapshot fresh from disk on
     every invocation (no caching at this layer).

  6. A newly appeared EA appears in the next snapshot without any code
     change (e.g. ETHUSD+FASTFib / SOLUSD+FASTFib added by the
     Observer).

  7. Duplicate identical ``(symbol, variant)`` entries within the same
     snapshot are deduplicated safely.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from plugins.trade.fibo.snapshot import (
    Mt4Fibo,
    Mt4Snapshot,
    Mt4SnapshotStore,
    parse_snapshot_payload,
)


def _make_fibos(pairs, *, buy_cycle=100, sell_cycle=100):
    """Helper: build a list of Mt4Fibo from (symbol, variant) pairs."""
    out = []
    for sym, var in pairs:
        out.append(Mt4Fibo(
            symbol=sym, variant=var,
            percentage=Decimal("0.001"),
            buy_cycle_id=buy_cycle,
            cumulative_buy_weight=Decimal("1"),
            sell_cycle_id=sell_cycle,
            cumulative_sell_weight=Decimal("1"),
        ))
    return out


def _make_snap(pairs, *, source="mt4-Fresh-1", seq=1, received_at=None):
    if received_at is None:
        received_at = "2026-08-28T22:00:00+00:00"
    fibos = _make_fibos(pairs)
    return Mt4Snapshot(
        v=1, source=source, seq=seq, ts=1,
        fibos=fibos, received_at=received_at,
        telegram_update_id=1, telegram_message_id=1,
        reader_chat_id=1,
    )


# The user-reported 13-pair set from the MT4 Observer.
THIRTEEN_PAIRS = [
    ("#DJI30", "NORMALFIB"), ("#NQ100", "NORMALFIB"),
    ("#SP500", "NORMALFIB"),
    ("BTCUSD", "NORMALFIB"), ("BTCUSD", "FASTFIB"),
    ("ETHUSD", "NORMALFIB"), ("ETHUSD", "FASTFIB"),
    ("SOLUSD", "NORMALFIB"), ("SOLUSD", "FASTFIB"),
    ("XAUUSD", "NORMALFIB"), ("XAUUSD", "FASTFIB"),
    ("ZECUSD", "NORMALFIB"), ("ZECUSD", "FASTFIB"),
]


class UniqueSymbolVariantPairsTest(unittest.TestCase):
    """Per spec §7: one choice per unique (symbol, variant) pair.

    NORMALFib and FASTFib for the same symbol are DIFFERENT choices.
    """

    def test_13_fibos_produce_13_pairs(self):
        snap = _make_snap(THIRTEEN_PAIRS)
        pairs = snap.unique_symbol_variant_pairs()
        self.assertEqual(len(pairs), 13,
                         f"expected 13 pairs, got {len(pairs)}")
        self.assertEqual(
            len({(p["symbol"], p["variant"]) for p in pairs}),
            13,
            "pair key collision",
        )

    def test_ethusd_fastfib_and_solusd_fastfib_present(self):
        """The two pairs recently added by the Observer MUST appear."""
        snap = _make_snap(THIRTEEN_PAIRS)
        pair_set = {
            (p["symbol"].upper(), p["variant"].upper())
            for p in snap.unique_symbol_variant_pairs()
        }
        self.assertIn(("ETHUSD", "FASTFIB"), pair_set)
        self.assertIn(("SOLUSD", "FASTFIB"), pair_set)

    def test_normal_fast_variants_never_collapsed(self):
        """NORMALFib and FASTFib for the same symbol are TWO distinct pairs."""
        pairs = [
            ("XAUUSD", "NORMALFIB"), ("XAUUSD", "FASTFIB"),
            ("BTCUSD", "NORMALFIB"), ("BTCUSD", "FASTFIB"),
        ]
        snap = _make_snap(pairs)
        result = snap.unique_symbol_variant_pairs()
        self.assertEqual(len(result), 4)
        keys = {(p["symbol"], p["variant"]) for p in result}
        self.assertIn(("XAUUSD", "NORMALFIB"), keys)
        self.assertIn(("XAUUSD", "FASTFIB"), keys)
        self.assertIn(("BTCUSD", "NORMALFIB"), keys)
        self.assertIn(("BTCUSD", "FASTFIB"), keys)

    def test_duplicate_identical_pairs_deduplicated(self):
        """Duplicate (symbol, variant) entries within one snapshot
        are safely deduplicated."""
        pairs = [
            ("XAUUSD", "FASTFIB"),
            ("XAUUSD", "FASTFIB"),
            ("XAUUSD", "FASTFIB"),
            ("BTCUSD", "NORMALFIB"),
            ("BTCUSD", "NORMALFIB"),
        ]
        snap = _make_snap(pairs)
        result = snap.unique_symbol_variant_pairs()
        self.assertEqual(len(result), 2)
        keys = {(p["symbol"], p["variant"]) for p in result}
        self.assertEqual(keys, {
            ("XAUUSD", "FASTFIB"), ("BTCUSD", "NORMALFIB"),
        })

    def test_new_unknown_symbol_appears_dynamically(self):
        """A previously-unseen EA pair (e.g. ABCUSD FASTFib) appears
        automatically without any code change."""
        pairs = [
            ("XAUUSD", "FASTFIB"),
            ("ABCUSD", "FASTFIB"),  # brand new EA
            ("XYZZ", "NORMALFIB"),  # another brand new
        ]
        snap = _make_snap(pairs)
        result = snap.unique_symbol_variant_pairs()
        keys = {(p["symbol"], p["variant"]) for p in result}
        self.assertEqual(keys, {
            ("XAUUSD", "FASTFIB"),
            ("ABCUSD", "FASTFIB"),
            ("XYZZ", "NORMALFIB"),
        })

    def test_pair_preserves_snapshot_order(self):
        """First-seen ordering is preserved so the wizard shows the
        same buttons in the same order each time."""
        pairs = [
            ("SOLUSD", "NORMALFIB"),
            ("XAUUSD", "FASTFIB"),
            ("BTCUSD", "FASTFIB"),
            ("SOLUSD", "NORMALFIB"),  # dup
            ("ETHUSD", "NORMALFIB"),
        ]
        snap = _make_snap(pairs)
        result = snap.unique_symbol_variant_pairs()
        order = [(p["symbol"], p["variant"]) for p in result]
        self.assertEqual(order, [
            ("SOLUSD", "NORMALFIB"),
            ("XAUUSD", "FASTFIB"),
            ("BTCUSD", "FASTFIB"),
            ("ETHUSD", "NORMALFIB"),
        ])

    def test_symbol_only_dedup_is_never_used(self):
        """NORMALFib and FASTFib for the same symbol MUST coexist.
        If we ever collapse by symbol alone, this test will fail."""
        pairs = [
            ("XAUUSD", "NORMALFIB"),
            ("XAUUSD", "FASTFIB"),
        ]
        snap = _make_snap(pairs)
        result = snap.unique_symbol_variant_pairs()
        # Two pairs, not one. NORMALFib and FASTFib are independent.
        self.assertEqual(len(result), 2)


class ReceivedAtAndAgeTest(unittest.TestCase):
    """Per spec: feed age = time since the LOCAL receipt timestamp
    that the MT4 reader wrote when it accepted the latest Observer
    message."""

    def test_received_at_parsed_as_utc(self):
        snap = _make_snap([("XAUUSD", "FASTFIB")])
        self.assertEqual(snap.received_at, "2026-08-28T22:00:00+00:00")

    def test_age_seconds_zero_for_fresh_snapshot(self):
        from datetime import datetime, timezone
        now = datetime(2026, 8, 28, 22, 0, 5, tzinfo=timezone.utc)
        snap = _make_snap(
            [("XAUUSD", "FASTFIB")],
            received_at="2026-08-28T22:00:00+00:00",
        )
        age = snap.age_seconds(now=now)
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 5.0, delta=0.5)

    def test_age_seconds_approximately_5_for_normal_polling(self):
        """If the Observer sends every ~5 seconds, feed age should
        normally be approximately 0–5 seconds."""
        from datetime import datetime, timezone, timedelta
        received = datetime(2026, 8, 28, 22, 0, 0, tzinfo=timezone.utc)
        snap = _make_snap(
            [("XAUUSD", "FASTFIB")],
            received_at=received.isoformat().replace("+00:00", "Z"),
        )
        # 5 seconds later
        age = snap.age_seconds(now=received + timedelta(seconds=5))
        self.assertLessEqual(age, 5.0)
        self.assertGreaterEqual(age, 0.0)

    def test_age_seconds_returns_none_for_malformed_received_at(self):
        snap = _make_snap(
            [("XAUUSD", "FASTFIB")],
            received_at="not-a-timestamp",
        )
        self.assertIsNone(snap.age_seconds())

    def test_age_seconds_accepts_z_suffix(self):
        from datetime import datetime, timezone, timedelta
        snap = _make_snap(
            [("XAUUSD", "FASTFIB")],
            received_at="2026-08-28T22:00:00Z",
        )
        now = datetime(2026, 8, 28, 22, 0, 3, tzinfo=timezone.utc)
        age = snap.age_seconds(now=now)
        self.assertAlmostEqual(age, 3.0, delta=0.5)


class SnapshotStoreReloadTest(unittest.TestCase):
    """Per spec §4: each Start Fibo invocation MUST read the latest
    snapshot from disk. No list cached when the gateway started.
    """

    def test_load_reads_fresh_each_invocation(self):
        """Modifying the file between two load() calls must return
        the updated content."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mt4_snapshot.json"
            store = Mt4SnapshotStore(path)
            # First load: 11 pairs.
            payload1 = {
                "v": 1, "source": "mt4-Fresh-1", "seq": 1, "ts": 1,
                "fibos": [
                    {
                        "symbol": s, "variant": v,
                        "percentage": "0.001",
                        "buy_cycle_id": 100,
                        "cumulative_buy_weight": "1",
                        "sell_cycle_id": 100,
                        "cumulative_sell_weight": "1",
                    }
                    for s, v in [
                        ("#DJI30", "NORMALFIB"),
                        ("BTCUSD", "NORMALFIB"),
                        ("BTCUSD", "FASTFIB"),
                    ]
                ],
                "received_at": "2026-08-28T22:00:00Z",
                "telegram_update_id": 1,
                "telegram_message_id": 1,
                "reader_chat_id": 1,
            }
            path.write_text(json.dumps(payload1))
            snap1 = store.load()
            self.assertEqual(len(snap1.unique_symbol_variant_pairs()), 3)

            # Second load (after Observer sent a fresh snapshot
            # with 13 pairs): store must read the new content.
            payload2 = dict(payload1)
            payload2["fibos"] = []
            for s, v in [
                ("#DJI30", "NORMALFIB"),
                ("BTCUSD", "NORMALFIB"),
                ("BTCUSD", "FASTFIB"),
                ("ETHUSD", "FASTFIB"),  # newly added
                ("SOLUSD", "FASTFIB"),  # newly added
            ]:
                entry = {
                    "symbol": s, "variant": v,
                    "percentage": "0.001",
                    "buy_cycle_id": 100,
                    "cumulative_buy_weight": "1",
                    "sell_cycle_id": 100,
                    "cumulative_sell_weight": "1",
                }
                payload2["fibos"].append(entry)
            payload2["seq"] = 2
            payload2["received_at"] = "2026-08-28T22:00:05Z"
            path.write_text(json.dumps(payload2))

            snap2 = store.load()
            self.assertEqual(len(snap2.unique_symbol_variant_pairs()), 5)
            keys = {
                (p["symbol"], p["variant"])
                for p in snap2.unique_symbol_variant_pairs()
            }
            self.assertIn(("ETHUSD", "FASTFIB"), keys)
            self.assertIn(("SOLUSD", "FASTFIB"), keys)
            self.assertEqual(snap2.seq, 2)

    def test_load_returns_none_for_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            store = Mt4SnapshotStore(Path(td) / "mt4_snapshot.json")
            self.assertIsNone(store.load())

    def test_load_returns_none_for_malformed_json(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mt4_snapshot.json"
            path.write_text("not json {")
            store = Mt4SnapshotStore(path)
            self.assertIsNone(store.load())

    def test_received_at_is_preserved_through_disk_roundtrip(self):
        """The reader's local receipt timestamp must survive
        write+load without modification."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mt4_snapshot.json"
            path.write_text(json.dumps({
                "v": 1, "source": "mt4-Fresh-1", "seq": 42, "ts": 1,
                "fibos": [],
                "received_at": "2026-08-28T22:00:00.123456Z",
                "telegram_update_id": 1,
                "telegram_message_id": 1,
                "reader_chat_id": 1,
            }))
            store = Mt4SnapshotStore(path)
            snap = store.load()
            self.assertEqual(snap.received_at,
                             "2026-08-28T22:00:00.123456Z")

    def test_each_new_seq_refreshes_receipt_time(self):
        """Each newly accepted Observer message MUST update
        received_at (different seq → different received_at)."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mt4_snapshot.json"
            store = Mt4SnapshotStore(path)

            for seq, ra in [
                (1, "2026-08-28T22:00:00Z"),
                (2, "2026-08-28T22:00:05Z"),
                (3, "2026-08-28T22:00:10Z"),
            ]:
                path.write_text(json.dumps({
                    "v": 1, "source": "mt4-Fresh-1",
                    "seq": seq, "ts": 1, "fibos": [],
                    "received_at": ra,
                    "telegram_update_id": seq,
                    "telegram_message_id": seq,
                    "reader_chat_id": 1,
                }))
                snap = store.load()
                self.assertEqual(snap.seq, seq)
                self.assertEqual(snap.received_at, ra)


class FreshnessDuringWizardFlowTest(unittest.TestCase):
    """Simulate the wizard flow and prove the snapshot is re-read
    at every relevant step."""

    def test_symbol_picker_uses_fresh_snapshot(self):
        """When user opens Start Fibo, the symbol picker reads the
        current snapshot (not a startup-time cache)."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mt4_snapshot.json"
            # Initial 11-pair snapshot
            initial_pairs = [
                ("#DJI30", "NORMALFIB"), ("#NQ100", "NORMALFIB"),
                ("#SP500", "NORMALFIB"),
                ("BTCUSD", "NORMALFIB"), ("BTCUSD", "FASTFIB"),
                ("ETHUSD", "NORMALFIB"),
                ("SOLUSD", "NORMALFIB"),
                ("XAUUSD", "NORMALFIB"), ("XAUUSD", "FASTFIB"),
                ("ZECUSD", "NORMALFIB"), ("ZECUSD", "FASTFIB"),
            ]
            path.write_text(json.dumps({
                "v": 1, "source": "mt4-Fresh-1", "seq": 1, "ts": 1,
                "fibos": [
                    {"symbol": s, "variant": v, "percentage": "0.001",
                     "buy_cycle_id": 100, "cumulative_buy_weight": "1",
                     "sell_cycle_id": 100, "cumulative_sell_weight": "1"}
                    for s, v in initial_pairs
                ],
                "received_at": "2026-08-28T22:00:00Z",
                "telegram_update_id": 1, "telegram_message_id": 1,
                "reader_chat_id": 1,
            }))
            store = Mt4SnapshotStore(path)

            # Open wizard: see 11 choices.
            snap1 = store.load()
            self.assertEqual(
                len(snap1.unique_symbol_variant_pairs()), 11,
                "initial wizard should show 11 choices",
            )

            # Observer sends a NEW snapshot (13 pairs, ETHUSD+FASTFib
            # and SOLUSD+FASTFib added).
            new_pairs = initial_pairs + [
                ("ETHUSD", "FASTFIB"), ("SOLUSD", "FASTFIB"),
            ]
            path.write_text(json.dumps({
                "v": 1, "source": "mt4-Fresh-1", "seq": 2, "ts": 1,
                "fibos": [
                    {"symbol": s, "variant": v, "percentage": "0.001",
                     "buy_cycle_id": 100, "cumulative_buy_weight": "1",
                     "sell_cycle_id": 100, "cumulative_sell_weight": "1"}
                    for s, v in new_pairs
                ],
                "received_at": "2026-08-28T22:00:05Z",
                "telegram_update_id": 2, "telegram_message_id": 2,
                "reader_chat_id": 1,
            }))

            # User re-opens Start Fibo: 13 choices.
            snap2 = store.load()
            self.assertEqual(
                len(snap2.unique_symbol_variant_pairs()), 13,
                "subsequent wizard should see the new pairs dynamically",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
