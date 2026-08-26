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
    venue_token = r.exchange_instrument or "� not selected"
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
    return (
        f"📌 {sym_variant}\n"
        f"   {exchange_account}\n"
        f"   {mt4}\n"
        f"   {target}\n"
        f"   {actual}\n"
        f"   {delta}\n"
        f"   Mode: DRY RUN"
    )


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
        for r in results:
            blocks.append(_compact_block(r))
        body = "📋 Running Fibo (DRY RUN — read-only)\n\n" + "\n\n".join(blocks)

    return {
        "text": body,
        "buttons": [
            [{"text": "❌ Exit", "callback_data": "fibo:exit"}],
        ],
    }


__all__ = ["build_running_screen"]