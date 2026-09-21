"""Public Binance compressed aggregate trades (REST, no auth).

Spot: GET /api/v3/aggTrades
  - startTime / endTime / fromId / limit (max 1000)
  - Paginate with fromId = last.a + 1 for long windows
  - No 1h restriction on modern spot API when using startTime alone + limit

USD-M futures (reference only): GET /fapi/v1/aggTrades — typically last ~48h.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, List, Optional, Sequence

from ..metrics.trade_vap import AggTrade

BINANCE_SPOT_REST = "https://api.binance.com"
BINANCE_USDM_REST = "https://fapi.binance.com"


def _get_json(url: str, *, timeout: float = 30.0, max_attempts: int = 8, pause_s: float = 1.0) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "GoldenFibo/0.2"})
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 public HTTPS
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code != 429 or attempt >= max_attempts:
                raise
            time.sleep(pause_s * attempt)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("unreachable")


def parse_agg_trade_row(row: object) -> AggTrade:
    """Parse REST object or list-shaped aggTrade into AggTrade."""
    if isinstance(row, dict):
        return AggTrade(
            agg_id=int(row["a"]),
            price=float(row["p"]),
            qty=float(row["q"]),
            ts_ms=int(row["T"]),
            first_trade_id=int(row.get("f", row["a"])),
            last_trade_id=int(row.get("l", row["a"])),
            id_domain="aggtrade",
            buyer_is_maker=bool(row.get("m")),
        )
    if isinstance(row, (list, tuple)) and len(row) >= 6:
        # uncommon; keep defensive
        return AggTrade(
            agg_id=int(row[0]),
            price=float(row[1]),
            qty=float(row[2]),
            ts_ms=int(row[5]),
            first_trade_id=int(row[3]) if len(row) > 3 else int(row[0]),
            last_trade_id=int(row[4]) if len(row) > 4 else int(row[0]),
            id_domain="aggtrade",
            buyer_is_maker=bool(row[6]) if len(row) > 6 else None,
        )
    raise TypeError(f"unexpected aggTrade row: {type(row)!r}")


def fetch_agg_trades_page(
    symbol: str,
    *,
    start_time_ms: Optional[int] = None,
    end_time_ms: Optional[int] = None,
    from_id: Optional[int] = None,
    limit: int = 1000,
    base_url: str = BINANCE_SPOT_REST,
    path: str = "/api/v3/aggTrades",
    timeout: float = 30.0,
    sleep_s: float = 0.0,
) -> List[AggTrade]:
    """Fetch one page of aggregate trades (max 1000)."""
    symbol = symbol.upper().replace("/", "")
    params: dict = {"symbol": symbol, "limit": int(min(1000, max(1, limit)))}
    if from_id is not None:
        params["fromId"] = int(from_id)
    else:
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        if end_time_ms is not None:
            params["endTime"] = int(end_time_ms)
    qs = urllib.parse.urlencode(params)
    url = f"{base_url}{path}?{qs}"
    if sleep_s > 0:
        time.sleep(sleep_s)
    data = _get_json(url, timeout=timeout)
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected aggTrades payload: {type(data)}")
    return [parse_agg_trade_row(row) for row in data]


def fetch_agg_trades_range(
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    market: str = "spot",
    base_url: str | None = None,
    path: str | None = None,
    limit: int = 1000,
    max_pages: int = 50_000,
    pause_s: float = 0.05,
    timeout: float = 30.0,
    fetch_page: Optional[Callable[..., List[AggTrade]]] = None,
) -> List[AggTrade]:
    """Download all aggTrades with T in [start_ms, end_ms] (inclusive).

    Spot uses /api/v3/aggTrades; USD-M futures uses /fapi/v1/aggTrades.
    Pagination continues with fromId = last.agg_id + 1 until past end_ms or empty.

    Raises on transport/API errors — does not invent trades.
    """
    start_ms = int(start_ms)
    end_ms = int(end_ms)
    if end_ms < start_ms:
        return []
    market_n = str(market or "spot").lower()
    if market_n in {"futures", "future", "usdm"}:
        base_url = base_url or BINANCE_USDM_REST
        path = path or "/fapi/v1/aggTrades"
        # USD-M futures aggTrades are limited to a recent 2-day search window.
        two_days_ms = 2 * 24 * 60 * 60 * 1000
        floor_ms = max(0, end_ms - two_days_ms)
        if start_ms < floor_ms:
            start_ms = floor_ms
    else:
        base_url = base_url or BINANCE_SPOT_REST
        path = path or "/api/v3/aggTrades"
    page_fn = fetch_page or fetch_agg_trades_page
    out: List[AggTrade] = []
    seen_ids: set[int] = set()

    page = page_fn(
        symbol,
        start_time_ms=start_ms,
        end_time_ms=None,
        from_id=None,
        limit=limit,
        base_url=base_url,
        path=path,
        timeout=timeout,
        sleep_s=0.0,
    )
    pages = 0
    while pages < max_pages:
        pages += 1
        if not page:
            break
        stop = False
        for t in page:
            if t.agg_id in seen_ids:
                continue
            seen_ids.add(t.agg_id)
            if t.ts_ms < start_ms:
                continue
            if t.ts_ms > end_ms:
                stop = True
                break
            out.append(t)
        if stop:
            break
        last = page[-1]
        if last.ts_ms > end_ms:
            break
        if len(page) < limit:
            # no more pages in this direction
            # but if last.ts still < end, try fromId continuation once more
            nxt = page_fn(
                symbol,
                from_id=last.agg_id + 1,
                limit=limit,
                base_url=base_url,
                path=path,
                timeout=timeout,
                sleep_s=pause_s,
            )
            if not nxt or nxt[0].agg_id <= last.agg_id:
                break
            page = nxt
            continue
        page = page_fn(
            symbol,
            from_id=last.agg_id + 1,
            limit=limit,
            base_url=base_url,
            path=path,
            timeout=timeout,
            sleep_s=pause_s,
        )
    out.sort(key=lambda t: (t.ts_ms, t.agg_id))
    return out


def coverage_from_fetch(
    trades: Sequence[AggTrade],
    *,
    requested_start_ms: int,
    requested_end_ms: int,
) -> tuple[Optional[int], bool]:
    """Return (earliest_trade_ts, looks_complete_at_start).

    Complete-at-start heuristic after a startTime-based fetch:
    first trade ts <= requested_start_ms + small slack OR trades empty with
    no evidence of a hard API floor (caller may still mark incomplete if a
    live buffer started late).
    """
    if not trades:
        return None, True  # empty market in range is complete coverage of "nothing"
    first = min(int(t.ts_ms) for t in trades)
    # Large positive gap with trades present only later often means API/history floor
    # or a truncated buffer. Caller decides with assess_trade_history_coverage.
    return first, first <= int(requested_start_ms)
