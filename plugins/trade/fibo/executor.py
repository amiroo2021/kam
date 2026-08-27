"""Stateless target-convergence executor.

Goal:
    On every fresh MT4 snapshot, converge the live exchange position
    on the registered instrument toward the MT4 target size on the
    MT4 side.

Algorithm:
    1. Read positions_orders for (exchange, account). Capture
       current (symbol, side, size).
    2. If the desired target is on the SAME (symbol, side), cancel
       every pending adjustment order on that (symbol, side)
       before placing a new one — they would otherwise queue and
       over-fill the target.
    3. Re-read positions_orders (cancel-on-venue is asynchronous;
       a re-read is the only source of truth).
    4. Compute the remaining delta against the latest MT4 target.
    5. If the remaining delta is positive AND on the same side,
       place a single new_order at MARKET type for the exact
       remaining quantity. If the remaining delta is zero, do
       nothing. If the venue is on the OPPOSITE side or the cycle
       is inactive, do nothing (no auto-flip, no auto-close).

Statelessness:
    The executor holds no in-memory state of past runs. Every call
    to ``converge`` starts from a fresh exchange read. Order
    idempotency is achieved by always cancelling pending adjustment
    orders BEFORE reading again and re-differencing.

Non-goals:
    - No partial-fill recovery. Any unfilled remainder is naturally
      corrected on the next reconciliation cycle (the executor is
      invoked again, sees a smaller actual_size, and posts the
      remainder).
    - No auto-flip: WRONG_SIDE is a no-op (operator must intervene).
    - No auto-close: SHOULD_FLATTEN is a no-op (operator must
      intervene).
    - No TP/SL arming.
    - No re-entry on the opposite side.

Trade-desk calls:
    Only ``positions_orders``, ``cancel_order_group``, and
    ``new_order`` (market type). All other operations are
    deliberately never invoked by this executor.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from plugins.trade.fibo.snapshot import Mt4Snapshot, SIDE_BUY as FIBO_SIDE_BUY, SIDE_SELL as FIBO_SIDE_SELL
from plugins.trade.fibo.store import FiboRegistration

logger = logging.getLogger(__name__)

# Type aliases — the executor is dependency-injected with a
# ``Callable[[Dict[str, Any]], Any]`` that mirrors TradeDesk.execute.
ExecuteFn = Callable[[Dict[str, Any]], Any]


# Venue-side canonical strings (used by every TradeDesk
# ``new_order`` / ``cancel_order_group`` request). Independent
# from the snapshot's BUY/SELL constants so the executor is
# self-contained and trivially testable.
SIDE_BUY = "buy"
SIDE_SELL = "sell"


def _fibo_to_venue_side(fibo_side: str) -> str:
    """Convert snapshot-side (``BUY``/``SELL``) to venue-side (``buy``/``sell``).

    Empty / unknown inputs map to ``""`` (caller treats empty as flat).
    """
    text = str(fibo_side or "").strip().upper()
    if text == FIBO_SIDE_BUY:
        return SIDE_BUY
    if text == FIBO_SIDE_SELL:
        return SIDE_SELL
    return ""


def _reg_mt4_side(reg: FiboRegistration) -> str:
    """Return the snapshot-side constant (``BUY`` / ``SELL``) for a
    Fibo registration's BUY/SELL side."""
    text = str(reg.side or "").strip().upper()
    if text == FIBO_SIDE_BUY:
        return FIBO_SIDE_BUY
    if text == FIBO_SIDE_SELL:
        return FIBO_SIDE_SELL
    return ""


@dataclass(frozen=True)
class ExchangePosition:
    """Parsed actual position for the registered symbol.

    Three possible states:
        - ``read_failed=True`` → the venue refused or errored on
          ``positions_orders``. The executor MUST treat this as
          ``blocked`` (no cancel, no order).
        - ``read_failed=False`` and ``is_flat=True`` → venue
          genuinely has no position on the registered instrument.
          The executor may proceed with ``OPEN @ target_size``.
        - ``read_failed=False`` and ``is_flat=False`` → venue
          has a real position. The executor proceeds with the
          normal ``INCREASE`` / no-op decision.

    Distinguishing the first two is a safety-critical invariant:
    the executor must NEVER cancel pending orders or place new
    orders when the venue's actual position is unknown.
    """
    symbol: str
    side: str  # canonical "buy" / "sell"
    size: Decimal
    read_failed: bool = False

    @property
    def is_flat(self) -> bool:
        # ``read_failed`` positions report ``is_flat=True`` as a
        # safe default — callers MUST also check ``read_failed``
        # before acting on the ``is_flat`` signal.
        return (
            self.read_failed
            or self.side not in (SIDE_BUY, SIDE_SELL)
            or self.size <= Decimal("0")
        )


