from __future__ import annotations

import json
from pathlib import Path

from fibolearn.storage.sqlite_store import FiboLearnStore

ROOT = Path('/root/kam')
DB = Path('/root/.hermes/fibolearn/fibolearn.sqlite')
OUT = ROOT / 'fibolearn/reports/phase3a_final_anomaly_audit.json'


def main() -> None:
    store = FiboLearnStore(DB, use_optimized_layout=True)
    report = {
        'ambiguous_episodes': len(store.ambiguous_episode_rows()),
        'episode_baselines': store.episode_baselines(include_ambiguous=True),
        'data_coverage': store.data_coverage_matrix(),
        'dataset_status': store.dataset_status_by_symbol(),
    }
    OUT.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps({
        'ambiguous_episodes': report['ambiguous_episodes'],
        'out': str(OUT),
    }, sort_keys=True))


if __name__ == '__main__':
    main()
