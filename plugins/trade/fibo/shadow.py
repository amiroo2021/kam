"""Phase 2.9 — Shadow-mode executor wiring.

This module wires the Phase 2.8 target-convergence executor into
the live Fibo reconciliation loop, but ONLY in shadow mode:

  - It performs a real read-only ``positions_orders`` call.
  - It identifies matching pending Fibo adjustments.
  - It computes what would be cancelled and what would be ordered.
  - It returns a ShadowOutput describing the would-be action.
  - It NEVER invokes ``cancel_order_group``.
  - It NEVER invokes ``new_order`` (or any other write op).

The shadow output is rendered in the Running Fibo screen so the
operator can compare the dry-run view to the hypothetical-live
view. No write path is reachable from this module.

Design contract:
  - Stateless across calls (same as Phase 2.8 executor).
  - Failure modes (read failure, cancel failure, wrong side,
    target flat) return a ShadowOutput with ``status="BLOCKED"``
    or ``status="NOOP"`` and a reason string — never silently
    emit a hypothetical order.
  - At most one hypothetical ``would_order`` per registration
    per call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from plugins.trade.fibo.executor import (
    SIDE_BUY, SIDE_SELL, _compute_remaining_delta, _Delta, _format_decimal,
    _normalize_actual_side, _parse_open_groups, _pending_groups_for_target,
    _read_actual_position, _read_actual_position_from_response,
    _resolve_mt4_target, ConvergeResult, ExchangePosition, Mt4Target,
)
from plugins.trade.fibo.snapshot import Mt4Snapshot
from plugins.trade.fibo.store import FiboRegistration

logger = logging.getLogger(__name__)

ExecuteFn = Callable[[Dict[str, Any]], Any]


@dataclass(frozen=True)
class ShadowWouldCancel:
    """One pending adjustment group that the executor WOULD cancel
    if it were not in shadow mode."""
    symbol: str
    side: str
    total_size: str


@dataclass(frozen=True)
class ShadowWouldOrder:
    """The exact ``new_order`` request the executor WOULD send
    if it were not in shadow mode. Includes the deterministic
    client_order_id derived from the current adjustment intent."""
    operation: str = "new_order"
    exchange: str = ""
    account: str = ""
    symbol: str = ""
    side: str = ""
    order_type: str = "market"
    volume: str = ""
    reduce_only: bool = False
    client_order_id: str = ""


@dataclass(frozen=True)
class ShadowOutput:
    """Shadow-mode output for ONE active registration.

    Status values:
        - ``"SHADOW_ONLY"`` — a hypothetical ``new_order`` was
          computed but NOT sent.
        - ``"NOOP"`` — no action would be taken (already at
          target, wrong side, target flat, etc.).
        - ``"BLOCKED"`` — convergence was blocked (read failure,
          cancel failure, etc.).
    """
    # Registration identity.
    registration_key: str
    source_symbol: str
    venue_instrument: str
    exchange: str
    account: str
    variant: str
    side: str
    starting_volume: str

    # MT4 side.
    mt4_cycle_id: int
    mt4_cumulative_weight: str

    # Live state.
    target_size: str
    actual_side: str
    actual_size: str

    # Pending groups matching the target (would_cancel input).
    matching_pending_groups: Tuple[ShadowWouldCancel, ...]
    would_cancel: Tuple[ShadowWouldCancel, ...]

    # The remaining same-direction delta.
    remaining_delta_side: str
    remaining_delta_size: str

    # The hypothetical order that WOULD be placed.
    would_order: Optional[ShadowWouldOrder]

    # Status + reason.
    status: str
    reason: str

    # Carry-through from ConvergeResult for operator visibility.
    read_failed: bool = False
    cancel_failed: bool = False


# ---------------------------------------------------------------------------
# Shadow-mode shadow executor
# ---------------------------------------------------------------------------


def shadow_run(
    reg: FiboRegistration,
    snap: Mt4Snapshot,
    *,
    execute_fn: ExecuteFn,
) -> ShadowOutput:
    """Compute the would-be action for one registration in SHADOW mode.

    This performs the SAME read-only logic as ``executor.converge``
    but it NEVER invokes ``cancel_order_group`` and it NEVER
    invokes ``new_order``. The output describes what those
    operations WOULD have been.

    Failure modes (read failure, cancel failure, wrong side,
    target flat) map to ``status="BLOCKED"`` or ``"NOOP"``.
    """
    target = _resolve_mt4_target(reg, snap)
    target_symbol = str(reg.exchange_instrument or "").strip().upper()

    # Step 1+3 (combined): one positions_orders read for both the
    # position (BEFORE) and the open-order groups (cancel input).
    request = {
        "operation": "positions_orders",
        "exchange": reg.exchange,
        "account": reg.account,
    }
    initial_groups: List[Any] = []
    read_failed = False
    try:
        first_response = execute_fn(request)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fibo_executor_shadow: positions_orders raised for %s: %s",
            reg.registration_key, exc,
        )
        first_response = None
        read_failed = True

    if first_response is not None and getattr(
        first_response, "success", False,
    ):
        before = _read_actual_position_from_response(reg, first_response)
        initial_groups = _parse_open_groups(first_response)
    elif first_response is not None:
        logger.warning(
            "fibo_executor_shadow: positions_orders returned failure for %s",
            reg.registration_key,
        )
        before = ExchangePosition(
            symbol=str(reg.exchange_instrument or "").upper(),
            side="", size=Decimal("0"),
            read_failed=True,
        )
        read_failed = True
    else:
        before = ExchangePosition(
            symbol=str(reg.exchange_instrument or "").upper(),
            side="", size=Decimal("0"),
            read_failed=True,
        )

    if before.read_failed or read_failed:
        return _build_blocked_output(
            reg=reg, target=target,
            actual=before,
            phase="BEFORE read",
            matching_groups=(),
            would_cancel=(),
            delta=None,
        )

    # Compute the delta we WOULD send based on the BEFORE view.
    prospective_delta = _compute_remaining_delta(before, target)

    # Identify matching pending groups. In shadow mode we
    # REPORT but never CANCEL.
    would_cancel: Tuple[ShadowWouldCancel, ...] = ()
    matching_groups: List[Any] = []
    if prospective_delta is not None and target_symbol:
        matching_groups = _pending_groups_for_target(
            initial_groups,
            target_symbol=target_symbol,
            target_side=prospective_delta.side,
        )
        would_cancel = tuple(
            ShadowWouldCancel(
                symbol=g.symbol,
                side=g.side,
                total_size=_format_decimal(g.total_size),
            )
            for g in matching_groups
        )

    # Step 5 (simulated): in shadow mode we still re-read the
    # venue so the dry-run sees the same end-state the live path
    # would. This second read is read-only.
    try:
        after = _read_actual_position(reg, execute_fn=execute_fn)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fibo_executor_shadow: positions_orders AFTER raised for %s: %s",
            reg.registration_key, exc,
        )
        after = ExchangePosition(
            symbol=str(reg.exchange_instrument or "").upper(),
            side="", size=Decimal("0"),
            read_failed=True,
        )

    if after.read_failed:
        return _build_blocked_output(
            reg=reg, target=target,
            actual=after,
            phase="AFTER read",
            matching_groups=tuple(
                ShadowWouldCancel(
                    symbol=g.symbol, side=g.side,
                    total_size=_format_decimal(g.total_size),
                )
                for g in matching_groups
            ),
            would_cancel=would_cancel,
            delta=None,
        )

    # Step 6: recompute against the post-read position.
    delta = _compute_remaining_delta(after, target)

    # Step 7: build the hypothetical order WITHOUT sending it.
    would_order: Optional[ShadowWouldOrder] = None
    if delta is not None:
        from plugins.trade.fibo.executor import _fibo_client_order_id
        # Phase 2.10 — same hash inputs as the live executor:
        # source + cycle_id + target + delta. NOT snap.seq.
        cycle_id = 0
        _fibo = snap.find_fibo(reg.source_symbol, reg.variant)
        if _fibo is not None:
            try:
                cycle_id = int(_fibo.side_cycle_id(reg.side) or 0)
            except (ValueError, AttributeError):
                cycle_id = 0
        would_order = ShadowWouldOrder(
            operation="new_order",
            exchange=reg.exchange,
            account=reg.account,
            symbol=reg.exchange_instrument or "",
            side=delta.side,
            order_type="market",
            volume=_format_decimal(delta.size),
            reduce_only=False,
            client_order_id=_fibo_client_order_id(
                reg,
                source=snap.source,
                cycle_id=cycle_id,
                target=target,
                delta=delta,
            ),
        )

    return _build_shadow_output(
        reg=reg,
        snap=snap,
        target=target,
        before=before,
        after=after,
        matching_groups=tuple(
            ShadowWouldCancel(
                symbol=g.symbol, side=g.side,
                total_size=_format_decimal(g.total_size),
            )
            for g in matching_groups
        ),
        would_cancel=would_cancel,
        delta=delta,
        would_order=would_order,
    )


# ---------------------------------------------------------------------------
# Output builders
# ---------------------------------------------------------------------------


def _build_shadow_output(
    *,
    reg: FiboRegistration,
    snap: Mt4Snapshot,
    target: Mt4Target,
    before: ExchangePosition,
    after: ExchangePosition,
    matching_groups: Tuple[ShadowWouldCancel, ...],
    would_cancel: Tuple[ShadowWouldCancel, ...],
    delta: Optional["_Delta"],
    would_order: Optional[ShadowWouldOrder],
) -> ShadowOutput:
    """Build a ShadowOutput for the happy-path / no-op / wrong-side
    / target-flat cases."""
    fibo = snap.find_fibo(reg.source_symbol, reg.variant)
    cycle_id = 0
    weight = Decimal("0")
    if fibo is not None:
        try:
            cycle_id = int(fibo.side_cycle_id(reg.side) or 0)
        except (ValueError, AttributeError):
            cycle_id = 0
        try:
            weight = Decimal(str(fibo.side_cumulative_weight(reg.side) or "0"))
        except Exception:  # noqa: BLE001
            weight = Decimal("0")

    if delta is not None:
        remaining_side = delta.side
        remaining_size = _format_decimal(delta.size)
        status = "SHADOW_ONLY"
        reason = (
            f"would place {delta.action} {delta.side} "
            f"{remaining_size} for {reg.registration_key}"
        )
    else:
        remaining_side = ""
        remaining_size = "0"
        if target.size <= Decimal("0"):
            status = "NOOP"
            reason = "mt4 target flat — no auto-flatten"
        elif after.is_flat:
            status = "NOOP"
            reason = "venue flat but target flat"
        elif after.side != target.side:
            status = "NOOP"
            reason = (
                "venue on opposite side of target — no auto-flip"
            )
        elif after.size >= target.size:
            status = "NOOP"
            reason = "already at target"
        else:
            status = "NOOP"
            reason = "no remaining delta"

    return ShadowOutput(
        registration_key=reg.registration_key,
        source_symbol=reg.source_symbol,
        venue_instrument=reg.exchange_instrument or "",
        exchange=reg.exchange,
        account=reg.account,
        variant=reg.variant,
        side=reg.side,
        starting_volume=_format_decimal(reg.starting_volume),
        mt4_cycle_id=cycle_id,
        mt4_cumulative_weight=_format_decimal(weight),
        target_size=_format_decimal(target.size),
        actual_side=after.side or "",
        actual_size=_format_decimal(after.size),
        matching_pending_groups=matching_groups,
        would_cancel=would_cancel,
        remaining_delta_side=remaining_side,
        remaining_delta_size=remaining_size,
        would_order=would_order,
        status=status,
        reason=reason,
    )


def _build_blocked_output(
    *,
    reg: FiboRegistration,
    target: Mt4Target,
    actual: ExchangePosition,
    phase: str,
    matching_groups: Tuple[ShadowWouldCancel, ...],
    would_cancel: Tuple[ShadowWouldCancel, ...],
    delta: Optional["_Delta"],
) -> ShadowOutput:
    return ShadowOutput(
        registration_key=reg.registration_key,
        source_symbol=reg.source_symbol,
        venue_instrument=reg.exchange_instrument or "",
        exchange=reg.exchange,
        account=reg.account,
        variant=reg.variant,
        side=reg.side,
        starting_volume=_format_decimal(reg.starting_volume),
        mt4_cycle_id=0,
        mt4_cumulative_weight="0",
        target_size=_format_decimal(target.size),
        actual_side="",
        actual_size="0",
        matching_pending_groups=matching_groups,
        would_cancel=would_cancel,
        remaining_delta_side=delta.side if delta else "",
        remaining_delta_size=_format_decimal(delta.size) if delta else "0",
        would_order=None,
        status="BLOCKED",
        reason=f"{phase} failed — convergence blocked (zero writes)",
        read_failed=True,
    )


def render_shadow_table(results: List[ShadowOutput]) -> str:
    """Render a list of ShadowOutput rows as a human-readable,
    sanitized Telegram block. No secrets, no token leakage."""
    lines: List[str] = []
    if not results:
        return "No Fibo registrations to shadow.\n"
    for r in results:
        lines.append("=" * 72)
        lines.append(f"🛰️ SHADOW  : {r.registration_key}")
        lines.append(f"  Status          : {r.status}")
        lines.append(
            f"  Exchange        : {r.exchange} / {r.account}"
        )
        lines.append(
            f"  MT4 src → venue : {r.source_symbol} → {r.venue_instrument}"
        )
        lines.append(
            f"  Variant / Side  : {r.variant} / {r.side}  "
            f"start_vol={r.starting_volume}"
        )
        lines.append(
            f"  MT4             : cycle={r.mt4_cycle_id}  "
            f"weight={r.mt4_cumulative_weight}"
        )
        lines.append(f"  Target size     : {r.target_size}")
        lines.append(
            f"  Actual          : side={r.actual_side or '(unknown)'}  "
            f"size={r.actual_size}"
        )
        if r.matching_pending_groups:
            for g in r.matching_pending_groups:
                lines.append(
                    f"  Matching group  : {g.symbol} {g.side} total={g.total_size}"
                )
        else:
            lines.append("  Matching group  : (none)")
        if r.would_cancel:
            wc = ", ".join(
                f"{g.symbol}/{g.side}@{g.total_size}" for g in r.would_cancel
            )
            lines.append(f"  Would cancel    : {wc}")
        else:
            lines.append("  Would cancel    : (none)")
        lines.append(
            f"  Remaining delta : side={r.remaining_delta_side or '-'}  "
            f"size={r.remaining_delta_size}"
        )
        if r.would_order is not None:
            lines.append(
                f"  Would order     : {r.would_order.symbol} "
                f"{r.would_order.side} {r.would_order.volume} "
                f"({r.would_order.order_type}) "
                f"client_order_id={r.would_order.client_order_id}"
            )
        else:
            lines.append("  Would order     : (none)")
        lines.append(f"  Reason          : {r.reason}")
        if r.read_failed or r.cancel_failed:
            flags = []
            if r.read_failed:
                flags.append("read_failed")
            if r.cancel_failed:
                flags.append("cancel_failed")
            lines.append(f"  Failure flags   : {', '.join(flags)}")
    return "\n".join(lines) + "\n"