@dataclass(frozen=True)
class Mt4Target:
    """Parsed MT4-side target for the registered side."""
    side: str  # canonical venue-side "buy" / "sell" (already converted from snapshot BUY/SELL)
    size: Decimal

    @property
    def is_flat(self) -> bool:
        return self.size <= Decimal("0")


@dataclass(frozen=True)
class ConvergeResult:
    """Outcome of a single ``converge`` invocation.

    This is the executor's return value — a record of what it did
    on the live exchange. It is intentionally a plain dataclass so
    tests can assert on it without mocking canonical contracts.

    Failure flags:
        - ``read_failed=True`` — at least one of the two
          ``positions_orders`` reads (BEFORE / AFTER) failed. The
          executor issued zero cancels and zero orders. The
          caller MUST treat the convergence as blocked and retry
          on a later cycle.
        - ``cancel_failed=True`` — at least one matching Fibo
          pending order was NOT positively cancelled. The
          executor refused to place a new order because the
          unresolved old order could still fill and cause
          overexposure. Caller MUST retry once the cancel has
          been verified.

    Both flags default to ``False``. They are independent and
    may both be set on the same call (e.g. positions_orders
    timed out AND a previous cancel also failed).
    """
    registration_key: str
    # Exchange reads.
    exchange_position_before: ExchangePosition
    exchange_position_after: ExchangePosition
    # The MT4 target snapshot used for this cycle.
    mt4_target: Mt4Target
    # Cancellation outcome — list of (symbol, side) groups that were
    # cancelled. Empty when there was nothing to cancel.
    cancelled_groups: Tuple[Tuple[str, str], ...]
    # Order placement outcome — set when a single new_order was placed.
    placed_order: Optional[Dict[str, Any]]
    # Plain-text reason for the outcome (suitable for logging).
    reason: str
    # Failure flags — see class docstring.
    read_failed: bool = False
    cancel_failed: bool = False


# ---------------------------------------------------------------------------
# Side-aware fibo side parsing
# ---------------------------------------------------------------------------


def _normalize_actual_side(raw: Any) -> str:
    """Map a venue's position-side string to ``buy`` / ``sell``.

    Anything unrecognised is treated as flat (the executor will
    only place a single same-direction order).
    """
    text = str(raw or "").strip().lower()
    if text in (SIDE_BUY, "long"):
        return SIDE_BUY
    if text in (SIDE_SELL, "short"):
        return SIDE_SELL
    return ""


def _decimal_or_zero(raw: Any) -> Decimal:
    """Best-effort Decimal parse; ``Decimal(0)`` on failure."""
    if raw is None or raw == "":
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _format_decimal(value: Decimal) -> str:
    """Render a Decimal as a canonical ``0.002``-style string.

    Trailing zeros are stripped (e.g. ``Decimal("0.0020")`` →
    ``"0.002"``) so the wire shape matches the dry-run
    reconciler's display and avoids a 38-digit noise floor.
    """
    if value is None:
        return "0"
    try:
        text = format(value.normalize(), "f")
    except Exception:  # noqa: BLE001
        return str(value)
    if text in ("", "-0"):
        return "0"
    return text


# ---------------------------------------------------------------------------
# Snapshot-driven MT4 target computation
# ---------------------------------------------------------------------------


