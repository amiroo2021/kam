from __future__ import annotations

import json
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.fibo.engine import CounterType, FiboConfig, FiboManager, RealOrderSide  # noqa: E402
from plugins.trade.fibo.adapters.ondoperps import OndoPerpsFiboAdapter  # noqa: E402
from plugins.trade.fibo.quote import Quote  # noqa: E402
from plugins.trade.tests.test_fibo_ondoperps import FakeOndoPerpsAgent, SequencedPositionAgent, _ok  # noqa: E402
from plugins.trade.tests.test_fibo_engine import FakeAdapter, FakeQuoteSource  # noqa: E402
from plugins.trade.fibo.runner import (  # noqa: E402
    FiboLiveRunner,
    JsonlLogSink,
    RegistrationSpec,
    build_specs_from_args,
    parse_args,
)


class FakeManager:
    def __init__(self) -> None:
        self.poll_calls = 0
        self.stopped: List[str] = []
        self.running: List[Any] = []

    def poll_once(self) -> None:
        self.poll_calls += 1

    def stop(self, key: str) -> bool:
        self.stopped.append(key)
        return True

    def list_running(self) -> List[Any]:
        return list(self.running)


class FailingSink:
    def __init__(self, fail_event: str) -> None:
        self.fail_event = fail_event
        self.rows: List[Dict[str, Any]] = []

    def write(self, payload: Dict[str, Any]) -> None:
        self.rows.append(dict(payload))
        if payload.get("event") == self.fail_event:
            raise TypeError(f"forced sink failure for {self.fail_event}")


