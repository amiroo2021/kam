from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .dukascopy_format import DukascopyCandle

DEFAULT_DUKASCOPY_CACHE_PATH = Path("/root/kam/GoldenFibo/data/dukascopy_klines.sqlite")

# A day is verified for a side only when that side's own status says so.
# "failed" for one side never says anything about the other side.
VERIFIED_COVERAGE_STATUSES = frozenset({"verified_data", "verified_provider_empty"})


def _resolve_side_status(prior: str, day_status: str, side_acquired: bool, explicit: str = "") -> str:
    """Resolve one side's coverage status from the day status and prior state.

    The day-level ``status`` alone cannot describe two independently
    acquired sides. A failed ASK must not erase a verified BID, so each side
    keeps its own status:

    * an explicit status is authoritative for the side the caller reported,
      UNLESS the side already holds a verified status — a stale explicit
      value must never downgrade a genuine verification,
    * a side that already holds a VERIFIED status always keeps it — a
      day-level status never downgrades a genuine verification,
    * a verified day status applies only to a side the caller actually
      acquired,
    * anything else keeps the prior status.

    ``side_acquired`` means "the caller just successfully acquired this
    side". It is deliberately NOT OR-ed with the stored completeness flag:
    a legacy row can carry a completeness flag that disagrees with its
    status, and honouring that flag would promote an unverified side to
    verified. Failing closed is the priority.

    A ``verified_data`` prior is additionally protected from being rewritten
    to ``verified_provider_empty``: the cached candles for that side were
    decoded and verified, and a later market-closure reclassification cannot
    un-know them. Downgrading would lose a genuine verification — exactly
    the failure mode this per-side state exists to prevent.
    """
    if explicit:
        # A genuine verification outranks a stale explicit value.
        if prior in VERIFIED_COVERAGE_STATUSES and explicit not in VERIFIED_COVERAGE_STATUSES:
            return prior
        candidate = explicit
    elif prior in VERIFIED_COVERAGE_STATUSES:
        # A genuine verification is never downgraded by a later day-level
        # status (including a market-closure reclassification).
        candidate = prior
    elif day_status in VERIFIED_COVERAGE_STATUSES and side_acquired:
        candidate = day_status
    else:
        candidate = prior or ""
    if prior == "verified_data" and candidate == "verified_provider_empty":
        return "verified_data"
    return candidate


@dataclass(frozen=True)
class Coverage:
    instrument: str
    day: date
    status: str
    bid_complete: bool = False
    ask_complete: bool = False
    bid_status: str = ""
    ask_status: str = ""


