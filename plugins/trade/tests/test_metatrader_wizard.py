"""Telegram /trade wizard integration for the unified MetaTrader agent.

Offline only: TradeDesk/agent calls are mocked. No live MT5 mutations.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.canonical import (  # noqa: E402
    CanonicalOrderGroup,
    CanonicalPosition,
    CanonicalResponse,
    make_failure,
    make_success,
)
from plugins.trade.tradedesk import TradeDesk  # noqa: E402
from plugins.trade.wizard import TradeWizard  # noqa: E402


def _zecusd_sell_group() -> CanonicalPosition:
    return CanonicalPosition(
        symbol="ZECUSD",
        side="SELL",
        size="9.07",
        entry_price="1650.12",
        pnl="12.34",
        tp="0",
        sl="0",
    )


def _buy_limit_group() -> CanonicalOrderGroup:
    return CanonicalOrderGroup(
        symbol="ZECUSD",
        side="buy",
        order_count=2,
        total_size="0.02",
        vwap="1573.01",
        min_price="1568.86",
        max_price="1577.16",
        classification="entry_limit",
        display_type="BUY_LIMIT",
    )


class MetaTraderDesk:
    """Records TradeDesk requests and returns canned MetaTrader-shaped responses."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self._exchanges = ["metatrader"]
        self._accounts = ["LITE7486706MT5"]
        self.bridge_payloads: List[Dict[str, Any]] = []

    def list_exchanges(self) -> List[str]:
        return list(self._exchanges)

    def list_accounts(self, exchange: str) -> List[str]:
        return list(self._accounts) if exchange == "metatrader" else []

    def capabilities(self, exchange: str) -> List[str]:
        if exchange != "metatrader":
            return []
        from plugins.trade.agents.x_metatrader_agent import capabilities

        return capabilities()

    def execute(self, request: Dict[str, Any]) -> CanonicalResponse:
        self.requests.append(dict(request))
        op = str(request.get("operation") or "")
        ex = str(request.get("exchange") or "")
        acct = str(request.get("account") or "")
        if op == "balance":
            from plugins.trade.canonical import CanonicalBalance, CanonicalPortfolioSummary

            return make_success(
                op,
                ex,
                acct,
                balance=CanonicalBalance(value="12693.10", unit="USD"),
                portfolio_summary=CanonicalPortfolioSummary(
                    account_value="10525.04",
                    withdrawable="8000.00",
                    margin_used="2525.04",
                    total_position_value="10525.04",
                    unit="USD",
                ),
            )
        if op in {"positions_orders", "positions_management"}:
            return make_success(
                op,
                ex,
                acct,
                positions=[_zecusd_sell_group()],
                open_order_count=1,
                order_groups=[
                    CanonicalOrderGroup(
                        symbol="ZECUSD",
                        side="sell",
                        order_count=1,
                        total_size="0.76",
                        vwap="1700.0",
                        min_price="1700.0",
                        max_price="1700.0",
                        classification="entry_limit",
                        display_type="SELL_LIMIT",
                    )
                ],
                data={
                    "positions": [
                        {
                            "symbol": "ZECUSD",
                            "direction": "SELL",
                            "count": 19,
                            "total_volume": "9.07",
                            "vwap": "1650.12",
                            "floating_pl": "12.34",
                        }
                    ]
                },
            )
        if op == "new_order":
            if str(request.get("order_type") or "").lower() != "limit":
                return make_failure(op, ex, acct, "UNSUPPORTED_ORDER_TYPE", "limit only")
            return make_success(op, ex, acct, data={"ok": True})
        if op == "ladder":
            return make_success(
                op,
                ex,
                acct,
                data={"requested": 2, "succeeded": 2, "failed": 0, "failures": []},
            )
        if op in {"cancel_orders", "cancel_order_group"}:
            return make_success(
                op,
                ex,
                acct,
                data={"matched": 2, "succeeded": 2, "failed": 0, "failures": []},
            )
        if op in {"set_tp", "set_sl", "close_position"}:
            return make_success(
                op,
                ex,
                acct,
                data={"matched": 19, "succeeded": 19, "failed": 0, "failures": []},
            )
        if op == "list_instruments":
            return make_success(
                op,
                ex,
                acct,
                data={
                    "instruments": [
                        {
                            "symbol": "ZECUSD",
                            "native_symbol": "ZECUSD",
                            "display_name": "ZECUSD",
                            "price_increment": "0.01",
                            "size_increment": "0.01",
                            "minimum_size": "0.01",
                        }
                    ],
                    "count": 1,
                },
            )
        if op == "resolve_instrument":
            from plugins.trade.canonical import CanonicalInstrument

            symbol = str(request.get("symbol") or "").strip().upper()
            return make_success(
                op,
                ex,
                acct,
                instrument=CanonicalInstrument(
                    requested_symbol=symbol,
                    symbol=symbol,
                    display_name=symbol,
                    price_increment="0.01",
                    size_increment="0.01",
                    minimum_size="0.01",
                ),
            )
        if op == "market_price":
            from plugins.trade.canonical import CanonicalMarketPrice

            return make_success(
                op,
                ex,
                acct,
                market_price=CanonicalMarketPrice(
                    requested_symbol=str(request.get("symbol") or ""),
                    market=str(request.get("symbol") or ""),
                    price="1656.43",
                    mark_price="1656.43",
                    last_external_price="1656.43",
                ),
            )
        return make_failure(op, ex, acct, "NOT_IMPLEMENTED", op)


