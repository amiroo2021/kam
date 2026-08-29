"""Phase 2 — read-only Fibo reconciler.

For each persisted Fibo registration, compare:

    MT4 desired state
vs
    actual exchange position (read-only GET /v1/perps/positions)

and compute the exact delta that WOULD be required. This module
**never places, cancels, closes, or modifies any order or
position**. It is read-only end-to-end.

Inputs:
    1. ~/.hermes/fibo/registrations.jsonl  (read-only)
    2. ~/.hermes/fibo/mt4_snapshot.json    (read-only)
    3. Exchange state via TradeDesk.execute()
       using ONLY the read operations:
         - ``positions_orders``
         - ``resolve_instrument``
       (Both are pure GET paths in the x_<exchange>_agent layer.
       See ``test_fibo_reconciler.py::test_no_exchange_writes_path``
       for the static source guard.)

Outputs:
    An in-memory ``ReconciliationResult`` per registration, plus an
    optional flat serialization via ``to_dict()``. Nothing is
    written back to disk.

Cycle handling:
    If MT4 cycle_id changes, surface
    ``previous_cycle_id``/``current_cycle_id`` and recalculate from
    the CURRENT cumulative weight.

    If MT4 side becomes inactive (cycle_id<=0 OR cumulative_weight<=0),
    desired_size becomes 0 and delta_action becomes ``SHOULD_FLATTEN``
    if the venue still has a position. The reconciler never closes
    the position itself in Phase 2.

Freshness gate:
    If snapshot age > 30s, ``delta_action`` is ``STALE_MT4`` and the
    numeric delta is left at 0 (the spec forbids actionable deltas on
    stale data).

Failure modes:
    Malformed registration, malformed snapshot, missing snapshot,
    or exchange read failure → ``delta_action=ERROR`` with a
    ``reason`` field. The reconciler fails closed (no delta proposed).
"""

from __future__ import annotations

import enum
import logging
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .snapshot import (
    Mt4Snapshot,
    Mt4SnapshotStore,
    SIDE_BUY,
    SIDE_SELL,
)
from .store import (
    FiboRegistration,
    FiboRegistrationStore,
)

logger = logging.getLogger(__name__)


# Spec §4: snapshot age threshold for staleness. Mirrors flow.STALE_THRESHOLD_SECONDS
# but is duplicated here to keep the reconciler standalone (no flow import).
STALE_MT4_THRESHOLD_SECONDS = 30.0

# Delta actions.
class DeltaAction(str, enum.Enum):
    NONE = "NONE"
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    INCREASE_LONG = "INCREASE_LONG"
    INCREASE_SHORT = "INCREASE_SHORT"
    REDUCE_LONG = "REDUCE_LONG"
    REDUCE_SHORT = "REDUCE_SHORT"
    SHOULD_FLATTEN = "SHOULD_FLATTEN"
    WRONG_SIDE = "WRONG_SIDE"
    STALE_MT4 = "STALE_MT4"
    NEEDS_INSTRUMENT_SELECTION = "NEEDS_INSTRUMENT_SELECTION"
    ERROR = "ERROR"


