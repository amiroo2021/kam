"""Deduplicate ladder_observations for the production FiboLearn DB.

The 30-day build accidentally inserted some 8-ladder-row sets twice. This
script keeps only the first ladder row per (market_id, percentage, direction)
keeping the original cycle_id/active_step pair so downstream episode
rebuilding is well-defined.

After this script, both legacy and streaming rebuilders should converge on
the same episode set for the 30-day BTC dataset.
"""
import sqlite3
import sys
from pathlib import Path

DB = Path('/root/.hermes/fibolearn/fibolearn.sqlite')
REPORT = Path('/root/kam/fibolearn/reports/phase3a_dedup_report.json')
REPORT.parent.mkdir(parents=True, exist_ok=True)

def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    # Inventory
    pre_total = conn.execute('select count(*) from ladder_observations').fetchone()[0]
    pre_dups = conn.execute('''
        select count(*) from (
          select market_id, percentage, direction, count(*) n
          from ladder_observations
          group by market_id, percentage, direction having n > 1
        )
    ''').fetchone()[0]
    print(f'pre: {pre_total} ladder rows; {pre_dups} duplicate (market_id, percentage, direction) groups')
    # Pick one representative per (market_id, percentage, direction): the row
    # with the largest closed_count, then smallest id.
    # We keep the "winner" for each (market_id, percentage, direction), delete
    # all others.
    rows = conn.execute('''
        select l.id, l.market_id, l.percentage, l.direction,
               json_extract(l.ladder_state_json, '$.closed_count') closed,
               l.cycle_id, l.active_step, l.ladder_state_json
        from ladder_observations l
        where (l.market_id, l.percentage, l.direction) in (
          select market_id, percentage, direction
          from ladder_observations
          group by market_id, percentage, direction having count(*) > 1
        )
        order by market_id, percentage, direction, closed desc nulls last, l.id asc
    ''').fetchall()
    keep_ids: set[int] = set()
    seen_keys: set[tuple] = set()
    drop_ids: list[int] = []
    for r in rows:
        key = (r['market_id'], r['percentage'], r['direction'])
        if key in seen_keys:
            drop_ids.append(int(r['id']))
        else:
            seen_keys.add(key)
            keep_ids.add(int(r['id']))
    print(f'will drop {len(drop_ids)} duplicate ladder rows; keep {len(keep_ids)} unique')
    if not drop_ids:
        print('nothing to dedup')
        import json
        REPORT.write_text(json.dumps({'pre_total': pre_total, 'pre_dups': pre_dups, 'dropped': 0}, indent=2))
        return
    # Delete in batches.
    n = 0
    for i in range(0, len(drop_ids), 500):
        batch = drop_ids[i:i+500]
        ph = ','.join('?' for _ in batch)
        conn.execute(f'delete from ladder_observations where id in ({ph})', batch)
        n += len(batch)
    conn.commit()
    post_total = conn.execute('select count(*) from ladder_observations').fetchone()[0]
    # Verify
    post_dups = conn.execute('''
        select count(*) from (
          select market_id, percentage, direction, count(*) n
          from ladder_observations
          group by market_id, percentage, direction having n > 1
        )
    ''').fetchone()[0]
    print(f'post: {post_total} ladder rows; {post_dups} duplicate groups remaining')
    # VACUUM to compact (may be slow; skip if user wants speed)
    # conn.execute('VACUUM')
    import json
    REPORT.write_text(json.dumps({
        'pre_total': pre_total, 'pre_dups': pre_dups,
        'dropped': n, 'post_total': post_total, 'post_dups': post_dups,
    }, indent=2))
    print('wrote', REPORT)


if __name__ == '__main__':
    main()
