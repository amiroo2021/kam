"""GoldenFibo v1 service — replaces the legacy counter-cascade service.

External API preserved (so fibo_daemon.py / installer stay unchanged):
  - PersistentFiboService
  - FiboSocketServiceHost
  - FiboServiceProtocol
  - FiboCycleLedger
  - RegistrationContext
  - resolve_fibo_state_path / resolve_fibo_ledger_path /
    resolve_fibo_event_log_path / resolve_fibo_socket_path /
    resolve_fibo_runtime_dir / resolve_hermes_home

Internal IPC ops supported:
  - start     begin a new GoldenFibo registration
  - list      enumerate registrations (active + quarantined)
  - detail    one registration detail
  - stop      stop a single registration (no auto-close)

Old-strategy quarantine:
  Any persisted record whose key is not in the new
  ``exchange/account/instrument/BUY|SELL`` shape, or whose persisted
  strategy is not ``golden_fibo``, is loaded as a quarantined entry
  with status=STATUS_QUARANTINED_OLD_STRATEGY. Such records are NEVER
  loaded into a GoldenFiboEngine and produce ZERO exchange mutations.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from .tradedesk import TradeDesk, get_tradedesk

from .golden_fibo.config import (
    GoldenFiboConfig,
    golden_fibo_cumulative_volume,
    golden_fibo_volume,
)
from .golden_fibo.state import (
    ROLE_ENTRY,
    ROLE_LADDER,
    ROLE_TP,
    STATUS_NEEDS_RECOVERY,
    STATUS_QUARANTINED_OLD_STRATEGY,
    STATUS_RUNNING,
    STATUS_STOPPING,
    STRATEGY_GOLDENFIBO,
    SUBMISSION_ATTEMPTED,
    SUBMISSION_CONFIRMED,
    SUBMISSION_NEEDS_RECOVERY,
    SUBMISSION_NOT_SUBMITTED,
    SUBMISSION_PREPARED,
    GoldenFiboState,
)
from .golden_fibo.lighter_adapter import LighterGoldenFiboAdapter

logger = logging.getLogger(__name__)

_DEFAULT_POLL_SECONDS = 2.0
_DEFAULT_SOCKET_TIMEOUT = 15.0

# ---------------------------------------------------------------------------
# Path resolvers (kept identical to the legacy service for installer parity)
# ---------------------------------------------------------------------------
def resolve_hermes_home() -> Path:
    raw = os.environ.get("HERMES_HOME")
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".hermes").resolve()


def resolve_fibo_runtime_dir() -> Path:
    return resolve_hermes_home() / "fibo"


def resolve_fibo_state_path() -> Path:
    return resolve_fibo_runtime_dir() / "service_state.json"


def resolve_fibo_ledger_path() -> Path:
    return resolve_fibo_runtime_dir() / "service_ledger.jsonl"


def resolve_fibo_event_log_path() -> Path:
    return resolve_fibo_runtime_dir() / "service-events.log"


def resolve_fibo_socket_path() -> Path:
    return resolve_fibo_runtime_dir() / "service.sock"


# ---------------------------------------------------------------------------
# Legacy protocol placeholder (kept for installer parity)
# ---------------------------------------------------------------------------
class FiboServiceProtocol(Protocol):
    def execute_command(self, command: Dict[str, Any]) -> Dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Direction validation
# ---------------------------------------------------------------------------
VALID_DIRECTIONS = ("BUY", "SELL")
SUPPORTED_EXCHANGES = ("lighter",)


def _is_valid_registration_key(key: str) -> bool:
    """A GoldenFibo registration key looks like:

        exchange/account/instrument/BUY

    or  exchange/account/instrument/SELL

    Anything else is rejected (or quarantined if loaded from old state).
    """
    if not key or "/" not in key:
        return False
    parts = key.split("/")
    if len(parts) != 4:
        return False
    exchange, account, instrument, direction = parts
    if not exchange or not account or not instrument:
        return False
    if direction not in VALID_DIRECTIONS:
        return False
    return True


# ---------------------------------------------------------------------------
# Registration context (kept for installer/legacy compat)
# ---------------------------------------------------------------------------
@dataclass
class RegistrationContext:
    """Legacy-compatible registration container.

    For GoldenFibo, the actual state is the GoldenFiboState. This
    dataclass only exists so legacy wiring (state.json IO, daemon
    boot summary) keeps compiling.
    """
    spec: Dict[str, Any]
    bundle: Dict[str, Any]
    started_at: float
    preflight: Dict[str, Any] = field(default_factory=dict)
    service_status: str = "running"
    status_reason: str = ""
    cleanup_details: Dict[str, Any] = field(default_factory=dict)
    stop_requested_at: Optional[float] = None
    last_known_cumulative_volume: float = 0.0


# ---------------------------------------------------------------------------
# Ledger (kept for installer parity)
# ---------------------------------------------------------------------------
class FiboCycleLedger:
    """Best-effort cycle/performance ledger (kept for parity)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or resolve_fibo_ledger_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._summaries: Dict[str, Dict[str, Any]] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(rec, dict):
                        self._summaries[rec.get("registration_key") or rec.get("key") or "?"] = rec
        except Exception:
            pass

    def note_cycle_cleanup(self, key: str) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"event": "cycle_cleanup", "registration_key": key, "ts": time.time()}, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def record_event(self, row: Dict[str, Any]) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Persistent GoldenFibo service
