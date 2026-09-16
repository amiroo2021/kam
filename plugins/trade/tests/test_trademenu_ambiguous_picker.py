"""Ambiguous instrument picker — shared Telegram/TradeMenu path."""

from __future__ import annotations

import unittest
from pathlib import Path
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


# Live-shaped silver catalog (5 HIP-3/native routes).
_SILVER_FIVE = [
    {"symbol": "flx:SILVER", "display_name": "SILVER", "base": "SILVER"},
    {"symbol": "hyna:SILVER", "display_name": "SILVER", "base": "SILVER"},
    {"symbol": "km:SILVER", "display_name": "SILVER", "base": "SILVER"},
    {"symbol": "mkts:SILVER", "display_name": "SILVER", "base": "SILVER"},
    {"symbol": "xyz:SILVER", "display_name": "SILVER", "base": "SILVER"},
]
_SILVER_PRICES = {
    "flx:SILVER": "65.65",
    "hyna:SILVER": "85.97",
    "km:SILVER": "70.451",
    "mkts:SILVER": "70.451",
    "xyz:SILVER": "63.124",
}


class AmbiguousPickerTests(unittest.TestCase):
    def test_limits_are_identical_telegram_trademenu(self) -> None:
        self.assertEqual(ip.INSTRUMENT_PICK_MAX, 5)
        self.assertEqual(ip.INSTRUMENT_PICK_MAX_TELEGRAM, ip.INSTRUMENT_PICK_MAX)
        self.assertEqual(ip.INSTRUMENT_PICK_MAX_TRADEMENU, ip.INSTRUMENT_PICK_MAX)

    def test_silver_five_candidates_includes_xyz(self) -> None:
        """Regression: TradeMenu must not drop xyz:SILVER via a private [:4] cap."""
        desk = FakeDesk(
            resolve_map={"SILVER": "AMBIGUOUS"},
            catalog=list(_SILVER_FIVE),
            prices=dict(_SILVER_PRICES),
        )
        # Shared builder (Telegram)
        tw = ip.build_priced_candidates(
            desk, "hyperliquid", "FLEX", "silver", limit=ip.INSTRUMENT_PICK_MAX
        )
        # TradeMenu one-shot
        tm = ip.resolve_with_candidates(
            desk, "hyperliquid", "FLEX", "silver", limit=ip.INSTRUMENT_PICK_MAX
        )
        tw_n = [c["symbol"] for c in tw]
        tm_n = [c["native_symbol"] for c in tm["candidates"]]
        self.assertEqual(tw_n, tm_n)
        self.assertEqual(len(tm_n), 5)
        self.assertIn("xyz:SILVER", tm_n)
        self.assertIn("flx:SILVER", tm_n)
        self.assertIn("hyna:SILVER", tm_n)
        self.assertIn("km:SILVER", tm_n)
        self.assertIn("mkts:SILVER", tm_n)
        # Prices match across surfaces
        tw_p = {c["symbol"]: str(c.get("price")) for c in tw}
        tm_p = {
            c["native_symbol"]: str(c.get("price") or c.get("last_price"))
            for c in tm["candidates"]
        }
        self.assertEqual(tw_p, tm_p)
        self.assertEqual(tm_p["xyz:SILVER"], "63.124")

    def test_service_returns_all_five_silver(self) -> None:
        desk = FakeDesk(
            resolve_map={"SILVER": "AMBIGUOUS"},
            catalog=list(_SILVER_FIVE),
            prices=dict(_SILVER_PRICES),
        )
        svc = TradeMenuService(desk=desk)  # type: ignore[arg-type]
        r = svc.resolve_instrument("hyperliquid", "FLEX", "silver")
        self.assertFalse(r["success"])
        self.assertEqual(r.get("status"), "ambiguous")
        natives = [c["native_symbol"] for c in r["candidates"]]
        self.assertEqual(len(natives), 5)
        self.assertIn("xyz:SILVER", natives)

    def test_shared_cap_not_four(self) -> None:
        catalog = [{"symbol": f"dex{i}:Silver", "base": "SILVER"} for i in range(8)]
        desk = FakeDesk(
            resolve_map={"SILVER": "AMBIGUOUS"},
            catalog=catalog,
            prices={f"dex{i}:Silver": str(60 + i) for i in range(8)},
        )
        out = ip.resolve_with_candidates(
            desk, "hyperliquid", "FLEX", "silver", limit=ip.INSTRUMENT_PICK_MAX
        )
        # Shared global safety cap is 5 (Telegram), never a TradeMenu-only 4.
        self.assertEqual(len(out["candidates"]), 5)
        self.assertNotEqual(len(out["candidates"]), 4)

    def test_exact_native_not_ambiguous(self) -> None:
        desk = FakeDesk(
            resolve_map={"XYZ:SILVER": "xyz:SILVER", "XYZ:SP500": "xyz:SP500", "BTC": "BTC"},
            catalog=[{"symbol": "xyz:SILVER"}, {"symbol": "BTC"}],
            prices={"xyz:SILVER": "63", "BTC": "76000"},
        )
        for q, native in (("xyz:Silver", "xyz:SILVER"), ("xyz:SP500", "xyz:SP500"), ("BTC", "BTC")):
            out = ip.resolve_with_candidates(desk, "hyperliquid", "FLEX", q)
            self.assertTrue(out["success"], out)
            self.assertEqual(out["status"], "resolved")
            self.assertEqual(out["native_symbol"], native)

    def test_quote_failure_keeps_candidate(self) -> None:
        desk = FakeDesk(
            resolve_map={"SILVER": "AMBIGUOUS"},
            catalog=[{"symbol": "xyz:Silver", "base": "SILVER"}],
            prices={},
        )
        out = ip.resolve_with_candidates(desk, "hyperliquid", "FLEX", "silver")
        self.assertEqual(len(out["candidates"]), 1)
        self.assertEqual(out["candidates"][0]["native_symbol"], "xyz:Silver")

    def test_frontend_does_not_slice_to_four(self) -> None:
        src = Path("/root/kam/plugins/trade/trademenu/static/app.js").read_text(encoding="utf-8")
        self.assertNotIn("slice(0, 4)", src)
        self.assertNotIn("slice(0,4)", src)
        self.assertIn("activateInstrument(native, native)", src)
        self.assertIn("showCandidatePicker", src)
        # Full list path (no TradeMenu-only truncation)
        self.assertIn("candidates.slice()", src)

    def test_click_exact_native_skips_second_ambiguous_pass(self) -> None:
        """Selecting xyz:SILVER must resolve uniquely — not re-run bare 'silver'."""
        desk = FakeDesk(
            resolve_map={
                "SILVER": "AMBIGUOUS",
                "XYZ:SILVER": "xyz:SILVER",
            },
            catalog=list(_SILVER_FIVE),
            prices=dict(_SILVER_PRICES),
        )
        svc = TradeMenuService(desk=desk)  # type: ignore[arg-type]
        ambiguous = svc.resolve_instrument("hyperliquid", "FLEX", "silver")
        self.assertEqual(ambiguous.get("status"), "ambiguous")
        self.assertIn("xyz:SILVER", [c["native_symbol"] for c in ambiguous["candidates"]])

        picked = svc.resolve_instrument("hyperliquid", "FLEX", "xyz:SILVER")
        self.assertTrue(picked["success"])
        self.assertEqual(picked["native_symbol"], "xyz:SILVER")
        self.assertEqual(picked.get("status"), "resolved")
        # Exact pick must not be treated as ambiguous again.
        self.assertNotEqual(picked.get("status"), "ambiguous")


if __name__ == "__main__":
    unittest.main()
