"""Minimal live runner for the real Fibo runtime.

This module contains NO strategy math and NO exchange protocol code.
It only wires together the existing real components:

    x_<exchange>_agent.py market_price / read surfaces
    → QuoteSource
    → FiboManager
    → FiboEngine
    → ExchangeAdapter
    → x_<exchange>_agent.py execute(...)

For the first controlled live test the only concrete runtime builder is
OndoPerps, but the runner architecture is registration-based and one
process / one manager can host multiple registrations later.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .adapters.ondoperps import OndoPerpsFiboAdapter
from .engine import CounterType, FiboConfig, FiboManager
from .quote_ondoperps import OndoPerpsQuoteSource


@dataclass(frozen=True)
class RegistrationSpec:
    exchange: str
    account: str
    instrument: str
    counter_type: CounterType
    divide_percent: float
    counter1: float
    counter2: float
    counter3: float
    counter4: float

    @property
    def key(self) -> str:
        return self.to_fibo_config().key

    def to_fibo_config(self) -> FiboConfig:
        return FiboConfig(
            exchange=self.exchange,
            account=self.account,
            instrument=self.instrument,
            counter_type=self.counter_type,
            divide_percent=self.divide_percent,
            counter1=self.counter1,
            counter2=self.counter2,
            counter3=self.counter3,
            counter4=self.counter4,
        )


@dataclass(frozen=True)
class RuntimeBundle:
    agent: Any
    adapter: Any
    quote_source: Any


@dataclass(frozen=True)
class PreflightSnapshot:
    registration_key: str
    market: str
    mark_price_raw: float
    oracle_price_raw: Optional[float]
    last_external_price_raw: Optional[float]
    last_updated_time: Optional[str]
    position_count: int
    open_order_count: int
    tp: Optional[str]
    sl: Optional[str]
    is_clean: bool


DEFAULT_RUNNER_LOG_PATH = Path("/root/.hermes/fibo/fibo-live-runner.log")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "to_dict") and callable(getattr(value, "to_dict")):
        try:
            return _json_safe(value.to_dict())
        except Exception:  # noqa: BLE001
            return repr(value)
    if hasattr(value, "__dict__"):
        try:
            return _json_safe(vars(value))
        except Exception:  # noqa: BLE001
            return repr(value)
    return repr(value)


class JsonlLogSink:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: Dict[str, Any]) -> None:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        row.update(_json_safe(payload))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


class FiboLiveRunner:
    def __init__(
        self,
        *,
        manager: Optional[FiboManager] = None,
        runtime_registry: Optional[Dict[str, Callable[[RegistrationSpec], Any]]] = None,
        poll_seconds: float = 2.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        log_sink: Optional[JsonlLogSink] = None,
    ) -> None:
        self.log_sink = log_sink if log_sink is not None else JsonlLogSink(DEFAULT_RUNNER_LOG_PATH)
        self.poll_seconds = float(poll_seconds)
        self.sleep_fn = sleep_fn
        self.stop_requested = False
        self._runtime_registry = runtime_registry or default_runtime_registry()
        self._started_specs: Dict[str, RegistrationSpec] = {}
        self.manager = manager if manager is not None else FiboManager(event_sink=self._on_engine_event)

    def _log(self, event: str, **fields: Any) -> None:
        payload = {"event": event}
        payload.update(fields)
        if self.log_sink is not None:
            try:
                self.log_sink.write(payload)
            except Exception as exc:  # noqa: BLE001
                fallback = {
                    "event": "runner_logging_failure",
                    "failed_event": event,
                    "log_error": f"{type(exc).__name__}: {exc}",
                    "original_payload": _json_safe(payload),
                }
                try:
                    if hasattr(self.log_sink, "path"):
                        JsonlLogSink(Path(getattr(self.log_sink, "path"))).write(fallback)
                    else:
                        sys.stderr.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fallback}, ensure_ascii=False, default=str) + "\n")
                except Exception:  # noqa: BLE001
                    pass

    def _on_engine_event(self, payload: Dict[str, Any]) -> None:
        self._log(**payload)

    def _resolve_runtime(self, spec: RegistrationSpec) -> RuntimeBundle:
        factory = self._runtime_registry.get(spec.exchange)
        if factory is None:
            raise ValueError(f"Unsupported exchange for Fibo runner: {spec.exchange}")
        built = factory(spec)
        if isinstance(built, RuntimeBundle):
            return built
        if isinstance(built, dict):
            return RuntimeBundle(
                agent=built["agent"],
                adapter=built["adapter"],
                quote_source=built["quote_source"],
            )
        raise TypeError("Runtime factory must return RuntimeBundle or dict bundle")

    def _attach_runtime_event_sink(self, bundle: RuntimeBundle) -> None:
        setter = getattr(bundle.adapter, "set_event_sink", None)
        if callable(setter):
            try:
                setter(self._on_engine_event)
            except Exception:  # noqa: BLE001
                pass

    def preflight_registration(self, spec: RegistrationSpec) -> PreflightSnapshot:
        bundle = self._resolve_runtime(spec)
        market_resp = bundle.agent.execute({
            "operation": "market_price",
            "exchange": spec.exchange,
            "account": spec.account,
            "symbol": spec.instrument,
        })
        if not getattr(market_resp, "success", None) and not (isinstance(market_resp, dict) and market_resp.get("success")):
            raise RuntimeError("PREFLIGHT_MARKET_PRICE_FAILED")

        if isinstance(market_resp, dict):
            mp = market_resp.get("market_price") or {}
        else:
            mp = getattr(market_resp, "market_price", None)
        market = _field(mp, "market") or spec.instrument
        mark_price = _as_float(_field(mp, "mark_price") or _field(mp, "markPrice") or _field(mp, "price"))
        oracle_price = _as_optional_float(_field(mp, "oracle_price") or _field(mp, "oraclePrice"))
        last_external = _as_optional_float(_field(mp, "last_external_price") or _field(mp, "lastExternalPrice"))
        last_updated_time = _field(mp, "last_updated_time") or _field(mp, "lastUpdatedTime")

        pos_resp = bundle.agent.execute({
            "operation": "position_state",
            "exchange": spec.exchange,
            "account": spec.account,
            "symbol": spec.instrument,
        })
        if not getattr(pos_resp, "success", None) and not (isinstance(pos_resp, dict) and pos_resp.get("success")):
            raise RuntimeError("PREFLIGHT_POSITION_STATE_FAILED")
        positions = _positions_list(pos_resp)
        tp = None
        sl = None
        if positions:
            tp = _field(positions[0], "tp")
            sl = _field(positions[0], "sl")

        orders_resp = bundle.agent.execute({
            "operation": "positions_orders",
            "exchange": spec.exchange,
            "account": spec.account,
        })
        if not getattr(orders_resp, "success", None) and not (isinstance(orders_resp, dict) and orders_resp.get("success")):
            raise RuntimeError("PREFLIGHT_POSITIONS_ORDERS_FAILED")
        order_groups = _order_groups(orders_resp)
        open_order_count = 0
        for group in order_groups:
            if str(_field(group, "symbol") or "").upper() == spec.instrument.upper():
                open_order_count += int(_field(group, "order_count") or 0)

        # OndoPerps-specific stop-lane inspection: use the real agent module's
        # internal read helper so HTTP/signing still live inside x_ondoperps_agent.py.
        stop_tp = None
        stop_sl = None
        if spec.exchange == "ondoperps" and hasattr(bundle.agent, "_lookup_credentials"):
            try:
                creds = bundle.agent._lookup_credentials(spec.account)
                if creds:
                    meta, _ = bundle.agent._resolve_market_metadata(creds, spec.instrument)
                    snap = bundle.agent._signed_get(
                        creds,
                        f"{bundle.agent._STOP_ORDER_PATH}?market={market}&positionDirection=long",
                    )
                    for entry in _normalize_stop_entries(snap, market):
                        stop_tp = stop_tp or _field(entry, "takeProfit")
                        stop_sl = stop_sl or _field(entry, "stopLoss")
                    snap = bundle.agent._signed_get(
                        creds,
                        f"{bundle.agent._STOP_ORDER_PATH}?market={market}&positionDirection=short",
                    )
                    for entry in _normalize_stop_entries(snap, market):
                        stop_tp = stop_tp or _field(entry, "takeProfit")
                        stop_sl = stop_sl or _field(entry, "stopLoss")
                    if meta:
                        market = str(meta.get("market") or market)
            except Exception:
                # If the deeper stop snapshot can't be read we fall back to the
                # position_state view. The smoke-test operator can still abort
                # after reviewing the preflight report.
                pass
        tp = tp or stop_tp
        sl = sl or stop_sl

        is_clean = (len(positions) == 0 and open_order_count == 0 and not tp and not sl)
        snapshot = PreflightSnapshot(
            registration_key=spec.key,
            market=market,
            mark_price_raw=mark_price,
            oracle_price_raw=oracle_price,
            last_external_price_raw=last_external,
            last_updated_time=last_updated_time,
            position_count=len(positions),
            open_order_count=open_order_count,
            tp=tp,
            sl=sl,
            is_clean=is_clean,
        )
        self._log(
            "preflight_checked",
            registration_key=spec.key,
            market=market,
            mark_price_raw=mark_price,
            position_count=len(positions),
            open_order_count=open_order_count,
            tp=tp,
            sl=sl,
            is_clean=is_clean,
        )
        if not is_clean:
            raise RuntimeError(f"DIRTY_LANE: {spec.key}")
        return snapshot

    def start_registration(self, spec: RegistrationSpec) -> PreflightSnapshot:
        snapshot = self.preflight_registration(spec)
        bundle = self._resolve_runtime(spec)
        self._attach_runtime_event_sink(bundle)
        self.manager.start(spec.to_fibo_config(), bundle.adapter, bundle.quote_source)
        self._started_specs[spec.key] = spec
        self._log("registration_started", registration_key=spec.key, poll_seconds=self.poll_seconds)
        return snapshot

    def stop_registration(self, key: str, *, reason: str = "user_stop") -> bool:
        stopped = self.manager.stop(key)
        if stopped:
            self._started_specs.pop(key, None)
            self._log("registration_stop_requested", registration_key=key, reason=reason)
        return stopped

    def stop_all(self, *, reason: str = "user_stop") -> None:
        for key in list(self._started_specs.keys()):
            self.stop_registration(key, reason=reason)
        self.stop_requested = True

    def handle_signal(self, signum: int, _frame: Any) -> None:
        self._log("signal_received", signal=signum)
        self.stop_all(reason="signal_stop")

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.handle_signal)
        signal.signal(signal.SIGTERM, self.handle_signal)

    def run_loop(self, *, max_iterations: Optional[int] = None) -> None:
        iterations = 0
        while not self.stop_requested:
            self.manager.poll_once()
            for instance in self.manager.list_running():
                self._log(
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
            iterations += 1
            self.sleep_fn(self.poll_seconds)
            if max_iterations is not None and iterations >= max_iterations:
                break


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real Fibo registrations through FiboManager.poll_once().")
    parser.add_argument("--exchange")
    parser.add_argument("--account")
    parser.add_argument("--instrument")
    parser.add_argument("--counter-type", dest="counter_type")
    # Default spacing is configurable per registration; 100 is only the default.
    parser.add_argument("--divide-percent", dest="divide_percent", type=float, default=100.0)
    parser.add_argument("--counter1", type=float, default=1.3)
    parser.add_argument("--counter2", type=float, default=0.8)
    parser.add_argument("--counter3", type=float, default=0.5)
    parser.add_argument("--counter4", type=float, default=0.3)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--log-file", default=str(Path("/root/.hermes/fibo/fibo-live-runner.log")))
    return parser.parse_args(list(argv) if argv is not None else None)


def build_specs_from_args(args: argparse.Namespace) -> List[RegistrationSpec]:
    required = [args.exchange, args.account, args.instrument, args.counter_type]
    if not all(required):
        raise ValueError("exchange/account/instrument/counter-type are required")
    return [RegistrationSpec(
        exchange=str(args.exchange),
        account=str(args.account),
        instrument=str(args.instrument).upper(),
        counter_type=CounterType(str(args.counter_type)),
        divide_percent=float(args.divide_percent),
        counter1=float(args.counter1),
        counter2=float(args.counter2),
        counter3=float(args.counter3),
        counter4=float(args.counter4),
    )]


def default_runtime_registry() -> Dict[str, Callable[[RegistrationSpec], RuntimeBundle]]:
    def _build_ondoperps(spec: RegistrationSpec) -> RuntimeBundle:
        from plugins.trade.agents import x_ondoperps_agent as agent
        return RuntimeBundle(
            agent=agent,
            adapter=OndoPerpsFiboAdapter(spec.exchange, spec.account, agent),
            quote_source=OndoPerpsQuoteSource(spec.exchange, spec.account, agent),
        )

    return {"ondoperps": _build_ondoperps}


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _positions_list(response: Any) -> List[Any]:
    if response is None:
        return []
    if isinstance(response, dict):
        return list(response.get("positions") or [])
    return list(getattr(response, "positions", None) or [])


def _order_groups(response: Any) -> List[Any]:
    if response is None:
        return []
    if isinstance(response, dict):
        return list(response.get("order_groups") or [])
    return list(getattr(response, "order_groups", None) or [])


def _normalize_stop_entries(payload: Any, market: str) -> List[Any]:
    rows = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []
    out: List[Any] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("market") or "") != market:
            continue
        out.append(row)
    return out


def _as_float(value: Any) -> float:
    return float(str(value))


def _as_optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    return float(str(value))


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    specs = build_specs_from_args(args)
    runner = FiboLiveRunner(
        poll_seconds=float(args.poll_seconds),
        log_sink=JsonlLogSink(Path(args.log_file)),
    )
    runner.install_signal_handlers()
    for spec in specs:
        runner.start_registration(spec)
    runner.run_loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