def _open_metatrader(wizard: TradeWizard, key: tuple = ("chat",)) -> TradeWizard:
    wizard.open(key)
    wizard.handle_callback(key, "exchange:metatrader")
    wizard.handle_callback(key, "account:LITE7486706MT5")
    return wizard


class MetaTraderDiscoveryTests(unittest.TestCase):
    def test_tradedesk_discovers_metatrader_agent(self) -> None:
        desk = TradeDesk()
        self.assertIn("metatrader", desk.list_exchanges())

    def test_wizard_lists_metatrader_exchange(self) -> None:
        desk = MetaTraderDesk()
        wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
        screen = wizard.open(("chat",))
        labels = [btn["text"] for row in screen.buttons for btn in row]
        self.assertIn("metatrader", labels)

    def test_wizard_lists_configured_metatrader_account(self) -> None:
        desk = MetaTraderDesk()
        wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
        wizard.open(("chat",))
        screen = wizard.handle_callback(("chat",), "exchange:metatrader")
        labels = [btn["text"] for row in screen.buttons for btn in row]
        self.assertIn("LITE7486706MT5", labels)


class MetaTraderWizardReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = MetaTraderDesk()
        self.wizard = TradeWizard(tradedesk=self.desk)  # type: ignore[arg-type]
        self.key = ("chat",)
        _open_metatrader(self.wizard, self.key)

    def test_balance_routes_to_metatrader_balance(self) -> None:
        screen = self.wizard.handle_callback(self.key, "action:balance")
        req = self.desk.requests[-1]
        self.assertEqual(req["operation"], "balance")
        self.assertEqual(req["exchange"], "metatrader")
        self.assertEqual(req["account"], "LITE7486706MT5")
        self.assertIn("12693.10", screen.text)
        self.assertIn("USD", screen.text)

    def test_positions_orders_shows_one_logical_symbol_side_position(self) -> None:
        screen = self.wizard.handle_callback(self.key, "action:positions_orders")
        req = self.desk.requests[-1]
        self.assertEqual(req["operation"], "positions_orders")
        self.assertEqual(screen.text.count("ZECUSD"), 2)  # position + pending group
        self.assertIn("Size: 9.07", screen.text)
        self.assertTrue("Entry: 1650.12" in screen.text or "Entry: 1,650.12" in screen.text)
        self.assertNotIn("ticket", screen.text.lower())


