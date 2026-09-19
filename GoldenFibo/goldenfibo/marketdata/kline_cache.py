"""Persistent Binance Spot kline cache (SQLite).

Cache is DATA-only: never fabricates candles; never changes OHLCV values.
GoldenFibo math is unaffected — same row fields reach feeders/metrics.
"""
from __future__ import annotations

import os
import sqlite3
import time
import urllib.parse
import urllib.request
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .binance_klines_range import (
    BinancePublicKlineSource,
    fetch_klines_range,
    inclusive_open_range_fetch_end,
    iter_klines_pages,
)
from .binance_public import BINANCE_SPOT_REST

BINANCE_USDM_REST = "https://fapi.binance.com"
from .timeframes import interval_ms, validate_interval

FetchFn = Callable[[str], list]

DEFAULT_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "backtest_klines.sqlite"
SOURCE = "binance"
MARKET_SPOT = "spot"

# Refresh the most recent N ms of closed history from network in AUTO mode.
DEFAULT_REFRESH_TAIL_MS = 24 * 60 * 60 * 1000  # 1 day


class CachePolicy(str, Enum):
    AUTO = "AUTO"          # cache + fill gaps + refresh recent boundary
    REFRESH = "REFRESH"    # re-download range and upsert cache
    BYPASS = "BYPASS"      # network only; no read/write


@dataclass
class CacheStats:
    bars_from_cache: int = 0
    bars_downloaded: int = 0
    bars_total: int = 0
    rest_pages: int = 0
    gaps_filled: int = 0
    gaps_remaining: int = 0
    refresh_window_ms: int = 0
    cache_read_ms: float = 0.0
    download_ms: float = 0.0
    policy: str = CachePolicy.AUTO.value
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict:
        return {
            "bars_from_cache": self.bars_from_cache,
            "bars_downloaded": self.bars_downloaded,
            "bars_total": self.bars_total,
            "rest_pages": self.rest_pages,
            "gaps_filled": self.gaps_filled,
            "gaps_remaining": self.gaps_remaining,
            "refresh_window_ms": self.refresh_window_ms,
            "cache_read_ms": round(self.cache_read_ms, 2),
            "download_ms": round(self.download_ms, 2),
            "policy": self.policy,
            "notes": list(self.notes),
        }


def _row_from_binance(k: Sequence) -> Tuple:
    """Map Binance kline array → DB tuple (strings preserve precision)."""
    # Binance: 0 ot, 1 o, 2 h, 3 l, 4 c, 5 vol, 6 ct, 7 qv, 8 trades, 9 tb_base, 10 tb_quote, 11 ignore
    def s(i, default="0"):
        return str(k[i]) if len(k) > i and k[i] is not None else default

    return (
        int(k[0]),
        s(1),
        s(2),
        s(3),
        s(4),
        s(5),
        int(k[6]) if len(k) > 6 else int(k[0]),
        s(7, s(5)),
        int(k[8]) if len(k) > 8 else 0,
        s(9, "0"),
        s(10, "0"),
    )


def _binance_from_row(r: sqlite3.Row | Tuple) -> list:
    """DB row → Binance-shaped list (same indices as REST)."""
    if isinstance(r, sqlite3.Row):
        ot, o, h, l, c, vol, ct, qv, n, tb, tq = (
            r["open_time"],
            r["open"],
            r["high"],
            r["low"],
            r["close"],
            r["volume"],
            r["close_time"],
            r["quote_volume"],
            r["trades"],
            r["taker_buy_base"],
            r["taker_buy_quote"],
        )
    else:
        ot, o, h, l, c, vol, ct, qv, n, tb, tq = r
    return [int(ot), o, h, l, c, vol, int(ct), qv, int(n or 0), tb, tq, "0"]