class DukascopyKlineCache:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DUKASCOPY_CACHE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS candles (
                instrument TEXT NOT NULL,
                open_time_ms INTEGER NOT NULL,
                bid_open REAL, bid_high REAL, bid_low REAL, bid_close REAL,
                ask_open REAL, ask_high REAL, ask_low REAL, ask_close REAL,
                volume REAL, bid_volume REAL, ask_volume REAL,
                volume_units TEXT NOT NULL DEFAULT 'provider',
                source_day TEXT NOT NULL,
                PRIMARY KEY (instrument, open_time_ms)
            );
            CREATE INDEX IF NOT EXISTS idx_candles_range ON candles(instrument, open_time_ms);
            CREATE TABLE IF NOT EXISTS coverage_days (
                instrument TEXT NOT NULL,
                day TEXT NOT NULL,
                status TEXT NOT NULL,
                bid_complete INTEGER NOT NULL DEFAULT 0,
                ask_complete INTEGER NOT NULL DEFAULT 0,
                bid_status TEXT NOT NULL DEFAULT '',
                ask_status TEXT NOT NULL DEFAULT '',
                artifact TEXT,
                verified_at_ms INTEGER NOT NULL,
                note TEXT,
                PRIMARY KEY (instrument, day)
            );
            """)
            candle_columns = {row[1] for row in db.execute("PRAGMA table_info(candles)")}
            if "bid_volume" not in candle_columns:
                db.execute("ALTER TABLE candles ADD COLUMN bid_volume REAL")
                db.execute("UPDATE candles SET bid_volume=volume WHERE bid_volume IS NULL")
            if "ask_volume" not in candle_columns:
                db.execute("ALTER TABLE candles ADD COLUMN ask_volume REAL")
                db.execute("UPDATE candles SET ask_volume=volume WHERE ask_volume IS NULL")
            columns = {row[1] for row in db.execute("PRAGMA table_info(coverage_days)")}
            if "bid_complete" not in columns:
                db.execute("ALTER TABLE coverage_days ADD COLUMN bid_complete INTEGER NOT NULL DEFAULT 0")
            if "ask_complete" not in columns:
                db.execute("ALTER TABLE coverage_days ADD COLUMN ask_complete INTEGER NOT NULL DEFAULT 0")
            # Per-side statuses let BID and ASK coverage be verified
            # independently. A legacy row is backfilled from the day status
            # only for the sides it actually claimed to cover, so an existing
            # database never gains an unearned verification.
            if "bid_status" not in columns:
                db.execute("ALTER TABLE coverage_days ADD COLUMN bid_status TEXT NOT NULL DEFAULT ''")
            if "ask_status" not in columns:
                db.execute("ALTER TABLE coverage_days ADD COLUMN ask_status TEXT NOT NULL DEFAULT ''")
            db.execute(
                "UPDATE coverage_days SET bid_status=status WHERE bid_status='' AND bid_complete=1"
            )
            db.execute(
                "UPDATE coverage_days SET ask_status=status WHERE ask_status='' AND ask_complete=1"
            )

    def upsert_side(self, instrument: str, candles: Iterable[DukascopyCandle], *, volume_units: str = "provider") -> int:
        rows = list(candles)
        if not rows:
            return 0
        side = rows[0].price_side.lower()
        if side not in {"bid", "ask"}:
            raise ValueError("price side must be bid or ask")
        cols = f"{side}_open,{side}_high,{side}_low,{side}_close"
        with self._connect() as db:
            for c in rows:
                o, h, l, cl = c.ohlc
                day = datetime.fromtimestamp(c.open_time_ms / 1000, tz=timezone.utc).date().isoformat()
                db.execute(
                    f"INSERT INTO candles(instrument,open_time_ms,{cols},{side}_volume,volume_units,source_day) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(instrument,open_time_ms) DO UPDATE SET "
                    f"{side}_open=excluded.{side}_open,{side}_high=excluded.{side}_high,"
                    f"{side}_low=excluded.{side}_low,{side}_close=excluded.{side}_close,"
                    f"{side}_volume=COALESCE(excluded.{side}_volume,candles.{side}_volume),"
                    "volume_units=excluded.volume_units",
                    (instrument.lower(), c.open_time_ms, o, h, l, cl, c.volume, volume_units, day),
                )
        return len(rows)

    def mark_day(
        self,
        instrument: str,
        day: date,
        status: str,
        *,
        artifact: str = "",
        note: str = "",
        bid_complete: bool = False,
        ask_complete: bool = False,
        bid_status: str = "",
        ask_status: str = "",
    ) -> None:
        """Record the coverage state for one day.

        ``bid_complete`` / ``ask_complete`` mean "this side was just
        successfully acquired and verified". A side that was not acquired
        keeps whatever status it already had; it is never promoted to
        verified by the day's status alone.
        """
        if status not in {"verified_data", "verified_provider_empty", "pending", "failed"}:
            raise ValueError(status)
        instrument_n = instrument.lower()
        day_iso = day.isoformat()
        with self._connect() as db:
            # The read-modify-write must be atomic: a concurrent writer that
            # verifies the peer side between our read and our write would
            # otherwise have its verification overwritten by a stale value.
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT bid_status,ask_status "
                "FROM coverage_days WHERE instrument=? AND day=?",
                (instrument_n, day_iso),
            ).fetchone()
            prior_bid_status = str(prior[0] or "") if prior else ""
            prior_ask_status = str(prior[1] or "") if prior else ""

            # Each side keeps its own verification. Only a side the caller
            # explicitly reported may be overridden; the peer side is
            # resolved HERE, from the row read inside this transaction, so a
            # stale value read by the caller before the write cannot
            # overwrite a newer verification.
            resolved_bid = _resolve_side_status(
                prior_bid_status,
                status,
                bid_complete,
                bid_status,
            )
            resolved_ask = _resolve_side_status(
                prior_ask_status,
                status,
                ask_complete,
                ask_status,
            )
            # Completeness follows the RESOLVED side status, so a side can
            # never be marked complete while its own status says otherwise.
            resolved_bid_complete = resolved_bid in VERIFIED_COVERAGE_STATUSES
            resolved_ask_complete = resolved_ask in VERIFIED_COVERAGE_STATUSES
            db.execute(
                "INSERT INTO coverage_days(instrument,day,status,bid_complete,ask_complete,"
                "bid_status,ask_status,artifact,verified_at_ms,note) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(instrument,day) DO UPDATE SET status=excluded.status,artifact=excluded.artifact,"
                "bid_complete=excluded.bid_complete,ask_complete=excluded.ask_complete,"
                "bid_status=excluded.bid_status,ask_status=excluded.ask_status,"
                "verified_at_ms=excluded.verified_at_ms,note=excluded.note",
                (
                    instrument_n,
                    day_iso,
                    status,
                    int(resolved_bid_complete),
                    int(resolved_ask_complete),
                    resolved_bid,
                    resolved_ask,
                    artifact,
                    int(datetime.now(timezone.utc).timestamp() * 1000),
                    note,
                ),
            )

    def coverage(self, instrument: str, start_day: date, end_day: date) -> list[Coverage]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT day,status,bid_complete,ask_complete,bid_status,ask_status "
                "FROM coverage_days WHERE instrument=? AND day>=? AND day<=? ORDER BY day",
                (instrument.lower(), start_day.isoformat(), end_day.isoformat()),
            ).fetchall()
        return [
            Coverage(
                instrument.lower(),
                date.fromisoformat(day),
                status,
                bool(bid),
                bool(ask),
                str(bid_status or ""),
                str(ask_status or ""),
            )
            for day, status, bid, ask, bid_status, ask_status in rows
        ]

    @staticmethod
    def side_status(entry: Coverage | None, side: str) -> str:
        """The side's own coverage status for a day.

        Returns "" when the side was never covered, the side's own status
        otherwise. Legacy rows that carry no per-side status fall back to the
        day status for the sides they actually claimed to cover.
        """
        if entry is None:
            return ""
        status = getattr(entry, f"{side}_status", "")
        if status:
            return status
        if getattr(entry, f"{side}_complete", False):
            return entry.status
        return ""

    @staticmethod
    def side_is_verified(entry: Coverage | None, side: str) -> bool:
        """True when the requested side is verified for the day.

        A side is verified when the side's own status is a verified status.
        Legacy rows written before per-side statuses existed fall back to the
        day status, but only for the sides they actually claimed to cover.
        """
        if entry is None:
            return False
        side_status = getattr(entry, f"{side}_status", "")
        if side_status:
            return side_status in VERIFIED_COVERAGE_STATUSES
        if not getattr(entry, f"{side}_complete", False):
            return False
        return entry.status in VERIFIED_COVERAGE_STATUSES

    def missing_days(
        self,
        instrument: str,
        start_day: date,
        end_day: date,
        *,
        refresh_days: set[date] | None = None,
        price_side: str | None = None,
    ) -> list[date]:
        if price_side is not None and price_side.lower() not in {"bid", "ask"}:
            raise ValueError("price side must be bid or ask")
        known = {c.day: c for c in self.coverage(instrument, start_day, end_day)}
        refresh_days = refresh_days or set()
        out: list[date] = []
        day = start_day
        while day <= end_day:
            entry = known.get(day)
            if price_side is None:
                complete = self.side_is_verified(entry, "bid") and self.side_is_verified(entry, "ask")
            else:
                complete = self.side_is_verified(entry, price_side.lower())
            if day in refresh_days or not complete:
                out.append(day)
            day += timedelta(days=1)
        return out

    def rows(self, instrument: str, start_ms: int, end_ms: int, *, price_side: str = "bid") -> list[list]:
        side = price_side.lower()
        if side not in {"bid", "ask"}:
            raise ValueError("price side must be bid or ask")
        with self._connect() as db:
            data = db.execute(f"SELECT open_time_ms,{side}_open,{side}_high,{side}_low,{side}_close,{side}_volume FROM candles WHERE instrument=? AND open_time_ms>=? AND open_time_ms<? ORDER BY open_time_ms", (instrument.lower(), start_ms, end_ms)).fetchall()
        # Replay keeps base volume in the legacy slot, but quote volume and
        # taker-buy fields are deliberately zero: Dukascopy volume is not
        # Binance quote/notional volume.
        return [[int(ts), o, h, l, c, float(vol or 0), int(ts) + 59_999, 0.0, 0, 0.0, 0.0, "0"] for ts, o, h, l, c, vol in data if o is not None]
