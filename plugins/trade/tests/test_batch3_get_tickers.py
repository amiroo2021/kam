"""RED-first offline tests for canonical ``get_tickers`` on Batch 3
exchanges: Lighter and Raydium.

Coverage mirrors the Batch 1 / Batch 2 taxonomy:

A. capabilities advertises ``get_tickers`` (additive only).
B. ``get_tickers`` is read-only (no write helper invoked, no per-symbol
   HTTP fan-out).
C. complete expected catalog is returned (catalog rows preserved even
   when dynamic data is absent).
D. ``symbols``/``query`` filtering narrows the result.
E. ``mark_price`` / oracle / funding / volume map from the same bulk
   payload the existing read path uses (no extra fetch per symbol).
F. unavailable fields stay ``None`` rather than ``0``.
G. ``symbol`` / ``native_symbol`` / ``display_symbol`` / ``display_name``
   / ``base`` / ``quote`` identity is preserved.
H. ``market_type`` preserved (and Lighter spot/perp disambiguated).
I. existing cache/bulk source is reused.
J. no per-symbol fan-out.
K. ``list_instruments`` still works.
L. ``market_price`` still works.
M. existing trading/write capabilities remain unchanged.
N. partial/missing dynamic price data does not remove the static
    instrument row.
O. zero-like unavailable values map to ``None``.

Additional Lighter-specific checks:
10. spot/perp disambiguation is preserved per-entry.
11. spot/perp collision handling (same display/base symbol does not
    collapse the two into one row).

Additional Raydium-specific checks:
12. native Orderly symbol mapping (PERP_X_USDC) is preserved and
    ``display_symbol_name`` is propagated.
13. catalog join key is the venue-native symbol, not a normalized form.
"""

from __future__ import annotations

import importlib
import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from plugins.trade.canonical import (
    CanonicalResponse,
    CanonicalTickersBatch,
)


WRITE_HELPERS = (
    "_new_order",
    "_ladder",
    "_execute_cancel_order",
    "_cancel_order_group",
    "_set_position_trigger",
    "_set_protection",
    "_close_position",
    "_batch_place",
    "_batch_cancel",
    "_cancel_all",
    "_apex_new_order",
    "_execute_new_order",
    "_apex_set_tp",
    "_apex_set_sl",
    "_apex_cancel_order",
    "_apex_close_position",
    "_submit_order",
    "_execute_ladder",
    "_execute_set_tp_sl",
    "_execute_cancel_order_group",
    "_positions_orders",
    "_balance",
)


def _patch_all_writes(agent_module: Any) -> List[Tuple[str, Any]]:
    """Wrap every write-helper on the module so a call raises."""
    originals: List[Tuple[str, Any]] = []
    for name in WRITE_HELPERS:
        if not hasattr(agent_module, name):
            continue
        original = getattr(agent_module, name)
        if isinstance(original, type) or not callable(original):
            continue

        def make_wrapper(fn: Any, helper_name: str):
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                raise AssertionError(
                    f"get_tickers must not call write helper {helper_name}"
                )
            return wrapper

        setattr(agent_module, name, make_wrapper(original, name))
        originals.append((name, original))
    return originals


# ---------------------------------------------------------------------------
# Lighter
# ---------------------------------------------------------------------------


def _lighter_perp_entry(
    symbol: str,
    *,
    market_id: int,
    mark_price: str = "100",
    last_trade_price: str = "99",
    index_price: Optional[str] = "100",
    daily_quote_volume: str = "500000",
    daily_base_volume: str = "5000",
    daily_price_change: str = "1.5",
    open_interest: str = "100",
    min_base_amount: str = "0.01",
    min_quote_amount: str = "10",
    price_decimals: int = 2,
    size_decimals: int = 3,
    status: str = "active",
    is_frozen: bool = False,
) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "market_id": market_id,
        "market_type": "perp",
        "status": status,
        "is_frozen": is_frozen,
        "mark_price": mark_price,
        "index_price": index_price,
        "last_trade_price": last_trade_price,
        "daily_quote_token_volume": daily_quote_volume,
        "daily_base_token_volume": daily_base_volume,
        "daily_price_change": daily_price_change,
        "open_interest": open_interest,
        "min_base_amount": min_base_amount,
        "min_quote_amount": min_quote_amount,
        "price_decimals": price_decimals,
        "size_decimals": size_decimals,
    }


