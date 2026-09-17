from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
from pathlib import Path

ROOT = Path('/root/kam')
sys.path.insert(0, str(ROOT))
from fibolearn.storage.sqlite_store import FiboLearnStore
DB = Path('/root/.hermes/fibolearn/fibolearn.sqlite')
REPORT = ROOT / 'fibolearn' / 'reports' / 'phase3a_stage4_rebuild.json'


def load_report() -> dict:
    if REPORT.exists():
        try:
            return json.loads(REPORT.read_text())
        except Exception:
            return {'symbols': {}}
    return {'symbols': {}}


def save_report(data: dict) -> None:
    tmp = REPORT.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, REPORT)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('symbol')
    ap.add_argument('--page-size', type=int, default=2000)
    args = ap.parse_args()

    store = FiboLearnStore(DB, use_optimized_layout=True)
    before = time.time()
    result = {'symbol': args.symbol, 'started_at': int(before), 'status': 'RUNNING', 'page_size': args.page_size}
    data = load_report()
    data.setdefault('symbols', {})[args.symbol] = result
    save_report(data)

    def progress(payload):
        data = load_report()
        sym = data.setdefault('symbols', {}).setdefault(args.symbol, {})
        sym['progress'] = payload
        sym['updated_at'] = int(time.time())
        save_report(data)

    try:
        # count current rows before rebuild for bookkeeping
        with store._connect() as c:
            market_rows = c.execute('select count(*) from market_observations where symbol=?', (args.symbol,)).fetchone()[0]
            ladder_rows = c.execute('select count(*) from ladder_observations l join market_observations m on m.id=l.market_id where m.symbol=?', (args.symbol,)).fetchone()[0]
            prev_episodes = c.execute('select count(*) from episodes where symbol=?', (args.symbol,)).fetchone()[0]
        start = time.time()
        episodes = store.rebuild_episodes_streaming(symbol=args.symbol, page_size=args.page_size, checkpoint=True, on_progress=progress)
        elapsed = time.time() - start
        with store._connect() as c:
            new_episodes = c.execute('select count(*) from episodes where symbol=?', (args.symbol,)).fetchone()[0]
            completed = c.execute("select count(*) from episodes where symbol=? and terminal_event in ('pn_plus_1_before_tp','tp_before_pn_plus_1')", (args.symbol,)).fetchone()[0]
            censored = c.execute("select count(*) from episodes where symbol=? and terminal_event='censored'", (args.symbol,)).fetchone()[0]
        try:
            rss_kb = int(os.environ.get('PEAK_RSS_KB', '0'))
        except Exception:
            rss_kb = 0
        data = load_report()
        data.setdefault('symbols', {})[args.symbol] = {
            'symbol': args.symbol,
            'status': 'PASS',
            'page_size': args.page_size,
            'market_rows': int(market_rows),
            'ladder_rows': int(ladder_rows),
            'episodes_emitted': int(episodes),
            'episodes_in_db': int(new_episodes),
            'completed_episodes': int(completed),
            'censored_episodes': int(censored),
            'previous_episode_count': int(prev_episodes),
            'elapsed_s': round(elapsed, 3),
            'peak_rss_kb': rss_kb,
            'finished_at': int(time.time()),
        }
        save_report(data)
    except Exception as e:
        data = load_report()
        data.setdefault('symbols', {})[args.symbol] = {
            'symbol': args.symbol,
            'status': 'FAIL',
            'error': repr(e),
            'finished_at': int(time.time()),
        }
        save_report(data)
        raise


if __name__ == '__main__':
    main()