class KlineCache:
    """SQLite cache keyed by (source, market, symbol, timeframe, open_time)."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_CACHE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candles (
                    source TEXT NOT NULL,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    open_time INTEGER NOT NULL,
                    open TEXT NOT NULL,
                    high TEXT NOT NULL,
                    low TEXT NOT NULL,
                    close TEXT NOT NULL,
                    volume TEXT NOT NULL,
                    close_time INTEGER NOT NULL,
                    quote_volume TEXT NOT NULL,
                    trades INTEGER NOT NULL DEFAULT 0,
                    taker_buy_base TEXT NOT NULL DEFAULT '0',
                    taker_buy_quote TEXT NOT NULL DEFAULT '0',
                    PRIMARY KEY (source, market, symbol, timeframe, open_time)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_candles_range
                ON candles (source, market, symbol, timeframe, open_time)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS availability (
                    source TEXT NOT NULL,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    first_available_open_time INTEGER,
                    last_available_open_time INTEGER,
                    PRIMARY KEY (source, market, symbol, timeframe)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_availability_key
                ON availability (source, market, symbol, timeframe)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS availability_checks (
                    source TEXT NOT NULL,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    checked_at_ms INTEGER NOT NULL,
                    checked_start_ms INTEGER NOT NULL,
                    checked_end_ms INTEGER NOT NULL,
                    observed_first_open_time INTEGER,
                    observed_last_open_time INTEGER,
                    response_status TEXT NOT NULL,
                    response_note TEXT NOT NULL,
                    PRIMARY KEY (source, market, symbol, timeframe, checked_start_ms, checked_end_ms)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_availability_checks_key
                ON availability_checks (source, market, symbol, timeframe, checked_at_ms)
                """
            )

    def stats_for(self, symbol: str, timeframe: str, *, market: str = MARKET_SPOT) -> Dict:
        symbol = symbol.upper().replace("/", "")
        timeframe = validate_interval(timeframe)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n,
                       MIN(open_time) AS first_ot,
                       MAX(open_time) AS last_ot
                FROM candles
                WHERE source=? AND market=? AND symbol=? AND timeframe=?
                """,
                (SOURCE, market, symbol, timeframe),
            ).fetchone()
            avail = conn.execute(
                """
                SELECT first_available_open_time, last_available_open_time
                FROM availability
                WHERE source=? AND market=? AND symbol=? AND timeframe=?
                """,
                (SOURCE, market, symbol, timeframe),
            ).fetchone()
        n = int(row["n"] or 0)
        return {
            "path": str(self.path),
            "source": SOURCE,
            "market": market,
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": n,
            "first_open_time": row["first_ot"],
            "last_open_time": row["last_ot"],
            "first_available_open_time": avail["first_available_open_time"] if avail else None,
            "last_available_open_time": avail["last_available_open_time"] if avail else None,
            "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def get_availability(self, symbol: str, timeframe: str, *, market: str = MARKET_SPOT) -> Dict:
        symbol = symbol.upper().replace("/", "")
        timeframe = validate_interval(timeframe)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT first_available_open_time, last_available_open_time
                FROM availability
                WHERE source=? AND market=? AND symbol=? AND timeframe=?
                """,
                (SOURCE, market, symbol, timeframe),
            ).fetchone()
        if row is None:
            return {
                "source": SOURCE,
                "market": market,
                "symbol": symbol,
                "timeframe": timeframe,
                "first_available_open_time": None,
                "last_available_open_time": None,
            }
        return {
            "source": SOURCE,
            "market": market,
            "symbol": symbol,
            "timeframe": timeframe,
            "first_available_open_time": row["first_available_open_time"],
            "last_available_open_time": row["last_available_open_time"],
        }

    def set_availability(
        self,
        symbol: str,
        timeframe: str,
        *,
        market: str = MARKET_SPOT,
        first_available_open_time: Optional[int] = None,
        last_available_open_time: Optional[int] = None,
    ) -> None:
        symbol = symbol.upper().replace("/", "")
        timeframe = validate_interval(timeframe)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO availability (
                    source, market, symbol, timeframe,
                    first_available_open_time, last_available_open_time
                ) VALUES (?,?,?,?,?,?)
                ON CONFLICT(source, market, symbol, timeframe) DO UPDATE SET
                    first_available_open_time=excluded.first_available_open_time,
                    last_available_open_time=excluded.last_available_open_time
                """,
                (SOURCE, market, symbol, timeframe, first_available_open_time, last_available_open_time),
            )

    def record_availability_check(
        self,
        symbol: str,
        timeframe: str,
        *,
        market: str = MARKET_SPOT,
        checked_start_ms: int,
        checked_end_ms: int,
        observed_first_open_time: Optional[int],
        observed_last_open_time: Optional[int],
        response_status: str,
        response_note: str,
    ) -> None:
        symbol = symbol.upper().replace("/", "")
        timeframe = validate_interval(timeframe)
        now_ms = int(time.time() * 1000)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO availability_checks (
                    source, market, symbol, timeframe,
                    checked_at_ms, checked_start_ms, checked_end_ms,
                    observed_first_open_time, observed_last_open_time,
                    response_status, response_note
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    SOURCE,
                    market,
                    symbol,
                    timeframe,
                    now_ms,
                    int(checked_start_ms),
                    int(checked_end_ms),
                    observed_first_open_time,
                    observed_last_open_time,
                    response_status,
                    response_note,
                ),
            )

    def discover_first_available_open_time(
        self,
        symbol: str,
        timeframe: str,
        *,
        market: str = MARKET_SPOT,
        fetch: Optional[FetchFn] = None,
        base_url: str | None = None,
        max_years_back: int = 10,
    ) -> Optional[int]:
        symbol = symbol.upper().replace("/", "")
        timeframe = validate_interval(timeframe)
        current = self.get_availability(symbol, timeframe, market=market).get("first_available_open_time")
        if current is not None:
            return int(current)
        base_url = base_url or (BINANCE_USDM_REST if market == "futures" else BINANCE_SPOT_REST)
        step = interval_ms(timeframe)
        now_ms = int(time.time() * 1000)

        def _fetch_json(url: str):
            if fetch is not None:
                return fetch(url)
            req = urllib.request.Request(url, headers={"User-Agent": "GoldenFibo/availability/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())

        # Binance klines return the first candle at/after startTime. A single
        # request from time 0 is usually enough to get the earliest available
        # candle cheaply and deterministically. If the symbol is truly absent,
        # Binance returns an empty list.
        qs = (
            f"symbol={urllib.parse.quote(symbol)}&interval={urllib.parse.quote(timeframe)}"
            f"&startTime=0&endTime={now_ms}&limit=1"
        )
        url = f"{base_url}/fapi/v1/klines?{qs}" if market == "futures" else f"{base_url}/api/v3/klines?{qs}"
        try:
            data = _fetch_json(url)
        except Exception as exc:
            self.record_availability_check(symbol, timeframe, market=market, checked_start_ms=0, checked_end_ms=now_ms, observed_first_open_time=None, observed_last_open_time=None, response_status="error", response_note=str(exc))
            return None
        if isinstance(data, list) and data:
            first = int(data[0][0])
            last = int(data[-1][0])
            self.record_availability_check(symbol, timeframe, market=market, checked_start_ms=0, checked_end_ms=now_ms, observed_first_open_time=first, observed_last_open_time=last, response_status="ok", response_note="direct_earliest_probe")
            self.set_availability(symbol, timeframe, market=market, first_available_open_time=first, last_available_open_time=last)
            return first
        self.record_availability_check(symbol, timeframe, market=market, checked_start_ms=0, checked_end_ms=now_ms, observed_first_open_time=None, observed_last_open_time=None, response_status="empty", response_note="direct_earliest_probe_empty")
        return None

    def clear(self, symbol: str, timeframe: str, *, market: str = MARKET_SPOT) -> int:
        symbol = symbol.upper().replace("/", "")
        timeframe = validate_interval(timeframe)
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM candles WHERE source=? AND market=? AND symbol=? AND timeframe=?",
                (SOURCE, market, symbol, timeframe),
            )
            return int(cur.rowcount)

    def read_range(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
        *,
        market: str = MARKET_SPOT,
    ) -> List[list]:
        """Return cached klines in half-open [start_ms, end_ms) ordered by open_time."""
        symbol = symbol.upper().replace("/", "")
        timeframe = validate_interval(timeframe)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT open_time, open, high, low, close, volume, close_time,
                       quote_volume, trades, taker_buy_base, taker_buy_quote
                FROM candles
                WHERE source=? AND market=? AND symbol=? AND timeframe=?
                  AND open_time >= ? AND open_time < ?
                ORDER BY open_time ASC
                """,
                (SOURCE, market, symbol, timeframe, int(start_ms), int(end_ms)),
            ).fetchall()
        return [_binance_from_row(r) for r in rows]

    def upsert_klines(
        self,
        symbol: str,
        timeframe: str,
        klines: Sequence[Sequence],
        *,
        market: str = MARKET_SPOT,
    ) -> int:
        if not klines:
            return 0
        symbol = symbol.upper().replace("/", "")
        timeframe = validate_interval(timeframe)
        rows = []
        for k in klines:
            ot, o, h, l, c, vol, ct, qv, n, tb, tq = _row_from_binance(k)
            rows.append(
                (SOURCE, market, symbol, timeframe, ot, o, h, l, c, vol, ct, qv, n, tb, tq)
            )
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO candles (
                    source, market, symbol, timeframe, open_time,
                    open, high, low, close, volume, close_time,
                    quote_volume, trades, taker_buy_base, taker_buy_quote
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source, market, symbol, timeframe, open_time) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    close_time=excluded.close_time,
                    quote_volume=excluded.quote_volume,
                    trades=excluded.trades,
                    taker_buy_base=excluded.taker_buy_base,
                    taker_buy_quote=excluded.taker_buy_quote
                """,
                rows,
            )
        return len(rows)

    def find_missing_ranges(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
        *,
        market: str = MARKET_SPOT,
    ) -> List[Tuple[int, int]]:
        """Return half-open missing ranges within [start_ms, end_ms).

        Does not invent candles for exchange-empty periods; caller downloads each
        range and re-checks. Gaps are by expected open_time step only.
        """
        step = interval_ms(timeframe)
        if end_ms <= start_ms:
            return []
        cached = self.read_range(symbol, timeframe, start_ms, end_ms, market=market)
        have = {int(k[0]) for k in cached}
        missing_ranges: List[Tuple[int, int]] = []
        gap_start: Optional[int] = None
        t = int(start_ms)
        # Align start to step boundary relative to start_ms (caller should pass aligned)
        while t < end_ms:
            if t not in have:
                if gap_start is None:
                    gap_start = t
            else:
                if gap_start is not None:
                    missing_ranges.append((gap_start, t))
                    gap_start = None
            t += step
        if gap_start is not None:
            missing_ranges.append((gap_start, int(end_ms)))
        return missing_ranges


def validate_klines_sequence(
    klines: Sequence[Sequence],
    *,
    timeframe: str,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    allow_exchange_gaps: bool = True,
) -> Dict:
    """Validate order, dupes, step continuity. Does not fabricate fills."""
    step = interval_ms(timeframe)
    notes: List[str] = []
    if not klines:
        return {"ok": True, "count": 0, "dupes": 0, "out_of_order": 0, "internal_gaps": 0, "notes": notes}
    times = [int(k[0]) for k in klines]
    dupes = len(times) - len(set(times))
    out_of_order = sum(1 for i in range(1, len(times)) if times[i] < times[i - 1])
    gaps = 0
    for i in range(1, len(times)):
        dt = times[i] - times[i - 1]
        if dt != step:
            if dt > step:
                gaps += (dt // step) - 1
            elif dt <= 0:
                pass
            else:
                notes.append(f"unexpected step {dt} at {times[i]}")
    if start_ms is not None and times and times[0] != int(start_ms):
        notes.append(f"first_open {times[0]} != start {start_ms}")
    if end_ms is not None and times and times[-1] >= int(end_ms):
        notes.append(f"last_open {times[-1]} not < end {end_ms}")
    ok = dupes == 0 and out_of_order == 0
    if not allow_exchange_gaps and gaps:
        ok = False
    return {
        "ok": ok,
        "count": len(klines),
        "dupes": dupes,
        "out_of_order": out_of_order,
        "internal_gaps": gaps,
        "first_open": times[0] if times else None,
        "last_open": times[-1] if times else None,
        "notes": notes,
    }


def merge_klines(*parts: Sequence[Sequence]) -> List[list]:
    """Dedupe by open_time (last write wins), sort ascending."""
    by_ot: Dict[int, list] = {}
    for part in parts:
        for k in part:
            by_ot[int(k[0])] = list(k)
    return [by_ot[t] for t in sorted(by_ot)]


@dataclass
class RangeFetchResult:
    klines: List[list]
    stats: CacheStats


def fetch_range_cached(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    cache: Optional[KlineCache] = None,
    policy: CachePolicy | str = CachePolicy.AUTO,
    closed_only_before_ms: Optional[int] = None,
    refresh_tail_ms: int = DEFAULT_REFRESH_TAIL_MS,
    base_url: str | None = BINANCE_SPOT_REST,
    fetch: Optional[FetchFn] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
    market: str = MARKET_SPOT,
    sleep_s: float = 0.05,
) -> RangeFetchResult:
    """Cache-first historical range for half-open [start_ms, end_ms).

    Only closed candles (open+interval <= closed_only_before_ms when set) are
    written to cache. Forming candles are never stored as final.
    """
    policy = CachePolicy(policy)
    if base_url is None:
        base_url = BINANCE_USDM_REST if market == "futures" else BINANCE_SPOT_REST
    elif market == "futures" and base_url == BINANCE_SPOT_REST:
        base_url = BINANCE_USDM_REST
    interval = validate_interval(interval)
    symbol = symbol.upper().replace("/", "")
    step = interval_ms(interval)
    stats = CacheStats(policy=policy.value, refresh_window_ms=refresh_tail_ms if policy is CachePolicy.AUTO else 0)
    cache = cache or KlineCache()

    # Effective end for CLOSED bars only when fence provided
    closed_end = end_ms
    if closed_only_before_ms is not None:
        # last closed open = floor((closed_only-1)/step)*step but end_ms already hist fence
        closed_end = min(end_ms, int(closed_only_before_ms))

    requested_candles = max(0, (int(end_ms) - int(start_ms)) // step)

    def emit(stage: str, **extra):
        if on_progress:
            d = {"stage": stage, "stats": stats.as_dict()}
            d.update(extra)
            on_progress(d)

    missing_total = 0
    download_done = 0
    download_total = 0
    cached_candles = 0
    final_candles = 0

    def _fetch_url(url: str, retries: int = 5) -> list:
        from .binance_klines_range import _default_fetch
        fn = fetch or _default_fetch
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                return fn(url)
            except Exception as exc:  # network/rate-limit
                last_exc = exc
                time.sleep(min(30.0, 0.5 * (2 ** attempt)))
        assert last_exc is not None
        raise last_exc

    def download(a: int, b: int, *, store: bool = False) -> List[list]:
        """Download [a,b) page-by-page with retries; optionally upsert closed bars."""
        if b <= a:
            return []
        t_dl = time.perf_counter()
        out: List[list] = []
        pages = 0
        for page in iter_klines_pages(
            symbol,
            interval,
            a,
            b,
            base_url=base_url,
            fetch=_fetch_url,
            closed_only_before_ms=closed_only_before_ms,
            sleep_s=sleep_s,
        ):
            pages += 1
            out.extend(page)
            stats.rest_pages += 1
            stats.bars_downloaded += len(page)
            if store:
                if closed_only_before_ms is not None:
                    store_rows = [k for k in page if int(k[0]) + step <= int(closed_only_before_ms)]
                else:
                    store_rows = page
                if store_rows:
                    cache.upsert_klines(symbol, interval, store_rows, market=market)
            emit(
                "downloading_gaps",
                requested_candles=requested_candles,
                cached_candles=stats.bars_from_cache,
                missing_candles=missing_total,
                download_total=missing_total,
                download_done=stats.bars_downloaded,
                final_candles=stats.bars_total,
                bars_done=stats.bars_from_cache + stats.bars_downloaded,
                pages=stats.rest_pages,
                pct=min(99.0, 100.0 * stats.bars_downloaded / max(1, missing_total)),
                range_start=a,
                range_end=b,
                last_open_ms=int(page[-1][0]) if page else None,
            )
        stats.download_ms += (time.perf_counter() - t_dl) * 1000
        return out

    if policy is CachePolicy.BYPASS:
        emit("downloading_gaps")
        kl = download(start_ms, end_ms)
        # filter closed
        if closed_only_before_ms is not None:
            kl = [k for k in kl if int(k[0]) + step <= closed_only_before_ms or int(k[0]) < end_ms]
        stats.bars_total = len(kl)
        emit("cache_ready", bars_total=len(kl))
        return RangeFetchResult(klines=kl, stats=stats)

    if policy is CachePolicy.REFRESH:
        emit("downloading_gaps")
        kl = download(start_ms, end_ms)
        # store only closed
        to_store = kl
        if closed_only_before_ms is not None:
            to_store = [k for k in kl if int(k[0]) + step <= int(closed_only_before_ms)]
        cache.upsert_klines(symbol, interval, to_store, market=market)
        stats.bars_total = len(kl)
        stats.notes.append("REFRESH re-downloaded range")
        emit("cache_ready", bars_total=len(kl))
        return RangeFetchResult(klines=kl, stats=stats)

    # AUTO
    t_read = time.perf_counter()
    emit("loading_cache")
    initial_cached = cache.read_range(symbol, interval, start_ms, closed_end, market=market)
    avail = cache.get_availability(symbol, interval, market=market)
    first_available = avail.get("first_available_open_time")
    last_available = avail.get("last_available_open_time")
    if first_available is None and (not initial_cached or int(initial_cached[0][0]) > int(start_ms)):
        first_available = cache.discover_first_available_open_time(symbol, interval, market=market, fetch=fetch, base_url=base_url)
        avail = cache.get_availability(symbol, interval, market=market)
        first_available = avail.get("first_available_open_time") if first_available is None else first_available
        last_available = avail.get("last_available_open_time") if last_available is None else last_available
    effective_start = max(int(start_ms), int(first_available) if first_available is not None else int(start_ms))
    effective_end = int(closed_end)
    cached = cache.read_range(symbol, interval, effective_start, effective_end, market=market)
    stats.cache_read_ms = (time.perf_counter() - t_read) * 1000
    before_ots = {int(k[0]) for k in cached}
    stats.bars_from_cache = len(cached)
    missing = cache.find_missing_ranges(symbol, interval, effective_start, effective_end, market=market)
    missing_total = sum(max(0, (b - a) // step) for a, b in missing)
    available_expected = max(0, (effective_end - effective_start) // step)
    stats.bars_total = available_expected
    emit(
        "loading_cache",
        requested_candles=requested_candles,
        effective_available_start=effective_start,
        first_available_open_time=first_available,
        cached_candles=len(cached),
        missing_candles=missing_total,
        unavailable_candles=max(0, (effective_start - int(start_ms)) // step),
        bars_done=len(cached),
        bars_est=available_expected,
    )

    refresh_from = max(effective_start, effective_end - max(0, int(refresh_tail_ms)))
    download_ranges = list(missing)
    if refresh_tail_ms > 0 and refresh_from < effective_end:
        download_ranges.append((refresh_from, effective_end))
        stats.refresh_window_ms = effective_end - refresh_from

    # Split huge ranges into ~3-day chunks so one failure does not lose everything
    chunk_ms = 3 * 24 * 60 * 60 * 1000
    split_ranges: List[Tuple[int, int]] = []
    for a, b in _merge_ranges(download_ranges):
        cur = a
        while cur < b:
            nxt = min(b, cur + chunk_ms)
            split_ranges.append((cur, nxt))
            cur = nxt

    stats.bars_downloaded = 0
    stats.rest_pages = 0
    for a, b in split_ranges:
        part = download(a, b, store=True)
        if part:
            stats.gaps_filled += 1

    t_read2 = time.perf_counter()
    kl = cache.read_range(symbol, interval, effective_start, effective_end, market=market)
    stats.cache_read_ms += (time.perf_counter() - t_read2) * 1000
    # Keep half-open [start, end)
    kl = [k for k in kl if effective_start <= int(k[0]) < effective_end]
    if closed_only_before_ms is not None:
        kl = [k for k in kl if int(k[0]) + step <= int(closed_only_before_ms)]

    final_ots = {int(k[0]) for k in kl}
    stats.bars_from_cache = sum(1 for ot in final_ots if ot in before_ots and ot < refresh_from)
    stats.bars_downloaded = sum(1 for ot in final_ots if ot not in before_ots or ot >= refresh_from)
    stats.bars_total = len(kl)

    still_missing = cache.find_missing_ranges(symbol, interval, effective_start, effective_end, market=market)
    stats.gaps_remaining = sum(max(0, (b - a) // step) for a, b in still_missing)
    if still_missing:
        stats.notes.append(
            f"exchange gaps remain (not fabricated): {len(still_missing)} ranges, ~{stats.gaps_remaining} bars"
        )

    v = validate_klines_sequence(kl, timeframe=interval, start_ms=None, end_ms=effective_end)
    if v["dupes"] or v["out_of_order"]:
        stats.notes.append(f"validation issues: {v}")
    emit("cache_ready", bars_total=len(kl), validation=v)
    return RangeFetchResult(klines=kl, stats=stats)


def _merge_ranges(ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    if not ranges:
        return []
    ranges = sorted((int(a), int(b)) for a, b in ranges if b > a)
    out = [ranges[0]]
    for a, b in ranges[1:]:
        pa, pb = out[-1]
        if a <= pb:
            out[-1] = (pa, max(pb, b))
        else:
            out.append((a, b))
    return out


class CachedBinanceKlineSource:
    """HistoricalBarSource with optional SQLite cache in front of public REST."""

    def __init__(
        self,
        *,
        cache: Optional[KlineCache] = None,
        base_url: str | None = BINANCE_SPOT_REST,
        fetch: Optional[FetchFn] = None,
        policy: CachePolicy | str = CachePolicy.AUTO,
        refresh_tail_ms: int = DEFAULT_REFRESH_TAIL_MS,
    ) -> None:
        self.cache = cache or KlineCache()
        self.base_url = base_url
        self.fetch = fetch
        self.policy = CachePolicy(policy)
        self.refresh_tail_ms = refresh_tail_ms
        self.last_stats: Optional[CacheStats] = None

    def fetch_range(
        self,
        *,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        closed_only_before_ms: Optional[int] = None,
        on_page: Optional[Callable[[dict], None]] = None,
        policy: Optional[CachePolicy | str] = None,
        on_progress: Optional[Callable[[dict], None]] = None,
    ) -> List[list]:
        result = fetch_range_cached(
            symbol,
            interval,
            start_ms,
            end_ms,
            cache=self.cache,
            policy=policy or self.policy,
            closed_only_before_ms=closed_only_before_ms,
            refresh_tail_ms=self.refresh_tail_ms,
            base_url=self.base_url,
            fetch=self.fetch,
            on_progress=on_progress or on_page,
        )
        self.last_stats = result.stats
        return result.klines
