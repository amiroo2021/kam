"""Hyperliquid public-mid poller for Golden Fibo paper engines."""

from __future__ import annotations

import argparse
import time
from typing import Dict, Iterable

import requests

from .multi_engine import GoldenFiboMultiEngine, MultiEngineConfig

HL_INFO_URL = "https://api.hyperliquid.xyz/info"


def fetch_hl_mids(timeout: float = 10.0) -> Dict[str, float]:
    """Fetch Hyperliquid public mids. Returns {coin: mid}."""
    r = requests.post(HL_INFO_URL, json={"type": "allMids"}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return {str(k): float(v) for k, v in data.items()}


def run_paper_loop(symbols: list[str], bot: str = "bgf", interval: float = 60.0) -> None:
    engine = GoldenFiboMultiEngine(MultiEngineConfig(symbols=symbols, bot=bot))
    while True:
        mids = fetch_hl_mids()
        engine.set_quotes_from_mids({s: mids[s] for s in symbols if s in mids})
        for line in engine.tick_all(paper_backfill=True):
            print(line, flush=True)
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Golden Fibo virtual paper bot using Hyperliquid public mids")
    p.add_argument("--bot", choices=["bgf", "sgf"], default="bgf")
    p.add_argument("--symbols", nargs="+", required=True, help="Coins, e.g. BTC ETH SOL")
    p.add_argument("--interval", type=float, default=60.0)
    args = p.parse_args(argv)
    run_paper_loop(args.symbols, bot=args.bot, interval=args.interval)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
