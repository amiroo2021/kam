# FL-VWAP-006 Prospective Methodology Preregistration / Freeze

**Status:** `METHODOLOGY_FROZEN_PRE_EXPOSURE = YES` (documentation freeze)  
**Hypothesis ID:** `FL-VWAP-006`  
**Methodology version:** `fl_vwap_006_methodology_v1`  
**Created (UTC):** `2026-09-21T10:02:03Z`  
**Code baseline at freeze drafting:** `c203e4a7dce7449785dba68d78dcea661aa06a10`  
**Freeze commit:** 
**Nature:** DOCUMENTATION + METHODOLOGY FREEZE ONLY  

```
FL_VWAP_006_DATA_ACCESSED = NO
FL_VWAP_006_DATA_ACQUIRED = NO
HYPOTHESIS_EFFECTS_CALCULATED = NO
```

This document freezes analytical decisions **before** any FL-VWAP-006 validation data are acquired or inspected.

Machine-readable companions:

- `fibolearn/reports/fl_vwap_006_preregistration.json`
- `fibolearn/reports/fl_vwap_006_methodology_freeze.json`

---

## 0. Hypothesis lineage (do not erase)

| ID | Role |
|----|------|
| **FL-VWAP-001** | Invalidated leakage |
| **FL-VWAP-002 / 003** | Developmental methodology iterations |
| **FL-VWAP-004** | Verified landmark / risk-set methodology foundation (`25aaf95`) |
| **FL-VWAP-005** | Attempted validation; **INVALID** because treated selection conditioned on future progression (`1db8503`); later used only for development/debugging of corrected prospective measurement |
| **FL-VWAP-006** | **First new validation attempt** after corrected prospective pipeline freeze |

**FL-VWAP-005 is CONTAMINATED / DEVELOPMENT-ONLY.**  
Its corrected prospective pipeline may define methodology.  
**FL-VWAP-005 results must NOT be treated as validation evidence.**

---

## 1. Scientific question (frozen)

Whether a **favorable cycle-anchored whole-ladder VWAP condition at landmark T** prospectively changes the probability that **P(n+1) occurs before TP**, compared with comparable risk-set controls that are alive, outcome-free, and event-free at the same elapsed time — estimated only on a **genuinely outcome-unseen** FL-VWAP-006 validation lineage.

---

## 2. Exposure definition (frozen)

**Feature version:** `whole_ladder_vwap_v1`  
**Implementation reference:** `fibolearn/research/phase3b_corrected.py::compute_cross_feature` and `fibolearn/research/phase3b_cross_riskset.py` prospective constructors (corrected eligibility path).

### 2.1 Cycle-anchored whole-ladder VWAP

Within an episode unit (fixed symbol, percentage, direction, cycle identity, active_step, episode start):

1. Walk 1-minute observations from episode start forward.
2. Maintain cumulative base volume and cumulative quote volume from **episode start**:
   - `cum_base += volume`
   - `cum_quote += price * volume`
   - `vwap = cum_quote / cum_base` (if `cum_base > 0`, else price)
3. Compare to the episode’s ladder **Pn** at the episode’s frozen active step (Pn from the episode-start ladder state; Pn does not use future terminal labels).

This is **cycle-local whole-ladder VWAP**, **not** UTC-daily market VWAP.

### 2.2 Favorable-cross condition

| Direction | Favorable condition |
|-----------|---------------------|
| **BUY** | `vwap <= pn` |
| **SELL** | `vwap >= pn` |

**Cross timestamp T_cross:** first observation timestamp at which the favorable condition is true after episode start.  
**Landmark T for treated:** `T = T_cross` (must satisfy `T_cross > episode_start_timestamp_ms`).  
**Cross elapsed:** `cross_elapsed_ms = T_cross - episode_start_timestamp_ms`.

### 2.3 Percentage / scale handling

Allowed percentages: production defaults  
`1`, `0.1`, `0.01`, `0.001`  
(`fibolearn.config.defaults.DEFAULT_PERCENTAGES`).

### 2.4 Directions

Both **BUY** and **SELL**.

