# FL-VWAP-006 raw acquisition — append policy

**Status:** `ACQUISITION_IN_PROGRESS`  
**Lineage ID:** `FL-VWAP-006-BINANCE-SPOT-1M-20260917-20261017`  
**Methodology freeze:** `69b4da676966f83822a66b8fc54068633004ca45`  
**Raw DB (not Git):** `/root/kam/fibolearn/data/fl_vwap_006_binance_validation.sqlite`

This document defines the **append-safe** acquisition procedure for the incomplete FL-VWAP-006 raw candle lineage. It does **not** change methodology, run outcomes, or effect analysis.

Tracked manifests:

- `fibolearn/reports/fl_vwap_006_raw_data_manifest.json`
- `fibolearn/reports/fl_vwap_006_acquisition_status.json`

---

## 1. Scope of this lineage

| Field | Value |
|-------|-------|
| Venue | Binance |
| Market | spot |
| Timeframe | 1m |
| Symbols | BTC, ETH, SOL, ZEC, PAXG |
| Preregistered start | 2026-09-17T00:00:00Z |
| Preregistered end | 2026-10-17T00:00:00Z (exclusive open-time bound) |
| Completeness | Incomplete until end date has passed and full range is filled |

Separate from:

- development `~/.hermes/fibolearn/fibolearn.sqlite`
- development `GoldenFibo/data/binance_klines.sqlite`
- FL-VWAP-005 `fibolearn/data/fl_vwap_005_binance_validation.sqlite`

---

## 2. Immutability rules

1. **Existing acquired raw minutes must not silently change.**  
2. Subsequent runs **append only** newly available `open_time` values with `open_time >= prereg_start` and `open_time < min(prereg_end, next_closed_minute)`.  
3. **Duplicates:**  
   - If `(symbol, open_time)` already exists and OHLC/volume fields are **byte-identical** → skip (idempotent).  
   - If `(symbol, open_time)` exists and any field **differs** → **STOP**, leave DB unchanged, report conflict.  
4. **No interpolation. No synthetic candles. No gap bridging.**  
5. Source gaps remain explicit in the manifest (`gap_events`, `missing_1m_intervals`).  
6. After each successful acquisition batch, rewrite versioned manifests (update `fl_vwap_006_raw_data_manifest.json` and `fl_vwap_006_acquisition_status.json`; keep prior copy under `.scratch/fl_vwap_006/manifest_history/` if needed).  
7. Recompute **canonical logical fingerprint** and per-symbol fingerprints after each append; physical SHA changes when bytes are appended (expected).  
8. Do **not** open/write development or FL-VWAP-005 DBs during 006 acquisition.  
9. Prefer a dedicated cache path; never use the live development `binance_klines.sqlite` as the 006 store.

---

## 3. Acquisition procedure (next append)

```text
1. Verify methodology freeze commit still ancestor of HEAD.
2. Snapshot size/mtime/sha256 of development + 005 DBs (must remain unchanged).
3. Open 006 DB; determine per-symbol max(open_time).
4. fetch_start = max(prereg_start, max_open_time + 60_000) per symbol
   fetch_end   = min(prereg_end, floor_to_minute(now) )  # closed bars only
5. If fetch_start >= fetch_end: nothing to append; refresh status only.
6. Fetch Binance spot 1m klines into memory/scratch.
7. For each row: INSERT OR conflict-check as above.
8. Integrity scan (gaps, dups, OHLC, volume).
9. Write new manifests + acquisition_log row.
10. Re-verify protected DBs unchanged.
```

Do **not** run episode rebuild, matching, or effect analysis as part of append.

---

## 4. Completion rule

When wall-clock ≥ 2026-10-17T00:00:00Z **and** each symbol has contiguous (or gap-documented) coverage for all available minutes in `[prereg_start, prereg_end)`:

```text
acquisition_status = ACQUISITION_COMPLETE_PENDING_OUTCOME_PHASE
```

Only then may a **separate** task begin FL-VWAP-006 reconstruction/analysis under the frozen preregistration.  
Until then: `ACQUISITION_IN_PROGRESS`.

---

## 5. Safety

- No GoldenFibo trading behavior changes  
- No orders / trading services  
- No `/tmp` for large files — use `/root/kam/.scratch/fl_vwap_006/`  
- SQLite file stays gitignored  
