"""HIP-3 / dex-prefixed Hyperliquid symbol + candle tests."""

from __future__ import annotations

import unittest
from unittest import mock

from plugins.trade.trademenu import marketdata as md
from plugins.trade.trademenu.service import TradeMenuService
from plugins.trade.canonical import CanonicalInstrument, make_success


class Hip3CandleTests(unittest.TestCase):
    def test_fetch_hl_preserves_dex_prefix(self) -> None:
        captured = {}

        def fake_http(url, method="GET", body=None, timeout=20):
            captured["body"] = body
            return [
                {"t": 1_700_000_000_000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10"},
            ]

        with mock.patch.object(md, "_http_json", side_effect=fake_http):
            rows = md.fetch_hyperliquid_candles("xyz:SP500", "15m", limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(captured["body"]["req"]["coin"], "xyz:SP500")

    def test_fetch_hl_bare_btc_unchanged(self) -> None:
        captured = {}

        def fake_http(url, method="GET", body=None, timeout=20):
            captured["body"] = body
            return [{"t": 1_700_000_000_000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10"}]

        with mock.patch.object(md, "_http_json", side_effect=fake_http):
            md.fetch_hyperliquid_candles("BTC", "15m", limit=10)
        self.assertEqual(captured["body"]["req"]["coin"], "BTC")

    def test_fetch_candles_structured_error_not_raw_500(self) -> None:
        import urllib.error

        def boom(*a, **k):
            raise urllib.error.HTTPError("https://x", 500, "Internal Server Error", hdrs=None, fp=None)

        with mock.patch.object(md, "_http_json", side_effect=boom):
            out = md.fetch_candles("hyperliquid", "FLEX", "xyz:SP500", "15m", limit=10)
        self.assertFalse(out["success"])
        self.assertEqual(out["error"]["code"], "CANDLES_UNAVAILABLE")
        self.assertNotIn("HTTP Error 500", out["error"]["message"])


class Hip3ResolveServiceTests(unittest.TestCase):
    def test_resolve_sp500_variants_to_native(self) -> None:
        class Desk:
            def list_exchanges(self):
                return ["hyperliquid"]

            def list_accounts(self, exchange):
                return ["FLEX"]

            def capabilities(self, exchange):
                return ["resolve_instrument"]

            def execute(self, request):
                sym = str(request.get("symbol") or "")
                key = sym.upper().replace("-", "")
                if key in {"SP500", "XYZ:SP500"} or key.endswith("SP500"):
                    return make_success(
                        "resolve_instrument",
                        "hyperliquid",
                        "FLEX",
                        instrument=CanonicalInstrument(
                            requested_symbol=sym,
                            symbol="xyz:SP500",
                            display_name="xyz:SP500",
                        ),
                    )
                if "BTC" in key:
                    return make_success(
                        "resolve_instrument",
                        "hyperliquid",
                        "FLEX",
                        instrument=CanonicalInstrument(requested_symbol=sym, symbol="BTC", display_name="BTC"),
                    )
                from plugins.trade.canonical import make_failure

                return make_failure("resolve_instrument", "hyperliquid", "FLEX", "INSTRUMENT_NOT_FOUND", "nope")

        svc = TradeMenuService(desk=Desk())  # type: ignore[arg-type]
        for sym in ("sp500", "SP500", "xyz:SP500"):
            r = svc.resolve_instrument("hyperliquid", "FLEX", sym)
            self.assertTrue(r["success"], r)
            self.assertEqual(r["native_symbol"], "xyz:SP500")
        btc = svc.resolve_instrument("hyperliquid", "FLEX", "BTCUSD")
        self.assertEqual(btc["native_symbol"], "BTC")

    def test_candles_path_keeps_native_prefix(self) -> None:
        class Desk:
            def list_exchanges(self):
                return ["hyperliquid"]

            def list_accounts(self, exchange):
                return ["FLEX"]

            def capabilities(self, exchange):
                return ["resolve_instrument"]

            def execute(self, request):
                return make_success(
                    "resolve_instrument",
                    "hyperliquid",
                    "FLEX",
                    instrument=CanonicalInstrument(
                        requested_symbol=str(request.get("symbol") or ""),
                        symbol="xyz:SP500",
                        display_name="xyz:SP500",
                    ),
                )

        svc = TradeMenuService(desk=Desk())  # type: ignore[arg-type]
        resolved = svc.resolve_instrument("hyperliquid", "FLEX", "SP500")
        native = resolved["native_symbol"]
        with mock.patch.object(md, "fetch_hyperliquid_candles", return_value=[
            {"time": 1, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1}
        ]) as m:
            out = md.fetch_candles("hyperliquid", "FLEX", native, "15m", 50)
        self.assertTrue(out["success"])
        m.assert_called()
        self.assertEqual(m.call_args[0][0], "xyz:SP500")


if __name__ == "__main__":
    unittest.main()
