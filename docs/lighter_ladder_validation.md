# Lighter ladder validation — phases 5.1 and 6

Operational record of the Lighter Premium-aware ladder work on `bitget` /
`ROBINHOOD` (account index 3499, api_key_index 4), and the rules a future
harness must follow.

Live artifacts (outside this repository) live under
`/root/workspace/lighter-phase5-20261121/` and
`/root/workspace/lighter-phase6-20261121/`.

## Phase 5.1 — bounded read-after-write reconciliation

Commit `c3983e16b03f450d7cdde6b163d2722e63ac5893`
(`fix(lighter): bounded read-after-write reconciliation after a WS batch`).

### The defect

Phase 5 placed 100 orders in two WS batches of 50. Batch 1 reconciled
50/50. Batch 2's reconciliation returned **0/50** — even though the
exchange had returned `outcome=success, api_code=200` and 50 tx hashes
for every child, and a follow-up read a moment later found all 50 on the
book with the expected `client_order_index`.

`GET /api/v1/accountActiveOrders` is unfiltered and unpaginated, so the
staleness is **server-side indexing latency**, not a client cache. A
single read can therefore miss a batch that definitively landed.

### The fix

`_reconcile_batch_by_client_order_index` is now a **bounded read-after-write
pass**, never a single read and never a resend:

| Property | Value |
|---|---|
| Deadline | 10 s (`LIGHTER_RECONCILE_DEADLINE_SECONDS`) |
| Backoff | 0.25 s → 0.5 s → 1.0 s |
| Hard attempt cap | 40 (`LIGHTER_RECONCILE_MAX_ATTEMPTS`) |

* Confirmed children **accumulate** across snapshots. A child confirmed by
  any snapshot stays confirmed, even if a later snapshot drops it because it
  filled.
* A child does **not** have to remain open to be confirmed — a filled child
  still proves it landed.
* Identity is checked on market, side, price, size and type.
* A child absent from every snapshot stays **unresolved** and is never
  counted as accepted.
* Children the exchange explicitly **rejected** are excluded from the
  expected set: they provably did not land, so waiting for them would
  turn a definite rejection into a false "pending" verdict and burn the
  whole deadline.
* The ladder runs the same pass on the **abnormal** path. A stale snapshot
  tells you nothing, and cleanup must cancel every child that landed.
* `cancel_order_group` accepts `client_order_indices` as an additional
  exact-identity targeting mode, so unresolved children (known by client
  order index, possibly without a known order id) can still be reached.

### Two further defects found while building it

* **An unbounded loop.** The deadline was only checked *after* each read,
  so with a mocked sleep the pass spun **1,251,224 times**. Fixed with the
  hard attempt cap and by moving the deadline check ahead of the read.
* **Rejection counted as "pending".** API-rejected children were in the
  expected set, so the pass waited out its full deadline for children that
  could never appear. Fixed by excluding them.

## Phase 6 — 250-order live validation

Deployed agent SHA
`0e65b4f90019f46539f4d34b5f11952830da5c4f662030d0d1bcaee0cd1662ab`
(unchanged by Phase 6 — no production code was modified).

**Result: 250/250 accepted, 250/250 reconciled, 250/250 cancelled, account
clean.**

### Sizing — check the live exchange minimums first

The live market metadata for SOL perp (market index 3) reported
`min_quote_amount = 10.000000`, `min_base_amount = 0.100`,
`size_decimals = 3`.

A 0.250 SOL order at $30.100 is worth **$7.525** — below the $10 minimum
and rejected. The ladder must therefore be sized for the **lowest** price
in the band, not the highest: the bottom of the ladder carries the
smallest notional.

| | 0.250 SOL | 0.400 SOL (executed) |
|---|---|---|
| Total quantity | 62.500 SOL | 100.000 SOL |
| Total notional | $2,659.375 | $4,255.000 |
| Lowest order notional | $7.525 ✗ | $12.040 ✓ |

`plugins.trade.ladder_harness.minimum_order_size` computes the minimum
size that clears both minimums at the lowest band price, so the harness
does not have to hard-code a size that the exchange can invalidate.

### Ladder

250 unique prices, contiguous and strictly descending, in five bands of 50:

| Band | Prices |
|---|---|
| 1 | $55.000 → $50.100 |
| 2 | $50.000 → $45.100 |
| 3 | $45.000 → $40.100 |
| 4 | $40.000 → $35.100 |
| 5 | $35.000 → $30.100 |

SOL perp market id 3, GTT limit, reduce_only=false, BUY, 0.400 SOL.

### Results