class MetaTraderProtectionDisplayTests(unittest.TestCase):
    def _screen_for(self, position: CanonicalPosition) -> str:
        class _PosDesk:
            def execute(self, request: Dict[str, Any]) -> CanonicalResponse:
                return make_success(
                    operation=str(request.get("operation") or "positions_management"),
                    exchange="metatrader",
                    account="LITE7486706MT5",
                    positions=[position],
                )

        wizard = TradeWizard(tradedesk=_PosDesk())  # type: ignore[arg-type]
        key = ("chat", "protection")
        state = wizard._state_for(key)
        state.exchange = "metatrader"
        state.account = "LITE7486706MT5"
        return wizard._render_positions_management(key, False).text

    def test_display_uniform_tp_and_blank_sl(self) -> None:
        text = self._screen_for(
            CanonicalPosition(symbol="ZECUSD", side="SELL", size="9.07", entry_price="1660.17640573", pnl="12.34", tp="500", sl=None)
        )
        self.assertIn("TP: 500", text)
        self.assertIn("SL: —", text)
        self.assertNotIn("ticket", text.lower())

    def test_display_both_unset(self) -> None:
        text = self._screen_for(
            CanonicalPosition(symbol="ZECUSD", side="SELL", size="9.07", entry_price="1660.17640573", pnl="12.34", tp=None, sl=None)
        )
        self.assertIn("TP: —", text)
        self.assertIn("SL: —", text)

    def test_display_same_tp_and_sl(self) -> None:
        text = self._screen_for(
            CanonicalPosition(symbol="ZECUSD", side="SELL", size="9.07", entry_price="1660.17640573", pnl="12.34", tp="1573.60", sl="1740.67")
        )
        self.assertIn("TP: 1,573.60", text)
        self.assertIn("SL: 1,740.67", text)

    def test_display_mixed_tp(self) -> None:
        text = self._screen_for(
            CanonicalPosition(symbol="ZECUSD", side="SELL", size="9.07", entry_price="1660.17640573", pnl="12.34", tp="Mixed", sl=None)
        )
        self.assertIn("TP: Mixed", text)
        self.assertIn("SL: —", text)

    def test_display_mixed_sl(self) -> None:
        text = self._screen_for(
            CanonicalPosition(symbol="ZECUSD", side="SELL", size="9.07", entry_price="1660.17640573", pnl="12.34", tp="500", sl="Mixed")
        )
        self.assertIn("TP: 500", text)
        self.assertIn("SL: Mixed", text)


class MetaTraderWizardWriteRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = MetaTraderDesk()
        self.wizard = TradeWizard(tradedesk=self.desk)  # type: ignore[arg-type]
        self.key = ("chat",)
        _open_metatrader(self.wizard, self.key)

    def _confirm_new_limit(self, side_cb: str, volume: str, price: str) -> Dict[str, Any]:
        self.wizard.handle_callback(self.key, "action:new_order")
        self.wizard.handle_callback(self.key, "other")
        self.wizard.handle_text(self.key, "ZECUSD")
        # If instrument confirm is shown, agree.
        state = self.wizard._state_for(self.key)
        if state.state == "instrument_confirm":
            self.wizard.handle_callback(self.key, "resolve:agree")
        self.wizard.handle_callback(self.key, side_cb)
        self.wizard.handle_text(self.key, volume)
        self.wizard.handle_text(self.key, price)
        self.wizard.handle_callback(self.key, "confirm")
        return self.desk.requests[-1]

    def test_new_buy_limit_routing(self) -> None:
        req = self._confirm_new_limit("side:buy", "0.01", "1488.65")
        self.assertEqual(req["operation"], "new_order")
        self.assertEqual(req["exchange"], "metatrader")
        self.assertEqual(req["account"], "LITE7486706MT5")
        self.assertEqual(str(req["symbol"]).upper(), "ZECUSD")
        self.assertEqual(str(req["side"]).lower(), "buy")
        self.assertEqual(str(req["order_type"]).lower(), "limit")
        self.assertEqual(str(req["volume"]), "0.01")
        self.assertEqual(str(req["price"]), "1488.65")
        self.assertNotEqual(str(req.get("order_type") or "").lower(), "market")

    def test_new_sell_limit_routing(self) -> None:
        req = self._confirm_new_limit("side:sell", "0.01", "1740.67")
        self.assertEqual(str(req["side"]).lower(), "sell")
        self.assertEqual(str(req["order_type"]).lower(), "limit")

    def test_unsupported_market_order_is_not_sent(self) -> None:
        self.wizard.handle_callback(self.key, "action:new_order")
        self.wizard.handle_callback(self.key, "other")
        self.wizard.handle_text(self.key, "ZECUSD")
        state = self.wizard._state_for(self.key)
        if state.state == "instrument_confirm":
            self.wizard.handle_callback(self.key, "resolve:agree")
        self.wizard.handle_callback(self.key, "side:buy")
        self.wizard.handle_text(self.key, "0.01")
        self.wizard.handle_text(self.key, "1488.65")
        self.wizard.handle_callback(self.key, "confirm")
        new_orders = [r for r in self.desk.requests if r.get("operation") == "new_order"]
        self.assertTrue(new_orders)
        self.assertTrue(all(str(r.get("order_type") or "").lower() == "limit" for r in new_orders))
        self.assertFalse(any(str(r.get("order_type") or "").lower() == "market" for r in new_orders))

    def _confirm_ladder(self, distribution_cb: str):
        self.wizard.handle_callback(self.key, "action:ladder")
        self.wizard.handle_callback(self.key, distribution_cb)
        self.wizard.handle_callback(self.key, "other")
        self.wizard.handle_text(self.key, "ZECUSD")
        state = self.wizard._state_for(self.key)
        if state.state == "instrument_confirm":
            self.wizard.handle_callback(self.key, "resolve:agree")
        self.wizard.handle_callback(self.key, "side:buy")
        self.wizard.handle_text(self.key, "2")
        self.wizard.handle_text(self.key, "0.02")
        self.wizard.handle_text(self.key, "1577.16")
        self.wizard.handle_text(self.key, "1568.86")
        screen = self.wizard.handle_callback(self.key, "confirm")
        return self.desk.requests[-1], screen

    def test_uniform_ladder_routing_and_result_counts(self) -> None:
        req, screen = self._confirm_ladder("distribution:uniform")
        self.assertEqual(req["operation"], "ladder")
        self.assertEqual(str(req["distribution"]), "uniform")
        self.assertEqual(str(req["order_count"]), "2")
        self.assertIn("requested", screen.text.lower())
        self.assertIn("succeeded", screen.text.lower())
        self.assertIn("failed", screen.text.lower())
        self.assertNotIn("5258008096", screen.text)

    def test_half_gaussian_ladder_routing(self) -> None:
        req, _screen = self._confirm_ladder("distribution:half_gaussian")
        self.assertEqual(req["operation"], "ladder")
        self.assertEqual(str(req["distribution"]), "half_gaussian")

    def test_cancel_orders_grouped_routing_includes_symbol_side_type(self) -> None:
        self.desk.execute = self._cancel_list_then_cancel  # type: ignore[method-assign]
        self.wizard.handle_callback(self.key, "action:cancel_orders")
        # Recreate listing with BUY_LIMIT group for the cancel screen.
        self.desk.requests.clear()

        def listing(request: Dict[str, Any]) -> CanonicalResponse:
            self.desk.requests.append(dict(request))
            op = str(request.get("operation") or "")
            if op == "positions_orders":
                return make_success(
                    op,
                    "metatrader",
                    "LITE7486706MT5",
                    positions=[],
                    open_order_count=2,
                    order_groups=[_buy_limit_group()],
                )
            if op in {"cancel_orders", "cancel_order_group"}:
                return make_success(op, "metatrader", "LITE7486706MT5", data={"matched": 2, "succeeded": 2, "failed": 0})
            return make_failure(op, "metatrader", "LITE7486706MT5", "NOT_IMPLEMENTED", op)

        self.desk.execute = listing  # type: ignore[method-assign]
        screen = self.wizard.handle_callback(self.key, "action:cancel_orders")
        cancel_buttons = [
            btn["callback_data"]
            for row in screen.buttons
            for btn in row
            if str(btn.get("callback_data") or "").startswith("cancel_group:")
        ]
        self.assertTrue(cancel_buttons)
        self.wizard.handle_callback(self.key, cancel_buttons[0])
        self.wizard.handle_callback(self.key, "confirm")
        cancel_req = [r for r in self.desk.requests if r.get("operation") in {"cancel_orders", "cancel_order_group"}][-1]
        self.assertEqual(str(cancel_req["symbol"]).upper(), "ZECUSD")
        self.assertEqual(str(cancel_req["side"]).lower(), "buy")
        order_type = str(cancel_req.get("order_type") or cancel_req.get("display_type") or "").upper()
        self.assertIn(order_type, {"BUY_LIMIT", "LIMIT"})

    def _cancel_list_then_cancel(self, request: Dict[str, Any]) -> CanonicalResponse:
        return MetaTraderDesk.execute(self.desk, request)

    def test_set_tp_grouped_routing_includes_side(self) -> None:
        self.wizard.handle_callback(self.key, "action:positions_management")
        self.wizard.handle_callback(self.key, "position:ZECUSD:sell")
        self.wizard.handle_callback(self.key, "set_tp")
        self.wizard.handle_text(self.key, "1573.6")
        self.wizard.handle_callback(self.key, "confirm")
        req = [r for r in self.desk.requests if r.get("operation") == "set_tp"][-1]
        self.assertEqual(str(req["symbol"]).upper(), "ZECUSD")
        self.assertEqual(str(req["side"]).lower(), "sell")
        self.assertEqual(str(req.get("price") or req.get("tp")), "1573.6")

    def test_set_sl_grouped_routing_includes_side(self) -> None:
        self.wizard.handle_callback(self.key, "action:positions_management")
        self.wizard.handle_callback(self.key, "position:ZECUSD:sell")
        self.wizard.handle_callback(self.key, "set_sl")
        self.wizard.handle_text(self.key, "1740.67")
        self.wizard.handle_callback(self.key, "confirm")
        req = [r for r in self.desk.requests if r.get("operation") == "set_sl"][-1]
        self.assertEqual(str(req["side"]).lower(), "sell")
        self.assertEqual(str(req.get("price") or req.get("sl")), "1740.67")

    def test_tp_zero_survives_wizard_routing(self) -> None:
        self.wizard.handle_callback(self.key, "action:positions_management")
        self.wizard.handle_callback(self.key, "position:ZECUSD:sell")
        self.wizard.handle_callback(self.key, "set_tp")
        self.wizard.handle_text(self.key, "0")
        state = self.wizard._state_for(self.key)
        if state.state == "position_tp_confirm":
            self.wizard.handle_callback(self.key, "confirm")
        tp_reqs = [r for r in self.desk.requests if r.get("operation") == "set_tp"]
        self.assertTrue(tp_reqs)
        price = tp_reqs[-1].get("price", tp_reqs[-1].get("tp"))
        self.assertEqual(str(price), "0")

    def test_sl_zero_survives_wizard_routing(self) -> None:
        self.wizard.handle_callback(self.key, "action:positions_management")
        self.wizard.handle_callback(self.key, "position:ZECUSD:sell")
        self.wizard.handle_callback(self.key, "set_sl")
        self.wizard.handle_text(self.key, "0")
        state = self.wizard._state_for(self.key)
        if state.state == "position_sl_confirm":
            self.wizard.handle_callback(self.key, "confirm")
        sl_reqs = [r for r in self.desk.requests if r.get("operation") == "set_sl"]
        self.assertTrue(sl_reqs)
        price = sl_reqs[-1].get("price", sl_reqs[-1].get("sl"))
        self.assertEqual(str(price), "0")

    def test_close_position_mock_routing_includes_symbol_and_side(self) -> None:
        self.wizard.handle_callback(self.key, "action:positions_management")
        self.wizard.handle_callback(self.key, "position:ZECUSD:sell")
        self.wizard.handle_callback(self.key, "close_position")
        self.wizard.handle_callback(self.key, "confirm")
        req = [r for r in self.desk.requests if r.get("operation") == "close_position"][-1]
        self.assertEqual(str(req["symbol"]).upper(), "ZECUSD")
        self.assertEqual(str(req["side"]).lower(), "sell")
        self.assertNotIn("ticket", req)


