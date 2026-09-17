from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from fibolearn.collector.live_collector import CollectorSettings, FiboLearnCollector
from fibolearn.config.defaults import DEFAULT_PERCENTAGES, SYMBOL_TO_BINANCE_SPOT
from fibolearn.data.goldenfibo_data import fetch_range_cached
from goldenfibo.marketdata.kline_cache import KlineCache, CachePolicy
from fibolearn.storage.sqlite_store import FiboLearnStore


def build(store: FiboLearnStore, *, days: int, symbols, end_ms: int | None = None, chunk_days: int = 7) -> dict:
    end_ms = end_ms or (int(time.time() * 1000) // 60000) * 60000
    start_ms = end_ms - days * 24 * 60 * 60000
    collector = FiboLearnCollector(store, settings=CollectorSettings(percentages=DEFAULT_PERCENTAGES))
    report = {'start_ms': start_ms, 'end_ms': end_ms, 'chunk_days': chunk_days, 'symbols': {}, 'started_at_ms': int(time.time() * 1000)}
    cache = KlineCache()
    for symbol in symbols:
        binance_symbol = SYMBOL_TO_BINANCE_SPOT.get(symbol) or (symbol + 'USDT')
        sym_start = time.perf_counter()
        total_candles = 0
        # Process in chunks to keep memory bounded.
        cur = start_ms
        while cur < end_ms:
            chunk_end = min(end_ms, cur + chunk_days * 24 * 60 * 60000)
            key = f'phase3aOptimized:{symbol}:1m:{cur}:{chunk_end}'
            cp = store.get_checkpoint(key)
            if cp and cp.get('completed'):
                total_candles += cp.get('candles', 0)
            else:
                ck_t = time.perf_counter()
                res = fetch_range_cached(binance_symbol, '1m', cur, chunk_end, cache=cache, policy=CachePolicy.AUTO)
                ck_load = time.perf_counter() - ck_t
                rt = time.perf_counter()
                saved = collector.collect_historical_streaming(symbol, iter(res.klines))
                rt_elapsed = time.perf_counter() - rt
                total_candles += len(res.klines)
                store.set_checkpoint(key, {'symbol': symbol, 'binance_symbol': binance_symbol, 'candles': len(res.klines), 'observations': saved * 8, 'load_s': round(ck_load, 2), 'replay_s': round(rt_elapsed, 2), 'first_ms': int(res.klines[0][0]) if res.klines else None, 'last_ms': int(res.klines[-1][0]) if res.klines else None, 'completed': True})
                print(f"  {symbol} {cur}-{chunk_end}: {len(res.klines)} candles, load {ck_load:.1f}s, replay {rt_elapsed:.1f}s")
            cur = chunk_end
        episodes = store.rebuild_episodes(symbol)
        row = {'symbol': symbol, 'binance_symbol': binance_symbol, 'candles': total_candles, 'episodes': episodes, 'elapsed_s': round(time.perf_counter() - sym_start, 2), 'completed': True}
        report['symbols'][symbol] = row
        print(f"  {symbol} total: {total_candles} candles, {episodes} episodes in {row['elapsed_s']}s")
    report['finished_at_ms'] = int(time.time() * 1000)
    store.set_checkpoint(f'phase3aOptimized:latest:{days}d', report)
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--store', default=str(Path.home() / '.hermes' / 'fibolearn' / 'fibolearn.sqlite'))
    ap.add_argument('--days', type=int, default=30)
    ap.add_argument('--symbols', default='BTC,ETH,SOL,ZEC,PAXG')
    ap.add_argument('--chunk-days', type=int, default=7)
    args = ap.parse_args()
    store = FiboLearnStore(args.store, use_optimized_layout=True)
    symbols = [s.strip() for s in args.symbols.split(',') if s.strip()]
    print(f"Building {args.days}-day dataset for {symbols} into {args.store} (chunk_days={args.chunk_days})")
    rep = build(store, days=args.days, symbols=symbols, chunk_days=args.chunk_days)
    print(json.dumps(rep, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