| Batch | API | Accepted | Rejected | Unknown | Reconciled | Batch ms | Reconcile ms |
|---|---|---|---|---|---|---|---|
| 1 | 200 | 50 | 0 | 0 | 50/50 | 352.0 | 287.3 |
| 2 | 200 | 50 | 0 | 0 | 50/50 | 359.3 | 290.8 |
| 3 | 200 | 50 | 0 | 0 | 50/50 | 365.4 | 596.9 |
| 4 | 200 | 50 | 0 | 0 | 50/50 | 357.9 | 559.7 |
| 5 | 200 | 50 | 0 | 0 | 50/50 | 384.5 | 583.3 |

WS connect 685.7 ms; total submission wall time ~4.82 s for 250 orders.
No local pacing wait: 250 of the 500 tx/60 s create budget and 250 of the
1000/60 s cancel budget were used, and no rate limit was hit.

Cleanup used the deployed production path — REST chunks of 15 (16 × 15 +
1 × 10 = 17 chunks), exact order ids only, no cancel-all, no symbol-wide
cancellation, no blind retries.

Final verification (25 independent checks, all passing): zero remaining
SOL orders, zero SOL positions, the 80 pre-existing ZEC orders preserved
with an identical id set and unchanged partial-fill row, `next_nonce`
2278 → 2778 (delta exactly 500 for 250 creates + 250 cancels), volume
quota delta 249 (creates only), and no outstanding ambiguous
transactions.

### Every batch's first reconciliation read was stale

This is the Phase 5.1 fix working as designed: every batch's first read
came back before the exchange had indexed the just-accepted children, and
every one was absorbed by the second read. Phase 5's blocker — a stale
read treated as a landing failure — is closed.

## Harness rules

Two defects in the Phase 6 harness cost real evidence. Both are now
encoded in `plugins/trade/ladder_harness.py` and covered by
`plugins/trade/tests/test_ladder_harness.py`.

### 1. Persist evidence incrementally

The Phase 6 harness crashed in its **reporting** code — `unresolved_cois`
was referenced in the `execute` dict before it was assigned — *after* all
five batches had been placed and reconciled, but *before* it wrote
`execute.json`. The per-batch timings, read counts and exchange order ids
lived only in memory, so the crash destroyed the run's primary evidence.

The trading path was never involved: no order was misplaced, no retry was
issued, no duplicate was created. The cleanup was then run by a separate
script that performed its own read-only discovery from the exchange, so it
did not depend on the lost bookkeeping.

Rules:

* Write each batch's evidence as it completes, not at the end of the run.
  `EvidenceLog` appends one JSONL record per batch and flushes it to disk
  immediately.
* Compute every value **before** the dict that consumes it.
* A torn final line from a hard kill must not lose the complete records
  before it.

### 2. Capture cancellation chunk timings from the ASYNC core

The Phase 6 harness patched the sync `_submit_cancel_tx_batch` wrapper.
The production group runner (`_run_cancel_tx_group`) awaits the **async**
core (`_submit_cancel_tx_batch_async`) directly, so the patch never fired
and the chunk timings were silently absent.

`time_cancel_chunks` patches the async core — the seam every chunk
actually flows through — and restores the original object in a `finally`.
It also **fails loudly** when a group ran chunks but no timing was
captured: reporting "0 chunks" would read as "the cancel was instant",
which is false.

The chunk-count and wall-time numbers from Phase 6 came from the runner's
own accounting, not from measured per-chunk timings.

### 3. Do not infer read counts from timings alone

The 0.25 s backoff overlaps the read latency range, so read counts cannot
be inferred from `reconcile_ms` alone. On this host `accountActiveOrders`
measured min 16.4 ms / median 17.9 ms / max 292.6 ms over 8 samples.

* **3 reads is impossible** at any observed value: the backoff alone costs
  0.75 s, and no observed `reconcile_ms` exceeded 0.60 s.
* **1 vs 2 reads** needs the latency distribution to disambiguate.

The Phase 6 run's per-batch read counts are therefore labelled *inferred*
in the artifact, not measured.

## Reconciliation rules

* Never treat a 0-match reconcile as proof the batch did not land.
  Cross-check the WS ack (`api_code`, tx hash count, nonces) and re-read
  the book.
* A reconcile that returns 0 matches for a **positively acknowledged**
  batch should be retried after a short backoff, not treated as a hard
  failure.
* Always run an independent stray sweep after the submit phase: match every
  open order whose `client_order_index` is in the submitted set but which
  is missing from the reconciliation map, and include those in the cancel
  targets. The stray sweep is what guarantees no order is left behind.
* HTTP 200 and a `tx_hash` prove API accept only. Landing proof is
  `client_order_index` on `accountActiveOrders`.