### 2.5 Allowed active steps

All integer active steps present in reconstructed episodes, including **Step 0 (P0)**.  
No post-hoc step exclusion after outcomes are seen.

### 2.6 Treated eligibility at T (prospective only)

Use `determine_treated_eligibility_at_T` / state reconstruction rules. A treated unit is eligible only if:

1. Cross timestamp reconstructable  
2. `T_cross > episode_start`  
3. Not same-candle ambiguous at the exposure decision  
4. Coverage sufficient through T  
5. Episode alive at T (TP / cycle-close has **not** occurred at or before T)  
6. P(n+1) has **not** occurred at or before T  

**Explicit bans:**

- `terminal_event` **MUST NOT** participate in treated eligibility  
- Future P(n+1), TP, cycle-close, censoring, or ambiguity labels **MUST NOT** participate in treated eligibility  
- The contaminated historical selector `valid_cross = ... and outcome != 'censored'` (and any `terminal_event == pn_plus_1_before_tp` filter) is **FORBIDDEN** for FL-VWAP-006  

Eligibility must be invariant to perturbation of future labels (future-independence).

---

## 3. Landmark / temporal outcome semantics (frozen)

### 3.1 Strict time rule

- Events with `timestamp == T` establish **landmark state only**  
- Competing outcome events must satisfy **`timestamp > T`**  

Implementation reference: `resolve_post_landmark_outcome_from_primitives` / `classify_post_landmark_outcome` after commit `779356f`.

### 3.2 Outcome categories (canonical)

| Label | Meaning |
|-------|---------|
| `PN_PLUS_1_FIRST` | Progression / P(n+1) wins strictly after T |
| `TP_FIRST` | TP / cycle-close wins strictly after T |
| `OTHER_TERMINAL` | Non-PN/TP terminal transition after T |
| `CENSORED` | Valid landmark at T; no usable competing primitive strictly after T (including coverage end) |
| `SAME_CANDLE_AMBIGUOUS` | Competing PN and TP (or multi-event) at the same post-T timestamp / unresolved same-candle order |
| `OTHER_UNKNOWN` | Should be ~0 after corrected accounting; must be reported if non-zero |

Aliases (diagnostic only):  
`pn_plus_1_before_tp` → `PN_PLUS_1_FIRST`; `tp_before_pn_plus_1` → `TP_FIRST`; etc.

### 3.3 Coverage-end rule

Valid landmark at T **and** no usable primitive observations strictly after T  
⇒ **`CENSORED`**

No special-case outcome repair from stored `terminal_event`.  
Stored historical `terminal_event` is **never** prospective outcome input (parity target only in development tests).

### 3.4 Production transition attachment

Domain events are **TRANSITION_ATTACHED**, not cumulative history.  
Boundary classification precedence matches production streaming builder (`progression` before `tp_hit` when both present on the transition row; else step/cycle change heuristics as implemented).

---

## 4. Control risk set — FL-VWAP-004 G0–G7 (frozen)

**Do not redesign.** Use verified implementation in `phase3b_cross_riskset.py`.

For treated with landmark elapsed `Δ = cross_elapsed_ms`, each candidate control episode in the same stratum defines:

```text
control_landmark_ms = control.episode_start_timestamp_ms + Δ
```

### Gate sequence (named G0–G7 for freeze documentation)

| Gate | Requirement |
|------|-------------|
| **G0** | Exact stratum match: `(symbol, percentage, direction, active_step)`; exclude self `episode_key` / unit id; optional partition equality when partitions exist |
| **G1** | Episode started by landmark: `start_timestamp_ms ≤ control_landmark_ms` |
| **G2** | Coverage through landmark: primitives/coverage span includes landmark |
| **G3** | Alive at landmark: TP/cycle-close **not** at or before landmark |
| **G4** | P(n+1) **not** reached at or before landmark |
| **G5** | Favorable VWAP cross **not** already true at or before landmark (`favorable_vwap_crossed_by_landmark is False`) |
| **G6** | Time-coordinate consistency (no seconds/ms mismatch); no unresolved same-candle ambiguity blocking state |
| **G7** | **Immutable landmark formula held**: `control_landmark_ms == start + treated.cross_elapsed_ms` from matcher through audit and outcome resolution |