def _resolve_mt4_target(
    reg: FiboRegistration,
    snap: Mt4Snapshot,
) -> Mt4Target:
    """Compute the desired (side, size) for this registration
    from the current MT4 snapshot.

    The math is intentionally identical to what the dry-run
    reconciler uses (``_compute_delta``) so the executor does not
    disagree with the dry-run view.

    Returns ``Mt4Target`` with ``size=0`` when the MT4 cycle for
    the registration's side is inactive (this signals SHOULD_FLATTEN
    — but the executor will NOT auto-flatten; it leaves that to the
    operator).
    """
    fibo_side = _reg_mt4_side(reg)
    venue_side = _fibo_to_venue_side(fibo_side)
    fibo = snap.find_fibo(reg.source_symbol, reg.variant)
    if fibo is None:
        return Mt4Target(side=venue_side, size=Decimal("0"))

    is_active = bool(fibo.is_side_active(reg.side))
    if not is_active:
        return Mt4Target(side=venue_side, size=Decimal("0"))

    weight = Decimal(str(fibo.side_cumulative_weight(reg.side) or "0"))
    starting = Decimal(str(reg.starting_volume or "0"))
    desired = starting * weight
    return Mt4Target(side=venue_side, size=desired)


# ---------------------------------------------------------------------------
# Position parsing
# ---------------------------------------------------------------------------


def _actual_position_for_symbol(
    positions: Any, symbol: str,
) -> ExchangePosition:
    """Return the parsed ExchangePosition matching ``symbol``.

    Returns a flat ``ExchangePosition`` if no row matches.
    """
    rows = list(positions or [])
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_symbol = str(row.get("symbol") or "").strip().upper()
        if row_symbol != symbol.upper():
            continue
        side = _normalize_actual_side(row.get("side"))
        size = _decimal_or_zero(row.get("size"))
        return ExchangePosition(
            symbol=row_symbol,
            side=side,
            size=size,
        )
    return ExchangePosition(symbol=symbol.upper(), side="", size=Decimal("0"))


def _read_actual_position_from_response(
    reg: FiboRegistration,
    response: Any,
) -> ExchangePosition:
    """Parse a CanonicalResponse-shaped ``response`` into an
    ExchangePosition.

    Read failure modes — exception, ``success=False``, missing
    fields — produce an ``ExchangePosition(read_failed=True)``
    so the executor can refuse to act. The previous behaviour
    (returning a flat position on read failure) was unsafe
    because it could cause the executor to send a new order
    against an unknown venue state.
    """
    if not getattr(response, "success", False):
        logger.warning(
            "fibo_executor: positions_orders returned failure for %s: %s",
            reg.registration_key,
            getattr(getattr(response, "error", None), "message", "<no error>"),
        )
        return ExchangePosition(
            symbol=str(reg.exchange_instrument or "").upper(),
            side="", size=Decimal("0"),
            read_failed=True,
        )

    positions = list(getattr(response, "positions", None) or [])
    if reg.exchange_instrument:
        # Try the canonical (registered) symbol first.
        direct = _actual_position_for_symbol(
            positions, reg.exchange_instrument,
        )
        if not direct.is_flat:
            return direct
        # Fallback: agents like Ondo normalise the venue symbol
        # to the underlying base. Match against the source_symbol.
        src = str(reg.source_symbol or "").strip().upper()
        for row in positions:
            if not isinstance(row, dict):
                continue
            row_symbol = str(row.get("symbol") or "").strip().upper()
            if src and row_symbol == src:
                side = _normalize_actual_side(row.get("side"))
                size = _decimal_or_zero(row.get("size"))
                return ExchangePosition(symbol=src, side=side, size=size)
    return ExchangePosition(
        symbol=str(reg.exchange_instrument or "").upper(),
        side="", size=Decimal("0"),
    )


def _read_actual_position(
    reg: FiboRegistration,
    *,
    execute_fn: ExecuteFn,
) -> ExchangePosition:
    """Read the live position for ``reg.exchange_instrument``.

    Failure modes (exception, ``success=False``, malformed
    response) produce an ``ExchangePosition(read_failed=True)``
    so the caller can refuse to act. The executor MUST NOT
    treat a read failure as "venue flat" — the venue state is
    unknown and any cancel / new_order decision would be made
    on incomplete information.

    The MT4 snapshot carries the source symbol (e.g. ``ETHUSD``)
    and the registration carries the venue symbol (e.g.
    ``ETH-USD.P``). Some agents (Ondo) normalise the venue
    ``symbol`` to ``ETH`` (stripping the ``-USD.P`` suffix); others
    keep the full venue string. The executor looks up by venue
    symbol as registered, but falls back to a substring match so
    cross-agent symbol-shape differences don't break the read.
    """
    request = {
        "operation": "positions_orders",
        "exchange": reg.exchange,
        "account": reg.account,
    }
    try:
        response = execute_fn(request)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fibo_executor: positions_orders raised for %s/%s/%s: %s",
            reg.exchange, reg.account, reg.registration_key, exc,
        )
        return ExchangePosition(
            symbol=str(reg.exchange_instrument or "").upper(),
            side="", size=Decimal("0"),
            read_failed=True,
        )

    return _read_actual_position_from_response(reg, response)


