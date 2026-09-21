# Regeneration parity checkpoint (GIANT portability)

**DOCUMENTATION ONLY.** No methodology change. No FL-VWAP-006. No effect sizes.

```
REGENERATION_PARITY = PASS
```

**Scope of PASS (do not broaden):**

| Field | Value |
|-------|-------|
| Host | GIANT |
| Symbol | **BTC only** |
| Evaluation window | **2026-08-21T00:00:00Z → 2026-08-22T00:00:00Z** |
| Evaluation start_ms | `1787270400000` |
| Evaluation end_ms | `1787356800000` (exclusive) |
| Scoring rule | `start_timestamp_ms >= eval_start` AND `end_timestamp_ms < eval_end` (fully closed interior) |

This checkpoint records that GIANT independently regenerated the frozen BTC 24h **development** window and matched packaged scored interior episode semantics. It is a **portability/reproducibility** result, not a scientific finding about VWAP.

---

## 1. What was verified

GIANT independently regenerated the frozen BTC 24h development window from the restored development candle source and compared against the packaged development episode store.

## 2. Packaged scored interior episodes

**123**

## 3. Exact parity (packaged interior keys)

| Check | Result |
|-------|--------|
| episode identity | **123 / 123** |
| missing | **0** |
| boundary mismatches | **0** |
| terminal_event | **123 / 123** |
| terminal mismatches | **0** |
| ambiguity mismatches | **0** |
| observation_count mismatches | **0** |

## 4. Reference terminal breakdown (packaged interior)

| terminal_event | count |
|----------------|------:|
| `pn_plus_1_before_tp` | **46** |
| `tp_before_pn_plus_1` | **65** |
| `other_terminal` | **12** |

## 5. domain_events parity

**1440 / 1440** for the evaluation window (one market observation per minute × 24h).

## 6. Independent regeneration path used

- Development candle lineage: `/root/kam/GoldenFibo/data/binance_klines.sqlite`
- Production reconstruction path: `FiboLearnCollector.collect_historical_streaming` → GoldenFibo LEGACY OHLC engine via `apply_ohlc_page`
- Continuous Stage-2 `domain_events` replay across the reconstruction context
- Episode builder: `FiboLearnStore.rebuild_episodes_streaming()`
- **No** synthetic/fallback reconstruction

Scratch outputs (not in Git):

```
/root/kam/.scratch/fibolearn-replay/WINDOW_FREEZE.json
/root/kam/.scratch/fibolearn-replay/FINAL_PARITY_REPORT.json
```

Reconstruction context used for warm-up / closure (not the scored window alone):

| Field | Value |
|-------|-------|
| context start | 2026-08-17T18:02:00Z (`1786989720000`) |
| context end | 2026-08-22T02:00:00Z (`1787364000000`) |
| source candles (context) | 6238 |
| source gaps (context / eval) | 0 / 0 |

---

## 7. IMPORTANT — historical reproduction semantic (load-bearing)

Historical collection used **3-day chunking with engine reset per chunk**.

That reset behavior is **load-bearing** for reproduction of the packaged development dataset.

A **continuously warm** engine across chunk boundaries is **NOT** semantically equivalent to the historical generation process.

Do **not** silently remove or change this behavior when reproducing historical development data.

Production chunk boundaries for BTC (from packaged `dataset_checkpoints`) include:

```
phase3aOptimized:BTC:1m:1786989720000:1787248920000   # 3 days, 4320 candles
phase3aOptimized:BTC:1m:1787248920000:1787508120000
...
```

Verified failure mode if ignored: continuous warm engines diverge at the first chunk boundary (`2026-08-20T18:02:00Z`); ladder `cycle_id` / `pn` no longer match the packaged observations.

---

## 8. IMPORTANT — episode_key / cycle_id keyspace finding

`cycle_id` **restarts** after each historical engine reset.

Therefore `episode_key` values of the form:

```text
{symbol}|{percentage}|{direction}|{cycle_id}|{active_step}
```

can **recur across chunks**.

Because `episode_key` is unique/upserted in the packaged episode store, **later** episodes can **replace** earlier episodes that share the same key.

This explains the count difference:

| Set | Count |
|-----|------:|
| Regenerated window interior episodes | **351** |
| Packaged interior episode keys still present | **123** |
| Additional regenerated keys vs packaged interior | **228** |

Control experiment:

- Rebuilding episodes from the **packaged observations** for the same window also produced **351** episodes.
- Independent regeneration matched those **351 / 351**.

**Classification:** the 228 additional regenerated episodes are a **historical keyspace/uniqueness artifact**, **NOT** a semantic reconstruction mismatch for the tested window.

---

## 9. What this finding must NOT be interpreted as

- VWAP edge evidence
- FL-VWAP-006 evidence
- Prospective validation
- Evidence that overwritten episodes are statistically independent
- Permission to change episode identity semantics before the next preregistration

Do **not** treat `REGENERATION_PARITY = PASS` as authorization to open FL-VWAP-006 or to compute development effect sizes.

---

## 10. Reference DB safety (operational requirement — do not implement here)

`KlineCache` was observed to write **availability / WAL metadata** even during the replay workflow when opening the live development candle DB path.

The development candle DB was restored to its original physical SHA-256 after the experiment:

| Path | Physical SHA-256 (restored / verified) |
|------|----------------------------------------|
| `/root/.hermes/fibolearn/fibolearn.sqlite` | `b38bcf14a25a01ef9dc53e8b801a0309dcb07e9f54f31a7e878361f596199cc5` |
| `/root/kam/GoldenFibo/data/binance_klines.sqlite` | `e44164be58717a0c67b3fd90d677653f97af0d4d8178a75116e00cb6910c5b05` |
| `/root/kam/fibolearn/data/fl_vwap_005_binance_validation.sqlite` | `2c17e874b92d8216fa9da505bf80aab8972cbf1a74649d42fb2d36e48cf208e6` |

**Operational requirement for future research/replay work:** open reference DBs truly read-only where supported, or use a scratch copy. Do **not** treat this documentation as an implemented code change.

---

## 11. Data lineage distinction (preserve)

**DEVELOPMENT lineage** (this checkpoint):

```text
/root/.hermes/fibolearn/fibolearn.sqlite
/root/kam/GoldenFibo/data/binance_klines.sqlite
```

**FL-VWAP-005 historical validation lineage** (distinct — not used as regen input):

```text
/root/kam/fibolearn/data/fl_vwap_005_binance_validation.sqlite
```

Do not merge, append, or treat these as interchangeable.

---

## 12. Artifact locations (local scratch — not Git)

```text
/root/kam/.scratch/fibolearn-replay/WINDOW_FREEZE.json
/root/kam/.scratch/fibolearn-replay/FINAL_PARITY_REPORT.json
```

Large scratch SQLite files under `.scratch/fibolearn-replay/` are temporary verification outputs and must **not** be committed.

---

## Status flags

```
REGENERATION_PARITY = PASS   # BTC 24h scope only (2026-08-21 → 2026-08-22 UTC)
FL_VWAP_006_ACCESSED = NO
HYPOTHESIS_EFFECTS_CALCULATED = NO
GOLDENFIBO_CODE_CHANGED = NO
FIBOLEARN_METHODOLOGY_CHANGED = NO
REFERENCE_DBS_INTENTIONALLY_MODIFIED = NO
```
