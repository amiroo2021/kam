"""Tests for the local Fibo registration store."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path

from plugins.trade.fibo.store import (
    DuplicateRegistrationError,
    FiboRegistration,
    FiboRegistrationStore,
    StoreBusy,
)


def _make_reg(**overrides) -> FiboRegistration:
    defaults = dict(
        exchange="ondoperps",
        account="bitget",
        symbol="BTCUSD",
        variant="FASTFib",
        side="buy",                 # input gets normalized to BUY
        starting_volume="0.10",
        source="obs-1",
        source_seq=42,
        source_cycle_id=42,
        source_cumulative_weight="2.5",
        source_percentage="0.001",
        source_snapshot_received_at="2026-08-25T03:14:02Z",
        desired_exchange_size="0.25",
    )
    defaults.update(overrides)
    return FiboRegistration.build(**defaults)


class BuildTests(unittest.TestCase):
    def test_identity_normalization(self) -> None:
        r = _make_reg(
            exchange="OndoPerps",
            account=" bitget ",
            symbol="btcusd",
            variant="fastfib",
            side="BUY",
        )
        self.assertEqual(r.exchange, "ondoperps")
        self.assertEqual(r.account, "BITGET")
        self.assertEqual(r.symbol, "BTCUSD")
        self.assertEqual(r.variant, "FASTFIB")
        self.assertEqual(r.side, "BUY")
        self.assertEqual(
            r.registration_key, "ondoperps/BITGET/BTCUSD/FASTFIB/BUY"
        )

    def test_decimal_preserves_trailing_zeros(self) -> None:
        r = _make_reg(starting_volume="0.10")
        self.assertEqual(r.starting_volume, Decimal("0.10"))
        self.assertEqual(r.to_dict()["starting_volume"], "0.10")

    def test_invalid_side_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _make_reg(side="long")

    def test_zero_volume_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _make_reg(starting_volume="0")

    def test_negative_volume_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _make_reg(starting_volume="-1")

    def test_nan_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _make_reg(starting_volume="NaN")

    def test_infinity_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _make_reg(starting_volume="Infinity")


class AppendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "registrations.jsonl"
        self.store = FiboRegistrationStore(self.path)

    def test_append_then_load_roundtrip(self) -> None:
        r = _make_reg()
        self.store.append(r)
        all_ = self.store.load_all()
        self.assertEqual(len(all_), 1)
        self.assertEqual(all_[0].registration_key, r.registration_key)
        self.assertEqual(all_[0].starting_volume, Decimal("0.10"))

    def test_duplicate_key_rejected_with_different_volume(self) -> None:
        """Spec §11: a same-key re-Create is REJECTED, never silently
        mutated. The new starting_volume is irrelevant — the second
        append raises DuplicateRegistrationError.
        """
        r1 = _make_reg(starting_volume="0.10", source_seq=10)
        r2 = _make_reg(starting_volume="0.20", source_seq=11)
        self.store.append(r1)
        with self.assertRaises(DuplicateRegistrationError):
            self.store.append(r2)
        # The file still holds only the first record.
        loaded = self.store.load_all()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].starting_volume, Decimal("0.10"))

    def test_latest_record_per_key_wins_after_uniqueness(self) -> None:
        """Distinct registration_keys yield distinct records; latest
        per key wins on identical keys that bypass dedup. Here we
        prove that two records with DIFFERENT identities both land."""
        r1 = _make_reg(account="ALICE", source_seq=10)
        r2 = _make_reg(account="BOB", source_seq=11)
        self.store.append(r1)
        self.store.append(r2)
        loaded = self.store.load_all()
        self.assertEqual(len(loaded), 2)
        keys = sorted([r.registration_key for r in loaded])
        self.assertEqual(keys, sorted([r1.registration_key, r2.registration_key]))

    def test_malformed_final_line_ignored_safely(self) -> None:
        r = _make_reg()
        self.store.append(r)
        with self.path.open("ab") as f:
            f.write(b'{"registration_key": "broken/line"\n')
        loaded = self.store.load_all()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].registration_key, r.registration_key)

    def test_internal_malformed_line_skipped(self) -> None:
        r = _make_reg()
        self.store.append(r)
        # Insert an internal broken line BEFORE the valid one by
        # rewriting the file.
        original = self.path.read_bytes()
        broken = b'{"registration_key": "x"\n'
        self.path.write_bytes(broken + original)
        loaded = self.store.load_all()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].registration_key, r.registration_key)

    def test_duplicate_key_rejected(self) -> None:
        r = _make_reg()
        self.store.append(r)
        with self.assertRaises(DuplicateRegistrationError) as ctx:
            self.store.append(r)
        self.assertEqual(
            ctx.exception.registration_key, r.registration_key
        )
        # File still has just one record.
        self.assertEqual(len(self.store.load_all()), 1)

    def test_permissions_0600(self) -> None:
        self.store.append(_make_reg())
        mode = self.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_directory_permissions_0700(self) -> None:
        self.store.append(_make_reg())
        mode = self.path.parent.stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)

    def test_flush_fsync_before_success(self) -> None:
        # We can't easily observe the fsync, but we can assert the
        # record is visible immediately after append returns (no
        # buffering surprise).
        r = _make_reg()
        self.store.append(r)
        self.assertTrue(self.path.is_file())
        self.assertGreater(self.path.stat().st_size, 0)

    def test_concurrent_writer_lock_contention(self) -> None:
        """Two concurrent appenders; the second waits and then either
        succeeds (after the first finishes) or raises StoreBusy."""
        results: list = [None, None]

        def worker(idx: int) -> None:
            try:
                self.store.append(_make_reg(
                    account=f"acct-{idx}",
                    symbol="BTCUSD",
                    variant="FASTFib",
                    side="BUY",
                ))
                results[idx] = "ok"
            except Exception as exc:
                results[idx] = exc

        t1 = threading.Thread(target=worker, args=(0,))
        t2 = threading.Thread(target=worker, args=(1,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        # Both succeeded with distinct registration_keys.
        self.assertEqual(results, ["ok", "ok"])
        self.assertEqual(len(self.store.load_all()), 2)

    def test_get_returns_latest_for_distinct_key(self) -> None:
        r1 = _make_reg(account="ALICE", source_seq=10)
        r2 = _make_reg(account="BOB", source_seq=11)
        self.store.append(r1)
        self.store.append(r2)
        self.assertEqual(
            self.store.get(r1.registration_key).source_seq, 10
        )
        self.assertEqual(
            self.store.get(r2.registration_key).source_seq, 11
        )

    def test_exists_true_false(self) -> None:
        r = _make_reg()
        self.assertFalse(self.store.exists(r.registration_key))
        self.store.append(r)
        self.assertTrue(self.store.exists(r.registration_key))


class FromDictTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        r = _make_reg()
        raw = r.to_dict()
        # Add the registration_key alias as it would appear in JSONL.
        raw["registration_key"] = r.registration_key
        loaded = FiboRegistration.from_dict(raw)
        self.assertEqual(loaded.registration_key, r.registration_key)
        self.assertEqual(loaded.starting_volume, r.starting_volume)


if __name__ == "__main__":
    unittest.main()