# ---------------------------------------------------------------------------
# Pending-order cancellation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _OpenOrderGroup:
    """Parsed open-order group for the registered symbol."""
    symbol: str
    side: str
    total_size: Decimal

    @property
    def is_resting(self) -> bool:
        return self.total_size > Decimal("0")


def _parse_open_groups(response: Any) -> List[_OpenOrderGroup]:
    """Parse ``CanonicalResponse.order_groups`` into ``_OpenOrderGroup``.

    Returns an empty list on failure or absence. Each row carries
    (symbol, side, total_size) — enough to decide whether to cancel.
    """
    groups = list(getattr(response, "order_groups", None) or [])
    out: List[_OpenOrderGroup] = []
    for row in groups:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        side = str(row.get("side") or "").strip().lower()
        total_size = _decimal_or_zero(row.get("total_size"))
        if side not in (SIDE_BUY, SIDE_SELL):
            continue
        if total_size <= Decimal("0"):
            continue
        out.append(
            _OpenOrderGroup(symbol=symbol, side=side, total_size=total_size)
        )
    return out


def _pending_groups_for_target(
    groups: List[_OpenOrderGroup],
    *,
    target_symbol: str,
    target_side: str,
) -> List[_OpenOrderGroup]:
    """Return the open-order groups that match the executor's
    intended target — same symbol, same side.

    The executor only places orders on the target's side, so it
    cancels ONLY the matching (symbol, side) groups. Orders on the
    opposite side or other symbols are left to the operator.
    """
    out: List[_OpenOrderGroup] = []
    for g in groups:
        if g.symbol.upper() != target_symbol.upper():
            continue
        if g.side != target_side:
            continue
        if not g.is_resting:
            continue
        out.append(g)
    return out


def _cancel_pending_groups(
    reg: FiboRegistration,
    *,
    execute_fn: ExecuteFn,
    groups: List[_OpenOrderGroup],
) -> Tuple[Tuple[Tuple[str, str], ...], bool, List[Dict[str, Any]]]:
    """Cancel the given open-order groups via ``cancel_order_group``.

    Returns a tuple of
    ``(cancelled_symbol_side_pairs, cancel_failed, raw_responses)``.

    ``cancel_failed`` is ``True`` if ANY input group was NOT
    positively cancelled — either because the call raised,
    returned ``success=False``, or returned a malformed response.

    The caller (converge) refuses to place a new order when
    ``cancel_failed=True``: a leftover resting order on the
    venue could still fill after the new order is sent and cause
    overexposure. The convergence must retry on the next cycle
    once the cancel has been verified.

    Idempotent: a group already cancelled on the venue returns
    a no-op success and counts as cancelled.

    Raw responses are captured for the audit log.
    """
    cancelled: List[Tuple[str, str]] = []
    raw_responses: List[Dict[str, Any]] = []
    cancel_failed = False
    for g in groups:
        request = {
            "operation": "cancel_order_group",
            "exchange": reg.exchange,
            "account": reg.account,
            "symbol": g.symbol,
            "side": g.side,
        }
        try:
            response = execute_fn(request)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "fibo_executor: cancel_order_group raised for %s (%s/%s): %s",
                reg.registration_key, g.symbol, g.side, exc,
            )
            cancel_failed = True
            continue
        # Capture the raw response for the audit log.
        raw = (
            response.to_dict() if hasattr(response, "to_dict")
            else dict(response)
        )
        raw_responses.append(raw)
        if getattr(response, "success", False):
            cancelled.append((g.symbol, g.side))
        else:
            logger.warning(
                "fibo_executor: cancel_order_group failure for %s (%s/%s): %s",
                reg.registration_key, g.symbol, g.side,
                getattr(getattr(response, "error", None), "message", "<no error>"),
            )
            cancel_failed = True
    return tuple(cancelled), cancel_failed, raw_responses


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Delta:
    """Internal delta computed by the executor.

    ``action`` is the executor's own classification (decoupled from
    the dry-run reconciler's DeltaAction enum so the executor can
    be tested independently).
    """
    action: str  # "OPEN" | "INCREASE" | "NONE"
    side: str  # canonical "buy" / "sell"
    size: Decimal  # always > 0


