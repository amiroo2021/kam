"""Paginated Binance public kline range download."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

from .binance_public import BINANCE_SPOT_REST
from .timeframes import interval_ms, validate_interval


def inclusive_open_range_fetch_end(end_open_ms: int, interval: str) -> int:
    """User End is inclusive by candle open → half-open fetch end = End + timeframe."""
    return int(end_open_ms) + interval_ms(interval)


FetchFn = Callable[[str], list]  # full URL → parsed JSON list


def _default_fetch(url: str, timeout: float = 30.0) -> list:
    req = urllib.request.Request(url, headers={"User-Agent": "GoldenFibo/0.3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 public HTTPS
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code in (418, 429):
            raise RuntimeError(f"binance rate limited HTTP {exc.code}") from exc
        raise
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected klines payload: {type(data)}")
    return data


def fetch_klines_range(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    limit: int = 1000,
    base_url: str = BINANCE_SPOT_REST,
    sleep_s: float = 0.05,
    max_pages: int = 5000,
    fetch: Optional[FetchFn] = None,
    closed_only_before_ms: Optional[int] = None,
    on_page: Optional[Callable[[dict], None]] = None,
) -> List[list]:
    """Download klines covering half-open [start_ms, end_ms) with pagination (limit≤1000).

    For user-facing BACKTEST inclusive open range [start_open, end_open], pass
    end_ms = inclusive_open_range_fetch_end(end_open, interval).

    - Dedupes by open_time.
    - Advances cursor by last_open + interval_ms.
    - If closed_only_before_ms is set, drops bars whose open_time + interval > that bound
      (i.e. still forming relative to that clock).
    - on_page(dict) called after each REST page (progress only; does not change data).
    """
    out: List[list] = []
    for page in iter_klines_pages(
        symbol,
        interval,
        start_ms,
        end_ms,
        limit=limit,
        base_url=base_url,
        sleep_s=sleep_s,
        max_pages=max_pages,
        fetch=fetch,
        closed_only_before_ms=closed_only_before_ms,
        on_page=on_page,
    ):
        out.extend(page)
    return out


def iter_klines_pages(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    limit: int = 1000,
    base_url: str = BINANCE_SPOT_REST,
    sleep_s: float = 0.05,
    max_pages: int = 5000,
    fetch: Optional[FetchFn] = None,
    closed_only_before_ms: Optional[int] = None,
    on_page: Optional[Callable[[dict], None]] = None,
    accumulate: bool = False,
) -> Iterator[list]:
    """Yield each page of klines (deduped, ordered within page by open_time).

    When accumulate=True, also builds full list via successive yields of *new rows only*
    (same as fetch_klines_range consumer: for page in iter: out.extend(page)).
    """
    if end_ms <= start_ms:
        return
        yield  # pragma: no cover — make this a generator
    interval = validate_interval(interval)
    symbol = symbol.upper().replace("/", "")
    limit = max(1, min(1000, int(limit)))
    step = interval_ms(interval)
    fetch = fetch or (lambda url: _default_fetch(url))
    est_bars = max(1, (int(end_ms) - int(start_ms)) // step)

    seen: set[int] = set()
    cursor = start_ms
    pages = 0
    bars_done = 0

    while cursor < end_ms and pages < max_pages:
        pages += 1
        qs = urllib.parse.urlencode(
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": int(cursor),
                "endTime": int(end_ms - 1),
                "limit": limit,
            }
        )
        url = f"{base_url}/api/v3/klines?{qs}"
        batch = fetch(url)
        if not batch:
            if on_page:
                on_page(
                    {
                        "pages": pages,
                        "bars_done": bars_done,
                        "bars_est": est_bars,
                        "pct": min(99.0, 100.0 * bars_done / est_bars),
                        "cursor_ms": cursor,
                        "done": True,
                    }
                )
            break
        page_rows: List[list] = []
        last_open = None
        for row in batch:
            ot = int(row[0])
            if ot < start_ms or ot >= end_ms:
                continue
            if ot in seen:
                continue
            if closed_only_before_ms is not None and ot + step > closed_only_before_ms:
                continue
            seen.add(ot)
            page_rows.append(row)
            last_open = ot
        page_rows.sort(key=lambda r: int(r[0]))
        bars_done += len(page_rows)
        if on_page:
            on_page(
                {
                    "pages": pages,
                    "bars_done": bars_done,
                    "bars_est": est_bars,
                    "pct": min(99.0, 100.0 * bars_done / est_bars),
                    "page_bars": len(page_rows),
                    "cursor_ms": cursor,
                    "last_open_ms": last_open,
                }
            )
        if page_rows:
            yield page_rows
        if last_open is None:
            break
        nxt = last_open + step
        if nxt <= cursor:
            break
        cursor = nxt
        if len(batch) < limit:
            # last partial page
            if len(page_rows) == 0:
                break
            # may still need another page if filtered; if batch short usually done
            if len(batch) < limit and last_open + step >= end_ms:
                break
        if sleep_s > 0 and len(batch) >= limit:
            time.sleep(sleep_s)


class BinancePublicKlineSource:
    """HistoricalBarSource backed by public Spot klines."""

    def __init__(self, *, base_url: str = BINANCE_SPOT_REST, fetch: Optional[FetchFn] = None) -> None:
        self.base_url = base_url
        self.fetch = fetch

    def fetch_range(
        self,
        *,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        closed_only_before_ms: Optional[int] = None,
        on_page: Optional[Callable[[dict], None]] = None,
    ) -> List[list]:
        return fetch_klines_range(
            symbol,
            interval,
            start_ms,
            end_ms,
            base_url=self.base_url,
            fetch=self.fetch,
            closed_only_before_ms=closed_only_before_ms,
            on_page=on_page,
        )

    def iter_pages(
        self,
        *,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        closed_only_before_ms: Optional[int] = None,
        on_page: Optional[Callable[[dict], None]] = None,
        sleep_s: float = 0.05,
    ) -> Iterator[list]:
        return iter_klines_pages(
            symbol,
            interval,
            start_ms,
            end_ms,
            base_url=self.base_url,
            fetch=self.fetch,
            closed_only_before_ms=closed_only_before_ms,
            on_page=on_page,
            sleep_s=sleep_s,
        )
