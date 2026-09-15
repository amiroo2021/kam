"""Paginated Binance public kline range download."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, List, Optional, Sequence

from .binance_public import BINANCE_SPOT_REST
from .timeframes import interval_ms, validate_interval

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
) -> List[list]:
    """Download klines covering [start_ms, end_ms) with pagination (limit≤1000).

    - Dedupes by open_time.
    - Advances cursor by last_open + interval_ms.
    - If closed_only_before_ms is set, drops bars whose open_time + interval > that bound
      (i.e. still forming relative to that clock).
    """
    if end_ms <= start_ms:
        return []
    interval = validate_interval(interval)
    symbol = symbol.upper().replace("/", "")
    limit = max(1, min(1000, int(limit)))
    step = interval_ms(interval)
    fetch = fetch or (lambda url: _default_fetch(url))

    out: List[list] = []
    seen: set[int] = set()
    cursor = start_ms
    pages = 0

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
            break
        advanced = False
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
            out.append(row)
            last_open = ot
            advanced = True
        if last_open is None:
            break
        nxt = last_open + step
        if nxt <= cursor:
            break
        cursor = nxt
        if not advanced and len(batch) < limit:
            break
        if sleep_s > 0 and len(batch) >= limit:
            time.sleep(sleep_s)

    out.sort(key=lambda r: int(r[0]))
    return out


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
    ) -> List[list]:
        return fetch_klines_range(
            symbol,
            interval,
            start_ms,
            end_ms,
            base_url=self.base_url,
            fetch=self.fetch,
            closed_only_before_ms=closed_only_before_ms,
        )