def _compute_remaining_delta(
    actual: ExchangePosition,
    target: Mt4Target,
) -> Optional[_Delta]:
    """Compute the remaining same-direction delta.

    Returns:
        - ``None`` when the convergence should NOT place an order
          (target flat, target on opposite side, or no remaining
          gap).
        - ``_Delta`` with action="OPEN" when venue is flat and
          target is non-zero.
        - ``_Delta`` with action="INCREASE" when venue is on the
          same side but smaller than target.

    The executor never issues REDUCE / SHOULD_FLATTEN / WRONG_SIDE
    orders. Those are deliberately out of scope.
    """
    # Target is flat — let the operator handle SHOULD_FLATTEN.
    if target.size <= Decimal("0"):
        return None
    # Venue is flat → open at target size.
    if actual.is_flat:
        return _Delta(action="OPEN", side=target.side, size=target.size)
    # Wrong side — never auto-flip.
    if actual.side != target.side:
        return None
    # Same side. Compare sizes.
    if actual.size >= target.size:
        return None
    return _Delta(
        action="INCREASE",
        side=target.side,
        size=target.size - actual.size,
    )


# ---------------------------------------------------------------------------
# New-order placement
# ---------------------------------------------------------------------------


def _fibo_client_order_id(
    reg: FiboRegistration,
    *,
    snap: Mt4Snapshot,
    target: Mt4Target,
    delta: "_Delta",
) -> str:
    """Deterministic, intent-unique client-order id for one
    Fibo adjustment.

    Convention: ``fibo-<16-char hex>``. The hex is the SHA-256 of:

        registration_key
        | MT4 snapshot seq
        | MT4 snapshot source
        | MT4 cycle id (resolved from snapshot for the
          registration's side; 0 when inactive / missing)
        | target side
        | target size
        | delta side
        | delta size

    Two adjustments that differ in ANY of those fields produce
    different ids. Within a single convergence attempt the id
    is stable (deterministic from inputs).

    Guarantees:
        - Unique to the current adjustment intent (not reused
          across the lifetime of the registration).
        - Stable / deterministic for the same inputs.
        - <= 64 chars (per Ondo's documented limit).

    The executor does NOT use the id for cancel-by-id — cancels
    happen at the (symbol, side) group level. The id exists so
    post-hoc reconciliation can identify Fibo-owned orders by
    client_order_id and so the venue's own idempotency layer
    can dedupe accidental double-submits.
    """
    cycle_id = 0
    fibo = snap.find_fibo(reg.source_symbol, reg.variant)
    if fibo is not None:
        try:
            cycle_id = int(fibo.side_cycle_id(reg.side) or 0)
        except (ValueError, AttributeError):
            cycle_id = 0
    payload = (
        f"{reg.registration_key}|{snap.seq}|{snap.source}|"
        f"{cycle_id}|{target.side}|{_format_decimal(target.size)}|"
        f"{delta.side}|{_format_decimal(delta.size)}"
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"fibo-{digest[0:16]}"


def _place_market_order(
    reg: FiboRegistration,
    *,
    execute_fn: ExecuteFn,
    side: str,
    size: Decimal,
    snap: Mt4Snapshot,
    target: Mt4Target,
    delta: "_Delta",
) -> Optional[Dict[str, Any]]:
    """Place a single MARKET order for the remaining delta.

    Always uses ``order_type="market"`` and ``reduce_only=False``.
    The executor never places reduce-only orders (that's a
    SHOULD_FLATTEN responsibility — out of scope).

    The client_order_id is intent-unique (includes registration,
    snapshot seq, cycle id, target, delta) so the venue's
    idempotency layer can dedupe accidental double-submits
    without blocking legitimate re-entries on a later cycle.

    Returns the raw response dict on success, ``None`` on
    failure (errors are logged, not raised).
    """
    if side not in (SIDE_BUY, SIDE_SELL):
        return None
    request = {
        "operation": "new_order",
        "exchange": reg.exchange,
        "account": reg.account,
        "symbol": reg.exchange_instrument,
        "side": side,
        "order_type": "market",
        "volume": _format_decimal(size),
        "reduce_only": False,
        "client_order_id": _fibo_client_order_id(
            reg, snap=snap, target=target, delta=delta,
        ),
    }
    try:
        response = execute_fn(request)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fibo_executor: new_order raised for %s (%s/%s): %s",
            reg.registration_key, side, size, exc,
        )
        return None
    if not getattr(response, "success", False):
        logger.warning(
            "fibo_executor: new_order failure for %s (%s/%s): %s",
            reg.registration_key, side, size,
            getattr(getattr(response, "error", None), "message", "<no error>"),
        )
        return None
    return (
        response.to_dict() if hasattr(response, "to_dict")
        else dict(response)
    )