**Invariant:** `control_landmark_ms` is **immutable** from G7 through matcher, audit, and outcome resolution.  
Never substitute the treated landmark timestamp for the control’s own landmark.

Control post-landmark outcomes use the **same** strict `> control_landmark_ms` rule.

---

## 5. Matching (frozen)

**Function:** `match_controls_for_cross` / `stream_match_controls_for_cross`  
**Constants:** `K_MATCH = 5`, default seed `'fl-vwap-002-cross'`  
**For FL-VWAP-006:** seed frozen as `'fl-vwap-006-match-v1'` (deterministic; chosen pre-exposure; does not change after outcomes).

| Element | Frozen rule |
|---------|-------------|
| Exact / stratification variables | `(symbol, percentage, direction, active_step)` + partition if present |
| Distance / score | No continuous distance; deterministic SHA-256 tie-break: `sha256(f"{seed}\|{treated_id}\|{control_id}")` ascending |
| Max controls per treated | **K = 5** |
| Replacement / reuse | Controls **may be reused** across treated units |
| Zero eligible controls | Treated unit **excluded** from matched primary analysis; count reported as `eligible_treated_unmatched` |
| Minimum controls | ≥ 1 required to enter matched set |
| Weighting | If `k*` controls selected (`1 ≤ k* ≤ 5`), each weight `w = 1/k*`; treated weight `1.0` |
| Effective control N (binary estimand) | Sum of control weights over controls with binary-resolved outcomes (`PN_PLUS_1_FIRST` or `TP_FIRST`) |
| Unique controls | Count distinct control unit ids in matched assignments |
| Determinism | Identical inputs ⇒ identical assignments (verified by streaming/batch parity tests) |

**No post-exposure re-tuning** of K, seed, stratum variables, or weighting.

---

## 6. Primary validation population (frozen, outcome-blind)

### 6.1 Market / source

