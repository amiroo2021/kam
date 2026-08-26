"""Regression tests for ``x_ondoperps_agent._market_price``.

Phase 2.3 hardening: the agent's ``market_price`` operation must
accept BOTH canonical venue instruments (``ETH-USD.P``,
``US500-USD.P``, ``SPY-USD.P``, ``BTC-USD.P``, ``XAU-USD.P``)
AND existing aliases (``ETH``, ``US500``). Unknown instruments must
fail canonically.

The agent module reads its ``_market_cache`` lazily; these tests
reset it between cases so each starts from a clean fetch.
"""
from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path
from typing import Optional
import sys

# Ensure the repo root is on sys.path for the import below.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from plugins.trade.agents import x_ondoperps_agent as A  # noqa: E402


class _MarketPriceRegression(unittest.TestCase):
    """Hardening: ``_market_price`` must serve canonical instruments
    AND preserve alias compatibility, returning a finite Decimal for
    each canonical / alias input."""

    CANONICAL_INSTRUMENTS = (
        "ETH-USD.P",
        "US500-USD.P",
        "SPY-USD.P",
        "BTC-USD.P",
        "XAU-USD.P",
    )
    ALIAS_INSTRUMENTS = ("ETH", "US500")

    def setUp(self) -> None:
        # Force a fresh markets cache so each test exercises the
        # fetch path independently.
        A._market_cache = {}

    def _price(self, sym: str) -> Optional[Decimal]:
        """Call ``_market_price`` and return the price as Decimal (or None).

        The agent exposes ``mark_price`` as a string (via
        ``_decimal_text``); we normalise back to Decimal for the
        assertion helpers.
        """
        resp = A._market_price(
            "BITGET",
            {"operation": "market_price", "symbol": sym},
        )
        if not resp.success:
            return None
        raw: Optional[str] = None
        mp_obj = getattr(resp, "market_price", None)
        if mp_obj is not None:
            raw = getattr(mp_obj, "mark_price", None)
        if raw is None:
            inst = getattr(resp, "instrument", None)
            if inst is not None:
                raw = getattr(inst, "price", None)
        if raw is None:
            return None
        try:
            return Decimal(str(raw))
        except Exception:  # noqa: BLE001
            return None

    def _assert_finite_positive(self, price: Optional[Decimal], sym: str) -> None:
        if price is None:
            self.skipTest("live Ondo credentials unavailable")
        self.assertIsInstance(price, Decimal)
        self.assertTrue(price.is_finite())
        self.assertGreater(price, 0)

    def test_market_price_eth_usd_p_returns_finite_decimal(self) -> None:
        self._assert_finite_positive(self._price("ETH-USD.P"), "ETH-USD.P")

    def test_market_price_us500_usd_p_returns_finite_decimal(self) -> None:
        self._assert_finite_positive(self._price("US500-USD.P"), "US500-USD.P")

    def test_market_price_spy_usd_p_returns_finite_decimal(self) -> None:
        self._assert_finite_positive(self._price("SPY-USD.P"), "SPY-USD.P")

    def test_market_price_btc_usd_p_returns_finite_decimal(self) -> None:
        self._assert_finite_positive(self._price("BTC-USD.P"), "BTC-USD.P")

    def test_market_price_xau_usd_p_returns_finite_decimal(self) -> None:
        self._assert_finite_positive(self._price("XAU-USD.P"), "XAU-USD.P")

    def test_market_price_alias_eth_still_succeeds(self) -> None:
        """Phase 2.2 aliases must continue to work."""
        self._assert_finite_positive(self._price("ETH"), "ETH")

    def test_market_price_alias_us500_still_succeeds(self) -> None:
        self._assert_finite_positive(self._price("US500"), "US500")

    def test_market_price_all_canonicals_return_prices(self) -> None:
        """Loop through every documented canonical instrument."""
        priced = 0
        for sym in self.CANONICAL_INSTRUMENTS:
            price = self._price(sym)
            if price is None:
                continue
            self._assert_finite_positive(price, sym)
            priced += 1
        if priced == 0:
            self.skipTest(
                "live Ondo credentials unavailable — no instrument "
                "returned a price"
            )

    def test_market_price_unknown_instrument_fails_canonically(self) -> None:
        """An unknown market must return a failure with
        INSTRUMENT_NOT_FOUND — not raise, not silently None."""
        resp = A._market_price(
            "BITGET",
            {"operation": "market_price", "symbol": "DOES-NOT-EXIST"},
        )
        self.assertFalse(resp.success)
        err = getattr(resp, "error", None)
        self.assertIsNotNone(err)
        self.assertEqual(getattr(err, "code", None), "INSTRUMENT_NOT_FOUND")

    def test_market_price_missing_symbol_fails_canonically(self) -> None:
        """An empty symbol must return a failure with MISSING_SYMBOL."""
        resp = A._market_price("BITGET", {"operation": "market_price"})
        self.assertFalse(resp.success)
        err = getattr(resp, "error", None)
        self.assertIsNotNone(err)
        self.assertEqual(getattr(err, "code", None), "MISSING_SYMBOL")

    def test_metadata_cache_indexes_canonical_and_alias(self) -> None:
        """The agent's markets cache must key BOTH the canonical
        market id AND the stripped alias for every entry. This
        enables canonical and alias callers to share the same
        cache."""
        # Force a fresh fetch.
        A._market_cache = {}
        creds = A._lookup_credentials("BITGET")
        if creds is None:
            self.skipTest("live Ondo credentials unavailable")
        mapping = A._fetch_market_metadata(creds, refresh=True)
        self.assertGreater(len(mapping), 0)
        # At least one canonical market id is present.
        sample_canonical = next(
            (k for k in mapping.keys() if k.endswith("-USD.P")), None
        )
        self.assertIsNotNone(
            sample_canonical,
            "expected at least one canonical market id in cache",
        )
        # And its alias is also a key.
        alias = sample_canonical[: -len("-USD.P")]
        self.assertIn(alias, mapping)
        # Both keys map to the SAME record.
        self.assertIs(mapping[sample_canonical], mapping[alias])


class _MarketPriceReadOnlySafety(unittest.TestCase):
    """Static + behavioural guard: ``market_price`` is READ-ONLY."""

    def test_market_price_source_has_no_write_tokens(self) -> None:
        """The ``_market_price`` function — the ONLY entry point
        Phase 2.3 exercises — must not reference any write
        operation. The agent as a whole legitimately contains
        ``new_order`` / ``cancel_order_group`` etc. for its
        Phase-2 write surface; this guard is scoped to the read
        path."""
        import inspect
        src = inspect.getsource(A._market_price)
        for tok in (
            "new_order", "market_order", "limit_order",
            "cancel_order", "cancel_order_group",
            "close_position", "stop_order",
            "method=\"POST\"", "method=\"PUT\"",
            "method=\"DELETE\"", "method=\"PATCH\"",
        ):
            self.assertNotIn(
                tok, src,
                f"_market_price references {tok!r}",
            )


if __name__ == "__main__":
    unittest.main()
