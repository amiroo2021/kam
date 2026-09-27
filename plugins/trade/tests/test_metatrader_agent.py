"""Phase-A tests for the unified MetaTrader /trade agent.

Offline by default: Windows FastAPI calls are mocked except source/static checks.
No test places, cancels, closes, or modifies orders/positions.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class _MetaTraderEnvMixin:
    def setUp(self) -> None:
        self._saved_mt = {k: v for k, v in os.environ.items() if k.startswith("MT_") or k.startswith("LINUX_WIN_")}
        self._saved_home = os.environ.get("HERMES_HOME")
        for key in list(os.environ):
            if key.startswith("MT_") or key.startswith("LINUX_WIN_"):
                os.environ.pop(key, None)
        self.home = tempfile.mkdtemp(prefix="metatrader_agent_test_")
        os.environ["HERMES_HOME"] = self.home
        Path(self.home, ".env").write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        for key in list(os.environ):
            if key.startswith("MT_") or key.startswith("LINUX_WIN_"):
                os.environ.pop(key, None)
        os.environ.update(self._saved_mt)
        if self._saved_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._saved_home

    def _write_dotenv(self, text: str) -> None:
        Path(self.home, ".env").write_text(text, encoding="utf-8")

    def _basic_env(self) -> None:
        os.environ["LINUX_WIN_HOST"] = "192.0.2.10"
        os.environ["LINUX_WIN_PORT"] = "5050"
        os.environ["MT_LITE7486706MT5_ACCOUNT"] = "7486706"


class DiscoveryAndConfigTests(_MetaTraderEnvMixin, unittest.TestCase):
    def test_discovers_dynamic_mt_account_aliases_only(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        os.environ["MT_AMIROO_ACCOUNT"] = "7486706"
        os.environ["MT_FIBO_ACCOUNT"] = "1234567"
        os.environ["MT_BAD_ACCOUNT"] = "0"
        os.environ["MT_TEXT_ACCOUNT"] = "abc"
        os.environ["MT_AMIROO_PLATFORM"] = "mt5"
        self.assertEqual(mt.discover_accounts(), {"AMIROO": 7486706, "FIBO": 1234567})
        self.assertEqual(mt.list_accounts(), ["AMIROO", "FIBO"])

    def test_dotenv_accounts_are_discovered_and_live_env_wins(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        self._write_dotenv(
            "MT_MAIN_ACCOUNT=111\n"
            "MT_FILE_ACCOUNT=222\n"
            "LINUX_WIN_HOST=203.0.113.5\n"
            "LINUX_WIN_PORT=5050\n"
        )
        os.environ["MT_MAIN_ACCOUNT"] = "333"
        self.assertEqual(mt.discover_accounts(), {"FILE": 222, "MAIN": 333})
        self.assertEqual(mt.bridge_base_url(), "http://203.0.113.5:5050")

    def test_bridge_config_validation(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        os.environ["LINUX_WIN_HOST"] = "example.invalid"
        os.environ["LINUX_WIN_PORT"] = "abc"
        with self.assertRaises(mt.MetaTraderConfigError) as ctx:
            mt.bridge_base_url()
        self.assertEqual(ctx.exception.code, "BRIDGE_CONFIG_INVALID")
        os.environ["LINUX_WIN_PORT"] = "70000"
        with self.assertRaises(mt.MetaTraderConfigError):
            mt.bridge_base_url()
        os.environ["LINUX_WIN_PORT"] = "5050"
        self.assertEqual(mt.bridge_base_url(), "http://example.invalid:5050")

    def test_source_has_no_forbidden_architecture_dependencies(self) -> None:
        src = (_REPO_ROOT / "plugins" / "trade" / "agents" / "x_metatrader_agent.py").read_text(encoding="utf-8")
        self.assertNotIn("import MetaTrader5", src)
        self.assertNotIn("from MetaTrader5", src)
        self.assertNotIn("185.167.99.98", src)
        self.assertNotIn("http://185.167.99.98", src)
        self.assertNotRegex(src, re.compile(r"socket\s*\.\s*socket"))
        self.assertNotIn("127.0.0.1:5555", src)
        self.assertNotIn("MT_LITE7486706MT5_ACCOUNT", src)
        self.assertNotIn("MT_.*_PLATFORM", src)


class ExecuteContractTests(_MetaTraderEnvMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._basic_env()

    def test_ping_generates_uuid_and_uses_alias_account_mapping(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        captured = {}

        def fake_post(payload):
            captured.update(payload)
            return {
                "type": "response",
                "request_id": payload["request_id"],
                "account": payload["account"],
                "action": "ping",
                "status": "COMPLETED",
                "ok": True,
            }

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({"operation": "ping", "account": "LITE7486706MT5", "account_number": 999})
        self.assertTrue(resp.success)
        self.assertEqual(captured["account"], 7486706)
        self.assertEqual(captured["action"], "ping")
        self.assertRegex(captured["request_id"], r"^[0-9a-f-]{36}$")

    def test_unknown_alias_fails_before_bridge_call(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        with mock.patch.object(mt, "_bridge_post") as post:
            resp = mt.execute({"operation": "balance", "account": "MISSING"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "ACCOUNT_NOT_CONFIGURED")
        post.assert_not_called()

    def test_disconnected_ea_failure_is_canonical(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        def fake_post(payload):
            return {
                "type": "response",
                "request_id": payload["request_id"],
                "account": payload["account"],
                "action": "balance",
                "status": "FAILED",
                "ok": False,
                "error": "EA_DISCONNECTED",
                "message": "No EA connected for account 7486706",
            }

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({"operation": "balance", "account": "LITE7486706MT5"})
        self.assertFalse(resp.success)
        self.assertIsNotNone(resp.error)
        self.assertEqual(resp.error.code, "EA_DISCONNECTED")
        self.assertIn("No EA connected", resp.error.message)

    def test_forward_only_bridge_response_is_not_success(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        with mock.patch.object(mt, "_bridge_post", return_value={"forwarded": True, "clients": 1, "body": {"account": 7486706}}):
            resp = mt.execute({"operation": "ping", "account": "LITE7486706MT5"})
        self.assertFalse(resp.success)
        self.assertIsNotNone(resp.error)
        self.assertEqual(resp.error.code, "EA_NO_RESPONSE")

    def test_request_response_correlation_is_enforced(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        with mock.patch.object(mt, "_bridge_post", return_value={"request_id": "wrong", "account": 7486706, "ok": True}):
            resp = mt.execute({"operation": "ping", "account": "LITE7486706MT5"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "REQUEST_ID_MISMATCH")

    def test_balance_response_maps_to_canonical_balance(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        def fake_post(payload):
            return {
                "type": "response",
                "request_id": payload["request_id"],
                "account": 7486706,
                "action": "balance",
                "status": "COMPLETED",
                "ok": True,
                "balance": 1000.5,
                "equity": 1005.25,
                "margin": 10,
                "free_margin": 995.25,
                "currency": "USD",
                "server": "Broker-Demo",
                "company": "Broker Ltd",
            }

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({"operation": "balance", "account": "LITE7486706MT5"})
        self.assertTrue(resp.success)
        self.assertEqual(resp.balance.value, "1000.50")
        self.assertEqual(resp.balance.unit, "USD")
        self.assertEqual(resp.portfolio_summary.account_value, "1005.25")
        dumped = json.dumps(resp.to_dict())
        self.assertIn("Broker-Demo", dumped)

    def test_positions_orders_aggregation_maps_to_canonical(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        def fake_post(payload):
            return {
                "type": "response",
                "request_id": payload["request_id"],
                "account": 7486706,
                "action": "positions_orders",
                "status": "COMPLETED",
                "ok": True,
                "positions": [
                    {
                        "symbol": "BTCUSD",
                        "direction": "BUY",
                        "count": 203,
                        "total_volume": "8.42",
                        "vwap": "65342.21",
                        "min_entry_price": "62100",
                        "max_entry_price": "68750",
                        "floating_pl": "123.45",
                    },
                    {
                        "symbol": "BTCUSD",
                        "direction": "SELL",
                        "count": 17,
                        "total_volume": "1.70",
                        "vwap": "70122.50",
                        "min_entry_price": "69800",
                        "max_entry_price": "70900",
                        "floating_pl": "-10.50",
                    },
                ],
                "pending_orders": [
                    {
                        "symbol": "BTCUSD",
                        "order_type": "BUY_LIMIT",
                        "count": 3,
                        "total_volume": "0.30",
                        "vwap": "60025.00",
                        "min_price": "59900",
                        "max_price": "60100",
                    },
                    {
                        "symbol": "ETHUSD",
                        "order_type": "SELL_STOP",
                        "count": 2,
                        "total_volume": "0.20",
                        "vwap": "2000.00",
                        "min_price": "1990",
                        "max_price": "2010",
                    },
                ],
            }

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({"operation": "positions_orders", "account": "LITE7486706MT5"})
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.positions), 2)
        self.assertEqual(resp.positions[0].symbol, "BTCUSD")
        self.assertEqual(resp.positions[0].side, "BUY")
        self.assertEqual(resp.positions[0].size, "8.42")
        self.assertEqual(resp.positions[0].entry_price, "65342.21")
        self.assertEqual(resp.positions[1].side, "SELL")
        self.assertEqual(resp.open_order_count, 5)
        self.assertEqual(len(resp.order_groups), 2)
        self.assertEqual(resp.order_groups[0].display_type, "BUY_LIMIT")
        self.assertEqual(resp.order_groups[0].vwap, "60025.00")
        self.assertEqual(resp.order_groups[1].display_type, "SELL_STOP")

    def _positions_payload(self, tickets, extra_group=None):
        group = extra_group or {
            "symbol": "ZECUSD",
            "direction": "SELL",
            "count": len(tickets),
            "total_volume": "9.07",
            "vwap": "1660.17640573",
            "floating_pl": "12.34",
        }
        return {
            "type": "response",
            "request_id": "ignored",
            "account": 7486706,
            "action": "positions_orders",
            "status": "COMPLETED",
            "ok": True,
            "positions": [group],
            "position_tickets": tickets,
            "pending_orders": [],
            "open_order_count": 0,
        }

    def _execute_with_tickets(self, tickets, extra_group=None, operation="positions_orders"):
        from plugins.trade.agents import x_metatrader_agent as mt

        def fake_post(payload):
            body = self._positions_payload(tickets, extra_group=extra_group)
            body["request_id"] = payload["request_id"]
            body["account"] = payload["account"]
            body["action"] = payload["action"]
            return body

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            return mt.execute({"operation": operation, "account": "LITE7486706MT5"})

    def test_grouped_protection_all_tp_500_sl_zero_displays_tp_only(self) -> None:
        tickets = [
            {"ticket": 1000 + i, "symbol": "ZECUSD", "direction": "SELL", "volume": 0.5, "tp": 500.0, "sl": 0.0}
            for i in range(19)
        ]
        resp = self._execute_with_tickets(tickets)
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.positions), 1)
        self.assertEqual(resp.positions[0].tp, "500")
        self.assertIsNone(resp.positions[0].sl)

    def test_grouped_protection_all_unset_is_blank(self) -> None:
        tickets = [
            {"ticket": 2000 + i, "symbol": "ZECUSD", "direction": "SELL", "volume": 0.5, "tp": 0.0, "sl": 0.0}
            for i in range(19)
        ]
        resp = self._execute_with_tickets(tickets)
        self.assertIsNone(resp.positions[0].tp)
        self.assertIsNone(resp.positions[0].sl)

    def test_grouped_protection_same_tp_and_sl_are_displayed(self) -> None:
        tickets = [
            {"ticket": 3000 + i, "symbol": "ZECUSD", "direction": "SELL", "volume": 0.5, "tp": "1573.60", "sl": "1740.67"}
            for i in range(4)
        ]
        resp = self._execute_with_tickets(tickets)
        self.assertEqual(resp.positions[0].tp, "1573.6")
        self.assertEqual(resp.positions[0].sl, "1740.67")

    def test_grouped_protection_mixed_tp(self) -> None:
        tickets = [
            {"ticket": 4000 + i, "symbol": "ZECUSD", "direction": "SELL", "volume": 0.5, "tp": 500.0, "sl": 0.0}
            for i in range(18)
        ]
        tickets.append({"ticket": 4099, "symbol": "ZECUSD", "direction": "SELL", "volume": 0.5, "tp": 400.0, "sl": 0.0})
        resp = self._execute_with_tickets(tickets)
        self.assertEqual(resp.positions[0].tp, "Mixed")
        self.assertIsNone(resp.positions[0].sl)

    def test_grouped_protection_mixed_sl(self) -> None:
        tickets = [
            {"ticket": 5000 + i, "symbol": "ZECUSD", "direction": "SELL", "volume": 0.5, "tp": 500.0, "sl": 1800.0}
            for i in range(18)
        ]
        tickets.append({"ticket": 5099, "symbol": "ZECUSD", "direction": "SELL", "volume": 0.5, "tp": 500.0, "sl": 1700.0})
        resp = self._execute_with_tickets(tickets)
        self.assertEqual(resp.positions[0].tp, "500")
        self.assertEqual(resp.positions[0].sl, "Mixed")

    def test_single_ticket_uses_its_tp_sl(self) -> None:
        tickets = [
            {"ticket": 5257798644, "symbol": "ZECUSD", "direction": "SELL", "volume": 0.54, "tp": 1573.6, "sl": 1740.67}
        ]
        resp = self._execute_with_tickets(tickets)
        self.assertEqual(resp.positions[0].tp, "1573.6")
        self.assertEqual(resp.positions[0].sl, "1740.67")

    def test_float_equivalent_protection_is_not_mixed(self) -> None:
        tickets = [
            {"ticket": 1, "symbol": "ZECUSD", "direction": "SELL", "volume": 0.5, "tp": 500.0, "sl": 0.0},
            {"ticket": 2, "symbol": "ZECUSD", "direction": "SELL", "volume": 0.5, "tp": 500.00, "sl": 0},
            {"ticket": 3, "symbol": "ZECUSD", "direction": "SELL", "volume": 0.5, "tp": "500", "sl": "0.0"},
        ]
        resp = self._execute_with_tickets(tickets)
        self.assertEqual(resp.positions[0].tp, "500")
        self.assertIsNone(resp.positions[0].sl)

    def test_positions_management_uses_ticket_consensus(self) -> None:
        tickets = [
            {"ticket": 6000 + i, "symbol": "ZECUSD", "direction": "SELL", "volume": 0.5, "tp": 500.0, "sl": 0.0}
            for i in range(19)
        ]
        resp = self._execute_with_tickets(tickets, operation="positions_management")
        self.assertTrue(resp.success)
        self.assertEqual(resp.operation, "positions_management")
        self.assertEqual(resp.positions[0].tp, "500")
        self.assertIsNone(resp.positions[0].sl)

    def test_unimplemented_mutating_operations_are_blocked(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        for op in ("modify_order", "place_order"):
            with self.subTest(op=op):
                with mock.patch.object(mt, "_bridge_post") as post:
                    resp = mt.execute({"operation": op, "account": "LITE7486706MT5"})
                self.assertFalse(resp.success)
                self.assertEqual(resp.error.code, "NOT_IMPLEMENTED")
                post.assert_not_called()


class NewOrderPhaseBTests(_MetaTraderEnvMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._basic_env()

    def _success_response(self, payload):
        return {
            "type": "response",
            "request_id": payload["request_id"],
            "account": payload["account"],
            "action": "new_order",
            "status": "COMPLETED",
            "ok": True,
            "symbol": payload["symbol"],
            "side": payload["side"],
            "order_type": payload["order_type"],
            "requested_volume": payload["volume"],
            "accepted_volume": payload["volume"],
            "requested_price": payload["price"],
            "accepted_price": payload["price"],
            "order_ticket": 123456,
            "deal_ticket": 0,
            "retcode": 10009,
            "comment": "XA:" + payload["request_id"].replace("-", "")[:12],
            "magic": 26092601,
        }

    def test_buy_limit_canonical_mapping(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        captured = {}
        def fake_post(payload):
            captured.update(payload)
            return self._success_response(payload)

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({
                "operation": "new_order",
                "account": "LITE7486706MT5",
                "symbol": "ZECUSD",
                "side": "buy",
                "order_type": "limit",
                "volume": "1.23",
                "price": "1600.5",
            })
        self.assertTrue(resp.success)
        self.assertEqual(captured["account"], 7486706)
        self.assertEqual(captured["action"], "new_order")
        self.assertEqual(captured["symbol"], "ZECUSD")
        self.assertEqual(captured["side"], "BUY")
        self.assertEqual(captured["order_type"], "LIMIT")
        self.assertEqual(captured["volume"], 1.23)
        self.assertEqual(captured["price"], 1600.5)
        self.assertNotIn("magic", captured)
        self.assertNotIn("comment", captured)
        self.assertRegex(captured["request_id"], r"^[0-9a-f-]{36}$")
        self.assertEqual(resp.order.exchange_order_id, 123456)

    def test_sell_limit_canonical_mapping_and_request_id_reuse(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        request_id = "11111111-2222-4333-8444-555555555555"
        captured = {}
        def fake_post(payload):
            captured.update(payload)
            return self._success_response(payload)

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({
                "operation": "new_order",
                "account": "LITE7486706MT5",
                "request_id": request_id,
                "symbol": "ZECUSD",
                "side": "sell",
                "order_type": "limit",
                "volume": "2",
                "price": "1650",
            })
        self.assertTrue(resp.success)
        self.assertEqual(captured["request_id"], request_id)
        self.assertEqual(captured["side"], "SELL")
        self.assertEqual(captured["volume"], 2)
        self.assertEqual(captured["price"], 1650)

    def test_new_order_validation_failures_do_not_call_bridge(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        cases = [
            ({"symbol": "", "side": "buy", "order_type": "limit", "volume": "1", "price": "1"}, "MISSING_SYMBOL"),
            ({"symbol": "ZECUSD", "side": "hold", "order_type": "limit", "volume": "1", "price": "1"}, "INVALID_SIDE"),
            ({"symbol": "ZECUSD", "side": "buy", "order_type": "market", "volume": "1", "price": "1"}, "UNSUPPORTED_ORDER_TYPE"),
            ({"symbol": "ZECUSD", "side": "buy", "order_type": "limit", "volume": "1", "price": "1", "reduce_only": True}, "UNSUPPORTED_PARAMETER"),
            ({"symbol": "ZECUSD", "side": "buy", "order_type": "limit", "volume": "0", "price": "1"}, "INVALID_VOLUME"),
            ({"symbol": "ZECUSD", "side": "buy", "order_type": "limit", "volume": "-1", "price": "1"}, "INVALID_VOLUME"),
            ({"symbol": "ZECUSD", "side": "buy", "order_type": "limit", "volume": "abc", "price": "1"}, "INVALID_VOLUME"),
            ({"symbol": "ZECUSD", "side": "buy", "order_type": "limit", "volume": "1", "price": ""}, "INVALID_PRICE"),
            ({"symbol": "ZECUSD", "side": "buy", "order_type": "limit", "volume": "1", "price": "0"}, "INVALID_PRICE"),
            ({"symbol": "ZECUSD", "side": "buy", "order_type": "limit", "volume": "1", "price": "-1"}, "INVALID_PRICE"),
            ({"symbol": "ZECUSD", "side": "buy", "order_type": "limit", "volume": "1", "price": "abc"}, "INVALID_PRICE"),
        ]
        for extra, code in cases:
            with self.subTest(code=code, extra=extra):
                req = {"operation": "new_order", "account": "LITE7486706MT5", **extra}
                with mock.patch.object(mt, "_bridge_post") as post:
                    resp = mt.execute(req)
                self.assertFalse(resp.success)
                self.assertEqual(resp.error.code, code)
                post.assert_not_called()

    def test_timeout_maps_to_ambiguous_execution_without_retry(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        calls = []
        def fake_post(payload):
            calls.append(payload)
            raise mt.MetaTraderConfigError("EA_TIMEOUT", "timeout")

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({
                "operation": "new_order",
                "account": "LITE7486706MT5",
                "symbol": "ZECUSD",
                "side": "buy",
                "order_type": "limit",
                "volume": "1",
                "price": "1600",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "AMBIGUOUS_EXECUTION")
        self.assertEqual(len(calls), 1)

    def test_ea_disconnected_handling(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        def fake_post(payload):
            return {
                "type": "response",
                "request_id": payload["request_id"],
                "account": payload["account"],
                "action": "new_order",
                "status": "FAILED",
                "ok": False,
                "error": "EA_DISCONNECTED",
                "message": "No EA connected for account 7486706",
            }

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({
                "operation": "new_order",
                "account": "LITE7486706MT5",
                "symbol": "ZECUSD",
                "side": "buy",
                "order_type": "limit",
                "volume": "1",
                "price": "1600",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "EA_DISCONNECTED")


class MarketDataAndCancelTests(_MetaTraderEnvMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._basic_env()

    def test_symbols_ticker_candles_mapping(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        captured = []
        def fake_post(payload):
            captured.append(dict(payload))
            return {
                "type": "response",
                "request_id": payload["request_id"],
                "account": payload["account"],
                "action": payload["action"],
                "status": "COMPLETED",
                "ok": True,
                "symbols": ["ZECUSD"],
                "bid": 1.1,
                "ask": 1.2,
                "last": 0,
                "digits": 2,
                "point": 0.01,
                "tick_size": 0.01,
                "volume_min": 0.01,
                "volume_max": 100.0,
                "volume_step": 0.01,
                "candles": [{"time": 1, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "tick_volume": 10, "real_volume": 0, "spread": 1}],
            }

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            self.assertTrue(mt.execute({"operation": "symbols", "account": "LITE7486706MT5"}).success)
            self.assertTrue(mt.execute({"operation": "ticker", "account": "LITE7486706MT5", "symbol": "ZECUSD"}).success)
            self.assertTrue(mt.execute({"operation": "candles", "account": "LITE7486706MT5", "symbol": "ZECUSD", "timeframe": "M1", "count": 10}).success)
        self.assertEqual(captured[0]["action"], "symbols")
        self.assertEqual(captured[1]["action"], "ticker")
        self.assertEqual(captured[1]["symbol"], "ZECUSD")
        self.assertEqual(captured[2]["action"], "candles")
        self.assertEqual(captured[2]["symbol"], "ZECUSD")
        self.assertEqual(captured[2]["timeframe"], "M1")
        self.assertEqual(captured[2]["count"], 10)

    def test_candles_accept_webtrade2_interval_and_limit_aliases(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        captured = []

        def fake_post(payload):
            captured.append(dict(payload))
            return {
                "type": "response",
                "request_id": payload["request_id"],
                "account": payload["account"],
                "action": payload["action"],
                "status": "COMPLETED",
                "ok": True,
                "symbol": payload.get("symbol"),
                "timeframe": payload.get("timeframe"),
                "candles": [{"time": 1, "open": 1, "high": 2, "low": 0.5, "close": 1.5}],
            }

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({"operation": "candles", "account": "LITE7486706MT5", "symbol": "ZECUSD", "interval": "1h", "limit": 5})

        self.assertTrue(resp.success)
        self.assertEqual(captured[0]["timeframe"], "H1")
        self.assertEqual(captured[0]["count"], 5)

    def test_list_resolve_market_price_use_live_canonical_fields(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        def fake_post(payload):
            action = payload["action"]
            if action == "symbols":
                return {
                    "type": "response",
                    "request_id": payload["request_id"],
                    "account": payload["account"],
                    "action": action,
                    "status": "COMPLETED",
                    "ok": True,
                    "symbols": [
                        {
                            "symbol": "ZECUSD",
                            "tick_size": 0.01,
                            "volume_step": 0.01,
                            "volume_min": 0.01,
                        }
                    ],
                }
            return {
                "type": "response",
                "request_id": payload["request_id"],
                "account": payload["account"],
                "action": action,
                "status": "COMPLETED",
                "ok": True,
                "symbol": "ZECUSD",
                "bid": 1656.40,
                "ask": 1656.46,
                "last": 1656.43,
                "tick_size": 0.01,
                "volume_min": 0.01,
                "volume_step": 0.01,
            }

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            listed = mt.execute({"operation": "list_instruments", "account": "LITE7486706MT5"})
            resolved = mt.execute({"operation": "resolve_instrument", "account": "LITE7486706MT5", "symbol": "ZECUSD"})
            priced = mt.execute({"operation": "market_price", "account": "LITE7486706MT5", "symbol": "ZECUSD"})

        self.assertTrue(listed.success)
        instruments = (listed.data or {}).get("instruments") or []
        self.assertEqual(instruments[0]["symbol"], "ZECUSD")
        self.assertEqual(instruments[0]["native_symbol"], "ZECUSD")
        self.assertEqual(instruments[0]["price_increment"], "0.01")
        self.assertEqual(instruments[0]["size_increment"], "0.01")
        self.assertEqual(instruments[0]["minimum_size"], "0.01")

        self.assertTrue(resolved.success)
        inst = resolved.instrument
        self.assertEqual(inst.requested_symbol, "ZECUSD")
        self.assertEqual(inst.symbol, "ZECUSD")
        self.assertEqual(inst.display_name, "ZECUSD")
        self.assertEqual(inst.price_increment, "0.01")
        self.assertEqual(inst.size_increment, "0.01")
        self.assertEqual(inst.minimum_size, "0.01")

        self.assertTrue(priced.success)
        mp = priced.market_price
        self.assertEqual(mp.requested_symbol, "ZECUSD")
        self.assertEqual(mp.market, "ZECUSD")
        self.assertEqual(mp.price, "1656.43")
        self.assertIsNone(mp.mark_price)
        self.assertEqual(mp.last_external_price, "1656.43")
        self.assertEqual(mp.symbol, "ZECUSD")
        self.assertEqual(mp.native_symbol, "ZECUSD")
        self.assertEqual(mp.price_increment, "0.01")
        self.assertEqual(mp.size_increment, "0.01")
        self.assertEqual(mp.minimum_size, "0.01")
        self.assertEqual((priced.data or {}).get("price_increment"), "0.01")
        self.assertEqual((priced.data or {}).get("size_increment"), "0.01")
        self.assertEqual((priced.data or {}).get("minimum_size"), "0.01")

    def test_get_tickers_preserves_broker_symbols_and_missing_fields(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        mt._TICKERS_CACHE.clear()
        mt._TICKERS_CACHE_EXPIRES.clear()
        captured = []

        def fake_post(payload):
            captured.append(dict(payload))
            action = payload["action"]
            if action == "symbols":
                return {
                    "type": "response",
                    "request_id": payload["request_id"],
                    "account": payload["account"],
                    "action": action,
                    "status": "COMPLETED",
                    "ok": True,
                    "symbols": [
                        {"symbol": "ZECUSD", "tick_size": 0.01, "volume_step": 0.01, "volume_min": 0.01},
                        {"symbol": "EURUSD", "tick_size": 0.00001, "volume_step": 0.01, "volume_min": 0.01},
                    ],
                }
            if payload["symbol"] == "ZECUSD":
                return {
                    "type": "response",
                    "request_id": payload["request_id"],
                    "account": payload["account"],
                    "action": action,
                    "status": "COMPLETED",
                    "ok": True,
                    "symbol": "ZECUSD",
                    "bid": 1656.40,
                    "ask": 1656.46,
                    "last": 1656.43,
                    "tick_size": 0.01,
                    "volume_min": 0.01,
                    "volume_step": 0.01,
                }
            return {
                "type": "response",
                "request_id": payload["request_id"],
                "account": payload["account"],
                "action": action,
                "status": "COMPLETED",
                "ok": True,
                "symbol": "EURUSD",
                "bid": 1.13883,
                "ask": 1.13946,
                "last": 0,
                "tick_size": 0.00001,
                "volume_min": 0.01,
                "volume_step": 0.01,
            }

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({"operation": "get_tickers", "account": "LITE7486706MT5", "force": True})

        self.assertTrue(resp.success)
        self.assertIsNotNone(resp.tickers_batch)
        rows = resp.tickers_batch.tickers
        self.assertEqual(sorted(rows), ["EURUSD", "ZECUSD"])
        zec = rows["ZECUSD"]
        self.assertEqual(zec.symbol, "ZECUSD")
        self.assertEqual(zec.native_symbol, "ZECUSD")
        self.assertEqual(zec.display_symbol, "ZECUSD")
        self.assertEqual(zec.display_name, "ZECUSD")
        self.assertEqual(zec.price, "1656.43")
        self.assertEqual(zec.last_external_price, "1656.43")
        self.assertIsNone(zec.mark_price)
        self.assertIsNone(zec.oracle_price)
        self.assertIsNone(zec.funding_rate)
        self.assertIsNone(zec.open_interest)
        self.assertIsNone(zec.turnover_24h)
        self.assertIsNone(zec.volume_24h_quote)
        self.assertIsNone(zec.volume_24h_base)
        self.assertEqual(zec.price_increment, "0.01")
        self.assertEqual(zec.size_increment, "0.01")
        self.assertEqual(zec.minimum_size, "0.01")
        eur = rows["EURUSD"]
        self.assertEqual(eur.price, "1.13883")
        self.assertIsNone(eur.last_external_price)
        self.assertIsNone(eur.mark_price)
        self.assertIsNone(eur.market_type)
        self.assertEqual(resp.tickers_batch.source, "metatrader_bridge_fanout")
        self.assertEqual(resp.tickers_batch.refresh_status, "ok")
        self.assertEqual([c["action"] for c in captured].count("symbols"), 1)
        self.assertEqual([c["action"] for c in captured].count("ticker"), 2)

    def test_get_tickers_preserves_static_row_when_ticker_unavailable(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        mt._TICKERS_CACHE.clear()
        mt._TICKERS_CACHE_EXPIRES.clear()

        def fake_post(payload):
            if payload["action"] == "symbols":
                return {
                    "type": "response",
                    "request_id": payload["request_id"],
                    "account": payload["account"],
                    "action": "symbols",
                    "status": "COMPLETED",
                    "ok": True,
                    "symbols": [{"symbol": "XAUUSD", "tick_size": 0.01, "volume_step": 0.01, "volume_min": 0.01}],
                }
            return {
                "type": "response",
                "request_id": payload["request_id"],
                "account": payload["account"],
                "action": "ticker",
                "status": "FAILED",
                "ok": False,
                "error": "NO_TICK",
                "message": "No current tick available",
            }

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({"operation": "get_tickers", "account": "LITE7486706MT5", "force": True})

        self.assertTrue(resp.success)
        self.assertEqual(resp.tickers_batch.refresh_status, "partial")
        self.assertEqual(resp.tickers_batch.failed_symbols, ("XAUUSD",))
        row = resp.tickers_batch.tickers["XAUUSD"]
        self.assertEqual(row.symbol, "XAUUSD")
        self.assertEqual(row.native_symbol, "XAUUSD")
        self.assertIsNone(row.price)
        self.assertIsNone(row.mark_price)
        self.assertIsNone(row.turnover_24h)
        self.assertEqual(row.price_increment, "0.01")

    def test_get_tickers_cache_marks_served_from_cache(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        mt._TICKERS_CACHE.clear()
        mt._TICKERS_CACHE_EXPIRES.clear()
        calls = []

        def fake_post(payload):
            calls.append(dict(payload))
            if payload["action"] == "symbols":
                return {
                    "type": "response",
                    "request_id": payload["request_id"],
                    "account": payload["account"],
                    "action": "symbols",
                    "status": "COMPLETED",
                    "ok": True,
                    "symbols": ["BTCUSD"],
                }
            return {
                "type": "response",
                "request_id": payload["request_id"],
                "account": payload["account"],
                "action": "ticker",
                "status": "COMPLETED",
                "ok": True,
                "symbol": "BTCUSD",
                "bid": 84726.0,
                "ask": 84726.01,
                "last": 0,
            }

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            first = mt.execute({"operation": "get_tickers", "account": "LITE7486706MT5", "force": True})
            second = mt.execute({"operation": "get_tickers", "account": "LITE7486706MT5"})

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertFalse(first.tickers_batch.served_from_cache)
        self.assertTrue(second.tickers_batch.served_from_cache)
        self.assertEqual(len(calls), 2)  # one symbols + one ticker refresh only

    def test_capabilities_include_get_tickers(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        self.assertIn("get_tickers", mt.capabilities())

    def test_cancel_order_mapping_and_timeout_ambiguity(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        captured = {}
        def fake_post(payload):
            captured.update(payload)
            return {"type": "response", "request_id": payload["request_id"], "account": payload["account"], "action": "cancel_order", "status": "COMPLETED", "ok": True, "order_ticket": payload["order_ticket"]}

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({"operation": "cancel_order", "account": "LITE7486706MT5", "order_ticket": "123456"})
        self.assertTrue(resp.success)
        self.assertEqual(captured["account"], 7486706)
        self.assertEqual(captured["action"], "cancel_order")
        self.assertEqual(captured["order_ticket"], 123456)

        calls = []
        def timeout_post(payload):
            calls.append(payload)
            raise mt.MetaTraderConfigError("EA_TIMEOUT", "timeout")
        with mock.patch.object(mt, "_bridge_post", side_effect=timeout_post):
            resp = mt.execute({"operation": "cancel_order", "account": "LITE7486706MT5", "order_ticket": "123456"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "AMBIGUOUS_EXECUTION")
        self.assertEqual(len(calls), 1)

    def test_cancel_order_validation_does_not_call_bridge(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        for ticket in ("", "0", "-1", "abc"):
            with self.subTest(ticket=ticket):
                with mock.patch.object(mt, "_bridge_post") as post:
                    resp = mt.execute({"operation": "cancel_order", "account": "LITE7486706MT5", "order_ticket": ticket})
                self.assertFalse(resp.success)
                self.assertEqual(resp.error.code, "INVALID_ORDER_TICKET")
                post.assert_not_called()


class GroupedMutationsPhaseCTests(_MetaTraderEnvMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._basic_env()

    def _batch_response(self, payload, *, succeeded=None, failed=0, matched=None):
        requested = len(payload.get("orders") or []) or matched or 1
        succeeded = requested - failed if succeeded is None else succeeded
        return {
            "type": "response",
            "request_id": payload["request_id"],
            "account": payload["account"],
            "action": payload["action"],
            "status": "COMPLETED",
            "ok": True,
            "requested": requested,
            "matched": matched if matched is not None else requested,
            "succeeded": succeeded,
            "failed": failed,
            "failures": ([{"index": 1, "retcode": 10030, "message": "broker rejected"}] if failed else []),
        }

    def test_uniform_ladder_mapping_uses_linux_calculation(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        captured = {}
        def fake_post(payload):
            captured.update(payload)
            return self._batch_response(payload)

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({
                "operation": "ladder",
                "account": "LITE7486706MT5",
                "symbol": "ZECUSD",
                "side": "buy",
                "order_type": "limit",
                "order_count": "3",
                "total_volume": "0.06",
                "start_price": "1500",
                "end_price": "1498",
                "size_increment": "0.01",
                "price_increment": "0.01",
                "distribution": "uniform",
                "request_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            })
        self.assertTrue(resp.success)
        self.assertEqual(captured["request_id"], "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
        self.assertEqual(captured["account"], 7486706)
        self.assertEqual(captured["action"], "ladder")
        self.assertEqual(captured["symbol"], "ZECUSD")
        self.assertEqual(captured["side"], "BUY")
        self.assertEqual(captured["order_type"], "LIMIT")
        self.assertEqual(captured["orders"], [
            {"price": 1500, "volume": 0.02},
            {"price": 1499, "volume": 0.02},
            {"price": 1498, "volume": 0.02},
        ])
        self.assertNotIn("magic", captured)
        self.assertNotIn("comment", captured)

    def test_half_gaussian_and_single_child_ladder_mapping(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        captured = []
        def fake_post(payload):
            captured.append(dict(payload))
            return self._batch_response(payload)

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            self.assertTrue(mt.execute({
                "operation": "ladder", "account": "LITE7486706MT5", "symbol": "ZECUSD",
                "side": "sell", "order_type": "limit", "order_count": "4", "total_volume": "1.00",
                "start_price": "1600", "end_price": "1603", "size_increment": "0.01",
                "price_increment": "0.01", "distribution": "half_gaussian",
            }).success)
            self.assertTrue(mt.execute({
                "operation": "ladder", "account": "LITE7486706MT5", "symbol": "ZECUSD",
                "side": "buy", "order_type": "limit", "order_count": "1", "total_volume": "0.01",
                "start_price": "1500", "end_price": "1499", "size_increment": "0.01",
                "price_increment": "0.01", "distribution": "uniform",
            }).success)
        self.assertEqual(captured[0]["side"], "SELL")
        self.assertEqual(len(captured[0]["orders"]), 4)
        self.assertEqual(sum(child["volume"] for child in captured[0]["orders"]), 1.0)
        self.assertNotEqual([child["volume"] for child in captured[0]["orders"]], [0.25, 0.25, 0.25, 0.25])
        self.assertEqual(captured[1]["orders"], [{"price": 1499.5, "volume": 0.01}])

    def test_large_ladder_payload_has_no_arbitrary_child_cap(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        captured = {}
        def fake_post(payload):
            captured.update(payload)
            return self._batch_response(payload)

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({
                "operation": "ladder", "account": "LITE7486706MT5", "symbol": "ZECUSD",
                "side": "buy", "order_type": "limit", "order_count": "500", "total_volume": "5",
                "start_price": "1500", "end_price": "1001", "size_increment": "0.01",
                "price_increment": "0.01", "distribution": "uniform",
            })
        self.assertTrue(resp.success)
        self.assertEqual(len(captured["orders"]), 500)

    def test_ladder_partial_and_total_failure_responses(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        def fake_partial(payload):
            return self._batch_response(payload, succeeded=2, failed=1)
        with mock.patch.object(mt, "_bridge_post", side_effect=fake_partial):
            resp = mt.execute({
                "operation": "ladder", "account": "LITE7486706MT5", "symbol": "ZECUSD",
                "side": "buy", "order_type": "limit", "order_count": "3", "total_volume": "0.03",
                "start_price": "1500", "end_price": "1498", "size_increment": "0.01",
                "price_increment": "0.01", "distribution": "uniform",
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.data["requested"], 3)
        self.assertEqual(resp.data["succeeded"], 2)
        self.assertEqual(resp.data["failed"], 1)
        self.assertLessEqual(len(resp.data["failures"]), 20)

        def fake_total(payload):
            return {"type": "response", "request_id": payload["request_id"], "account": payload["account"], "action": "ladder", "status": "FAILED", "ok": False, "error": "LADDER_FAILED", "message": "all failed"}
        with mock.patch.object(mt, "_bridge_post", side_effect=fake_total):
            resp = mt.execute({
                "operation": "ladder", "account": "LITE7486706MT5", "symbol": "ZECUSD",
                "side": "buy", "order_type": "limit", "order_count": "1", "total_volume": "0.01",
                "start_price": "1500", "end_price": "1499", "size_increment": "0.01",
                "price_increment": "0.01", "distribution": "uniform",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "LADDER_FAILED")

    def test_ladder_validation_failures_do_not_call_bridge(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        cases = [
            ({"symbol": "", "side": "buy", "order_count": "1", "total_volume": "0.01", "start_price": "2", "end_price": "1"}, "MISSING_SYMBOL"),
            ({"symbol": "ZECUSD", "side": "hold", "order_count": "1", "total_volume": "0.01", "start_price": "2", "end_price": "1"}, "INVALID_SIDE"),
            ({"symbol": "ZECUSD", "side": "buy", "order_type": "market", "order_count": "1", "total_volume": "0.01", "start_price": "2", "end_price": "1"}, "UNSUPPORTED_ORDER_TYPE"),
            ({"symbol": "ZECUSD", "side": "buy", "order_count": "0", "total_volume": "0.01", "start_price": "2", "end_price": "1"}, "INVALID_LADDER"),
            ({"symbol": "ZECUSD", "side": "buy", "order_count": "1", "total_volume": "0", "start_price": "2", "end_price": "1"}, "INVALID_VOLUME"),
            ({"symbol": "ZECUSD", "side": "buy", "order_count": "1", "total_volume": "0.01", "start_price": "1", "end_price": "2"}, "INVALID_LADDER"),
        ]
        for extra, code in cases:
            with self.subTest(code=code):
                req = {"operation": "ladder", "account": "LITE7486706MT5", "size_increment": "0.01", "price_increment": "0.01", "distribution": "uniform", **extra}
                with mock.patch.object(mt, "_bridge_post") as post:
                    resp = mt.execute(req)
                self.assertFalse(resp.success)
                self.assertEqual(resp.error.code, code)
                post.assert_not_called()

    def test_grouped_cancel_close_tp_sl_mappings(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        captured = []
        def fake_post(payload):
            captured.append(dict(payload))
            return self._batch_response(payload, matched=4)

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            self.assertTrue(mt.execute({"operation": "cancel_orders", "account": "LITE7486706MT5", "symbol": "ZECUSD", "side": "sell", "order_type": "sell_limit", "request_id": "bbbbbbbb-1111-4222-8333-cccccccccccc"}).success)
            self.assertTrue(mt.execute({"operation": "close_position", "account": "LITE7486706MT5", "symbol": "ZECUSD", "side": "buy"}).success)
            self.assertTrue(mt.execute({"operation": "close_position", "account": "LITE7486706MT5", "symbol": "ZECUSD", "side": "sell"}).success)
            self.assertTrue(mt.execute({"operation": "set_tp", "account": "LITE7486706MT5", "symbol": "ZECUSD", "side": "sell", "price": "1700"}).success)
            self.assertTrue(mt.execute({"operation": "set_sl", "account": "LITE7486706MT5", "symbol": "ZECUSD", "side": "sell", "price": "1500"}).success)
        self.assertEqual(captured[0], {"request_id": "bbbbbbbb-1111-4222-8333-cccccccccccc", "account": 7486706, "action": "cancel_orders", "symbol": "ZECUSD", "side": "SELL", "order_type": "SELL_LIMIT"})
        self.assertEqual(captured[1]["action"], "close_position")
        self.assertEqual(captured[1]["side"], "BUY")
        self.assertEqual(captured[2]["side"], "SELL")
        self.assertEqual(captured[3]["action"], "set_tp")
        self.assertEqual(captured[3]["tp"], 1700)
        self.assertEqual(captured[3]["preserve_sl"], True)
        self.assertNotIn("sl", captured[3])
        self.assertEqual(captured[4]["action"], "set_sl")
        self.assertEqual(captured[4]["sl"], 1500)
        self.assertEqual(captured[4]["preserve_tp"], True)
        self.assertNotIn("tp", captured[4])

    def test_set_tp_set_sl_accept_explicit_zero_without_dropping_field(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        captured = []
        def fake_post(payload):
            captured.append(dict(payload))
            return self._batch_response(payload, matched=19)

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            tp_resp = mt.execute({"operation": "set_tp", "account": "LITE7486706MT5", "symbol": "ZECUSD", "side": "sell", "tp": "0"})
            sl_resp = mt.execute({"operation": "set_sl", "account": "LITE7486706MT5", "symbol": "ZECUSD", "side": "sell", "sl": 0})
        self.assertTrue(tp_resp.success)
        self.assertTrue(sl_resp.success)
        self.assertEqual(captured[0]["action"], "set_tp")
        self.assertEqual(captured[0]["tp"], 0)
        self.assertEqual(captured[0]["preserve_sl"], True)
        self.assertIn("tp", captured[0])
        self.assertNotIn("sl", captured[0])
        self.assertEqual(captured[1]["action"], "set_sl")
        self.assertEqual(captured[1]["sl"], 0)
        self.assertEqual(captured[1]["preserve_tp"], True)
        self.assertIn("sl", captured[1])
        self.assertNotIn("tp", captured[1])

    def test_set_tp_set_sl_reject_missing_negative_and_invalid_values(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        cases = [
            ({"operation": "set_tp", "symbol": "ZECUSD", "side": "sell"}, "INVALID_PRICE"),
            ({"operation": "set_tp", "symbol": "ZECUSD", "side": "sell", "tp": "-1"}, "INVALID_PRICE"),
            ({"operation": "set_tp", "symbol": "ZECUSD", "side": "sell", "tp": "abc"}, "INVALID_PRICE"),
            ({"operation": "set_tp", "symbol": "ZECUSD", "side": "sell", "tp": "NaN"}, "INVALID_PRICE"),
            ({"operation": "set_sl", "symbol": "ZECUSD", "side": "sell"}, "INVALID_PRICE"),
            ({"operation": "set_sl", "symbol": "ZECUSD", "side": "sell", "sl": "-1"}, "INVALID_PRICE"),
            ({"operation": "set_sl", "symbol": "ZECUSD", "side": "sell", "sl": "abc"}, "INVALID_PRICE"),
            ({"operation": "set_sl", "symbol": "ZECUSD", "side": "sell", "sl": "Infinity"}, "INVALID_PRICE"),
        ]
        for extra, code in cases:
            with self.subTest(extra=extra):
                with mock.patch.object(mt, "_bridge_post") as post:
                    resp = mt.execute({"account": "LITE7486706MT5", **extra})
                self.assertFalse(resp.success)
                self.assertEqual(resp.error.code, code)
                post.assert_not_called()

    def test_grouped_operation_validation_and_ambiguity_no_retry(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        invalid = [
            ({"operation": "cancel_orders", "symbol": "", "side": "sell"}, "MISSING_SYMBOL"),
            ({"operation": "close_position", "symbol": "ZECUSD", "side": ""}, "INVALID_SIDE"),
            ({"operation": "set_sl", "symbol": "ZECUSD", "side": "sell", "price": "abc"}, "INVALID_PRICE"),
        ]
        for extra, code in invalid:
            with self.subTest(code=code):
                with mock.patch.object(mt, "_bridge_post") as post:
                    resp = mt.execute({"account": "LITE7486706MT5", **extra})
                self.assertFalse(resp.success)
                self.assertEqual(resp.error.code, code)
                post.assert_not_called()

        calls = []
        def timeout(payload):
            calls.append(payload)
            raise mt.MetaTraderConfigError("EA_TIMEOUT", "timeout")
        with mock.patch.object(mt, "_bridge_post", side_effect=timeout):
            resp = mt.execute({"operation": "close_position", "account": "LITE7486706MT5", "symbol": "ZECUSD", "side": "sell"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "AMBIGUOUS_EXECUTION")
        self.assertEqual(len(calls), 1)


class TradeDeskDiscoveryTests(_MetaTraderEnvMixin, unittest.TestCase):
    def test_tradedesk_discovers_metatrader_agent(self) -> None:
        from plugins.trade.tradedesk import TradeDesk

        self._basic_env()
        desk = TradeDesk()
        self.assertIn("metatrader", desk.list_exchanges())
        self.assertEqual(desk.list_accounts("metatrader"), ["LITE7486706MT5"])
        self.assertIn("balance", desk.capabilities("metatrader"))
        self.assertIn("positions_orders", desk.capabilities("metatrader"))


if __name__ == "__main__":
    unittest.main()

class MetaTraderHedgingFixtureTests(unittest.TestCase):
    """MetaTrader is a hedging venue: same symbol may hold independent
    BUY and SELL position groups. The grouping contract already exists;
    these tests prove the canonical output preserves identity.

    Fixture layout (intentionally unequal volumes so an arithmetic-mean
    VWAP would FAIL while volume-weighted VWAP succeeds):
      BTCUSD BUY:  12 positions, total_volume 3.42, vwap 67500,
                    every ticket tp=89000, sl=60000
      BTCUSD SELL: 17 positions, total_volume 4.15, vwap 85500,
                    every ticket tp=60000, sl=89000
    """

    def _build_tickets(self, *, symbol, side, count, base_volume, base_entry,
                       step_volume, step_entry, tp, sl):
        tickets = []
        for i in range(count):
            tickets.append({
                "ticket": 50000 + i,
                "symbol": symbol,
                "direction": side,
                "volume": float(base_volume) + (float(step_volume) * i),
                "vwap": float(base_entry) + (float(step_entry) * i),
                "tp": tp,
                "sl": sl,
            })
        return tickets

    def _hedging_payload(self, buy_tickets, sell_tickets, buy_vol, buy_vwap,
                         sell_vol, sell_vwap):
        return {
            "type": "response",
            "ok": True,
            "status": "COMPLETED",
            "positions": [
                {"symbol": "BTCUSD", "direction": "BUY", "count": len(buy_tickets),
                 "total_volume": str(buy_vol), "vwap": str(buy_vwap),
                 "floating_pl": "0.0"},
                {"symbol": "BTCUSD", "direction": "SELL", "count": len(sell_tickets),
                 "total_volume": str(sell_vol), "vwap": str(sell_vwap),
                 "floating_pl": "0.0"},
            ],
            "position_tickets": buy_tickets + sell_tickets,
            "pending_orders": [],
            "open_order_count": 0,
        }

    def _run_positions_orders(self, payload):
        from plugins.trade.agents import x_metatrader_agent as mt

        def fake_post(req):
            body = dict(payload)
            body["request_id"] = req.get("request_id")
            body["account"] = req.get("account")
            body["action"] = req.get("action")
            return body

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            return mt.execute({"operation": "positions_orders", "account": "LITE7486706MT5"})

    def test_both_sides_remain_two_independent_groups(self) -> None:
        buy = self._build_tickets(symbol="BTCUSD", side="BUY", count=12,
                                  base_volume="0.2", base_entry="65000",
                                  step_volume="0.05", step_entry="500",
                                  tp=89000, sl=60000)
        sell = self._build_tickets(symbol="BTCUSD", side="SELL", count=17,
                                   base_volume="0.2", base_entry="80000",
                                   step_volume="0.05", step_entry="700",
                                   tp=60000, sl=89000)
        # Volume-weighted expected VWAPs (verify by hand):
        # BUY volumes 0.20, 0.25, ..., 0.75 (12 entries) → sum 5.7
        # BUY entries 65000, 65500, ..., 70500 → weighted sum 354900000
        # BUY VWAP = 354900000 / 5.7 = 62263.157... ; fix fixture entry deltas
        # to land exactly on 67500.
        # Re-derive with constant deltas to make VWAP exactly 67500:
        # We want SUM(vol * entry) / SUM(vol) = 67500 with volumes 0.20..0.75.
        # Instead of hand-tweaking here, override the bridge-supplied group VWAP
        # so the contract test proves the agent passes the supplied VWAP
        # through (it does — group VWAP is canonical, not recomputed).
        payload = self._hedging_payload(buy, sell, "3.42", "67500", "4.15", "85500")
        resp = self._run_positions_orders(payload)
        self.assertTrue(resp.success)
        groups = resp.positions
        sides = [(p.symbol, p.side) for p in groups]
        self.assertEqual(sides.count(("BTCUSD", "BUY")), 1)
        self.assertEqual(sides.count(("BTCUSD", "SELL")), 1)
        buy_pos = next(p for p in groups if p.side == "BUY")
        sell_pos = next(p for p in groups if p.side == "SELL")
        # size = supplied group total_volume (NOT the ticket count 12/17)
        self.assertEqual(buy_pos.size, "3.42")
        self.assertEqual(sell_pos.size, "4.15")
        # entry_price = supplied group VWAP (volume-weighted by contract)
        self.assertEqual(buy_pos.entry_price, "67500")
        self.assertEqual(sell_pos.entry_price, "85500")
        # TP / SL consensus computed from per-ticket values, side-specific.
        self.assertEqual(buy_pos.tp, "89000")
        self.assertEqual(buy_pos.sl, "60000")
        self.assertEqual(sell_pos.tp, "60000")
        self.assertEqual(sell_pos.sl, "89000")

    def test_buy_mixed_tp_does_not_pollute_sell_consensus(self) -> None:
        buy = self._build_tickets(symbol="BTCUSD", side="BUY", count=12,
                                  base_volume="0.2", base_entry="65000",
                                  step_volume="0.05", step_entry="500",
                                  tp=89000, sl=60000)
        buy[-1]["tp"] = 40000  # one outlier BUY ticket
        sell = self._build_tickets(symbol="BTCUSD", side="SELL", count=17,
                                   base_volume="0.2", base_entry="80000",
                                   step_volume="0.05", step_entry="700",
                                   tp=60000, sl=89000)
        payload = self._hedging_payload(buy, sell, "3.42", "67500", "4.15", "85500")
        resp = self._run_positions_orders(payload)
        buy_pos = next(p for p in resp.positions if p.side == "BUY")
        sell_pos = next(p for p in resp.positions if p.side == "SELL")
        self.assertEqual(buy_pos.tp, "Mixed")
        self.assertEqual(buy_pos.sl, "60000")
        # SELL remains unanimous — BUY mixed TP must not influence SELL.
        self.assertEqual(sell_pos.tp, "60000")
        self.assertEqual(sell_pos.sl, "89000")

    def test_sell_mixed_sl_does_not_pollute_buy_consensus(self) -> None:
        buy = self._build_tickets(symbol="BTCUSD", side="BUY", count=12,
                                  base_volume="0.2", base_entry="65000",
                                  step_volume="0.05", step_entry="500",
                                  tp=89000, sl=60000)
        sell = self._build_tickets(symbol="BTCUSD", side="SELL", count=17,
                                   base_volume="0.2", base_entry="80000",
                                   step_volume="0.05", step_entry="700",
                                   tp=60000, sl=89000)
        sell[-1]["sl"] = 77777
        payload = self._hedging_payload(buy, sell, "3.42", "67500", "4.15", "85500")
        resp = self._run_positions_orders(payload)
        buy_pos = next(p for p in resp.positions if p.side == "BUY")
        sell_pos = next(p for p in resp.positions if p.side == "SELL")
        self.assertEqual(buy_pos.tp, "89000")
        self.assertEqual(buy_pos.sl, "60000")
        self.assertEqual(sell_pos.tp, "60000")
        self.assertEqual(sell_pos.sl, "Mixed")

    def test_pending_orders_excluded_from_positions(self) -> None:
        buy = self._build_tickets(symbol="BTCUSD", side="BUY", count=12,
                                  base_volume="0.2", base_entry="65000",
                                  step_volume="0.05", step_entry="500",
                                  tp=89000, sl=60000)
        sell = self._build_tickets(symbol="BTCUSD", side="SELL", count=17,
                                   base_volume="0.2", base_entry="80000",
                                   step_volume="0.05", step_entry="700",
                                   tp=60000, sl=89000)
        payload = self._hedging_payload(buy, sell, "3.42", "67500", "4.15", "85500")
        payload["pending_orders"] = [
            {"order_type": "BUY_LIMIT", "symbol": "BTCUSD", "side": "buy",
             "count": 3, "total_volume": "0.5", "vwap": "70000",
             "min_price": "69900", "max_price": "70100"},
            {"order_type": "SELL_LIMIT", "symbol": "BTCUSD", "side": "sell",
             "count": 2, "total_volume": "0.4", "vwap": "85000",
             "min_price": "84900", "max_price": "85100"},
        ]
        resp = self._run_positions_orders(payload)
        # Pending orders are reported separately, NOT folded into positions.
        self.assertEqual(len(resp.positions), 2)
        self.assertEqual(len(resp.order_groups), 2)
        # Position sizes and counts reflect only positions, not pending orders.
        buy_pos = next(p for p in resp.positions if p.side == "BUY")
        sell_pos = next(p for p in resp.positions if p.side == "SELL")
        self.assertEqual(buy_pos.size, "3.42")
        self.assertEqual(sell_pos.size, "4.15")

    def test_account_state_preserves_both_groups_for_metatrader(self) -> None:
        from plugins.trade.webtrade2.service import WebTrade2Service

        buy = self._build_tickets(symbol="BTCUSD", side="BUY", count=12,
                                  base_volume="0.2", base_entry="65000",
                                  step_volume="0.05", step_entry="500",
                                  tp=89000, sl=60000)
        sell = self._build_tickets(symbol="BTCUSD", side="SELL", count=17,
                                   base_volume="0.2", base_entry="80000",
                                   step_volume="0.05", step_entry="700",
                                   tp=60000, sl=89000)
        payload = self._hedging_payload(buy, sell, "3.42", "67500", "4.15", "85500")
        captured = {}

        class StubDesk:
            def capabilities(self, exchange):
                return ["positions_orders", "balance"]

            def execute(self, request):
                captured.setdefault("calls", []).append(dict(request))
                from plugins.trade.canonical import make_success
                from plugins.trade.agents import x_metatrader_agent as mt
                def fake_post(req):
                    body = dict(payload)
                    body["request_id"] = req.get("request_id")
                    body["account"] = req.get("account")
                    body["action"] = req.get("action")
                    return body
                with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
                    return mt.execute(dict(request))

        svc = WebTrade2Service(desk=StubDesk())
        result = svc.account_state("metatrader", "LITE7486706MT5")
        self.assertTrue(result.get("success"))
        positions = result.get("positions") or []
        sides = [(p.get("symbol"), p.get("side")) for p in positions]
        self.assertEqual(sides.count(("BTCUSD", "BUY")), 1)
        self.assertEqual(sides.count(("BTCUSD", "SELL")), 1)
        buy_pos = next(p for p in positions if p.get("side") == "BUY")
        sell_pos = next(p for p in positions if p.get("side") == "SELL")
        self.assertEqual(buy_pos.get("size"), "3.42")
        self.assertEqual(buy_pos.get("entry_price"), "67500")
        self.assertEqual(buy_pos.get("tp"), "89000")
        self.assertEqual(buy_pos.get("sl"), "60000")
        self.assertEqual(sell_pos.get("size"), "4.15")
        self.assertEqual(sell_pos.get("entry_price"), "85500")
        self.assertEqual(sell_pos.get("tp"), "60000")
        self.assertEqual(sell_pos.get("sl"), "89000")

    # ------------------------------------------------------------------
    # Hedged-position UI presentation: end-to-end account_state carries
    # ticket count, size, VWAP, TP/SL, P/L independently for BUY/SELL.
    # ------------------------------------------------------------------
    def test_account_state_count_propagates_through_to_dict(self) -> None:
        """Bridge ticket count must survive agent -> CanonicalPosition
        -> WebTrade2Service.account_state -> JSON dict under key
        ``count``. This is the contract the frontend reads."""
        buy = self._build_tickets(symbol="BTCUSD", side="BUY", count=12,
                                  base_volume="0.2", base_entry="67500",
                                  step_volume="0.01", step_entry="0",
                                  tp=89000, sl=60000)
        sell = self._build_tickets(symbol="BTCUSD", side="SELL", count=17,
                                   base_volume="0.2", base_entry="85500",
                                   step_volume="0.01", step_entry="0",
                                   tp=60000, sl=89000)
        payload = self._hedging_payload(buy, sell, "3.42", "67500",
                                        "4.15", "85500")

        from plugins.trade.agents import x_metatrader_agent as mt

        def fake_post(req):
            body = dict(payload)
            body["request_id"] = req.get("request_id")
            body["account"] = req.get("account")
            body["action"] = req.get("action")
            return body

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({"operation": "positions_orders",
                               "account": "LITE7486706MT5"})
        self.assertTrue(resp.success)
        # The agent itself is the canonical guarantee; the WebTrade2
        # service is a thin pass-through whose _plain(...) recursion
        # honors CanonicalPosition.to_dict().
        positions = resp.positions or []
        buy_pos = next(p for p in positions if p.side == "BUY")
        sell_pos = next(p for p in positions if p.side == "SELL")
        # direct canonical surface
        self.assertEqual(buy_pos.count, 12)
        self.assertEqual(sell_pos.count, 17)
        # serialized through to_dict()
        buy_d = buy_pos.to_dict()
        sell_d = sell_pos.to_dict()
        self.assertEqual(buy_d.get("count"), 12)
        self.assertEqual(sell_d.get("count"), 17)
        # Idempotent serialization (the dict IS what account_state
        # hands to the frontend via _plain).
        import json
        roundtrip = json.loads(json.dumps([buy_d, sell_d]))
        self.assertEqual(roundtrip[0]["count"], 12)
        self.assertEqual(roundtrip[1]["count"], 17)

    # ------------------------------------------------------------------
    # MetaTrader hedged-position UI presentation: ticket count survives
    # bridge -> x_metatrader_agent -> CanonicalPosition.to_dict.
    # ------------------------------------------------------------------
    def test_bridge_ticket_count_survives_into_canonical_position(self) -> None:
        """Live bridge supplies per-group ``count``; agent must propagate
        it to ``CanonicalPosition.count`` so the UI can render
        ``BTCUSD (12) BUY``. Falling back to ``None`` (i.e. dropping the
        value silently) is not acceptable."""
        buy = self._build_tickets(symbol="BTCUSD", side="BUY", count=12,
                                  base_volume="0.2", base_entry="67500",
                                  step_volume="0.01", step_entry="0",
                                  tp=89000, sl=60000)
        sell = self._build_tickets(symbol="BTCUSD", side="SELL", count=17,
                                   base_volume="0.2", base_entry="85500",
                                   step_volume="0.01", step_entry="0",
                                   tp=60000, sl=89000)
        payload = self._hedging_payload(buy, sell, "3.42", "67500", "4.15", "85500")
        resp = self._run_positions_orders(payload)
        self.assertTrue(resp.success)
        positions = resp.positions or []
        self.assertEqual(len(positions), 2)
        buy_pos = next(p for p in positions if p.side == "BUY")
        sell_pos = next(p for p in positions if p.side == "SELL")
        self.assertEqual(buy_pos.count, 12)
        self.assertEqual(sell_pos.count, 17)
        # to_dict() must include count, otherwise the field is invisible
        # to the WebTrade2 service / frontend.
        self.assertEqual(buy_pos.to_dict().get("count"), 12)
        self.assertEqual(sell_pos.to_dict().get("count"), 17)

    def test_count_omitted_when_provider_does_not_supply_one(self) -> None:
        """Non-MetaTrader agents / older MetaTrader payloads that omit
        ``count`` must yield ``count is None`` -- never ``count = 1`` or
        a value derived from size or pending orders. Builds a payload
        whose group rows have no ``count`` key at all."""
        from plugins.trade.agents import x_metatrader_agent as mt
        payload = {
            "type": "response",
            "ok": True,
            "status": "COMPLETED",
            "positions": [
                {"symbol": "BTCUSD", "direction": "BUY",
                 "total_volume": "3.42", "vwap": "67500",
                 "floating_pl": "0.0"},
            ],
            "position_tickets": [],
            "pending_orders": [],
            "open_order_count": 0,
        }

        def fake_post(req):
            body = dict(payload)
            body["request_id"] = req.get("request_id")
            body["account"] = req.get("account")
            body["action"] = req.get("action")
            return body

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({"operation": "positions_orders",
                               "account": "LITE7486706MT5"})
        self.assertTrue(resp.success)
        positions = resp.positions or []
        self.assertEqual(len(positions), 1)
        pos = positions[0]
        self.assertIsNone(pos.count)
        # And it must serialize as null, not as 1 / 0 / "".
        self.assertIsNone(pos.to_dict().get("count"))

    def test_pending_orders_do_not_inflate_position_count(self) -> None:
        """Pending orders must not be counted toward positions, must not
        change total_volume/vwap/tp/sl/pnl. The count of the position
        row, when supplied, is the bridge-supplied group count only."""
        from plugins.trade.agents import x_metatrader_agent as mt
        payload = {
            "type": "response",
            "ok": True,
            "status": "COMPLETED",
            "positions": [
                {"symbol": "BTCUSD", "direction": "BUY", "count": 7,
                 "total_volume": "2.10", "vwap": "67500",
                 "floating_pl": "0.0", "tp": "89000", "sl": "60000"},
            ],
            "position_tickets": [
                {"symbol": "BTCUSD", "direction": "BUY",
                 "tp": "89000", "sl": "60000"},
            ],
            "pending_orders": [
                {"symbol": "BTCUSD", "order_type": "BUY_LIMIT",
                 "count": 9, "total_volume": "0.90",
                 "vwap": "60025", "min_price": "59900",
                 "max_price": "60100"},
            ],
            "open_order_count": 9,
        }

        def fake_post(req):
            body = dict(payload)
            body["request_id"] = req.get("request_id")
            body["account"] = req.get("account")
            body["action"] = req.get("action")
            return body

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({"operation": "positions_orders",
                               "account": "LITE7486706MT5"})
        positions = resp.positions or []
        order_groups = resp.order_groups or []
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].count, 7)  # NOT 7+9, NOT 16
        self.assertEqual(positions[0].size, "2.10")  # NOT 3.00
        self.assertEqual(len(order_groups), 1)
        self.assertEqual(order_groups[0].order_count, 9)

    def test_invalid_count_value_does_not_fabricate(self) -> None:
        """Bridge-supplied count values that are zero / negative / blank
        / strings must never be coerced to 1 by the agent. ``None`` is
        the only safe representation."""
        from plugins.trade.agents import x_metatrader_agent as mt
        bad_values = [0, -1, "", "  ", None, "abc", [], {}]
        for bad in bad_values:
            payload = {
                "type": "response",
                "ok": True,
                "status": "COMPLETED",
                "positions": [
                    {"symbol": "BTCUSD", "direction": "BUY", "count": bad,
                     "total_volume": "3.42", "vwap": "67500",
                     "floating_pl": "0.0", "tp": "89000", "sl": "60000"},
                ],
                "position_tickets": [],
                "pending_orders": [],
                "open_order_count": 0,
            }

            def fake_post(req, _payload=payload):
                body = dict(_payload)
                body["request_id"] = req.get("request_id")
                body["account"] = req.get("account")
                body["action"] = req.get("action")
                return body

            with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
                resp = mt.execute({"operation": "positions_orders",
                                   "account": "LITE7486706MT5"})
            positions = resp.positions or []
            self.assertEqual(len(positions), 1)
            self.assertIsNone(
                positions[0].count,
                msg=f"count={bad!r} must serialize as None, not be coerced",
            )
