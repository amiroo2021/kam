"""Stage 3 parity using streaming SELECT+INSERT (no full DB copy).

The parity DB is a fresh SQLite file with only the BTC schema needed.
We stream the 30-day BTC market+ladder rows from production into it.
This keeps the parity process under ~500 MiB.
"""
import sys, time, json, sqlite3
from pathlib import Path

ROOT = Path('/root/kam')
sys.path.insert(0, str(ROOT))

PROD = Path('/root/.hermes/fibolearn/fibolearn.sqlite')
DB = Path('/root/kam/fibolearn/reports/phase3a_parity_btc.sqlite')

SCHEMA = """
CREATE TABLE schema_meta(version integer not null, applied_at integer not null);
CREATE TABLE market_observations(
  id integer primary key autoincrement,
  schema_version integer not null,
  symbol text not null,
  timestamp_ms integer not null,
  market_json text not null,
  feature_definition_json text not null,
  price_action_json text not null,
  significant_levels_versioned_json text not null,
  source_candles integer not null,
  created_at integer not null);
CREATE TABLE ladder_observations(
  id integer primary key autoincrement,
  market_id integer not null,
  percentage text not null,
  direction text not null,
  cycle_id text not null,
  active_step integer not null,
  ladder_state_json text not null,
  created_at integer not null);
CREATE TABLE episodes(...);
CREATE TABLE outcomes(...);
CREATE TABLE runtime_state(...);
CREATE TABLE patterns(...);
CREATE TABLE backtests(...);
CREATE TABLE dataset_checkpoints(...);
"""


def fingerprint(rows):
    return sorted([
        (e['episode_key'], e['terminal_event'],
         int(e['start_timestamp_ms']), int(e['end_timestamp_ms']),
         str(e['outcome'].get('mfe')), str(e['outcome'].get('mae')),
         e['active_step'], e['cycle_id'])
        for e in rows
    ])


def prepare_btc_subset():
    # Stream rows from production into a fresh DB.
    src = sqlite3.connect(str(PROD))
    src.row_factory = sqlite3.Row
    if DB.exists():
        DB.unlink()
    dst = sqlite3.connect(str(DB))
    dst.executescript("""
        CREATE TABLE schema_meta(version integer not null, applied_at integer not null);
        CREATE TABLE market_observations(
          id integer primary key autoincrement,
          schema_version integer not null,
          symbol text not null,
          timestamp_ms integer not null,
          market_json text not null,
          feature_definition_json text not null,
          price_action_json text not null,
          significant_levels_versioned_json text not null,
          source_candles integer not null,
          created_at integer not null);
        CREATE TABLE ladder_observations(
          id integer primary key autoincrement,
          market_id integer not null,
          percentage text not null,
          direction text not null,
          cycle_id text not null,
          active_step integer not null,
          ladder_state_json text not null,
          created_at integer not null);
        CREATE TABLE outcomes(observation_id integer primary key, outcome_json text not null, created_at integer not null);
        CREATE TABLE runtime_state(key text primary key, value_json text not null, updated_at integer not null);
        CREATE TABLE patterns(id integer primary key autoincrement, name text not null, status text not null, definition_json text not null, metrics_json text not null, created_at integer not null);
        CREATE TABLE backtests(id integer primary key autoincrement, pattern_id integer, symbols_json text not null, result_json text not null, created_at integer not null);
        CREATE TABLE dataset_checkpoints(key text primary key, value_json text not null, updated_at integer not null);
        CREATE TABLE episodes(
          id integer primary key autoincrement,
          episode_key text not null unique,
          symbol text not null,
          percentage text not null,
          direction text not null,
          cycle_id text not null,
          active_step integer not null,
          start_timestamp_ms integer not null,
          end_timestamp_ms integer not null,
          terminal_event text not null,
          observation_count integer not null,
          raw_observation_ids_json text not null,
          start_state_json text not null,
          end_state_json text not null,
          evolution_json text not null,
          outcome_json text not null,
          created_at integer not null);
        INSERT INTO schema_meta VALUES (1, 1);
    """)
    # Stream BTC market rows + their ladder rows.
    print('streaming BTC market_observations...')
    cur = src.execute(
        "select id, schema_version, symbol, timestamp_ms, market_json, feature_definition_json, "
        "price_action_json, significant_levels_versioned_json, source_candles, created_at "
        "from market_observations where symbol='BTC' order by timestamp_ms"
    )
    id_map = {}
    n_mkt = 0
    while True:
        rows = cur.fetchmany(2000)
        if not rows: break
        for r in rows:
            new_id = n_mkt + 1
            id_map[r['id']] = new_id
            dst.execute(
                'insert into market_observations(id,schema_version,symbol,timestamp_ms,market_json,feature_definition_json,price_action_json,significant_levels_versioned_json,source_candles,created_at) values (?,?,?,?,?,?,?,?,?,?)',
                (new_id, r['schema_version'], r['symbol'], r['timestamp_ms'], r['market_json'], r['feature_definition_json'], r['price_action_json'], r['significant_levels_versioned_json'], r['source_candles'], r['created_at'])
            )
            n_mkt += 1
    print(f'  {n_mkt} BTC market rows')
    print('streaming ladder_observations for BTC...')
    cur = src.execute(
        "select id, market_id, percentage, direction, cycle_id, active_step, ladder_state_json, created_at "
        "from ladder_observations where percentage in ('1','0.1','0.01','0.001') and direction in ('BUY','SELL') "
        "and market_id in (select id from market_observations where symbol='BTC') order by id"
    )
    n_lad = 0
    while True:
        rows = cur.fetchmany(5000)
        if not rows: break
        for r in rows:
            new_mid = id_map.get(r['market_id'])
            if new_mid is None:
                continue
            dst.execute(
                'insert into ladder_observations(id,market_id,percentage,direction,cycle_id,active_step,ladder_state_json,created_at) values (?,?,?,?,?,?,?,?)',
                (n_lad + 1, new_mid, r['percentage'], r['direction'], r['cycle_id'], r['active_step'], r['ladder_state_json'], r['created_at'])
            )
            n_lad += 1
    dst.commit()
    print(f'  {n_lad} ladder rows for BTC')
    src.close(); dst.close()


