from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.fibo.engine import CounterType, FiboConfig, FiboInstance  # noqa: E402
from plugins.trade.fibo.runner import PreflightSnapshot, RegistrationSpec, RuntimeBundle  # noqa: E402
from plugins.trade.fibo_service import FiboCycleLedger, PersistentFiboService  # noqa: E402
from plugins.trade.fibo_wizard import FiboWizard  # noqa: E402


@dataclass
class FakePosition:
    symbol: str
    side: str
    size: str
    entry_price: str
    mark_price: str
    sl: Optional[str] = None
    tp: Optional[str] = None


@dataclass
class FakeOrderGroup:
    symbol: str
    order_count: int


class FakeAgent:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.lanes: Dict[str, Dict[str, Any]] = {}
        self.on_call: Optional[Callable[[Dict[str, Any]], None]] = None

    def configure_lane(
        self,
        symbol: str,
        *,
        side: Optional[str],
        size: float,
        entry_price: float,
        mark_price: float,
        sl: Optional[float],
        tp: Optional[float],
        open_orders: int = 0,
    ) -> None:
        self.lanes[symbol.upper()] = {
            "side": side,
            "size": size,
            "entry_price": entry_price,
            "mark_price": mark_price,
            "sl": sl,
            "tp": tp,
            "open_orders": open_orders,
        }

    def execute(self, request: Dict[str, Any]) -> Any:
        self.calls.append(dict(request))
        if callable(self.on_call):
            self.on_call(dict(request))
        op = request["operation"]
        symbol = str(request.get("symbol") or "").upper()
        if op == "position_state":
            lane = self.lanes.get(symbol, {})
            positions = []
            if lane.get("side") and float(lane.get("size") or 0) > 0:
                positions.append(
                    FakePosition(
                        symbol=symbol,
                        side=str(lane["side"]),
                        size=str(lane["size"]),
                        entry_price=str(lane["entry_price"]),
                        mark_price=str(lane["mark_price"]),
                        sl=None if lane.get("sl") is None else str(lane.get("sl")),
                        tp=None if lane.get("tp") is None else str(lane.get("tp")),
                    )
                )
            return SimpleNamespace(success=True, positions=positions)
        if op == "market_price":
            lane = self.lanes.get(symbol, {})
            mp = SimpleNamespace(mark_price=str(lane.get("mark_price", 0)), market=f"{symbol}-USD.P")
            return SimpleNamespace(success=True, market_price=mp)
        if op == "positions_orders":
            groups = []
            for sym, lane in self.lanes.items():
                if int(lane.get("open_orders") or 0) > 0:
                    groups.append(FakeOrderGroup(symbol=sym, order_count=int(lane.get("open_orders") or 0)))
            return SimpleNamespace(success=True, order_groups=groups)
        if op == "close_position":
            lane = self.lanes.setdefault(symbol, {})
            lane["side"] = None
            lane["size"] = 0.0
            lane["entry_price"] = 0.0
            return SimpleNamespace(success=True)
        if op == "set_tp":
            lane = self.lanes.setdefault(symbol, {})
            lane["tp"] = None if str(request.get("price")) == "0" else float(request["price"])
            return SimpleNamespace(success=True)
        if op == "set_sl":
            lane = self.lanes.setdefault(symbol, {})
            lane["sl"] = None if str(request.get("price")) == "0" else float(request["price"])
            return SimpleNamespace(success=True)
        raise AssertionError(f"unexpected operation: {op}")

    def _lookup_credentials(self, account: str) -> Dict[str, str]:
        return {"account": account}

    def _signed_get(self, creds: Dict[str, str], path: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for sym, lane in self.lanes.items():
            market = f"{sym}-USD.P"
            if lane.get("sl") is not None:
                rows.append({"market": market, "type": "stopLoss", "triggerPrice": str(lane['sl'])})
            if lane.get("tp") is not None:
                rows.append({"market": market, "type": "takeProfit", "triggerPrice": str(lane['tp'])})
        return rows


class FakeAdapter:
    def __init__(self) -> None:
        self.event_sink = None
        self.started: List[str] = []
        self.stopped: List[str] = []

    def set_event_sink(self, sink: Any) -> None:
        self.event_sink = sink

    def on_registration_started(self, key: str) -> None:
        self.started.append(key)

    def on_registration_stopped(self, key: str) -> None:
        self.stopped.append(key)


class FakeQuoteSource:
    def current_bid_ask(self, instrument: str) -> Any:
        raise LookupError(instrument)


class FakeManager:
    def __init__(self) -> None:
        self._engines: Dict[str, Any] = {}
        self.started: List[str] = []
        self.started_configs: List[FiboConfig] = []
        self.stopped: List[str] = []
        self.poll_count = 0

    def start(self, config: FiboConfig, adapter: Any, quote_source: Any) -> FiboInstance:
        if config.key in self._engines:
            raise ValueError(f"Fibo already running: {config.key}")
        if hasattr(adapter, "on_registration_started"):
            adapter.on_registration_started(config.key)
        instance = FiboInstance(config=config, running=True)
        instance.cascade.step0_price = 100.0 if config.instrument != "XAU" else 4359.18
        instance.cascade.highest_step = 1
        instance.mark_activated(1)
        instance.cumulative_volume = Decimal(str(config.counter1))
        instance.protection.sl_price = 99.0 if config.instrument != "XAU" else 4359.18
        self._engines[config.key] = SimpleNamespace(instance=instance, adapter=adapter, quote_source=quote_source)
        self.started.append(config.key)
        self.started_configs.append(config)
        return instance

    def stop(self, key: str) -> bool:
        engine = self._engines.pop(key, None)
        if engine is None:
            return False
        engine.instance.running = False
        if hasattr(engine.adapter, "on_registration_stopped"):
            engine.adapter.on_registration_stopped(key)
        self.stopped.append(key)
        return True

    def is_running(self, key: str) -> bool:
        return key in self._engines

    def list_running(self) -> List[FiboInstance]:
        return [engine.instance for engine in self._engines.values()]

    def poll_once(self) -> None:
        self.poll_count += 1


class FakeRunner:
    def __init__(self, agent: FakeAgent, manager: Optional[FakeManager] = None) -> None:
        self.agent = agent
        self.manager = manager or FakeManager()
        self._started_specs: Dict[str, RegistrationSpec] = {}
        self.logged: List[Dict[str, Any]] = []

    def preflight_registration(self, spec: RegistrationSpec) -> PreflightSnapshot:
        return PreflightSnapshot(
            registration_key=spec.key,
            market=f"{spec.instrument}-USD.P",
            mark_price_raw=100.0,
            oracle_price_raw=None,
            last_external_price_raw=None,
            last_updated_time=None,
            position_count=0,
            open_order_count=0,
            tp=None,
            sl=None,
            is_clean=True,
        )

    def _resolve_runtime(self, spec: RegistrationSpec) -> RuntimeBundle:
        return RuntimeBundle(agent=self.agent, adapter=FakeAdapter(), quote_source=FakeQuoteSource())

    def _attach_runtime_event_sink(self, bundle: RuntimeBundle) -> None:
        if hasattr(bundle.adapter, "set_event_sink"):
            bundle.adapter.set_event_sink(lambda payload: None)

    def _log(self, event: str, **fields: Any) -> None:
        row = {"event": event}
        row.update(fields)
        self.logged.append(row)


class FakeTradeDesk:
    def list_exchanges(self) -> List[str]:
        return ["ondoperps"]

    def list_accounts(self, exchange: str) -> List[str]:
        return ["amiroo", "bitget"] if exchange == "ondoperps" else []


class ExplodingLedger(FiboCycleLedger):
    def __init__(self) -> None:
        self._summaries = {}
        self.path = Path("/tmp/exploding-ledger.jsonl")

    def record_event(self, row: Dict[str, Any]) -> None:
        raise RuntimeError("ledger boom")

    def note_cycle_cleanup(self, registration_key: str) -> None:
        raise RuntimeError("ledger boom")


class PersistentFiboServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agent = FakeAgent()
        self.runner = FakeRunner(self.agent)
        self.state_path = Path(self.tmp.name) / "service_state.json"
        self.ledger_path = Path(self.tmp.name) / "ledger.jsonl"
        self.event_log_path = Path(self.tmp.name) / "events.jsonl"
        self.ledger = FiboCycleLedger(self.ledger_path)
        self.service = PersistentFiboService(
            tradedesk=FakeTradeDesk(),
            runner=self.runner,
            state_path=self.state_path,
            ledger=self.ledger,
            event_log_path=self.event_log_path,
            start_thread=False,
        )

    def _start(self, instrument: str, account: str = "amiroo", counter_side: str = "counterSELL", c1: float = 1.0) -> str:
        cmd = {
            "op": "start",
            "exchange": "ondoperps",
            "account": account,
            "instrument": instrument,
            "counter_side": counter_side,
            "divide_percent": 100.0,
            "counter1": c1,
            "counter2": 0.0,
            "counter3": 0.0,
            "counter4": 0.0,
            "poll_seconds": 2.0,
        }
        result = self.service.execute_command(cmd)
        self.assertTrue(result["ok"])
        return result["registration_key"]

    def test_service_creates_exactly_one_registration_and_rejects_duplicate(self) -> None:
        key = self._start("ONDO")
        self.assertEqual(self.runner.manager.started, [key])
        dup = self.service.execute_command(
            {
                "op": "start",
                "exchange": "ondoperps",
                "account": "amiroo",
                "instrument": "ONDO",
                "counter_side": "counterSELL",
                "divide_percent": 100.0,
                "counter1": 1.0,
                "counter2": 0.0,
                "counter3": 0.0,
                "counter4": 0.0,
                "poll_seconds": 2.0,
            }
        )
        self.assertFalse(dup["ok"])
        self.assertEqual(dup["error"], "DUPLICATE_REGISTRATION")
        self.assertEqual(len(self.runner.manager.list_running()), 1)

    def test_multiple_registrations_coexist_independently(self) -> None:
        key_a = self._start("ONDO", account="amiroo", counter_side="counterSELL", c1=1.0)
        key_b = self._start("XAU", account="bitget", counter_side="counterBUY", c1=0.01)
        listed = self.service.execute_command({"op": "list"})
        self.assertTrue(listed["ok"])
        self.assertEqual({row["registration_key"] for row in listed["registrations"]}, {key_a, key_b})

    def test_detail_corresponds_to_correct_registration(self) -> None:
        key = self._start("XAU", account="bitget", counter_side="counterBUY", c1=0.01)
        self.agent.configure_lane("XAU", side="long", size=0.01, entry_price=4360.86, mark_price=4372.28, sl=4359.18, tp=None)
        detail = self.service.execute_command({"op": "detail", "registration_key": key})
        self.assertTrue(detail["ok"])
        payload = detail["detail"]
        self.assertEqual(payload["registration_key"], key)
        self.assertEqual(payload["instrument"], "XAU")
        self.assertEqual(payload["counter_side"], "counterBUY")
        self.assertEqual(payload["position_side"], "long")
        self.assertEqual(payload["position_size"], 0.01)

    def test_stop_close_targets_only_selected_registration_and_verifies_clean(self) -> None:
        key_a = self._start("ONDO", account="amiroo", counter_side="counterSELL", c1=1.0)
        key_b = self._start("XAU", account="bitget", counter_side="counterBUY", c1=0.01)
        self.agent.configure_lane("ONDO", side="short", size=1.0, entry_price=0.32966, mark_price=0.3274, sl=0.32989, tp=None)
        self.agent.configure_lane("XAU", side="long", size=0.01, entry_price=4360.86, mark_price=4372.28, sl=4359.18, tp=4383.42)
        result = self.service.execute_command({"op": "stop_close", "registration_key": key_b})
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified_clean"])
        self.assertEqual(result["status"], "stopped_clean")
        self.assertTrue(self.runner.manager.is_running(key_a))
        self.assertFalse(self.runner.manager.is_running(key_b))
        xau_ops = [call["operation"] for call in self.agent.calls if str(call.get("symbol", "")).upper() == "XAU"]
        self.assertIn("close_position", xau_ops)
        self.assertIn("set_tp", xau_ops)
        self.assertIn("set_sl", xau_ops)
        ondo_ops = [call["operation"] for call in self.agent.calls if str(call.get("symbol", "")).upper() == "ONDO"]
        self.assertNotIn("close_position", ondo_ops)

    def test_stop_close_enters_stopping_before_cleanup_and_halts_instance(self) -> None:
        key = self._start("ONDO", account="amiroo", counter_side="counterSELL", c1=1.0)
        self.agent.configure_lane("ONDO", side="short", size=1.0, entry_price=0.32966, mark_price=0.3274, sl=0.32989, tp=0.32001)
        observed_running_flags: List[bool] = []

        def on_call(request: Dict[str, Any]) -> None:
            if request.get("operation") in {"close_position", "set_tp", "set_sl"}:
                observed_running_flags.append(self.runner.manager._engines[key].instance.running)

        self.agent.on_call = on_call
        result = self.service.execute_command({"op": "stop_close", "registration_key": key})
        self.assertTrue(result["ok"])
        self.assertTrue(observed_running_flags)
        self.assertTrue(all(flag is False for flag in observed_running_flags))
        stopping_events = [row for row in self.runner.logged if row["event"] == "registration_stopping" and row.get("registration_key") == key]
        self.assertEqual(len(stopping_events), 1)

    def test_cleanup_failure_leaves_registration_visible_as_stop_error(self) -> None:
        key = self._start("ONDO", account="amiroo", counter_side="counterSELL", c1=1.0)
        self.agent.configure_lane("ONDO", side="short", size=1.0, entry_price=0.32966, mark_price=0.3274, sl=0.32989, tp=None, open_orders=1)
        result = self.service.execute_command({"op": "stop_close", "registration_key": key})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "CLEANUP_FAILED")
        self.assertTrue(self.runner.manager.is_running(key))
        detail = self.service.execute_command({"op": "detail", "registration_key": key})
        self.assertTrue(detail["ok"])
        self.assertEqual(detail["detail"]["status"], "stop_error")
        self.assertEqual(detail["detail"]["cleanup_details"]["error"], "CLEANUP_FAILED")
        self.assertFalse(self.runner.manager._engines[key].instance.running)

    def test_naturally_flat_registration_stop_close_succeeds_cleanly(self) -> None:
        key = self._start("ONDO", account="amiroo", counter_side="counterSELL", c1=1.0)
        self.agent.configure_lane("ONDO", side=None, size=0.0, entry_price=0.0, mark_price=0.3274, sl=None, tp=None)
        result = self.service.execute_command({"op": "stop_close", "registration_key": key})
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "stopped_clean")
        ops = [call["operation"] for call in self.agent.calls if str(call.get("symbol", "")).upper() == "ONDO"]
        self.assertIn("set_tp", ops)
        self.assertIn("set_sl", ops)
        self.assertNotIn("close_position", ops)
        self.assertFalse(self.runner.manager.is_running(key))

    def test_wizard_confirmation_payload_matches_service_and_fiboconfig_exactly(self) -> None:
        wizard = FiboWizard(tradedesk=FakeTradeDesk(), service=self.service)  # type: ignore[arg-type]
        key = ("chat-contract",)
        wizard.open(key)
        wizard.handle_callback(key, "menu:start")
        wizard.handle_callback(key, "exchange:ondoperps")
        wizard.handle_callback(key, "account:bitget")
        wizard.handle_callback(key, "symbol:XAU")
        wizard.handle_callback(key, "counter_side:counterBUY")
        wizard.handle_text(key, "1000")
        wizard.handle_text(key, "0.01")
        wizard.handle_text(key, "0")
        wizard.handle_text(key, "0.5")
        wizard.handle_text(key, "0")
        result = wizard.handle_callback(key, "confirm_start")
        self.assertIn("started", result.text.lower())
        cfg = self.runner.manager.started_configs[-1]
        self.assertEqual(cfg.exchange, "ondoperps")
        self.assertEqual(cfg.account, "bitget")
        self.assertEqual(cfg.instrument, "XAU")
        self.assertEqual(cfg.counter_type.value, "counterBUY")
        self.assertEqual(cfg.divide_percent, 1000.0)
        self.assertEqual(cfg.counter1, 0.01)
        self.assertEqual(cfg.counter2, 0.0)
        self.assertEqual(cfg.counter3, 0.5)
        self.assertEqual(cfg.counter4, 0.0)
        self.assertEqual(self.service._contexts[cfg.key].spec.instrument, "XAU")  # noqa: SLF001
        self.assertEqual(self.service._contexts[cfg.key].spec.counter1, 0.01)  # noqa: SLF001

    def test_ambiguous_non_fibo_ownership_blocks_destructive_cleanup(self) -> None:
        key = self._start("ONDO", account="amiroo", counter_side="counterSELL", c1=1.0)
        self.agent.configure_lane("ONDO", side="short", size=2.0, entry_price=0.32966, mark_price=0.3274, sl=0.32989, tp=None)
        result = self.service.execute_command({"op": "stop_close", "registration_key": key})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "OWNERSHIP_MISMATCH")
        self.assertTrue(self.runner.manager.is_running(key))
        ops = [call["operation"] for call in self.agent.calls if str(call.get("symbol", "")).upper() == "ONDO"]
        self.assertNotIn("close_position", ops)
        self.assertNotIn("set_tp", ops)
        self.assertNotIn("set_sl", ops)

    def test_service_ledger_failures_do_not_raise_or_interrupt(self) -> None:
        exploding = ExplodingLedger()
        service = PersistentFiboService(
            tradedesk=FakeTradeDesk(),
            runner=FakeRunner(self.agent),
            state_path=Path(self.tmp.name) / "state2.json",
            ledger=exploding,
            event_log_path=Path(self.tmp.name) / "events2.jsonl",
            start_thread=False,
        )
        service._safe_event_sink({"event": "cycle_cleanup", "registration_key": "ondoperps:amiroo:ONDO:counterSELL"})  # noqa: SLF001
        service._safe_event_sink({"event": "client_order_id_prepared", "registration_key": "ondoperps:amiroo:ONDO:counterSELL"})  # noqa: SLF001
        result = service.execute_command(
            {
                "op": "start",
                "exchange": "ondoperps",
                "account": "amiroo",
                "instrument": "ETH",
                "counter_side": "counterSELL",
                "divide_percent": 100.0,
                "counter1": 1.0,
                "counter2": 0.0,
                "counter3": 0.0,
                "counter4": 0.0,
                "poll_seconds": 2.0,
            }
        )
        self.assertTrue(result["ok"])

    def test_telegram_restart_does_not_terminate_active_registrations(self) -> None:
        key = self._start("ONDO")
        wizard_a = FiboWizard(tradedesk=FakeTradeDesk(), service=self.service)
        wizard_b = FiboWizard(tradedesk=FakeTradeDesk(), service=self.service)
        screen_a = wizard_a.handle_callback(("chat-a",), "menu:running")
        self.assertIn("Ondoperps / amiroo / ONDO / Counter SELL", screen_a.text)
        screen_b = wizard_b.handle_callback(("chat-b",), "menu:running")
        self.assertIn("Ondoperps / amiroo / ONDO / Counter SELL", screen_b.text)
        self.assertTrue(self.runner.manager.is_running(key))

    def test_service_restart_surfaces_needs_recovery_without_resuming_running_state(self) -> None:
        key = self._start("ONDO")
        restarted = PersistentFiboService(  # type: ignore[arg-type]
            tradedesk=FakeTradeDesk(),
            runner=FakeRunner(self.agent),
            state_path=self.state_path,
            ledger=self.ledger,
            event_log_path=Path(self.tmp.name) / "events-restarted.jsonl",
            start_thread=False,
        )
        listed = restarted.execute_command({"op": "list"})
        self.assertTrue(listed["ok"])
        row = next(item for item in listed["registrations"] if item["registration_key"] == key)
        self.assertEqual(row["status"], "needs_recovery")
        self.assertFalse(restarted._runner.manager.is_running(key))  # noqa: SLF001
        detail = restarted.execute_command({"op": "detail", "registration_key": key})
        self.assertEqual(detail["detail"]["status"], "needs_recovery")


if __name__ == "__main__":
    unittest.main()
