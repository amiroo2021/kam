"""Phase 2 — Running Fibo dry-run rendering.

Spec §11: extend "Running Fibo" to show the latest READ-ONLY
reconciliation state. For each registration show something
compact like:

    ETHUSD NORMALFIB SELL
    OndoPerps / BITGET
    MT4: cycle 46871101 · weight 1
    Target: SHORT 0.001
    Actual: FLAT
    Delta: OPEN_SHORT 0.001
    Mode: DRY RUN

No buttons that execute anything. The only button on this
screen is ❌ Exit (the existing UI-only close). The screen is a
view-only snapshot — pressing it repeatedly just re-renders
with the latest data.
"""

from __future__ import annotations

import logging
from typing import List

from .reconciler import (
    DeltaAction,
    FiboReconciler,
    ReconciliationResult,
    Side,
    render_table,
)

logger = logging.getLogger(__name__)


# Compact per-registration block for the wizard screen.
def _compact_block(r: ReconciliationResult) -> str:
    # Phase 2.1: show source vs exchange distinctly. For legacy
    # records (exchange_instrument empty), we flag it.
    venue_token = r.exchange_instrument or ": not selected"
    sym_variant = (
        f"{r.source_symbol} {r.variant} {r.side}"
    )
    exchange_account = f"{r.exchange} / {r.account}"
    # Show the exchange market on the same line as MT4 (compact).
    mt4 = (
        f"MT4 src: {r.source_symbol} → venue {venue_token}\n"
        f"   cycle {r.mt4_cycle_id} · weight {r.mt4_weight} · "
        f"pct {r.mt4_percentage} · age "
        f"{('%.1fs' % r.mt4_age_seconds) if r.mt4_age_seconds is not None else '?'}"
    )
    target = f"Target: {r.desired_side} {r.desired_size}"
    actual = f"Actual: {r.actual_side} {r.actual_size}"
    delta = f"Delta: {r.delta_action} {r.delta_size}"
    # Phase 2.13.20: derive the displayed mode from the live
    # runtime state. If fibo-converge.timer is active AND the
    # registration has a persisted synchronized cycle, this is
    # the LIVE convergence path — display "LIVE". Otherwise
    # (dry-run only / no live timer) keep the legacy "DRY RUN"
    # label so the operator can never confuse UI shadow with
    # actual execution.
    mode_label = _resolve_mode_label(
        registration_key=r.registration_key,
        source=r.source_symbol,
    )
    return (
        f"📌 {sym_variant}\n"
        f"   {exchange_account}\n"
        f"   {mt4}\n"
        f"   {target}\n"
        f"   {actual}\n"
        f"   {delta}\n"
        f"   Mode: {mode_label}"
    )


def _resolve_mode_label(
    *,
    registration_key: str,
    source: str,
) -> str:
    """Return "LIVE" if the live timer is armed AND this
    registration has a persisted cycle_state ownership
    record. Returns "DRY RUN" otherwise.

    The check is read-only (no exchange calls, no MT4
    reads). If the timer is not active, or the cycle_state
    file is absent / has no entry for this registration, the
    operator sees the legacy "DRY RUN" label — never the
    other way around. This means a missing cycle_state is
    always rendered as DRY RUN, which is the safe
    conservative default.
    """
    import json
    import os
    import pathlib
    try:
        timer_active = (
            pathlib.Path(
                "/etc/systemd/system/fibo-converge.timer"
            ).exists()
        )
    except OSError:
        timer_active = False
    if not timer_active:
        return "DRY RUN"
    hermes_home = os.environ.get(
        "HERMES_HOME", str(pathlib.Path.home() / ".hermes"),
    )
    cs_path = pathlib.Path(hermes_home) / "fibo" / "cycle_state.json"
    if not cs_path.exists():
        return "DRY RUN"
    try:
        data = json.loads(cs_path.read_text())
    except (OSError, ValueError):
        return "DRY RUN"
    regs = data.get("registrations", {})
    entry = regs.get(registration_key)
    if not entry:
        return "DRY RUN"
    synced = entry.get("synchronized_cycle_id")
    transition = entry.get("transition")
    if synced is None or synced == 0:
        return "DRY RUN"
    if transition is not None:
        return f"LIVE — {transition}"
    return "LIVE"


def build_running_screen(
    reconciler: FiboReconciler,
) -> dict:
    """Build the Running Fibo dry-run screen (Telegram-friendly dict).

    Returns a ``{"text": str, "buttons": rows}`` dict, exactly the
    shape the existing wizard shim accepts. The only button is
    ❌ Exit (UI-only close). No executable actions.
    """
    try:
        results = reconciler.reconcile_all()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fibo_dryrun: reconcile_all failed: %s", exc
        )
        return {
            "text": (
                "📋 Running Fibo\n\n"
                "⚠️ Reconciliation failed:\n"
                f"{exc}\n\n"
                "The reconciler is read-only and never executes trades."
            ),
            "buttons": [
                [{"text": "❌ Exit", "callback_data": "fibo:exit"}],
            ],
        }

    if not results:
        body = (
            "📋 Running Fibo\n\n"
            "No persisted Fibo registrations yet.\n"
            "Use ▶️ Start Fibo to create one."
        )
    else:
        blocks: List[str] = []
        # Phase 2.13.22: derive the section title from the
        # actual mode of the displayed registrations. If every
        # active reconciliation result is a NOOP, the title
        # says "Running Fibo (LIVE — read-only)". If any
        # registration reports ERROR/STALE/SHOULD_FLATTEN/
        # INCREASE/REDUCE, fall back to the legacy "(DRY RUN —
        # read-only)" header so the operator can never confuse
        # a per-registration fail-closed condition with the
        # system-wide live convergence engine.
        from .reconciler import DeltaAction
        def _result_is_steady_noop(r) -> bool:
            return getattr(r, "delta_action", "") == DeltaAction.NONE.value
        if results and all(_result_is_steady_noop(r) for r in results):
            title = "📋 Running Fibo (LIVE — read-only)"
        else:
            title = "📋 Running Fibo (DRY RUN — read-only)"
        for r in results:
            blocks.append(_compact_block(r))
        body = title + "\n\n" + "\n\n".join(blocks)

    return {
        "text": body,
        "buttons": [
            [{"text": "❌ Exit", "callback_data": "fibo:exit"}],
        ],
    }


__all__ = ["build_running_screen"]