class RunnerHarnessTests(unittest.TestCase):
    def test_cli_maps_into_registration_spec(self) -> None:
        args = parse_args([
            "--exchange", "ondoperps",
            "--account", "amiroo",
            "--instrument", "ONDO",
            "--counter-type", "counterSELL",
            "--divide-percent", "1000",
            "--counter1", "1",
            "--counter2", "0",
            "--counter3", "0",
            "--counter4", "0",
            "--poll-seconds", "2",
        ])
        specs = build_specs_from_args(args)
        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertEqual(spec.exchange, "ondoperps")
        self.assertEqual(spec.account, "amiroo")
        self.assertEqual(spec.instrument, "ONDO")
        self.assertEqual(spec.counter_type, CounterType.COUNTER_SELL)
        self.assertEqual(spec.divide_percent, 1000)
        self.assertEqual(spec.counter1, 1)
        self.assertEqual(spec.counter2, 0)
        self.assertEqual(spec.counter3, 0)
        self.assertEqual(spec.counter4, 0)
        self.assertEqual(args.poll_seconds, 2.0)

    def test_cli_defaults_divide_percent_to_100(self) -> None:
        args = parse_args([
            "--exchange", "ondoperps",
            "--account", "amiroo",
            "--instrument", "ONDO",
            "--counter-type", "counterSELL",
        ])
        specs = build_specs_from_args(args)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].divide_percent, 100)

    def test_runner_uses_manager_poll_once(self) -> None:
        manager = FakeManager()
        sleeps: List[float] = []
        runner = FiboLiveRunner(manager=manager, sleep_fn=sleeps.append)
        runner.run_loop(max_iterations=3)
        self.assertEqual(manager.poll_calls, 3)
        self.assertEqual(sleeps, [2.0, 2.0, 2.0])

    def test_runner_file_contains_no_strategy_math_helpers(self) -> None:
        text = (_REPO_ROOT / "plugins/trade/fibo/runner.py").read_text()
        self.assertNotIn("step_price(", text)
        self.assertNotIn("step_tp(", text)
        self.assertNotIn("fib_distance(", text)

    def test_signal_shutdown_calls_user_stop_only(self) -> None:
        manager = FakeManager()
        runner = FiboLiveRunner(manager=manager)
        spec = RegistrationSpec(
            exchange="ondoperps", account="amiroo", instrument="ONDO",
            counter_type=CounterType.COUNTER_SELL,
            divide_percent=1000, counter1=1, counter2=0, counter3=0, counter4=0,
        )
        runner._started_specs[spec.key] = spec
        runner.handle_signal(signal.SIGTERM, None)
        self.assertEqual(manager.stopped, [spec.key])
        self.assertTrue(runner.stop_requested)

    def test_preflight_rejects_dirty_lane(self) -> None:
        agent = FakeOndoPerpsAgent()
        agent.scripted[("market_price", "ONDO")] = {
            "success": True,
            "operation": "market_price",
            "exchange": "ondoperps",
            "account": "amiroo",
            "market_price": {
                "requested_symbol": "ONDO",
                "market": "ONDO-USD.P",
                "markPrice": "0.333687",
            },
        }
        agent.scripted[("position_state", "ONDO")] = {
            "success": True,
            "operation": "position_state",
            "exchange": "ondoperps",
            "account": "amiroo",
            "positions": [{
                "symbol": "ONDO", "side": "short", "size": "1",
                "entry_price": "0.33", "pnl": "0", "tp": None, "sl": None,
                "tp_count": None, "sl_count": None,
            }],
        }
        agent.scripted[("positions_orders", "*")] = {
            "success": True,
            "operation": "positions_orders",
            "exchange": "ondoperps",
            "account": "amiroo",
            "positions": [],
            "open_order_count": 0,
            "order_groups": [],
        }
        bundle = {
            "agent": agent,
            "adapter": FakeAdapter(),
            "quote_source": FakeQuoteSource(quotes_by_symbol={"ONDO": [Quote(bid=0.333687, ask=0.333687)]}),
        }
        runner = FiboLiveRunner(runtime_registry={"ondoperps": lambda spec: bundle})
        spec = RegistrationSpec(
            exchange="ondoperps", account="amiroo", instrument="ONDO",
            counter_type=CounterType.COUNTER_SELL,
            divide_percent=1000, counter1=1, counter2=0, counter3=0, counter4=0,
        )
        with self.assertRaisesRegex(RuntimeError, "DIRTY_LANE"):
            runner.preflight_registration(spec)

    def test_preflight_accepts_clean_lane_and_preserves_raw_mark_price(self) -> None:
        agent = FakeOndoPerpsAgent()
        agent.scripted[("market_price", "ONDO")] = {
            "success": True,
            "operation": "market_price",
            "exchange": "ondoperps",
            "account": "amiroo",
            "market_price": {
                "requested_symbol": "ONDO",
                "market": "ONDO-USD.P",
                "markPrice": "0.333687",
                "oraclePrice": "0.33374",
                "lastExternalPrice": "0.33374",
                "lastUpdatedTime": "ts",
            },
        }
        agent.scripted[("position_state", "ONDO")] = {
            "success": True,
            "operation": "position_state",
            "exchange": "ondoperps",
            "account": "amiroo",
            "positions": [],
        }
        agent.scripted[("positions_orders", "*")] = {
            "success": True,
            "operation": "positions_orders",
            "exchange": "ondoperps",
            "account": "amiroo",
            "positions": [],
            "open_order_count": 0,
            "order_groups": [],
        }
        bundle = {
            "agent": agent,
            "adapter": FakeAdapter(),
            "quote_source": FakeQuoteSource(quotes_by_symbol={"ONDO": [Quote(bid=0.333687, ask=0.333687)]}),
        }
        runner = FiboLiveRunner(runtime_registry={"ondoperps": lambda spec: bundle})
        spec = RegistrationSpec(
            exchange="ondoperps", account="amiroo", instrument="ONDO",
            counter_type=CounterType.COUNTER_SELL,
            divide_percent=1000, counter1=1, counter2=0, counter3=0, counter4=0,
        )
        snapshot = runner.preflight_registration(spec)
        self.assertEqual(snapshot.mark_price_raw, 0.333687)
        self.assertTrue(snapshot.is_clean)

    def test_runner_raw_price_is_logged_without_rounding(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sink = JsonlLogSink(Path(td) / "runner.log")
            sink.write({"event": "step0_seeded", "step0_raw": 0.333687})
            rows = [json.loads(x) for x in (Path(td) / "runner.log").read_text().splitlines()]
            self.assertEqual(rows[0]["step0_raw"], 0.333687)

    def test_jsonl_log_sink_falls_back_on_unserializable_payload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sink = JsonlLogSink(Path(td) / "runner.log")
            sink.write({"event": "odd_payload", "value": object()})
            rows = [json.loads(x) for x in (Path(td) / "runner.log").read_text().splitlines()]
            self.assertEqual(rows[0]["event"], "odd_payload")
            self.assertIsInstance(rows[0]["value"], str)

    def test_sink_failure_after_fill_does_not_prevent_sl_or_duplicate_order(self) -> None:
        agent = FakeOndoPerpsAgent()
        agent.scripted[("market_price", "ONDO")] = {
            "success": True,
            "operation": "market_price",
            "exchange": "ondoperps",
            "account": "amiroo",
            "market_price": {"requested_symbol": "ONDO", "market": "ONDO-USD.P", "markPrice": "0.333687"},
        }
        agent.scripted[("position_state", "ONDO")] = {
            "success": True,
            "operation": "position_state",
            "exchange": "ondoperps",
            "account": "amiroo",
            "positions": [],
        }
        agent.scripted[("positions_orders", "*")] = {
            "success": True,
            "operation": "positions_orders",
            "exchange": "ondoperps",
            "account": "amiroo",
            "positions": [],
            "open_order_count": 0,
            "order_groups": [],
        }
        adapter = FakeAdapter()
        quote_source = FakeQuoteSource(quotes_by_symbol={
            "ONDO": [Quote(bid=0.333687, ask=0.333687), Quote(bid=0.333500, ask=0.333500)]
        })
        bundle = {"agent": agent, "adapter": adapter, "quote_source": quote_source}
        sink = FailingSink("cumulative_position_confirmed")
        runner = FiboLiveRunner(runtime_registry={"ondoperps": lambda spec: bundle}, sleep_fn=lambda _: None, log_sink=sink)
        spec = RegistrationSpec(
            exchange="ondoperps", account="amiroo", instrument="ONDO",
            counter_type=CounterType.COUNTER_SELL,
            divide_percent=1000, counter1=1, counter2=0, counter3=0, counter4=0,
        )
        runner.start_registration(spec)
        runner.run_loop(max_iterations=2)
        self.assertEqual(len(adapter.submissions), 1)
        self.assertEqual(len(adapter.set_sl_calls), 1)
        self.assertEqual(len(adapter.verify_sl_calls), 1)
        self.assertFalse(runner.manager.list_running()[0].frozen)

    def test_runner_persists_critical_handoff_events_to_disk_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = SequencedPositionAgent([
                _ok("position_state", positions=[]),
                _ok("position_state", positions=[{
                    "symbol": "ONDO", "side": "short", "size": "1",
                    "entry_price": "1", "pnl": "0",
                    "tp": None, "sl": None,
                    "tp_count": None, "sl_count": None,
                }]),
                _ok("position_state", positions=[{
                    "symbol": "ONDO", "side": "short", "size": "1",
                    "entry_price": "1", "pnl": "0",
                    "tp": None, "sl": "1",
                    "tp_count": None, "sl_count": 1,
                }]),
            ])
            agent.scripted[("market_price", "ONDO")] = {
                "success": True,
                "operation": "market_price",
                "exchange": "ondoperps",
                "account": "amiroo",
                "market_price": {"requested_symbol": "ONDO", "market": "ONDO-USD.P", "markPrice": "1.0"},
            }
            agent.scripted[("positions_orders", "*")] = {
                "success": True,
                "operation": "positions_orders",
                "exchange": "ondoperps",
                "account": "amiroo",
                "positions": [],
                "open_order_count": 0,
                "order_groups": [],
            }
            adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
            quote_source = FakeQuoteSource(quotes_by_symbol={
                "ONDO": [Quote(bid=1.0, ask=1.0), Quote(bid=0.5, ask=0.5)]
            })
            sink = JsonlLogSink(Path(td) / "runner.log")
            bundle = {"agent": agent, "adapter": adapter, "quote_source": quote_source}
            runner = FiboLiveRunner(runtime_registry={"ondoperps": lambda spec: bundle}, sleep_fn=lambda _: None, log_sink=sink)
            spec = RegistrationSpec(
                exchange="ondoperps", account="amiroo", instrument="ONDO",
                counter_type=CounterType.COUNTER_SELL,
                divide_percent=1000, counter1=1, counter2=0, counter3=0, counter4=0,
            )
            runner.start_registration(spec)
            runner.run_loop(max_iterations=2)
            events = [json.loads(x)["event"] for x in (Path(td) / "runner.log").read_text().splitlines()]
            for name in [
                "step0_seeded",
                "level_activated",
                "client_order_id_prepared",
                "order_create_response",
                "exact_order_verified",
                "position_confirmation_attempt",
                "cumulative_position_confirmed",
                "sl_requested",
                "sl_set_response",
                "sl_verified",
            ]:
                self.assertIn(name, events)
            self.assertLess(events.index("client_order_id_prepared"), events.index("order_create_response"))
            self.assertLess(events.index("order_create_response"), events.index("exact_order_verified"))
            self.assertLess(events.index("exact_order_verified"), events.index("position_confirmation_attempt"))
            self.assertLess(events.index("position_confirmation_attempt"), events.index("cumulative_position_confirmed"))
            self.assertLess(events.index("cumulative_position_confirmed"), events.index("sl_requested"))
            self.assertLess(events.index("sl_requested"), events.index("sl_set_response"))
            self.assertLess(events.index("sl_set_response"), events.index("sl_verified"))

    def test_frozen_registration_stops_further_mutation(self) -> None:
        adapter = FakeAdapter(fail_set_sl=[1])
        manager = FiboManager()
        spec = RegistrationSpec(
            exchange="ondoperps", account="amiroo", instrument="ONDO",
            counter_type=CounterType.COUNTER_SELL,
            divide_percent=1000, counter1=1, counter2=0, counter3=0, counter4=0,
        )
        quote_source = FakeQuoteSource(quotes_by_symbol={
            "ONDO": [
                Quote(bid=0.333687, ask=0.333687),
                Quote(bid=0.333500, ask=0.333500),
                Quote(bid=0.333400, ask=0.333400),
            ]
        })
        manager.start(spec.to_fibo_config(), adapter, quote_source)
        runner = FiboLiveRunner(manager=manager, sleep_fn=lambda _: None)
        runner._started_specs[spec.key] = spec
        runner.run_loop(max_iterations=3)
        self.assertEqual(len(adapter.submissions), 1)
        self.assertEqual(len(adapter.verify_sl_calls), 0)
        self.assertEqual(len(adapter.set_tp_calls), 0)
        running = manager.list_running()[0]
        self.assertTrue(running.frozen)

    def test_multiple_registrations_can_share_one_manager(self) -> None:
        manager = FiboManager()
        spec_a = RegistrationSpec(
            exchange="ondoperps", account="amiroo", instrument="ONDO",
            counter_type=CounterType.COUNTER_SELL,
            divide_percent=1000, counter1=1, counter2=0, counter3=0, counter4=0,
        )
        spec_b = RegistrationSpec(
            exchange="ondoperps", account="bitget", instrument="US100",
            counter_type=CounterType.COUNTER_BUY,
            divide_percent=1000, counter1=1, counter2=0, counter3=0, counter4=0,
        )
        agent_a = FakeOndoPerpsAgent()
        agent_a.scripted[("market_price", "ONDO")] = {
            "success": True, "operation": "market_price", "exchange": "ondoperps", "account": "amiroo",
            "market_price": {"requested_symbol": "ONDO", "market": "ONDO-USD.P", "markPrice": "1.0"},
        }
        agent_a.scripted[("position_state", "ONDO")] = {
            "success": True, "operation": "position_state", "exchange": "ondoperps", "account": "amiroo", "positions": [],
        }
        agent_a.scripted[("positions_orders", "*")] = {
            "success": True, "operation": "positions_orders", "exchange": "ondoperps", "account": "amiroo",
            "positions": [], "open_order_count": 0, "order_groups": [],
        }
        agent_b = FakeOndoPerpsAgent()
        agent_b.scripted[("market_price", "US100")] = {
            "success": True, "operation": "market_price", "exchange": "ondoperps", "account": "bitget",
            "market_price": {"requested_symbol": "US100", "market": "US100-USD.P", "markPrice": "100.0"},
        }
        agent_b.scripted[("position_state", "US100")] = {
            "success": True, "operation": "position_state", "exchange": "ondoperps", "account": "bitget", "positions": [],
        }
        agent_b.scripted[("positions_orders", "*")] = {
            "success": True, "operation": "positions_orders", "exchange": "ondoperps", "account": "bitget",
            "positions": [], "open_order_count": 0, "order_groups": [],
        }
        bundle_a = {
            "agent": agent_a,
            "adapter": FakeAdapter(),
            "quote_source": FakeQuoteSource(quotes_by_symbol={"ONDO": [Quote(bid=1, ask=1)]}),
        }
        bundle_b = {
            "agent": agent_b,
            "adapter": FakeAdapter(),
            "quote_source": FakeQuoteSource(quotes_by_symbol={"US100": [Quote(bid=100, ask=100)]}),
        }
        registry = {
            "ondoperps": lambda spec: bundle_a if spec.account == "amiroo" else bundle_b,
        }
        runner = FiboLiveRunner(manager=manager, runtime_registry=registry)
        runner.start_registration(spec_a)
        runner.start_registration(spec_b)
        keys = sorted(x.key for x in manager.list_running())
        self.assertEqual(keys, sorted([spec_a.key, spec_b.key]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
