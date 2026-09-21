# Verification procedure

## A. Bundle integrity

```bash
cd /root/kam/migration/fibolearn
bash scripts/verify_bundle.sh --bundle bundles/<bundle_file>.tar.gz
```

Checks:

- archive readable
- expected member paths present
- SHA-256 of archive matches `MIGRATION_MANIFEST.json`
- SHA-256 of extracted REQUIRED DB files match manifest **physical** hashes
- `PRAGMA integrity_check` == `ok` on packaged SQLite files (read-only)

## B. Checkpoint accounting (no effect sizes)

Read-only verification of the **closed development accounting** report:

```bash
python3 /root/kam/migration/fibolearn/scripts/verify_checkpoint_counts.py \
  --accounting /root/kam/fibolearn/reports/fl_vwap_corrected_development_outcome_accounting.json
```

Expected (must match exactly):

```
raw_favorable_crosses = 2176
prospective_eligible_treated = 2176
matched_treated = 2047
PN_PLUS_1_FIRST = 779
TP_FIRST = 1001
OTHER_TERMINAL = 258
CENSORED = 9
SAME_CANDLE_AMBIGUOUS = 0
OTHER_UNKNOWN = 0
real_primitive_source_count = 2047
synthetic_fallback_count = 0
```

This script **does not** recompute outcomes from candles. It validates the frozen report artifact that closed the checkpoint.

To recompute from primitives would be expensive and is **out of scope** for migration smoke checks. If a full recompute is required later, use the FiboLearn prospective verification tooling on commit ≥ `1a07427` and compare to these expected values — still **without** computing effect sizes / RR / OR.

## C. Database integrity (restored paths)

```bash
sqlite3 /root/.hermes/fibolearn/fibolearn.sqlite 'PRAGMA integrity_check;'
sqlite3 /root/kam/fibolearn/data/fl_vwap_005_binance_validation.sqlite 'PRAGMA integrity_check;'
```

Both must print `ok`.

Reference row counts (clinic, at packaging time):

### fibolearn.sqlite

| table | rows |
|-------|-----:|
| episodes | 16371 |
| market_observations | 216000 |
| ladder_observations | 1728000 |
| observations | 14400 |
| outcomes | 295 |
| dataset_checkpoints | 57 |

### fl_vwap_005_binance_validation.sqlite

| table | rows |
|-------|-----:|
| candles | 15370978 |
| checkpoints | 5 |

## D. Logical fingerprint (not physical DB hash)

FL-VWAP-005 raw validation **canonical logical** fingerprint:

```
75a7af08a21d9ffe1731079e37f198b1663fcd5c4da4d3dcedcb9d7d7480cd60
```

Confirmed in Git reports:

- `fibolearn/reports/fl_vwap_005_validation_freeze.json`
- `fibolearn/reports/fl_vwap_005_final_summary.json`

**Do not** expect this string to equal `sha256sum` of the `.sqlite` file.

Physical SHA-256 of the validation DB is in `MIGRATION_MANIFEST.json` under `physical_sha256`.

## E. Tests (code path smoke)

GoldenFibo (process-isolated on small hosts; avoid huge combined RSS):

```bash
cd /root/kam/GoldenFibo
./.venv/bin/python -m pytest tests/test_config_and_compat.py -q
# optional broader:
# ./.venv/bin/python -m pytest -q
```

FiboLearn focused tests (from monorepo root with PYTHONPATH):

```bash
cd /root/kam
export PYTHONPATH=/root/kam
# examples present in tree:
# python3 -m pytest fibolearn/tests -q
# python3 -m pytest tests/test_fl_vwap_005_fingerprint.py -q   # if present at monorepo tests/
```

On memory-constrained hosts (≈2.5 GiB cgroup, no swap): run **one test node per process**.

## F. Git identity checks

```bash
cd /root/kam
git remote -v
git rev-parse HEAD
git rev-parse origin/main
git merge-base --is-ancestor 1a074275f31ec4b804c1929c1e7431bbe2bcdbe1 HEAD && echo ancestor_ok
git status --short
```

## G. Explicit non-goals during verify

- Do **not** start FL-VWAP-006
- Do **not** inspect FL-VWAP-006 outcomes
- Do **not** calculate development effect sizes
- Do **not** treat INVALID `1db8503` results as pass criteria
