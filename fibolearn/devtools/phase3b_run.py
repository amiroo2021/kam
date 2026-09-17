from __future__ import annotations

import json
from pathlib import Path

from fibolearn.storage.sqlite_store import FiboLearnStore
from fibolearn.research.phase3b import run_phase3b

DB = Path('/root/.hermes/fibolearn/fibolearn.sqlite')
REPORT_DIR = Path('/root/kam/fibolearn/reports')


def main() -> None:
    store = FiboLearnStore(DB, use_optimized_layout=True)
    run_phase3b(store, report_dir=REPORT_DIR)
    print(json.dumps({'status': 'ok', 'reports': [
        str(REPORT_DIR / 'phase3b_baselines.json'),
        str(REPORT_DIR / 'phase3b_fl_vwap_001.json'),
        str(REPORT_DIR / 'phase3b_pattern_discovery.json'),
        str(REPORT_DIR / 'phase3b_oos.json'),
        str(REPORT_DIR / 'phase3b_summary.json'),
    ]}, sort_keys=True))


if __name__ == '__main__':
    main()
