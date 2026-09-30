"""Unit tests for the Telegram /tradespot SPOT wizard.

Offline only. No live exchange or Telegram calls.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.canonical import (  # noqa: E402
    CanonicalBalance,
    CanonicalMarketPrice,
    CanonicalOrderGroup,
    CanonicalResponse,
    make_failure,
    make_success,
)
from plugins.trade.spotdesk import (  # noqa: E402
    SpotDesk,
    _exchange_name_from_filename,
    _iter_agent_files,
)
from plugins.trade.tradedesk import _exchange_name_from_filename as trade_exchange_name_from_filename  # noqa: E402
from plugins.trade.tradespot_wizard import TradeSpotWizard, handle_tradespot_command  # noqa: E402


def _labels(screen: Any) -> List[str]:
    return [btn["text"] for row in screen.buttons for btn in row]


def _callbacks(screen: Any) -> List[str]:
    return [btn["callback_data"] for row in screen.buttons for btn in row]


class SpotDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="spot_agents_")
        self.agents = Path(self.tmp.name)

    def tearDown(self) -> None:
        for name in list(sys.modules):
            if name.startswith("plugins.trade.agents.x_") and name.endswith("_agent_spot"):
                sys.modules.pop(name, None)
        self.tmp.cleanup()

    def _write_agent(self, filename: str, name: str, caps: List[str] | None = None) -> None:
        caps = caps or ["balance"]
        Path(self.agents, filename).write_text(
            "from plugins.trade.canonical import CanonicalBalance, make_success\n"
            f"name = {name!r}\n"
            "def list_accounts(): return ['amiroo', {'account': 'bitget', 'label': 'bitget'}]\n"
            f"def capabilities(): return {caps!r}\n"
            "def execute(request):\n"
            "    return make_success(request.get('operation',''), name, request.get('account',''), balance=CanonicalBalance('1.00', 'USDC'))\n",
            encoding="utf-8",
        )

    def test_spot_filename_parser_accepts_exact_spot_convention(self) -> None:
        self.assertEqual(_exchange_name_from_filename("x_raydium_agent_spot.py"), "raydium")
        self.assertEqual(_exchange_name_from_filename("x_aftermath_agent_spot.py"), "aftermath")
        self.assertEqual(_exchange_name_from_filename("x_mexc_agent_spot.py"), "mexc")

    def test_spot_filename_parser_rejects_normal_agents_and_malformed_files(self) -> None:
        self.assertIsNone(_exchange_name_from_filename("x_hyperliquid_agent.py"))
        self.assertIsNone(_exchange_name_from_filename("x__agent_spot.py"))
        self.assertIsNone(_exchange_name_from_filename("x_42bad_agent_spot.py"))
        self.assertIsNone(_exchange_name_from_filename("helper.py"))

    def test_spotdesk_discovers_only_valid_spot_agents(self) -> None:
        self._write_agent("x_raydium_agent_spot.py", "raydium")
        self._write_agent("x_aftermath_agent_spot.py", "aftermath")
        self._write_agent("x_mexc_agent.py", "mexc")  # normal /trade file, must be ignored
        Path(self.agents, "x_bad_agent_spot.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        Path(self.agents, "x_42bad_agent_spot.py").write_text("name='bad'\n", encoding="utf-8")
        desk = SpotDesk(agents_dir=self.agents)
        self.assertEqual(desk.list_exchanges(), ["aftermath", "raydium"])

    def test_iter_agent_files_ignores_normal_agents_tests_helpers_and_malformed(self) -> None:
        for filename in [
            "x_raydium_agent_spot.py",
            "x_hyperliquid_agent.py",
            "test_spot.py",
            "_helper.py",
            "x_42bad_agent_spot.py",
        ]:
            Path(self.agents, filename).write_text("# test\n", encoding="utf-8")
        self.assertEqual([p.name for p in _iter_agent_files(self.agents)], ["x_raydium_agent_spot.py"])

    def test_spot_agents_are_not_accidentally_added_to_trade_discovery(self) -> None:
        self.assertIsNone(trade_exchange_name_from_filename("x_raydium_agent_spot.py"))
        self.assertEqual(trade_exchange_name_from_filename("x_hyperliquid_agent.py"), "hyperliquid")


class FakeSpotDesk:
    def __init__(self, caps: List[str] | None = None) -> None:
        self.requests: List[Dict[str, Any]] = []
        self._caps = caps if caps is not None else ["balance", "orders"]

    def list_exchanges(self) -> List[str]:
        return ["aftermath", "raydium"]

    def list_accounts(self, exchange: str) -> List[Any]:
        if exchange == "raydium":
            return ["amiroo", {"account": "bitget", "label": "bitget"}]
        if exchange == "aftermath":
            return ["wallet"]
        return []

    def capabilities(self, exchange: str) -> List[str]:
        return list(self._caps) if exchange in {"aftermath", "raydium"} else []

    def execute(self, request: Dict[str, Any]) -> CanonicalResponse:
        self.requests.append(dict(request))
        op = str(request.get("operation") or "")
        ex = str(request.get("exchange") or "")
        acct = str(request.get("account") or "")
        if op == "balance":
            return make_success(op, ex, acct, balance=CanonicalBalance("12.34", "USDC"), data={"assets": [{"asset": "SOL", "amount": "2"}]})
        if op == "orders":
            return make_success(op, ex, acct, open_order_count=1, order_groups=[CanonicalOrderGroup(symbol="SOL/USDC", side="buy", order_count=1, total_size="2", vwap="10", min_price="10", max_price="10")])
        if op == "positions_orders":
            return make_success(op, ex, acct, open_order_count=0, order_groups=[])
        return make_success(op, ex, acct, data={"ok": True})


class FakeMexcSpotDesk:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.price_failures: set[str] = set()
        self.instruments: List[Dict[str, Any]] = [
            self._inst("SOLUSDT", "SOL", "USDT", quote_precision=2),
            self._inst("SOLUSDC", "SOL", "USDC", quote_precision=2),
            self._inst("SOLBTC", "SOL", "BTC", quote_precision=8),
            self._inst("ETHUSDT", "ETH", "USDT", quote_precision=2),
            self._inst("HYPEUSDT", "HYPE", "USDT", quote_precision=4),
            self._inst("SUIUSDT", "SUI", "USDT", quote_precision=4),
            self._inst("BTCUSDT", "BTC", "USDT", quote_precision=2),
            self._inst("BTCUSDC", "BTC", "USDC", quote_precision=2),
            self._inst("SOLDELIST", "SOL", "DELIST", status="0"),
            self._inst("SOLNOAPI", "SOL", "NOAPI", api_enabled=False),
            self._inst("SOLNOSPOT", "SOL", "NOSPOT", spot_allowed=False),
        ]
        self.prices = {
            "SOLUSDT": "110.30",
            "SOLUSDC": "110.10",
            "SOLBTC": "0.00123",
            "ETHUSDT": "4000.12",
            "HYPEUSDT": "47.1234",
            "SUIUSDT": "1.2345",
            "BTCUSDT": "114000.12",
            "BTCUSDC": "113990.11",
        }

    def _inst(
        self,
        symbol: str,
        base: str,
        quote: str,
        *,
        quote_precision: int = 2,
        status: str = "1",
        api_enabled: bool = True,
        spot_allowed: bool = True,
    ) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "baseAsset": base,
            "quoteAsset": quote,
            "display_name": f"{base}/{quote}",
            "status": status,
            "isSpotTradingAllowed": spot_allowed,
            "orderTypes": ["LIMIT", "MARKET", "LIMIT_MAKER"],
            "quotePrecision": quote_precision,
            "baseSizePrecision": "0.01",
            "api_enabled_for_key": api_enabled,
            "api_eligible": api_enabled and spot_allowed and status == "1",
        }

    def list_exchanges(self) -> List[str]:
        return ["mexc"]

    def list_accounts(self, exchange: str) -> List[Any]:
        return ["amiroo"] if exchange == "mexc" else []

    def capabilities(self, exchange: str) -> List[str]:
        if exchange != "mexc":
            return []
        return ["balance", "orders", "open_orders", "list_instruments", "resolve_instrument", "market_price"]

    def execute(self, request: Dict[str, Any]) -> CanonicalResponse:
        self.requests.append(dict(request))
        op = str(request.get("operation") or "")
        if op == "list_instruments":
            q = str(request.get("query") or "").upper().replace("/", "")
            rows = [dict(x) for x in self.instruments if not q or q in x["symbol"] or q == x["baseAsset"]]
            return make_success(op, "mexc", "amiroo", data={"instruments": rows, "count": len(rows)})
        if op == "market_price":
            symbol = str(request.get("symbol") or "").upper()
            if symbol in self.price_failures:
                return make_failure(op, "mexc", "amiroo", code="PRICE_UNAVAILABLE", message="price unavailable")
            price = self.prices.get(symbol, "1")
            return make_success(
                op,
                "mexc",
                "amiroo",
                market_price=CanonicalMarketPrice(requested_symbol=symbol, market=symbol, mark_price=price, price=price),
                data={"symbol": symbol, "price": price},
            )
        return make_success(op, "mexc", "amiroo", data={})


class TradeSpotWizardNavigationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = FakeSpotDesk()
        self.wizard = TradeSpotWizard(spotdesk=self.desk)  # type: ignore[arg-type]
        self.key = ("chat",)

    def test_exchange_selection_works(self) -> None:
        screen = self.wizard.open(self.key)
        self.assertIn("🟦 Spot Trading", screen.text)
        self.assertIn("aftermath", _labels(screen))
        self.assertIn("raydium", _labels(screen))
        self.assertNotIn("hyperliquid", _labels(screen))
        self.assertTrue(all(not cb.startswith("trade:") for cb in _callbacks(screen)))

    def test_account_selection_works(self) -> None:
        self.wizard.open(self.key)
        screen = self.wizard.handle_callback(self.key, "exchange:raydium")
        self.assertIn("Exchange: raydium", screen.text)
        self.assertIn("amiroo", _labels(screen))
        self.assertIn("bitget", _labels(screen))
        screen = self.wizard.handle_callback(self.key, "account:amiroo")
        self.assertIn("Exchange: raydium", screen.text)
        self.assertIn("Account: amiroo", screen.text)

    def test_back_and_close_work(self) -> None:
        self.wizard.open(self.key)
        self.wizard.handle_callback(self.key, "exchange:raydium")
        screen = self.wizard.handle_callback(self.key, "back")
        self.assertIn("Select Exchange", screen.text)
        closed = self.wizard.handle_callback(self.key, "close")
        self.assertEqual(closed.state, "closed")
        self.assertEqual(closed.buttons, [])

    def test_capability_filtering_hides_unsupported_operations(self) -> None:
        desk = FakeSpotDesk(caps=["balance"])
        wizard = TradeSpotWizard(spotdesk=desk)  # type: ignore[arg-type]
        wizard.open(self.key)
        wizard.handle_callback(self.key, "exchange:raydium")
        screen = wizard.handle_callback(self.key, "account:amiroo")
        labels = _labels(screen)
        self.assertIn("💰 Balance", labels)
        self.assertNotIn("📋 Orders", labels)
        self.assertNotIn("➕ New Order", labels)
        self.assertNotIn("🪜 Ladder", labels)
        self.assertNotIn("❌ Cancel Orders", labels)

    def test_balance_and_orders_route_to_spotdesk(self) -> None:
        self.wizard.open(self.key)
        self.wizard.handle_callback(self.key, "exchange:raydium")
        self.wizard.handle_callback(self.key, "account:amiroo")
        balance = self.wizard.handle_callback(self.key, "action:balance")
        self.assertIn("12.34 USDC", balance.text)
        self.assertEqual(self.desk.requests[-1]["operation"], "balance")
        orders = self.wizard.handle_callback(self.key, "back")
        self.assertEqual(orders.state, "action")
        orders = self.wizard.handle_callback(self.key, "action:orders")
        self.assertIn("Open orders: 1", orders.text)
        self.assertEqual(self.desk.requests[-1]["operation"], "orders")

    def test_callback_namespace_does_not_collide_with_trade(self) -> None:
        screen = self.wizard.open(self.key)
        self.assertTrue(all(not cb.startswith("trade:") for cb in _callbacks(screen)))
        self.assertTrue(all(not cb.startswith("tradespot:") for cb in _callbacks(screen)))


class TradeSpotMexcNewOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = FakeMexcSpotDesk()
        self.wizard = TradeSpotWizard(spotdesk=self.desk)  # type: ignore[arg-type]
        self.key = ("chat-mexc",)
        self.wizard.open(self.key)
        self.wizard.handle_callback(self.key, "exchange:mexc")
        self.wizard.handle_callback(self.key, "account:amiroo")

    def _open_new_order(self) -> Any:
        self.wizard.open(self.key)
        self.wizard.handle_callback(self.key, "exchange:mexc")
        self.wizard.handle_callback(self.key, "account:amiroo")
        return self.wizard.handle_callback(self.key, "action:new_order")

    def test_new_order_initially_shows_quick_pick_assets_and_other(self) -> None:
        screen = self._open_new_order()
        self.assertIn("MEXC Spot — New Order", screen.text)
        labels = _labels(screen)
        for label in ["SOL", "ETH", "HYPE", "SUI", "Other"]:
            self.assertIn(label, labels)

    def test_clicking_sol_discovers_quote_markets_and_prices_dynamically(self) -> None:
        self._open_new_order()
        screen = self.wizard.handle_callback(self.key, "asset:SOL")
        labels = _labels(screen)
        self.assertTrue(any("SOL/USDT" in label and "110.30" in label for label in labels))
        self.assertTrue(any("SOL/USDC" in label and "110.10" in label for label in labels))
        self.assertTrue(any("SOL/BTC" in label and "0.00123" in label for label in labels))
        self.assertFalse(any("SOL/NOAPI" in label for label in labels))
        self.assertFalse(any("SOL/NOSPOT" in label for label in labels))
        self.assertFalse(any("SOL/DELIST" in label for label in labels))
        list_requests = [r for r in self.desk.requests if r.get("operation") == "list_instruments"]
        self.assertEqual(list_requests[-1]["query"], "SOL")

    def test_quick_pick_filters_each_base_asset(self) -> None:
        for asset, expected in [("ETH", "ETH/USDT"), ("HYPE", "HYPE/USDT"), ("SUI", "SUI/USDT")]:
            self._open_new_order()
            screen = self.wizard.handle_callback(self.key, f"asset:{asset}")
            labels = _labels(screen)
            self.assertTrue(any(expected in label for label in labels), (asset, labels))
            self.assertTrue(all(label.startswith(asset + "/") or label in {"⬅️ Back", "❌ Close"} for label in labels))

    def test_no_hard_coded_assumption_that_usdt_or_usdc_exists(self) -> None:
        self.desk.instruments = [self.desk._inst("SOLBTC", "SOL", "BTC", quote_precision=8)]
        self.desk.prices = {"SOLBTC": "0.00123"}
        self._open_new_order()
        screen = self.wizard.handle_callback(self.key, "asset:SOL")
        labels = _labels(screen)
        self.assertTrue(any("SOL/BTC" in label for label in labels))
        self.assertFalse(any("SOL/USDT" in label for label in labels))
        self.assertFalse(any("SOL/USDC" in label for label in labels))

    def test_other_base_asset_finds_all_btc_pairs(self) -> None:
        self._open_new_order()
        self.wizard.handle_callback(self.key, "asset:other")
        screen = self.wizard.handle_text(self.key, "BTC")
        assert screen is not None
        labels = _labels(screen)
        self.assertTrue(any("BTC/USDT" in label for label in labels))
        self.assertTrue(any("BTC/USDC" in label for label in labels))

    def test_other_complete_pair_resolves_slash_and_compact_symbols(self) -> None:
        for value in ["BTC/USDC", "BTCUSDC"]:
            self._open_new_order()
            self.wizard.handle_callback(self.key, "asset:other")
            screen = self.wizard.handle_text(self.key, value)
            assert screen is not None
            labels = _labels(screen)
            self.assertTrue(any("BTC/USDC" in label for label in labels), value)
            self.assertFalse(any("BTC/USDT" in label for label in labels), value)

    def test_invalid_symbol_gives_useful_retry_error(self) -> None:
        self._open_new_order()
        self.wizard.handle_callback(self.key, "asset:other")
        screen = self.wizard.handle_text(self.key, "NOTREAL")
        assert screen is not None
        self.assertIn("No API-enabled tradable MEXC spot pair found", screen.text)
        self.assertIn("Enter a spot symbol or pair", screen.text)

    def test_one_failed_ticker_lookup_does_not_break_pair_picker(self) -> None:
        self.desk.price_failures.add("SOLUSDC")
        self._open_new_order()
        screen = self.wizard.handle_callback(self.key, "asset:SOL")
        labels = _labels(screen)
        self.assertTrue(any("SOL/USDT" in label and "110.30" in label for label in labels))
        self.assertTrue(any("SOL/USDC" in label and "price unavailable" in label for label in labels))

    def test_selected_pair_stores_base_quote_and_side_semantics(self) -> None:
        self._open_new_order()
        pairs = self.wizard.handle_callback(self.key, "asset:SOL")
        callbacks = _callbacks(pairs)
        sol_usdc_cb = callbacks[_labels(pairs).index(next(label for label in _labels(pairs) if "SOL/USDC" in label))]
        side = self.wizard.handle_callback(self.key, sol_usdc_cb)
        self.assertIn("MEXC Spot — SOL/USDC", side.text)
        state = self.wizard._state_for(self.key)
        self.assertEqual(state.selected_instrument["symbol"], "SOLUSDC")  # type: ignore[index]
        self.assertEqual(state.selected_instrument["baseAsset"], "SOL")  # type: ignore[index]
        self.assertEqual(state.selected_instrument["quoteAsset"], "USDC")  # type: ignore[index]

        self.wizard.handle_callback(self.key, "side:buy")
        self.wizard.handle_text(self.key, "10")
        preview = self.wizard.handle_text(self.key, "110.10")
        assert preview is not None
        self.assertIn("Required balance: USDC", preview.text)
        self.assertIn("Estimated cost: 1101 USDC", preview.text)

        self._open_new_order()
        pairs = self.wizard.handle_callback(self.key, "asset:SOL")
        sol_usdc_cb = _callbacks(pairs)[_labels(pairs).index(next(label for label in _labels(pairs) if "SOL/USDC" in label))]
        self.wizard.handle_callback(self.key, sol_usdc_cb)
        self.wizard.handle_callback(self.key, "side:sell")
        self.wizard.handle_text(self.key, "10")
        preview = self.wizard.handle_text(self.key, "110.10")
        assert preview is not None
        self.assertIn("Required balance: SOL", preview.text)
        self.assertIn("Estimated proceeds: 1101 USDC", preview.text)

    def test_back_from_pair_selection_returns_to_asset_picker(self) -> None:
        self._open_new_order()
        self.wizard.handle_callback(self.key, "asset:SOL")
        screen = self.wizard.handle_callback(self.key, "back")
        labels = _labels(screen)
        for label in ["SOL", "ETH", "HYPE", "SUI", "Other"]:
            self.assertIn(label, labels)

    def test_confirm_does_not_submit_live_order(self) -> None:
        self._open_new_order()
        pairs = self.wizard.handle_callback(self.key, "asset:SOL")
        sol_usdc_cb = _callbacks(pairs)[_labels(pairs).index(next(label for label in _labels(pairs) if "SOL/USDC" in label))]
        self.wizard.handle_callback(self.key, sol_usdc_cb)
        self.wizard.handle_callback(self.key, "side:buy")
        self.wizard.handle_text(self.key, "1")
        self.wizard.handle_text(self.key, "110")
        screen = self.wizard.handle_callback(self.key, "confirm_disabled")
        self.assertIn("No order was placed", screen.text)
        self.assertFalse(any(r.get("operation") == "new_order" for r in self.desk.requests))


class TradeSpotCommandRegistrationTests(unittest.TestCase):
    def test_tradespot_command_is_registered_with_trade_capability(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kam_home_") as home:
            state_dir = Path(home) / "kam"
            state_dir.mkdir(parents=True)
            (state_dir / "install_state.json").write_text('{"capabilities":{"trade":true}}', encoding="utf-8")
            old_home = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = home
            try:
                import plugins.trade as trade_plugin

                importlib.reload(trade_plugin)
                self.assertIn("tradespot", trade_plugin.registered_commands())
            finally:
                if old_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = old_home

    def test_tradespot_command_handler_uses_tradespot_prefix(self) -> None:
        class Chat:
            id = 123

        class Msg:
            text = "/tradespot"
            chat = Chat()
            message_thread_id = None

        class Adapter:
            def __init__(self) -> None:
                self.sent: List[Dict[str, Any]] = []

            async def send_inline_keyboard(self, **kwargs: Any) -> None:
                self.sent.append(dict(kwargs))

        adapter = Adapter()
        handled = asyncio.run(handle_tradespot_command(adapter, Msg()))
        self.assertTrue(handled)
        self.assertEqual(adapter.sent[-1]["callback_prefix"], "tradespot")
        self.assertIn("Spot Trading", adapter.sent[-1]["text"])


class FakeMexcBalanceDesk:
    def __init__(self, assets: List[Dict[str, Any]], balance: CanonicalBalance | None = None) -> None:
        self.assets = assets
        self.balance = balance or CanonicalBalance("0.00", "USDT")

    def list_exchanges(self) -> List[str]:
        return ["mexc"]

    def list_accounts(self, exchange: str) -> List[Any]:
        return ["amiroo"] if exchange == "mexc" else []

    def capabilities(self, exchange: str) -> List[str]:
        return ["balance"] if exchange == "mexc" else []

    def execute(self, request: Dict[str, Any]) -> CanonicalResponse:
        op = str(request.get("operation") or "")
        return make_success(
            op,
            "mexc",
            "amiroo",
            balance=self.balance,
            data={"assets": list(self.assets)},
        )


class TradeSpotMexcBalanceScreenTests(unittest.TestCase):
    def _open_balance(self, assets: List[Dict[str, Any]], balance: CanonicalBalance | None = None):
        wizard = TradeSpotWizard(spotdesk=FakeMexcBalanceDesk(assets, balance))  # type: ignore[arg-type]
        key = ("mexc-balance",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:mexc")
        wizard.handle_callback(key, "account:amiroo")
        return wizard.handle_callback(key, "action:balance")

    def _inventory_lines(self, text: str) -> List[str]:
        lines = text.splitlines()
        self.assertIn("Assets", lines)
        start = lines.index("Assets") + 1
        return [ln for ln in lines[start:] if ln.strip()]

    def test_usdt_usdc_and_multiple_base_assets(self) -> None:
        screen = self._open_balance(
            [
                {"asset": "SUI", "total": "850"},
                {"asset": "USDT", "total": "1000.09"},
                {"asset": "ETH", "total": "0.40"},
                {"asset": "SOL", "total": "12.50"},
                {"asset": "USDC", "total": "500"},
                {"asset": "BTC", "total": "0.015"},
            ]
        )
        self.assertNotIn("Balance:", screen.text)
        self.assertEqual(
            self._inventory_lines(screen.text),
            [
                "USDT: 1,000.09",
                "USDC: 500.00",
                "BTC: 0.015",
                "ETH: 0.4",
                "SOL: 12.5",
                "SUI: 850",
            ],
        )

    def test_zero_usdt_still_listed(self) -> None:
        screen = self._open_balance(
            [
                {"asset": "USDC", "total": "7.10"},
                {"asset": "MX", "total": "1.25"},
            ]
        )
        lines = self._inventory_lines(screen.text)
        self.assertEqual(lines[0], "USDT: 0.00")
        self.assertEqual(lines[1], "USDC: 7.10")
        self.assertIn("MX: 1.25", lines)

    def test_zero_usdc_still_listed(self) -> None:
        screen = self._open_balance(
            [
                {"asset": "USDT", "total": "0.09"},
                {"asset": "MX", "total": "1.25"},
            ]
        )
        lines = self._inventory_lines(screen.text)
        self.assertEqual(lines[0], "USDT: 0.09")
        self.assertEqual(lines[1], "USDC: 0.00")
        self.assertIn("MX: 1.25", lines)

    def test_both_quote_assets_zero(self) -> None:
        screen = self._open_balance([{"asset": "SOL", "amount": "2"}])
        lines = self._inventory_lines(screen.text)
        self.assertEqual(lines[0], "USDT: 0.00")
        self.assertEqual(lines[1], "USDC: 0.00")
        self.assertIn("SOL: 2", lines)

    def test_small_btc_eth_precision_is_preserved(self) -> None:
        screen = self._open_balance(
            [
                {"asset": "BTC", "total": "0.00000015"},
                {"asset": "ETH", "total": "0.0000123"},
            ]
        )
        lines = self._inventory_lines(screen.text)
        self.assertIn("BTC: 0.00000015", lines)
        self.assertIn("ETH: 0.0000123", lines)
        self.assertNotIn("BTC: 0", lines)
        self.assertNotIn("BTC: 0.00", lines)

    def test_locked_amounts_are_included_in_total(self) -> None:
        screen = self._open_balance(
            [
                {"asset": "USDT", "free": "10", "locked": "2.5"},
                {"asset": "SOL", "free": "1.1", "locked": "0.4"},
            ]
        )
        lines = self._inventory_lines(screen.text)
        self.assertEqual(lines[0], "USDT: 12.50")
        self.assertIn("SOL: 1.5", lines)

    def test_no_duplicate_assets(self) -> None:
        screen = self._open_balance(
            [
                {"asset": "USDT", "total": "1.00"},
                {"asset": "USDT", "total": "2.00"},
                {"asset": "ETH", "total": "0.5"},
                {"asset": "ETH", "total": "0.25"},
            ]
        )
        lines = self._inventory_lines(screen.text)
        names = [ln.split(":", 1)[0] for ln in lines]
        self.assertEqual(names, ["USDT", "USDC", "ETH"])
        self.assertEqual(lines[0], "USDT: 3.00")
        self.assertEqual(lines[1], "USDC: 0.00")
        self.assertIn("ETH: 0.75", lines)

    def test_zero_non_quote_assets_are_omitted(self) -> None:
        screen = self._open_balance(
            [
                {"asset": "USDT", "total": "1"},
                {"asset": "SOL", "total": "0"},
                {"asset": "MX", "free": "0", "locked": "0"},
            ]
        )
        lines = self._inventory_lines(screen.text)
        self.assertEqual(lines, ["USDT: 1.00", "USDC: 0.00"])

    def test_deterministic_alphabetical_order_after_quotes(self) -> None:
        screen = self._open_balance(
            [
                {"asset": "SOL", "total": "1"},
                {"asset": "BTC", "total": "1"},
                {"asset": "ETH", "total": "1"},
                {"asset": "USDC", "total": "1"},
                {"asset": "MX", "total": "1"},
                {"asset": "USDT", "total": "1"},
            ]
        )
        self.assertEqual(
            self._inventory_lines(screen.text),
            [
                "USDT: 1.00",
                "USDC: 1.00",
                "BTC: 1",
                "ETH: 1",
                "MX: 1",
                "SOL: 1",
            ],
        )


if __name__ == "__main__":
    unittest.main()