# Normalized sides.
class Side(str, enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationResult:
    """One read-only reconciliation outcome for a single registration.

    Mirrors the schema in the spec exactly. All Decimal fields are
    serialized as strings so the JSONL/on-wire shape is lossless
    (no float drift).
    """

    registration_key: str
    exchange: str
    account: str
    source_symbol: str           # MT4 source symbol (e.g. "ETHUSD")
    exchange_instrument: str     # venue contract (e.g. "ETH-USD.P")
    variant: str
    side: str  # canonical BUY / SELL

    starting_volume: str

    # MT4 side
    mt4_source: str
    mt4_seq: int
    mt4_cycle_id: int
    mt4_weight: str
    mt4_percentage: str
    mt4_age_seconds: Optional[float]
    mt4_active: bool

    # Cycle change detection
    previous_cycle_id: Optional[int]
    cycle_changed: bool

    # Desired vs actual
    desired_side: str  # LONG / SHORT / FLAT
    desired_size: str

    actual_side: str  # LONG / SHORT / FLAT
    actual_size: str
    actual_entry_price: Optional[str]

    # Delta
    delta_action: str
    delta_size: str
    safe_to_execute_later: bool
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    if not d.is_finite():
        return None
    return d


def _fmt(d: Optional[Decimal]) -> str:
    if d is None:
        return "0"
    # Use the normalized form so trailing zeros from the user's
    # input are preserved (e.g. 0.10 stays 0.10).
    try:
        return format(d, "f")
    except Exception:  # noqa: BLE001
        return str(d)


def _normalize_actual_side(side_str: str) -> Side:
    """Normalize a venue's position-side string to LONG / SHORT / FLAT.

    The Ondo agent returns ``"long"`` or ``"short"`` for open positions.
    A position with size=0 is treated as FLAT.
    """
    s = (side_str or "").strip().lower()
    if s in ("long", "buy", "b"):
        return Side.LONG
    if s in ("short", "sell", "s"):
        return Side.SHORT
    return Side.FLAT


def _actual_position_for_symbol(
    positions: List[Any],
    symbol: str,
) -> Optional[Any]:
    """Return the CanonicalPosition matching ``symbol`` (case-insensitive),
    or None if not present / empty.

    Phase 2.13.22: the canonical matcher is owned by
    ``executor._row_identity`` (which checks ``exchange_instrument``
    first, then ``symbol``). The Running Fibo display must use the
    SAME matcher as production convergence so the read-only
    Actual/Delta panel reflects what ``live_converge`` sees.
    Importing here avoids a second independent position-matching
    implementation.
    """
    from .executor import _row_identity
    target = (symbol or "").strip().upper()
    for p in positions or []:
        if _row_identity(p) == target:
            return p
    return None


def _build_error(
    reg: FiboRegistration,
    *,
    reason: str,
    mt4_snapshot: Optional[Mt4Snapshot] = None,
) -> ReconciliationResult:
    """Construct an ERROR result for a registration that cannot be
    reconciled. Fails closed — no actionable delta."""
    mt4_age = mt4_snapshot.age_seconds() if mt4_snapshot is not None else None
    return ReconciliationResult(
        registration_key=reg.registration_key,
        exchange=reg.exchange,
        account=reg.account,
        source_symbol=reg.source_symbol,
        exchange_instrument=reg.exchange_instrument,
        variant=reg.variant,
        side=reg.side,
        starting_volume=_fmt(reg.starting_volume),
        mt4_source=mt4_snapshot.source if mt4_snapshot else "",
        mt4_seq=mt4_snapshot.seq if mt4_snapshot else 0,
        mt4_cycle_id=0,
        mt4_weight="0",
        mt4_percentage="0",
        mt4_age_seconds=mt4_age,
        mt4_active=False,
        previous_cycle_id=None,
        cycle_changed=False,
        desired_side=Side.FLAT.value,
        desired_size="0",
        actual_side=Side.FLAT.value,
        actual_size="0",
        actual_entry_price=None,
        delta_action=DeltaAction.ERROR.value,
        delta_size="0",
        safe_to_execute_later=False,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


class FiboReconciler:
    """Compute the read-only delta for each persisted registration.

    Construction is dependency-injected so unit tests can pass fake
    TradeDesk-like callables (e.g. a stub that returns canned
    ``CanonicalResponse`` payloads without any network call).
    """

    def __init__(
        self,
        *,
        registration_store: FiboRegistrationStore,
        snapshot_store: Mt4SnapshotStore,
        execute_fn: Callable[[Dict[str, Any]], Any],
    ) -> None:
        # The injected ``execute_fn`` is the single call surface the
        # reconciler uses. Tests pass a stub; production passes
        # ``TradeDesk.execute``.
        self._registrations = registration_store
        self._snapshots = snapshot_store
        self._execute = execute_fn

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def reconcile_all(self) -> List[ReconciliationResult]:
        """Reconcile every persisted ACTIVE registration.

        Phase 2.6: stopped registrations are excluded. A stopped
        registration must never reach exchange reconciliation /
        positions inspection / action planning. ``load_all`` already
        returns the latest row per ``registration_key``, so the
        ``is_stopped`` check uses that effective state.
        """
        try:
            regs = self._registrations.load_all()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "fibo_reconciler: failed to load registrations: %s", exc
            )
            return []
        active = [r for r in regs if r.is_active]
        return [self.reconcile_one(r) for r in active]

    def reconcile_one(self, reg: FiboRegistration) -> ReconciliationResult:
        """Reconcile a single registration by its identity.

        Phase 2.1 identity split:
            * MT4 lookup uses ``reg.source_symbol`` (the symbol the
              MT4 Observer publishes, e.g. ``"ETHUSD"``).
            * Exchange lookup uses ``reg.exchange_instrument`` (the
              venue's contract identifier, e.g. ``"ETH-USD.P"``).
            * Legacy records with empty ``exchange_instrument`` are
              classified as ``NEEDS_INSTRUMENT_SELECTION`` and the
              reconciler makes NO exchange call.
        """
        # Defensive: re-validate identity components. A malformed
        # registration cannot be matched against the snapshot, so
        # fail closed.
        if reg.starting_volume <= 0:
            return _build_error(
                reg, reason="malformed registration: non-positive starting_volume"
            )

        # Phase 2.1: legacy records (no exchange_instrument) are
        # classified as NEEDS_INSTRUMENT_SELECTION. We still load
        # the snapshot so we can report MT4 fields in the result,
        # but we never call the venue.
        legacy = reg.is_legacy

        snap = self._snapshots.load()
        if snap is None:
            return _build_error(
                reg,
                reason="malformed snapshot: missing or invalid mt4_snapshot.json",
            )

        # Match the MT4 entry by (source_symbol, variant). Case-insensitive
        # on both sides; fibo.find_fibo already does this.
        fibo = snap.find_fibo(reg.source_symbol, reg.variant)
        if fibo is None:
            return _build_error(
                reg,
                reason=(
                    f"snapshot does not contain ({reg.source_symbol!r}, "
                    f"{reg.variant!r})"
                ),
                mt4_snapshot=snap,
            )

        # Side-specific MT4 fields.
        side = reg.side
        if side not in (SIDE_BUY, SIDE_SELL):
            return _build_error(
                reg,
                reason=f"unknown registration side {side!r}",
                mt4_snapshot=snap,
            )
        cycle_id = fibo.side_cycle_id(side)
        weight = fibo.side_cumulative_weight(side)
        percentage = fibo.percentage
        mt4_active = fibo.is_side_active(side)
        mt4_age = snap.age_seconds()

        # Freshness gate (spec §8). If stale, we still produce a
        # diagnostic result with delta_action=STALE_MT4 and
        # delta_size=0 — but no actionable delta.
        if mt4_age is not None and mt4_age > STALE_MT4_THRESHOLD_SECONDS:
            return ReconciliationResult(
                registration_key=reg.registration_key,
                exchange=reg.exchange,
                account=reg.account,
                source_symbol=reg.source_symbol,
                exchange_instrument=reg.exchange_instrument,
                variant=reg.variant,
                side=side,
                starting_volume=_fmt(reg.starting_volume),
                mt4_source=snap.source,
                mt4_seq=snap.seq,
                mt4_cycle_id=cycle_id,
                mt4_weight=_fmt(weight),
                mt4_percentage=_fmt(percentage),
                mt4_age_seconds=mt4_age,
                mt4_active=mt4_active,
                previous_cycle_id=None,
                cycle_changed=False,
                desired_side=Side.FLAT.value,
                desired_size="0",
                actual_side=Side.FLAT.value,
                actual_size="0",
                actual_entry_price=None,
                delta_action=DeltaAction.STALE_MT4.value,
                delta_size="0",
                safe_to_execute_later=False,
                reason=(
                    f"mt4_snapshot age {mt4_age:.1f}s exceeds "
                    f"{STALE_MT4_THRESHOLD_SECONDS:.0f}s threshold"
                ),
            )

        # Cycle change detection (spec §7). The registration records
        # the cycle_id at the time of Create. If the current MT4
        # cycle_id differs, we surface that and recalculate from the
        # CURRENT cumulative weight.
        previous_cycle_id = reg.source_cycle_id
        cycle_changed = (cycle_id != previous_cycle_id)

        # Resolve desired target.
        if not mt4_active:
            # Inactive MT4 cycle → desired = FLAT.
            desired_side = Side.FLAT
            desired_size = Decimal("0")
        else:
            # Decimal math (spec §3, no float).
            try:
                desired_size = reg.starting_volume * weight
            except Exception as exc:  # noqa: BLE001
                return _build_error(
                    reg,
                    reason=f"decimal target calc failed: {exc}",
                    mt4_snapshot=snap,
                )
            desired_side = Side.LONG if side == SIDE_BUY else Side.SHORT

        # Phase 2.1: legacy short-circuit. If the record has no
        # exchange_instrument we never call the venue; we report
        # the target with delta_action=NEEDS_INSTRUMENT_SELECTION
        # and safe_to_execute_later=False.
        if legacy:
            return ReconciliationResult(
                registration_key=reg.registration_key,
                exchange=reg.exchange,
                account=reg.account,
                source_symbol=reg.source_symbol,
                exchange_instrument=reg.exchange_instrument,
                variant=reg.variant,
                side=side,
                starting_volume=_fmt(reg.starting_volume),
                mt4_source=snap.source,
                mt4_seq=snap.seq,
                mt4_cycle_id=cycle_id,
                mt4_weight=_fmt(weight),
                mt4_percentage=_fmt(percentage),
                mt4_age_seconds=mt4_age,
                mt4_active=mt4_active,
                previous_cycle_id=previous_cycle_id,
                cycle_changed=cycle_changed,
                desired_side=desired_side.value,
                desired_size=_fmt(desired_size),
                actual_side=Side.FLAT.value,
                actual_size="0",
                actual_entry_price=None,
                delta_action=DeltaAction.NEEDS_INSTRUMENT_SELECTION.value,
                delta_size="0",
                safe_to_execute_later=False,
                reason=(
                    "registration pre-dates Phase 2.1 and has no "
                    "exchange_instrument; recreate via Start Fibo to "
                    "select the venue contract"
                ),
            )

        # Fetch exchange state (READ-ONLY). We pass the stored
        # exchange_instrument; we do NOT call resolve_instrument.
        try:
            resolved_instrument, actual_side, actual_size, actual_entry = (
                self._fetch_exchange_state(reg)
            )
        except Exception as exc:  # noqa: BLE001
            return ReconciliationResult(
                registration_key=reg.registration_key,
                exchange=reg.exchange,
                account=reg.account,
                source_symbol=reg.source_symbol,
                exchange_instrument=reg.exchange_instrument,
                variant=reg.variant,
                side=side,
                starting_volume=_fmt(reg.starting_volume),
                mt4_source=snap.source,
                mt4_seq=snap.seq,
                mt4_cycle_id=cycle_id,
                mt4_weight=_fmt(weight),
                mt4_percentage=_fmt(percentage),
                mt4_age_seconds=mt4_age,
                mt4_active=mt4_active,
                previous_cycle_id=previous_cycle_id,
                cycle_changed=cycle_changed,
                desired_side=desired_side.value,
                desired_size=_fmt(desired_size),
                actual_side=Side.FLAT.value,
                actual_size="0",
                actual_entry_price=None,
                delta_action=DeltaAction.ERROR.value,
                delta_size="0",
                safe_to_execute_later=False,
                reason=f"exchange read failed: {exc}",
            )

        # Compute the delta.
        delta_action, delta_size, reason, safe = self._compute_delta(
            desired_side=desired_side,
            desired_size=desired_size,
            actual_side=actual_side,
            actual_size=actual_size,
            mt4_active=mt4_active,
            cycle_changed=cycle_changed,
        )

        return ReconciliationResult(
            registration_key=reg.registration_key,
            exchange=reg.exchange,
            account=reg.account,
            source_symbol=reg.source_symbol,
            exchange_instrument=resolved_instrument,
            variant=reg.variant,
            side=side,
            starting_volume=_fmt(reg.starting_volume),
            mt4_source=snap.source,
            mt4_seq=snap.seq,
            mt4_cycle_id=cycle_id,
            mt4_weight=_fmt(weight),
            mt4_percentage=_fmt(percentage),
            mt4_age_seconds=mt4_age,
            mt4_active=mt4_active,
            previous_cycle_id=previous_cycle_id,
            cycle_changed=cycle_changed,
            desired_side=desired_side.value,
            desired_size=_fmt(desired_size),
            actual_side=actual_side.value,
            actual_size=_fmt(actual_size),
            actual_entry_price=actual_entry,
            delta_action=delta_action.value,
            delta_size=_fmt(delta_size),
            safe_to_execute_later=safe,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Exchange state fetch (READ-ONLY)
    # ------------------------------------------------------------------

    def _fetch_exchange_state(
        self, reg: FiboRegistration
    ) -> Tuple[str, Side, Decimal, Optional[str]]:
        """Fetch the actual exchange position for the stored
        ``exchange_instrument``.

        Returns: (resolved_instrument, actual_side, actual_size, actual_entry_price)

        Phase 2.1: we use the stored ``reg.exchange_instrument``
        DIRECTLY. We do NOT call ``resolve_instrument`` — the
        source/exchange identity split is the registration's
        responsibility, not the reconciler's.

        Uses ONLY TradeDesk operation:
          - ``positions_orders`` (GET /v1/perps/positions +
            GET /v1/perps/orders?status=open)

        Returns FLAT (with size=0) if the venue reports no open
        position for the stored instrument.
        """
        # 1. Use the stored exchange_instrument directly. We do
        # NOT call resolve_instrument here — that would substitute
        # the source symbol for the venue contract and reintroduce
        # the bug Phase 2 fixed.
        resolved = reg.exchange_instrument.strip()
        if not resolved:
            raise RuntimeError(
                "registration has no exchange_instrument; "
                "recreate via Start Fibo to select the venue contract"
            )

        # 2. Fetch actual positions (read-only).
        try:
            po_response = self._execute({
                "operation": "positions_orders",
                "exchange": reg.exchange,
                "account": reg.account,
            })
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"positions_orders call failed: {exc}"
            ) from exc

        if not getattr(po_response, "success", False):
            err = getattr(po_response, "error", None)
            err_msg = getattr(err, "message", "<no error>") if err else "<no error>"
            raise RuntimeError(
                f"positions_orders returned failure: {err_msg}"
            )

        positions = list(getattr(po_response, "positions", None) or [])
        position = _actual_position_for_symbol(positions, resolved)
        if position is None:
            return resolved, Side.FLAT, Decimal("0"), None

        size_d = _to_decimal(getattr(position, "size", "0")) or Decimal("0")
        side = _normalize_actual_side(str(getattr(position, "side", "")))
        entry = getattr(position, "entry_price", None)
        return resolved, side, size_d, str(entry) if entry else None

    # ------------------------------------------------------------------
    # Delta math
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_delta(
        *,
        desired_side: Side,
        desired_size: Decimal,
        actual_side: Side,
        actual_size: Decimal,
        mt4_active: bool,
        cycle_changed: bool,
    ) -> Tuple[DeltaAction, Decimal, str, bool]:
        """Pure function. Returns (action, size, reason, safe)."""
        # Inactive MT4 cycle → desired=FLAT. If the venue still has
        # a position, the action is SHOULD_FLATTEN (but Phase 2 does
        # not close anything — just reports).
        if not mt4_active:
            if actual_side == Side.FLAT:
                return (
                    DeltaAction.NONE,
                    Decimal("0"),
                    "MT4 cycle inactive; venue is flat — no action",
                    True,
                )
            return (
                DeltaAction.SHOULD_FLATTEN,
                actual_size,
                "MT4 cycle inactive; venue position present — "
                "should flatten (Phase 2: report only)",
                False,
            )

        # The MT4 side is active. We have a desired position.
        # Mirror spec §6: BUY → LONG, SELL → SHORT.
        # Case A: actual FLAT.
        if actual_side == Side.FLAT:
            if desired_size <= 0:
                return (
                    DeltaAction.NONE,
                    Decimal("0"),
                    "desired size is zero",
                    True,
                )
            action = (
                DeltaAction.OPEN_LONG
                if desired_side == Side.LONG
                else DeltaAction.OPEN_SHORT
            )
            return (
                action,
                desired_size,
                f"venue flat; open {action.value.replace('OPEN_', '')} {desired_size}",
                True,
            )

        # Wrong side on the venue.
        if (desired_side == Side.LONG and actual_side == Side.SHORT) or (
            desired_side == Side.SHORT and actual_side == Side.LONG
        ):
            return (
                DeltaAction.WRONG_SIDE,
                actual_size,
                f"venue on opposite side "
                f"(desired={desired_side.value}, actual={actual_side.value}); "
                f"no auto-flip in Phase 2",
                False,
            )

        # Same side. Compare sizes.
        if actual_size < desired_size:
            action = (
                DeltaAction.INCREASE_LONG
                if desired_side == Side.LONG
                else DeltaAction.INCREASE_SHORT
            )
            diff = desired_size - actual_size
            return (
                action,
                diff,
                f"venue size {actual_size} < target {desired_size}; increase by {diff}",
                True,
            )

        if actual_size > desired_size:
            action = (
                DeltaAction.REDUCE_LONG
                if desired_side == Side.LONG
                else DeltaAction.REDUCE_SHORT
            )
            diff = actual_size - desired_size
            return (
                action,
                diff,
                f"venue size {actual_size} > target {desired_size}; reduce by {diff}",
                True,
            )

        # Equal.
        cycle_note = (
            "; MT4 cycle changed" if cycle_changed else ""
        )
        return (
            DeltaAction.NONE,
            Decimal("0"),
            f"venue size matches target exactly{cycle_note}",
            True,
        )


# ---------------------------------------------------------------------------
# Sanitized table rendering
# ---------------------------------------------------------------------------


def render_table(results: List[ReconciliationResult]) -> str:
    """Return a human-readable, sanitized summary table.

    No token, secret, or auth-scheme value is ever printed.
    """
    lines: List[str] = []
    if not results:
        return "No Fibo registrations to reconcile.\n"
    for r in results:
        lines.append("=" * 72)
        lines.append(f"Registration : {r.registration_key}")
        lines.append(f"  Exchange       : {r.exchange}")
        lines.append(f"  Account        : {r.account}")
        # Phase 2.1: show source vs exchange distinctly. For legacy
        # records, the exchange_instrument is empty and we flag the
        # row as NEEDS_INSTRUMENT_SELECTION.
        venue_token = r.exchange_instrument or "⚠ not selected"
        lines.append(
            f"  MT4 symbol     : {r.source_symbol}  "
            f"-> venue: {venue_token}"
        )
        lines.append(f"  Variant / Side : {r.variant} / {r.side}")
        lines.append(f"  Starting vol   : {r.starting_volume}")
        lines.append(
            f"  MT4            : source={r.mt4_source or '<none>'}  "
            f"seq={r.mt4_seq}  cycle_id={r.mt4_cycle_id}  "
            f"weight={r.mt4_weight}  pct={r.mt4_percentage}  "
            f"active={r.mt4_active}"
        )
        if r.mt4_age_seconds is not None:
            lines.append(
                f"  MT4 age        : {r.mt4_age_seconds:.2f}s"
            )
        if r.cycle_changed:
            lines.append(
                f"  Cycle change   : {r.previous_cycle_id} -> {r.mt4_cycle_id}"
            )
        lines.append(
            f"  Desired        : {r.desired_side} {r.desired_size}"
        )
        lines.append(
            f"  Actual         : {r.actual_side} {r.actual_size}  "
            f"entry={r.actual_entry_price or '-'}"
        )
        lines.append(
            f"  Delta          : {r.delta_action} {r.delta_size}"
        )
        lines.append(
            f"  Safe to exec   : {r.safe_to_execute_later}"
        )
        if r.reason:
            lines.append(f"  Reason         : {r.reason}")
    lines.append("=" * 72)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI (--registration <key> is optional; no daemon, no service)
# ---------------------------------------------------------------------------


def _build_default_reconciler(
    hermes_home: Path,
) -> FiboReconciler:
    """Construct a reconciler using the live TradeDesk path."""
    from .snapshot import Mt4SnapshotStore as _Snap
    from .store import FiboRegistrationStore as _Reg
    # Import lazily so the reconciler can be imported in test
    # environments without requiring the full TradeDesk stack.
    from plugins.trade.tradedesk import get_tradedesk

    snap_path = Path(hermes_home) / "fibo" / "mt4_snapshot.json"
    reg_path = Path(hermes_home) / "fibo" / "registrations.jsonl"
    desk = get_tradedesk()
    return FiboReconciler(
        registration_store=_Reg(reg_path),
        snapshot_store=_Snap(snap_path),
        execute_fn=desk.execute,
    )


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(
        prog="plugins.trade.fibo.reconciler",
        description=(
            "Read-only Fibo reconciler. Compares persisted Fibo "
            "registrations against live MT4 + exchange state and "
            "prints the delta that WOULD be required. NEVER executes."
        ),
    )
    parser.add_argument(
        "--registration",
        default=None,
        help="Reconcile only this registration_key (default: all).",
    )
    parser.add_argument(
        "--hermes-home",
        default=os.environ.get("HERMES_HOME", "/root/.hermes"),
        help="Path to Hermes home (default: $HERMES_HOME or /root/.hermes).",
    )
    args = parser.parse_args(argv)

    try:
        reconciler = _build_default_reconciler(Path(args.hermes_home))
    except Exception as exc:  # noqa: BLE001
        print(f"fibo_reconciler: failed to initialize: {exc}", file=sys.stderr)
        return 2

    results = reconciler.reconcile_all()
    if args.registration:
        results = [r for r in results if r.registration_key == args.registration]
        if not results:
            print(
                f"No registration matched: {args.registration!r}",
                file=sys.stderr,
            )
            return 1

    print(render_table(results))
    return 0


__all__ = [
    "STALE_MT4_THRESHOLD_SECONDS",
    "DeltaAction",
    "Side",
    "ReconciliationResult",
    "FiboReconciler",
    "render_table",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(main(sys.argv[1:]))