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
  - start              begin a new GoldenFibo registration
  - list               enumerate registrations (active + quarantined)
  - detail             one registration detail
  - stop               legacy soft-stop (tombstone / remove if flat; no close)
  - smooth_shutdown    finish current cycle then deregister (no new Step0)
  - emergency_stop     cancel owned ladder/TP, close owned position, deregister

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
    SHUTDOWN_MODE_EMERGENCY,
    SHUTDOWN_MODE_NONE,
    SHUTDOWN_MODE_SMOOTH,
    STATUS_COMPLETED,
    STATUS_NEEDS_RECOVERY,
    STATUS_QUARANTINED_OLD_STRATEGY,
    STATUS_RUNNING,
    STATUS_SMOOTH_SHUTDOWN,
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
from .golden_fibo.arcus_adapter import ArcusGoldenFiboAdapter
from .golden_fibo.rise_adapter import RiseGoldenFiboAdapter

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
SUPPORTED_EXCHANGES = ("lighter", "arcus", "rise")


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
        self._adapters: Dict[str, Any] = {}
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
    def _adapter_for(self, key: str) -> Any:
        adapter = self._adapters.get(key)
        if adapter is None:
            exchange = str(key.split("/", 1)[0] if "/" in key else "").lower()
            # Prefer registration state exchange when available.
            st = self._states.get(key)
            if st is not None and st.exchange:
                exchange = str(st.exchange).lower()
            if exchange == "arcus":
                adapter = ArcusGoldenFiboAdapter()
            elif exchange == "rise":
                adapter = RiseGoldenFiboAdapter()
            else:
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
        """Legacy factory retained for older engine call sites / tests.

        Production GoldenFiboEngine paths use V2 client_id_v2 allocation
        from persisted state (cycle_uid + seq map). This factory is only
        used when client_id_version < 2.
        """
        counter = {"n": self._states[key].cycle_id * 1000000 + 100000}

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
                    # Emergency STOP in progress or tombstone — no tick mutations
                    continue
                # Emergency mode: never let normal/reconcile polling compete with
                # the emergency path (Arcus 429 storms observed live).
                if str(getattr(state, "shutdown_mode", "") or "") == SHUTDOWN_MODE_EMERGENCY:
                    continue
                if state.status == STATUS_COMPLETED:
                    # Should have been popped; defensive cleanup
                    self._states.pop(key, None)
                    self._save_state()
                    continue
                # Arcus-specific: while HTTP GET backoff is active, skip reconcile
                # hammering that only amplifies 429s.
                if str(state.exchange).lower() == "arcus" and state.status == STATUS_NEEDS_RECOVERY:
                    try:
                        from .agents import x_arcus_agent as _arcus_mod

                        if float(_arcus_mod.arcus_http_backoff_remaining()) > 0.05:
                            continue
                    except Exception:
                        pass
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
            cfg = self._config_for(key, state)
            adapter = self._adapter_for(key)
            from .golden_fibo.engine import GoldenFiboEngine
            engine = GoldenFiboEngine(
                cfg,
                state,
                adapter,
                self._client_id_factory(key),
            )
            pre_reconcile_status = state.status

        # Run the tick OUTSIDE the lock so callers can read/list concurrently.
        if pre_reconcile_status == STATUS_NEEDS_RECOVERY:
            # Explicit recovery path: only when the pending logical ladder
            # order is still persisted with a durable identity AND the
            # fallback-aware venue lookup proves it FILLED. No normal tick
            # mutations, no START, no Step0 creation.
            result = engine.reconcile_needs_recovery_pending_fill([])
        else:
            result = engine.tick()
        with self._lock:
            # The engine mutates state in place; persist.
            self._states[key] = result.state
            # Smooth shutdown finished: deregister after this tick.
            if result.state.status == STATUS_COMPLETED:
                self._states.pop(key, None)
                self._save_state()
                logger.info("smooth_shutdown complete; deregistered %s", key)
                return
            self._save_state()
            # Read through-after-mutation fields.
            s = self._states.get(key)
            if s is None:
                return
            # If Step0 (entry) is FILLED and we have a position, the
            # service must call confirm_step0_filled with the actual
            # fill price (read from the venue via resolve_instrument /
            # position_state) BEFORE the next tick.
            if (
                s.pending_order_role == ROLE_ENTRY
                and s.next_step == 0
            ):
                # Snapshot: don't recurse infinitely. The next tick
                # will pick up the confirmed state. The service's
                # _maybe_confirm_step0 now handles both the case where
                # pending_order_exchange_id is set (get_order_state path)
                # and where it is None (position delta path).
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

        Normal Lighter Step0 confirmation path:
          persisted client_order_index
          -> get_order_state (when exchange_order_id is known)
          -> get_order_state_by_client_id (when exchange_order_id is None)
          -> confirm FILLED
          -> backfill exchange order_index
          -> recover authoritative P0 (native actual_fill_price, else
             filled_quote / filled_base)
          -> persist confirmed state and place TP + Step1.

        NEVER resubmits Step0. If the order cannot be found after a
        bounded number of ticks, NEEDS_RECOVERY is set.
        """
        with self._lock:
            state = self._states.get(key)
            if state is None or state.pending_order_role != ROLE_ENTRY:
                return
            exchange_order_id = state.pending_order_exchange_id
            client_id = state.submission_client_id or state.pending_order_client_id

        order_state = None

        # Path A: exchange_order_id known -> get_order_state.
        if exchange_order_id is not None:
            try:
                order_state = self._adapter_for(key).get_order_state(
                    state.account, int(exchange_order_id)
                )
            except Exception as exc:
                logger.warning("step0 get_order_state failed for %s: %s", key, exc)
                return

        # Path B: no exchange_order_id (Lighter market orders) ->
        # look up by persisted client_order_index. This is the normal
        # Lighter Step0 confirmation path. Bounded read-after-write.
        if not order_state and client_id is not None:
            attempts = getattr(self, "_step0_lookup_attempts", {}).get(key, 0)
            if attempts >= 8:  # ~8 poll ticks of read-after-write
                with self._lock:
                    state = self._states.get(key)
                    if state is None:
                        return
                    state.status = STATUS_NEEDS_RECOVERY
                    state.freeze_reason = (
                        f"Step0 order with client_order_index={client_id} "
                        f"not found in active/inactive surface after bounded retry"
                    )
                    self._save_state()
                return
            try:
                order_state = self._adapter_for(key).get_order_state_by_client_id(
                    state.account, state.instrument, int(client_id)
                )
            except Exception as exc:
                logger.warning("step0 get_order_state_by_client_id failed for %s: %s", key, exc)
                return
            if not hasattr(self, "_step0_lookup_attempts"):
                self._step0_lookup_attempts = {}
            self._step0_lookup_attempts[key] = attempts + 1

        if not order_state:
            # Not found yet — wait for the next poll tick (bounded).
            return

        # Ownership verification: client_order_index must match.
        rec_client = order_state.get("client_order_index")
        try:
            rec_client_int = int(rec_client) if rec_client is not None else None
        except (TypeError, ValueError):
            rec_client_int = None
        if client_id is not None and rec_client_int is not None and rec_client_int != int(client_id):
            with self._lock:
                state = self._states.get(key)
                if state is None:
                    return
                state.status = STATUS_NEEDS_RECOVERY
                state.freeze_reason = (
                    f"client_order_index mismatch: expected {client_id}, "
                    f"venue returned {rec_client_int}"
                )
                self._save_state()
            return

        # Side verification.
        expected_side = "buy" if state.direction == "BUY" else "sell"
        rec_side = str(order_state.get("side") or "").lower()
        if rec_side and rec_side != expected_side:
            with self._lock:
                state = self._states.get(key)
                if state is None:
                    return
                state.status = STATUS_NEEDS_RECOVERY
                state.freeze_reason = (
                    f"Step0 side mismatch: expected {expected_side}, venue {rec_side}"
                )
                self._save_state()
            return

        expected_size = Decimal(str(state.step0_volume or "0"))
        filled_size_raw = order_state.get("filled_size") or order_state.get("requested_size")
        try:
            filled_size = Decimal(str(filled_size_raw)) if filled_size_raw is not None else None
        except Exception:
            filled_size = None

        status = str(order_state.get("status") or "")
        taxonomy = str(order_state.get("taxonomy") or "")

        # ACTIVE: keep waiting (do NOT resubmit).
        if taxonomy == "ACTIVE":
            return

        # Terminal non-fill: NEEDS_RECOVERY, never resubmit.
        if taxonomy in ("CANCELED", "REJECTED", "EXPIRED"):
            with self._lock:
                state = self._states.get(key)
                if state is None:
                    return
                state.status = STATUS_NEEDS_RECOVERY
                state.freeze_reason = f"Step0 order {taxonomy.lower()}"
                self._save_state()
            return

        # FILLED only.
        if taxonomy != "FILLED" and status != "filled":
            return

        # Backfill exchange order_index discovered via client-id lookup.
        backfilled_oid = order_state.get("exchange_order_id")

        # P0: prefer native actual_fill_price; else filled_quote / filled_base.
        p0: Optional[Decimal] = None
        afp = order_state.get("actual_fill_price")
        if afp is not None:
            try:
                p0 = Decimal(str(afp))
            except Exception:
                p0 = None
        if (p0 is None or p0 <= 0) and filled_size is not None and filled_size > 0:
            fq = order_state.get("filled_quote")
            try:
                fq_dec = Decimal(str(fq)) if fq is not None else None
            except Exception:
                fq_dec = None
            if fq_dec is not None and fq_dec > 0:
                p0 = fq_dec / filled_size

        if p0 is None or p0 <= 0:
            with self._lock:
                state = self._states.get(key)
                if state is None:
                    return
                state.status = STATUS_NEEDS_RECOVERY
                state.freeze_reason = "could not establish Step0 fill price from order record"
                self._save_state()
            return

        # Size verification: filled size must be >= expected step0.
        if filled_size is not None and expected_size > 0 and filled_size < expected_size:
            with self._lock:
                state = self._states.get(key)
                if state is None:
                    return
                state.status = STATUS_NEEDS_RECOVERY
                state.freeze_reason = (
                    f"Step0 filled size {filled_size} < expected {expected_size}"
                )
                self._save_state()
            return

        # Promote P0, backfill exchange identity, place TP + Step1.
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return
            # Reset the bounded-lookup counter on success.
            if hasattr(self, "_step0_lookup_attempts"):
                self._step0_lookup_attempts.pop(key, None)
            from .golden_fibo.engine import GoldenFiboEngine
            cfg = self._config_for(key, state)
            adapter = self._adapter_for(key)
            engine = GoldenFiboEngine(cfg, state, adapter, self._client_id_factory(key))
            if backfilled_oid is not None:
                try:
                    engine.state.pending_order_exchange_id = int(backfilled_oid)
                    engine.state.submission_exchange_order_id = int(backfilled_oid)
                except (TypeError, ValueError):
                    pass
            engine.confirm_step0_filled(p0)
            result = engine.place_step0_tp_and_step1(p0)
            self._states[key] = engine.state
            self._save_state()
            if result is not None:
                # Engine froze — propagate
                self._states[key] = result.state
                self._save_state()
        return

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
            if op == "smooth_shutdown":
                return self._cmd_smooth_shutdown(command)
            if op == "emergency_stop":
                return self._cmd_emergency_stop(command)
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

        # ---- GoldenFibo Lighter preflight (BEFORE any exchange mutation) ----
        # Validates the full proposed ladder (Step0 MARKET + Step1..Step20
        # LIMIT) against venue base-size, price-increment, and minimum-quote
        # constraints. Never changes the requested volume; either accepts or
        # rejects with a reported safe minimum. Read-only venue calls only.
        preflight_reject = self._golden_fibo_preflight(
            exchange, account, instrument, direction, key, pct, v0
        )
        if preflight_reject is not None:
            return {"ok": False, **preflight_reject}

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
                    "shutdown_mode": getattr(state, "shutdown_mode", "") or "",
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

    def _cmd_smooth_shutdown(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """Finish the CURRENT cycle normally; never start a new Step0 afterward.

        Durable: sets status=smooth_shutdown + shutdown_mode=smooth so restarts
        resume management and still refuse a fresh cycle after TP exit.
        """
        key = str(command.get("registration_key") or "").strip()
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return {"ok": False, "error": "NOT_FOUND", "registration_key": key}
            if state.status == STATUS_QUARANTINED_OLD_STRATEGY:
                return {"ok": False, "error": "OLD_STRATEGY_REGISTRATION", "registration_key": key}
            if state.status == STATUS_STOPPING and (getattr(state, "shutdown_mode", "") or "") == SHUTDOWN_MODE_EMERGENCY:
                return {
                    "ok": False,
                    "error": "EMERGENCY_STOP_IN_PROGRESS",
                    "registration_key": key,
                }

            adapter = self._adapter_for(key)
            # Snapshot venue (read-only) for already-flat special case
            try:
                position = adapter.position_state(state.account, state.instrument)
            except Exception as exc:
                return {
                    "ok": False,
                    "error": "VENUE_READ_FAILED",
                    "detail": str(exc),
                    "registration_key": key,
                }
            live_size = Decimal(str(position.get("size") or "0"))
            live_side = position.get("side")
            has_position = live_size > 0 and live_side in ("long", "short")
            has_pending = state.pending_order_exchange_id is not None
            has_tp = state.current_tp_order_id is not None

            if not has_position and not has_pending and not has_tp:
                # Already clean — immediate deregister, no Step0
                self._states.pop(key, None)
                self._save_state()
                return {
                    "ok": True,
                    "registration_key": key,
                    "status": "stopped",
                    "mode": "smooth",
                    "immediate": True,
                    "detail": "already flat and order-free; deregistered without new Step0",
                }

            if not has_position and has_pending:
                # Flat + orphan pending → cancel owned pending, then deregister
                try:
                    adapter.cancel_order(
                        account=state.account,
                        order_index=int(state.pending_order_exchange_id),
                    )
                except Exception as exc:
                    state.status = STATUS_NEEDS_RECOVERY
                    state.freeze_reason = f"smooth_shutdown orphan cancel failed: {exc}"
                    self._save_state()
                    return {
                        "ok": False,
                        "error": "NEEDS_RECOVERY",
                        "detail": state.freeze_reason,
                        "registration_key": key,
                    }
                # Best-effort TP cancel if any residual identity
                if state.current_tp_order_id is not None:
                    try:
                        adapter.cancel_order(
                            account=state.account,
                            order_index=int(state.current_tp_order_id),
                        )
                    except Exception:
                        pass
                self._states.pop(key, None)
                self._save_state()
                return {
                    "ok": True,
                    "registration_key": key,
                    "status": "stopped",
                    "mode": "smooth",
                    "immediate": True,
                    "detail": "flat with orphan pending canceled; deregistered",
                }

            # Live cycle in progress — mark durable smooth intent; engine continues
            state.shutdown_mode = SHUTDOWN_MODE_SMOOTH
            state.status = STATUS_SMOOTH_SHUTDOWN
            state.freeze_reason = None
            self._save_state()
        return {
            "ok": True,
            "registration_key": key,
            "status": STATUS_SMOOTH_SHUTDOWN,
            "mode": "smooth",
            "immediate": False,
            "detail": (
                "Smooth Shutdown armed. Current cycle continues until TP closes "
                "the position; no new Step0 will start afterward."
            ),
        }

    def _cmd_emergency_stop(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """Immediately stop strategy progression and clean owned venue exposure.

        Ownership-gated: only cancels tracked pending/TP IDs and closes a
        position whose side matches the registration direction. On ambiguity
        freezes NEEDS_RECOVERY without guessing.
        """
        key = str(command.get("registration_key") or "").strip()
        actions: List[str] = []

        def _read_position_bounded(adapter: Any, account: str, instrument: str, label: str) -> Dict[str, Any]:
            """Bounded read with Arcus 429-aware waits; never invents state."""
            last_exc: Optional[BaseException] = None
            for attempt in range(1, 6):
                try:
                    return adapter.position_state(account, instrument)
                except Exception as exc:
                    last_exc = exc
                    msg = str(exc)
                    is_429 = "429" in msg or "Too Many Requests" in msg
                    actions.append(f"{label} position_state attempt={attempt} err={exc}")
                    if not is_429 or attempt >= 5:
                        break
                    delay = min(20.0, 1.5 * (2 ** (attempt - 1)))
                    # Prefer Arcus gate remaining if available.
                    try:
                        if str(getattr(adapter, "name", "")).find("arcus") >= 0 or "arcus" in key:
                            from .agents import x_arcus_agent as _arcus_mod

                            delay = max(delay, float(_arcus_mod.arcus_http_backoff_remaining()) + 0.2)
                    except Exception:
                        pass
                    time.sleep(delay)
            raise RuntimeError(str(last_exc) if last_exc else "position_state failed")

        with self._lock:
            state = self._states.get(key)
            if state is None:
                return {"ok": False, "error": "NOT_FOUND", "registration_key": key}
            if state.status == STATUS_QUARANTINED_OLD_STRATEGY:
                return {"ok": False, "error": "OLD_STRATEGY_REGISTRATION", "registration_key": key}

            # 1) Stop progression first
            state.shutdown_mode = SHUTDOWN_MODE_EMERGENCY
            state.status = STATUS_STOPPING
            state.freeze_reason = "emergency_stop_in_progress"
            self._save_state()
            adapter = self._adapter_for(key)
            account = state.account
            instrument = state.instrument
            direction = state.direction
            pending_oid = state.pending_order_exchange_id
            tp_oid = state.current_tp_order_id
            expected_side = "long" if direction == "BUY" else "short"

        # Work outside lock for venue I/O
        try:
            position = _read_position_bounded(adapter, account, instrument, "initial")
        except Exception as exc:
            with self._lock:
                st = self._states.get(key)
                if st is not None:
                    # Keep STOPPING + emergency mode so poll does not resume hammering.
                    st.status = STATUS_STOPPING
                    st.shutdown_mode = SHUTDOWN_MODE_EMERGENCY
                    st.freeze_reason = f"emergency_stop position_state failed: {exc}"
                    self._save_state()
            return {
                "ok": False,
                "error": "NEEDS_RECOVERY",
                "detail": f"position_state failed: {exc}",
                "registration_key": key,
                "actions": actions,
            }

        live_size = Decimal(str(position.get("size") or "0"))
        live_side = position.get("side")
        has_position = live_size > 0 and live_side in ("long", "short")

        if has_position and live_side != expected_side:
            with self._lock:
                st = self._states.get(key)
                if st is not None:
                    st.status = STATUS_NEEDS_RECOVERY
                    st.freeze_reason = (
                        f"OWNERSHIP_MISMATCH: live side={live_side} "
                        f"registration direction={direction}"
                    )
                    self._save_state()
            return {
                "ok": False,
                "error": "OWNERSHIP_MISMATCH",
                "detail": f"live side {live_side} != expected {expected_side}",
                "registration_key": key,
            }

        # Helpers -----------------------------------------------------------
        def _order_absent_or_terminal(adapter: Any, account: str, oid: int) -> bool:
            """True if order is gone or not ACTIVE (idempotent cancel success)."""
            try:
                st_ord = adapter.get_order_state(account, int(oid)) or {}
            except Exception:
                st_ord = {}
            if not st_ord:
                return True
            tax = str(st_ord.get("taxonomy") or "").upper()
            status = str(st_ord.get("status") or "").upper()
            if tax in ("ACTIVE",):
                return False
            if status in ("OPEN", "NEW", "LIVE", "UNTRIGGERED", "PARTIALLY_FILLED"):
                # Some venues leave taxonomy empty while still open.
                if tax in ("", "UNKNOWN") and status in ("OPEN", "NEW", "LIVE", "UNTRIGGERED", "PARTIALLY_FILLED"):
                    return False
            return True

        def _cancel_owned(adapter: Any, account: str, oid: Optional[int], label: str) -> bool:
            if oid is None:
                return True
            try:
                ok = bool(adapter.cancel_order(account=account, order_index=int(oid)))
            except Exception as exc:
                actions.append(f"cancel_{label} oid={oid} exc={exc}")
                ok = False
            if ok:
                actions.append(f"cancel_{label} oid={oid} ok=True")
                return True
            # Idempotent: already gone / filled / canceled
            if _order_absent_or_terminal(adapter, account, int(oid)):
                actions.append(f"cancel_{label} oid={oid} idempotent_absent=True")
                return True
            actions.append(f"cancel_{label} oid={oid} ok=False still_active")
            return False

        def _wait_until_flat(
            adapter: Any,
            account: str,
            instrument: str,
            *,
            total_timeout: float = 75.0,
        ) -> tuple[bool, Dict[str, Any]]:
            """Read-only wait for flat. Never submits close. Honors 429 backoff."""
            delays = [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0, 10.0, 10.0]
            deadline = time.time() + float(total_timeout)
            last_pos: Dict[str, Any] = {}
            for i, dly in enumerate(delays):
                if time.time() >= deadline:
                    break
                try:
                    # Prefer Arcus gate remaining
                    try:
                        if "arcus" in key or "arcus" in str(getattr(adapter, "name", "")).lower():
                            from .agents import x_arcus_agent as _arcus_mod
                            wait_b = float(_arcus_mod.arcus_http_backoff_remaining())
                            if wait_b > 0.05:
                                actions.append(f"flat_wait backoff={wait_b:.1f}s")
                                time.sleep(min(wait_b + 0.2, max(0.0, deadline - time.time())))
                    except Exception:
                        pass
                    last_pos = _read_position_bounded(adapter, account, instrument, f"flat_wait_{i}")
                    sz = Decimal(str(last_pos.get("size") or "0"))
                    side = last_pos.get("side")
                    open_ = sz > 0 and side in ("long", "short")
                    actions.append(f"flat_wait i={i} size={sz} side={side}")
                    if not open_:
                        return True, last_pos
                except Exception as exc:
                    actions.append(f"flat_wait i={i} err={exc}")
                    # 429 etc: continue until deadline
                remain = deadline - time.time()
                if remain <= 0:
                    break
                time.sleep(min(dly, remain))
            return False, last_pos

        # 3) Cancel owned pending ladder (idempotent)
        if pending_oid is not None:
            if not _cancel_owned(adapter, account, pending_oid, "pending"):
                # Not fatal yet — close may still flatten; continue
                actions.append("pending cancel inconclusive; continuing to close/verify")

        # Re-read position after cancel (partial fill may have left size)
        try:
            position = _read_position_bounded(adapter, account, instrument, "post_cancel")
        except Exception as exc:
            with self._lock:
                st = self._states.get(key)
                if st is not None:
                    st.status = STATUS_STOPPING
                    st.shutdown_mode = SHUTDOWN_MODE_EMERGENCY
                    st.freeze_reason = f"emergency_stop re-read position failed: {exc}"
                    self._save_state()
            return {
                "ok": False,
                "error": "NEEDS_RECOVERY",
                "detail": str(exc),
                "registration_key": key,
                "actions": actions,
            }
        live_size = Decimal(str(position.get("size") or "0"))
        live_side = position.get("side")
        has_position = live_size > 0 and live_side in ("long", "short")

        if has_position and live_side != expected_side:
            with self._lock:
                st = self._states.get(key)
                if st is not None:
                    st.status = STATUS_NEEDS_RECOVERY
                    st.freeze_reason = (
                        f"OWNERSHIP_MISMATCH after cancel: live side={live_side}"
                    )
                    self._save_state()
            return {
                "ok": False,
                "error": "OWNERSHIP_MISMATCH",
                "registration_key": key,
                "actions": actions,
            }

        # 4) Close owned position at actual live size — exactly once
        already_submitted = False
        close_client_id = None
        with self._lock:
            st0 = self._states.get(key)
            if st0 is not None and str(st0.emergency_close_phase or "") == "submitted":
                already_submitted = True
                close_client_id = st0.emergency_close_client_id
                actions.append(
                    f"resume_emergency_close_submitted client_order_id={close_client_id}"
                )

        if has_position and not already_submitted:
            try:
                from .golden_fibo.client_id_v2 import (
                    ROLE_EMERGENCY_CLOSE as V2_ROLE_EMERGENCY_CLOSE,
                    STEP_UNKNOWN,
                    ClientIdError,
                    allocate_client_id,
                )
                with self._lock:
                    st = self._states.get(key)
                    if st is not None and int(getattr(st, "client_id_version", 2) or 2) >= 2:
                        # Reuse existing emergency close id if already allocated
                        if st.emergency_close_client_id is not None:
                            close_client_id = int(st.emergency_close_client_id)
                        else:
                            if not st.cycle_uid:
                                from .golden_fibo.client_id_v2 import allocate_cycle_uid
                                prev = int(st.highest_cycle_uid or 0) or None
                                uid = allocate_cycle_uid(previous_local_cycle_uid=prev)
                                st.cycle_uid = uid
                                st.highest_cycle_uid = max(int(st.highest_cycle_uid or 0), uid)
                                st.client_seq_by_role_step = dict(st.client_seq_by_role_step or {})
                            step_ec = int(st.highest_filled_step)
                            if step_ec < 0:
                                step_ec = STEP_UNKNOWN
                            close_client_id = allocate_client_id(
                                direction=st.direction or direction,
                                role=V2_ROLE_EMERGENCY_CLOSE,
                                cycle_uid=int(st.cycle_uid),
                                step=int(step_ec),
                                seq_map=st.client_seq_by_role_step,
                            )
                            st.emergency_close_client_id = int(close_client_id)
                        self._save_state()
            except ClientIdError as exc:
                with self._lock:
                    st = self._states.get(key)
                    if st is not None:
                        st.status = STATUS_STOPPING
                        st.shutdown_mode = SHUTDOWN_MODE_EMERGENCY
                        st.freeze_reason = f"emergency_stop client_id allocation failed: {exc}"
                        self._save_state()
                return {
                    "ok": False,
                    "error": "NEEDS_RECOVERY",
                    "detail": str(exc),
                    "registration_key": key,
                    "actions": actions,
                }
            except Exception:
                close_client_id = None  # fall back to agent time_ns id
            try:
                close_kw = {"account": account, "instrument": instrument}
                if close_client_id is not None:
                    close_kw["client_order_id"] = int(close_client_id)
                close_res = adapter.close_position(**close_kw)
                actions.append(f"close_position result={close_res} client_order_id={close_client_id}")
                submitted_ok = bool(close_res.get("success")) or bool(close_res.get("verified"))
                if not submitted_ok:
                    if close_res.get("error") not in ("POSITION_NOT_FOUND",):
                        with self._lock:
                            st = self._states.get(key)
                            if st is not None:
                                st.status = STATUS_STOPPING
                                st.shutdown_mode = SHUTDOWN_MODE_EMERGENCY
                                st.freeze_reason = f"emergency_stop close failed: {close_res}"
                                self._save_state()
                        return {
                            "ok": False,
                            "error": "NEEDS_RECOVERY",
                            "detail": f"close_position failed: {close_res}",
                            "registration_key": key,
                            "actions": actions,
                        }
                # Persist EMERGENCY_CLOSE_SUBMITTED even if verified=False
                with self._lock:
                    st = self._states.get(key)
                    if st is not None:
                        st.emergency_close_phase = "submitted"
                        st.freeze_reason = "EMERGENCY_CLOSE_SUBMITTED"
                        st.status = STATUS_STOPPING
                        st.shutdown_mode = SHUTDOWN_MODE_EMERGENCY
                        if close_client_id is not None:
                            st.emergency_close_client_id = int(close_client_id)
                        ex_oid = None
                        raw = close_res.get("raw") if isinstance(close_res, dict) else None
                        if isinstance(raw, dict):
                            pa = raw.get("position_action") or {}
                            if isinstance(pa, dict):
                                ex_oid = pa.get("exchange_order_id")
                        if ex_oid is None and isinstance(close_res, dict):
                            ex_oid = close_res.get("exchange_order_id")
                        if ex_oid is not None:
                            try:
                                st.emergency_close_exchange_id = int(ex_oid)
                            except (TypeError, ValueError):
                                pass
                        st.emergency_close_submitted_at = time.time()
                        st.emergency_close_pre_size = str(live_size)
                        self._save_state()
                actions.append("phase=EMERGENCY_CLOSE_SUBMITTED")
            except Exception as exc:
                with self._lock:
                    st = self._states.get(key)
                    if st is not None:
                        st.status = STATUS_STOPPING
                        st.shutdown_mode = SHUTDOWN_MODE_EMERGENCY
                        st.freeze_reason = f"emergency_stop close exception: {exc}"
                        self._save_state()
                return {
                    "ok": False,
                    "error": "NEEDS_RECOVERY",
                    "detail": str(exc),
                    "registration_key": key,
                    "actions": actions,
                }

        # 5) Patient flat verification (no second close)
        flat, position = _wait_until_flat(adapter, account, instrument, total_timeout=75.0)
        if not flat:
            live_size = Decimal(str((position or {}).get("size") or "0"))
            with self._lock:
                st = self._states.get(key)
                if st is not None:
                    st.status = STATUS_STOPPING
                    st.shutdown_mode = SHUTDOWN_MODE_EMERGENCY
                    st.freeze_reason = (
                        f"emergency_stop incomplete: position not confirmed flat "
                        f"size={live_size} (close may still be settling)"
                    )
                    # Keep emergency_close_phase=submitted so retry won't double-close
                    if not st.emergency_close_phase:
                        st.emergency_close_phase = "submitted"
                    self._save_state()
            return {
                "ok": False,
                "error": "NEEDS_RECOVERY",
                "detail": f"position not confirmed flat size={live_size}",
                "registration_key": key,
                "actions": actions,
            }

        with self._lock:
            st = self._states.get(key)
            if st is not None:
                st.emergency_close_phase = "verified"
                st.freeze_reason = "EMERGENCY_CLOSE_VERIFIED_FLAT"
                self._save_state()
        actions.append("flat_confirmed")

        # 6) TP cleanup AFTER flat (position TP often auto-gone)
        if tp_oid is not None:
            if not _cancel_owned(adapter, account, tp_oid, "tp"):
                # Re-check once after short wait
                time.sleep(1.0)
                if not _order_absent_or_terminal(adapter, account, int(tp_oid)):
                    with self._lock:
                        st = self._states.get(key)
                        if st is not None:
                            st.status = STATUS_STOPPING
                            st.shutdown_mode = SHUTDOWN_MODE_EMERGENCY
                            st.freeze_reason = (
                                f"emergency_stop incomplete: tp oid={tp_oid} still ACTIVE"
                            )
                            self._save_state()
                    return {
                        "ok": False,
                        "error": "NEEDS_RECOVERY",
                        "detail": f"tp still ACTIVE oid={tp_oid}",
                        "registration_key": key,
                        "actions": actions,
                    }
                actions.append(f"cancel_tp oid={tp_oid} gone_after_wait")

        # Also ensure pending gone
        if pending_oid is not None and not _order_absent_or_terminal(adapter, account, int(pending_oid)):
            _cancel_owned(adapter, account, pending_oid, "pending_final")
            if not _order_absent_or_terminal(adapter, account, int(pending_oid)):
                with self._lock:
                    st = self._states.get(key)
                    if st is not None:
                        st.status = STATUS_STOPPING
                        st.shutdown_mode = SHUTDOWN_MODE_EMERGENCY
                        st.freeze_reason = (
                            f"emergency_stop incomplete: pending oid={pending_oid} still ACTIVE"
                        )
                        self._save_state()
                return {
                    "ok": False,
                    "error": "NEEDS_RECOVERY",
                    "detail": f"pending still ACTIVE oid={pending_oid}",
                    "registration_key": key,
                    "actions": actions,
                }

        # 7) Deregister
        with self._lock:
            self._states.pop(key, None)
            self._save_state()
        actions.append("deregistered")
        return {
            "ok": True,
            "registration_key": key,
            "status": "stopped",
            "mode": "emergency",
            "actions": actions,
        }

    def _golden_fibo_preflight(
        self,
        exchange: str,
        account: str,
        instrument: str,
        direction: str,
        key: str,
        percentage: Decimal,
        step0_volume: Decimal,
    ) -> Optional[Dict[str, Any]]:
        """GoldenFibo Lighter preflight. Read-only. Returns an error dict on
        rejection, or None when the proposed ladder is venue-valid.

        Validates Step0 base size plus the full Step1..Step20 ladder against
        base-size, price-increment, and minimum-quote constraints. The TP
        uses the dedicated set_tp primitive, so the ordinary LIMIT min-quote
        rule is applied to the resting LIMIT ladder (Step1+), NOT to the TP.
        """
        from .golden_fibo.preflight import golden_fibo_lighter_preflight

        adapter = self._adapter_for(key)
        # Venue constraints (read-only).
        try:
            constraints = adapter.get_venue_constraints(account, instrument)
        except Exception as exc:
            logger.warning("golden-fibo preflight constraints read failed for %s: %s", key, exc)
            return None  # fail-open: do not block START on a read failure
        if not constraints:
            return None
        # Market price for the P0 estimate (read-only).
        try:
            mp = adapter.market_price(account, instrument)
        except Exception as exc:
            logger.warning("golden-fibo preflight market price read failed for %s: %s", key, exc)
            return None
        est_p0_raw = (mp or {}).get("mark_price") or (mp or {}).get("last_external_price")
        try:
            est_p0 = Decimal(str(est_p0_raw)) if est_p0_raw is not None else None
        except Exception:
            est_p0 = None
        if est_p0 is None or est_p0 <= 0:
            return None

        min_base = Decimal(str(constraints.get("min_base_amount") or "0"))
        min_quote = Decimal(str(constraints.get("min_quote_amount") or "0"))
        size_dec = int(constraints.get("size_decimals") or 0)
        price_dec = int(constraints.get("price_decimals") or 0)

        result = golden_fibo_lighter_preflight(
            direction=direction,
            percentage=percentage,
            step0_volume=step0_volume,
            estimated_p0=est_p0,
            min_base_amount=min_base,
            min_quote_amount=min_quote,
            size_decimals=size_dec,
            price_decimals=price_dec,
        )
        if result.ok:
            return None
        rejection = {
            "error": result.error,
            "detail": result.detail,
            "estimated_p0": None if result.estimated_p0 is None else str(result.estimated_p0),
            "estimated_tp0": None if result.estimated_tp0 is None else str(result.estimated_tp0),
            "estimated_p1": None if result.estimated_p1 is None else str(result.estimated_p1),
            "min_quote_amount": None if result.min_quote_amount is None else str(result.min_quote_amount),
            "safe_min_step0_volume": None if result.safe_min_step0_volume is None else str(result.safe_min_step0_volume),
            "failing_step": result.failing_step,
            "percentage": None if result.percentage is None else str(result.percentage),
        }
        if result.failing_raw_price is not None:
            rejection["raw_price"] = str(result.failing_raw_price)
        # For non-positive ladder price, also report the maximum percentage
        # that keeps the full Step1..20 ladder positive at the current price.
        if result.error == "LADDER_PRICE_NON_POSITIVE":
            try:
                from .golden_fibo.preflight import compute_max_positive_ladder_percentage
                max_pct = compute_max_positive_ladder_percentage(
                    direction=direction, estimated_p0=est_p0
                )
                rejection["max_positive_percentage"] = str(max_pct)
            except Exception:  # noqa: BLE001
                pass
        return rejection

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
# Socket server + client (IPC control plane)
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


class FiboSocketClient:
    """Unix-socket IPC client used by the Telegram /fibo wizard (gateway).

    This is the ONLY production service handle the gateway may hold.
    It never constructs PersistentFiboService and never starts a poll
    thread. If fibo.service / the socket is down, every command returns
    ``{"ok": False, "error": "SERVICE_UNAVAILABLE", ...}`` with ZERO
    exchange side effects.
    """

    def __init__(
        self,
        socket_path: Optional[Path] = None,
        *,
        timeout: float = _DEFAULT_SOCKET_TIMEOUT,
    ) -> None:
        self.socket_path = Path(socket_path or resolve_fibo_socket_path())
        self.timeout = float(timeout)

    def ping(self) -> bool:
        """True if the daemon socket accepts a connection right now."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(str(self.socket_path))
            return True
        except OSError:
            return False

    def execute_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        path = self.socket_path
        if not path.exists():
            return {
                "ok": False,
                "error": "SERVICE_UNAVAILABLE",
                "detail": (
                    f"fibo.service is not reachable (socket missing: {path}). "
                    "Start/enable fibo.service before using /fibo START, "
                    "Running, or STOP. Opening the menu does not start the robot."
                ),
            }
        try:
            payload = json.dumps(command, ensure_ascii=False, default=str).encode("utf-8") + b"\n"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(str(path))
                sock.sendall(payload)
                # One JSON response line
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            if not buf.strip():
                return {
                    "ok": False,
                    "error": "SERVICE_UNAVAILABLE",
                    "detail": f"empty response from fibo.service at {path}",
                }
            try:
                resp = json.loads(buf.decode("utf-8").strip())
            except json.JSONDecodeError as exc:
                return {
                    "ok": False,
                    "error": "SERVICE_UNAVAILABLE",
                    "detail": f"invalid JSON from fibo.service: {exc}",
                }
            if not isinstance(resp, dict):
                return {
                    "ok": False,
                    "error": "SERVICE_UNAVAILABLE",
                    "detail": "non-object response from fibo.service",
                }
            return resp
        except (ConnectionRefusedError, FileNotFoundError, TimeoutError, OSError) as exc:
            return {
                "ok": False,
                "error": "SERVICE_UNAVAILABLE",
                "detail": (
                    f"fibo.service unreachable at {path}: {exc}. "
                    "The Telegram gateway will not run the trading poll loop."
                ),
            }


# ---------------------------------------------------------------------------
# Singleton accessor — GATEWAY/UI uses IPC client ONLY
# ---------------------------------------------------------------------------
_service_singleton: Optional[FiboServiceProtocol] = None
_service_lock = threading.Lock()


def get_fibo_service() -> FiboServiceProtocol:
    """Return the process-wide **IPC client** to fibo.service.

    CRITICAL control-plane rule (2026-08-19):
    - Telegram gateway / /fibo wizard MUST use this client.
    - This function MUST NEVER construct PersistentFiboService.
    - This function MUST NEVER start ``golden-fibo-poll``.
    - Trading ownership lives exclusively in fibo_daemon / fibo.service.

    Tests that need an in-process engine construct
    ``PersistentFiboService(start_thread=False, ...)`` directly and
    inject it into FiboWizard(service=...).
    """
    global _service_singleton
    with _service_lock:
        if _service_singleton is None:
            _service_singleton = FiboSocketClient()
        return _service_singleton


def _reset_fibo_service() -> None:
    """Drop the gateway IPC client singleton (tests / process cleanup)."""
    global _service_singleton
    with _service_lock:
        # Client has no poll thread; nothing to shut down.
        _service_singleton = None
