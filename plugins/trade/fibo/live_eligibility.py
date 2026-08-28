"""Phase 2.13.12 — Dynamic live eligibility for Fibo convergence.

Replaces the Phase 2.10 single hard-coded allowlist with a set of explicit
gates that decide whether a *persisted active* registration may reach the
``live_converge`` execution path.

Authorization model
-------------------

The canonical persisted Fibo registration store (``registrations.jsonl``) is
the authority boundary for the live path. A raw MT4 snapshot entry is NEVER
sufficient authorization to trade. A registration may reach
``live_converge`` only when ALL applicable gates pass.

Public API
----------

  - ``BlockReason`` — enum of explicit reasons a registration is blocked.
  - ``evaluate(reg, snap, ...)`` — returns ``LiveEligibility`` with
    ``eligible: bool``, ``reason_code: str``, ``reason: str``.

The ``reason_code`` is suitable for inclusion in the live-eligibility
report and is one of ``LIVE_ELIGIBLE`` or a ``BLOCKED_*`` constant.

Fail-closed semantics
----------------------

Every gate either accepts or fails CLOSED. We do NOT silently treat
unrecognized values as "okay" — they are blocked with an explicit reason.
The Phase 2.11 fail-closed contract is preserved: any failure returns
``eligible=False`` with an explicit reason rather than allowing a write.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from plugins.trade.fibo.executor import (
    SIDE_BUY, SIDE_SELL, _fibo_to_venue_side, _reg_mt4_side,
)
from plugins.trade.fibo.snapshot import Mt4Snapshot
from plugins.trade.fibo.store import FiboRegistrationStore


# -------------------------------------------------------------------
# Block reasons
# -------------------------------------------------------------------


class BlockReason(str, enum.Enum):
    """Explicit block reasons returned by ``evaluate``."""

    LIVE_ELIGIBLE                       = "LIVE_ELIGIBLE"
    BLOCKED_STALE_SNAPSHOT               = "BLOCKED_STALE_SNAPSHOT"
    BLOCKED_UNRESOLVED_INSTRUMENT        = "BLOCKED_UNRESOLVED_INSTRUMENT"
    BLOCKED_MISSING_SNAPSHOT             = "BLOCKED_MISSING_SNAPSHOT"
    BLOCKED_MISSING_CYCLE                = "BLOCKED_MISSING_CYCLE"
    BLOCKED_INVALID_TARGET               = "BLOCKED_INVALID_TARGET"
    BLOCKED_UNSUPPORTED_EXCHANGE         = "BLOCKED_UNSUPPORTED_EXCHANGE"
    BLOCKED_INVALID_ACCOUNT              = "BLOCKED_INVALID_ACCOUNT"
    BLOCKED_NOT_ACTIVE                   = "BLOCKED_NOT_ACTIVE"
    BLOCKED_VARIANT                      = "BLOCKED_VARIANT"
    BLOCKED_SIDE                         = "BLOCKED_SIDE"
    BLOCKED_INVALID_WEIGHT               = "BLOCKED_INVALID_WEIGHT"
    BLOCKED_INVALID_STARTING_VOLUME      = "BLOCKED_INVALID_STARTING_VOLUME"
    BLOCKED_SNAPSHOT_FIBO_MISMATCH       = "BLOCKED_SNAPSHOT_FIBO_MISMATCH"
    BLOCKED_INVALID_REGISTRATION          = "BLOCKED_INVALID_REGISTRATION"
    BLOCKED_PROVENANCE                   = "BLOCKED_PROVENANCE"
    BLOCKED_SOURCE_MISMATCH              = "BLOCKED_SOURCE_MISMATCH"


# -------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------


# Variant allowlist (per spec gate 7). NORMALFIB and FASTFIB are the
# only supported Fibo variants in the current production deployment.
SUPPORTED_VARIANTS = frozenset({"NORMALFIB", "FASTFIB"})

# Side allowlist (per spec gate 8). BUY or SELL.
SUPPORTED_SIDES = frozenset({SIDE_BUY, SIDE_SELL})

# Default MT4 freshness threshold (Phase 2.11). Reused, not redefined.
MT4_DEFAULT_MAX_AGE_SECONDS = 30


# -------------------------------------------------------------------
# Result dataclass
# -------------------------------------------------------------------


@dataclass(frozen=True)
class LiveEligibility:
    """Outcome of evaluating one registration against all live gates."""

    eligible: bool
    reason_code: BlockReason
    reason: str
    # Optional structured detail for callers / reports.
    registration_key: str = ""
    gate: str = ""

    @property
    def is_eligible(self) -> bool:
        return self.eligible


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _is_supported_exchange(name: str, supported: frozenset) -> bool:
    return (name or "").strip().lower() in {e.lower() for e in supported}


def _valid_decimal(value: object) -> Optional[Decimal]:
    """Return a Decimal if value is finite/non-negative, else None.

    Negative, NaN, Inf, non-numeric strings → None. Zero is allowed.
    """
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not d.is_finite():
        return None
    if d < 0:
        return None
    return d


def _is_finite_positive_decimal(value: object) -> bool:
    """True iff value is a finite, strictly positive decimal."""
    d = _valid_decimal(value)
    return d is not None and d > 0


# -------------------------------------------------------------------
# Main API
# -------------------------------------------------------------------


def evaluate(
    reg,
    snap: Optional[Mt4Snapshot],
    *,
    supported_exchanges: frozenset,
    supported_variants: frozenset = SUPPORTED_VARIANTS,
    supported_sides: frozenset = SUPPORTED_SIDES,
    mt4_max_age_seconds: float = MT4_DEFAULT_MAX_AGE_SECONDS,
    store: Optional[FiboRegistrationStore] = None,
    validate_accounts_fn: Optional[Any] = None,
) -> LiveEligibility:
    """Evaluate one registration against all live gates.

    The caller MUST be the canonical convergence iteration that reads
    from the persisted store. We do not auto-register or auto-discover
    MT4 entries.

    Parameters
    ----------
    reg : FiboRegistration
        The candidate registration. MUST be the latest state loaded from
        the canonical store.
    snap : Mt4Snapshot | None
        The current MT4 snapshot, already loaded by the caller.
    supported_exchanges : frozenset
        The set of exchanges the current Fibo/trade adapter layer
        supports. Required (no default).
    supported_variants : frozenset
        Optional override for supported variants (defaults to
        ``{NORMALFIB, FASTFIB}``).
    supported_sides : frozenset
        Optional override for supported sides (defaults to ``{buy, sell}``).
    mt4_max_age_seconds : float
        The freshness threshold. Defaults to ``30`` seconds (Phase 2.11).
    store : FiboRegistrationStore | None
        Optional store reference. When provided, gates 1-3 verify the
        registration is the canonical latest state for its key
        (i.e., the latest row in the JSONL is exactly this registration).
    validate_accounts_fn : callable | None
        Optional zero-arg-side-effect account validation function.
        Must accept ``exchange: str`` and return ``List[str]`` of
        configured account identifiers for that exchange (case
        preserved). The default is ``None`` which means "caller
        did not provide an account validator" — in that case gate 5
        returns ``BLOCKED_INVALID_ACCOUNT`` (fail-closed). The
        production caller is expected to pass a function that wraps
        ``TradeDesk.list_accounts``.

    Returns
    -------
    LiveEligibility
        ``eligible=True`` only when ALL gates pass.
    """
    key = getattr(reg, "registration_key", "") or ""

    exchange = (getattr(reg, "exchange", "") or "").strip()
    account = (getattr(reg, "account", "") or "").strip()
    source_symbol = (getattr(reg, "source_symbol", "") or
                     getattr(reg, "symbol", "") or "").strip()
    exchange_instrument = (getattr(reg, "exchange_instrument", "") or
                           "").strip()
    variant = (getattr(reg, "variant", "") or "").strip().upper()
    side = (getattr(reg, "side", "") or "").strip().upper()

    # The most specific block reason wins. We test each
    # identity-defining field individually so the operator
    # sees a precise reason rather than a generic
    # BLOCKED_INVALID_REGISTRATION.

    # --- exchange_instrument (gate 6) — the most specific
    # "unresolved instrument" reason. Tested BEFORE generic
    # identity so BLOCKED_UNRESOLVED_INSTRUMENT wins for the
    # common "instrument not selected" case.
    if not exchange_instrument:
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_UNRESOLVED_INSTRUMENT,
            reason=("exchange_instrument is empty / not resolved; "
                    "the venue contract has not been selected"),
            registration_key=key, gate="exchange_instrument",
        )

    # --- exchange / account / source_symbol / variant / side
    # completeness (gate 17). Tested in order so the first
    # missing field is reported specifically.
    for fname, fname_value, br in [
        ("exchange", exchange, BlockReason.BLOCKED_INVALID_REGISTRATION),
        ("account", account, BlockReason.BLOCKED_INVALID_ACCOUNT),
        ("source_symbol", source_symbol,
         BlockReason.BLOCKED_INVALID_REGISTRATION),
        ("variant", variant, BlockReason.BLOCKED_INVALID_REGISTRATION),
        ("side", side, BlockReason.BLOCKED_INVALID_REGISTRATION),
    ]:
        if not fname_value:
            return LiveEligibility(
                eligible=False,
                reason_code=br,
                reason=(f"registration field {fname!r} is empty; "
                        "the registration is not internally complete"),
                registration_key=key, gate=f"identity_{fname}",
            )

    # ----------------------------------------------------------------
    # Gate 4: exchange is supported by the current adapter layer.
    # ----------------------------------------------------------------
    if not _is_supported_exchange(exchange, supported_exchanges):
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_UNSUPPORTED_EXCHANGE,
            reason=(f"exchange={exchange!r} is not in the current "
                    f"supported exchanges: "
                    f"{sorted(supported_exchanges)}"),
            registration_key=key, gate="supported_exchange",
        )

    # ----------------------------------------------------------------
    # Gate 5: account validity is delegated to the canonical
    # account-resolution mechanism. We use the SAME mechanism
    # the Start-Fibo flow already uses (``TradeDesk.list_accounts``)
    # — no second registry is invented. Account validation is
    # a local read; ``list_accounts`` reads from local config /
    # env, not the exchange. The caller must pass
    # ``validate_accounts_fn(exchange)`` which wraps
    # ``TradeDesk.list_accounts``. If the caller did not provide
    # one, we FAIL CLOSED (BLOCKED_INVALID_ACCOUNT) — we do NOT
    # silently equate "present in JSONL" with "valid account".
    # ----------------------------------------------------------------
    if validate_accounts_fn is None:
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_INVALID_ACCOUNT,
            reason=("validate_accounts_fn was not provided; refusing "
                    "to authorize without canonical account validation"),
            registration_key=key, gate="account",
        )
    try:
        configured_accounts = validate_accounts_fn(exchange)
    except Exception as exc:  # noqa: BLE001
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_INVALID_ACCOUNT,
            reason=(f"account validation raised: {exc}"),
            registration_key=key, gate="account_validator",
        )
    if not isinstance(configured_accounts, (list, tuple, set, frozenset)):
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_INVALID_ACCOUNT,
            reason=("account validator returned a non-list result; "
                    f"got {type(configured_accounts).__name__}"),
            registration_key=key, gate="account_validator_type",
        )
    configured_set = {str(a).strip().upper()
                      for a in configured_accounts
                      if str(a).strip()}
    if str(account or "").strip().upper() not in configured_set:
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_INVALID_ACCOUNT,
            reason=(f"account={account!r} is not configured for "
                    f"exchange={exchange!r}; configured={sorted(configured_set)}"),
            registration_key=key, gate="account_match",
        )

    # ----------------------------------------------------------------
    # Gate 7: variant.
    # ----------------------------------------------------------------
    if variant not in supported_variants:
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_VARIANT,
            reason=(f"variant={variant!r} not in supported_variants="
                    f"{sorted(supported_variants)}"),
            registration_key=key, gate="variant",
        )

    # ----------------------------------------------------------------
    # Gate 8: side.
    # ----------------------------------------------------------------
    if side.lower() not in {s.lower() for s in supported_sides}:
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_SIDE,
            reason=(f"side={side!r} not in supported_sides="
                    f"{sorted(supported_sides)}"),
            registration_key=key, gate="side",
        )

    # ----------------------------------------------------------------
    # Gates 1-3: latest active state in canonical store.
    # ----------------------------------------------------------------
    if not bool(getattr(reg, "is_active", False)):
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_NOT_ACTIVE,
            reason=(f"status={getattr(reg, 'status', None)!r} "
                    f"is not active; cannot be live-eligible"),
            registration_key=key, gate="active",
        )

    # ----------------------------------------------------------------
    # Gate 1a: source identity.
    #
    # Defense-in-depth: even though the MT4 reader enforces a
    # single-active-source invariant upstream, the live-trading
    # authorization decision MUST independently verify that the
    # persisted registration's MT4 source matches the current
    # snapshot's MT4 source. A difference means the registration
    # was created under a different observer; without this check a
    # reconstructed Registration object with a stale source could
    # authorize against a snapshot published under a new source.
    #
    # Both registration.source and snapshot.source must be
    # non-empty (fail closed on empty) and must compare equal.
    # ----------------------------------------------------------------
    reg_source = (getattr(reg, "source", "") or "").strip()
    snap_source = (
        (getattr(snap, "source", "") or "").strip()
        if snap is not None else ""
    )
    if not reg_source:
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_SOURCE_MISMATCH,
            reason=("registration.source is empty; cannot authorize "
                    "without a known observer source"),
            registration_key=key, gate="source_identity_reg",
        )
    if not snap_source:
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_SOURCE_MISMATCH,
            reason=("snapshot.source is empty; cannot authorize "
                    "without a current observer source"),
            registration_key=key, gate="source_identity_snap",
        )
    if reg_source != snap_source:
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_SOURCE_MISMATCH,
            reason=(f"registration.source={reg_source!r} does not "
                    f"match snapshot.source={snap_source!r}; "
                    f"the registration was created under a different "
                    f"observer and must be re-registered before live "
                    f"trading"),
            registration_key=key, gate="source_identity",
        )

    if store is not None:
        # Verify that the latest persisted row for this key has
        # the SAME identity-defining fields as ``reg``. We compare
        # by VALUE (not by Python object identity ``is``) so that
        # an equivalent-but-distinct deserialized Registration is
        # recognized as the canonical row.
        try:
            latest = store.get(key)
        except Exception as exc:  # noqa: BLE001
            return LiveEligibility(
                eligible=False,
                reason_code=BlockReason.BLOCKED_INVALID_REGISTRATION,
                reason=(f"store.get({key!r}) raised: {exc}"),
                registration_key=key, gate="store.get",
            )
        if latest is None:
            return LiveEligibility(
                eligible=False,
                reason_code=BlockReason.BLOCKED_INVALID_REGISTRATION,
                reason=("no persisted row for this registration_key; "
                        "refusing to authorize without canonical "
                        "state"),
                registration_key=key, gate="canonical_latest",
            )
        # Compare by value on the identity-defining fields. A
        # deserialized copy of the same row passes this check.
        # ``source`` is AUTHORIZATION_RELEVANT (see Gate 1a).
        # Including it here closes any path where a caller could
        # bypass the canonical-row check by passing a Registration
        # that has the same identity-defining fields as the latest
        # persisted row but a different ``source`` field. The
        # upstream MT4 reader already retires non-current sources,
        # so this is defense-in-depth.
        identity_fields = (
            "registration_key", "exchange", "account",
            "exchange_instrument", "source_symbol", "variant",
            "side", "starting_volume", "status", "source",
        )
        mismatches = []
        for f in identity_fields:
            if getattr(latest, f, None) != getattr(reg, f, None):
                mismatches.append(f)
        if mismatches:
            return LiveEligibility(
                eligible=False,
                reason_code=BlockReason.BLOCKED_INVALID_REGISTRATION,
                reason=(f"candidate row does not match the canonical "
                        f"latest row in the store; mismatched fields: "
                        f"{mismatches}"),
                registration_key=key,
                gate="canonical_latest_value_mismatch",
            )

    # ----------------------------------------------------------------
    # Gate 6: instrument is resolved.
    # ----------------------------------------------------------------
    if not exchange_instrument:
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_UNRESOLVED_INSTRUMENT,
            reason="exchange_instrument is empty / not resolved",
            registration_key=key, gate="exchange_instrument",
        )

    # ----------------------------------------------------------------
    # Gate 9 + 10: a current MT4 snapshot exists and is fresh.
    # ----------------------------------------------------------------
    if snap is None:
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_MISSING_SNAPSHOT,
            reason="no MT4 snapshot available",
            registration_key=key, gate="snapshot",
        )

    # Freshness: reuse the canonical freshness check from converge_once.
    from plugins.trade.fibo.converge_once import (
        MT4_MAX_AGE_SECONDS as _canonical_max_age,
    )
    # We accept the caller's mt4_max_age_seconds if it matches the
    # canonical constant; otherwise we fall back to the canonical value.
    if mt4_max_age_seconds != _canonical_max_age:
        mt4_max_age_seconds = _canonical_max_age

    try:
        from datetime import datetime, timezone
        received = datetime.fromisoformat(
            str(snap.received_at).replace("Z", "+00:00")
        )
    except Exception as exc:  # noqa: BLE001
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_STALE_SNAPSHOT,
            reason=f"snapshot received_at unparseable: {exc}",
            registration_key=key, gate="snapshot_fresh",
        )
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - received).total_seconds()
    if age > mt4_max_age_seconds:
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_STALE_SNAPSHOT,
            reason=(f"MT4 snapshot stale (age={age:.2f}s > "
                    f"{mt4_max_age_seconds}s)"),
            registration_key=key, gate="snapshot_fresh",
        )

    # ----------------------------------------------------------------
    # Gate 11: source_symbol + variant matches exactly one MT4 fibo.
    # ----------------------------------------------------------------
    fibo = snap.find_fibo(source_symbol, variant)
    if fibo is None:
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_SNAPSHOT_FIBO_MISMATCH,
            reason=(f"snapshot has no fibo for "
                    f"symbol={source_symbol!r} variant={variant!r}; "
                    f"registration identity does not match the live "
                    f"snapshot"),
            registration_key=key, gate="snapshot_match",
        )

    # ----------------------------------------------------------------
    # Gate 12: selected-side cycle_id validity. A cycle_id of 0
    # means the side is INACTIVE (the MT4 observer / Fibo
    # model uses 0 for "no current cycle for this side").
    # This is the legitimate "side inactive" path. Negative
    # cycle_ids are impossible (MT4 cycle ids are non-negative
    # integers) and would indicate a data corruption — block.
    # The weight-consistency check below (gate 13) ensures the
    # cycle_id and weight are consistent (both 0 or both > 0).
    # ----------------------------------------------------------------
    cycle_id_raw = fibo.side_cycle_id(side)
    if not isinstance(cycle_id_raw, int):
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_MISSING_CYCLE,
            reason=(f"cycle_id for side={side!r} is not an int: "
                    f"{cycle_id_raw!r}"),
            registration_key=key, gate="cycle_id_type",
        )
    if cycle_id_raw < 0:
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_MISSING_CYCLE,
            reason=(f"cycle_id for side={side!r} is negative: "
                    f"{cycle_id_raw}"),
            registration_key=key, gate="cycle_id_negative",
        )
    cycle_id = cycle_id_raw

    weight_raw = fibo.side_cumulative_weight(side)
    weight = _valid_decimal(weight_raw)
    if weight is None:
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_INVALID_WEIGHT,
            reason=(f"invalid cumulative weight for side={side!r}: "
                    f"{weight_raw!r}"),
            registration_key=key, gate="weight",
        )
    if (weight <= 0) != (cycle_id <= 0):
        # Inconsistent MT4 state for the selected side.
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_INVALID_WEIGHT,
            reason=(f"inconsistent MT4 state for side={side!r}: "
                    f"weight={weight}, cycle_id={cycle_id}. "
                    f"Either both are 0 (side inactive) or both are "
                    f"> 0 (side active). Mixed state is not allowed."),
            registration_key=key, gate="weight_cycle_consistency",
        )

    starting_volume = _valid_decimal(getattr(reg, "starting_volume", None))
    if starting_volume is None or starting_volume <= 0:
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_INVALID_STARTING_VOLUME,
            reason=(f"invalid starting_volume: "
                    f"{getattr(reg, 'starting_volume', None)!r}"),
            registration_key=key, gate="starting_volume",
        )

    # ----------------------------------------------------------------
    # Gate 16: calculated target.
    # ----------------------------------------------------------------
    target = starting_volume * weight
    if not target.is_finite() or target < 0:
        return LiveEligibility(
            eligible=False,
            reason_code=BlockReason.BLOCKED_INVALID_TARGET,
            reason=(f"invalid calculated target: "
                    f"starting_volume={starting_volume} * weight={weight} = "
                    f"{target}"),
            registration_key=key, gate="target",
        )

    # ----------------------------------------------------------------
    # Gate 19: existing approved Fibo operation surface — enforced at
    # executor level. We pass through.
    # ----------------------------------------------------------------

    return LiveEligibility(
        eligible=True,
        reason_code=BlockReason.LIVE_ELIGIBLE,
        reason="all gates passed",
        registration_key=key,
        gate="all_passed",
    )


__all__ = [
    "BlockReason",
    "LiveEligibility",
    "evaluate",
    "SUPPORTED_VARIANTS",
    "SUPPORTED_SIDES",
    "MT4_DEFAULT_MAX_AGE_SECONDS",
]