def main():
    print('=== preparing BTC parity subset ===')
    prepare_btc_subset()
    print()
    from fibolearn.storage.sqlite_store import FiboLearnStore

    print('=== LEGACY ===')
    store = FiboLearnStore(DB, use_optimized_layout=True)
    t = time.perf_counter()
    n = store.rebuild_episodes(symbol='BTC')
    legacy_t = round(time.perf_counter() - t, 3)
    legacy_rows = store.episode_rows(symbol='BTC')
    legacy_fp = fingerprint(legacy_rows)
    print(f'  episodes={n}  elapsed={legacy_t} s')
    print()

    print('=== STREAMING ===')
    # Rebuild DB from scratch since legacy may have mutated.
    prepare_btc_subset()
    store = FiboLearnStore(DB, use_optimized_layout=True)
    t = time.perf_counter()
    n2 = store.rebuild_episodes_streaming(symbol='BTC', page_size=2000, checkpoint=False)
    stream_t = round(time.perf_counter() - t, 3)
    stream_rows = store.episode_rows(symbol='BTC')
    stream_fp = fingerprint(stream_rows)
    print(f'  episodes={n2}  elapsed={stream_t} s')
    print()

    legacy_set = set(tuple(t) for t in legacy_fp)
    stream_set = set(tuple(t) for t in stream_fp)
    only_legacy = sorted(legacy_set - stream_set)
    only_stream = sorted(stream_set - legacy_set)
    both = sorted(legacy_set & stream_set)
    match = (len(only_legacy) == 0 and len(only_stream) == 0 and len(legacy_set) == len(stream_set))

    result = {
        'subset_symbol': 'BTC',
        'subset_days': 30,
        'legacy': {'episodes': n, 'elapsed_s': legacy_t},
        'streaming': {'episodes': n2, 'elapsed_s': stream_t},
        'shared_episodes_full_fp': len(both),
        'only_in_legacy': len(only_legacy),
        'only_in_streaming': len(only_stream),
        'exact_match_full_fp': match,
        'first_5_only_in_legacy': [list(t) for t in only_legacy[:5]],
        'first_5_only_in_streaming': [list(t) for t in only_stream[:5]],
    }
    out = Path('/root/kam/fibolearn/reports/phase3a_streaming_parity.json')
    out.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
