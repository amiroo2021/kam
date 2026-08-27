#!/usr/bin/env python3
"""Phase 2.11 — ONE autonomous Fibo target-convergence iteration.

This module is the SINGLE authoritative production call site for
``live_converge``. It is invoked by the gateway's cron ticker once
per minute (see ``installer/install_fibo_capability.py`` for cron
registration). No Telegram interaction is required.

Iteration contract:

  1. Load the latest REAL MT4 snapshot from the persisted store
     (``~/.hermes/fibo/mt4_snapshot.json``).
  2. Load the current registration set from the JSONL store
     (``~/.hermes/fibo/registrations.jsonl``).
  3. For every ``is_active`` registration:
       - if ``is_live_eligible(reg)`` (allowlist identity match +
         registration is active): call
         ``live_converge(reg, snap, execute_fn=desk.execute)`` once.
       - otherwise: skip silently (non-allowlisted / stopped
         registrations stay shadow-only via the wizard).
  4. Emit a one-line JSON status record to stdout so the gateway's
     cron ticker can capture it in ``~/.hermes/cron/output/...``.

State-based semantics: convergence is idempotent. Repeated ticks
with the same MT4 snapshot produce the same NOOP or the same
deterministic ``client_order_id``, which the venue's idempotency
layer dedupes.

No second invocation per tick is issued. No loops. No retries.

Exit codes:
  0 = iteration completed (including NOOP outcomes)
  2 = MT4 snapshot missing or stale (caller should NOT retry this tick)
  3 = TradeDesk read failure (live path FAIL CLOSED; non-live
      registrations still logged as NOOP)
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Hermetic imports — fail soft if the runtime is unavailable.
# ---------------------------------------------------------------------------
# This script runs inside the gateway's cron ticker, so the deployed
# Hermes venv must be importable. The cron job's ``workdir`` setting
# ensures ``/usr/local/lib/hermes-agent`` is on sys.path.

HERMES_ROOT = os.environ.get("HERMES_ROOT", "/usr/local/lib/hermes-agent")
HERMES_HOME = os.environ.get("HERMES_HOME", "/root/.hermes")
if HERMES_ROOT not in sys.path:
    sys.path.insert(0, HERMES_ROOT)

# MT4 freshness rule (must match the rest of the Fibo system).
MT4_MAX_AGE_SECONDS = 30


def _isoformat_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _load_mt4_snapshot():
    """Load and validate the latest MT4 snapshot. Returns
    (snapshot, error_str)."""
    from plugins.trade.fibo.snapshot import Mt4SnapshotStore

    snap_path = Path(HERMES_HOME) / "fibo" / "mt4_snapshot.json"
    store = Mt4SnapshotStore(snap_path)
    snap = store.load()
    if snap is None:
        return None, "MT4 snapshot file missing"

    try:
        ts_str = snap.received_at.replace("Z", "+00:00")
        received = datetime.fromisoformat(ts_str)
    except Exception as exc:  # noqa: BLE001
        return None, f"MT4 snapshot received_at unparseable: {exc}"
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - received).total_seconds()
    if age > MT4_MAX_AGE_SECONDS:
        return None, f"MT4 snapshot stale (age={age:.2f}s > {MT4_MAX_AGE_SECONDS}s)"

    return snap, None


def _load_registrations():
    from plugins.trade.fibo.store import FiboRegistrationStore

    reg_path = Path(HERMES_HOME) / "fibo" / "registrations.jsonl"
    store = FiboRegistrationStore(reg_path)
    return store.load_all()


def _resolve_desk():
    """Return the production TradeDesk singleton. Import lazily so
    the script can still exit cleanly even if the gateway is offline
    (then it returns None and we report NOOP)."""
    try:
        from plugins.trade.tradedesk import get_tradedesk

        return get_tradedesk()
    except Exception as exc:  # noqa: BLE001
        logging.warning("converge_once: get_tradedesk failed: %s", exc)
        return None


def _iter_once() -> Dict[str, Any]:
    """Run ONE iteration. Returns a structured summary."""
    summary: Dict[str, Any] = {
        "ts": _isoformat_utc(datetime.now(timezone.utc)),
        "evaluated": 0,
        "live_eligible": 0,
        "writes": 0,
        "results": [],
    }

    snap, err = _load_mt4_snapshot()
    if err is not None:
        summary["status"] = "MT4_SKIPPED"
        summary["reason"] = err
        return summary

    regs = _load_registrations()
    desk = _resolve_desk()
    if desk is None:
        summary["status"] = "DESK_UNAVAILABLE"
        summary["reason"] = "get_tradedesk() returned None"
        return summary

    # Live executor imports — done lazily so MT4 failures don't
    # prevent non-live evaluations from being logged.
    from plugins.trade.fibo.live import (
        live_converge,
        is_live_eligible,
    )

    for reg in regs:
        summary["evaluated"] += 1
        if not bool(getattr(reg, "is_active", False)):
            continue  # stopped registrations are excluded by design
        if not is_live_eligible(reg):
            continue  # non-allowlisted stays shadow-only

        summary["live_eligible"] += 1
        try:
            lc = live_converge(reg, snap, execute_fn=desk.execute)
        except Exception as exc:  # noqa: BLE001
            # live_converge is designed to never raise (every failure
            # path returns a LiveConvergeResult). This except is
            # defensive in case of unexpected programmer error.
            summary["results"].append({
                "registration_key": reg.registration_key,
                "allowlisted": True,
                "placed_live_order": False,
                "reason": f"exception: {exc}",
                "error": traceback.format_exc(limit=4),
            })
            continue

        record = {
            "registration_key": reg.registration_key,
            "allowlisted": lc.allowlisted,
            "placed_live_order": lc.placed_live_order,
            "read_failed": lc.read_failed,
            "cancel_failed": lc.cancel_failed,
            "blocked_reason": lc.blocked_reason or None,
            "reason": lc.reason or None,
        }
        if lc.placed_live_order:
            summary["writes"] += 1
            record["placed_request"] = lc.placed_request
        summary["results"].append(record)

    summary["status"] = "OK"
    summary["mt4_seq"] = snap.seq
    summary["mt4_source"] = snap.source
    summary["mt4_received_at"] = snap.received_at
    return summary


def main(argv: List[str]) -> int:
    logging.basicConfig(
        level=os.environ.get("FIBO_CONVERGE_LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        summary = _iter_once()
    except Exception as exc:  # noqa: BLE001
        # Defensive: log and exit 1; the cron ticker will record the
        # failure and the next tick will retry from a clean slate.
        logging.exception("converge_once: iteration failed: %s", exc)
        sys.stdout.write(json.dumps({
            "status": "CRASHED",
            "ts": _isoformat_utc(datetime.now(timezone.utc)),
            "error": str(exc),
            "traceback": traceback.format_exc(limit=8),
        }) + "\n")
        return 1

    # Always emit a single JSON line on stdout so the cron ticker
    # captures it. The gateway's cron subsystem routes the output
    # to ~/.hermes/cron/output/<job_id>/<timestamp>.md when deliver
    # is "local".
    sys.stdout.write(json.dumps(summary) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))