def _lighter_spot_entry(
    symbol: str,
    *,
    market_id: int,
    last_trade_price: str = "99",
    daily_quote_volume: str = "100000",
    daily_base_volume: str = "1000",
    daily_price_change: str = "0.5",
    min_base_amount: str = "1",
    min_quote_amount: str = "10",
    price_decimals: int = 4,
    size_decimals: int = 2,
) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "market_id": market_id,
        "market_type": "spot",
        "status": "active",
        "is_frozen": False,
        "last_trade_price": last_trade_price,
        "daily_quote_token_volume": daily_quote_volume,
        "daily_base_token_volume": daily_base_volume,
        "daily_price_change": daily_price_change,
        "open_interest": "0",
        "min_base_amount": min_base_amount,
        "min_quote_amount": min_quote_amount,
        "price_decimals": price_decimals,
        "size_decimals": size_decimals,
    }


def _lighter_combined_catalog(
    perp: List[Dict[str, Any]],
    spot: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "code": 200,
        "order_book_details": perp,
        "spot_order_book_details": spot,
    }


class TestLighterGetTickersOffline(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.agent_module = importlib.import_module("plugins.trade.agents.x_lighter_agent")

    def setUp(self) -> None:
        self._write_originals = _patch_all_writes(self.agent_module)

    def tearDown(self) -> None:
        for name, original in self._write_originals:
            setattr(self.agent_module, name, original)

    def _perp_catalog(self) -> Dict[str, Any]:
        return _lighter_combined_catalog(
            perp=[
                _lighter_perp_entry("BTC", market_id=1, mark_price="60000",
                                    last_trade_price="59900", index_price="60010",
                                    daily_quote_volume="3000000", daily_base_volume="50",
                                    daily_price_change="1.2", open_interest="500",
                                    min_base_amount="0.0001", min_quote_amount="10",
                                    price_decimals=1, size_decimals=5),
                _lighter_perp_entry("ETH", market_id=2, mark_price="2700",
                                    last_trade_price="2699", index_price="2701",
                                    daily_quote_volume="1000000", daily_base_volume="400",
                                    daily_price_change="-0.4", open_interest="2000",
                                    min_base_amount="0.001", min_quote_amount="10",
                                    price_decimals=2, size_decimals=4),
                _lighter_perp_entry("DOGE", market_id=3, mark_price="0.12345",
                                    last_trade_price="", index_price=None,
                                    daily_quote_volume="", daily_base_volume="",
                                    daily_price_change="0", open_interest="0",
                                    min_base_amount="1", min_quote_amount="10",
                                    price_decimals=5, size_decimals=0),
            ],
            spot=[
                _lighter_spot_entry("UNI/USDC", market_id=2051, last_trade_price="9.7342",
                                    daily_quote_volume="4768.80", daily_base_volume="513.29",
                                    daily_price_change="5.71", price_decimals=4, size_decimals=2),
                _lighter_spot_entry("ETH/USDC", market_id=2052, last_trade_price="2700",
                                    daily_quote_volume="500000", daily_base_volume="185",
                                    daily_price_change="0.1", price_decimals=2, size_decimals=4),
            ],
        )

    def test_A_capabilities_advertise_get_tickers(self) -> None:
        caps = list(self.agent_module.capabilities())
        self.assertIn("get_tickers", caps)
        for expected in ("list_instruments", "market_price", "new_order",
                         "ladder", "resolve_instrument"):
            self.assertIn(expected, caps)

    def test_B_get_tickers_is_read_only_and_bulk(self) -> None:
        payload = self._perp_catalog()
        with mock.patch.object(
            self.agent_module, "_fetch_market_catalog", return_value=(
                list(payload["order_book_details"]) +
                list(payload["spot_order_book_details"])
            ),
        ) as bulk_calls, mock.patch.object(
            self.agent_module,
            "_fetch_lighter_public_market_price",
            side_effect=AssertionError("no per-symbol fan-out"),
        ) as per_symbol, mock.patch.object(
            self.agent_module, "requests", side_effect=AssertionError("no extra http"),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        self.assertTrue(response.success, response.to_dict())
        # Exactly one bulk call to the catalog endpoint; never per-symbol.
        self.assertEqual(bulk_calls.call_count, 1)
        self.assertFalse(per_symbol.called)
        batch = response.tickers_batch
        self.assertIsInstance(batch, CanonicalTickersBatch)
        self.assertEqual(batch.refresh_status, "ok")

    def test_C_complete_catalog_preserved_when_dynamic_missing(self) -> None:
        payload = self._perp_catalog()
        with mock.patch.object(
            self.agent_module, "_fetch_market_catalog", return_value=(
                list(payload["order_book_details"]) +
                list(payload["spot_order_book_details"])
            ),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        # All 5 rows preserved (3 perp + 2 spot), even when DOGE has
        # blank/zero fields.
        self.assertEqual(len(rows), 5)

    def test_D_filter_by_symbols_narrows_result(self) -> None:
        payload = self._perp_catalog()
        with mock.patch.object(
            self.agent_module, "_fetch_market_catalog", return_value=(
                list(payload["order_book_details"]) +
                list(payload["spot_order_book_details"])
            ),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
                "symbols": ["BTC"],
            })
        self.assertEqual(list(response.tickers_batch.tickers), ["BTC"])

    def test_E_mark_price_and_oracle_map_from_bulk(self) -> None:
        payload = self._perp_catalog()
        with mock.patch.object(
            self.agent_module, "_fetch_market_catalog", return_value=(
                list(payload["order_book_details"]) +
                list(payload["spot_order_book_details"])
            ),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        self.assertEqual(rows["BTC"].mark_price, "60000")
        self.assertEqual(rows["BTC"].oracle_price, "60010")
        self.assertEqual(rows["ETH"].mark_price, "2700")
        self.assertEqual(rows["ETH"].oracle_price, "2701")

    def test_F_unavailable_fields_stay_none(self) -> None:
        payload = self._perp_catalog()
        with mock.patch.object(
            self.agent_module, "_fetch_market_catalog", return_value=(
                list(payload["order_book_details"]) +
                list(payload["spot_order_book_details"])
            ),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        for row in rows.values():
            # Lighter does not publish a per-row current funding rate.
            self.assertIsNone(row.funding_rate)
        # Quote-volume alias is the daily_quote_token_volume field.
        self.assertEqual(rows["BTC"].turnover_24h, "3000000")
        self.assertEqual(rows["BTC"].volume_24h_quote, "3000000")
        self.assertEqual(rows["BTC"].volume_24h_base, "50")
        self.assertEqual(rows["BTC"].change_24h_pct, "1.2")
        self.assertEqual(rows["BTC"].open_interest, "500")
        # DOGE has blank daily_quote_token_volume and "" daily_base_volume
        # → both must collapse to None rather than "0".
        self.assertIsNone(rows["DOGE"].volume_24h_quote)
        self.assertIsNone(rows["DOGE"].volume_24h_base)

    def test_G_identity_fields(self) -> None:
        payload = self._perp_catalog()
        with mock.patch.object(
            self.agent_module, "_fetch_market_catalog", return_value=(
                list(payload["order_book_details"]) +
                list(payload["spot_order_book_details"])
            ),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        btc = rows["BTC"]
        self.assertEqual(btc.symbol, "BTC")
        self.assertEqual(btc.native_symbol, "BTC")
        self.assertEqual(btc.display_symbol, "BTC")
        self.assertEqual(btc.display_name, "BTC")
        self.assertEqual(btc.base, "BTC")
        self.assertEqual(btc.market_type, "perp")

    def test_H_market_type_preserved(self) -> None:
        payload = self._perp_catalog()
        with mock.patch.object(
            self.agent_module, "_fetch_market_catalog", return_value=(
                list(payload["order_book_details"]) +
                list(payload["spot_order_book_details"])
            ),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        self.assertEqual(rows["BTC"].market_type, "perp")
        self.assertEqual(rows["ETH"].market_type, "perp")
        self.assertEqual(rows["UNI/USDC"].market_type, "spot")
        self.assertEqual(rows["ETH/USDC"].market_type, "spot")

    def test_O_zero_or_missing_price_maps_to_none(self) -> None:
        payload = self._perp_catalog()
        with mock.patch.object(
            self.agent_module, "_fetch_market_catalog", return_value=(
                list(payload["order_book_details"]) +
                list(payload["spot_order_book_details"])
            ),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        # DOGE row has mark_price="0.12345" so mark_price is set.
        # last_trade_price is "" and index_price is None → both None.
        self.assertEqual(rows["DOGE"].mark_price, "0.12345")
        self.assertIsNone(rows["DOGE"].last_external_price)
        self.assertIsNone(rows["DOGE"].oracle_price)
        # Static identity still present.
        self.assertEqual(rows["DOGE"].display_symbol, "DOGE")

    def test_10_spot_perp_disambiguation_preserved(self) -> None:
        payload = self._perp_catalog()
        with mock.patch.object(
            self.agent_module, "_fetch_market_catalog", return_value=(
                list(payload["order_book_details"]) +
                list(payload["spot_order_book_details"])
            ),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        # Spot and perp rows must NOT collapse just because their
        # base/display symbols overlap.
        # ETH perp market is "ETH" and ETH spot market is "ETH/USDC".
        self.assertEqual(rows["ETH"].market_type, "perp")
        self.assertEqual(rows["ETH/USDC"].market_type, "spot")

    def test_11_spot_perp_collision_does_not_merge(self) -> None:
        # Construct a catalog where the spot and perp entries share the
        # same base symbol ("ETH") so naive dedup would drop one.
        payload = _lighter_combined_catalog(
            perp=[_lighter_perp_entry("ETH", market_id=2)],
            spot=[_lighter_spot_entry("ETH/USDC", market_id=2052)],
        )
        with mock.patch.object(
            self.agent_module, "_fetch_market_catalog", return_value=(
                list(payload["order_book_details"]) +
                list(payload["spot_order_book_details"])
            ),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        # Both rows must be present and distinct.
        self.assertEqual(set(rows), {"ETH", "ETH/USDC"})
        self.assertEqual(rows["ETH"].market_type, "perp")
        self.assertEqual(rows["ETH/USDC"].market_type, "spot")

    def test_K_list_instruments_still_works(self) -> None:
        payload = self._perp_catalog()
        with mock.patch.object(
            self.agent_module, "_fetch_market_catalog", return_value=(
                list(payload["order_book_details"]) +
                list(payload["spot_order_book_details"])
            ),
        ), mock.patch.object(
            self.agent_module, "_fetch_lighter_market_catalog_raw",
            side_effect=lambda base_url, bucket: list(payload.get(bucket) or []),
        ):
            response = self.agent_module.execute({
                "operation": "list_instruments",
                "account": "demo",
            })
        self.assertTrue(response.success, response.to_dict())
        data = response.data or {}
        instruments = data.get("instruments") or []
        self.assertEqual(len(instruments), 5)

    def test_L_market_price_still_works(self) -> None:
        btc_entry = _lighter_perp_entry("BTC", market_id=1, mark_price="60000")
        with mock.patch.object(
            self.agent_module, "_resolve_market", return_value=btc_entry,
        ), mock.patch.object(
            self.agent_module, "_lookup_credentials",
            return_value={"base_url": "https://example", "chain": "ARBITRUM",
                          "account_index": 0},
        ), mock.patch.object(
            self.agent_module, "_fetch_lighter_public_market_price",
            return_value={"mark": "60000", "last_trade": "59900", "bid": None,
                          "ask": None, "ts": 0},
        ):
            response = self.agent_module.execute({
                "operation": "market_price",
                "account": "demo",
                "symbol": "BTC",
            })
        self.assertTrue(response.success, response.to_dict())
        self.assertEqual(response.market_price.mark_price, "60000")


# ---------------------------------------------------------------------------
# Raydium
# ---------------------------------------------------------------------------


def _raydium_info_payload() -> Dict[str, Any]:
    return {
        "success": True,
        "data": {
            "rows": [
                {
                    "symbol": "PERP_BTC_USDC",
                    "display_symbol_name": "BTC",
                    "status": "ACTIVE",
                    "quote_min": 0,
                    "quote_max": 100000,
                    "quote_tick": 0.1,
                    "base_min": 0.0001,
                    "base_max": 1000,
                    "base_tick": 0.0001,
                    "min_notional": 10,
                    "price_scope": 0.6,
                    "is_pretge": False,
                },
                {
                    "symbol": "PERP_ETH_USDC",
                    "display_symbol_name": "ETH",
                    "status": "ACTIVE",
                    "quote_min": 0,
                    "quote_max": 100000,
                    "quote_tick": 0.01,
                    "base_min": 0.001,
                    "base_max": 10000,
                    "base_tick": 0.001,
                    "min_notional": 10,
                    "price_scope": 0.6,
                    "is_pretge": False,
                },
                {
                    "symbol": "PERP_1000BONK_USDC",
                    "display_symbol_name": "1000BONK",
                    "status": "ACTIVE",
                    "quote_min": 0,
                    "quote_max": 100000,
                    "quote_tick": 0.000001,
                    "base_min": 1,
                    "base_max": 23200000,
                    "base_tick": 10,
                    "min_notional": 10,
                    "price_scope": 0.6,
                    "is_pretge": False,
                },
            ]
        },
    }


def _raydium_futures_payload() -> Dict[str, Any]:
    return {
        "success": True,
        "data": {
            "rows": [
                {
                    "symbol": "PERP_BTC_USDC",
                    "display_symbol_name": "BTC",
                    "status": "ACTIVE",
                    "index_price": 60000,
                    "mark_price": 60010,
                    "sum_unitary_funding": 0.0001,
                    "est_funding_rate": 0.00015,
                    "last_funding_rate": 0.00012,
                    "next_funding_time": 1790352000000,
                    "open_interest": 500,
                    "is_pretge": False,
                    "24h_open": 59500,
                    "24h_close": 60000,
                    "24h_high": 60500,
                    "24h_low": 59400,
                    "24h_volume": 50,
                    "24h_amount": 3000000,
                },
                {
                    "symbol": "PERP_ETH_USDC",
                    "display_symbol_name": "ETH",
                    "status": "ACTIVE",
                    "index_price": 2700,
                    "mark_price": 2702,
                    "sum_unitary_funding": 0.0002,
                    "est_funding_rate": 0.00018,
                    "last_funding_rate": 0.00016,
                    "next_funding_time": 1790352000000,
                    "open_interest": 2000,
                    "is_pretge": False,
                    "24h_open": 2680,
                    "24h_close": 2700,
                    "24h_high": 2710,
                    "24h_low": 2670,
                    "24h_volume": 400,
                    "24h_amount": 1080000,
                },
                # 1000BONK intentionally omitted — dynamic data missing.
            ]
        },
    }


class TestRaydiumGetTickersOffline(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.agent_module = importlib.import_module("plugins.trade.agents.x_raydium_agent")

    def setUp(self) -> None:
        self._write_originals = _patch_all_writes(self.agent_module)

    def tearDown(self) -> None:
        for name, original in self._write_originals:
            setattr(self.agent_module, name, original)

    def test_A_capabilities_advertise_get_tickers(self) -> None:
        caps = list(self.agent_module.capabilities())
        self.assertIn("get_tickers", caps)
        for expected in ("list_instruments", "market_price", "new_order",
                         "ladder", "resolve_instrument", "close_position"):
            self.assertIn(expected, caps)

    def test_B_get_tickers_is_read_only_and_two_bulk_calls(self) -> None:
        info = _raydium_info_payload()
        fut = _raydium_futures_payload()
        with mock.patch.object(
            self.agent_module, "_public_get",
            side_effect=[info, fut],
        ) as public_get, mock.patch.object(
            self.agent_module,
            "_fetch_symbol_rules",
            side_effect=AssertionError("no per-symbol fan-out"),
        ) as per_symbol, mock.patch.object(
            self.agent_module,
            "_resolve_symbol_metadata",
            side_effect=AssertionError("no per-symbol fan-out"),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        self.assertTrue(response.success, response.to_dict())
        self.assertFalse(per_symbol.called)
        # Exactly two bulk public calls: /v1/public/info + /v1/public/futures.
        paths = [c.args[0] for c in public_get.call_args_list]
        self.assertEqual(paths.count("/v1/public/info"), 1)
        self.assertEqual(paths.count("/v1/public/futures"), 1)
        self.assertEqual(len(paths), 2)

    def test_C_complete_catalog_preserved_when_futures_row_missing(self) -> None:
        info = _raydium_info_payload()
        fut = _raydium_futures_payload()  # 1000BONK missing
        with mock.patch.object(
            self.agent_module, "_public_get",
            side_effect=[info, fut],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        # All three catalog rows preserved even when dynamic data is absent.
        self.assertEqual(len(rows), 3)

    def test_D_filter_by_symbols_narrows_result(self) -> None:
        info = _raydium_info_payload()
        fut = _raydium_futures_payload()
        with mock.patch.object(
            self.agent_module, "_public_get",
            side_effect=[info, fut],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
                "symbols": ["PERP_ETH_USDC"],
            })
        self.assertEqual(list(response.tickers_batch.tickers), ["PERP_ETH_USDC"])

    def test_E_mark_price_oracle_and_funding_map_from_futures(self) -> None:
        info = _raydium_info_payload()
        fut = _raydium_futures_payload()
        with mock.patch.object(
            self.agent_module, "_public_get",
            side_effect=[info, fut],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        self.assertEqual(rows["PERP_BTC_USDC"].mark_price, "60010")
        self.assertEqual(rows["PERP_BTC_USDC"].oracle_price, "60000")
        self.assertEqual(rows["PERP_BTC_USDC"].funding_rate, "0.00012")
        self.assertEqual(rows["PERP_BTC_USDC"].open_interest, "500")

    def test_F_unavailable_fields_stay_none(self) -> None:
        info = _raydium_info_payload()
        fut = _raydium_futures_payload()
        with mock.patch.object(
            self.agent_module, "_public_get",
            side_effect=[info, fut],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        # 1000BONK has no futures row → mark/oracle/funding/oi stay None.
        for f in ("mark_price", "oracle_price", "funding_rate", "open_interest",
                  "turnover_24h", "volume_24h_quote", "volume_24h_base",
                  "change_24h_pct", "last_updated_time"):
            self.assertIsNone(getattr(rows["PERP_1000BONK_USDC"], f),
                              f"PERP_1000BONK_USDC.{f} should be None")
        # Static identity still present.
        self.assertEqual(rows["PERP_1000BONK_USDC"].display_symbol, "1000BONK")
        self.assertEqual(rows["PERP_1000BONK_USDC"].display_name, "1000BONK")
        self.assertEqual(rows["PERP_1000BONK_USDC"].base, "1000BONK")
        self.assertEqual(rows["PERP_1000BONK_USDC"].quote, "USDC")
        self.assertEqual(rows["PERP_1000BONK_USDC"].market_type, "perp")

    def test_G_identity_fields(self) -> None:
        info = _raydium_info_payload()
        fut = _raydium_futures_payload()
        with mock.patch.object(
            self.agent_module, "_public_get",
            side_effect=[info, fut],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        btc = rows["PERP_BTC_USDC"]
        self.assertEqual(btc.symbol, "PERP_BTC_USDC")
        self.assertEqual(btc.native_symbol, "PERP_BTC_USDC")
        self.assertEqual(btc.display_symbol, "BTC")
        self.assertEqual(btc.display_name, "BTC")
        self.assertEqual(btc.base, "BTC")
        self.assertEqual(btc.quote, "USDC")
        self.assertEqual(btc.market_type, "perp")

    def test_H_market_type_preserved(self) -> None:
        info = _raydium_info_payload()
        fut = _raydium_futures_payload()
        with mock.patch.object(
            self.agent_module, "_public_get",
            side_effect=[info, fut],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        for row in response.tickers_batch.tickers.values():
            self.assertEqual(row.market_type, "perp")

    def test_12_native_orderly_symbol_and_display_mapping(self) -> None:
        info = _raydium_info_payload()
        fut = _raydium_futures_payload()
        with mock.patch.object(
            self.agent_module, "_public_get",
            side_effect=[info, fut],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        # Join key is the native Orderly id, not a normalized form.
        self.assertIn("PERP_BTC_USDC", rows)
        self.assertIn("PERP_ETH_USDC", rows)
        # display_symbol_name is the human label, not the native id.
        self.assertEqual(rows["PERP_BTC_USDC"].display_symbol, "BTC")
        self.assertEqual(rows["PERP_1000BONK_USDC"].display_symbol, "1000BONK")

    def test_13_volume_semantics_quote_vs_base(self) -> None:
        info = _raydium_info_payload()
        fut = _raydium_futures_payload()
        with mock.patch.object(
            self.agent_module, "_public_get",
            side_effect=[info, fut],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        # 24h_amount is the quote/USDC turnover (NOT a fabrication from
        # base volume). Both turnover_24h and volume_24h_quote are set
        # because they are aliases in the canonical contract.
        self.assertEqual(rows["PERP_BTC_USDC"].turnover_24h, "3000000")
        self.assertEqual(rows["PERP_BTC_USDC"].volume_24h_quote, "3000000")
        # 24h_volume is the base-asset volume. Surfaced for ranking
        # inspection but NOT used by WebTrade2's _volume_key.
        self.assertEqual(rows["PERP_BTC_USDC"].volume_24h_base, "50")

    def test_14_change_24h_pct_derived_from_24h_open_close(self) -> None:
        info = _raydium_info_payload()
        fut = _raydium_futures_payload()
        with mock.patch.object(
            self.agent_module, "_public_get",
            side_effect=[info, fut],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        # (60000 - 59500) / 59500 * 100 = 0.84033613...
        # Accept the decimal-stripped canonical form.
        change = Decimal(rows["PERP_BTC_USDC"].change_24h_pct or "0")
        self.assertAlmostEqual(float(change), 0.84033613, places=3)

    def test_O_zero_or_missing_mark_maps_to_none(self) -> None:
        info = _raydium_info_payload()
        fut = {
            "success": True,
            "data": {"rows": [
                {
                    "symbol": "PERP_BTC_USDC",
                    "display_symbol_name": "BTC",
                    "status": "ACTIVE",
                    "mark_price": "0",
                    "index_price": "60000",
                    "open_interest": 0,
                    "24h_open": 59500,
                    "24h_close": 60000,
                    "24h_amount": 0,
                    "24h_volume": 0,
                }
            ]}
        }
        with mock.patch.object(
            self.agent_module, "_public_get",
            side_effect=[info, fut],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        self.assertIsNone(rows["PERP_BTC_USDC"].mark_price)
        # Oracle still set.
        self.assertEqual(rows["PERP_BTC_USDC"].oracle_price, "60000")

    def test_K_list_instruments_still_works(self) -> None:
        info = _raydium_info_payload()
        fut = _raydium_futures_payload()
        with mock.patch.object(
            self.agent_module, "_public_get",
            side_effect=[info, fut],
        ):
            response = self.agent_module.execute({
                "operation": "list_instruments",
                "account": "demo",
            })
        self.assertTrue(response.success, response.to_dict())
        data = response.data or {}
        instruments = data.get("instruments") or []
        self.assertEqual(len(instruments), 3)

    def test_L_market_price_still_works(self) -> None:
        with mock.patch.object(
            self.agent_module, "_resolve_symbol_metadata",
            return_value={
                "symbol": "PERP_BTC_USDC",
                "display_symbol": "BTC",
                "mark_price": Decimal("60000"),
                "quote_tick": Decimal("0.1"),
                "base_tick": Decimal("0.0001"),
                "min_quantity": Decimal("0.0001"),
                "min_notional": Decimal("10"),
                "base_currency": "BTC",
                "quote_currency": "USDC",
            },
        ):
            response = self.agent_module.execute({
                "operation": "market_price",
                "account": "demo",
                "symbol": "BTC",
            })
        self.assertTrue(response.success, response.to_dict())
        self.assertEqual(response.market_price.mark_price, "60000")


if __name__ == "__main__":
    unittest.main()
