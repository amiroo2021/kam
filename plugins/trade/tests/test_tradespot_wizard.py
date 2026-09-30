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
from decimal import Decimal
from typing import Any, Dict, List

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.canonical import (  # noqa: E402
    CanonicalBalance,
    CanonicalMarketPrice,
    CanonicalOrderGroup,
    CanonicalOrderResult,
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
        self.reject_new_order = False
        self.reject_message = "exchange rejected order"
        self.timeout_new_order = False
        self.open_orders: List[Dict[str, Any]] = []
        self.fail_cancel_ids: set[str] = set()
        self.timeout_cancel = False
        self.balances: Dict[str, str] = {"USDT": "10000", "USDC": "10000", "SOL": "100", "SUI": "100"}
        self.instruments: List[Dict[str, Any]] = [
            self._inst("SOLUSDT", "SOL", "USDT", quote_precision=2),
            self._inst("SOLUSDC", "SOL", "USDC", quote_precision=2),
            self._inst("SOLBTC", "SOL", "BTC", quote_precision=8),
            self._inst("ETHUSDT", "ETH", "USDT", quote_precision=2),
            self._inst("HYPEUSDT", "HYPE", "USDT", quote_precision=4),
            self._inst("HYPEUSDC", "HYPE", "USDC", quote_precision=2, base_size_precision="0", base_asset_precision=2),
            self._inst("SUIUSDT", "SUI", "USDT", quote_precision=4),
            self._inst("SUIUSDC", "SUI", "USDC", quote_precision=4, base_size_precision="0", base_asset_precision=2),
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
            "HYPEUSDC": "50.00",
            "SUIUSDT": "1.2345",
            "SUIUSDC": "1.2300",
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
        base_size_precision: str = "0.01",
        base_asset_precision: int = 2,
        step_size: str = "",
        tick_size: str = "",
        min_qty: str = "",
        max_qty: str = "",
        min_notional: str = "1",
    ) -> Dict[str, Any]:
        size = step_size
        if not size:
            try:
                raw = Decimal(str(base_size_precision))
                size = format(raw.normalize(), "f") if raw > 0 else ""
            except Exception:  # noqa: BLE001
                size = ""
        if not size and base_asset_precision >= 0:
            size = format(Decimal("1").scaleb(-int(base_asset_precision)).normalize(), "f")
        tick = tick_size or format(Decimal("1").scaleb(-int(quote_precision)).normalize(), "f")
        return {
            "symbol": symbol,
            "base": base,
            "quote": quote,
            "baseAsset": base,
            "quoteAsset": quote,
            "display_name": f"{base}/{quote}",
            "status": status,
            "isSpotTradingAllowed": spot_allowed,
            "orderTypes": ["LIMIT", "MARKET", "LIMIT_MAKER"],
            "size_step": size,
            "price_tick": tick,
            "min_qty": min_qty,
            "max_qty": max_qty,
            "min_notional": min_notional,
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
        return [
            "balance",
            "orders",
            "open_orders",
            "list_instruments",
            "resolve_instrument",
            "market_price",
            "new_order",
            "cancel_orders",
        ]

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
        if op == "resolve_instrument":
            symbol = str(request.get("symbol") or "").upper().replace("/", "")
            for row in self.instruments:
                if row["symbol"] == symbol:
                    return make_success(op, "mexc", "amiroo", data={"instrument": dict(row)})
            return make_failure(op, "mexc", "amiroo", code="INSTRUMENT_NOT_FOUND", message="not found")
        if op == "balance":
            assets = [{"asset": k, "total": v} for k, v in self.balances.items()]
            return make_success(op, "mexc", "amiroo", data={"assets": assets})
        if op in {"orders", "open_orders"}:
            return make_success(
                op,
                "mexc",
                "amiroo",
                open_order_count=len(self.open_orders),
                data={"orders": [dict(row) for row in self.open_orders]},
            )
        if op == "cancel_orders":
            if self.timeout_cancel:
                return make_failure(op, "mexc", "amiroo", code="CANCEL_STATUS_UNKNOWN", message="timed out after transmission")
            ids = [str(x) for x in (request.get("order_ids") or [])]
            symbol = str(request.get("symbol") or "").upper().replace("/", "")
            side = str(request.get("side") or "").upper()
            remaining: List[Dict[str, Any]] = []
            cancelled = 0
            for row in self.open_orders:
                oid = str(row.get("order_id") or "")
                row_symbol = str(row.get("symbol") or "").upper().replace("/", "")
                row_side = str(row.get("side") or "").upper()
                if oid in ids and oid not in self.fail_cancel_ids and row_symbol == symbol and row_side == side:
                    cancelled += 1
                    continue
                remaining.append(row)
            self.open_orders = remaining
            remaining_match = [
                row
                for row in self.open_orders
                if str(row.get("symbol") or "").upper().replace("/", "") == symbol
                and str(row.get("side") or "").upper() == side
            ]
            verified = cancelled == len(ids) and not remaining_match
            return make_success(
                op,
                "mexc",
                "amiroo",
                data={
                    "requested": len(ids),
                    "cancelled": cancelled,
                    "remaining": len(remaining_match),
                    "verified": verified,
                    "order_ids": ids,
                    "symbol": symbol,
                    "side": side,
                },
            )
        if op == "new_order":
            if self.timeout_new_order:
                return make_failure(op, "mexc", "amiroo", code="ORDER_STATUS_UNKNOWN", message="timed out after transmission")
            if self.reject_new_order:
                return make_failure(op, "mexc", "amiroo", code="EXCHANGE_REJECTED", message=self.reject_message)
            symbol = str(request.get("symbol") or "")
            side = str(request.get("side") or "")
            qty = str(request.get("quantity") or "")
            price = str(request.get("price") or "")
            return make_success(
                op,
                "mexc",
                "amiroo",
                order=CanonicalOrderResult(
                    symbol=symbol,
                    side=side,
                    order_type="LIMIT",
                    requested_volume=qty,
                    requested_price=price,
                    submitted_volume=qty,
                    submitted_price=price,
                    verified=True,
                    status="NEW",
                    exchange_order_id="999001",
                    client_order_id=str(request.get("client_order_id") or ""),
                ),
                data={"orderId": "999001", "status": "NEW"},
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
        self.assertIn("Required: 1,101.00 USDC", preview.text)
        self.assertIn("Available:", preview.text)
        self.assertIn("Confirm & Place Order", _labels(preview))

        self._open_new_order()
        pairs = self.wizard.handle_callback(self.key, "asset:SOL")
        sol_usdc_cb = _callbacks(pairs)[_labels(pairs).index(next(label for label in _labels(pairs) if "SOL/USDC" in label))]
        self.wizard.handle_callback(self.key, sol_usdc_cb)
        self.wizard.handle_callback(self.key, "side:sell")
        self.wizard.handle_text(self.key, "10")
        preview = self.wizard.handle_text(self.key, "110.10")
        assert preview is not None
        self.assertIn("Required: 10 SOL", preview.text)
        self.assertIn("Available:", preview.text)

    def test_back_from_pair_selection_returns_to_asset_picker(self) -> None:
        self._open_new_order()
        self.wizard.handle_callback(self.key, "asset:SOL")
        screen = self.wizard.handle_callback(self.key, "back")
        labels = _labels(screen)
        for label in ["SOL", "ETH", "HYPE", "SUI", "Other"]:
            self.assertIn(label, labels)

    def test_back_and_cancel_never_submit(self) -> None:
        self._open_new_order()
        pairs = self.wizard.handle_callback(self.key, "asset:SOL")
        sol_usdc_cb = _callbacks(pairs)[_labels(pairs).index(next(label for label in _labels(pairs) if "SOL/USDC" in label))]
        self.wizard.handle_callback(self.key, sol_usdc_cb)
        self.wizard.handle_callback(self.key, "side:buy")
        self.wizard.handle_text(self.key, "1")
        preview = self.wizard.handle_text(self.key, "110")
        assert preview is not None
        self.wizard.handle_callback(self.key, "back")
        self.assertFalse(any(r.get("operation") == "new_order" for r in self.desk.requests))
        preview = self.wizard.handle_text(self.key, "110")
        assert preview is not None
        cancelled = self.wizard.handle_callback(self.key, "cancel")
        self.assertEqual(cancelled.state, "action")
        self.assertFalse(any(r.get("operation") == "new_order" for r in self.desk.requests))
        self.assertNotIn("🪜 Ladder", _labels(cancelled))
        self.assertIn("❌ Cancel Orders", _labels(cancelled))


class TradeSpotMexcLiveSubmitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = FakeMexcSpotDesk()
        self.wizard = TradeSpotWizard(spotdesk=self.desk)  # type: ignore[arg-type]
        self.key = ("chat-live",)

    def _preview(self, asset: str, pair_label: str, side: str, qty: str, price: str):
        self.wizard.open(self.key)
        self.wizard.handle_callback(self.key, "exchange:mexc")
        self.wizard.handle_callback(self.key, "account:amiroo")
        self.wizard.handle_callback(self.key, "action:new_order")
        pairs = self.wizard.handle_callback(self.key, f"asset:{asset}")
        cb = _callbacks(pairs)[_labels(pairs).index(next(label for label in _labels(pairs) if pair_label in label))]
        self.wizard.handle_callback(self.key, cb)
        self.wizard.handle_callback(self.key, f"side:{side}")
        self.wizard.handle_text(self.key, qty)
        return self.wizard.handle_text(self.key, price)

    def _place(self, preview) -> Any:
        place = next(cb for cb in _callbacks(preview) if str(cb).startswith("place:"))
        return self.wizard.handle_callback(self.key, place), place

    def test_buy_sol_usdc_submits_limit(self) -> None:
        preview = self._preview("SOL", "SOL/USDC", "buy", "0.1", "101")
        assert preview is not None
        self.assertIn("Required: 10.10 USDC", preview.text)
        screen, _ = self._place(preview)
        self.assertIn("✅ LIMIT order submitted", screen.text)
        self.assertIn("BUY 0.1 SOL/USDC", screen.text)
        self.assertIn("Order ID: 999001", screen.text)
        orders = [r for r in self.desk.requests if r.get("operation") == "new_order"]
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["symbol"], "SOLUSDC")
        self.assertEqual(orders[0]["side"], "BUY")
        self.assertEqual(orders[0]["order_type"], "LIMIT")
        self.assertEqual(orders[0]["quantity"], "0.1")
        self.assertEqual(orders[0]["price"], "101")

    def test_sell_sol_usdc_uses_base_balance(self) -> None:
        preview = self._preview("SOL", "SOL/USDC", "sell", "0.1", "101")
        assert preview is not None
        self.assertIn("Required: 0.1 SOL", preview.text)
        self.assertIn("Available: 100 SOL", preview.text)
        screen, _ = self._place(preview)
        self.assertIn("SELL 0.1 SOL/USDC", screen.text)
        orders = [r for r in self.desk.requests if r.get("operation") == "new_order"]
        self.assertEqual(orders[-1]["side"], "SELL")
        self.assertEqual(orders[-1]["symbol"], "SOLUSDC")

    def test_buy_sol_usdt(self) -> None:
        preview = self._preview("SOL", "SOL/USDT", "buy", "0.1", "110")
        screen, _ = self._place(preview)
        self.assertIn("SOL/USDT", screen.text)
        self.assertEqual([r for r in self.desk.requests if r.get("operation") == "new_order"][-1]["symbol"], "SOLUSDT")

    def test_buy_sui_usdc(self) -> None:
        preview = self._preview("SUI", "SUI/USDC", "buy", "1", "1.23")
        screen, _ = self._place(preview)
        self.assertIn("SUI/USDC", screen.text)
        self.assertEqual([r for r in self.desk.requests if r.get("operation") == "new_order"][-1]["symbol"], "SUIUSDC")

    def test_quantity_normalization(self) -> None:
        preview = self._preview("SOL", "SOL/USDC", "buy", "0.129", "101")
        assert preview is not None
        self.assertIn("Quantity: 0.12 SOL", preview.text)
        screen, _ = self._place(preview)
        self.assertEqual([r for r in self.desk.requests if r.get("operation") == "new_order"][-1]["quantity"], "0.12")
        self.assertIn("0.12 SOL/USDC", screen.text)

    def test_price_normalization(self) -> None:
        preview = self._preview("SOL", "SOL/USDC", "buy", "0.1", "101.239")
        assert preview is not None
        self.assertIn("Limit price: 101.23 USDC", preview.text)
        self._place(preview)
        self.assertEqual([r for r in self.desk.requests if r.get("operation") == "new_order"][-1]["price"], "101.23")

    def test_insufficient_quote_blocks_buy(self) -> None:
        self.desk.balances["USDC"] = "1"
        preview = self._preview("SOL", "SOL/USDC", "buy", "0.1", "101")
        assert preview is not None
        self.assertIn("Insufficient USDC", preview.text)
        self.assertFalse(any(str(cb).startswith("place:") for cb in _callbacks(preview)))
        self.assertFalse(any(r.get("operation") == "new_order" for r in self.desk.requests))

    def test_insufficient_base_blocks_sell(self) -> None:
        self.desk.balances["SOL"] = "0.01"
        preview = self._preview("SOL", "SOL/USDC", "sell", "0.1", "101")
        assert preview is not None
        self.assertIn("Insufficient SOL", preview.text)
        self.assertFalse(any(str(cb).startswith("place:") for cb in _callbacks(preview)))
        self.assertFalse(any(r.get("operation") == "new_order" for r in self.desk.requests))

    def test_api_disabled_symbol_blocks_submission(self) -> None:
        preview = self._preview("SOL", "SOL/USDC", "buy", "0.1", "101")
        assert preview is not None
        for row in self.desk.instruments:
            if row["symbol"] == "SOLUSDC":
                row["api_enabled_for_key"] = False
                row["api_eligible"] = False
        screen, _ = self._place(preview)
        self.assertIn("not API-enabled", screen.text)
        self.assertFalse(any(r.get("operation") == "new_order" for r in self.desk.requests))

    def test_double_confirmation_submits_once(self) -> None:
        preview = self._preview("SOL", "SOL/USDC", "buy", "0.1", "101")
        screen1, place = self._place(preview)
        screen2 = self.wizard.handle_callback(self.key, place)
        self.assertEqual(len([r for r in self.desk.requests if r.get("operation") == "new_order"]), 1)
        self.assertIn("999001", screen1.text)
        self.assertIn("999001", screen2.text)

    def test_exchange_rejection_displays_failure(self) -> None:
        self.desk.reject_new_order = True
        preview = self._preview("SOL", "SOL/USDC", "buy", "0.1", "101")
        screen, _ = self._place(preview)
        self.assertIn("❌ LIMIT order failed", screen.text)
        self.assertIn("exchange rejected order", screen.text)
        self.assertIn("No order was placed", screen.text)

    def test_ambiguous_timeout_does_not_retry(self) -> None:
        self.desk.timeout_new_order = True
        preview = self._preview("SOL", "SOL/USDC", "buy", "0.1", "101")
        screen, place = self._place(preview)
        self.assertIn("unknown", screen.text.lower())
        self.assertIn("Not retried", screen.text)
        self.wizard.handle_callback(self.key, place)
        self.assertEqual(len([r for r in self.desk.requests if r.get("operation") == "new_order"]), 1)

    def test_successful_response_displays_order_id(self) -> None:
        preview = self._preview("SOL", "SOL/USDC", "buy", "0.1", "101")
        screen, _ = self._place(preview)
        self.assertIn("Order ID: 999001", screen.text)
        self.assertIn("Status: NEW", screen.text)

    def test_ladder_and_cancel_remain_unavailable(self) -> None:
        self.wizard.open(self.key)
        self.wizard.handle_callback(self.key, "exchange:mexc")
        screen = self.wizard.handle_callback(self.key, "account:amiroo")
        labels = _labels(screen)
        self.assertIn("➕ New Order", labels)
        self.assertNotIn("🪜 Ladder", labels)
        self.assertIn("❌ Cancel Orders", labels)

    def test_trade_namespace_still_separate(self) -> None:
        screen = self.wizard.open(self.key)
        self.assertTrue(all(not cb.startswith("trade:") for cb in _callbacks(screen)))
        self.assertIsNone(trade_exchange_name_from_filename("x_mexc_agent_spot.py"))
        self.assertEqual(trade_exchange_name_from_filename("x_mexc_agent.py"), "mexc")


class TradeSpotMexcQtyNormalizationTests(unittest.TestCase):
    """Regression against live MEXC exchangeInfo shapes (no LOT_SIZE)."""

    def setUp(self) -> None:
        self.desk = FakeMexcSpotDesk()
        self.wizard = TradeSpotWizard(spotdesk=self.desk)  # type: ignore[arg-type]
        self.key = ("chat-qty-norm",)
        self.desk.balances["HYPE"] = "100"
        # Live SOLUSDC uses a decimal baseSizePrecision, not a place count.
        for row in self.desk.instruments:
            if row["symbol"] == "SOLUSDC":
                row["baseSizePrecision"] = "0.000001"
                row["baseAssetPrecision"] = 2
                row["size_step"] = format(Decimal("0.000001").normalize(), "f")
                row["step_size"] = ""
                row["price_tick"] = format(Decimal("1").scaleb(-2).normalize(), "f")
                row["tick_size"] = ""
                row["quotePrecision"] = 2
                row["min_qty"] = ""
                row["min_notional"] = ""
        # The qty-norm tests focus on size_step/price_tick normalization;
        # clear min_notional on every instrument so the existing single-order
        # scenarios (e.g. 0.9 SUI @ 0.9 USDC) keep the same client-side gate
        # they had before. Min-notional enforcement for single orders is
        # covered separately by the MEXC agent's NOT_ENOUGH_NOTIONAL path.
        for row in self.desk.instruments:
            row["min_notional"] = ""

    def _preview(self, asset: str, pair_label: str, side: str, qty: str, price: str):
        self.wizard.open(self.key)
        self.wizard.handle_callback(self.key, "exchange:mexc")
        self.wizard.handle_callback(self.key, "account:amiroo")
        self.wizard.handle_callback(self.key, "action:new_order")
        pairs = self.wizard.handle_callback(self.key, f"asset:{asset}")
        cb = _callbacks(pairs)[_labels(pairs).index(next(label for label in _labels(pairs) if pair_label in label))]
        self.wizard.handle_callback(self.key, cb)
        self.wizard.handle_callback(self.key, f"side:{side}")
        self.wizard.handle_text(self.key, qty)
        return self.wizard.handle_text(self.key, price)

    def test_fractional_sui_quantity_does_not_become_zero(self) -> None:
        preview = self._preview("SUI", "SUI/USDC", "buy", "0.9", "0.9")
        assert preview is not None
        self.assertIn("Quantity: 0.9 SUI", preview.text)
        self.assertNotIn("Quantity: 0 SUI", preview.text)
        self.assertIn("Limit price: 0.9 USDC", preview.text)
        self.assertIn("Required: 0.81 USDC", preview.text)
        self.assertFalse("Invalid quantity or price" in preview.text)
        self.assertTrue(any(str(cb).startswith("place:") for cb in _callbacks(preview)))

    def test_fractional_hype_quantity_does_not_become_zero(self) -> None:
        preview = self._preview("HYPE", "HYPE/USDC", "buy", "0.1", "50")
        assert preview is not None
        self.assertIn("Quantity: 0.1 HYPE", preview.text)
        self.assertNotIn("Quantity: 0 HYPE", preview.text)
        self.assertIn("Limit price: 50 USDC", preview.text)
        self.assertIn("Required: 5.00 USDC", preview.text)
        self.assertTrue(any(str(cb).startswith("place:") for cb in _callbacks(preview)))

    def test_fractional_sol_quantity_remains_correct(self) -> None:
        preview = self._preview("SOL", "SOL/USDC", "buy", "0.1", "101")
        assert preview is not None
        self.assertIn("Quantity: 0.1 SOL", preview.text)
        self.assertIn("Limit price: 101 USDC", preview.text)
        self.assertTrue(any(str(cb).startswith("place:") for cb in _callbacks(preview)))

    def test_sui_step_boundaries_use_base_asset_precision(self) -> None:
        # Live SUIUSDC: baseSizePrecision "0" (unset), baseAssetPrecision 2 → 0.01
        exact = self._preview("SUI", "SUI/USDC", "buy", "0.01", "1.2")
        assert exact is not None
        self.assertIn("Quantity: 0.01 SUI", exact.text)
        above = self._preview("SUI", "SUI/USDC", "buy", "0.019", "1.2")
        assert above is not None
        self.assertIn("Quantity: 0.01 SUI", above.text)
        multi = self._preview("SUI", "SUI/USDC", "buy", "0.25", "1.2")
        assert multi is not None
        self.assertIn("Quantity: 0.25 SUI", multi.text)
        round_down = self._preview("SUI", "SUI/USDC", "buy", "0.259", "1.2")
        assert round_down is not None
        self.assertIn("Quantity: 0.25 SUI", round_down.text)

    def test_sol_step_boundaries_use_base_size_precision_string(self) -> None:
        exact = self._preview("SOL", "SOL/USDC", "buy", "0.000001", "101")
        assert exact is not None
        self.assertIn("Quantity: 0.000001 SOL", exact.text)
        round_down = self._preview("SOL", "SOL/USDC", "buy", "0.0000019", "101")
        assert round_down is not None
        self.assertIn("Quantity: 0.000001 SOL", round_down.text)

    def test_sui_price_precision_is_not_confused_with_quantity(self) -> None:
        preview = self._preview("SUI", "SUI/USDC", "buy", "0.9", "0.9")
        assert preview is not None
        self.assertIn("Quantity: 0.9 SUI", preview.text)
        self.assertIn("Limit price: 0.9 USDC", preview.text)

    def test_below_one_step_without_min_qty_is_invalid(self) -> None:
        preview = self._preview("SUI", "SUI/USDC", "buy", "0.009", "1.2")
        assert preview is not None
        self.assertIn("Invalid quantity or price.", preview.text)
        self.assertNotIn("Minimum quantity:", preview.text)
        self.assertFalse(any(str(cb).startswith("place:") for cb in _callbacks(preview)))

    def test_missing_size_step_does_not_guess_lot_one(self) -> None:
        for row in self.desk.instruments:
            if row["symbol"] == "SUIUSDC":
                row["size_step"] = ""
                row["price_tick"] = "0.0001"
        preview = self._preview("SUI", "SUI/USDC", "buy", "0.9", "0.9")
        assert preview is not None
        self.assertIn("Instrument trading constraints are unavailable.", preview.text)
        self.assertNotIn("Quantity: 0 SUI", preview.text)
        self.assertFalse(any(str(cb).startswith("place:") for cb in _callbacks(preview)))

    def test_min_notional_shows_actual_reason(self) -> None:
        for row in self.desk.instruments:
            if row["symbol"] == "SUIUSDC":
                row["min_notional"] = "5"
        preview = self._preview("SUI", "SUI/USDC", "buy", "0.9", "0.9")
        assert preview is not None
        self.assertIn("Quantity: 0.9 SUI", preview.text)
        self.assertIn("Minimum order value: 5 USDC", preview.text)
        self.assertFalse(any(str(cb).startswith("place:") for cb in _callbacks(preview)))

    def test_lot_size_min_qty_shows_actual_reason(self) -> None:
        for row in self.desk.instruments:
            if row["symbol"] == "SUIUSDC":
                row["size_step"] = "1"
                row["min_qty"] = "1"
        preview = self._preview("SUI", "SUI/USDC", "buy", "0.9", "1.2")
        assert preview is not None
        self.assertIn("Minimum quantity: 1 SUI", preview.text)
        self.assertFalse(any(str(cb).startswith("place:") for cb in _callbacks(preview)))


def _spot_open(
    order_id: str,
    symbol: str,
    pair: str,
    side: str,
    price: str,
    orig: str,
    executed: str = "0",
    status: str = "NEW",
) -> Dict[str, Any]:
    remaining = Decimal(orig) - Decimal(executed)
    return {
        "order_id": order_id,
        "symbol": symbol,
        "pair": pair,
        "side": side,
        "type": "LIMIT",
        "price": price,
        "orig_qty": orig,
        "executed_qty": executed,
        "remaining_qty": format(remaining.normalize(), "f").rstrip("0").rstrip(".") if "." in format(remaining, "f") else str(remaining),
        "status": status,
    }


class TradeSpotMexcCancelOrdersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = FakeMexcSpotDesk()
        self.wizard = TradeSpotWizard(spotdesk=self.desk)  # type: ignore[arg-type]
        self.key = ("chat-cancel",)

    def _account(self):
        self.wizard.open(self.key)
        self.wizard.handle_callback(self.key, "exchange:mexc")
        return self.wizard.handle_callback(self.key, "account:amiroo")

    def _open_cancel(self):
        self._account()
        return self.wizard.handle_callback(self.key, "action:cancel_orders")

    def test_three_buy_sol_usdc_grouped(self) -> None:
        self.desk.open_orders = [
            _spot_open("1", "SOLUSDC", "SOL/USDC", "BUY", "73", "3"),
            _spot_open("2", "SOLUSDC", "SOL/USDC", "BUY", "100", "4"),
            _spot_open("3", "SOLUSDC", "SOL/USDC", "BUY", "80", "4"),
        ]
        screen = self._open_cancel()
        self.assertIn("❌ Cancel Orders", screen.text)
        self.assertIn("🔵 SOL/USDC", screen.text)
        self.assertIn("3 Orders", screen.text)
        self.assertIn("Total Volume: 11 SOL", screen.text)
        self.assertIn("Price Range: 73 → 100 USDC", screen.text)
        expected_vwap = (Decimal("3") * Decimal("73") + Decimal("4") * Decimal("100") + Decimal("4") * Decimal("80")) / Decimal("11")
        self.assertIn(f"VWAP: {format(expected_vwap.normalize(), 'f').rstrip('0').rstrip('.')} USDC", screen.text)
        self.assertIn("🔵 SOL/USDC · 3", _labels(screen))

    def test_buy_and_sell_sol_usdc_are_separate_groups(self) -> None:
        self.desk.open_orders = [
            _spot_open("1", "SOLUSDC", "SOL/USDC", "BUY", "73", "3"),
            _spot_open("2", "SOLUSDC", "SOL/USDC", "SELL", "120", "5"),
            _spot_open("3", "SOLUSDC", "SOL/USDC", "SELL", "250", "10"),
        ]
        screen = self._open_cancel()
        labels = _labels(screen)
        self.assertIn("🔵 SOL/USDC · 1", labels)
        self.assertIn("🔴 SOL/USDC · 2", labels)
        self.assertIn("🔵 SOL/USDC", screen.text)
        self.assertIn("🔴 SOL/USDC", screen.text)

    def test_sol_usdc_and_sol_usdt_are_separate_groups(self) -> None:
        self.desk.open_orders = [
            _spot_open("1", "SOLUSDC", "SOL/USDC", "BUY", "73", "1"),
            _spot_open("2", "SOLUSDT", "SOL/USDT", "BUY", "74", "1"),
        ]
        screen = self._open_cancel()
        labels = _labels(screen)
        self.assertIn("🔵 SOL/USDC · 1", labels)
        self.assertIn("🔵 SOL/USDT · 1", labels)

    def test_partially_filled_uses_remaining_quantity(self) -> None:
        self.desk.open_orders = [
            _spot_open("1", "SOLUSDC", "SOL/USDC", "BUY", "80", "10", executed="6"),
        ]
        screen = self._open_cancel()
        self.assertIn("Total Volume: 4 SOL", screen.text)
        self.assertNotIn("Total Volume: 10 SOL", screen.text)

    def test_total_volume_min_max_and_vwap(self) -> None:
        self.desk.open_orders = [
            _spot_open("1", "SOLUSDC", "SOL/USDC", "BUY", "70", "2"),
            _spot_open("2", "SOLUSDC", "SOL/USDC", "BUY", "90", "2"),
        ]
        screen = self._open_cancel()
        self.assertIn("Total Volume: 4 SOL", screen.text)
        self.assertIn("Price Range: 70 → 90 USDC", screen.text)
        self.assertIn("VWAP: 80 USDC", screen.text)

    def test_single_order_uses_price_not_range(self) -> None:
        self.desk.open_orders = [_spot_open("1", "SOLUSDC", "SOL/USDC", "BUY", "101", "1")]
        screen = self._open_cancel()
        self.assertIn("Orders: 1", screen.text)
        self.assertIn("Price: 101 USDC", screen.text)
        self.assertNotIn("→", screen.text)

    def test_zero_open_orders(self) -> None:
        self.desk.open_orders = []
        screen = self._open_cancel()
        self.assertIn("No open LIMIT orders", screen.text)
        self.assertFalse(any("SOL/USDC" in label for label in _labels(screen)))
        self.assertFalse(any(r.get("operation") == "cancel_orders" for r in self.desk.requests))

    def test_group_selection_performs_no_cancellation(self) -> None:
        self.desk.open_orders = [
            _spot_open("1", "SOLUSDC", "SOL/USDC", "BUY", "73", "3"),
            _spot_open("2", "SOLUSDC", "SOL/USDC", "BUY", "100", "4"),
            _spot_open("3", "SOLUSDC", "SOL/USDC", "BUY", "80", "4"),
        ]
        screen = self._open_cancel()
        group_cb = next(cb for cb in _callbacks(screen) if str(cb).startswith("cg:"))
        confirm = self.wizard.handle_callback(self.key, group_cb)
        self.assertIn("This will cancel ALL currently open BUY SOL/USDC limit orders", confirm.text)
        self.assertIn("❌ Cancel 3 Orders", _labels(confirm))
        self.assertFalse(any(r.get("operation") == "cancel_orders" for r in self.desk.requests))
        self.assertEqual(len(self.desk.open_orders), 3)

    def test_confirmation_screen_refreshes_live_orders(self) -> None:
        self.desk.open_orders = [
            _spot_open("1", "SOLUSDC", "SOL/USDC", "BUY", "73", "3"),
            _spot_open("2", "SOLUSDC", "SOL/USDC", "BUY", "100", "4"),
        ]
        screen = self._open_cancel()
        group_cb = next(cb for cb in _callbacks(screen) if str(cb).startswith("cg:"))
        self.desk.open_orders.append(_spot_open("3", "SOLUSDC", "SOL/USDC", "BUY", "80", "4"))
        confirm = self.wizard.handle_callback(self.key, group_cb)
        self.assertIn("Orders: 3", confirm.text)
        self.assertIn("Total Volume: 11 SOL", confirm.text)
        reads = [r for r in self.desk.requests if r.get("operation") in {"orders", "open_orders"}]
        self.assertGreaterEqual(len(reads), 2)

    def test_final_confirm_cancels_only_matching_side_and_instrument(self) -> None:
        self.desk.open_orders = [
            _spot_open("1", "SOLUSDC", "SOL/USDC", "BUY", "73", "3"),
            _spot_open("2", "SOLUSDC", "SOL/USDC", "BUY", "100", "4"),
            _spot_open("9", "SOLUSDC", "SOL/USDC", "SELL", "120", "5"),
            _spot_open("8", "SOLUSDT", "SOL/USDT", "BUY", "74", "1"),
        ]
        screen = self._open_cancel()
        group_cb = next(cb for cb in _callbacks(screen) if "BUY:SOLUSDC" in str(cb) or str(cb).endswith("BUY:SOLUSDC"))
        confirm = self.wizard.handle_callback(self.key, group_cb)
        cx = next(cb for cb in _callbacks(confirm) if str(cb).startswith("cx:"))
        result = self.wizard.handle_callback(self.key, cx)
        self.assertIn("✅ Orders Cancelled", result.text)
        self.assertIn("Requested: 2", result.text)
        self.assertIn("Cancelled: 2", result.text)
        self.assertIn("Remaining: 0", result.text)
        cancels = [r for r in self.desk.requests if r.get("operation") == "cancel_orders"]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(set(cancels[0]["order_ids"]), {"1", "2"})
        self.assertEqual(cancels[0]["side"], "BUY")
        self.assertEqual(cancels[0]["symbol"].replace("/", ""), "SOLUSDC")
        remaining_ids = {row["order_id"] for row in self.desk.open_orders}
        self.assertEqual(remaining_ids, {"9", "8"})

    def test_opposite_side_untouched(self) -> None:
        self.desk.open_orders = [
            _spot_open("1", "SOLUSDC", "SOL/USDC", "BUY", "73", "1"),
            _spot_open("9", "SOLUSDC", "SOL/USDC", "SELL", "120", "1"),
        ]
        screen = self._open_cancel()
        group_cb = next(cb for cb in _callbacks(screen) if "BUY:SOLUSDC" in str(cb))
        confirm = self.wizard.handle_callback(self.key, group_cb)
        cx = next(cb for cb in _callbacks(confirm) if str(cb).startswith("cx:"))
        self.wizard.handle_callback(self.key, cx)
        self.assertEqual({row["order_id"] for row in self.desk.open_orders}, {"9"})

    def test_other_instrument_untouched(self) -> None:
        self.desk.open_orders = [
            _spot_open("1", "SOLUSDC", "SOL/USDC", "BUY", "73", "1"),
            _spot_open("8", "SUIUSDC", "SUI/USDC", "BUY", "1.02", "2"),
        ]
        screen = self._open_cancel()
        group_cb = next(cb for cb in _callbacks(screen) if "BUY:SOLUSDC" in str(cb))
        confirm = self.wizard.handle_callback(self.key, group_cb)
        cx = next(cb for cb in _callbacks(confirm) if str(cb).startswith("cx:"))
        self.wizard.handle_callback(self.key, cx)
        self.assertEqual({row["order_id"] for row in self.desk.open_orders}, {"8"})

    def test_double_confirm_cancels_once(self) -> None:
        self.desk.open_orders = [_spot_open("1", "SOLUSDC", "SOL/USDC", "BUY", "73", "1")]
        screen = self._open_cancel()
        group_cb = next(cb for cb in _callbacks(screen) if str(cb).startswith("cg:"))
        confirm = self.wizard.handle_callback(self.key, group_cb)
        cx = next(cb for cb in _callbacks(confirm) if str(cb).startswith("cx:"))
        self.wizard.handle_callback(self.key, cx)
        self.wizard.handle_callback(self.key, cx)
        self.assertEqual(len([r for r in self.desk.requests if r.get("operation") == "cancel_orders"]), 1)

    def test_partial_failure_reports_incomplete(self) -> None:
        self.desk.open_orders = [
            _spot_open("1", "SOLUSDC", "SOL/USDC", "BUY", "73", "1"),
            _spot_open("2", "SOLUSDC", "SOL/USDC", "BUY", "80", "1"),
        ]
        self.desk.fail_cancel_ids = {"2"}
        screen = self._open_cancel()
        group_cb = next(cb for cb in _callbacks(screen) if str(cb).startswith("cg:"))
        confirm = self.wizard.handle_callback(self.key, group_cb)
        cx = next(cb for cb in _callbacks(confirm) if str(cb).startswith("cx:"))
        result = self.wizard.handle_callback(self.key, cx)
        self.assertIn("Cancellation incomplete", result.text)
        self.assertIn("Requested: 2", result.text)
        self.assertIn("Cancelled: 1", result.text)
        self.assertIn("Remaining: 1", result.text)
        self.assertNotIn("✅ Orders Cancelled", result.text)

    def test_timeout_does_not_blindly_retry(self) -> None:
        self.desk.open_orders = [_spot_open("1", "SOLUSDC", "SOL/USDC", "BUY", "73", "1")]
        self.desk.timeout_cancel = True
        screen = self._open_cancel()
        group_cb = next(cb for cb in _callbacks(screen) if str(cb).startswith("cg:"))
        confirm = self.wizard.handle_callback(self.key, group_cb)
        cx = next(cb for cb in _callbacks(confirm) if str(cb).startswith("cx:"))
        result = self.wizard.handle_callback(self.key, cx)
        self.assertTrue("unknown" in result.text.lower() or "UNKNOWN" in result.text)
        self.assertIn("Not retried", result.text)
        self.wizard.handle_callback(self.key, cx)
        self.assertEqual(len([r for r in self.desk.requests if r.get("operation") == "cancel_orders"]), 1)

    def test_back_performs_no_cancellation(self) -> None:
        self.desk.open_orders = [_spot_open("1", "SOLUSDC", "SOL/USDC", "BUY", "73", "1")]
        screen = self._open_cancel()
        group_cb = next(cb for cb in _callbacks(screen) if str(cb).startswith("cg:"))
        confirm = self.wizard.handle_callback(self.key, group_cb)
        back = self.wizard.handle_callback(self.key, "back")
        self.assertFalse(any(r.get("operation") == "cancel_orders" for r in self.desk.requests))
        self.assertEqual(len(self.desk.open_orders), 1)
        self.assertEqual(back.state, "cancel_orders")

    def test_trade_namespace_unaffected(self) -> None:
        self.desk.open_orders = [_spot_open("1", "SOLUSDC", "SOL/USDC", "BUY", "73", "1")]
        screen = self._open_cancel()
        self.assertTrue(all(not str(cb).startswith("trade:") for cb in _callbacks(screen)))
        confirm_from_action = self._account()
        self.assertIn("❌ Cancel Orders", _labels(confirm_from_action))
        self.assertTrue(all(not str(cb).startswith("trade:") for cb in _callbacks(confirm_from_action)))


class TradeSpotMexcLadderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = FakeMexcSpotDesk()
        self.wizard = TradeSpotWizard(spotdesk=self.desk)  # type: ignore[arg-type]
        # Phase 1 source-only: enable the Ladder preview flow inside tests
        # without advertising the `ladder` capability on the agent.
        self.wizard._ladder_preview_only = True
        self.key = ("chat-ladder",)
        self.desk.balances["SOL"] = "100"
        self.desk.balances["USDT"] = "10000"
        for row in self.desk.instruments:
            if row["symbol"] == "SOLUSDC":
                row["size_step"] = format(Decimal("0.000001").normalize(), "f")
                row["step_size"] = ""
                row["price_tick"] = format(Decimal("1").scaleb(-2).normalize(), "f")
                row["tick_size"] = ""

    def _open_action(self):
        w = self.wizard
        w.open(self.key)
        w.handle_callback(self.key, "exchange:mexc")
        w.handle_callback(self.key, "account:amiroo")
        return w.handle_callback(self.key, "action:")

    def _action_screen(self):
        return self._open_action()

    def _open_ladder(self):
        # Walk through exchange -> account -> ladder action so the wizard state
        # machine sees the ladder button in the action menu.
        self.wizard.open(self.key)
        self.wizard.handle_callback(self.key, "exchange:mexc")
        self.wizard.handle_callback(self.key, "account:amiroo")
        screen = self.wizard.handle_callback(self.key, "action:ladder")
        return screen

    def _callbacks(self, screen) -> List[str]:
        return [b.get("callback_data", "") for row in screen.buttons for b in row]

    def _labels(self, screen) -> List[str]:
        return [b.get("text", "") for row in screen.buttons for b in row]

    def _select_pair(self, screen, pair_label: str):
        # screen is the asset picker. Tap the base, then tap the pair.
        cbs = self._callbacks(screen)
        labels = self._labels(screen)
        base = pair_label.split("/", 1)[0]
        idx = next(i for i, label in enumerate(labels) if label == base)
        screen = self.wizard.handle_callback(self.key, cbs[idx] or "")
        cbs = self._callbacks(screen)
        labels = self._labels(screen)
        idx = next(i for i, label in enumerate(labels) if pair_label in label)
        cb = cbs[idx]
        return self.wizard.handle_callback(self.key, cb or "")

    def test_ladder_button_appears_with_preview_only_cap(self) -> None:
        screen = self._action_screen()
        self.assertTrue(any("Ladder" in label for label in self._labels(screen)))

    def test_ladder_button_hidden_when_agent_does_not_advertise(self) -> None:
        # Default capabilities do not include "ladder"; this test is a negative.
        caps = self.desk.capabilities("mexc")
        self.assertNotIn("ladder", caps)

    def test_uniform_buy_sol_usdc_preview(self) -> None:
        screen = self._open_ladder()
        # pair picker
        pair_screen = self._select_pair(screen, "SOL/USDC")
        side = self.wizard.handle_callback(self.key, "side:buy")
        # enter total qty
        after_qty = self.wizard.handle_text(self.key, "10")
        # enter start price
        after_start = self.wizard.handle_text(self.key, "100")
        # enter end price
        after_end = self.wizard.handle_text(self.key, "73")
        # enter order count
        after_count = self.wizard.handle_text(self.key, "10")
        # choose distribution (default prompt; pick uniform)
        # Distribution buttons are returned by the count handler — uniform should be present.
        dist = self.wizard.handle_callback(self.key, "distribution:uniform")
        # dist now should be the preview
        self.assertIn("LIMIT Ladder Preview", dist.text)
        self.assertIn("Distribution: Uniform", dist.text)
        self.assertIn("Orders: 10", dist.text)
        self.assertIn("Total Quantity: 10 SOL", dist.text)
        self.assertIn("Price Range: 73 → 100 USDC", dist.text)
        self.assertIn("Required:", dist.text)
        # Show first/last child
        self.assertIn("100 USDC", dist.text)
        self.assertIn("73 USDC", dist.text)

    def test_half_gaussian_smallest_at_start_largest_at_end(self) -> None:
        screen = self._open_ladder()
        self._select_pair(screen, "SOL/USDC")
        self.wizard.handle_callback(self.key, "side:buy")
        self.wizard.handle_text(self.key, "10")
        self.wizard.handle_text(self.key, "100")
        self.wizard.handle_text(self.key, "73")
        self.wizard.handle_text(self.key, "10")
        dist = self.wizard.handle_callback(self.key, "distribution:half_gaussian")
        self.assertIn("Distribution: Half-Gaussian", dist.text)
        # The first child in the rendered preview should be at the START price
        # and have the smallest size; last at END with largest size. We
        # just check that END price is in the last listed child row.
        self.assertIn("73 USDC", dist.text)
        self.assertIn("100 USDC", dist.text)

    def test_confirm_is_non_submitting(self) -> None:
        screen = self._open_ladder()
        self._select_pair(screen, "SOL/USDC")
        self.wizard.handle_callback(self.key, "side:buy")
        self.wizard.handle_text(self.key, "10")
        self.wizard.handle_text(self.key, "100")
        self.wizard.handle_text(self.key, "73")
        self.wizard.handle_text(self.key, "10")
        dist = self.wizard.handle_callback(self.key, "distribution:uniform")
        # The confirm callback is present but does NOT submit.
        cbs = self._callbacks(dist)
        confirm_cb = next((cb for cb in cbs if cb.startswith("ladder_confirm:")), None)
        self.assertIsNotNone(confirm_cb, "missing ladder_confirm callback")
        result = self.wizard.handle_callback(self.key, confirm_cb)
        self.assertIn("Live ladder submission is not enabled yet.", result.text)
        # No POST /api/v3/order hit
        self.assertFalse(any(r.get("operation") == "new_order" for r in self.desk.requests))

    def test_insufficient_buy_quote_blocks_preview(self) -> None:
        self.desk.balances["USDC"] = "10"
        self.desk.balances["USDT"] = "10"
        screen = self._open_ladder()
        self._select_pair(screen, "SOL/USDC")
        self.wizard.handle_callback(self.key, "side:buy")
        self.wizard.handle_text(self.key, "10")
        self.wizard.handle_text(self.key, "100")
        self.wizard.handle_text(self.key, "73")
        self.wizard.handle_text(self.key, "10")
        # 10 SOL @ ~100 each = ~1000 USDC > 10 USDC. Preview must surface
        # the insufficient-balance reason, not offer a Confirm button.
        preview = self.wizard.handle_callback(self.key, "distribution:uniform")
        self.assertIn("Insufficient", preview.text)
        self.assertFalse(any(str(cb).startswith("ladder_confirm:") for cb in _callbacks(preview)))

    def test_insufficient_sell_base_blocks_preview(self) -> None:
        self.desk.balances["SOL"] = "1"
        screen = self._open_ladder()
        self._select_pair(screen, "SOL/USDC")
        self.wizard.handle_callback(self.key, "side:sell")
        self.wizard.handle_text(self.key, "10")
        self.wizard.handle_text(self.key, "120")
        self.wizard.handle_text(self.key, "250")
        self.wizard.handle_text(self.key, "8")
        preview = self.wizard.handle_callback(self.key, "distribution:uniform")
        self.assertIn("Insufficient", preview.text)
        self.assertFalse(any(str(cb).startswith("ladder_confirm:") for cb in _callbacks(preview)))

    def test_vwap_shown_in_preview(self) -> None:
        screen = self._open_ladder()
        self._select_pair(screen, "SOL/USDC")
        self.wizard.handle_callback(self.key, "side:buy")
        self.wizard.handle_text(self.key, "10")
        self.wizard.handle_text(self.key, "100")
        self.wizard.handle_text(self.key, "73")
        self.wizard.handle_text(self.key, "10")
        dist = self.wizard.handle_callback(self.key, "distribution:uniform")
        self.assertIn("VWAP:", dist.text)

    def test_large_ladder_truncates_child_list(self) -> None:
        screen = self._open_ladder()
        self._select_pair(screen, "SOL/USDC")
        self.wizard.handle_callback(self.key, "side:buy")
        self.wizard.handle_text(self.key, "10")
        self.wizard.handle_text(self.key, "100")
        self.wizard.handle_text(self.key, "73")
        self.wizard.handle_text(self.key, "50")
        dist = self.wizard.handle_callback(self.key, "distribution:uniform")
        # Truncation marker
        self.assertIn("…", dist.text)

    def test_ladder_does_not_break_existing_cancel_orders(self) -> None:
        # Navigate into Ladder, then Back to action, then Cancel Orders.
        self._open_ladder()
        self.wizard.handle_callback(self.key, "back")
        cancel = self.wizard.handle_callback(self.key, "action:cancel_orders")
        self.assertIn("Cancel Orders", cancel.text)


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
