"""Phase 2.10 — Controlled live target convergence.

Goal:
    The smallest possible real execution path. ONLY the
    pre-authorized registration may issue exchange writes in
    Phase 2.10. Every other registration remains in shadow
    / read-only mode.

Allowed registration (identity must match EXACTLY):

    exchange            = ondoperps
    account             = BITGET
    exchange_instrument = ETH-USD.P
    variant             = NORMALFIB
    side                = BUY

Algorithm (each fresh MT4 snapshot):
    1. Compute target = starting_volume × cumulative BUY weight.
    2. Read positions_orders.
    3. On BEFORE-read failure -> STOP, no writes.
    4. Inspect ETH-USD.P only.
    5. Wrong side -> STOP, no flip.
    6. Target zero -> STOP, no flatten, no cancel, no order.
    7. actual LONG >= target -> NO-OP (Phase 2.10 must not
       reduce exposure).
    8. actual flat or LONG below target ->
         remaining_delta = target - actual_size.
    9. Find matching pending groups (same account / symbol / side).
    10. If matching groups exist, cancel_order_group them.
        Any cancel failure -> STOP, no order.
    11. Re-read positions_orders.
    12. On AFTER-read failure -> STOP, no order.
    13. Recompute remaining_delta.
    14. target achieved -> NO-OP.
    15. Still flat or below -> ONE new_order at MARKET for the
        exact remaining_delta (no reduce_only).
    16. At most one new_order per call.
    17. The next MT4/exchange read is the only recovery mechanism.

Deliberately NOT implemented:
    - partial-fill state machine
    - retry counters
    - execution journals / replay tables
    - reductions / auto-close / auto-flip
    - TP / SL / position_protections / ladder
    - new persistence files
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from plugins.trade.fibo.executor import (
    SIDE_BUY, SIDE_SELL, _compute_remaining_delta, _Delta,
    _fibo_client_order_id, _format_decimal, _parse_open_groups,
    _pending_groups_for_target, _read_actual_position,
    _resolve_mt4_target, ConvergeResult, ExchangePosition, Mt4Target,
)
from plugins.trade.fibo.snapshot import Mt4Snapshot
from plugins.trade.fibo.store import FiboRegistration

logger = logging.getLogger(__name__)

ExecuteFn = Callable[[Dict[str, Any]], Any]


# ---------------------------------------------------------------------------
# AUTHORIZED LIVE REGISTRATION — Phase 2.10 hard-coded identity allowlist.
# ---------------------------------------------------------------------------

ALLOWED_EXCHANGE = "ondoperps"
ALLOWED_ACCOUNT = "BITGET"
ALLOWED_EXCHANGE_INSTRUMENT = "ETH-USD.P"
ALLOWED_VARIANT = "NORMALFIB"
ALLOWED_SIDE_BUY = "BUY"  # FiboRegistration side string


def is_allowlisted(reg: FiboRegistration) -> bool:
    """Return True iff ``reg`` matches the Phase 2.10 allowlist
    EXACTLY on every identity field. Phase 2.10 is a single-
    registration live path; any other registration remains in
    shadow mode.

    Note: this is a strict IDENTITY check. Phase 2.11 also
    requires the registration to be ``is_active`` (not stopped)
    before live_converge runs. Use ``is_live_eligible`` for the
    combined check.
    """
    return (
        str(reg.exchange or "").strip().lower()
        == ALLOWED_EXCHANGE.lower()
        and str(reg.account or "").strip().upper()
        == ALLOWED_ACCOUNT.upper()
        and str(reg.exchange_instrument or "").strip().upper()
        == ALLOWED_EXCHANGE_INSTRUMENT.upper()
        and str(reg.variant or "").strip().upper()
        == ALLOWED_VARIANT.upper()
        and str(reg.side or "").strip().upper() == ALLOWED_SIDE_BUY
    )


def is_live_eligible(reg: FiboRegistration) -> bool:
    """Phase 2.11 — combined gate for the live path.

    A registration is live-eligible iff it matches the allowlist
    AND is currently active (not stopped). Stopped registrations
    are excluded from the live path even if their identity fields
    match the controlled registration.
    """
    if not is_allowlisted(reg):
        return False
    try:
        is_active = bool(getattr(reg, "is_active", False))
    except Exception:  # noqa: BLE001
        return False
    return is_active


# Operations the executor is allowed to invoke. Any other
# operation on the TradeDesk surface is unreachable from this
# module — that is enforced by a static guard test.
ALLOWED_OPERATIONS = frozenset(
    {
        "positions_orders",  # read (BEFORE + AFTER)
        "cancel_order_group",  # cancel matching Fibo pending groups
        "new_order",  # place exactly ONE market adjustment
    }
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveConvergeResult:
    """Outcome of a single ``live_converge`` invocation."""
    registration_key: str
    # Was this registration allowlisted for live writes?
    allowlisted: bool
    # True iff at least one new_order was placed in this call.
    placed_live_order: bool
    # The exact request body that was sent (None when no order
    # was placed).
    placed_request: Optional[Dict[str, Any]]
    # Cancelled pending groups.
    cancelled_groups: Tuple[Tuple[str, str], ...]
    # Failure flags.
    read_failed: bool = False
    cancel_failed: bool = False
    blocked_reason: str = ""
    # Summary.
    reason: str = ""


# ---------------------------------------------------------------------------
# Live convergence
# ---------------------------------------------------------------------------


def live_converge(
    reg: FiboRegistration,
    snap: Mt4Snapshot,
    *,
    execute_fn: ExecuteFn,
) -> LiveConvergeResult:
    """Converge the live exchange position toward the MT4 target
    on the allowlisted registration only.

    For non-allowlisted registrations, returns a result with
    ``allowlisted=False``, ``placed_live_order=False``, and a
    ``blocked_reason`` describing why the executor refuses to
    act. No TradeDesk calls are issued for non-allowlisted
    registrations.
    """
    target = _resolve_mt4_target(reg, snap)
    target_symbol = str(reg.exchange_instrument or "").strip().upper()

    # ------------------------------------------------------------------
    # Allowlist + active gate (Phase 2.11).
    # ------------------------------------------------------------------
    if not is_allowlisted(reg):
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=False,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            blocked_reason=(
                f"registration is not on the Phase 2.10 allowlist "
                f"(required exchange={ALLOWED_EXCHANGE!r}, "
                f"account={ALLOWED_ACCOUNT!r}, "
                f"instrument={ALLOWED_EXCHANGE_INSTRUMENT!r}, "
                f"variant={ALLOWED_VARIANT!r}, side={ALLOWED_SIDE_BUY!r})"
            ),
            reason="not on allowlist — shadow only",
        )
    if not bool(getattr(reg, "is_active", False)):
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            blocked_reason=(
                f"registration identity matches the allowlist but "
                f"status={reg.status!r} (not active); excluded from "
                f"the live path"
            ),
            reason="registration not active — shadow only",
        )

    # ------------------------------------------------------------------
    # Step 1 — BEFORE positions_orders read. FAIL-CLOSED.
    # ------------------------------------------------------------------
    request = {
        "operation": "positions_orders",
        "exchange": reg.exchange,
        "account": reg.account,
    }
    initial_groups: List[Any] = []
    try:
        first_response = execute_fn(request)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fibo_live: positions_orders raised for %s: %s",
            reg.registration_key, exc,
        )
        first_response = None

    if first_response is not None and getattr(
        first_response, "success", False,
    ):
        before = ExchangePosition(
            symbol=str(reg.exchange_instrument or "").upper(),
            side="", size=Decimal("0"),
        )
        # Parse via executor's internal helpers (private; safe
        # to reuse since both modules live in the same package).
        from plugins.trade.fibo.executor import (
            _read_actual_position_from_response,
        )
        before = _read_actual_position_from_response(reg, first_response)
        initial_groups = _parse_open_groups(first_response)
    elif first_response is not None:
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            read_failed=True,
            blocked_reason="positions_orders BEFORE read failed",
            reason="read failed — no writes",
        )
    else:
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            read_failed=True,
            blocked_reason="positions_orders BEFORE exception",
            reason="read exception — no writes",
        )

    # ------------------------------------------------------------------
    # Step 4-6 — Inspect ETH-USD.P only.
    # Step 5 — wrong side -> STOP. No flip.
    # Step 6 — target zero -> STOP. No flatten, no cancel.
    # Step 7 — actual LONG >= target -> NO-OP. No reduction.
    # ------------------------------------------------------------------
    if target.size <= Decimal("0"):
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            blocked_reason="target flat — no auto-flatten",
            reason="target zero — no flatten, no cancel, no order",
        )
    if before.is_flat:
        before_side_for_compare = ""
    elif before.side != SIDE_BUY:
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            blocked_reason=(
                f"venue on opposite side ({before.side!r}) of target "
                f"({SIDE_BUY!r}) — no auto-flip"
            ),
            reason="wrong side — no flip",
        )
    if (not before.is_flat
            and before.side == SIDE_BUY
            and before.size >= target.size):
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            blocked_reason="already at target — no reduction",
            reason="actual >= target — no-op",
        )

    # ------------------------------------------------------------------
    # Step 8 — compute remaining delta from BEFORE.
    # ------------------------------------------------------------------
    remaining_before = target.size - (before.size if not before.is_flat else Decimal("0"))
    if remaining_before <= Decimal("0"):
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            blocked_reason="remaining delta is zero",
            reason="no remaining delta",
        )

    # ------------------------------------------------------------------
    # Step 9-10 — cancel matching Fibo pending groups.
    # ------------------------------------------------------------------
    # Match by (account, symbol, side=BUY).
    target_groups = _pending_groups_for_target(
        initial_groups,
        target_symbol=target_symbol,
        target_side=SIDE_BUY,
    )
    cancelled: Tuple[Tuple[str, str], ...] = ()
    cancel_failed = False
    if target_groups:
        from plugins.trade.fibo.executor import _cancel_pending_groups
        cancelled, cancel_failed, _raw = _cancel_pending_groups(
            reg, execute_fn=execute_fn, groups=target_groups,
        )
        if cancel_failed:
            return LiveConvergeResult(
                registration_key=reg.registration_key,
                allowlisted=True,
                placed_live_order=False,
                placed_request=None,
                cancelled_groups=cancelled,
                cancel_failed=True,
                blocked_reason="cancel failed — no new_order",
                reason="matching cancel did not positively succeed",
            )

    # ------------------------------------------------------------------
    # Step 11-13 — AFTER read. FAIL-CLOSED.
    # ------------------------------------------------------------------
    after = _read_actual_position(reg, execute_fn=execute_fn)
    if after.read_failed:
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=cancelled,
            read_failed=True,
            blocked_reason="positions_orders AFTER read failed",
            reason="AFTER read failed — no writes",
        )

    if after.is_flat:
        after_size_for_calc = Decimal("0")
        after_side_for_calc = ""
    elif after.side != SIDE_BUY:
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=cancelled,
            blocked_reason=(
                f"AFTER read shows opposite side ({after.side!r}) — no flip"
            ),
            reason="AFTER wrong side — no flip",
        )
    else:
        after_size_for_calc = after.size
        after_side_for_calc = after.side

    remaining = target.size - after_size_for_calc
    if remaining <= Decimal("0"):
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=cancelled,
            blocked_reason="target achieved after cancel",
            reason="target achieved",
        )

    # ------------------------------------------------------------------
    # Step 14-16 — place exactly ONE new_order at MARKET for
    # the remaining delta. reduce_only=False. deterministic
    # client_order_id (Phase 2.10 semantics).
    # ------------------------------------------------------------------
    # Resolve cycle_id for the client_order_id hash.
    cycle_id = 0
    _fibo = snap.find_fibo(reg.source_symbol, reg.variant)
    if _fibo is not None:
        try:
            cycle_id = int(_fibo.side_cycle_id(reg.side) or 0)
        except (ValueError, AttributeError):
            cycle_id = 0

    # Build a synthetic _Delta for the client_order_id hash so
    # the executor's helper signature is satisfied. The executor
    # is the only place that constructs _Delta; here we mirror
    # the same payload shape by hand to avoid leaking the type.
    cid_payload = (
        f"{reg.registration_key}|{snap.source}|"
        f"{int(cycle_id)}|"
        f"{target.side}|{_format_decimal(target.size)}|"
        f"{SIDE_BUY}|{_format_decimal(remaining)}"
    )
    import hashlib
    digest = hashlib.sha256(cid_payload.encode("utf-8")).hexdigest()
    client_order_id = f"fibo-{digest[0:16]}"

    request_body = {
        "operation": "new_order",
        "exchange": reg.exchange,
        "account": reg.account,
        "symbol": reg.exchange_instrument,
        "side": SIDE_BUY,
        "order_type": "market",
        "volume": _format_decimal(remaining),
        "reduce_only": False,
        "client_order_id": client_order_id,
    }

    try:
        response = execute_fn(request_body)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fibo_live: new_order raised for %s (%s/%s): %s",
            reg.registration_key, SIDE_BUY, remaining, exc,
        )
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=cancelled,
            blocked_reason="new_order raised",
            reason="new_order exception",
        )
    if not getattr(response, "success", False):
        logger.warning(
            "fibo_live: new_order failure for %s: %s",
            reg.registration_key,
            getattr(getattr(response, "error", None), "message",
                    "<no error>"),
        )
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=cancelled,
            blocked_reason="new_order returned success=False",
            reason="new_order failed",
        )

    placed_dict = (
        response.to_dict() if hasattr(response, "to_dict")
        else dict(response)
    )
    return LiveConvergeResult(
        registration_key=reg.registration_key,
        allowlisted=True,
        placed_live_order=True,
        placed_request=request_body,
        cancelled_groups=cancelled,
        reason=(
            f"placed OPEN {SIDE_BUY} {_format_decimal(remaining)} "
            f"for {reg.registration_key}"
        ),
    )
