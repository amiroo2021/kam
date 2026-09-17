"""
Stage 2 replay — deterministic event reconstruction for the existing 30-day
FiboLearn DB.

For every stored market_observation, replay the original 1m kline through
GoldenFibo's read-only engine instances for all (percentage, direction)
families and persist the resulting domain_events back into market_json.

The replay is READ-ONLY against Binance / network; only the FiboLearn DB is
updated. Existing ladder_state, market_state and significant_levels are
preserved verbatim. Only `market_json.domain_events` is augmented.

This is a one-shot deterministic replay; it does NOT rebuild episodes.
Stage 4 runs the streaming episode builder afterwards.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

ROOT = Path("/root/kam")
REPORT = ROOT / "fibolearn/reports"
REPORT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))

from fibolearn.config.defaults import DEFAULT_PERCENTAGES, SYMBOL_TO_BINANCE_SPOT  # noqa: E402
from fibolearn.data.goldenfibo_data import fetch_range  # noqa: E402
from fibolearn.goldenfibo_compat import GF_ROOT  # noqa: E402  # noqa: F401
from goldenfibo.engine.config import EngineConfig, Side  # noqa: E402
from goldenfibo.engine.engine import GoldenFiboEngine  # noqa: E402
from goldenfibo.session.runner import apply_ohlc_page  # noqa: E402

DB = Path("/root/.hermes/fibolearn/fibolearn.sqlite")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def replay_one(conn: sqlite3.Connection, *, symbol: str, page_size: int = 1000) -> dict:
    """Replay one symbol's stored 1m candles and persist domain_events."""
    cur = conn.execute(
        "select id, timestamp_ms from market_observations where symbol=? order by timestamp_ms asc",
        (symbol,),
    )
    rows = [(int(r[0]), int(r[1])) for r in cur.fetchall()]
    if not rows:
        return {"symbol": symbol, "rows": 0, "events_added": 0}
    first_ts = rows[0][1]
    last_ts = rows[-1][1]

    # Pull the cached klines for this exact range. Uses the existing
    # GoldenFibo KlineCache (no network if cached).
    binance_symbol = SYMBOL_TO_BINANCE_SPOT.get(symbol) or (symbol + "USDT")
    candles = fetch_range(binance_symbol, "1m", first_ts, last_ts, policy="AUTO")
    by_ts = {int(k[0]): k for k in candles}

    # Fresh engines per family.
    engines: Dict[Tuple[str, str], GoldenFiboEngine] = {}
    for pct in DEFAULT_PERCENTAGES:
        for side in ("BUY", "SELL"):
            engines[(str(pct), side)] = GoldenFiboEngine(
                EngineConfig(side=Side(side), percentage=Decimal(str(pct)), symbol=symbol.upper())
            )

    # Apply each candle to each engine; record domain_events per candle.
    updates: Dict[int, Dict[str, list]] = {}
    for market_id, ts in rows:
        kline = by_ts.get(ts)
        if not kline:
            continue
        events_per_family: Dict[str, list] = {}
        for (pct, side), eng in engines.items():
            res = apply_ohlc_page(eng, [kline], mode=__import__("goldenfibo.engine.config", fromlist=["OhlcResolveMode"]).OhlcResolveMode.LEGACY)
            key = f"{pct}:{side}"
            events_per_family[key] = [e.kind.value for e in res.domain]
        updates[market_id] = events_per_family

    # Persist updates in a single transaction.
    conn.execute("BEGIN IMMEDIATE")
    written = 0
    for market_id, ts in rows:
        if market_id not in updates:
            continue
        r = conn.execute("select market_json from market_observations where id=?", (market_id,)).fetchone()
        if not r:
            continue
        try:
            d = json.loads(r[0])
        except Exception:
            continue
        d["domain_events"] = updates[market_id]
        new_json = json.dumps(d, sort_keys=True)
        conn.execute("update market_observations set market_json=? where id=?", (new_json, market_id))
        written += 1
    conn.execute("COMMIT")
    return {"symbol": symbol, "rows": written, "first_ts": first_ts, "last_ts": last_ts, "events_added": written}


def main() -> None:
    if not DB.exists():
        print("db not found", DB)
        sys.exit(1)
    sym_filter = set()
    if len(sys.argv) > 1:
        sym_filter = set(s for s in sys.argv[1].split(",") if s)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    syms = [r["symbol"] for r in conn.execute("select distinct symbol from market_observations order by symbol")]
    if sym_filter:
        syms = [s for s in syms if s in sym_filter]

    report: dict = {
        "started_at": _now(),
        "db": str(DB),
        "symbols": {},
    }
    for sym in syms:
        ts = _now()
        print(f"[{ts}] replaying {sym} ...", flush=True)
        t0 = time.perf_counter()
        try:
            r = replay_one(conn, symbol=sym)
        except Exception as e:
            r = {"symbol": sym, "error": str(e)}
        r["elapsed_s"] = round(time.perf_counter() - t0, 3)
        r["ended_at"] = _now()
        report["symbols"][sym] = r
        print(f"  -> {r}", flush=True)
    report["ended_at"] = _now()
    conn.close()
    out = REPORT / "phase3a_replay_report.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True))
    print("wrote", out)


if __name__ == "__main__":
    main()
