"""Real-dispatch tests: Rise GoldenFibo read operations.

These tests do NOT mock ``x_rise_agent.execute()``. They patch only the
low-level HTTP/helper reads (markets payload, portfolio fetch, reference
price, credentials) and then call the real dispatch:

    x_rise_agent.execute(request)

for each of:

    resolve_instrument
    market_constraints
    market_price
    position_state

and prove they no longer return ``NOT_IMPLEMENTED``, plus that GoldenFibo
adapter preflight can obtain real metadata, mark, and flat position through
the real dispatch. This is the exact gap that blocked the live Rise Golden
Fibo validation at PRE-FLIGHT.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest import mock

from plugins.trade.agents import x_rise_agent as rise
from plugins.trade.golden_fibo.rise_adapter import RiseGoldenFiboAdapter


IDENT = "0x" + "ab" * 20
SIGNER = "0x" + "11" * 32


def _markets_payload(symbol: str = "HYPE", market_id: str = "5",
                     step_size: str = "0.01", step_price: str = "0.001",
                     min_order_size: str = "0.01",
                     last_price: str = "66.857", include_steps: bool = True) -> Dict[str, Any]:
    config: Dict[str, Any] = {"name": f"{symbol}/USDC", "min_order_size": min_order_size}
    if include_steps:
        config["step_size"] = step_size
        config["step_price"] = step_price
    return {
        "markets": [
            {
                "market_id": market_id,
                "display_name": f"{symbol}/USDC",
                "base_asset_symbol": symbol,
                "active": True,
                "config": config,
                "last_price": last_price,
            }
        ]
    }


def _portfolio_payload(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"data": {"positions": positions}}


def _flat_payload() -> Dict[str, Any]:
    return _portfolio_payload([])


def _creds_patch():
    return mock.patch.object(rise, "_lookup_credentials", return_value=(IDENT, SIGNER))


class RealResolveInstrumentDispatchTests(unittest.TestCase):
    def test_execute_resolve_instrument_is_not_not_implemented(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_payload()), \
             _creds_patch():
            r = rise.execute({"operation": "resolve_instrument", "account": "BASED", "symbol": "HYPE"})
        d = r.to_dict()
        self.assertTrue(d["success"])
        self.assertIsNone(d.get("error"))
        inst = d.get("instrument") or {}
        self.assertEqual(inst["symbol"], "HYPE")
        self.assertEqual(inst["price_increment"], "0.001")
        self.assertEqual(inst["size_increment"], "0.01")
        self.assertEqual(inst["minimum_size"], "0.01")
        os_ = d.get("order_state") or {}
        self.assertEqual(os_["price_tick"], "0.001")
        self.assertEqual(os_["size_step"], "0.01")

    def test_execute_resolve_instrument_unknown_symbol_fails(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_payload()), \
             _creds_patch():
            r = rise.execute({"operation": "resolve_instrument", "account": "BASED", "symbol": "NOTREAL"})
        d = r.to_dict()
        self.assertFalse(d["success"])
        self.assertEqual(d["error"]["code"], "INSTRUMENT_NOT_FOUND")


class RealMarketConstraintsDispatchTests(unittest.TestCase):
    def test_execute_market_constraints_not_not_implemented(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_payload()), \
             _creds_patch():
            r = rise.execute({"operation": "market_constraints", "account": "BASED", "symbol": "HYPE"})
        d = r.to_dict()
        self.assertTrue(d["success"])
        self.assertIsNone(d.get("error"))
        os_ = d.get("order_state") or {}
        self.assertEqual(os_["symbol"], "HYPE")
        self.assertEqual(os_["market_id"], "5")
        self.assertEqual(os_["price_tick"], "0.001")
        self.assertEqual(os_["size_step"], "0.01")
        self.assertEqual(os_["min_size"], "0.01")

    def test_execute_market_constraints_unknown_symbol_fails(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_payload()), \
             _creds_patch():
            r = rise.execute({"operation": "market_constraints", "account": "BASED", "symbol": "UNREAL"})
        d = r.to_dict()
        self.assertFalse(d["success"])
        self.assertEqual(d["error"]["code"], "INSTRUMENT_NOT_FOUND")

    def test_execute_market_constraints_missing_mandatory_metadata_fails(self):
        bad = _markets_payload(include_steps=False)
        with mock.patch.object(rise, "_fetch_markets_payload", return_value=bad), \
             _creds_patch():
            r = rise.execute({"operation": "market_constraints", "account": "BASED", "symbol": "HYPE"})
        d = r.to_dict()
        self.assertFalse(d["success"])
        self.assertEqual(d["error"]["code"], "INVALID_MARKET_METADATA")


class RealMarketPriceDispatchTests(unittest.TestCase):
    def test_execute_market_price_same_source_as_immediate(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_payload()), \
             _creds_patch(), \
             mock.patch.object(rise, "_rise_market_price", return_value=Decimal("66.857")):
            r = rise.execute({"operation": "market_price", "account": "BASED", "symbol": "HYPE"})
        d = r.to_dict()
        self.assertTrue(d["success"])
        mp = d.get("market_price") or {}
        self.assertEqual(str(mp["mark_price"]), "66.857")
        self.assertEqual(str(mp["price"]), "66.857")
        self.assertEqual((d.get("order_state") or {}).get("symbol"), "HYPE")

    def test_execute_market_price_unavailable_is_failure(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_payload()), \
             _creds_patch(), \
             mock.patch.object(rise, "_rise_market_price", return_value=Decimal("0")):
            r = rise.execute({"operation": "market_price", "account": "BASED", "symbol": "HYPE"})
        d = r.to_dict()
        self.assertFalse(d["success"])
        self.assertEqual(d["error"]["code"], "MARK_PRICE_UNAVAILABLE")


class RealPositionStateDispatchTests(unittest.TestCase):
    def test_flat(self):
        with _creds_patch(), \
             mock.patch.object(rise, "_fetch_portfolio", return_value=_flat_payload()):
            r = rise.execute({"operation": "position_state", "account": "BASED", "symbol": "HYPE"})
        d = r.to_dict()
        self.assertTrue(d["success"])
        pos = (d.get("positions") or [])[0]
        self.assertEqual(pos["side"], "flat")
        self.assertEqual(pos["size"], "0")

    def test_long(self):
        payload = _portfolio_payload([
            {"market_name": "HYPE/USDC", "market_id": "5", "side": 0,
             "size": "0.2", "avg_entry_price": "66.5"},
        ])
        with _creds_patch(), \
             mock.patch.object(rise, "_fetch_portfolio", return_value=payload):
            r = rise.execute({"operation": "position_state", "account": "BASED", "symbol": "HYPE"})
        d = r.to_dict()
        pos = (d.get("positions") or [])[0]
        self.assertEqual(pos["side"], "long")
        self.assertEqual(pos["size"], "0.2")

    def test_short(self):
        payload = _portfolio_payload([
            {"market_name": "HYPE/USDC", "market_id": "5", "side": 1,
             "size": "-0.3", "avg_entry_price": "67.1"},
        ])
        with _creds_patch(), \
             mock.patch.object(rise, "_fetch_portfolio", return_value=payload):
            r = rise.execute({"operation": "position_state", "account": "BASED", "symbol": "HYPE"})
        d = r.to_dict()
        pos = (d.get("positions") or [])[0]
        self.assertEqual(pos["side"], "short")
        self.assertEqual(pos["size"], "0.3")


class AdapterThroughRealDispatchTests(unittest.TestCase):
    """GoldenFibo adapter -> real x_rise_agent.execute() (no execute mock).

    Patches use STRING paths (``plugins.trade.agents.x_rise_agent.<fn>``) so
    they always target the same ``sys.modules`` object that the adapter's
    ``_get_rise_agent()`` resolves at call time — immune to full-suite module
    re-imports.
    """

    def test_adapter_resolve_constraints_price_via_real_dispatch(self):
        ad = RiseGoldenFiboAdapter()
        with mock.patch("plugins.trade.agents.x_rise_agent._fetch_markets_payload",
                        return_value=_markets_payload()), \
             mock.patch("plugins.trade.agents.x_rise_agent._lookup_credentials",
                        return_value=(IDENT, SIGNER)), \
             mock.patch("plugins.trade.agents.x_rise_agent._rise_market_price",
                        return_value=Decimal("66.857")):
            inst = ad.resolve_instrument("BASED", "HYPE")
            cons = ad.get_venue_constraints("BASED", "HYPE")
            price = ad.market_price("BASED", "HYPE")
        self.assertEqual(inst["market_id"], "5")
        self.assertEqual(cons["min_size"], "0.01")
        self.assertEqual(str(price["mark_price"]), "66.857")
        self.assertEqual(str(price["price"]), "66.857")

    def test_adapter_position_state_flat_via_real_dispatch(self):
        ad = RiseGoldenFiboAdapter()
        with mock.patch("plugins.trade.agents.x_rise_agent._lookup_credentials",
                        return_value=(IDENT, SIGNER)), \
             mock.patch("plugins.trade.agents.x_rise_agent._fetch_portfolio",
                        return_value=_flat_payload()):
            pos = ad.position_state("BASED", "HYPE")
        self.assertIsNone(pos["side"])
        self.assertEqual(pos["size"], "0")


if __name__ == "__main__":
    unittest.main()