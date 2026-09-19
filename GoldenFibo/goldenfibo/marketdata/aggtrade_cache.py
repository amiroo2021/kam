"""Persistent aggTrade cache for GoldenFibo.

Archive, REST, and WebSocket are ingestion mechanisms only.
Metric calculation should read from the local cache, not Binance transports.
"""
from __future__ import annotations

import io
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from ..metrics.trade_vap import AggTrade
from .binance_agg_trades import BINANCE_SPOT_REST, BINANCE_USDM_REST, fetch_agg_trades_range

DEFAULT_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "aggtrades.sqlite"
MARKET_SPOT = "spot"
MARKET_FUTURES = "futures"
_TWO_DAYS_MS = 2 * 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class CoverageInterval:
    market: str
    symbol: str
    start_ms: int
    end_ms: int
    source: str = ""
    verified_at_ms: int = 0
    note: str = ""


@dataclass(frozen=True)
class EnsureCoverageResult:
    market: str
    symbol: str
    requested_start_ms: int
    requested_end_ms: int
    covered: bool
    missing_ranges: List[Tuple[int, int]] = field(default_factory=list)
    fetched_trade_count: int = 0
    inserted_trade_count: int = 0
    archive_requests: int = 0
    rest_requests: int = 0
    coverage_intervals: List[CoverageInterval] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


