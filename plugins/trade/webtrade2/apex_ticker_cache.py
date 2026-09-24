"""
Apex ticker enrichment cache for WebTrade2.

Apex does not expose a bulk 24h ticker endpoint — the only path is
``GET /api/v3/ticker?symbol=<X>`` per symbol. With ~50-100 perp
contracts this would be a sequential fan-out of 50-100 HTTP requests on
every market-list refresh.

This module:

  * Fetches Apex ticker data **concurrently** with bounded parallelism
    using a ``ThreadPoolExecutor`` (FastAPI handlers are sync).
  * Caches the merged snapshot for ``APEX_TICKER_TTL_SECONDS`` so a normal
    browser refresh uses the cache and does NOT re-fan-out.
  * Tolerates per-symbol failures (each symbol has its own try/except;
    one failure does not fail the batch).
  * Surfaces per-symbol data: markPrice/lastPrice, price24hPcnt,
    turnover24h, volume24h, fundingRate, openInterest (raw + notional).

It is intentionally exchange-scoped (Apex only). Other exchanges with
true bulk ticker endpoints continue to return their data inline.

No secrets are read or logged. The cache is in-process and does not
persist across restarts.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("webtrade2.apex_ticker_cache")

APEX_TICKER_TTL_SECONDS = 30.0
APEX_TICKER_MAX_CONCURRENCY = 10        # bounded concurrent ticker fetches
APEX_TICKER_PER_REQUEST_TIMEOUT_S = 8.0 # hard ceiling per ticker fetch


def _sanitize_ticker(out: Any) -> Dict[str, Any]:
    """Coerce ticker dict into the API contract: numeric strings → float, no secrets."""
    if not isinstance(out, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for k, v in out.items():
        # Strip nested objects; only flat fields allowed.
        if isinstance(v, (list, dict)):
            continue
        cleaned[str(k)] = v
    # Coerce the canonical numeric fields.
    for k in ("markPrice", "lastPrice", "oraclePrice", "indexPrice",
              "price24hPcnt", "turnover24h", "volume24h", "fundingRate",
              "openInterest", "openInterestValue", "openInterestNotional",
              "high24h", "low24h", "bestBid", "bestAsk"):
        if k in cleaned:
            try:
                cleaned[k] = float(cleaned[k])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                cleaned.pop(k, None)
    return cleaned


class ApexTickerCache:
    """Thread-safe TTL cache for the merged Apex per-symbol ticker snapshot.

    The snapshot is keyed by the (exchange, account) pair. A `None` account
    means "any default".
    """

    def __init__(self,
                 ttl_seconds: float = APEX_TICKER_TTL_SECONDS,
                 max_concurrency: int = APEX_TICKER_MAX_CONCURRENCY,
                 per_request_timeout_s: float = APEX_TICKER_PER_REQUEST_TIMEOUT_S) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self.max_concurrency = max(1, int(max_concurrency))
        self.per_request_timeout_s = float(per_request_timeout_s)
        self._lock = threading.Lock()
        self._snapshot: Dict[Tuple[Optional[str], str], float] = {}  # ts_key → epoch
        self._data: Dict[Tuple[Optional[str], str], Dict[str, Dict[str, Any]]] = {}
        self._inflight: Dict[Tuple[Optional[str], str], threading.Event] = {}
        self._inflight_lock = threading.Lock()
        # Shared thread pool for ticker fetches. Sized to max_concurrency + a
        # little slack so the event loop is not blocked.
        self._pool = ThreadPoolExecutor(
            max_workers=max(self.max_concurrency + 2, 4),
            thread_name_prefix="apex-ticker",
        )

    # -- public API -----------------------------------------------------

    def get(self, exchange: str, account: Optional[str]) -> Optional[Dict[str, Dict[str, Any]]]:
        """Return cached data if fresh, else None."""
        key = (str(account or "") or None, str(exchange or "").lower())
        ts = self._snapshot.get(key)
        if ts is None:
            return None
        if (time.time() - ts) > self.ttl_seconds:
            return None
        with self._lock:
            return dict(self._data.get(key) or {})

    def age_seconds(self, exchange: str, account: Optional[str]) -> Optional[float]:
        key = (str(account or "") or None, str(exchange or "").lower())
        ts = self._snapshot.get(key)
        return None if ts is None else (time.time() - ts)

    def refresh_sync(
        self,
        exchange: str,
        account: Optional[str],
        symbols: List[str],
        fetch_one: Callable[[str], Optional[Dict[str, Any]]],
    ) -> Dict[str, Dict[str, Any]]:
        """Refetch the snapshot for ``(exchange, account)`` for every symbol.

        ``fetch_one(symbol)`` is a SYNC callable that returns either:
          * a dict with the ticker fields, OR
          * None / raises on failure (per-symbol failure is tolerated).

        Concurrent fan-out is bounded by ``max_concurrency``. Concurrent
        calls for the same key are coalesced. A fresh cache hit short-
        circuits fan-out entirely.
        """
        key = (str(account or "") or None, str(exchange or "").lower())

        # Fresh-cache short-circuit. If the snapshot is within TTL, return
        # it without fan-out. This is the path that protects the Apex
        # upstream from being hit on every browser refresh.
        cached = self.get(exchange, account)
        if cached is not None:
            return cached

        # Coalesce concurrent refreshes for the same key.
        with self._inflight_lock:
            ev = self._inflight.get(key)
            if ev is None:
                ev = threading.Event()
                self._inflight[key] = ev
                leader = True
            else:
                leader = False

        if not leader:
            # Wait for the leader to finish, then return whatever landed.
            ev.wait(timeout=self.per_request_timeout_s * (len(symbols) + 1))
            cached = self.get(exchange, account)
            return cached or {}

        # We're the leader. Refetch. Collect successful results into a
        # fresh dict; on success, merge with any prior snapshot so that a
        # partially-failed refresh does NOT wipe out previously-successful
        # rows (preserves last-known data on transient per-symbol errors).
        previous_snapshot = dict(self._data.get(key) or {})
        sem = threading.BoundedSemaphore(self.max_concurrency)
        results: Dict[str, Dict[str, Any]] = {}

        def _worker(sym: str) -> Tuple[str, Optional[Dict[str, Any]]]:
            with sem:
                t0 = time.perf_counter()
                try:
                    out = fetch_one(sym)
                except Exception as exc:  # noqa: BLE001
                    log.info("apex ticker %s failed: %s", sym, exc)
                    return (sym, None)
                dt = (time.perf_counter() - t0) * 1000
                if isinstance(out, dict):
                    log.debug("apex ticker %s ok in %.0fms", sym, dt)
                    return (sym, _sanitize_ticker(out))
                log.info("apex ticker %s returned %r (skipped)", sym, out)
                return (sym, None)
        try:
            futures = [self._pool.submit(_worker, s) for s in symbols]
            for fut in as_completed(futures):
                sym, payload = fut.result()
                if payload:
                    results[sym] = payload
            # Merge: every successful fetch overwrites its prior row; any
            # symbol missing from this refresh falls back to the previous
            # snapshot so stale-but-successful data survives a partial
            # outage. Then sort the symbol list so the next refresh is
            # against the same set of keys we already cached.
            merged = dict(previous_snapshot)
            for sym, row in results.items():
                merged[sym] = row
            with self._lock:
                self._data[key] = merged
                self._snapshot[key] = time.time()
            return dict(merged)
        finally:
            with self._inflight_lock:
                self._inflight.pop(key, None)
            ev.set()


def merge_ticker_into_rows(
    rows: List[Dict[str, Any]],
    ticker_map: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Attach ticker fields (price/24h/turnover/OI/funding) to each catalog row.

    Missing ticker data leaves the corresponding field as None so the
    frontend renders ``—`` and the row sorts to the bottom by turnover.
    """
    out: List[Dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or row.get("instrument") or "").strip()
        # Apex ticker rows use no-dash form (BTCUSDT) while the catalog may
        # use the same no-dash form, so try multiple normalizations to find
        # a matching ticker payload.
        tk = (
            ticker_map.get(symbol)
            or ticker_map.get(symbol.replace("-", ""))
            or ticker_map.get(symbol.replace("_", ""))
        )
        merged = dict(row)
        if tk:
            for k, v in tk.items():
                merged[k] = v
            merged["price"] = tk.get("markPrice") or tk.get("lastPrice")
            merged["change_24h"] = tk.get("price24hPcnt")
            merged["volume_24h"] = tk.get("turnover24h") or tk.get("volume24h")
            merged["turnover24h"] = tk.get("turnover24h")
            merged["openInterest"] = tk.get("openInterest")
            merged["fundingRate"] = tk.get("fundingRate")
            # Frontend's market-header renderer reads ``funding``; mirror
            # fundingRate into it so the display works on Apex.
            merged["funding"] = tk.get("fundingRate")
        else:
            # Leave the canonical fields as None; the frontend renders — and
            # the ranker sorts these rows to the bottom.
            merged.setdefault("price", None)
            merged.setdefault("change_24h", None)
            merged.setdefault("volume_24h", None)
            merged.setdefault("turnover24h", None)
            merged.setdefault("openInterest", None)
            merged.setdefault("fundingRate", None)
            merged.setdefault("funding", None)
        out.append(merged)
    return out


# Module-level singleton (one cache per process is sufficient).
_CACHE = ApexTickerCache()


def get_cache() -> ApexTickerCache:
    return _CACHE
