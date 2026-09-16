"""Ambiguous instrument picker — shared Telegram/TradeMenu path."""

from __future__ import annotations

import unittest
from unittest import mock

from plugins.trade import instrument_picker as ip
from plugins.trade.canonical import CanonicalInstrument, make_failure, make_success
from plugins.trade.trademenu.service import TradeMenuService


class FakeDesk:
    def __init__(self, *, resolve_map=None, catalog=None, prices=None, caps=None):
        self.resolve_map = resolve_map or {}
        self.catalog = catalog or []
        self.prices = prices or {}
        self.caps = caps or ["resolve_instrument", "list_instruments", "market_price"]
        self.calls = []

    def list_exchanges(self):
        return ["hyperliquid"]

    def list_accounts(self, exchange):
        return ["FLEX"]

    def capabilities(self, exchange):
        return list(self.caps)

    def execute(self, request):
        op = request.get("operation")
        self.calls.append((op, request.get("symbol")))
        if op == "resolve_instrument":
            key = str(request.get("symbol") or "").strip().upper()
            hit = self.resolve_map.get(key)
            if hit == "AMBIGUOUS":
                return make_failure(
                    "resolve_instrument",
                    "hyperliquid",
                    "FLEX",
                    "INSTRUMENT_AMBIGUOUS",
                    "Multiple instruments match this symbol.",
                )
            if isinstance(hit, str):
                return make_success(
                    "resolve_instrument",
                    "hyperliquid",
                    "FLEX",
                    instrument=CanonicalInstrument(
                        requested_symbol=request.get("symbol") or "",
                        symbol=hit,
                        display_name=hit,
                    ),
                )
            return make_failure(
                "resolve_instrument",
                "hyperliquid",
                "FLEX",
                "INSTRUMENT_NOT_FOUND",
                "Instrument not found.",
            )
        if op == "list_instruments":
            return make_success(
                "list_instruments",
                "hyperliquid",
                "FLEX",
                data={"instruments": list(self.catalog)},
            )
        if op == "market_price":
            sym = str(request.get("symbol") or "")
            px = self.prices.get(sym) or self.prices.get(sym.upper())
            if px is None:
                return make_failure("market_price", "hyperliquid", "FLEX", "NO_PRICE", "no")
            from plugins.trade.canonical import CanonicalMarketPrice

            return make_success(
                "market_price",
                "hyperliquid",
                "FLEX",
                market_price=CanonicalMarketPrice(
                    requested_symbol=sym, market="perp", price=str(px)
                ),
            )
        return make_failure(op or "x", "hyperliquid", "FLEX", "NOPE", "nope")


class AmbiguousPickerTests(unittest.TestCase):
    def test_ambiguous_silver_returns_priced_candidates(self) -> None:
        desk = FakeDesk(
            resolve_map={"SILVER": "AMBIGUOUS"},
            catalog=[
                {"symbol": "xyz:Silver", "display_name": "Silver", "base": "SILVER"},
                {"symbol": "flx:SILVER", "display_name": "SILVER", "base": "SILVER"},
                {"symbol": "BTC", "display_name": "BTC", "base": "BTC"},
            ],
            prices={"xyz:Silver": "63.07", "flx:SILVER": "63.11"},
        )
        out = ip.resolve_with_candidates(desk, "hyperliquid", "FLEX", "silver", limit=4)
        self.assertEqual(out["status"], "ambiguous")
        self.assertFalse(out["success"])
        cands = out["candidates"]
        self.assertGreaterEqual(len(cands), 1)
        self.assertLessEqual(len(cands), 4)
        natives = [c["native_symbol"] for c in cands]
        self.assertTrue(any("Silver" in n or "SILVER" in n for n in natives))
        # HIP-3 prefix preserved
        self.assertTrue(any(n.startswith("xyz:") or n.startswith("flx:") for n in natives))
        priced = [c for c in cands if c.get("price") or c.get("last_price")]
        self.assertGreaterEqual(len(priced), 1)

    def test_max_four_candidates(self) -> None:
        catalog = [{"symbol": f"dex{i}:Silver", "base": "SILVER"} for i in range(8)]
        desk = FakeDesk(
            resolve_map={"SILVER": "AMBIGUOUS"},
            catalog=catalog,
            prices={f"dex{i}:Silver": str(60 + i) for i in range(8)},
        )
        out = ip.resolve_with_candidates(desk, "hyperliquid", "FLEX", "silver", limit=4)
        self.assertLessEqual(len(out["candidates"]), 4)

    def test_exact_native_not_ambiguous(self) -> None:
        desk = FakeDesk(
            resolve_map={"XYZ:SILVER": "xyz:SILVER", "XYZ:SP500": "xyz:SP500", "BTC": "BTC"},
            catalog=[{"symbol": "xyz:SILVER"}, {"symbol": "BTC"}],
            prices={"xyz:SILVER": "63", "BTC": "76000"},
        )
        for q, native in (("xyz:Silver", "xyz:SILVER"), ("xyz:SP500", "xyz:SP500"), ("BTC", "BTC")):
            out = ip.resolve_with_candidates(desk, "hyperliquid", "FLEX", q, limit=4)
            self.assertTrue(out["success"], out)
            self.assertEqual(out["status"], "resolved")
            self.assertEqual(out["native_symbol"], native)

    def test_service_exposes_candidates(self) -> None:
        desk = FakeDesk(
            resolve_map={"GOLD": "AMBIGUOUS"},
            catalog=[
                {"symbol": "xyz:Gold", "base": "GOLD", "display_name": "Gold"},
                {"symbol": "abc:GOLD", "base": "GOLD"},
            ],
            prices={"xyz:Gold": "4267.5", "abc:GOLD": "4268"},
        )
        svc = TradeMenuService(desk=desk)  # type: ignore[arg-type]
        r = svc.resolve_instrument("hyperliquid", "FLEX", "gold")
        self.assertFalse(r["success"])
        self.assertEqual(r.get("status"), "ambiguous")
        self.assertTrue(r.get("candidates"))
        self.assertLessEqual(len(r["candidates"]), 4)

    def test_quote_failure_keeps_candidate(self) -> None:
        desk = FakeDesk(
            resolve_map={"SILVER": "AMBIGUOUS"},
            catalog=[{"symbol": "xyz:Silver", "base": "SILVER"}],
            prices={},  # no prices
        )
        out = ip.resolve_with_candidates(desk, "hyperliquid", "FLEX", "silver", limit=4)
        self.assertEqual(len(out["candidates"]), 1)
        self.assertEqual(out["candidates"][0]["native_symbol"], "xyz:Silver")
        # price may be missing — still selectable
        self.assertNotIn("must", str(out["candidates"][0]).lower())

    def test_app_js_candidate_click_uses_activate(self) -> None:
        from pathlib import Path

        src = Path("/root/kam/plugins/trade/trademenu/static/app.js").read_text(encoding="utf-8")
        self.assertIn("showCandidatePicker", src)
        self.assertIn("activateInstrument(native, native)", src)
        self.assertIn("Multiple instruments match", src)
        self.assertIn("clearCandidatePicker", src)
        # Resolve before candles so ambiguous never hits chart error path first.
        i_res = src.find("const native = await resolveSymbol()")
        i_candles = src.find("/api/candles?")
        self.assertGreater(i_res, 0)
        self.assertGreater(i_candles, i_res)


if __name__ == "__main__":
    unittest.main()