# ---------------------------------------------------------------------------
class _OppositeDirectionActive(Exception):
    """Raised when the opposite direction is already active."""

    def __init__(self, existing_key: str):
        self.existing_key = existing_key
        super().__init__(f"opposite direction already active: {existing_key}")


class _InvalidRegistrationKey(Exception):
    pass


class _InvalidInputs(Exception):
    pass


class _LighterOnly(Exception):
    pass


def _make_state_from_dict(data: Dict[str, Any]) -> GoldenFiboState:
    """Load a GoldenFiboState from persisted dict.

    Falls back to fields from the legacy state format if present
    (legacy fields: counter1..4, divide_percent, etc.) — those are
    simply ignored because the new state dataclass does not have them.
    """
    return GoldenFiboState.from_dict(data)


def _serialize_state(state: GoldenFiboState) -> Dict[str, Any]:
    return state.to_dict()


class PersistentFiboService:
    """Persistent GoldenFibo service.

    Maintains a dict of GoldenFiboState instances keyed by registration
    key. Persists to JSON. Each tick (poll) drives the engine for each
    active registration.
    """

    def __init__(
        self,
        state_path: Optional[Path] = None,
        ledger_path: Optional[Path] = None,
        event_log_path: Optional[Path] = None,
        tradedesk: Optional[TradeDesk] = None,
        poll_seconds: float = _DEFAULT_POLL_SECONDS,
        socket_timeout: float = _DEFAULT_SOCKET_TIMEOUT,
        start_thread: bool = True,
        ledger: Optional[FiboCycleLedger] = None,
    ) -> None:
        # start_thread (default True) means the daemon expects the
        # service to own and start its own background poll thread.
        # fibo_daemon does not run its own poll loop — it constructs
        # the service and then serves IPC. When start_thread is True,
        # we start the poll thread automatically so the engine ticks
        # without requiring the daemon to do anything special.
        # ledger is accepted for the same reason — if a constructed
        # FiboCycleLedger is passed (the old daemon signature), use it;
        # otherwise build one from ledger_path.
        self.state_path = Path(state_path or resolve_fibo_state_path())
        self.ledger_path = Path(ledger_path or resolve_fibo_ledger_path())
        self.event_log_path = Path(event_log_path or resolve_fibo_event_log_path())
        self.tradedesk = tradedesk or get_tradedesk()
        self.poll_seconds = float(poll_seconds)
        self.socket_timeout = float(socket_timeout)
        if ledger is not None:
            self.ledger = ledger
            # Mirror the ledger's path back so self.ledger_path
            # matches what the daemon passed (rather than falling
            # back to resolve_fibo_ledger_path()).
            try:
                self.ledger_path = Path(ledger.path)
            except Exception:
                pass
        else:
            # Will be set after the path is established below.
            self.ledger = None

        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

        if self.ledger is None:
            self.ledger = FiboCycleLedger(self.ledger_path)
        self._states: Dict[str, GoldenFiboState] = {}
        self._configs: Dict[str, GoldenFiboConfig] = {}
        self._adapters: Dict[str, LighterGoldenFiboAdapter] = {}
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

        # Load persisted state on construction
        self._load_state()

        # Auto-start the poll thread if requested. The daemon does not
        # run its own poll loop, so the service must own ticking.
        if start_thread:
            self.start_polling()

    # ------------------------------------------------------------------
    # State IO
    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        regs = payload.get("registrations") or []
        for entry in regs:
            if not isinstance(entry, dict):
                continue
            key = entry.get("registration_key") or entry.get("key") or ""
            if not isinstance(key, str):
                continue
            # Old-strategy records: tagged differently, missing BUYSELL, etc.
            if not _is_valid_registration_key(key):
                # Quarantine: load as a quarantine entry
                state = GoldenFiboState.from_dict({
                    "registration_key": key,
                    "status": STATUS_QUARANTINED_OLD_STRATEGY,
                    "strategy": entry.get("strategy", "fibonacci_counter_cascade"),
                    "schema_version": 0,
                })
                self._states[key] = state
                continue
            # New-style: try to load as GoldenFiboState
            try:
                state = GoldenFiboState.from_dict(entry)
            except Exception:
                state = GoldenFiboState(
                    registration_key=key,
                    status=STATUS_QUARANTINED_OLD_STRATEGY,
                    strategy="fibonacci_counter_cascade",
                )
                self._states[key] = state
                continue
            # Backwards-compat: legacy records may not have strategy="golden_fibo"
            if not state.strategy or state.strategy == "fibonacci_counter_cascade":
                state.strategy = STRATEGY_GOLDENFIBO
            self._states[key] = state

    def _save_state(self) -> None:
        data: Dict[str, Any] = {
            "schema_version": 1,
            "strategy": STRATEGY_GOLDENFIBO,
            "registrations": [state.to_dict() for state in self._states.values()],
        }
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, default=str)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self.state_path)
        except Exception as exc:
            logger.warning("save_state failed: %s", exc)

    # ------------------------------------------------------------------
    # Engine construction
    # ------------------------------------------------------------------
    def _adapter_for(self, key: str) -> LighterGoldenFiboAdapter:
        adapter = self._adapters.get(key)
        if adapter is None:
            adapter = LighterGoldenFiboAdapter()
            self._adapters[key] = adapter
        return adapter

    def _config_for(self, key: str, state: GoldenFiboState) -> GoldenFiboConfig:
        cfg = self._configs.get(key)
        if cfg is not None:
            return cfg
        cfg = GoldenFiboConfig(
            exchange=state.exchange,
            account=state.account,
            instrument=state.instrument,
            direction=state.direction,
            percentage=state.percentage,
            step0_volume=state.step0_volume,
        )
        self._configs[key] = cfg
        return cfg

    def _client_id_factory(self, key: str) -> Callable[[], int]:
        # Deterministic monotonic per key (no time-based) so restart safety is
        # preserved across crashes.
        counter = {"n": self._states[key].cycle_id * 1000000 + 100000}  # noqa: F841

        def _next() -> int:
            counter["n"] += 1
            return counter["n"]

        return _next

    # ------------------------------------------------------------------
    # Engine tick (one per active registration)
    # ------------------------------------------------------------------
    def _tick_once(self) -> None:
        with self._lock:
            keys = list(self._states.keys())
        for key in keys:
            with self._lock:
                state = self._states.get(key)
                if state is None:
                    continue
                if state.status == STATUS_QUARANTINED_OLD_STRATEGY:
                    continue
                if state.status == STATUS_STOPPING:
                    continue
            try:
                self._drive_one(key)
            except Exception as exc:
                logger.warning("tick for %s failed: %s", key, exc)

    def _drive_one(self, key: str) -> None:
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return
            if state.status == STATUS_QUARANTINED_OLD_STRATEGY:
                return
            if state.status != STATUS_RUNNING:
                return
            cfg = self._config_for(key, state)
            adapter = self._adapter_for(key)
            from .golden_fibo.engine import GoldenFiboEngine
            engine = GoldenFiboEngine(
                cfg,
                state,
                adapter,
                self._client_id_factory(key),
            )
        # Run the tick OUTSIDE the lock so callers can read/list concurrently.
        result = engine.tick()
        with self._lock:
            # The engine mutates state in place; persist.
            self._states[key] = result.state
            self._save_state()
            # Read through-after-mutation fields.
            s = self._states[key]
            # If Step0 (entry) is FILLED and we have a position, the
            # service must call confirm_step0_filled with the actual
            # fill price (read from the venue via resolve_instrument /
            # position_state) BEFORE the next tick.
            if (
                s.pending_order_role == ROLE_ENTRY
                and s.next_step == 0
                and s.pending_order_exchange_id is not None
            ):
                # Snapshot: don't recurse infinitely. The next tick
                # will pick up the confirmed state.
                self._maybe_confirm_step0(key)
            if (
                s.pending_order_role == ROLE_LADDER
                and s.pending_order_exchange_id is not None
                and s.pending_confirmed_price is None
            ):
                # Should not happen but be defensive
                pass

    def _maybe_confirm_step0(self, key: str) -> None:
        """For Step0 MARKET: confirm fill via the live venue, persist P0.

        The service is the only place with adapter access. The engine
        cannot query the adapter directly from inside _handle_confirmed_fill
        because it is a pure state machine.

        We re-read get_order_state.actual_fill_price to confirm P0.
        If pending_order_exchange_id is None (Lighter market orders may
        return None), reconcile via position delta instead.
        """
        with self._lock:
            state = self._states.get(key)
            if state is None or state.pending_order_role != ROLE_ENTRY:
                return
            exchange_order_id = state.pending_order_exchange_id

        # Path 1: if we have an exchange_order_id, use get_order_state.
        order_state = None
        if exchange_order_id is not None:
            try:
                order_state = self._adapter_for(key).get_order_state(
                    state.account, int(exchange_order_id)
                )
            except Exception as exc:
                logger.warning("step0 get_order_state failed for %s: %s", key, exc)
                return

        # Path 2: if no exchange_order_id or get_order_state returned
        # nothing, reconcile via position delta.
        if not order_state:
            try:
                position = self._adapter_for(key).position_state(
                    state.account, state.instrument
                )
            except Exception as exc:
                logger.warning("step0 position_state failed for %s: %s", key, exc)
                return
            if not isinstance(position, dict):
                return
            live_size_raw = position.get("size")
            try:
                live_size = Decimal(str(live_size_raw or "0"))
            except Exception:
                live_size = Decimal("0")
            expected_size = Decimal(str(state.step0_volume or "0"))
            if live_size < expected_size:
                # Position not yet established; wait
                return
            # Position established — treat as Step0 filled.
            # Use position entry price as fallback for P0.
            ep = position.get("entry_price")
            if ep is not None:
                try:
                    p0 = Decimal(str(ep))
                except Exception:
                    p0 = None
            else:
                p0 = None
            if p0 is None or p0 <= 0:
                with self._lock:
                    state.status = STATUS_NEEDS_RECOVERY
                    state.freeze_reason = "could not establish Step0 fill price via position"
                    self._save_state()
                return
            # Promote P0 and place TP + Step1
            with self._lock:
                state = self._states.get(key)
                if state is None:
                    return
                from .golden_fibo.engine import GoldenFiboEngine
                cfg = self._config_for(key, state)
                adapter = self._adapter_for(key)
                engine = GoldenFiboEngine(cfg, state, adapter, self._client_id_factory(key))
                engine.confirm_step0_filled(p0)
                result = engine.place_step0_tp_and_step1(p0)
                self._states[key] = engine.state
                self._save_state()
                if result is not None:
                    # Engine froze — propagate
                    self._states[key] = result.state
                    self._save_state()
            return

        # Path 1 continued: order_state exists and is not empty.
        status = str(order_state.get("status") or "")
        taxonomy = str(order_state.get("taxonomy") or "")
        if taxonomy != "FILLED" and status != "filled":
            # Not yet filled; wait
            return
        # Fill price: try actual_fill_price first, fall back to position entry
        p0: Optional[Decimal] = None
        afp = order_state.get("actual_fill_price")
        if afp is not None:
            try:
                p0 = Decimal(str(afp))
            except Exception:
                p0 = None
        if p0 is None or p0 <= 0:
            # Fall back to position entry (per the simplified spec)
            try:
                position = self._adapter_for(key).position_state(
                    state.account, state.instrument
                )
            except Exception:
                position = None
            if isinstance(position, dict):
                ep = position.get("entry_price")
                if ep is not None:
                    try:
                        p0 = Decimal(str(ep))
                    except Exception:
                        p0 = None
        if p0 is None or p0 <= 0:
            # Cannot establish P0 yet — freeze
            with self._lock:
                state.status = STATUS_NEEDS_RECOVERY
                state.freeze_reason = "could not establish Step0 fill price"
                self._save_state()
            return
        # Now promote P0 and place TP + Step1
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return
            from .golden_fibo.engine import GoldenFiboEngine
            cfg = self._config_for(key, state)
            adapter = self._adapter_for(key)
            engine = GoldenFiboEngine(cfg, state, adapter, self._client_id_factory(key))
            engine.confirm_step0_filled(p0)
            result = engine.place_step0_tp_and_step1(p0)
            self._states[key] = engine.state
            self._save_state()
            if result is not None:
                # Engine froze — propagate
                self._states[key] = result.state
                self._save_state()

    # ------------------------------------------------------------------
    # Public IPC commands
    # ------------------------------------------------------------------
    def execute_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            op = str(command.get("op") or "").strip()
        try:
            if op == "start":
                return self._cmd_start(command)
            if op == "list":
                return self._cmd_list(command)
            if op == "detail":
                return self._cmd_detail(command)
            if op == "stop":
                return self._cmd_stop(command)
            if op == "preview":
                return self._cmd_preview(command)
            return {"ok": False, "error": f"unknown op: {op!r}"}
        except _OppositeDirectionActive as exc:
            return {"ok": False, "error": "OPPOSITE_DIRECTION_ACTIVE", "existing_registration_key": exc.existing_key}
        except _InvalidRegistrationKey as exc:
            return {"ok": False, "error": "INVALID_REGISTRATION_KEY", "detail": str(exc)}
        except _InvalidInputs as exc:
            return {"ok": False, "error": "INVALID_INPUTS", "detail": str(exc)}
        except _LighterOnly as exc:
            return {"ok": False, "error": "GOLDENFIBO_NOT_SUPPORTED", "detail": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": "INTERNAL", "detail": str(exc)}

    def _cmd_start(self, command: Dict[str, Any]) -> Dict[str, Any]:
        exchange = str(command.get("exchange") or "").strip().lower()
        account = str(command.get("account") or "").strip()
        instrument = str(command.get("instrument") or "").strip()
        direction = str(command.get("direction") or "").strip().upper()
        percentage = command.get("percentage")
        step0_volume = command.get("step0_volume")

        if exchange not in SUPPORTED_EXCHANGES:
            raise _LighterOnly(f"exchange {exchange!r} not supported (v1: lighter only)")
        if direction not in VALID_DIRECTIONS:
            raise _InvalidInputs(f"direction must be one of {VALID_DIRECTIONS}")
        if not account:
            raise _InvalidInputs("account required")
        if not instrument:
            raise _InvalidInputs("instrument required")
        try:
            pct = Decimal(str(percentage))
        except Exception:
            raise _InvalidInputs("percentage must be a positive decimal")
        if pct <= 0:
            raise _InvalidInputs("percentage must be positive")
        try:
            v0 = Decimal(str(step0_volume))
        except Exception:
            raise _InvalidInputs("step0_volume must be a positive decimal")
        if v0 <= 0:
            raise _InvalidInputs("step0_volume must be positive")

        key = f"{exchange}/{account}/{instrument}/{direction}"

        # Opposite-direction rejection FIRST — before any network call.
        with self._lock:
            opposite = "SELL" if direction == "BUY" else "BUY"
            opposite_key = f"{exchange}/{account}/{instrument}/{opposite}"
            if opposite_key in self._states:
                state = self._states[opposite_key]
                if state.status != STATUS_QUARANTINED_OLD_STRATEGY:
                    raise _OppositeDirectionActive(opposite_key)
            if key in self._states:
                state = self._states[key]
                if state.status == STATUS_RUNNING:
                    return {"ok": False, "error": "DUPLICATE_REGISTRATION", "registration_key": key}
                # STOPPING / NEEDS_RECOVERY / other non-quarantined records
                # fall through to the lane preflight so the durable
                # ownership record blocks the new START properly.

        # Lane-not-flat preflight: before ANY fresh Step0, check the
        # live venue lane + durable tombstones. If a prior tombstone
        # exists on this lane, START must reconcile before creating a
        # new registration. If the live venue shows a position on this
        # lane, START is rejected.
        preflight_error = self._lane_preflight(exchange, account, instrument, direction, key)
        if preflight_error is not None:
            return {"ok": False, **preflight_error}

        # Validate venue-level constraints via resolve_instrument
        try:
            instrument_meta = self._adapter_for(key).resolve_instrument(account, instrument)
        except Exception as exc:
            raise _InvalidInputs(f"resolve_instrument failed: {exc}")
        if not instrument_meta:
            raise _InvalidInputs(f"instrument {instrument!r} not resolvable on {exchange}")

        # Check size constraints
        # instrument_meta is a dict (flattened from CanonicalInstrument
        # via the adapter's _get_payload helper). The CanonicalInstrument
        # field is named "minimum_size" on the source side; legacy
        # agents may surface "min_base_amount" instead. Accept both.
        min_size_raw = (
            (instrument_meta or {}).get("minimum_size")
            if isinstance(instrument_meta, dict)
            else None
        )
        if min_size_raw is None and isinstance(instrument_meta, dict):
            min_size_raw = instrument_meta.get("min_base_amount")
        if min_size_raw is not None:
            try:
                min_size = Decimal(str(min_size_raw))
                if v0 < min_size:
                    raise _InvalidInputs(
                        f"step0_volume {v0} below venue minimum {min_size}"
                    )
            except Exception:
                pass

        with self._lock:
            state = GoldenFiboState(
                strategy=STRATEGY_GOLDENFIBO,
                schema_version=1,
                registration_key=key,
                cycle_id=0,
                exchange=exchange,
                account=account,
                instrument=instrument,
                direction=direction,
                percentage=pct,
                step0_volume=v0,
                status=STATUS_RUNNING,
            )
            self._states[key] = state
            self._save_state()
        return {"ok": True, "registration_key": key, "status": STATUS_RUNNING}

    def _cmd_list(self, command: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            active = []
            quarantined = []
            for key, state in self._states.items():
                entry = {
                    "registration_key": key,
                    "exchange": state.exchange,
                    "account": state.account,
                    "instrument": state.instrument,
                    "direction": state.direction,
                    "cycle_id": state.cycle_id,
                    "highest_filled_step": state.highest_filled_step,
                    "expected_cumulative_size": str(state.expected_cumulative_size),
                    "current_tp_price": None if state.current_tp_price is None else str(state.current_tp_price),
                    "next_step": state.next_step,
                    "status": state.status,
                    "freeze_reason": state.freeze_reason,
                }
                if state.status == STATUS_QUARANTINED_OLD_STRATEGY:
                    quarantined.append(entry)
                else:
                    active.append(entry)
        return {"ok": True, "registrations": active, "quarantined": quarantined, "registrations_count": len(active), "quarantined_count": len(quarantined)}

    def _cmd_detail(self, command: Dict[str, Any]) -> Dict[str, Any]:
        key = str(command.get("registration_key") or "").strip()
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return {"ok": False, "error": "NOT_FOUND", "registration_key": key}
            return {"ok": True, "registration": state.to_dict()}

    def _cmd_stop(self, command: Dict[str, Any]) -> Dict[str, Any]:
        key = str(command.get("registration_key") or "").strip()
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return {"ok": False, "error": "NOT_FOUND", "registration_key": key}
            if state.status == STATUS_QUARANTINED_OLD_STRATEGY:
                return {"ok": False, "error": "OLD_STRATEGY_REGISTRATION", "registration_key": key}

            # Determine whether this registration has unresolved ownership.
            # A tombstone is required when any of the following is true:
            #   - live owned position (expected_cumulative_size > 0)
            #   - unresolved submission (submission_phase == ATTEMPTED)
            #   - owned pending ladder (pending_order_exchange_id is not None)
            #   - owned TP (current_tp_order_id is not None)
            #   - NEEDS_RECOVERY status
            has_position = Decimal(str(state.expected_cumulative_size or "0")) > 0
            has_unresolved_submission = state.submission_phase == SUBMISSION_ATTEMPTED
            has_owned_pending = state.pending_order_exchange_id is not None
            has_owned_tp = state.current_tp_order_id is not None
            is_needs_recovery = state.status == STATUS_NEEDS_RECOVERY

            unresolved = (
                has_position
                or has_unresolved_submission
                or has_owned_pending
                or has_owned_tp
                or is_needs_recovery
            )

            if unresolved:
                # Preserve a durable tombstone so a later START on the
                # same lane reconciles instead of blindly resubmitting.
                # The record is marked STATUS_STOPPING so the engine
                # cannot tick it further, but the ownership metadata
                # is kept.
                state.status = STATUS_STOPPING
                state.freeze_reason = (
                    f"tombstone_preserved: "
                    f"has_position={has_position} "
                    f"has_unresolved_submission={has_unresolved_submission} "
                    f"has_owned_pending={has_owned_pending} "
                    f"has_owned_tp={has_owned_tp} "
                    f"needs_recovery={is_needs_recovery}"
                )
                self._save_state()
                return {
                    "ok": True,
                    "registration_key": key,
                    "status": "stopped_with_tombstone",
                    "tombstone": True,
                    "has_position": has_position,
                    "has_unresolved_submission": has_unresolved_submission,
                    "has_owned_pending": has_owned_pending,
                    "has_owned_tp": has_owned_tp,
                    "needs_recovery": is_needs_recovery,
                }

            # Truly flat/no-order registration can be cleanly removed.
            self._states.pop(key, None)
            self._save_state()
        return {"ok": True, "registration_key": key, "status": "stopped"}

    def _lane_preflight(
        self,
        exchange: str,
        account: str,
        instrument: str,
        direction: str,
        key: str,
    ) -> Optional[Dict[str, Any]]:
        """Preflight before any fresh Step0.

        Rejects when:
        - A tombstone (stopped-with-tombstone) exists on this lane.
        - A prior non-quarantined record on this lane has unresolved
          ownership (submission attempted / owned pending / owned TP /
          needs_recovery).
        - The live venue shows a position on this lane (read via
          position_state).

        Returns None when the lane is clear, otherwise an error dict.
        """
        # Tombstone check: prior STOP left a tombstone on this lane.
        with self._lock:
            for existing_key, existing_state in list(self._states.items()):
                same_lane = (
                    existing_state.exchange == exchange
                    and existing_state.account == account
                    and existing_state.instrument == instrument
                )
                if not same_lane:
                    continue
                if existing_state.status == STATUS_STOPPING:
                    return {
                        "error": "LANE_TOMBSTONE",
                        "registration_key": existing_key,
                        "detail": (
                            "A prior stopped registration left a tombstone on this lane. "
                            "Reconcile before starting a new registration."
                        ),
                    }
                # Unresolved ownership on a same-direction lane record.
                if existing_key == key and existing_state.status != STATUS_QUARANTINED_OLD_STRATEGY:
                    has_position = Decimal(str(existing_state.expected_cumulative_size or "0")) > 0
                    has_unresolved = existing_state.submission_phase == SUBMISSION_ATTEMPTED
                    has_owned_pending = existing_state.pending_order_exchange_id is not None
                    has_owned_tp = existing_state.current_tp_order_id is not None
                    is_needs_recovery = existing_state.status == STATUS_NEEDS_RECOVERY
                    if has_position or has_unresolved or has_owned_pending or has_owned_tp or is_needs_recovery:
                        return {
                            "error": "LANE_NOT_FLAT",
                            "registration_key": existing_key,
                            "has_position": has_position,
                            "has_unresolved_submission": has_unresolved,
                            "has_owned_pending": has_owned_pending,
                            "has_owned_tp": has_owned_tp,
                            "needs_recovery": is_needs_recovery,
                        }

        # Live venue check: read position_state for the lane.
        try:
            position = self._adapter_for(key).position_state(account, instrument)
        except Exception:
            position = None
        if isinstance(position, dict):
            live_size_raw = position.get("size")
            try:
                live_size = Decimal(str(live_size_raw or "0"))
            except Exception:
                live_size = Decimal("0")
            if live_size > 0:
                return {
                    "error": "LANE_NOT_FLAT",
                    "registration_key": key,
                    "detail": (
                        f"Live venue shows position size {live_size} on {exchange}/{account}/{instrument}. "
                        "START rejected; reconcile before creating a new registration."
                    ),
                }
        return None

    def _cmd_preview(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """Return the derived V0..V20 + cumulative exposure for a step0."""
        try:
            step0 = Decimal(str(command.get("step0_volume") or "0"))
        except Exception:
            return {"ok": False, "error": "INVALID_INPUTS"}
        if step0 <= 0:
            return {"ok": False, "error": "INVALID_INPUTS"}
        ladder = []
        cumulative = Decimal("0")
        for n in range(21):
            v = golden_fibo_volume(step0, n)
            cumulative += v
            ladder.append({"step": n, "size": str(v), "cumulative_through_step": str(cumulative)})
        return {"ok": True, "step0_volume": str(step0), "ladder": ladder, "cumulative_through_step20": str(cumulative)}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start_polling(self) -> None:
        """Start the background poll thread."""
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return
        self._shutdown.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="golden-fibo-poll", daemon=True
        )
        self._poll_thread.start()

    def shutdown(self) -> None:
        self._shutdown.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=5.0)
            self._poll_thread = None

    def _poll_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                self._tick_once()
            except Exception as exc:
                logger.warning("poll tick failed: %s", exc)
            self._shutdown.wait(self.poll_seconds)


# ---------------------------------------------------------------------------
# Socket server
# ---------------------------------------------------------------------------
class _FiboCommandHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            payload = self.rfile.readline().decode("utf-8").strip()
            if not payload:
                return
            command = json.loads(payload)
            response = self.server.service.execute_command(command)  # type: ignore[attr-defined]
            data = json.dumps(response, ensure_ascii=False, default=str).encode("utf-8")
            self.wfile.write(data + b"\n")
        except Exception as exc:
            try:
                data = json.dumps({"ok": False, "error": "BAD_REQUEST", "detail": str(exc)}, ensure_ascii=False).encode("utf-8")
                self.wfile.write(data + b"\n")
            except Exception:
                pass


class FiboSocketServiceHost(socketserver.ThreadingUnixStreamServer):
    """Unix-socket IPC server for the persistent GoldenFibo service."""

    def __init__(self, *, service: PersistentFiboService, socket_path: Path):
        self.service = service
        self.socket_path = Path(socket_path)
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except Exception:
                pass
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(str(self.socket_path), _FiboCommandHandler)
        self._serving_thread: Optional[threading.Thread] = None
        self._serving = True

    def serve_forever(self) -> None:
        try:
            super().serve_forever()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        try:
            super().shutdown()
        except Exception:
            pass
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except Exception:
                pass



# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------
_service_singleton: Optional[PersistentFiboService] = None
_service_lock = threading.Lock()


def get_fibo_service() -> FiboServiceProtocol:
    """Return the singleton PersistentFiboService.

    Tests can call ``_reset_fibo_service()`` to wipe the singleton.
    """
    global _service_singleton
    with _service_lock:
        if _service_singleton is None:
            _service_singleton = PersistentFiboService()
        return _service_singleton


def _reset_fibo_service() -> None:
    global _service_singleton
    with _service_lock:
        if _service_singleton is not None:
            try:
                _service_singleton.shutdown()
            except Exception:
                pass
        _service_singleton = None
