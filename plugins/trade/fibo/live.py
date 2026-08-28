"""Phase 2.13.12 — Dynamic live target convergence.

The live execution path is gated by the dynamic live-eligibility layer
(``plugins.trade.fibo.live_eligibility``). The canonical persisted
registration store (``registrations.jsonl``) is the authorization
boundary — a raw MT4 snapshot entry is NEVER sufficient to trade.

There is exactly ONE authorization model. The Phase 2.10 hard-coded
identity allowlist has been retired; do not reintroduce it.

The supported exchanges surface remains whatever the live
``TradeDesk.list_exchanges()`` returns at evaluation time; we do not
hard-code the set.

Algorithm (each fresh MT4 snapshot):
    1. The caller (converge_once) iterates over canonical persisted
       registrations (latest per key) loaded from the JSONL store.
    2. For each active registration, the live-eligibility gates
       (see ``live_eligibility.evaluate``) are applied in order. Any
       gate failure produces an explicit ``BlockReason`` and no
       write is attempted.
    3. If eligible, compute the target from the MT4 snapshot using
       the registration's own side, variant, and exchange_instrument.
    4. Read positions_orders for the resolved exchange+account.
    5. On BEFORE-read failure -> STOP, no writes.
    6. Inspect ONLY the resolved ``exchange_instrument``.
    7. Wrong side (actual on opposite side of target) -> STOP, no flip.
    8. Target zero -> STOP, no flatten, no cancel, no order.
    9. actual size >= target size on the SAME side -> NO-OP.
   10. actual flat or below target on the target side ->
         remaining_delta = target - actual_size.
   11. Find matching pending groups (same account / symbol / side).
   12. If matching groups exist, cancel_order_group them.
         Any cancel failure -> STOP, no order.
   13. Re-read positions_orders.
   14. On AFTER-read failure -> STOP, no order.
   15. Recompute remaining_delta.
   16. target achieved -> NO-OP.
   17. Still flat or below -> ONE new_order at MARKET for the
         exact remaining_delta (no reduce_only).
   18. At most one new_order per call.
   19. The next MT4/exchange read is the only recovery mechanism.

Deliberately NOT implemented:
    - partial-fill state machine
    - retry counters
    - execution journals / replay tables
    - reductions / auto-close / auto-flip
    - TP / SL / position_protections / ladder
    - new persistence files
    - position-closing behavior on Stop Fibo
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from plugins.trade.fibo.executor import (
    SIDE_BUY, SIDE_SELL, _compute_remaining_delta, _Delta,
    _fibo_client_order_id, _fibo_to_venue_side, _format_decimal,
    _parse_open_groups, _pending_groups_for_target,
    _read_actual_position, _reg_mt4_side, _resolve_mt4_target,
    ConvergeResult, ExchangePosition, Mt4Target,
)
from plugins.trade.fibo.live_eligibility import LiveEligibility
from plugins.trade.fibo.snapshot import Mt4Snapshot
from plugins.trade.fibo.store import FiboRegistration


logger = logging.getLogger(__name__)


ExecuteFn = Callable[[Dict[str, Any]], Any]


# -------------------------------------------------------------------
# Operations the executor is allowed to invoke. Any other
# operation on the TradeDesk surface is unreachable from this
# module — that is enforced by a static guard test.
# -------------------------------------------------------------------
ALLOWED_OPERATIONS = frozenset(
    {
        "positions_orders",  # read (BEFORE + AFTER)
        "cancel_order_group",  # cancel matching Fibo pending groups
        "new_order",  # place exactly ONE market adjustment
        "close_position",  # close an entire instrument position (cycle transition)
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
    store: Optional[Any] = None,
    supported_exchanges: frozenset,
    validate_accounts_fn: Optional[Any] = None,
) -> LiveConvergeResult:
    """Converge the live exchange position toward the MT4 target
    on the dynamic-eligibility registration.

    For registrations that fail any live-eligibility gate, returns
    a result with ``allowlisted=False``, ``placed_live_order=False``,
    and a ``blocked_reason`` carrying the explicit ``BlockReason``
    code. No TradeDesk calls are issued for ineligible registrations.

    Parameters
    ----------
    reg : FiboRegistration
        The candidate registration. Must be the canonical latest
        state loaded from the persisted store.
    snap : Mt4Snapshot
        The current MT4 snapshot.
    execute_fn : callable
        The TradeDesk execute function (positions_orders,
        new_order, cancel_order_group).
    store : FiboRegistrationStore | None
        Optional store reference. When provided, the eligibility
        layer verifies the registration IS the canonical latest row
        for its registration_key. Recommended.
    supported_exchanges : frozenset[str]
        REQUIRED. The set of exchanges the current Fibo/trade
        adapter layer supports. The caller MUST pass this. If a
        caller is unable to provide it, the production code must
        resolve it once via ``TradeDesk.list_exchanges()`` and
        forward it.

    Notes
    -----
    There is NO legacy fallback to the Phase 2.10 hardcoded
    ETH-only allowlist. The single authorization model is the
    dynamic eligibility layer in ``live_eligibility.evaluate``.
    There is exactly one authorization path: this function
    delegates to ``evaluate`` and no other code path may
    authorize a live write.
    """
    from plugins.trade.fibo.live_eligibility import (
        BlockReason, evaluate,
    )

    target = _resolve_mt4_target(reg, snap)
    target_symbol = str(reg.exchange_instrument or "").strip().upper()

    # ------------------------------------------------------------------
    # Dynamic live eligibility (Phase 2.13.12).
    # ------------------------------------------------------------------
    # The single authorization model. supported_exchanges is REQUIRED;
    # a missing value fails closed.
    if not supported_exchanges:
        # Empty frozenset is permitted (e.g. a deliberate empty
        # supported set) but means every registration is blocked.
        # We do NOT silently fall back to the Phase 2.10 hardcoded
        # allowlist.
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=False,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            read_failed=False,
            blocked_reason=(
                f"{BlockReason.BLOCKED_UNSUPPORTED_EXCHANGE.value}: "
                f"supported_exchanges is empty; refusing to "
                f"authorize any live convergence"
            ),
            reason=(
                f"{BlockReason.BLOCKED_UNSUPPORTED_EXCHANGE.value} — "
                f"supported_exchanges not provided"
            ),
        )

    eligibility = evaluate(
        reg, snap,
        supported_exchanges=supported_exchanges,
        store=store,
        validate_accounts_fn=validate_accounts_fn,
    )

    if not eligibility.eligible:
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=False,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            blocked_reason=(
                f"{eligibility.reason_code.value}: {eligibility.reason}"
            ),
            reason=f"{eligibility.reason_code.value} — shadow only",
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

    resolved_side = _reg_mt4_side(reg)  # fibo-side ('buy'/'sell')
    venue_target_side = _fibo_to_venue_side(resolved_side)  # venue-side
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
    # Step 1.5 — Phase 2.13.18 cycle-aware decision.
    #
    # Inspect the persisted cycle-state and decide whether the
    # existing exchange position belongs to the same MT4 cycle
    # the current snapshot reports, or to a previous cycle. If
    # different, the executor must close the old-cycle position
    # before opening the new-cycle target.
    # ------------------------------------------------------------------
    cycle_decision = _evaluate_cycle_decision(
        reg=reg,
        target=target,
        before=before,
        snap=snap,
    )
    if cycle_decision is not None:
        if cycle_decision["action"] == "FAIL_CLOSED":
            return LiveConvergeResult(
                registration_key=reg.registration_key,
                allowlisted=True,
                placed_live_order=False,
                placed_request=None,
                cancelled_groups=(),
                read_failed=False,
                blocked_reason=cycle_decision["reason"],
                reason=cycle_decision["reason"],
            )
        if cycle_decision["action"] == "CLOSE_REQUIRED":
            # Execute the close. Use the canonical close_position
            # operation exposed by the exchange agent. After the
            # close, we must verify the instrument is flat before
            # opening the new cycle's target.
            return _execute_close_transition(
                reg=reg,
                target=target,
                before=before,
                execute_fn=execute_fn,
                cycle_decision=cycle_decision,
            )

    # ------------------------------------------------------------------
    # Step 4-6 — Inspect only the resolved exchange_instrument.
    # Use the REGISTRATION'S side (not the hard-coded BUY).
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Target=0 diagnostic: distinct messages for flat vs. non-flat
    # actual positions.
    #
    # MT4 reports cycle=0 / weight=0 → target=0 means "this side
    # is currently inactive". The executor intentionally does NOT
    # auto-flatten any existing exchange position at target=0,
    # because there is no Fibo-owned-position attribution ledger
    # in the persisted state. A legacy / manual / cross-cycle
    # position must NOT be auto-closed by an MT4 cycle zero.
    # Operator responsibility: manage exchange exposure outside
    # of Fibo or via an explicit ownership-tracked close path.
    # ------------------------------------------------------------------
    if target.size <= Decimal("0"):
        if before.is_flat:
            return LiveConvergeResult(
                registration_key=reg.registration_key,
                allowlisted=True,
                placed_live_order=False,
                placed_request=None,
                cancelled_groups=(),
                blocked_reason=(
                    "target flat — no auto-flatten (actual is flat)"
                ),
                reason=(
                    "target zero, actual flat — nothing to do"
                ),
            )
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            blocked_reason=(
                f"target zero but non-zero exchange exposure exists "
                f"({_format_decimal(before.size)} {before.side.upper()} "
                f"{reg.exchange_instrument!r}); no auto-flatten because "
                f"position ownership is not proven"
            ),
            reason=(
                "target zero, actual non-flat — no auto-flatten "
                "(ownership not proven); manual review required"
            ),
        )
    if before.is_flat:
        before_side_for_compare = ""
    elif before.side != venue_target_side:
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            blocked_reason=(
                f"venue on opposite side ({before.side!r}) of target "
                f"({venue_target_side!r}) — no auto-flip"
            ),
            reason="wrong side — no flip",
        )
    if (not before.is_flat
            and before.side == venue_target_side
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
    # The executor's helper is side-agnostic; it takes the target
    # side as an argument.
    # ------------------------------------------------------------------
    before_size = before.size if not before.is_flat else Decimal("0")
    remaining_before = target.size - before_size
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
    # Match by (account, symbol, side=venue_target_side).
    # ------------------------------------------------------------------
    target_groups = _pending_groups_for_target(
        initial_groups,
        target_symbol=target_symbol,
        target_side=venue_target_side,
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
    elif after.side != venue_target_side:
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
    #
    # The client_order_id already includes the cycle_id; the executor
    # is the single place that constructs it. We compute it from the
    # registration's side.
    # ------------------------------------------------------------------
    cycle_id = 0
    _fibo = snap.find_fibo(reg.source_symbol, reg.variant)
    if _fibo is not None:
        try:
            cycle_id = int(_fibo.side_cycle_id(resolved_side) or 0)
        except (ValueError, AttributeError):
            cycle_id = 0

    cid_payload = (
        f"{reg.registration_key}|{snap.source}|"
        f"{int(cycle_id)}|"
        f"{target.side}|{_format_decimal(target.size)}|"
        f"{venue_target_side}|{_format_decimal(remaining)}"
    )
    import hashlib
    digest = hashlib.sha256(cid_payload.encode("utf-8")).hexdigest()
    client_order_id = f"fibo-{digest[0:16]}"

    request_body = {
        "operation": "new_order",
        "exchange": reg.exchange,
        "account": reg.account,
        "symbol": reg.exchange_instrument,
        "side": venue_target_side,
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




# -------------------------------------------------------------------
# Phase 2.13.18 cycle-aware helpers
# -------------------------------------------------------------------


def _evaluate_cycle_decision(
    *,
    reg,
    target,
    before,
    snap,
):
    """Run the cycle-decision module against the persisted state.

    Returns:
        None — the same-cycle path is not interesting for the
        caller; the legacy executor logic still applies.
        A dict describing the action to take:
            {"action": "FAIL_CLOSED", "reason": ...}
            {"action": "CLOSE_REQUIRED", "reason": ...,
             "close_size": <Decimal>, "new_cycle_id": <int>}

    Same-cycle returns None so the existing executor
    flow (NOOP / delta-open / same-side reduction) is
    preserved.
    """
    # Import lazily to avoid import cycles.
    from plugins.trade.fibo.cycle_decide import (
        decide_cycle_action,
    )
    from plugins.trade.fibo.cycle_state import (
        CycleStateStore,
    )

    try:
        store = CycleStateStore()
    except OSError as exc:
        return {
            "action": "FAIL_CLOSED",
            "reason": (
                f"BLOCKED_CYCLE_STATE_UNREADABLE: cannot access "
                f"cycle state: {exc}"
            ),
        }

    synchronized = store.get_synchronized_cycle_id(
        reg.registration_key,
    )

    # Determine the side the registration is targeting. The
    # registration's side is canonical.
    side = str(reg.side or "").upper()
    if side not in ("BUY", "SELL"):
        return {
            "action": "FAIL_CLOSED",
            "reason": (
                f"BLOCKED_INVALID_SIDE: registration side "
                f"{reg.side!r} is not BUY/SELL"
            ),
        }

    decision = decide_cycle_action(
        registration_key=reg.registration_key,
        source_symbol=reg.source_symbol,
        variant=reg.variant,
        side=side,
        target=target,
        before=before,
        snap=snap,
        synchronized_cycle_id=synchronized,
        transition=store.get_transition(reg.registration_key),
    )

    if decision.action == "NOOP":
        # Same-cycle case: legacy executor logic handles it.
        # Do not interfere.
        return None
    if decision.action == "OPEN_REQUIRED":
        # The cycle-decide returned OPEN_REQUIRED. Two distinct
        # cases:
        #   (a) same-cycle delta-open (synchronized_cycle_id
        #       matches the current MT4 cycle): legacy executor
        #       computes remaining = target - actual and
        #       issues a single new_order. State remains
        #       STEADY; no further persistence required.
        #   (b) bootstrap-adopt (no prior state): we MUST
        #       persist adopt_first_cycle BEFORE issuing the
        #       new_order, so that if the executor crashes
        #       between persisting and verifying, the next
        #       run does not re-classify the position as
        #       BLOCKED_CYCLE_OWNERSHIP_UNKNOWN.
        if synchronized is None or synchronized == 0:
            # Bootstrap-adopt path: persist the current cycle
            # BEFORE falling through to the legacy executor.
            try:
                store.adopt_first_cycle(
                    reg.registration_key,
                    source=reg.source,
                    exchange=reg.exchange,
                    account=reg.account,
                    exchange_instrument=reg.exchange_instrument,
                    variant=reg.variant,
                    side=side,
                    cycle_id=int(decision.new_cycle_id or 0),
                )
            except OSError as exc:
                return {
                    "action": "FAIL_CLOSED",
                    "reason": (
                        f"BLOCKED_CYCLE_STATE_UNWRITEABLE: cannot "
                        f"persist bootstrap-adopt: {exc}"
                    ),
                }
        # Same-cycle delta-open: legacy executor logic still
        # handles it via the existing target->remaining flow.
        # We do not pass the delta through here; the legacy
        # path computes remaining = target - actual.
        return None
    # FAIL_CLOSED or CLOSE_REQUIRED
    if decision.action == "CLOSE_REQUIRED":
        return {
            "action": "CLOSE_REQUIRED",
            "reason": decision.reason,
            "close_size": decision.close_size,
            "new_cycle_id": decision.new_cycle_id,
        }
    # FAIL_CLOSED
    return {
        "action": "FAIL_CLOSED",
        "reason": decision.reason,
    }


def _execute_close_transition(
    *,
    reg,
    target,
    before,
    execute_fn,
    cycle_decision,
):
    """Execute the close for a cycle transition.

    Phase 2.13.18: this is a CRASH-SAFE two-step. The close
    is sent, then a positions_orders read verifies the
    instrument is flat. If the verification fails, the
    executor returns FAIL_CLOSED rather than blindly opening
    a new position.

    Persisted cycle state machine updates:
      begin_transition_close_sent
      advance_transition_close_verified (only after verified flat)

    The current call returns a result that the caller (the
    convergence loop) will see. The new-cycle OPEN is NOT
    issued in this call — convergence runs every minute and
    the next natural fire will see the now-flat position and
    trigger the OPEN_REQUIRED path (or, if the close itself
    is mid-flight, the recovery logic will pick up the
    transition state).
    """
    from plugins.trade.fibo.cycle_state import (
        CycleStateStore,
    )

    close_size = cycle_decision["close_size"]
    new_cycle_id = cycle_decision["new_cycle_id"]

    # Persist: a close is about to be sent.
    try:
        store = CycleStateStore()
        store.begin_transition_close_sent(
            reg.registration_key,
            old_cycle_id=(
                store.get_synchronized_cycle_id(
                    reg.registration_key
                )
                or 0
            ),
        )
    except OSError as exc:
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            read_failed=False,
            blocked_reason=(
                f"BLOCKED_CYCLE_STATE_UNWRITEABLE: cannot "
                f"persist close transition: {exc}"
            ),
            reason="cycle transition aborted",
        )

    # Issue the close.
    close_request = {
        "operation": "close_position",
        "exchange": reg.exchange,
        "account": reg.account,
        "symbol": reg.exchange_instrument,
    }
    try:
        close_response = execute_fn(close_request)
    except Exception as exc:
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            read_failed=False,
            blocked_reason=(
                f"close_position raised: {exc}"
            ),
            reason="close exception",
        )

    if not getattr(close_response, "success", False):
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            read_failed=False,
            blocked_reason=(
                f"close_position returned failure: "
                f"{getattr(getattr(close_response, 'error', None), 'message', '<no error>')}"
            ),
            reason="close failed",
        )

    # Re-read positions to verify flat.
    verify_request = {
        "operation": "positions_orders",
        "exchange": reg.exchange,
        "account": reg.account,
    }
    try:
        verify_response = execute_fn(verify_request)
    except Exception as exc:
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            read_failed=False,
            blocked_reason=(
                f"positions_orders verify raised: {exc}"
            ),
            reason="verify exception — close completed but unverified",
        )
    if not getattr(verify_response, "success", False):
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            read_failed=False,
            blocked_reason=(
                f"verify positions_orders returned failure: "
                f"{getattr(getattr(verify_response, 'error', None), 'message', '<no error>')}"
            ),
            reason="verify failed",
        )

    # Parse the verify response to check if instrument is flat.
    from plugins.trade.fibo.executor import (
        _read_actual_position_from_response,
    )
    after_close = _read_actual_position_from_response(reg, verify_response)
    if not after_close.is_flat:
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            read_failed=False,
            blocked_reason=(
                f"CLOSE_NOT_FLAT: after close_position the "
                f"instrument still shows "
                f"{_format_decimal(after_close.size)} "
                f"{after_close.side.upper()} — refusing to open "
                f"new-cycle target"
            ),
            reason="close did not flatten",
        )

    # Persist verified-flat and mark that the new-cycle open
    # is now required. We transition from CLOSE_VERIFIED to
    # OPEN_SENT so the next natural run can resume via the
    # cycle-decide's CASE 0b (OPEN_SENT + actual flat → open).
    try:
        if new_cycle_id == 0:
            store.finalize_inactive(reg.registration_key)
        else:
            # Advance transition state from CLOSE_VERIFIED to
            # OPEN_SENT (do NOT yet update synchronized_cycle_id;
            # that happens only when the new-cycle open verifies).
            store.advance_transition_open_sent(
                reg.registration_key,
                new_cycle_id=new_cycle_id,
            )
    except OSError as exc:
        return LiveConvergeResult(
            registration_key=reg.registration_key,
            allowlisted=True,
            placed_live_order=False,
            placed_request=None,
            cancelled_groups=(),
            read_failed=False,
            blocked_reason=(
                f"BLOCKED_CYCLE_STATE_UNWRITEABLE: cannot "
                f"persist close verification: {exc}"
            ),
            reason="close verified but unwriteable",
        )

    # Success. Close completed and verified flat. The next
    # natural convergence run will:
    #   - if new_cycle_id is non-zero, see that the cycle
    #     changed AND exchange is flat, and the legacy
    #     executor will open the new-cycle target via the
    #     normal Step 8 flow.
    #   - if new_cycle_id == 0, see target=0 + actual flat,
    #     and the legacy target=0/flat branch returns NOOP.
    return LiveConvergeResult(
        registration_key=reg.registration_key,
        allowlisted=True,
        placed_live_order=False,
        placed_request=None,
        cancelled_groups=(),
        read_failed=False,
        blocked_reason=None,
        reason=(
            f"cycle transition close complete: synchronized "
            f"old cycle was {cycle_decision['new_cycle_id']}; "
            f"instrument is now flat; awaiting next convergence "
            f"run for new-cycle OPEN"
        ),
    )


def _is_cycle_change_open_state(
    registration_key: str,
    snap,
    target,
) -> bool:
    """True iff the persisted cycle-state has an in-progress
    transition (CLOSE_VERIFIED or OPEN_SENT) AND the current
    MT4 cycle differs from the persisted synchronized_cycle.
    This indicates we are mid-cycle-change and a successful
    legacy open should be advanced to STEADY for the new cycle.
    """
    try:
        from plugins.trade.fibo.cycle_state import CycleStateStore
        store_obj = CycleStateStore()
    except OSError:
        return False
    synced = store_obj.get_synchronized_cycle_id(registration_key)
    transition = store_obj.get_transition(registration_key)
    if synced is None or synced == 0:
        return False
    if transition not in ("CLOSE_VERIFIED", "OPEN_SENT"):
        return False
    fibo = snap.find_fibo(target.symbol_for_lookup, target.variant_for_lookup)
    side_upper = str(target.side).upper()
    if fibo is None:
        return False
    current_cycle_id = int(fibo.side_cycle_id(side_upper) or 0)
    return current_cycle_id > 0 and current_cycle_id != int(synced)


def _current_mt4_cycle_id(reg, snap, target) -> int:
    """Look up the current MT4 cycle_id for this registration's
    side. Returns 0 if no fibo is found or cycle is inactive.
    """
    from plugins.trade.fibo.snapshot import Mt4Snapshot
    # We don't need to look up by reg.source_symbol / reg.variant
    # — target already encodes the side, and the cycle-decide
    # uses these. The caller has the snapshot, so we re-derive.
    fibo = snap.find_fibo(reg.source_symbol, reg.variant)
    if fibo is None:
        return 0
    side_upper = str(reg.side).upper()
    if not fibo.is_side_active(side_upper):
        return 0
    return int(fibo.side_cycle_id(side_upper) or 0)