class MetaTraderAgentWizardContractTests(unittest.TestCase):
    """Real agent mappings used by the wizard, with the Windows bridge mocked."""

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in list(os.environ) if k.startswith("MT_") or k.startswith("LINUX_WIN_") or k == "HERMES_HOME"}
        for key in list(os.environ):
            if key.startswith("MT_") or key.startswith("LINUX_WIN_"):
                os.environ.pop(key, None)
        self.home = tempfile.mkdtemp(prefix="mt_wizard_")
        os.environ["HERMES_HOME"] = self.home
        Path(self.home, ".env").write_text("", encoding="utf-8")
        os.environ["LINUX_WIN_HOST"] = "192.0.2.10"
        os.environ["LINUX_WIN_PORT"] = "5050"
        os.environ["MT_LITE7486706MT5_ACCOUNT"] = "7486706"

    def tearDown(self) -> None:
        for key in list(os.environ):
            if key.startswith("MT_") or key.startswith("LINUX_WIN_"):
                os.environ.pop(key, None)
        os.environ.pop("HERMES_HOME", None)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_aggregated_positions_are_one_logical_row(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        def fake_post(payload):
            return {
                "ok": True,
                "request_id": payload["request_id"],
                "account": payload["account"],
                "positions": [
                    {
                        "symbol": "ZECUSD",
                        "direction": "SELL",
                        "count": 19,
                        "total_volume": "9.07",
                        "vwap": "1650.12",
                        "floating_pl": "12.34",
                        "tp": "0",
                        "sl": "0",
                    }
                ],
                "pending_orders": [],
                "open_order_count": 0,
            }

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({"operation": "positions_orders", "account": "LITE7486706MT5"})
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.positions), 1)
        self.assertEqual(resp.positions[0].symbol, "ZECUSD")
        self.assertEqual(resp.positions[0].side.upper(), "SELL")
        self.assertEqual(resp.positions[0].size, "9.07")

    def test_positions_management_aliases_positions_orders(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        captured = []

        def fake_post(payload):
            captured.append(dict(payload))
            return {
                "ok": True,
                "request_id": payload["request_id"],
                "account": payload["account"],
                "positions": [
                    {"symbol": "ZECUSD", "direction": "SELL", "count": 19, "total_volume": "9.07", "vwap": "1650.12", "floating_pl": "0"}
                ],
                "pending_orders": [],
                "open_order_count": 0,
            }

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute({"operation": "positions_management", "account": "LITE7486706MT5"})
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.positions), 1)
        self.assertEqual(captured[0]["action"], "positions_orders")

    def test_cancel_order_group_aliases_cancel_orders_filters(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        captured = []

        def fake_post(payload):
            captured.append(dict(payload))
            return {
                "ok": True,
                "request_id": payload["request_id"],
                "account": payload["account"],
                "matched": 2,
                "succeeded": 2,
                "failed": 0,
                "failures": [],
            }

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute(
                {
                    "operation": "cancel_order_group",
                    "account": "LITE7486706MT5",
                    "symbol": "ZECUSD",
                    "side": "buy",
                    "order_type": "BUY_LIMIT",
                }
            )
        self.assertTrue(resp.success)
        self.assertEqual(captured[0]["action"], "cancel_orders")
        self.assertEqual(captured[0]["symbol"], "ZECUSD")
        self.assertEqual(captured[0]["side"], "BUY")
        self.assertEqual(captured[0]["order_type"], "BUY_LIMIT")
        self.assertEqual(captured[0]["account"], 7486706)

    def test_explicit_zero_tp_sl_reach_bridge(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        captured = []

        def fake_post(payload):
            captured.append(dict(payload))
            return {
                "ok": True,
                "request_id": payload["request_id"],
                "account": payload["account"],
                "matched": 19,
                "succeeded": 19,
                "failed": 0,
            }

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            tp = mt.execute({"operation": "set_tp", "account": "LITE7486706MT5", "symbol": "ZECUSD", "side": "sell", "price": "0"})
            sl = mt.execute({"operation": "set_sl", "account": "LITE7486706MT5", "symbol": "ZECUSD", "side": "sell", "price": 0})
        self.assertTrue(tp.success)
        self.assertTrue(sl.success)
        self.assertIn("tp", captured[0])
        self.assertEqual(captured[0]["tp"], 0)
        self.assertIn("sl", captured[1])
        self.assertEqual(captured[1]["sl"], 0)

    def test_close_position_payload_is_symbol_side_only(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        captured = []

        def fake_post(payload):
            captured.append(dict(payload))
            return {
                "ok": True,
                "request_id": payload["request_id"],
                "account": payload["account"],
                "matched": 19,
                "succeeded": 19,
                "failed": 0,
            }

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            resp = mt.execute(
                {"operation": "close_position", "account": "LITE7486706MT5", "symbol": "ZECUSD", "side": "sell"}
            )
        self.assertTrue(resp.success)
        self.assertEqual(captured[0]["action"], "close_position")
        self.assertEqual(captured[0]["symbol"], "ZECUSD")
        self.assertEqual(captured[0]["side"], "SELL")
        self.assertNotIn("volume", captured[0])

    def test_market_order_rejected_before_bridge(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt

        with mock.patch.object(mt, "_bridge_post") as post:
            resp = mt.execute(
                {
                    "operation": "new_order",
                    "account": "LITE7486706MT5",
                    "symbol": "ZECUSD",
                    "side": "buy",
                    "order_type": "market",
                    "volume": "0.01",
                    "price": "1",
                }
            )
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "UNSUPPORTED_ORDER_TYPE")
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
