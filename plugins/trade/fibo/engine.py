"""Fibo v1 engine — cumulative-counter protection model (LOCKED).

ARCHITECTURE (per the locked spec):

  One Fibo registration = exchange + account + instrument + counterType

  counterType:
    - counterBUY  : runs ONLY the virtual BUY-side Fibonacci cascade.
                    When a counter level activates, sends a REAL BUY market
                    order into the (exchange, account, instrument).
    - counterSELL : runs ONLY the virtual SELL-side Fibonacci cascade.
                    When a counter level activates, sends a REAL SELL market
                    order into the (exchange, account, instrument).

  Both virtual cascades never run inside one instance. The engine holds
  exactly one CascadeState per FiboInstance, selected by CounterType.

COUNTERS ARE CUMULATIVE, NOT INDEPENDENT

  Each registration contributes to ONE cumulative directional exchange
  position. Counters 1..4 add their configured volume (if > 0) and the
  protective record advances as a single rolling SL with TP only at
  Counter4:

    Counter1 activated: optional C1 addition; SL = Step0;  NO TP
    Counter2 activated: optional C2 addition; SL = Step1;  NO TP
    Counter3 activated: optional C3 addition; SL = Step2;  NO TP
    Counter4 activated: optional C4 addition; SL = Step3;  TP = Step5

  Counter volume = 0 means no market order at that level. The level
  STILL activates; SL still progresses; (at Counter4) TP is still
  installed. This is intentional — see spec sections 3, 8, 10.

  All-volume-zero edge case: cascade advances normally but the engine
  makes zero exchange mutations. There is no Fibo position to protect.

POSITION-LEVEL PROTECTION MODEL

  OndoPerps — and likely most DEXes — expose one TP + one SL per
  (account, instrument, direction) net position. The locked model
  intentionally matches this: ONE cumulative Fibo position per
  registration, ONE rolling SL, ONE TP installed at Counter4.

  IMPORTANT POSITION-SCOPING LIMITATION (reported separately):
  OndoPerps' protective record is per (account, instrument, direction)
  with NO client-order-id tagging. If the user (or /trade) already has a
  MANUAL position in the same (account, instrument) on the same side,
  Fibo's protective record will protect the entire net position —
  including the manual volume — not just Fibo's volume. See the
  ``OndoPerpsFiboAdapter`` docstring and the Phase 2 report for the
  full disclosure.

ERROR / UNPROTECTED FREEZE

  If the engine submits a market order successfully but a required
  protection update (SL or Counter4 TP) fails, the registration is
  marked ERROR / UNPROTECTED and frozen. No further cascade processing,
  no further exchange mutations. The existing position and its existing
  protection remain untouched.

USER STOP

  ``FiboManager.stop(key)`` removes the registration from the active
  registry. STOP MUST NOT cancel orders, close positions, remove TP/SL,
  or modify any exchange state. STOP MUST NOT call any adapter method.

STRATEGY RESET / KILL / RECOVERY (see open-questions report)

  Recovery and kill-cycle inside ``on_quote`` continue to call
  ``adapter.cleanup_counters`` for now — the same call as before. With
  the new cumulative model this is no longer "close Fibo counter
  positions" (they don't exist as separate positions). The cleanup hook
  is now an INTENTIONAL NO-OP in the OndoPerps adapter (documented there).
  Whether recovery/kill should do anything is an open question flagged
  in the report.

This module has NO Telegram coupling, NO exchange-specific code, and NO
networking. Real-order execution is delegated to ``ExchangeAdapter``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from .quote import Quote, QuoteSource


# ---------------------------------------------------------------------
# Fibonacci constants — fixed strategy behavior, do NOT change.
# ---------------------------------------------------------------------

FIB_TABLE: List[int] = [
    1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 233, 377, 610, 987,
    1597, 2584, 4181, 6765, 10946, 17711, 28657, 46368, 75025,
    121393, 196418, 317811, 514229, 832040, 1346269, 2178309,
    3524578, 5702887, 9227465, 14930352, 24157817, 39088169,
    63245986, 102334155,
]

FIB_START_VALUE: int = 21
FIB_START_INDEX: int = FIB_TABLE.index(FIB_START_VALUE)
KILL_CYCLE_STEP: int = 5

DEFAULT_DIVIDE_PERCENT: float = 100.0
DEFAULT_COUNTER_1: float = 1.3
DEFAULT_COUNTER_2: float = 0.8
DEFAULT_COUNTER_3: float = 0.5
DEFAULT_COUNTER_4: float = 0.3

NUM_COUNTERS: int = 4

EventSink = Callable[[Dict[str, Any]], None]


class CounterType(str, Enum):
    """Which virtual cascade runs inside a Fibo instance.

    counterBUY  → runs the BUY  cascade, sends REAL BUY market counters.
    counterSELL → runs the SELL cascade, sends REAL SELL market counters.
    """

    COUNTER_BUY = "counterBUY"
    COUNTER_SELL = "counterSELL"


class RealOrderSide(str, Enum):
    """Which side of the book the REAL counter market order goes on."""

    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class FiboConfig:
    """Configuration for one Fibo registration.

    ``key`` is computed from (exchange, account, instrument, counterType)
    and is the unique identifier for a registration. There is no separate
    instance ID — the key IS the identity.
    """

    exchange: str
    account: str
    instrument: str
    counter_type: CounterType

    divide_percent: float = DEFAULT_DIVIDE_PERCENT

    counter1: float = DEFAULT_COUNTER_1
    counter2: float = DEFAULT_COUNTER_2
    counter3: float = DEFAULT_COUNTER_3
    counter4: float = DEFAULT_COUNTER_4

    def __post_init__(self) -> None:
        # Coerce string counter_type to enum if needed.
        if isinstance(self.counter_type, str):
            object.__setattr__(self, "counter_type", CounterType(self.counter_type))
        self.validate()

    def validate(self) -> None:
        if not self.exchange or not self.exchange.strip():
            raise ValueError("exchange is required")
        if not self.account or not self.account.strip():
            raise ValueError("account is required")
        if not self.instrument or not self.instrument.strip():
            raise ValueError("instrument is required")
        if not isinstance(self.counter_type, CounterType):
            raise ValueError(
                f"counter_type must be a CounterType, got {self.counter_type!r}"
            )
        if self.divide_percent <= 0:
            raise ValueError("divide_percent must be > 0")
        for name, value in (
            ("counter1", self.counter1),
            ("counter2", self.counter2),
            ("counter3", self.counter3),
            ("counter4", self.counter4),
        ):
            if value < 0:
                raise ValueError(f"{name} must be >= 0")

    @property
    def key(self) -> str:
        return (
            f"{self.exchange}:{self.account}:{self.instrument}"
            f":{self.counter_type.value}"
        )

    def counter_volume(self, step: int) -> float:
        return {
            1: self.counter1,
            2: self.counter2,
            3: self.counter3,
            4: self.counter4,
        }.get(step, 0.0)


# ---------------------------------------------------------------------
# Protection state — what the engine knows about the exchange position
# ---------------------------------------------------------------------


@dataclass
class ProtectionState:
    """The engine's view of the protective record on the exchange.

    Mirrors what the adapter has most recently confirmed via the
    ``current_protection_state`` adapter call (or, when not available,
    what the engine has most recently installed itself).

    All price fields are ``None`` when the leg does not exist. Fibo
    installs SL progressively across Counter1..Counter4. Fibo installs
    TP only at Counter4 and never updates it afterwards.
    """

    sl_price: Optional[float] = None
    tp_price: Optional[float] = None


@dataclass
class FiboInstance:
    """One Fibo registration.

    State held per registration (see locked spec section 18):
      - cumulative_volume: total requested Fibo volume across all activated
        counters (sum of counter_volume(N) for every activated N where
        counter_volume(N) > 0). This is the engine's view of the Fibo
        share of the net directional position.
      - activated_levels: set of counter levels (1..4) that have already
        been processed by this registration. Used for idempotency on
        gap-crossing ticks.
      - protection: the engine's last-known view of the protective
        record on the exchange (SL + TP).

    Lifecycle flags:
      - running: False after STOP.
      - frozen: True after an UNPROTECTED-COUNTER error. The engine
        halts strategy processing for THIS registration only.
    """

    config: FiboConfig
    running: bool = True
    cascade: CascadeState = field(init=False)
    protection: ProtectionState = field(init=False)
    cumulative_volume: Decimal = field(default_factory=lambda: Decimal("0"))

    def __post_init__(self) -> None:
        self.config.validate()
        self.cascade = CascadeState(
            cascade_side_text=self._cascade_side_text(),
        )
        self.protection = ProtectionState()
        self._activated_levels: set[int] = set()
        self._pending_unprotected: Dict[int, str] = {}
        self.frozen: bool = False
        self.frozen_reason: str = ""
        self.last_protection_error: Optional[Tuple[str, str]] = None

    def _cascade_side_text(self) -> str:
        """The VIRTUAL cascade direction. Informational only.

        counterSELL → virtual SELL cascade (which goes downward).
        counterBUY  → virtual BUY  cascade (which goes upward).
        """
        return (
            "BUY" if self.counter_type_is_buy() else "SELL"
        )

    def counter_type_is_buy(self) -> bool:
        return self.config.counter_type == CounterType.COUNTER_BUY

    @property
    def key(self) -> str:
        return self.config.key

    # --- internal helpers used by FiboEngine -----------------------------

    def mark_activated(self, level: int) -> None:
        self._activated_levels.add(int(level))

    def is_activated(self, level: int) -> bool:
        return int(level) in self._activated_levels

    def reset_activations(self) -> None:
        """Drop the per-level activated markers.

        Called on cascade reset (recovery / kill-cycle) so a NEW cycle
        can re-activate Counter1..Counter4 with fresh market additions
        and progressive SL/TP.
        """
        self._activated_levels.clear()

    def mark_unprotected(self, level: int, reason: str) -> None:
        self._pending_unprotected[int(level)] = str(reason)

    def clear_unprotected(self, level: int) -> None:
        self._pending_unprotected.pop(int(level), None)

    @property
    def pending_unprotected(self) -> Dict[int, str]:
        return dict(self._pending_unprotected)

    def freeze(self, reason: str) -> None:
        """Halt strategy processing for this registration.

        Per the locked spec section 13 — when an unprotected-counter
        error is observed, this EXACT registration is marked
        ERROR/UNPROTECTED and frozen. The freeze stops:

          - processing new virtual levels for this registration
          - sending any further Fibo counter orders for this registration

        STOP, recovery, kill-cycle cleanup, and exchange-side mutations
        are NOT triggered by a freeze. The user must explicitly STOP this
        registration to clear the freeze and start fresh.
        """
        if not self.frozen:
            self.frozen = True
            self.frozen_reason = str(reason)

    def unfreeze(self) -> None:
        """Clear the frozen state. Used only by ``FiboManager.stop()``.

        Importantly, ``unfreeze`` does NOT clear ``activated_levels`` or
        ``protection``. STOP stops the registration; it does not restart
        it. A subsequent ``manager.start(...)`` with the same key creates
        a fresh ``FiboInstance`` whose state is empty.
        """
        self.frozen = False
        self.frozen_reason = ""
        self.last_protection_error = None


@dataclass
class CascadeState:
    """Virtual Fibonacci cascade state for one FiboInstance.

    ``highest_step`` is -1 when no STEP0 has been seeded yet (fresh quote).
    ``step0_price`` is the price at which STEP0 was set: ask for the SELL
    cascade, bid for the BUY cascade.
    """

    cascade_side_text: str  # "BUY" or "SELL" — informational only.
    step0_price: float = 0.0
    highest_step: int = -1  # -1 = inactive, 0 = STEP0 active, 1..4 = advanced.

    @property
    def active(self) -> bool:
        return self.highest_step >= 0

    def reset(self) -> None:
        self.step0_price = 0.0
        self.highest_step = -1


class UnprotectedCounterError(RuntimeError):
    """Raised by ``FiboEngine`` when a cumulative-protection operation fails.

    Carries the counter level and a reason code that callers (the
    manager, future wizards) can render to the operator. ``operation``
    is one of ``"submit"``, ``"confirm"``, ``"sl_set"``, ``"sl_verify"``,
    ``"tp_set"``, ``"tp_verify"``.
    """

    def __init__(
        self, level: int, reason_code: str, detail: str = "",
        operation: str = "",
        context: Optional[Dict[str, Any]] = None,
    ):
        self.level = int(level)
        self.reason_code = str(reason_code)
        self.detail = str(detail)
        self.operation = str(operation)
        self.context = dict(context or {})
        super().__init__(
            f"unprotected counter at level {self.level}: "
            f"{self.reason_code}"
            + (f" [{self.operation}]" if self.operation else "")
            + (f" ({self.detail})" if self.detail else "")
        )


class ExchangeAdapter(Protocol):
    """Minimal exchange-facing API required by Fibo.

    The engine itself contains no exchange-specific code. Implementations
    identify Fibo counters internally — OndoPerps supports
    ``clientOrderId`` on ``/v1/perps/orders``, but not on the protective
    ``/v1/perps/stop_order`` path.

    All methods are part of the locked cumulative-position model:

      - ``submit_volume_market_order``  → opens or increases the
        directional position by ``volume``. MUST NOT be reduce-only.
      - ``confirm_cumulative_position`` → verifies the (instrument, side)
        row exists with size > 0.
      - ``set_cumulative_sl``             → installs/replaces the SL at
        ``sl_price`` on the (instrument, side) position.
      - ``verify_cumulative_sl``          → re-reads and confirms the SL
        is at the expected price.
      - ``set_cumulative_tp``             → installs/replaces the TP at
        ``tp_price`` on the (instrument, side) position.
      - ``verify_cumulative_tp``          → re-reads and confirms the TP
        is at the expected price.
      - ``current_protection_state``      → returns ``(sl_price, tp_price)``
        for diagnostics + cross-cycle reset handling.

    Optional:
      - ``remove_cumulative_tp``          → removes ONLY the TP. Used by
        recovery/kill-cycle hooks when we want to clear a stale Fibo TP
        before starting a new cycle. The default implementation is a
        no-op; concrete adapters override when safe.
      - ``cleanup_counters``               → strategy-driven cleanup hook
        (recovery / kill-cycle). May be a no-op for adapters whose
        exchange model has no per-counter state to clean up.
    """

    def submit_volume_market_order(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
        counter_step: int,
        volume: float,
    ) -> str:
        """Submit a REAL non-reduce-only MARKET counter volume.

        Returns an opaque ``exchange_order_id`` (string). Raises
        ``RuntimeError`` on submission failure.
        """
        ...

    def confirm_cumulative_position(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
    ) -> bool:
        """Return True iff a position row for ``(instrument, side)`` has
        size > 0. The engine calls this AFTER any market-order addition.
        """
        ...

    def set_cumulative_sl(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
        sl_price: float,
    ) -> bool:
        """Install/replace the SL. Returns True on success."""
        ...

    def verify_cumulative_sl(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
        sl_price: float,
    ) -> bool:
        """Re-read and confirm the SL is at ``sl_price``. Returns True
        iff the SL is present at the expected price.
        """
        ...

    def set_cumulative_tp(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
        tp_price: float,
    ) -> bool:
        """Install/replace the TP. Returns True on success."""
        ...

    def verify_cumulative_tp(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
        tp_price: float,
    ) -> bool:
        """Re-read and confirm the TP is at ``tp_price``. Returns True
        iff the TP is present at the expected price.
        """
        ...

    def current_protection_state(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
    ) -> Tuple[Optional[float], Optional[float]]:
        """Return ``(sl_price, tp_price)`` currently installed on the
        position. ``None`` for legs that are absent.
        """
        ...

    # ---- optional cleanup hooks ------------------------------------------

    def remove_cumulative_tp(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
    ) -> bool:
        """Remove ONLY the TP. Default is no-op for adapters that lack
        a safe way to distinguish Fibo TP from a manual TP.
        """
        ...

    def cleanup_counters(
        self,
        *,
        instance_key: str,
        instrument: str,
    ) -> None:
        """STRATEGY-DRIVEN cleanup only.

        Called by the engine on normal virtual-ladder recovery (cascade
        has returned to step0_tp) or on kill-cycle (cascade crossed
        STEP5). For OndoPerps v1 this is intentionally a no-op — the
        exchange model has no per-counter state to clean up; the
        cumulative position is already protected by the rolling SL /
        Counter4 TP installed by Fibo.

        IMPORTANT: ``FiboManager.stop()`` MUST NEVER call this. STOP
        leaves the existing exchange state untouched.
        """
        ...


# ---------------------------------------------------------------------
# Fibonacci percent-math helpers (MQ4 reference) — UNCHANGED.
# ---------------------------------------------------------------------


def fib_distance(step_offset: int) -> int:
    idx = FIB_START_INDEX + step_offset
    if idx < 0 or idx >= len(FIB_TABLE):
        raise IndexError(
            f"FIB_TABLE exhausted at offset={step_offset}, idx={idx}"
        )
    return FIB_TABLE[idx]


def step_price(
    step0_price: float,
    step_n: int,
    *,
    is_buy_cascade: bool,
    divide_percent: float,
) -> float:
    """Exact percent-mode price math from the reference MQ4.

    BUY cascade: levels ascend (price increases).
    SELL cascade: levels descend (price decreases).
    """
    if step_n <= 0:
        return float(step0_price)

    price = float(step0_price)
    for i in range(1, step_n + 1):
        dist = fib_distance(i)
        move = price * (float(dist) / 100.0) / divide_percent
        price = price + move if is_buy_cascade else price - move
    return price


def step_tp(
    step0_price: float,
    step_n: int,
    *,
    is_buy_cascade: bool,
    divide_percent: float,
) -> float:
    """SL price for counter at step_n.

    Convention from the reference: SL of counter N equals
    ``step_price(step0, n - 1)``. Used by the engine as the
    progressive SL at each counter level.
    """
    if step_n < 1:
        raise ValueError("step_tp is for step_n >= 1")
    return step_price(
        step0_price,
        step_n - 1,
        is_buy_cascade=is_buy_cascade,
        divide_percent=divide_percent,
    )


def step0_tp(
    step0_price: float,
    *,
    is_buy_cascade: bool,
    divide_percent: float,
) -> float:
    """Recovery threshold: opposite-side trigger back across step0."""
    dist = fib_distance(0)
    move = step0_price * (float(dist) / 100.0) / divide_percent
    return step0_price - move if is_buy_cascade else step0_price + move


# ---------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------


@dataclass
class FiboEngine:
    """One Fibo registration's strategy logic.

    Pure exchange-neutral logic. Driven by ``on_quote``. Uses an
    ``ExchangeAdapter`` to write real orders when the cascade crosses a
    level whose counter_volume > 0. Uses a ``QuoteSource`` to obtain the
    current bid/ask for an instrument.

    The engine enforces the locked cumulative-protection model:

      * Each level activation is idempotent (``FiboInstance.is_activated``).
      * If ``counter_volume(N) > 0``, a non-reduce-only market order is
        submitted for that volume and the cumulative position is
        re-confirmed.
      * If ``cumulative_volume > 0`` after the optional addition, the
        rolling SL is set to ``step_tp(step0, N) = step_price(step0, N-1)``
        and re-verified. (No TP for N < 4.)
      * At ``N == 4``, additionally the TP is set to ``step_price(step0, 5)``
        and re-verified.
      * If any step fails, the registration is frozen
        (UnprotectedCounterError → ``FiboInstance.freeze``).
    """

    instance: FiboInstance
    adapter: ExchangeAdapter
    quote_source: QuoteSource
    event_sink: Optional[EventSink] = None

    def _emit(self, event: str, **fields: Any) -> None:
        if self.event_sink is None:
            return
        payload: Dict[str, Any] = {
            "event": event,
            "registration_key": self.instance.key,
        }
        payload.update(fields)
        self.event_sink(payload)

    def _current_state_payload(self) -> Dict[str, Any]:
        return {
            "highest_step": self.instance.cascade.highest_step,
            "step0_raw": self.instance.cascade.step0_price,
            "cumulative_volume": str(self.instance.cumulative_volume),
            "current_sl_raw": self.instance.protection.sl_price,
            "current_tp_raw": self.instance.protection.tp_price,
            "frozen": self.instance.frozen,
        }

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def on_quote(self, quote: Quote) -> None:
        if not self.instance.running:
            return

        # Frozen registrations are halted at the strategy level.
        if self.instance.frozen:
            return

        self._emit(
            "quote_received",
            bid=quote.bid,
            ask=quote.ask,
            mark_price=quote.bid if quote.bid == quote.ask else None,
            **self._current_state_payload(),
        )

        if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
            raise ValueError(
                f"invalid quote bid={quote.bid} ask={quote.ask}"
            )

        is_buy_cascade = self._is_buy_cascade()

        # Fresh virtual STEP0.
        if not self.instance.cascade.active:
            self.instance.cascade.step0_price = (
                quote.bid if is_buy_cascade else quote.ask
            )
            self.instance.cascade.highest_step = 0
            self._emit(
                "step0_seeded",
                reason="initial",
                **self._current_state_payload(),
            )
            return

        step0 = self.instance.cascade.step0_price

        try:
            self._advance_cascade(quote, is_buy_cascade, step0)

            if not self.instance.cascade.active:
                return
            self._maybe_recover(quote, is_buy_cascade, step0)
        except UnprotectedCounterError:
            # Lock the registration per the locked spec section 13.
            self.instance.freeze("UNPROTECTED_COUNTER")
            self._emit(
                "registration_frozen",
                reason=self.instance.frozen_reason,
                **self._current_state_payload(),
            )
            raise

    def _advance_cascade(
        self, quote: Quote, is_buy_cascade: bool, step0: float
    ) -> None:
        """Walk the virtual cascade forward through every crossed level."""
        guard = 0
        while guard < 8:
            guard += 1
            next_step = self.instance.cascade.highest_step + 1

            candidate = step_price(
                step0,
                next_step,
                is_buy_cascade=is_buy_cascade,
                divide_percent=self.instance.config.divide_percent,
            )
            market = quote.bid if is_buy_cascade else quote.ask
            crossed = (
                market >= candidate if is_buy_cascade else market <= candidate
            )
            if not crossed:
                break

            # STEP5 is the fixed virtual kill boundary.
            if next_step >= KILL_CYCLE_STEP:
                self._emit(
                    "cycle_cleanup",
                    reason="kill",
                    **self._current_state_payload(),
                )
                self.adapter.cleanup_counters(
                    instance_key=self.instance.key,
                    instrument=self.instance.config.instrument,
                )
                self._reset_cascade(reason="kill")
                self._seed_step0_from_quote_source(is_buy_cascade=is_buy_cascade)
                return

            self.instance.cascade.highest_step = next_step

            # Always activate the level (mandatory per spec section 10).
            # The level-activation body decides whether to add market
            # volume and how to update protection.
            self._activate_level(level=next_step, is_buy_cascade=is_buy_cascade)

    def _activate_level(self, *, level: int, is_buy_cascade: bool) -> None:
        """Activate counter level ``level``.

        Per the locked spec section 10 (general rule):

          A. Mark the virtual level as activated.
          B. If configured CounterN volume > 0:
             → send additional real market counter volume
             → confirm resulting cumulative position
          C. If cumulative Fibo position volume > 0:
             → update cumulative SL to Step(N-1)
          D. If N < 4:
             → there should be NO Fibo TP
          E. If N == 4 and cumulative Fibo position volume > 0:
             → set cumulative TP = Step5

        Idempotency: re-activation of an already-activated level is a
        no-op (no second market order, no second SL update, no second
        TP install).
        """
        if self.instance.is_activated(level):
            return  # Idempotency guard for gap-crossing.

        # Step A: mark activated BEFORE attempting exchange work so a
        # partial failure does not cause us to retry the same volume.
        self.instance.mark_activated(level)

        cfg = self.instance.config
        side = self._real_order_side()
        step0 = self.instance.cascade.step0_price
        configured_volume = cfg.counter_volume(level)
        self._emit(
            "level_activated",
            level=level,
            configured_counter_volume=float(configured_volume),
            market_volume_sent=bool(configured_volume > 0),
            **self._current_state_payload(),
        )

        # Step B: optional market volume addition.
        if configured_volume > 0:
            expected_cumulative_volume = (
                self.instance.cumulative_volume + Decimal(str(configured_volume))
            )
            try:
                order_id = self.adapter.submit_volume_market_order(
                    instance_key=self.instance.key,
                    instrument=cfg.instrument,
                    side=side,
                    counter_step=level,
                    volume=float(configured_volume),
                )
            except Exception as exc:  # noqa: BLE001
                reason_code = "MARKET_SUBMIT_FAILED"
                detail = str(exc)
                if detail.startswith("ORDER_VERIFY_FAILED"):
                    reason_code = "ORDER_VERIFY_FAILED"
                raise UnprotectedCounterError(
                    level, reason_code, detail, "submit",
                ) from exc
            self._emit(
                "market_volume_submitted",
                level=level,
                requested_volume=float(configured_volume),
                real_order_side=side.value,
                exchange_order_id=str(order_id),
                **self._current_state_payload(),
            )

            try:
                confirmed = self.adapter.confirm_cumulative_position(
                    instance_key=self.instance.key,
                    instrument=cfg.instrument,
                    side=side,
                    expected_size=float(expected_cumulative_volume),
                )
            except Exception as exc:  # noqa: BLE001
                confirm_diag = {}
                diag_getter = getattr(self.adapter, "last_confirmation_diagnostic", None)
                if callable(diag_getter):
                    try:
                        confirm_diag = dict(diag_getter(self.instance.key) or {})
                    except Exception:  # noqa: BLE001
                        confirm_diag = {}
                submit_diag = {}
                submit_getter = getattr(self.adapter, "last_submission_context", None)
                if callable(submit_getter):
                    try:
                        submit_diag = dict(submit_getter(self.instance.key) or {})
                    except Exception:  # noqa: BLE001
                        submit_diag = {}
                context = {
                    "registration_key": self.instance.key,
                    "level": level,
                    "stage": "confirm",
                    "expected_order_side": side.value,
                    "expected_position_side": "long" if side == RealOrderSide.BUY else "short",
                    "previous_cumulative_volume": str(self.instance.cumulative_volume),
                    "newly_filled_volume": str(configured_volume),
                    "expected_cumulative_volume": str(expected_cumulative_volume),
                    **submit_diag,
                    "position_confirmation": confirm_diag,
                }
                raise UnprotectedCounterError(
                    level,
                    (confirm_diag.get("reason_code") if isinstance(confirm_diag, dict) and confirm_diag.get("reason_code") else "FILL_VERIFICATION_ERROR"),
                    str(exc),
                    "confirm",
                    context=context,
                ) from exc
            if not confirmed:
                confirm_diag = {}
                diag_getter = getattr(self.adapter, "last_confirmation_diagnostic", None)
                if callable(diag_getter):
                    try:
                        confirm_diag = dict(diag_getter(self.instance.key) or {})
                    except Exception:  # noqa: BLE001
                        confirm_diag = {}
                submit_diag = {}
                submit_getter = getattr(self.adapter, "last_submission_context", None)
                if callable(submit_getter):
                    try:
                        submit_diag = dict(submit_getter(self.instance.key) or {})
                    except Exception:  # noqa: BLE001
                        submit_diag = {}
                context = {
                    "registration_key": self.instance.key,
                    "level": level,
                    "stage": "confirm",
                    "expected_order_side": side.value,
                    "expected_position_side": "long" if side == RealOrderSide.BUY else "short",
                    "previous_cumulative_volume": str(self.instance.cumulative_volume),
                    "newly_filled_volume": str(configured_volume),
                    "expected_cumulative_volume": str(expected_cumulative_volume),
                    **submit_diag,
                    "position_confirmation": confirm_diag,
                }
                raise UnprotectedCounterError(
                    level,
                    (confirm_diag.get("reason_code") if isinstance(confirm_diag, dict) and confirm_diag.get("reason_code") else "POSITION_CONFIRM_FAILED"),
                    json.dumps(context, sort_keys=True, default=str),
                    "confirm",
                    context=context,
                )

            # Cumulative volume reflects what we asked the exchange to
            # add (subject to rounding + fill acceptance by the adapter's
            # confirm step). We track the requested amount here; the
            # engine's view of "Fibo's exposure" is the sum of
            # counter_volume(N) for every activated level.
            self.instance.cumulative_volume = (
                self.instance.cumulative_volume
                + Decimal(str(configured_volume))
            )
            self._emit(
                "cumulative_position_confirmed",
                level=level,
                requested_volume=float(configured_volume),
                real_order_side=side.value,
                **self._current_state_payload(),
            )

            # The adapter may tag the market addition with a client order
            # id internally, but the engine still treats ``order_id`` as an
            # opaque exchange handle and relies on cumulative confirm +
            # progressive SL installation to keep the strategy coherent with
            # exchange state.
            del order_id  # unused after this point

        # Step C: update cumulative SL if we have any Fibo exposure.
        if self.instance.cumulative_volume > 0:
            sl_price = step_tp(
                step0, level,
                is_buy_cascade=is_buy_cascade,
                divide_percent=cfg.divide_percent,
            )
            try:
                self._emit(
                    "sl_requested",
                    level=level,
                    sl_raw=float(sl_price),
                    **self._current_state_payload(),
                )
                sl_ok = self.adapter.set_cumulative_sl(
                    instance_key=self.instance.key,
                    instrument=cfg.instrument,
                    side=side,
                    sl_price=float(sl_price),
                )
            except Exception as exc:  # noqa: BLE001
                raise UnprotectedCounterError(
                    level, "SL_SET_ERROR", str(exc), "sl_set",
                ) from exc
            if not sl_ok:
                raise UnprotectedCounterError(
                    level, "SL_SET_FAILED", "", "sl_set",
                )
            try:
                sl_verify = self.adapter.verify_cumulative_sl(
                    instance_key=self.instance.key,
                    instrument=cfg.instrument,
                    side=side,
                    sl_price=float(sl_price),
                )
            except Exception as exc:  # noqa: BLE001
                raise UnprotectedCounterError(
                    level, "SL_VERIFY_ERROR", str(exc), "sl_verify",
                ) from exc
            if not sl_verify:
                raise UnprotectedCounterError(
                    level, "SL_VERIFY_FAILED", "", "sl_verify",
                )
            self.instance.protection.sl_price = float(sl_price)
            self._emit(
                "sl_verified",
                level=level,
                sl_raw=float(sl_price),
                verified=True,
                **self._current_state_payload(),
            )

        # Step E: at Counter4 install the TP.
        if level == 4 and self.instance.cumulative_volume > 0:
            tp_price = step_price(
                step0, level + 1,
                is_buy_cascade=is_buy_cascade,
                divide_percent=cfg.divide_percent,
            )
            try:
                self._emit(
                    "tp_requested",
                    level=level,
                    tp_raw=float(tp_price),
                    **self._current_state_payload(),
                )
                tp_ok = self.adapter.set_cumulative_tp(
                    instance_key=self.instance.key,
                    instrument=cfg.instrument,
                    side=side,
                    tp_price=float(tp_price),
                )
            except Exception as exc:  # noqa: BLE001
                raise UnprotectedCounterError(
                    level, "TP_SET_ERROR", str(exc), "tp_set",
                ) from exc
            if not tp_ok:
                raise UnprotectedCounterError(
                    level, "TP_SET_FAILED", "", "tp_set",
                )
            try:
                tp_verify = self.adapter.verify_cumulative_tp(
                    instance_key=self.instance.key,
                    instrument=cfg.instrument,
                    side=side,
                    tp_price=float(tp_price),
                )
            except Exception as exc:  # noqa: BLE001
                raise UnprotectedCounterError(
                    level, "TP_VERIFY_ERROR", str(exc), "tp_verify",
                ) from exc
            if not tp_verify:
                raise UnprotectedCounterError(
                    level, "TP_VERIFY_FAILED", "", "tp_verify",
                )
            self.instance.protection.tp_price = float(tp_price)
            self._emit(
                "tp_verified",
                level=level,
                tp_raw=float(tp_price),
                verified=True,
                **self._current_state_payload(),
            )

    def _maybe_recover(
        self, quote: Quote, is_buy_cascade: bool, step0: float
    ) -> None:
        """MQ4-style recovery: when price returns across step_tp(highest)
        in the opposite direction, the virtual cascade resets.
        """
        highest = self.instance.cascade.highest_step
        recovery_level = (
            step0_tp(
                step0,
                is_buy_cascade=is_buy_cascade,
                divide_percent=self.instance.config.divide_percent,
            )
            if highest == 0
            else step_tp(
                step0,
                highest,
                is_buy_cascade=is_buy_cascade,
                divide_percent=self.instance.config.divide_percent,
            )
        )
        recovery_market = quote.bid if is_buy_cascade else quote.ask
        comparison_operator = "<=" if is_buy_cascade else ">="
        recovered = (
            recovery_market <= recovery_level
            if is_buy_cascade
            else recovery_market >= recovery_level
        )
        self._emit(
            "recovery_evaluated",
            counter_type=self.instance.config.counter_type.value,
            highest_step=highest,
            current_price_raw=recovery_market,
            step0_raw=step0,
            recovery_target_raw=recovery_level,
            recovery_target_type="step0_tp" if highest == 0 else f"step_tp({highest})",
            comparison_operator=comparison_operator,
            comparison_result=recovered,
            cumulative_volume=str(self.instance.cumulative_volume),
            activated_levels=sorted(self.instance._activated_levels),
        )
        if not recovered:
            return

        if highest == 0 and self.instance.cumulative_volume == 0:
            self._emit(
                "virtual_recovery",
                reason="pre_counter1_step0tp",
                current_price_raw=recovery_market,
                recovery_target_raw=recovery_level,
                **self._current_state_payload(),
            )
            self._reset_cascade(reason="recovery_virtual")
            self._seed_step0_from_quote(quote=quote, is_buy_cascade=is_buy_cascade, reason="fresh_cycle_virtual")
            return

        self._emit(
            "cycle_cleanup",
            reason="recovery",
            current_price_raw=recovery_market,
            recovery_target_raw=recovery_level,
            **self._current_state_payload(),
        )
        self.adapter.cleanup_counters(
            instance_key=self.instance.key,
            instrument=self.instance.config.instrument,
        )
        self._reset_cascade(reason="recovery")
        self._seed_step0_from_quote_source(is_buy_cascade=is_buy_cascade)

    def _reset_cascade(self, *, reason: str) -> None:
        del reason  # reserved for logging hooks
        self.instance.cascade.reset()
        self.instance.reset_activations()
        self.instance.cumulative_volume = Decimal("0")
        self.instance.protection.sl_price = None
        self.instance.protection.tp_price = None

    def _seed_step0_from_quote_source(self, *, is_buy_cascade: bool) -> None:
        """Immediately seed a fresh Step0 after strategy-cycle cleanup."""
        quote = self.quote_source.current_bid_ask(self.instance.config.instrument)
        self._seed_step0_from_quote(
            quote=quote,
            is_buy_cascade=is_buy_cascade,
            reason="fresh_cycle",
        )

    def _seed_step0_from_quote(
        self, *, quote: Quote, is_buy_cascade: bool, reason: str
    ) -> None:
        self.instance.cascade.step0_price = (
            quote.bid if is_buy_cascade else quote.ask
        )
        self.instance.cascade.highest_step = 0
        self._emit(
            "step0_seeded",
            reason=reason,
            **self._current_state_payload(),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_buy_cascade(self) -> bool:
        """The virtual cascade direction.

        counterBUY → virtual BUY cascade (moves UP).
        counterSELL → virtual SELL cascade (moves DOWN).
        """
        return self.instance.counter_type_is_buy()

    def _real_order_side(self) -> RealOrderSide:
        return (
            RealOrderSide.BUY
            if self.instance.counter_type_is_buy()
            else RealOrderSide.SELL
        )


# ---------------------------------------------------------------------
# Manager — minimal in-memory active registry.
# ---------------------------------------------------------------------


class FiboManager:
    """Minimal in-memory registry of running Fibo registrations.

    There is intentionally no STOPPED registry visible to the operator:
      - ``start()`` adds an active instance
      - ``stop()`` removes it (no exchange side effects)
      - ``list_running()`` returns only active instances

    ``poll_once()`` is the optional single-call driver for a future
    event loop.
    """

    def __init__(self, event_sink: Optional[EventSink] = None) -> None:
        self._engines: Dict[str, FiboEngine] = {}
        self._unprotected_log: List[Tuple[str, int, str, str]] = []
        self._event_sink = event_sink

    def _emit(self, event: str, **fields: Any) -> None:
        if self._event_sink is None:
            return
        payload: Dict[str, Any] = {"event": event}
        payload.update(fields)
        self._event_sink(payload)

    def start(
        self,
        config: FiboConfig,
        adapter: ExchangeAdapter,
        quote_source: QuoteSource,
    ) -> FiboInstance:
        config.validate()
        key = config.key
        if key in self._engines:
            raise ValueError(f"Fibo already running: {key}")
        start_hook = getattr(adapter, "on_registration_started", None)
        if callable(start_hook):
            start_hook(key)
        instance = FiboInstance(config=config, running=True)
        self._engines[key] = FiboEngine(instance, adapter, quote_source, self._event_sink)
        return instance

    def stop(self, key: str) -> bool:
        engine = self._engines.pop(key, None)
        if engine is None:
            return False
        # STOP MUST NOT call cleanup_counters. Must NOT close / cancel /
        # modify any exchange state. STOP MUST clear the frozen flag so
        # a subsequent restart starts fresh.
        engine.instance.running = False
        engine.instance.unfreeze()
        stop_hook = getattr(engine.adapter, "on_registration_stopped", None)
        if callable(stop_hook):
            stop_hook(key)
        self._emit("user_stop", registration_key=key)
        return True

    def list_running(self) -> List[FiboInstance]:
        return [engine.instance for engine in self._engines.values()]

    def is_running(self, key: str) -> bool:
        return key in self._engines

    def on_quote(self, key: str, quote: Quote) -> None:
        engine = self._engines.get(key)
        if engine is None:
            return
        try:
            engine.on_quote(quote)
        except UnprotectedCounterError as err:
            self._record_unprotected(engine.instance, err)

    def poll_once(self) -> None:
        for key, engine in list(self._engines.items()):
            try:
                quote = engine.quote_source.current_bid_ask(
                    engine.instance.config.instrument
                )
            except LookupError:
                continue
            except Exception:  # noqa: BLE001
                continue
            try:
                engine.on_quote(quote)
            except UnprotectedCounterError as err:
                self._record_unprotected(engine.instance, err)

    def _record_unprotected(
        self, instance: FiboInstance, err: UnprotectedCounterError
    ) -> None:
        instance.mark_unprotected(err.level, err.reason_code)
        self._unprotected_log.append(
            (instance.key, err.level, err.reason_code, err.detail)
        )
        extra_context = {
            k: v for k, v in err.context.items()
            if k not in {"registration_key", "level", "reason_code", "detail", "operation"}
        }
        self._emit(
            "unprotected_error",
            registration_key=instance.key,
            level=err.level,
            reason_code=err.reason_code,
            detail=err.detail,
            operation=err.operation,
            **extra_context,
        )

    def unprotected_log(self) -> List[Tuple[str, int, str, str]]:
        return list(self._unprotected_log)

    def clear_unprotected_log(self) -> None:
        self._unprotected_log.clear()


__all__ = [
    # constants
    "FIB_TABLE",
    "FIB_START_VALUE",
    "FIB_START_INDEX",
    "KILL_CYCLE_STEP",
    "NUM_COUNTERS",
    "DEFAULT_DIVIDE_PERCENT",
    "DEFAULT_COUNTER_1",
    "DEFAULT_COUNTER_2",
    "DEFAULT_COUNTER_3",
    "DEFAULT_COUNTER_4",
    # enums
    "CounterType",
    "RealOrderSide",
    # dataclasses
    "CascadeState",
    "ProtectionState",
    "FiboConfig",
    "FiboInstance",
    "FiboEngine",
    "FiboManager",
    # protocols / errors
    "ExchangeAdapter",
    "UnprotectedCounterError",
    # math helpers
    "fib_distance",
    "step_price",
    "step_tp",
    "step0_tp",
]
