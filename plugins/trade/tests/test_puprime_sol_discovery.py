"""PUPrime SOLUSD discovery + ranking regressions."""
from __future__ import annotations

import unittest
from unittest import mock

from plugins.trade.instrument_picker import (
    build_priced_candidates,
    list_instruments,
    rank_catalog_candidates,
)


class RankSolTests(unittest.TestCase):
    def test_sol_ranks_solusd_above_so_and_usousd(self) -> None:
        catalog = [
            {"symbol": "USOUSD.pc", "path": r"Oil.pc\USOUSD.pc"},
            {"symbol": "SO", "path": r"Equity-US\SO"},
            {"symbol": "SOLJPY", "path": r"Cryptos\SOLJPY"},
            {"symbol": "SOLUSD", "path": r"Cryptos\SOLUSD"},
            {"symbol": "ETHUSD", "path": r"Cryptos\ETHUSD"},
        ]
        ranked = rank_catalog_candidates(catalog, "SOL")
        self.assertTrue(ranked)
        top = [str(r.get("symbol") or "").upper() for r in ranked]
        self.assertEqual(top[0], "SOLUSD")
        self.assertIn("SOLUSD", top)
        # SOLJPY may appear but after SOLUSD
        if "SOLJPY" in top:
            self.assertLess(top.index("SOLUSD"), top.index("SOLJPY"))
        # SO must not outrank SOLUSD
        if "SO" in top:
            self.assertLess(top.index("SOLUSD"), top.index("SO"))
        if "USOUSD.PC" in top:
            self.assertLess(top.index("SOLUSD"), top.index("USOUSD.PC"))

    def test_list_instruments_forwards_query(self) -> None:
        desk = mock.Mock()

        class Resp:
            success = True
            data = {
                "instruments": [
                    {"symbol": "SOLUSD", "native_symbol": "SOLUSD"},
                    {"name": "SOLJPY"},
                ]
            }

        desk.execute.return_value = Resp()
        rows = list_instruments(desk, "mt5linux", "PUPRIME33849951", query="SOL")
        self.assertEqual({r["symbol"] for r in rows}, {"SOLUSD", "SOLJPY"})
        args = desk.execute.call_args[0][0]
        self.assertEqual(args["query"], "SOL")
        self.assertEqual(args["account"], "PUPRIME33849951")
        self.assertEqual(args["exchange"], "mt5linux")

    def test_build_priced_candidates_uses_query_catalog(self) -> None:
        desk = mock.Mock()
        desk.capabilities.return_value = ["list_instruments", "market_price"]

        def _exec(req):
            op = req.get("operation")
            if op == "list_instruments":
                self.assertEqual(req.get("query"), "SOL")
                self.assertEqual(req.get("account"), "PUPRIME33849951")

                class R:
                    success = True
                    data = {
                        "instruments": [
                            {"symbol": "SO"},
                            {"symbol": "USOUSD.pc"},
                            {"symbol": "SOLUSD", "path": r"Cryptos\SOLUSD"},
                            {"symbol": "SOLJPY"},
                        ]
                    }

                return R()

            class MP:
                success = True
                market_price = type("P", (), {"price": "94.5", "mark_price": "94.5", "last_external_price": None})()
                data = {"price": "94.5"}

            return MP()

        desk.execute.side_effect = _exec
        cands = build_priced_candidates(
            desk,
            "mt5linux",
            "PUPRIME33849951",
            "SOL",
            limit=5,
            capabilities=["list_instruments", "market_price"],
        )
        self.assertTrue(cands)
        self.assertEqual(str(cands[0].get("symbol") or "").upper(), "SOLUSD")


class AgentListQueryTests(unittest.TestCase):
    def test_symbols_query_sorts_prefix_first(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        # Remote ranking unit-tested via pure python of the sort key logic.
        rows = [
            {"name": "USOUSD.pc"},
            {"name": "SO"},
            {"name": "SOLJPY"},
            {"name": "SOLUSD"},
            {"name": "BSOLUSD"},
        ]
        q = "SOL"

        def _rank_key(row):
            up = str(row.get("name") or "").upper()
            compact = up.replace(".", "").replace("-", "").replace("_", "").replace(" ", "")
            prefs = ("USDT", "USDC", "USD", "UST")
            if up == q or compact == q:
                return (0, 0, 0, up)
            if up.startswith(q) or compact.startswith(q):
                rest = up[len(q):] if up.startswith(q) else compact[len(q):]
                qboost = 50
                for i, suf in enumerate(prefs):
                    if rest == suf:
                        qboost = i
                        break
                return (0, 1, qboost, up)
            pos = up.find(q) if q in up else compact.find(q)
            return (0, 2, pos if pos >= 0 else 999, len(up), up)

        filtered = []
        for r in rows:
            up = r["name"].upper()
            compact = up.replace(".", "")
            if q in up or up.startswith(q) or q in compact or compact.startswith(q):
                filtered.append(r)
        filtered.sort(key=_rank_key)
        names = [r["name"] for r in filtered]
        # With USD quote preference, SOLUSD leads prefix tier over SOLJPY.
        self.assertEqual(names[0], "SOLUSD")
        self.assertIn("SOLJPY", names)
        self.assertTrue(names.index("SOLUSD") < names.index("BSOLUSD"))


if __name__ == "__main__":
    unittest.main()
