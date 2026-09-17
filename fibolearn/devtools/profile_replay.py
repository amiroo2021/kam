from __future__ import annotations

import argparse
import cProfile
import io
import json
import pstats
import time
from decimal import Decimal
from pathlib import Path

from fibolearn.backtest.engine import replay_multiscale
from fibolearn.config.defaults import DEFAULT_PERCENTAGES
from fibolearn.data.goldenfibo_data import fetch_range


def k(ts, o, h, l, c, v='10'):
    from decimal import Decimal
    return [ts, str(o), str(h), str(l), str(c), v, ts + 59999, str(Decimal(str(c)) * Decimal(str(v))), 1, '0', '0', '0']


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbol', default='BTCUSDT')
    ap.add_argument('--days', type=int, default=2)
    args = ap.parse_args()

    end = int(time.time() * 1000) // 60000 * 60000
    start = end - args.days * 24 * 60 * 60000
    candles = fetch_range(args.symbol, '1m', start, end, policy='AUTO')
    print(f"Loaded {len(candles)} candles for {args.symbol}")

    pr = cProfile.Profile()
    pr.enable()
    t0 = time.perf_counter()
    snaps = replay_multiscale(args.symbol, candles, percentages=DEFAULT_PERCENTAGES, use_optimized_market=True)
    total = time.perf_counter() - t0
    pr.disable()

    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats('cumulative')
    ps.print_stats(40)
    print(s.getvalue())
    print(f'replay total {total:.2f}s for {len(candles)} candles, {len(snaps)} snapshots')
    json.dump({'symbol': args.symbol, 'days': args.days, 'candles': len(candles), 'snapshots': len(snaps), 'elapsed_s': total}, open('/tmp/profile_summary.json', 'w'))


if __name__ == '__main__':
    main()
