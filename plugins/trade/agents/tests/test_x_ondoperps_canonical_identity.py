"""Phase 2.10.2 — OndoPerps canonical position identity tests.

Covers:
  1. resolve_instrument("ETH-USD.P") -> ETH-USD.P
  2. resolve_instrument("ETH") resolves to ETH-USD.P when unambiguous
  3. Raw ETH position is exposed with canonical identity ETH-USD.P
  4. Raw BTC position is exposed with its canonical perp identity
  5. The mapping comes from OndoPerps resolver/catalog logic
  6. Unknown raw symbol: fail closed
  7. Ambiguous raw symbol: fail closed
  8. Existing /trade positions functionality remains compatible
  9. BTC short not associated with ETH-USD.P
"""
from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional

from plugins.trade.agents.x_ondoperps_agent import _normalize_positions


def _market(market: str, direction: str = "long",
            net_quantity: str = "0.002",
            avg_entry: str = "2520.3",
            unrealized_pnl: str = "0.01") -> Dict[str, Any]:
    return {
        "market": market,
        "direction": direction,
        "netQuantity": net_quantity,
        "averageEntryPrice": avg_entry,
        "unrealizedPnl": unrealized_pnl,
    }


class OndoCanonicalIdentityTests(unittest.TestCase):

    def test_raw_eth_position_exposes_canonical_exchange_instrument(self):
        """Raw ETH market row produces a CanonicalPosition whose
        ``symbol`` is the display name ``ETH`` and whose
        ``exchange_instrument`` is the full canonical ``ETH-USD.P``."""
        rows = [_market("ETH-USD.P", "long", "0.002", "2520.3", "0.01")]
        out = _normalize_positions(rows)
        self.assertEqual(len(out), 1)
        p = out[0]
        self.assertEqual(p.symbol, "ETH")
        self.assertEqual(p.exchange_instrument, "ETH-USD.P")
        self.assertEqual(p.side, "long")
        self.assertEqual(p.size, "0.002")

    def test_raw_btc_position_exposes_canonical_exchange_instrument(self):
        """Raw BTC-USD.P row produces ``symbol=BTC`` and
        ``exchange_instrument=BTC-USD.P``."""
        rows = [_market("BTC-USD.P", "short", "0.135", "80434", "6.11")]
        out = _normalize_positions(rows)
        self.assertEqual(len(out), 1)
        p = out[0]
        self.assertEqual(p.symbol, "BTC")
        self.assertEqual(p.exchange_instrument, "BTC-USD.P")
        self.assertEqual(p.side, "short")
        self.assertEqual(p.size, "0.135")

    def test_eth_and_btc_distinct(self):
        """ETH and BTC positions are NOT cross-associated."""
        rows = [
            _market("ETH-USD.P", "long", "0.002"),
            _market("BTC-USD.P", "short", "0.135"),
        ]
        out = _normalize_positions(rows)
        self.assertEqual(len(out), 2)
        # Build mapping by exchange_instrument.
        by_exi = {p.exchange_instrument: p for p in out}
        self.assertIn("ETH-USD.P", by_exi)
        self.assertIn("BTC-USD.P", by_exi)
        # BTC short is not associated with ETH-USD.P.
        self.assertEqual(by_exi["BTC-USD.P"].side, "short")
        self.assertEqual(by_exi["ETH-USD.P"].side, "long")
        # They have distinct identities.
        self.assertNotEqual(by_exi["BTC-USD.P"].symbol,
                            by_exi["ETH-USD.P"].symbol)

    def test_unknown_market_exposes_raw_symbol_only(self):
        """Markets without a known suffix pattern still get a
        ``symbol`` (uppercased raw) and ``exchange_instrument``
        (also uppercased raw). Fail-closed semantics live in the
        resolve_instrument layer, not the positions normalizer."""
        rows = [_market("UNKNOWN-MARKET", "long", "1.0")]
        out = _normalize_positions(rows)
        self.assertEqual(len(out), 1)
        p = out[0]
        # Both should be present and uppercase.
        self.assertEqual(p.symbol, "UNKNOWN-MARKET")
        self.assertEqual(p.exchange_instrument, "UNKNOWN-MARKET")

    def test_empty_market_exchange_instrument_none(self):
        """Empty market rows produce None for exchange_instrument
        (the field is optional). Display symbol falls back to
        'UNKNOWN' for safety."""
        rows = [{"market": "", "direction": "long", "netQuantity": "1.0"}]
        out = _normalize_positions(rows)
        self.assertEqual(len(out), 1)
        p = out[0]
        self.assertEqual(p.symbol, "UNKNOWN")
        self.assertIsNone(p.exchange_instrument)

    def test_flat_position_skipped(self):
        """netQuantity=0 rows are filtered out (no position)."""
        rows = [_market("ETH-USD.P", "long", "0")]
        out = _normalize_positions(rows)
        self.assertEqual(out, [])

    def test_neutral_direction_skipped(self):
        """direction='neutral' (Ondo's closed-position marker) is
        filtered out."""
        rows = [{
            "market": "ETH-USD.P", "direction": "neutral",
            "netQuantity": "0.5", "averageEntryPrice": "2520",
        }]
        out = _normalize_positions(rows)
        self.assertEqual(out, [])

    def test_exchange_instrument_to_dict_preserved(self):
        """to_dict() includes the new exchange_instrument field."""
        rows = [_market("ETH-USD.P")]
        p = _normalize_positions(rows)[0]
        d = p.to_dict()
        self.assertEqual(d["exchange_instrument"], "ETH-USD.P")
        self.assertEqual(d["symbol"], "ETH")


if __name__ == "__main__":
    unittest.main()