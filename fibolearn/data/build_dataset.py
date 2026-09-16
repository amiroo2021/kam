from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable
from urllib.parse import urlencode
from urllib.request import urlopen

from fibolearn.collector.live_collector import CollectorSettings, FiboLearnCollector
from fibolearn.config.defaults import DEFAULT_PERCENTAGES, DEFAULT_SYMBOLS, SYMBOL_TO_BINANCE_SPOT
from fibolearn.data.goldenfibo_data import fetch_range
from fibolearn.storage.sqlite_store import FiboLearnStore


def iso_to_ms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp() * 1000)


def ms_to_iso(ms: int | None) -> str:
    if ms is None:
        return '—'
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat().replace('+00:00', 'Z')


def first_binance_1m_open(symbol: str) -> int:
    qs = urlencode({'symbol': SYMBOL_TO_BINANCE_SPOT.get(symbol, symbol + 'USDT'), 'interval': '1m', 'startTime': 0, 'limit': 1})
    data = json.loads(urlopen('https://api.binance.com/api/v3/klines?' + qs, timeout=30).read().decode())
    return int(data[0][0])


def availability(symbols=DEFAULT_SYMBOLS) -> Dict[str, Dict]:
    now_ms = int(time.time() * 1000)
    out = {}
    for symbol in symbols:
        first = first_binance_1m_open(symbol)
        candles = max(0, (now_ms - first) // 60000)
        out[symbol] = {
            'symbol': symbol,
            'binance_symbol': SYMBOL_TO_BINANCE_SPOT.get(symbol, symbol + 'USDT'),
            'earliest_reliable_1m_ms': first,
            'earliest_reliable_1m': ms_to_iso(first),
            'expected_full_candles': candles,
            'expected_full_ladder_rows': candles * 8,
        }
    return out


def estimate(symbols=DEFAULT_SYMBOLS, *, days: int = 7) -> Dict[str, Dict]:
    avail = availability(symbols)
    per_symbol_candles = days * 24 * 60
    out = {}
    for symbol, row in avail.items():
        candles = min(per_symbol_candles, row['expected_full_candles'])
        out[symbol] = dict(row, planned_candles=candles, planned_ladder_rows=candles * 8, estimated_observation_storage_bytes=candles * 9000)
    return out


def build_dataset(store: FiboLearnStore, *, days: int = 7, symbols=DEFAULT_SYMBOLS, end_ms: int | None = None) -> Dict[str, Dict]:
    end_ms = end_ms or (int(time.time() * 1000) // 60000) * 60000
    start_ms = end_ms - days * 24 * 60 * 60000
    collector = FiboLearnCollector(store, settings=CollectorSettings(symbols=tuple(symbols), percentages=DEFAULT_PERCENTAGES))
    report = {'start_ms': start_ms, 'end_ms': end_ms, 'symbols': {}, 'started_at_ms': int(time.time()*1000)}
    for symbol in symbols:
        key = f'phase3a:{symbol}:1m:{start_ms}:{end_ms}'
        checkpoint = store.get_checkpoint(key)
        if checkpoint and checkpoint.get('completed'):
            report['symbols'][symbol] = dict(checkpoint, skipped=True)
            continue
        t0 = time.perf_counter()
        binance_symbol = SYMBOL_TO_BINANCE_SPOT.get(symbol) or (symbol + 'USDT')
        candles = fetch_range(binance_symbol, '1m', start_ms, end_ms, policy='AUTO')
        saved = collector.collect_historical(symbol, candles)
        episodes = store.rebuild_episodes(symbol)
        elapsed = time.perf_counter() - t0
        row = {'symbol': symbol, 'binance_symbol': binance_symbol, 'candles': len(candles), 'observations': saved, 'ladder_rows': saved*8, 'episodes': episodes, 'elapsed_s': round(elapsed, 3), 'first_ms': int(candles[0][0]) if candles else None, 'last_ms': int(candles[-1][0]) if candles else None, 'completed': True}
        store.set_checkpoint(key, row)
        report['symbols'][symbol] = row
    report['finished_at_ms'] = int(time.time()*1000)
    store.set_checkpoint(f'phase3a:latest:{days}d', report)
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description='Build resumable FiboLearn Phase 3A historical dataset')
    ap.add_argument('--store', default=str(Path.home()/'.hermes'/'fibolearn'/'fibolearn.sqlite'))
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--estimate', action='store_true')
    args = ap.parse_args()
    if args.estimate:
        print(json.dumps(estimate(days=args.days), indent=2, sort_keys=True))
        return
    store = FiboLearnStore(args.store)
    print(json.dumps(build_dataset(store, days=args.days), indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
