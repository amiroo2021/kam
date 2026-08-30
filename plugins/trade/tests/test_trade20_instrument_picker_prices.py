"""Trade 2.0 — New Order instrument picker with market prices (Lighter-shaped).

Offline only. Exercises wizard wiring against a stub desk that mirrors
Lighter capabilities: resolve_instrument + list_instruments + market_price.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.canonical import (  # noqa: E402
    CanonicalInstrument,
    CanonicalMarketPrice,
    CanonicalOrderResult,
    make_failure,
    make_success,
)
from plugins.trade.wizard import TradeWizard  # noqa: E402


class LighterShapedStubDesk:
    """Stub desk with Lighter-like caps and catalog/price responses."""

    def __init__(
        self,
        *,
        resolve_map: Optional[Dict[str, str]] = None,
        catalog: Optional[List[Dict[str, Any]]] = None,
        prices: Optional[Dict[str, str]] = None,
        exchanges: Optional[List[str]] = None,
        accounts: Optional[Dict[str, List[str]]] = None,
    ) -> None:
        self.resolve_map = dict(resolve_map or {})
        self.catalog = list(
            catalog
            or [
                {"instrument": "BTC", "base": "BTC", "market_type": "perp"},
                {"instrument": "ETH", "base": "ETH", "market_type": "perp"},
                {"instrument": "SOL", "base": "SOL", "market_type": "perp"},
                {"instrument": "HYPE", "base": "HYPE", "market_type": "perp"},
                {"instrument": "XAU", "base": "XAU", "market_type": "perp"},
            ]
        )
        self.prices = dict(
            prices
            or {
                "BTC": "95000.5",
                "ETH": "3400.25",
                "SOL": "145.1",
                "HYPE": "22.5",
                "XAU": "2650.0",
            }
        )
        self._exchanges = list(exchanges or ["lighter"])
        self._accounts = dict(accounts or {"lighter": ["main"]})
        self.requests: List[Dict[str, Any]] = []

    def list_exchanges(self) -> List[str]:
        return list(self._exchanges)

    def list_accounts(self, exchange: str) -> List[str]:
        return list(self._accounts.get(exchange, []))

    def capabilities(self, exchange: str) -> List[str]:
        return [
            "resolve_instrument",
            "list_instruments",
            "market_price",
            "new_order",
            "ladder",
        ]

    def execute(self, request: Dict[str, Any]):
        req = dict(request)
        self.requests.append(req)
        op = str(req.get("operation") or "")
        exchange = str(req.get("exchange") or "")
        account = str(req.get("account") or "")
        symbol = str(req.get("symbol") or "").strip().upper()

        if op == "resolve_instrument":
            native = self.resolve_map.get(symbol)
            if native is None:
                return make_failure(
                    operation=op,
                    exchange=exchange,
                    account=account,
                    code="INSTRUMENT_NOT_FOUND",
                    message=f"Instrument not found: {symbol}",
                )
            return make_success(
                operation=op,
                exchange=exchange,
                account=account,
                instrument=CanonicalInstrument(
                    requested_symbol=symbol,
                    symbol=native,
                    display_name=native,
                ),
            )

        if op == "list_instruments":
            return make_success(
                operation=op,
                exchange=exchange,
                account=account,
                data={"instruments": list(self.catalog)},
            )

        if op == "market_price":
            price = self.prices.get(symbol)
            if price is None:
                return make_failure(
                    operation=op,
                    exchange=exchange,
                    account=account,
                    code="PRICE_UNAVAILABLE",
                    message=f"Price unavailable for {symbol}",
                )
            return make_success(
                operation=op,
                exchange=exchange,
                account=account,
                market_price=CanonicalMarketPrice(
                    requested_symbol=symbol,
                    market=symbol,
                    mark_price=price,
                    price=price,
                ),
            )

        if op == "new_order":
            return make_success(
                operation=op,
                exchange=exchange,
                account=account,
                order=CanonicalOrderResult(
                    symbol=symbol or str(req.get("symbol") or ""),
                    side=str(req.get("side") or "buy"),
                    order_type="limit",
                    requested_volume=str(req.get("volume") or "0"),
                    requested_price=str(req.get("price") or "0"),
                    submitted_volume=str(req.get("volume") or "0"),
                    submitted_price=str(req.get("price") or "0"),
                    verified=True,
                ),
            )

        return make_failure(
            operation=op,
            exchange=exchange,
            account=account,
            code="NOT_IMPLEMENTED",
            message=f"stub missing op {op}",
        )


class TestTrade20InstrumentPickerPrices(unittest.TestCase):
    def _open_new_order(self, desk: LighterShapedStubDesk):
        wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
        key = ("chat",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:lighter")
        wizard.handle_callback(key, "account:main")
        wizard.handle_callback(key, "action:new_order")
        return wizard, key

    def test_btc_preset_shows_priced_pick_button(self) -> None:
        desk = LighterShapedStubDesk(resolve_map={"BTC": "BTC"})
        wizard, key = self._open_new_order(desk)
        screen = wizard.handle_callback(key, "symbol:BTC")
        self.assertEqual(screen.state, "instrument_confirm")
        self.assertIn("Select Instrument", screen.text)
        self.assertIn("Source: BTC", screen.text)
        labels = [b["text"] for row in screen.buttons for b in row]
        self.assertTrue(any("BTC" in t and "95,000.5" in t for t in labels))
        self.assertTrue(any(t.startswith("Other") for t in labels))
        # Not committed until pick.
        self.assertIsNone(wizard._state_for(key).symbol)

        ops = [r["operation"] for r in desk.requests]
        self.assertIn("resolve_instrument", ops)
        self.assertIn("market_price", ops)

        side = wizard.handle_callback(key, "resolve:pick:0")
        self.assertIn("Select Side:", side.text)
        self.assertIn("Symbol: BTC", side.text)
        self.assertEqual(wizard._state_for(key).symbol, "BTC")

    def test_xau_free_text_shows_resolved_with_price_and_similars(self) -> None:
        desk = LighterShapedStubDesk(resolve_map={"XAU": "XAU"})
        wizard, key = self._open_new_order(desk)
        wizard.handle_callback(key, "symbol:other")
        screen = wizard.handle_text(key, "xau")
        assert screen is not None
        self.assertEqual(screen.state, "instrument_confirm")
        self.assertIn("XAU", screen.text)
        labels = [b["text"] for row in screen.buttons for b in row]
        self.assertTrue(any("XAU" in t for t in labels))
        self.assertTrue(any("2,650" in t for t in labels))
        self.assertTrue(any(t.startswith("Other") for t in labels))

        side = wizard.handle_callback(key, "resolve:pick:0")
        self.assertIn("Symbol: XAU", side.text)

    def test_pick_then_finish_order_sends_new_order_to_agent(self) -> None:
        desk = LighterShapedStubDesk(resolve_map={"ETH": "ETH"})
        wizard, key = self._open_new_order(desk)
        wizard.handle_callback(key, "symbol:ETH")
        wizard.handle_callback(key, "resolve:pick:0")
        wizard.handle_callback(key, "side:buy")
        wizard.handle_text(key, "0.5")
        wizard.handle_text(key, "3400")
        result = wizard.handle_callback(key, "confirm")
        self.assertIn("Order Submitted", result.text)
        new_orders = [r for r in desk.requests if r.get("operation") == "new_order"]
        self.assertEqual(len(new_orders), 1)
        self.assertEqual(new_orders[0]["exchange"], "lighter")
        self.assertEqual(new_orders[0]["account"], "main")
        self.assertEqual(new_orders[0]["symbol"], "ETH")
        self.assertEqual(new_orders[0]["side"], "buy")
        self.assertEqual(new_orders[0]["volume"], "0.5")
        self.assertEqual(new_orders[0]["price"], "3400")

    def test_not_found_with_catalog_similars_as_buttons(self) -> None:
        desk = LighterShapedStubDesk(resolve_map={})  # nothing resolves
        wizard, key = self._open_new_order(desk)
        wizard.handle_callback(key, "symbol:other")
        screen = wizard.handle_text(key, "BT")
        assert screen is not None
        # Ranker should surface BTC as a similar; picker or unresolved with picks.
        labels = [b["text"] for row in screen.buttons for b in row]
        self.assertTrue(
            any("BTC" in t for t in labels) or "not found" in screen.text.lower(),
            msg=f"text={screen.text!r} labels={labels!r}",
        )
        if any("BTC" in t for t in labels):
            # Click similar BTC and continue.
            pick = next(
                b["callback_data"]
                for row in screen.buttons
                for b in row
                if "BTC" in b["text"] and b["callback_data"].startswith("resolve:pick:")
            )
            side = wizard.handle_callback(key, pick)
            self.assertIn("Symbol: BTC", side.text)

    def test_gold_alias_resolves_to_xau_with_price(self) -> None:
        """User types GOLD → Lighter venue XAU with market price button."""
        desk = LighterShapedStubDesk(
            resolve_map={"GOLD": "XAU", "XAU": "XAU"},
            catalog=[
                {"instrument": "BTC", "base": "BTC", "market_type": "perp"},
                {"instrument": "XAU", "base": "XAU", "market_type": "perp"},
                {"instrument": "XAG", "base": "XAG", "market_type": "perp"},
            ],
            prices={"XAU": "4466.74", "XAG": "66.5", "BTC": "95000"},
        )
        wizard, key = self._open_new_order(desk)
        wizard.handle_callback(key, "symbol:other")
        screen = wizard.handle_text(key, "GOLD")
        assert screen is not None
        self.assertEqual(screen.state, "instrument_confirm")
        self.assertIn("Source: GOLD", screen.text)
        self.assertIn("XAU", screen.text)
        labels = [b["text"] for row in screen.buttons for b in row]
        self.assertTrue(any("XAU" in t for t in labels), labels)
        self.assertTrue(any("4,466.74" in t for t in labels), labels)
        self.assertIsNone(wizard._state_for(key).symbol)

        side = wizard.handle_callback(key, "resolve:pick:0")
        self.assertIn("Symbol: XAU", side.text)
        self.assertEqual(wizard._state_for(key).symbol, "XAU")
        self.assertEqual(wizard._state_for(key).order.get("symbol"), "XAU")

        # Finish order — agent receives XAU, not GOLD.
        wizard.handle_callback(key, "side:sell")
        wizard.handle_text(key, "0.1")
        wizard.handle_text(key, "4800")
        result = wizard.handle_callback(key, "confirm")
        self.assertIn("Order Submitted", result.text)
        new_orders = [r for r in desk.requests if r.get("operation") == "new_order"]
        self.assertEqual(new_orders[-1]["symbol"], "XAU")
        self.assertEqual(new_orders[-1]["side"], "sell")

    def test_lighter_alias_keys_gold_maps_to_xau(self) -> None:
        from plugins.trade.agents.x_lighter_agent import _lighter_alias_keys

        keys = _lighter_alias_keys("GOLD")
        self.assertIn("GOLD", keys)
        self.assertIn("XAU", keys)
        self.assertIn("XAU", _lighter_alias_keys("XAUUSD"))

    def test_fibo_rank_gold_finds_xau_without_display_name(self) -> None:
        from plugins.trade.fibo.candidates import rank_candidates, _search_hints

        self.assertIn("XAU", _search_hints("GOLD"))
        ranked = rank_candidates(
            [{"instrument": "XAU", "base": "XAU", "market_type": "perp"}],
            "GOLD",
        )
        self.assertTrue(ranked)
        self.assertEqual(ranked[0].instrument, "XAU")
        self.assertGreaterEqual(ranked[0].score, 95)


if __name__ == "__main__":
    unittest.main()


class TestSystematicCatalogRanking(unittest.TestCase):
    def test_oil_ranks_wti_and_brent_from_catalog(self) -> None:
        from plugins.trade.wizard import TradeWizard

        tw = TradeWizard.__new__(TradeWizard)
        catalog = [
            {"instrument": "WTI-USD.P", "description": "WTIUSD"},
            {"instrument": "BRENT-USD.P", "description": "BRENTUSD"},
            {"instrument": "ETH-USD.P", "description": "ETH"},
            {"instrument": "XAU-USD.P", "description": "XAUUSD"},
        ]
        ranked = tw._rank_catalog_candidates(catalog, "OIL")
        symbols = [r["symbol"] for r in ranked]
        self.assertIn("WTI-USD.P", symbols)
        self.assertIn("BRENT-USD.P", symbols)
        self.assertNotIn("ETH-USD.P", symbols[:2])

    def test_oil_picker_shows_priced_buttons_on_ondo_shaped_desk(self) -> None:
        desk = LighterShapedStubDesk(
            exchanges=["ondoperps"],
            accounts={"ondoperps": ["amiroo"]},
            resolve_map={},  # OIL does not exact-resolve
            catalog=[
                {"instrument": "WTI-USD.P", "description": "WTIUSD", "base": "WTI"},
                {"instrument": "BRENT-USD.P", "description": "BRENTUSD", "base": "BRENT"},
                {"instrument": "ETH-USD.P", "description": "ETH", "base": "ETH"},
            ],
            prices={"WTI-USD.P": "72.5", "BRENT-USD.P": "76.1", "ETH-USD.P": "3400"},
        )
        # Override caps name doesn't matter; desk returns same caps
        wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
        key = ("oil",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:ondoperps")
        wizard.handle_callback(key, "account:amiroo")
        wizard.handle_callback(key, "action:new_order")
        wizard.handle_callback(key, "symbol:other")
        screen = wizard.handle_text(key, "OIL")
        assert screen is not None
        self.assertEqual(screen.state, "instrument_confirm")
        labels = [b["text"] for row in screen.buttons for b in row]
        self.assertTrue(any("WTI" in t for t in labels), labels)
        self.assertTrue(any("BRENT" in t for t in labels), labels)
        self.assertTrue(any(t.startswith("Other") for t in labels))
        # Pick WTI
        pick = next(
            b["callback_data"]
            for row in screen.buttons
            for b in row
            if "WTI" in b["text"] and b["callback_data"].startswith("resolve:pick:")
        )
        side = wizard.handle_callback(key, pick)
        self.assertIn("Symbol: WTI-USD.P", side.text)
