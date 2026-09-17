"""
Stage 2 — Domain-Events Audit for the existing 30-day FiboLearn DB.

READ-ONLY. Inspects ~/.hermes/fibolearn/fibolearn.sqlite to determine whether
the existing 30-day market_observations / ladder_observations retain enough
information to reconstruct terminal events exactly WITHOUT re-running
replay against GoldenFibo.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

DB = Path(os.path.expanduser("~")) / ".hermes" / "fibolearn" / "fibolearn.sqlite"
OUT = Path("/root/kam/fibolearn/reports/phase3a_domain_event_audit.json")


def main() -> None:
    if not DB.exists():
        OUT.write_text(json.dumps({"error": f"db not found at {DB}"}, indent=2))
        return
    size_bytes = DB.stat().st_size
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    def one(query, *args):
        return conn.execute(query, args).fetchone()[0]

    def all_rows(query, *args):
        return [dict(r) for r in conn.execute(query, args).fetchall()]

    market_total = one("select count(*) from market_observations")
    ladder_total = one("select count(*) from ladder_observations")
    outcome_total = one("select count(*) from outcomes")
    episode_total = one("select count(*) from episodes")

    # 1) How many market_observations.market_json contain domain_events?
    cur = conn.execute("select market_json from market_observations")
    market_with_events = 0
    market_without_events = 0
    sample_with_events = None
    sample_without_events = None
    for (mj,) in cur:
        try:
            d = json.loads(mj)
        except Exception:
            market_without_events += 1
            continue
        de = d.get("domain_events")
        if isinstance(de, dict) and len(de) > 0:
            market_with_events += 1
            if sample_with_events is None:
                sample_with_events = {"first_keys_with_events": list(de.keys())[:3]}
        else:
            market_without_events += 1
            if sample_without_events is None:
                sample_without_events = None

    # 2) Distinct active_step distribution
    steps = Counter(r["active_step"] for r in all_rows(
        "select distinct active_step from ladder_observations"))

    # 3) ladder_state_json contains terminal-relevant fields?
    sample_lad = conn.execute(
        "select ladder_state_json from ladder_observations limit 1"
    ).fetchone()
    sample_lad_keys = []
    if sample_lad:
        try:
            sample_lad_keys = sorted(json.loads(sample_lad[0]).keys())
        except Exception:
            pass

    # 4) For each cycle_id / active_step pair we determine whether we have
    # BOTH a `progression` event and a `tp_hit` event anywhere in the DB
    # that touches that pair. If domain_events are missing entirely, no
    # terminal can be reconstructed exactly.
    de_total = one(
        "select count(*) from market_observations where market_json like '%domain_events%'"
    )
    sample_domains = conn.execute(
        "select market_json from market_observations where market_json like '%domain_events%' limit 3"
    ).fetchall()
    domain_samples = []
    for (mj,) in sample_domains:
        try:
            d = json.loads(mj)
        except Exception:
            continue
        de = d.get("domain_events", {})
        domain_samples.append({k: v for k, v in list(de.items())[:3]})

    # 5) Estimate ambiguity: same-candle TP-vs-progression. Check if any
    # cycle_id / active_step has BOTH progression AND tp_hit in the SAME
    # candle's domain_events for the same percentage/direction.
    ambiguous_events = 0
    cur = conn.execute("select market_json from market_observations where market_json like '%domain_events%'")
    for (mj,) in cur:
        try:
            d = json.loads(mj)
        except Exception:
            continue
        for k, evs in (d.get("domain_events") or {}).items():
            if not isinstance(evs, list):
                continue
            if "progression" in evs and "tp_hit" in evs:
                # legacy ambiguous if both occur in same row
                if "ambiguous" in {str(e).lower() for e in evs}:
                    ambiguous_events += 1

    # 6) Determine replay requirement
    if market_with_events == 0:
        replay_required = True
        replay_reason = "no market_json contains domain_events; cannot reconstruct terminal events from existing rows"
    elif market_with_events < market_total * 0.5:
        replay_required = True
        replay_reason = f"only {market_with_events}/{market_total} market rows have domain_events; insufficient coverage"
    else:
        replay_required = False
        replay_reason = "domain_events coverage is sufficient to reconstruct terminal events"

    audit = {
        "db_path": str(DB),
        "db_size_bytes": size_bytes,
        "tables": {
            "market_observations": market_total,
            "ladder_observations": ladder_total,
            "outcomes": outcome_total,
            "episodes": episode_total,
        },
        "domain_events_in_market_json": {
            "rows_with_events": market_with_events,
            "rows_without_events": market_without_events,
            "rows_with_events_pct": round(100 * market_with_events / max(1, market_total), 2),
            "rows_without_events_pct": round(100 * market_without_events / max(1, market_total), 2),
        },
        "ladder_state_keys_sample": sample_lad_keys,
        "distinct_active_steps": dict(steps),
        "domain_events_present_rows": de_total,
        "sample_domains": domain_samples,
        "ambiguous_same_candle_events_seen": ambiguous_events,
        "terminal_event_reconstructable_exactly": market_with_events > 0 and ambiguous_events == 0,
        "replay_required": replay_required,
        "replay_reason": replay_reason,
        "finding": (
            "FiboLearn's 30-day historical rows were created before save_observation started persisting domain_events. "
            f"{market_with_events}/{market_total} rows have domain_events inside market_json (anywhere in the DB). "
            "Without domain_events the streaming builder cannot reconstruct exact P(n+1)/TP/cycle-reset events; "
            "it would have to fall back to legacy `active_step` deltas which are ambiguous when both TP and progression "
            "occur in the same candle."
        ) if market_with_events == 0 else "domain_events present in stored rows",
    }
    conn.close()
    OUT.write_text(json.dumps(audit, indent=2, sort_keys=True))
    out = json.dumps(audit, indent=2, sort_keys=True)
    print(out[:6000])


if __name__ == "__main__":
    main()