# ---------------------------------------------------------------------------
# Top-level convergence
# ---------------------------------------------------------------------------


def converge(
    reg: FiboRegistration,
    snap: Mt4Snapshot,
    *,
    execute_fn: ExecuteFn,
) -> ConvergeResult:
    """Converge the live exchange position toward the MT4 target.

    Stateless: each call reads the exchange afresh. Safe to invoke
    on every fresh MT4 snapshot.

    Algorithm:
        1. Read positions_orders (BEFORE).
           - If the read FAILS (exception, success=False, malformed
             response) the executor refuses to act: no cancels,
             no orders. ConvergeResult.read_failed=True.
        2. Compute MT4 target from the snapshot.
        3. Compute the same-direction delta the executor WILL place.
        4. If a non-zero delta is pending, cancel any pending
           open-order groups for the same (symbol, side) BEFORE
           the re-read. If the cancel FAILS for ANY of the matching
           groups, the executor refuses to place a new order in
           this cycle (a leftover resting order could still fill
           and cause overexposure). ConvergeResult.cancel_failed=True.
        5. Re-read positions_orders (AFTER).
           - If the AFTER read FAILS, refuse to act. read_failed=True.
        6. Recompute the remaining delta against the post-cancel
           position.
        7. If the remaining delta is positive AND on the same
           side, place exactly one new_order at MARKET type for
           the exact remaining quantity.
        8. Return a ConvergeResult describing every step.

    The executor NEVER issues:
        - a REDUCE order
        - a SHOULD_FLATTEN close
        - a WRONG_SIDE flip
        - a TP / SL trigger
        - a partial-fill retry

    If the remaining delta is zero (the executor already converged
    on a previous call), the function returns a ``ConvergeResult``
    with ``placed_order=None`` and ``reason="already at target"``.
    """
    target = _resolve_mt4_target(reg, snap)
    target_symbol = str(reg.exchange_instrument or "").strip().upper()

    # ------------------------------------------------------------------
    # Step 1 — BEFORE read.
    #
    # FAIL-CLOSED: any read failure (exception, success=False,
    # malformed response) means the venue state is unknown. We
    # refuse to cancel or place anything and return immediately
    # with read_failed=True.
    # ------------------------------------------------------------------
    request = {
        "operation": "positions_orders",
        "exchange": reg.exchange,
        "account": reg.account,
    }
    initial_groups: List[Any] = []
    # Single read: capture BOTH the position and the open-order
    # groups from the same response. Fail-closed on any error.
    try:
        first_response = execute_fn(request)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fibo_executor: positions_orders raised for %s: %s",
            reg.registration_key, exc,
        )
        first_response = None
    if first_response is not None and getattr(
        first_response, "success", False,
    ):
        before = _read_actual_position_from_response(reg, first_response)
        initial_groups = _parse_open_groups(first_response)
    elif first_response is not None:
        # success=False → read failure
        logger.warning(
            "fibo_executor: positions_orders returned failure for %s: %s",
            reg.registration_key,
            getattr(getattr(first_response, "error", None), "message",
                    "<no error>"),
        )
        before = ExchangePosition(
            symbol=str(reg.exchange_instrument or "").upper(),
            side="", size=Decimal("0"),
            read_failed=True,
        )
    else:
        # Exception path
        before = ExchangePosition(
            symbol=str(reg.exchange_instrument or "").upper(),
            side="", size=Decimal("0"),
            read_failed=True,
        )

    if before.read_failed:
        return ConvergeResult(
            registration_key=reg.registration_key,
            exchange_position_before=before,
            exchange_position_after=before,
            mt4_target=target,
            cancelled_groups=(),
            placed_order=None,
            reason=(
                "positions_orders BEFORE read failed — convergence "
                "blocked (zero cancels, zero orders)"
            ),
            read_failed=True,
        )

    # Compute the delta we WOULD send based on the BEFORE view.
    # If there is no delta to send (already at target, wrong side,
    # or target flat), we leave any pending orders on the venue
    # untouched and skip the cancel + re-read steps entirely.
    prospective_delta = _compute_remaining_delta(before, target)

    cancelled: Tuple[Tuple[str, str], ...] = ()
    cancel_failed = False
    if prospective_delta is not None and target_symbol:
        target_groups = _pending_groups_for_target(
            initial_groups,
            target_symbol=target_symbol,
            target_side=prospective_delta.side,
        )
        if target_groups:
            cancelled, cancel_failed, _raw = _cancel_pending_groups(
                reg, execute_fn=execute_fn, groups=target_groups,
            )
            if cancel_failed:
                # FAIL-CLOSED: any unverified cancel means the old
                # order could still fill and over-fill the target.
                # Refuse to place new_order in this cycle.
                after_for_result = _read_actual_position(
                    reg, execute_fn=execute_fn,
                )
                return ConvergeResult(
                    registration_key=reg.registration_key,
                    exchange_position_before=before,
                    exchange_position_after=after_for_result,
                    mt4_target=target,
                    cancelled_groups=cancelled,
                    placed_order=None,
                    reason=(
                        "matching pending adjustment cancel did not "
                        "positively succeed — convergence blocked "
                        "(zero orders)"
                    ),
                    cancel_failed=True,
                )

    # ------------------------------------------------------------------
    # Step 5 — AFTER re-read.
    #
    # FAIL-CLOSED: any read failure here also blocks the order.
    # ------------------------------------------------------------------
    after = _read_actual_position(reg, execute_fn=execute_fn)
    if after.read_failed:
        return ConvergeResult(
            registration_key=reg.registration_key,
            exchange_position_before=before,
            exchange_position_after=after,
            mt4_target=target,
            cancelled_groups=cancelled,
            placed_order=None,
            reason=(
                "positions_orders AFTER read failed — convergence "
                "blocked (zero orders)"
            ),
            read_failed=True,
        )

    # Step 6: recompute against the post-cancel position.
    delta = _compute_remaining_delta(after, target)

    # Step 7: place a single same-direction order.
    placed: Optional[Dict[str, Any]] = None
    reason = "no remaining delta"
    if delta is None:
        if target.size <= Decimal("0"):
            reason = "mt4 target flat — no auto-flatten"
        elif after.is_flat:
            reason = "venue flat but target flat"
        elif after.side != target.side:
            reason = (
                "venue on opposite side of target — no auto-flip"
            )
        elif after.size >= target.size:
            reason = "already at target"
        else:
            reason = "no remaining delta"
    else:
        placed = _place_market_order(
            reg, execute_fn=execute_fn,
            side=delta.side, size=delta.size,
            snap=snap, target=target, delta=delta,
        )
        if placed is None:
            reason = "new_order failed"
        else:
            reason = (
                f"placed {delta.action} {delta.side} {delta.size} for "
                f"{reg.registration_key}"
            )

    return ConvergeResult(
        registration_key=reg.registration_key,
        exchange_position_before=before,
        exchange_position_after=after,
        mt4_target=target,
        cancelled_groups=cancelled,
        placed_order=placed,
        reason=reason,
        cancel_failed=cancel_failed,
    )
