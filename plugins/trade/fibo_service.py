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
from typing import Any, Callable, Dict, List, Optional, Protocol

from .tradedesk import TradeDesk, get_tradedesk
from .fibo.engine import CounterType, FiboInstance, FiboManager, step0_tp, step_price
from .fibo.runner import FiboLiveRunner, JsonlLogSink, RegistrationSpec, RuntimeBundle, default_runtime_registry

logger = logging.getLogger(__name__)

_DEFAULT_POLL_SECONDS = 2.0
_DEFAULT_SOCKET_TIMEOUT = 15.0


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


class FiboServiceProtocol(Protocol):
    def execute_command(self, command: Dict[str, Any]) -> Dict[str, Any]: ...


@dataclass
class RegistrationContext:
    spec: RegistrationSpec
    bundle: RuntimeBundle
    started_at: float
    preflight: Dict[str, Any] = field(default_factory=dict)
    service_status: str = "running"
    status_reason: str = ""
    cleanup_details: Dict[str, Any] = field(default_factory=dict)
    stop_requested_at: Optional[float] = None
    last_known_cumulative_volume: float = 0.0


class FiboCycleLedger:
    """Best-effort cycle/performance ledger.

    Ledger/reporting must never interrupt engine processing. It persists raw
    event rows plus small in-memory summaries keyed by registration.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or resolve_fibo_ledger_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._summaries: Dict[str, Dict[str, Any]] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    self._ingest_loaded_row(row)
        except Exception:
            logger.exception("Failed loading Fibo ledger")

    def _summary_for(self, registration_key: str) -> Dict[str, Any]:
        return self._summaries.setdefault(
            registration_key,
            {
                "completed_cycles": 0,
                "profitable_cycles": 0,
                "losing_cycles": 0,
                "total_realized_pnl": None,
                "fees": None,
                "last_run_id": None,
                "last_cycle": None,
            },
        )

    def _extract_cycle_info(self, row: Dict[str, Any]) -> tuple[Optional[str], Optional[int]]:
        client_order_id = str(row.get("client_order_id") or "").strip()
        if not client_order_id:
            return None, None
        run_id = None
        cycle = None
        parts = client_order_id.split("_")
        for part in parts:
            if len(part) == 4 and part.isalnum() and part.isupper():
                run_id = part
            if part.startswith("Y") and part[1:].isdigit():
                cycle = int(part[1:])
        return run_id, cycle

    def _ingest_loaded_row(self, row: Dict[str, Any]) -> None:
        key = str(row.get("registration_key") or "").strip()
        if not key:
            return
        summary = self._summary_for(key)
        event = str(row.get("event") or "")
        run_id, cycle = self._extract_cycle_info(row)
        if run_id:
            summary["last_run_id"] = run_id
        if cycle is not None:
            summary["last_cycle"] = cycle
        if event == "cycle_completed":
            summary["completed_cycles"] = max(summary["completed_cycles"], int(row.get("completed_cycles") or 0))
        if event == "performance_snapshot":
            summary["profitable_cycles"] = int(row.get("profitable_cycles") or 0)
            summary["losing_cycles"] = int(row.get("losing_cycles") or 0)
            summary["total_realized_pnl"] = row.get("total_realized_pnl")
            summary["fees"] = row.get("fees")

    def record_event(self, row: Dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            self._ingest_loaded_row(row)
        except Exception:
            logger.exception("Fibo ledger write failed")

    def note_cycle_cleanup(self, registration_key: str) -> None:
        summary = self._summary_for(registration_key)
        summary["completed_cycles"] += 1
        self.record_event(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "event": "cycle_completed",
                "registration_key": registration_key,
                "completed_cycles": summary["completed_cycles"],
            }
        )

    def summary(self, registration_key: str) -> Dict[str, Any]:
        return dict(self._summary_for(registration_key))


class PersistentFiboService:
    def __init__(
        self,
        *,
        tradedesk: Optional[TradeDesk] = None,
        runner: Optional[FiboLiveRunner] = None,
        runtime_registry: Optional[Dict[str, Callable[[RegistrationSpec], RuntimeBundle]]] = None,
        state_path: Path | None = None,
        ledger: Optional[FiboCycleLedger] = None,
        event_log_path: Path | None = None,
        start_thread: bool = True,
    ) -> None:
        self._desk = tradedesk or get_tradedesk()
        self._state_path = Path(state_path or resolve_fibo_state_path())
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._ledger = ledger or FiboCycleLedger()
        self._event_log = JsonlLogSink(Path(event_log_path or resolve_fibo_event_log_path()))
        self._lock = threading.RLock()
        self._runtime_registry = runtime_registry or default_runtime_registry()
        self._runner = runner or FiboLiveRunner(
            manager=FiboManager(event_sink=self._safe_event_sink),
            runtime_registry=self._runtime_registry,
            poll_seconds=_DEFAULT_POLL_SECONDS,
            log_sink=self._event_log,
        )
        if runner is not None:
            self._runner.manager._event_sink = self._safe_event_sink  # type: ignore[attr-defined]
        self._contexts: Dict[str, RegistrationContext] = {}
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._load_state()
        if start_thread:
            self._ensure_thread()

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._poll_loop, name="fibo-service", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        self._runner.stop_requested = True
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1)

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                with self._lock:
                    self._runner.manager.poll_once()
                    for instance in self._runner.manager.list_running():
                        self._runner._log(  # noqa: SLF001
                            "poll_state",
                            registration_key=instance.key,
                            step0_raw=instance.cascade.step0_price,
                            highest_step=instance.cascade.highest_step,
                            cumulative_volume=str(instance.cumulative_volume),
                            sl_raw=instance.protection.sl_price,
                            tp_raw=instance.protection.tp_price,
                            frozen=instance.frozen,
                            frozen_reason=instance.frozen_reason,
                            pending_unprotected=instance.pending_unprotected,
                        )
            except Exception:
                logger.exception("Fibo service poll loop failure")
            time.sleep(_DEFAULT_POLL_SECONDS)

    def _safe_event_sink(self, payload: Dict[str, Any]) -> None:
        try:
            self._runner._log(**payload)  # noqa: SLF001
        except Exception:
            logger.exception("Fibo service event log failure")
        try:
            if payload.get("event") == "cycle_cleanup":
                key = str(payload.get("registration_key") or "")
                if key:
                    self._ledger.note_cycle_cleanup(key)
            else:
                row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                row.update(payload)
                self._ledger.record_event(row)
        except Exception:
            logger.exception("Fibo service ledger failure")

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to read fibo service state")
            return
        for row in list(payload.get("registrations") or []):
            try:
                spec = RegistrationSpec(
                    exchange=str(row["exchange"]),
                    account=str(row["account"]),
                    instrument=str(row["instrument"]).upper(),
                    counter_type=CounterType(str(row["counter_side"])),
                    divide_percent=float(row["divide_percent"]),
                    counter1=float(row["counter1"]),
                    counter2=float(row["counter2"]),
                    counter3=float(row["counter3"]),
                    counter4=float(row["counter4"]),
                )
                bundle = self._runner._resolve_runtime(spec)  # noqa: SLF001
                self._runner._attach_runtime_event_sink(bundle)  # noqa: SLF001
                persisted_status = str(row.get("service_status") or "running").strip().lower()
                if persisted_status in {"running", "stopping"}:
                    persisted_status = "needs_recovery"
                self._contexts[spec.key] = RegistrationContext(
                    spec=spec,
                    bundle=bundle,
                    started_at=float(row.get("started_at") or time.time()),
                    preflight=dict(row.get("preflight") or {}),
                    service_status=persisted_status or "needs_recovery",
                    status_reason=str(row.get("status_reason") or "Recovered from fibo.service state; manual recovery required."),
                    cleanup_details=dict(row.get("cleanup_details") or {}),
                    stop_requested_at=float(row["stop_requested_at"]) if row.get("stop_requested_at") is not None else None,
                    last_known_cumulative_volume=float(row.get("last_known_cumulative_volume") or 0.0),
                )
            except Exception:
                logger.exception("Failed to restore fibo registration metadata")

    def _save_state(self) -> None:
        try:
            payload = {
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "registrations": [
                    {
                        "registration_key": key,
                        "exchange": ctx.spec.exchange,
                        "account": ctx.spec.account,
                        "instrument": ctx.spec.instrument,
                        "counter_side": ctx.spec.counter_type.value,
                        "divide_percent": ctx.spec.divide_percent,
                        "counter1": ctx.spec.counter1,
                        "counter2": ctx.spec.counter2,
                        "counter3": ctx.spec.counter3,
                        "counter4": ctx.spec.counter4,
                        "started_at": ctx.started_at,
                        "preflight": ctx.preflight,
                        "service_status": self._status_for(key, ctx, self._instance_for(key)),
                        "status_reason": ctx.status_reason,
                        "cleanup_details": ctx.cleanup_details,
                        "stop_requested_at": ctx.stop_requested_at,
                        "last_known_cumulative_volume": self._last_known_cumulative_volume(key, ctx),
                    }
                    for key, ctx in sorted(self._contexts.items())
                ],
            }
            self._state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("Failed to save fibo service state")

    def execute_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        op = str(command.get("op") or "").strip().lower()
        with self._lock:
            if op == "start":
                return self._command_start(command)
            if op == "list":
                return self._command_list()
            if op == "detail":
                return self._command_detail(command)
            if op == "stop_close":
                return self._command_stop_close(command)
            return {"ok": False, "error": "UNKNOWN_COMMAND", "message": f"Unknown Fibo service op: {op}"}

    def _command_start(self, command: Dict[str, Any]) -> Dict[str, Any]:
        spec = RegistrationSpec(
            exchange=str(command["exchange"]),
            account=str(command["account"]),
            instrument=str(command["instrument"]).upper(),
            counter_type=CounterType(str(command["counter_side"])),
            divide_percent=float(command["divide_percent"]),
            counter1=float(command["counter1"]),
            counter2=float(command["counter2"]),
            counter3=float(command["counter3"]),
            counter4=float(command["counter4"]),
        )
        if spec.key in self._contexts or self._runner.manager.is_running(spec.key):
            return {
                "ok": False,
                "error": "DUPLICATE_REGISTRATION",
                "message": f"Duplicate active registration: {spec.key}",
                "registration_key": spec.key,
            }
        snapshot = self._runner.preflight_registration(spec)
        bundle = self._runner._resolve_runtime(spec)  # noqa: SLF001
        self._runner._attach_runtime_event_sink(bundle)  # noqa: SLF001
        self._runner.manager.start(spec.to_fibo_config(), bundle.adapter, bundle.quote_source)
        self._runner._started_specs[spec.key] = spec  # noqa: SLF001
        self._contexts[spec.key] = RegistrationContext(
            spec=spec,
            bundle=bundle,
            started_at=time.time(),
            preflight={
                "market": snapshot.market,
                "mark_price_raw": snapshot.mark_price_raw,
                "position_count": snapshot.position_count,
                "open_order_count": snapshot.open_order_count,
                "tp": snapshot.tp,
                "sl": snapshot.sl,
                "is_clean": snapshot.is_clean,
            },
            service_status="running",
            last_known_cumulative_volume=float(spec.counter1),
        )
        self._runner._log("registration_started", registration_key=spec.key, poll_seconds=_DEFAULT_POLL_SECONDS)  # noqa: SLF001
        self._save_state()
        return {"ok": True, "registration_key": spec.key}

    def _command_list(self) -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = []
        for key, ctx in sorted(self._contexts.items()):
            instance = self._instance_for(key)
            rows.append(
                {
                    "registration_key": key,
                    "exchange": ctx.spec.exchange,
                    "account": ctx.spec.account,
                    "instrument": ctx.spec.instrument,
                    "counter_side": ctx.spec.counter_type.value,
                    "status": self._status_for(key, ctx, instance),
                    "frozen": bool(instance.frozen) if instance is not None else False,
                }
            )
        return {"ok": True, "registrations": rows}

    def _command_detail(self, command: Dict[str, Any]) -> Dict[str, Any]:
        key = str(command["registration_key"])
        ctx = self._contexts.get(key)
        if ctx is None:
            return {"ok": False, "error": "NOT_FOUND", "registration_key": key}
        instance = self._instance_for(key)
        exchange_state = self._read_exchange_state(ctx)
        metrics = self._ledger.summary(key)
        detail = self._build_detail_payload(ctx, instance, exchange_state, metrics)
        return {"ok": True, "detail": detail}

    def _command_stop_close(self, command: Dict[str, Any]) -> Dict[str, Any]:
        key = str(command["registration_key"])
        ctx = self._contexts.get(key)
        instance = self._instance_for(key)
        if ctx is None:
            return {"ok": False, "error": "NOT_FOUND", "registration_key": key}
        if ctx.service_status == "stopping":
            return {
                "ok": False,
                "error": "STOP_ALREADY_IN_PROGRESS",
                "registration_key": key,
                "message": "STOP_CLOSE is already in progress for this registration.",
            }
        exchange_state = self._read_exchange_state(ctx)
        safety = self._assess_stop_close_safety(ctx, instance, exchange_state)
        if not safety["ok"]:
            return {
                "ok": False,
                "error": safety["error"],
                "message": safety["message"],
                "registration_key": key,
            }
        self._mark_stopping(key, ctx, instance)
        try:
            result = self._execute_stop_close_cleanup(ctx, exchange_state)
        except Exception as exc:  # noqa: BLE001
            logger.exception("STOP_CLOSE cleanup failure")
            result = {
                "ok": False,
                "error": "CLEANUP_FAILED",
                "message": f"STOP_CLOSE cleanup raised: {exc}",
            }
        result.setdefault("registration_key", key)
        result.setdefault("status", "stopped_clean" if result.get("ok") else "stop_error")
        if result.get("ok"):
            self._finalize_stopped_clean(key)
        else:
            self._mark_stop_error(key, ctx, result)
        return result

    def _instance_for(self, key: str) -> Optional[FiboInstance]:
        engine = self._runner.manager._engines.get(key)  # noqa: SLF001
        return None if engine is None else engine.instance

    def _last_known_cumulative_volume(self, key: str, ctx: RegistrationContext) -> float:
        instance = self._instance_for(key)
        if instance is not None:
            try:
                ctx.last_known_cumulative_volume = float(instance.cumulative_volume)
            except Exception:
                pass
        return float(ctx.last_known_cumulative_volume or 0.0)

    def _status_for(self, key: str, ctx: RegistrationContext, instance: Optional[FiboInstance]) -> str:
        if ctx.service_status in {"needs_recovery", "stopping", "stop_error"}:
            return ctx.service_status
        if instance is None:
            return ctx.service_status or "needs_recovery"
        if instance.frozen:
            return "frozen"
        if not instance.running:
            return ctx.service_status or "stopped"
        return "running"

    def _mark_stopping(self, key: str, ctx: RegistrationContext, instance: Optional[FiboInstance]) -> None:
        ctx.service_status = "stopping"
        ctx.status_reason = "STOP_CLOSE in progress. Strategy halted pending transactional cleanup."
        ctx.stop_requested_at = time.time()
        ctx.cleanup_details = {}
        if instance is not None:
            instance.running = False
        self._runner._log("registration_stopping", registration_key=key)  # noqa: SLF001
        self._save_state()

    def _mark_stop_error(self, key: str, ctx: RegistrationContext, result: Dict[str, Any]) -> None:
        ctx.service_status = "stop_error"
        ctx.status_reason = str(result.get("error") or "CLEANUP_FAILED")
        ctx.cleanup_details = dict(result)
        self._save_state()

    def _finalize_stopped_clean(self, key: str) -> None:
        self._runner.manager.stop(key)
        self._runner._started_specs.pop(key, None)  # noqa: SLF001
        self._contexts.pop(key, None)
        self._save_state()

    def _build_detail_payload(
        self,
        ctx: RegistrationContext,
        instance: Optional[FiboInstance],
        exchange_state: Dict[str, Any],
        metrics: Dict[str, Any],
    ) -> Dict[str, Any]:
        step0 = instance.cascade.step0_price if instance is not None and instance.cascade.active else None
        is_buy = ctx.spec.counter_type == CounterType.COUNTER_BUY
        steps: Dict[str, Any] = {f"step{i}": None for i in range(1, 6)}
        step0tp = None
        if step0 is not None:
            step0tp = step0_tp(step0, is_buy_cascade=is_buy, divide_percent=ctx.spec.divide_percent)
            for idx in range(1, 6):
                steps[f"step{idx}"] = step_price(step0, idx, is_buy_cascade=is_buy, divide_percent=ctx.spec.divide_percent)
        status = self._status_for(ctx.spec.key, ctx, instance)
        highest_step = instance.cascade.highest_step if instance is not None else None
        activated_levels = sorted(getattr(instance, "_activated_levels", set())) if instance is not None else []
        cumulative_real_volume = (
            float(instance.cumulative_volume) if instance is not None else float(ctx.last_known_cumulative_volume or 0.0)
        )
        current_strategy_raw_sl = instance.protection.sl_price if instance is not None else None
        current_strategy_raw_tp = instance.protection.tp_price if instance is not None else None
        frozen_reason = instance.frozen_reason if instance is not None else ""
        return {
            "registration_key": ctx.spec.key,
            "status": status,
            "exchange": ctx.spec.exchange,
            "account": ctx.spec.account,
            "instrument": ctx.spec.instrument,
            "counter_side": ctx.spec.counter_type.value,
            "current_mark_price": exchange_state.get("mark_price"),
            "step0": step0,
            "step0tp": step0tp,
            **steps,
            "highest_activated_level": highest_step,
            "activated_levels": activated_levels,
            "configured_c1": ctx.spec.counter1,
            "configured_c2": ctx.spec.counter2,
            "configured_c3": ctx.spec.counter3,
            "configured_c4": ctx.spec.counter4,
            "cumulative_real_volume": cumulative_real_volume,
            "position_side": exchange_state.get("position_side"),
            "position_size": exchange_state.get("position_size"),
            "average_entry_price": exchange_state.get("entry_price"),
            "current_strategy_raw_sl": current_strategy_raw_sl,
            "actual_exchange_sl": exchange_state.get("sl"),
            "current_strategy_raw_tp": current_strategy_raw_tp,
            "actual_exchange_tp": exchange_state.get("tp"),
            "frozen_reason": frozen_reason,
            "status_reason": ctx.status_reason,
            "cleanup_details": ctx.cleanup_details,
            "completed_cycles": metrics.get("completed_cycles"),
            "profitable_cycles": metrics.get("profitable_cycles"),
            "losing_cycles": metrics.get("losing_cycles"),
            "total_realized_pnl": metrics.get("total_realized_pnl"),
            "fees": metrics.get("fees"),
        }

    def _read_exchange_state(self, ctx: RegistrationContext) -> Dict[str, Any]:
        agent = ctx.bundle.agent
        pos = agent.execute(
            {
                "operation": "position_state",
                "exchange": ctx.spec.exchange,
                "account": ctx.spec.account,
                "symbol": ctx.spec.instrument,
            }
        )
        price = agent.execute(
            {
                "operation": "market_price",
                "exchange": ctx.spec.exchange,
                "account": ctx.spec.account,
                "symbol": ctx.spec.instrument,
            }
        )
        orders = agent.execute(
            {
                "operation": "positions_orders",
                "exchange": ctx.spec.exchange,
                "account": ctx.spec.account,
            }
        )
        positions = list(getattr(pos, "positions", None) or []) if not isinstance(pos, dict) else list(pos.get("positions") or [])
        groups = list(getattr(orders, "order_groups", None) or []) if not isinstance(orders, dict) else list(orders.get("order_groups") or [])
        lane_positions = [p for p in positions if str(self._field(p, "symbol") or "").upper() == ctx.spec.instrument.upper()]
        lane_group_count = 0
        for group in groups:
            if str(self._field(group, "symbol") or "").upper() == ctx.spec.instrument.upper():
                lane_group_count += int(self._field(group, "order_count") or 0)
        market_price = None
        if isinstance(price, dict):
            mp = price.get("market_price") or {}
        else:
            mp = getattr(price, "market_price", None)
        if mp is not None:
            market_price = self._field(mp, "mark_price") or self._field(mp, "markPrice") or self._field(mp, "price")
        lane_pos = lane_positions[0] if lane_positions else None
        stop_rows = self._read_stop_rows(ctx)
        return {
            "position_count": len(lane_positions),
            "position_side": self._field(lane_pos, "side"),
            "position_size": self._as_float_or_none(self._field(lane_pos, "size")),
            "entry_price": self._as_float_or_none(self._field(lane_pos, "entry_price")),
            "mark_price": self._as_float_or_none(market_price),
            "sl": self._as_float_or_none(self._field(lane_pos, "sl")) or self._as_float_or_none(stop_rows.get("sl")),
            "tp": self._as_float_or_none(self._field(lane_pos, "tp")) or self._as_float_or_none(stop_rows.get("tp")),
            "open_orders": lane_group_count,
            "stop_rows": stop_rows,
        }

    def _read_stop_rows(self, ctx: RegistrationContext) -> Dict[str, Any]:
        agent = ctx.bundle.agent
        creds_getter = getattr(agent, "_lookup_credentials", None)
        if not callable(creds_getter):
            return {"sl": None, "tp": None, "rows": []}
        try:
            creds = creds_getter(ctx.spec.account)
            if not creds:
                return {"sl": None, "tp": None, "rows": []}
            raw = agent._signed_get(creds, "/v1/perps/stop_order")
        except Exception:
            return {"sl": None, "tp": None, "rows": []}
        market_prefix = f"{ctx.spec.instrument.upper()}-"
        sl = None
        tp = None
        rows = []
        for row in list(raw or []):
            if str(row.get("market") or "").upper().startswith(market_prefix):
                rows.append(row)
                if row.get("type") == "stopLoss":
                    sl = row.get("triggerPrice")
                elif row.get("type") == "takeProfit":
                    tp = row.get("triggerPrice")
                else:
                    sl = sl or row.get("stopLoss")
                    tp = tp or row.get("takeProfit")
        return {"sl": sl, "tp": tp, "rows": rows}

    def _assess_stop_close_safety(self, ctx: RegistrationContext, instance: Optional[FiboInstance], exchange_state: Dict[str, Any]) -> Dict[str, Any]:
        expected_side = "long" if ctx.spec.counter_type == CounterType.COUNTER_BUY else "short"
        expected_size = float(instance.cumulative_volume) if instance is not None else float(ctx.last_known_cumulative_volume or 0.0)
        ctx.last_known_cumulative_volume = expected_size
        actual_count = int(exchange_state.get("position_count") or 0)
        actual_side = str(exchange_state.get("position_side") or "")
        actual_size = exchange_state.get("position_size")
        actual_sl = exchange_state.get("sl")
        actual_tp = exchange_state.get("tp")
        open_orders = int(exchange_state.get("open_orders") or 0)
        if actual_count == 0:
            if actual_sl is None and actual_tp is None and open_orders == 0:
                return {"ok": True}
            if expected_size <= 0:
                return {
                    "ok": False,
                    "error": "OWNERSHIP_MISMATCH",
                    "message": "Refusing STOP & CLOSE: lane ownership is ambiguous for a flat zero-volume registration with leftover protection or orders.",
                }
            return {"ok": True}
        if expected_size <= 0:
            return {
                "ok": False,
                "error": "OWNERSHIP_MISMATCH",
                "message": "Refusing STOP & CLOSE: lane ownership is ambiguous for a zero-volume Fibo registration.",
            }
        if actual_count != 1:
            return {
                "ok": False,
                "error": "OWNERSHIP_MISMATCH",
                "message": "Refusing STOP & CLOSE: expected exactly one matching position row.",
            }
        if actual_side != expected_side:
            return {
                "ok": False,
                "error": "OWNERSHIP_MISMATCH",
                "message": "Refusing STOP & CLOSE: exchange position side does not match Fibo ownership.",
            }
        if actual_size is None or abs(float(actual_size) - float(expected_size)) > 1e-9:
            return {
                "ok": False,
                "error": "OWNERSHIP_MISMATCH",
                "message": "Refusing STOP & CLOSE: exchange position size does not match engine cumulative volume.",
            }
        return {"ok": True}

    def _execute_stop_close_cleanup(self, ctx: RegistrationContext, exchange_state: Dict[str, Any]) -> Dict[str, Any]:
        agent = ctx.bundle.agent
        if exchange_state.get("position_count"):
            agent.execute(
                {
                    "operation": "close_position",
                    "exchange": ctx.spec.exchange,
                    "account": ctx.spec.account,
                    "symbol": ctx.spec.instrument,
                }
            )
        agent.execute(
            {
                "operation": "set_tp",
                "exchange": ctx.spec.exchange,
                "account": ctx.spec.account,
                "symbol": ctx.spec.instrument,
                "price": "0",
            }
        )
        agent.execute(
            {
                "operation": "set_sl",
                "exchange": ctx.spec.exchange,
                "account": ctx.spec.account,
                "symbol": ctx.spec.instrument,
                "price": "0",
            }
        )
        final_state = self._read_exchange_state(ctx)
        verified_clean = (
            int(final_state.get("position_count") or 0) == 0
            and int(final_state.get("open_orders") or 0) == 0
            and final_state.get("sl") is None
            and final_state.get("tp") is None
        )
        result = {
            "ok": verified_clean,
            "verified_clean": verified_clean,
            "position_count": final_state.get("position_count"),
            "open_orders": final_state.get("open_orders"),
            "sl": final_state.get("sl"),
            "tp": final_state.get("tp"),
            "message": "STOP & CLOSE completed and verified clean." if verified_clean else "STOP_CLOSE cleanup verification failed; registration remains tracked.",
        }
        if not verified_clean:
            result["error"] = "CLEANUP_FAILED"
        return result

    @staticmethod
    def _field(obj: Any, name: str) -> Any:
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    @staticmethod
    def _as_float_or_none(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(str(value))
        except Exception:
            return None


class SocketFiboServiceClient:
    def __init__(self, socket_path: Path | None = None, timeout: float = _DEFAULT_SOCKET_TIMEOUT) -> None:
        self.socket_path = Path(socket_path or resolve_fibo_socket_path())
        self.timeout = float(timeout)

    def execute_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        payload = json.dumps(command, ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(str(self.socket_path))
                sock.sendall(payload)
                sock.shutdown(socket.SHUT_WR)
                chunks: List[bytes] = []
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
        except FileNotFoundError:
            return {
                "ok": False,
                "error": "SERVICE_UNAVAILABLE",
                "message": f"Fibo service socket not found: {self.socket_path}",
            }
        except ConnectionRefusedError:
            return {
                "ok": False,
                "error": "SERVICE_UNAVAILABLE",
                "message": f"Fibo service refused connection: {self.socket_path}",
            }
        except OSError as exc:
            return {
                "ok": False,
                "error": "SERVICE_UNAVAILABLE",
                "message": f"Fibo service IPC failed: {exc}",
            }
        if not chunks:
            return {"ok": False, "error": "SERVICE_EMPTY_RESPONSE", "message": "Fibo service returned no response."}
        try:
            return json.loads(b"".join(chunks).decode("utf-8"))
        except Exception as exc:
            return {"ok": False, "error": "SERVICE_BAD_RESPONSE", "message": f"Invalid Fibo service response: {exc}"}


class _FiboSocketHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            raw = self.rfile.readline()
            if not raw:
                response = {"ok": False, "error": "EMPTY_REQUEST", "message": "No command payload received."}
            else:
                command = json.loads(raw.decode("utf-8"))
                response = self.server.fibo_service.execute_command(command)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fibo socket handler failure")
            response = {"ok": False, "error": "SERVICE_EXCEPTION", "message": str(exc)}
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class FiboSocketServiceHost:
    def __init__(self, service: Optional[PersistentFiboService] = None, socket_path: Path | None = None) -> None:
        self.service = service or PersistentFiboService()
        self.socket_path = Path(socket_path or resolve_fibo_socket_path())
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._server: Optional[_ThreadingUnixServer] = None

    def serve_forever(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink(missing_ok=True)
        server = _ThreadingUnixServer(str(self.socket_path), _FiboSocketHandler)
        server.fibo_service = self.service  # type: ignore[attr-defined]
        self._server = server
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        try:
            self.service.shutdown()
        except Exception:
            pass
        if self.socket_path.exists():
            self.socket_path.unlink(missing_ok=True)


_LOCAL_SERVICE: Optional[PersistentFiboService] = None
_CLIENT: Optional[SocketFiboServiceClient] = None


def get_local_fibo_service() -> PersistentFiboService:
    global _LOCAL_SERVICE
    if _LOCAL_SERVICE is None:
        _LOCAL_SERVICE = PersistentFiboService()
    return _LOCAL_SERVICE


def get_fibo_service() -> FiboServiceProtocol:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = SocketFiboServiceClient()
    return _CLIENT


__all__ = [
    "FiboServiceProtocol",
    "FiboCycleLedger",
    "PersistentFiboService",
    "RegistrationContext",
    "SocketFiboServiceClient",
    "FiboSocketServiceHost",
    "resolve_hermes_home",
    "resolve_fibo_runtime_dir",
    "resolve_fibo_state_path",
    "resolve_fibo_ledger_path",
    "resolve_fibo_event_log_path",
    "resolve_fibo_socket_path",
    "get_local_fibo_service",
    "get_fibo_service",
]
