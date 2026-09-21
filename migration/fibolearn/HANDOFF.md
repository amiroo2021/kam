# FiboLearn / GoldenFibo Handoff (fresh Hermes server)

This document is the **authoritative project-state handoff**. It must not depend on Hermes conversational memory. Pair it with:

- `MIGRATION_MANIFEST.json` — inventory, hashes, classifications
- `RESTORE.md` — restore steps
- `VERIFY.md` — verification steps
- `REGENERATION_PARITY_CHECKPOINT.md` — GIANT BTC 24h regeneration-parity PASS (scoped)
- `FL_VWAP_006_METHODOLOGY_PREREGISTRATION.md` — pre-exposure FL-VWAP-006 methodology freeze
- data bundle under `bundles/` (non-Git REQUIRED databases)

Generated on clinic host. Do not treat chat history as source of truth.

---

## A. What GoldenFibo is

GoldenFibo is the **live / backtest execution and visualization system**:

- Deterministic Fibonacci ladder engine (`GoldenFibo/goldenfibo/engine/`)
- Session controller (REPLAY → LIVE handoff, LIVE, BACKTEST)
- Web UI (`goldenfibo-web` / `goldenfibo.service`)
- Trade metrics (OHLC approximation + aggTrade streaming VAP/VWAP)
- Telegram `/fibo` and `/trade` surfaces (via kam `plugins/trade`)

Package root: `/root/kam/GoldenFibo` (monorepo path `GoldenFibo/` inside `github.com/amiroo2021/kam`).

## B. What FiboLearn is

FiboLearn is a **read-only research / observation layer** over GoldenFibo semantics:

- Reconstructs multi-percentage × BUY/SELL ladder states historically
- Stores market/ladder observations and episodes in SQLite
- Runs prospective VWAP / risk-set research **without placing orders**
- Package root: `/root/kam/fibolearn`
- Default research DB path: `~/.hermes/fibolearn/fibolearn.sqlite` (outside Git)

## C. Critical separation

| System | Role | May trade? |
|--------|------|------------|
| **GoldenFibo** | Live execution + UI | Yes (when configured) |
| **FiboLearn** | Research / read-only | **No** |

Rules:

- Do **not** modify GoldenFibo execution behavior from FiboLearn work.
- Do **not** merge spot and futures cache identities.
- FiboLearn may **read** GoldenFibo engine/cache primitives; it must not place/cancel/modify orders.

## D. Current research hypothesis

Whether a **favorable cycle-anchored whole-ladder VWAP condition at landmark T** prospectively changes the probability that **P(n+1) occurs before TP**, compared with comparable risk-set controls that are alive, outcome-free, and event-free at the same elapsed time.

## E. Critical VWAP distinction

**Do NOT confuse:**

1. **Cycle-anchored whole-ladder VWAP** (research target): VWAP over the current GoldenFibo ladder window from current P0 → now (or landmark T), cycle-local.
2. **Older UTC-daily market VWAP feature**: calendar-day market VWAP. **Not** the FL-VWAP hypothesis target.

Using the wrong VWAP definition invalidates the research question.

## F. Major invalidated work (never treat as edge evidence)

| Item | Status |
|------|--------|
| Original episode-level VWAP selection leakage | **INVALID** |
| FL-VWAP-005 result with ~100% treated progression (`1db8503`) | **INVALID** — treated selection conditioned on future progression (`terminal_event == pn_plus_1_before_tp`) |
| Earlier resolver temporal-selection bug | **FIXED** later; prior results not rehabilitated |
| Earlier OTHER_UNKNOWN mass failure | **FIXED** in corrected accounting; old mass OTHER_UNKNOWN is not a scientific finding |

See `fibolearn/reports/fl_vwap_005_invalidation.json` and commit `285815e`.

**INVALID FL-VWAP-005 effect estimates must never be used as evidence for VWAP edge.**

## G. Correct prospective semantics

1. **Landmark T freezes state.** Events at timestamp `== T` are **state-only**.
2. Competing outcome events must be **strictly `> T`** (post-landmark).
3. **`terminal_event` must never be used as prospective input** (eligibility / matching / treatment assignment).
4. `terminal_event` may only be a **diagnostic / parity target**, not a selection filter for treated.
5. Event ownership, censoring, and same-candle ambiguity follow the corrected prospective primitive path (see commits `779356f`, `e3b10e1`, `1a07427`).
6. Canonical outcome labels:
   - `PN_PLUS_1_FIRST`
   - `TP_FIRST`
   - `OTHER_TERMINAL`
   - `CENSORED`
   - `SAME_CANDLE_AMBIGUOUS`
   - `OTHER_UNKNOWN` (should be ~0 after corrected accounting)

Coverage-end cases are treated as **CENSORED** (commit `1a07427`), not OTHER_UNKNOWN.

## H. Current verified development accounting

Source of truth report (also in Git):

`fibolearn/reports/fl_vwap_corrected_development_outcome_accounting.json`

| Metric | Value |
|--------|------:|
| raw favorable crosses | **2176** |
| prospective eligible treated | **2176** |
| matched treated | **2047** |
| PN_PLUS_1_FIRST | **779** |
| TP_FIRST | **1001** |
| OTHER_TERMINAL | **258** |
| CENSORED | **9** |
| SAME_CANDLE_AMBIGUOUS | **0** |
| OTHER_UNKNOWN | **0** |
| REAL primitive source | **2047 / 2047** |
| synthetic fallback | **0** |