| Field | Value |
|-------|-------|
| Venue | Binance |
| Market | **spot** |
| Symbols | BTC, ETH, SOL, ZEC, PAXG (Binance: `BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `ZECUSDT`, `PAXGUSDT`) |
| Timeframe | **1m** OHLC |
| Engine path | GoldenFibo LEGACY OHLC resolve (`OhlcResolveMode.LEGACY`) via production FiboLearn collector/reconstruction |

### 6.2 Acquisition window rule (deterministic; no outcome inspection)

Let:

- `T_dev_end` = max timestamp in development episode/market coverage for these symbols in `~/.hermes/fibolearn/fibolearn.sqlite` lineage  
- `T_005_end` = end of FL-VWAP-005 validation candle coverage (historical; already outcome-exposed)

Then:

```text
acq_start = UTC midnight strictly after max(T_dev_end, T_005_end, 2026-09-16T18:01:00Z)
acq_end   = acq_start + 30 calendar days
```

Using current packaged development end ≈ `2026-09-16T18:01:00Z` and FL-VWAP-005 validation end `2024-12-31`, the **planned** window is:

```text
acq_start = 2026-09-17T00:00:00Z
acq_end   = 2026-10-17T00:00:00Z
```

If at acquisition time the live market has not yet reached `acq_end`, acquisition waits; it does **not** substitute an earlier outcome-exposed range.

**Primary analysis population** = all prospectively eligible matched treated units in this window after integrity gates, across all five symbols, both sides, all default percentages, all steps.

No cleaner/secondary symbol split as **primary**. (Symbol splits are secondary only.)

### 6.3 Warm-up / history

Before `acq_start`, reconstruct engines with sufficient predecessor context so episode state at `acq_start` is well-defined (minimum: continuous 1m history from a documented warm-start ≥ 72h before `acq_start`, or from first available contiguous cache without gap-bridging).  
Scored population includes only units whose **landmarks and post-landmark resolution** fall inside `[acq_start, acq_end)`.

### 6.4 Gaps

See §8. No bridging.

### 6.5 Lineage ID

```text
LINEAGE_ID = FL-VWAP-006-BINANCE-SPOT-1M-20260917-20261017
```

Distinct from development and FL-VWAP-005 lineages.

---

## 7. Data-lineage rules (frozen)

| Lineage | Paths | Role |
|---------|-------|------|
| DEVELOPMENT | `~/.hermes/fibolearn/fibolearn.sqlite`, `GoldenFibo/data/binance_klines.sqlite` | Methodology development only |
| FL-VWAP-005 | `fibolearn/data/fl_vwap_005_binance_validation.sqlite` | Contaminated / not primary validation |
| **FL-VWAP-006** | New store under a dedicated path (to be created only at acquisition), e.g. `fibolearn/data/fl_vwap_006_binance_validation.sqlite` | Sole primary validation lineage |

**Forbidden:** reuse outcome-exposed FL-VWAP-005 periods as primary validation; merge development episodes into 006; silent upsert collisions.

### Pre-outcome acquisition artifacts (required before any effect estimate)

1. Raw-data manifest  
2. Row counts  
3. Timestamp ranges per symbol  
4. Gap counts  
5. Duplicate counts  
6. OHLC/volume integrity checks  
7. Canonical logical-content fingerprint (whole DB)  
8. Per-symbol logical fingerprints  
9. Explicit prior-use classification per symbol (must be `NO_PRIOR_OUTCOME_INSPECTION` for primary claim)

---

## 8. Gap policy (frozen)

- **Never** bridge a missing source interval as continuous  
- **No** interpolation  
- **No** synthetic candles  

If exposure, landmark eligibility, P(n+1), TP, alive-state, ordering, or coverage cannot be determined because of a gap ⇒ classify under insufficient-coverage / **CENSORED** rules.  
Do not infer across the gap.

---

## 9. Ambiguity policy (frozen)

With 1m OHLC, if competing levels/events occur in the same candle and temporal order cannot be established:

⇒ **`SAME_CANDLE_AMBIGUOUS`**

- Do **not** force `PN_PLUS_1_FIRST` or `TP_FIRST`  
- **Exclude** from primary binary estimand denominator  
- **Report** count and rate separately  

---

## 10. Primary estimand (exactly one)

### 10.1 Question

Among prospectively eligible favorable VWAP-cross treated landmarks compared with their preregistered matched risk-set controls, what is the difference in subsequent **`PN_PLUS_1_FIRST` versus `TP_FIRST`**?

### 10.2 Denominators

| Arm | Inclusion in primary binary rates |
|-----|-----------------------------------|
| Treated | Matched treated units with post-landmark outcome ∈ {`PN_PLUS_1_FIRST`, `TP_FIRST`} |
| Control | Matched control assignments with weight `w`, outcome ∈ {`PN_PLUS_1_FIRST`, `TP_FIRST`} |

### 10.3 Handling of other labels

| Label | Primary binary estimand | Reporting |
|-------|-------------------------|-----------|
| `OTHER_TERMINAL` | Excluded from binary denominator | Count/rate by arm |
| `CENSORED` | Excluded from binary denominator | Count/rate by arm |
| `SAME_CANDLE_AMBIGUOUS` | Excluded from binary denominator | Count/rate by arm |
| `OTHER_UNKNOWN` | Excluded; integrity concern if non-negligible | Count + forensic |

### 10.4 Primary effect measures (point estimates)

All computed on the binary-resolved matched population:

1. **Primary point contrast:** risk difference  
   `RD = p_treated - p_control`  
   where `p = PN_PLUS_1_FIRST / (PN_PLUS_1_FIRST + TP_FIRST)` (control uses weights)
2. **Companion:** relative risk `RR = p_treated / p_control` (if `p_control > 0`)
3. **Companion:** odds ratio with Haldane–Anscombe 0.5 correction (descriptive companion only; **not** the primary inferential CI basis)

**There is exactly one primary estimand:** matched RD on binary PN-first vs TP-first.

---

## 11. Statistical inference (frozen; accounts for dependence)

Naive independent-Bernoulli / naive OR Wald CI is **not** the primary inferential CI  
(`ci_accounts_for_reused_controls=false` paths are forbidden as primary).

### 11.1 Primary CI method: matched-set cluster bootstrap

1. Let each **matched treated set** be a cluster (treated unit + its selected controls and weights).  
2. Resample clusters **with replacement**, B = **5000** bootstrap replicates (fixed).  
3. For each replicate, recompute weighted `p_treated`, `p_control`, and `RD`.  
4. Primary 95% CI = empirical 2.5 and 97.5 percentiles of bootstrap RD.  
5. Report also bootstrap CIs for RR as secondary inferential companions.

This respects:

- control reuse (weights stay inside resampled clusters)  
- multiple treated landmarks (clusters are the independent resample units under the working assumption)  
- weighting  

### 11.2 Dependence / ESS reporting (required)

Always report:

- `matched_treated_n`  
- `matched_treated_binary_n`  
- `unique_controls`  
- `sum_control_weights_binary` (effective control N for binary outcomes)  
- control reuse distribution (median, p90, p95, max)  
- design effect note: reuse can inflate precision under naive CI; bootstrap is primary  

### 11.3 Working assumptions (explicit)

Bootstrap clusters treated matched sets as exchangeable. Residual within-symbol temporal dependence may remain; this is acknowledged. No model-based claim of full independence across time.

---

## 12. Secondary / exploratory analyses (frozen list)

Secondary analyses **cannot replace** an unfavorable primary result.

Allowed secondary (pre-specified):

1. BUY vs SELL  
2. By symbol  
3. By percentage/scale  
4. By active step (P0, P1, P2, P3, P4, P5+)  
5. Early vs middle vs late cross (`cross_fraction_elapsed` tertiles using episode duration available at T only if duration-to-now is not future-leaky; prefer elapsed-from-start bins fixed at T: early ≤ 1h, mid ≤ 6h, late > 6h wall-clock elapsed — **elapsed-from-start only**, no future episode end)  
6. Multiscale context / POC / VAH / VAL / significant levels — **exploratory only**

### Multiple-testing policy

- Primary RD test/CI: **family-wise primary** (no multiplicity penalty)  
- Secondary hypothesis looks: Benjamini–Hochberg FDR **q = 0.10** across the pre-listed secondary RD contrasts that are actually computed  
- Exploratory (item 6): no confirmatory claim language  

---

## 13. Validation success language (frozen)

Not defined as `p < 0.05` alone.

| Label | Objective criteria (all integrity gates PASS first) |
|-------|-----------------------------------------------------|
| **SUPPORTED_HISTORICAL_ASSOCIATION** | `RD > 0` and bootstrap 95% CI for RD **excludes 0** and `matched_treated_binary_n ≥ 100` and `unique_controls ≥ 30` |
| **NOT_SUPPORTED** | `matched_treated_binary_n ≥ 100` and `unique_controls ≥ 30` and bootstrap 95% CI for RD is entirely **≤ 0** |
| **INCONCLUSIVE** | Integrity PASS but (CI includes 0) or sample below thresholds above |
| **INSUFFICIENT_SAMPLE** | Integrity PASS but `matched_treated_binary_n < 100` or `unique_controls < 30` |
| **INVALIDATED_DATA_INTEGRITY** | Any critical data/lineage/fingerprint gate FAIL |
| **INVALIDATED_METHODOLOGY** | Future-independence / terminal independence / landmark immutability / strict `>T` gate FAIL |

Point magnitude must be reported with CI and ESS; labels above are the only allowed top-line classifications.

---

## 14. Integrity gates (all required PASS before interpreting effects)

1. Raw-data fingerprint match to freeze-time acquisition manifest  
2. Raw-data integrity (OHLC, volume, duplicates, gap inventory)  
3. No prohibited development / FL-VWAP-005 primary overlap  
4. Future-independence tests (eligibility unchanged under future-label perturbation)  
5. `terminal_event` independence for eligibility and matching  
6. G0–G7 invariants  
7. Immutable control landmarks  
8. Matcher determinism (repeat hash equality)  
9. Outcome resolver strict `>T` semantics  
10. Ambiguity handling as frozen  
11. **No synthetic fallback** unless explicitly counted and preregistered (default: **synthetic_fallback_count must be 0**)  
12. Deterministic repeat of full assignment+outcome accounting  
13. Accounting sums (categories partition matched population)  
14. Source lineage verification (`LINEAGE_ID`)  
15. Episode-identity uniqueness: zero silent key collisions/upserts in 006 store  

If any **critical** gate fails: **do not interpret effect results** until resolved (likely `INVALIDATED_*`).

---

## 15. Episode identity for FL-VWAP-006 (critical freeze)

### 15.1 Historical development artifact (do not import blindly)

Development history used **3-day chunk engine resets**.  
`cycle_id` restarts ⇒ short `episode_key` can recur; packaged unique upserts can drop earlier collisions (BTC 24h regen: 351 window vs 123 packaged keys).  
That is a **keyspace artifact**, not permission to lose validation mass.

### 15.2 FL-VWAP-006 construction rules

1. **Preferred generation:** continuous engine state per symbol across the acquisition window (**no 3-day reset**), unless a reset is unavoidable for engineering reasons.  
2. If chunking/reset is used, chunk boundaries **must** be encoded into identity.  
3. **Canonical unit id (frozen):**

```text
unit_id = "{symbol}|{percentage}|{direction}|{active_step}|{start_timestamp_ms}|{cycle_id}|{run_id}"
```

where `run_id` is a constant per acquisition build (e.g. `FL006V1`).

4. Store **UNIQUE(unit_id)**; **forbid** overwrite upserts.  
5. Short display keys may exist but must not be the uniqueness key.  
6. Validation population membership is by `unit_id`, not by colliding short keys.

**Episode-key collision issue:** **RESOLVED/FROZEN** by the above rules (not by changing GoldenFibo live behavior).

---

## 16. Prohibited post-exposure changes

After any FL-VWAP-006 outcome becomes visible:

- No changing exposure, landmark, matcher, estimand, CI method, gates, or success language  
- No dropping symbols/steps because results look bad  
- No promoting secondary to primary  
- No rehabilitating FL-VWAP-005 INVALID effects  
- No quiet gap bridging or synthetic candles  

---

## 17. Pre-exposure semantic tests (no 006 data)

Executed at freeze time on synthetic fixtures + development/005-contaminated code paths only:

| Suite | Result |
|-------|--------|
| `tests/test_fl_vwap_005_outcome_resolver.py` | PASS |
| `tests/test_phase3b_cross_riskset.py` | PASS |
| `tests/test_phase3b_landmark_reconstruction.py` | PASS |
| `tests/test_phase3b_corrected.py` | PASS |
| `tests/test_fibolearn_ambiguity_policy.py` | PASS |
| **Total** | **54 passed** |

These establish:

- treated eligibility ignores `terminal_event`  
- future-label independence patterns covered by existing tests  
- `==T` vs `>T` outcome timing  
- control landmark formula  
- ambiguity / matcher determinism surfaces  

**No FL-VWAP-006 data required or used.**

---

## 18. Unresolved critical decisions

**None remaining for freeze.**  

Decisions explicitly frozen above that were previously underspecified in FL-VWAP-005:

- primary CI = matched-set cluster bootstrap (not naive OR CI)  
- 006 episode `unit_id` uniqueness rule  
- outcome-blind 30-day acquisition calendar rule  
- single primary estimand = matched RD  

---

## 19. Operational notes (not code changes)

- Open reference DBs read-only or via scratch copies (`KlineCache` may write WAL/availability metadata).  
- Do not commit large validation SQLite files without explicit packaging process.  

---

## 20. Ready flags

```
METHODOLOGY_FROZEN_PRE_EXPOSURE = YES
READY_TO_ACQUIRE_FL_VWAP_006 = YES
```

Acquisition is allowed **only after** this freeze commit is on `origin/main` and **must** produce the §7 pre-outcome artifacts before any effect estimate.