class AggTradeCache:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_CACHE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=60000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agg_trades (
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    agg_trade_id INTEGER NOT NULL,
                    price TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    first_trade_id INTEGER NOT NULL,
                    last_trade_id INTEGER NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    buyer_is_maker INTEGER NOT NULL,
                    id_domain TEXT NOT NULL DEFAULT 'aggtrade',
                    source TEXT NOT NULL DEFAULT '',
                    inserted_at_ms INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (market, symbol, agg_trade_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agg_trades_ts
                ON agg_trades (market, symbol, timestamp_ms)
                """
            )
            cols = {r[1] for r in conn.execute("PRAGMA table_info(agg_trades)").fetchall()}
            if "id_domain" not in cols:
                conn.execute("ALTER TABLE agg_trades ADD COLUMN id_domain TEXT NOT NULL DEFAULT 'aggtrade'")
            conn.execute("UPDATE agg_trades SET id_domain='trade' WHERE source='ws'")
            conn.execute("UPDATE agg_trades SET id_domain='aggtrade' WHERE source IN ('rest', 'archive') OR id_domain='' OR id_domain IS NULL")
            # Raw @trade IDs and aggregate-trade IDs are separate domains, but
            # the legacy SQLite PK is (market, symbol, agg_trade_id). Store raw
            # trade IDs in a negative keyspace while preserving the real raw ID
            # in first_trade_id/last_trade_id. This avoids a rebuild and
            # prevents cross-domain numeric collisions.
            conn.execute(
                """
                UPDATE agg_trades
                SET agg_trade_id = -ABS(agg_trade_id),
                    first_trade_id = ABS(first_trade_id),
                    last_trade_id = ABS(last_trade_id)
                WHERE id_domain='trade' AND agg_trade_id > 0
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coverage_intervals (
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    verified_at_ms INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (market, symbol, start_ms, end_ms)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_coverage_intervals_lookup
                ON coverage_intervals (market, symbol, start_ms, end_ms)
                """
            )

    @staticmethod
    def _normalize_market(market: str) -> str:
        m = str(market or MARKET_SPOT).lower()
        if m in {"futures", "future", "usdm"}:
            return MARKET_FUTURES
        return MARKET_SPOT

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return str(symbol).upper().replace("/", "")

    @staticmethod
    def _utc_day_bounds_ms(ms: int) -> tuple[int, int]:
        dt = datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)
        start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1) - timedelta(milliseconds=1)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    @staticmethod
    def _iter_days(start_ms: int, end_ms: int) -> Iterable[tuple[int, int, str]]:
        start_dt = datetime.fromtimestamp(int(start_ms) / 1000.0, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(int(end_ms) / 1000.0, tz=timezone.utc)
        cur = datetime(start_dt.year, start_dt.month, start_dt.day, tzinfo=timezone.utc)
        last = datetime(end_dt.year, end_dt.month, end_dt.day, tzinfo=timezone.utc)
        while cur <= last:
            day_start = int(cur.timestamp() * 1000)
            day_end = int((cur + timedelta(days=1)).timestamp() * 1000) - 1
            yield day_start, day_end, cur.strftime("%Y-%m-%d")
            cur += timedelta(days=1)

    @staticmethod
    def archive_url_for_day(symbol: str, day_ymd: str, *, market: str) -> str:
        symbol = symbol.upper().replace("/", "")
        market_n = AggTradeCache._normalize_market(market)
        if market_n == MARKET_FUTURES:
            return (
                "https://data.binance.vision/data/futures/um/daily/aggTrades/"
                f"{symbol}/{symbol}-aggTrades-{day_ymd}.zip"
            )
        return f"https://data.binance.vision/data/spot/daily/aggTrades/{symbol}/{symbol}-aggTrades-{day_ymd}.zip"

    def _parse_aggtrade_csv(self, csv_bytes: bytes) -> List[AggTrade]:
        rows: List[AggTrade] = []
        text = csv_bytes.decode("utf-8", errors="replace")
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 7:
                continue
            try:
                rows.append(
                    AggTrade(
                        agg_id=int(parts[0]),
                        price=float(parts[1]),
                        qty=float(parts[2]),
                        ts_ms=int(parts[5]),
                        first_trade_id=int(parts[3]),
                        last_trade_id=int(parts[4]),
                        id_domain="aggtrade",
                    )
                )
            except (TypeError, ValueError):
                continue
        return rows

    def _download_archive_zip(self, url: str, *, timeout: float = 60.0) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "GoldenFibo/0.2"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 public HTTPS
            return resp.read()

    def _fetch_archive_day(
        self,
        market: str,
        symbol: str,
        day_start_ms: int,
        day_end_ms: int,
        day_ymd: str,
        *,
        timeout: float = 60.0,
    ) -> tuple[List[AggTrade], str]:
        url = self.archive_url_for_day(symbol, day_ymd, market=market)
        try:
            payload = self._download_archive_zip(url, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return [], "404"
            raise
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if not names:
                return [], "empty"
            # Binance ZIPs usually contain a single CSV.
            data = zf.read(names[0])
        trades = self._parse_aggtrade_csv(data)
        if trades:
            self.insert_trades(market, symbol, trades, source="archive")
            self.record_coverage(market, symbol, day_start_ms, day_end_ms, source="archive", note=day_ymd)
        return trades, "ok"

    def fetch_archive_range(
        self,
        market: str,
        symbol: str,
        start_ms: int,
        end_ms: int,
        *,
        timeout: float = 60.0,
    ) -> List[AggTrade]:
        market_n = self._normalize_market(market)
        symbol_n = self._normalize_symbol(symbol)
        out: List[AggTrade] = []
        for day_start, day_end, day_ymd in self._iter_days(start_ms, end_ms):
            try:
                rows, status = self._fetch_archive_day(
                    market_n,
                    symbol_n,
                    day_start,
                    day_end,
                    day_ymd,
                    timeout=timeout,
                )
            except Exception:
                continue
            if status == "ok":
                out.extend(rows)
        return out

    def insert_trades(
        self,
        market: str,
        symbol: str,
        trades: Sequence[AggTrade | dict],
        *,
        source: str = "",
    ) -> int:
        market_n = self._normalize_market(market)
        symbol_n = self._normalize_symbol(symbol)
        rows: list[tuple] = []
        inserted_at_ms = int(time.time() * 1000)
        for t in trades:
            if isinstance(t, AggTrade):
                id_domain = str(getattr(t, "id_domain", "aggtrade") or "aggtrade")
                raw_id = int(t.agg_id)
                agg_id = -abs(raw_id) if id_domain == "trade" else raw_id
                price = str(float(t.price))
                qty = str(float(t.qty))
                ts_ms = int(t.ts_ms)
                first_trade_id = int(t.first_trade_id) if t.first_trade_id is not None else raw_id
                last_trade_id = int(t.last_trade_id) if t.last_trade_id is not None else raw_id
                buyer_is_maker = 0
            else:
                try:
                    id_domain = "aggtrade" if "a" in t else "trade"
                    raw_id = int(t["a"] if "a" in t else t["t"])
                    agg_id = -abs(raw_id) if id_domain == "trade" else raw_id
                    price = str(t["p"])
                    qty = str(t["q"])
                    first_trade_id = int(t.get("f", raw_id))
                    last_trade_id = int(t.get("l", raw_id))
                    ts_ms = int(t.get("T") or t.get("E") or 0)
                    buyer_is_maker = 1 if bool(t.get("m")) else 0
                except Exception:
                    continue
            rows.append(
                (
                    market_n,
                    symbol_n,
                    agg_id,
                    price,
                    qty,
                    first_trade_id,
                    last_trade_id,
                    ts_ms,
                    buyer_is_maker,
                    id_domain,
                    source,
                    inserted_at_ms,
                )
            )
        if not rows:
            return 0
        with self._connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO agg_trades(
                    market, symbol, agg_trade_id, price, quantity,
                    first_trade_id, last_trade_id, timestamp_ms,
                    buyer_is_maker, id_domain, source, inserted_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market, symbol, agg_trade_id) DO NOTHING
                """,
                rows,
            )
            inserted = conn.total_changes - before
        return int(inserted)

    def query_trades_range(
        self,
        market: str,
        symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> List[AggTrade]:
        """Return canonical trade rows for a metric window.

        REST/archive aggregate rows and live WS raw trade rows may coexist in
        the cache. When both representations cover the same economic trade we
        must keep the aggregate and drop the raw row, never the other way
        around. The filter is determined by underlying raw trade IDs:

          aggregate row: first_trade_id .. last_trade_id
          raw trade row: first_trade_id == last_trade_id == real Binance raw ID

        The previous implementation used a correlated SQL EXISTS for every
        raw row, which degenerated into a per-row scan over the aggregate
        population (millions of rows). The new algorithm:

          1. Fetch aggregate rows for the window by timestamp.
          2. Fetch raw trade rows for the window by timestamp.
          3. Build merged [first_trade_id, last_trade_id] intervals from the
             aggregate rows and binary-search each raw trade's ID against
             those intervals in O(log A).

        All three retrievals use the existing timestamp index. No correlated
        subquery, no per-row containment scan.
        """
        market_n = self._normalize_market(market)
        symbol_n = self._normalize_symbol(symbol)
        lo = int(start_ms)
        hi = int(end_ms)
        with self._connect() as conn:
            aggregate_rows = conn.execute(
                """
                SELECT agg_trade_id, price, quantity, timestamp_ms,
                       first_trade_id, last_trade_id
                FROM agg_trades
                WHERE market=? AND symbol=? AND id_domain='aggtrade'
                  AND timestamp_ms BETWEEN ? AND ?
                """,
                (market_n, symbol_n, lo, hi),
            ).fetchall()
            raw_rows = conn.execute(
                """
                SELECT agg_trade_id, price, quantity, timestamp_ms,
                       first_trade_id, last_trade_id
                FROM agg_trades
                WHERE market=? AND symbol=? AND id_domain='trade'
                  AND timestamp_ms BETWEEN ? AND ?
                """,
                (market_n, symbol_n, lo, hi),
                # Use a small chunked iterator instead of materializing the
                # full result set in a single fetchall() to keep peak memory
                # bounded for large metric windows.
            )
            raw_rows_iter = raw_rows  # keep handle alive

        intervals: List[Tuple[int, int]] = []
        for r in aggregate_rows:
            f = int(r["first_trade_id"])
            l = int(r["last_trade_id"])
            if l < f:
                f, l = l, f
            if intervals and intervals[-1][1] >= f - 1:
                prev_f, prev_l = intervals[-1]
                intervals[-1] = (prev_f, max(prev_l, l))
            else:
                intervals.append((f, l))
        intervals.sort(key=lambda iv: iv[0])

        def covered(raw_tid: int) -> bool:
            if not intervals:
                return False
            lo_i, hi_i = 0, len(intervals) - 1
            while lo_i <= hi_i:
                mid = (lo_i + hi_i) // 2
                iv_f, iv_l = intervals[mid]
                if raw_tid < iv_f:
                    hi_i = mid - 1
                elif raw_tid > iv_l:
                    lo_i = mid + 1
                else:
                    return True
            return False

        out: List[AggTrade] = []
        for r in aggregate_rows:
            out.append(
                AggTrade(
                    agg_id=int(r["agg_trade_id"]),
                    price=float(r["price"]),
                    qty=float(r["quantity"]),
                    ts_ms=int(r["timestamp_ms"]),
                    first_trade_id=int(r["first_trade_id"]),
                    last_trade_id=int(r["last_trade_id"]),
                    id_domain="aggtrade",
                )
            )
        for r in raw_rows_iter:
            raw_tid = int(r["first_trade_id"])
            if covered(raw_tid):
                continue
            out.append(
                AggTrade(
                    agg_id=int(r["agg_trade_id"]),
                    price=float(r["price"]),
                    qty=float(r["quantity"]),
                    ts_ms=int(r["timestamp_ms"]),
                    first_trade_id=raw_tid,
                    last_trade_id=raw_tid,
                    id_domain="trade",
                )
            )

        out.sort(key=lambda t: (t.ts_ms, t.agg_id))
        return out

    def iter_deduped_trades_range(
        self,
        market: str,
        symbol: str,
        start_ms: int,
        end_ms: int,
        *,
        chunk_size: int = 50_000,
    ):
        """Yield canonical deduped trades for the window without materializing.

        REST/archive aggregate rows take precedence over an overlapping raw
        ``@trade`` row that covers the same underlying real trade ID.

        Implementation:

        1. Stream aggregate rows in timestamp chunks of ``chunk_size`` via
           keyset iteration on ``(timestamp_ms, agg_trade_id)``.
        2. Yield each aggregate row as-is.
        3. After aggregate streaming is complete, collect the small set of raw
           ``@trade`` rows for the window and filter them in memory using the
           merged aggregate intervals — raw populations are far smaller than
           aggregate populations in practice.

        Memory bound: one chunk of aggregates + raw window + interval list.
        """
        market_n = self._normalize_market(market)
        symbol_n = self._normalize_symbol(symbol)
        lo = int(start_ms)
        hi = int(end_ms)

        intervals: List[Tuple[int, int]] = []

        # Pass 1: stream aggregate rows by (ts, id) keyset and yield.
        with self._connect() as conn:
            cur_ts = lo
            cur_id = -(2**63)
            while True:
                rows = conn.execute(
                    """
                    SELECT agg_trade_id, price, quantity, timestamp_ms,
                           first_trade_id, last_trade_id
                    FROM agg_trades
                    WHERE market=? AND symbol=? AND id_domain='aggtrade'
                      AND timestamp_ms BETWEEN ? AND ?
                      AND (timestamp_ms > ? OR (timestamp_ms = ? AND agg_trade_id > ?))
                    ORDER BY timestamp_ms ASC, agg_trade_id ASC
                    LIMIT ?
                    """,
                    (market_n, symbol_n, lo, hi, cur_ts, cur_ts, cur_id, int(chunk_size)),
                ).fetchall()
                if not rows:
                    break
                for r in rows:
                    f = int(r["first_trade_id"])
                    l = int(r["last_trade_id"])
                    if l < f:
                        f, l = l, f
                    if intervals and intervals[-1][1] >= f - 1:
                        prev_f, prev_l = intervals[-1]
                        intervals[-1] = (prev_f, max(prev_l, l))
                    else:
                        intervals.append((f, l))
                    yield AggTrade(
                        agg_id=int(r["agg_trade_id"]),
                        price=float(r["price"]),
                        qty=float(r["quantity"]),
                        ts_ms=int(r["timestamp_ms"]),
                        first_trade_id=int(r["first_trade_id"]),
                        last_trade_id=int(r["last_trade_id"]),
                        id_domain="aggtrade",
                    )
                last = rows[-1]
                cur_ts = int(last["timestamp_ms"])
                cur_id = int(last["agg_trade_id"])
                if len(rows) < int(chunk_size):
                    break

        # No aggregates → no coverage intervals, but we still need to
        # yield raw rows. The covered() helper handles empty intervals.
        if intervals:
            intervals.sort(key=lambda iv: iv[0])

        def covered(raw_tid: int) -> bool:
            if not intervals:
                return False
            lo_i, hi_i = 0, len(intervals) - 1
            while lo_i <= hi_i:
                mid = (lo_i + hi_i) // 2
                iv_f, iv_l = intervals[mid]
                if raw_tid < iv_f:
                    hi_i = mid - 1
                elif raw_tid > iv_l:
                    lo_i = mid + 1
                else:
                    return True
            return False

        # Pass 2: stream raw rows (population is much smaller than aggregates).
        with self._connect() as conn:
            cur_ts = lo
            cur_id = -(2**63)
            while True:
                rows = conn.execute(
                    """
                    SELECT agg_trade_id, price, quantity, timestamp_ms,
                           first_trade_id, last_trade_id
                    FROM agg_trades
                    WHERE market=? AND symbol=? AND id_domain='trade'
                      AND timestamp_ms BETWEEN ? AND ?
                      AND (timestamp_ms > ? OR (timestamp_ms = ? AND agg_trade_id > ?))
                    ORDER BY timestamp_ms ASC, agg_trade_id ASC
                    LIMIT ?
                    """,
                    (market_n, symbol_n, lo, hi, cur_ts, cur_ts, cur_id, int(chunk_size)),
                ).fetchall()
                if not rows:
                    break
                for r in rows:
                    raw_tid = int(r["first_trade_id"])
                    if covered(raw_tid):
                        continue
                    yield AggTrade(
                        agg_id=int(r["agg_trade_id"]),
                        price=float(r["price"]),
                        qty=float(r["quantity"]),
                        ts_ms=int(r["timestamp_ms"]),
                        first_trade_id=raw_tid,
                        last_trade_id=raw_tid,
                        id_domain="trade",
                    )
                last = rows[-1]
                cur_ts = int(last["timestamp_ms"])
                cur_id = int(last["agg_trade_id"])
                if len(rows) < int(chunk_size):
                    break

    def iter_after_frontier(
        self,
        market: str,
        symbol: str,
        frontier_ts_ms: int,
        frontier_storage_id: int,
        *,
        max_ts_ms: Optional[int] = None,
        chunk_size: int = 50_000,
    ):
        """Yield deduped canonical trades strictly after the (ts, agg_trade_id) frontier.

        Used by the controller's post-install catch-up to retrieve any trades
        that were persisted into SQLite between the backfill endpoint and
        install time. Aggregate rows are matched on
        ``(ts, agg_trade_id)``; raw rows are matched on
        ``(ts, first_trade_id)`` to avoid the negative-keyspace collision.

        Yielded trades are guaranteed distinct from any trades already in the
        accumulator up to ``(frontier_ts_ms, frontier_storage_id)`` exclusive
        (strict less-than on (ts, storage_id)).
        """
        market_n = self._normalize_market(market)
        symbol_n = self._normalize_symbol(symbol)
        frontier_ts = int(frontier_ts_ms)
        frontier_id = int(frontier_storage_id)
        max_ts = int(max_ts_ms) if max_ts_ms is not None else 9_999_999_999_999

        intervals: List[Tuple[int, int]] = []

        # Aggregate pass.
        with self._connect() as conn:
            cur_ts = frontier_ts
            cur_id = frontier_id
            while True:
                rows = conn.execute(
                    """
                    SELECT agg_trade_id, price, quantity, timestamp_ms,
                           first_trade_id, last_trade_id
                    FROM agg_trades
                    WHERE market=? AND symbol=? AND id_domain='aggtrade'
                      AND timestamp_ms <= ?
                      AND (timestamp_ms > ? OR (timestamp_ms = ? AND agg_trade_id > ?))
                    ORDER BY timestamp_ms ASC, agg_trade_id ASC
                    LIMIT ?
                    """,
                    (market_n, symbol_n, max_ts, cur_ts, cur_ts, cur_id, int(chunk_size)),
                ).fetchall()
                if not rows:
                    break
                for r in rows:
                    f = int(r["first_trade_id"])
                    l = int(r["last_trade_id"])
                    if l < f:
                        f, l = l, f
                    if intervals and intervals[-1][1] >= f - 1:
                        prev_f, prev_l = intervals[-1]
                        intervals[-1] = (prev_f, max(prev_l, l))
                    else:
                        intervals.append((f, l))
                    yield AggTrade(
                        agg_id=int(r["agg_trade_id"]),
                        price=float(r["price"]),
                        qty=float(r["quantity"]),
                        ts_ms=int(r["timestamp_ms"]),
                        first_trade_id=int(r["first_trade_id"]),
                        last_trade_id=int(r["last_trade_id"]),
                        id_domain="aggtrade",
                    )
                last = rows[-1]
                cur_ts = int(last["timestamp_ms"])
                cur_id = int(last["agg_trade_id"])
                if len(rows) < int(chunk_size):
                    break

        if intervals:
            intervals.sort(key=lambda iv: iv[0])

            def covered(raw_tid: int) -> bool:
                lo_i, hi_i = 0, len(intervals) - 1
                while lo_i <= hi_i:
                    mid = (lo_i + hi_i) // 2
                    iv_f, iv_l = intervals[mid]
                    if raw_tid < iv_f:
                        hi_i = mid - 1
                    elif raw_tid > iv_l:
                        lo_i = mid + 1
                    else:
                        return True
                return False
        else:
            def covered(raw_tid: int) -> bool:  # noqa: F811
                return False

        # Raw pass: use (ts, first_trade_id) as the keyset (positive space,
        # always increasing within a symbol). frontier_id is the historical
        # aggregate's agg_trade_id; for the raw continuation we use it as a
        # lower-bound hint only — but the strongest constraint is
        # ts >= frontier_ts. We also exclude raw rows whose real_trade_id is
        # covered by an aggregate interval built from the post-frontier
        # aggregates above.
        with self._connect() as conn:
            cur_ts = frontier_ts
            cur_raw_id = -(2**63)
            while True:
                rows = conn.execute(
                    """
                    SELECT agg_trade_id, price, quantity, timestamp_ms,
                           first_trade_id, last_trade_id
                    FROM agg_trades
                    WHERE market=? AND symbol=? AND id_domain='trade'
                      AND timestamp_ms <= ?
                      AND (timestamp_ms > ? OR (timestamp_ms = ? AND first_trade_id > ?))
                    ORDER BY timestamp_ms ASC, first_trade_id ASC
                    LIMIT ?
                    """,
                    (market_n, symbol_n, max_ts, cur_ts, cur_ts, cur_raw_id, int(chunk_size)),
                ).fetchall()
                if not rows:
                    break
                for r in rows:
                    raw_tid = int(r["first_trade_id"])
                    if covered(raw_tid):
                        continue
                    yield AggTrade(
                        agg_id=int(r["agg_trade_id"]),
                        price=float(r["price"]),
                        qty=float(r["quantity"]),
                        ts_ms=int(r["timestamp_ms"]),
                        first_trade_id=raw_tid,
                        last_trade_id=raw_tid,
                        id_domain="trade",
                    )
                last = rows[-1]
                cur_ts = int(last["timestamp_ms"])
                cur_raw_id = int(last["first_trade_id"])
                if len(rows) < int(chunk_size):
                    break

    def query_trades_minmax(
        self,
        market: str,
        symbol: str,
    ) -> tuple[Optional[int], Optional[int], int]:
        market_n = self._normalize_market(market)
        symbol_n = self._normalize_symbol(symbol)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT MIN(timestamp_ms) AS first_ts,
                       MAX(timestamp_ms) AS last_ts,
                       COUNT(*) AS n
                FROM agg_trades
                WHERE market=? AND symbol=?
                """,
                (market_n, symbol_n),
            ).fetchone()
        return (
            int(row["first_ts"]) if row and row["first_ts"] is not None else None,
            int(row["last_ts"]) if row and row["last_ts"] is not None else None,
            int(row["n"] or 0),
        )

    def coverage_intervals(self, market: str, symbol: str) -> List[CoverageInterval]:
        market_n = self._normalize_market(market)
        symbol_n = self._normalize_symbol(symbol)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT market, symbol, start_ms, end_ms, source, verified_at_ms, note
                FROM coverage_intervals
                WHERE market=? AND symbol=?
                ORDER BY start_ms, end_ms
                """,
                (market_n, symbol_n),
            ).fetchall()
        return [
            CoverageInterval(
                market=r["market"],
                symbol=r["symbol"],
                start_ms=int(r["start_ms"]),
                end_ms=int(r["end_ms"]),
                source=r["source"] or "",
                verified_at_ms=int(r["verified_at_ms"] or 0),
                note=r["note"] or "",
            )
            for r in rows
        ]

    def record_coverage(
        self,
        market: str,
        symbol: str,
        start_ms: int,
        end_ms: int,
        *,
        source: str = "",
        note: str = "",
    ) -> None:
        market_n = self._normalize_market(market)
        symbol_n = self._normalize_symbol(symbol)
        start = int(start_ms)
        end = int(end_ms)
        if end < start:
            return
        verified_at_ms = int(time.time() * 1000)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT rowid, start_ms, end_ms, source, note, verified_at_ms
                FROM coverage_intervals
                WHERE market=? AND symbol=? AND NOT (end_ms < ? - 1 OR start_ms > ? + 1)
                ORDER BY start_ms, end_ms
                """,
                (market_n, symbol_n, start, end),
            ).fetchall()
            merged_start = start
            merged_end = end
            sources = {source} if source else set()
            notes = {note} if note else set()
            rowids = []
            for row in rows:
                rowids.append(int(row["rowid"]))
                merged_start = min(merged_start, int(row["start_ms"]))
                merged_end = max(merged_end, int(row["end_ms"]))
                if row["source"]:
                    sources.add(str(row["source"]))
                if row["note"]:
                    notes.add(str(row["note"]))
            if rowids:
                conn.executemany("DELETE FROM coverage_intervals WHERE rowid=?", [(rid,) for rid in rowids])
            conn.execute(
                """
                INSERT INTO coverage_intervals(
                    market, symbol, start_ms, end_ms, source, verified_at_ms, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market, symbol, start_ms, end_ms)
                DO UPDATE SET source=excluded.source,
                              verified_at_ms=excluded.verified_at_ms,
                              note=excluded.note
                """,
                (
                    market_n,
                    symbol_n,
                    merged_start,
                    merged_end,
                    "|".join(sorted(sources)),
                    verified_at_ms,
                    "|".join(sorted(notes)),
                ),
            )

    def coverage_covers(self, market: str, symbol: str, start_ms: int, end_ms: int) -> bool:
        return not self.missing_ranges(market, symbol, start_ms, end_ms)

    def missing_ranges(
        self,
        market: str,
        symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> List[Tuple[int, int]]:
        start = int(start_ms)
        end = int(end_ms)
        if end < start:
            return []
        intervals = self.coverage_intervals(market, symbol)
        cursor = start
        missing: List[Tuple[int, int]] = []
        for iv in intervals:
            if iv.end_ms < cursor:
                continue
            if iv.start_ms > cursor:
                missing.append((cursor, min(end, iv.start_ms - 1)))
            cursor = max(cursor, iv.end_ms + 1)
            if cursor > end:
                break
        if cursor <= end:
            missing.append((cursor, end))
        return [(a, b) for a, b in missing if a <= b]

    def _rest_fetch_range(
        self,
        market: str,
        symbol: str,
        start_ms: int,
        end_ms: int,
        *,
        fetcher: Optional[Callable[..., List[AggTrade]]] = None,
        pause_s: float = 0.0,
        timeout: float = 30.0,
    ) -> List[AggTrade]:
        fn = fetcher or fetch_agg_trades_range
        return fn(symbol, start_ms, end_ms, market=market, pause_s=pause_s, timeout=timeout)

    def ensure_coverage(
        self,
        market: str,
        symbol: str,
        start_ms: int,
        end_ms: int,
        *,
        rest_fetcher: Optional[Callable[..., List[AggTrade]]] = None,
        archive_fetcher: Optional[Callable[[str, str, int, int], Sequence[AggTrade]]] = None,
        now_ms: Optional[int] = None,
        pause_s: float = 0.0,
        timeout: float = 30.0,
    ) -> EnsureCoverageResult:
        market_n = self._normalize_market(market)
        symbol_n = self._normalize_symbol(symbol)
        start = int(start_ms)
        end = int(end_ms)
        if end < start:
            return EnsureCoverageResult(
                market=market_n,
                symbol=symbol_n,
                requested_start_ms=start,
                requested_end_ms=end,
                covered=True,
            )

        notes: List[str] = []
        fetched_trade_count = 0
        inserted_trade_count = 0
        archive_requests = 0
        rest_requests = 0

        missing = self.missing_ranges(market_n, symbol_n, start, end)
        if not missing:
            return EnsureCoverageResult(
                market=market_n,
                symbol=symbol_n,
                requested_start_ms=start,
                requested_end_ms=end,
                covered=True,
                coverage_intervals=self.coverage_intervals(market_n, symbol_n),
            )

        now = int(now_ms) if now_ms is not None else int(time.time() * 1000)
        rest_floor = max(start, now - _TWO_DAYS_MS) if market_n == MARKET_FUTURES else start

        for miss_start, miss_end in missing:
            if miss_end < miss_start:
                continue
            if market_n == MARKET_FUTURES and miss_start < rest_floor:
                archive_end = min(miss_end, rest_floor - 1)
                if archive_end >= miss_start:
                    archive_requests += 1
                    archive_fn = archive_fetcher or self.fetch_archive_range
                    rows = list(archive_fn(market_n, symbol_n, miss_start, archive_end))
                    fetched_trade_count += len(rows)
                    inserted_trade_count += self.insert_trades(market_n, symbol_n, rows, source="archive")
                    # Daily archive ingestion verifies the whole day slice it touched.
                    for day_start, day_end, day_ymd in self._iter_days(miss_start, archive_end):
                        day_rows = self.query_trades_range(market_n, symbol_n, day_start, day_end)
                        if day_rows:
                            self.record_coverage(market_n, symbol_n, day_start, day_end, source="archive", note=day_ymd)
                    notes.append(f"archive:{miss_start}-{archive_end}")
                miss_start = max(miss_start, rest_floor)
            if miss_start <= miss_end:
                rest_requests += 1
                rows = self._rest_fetch_range(
                    market_n,
                    symbol_n,
                    miss_start,
                    miss_end,
                    fetcher=rest_fetcher,
                    pause_s=pause_s,
                    timeout=timeout,
                )
                fetched_trade_count += len(rows)
                rows_to_store = [t for t in rows if int(t.ts_ms) <= int(miss_end)]
                inserted_trade_count += self.insert_trades(market_n, symbol_n, rows_to_store, source="rest")
                if rows:
                    actual_end = min(max(int(t.ts_ms) for t in rows), int(miss_end))
                    # The requested start was verified even if the first trade arrived later.
                    # Record the requested start through the last returned trade.
                    self.record_coverage(
                        market_n,
                        symbol_n,
                        miss_start,
                        actual_end,
                        source="rest",
                        note="rest-range",
                    )
                    notes.append(f"rest:{miss_start}-{actual_end}")
                else:
                    notes.append(f"rest:{miss_start}-{miss_end}:empty")

        intervals = self.coverage_intervals(market_n, symbol_n)
        covered = self.coverage_covers(market_n, symbol_n, start, end)
        if not covered:
            notes.append("coverage still incomplete after ensure")
        return EnsureCoverageResult(
            market=market_n,
            symbol=symbol_n,
            requested_start_ms=start,
            requested_end_ms=end,
            covered=covered,
            missing_ranges=self.missing_ranges(market_n, symbol_n, start, end),
            fetched_trade_count=fetched_trade_count,
            inserted_trade_count=inserted_trade_count,
            archive_requests=archive_requests,
            rest_requests=rest_requests,
            coverage_intervals=intervals,
            notes=notes,
        )

    def ingest_ws_message(self, market: str, symbol: str, data: dict, *, source: str = "ws") -> bool:
        try:
            raw_id = data["a"] if "a" in data else data["t"]
            trade = AggTrade(
                agg_id=int(raw_id),
                price=float(data["p"]),
                qty=float(data["q"]),
                ts_ms=int(data.get("T") or data.get("E") or 0),
                first_trade_id=int(raw_id),
                last_trade_id=int(raw_id),
                id_domain="aggtrade" if "a" in data else "trade",
            )
        except (KeyError, TypeError, ValueError):
            return False
        inserted = self.insert_trades(market, symbol, [trade], source=source)
        return inserted > 0

    def stats(self, market: str, symbol: str) -> dict:
        first_ts, last_ts, n = self.query_trades_minmax(market, symbol)
        return {
            "path": str(self.path),
            "market": self._normalize_market(market),
            "symbol": self._normalize_symbol(symbol),
            "rows": n,
            "first_timestamp_ms": first_ts,
            "last_timestamp_ms": last_ts,
            "coverage_intervals": [iv.__dict__ for iv in self.coverage_intervals(market, symbol)],
        }
