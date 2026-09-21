#!/usr/bin/env python3
"""Read-only verification of closed development prospective outcome accounting.

Does NOT compute effect sizes. Does NOT touch FL-VWAP-006.
Does NOT recompute outcomes from candles (expensive); validates frozen report JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXPECTED = {
    "raw_favorable_crosses": 2176,
    "prospective_eligible_treated": 2176,
    "matched_treated": 2047,
    "PN_PLUS_1_FIRST": 779,
    "TP_FIRST": 1001,
    "OTHER_TERMINAL": 258,
    "CENSORED": 9,
    "SAME_CANDLE_AMBIGUOUS": 0,
    "OTHER_UNKNOWN": 0,
    "real_primitive_source_count": 2047,
    "synthetic_fallback_count": 0,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--accounting",
        default="/root/kam/fibolearn/reports/fl_vwap_corrected_development_outcome_accounting.json",
    )
    args = ap.parse_args()
    path = Path(args.accounting)
    if not path.is_file():
        print(f"FAIL missing accounting file: {path}", file=sys.stderr)
        return 2
    data = json.loads(path.read_text())
    ups = data.get("upstream_population_counts") or {}
    outcomes = data.get("outcome_counts") or {}
    got = {
        "raw_favorable_crosses": ups.get("raw_favorable_crosses"),
        "prospective_eligible_treated": ups.get("prospective_eligible_treated"),
        "matched_treated": ups.get("matched_treated"),
        "PN_PLUS_1_FIRST": outcomes.get("PN_PLUS_1_FIRST"),
        "TP_FIRST": outcomes.get("TP_FIRST"),
        "OTHER_TERMINAL": outcomes.get("OTHER_TERMINAL"),
        "CENSORED": outcomes.get("CENSORED"),
        "SAME_CANDLE_AMBIGUOUS": outcomes.get("SAME_CANDLE_AMBIGUOUS"),
        "OTHER_UNKNOWN": outcomes.get("OTHER_UNKNOWN"),
        "real_primitive_source_count": data.get("real_primitive_source_count"),
        "synthetic_fallback_count": data.get("synthetic_fallback_count"),
    }
    rc = 0
    for k, exp in EXPECTED.items():
        actual = got.get(k)
        status = "OK" if actual == exp else "FAIL"
        print(f"{status} {k}: expected={exp} actual={actual}")
        if actual != exp:
            rc = 1
    total = sum(
        int(outcomes.get(k) or 0)
        for k in (
            "PN_PLUS_1_FIRST",
            "TP_FIRST",
            "OTHER_TERMINAL",
            "CENSORED",
            "SAME_CANDLE_AMBIGUOUS",
            "OTHER_UNKNOWN",
        )
    )
    print(f"{'OK' if total == 2047 else 'FAIL'} outcome_counts_sum: expected=2047 actual={total}")
    if total != 2047:
        rc = 1
    verdict = data.get("verdict")
    print(f"verdict={verdict}")
    if rc == 0:
        print("CHECKPOINT_ACCOUNTING_VERIFIED")
    else:
        print("CHECKPOINT_ACCOUNTING_MISMATCH")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
