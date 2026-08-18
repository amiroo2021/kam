"""GoldenFibo mutable state per registration."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Optional


# Strategy names
STRATEGY_GOLDENFIBO = "golden_fibo"
STRATEGY_OLD_COUNTER_CASCADE = "fibonacci_counter_cascade"

# Status values
STATUS_NEVER_STARTED = "never_started"
STATUS_RUNNING = "running"
STATUS_NEEDS_RECOVERY = "needs_recovery"
STATUS_STOPPING = "stopping"
STATUS_QUARANTINED_OLD_STRATEGY = "quarantined_old_strategy"

# Order roles
ROLE_ENTRY = "entry"
ROLE_LADDER = "ladder"
ROLE_TP = "tp"

# Durable submission phases. Once SUBMISSION_ATTEMPTED is persisted,
# an exception/timeout/unknown response MUST NOT permit automatic
# resubmission of the same logical order. The exchange must be
# reconciled first.
SUBMISSION_NOT_SUBMITTED = "not_submitted"
SUBMISSION_PREPARED = "submission_prepared"
SUBMISSION_ATTEMPTED = "submission_attempted"
SUBMISSION_CONFIRMED = "confirmed"
SUBMISSION_NEEDS_RECOVERY = "needs_recovery"


@dataclass
class GoldenFiboState:
    """Mutable per-registration GoldenFibo state.

    Persisted in service_state.json. Restart safety: every order we
    expect to find on the venue must have its deterministic identity
    (exchange_order_id, client_order_id) persisted here BEFORE we
    trust the live venue to reflect it.
    """

    # versioning + ownership
    strategy: str = STRATEGY_GOLDENFIBO
    schema_version: int = 1

    registration_key: str = ""
    cycle_id: int = 0

    # static config (mirrors GoldenFiboConfig)
    exchange: str = ""
    account: str = ""
    instrument: str = ""
    direction: str = "BUY"
    percentage: Decimal = Decimal("0")
    step0_volume: Decimal = Decimal("0")

    # progress
    highest_filled_step: int = -1
    fill_prices: Dict[int, Decimal] = field(default_factory=dict)
    expected_cumulative_size: Decimal = Decimal("0")

    # current shared TP
    current_tp_price: Optional[Decimal] = None
    current_tp_order_id: Optional[int] = None
    current_tp_client_id: Optional[int] = None
    current_tp_role: Optional[str] = None  # ROLE_TP

    # next pending ladder order
    next_step: int = 0
    pending_order_client_id: Optional[int] = None
    pending_order_exchange_id: Optional[int] = None
    pending_requested_price: Optional[Decimal] = None
    pending_requested_size: Optional[Decimal] = None
    pending_confirmed_price: Optional[Decimal] = None
    pending_confirmed_size: Optional[Decimal] = None
    pending_order_role: Optional[str] = None  # ROLE_LADDER or ROLE_ENTRY

    # Per-step durable order history. Maps logical step number -> the
    # confirmed order identity that created/advanced that step. This
    # preserves Step0 ENTRY ownership, Step1 LADDER identity, etc., so a
    # generic "current submission" field can never erase historical
    # ownership. Entry example:
    #   step_orders[0] = {"role": "entry", "client_id": ..., "exchange_order_id": ...,
    #                     "status": "filled", "price": "76.370"}
    # Keys are ints in memory; serialized as strings in JSON.
    step_orders: Dict[int, Dict[str, object]] = field(default_factory=dict)

    # Durable submission tracking. Written BEFORE the venue call so a
    # crash mid-submission can never silently retry the same logical
    # order. submission_client_id is deterministic per
    # (registration_key, cycle_id, step, role) and persisted BEFORE
    # the venue call.
    submission_phase: str = SUBMISSION_NOT_SUBMITTED
    submission_client_id: Optional[int] = None
    submission_step: Optional[int] = None
    submission_role: Optional[str] = None
    submission_attempted_at: Optional[float] = None
    submission_exchange_order_id: Optional[int] = None

    # status
    status: str = STATUS_NEVER_STARTED
    freeze_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "strategy": self.strategy,
            "schema_version": self.schema_version,
            "registration_key": self.registration_key,
            "cycle_id": self.cycle_id,
            "exchange": self.exchange,
            "account": self.account,
            "instrument": self.instrument,
            "direction": self.direction,
            "percentage": str(self.percentage),
            "step0_volume": str(self.step0_volume),
            "highest_filled_step": self.highest_filled_step,
            "fill_prices": {str(k): str(v) for k, v in self.fill_prices.items()},
            "expected_cumulative_size": str(self.expected_cumulative_size),
            "current_tp_price": None if self.current_tp_price is None else str(self.current_tp_price),
            "current_tp_order_id": self.current_tp_order_id,
            "current_tp_client_id": self.current_tp_client_id,
            "current_tp_role": self.current_tp_role,
            "next_step": self.next_step,
            "pending_order_client_id": self.pending_order_client_id,
            "pending_order_exchange_id": self.pending_order_exchange_id,
            "pending_requested_price": None if self.pending_requested_price is None else str(self.pending_requested_price),
            "pending_requested_size": None if self.pending_requested_size is None else str(self.pending_requested_size),
            "pending_confirmed_price": None if self.pending_confirmed_price is None else str(self.pending_confirmed_price),
            "pending_confirmed_size": None if self.pending_confirmed_size is None else str(self.pending_confirmed_size),
            "pending_order_role": self.pending_order_role,
            "step_orders": {str(k): v for k, v in self.step_orders.items()},
            "submission_phase": self.submission_phase,
            "submission_client_id": self.submission_client_id,
            "submission_step": self.submission_step,
            "submission_role": self.submission_role,
            "submission_attempted_at": self.submission_attempted_at,
            "submission_exchange_order_id": self.submission_exchange_order_id,
            "status": self.status,
            "freeze_reason": self.freeze_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "GoldenFiboState":
        return cls(
            strategy=str(data.get("strategy", STRATEGY_GOLDENFIBO)),
            schema_version=int(data.get("schema_version", 1) or 1),
            registration_key=str(data.get("registration_key", "")),
            cycle_id=int(data.get("cycle_id", 0) or 0),
            exchange=str(data.get("exchange", "")),
            account=str(data.get("account", "")),
            instrument=str(data.get("instrument", "")),
            direction=str(data.get("direction", "BUY")),
            percentage=Decimal(str(data.get("percentage", "0"))),
            step0_volume=Decimal(str(data.get("step0_volume", "0"))),
            highest_filled_step=int(data.get("highest_filled_step", -1) or -1),
            fill_prices={int(k): Decimal(v) for k, v in (data.get("fill_prices") or {}).items()},
            expected_cumulative_size=Decimal(str(data.get("expected_cumulative_size", "0"))),
            current_tp_price=None if data.get("current_tp_price") is None else Decimal(str(data.get("current_tp_price"))),
            current_tp_order_id=None if data.get("current_tp_order_id") is None else int(data.get("current_tp_order_id") or 0),
            current_tp_client_id=None if data.get("current_tp_client_id") is None else int(data.get("current_tp_client_id") or 0),
            current_tp_role=data.get("current_tp_role"),
            next_step=int(data.get("next_step", 0) or 0),
            pending_order_client_id=None if data.get("pending_order_client_id") is None else int(data.get("pending_order_client_id") or 0),
            pending_order_exchange_id=None if data.get("pending_order_exchange_id") is None else int(data.get("pending_order_exchange_id") or 0),
            pending_requested_price=None if data.get("pending_requested_price") is None else Decimal(str(data.get("pending_requested_price"))),
            pending_requested_size=None if data.get("pending_requested_size") is None else Decimal(str(data.get("pending_requested_size"))),
            pending_confirmed_price=None if data.get("pending_confirmed_price") is None else Decimal(str(data.get("pending_confirmed_price"))),
            pending_confirmed_size=None if data.get("pending_confirmed_size") is None else Decimal(str(data.get("pending_confirmed_size"))),
            pending_order_role=data.get("pending_order_role"),
            step_orders={int(k): v for k, v in (data.get("step_orders") or {}).items()},
            submission_phase=str(data.get("submission_phase", SUBMISSION_NOT_SUBMITTED)),
            submission_client_id=None if data.get("submission_client_id") is None else int(data.get("submission_client_id") or 0),
            submission_step=None if data.get("submission_step") is None else int(data.get("submission_step") or 0),
            submission_role=data.get("submission_role"),
            submission_attempted_at=None if data.get("submission_attempted_at") is None else float(data.get("submission_attempted_at") or 0.0),
            submission_exchange_order_id=None if data.get("submission_exchange_order_id") is None else int(data.get("submission_exchange_order_id") or 0),
            status=str(data.get("status", STATUS_NEVER_STARTED)),
            freeze_reason=data.get("freeze_reason"),
        )