Sum of outcome counts = 2047. Scope: **development-only prospective outcome accounting** on frozen matched treated population — **not** effect estimation.

## I. Current stage

```
DEVELOPMENT_PROSPECTIVE_OUTCOME_ACCOUNTING_CLOSED = YES
PROSPECTIVE_MEASUREMENT_PIPELINE_READY_TO_FREEZE = YES
```

Checkpoint commit (research accounting closed):

`1a074275f31ec4b804c1929c1e7431bbe2bcdbe1` — *Treat coverage-end cases as censored*

### I.1 GIANT regeneration-parity checkpoint (documentation)

Portability verification on GIANT after migration + environment reproduction:

```
REGENERATION_PARITY = PASS
```

**Scope only:** BTC, `2026-08-21T00:00:00Z → 2026-08-22T00:00:00Z` (packaged scored interior **123** episodes; identity/terminal/obs **123/123**).

Authoritative write-up:

- `migration/fibolearn/REGENERATION_PARITY_CHECKPOINT.md`
- `fibolearn/reports/regeneration_parity_btc_24h_giant.json`

Load-bearing historical semantics (do not silently change when reproducing development data):

1. **3-day chunk engine reset** during historical collection — continuously warm engines are **not** equivalent.
2. **`cycle_id` / `episode_key` reuse across chunks** — packaged unique keys can overwrite earlier collisions; window rebuild can show more episodes than packaged interior keys (351 vs 123); classified as keyspace artifact, not semantic mismatch.

This is **not** VWAP evidence, **not** FL-VWAP-006, **not** prospective validation.

Local scratch only (not Git): `/root/kam/.scratch/fibolearn-replay/WINDOW_FREEZE.json`, `FINAL_PARITY_REPORT.json`.

## J. IMPORTANT NEXT STEP

### J.1 FL-VWAP-006 methodology freeze (pre-exposure)

Formal prospective preregistration is documented at:

- `migration/fibolearn/FL_VWAP_006_METHODOLOGY_PREREGISTRATION.md`
- `fibolearn/reports/fl_vwap_006_preregistration.json`
- `fibolearn/reports/fl_vwap_006_methodology_freeze.json`

```
METHODOLOGY_FROZEN_PRE_EXPOSURE = YES   # after freeze commit on origin/main
READY_TO_ACQUIRE_FL_VWAP_006 = YES        # acquisition only after freeze commit
FL_VWAP_005 = CONTAMINATED_DEVELOPMENT_ONLY
```

Do **NOT**:

- calculate development effect sizes as validation evidence
- inspect or acquire FL-VWAP-006 before the freeze commit is on `origin/main`
- rehabilitate INVALID FL-VWAP-005 effect estimates
- change frozen matcher/estimand/inference after any 006 outcome is visible

## Historical commits (preserve meaning)

| Commit | Meaning |
|--------|---------|
| `25aaf95f31eb6c9152135e5ffae2bc02d4c9809b` | FL-VWAP-004 methodology freeze |
| `6d40e12621446067ea4a63e804b5804162133b7e` | FL-VWAP-005 validation data freeze |
| `1db85039f5ab8e94d0b02169c77066f420bf2c92` | **INVALID** FL-VWAP-005 result — future-conditioned treated selection |
| `285815e40a13ba74015dbf70c1c81a67d821549a` | FL-VWAP-005 invalidation / prospective semantics correction |
| `779356f05d9bbeb86375d561f23df48bdf0322ce` | Strict post-landmark `>T` outcome timing fix |
| `e3b10e13234fe8c7917318ac28f2c22611521dd2` | Focused prospective primitive verification |
| `1a074275f31ec4b804c1929c1e7431bbe2bcdbe1` | **Current** closed development accounting / coverage-aware censoring |

## Logical vs physical hashes

- **CANONICAL LOGICAL DATA SHA-256** (FL-VWAP-005 raw validation fingerprint):
  `75a7af08a21d9ffe1731079e37f198b1663fcd5c4da4d3dcedcb9d7d7480cd60`
  This is **not** the physical SQLite file hash.
- **PHYSICAL FILE SHA-256** values are recorded in `MIGRATION_MANIFEST.json` per file.

Never compare a physical-byte SQLite hash to a logical-content fingerprint.

## Repo layout (monorepo)

```
/root/kam/                 # git root (github.com/amiroo2021/kam)
  GoldenFibo/              # execution system
  fibolearn/               # research system
  plugins/trade/           # Telegram wizards
  migration/fibolearn/     # this migration package (docs/scripts)
```

Default non-Git data:

```
/root/.hermes/fibolearn/fibolearn.sqlite          # main FiboLearn DB
/root/kam/fibolearn/data/fl_vwap_005_binance_validation.sqlite
/root/kam/GoldenFibo/data/binance_klines.sqlite   # optional GF cache
```

## Safety

- No live order placement from migration docs.
- No secrets in Git or bundles.
- Secrets are restored **manually** on the new server (see `SECRET_CHECKLIST.md`).
