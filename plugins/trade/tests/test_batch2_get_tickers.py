"""RED-first offline tests for canonical ``get_tickers`` on the Batch 2
exchanges (Arcus, OndoPerps, Nado).

The tests prove:

A. capabilities advertises ``get_tickers`` (additive only).
B. ``get_tickers`` is read-only (no write helper invoked, no per-symbol
   HTTP fan-out beyond the bulk sources the agent already owns).
C. complete expected catalog is returned (catalog rows preserved even
   when dynamic price data is absent).
D. ``symbols``/``query`` filtering narrows the result.
E. ``mark_price`` maps from the same bulk source the existing
   ``market_price`` reads (no extra fetch per symbol).
F. unavailable fields stay ``None`` rather than ``0``.
G. ``symbol`` / ``native_symbol`` / ``display_symbol`` / ``display_name``
   / ``base`` / ``quote`` identity is preserved.
H. ``market_type`` preserved.
I. existing cache/bulk source is reused.
J. no per-symbol fan-out (write-counter patches assert bulk-only).
K. ``list_instruments`` still works.
L. ``market_price`` still works.
M. existing trading/write capabilities remain unchanged.
N. partial/missing dynamic price data does not remove the static
    instrument row.
O. zero-like unavailable values map to ``None``.

These tests are intentionally named with the same taxonomy as Batch 1 so
that ``test_webtrade2_canonical_market_data.py`` can fold them in.
"""

from __future__ import annotations

import importlib
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from plugins.trade.canonical import (
    CanonicalResponse,
    CanonicalTickersBatch,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

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
)


def _patch_all_writes(agent_module: Any) -> List[Tuple[str, Any]]:
    """Wrap every write-helper on the module so a call raises.

    Returns the list of (name, original) for unwinding in setUp/tearDown.
    """
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
# Arcus
# ---------------------------------------------------------------------------

class _ArcusCatalogFixtures:
    """A static Arcus /v1/markets payload with realistic field coverage."""

    @staticmethod
    def markets_payload() -> Dict[str, Any]:
        return {
            "markets": [
                {
                    "marketId": 1,
                    "marketDisplayName": "ETH-USD.P",
                    "baseAsset": "ETH",
                    "quoteAsset": "USD",
                    "marketType": "perp",
                    "markPx": "2709.4",
                    "oraclePrice": "2710.0",
                    "lastPrice": "2709.1",
                    "midPrice": "2709.3",
                    "description": "ETH-USD.P",
                    "quoteDisplayName": "USD-PERP",
                },
                {
                    "marketId": 2,
                    "marketDisplayName": "BTC-USD.P",
                    "baseAsset": "BTC",
                    "quoteAsset": "USD",
                    "marketType": "perp",
                    "markPx": "0",
                    "oraclePrice": "84600.0",
                    "lastPrice": "",
                    "midPrice": None,
                    "description": "BTC-USD.P",
                },
                {
                    "marketId": 3,
                    "marketDisplayName": "DOGE-USD.P",
                    "baseAsset": "DOGE",
                    "quoteAsset": "USD",
                    "marketType": "perp",
                    "markPx": "0.12345",
                    "description": "DOGE perp",
                },
            ]
        }


class TestArcusGetTickersOffline(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.agent_module = importlib.import_module("plugins.trade.agents.x_arcus_agent")
        cls.fixture = _ArcusCatalogFixtures()

    def setUp(self) -> None:
        self._write_originals = _patch_all_writes(self.agent_module)

    def tearDown(self) -> None:
        for name, original in self._write_originals:
            setattr(self.agent_module, name, original)

    def _seed_market_cache(self) -> None:
        # Seed the existing /v1/markets cache directly to bypass the gate.
        cache = self.agent_module._ARCUS_MARKETS_CACHE
        cache["by_id"] = {
            int(m["marketId"]): m for m in self.fixture.markets_payload()["markets"]
        }
        cache["by_display_name"] = {
            m["marketDisplayName"]: int(m["marketId"])
            for m in self.fixture.markets_payload()["markets"]
        }
        cache["ts"] = __import__("time").time()

    def test_A_capabilities_advertise_get_tickers(self) -> None:
        caps = list(self.agent_module.capabilities())
        self.assertIn("get_tickers", caps)
        # Pre-Batch-2 capabilities must still be present.
        for expected in ("list_instruments", "market_price", "new_order", "close_position"):
            self.assertIn(expected, caps)

    def test_B_get_tickers_is_read_only_and_bulk(self) -> None:
        # /v1/markets must be hit ONCE per call; no per-symbol HTTP fan-out.
        with mock.patch.object(
            self.agent_module,
            "_fetch_arcus_markets_payload",
            return_value=[m for m in self.fixture.markets_payload()["markets"]],
        ) as bulk_calls, mock.patch.object(
            self.agent_module, "_arcus_fetch_mark_price", side_effect=AssertionError("no per-symbol fetch")
        ) as per_symbol, mock.patch.object(
            self.agent_module, "_public_get", side_effect=AssertionError("no extra http")
        ) as public_calls:
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        self.assertTrue(response.success, response.to_dict())
        self.assertEqual(bulk_calls.call_count, 1)
        self.assertFalse(per_symbol.called)
        self.assertFalse(public_calls.called)
        batch = response.tickers_batch
        self.assertIsInstance(batch, CanonicalTickersBatch)
        self.assertEqual(batch.refresh_status, "ok")

    def test_C_complete_catalog_with_and_without_dynamic_price(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_fetch_arcus_markets_payload",
            return_value=[m for m in self.fixture.markets_payload()["markets"]],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        self.assertTrue(response.success, response.to_dict())
        rows = response.tickers_batch.tickers
        # Three rows even when two have degraded prices.
        self.assertEqual(len(rows), 3)
        self.assertIn("ETH-USD.P", rows)
        self.assertIn("BTC-USD.P", rows)
        self.assertIn("DOGE-USD.P", rows)

    def test_D_filter_by_symbols_narrows_result(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_fetch_arcus_markets_payload",
            return_value=[m for m in self.fixture.markets_payload()["markets"]],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
                "symbols": ["ETH-USD.P"],
            })
        self.assertTrue(response.success, response.to_dict())
        self.assertEqual(list(response.tickers_batch.tickers), ["ETH-USD.P"])

    def test_E_mark_price_and_oracle_map_from_bulk(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_fetch_arcus_markets_payload",
            return_value=[m for m in self.fixture.markets_payload()["markets"]],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        self.assertEqual(rows["ETH-USD.P"].mark_price, "2709.4")
        self.assertEqual(rows["ETH-USD.P"].oracle_price, "2710")
        self.assertEqual(rows["DOGE-USD.P"].mark_price, "0.12345")

    def test_F_unavailable_fields_stay_none(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_fetch_arcus_markets_payload",
            return_value=[m for m in self.fixture.markets_payload()["markets"]],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        # Arcus /v1/markets does not publish these dynamic metrics.
        for row in rows.values():
            self.assertIsNone(row.turnover_24h)
            self.assertIsNone(row.volume_24h_quote)
            self.assertIsNone(row.volume_24h_base)
            self.assertIsNone(row.funding_rate)
            self.assertIsNone(row.open_interest)
            self.assertIsNone(row.change_24h_pct)

    def test_G_identity_fields(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_fetch_arcus_markets_payload",
            return_value=[m for m in self.fixture.markets_payload()["markets"]],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        eth = rows["ETH-USD.P"]
        self.assertEqual(eth.symbol, "ETH-USD.P")
        self.assertEqual(eth.native_symbol, "ETH-USD.P")
        self.assertEqual(eth.display_symbol, "ETH")
        self.assertEqual(eth.base, "ETH")
        self.assertEqual(eth.quote, "USD")
        self.assertEqual(eth.display_name, "USD-PERP")

    def test_H_market_type_preserved(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_fetch_arcus_markets_payload",
            return_value=[m for m in self.fixture.markets_payload()["markets"]],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        for row in response.tickers_batch.tickers.values():
            self.assertEqual(row.market_type, "perp")

    def test_O_zero_price_maps_to_none_not_zero(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_fetch_arcus_markets_payload",
            return_value=[m for m in self.fixture.markets_payload()["markets"]],
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        # BTC-USD.P row carries markPx=0 / blank last / None mid; all must be None.
        self.assertIsNone(rows["BTC-USD.P"].mark_price)
        self.assertIsNone(rows["BTC-USD.P"].price)
        # Static identity survives so the catalog stays complete.
        self.assertEqual(rows["BTC-USD.P"].display_symbol, "BTC")

    def test_K_list_instruments_still_works(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_fetch_arcus_markets_payload",
            return_value=[m for m in self.fixture.markets_payload()["markets"]],
        ):
            response = self.agent_module.execute({
                "operation": "list_instruments",
                "account": "demo",
            })
        self.assertTrue(response.success, response.to_dict())
        data = response.data or {}
        instruments = data.get("instruments") or []
        self.assertGreaterEqual(len(instruments), 3)

    def test_L_market_price_still_works(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_lookup_credentials",
            return_value={"account": "demo", "base_url": "https://api.example", "api_key": "ak", "api_secret": "sk"},
        ), mock.patch.object(
            self.agent_module,
            "_arcus_fetch_mark_price",
            return_value=__import__("decimal").Decimal("2709.4"),
        ), mock.patch.object(
            self.agent_module,
            "_resolve_market",
            return_value={
                "display_symbol": "ETH",
                "market_id": 1,
                "marketDisplayName": "ETH-USD.P",
                "baseAsset": "ETH",
            },
        ):
            response = self.agent_module.execute({
                "operation": "market_price",
                "account": "demo",
                "symbol": "ETH-USD.P",
            })
        self.assertTrue(response.success, response.to_dict())
        self.assertEqual(response.market_price.mark_price, "2709.4")


# ---------------------------------------------------------------------------
# OndoPerps
# ---------------------------------------------------------------------------

class _OndoPerpsFixtures:
    @staticmethod
    def trading_pairs() -> List[Dict[str, Any]]:
        return [
            {
                "market": "ETH-USD.P",
                "baseCurrency": "ETH",
                "quoteCurrency": "USD",
                "marketType": "perp",
                "description": "ETH perpetual",
            },
            {
                "market": "BTC-USD.P",
                "baseCurrency": "BTC",
                "quoteCurrency": "USD",
                "marketType": "perp",
                "description": "BTC perpetual",
            },
            {
                "market": "OLD-USD.P",
                "baseCurrency": "OLD",
                "quoteCurrency": "USD",
                "marketType": "perp",
                "description": "deprecated",
            },
        ]

    @staticmethod
    def mark_prices_payload() -> Dict[str, Any]:
        return {
            "result": {
                "ETH-USD.P": {"markPrice": "2709.4", "oraclePrice": "2710.0"},
                "BTC-USD.P": {"markPrice": "0", "oraclePrice": "84600.0"},
                # OLD-USD.P intentionally omitted -> degraded row
            }
        }


class TestOndoPerpsGetTickersOffline(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.agent_module = importlib.import_module("plugins.trade.agents.x_ondoperps_agent")
        cls.fixture = _OndoPerpsFixtures()

    def setUp(self) -> None:
        self._write_originals = _patch_all_writes(self.agent_module)

    def tearDown(self) -> None:
        for name, original in self._write_originals:
            setattr(self.agent_module, name, original)

    def test_A_capabilities_advertise_get_tickers(self) -> None:
        caps = list(self.agent_module.capabilities())
        self.assertIn("get_tickers", caps)
        for expected in ("list_instruments", "market_price", "new_order", "ladder", "close_position"):
            self.assertIn(expected, caps)

    def test_B_get_tickers_is_bulk_only(self) -> None:
        # Both /v1/markets and /v1/perps/mark_prices must be hit ONCE per call.
        trading = self.fixture.trading_pairs()
        marks = self.fixture.mark_prices_payload()
        with mock.patch.object(
            self.agent_module,
            "_fetch_ondoperps_trading_pairs_for",
            return_value=trading,
        ) as pairs_calls, mock.patch.object(
            self.agent_module,
            "_signed_get",
            return_value=self.fixture.mark_prices_payload(),
        ) as signed_calls:
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        self.assertTrue(response.success, response.to_dict())
        # Exactly one bulk /v1/markets and one bulk /v1/perps/mark_prices.
        self.assertEqual(pairs_calls.call_count, 1)
        paths = [c.args[1] for c in signed_calls.call_args_list]
        self.assertEqual(paths.count(self.agent_module._PATH_PERPS_MARK_PRICES), 1)
        self.assertEqual(len(paths), 1)

    def test_C_complete_catalog_preserved_when_mark_missing(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_fetch_ondoperps_trading_pairs_for",
            return_value=self.fixture.trading_pairs(),
        ), mock.patch.object(
            self.agent_module,
            "_signed_get",
            return_value=self.fixture.mark_prices_payload(),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        # All three rows present even when OLD has no mark_price entry.
        self.assertEqual(set(rows), {"ETH-USD.P", "BTC-USD.P", "OLD-USD.P"})

    def test_D_filter_by_symbols(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_fetch_ondoperps_trading_pairs_for",
            return_value=self.fixture.trading_pairs(),
        ), mock.patch.object(
            self.agent_module,
            "_signed_get",
            return_value=self.fixture.mark_prices_payload(),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
                "symbols": ["ETH-USD.P"],
            })
        self.assertEqual(list(response.tickers_batch.tickers), ["ETH-USD.P"])

    def test_E_mark_price_maps_correctly(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_fetch_ondoperps_trading_pairs_for",
            return_value=self.fixture.trading_pairs(),
        ), mock.patch.object(
            self.agent_module,
            "_signed_get",
            return_value=self.fixture.mark_prices_payload(),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        self.assertEqual(rows["ETH-USD.P"].mark_price, "2709.4")
        self.assertEqual(rows["ETH-USD.P"].oracle_price, "2710")

    def test_F_unavailable_fields_stay_none(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_fetch_ondoperps_trading_pairs_for",
            return_value=self.fixture.trading_pairs(),
        ), mock.patch.object(
            self.agent_module,
            "_signed_get",
            return_value=self.fixture.mark_prices_payload(),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        for row in rows.values():
            self.assertIsNone(row.turnover_24h)
            self.assertIsNone(row.volume_24h_quote)
            self.assertIsNone(row.volume_24h_base)
            self.assertIsNone(row.funding_rate)
            self.assertIsNone(row.open_interest)
            self.assertIsNone(row.change_24h_pct)

    def test_G_identity_fields(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_fetch_ondoperps_trading_pairs_for",
            return_value=self.fixture.trading_pairs(),
        ), mock.patch.object(
            self.agent_module,
            "_signed_get",
            return_value=self.fixture.mark_prices_payload(),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        eth = rows["ETH-USD.P"]
        self.assertEqual(eth.symbol, "ETH-USD.P")
        self.assertEqual(eth.native_symbol, "ETH-USD.P")
        self.assertEqual(eth.base, "ETH")
        self.assertEqual(eth.quote, "USD")

    def test_H_market_type_preserved(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_fetch_ondoperps_trading_pairs_for",
            return_value=self.fixture.trading_pairs(),
        ), mock.patch.object(
            self.agent_module,
            "_signed_get",
            return_value=self.fixture.mark_prices_payload(),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        for row in response.tickers_batch.tickers.values():
            self.assertEqual(row.market_type, "perp")

    def test_O_zero_or_missing_price_maps_to_none(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_fetch_ondoperps_trading_pairs_for",
            return_value=self.fixture.trading_pairs(),
        ), mock.patch.object(
            self.agent_module,
            "_signed_get",
            return_value=self.fixture.mark_prices_payload(),
        ):
            response = self.agent_module.execute({
                "operation": "get_tickers",
                "account": "demo",
            })
        rows = response.tickers_batch.tickers
        self.assertIsNone(rows["BTC-USD.P"].mark_price)
        self.assertEqual(rows["BTC-USD.P"].oracle_price, "84600")
        self.assertIsNone(rows["OLD-USD.P"].mark_price)
        self.assertIsNone(rows["OLD-USD.P"].oracle_price)

    def test_K_list_instruments_still_works(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_lookup_credentials",
            return_value={"account": "demo", "api_key": "ak", "api_secret": "sk", "base_url": "https://api.example"},
        ), mock.patch.object(
            self.agent_module,
            "_fetch_ondoperps_trading_pairs_for",
            return_value=self.fixture.trading_pairs(),
        ):
            response = self.agent_module.execute({
                "operation": "list_instruments",
                "account": "demo",
            })
        self.assertTrue(response.success, response.to_dict())
        data = response.data or {}
        instruments = data.get("instruments") or []
        self.assertGreaterEqual(len(instruments), 3)

    def test_L_market_price_still_works(self) -> None:
        with mock.patch.object(
            self.agent_module,
            "_lookup_credentials",
            return_value={"account": "demo", "api_key": "ak", "api_secret": "sk", "base_url": "https://api.example"},
        ), mock.patch.object(
            self.agent_module,
            "_resolve_market_metadata",
            return_value=(
                {"market": "ETH-USD.P", "quote_increment": "0.1", "base_increment": "0.01"},
                None,
            ),
        ), mock.patch.object(
            self.agent_module,
            "_signed_get",
            return_value=self.fixture.mark_prices_payload(),
        ):
            response = self.agent_module.execute({
                "operation": "market_price",
                "account": "demo",
                "symbol": "ETH-USD.P",
            })
        self.assertTrue(response.success, response.to_dict())
        self.assertEqual(response.market_price.mark_price, "2709.4")


# ---------------------------------------------------------------------------
# Nado
# ---------------------------------------------------------------------------

class _NadoFixtures:
    @staticmethod
    def symbols_payload() -> Dict[str, Any]:
        return {
            "status": "success",
            "data": {
                "symbols": {
                    "BTC-PERP": {
                        "product_id": 4,
                        "symbol": "BTC-PERP",
                        "type": "perp",
                        "price_increment_x18": str(10 ** 15),
                        "size_increment": str(10 ** 17),
                        "min_size": str(10 ** 16),
                    },
                    "ETH-PERP": {
                        "product_id": 8,
                        "symbol": "ETH-PERP",
                        "type": "perp",
                        "price_increment_x18": str(10 ** 14),
                        "size_increment": str(10 ** 17),
                        "min_size": str(10 ** 16),
                    },
                    "XYZ-PERP": {
                        "product_id": 12,
                        "symbol": "XYZ-PERP",
                        "type": "perp",
                        "price_increment_x18": str(10 ** 14),
                        "size_increment": str(10 ** 17),
                        "min_size": str(10 ** 16),
                    },
                    "BTC-SPOT": {
                        "product_id": 100,
                        "symbol": "BTC-SPOT",
                        "type": "spot",
                    },
                }
            },
        }

    @staticmethod
    def all_products_payload() -> Dict[str, Any]:
        return {
            "status": "success",
            "data": {
                "perp_products": [
                    {
                        "product_id": 4,
                        "symbol": "BTC-PERP",
                        "oracle_price_x18": str(int(84600 * 10 ** 18)),
                    },
                    {
                        "product_id": 8,
                        "symbol": "ETH-PERP",
                        "oracle_price_x18": str(int(2709 * 10 ** 18)),
                    },
                    {
                        "product_id": 12,
                        "symbol": "XYZ-PERP",
                        "oracle_price_x18": "0",
                    },
                ],
                "spot_products": [
                    {
                        "product_id": 100,
                        "symbol": "BTC-SPOT",
                        "oracle_price_x18": "0",
                    }
                ],
            },
        }


class TestNadoGetTickersOffline(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.agent_module = importlib.import_module("plugins.trade.agents.x_nado_agent")
        cls.fixture = _NadoFixtures()

    def setUp(self) -> None:
        self._write_originals = _patch_all_writes(self.agent_module)
        # Reset symbols cache so every test seeds it deterministically.
        self.agent_module._symbols_cache.update({
            "by_symbol": {},
            "by_pid": {},
            "ts": 0.0,
        })
        self.agent_module._contracts_cache.update({"data": None, "ts": 0.0})

    def tearDown(self) -> None:
        for name, original in self._write_originals:
            setattr(self.agent_module, name, original)

    def _seed_symbols(self) -> None:
        # Force the symbols cache through _ensure_symbols using a fake gateway.
        fake_credentials = {"account": "demo"}

        def fake_query(creds: Dict[str, str], request: Dict[str, Any]) -> Dict[str, Any]:
            kind = request.get("type")
            if kind == "symbols":
                return self.fixture.symbols_payload()
            if kind == "contracts":
                return {"status": "success", "data": {"chain_id": 1, "endpoint_addr": "0x0"}}
            if kind == "all_products":
                return self.fixture.all_products_payload()
            if kind == "market_prices":
                return {"status": "success", "data": {"market_prices": []}}
            return {"status": "error", "error": f"unknown {kind}"}

        self.agent_module._lookup_credentials = lambda account: fake_credentials  # type: ignore[assignment]
        self.agent_module._gateway_query = fake_query  # type: ignore[assignment]

    def test_A_capabilities_advertise_get_tickers(self) -> None:
        caps = list(self.agent_module.capabilities())
        self.assertIn("get_tickers", caps)
        for expected in ("list_instruments", "market_price", "new_order", "ladder", "close_position"):
            self.assertIn(expected, caps)

    def test_B_get_tickers_is_bulk_only(self) -> None:
        self._seed_symbols()
        # Track every gateway call to assert no per-symbol fan-out.
        calls: List[Dict[str, Any]] = []
        original_query = self.agent_module._gateway_query

        def tracking_query(creds: Dict[str, str], request: Dict[str, Any]) -> Dict[str, Any]:
            calls.append(dict(request))
            return original_query(creds, request)

        self.agent_module._gateway_query = tracking_query  # type: ignore[assignment]
        response = self.agent_module.execute({
            "operation": "get_tickers",
            "account": "demo",
        })
        self.assertTrue(response.success, response.to_dict())
        # Bulk calls only: at most one symbols + one all_products (+ one contracts).
        types = [c.get("type") for c in calls]
        # No per-product_id market_prices fan-out.
        self.assertNotIn("market_prices", types)
        self.assertLessEqual(types.count("all_products"), 1)

    def test_C_complete_catalog_with_filtering_to_perps_only(self) -> None:
        self._seed_symbols()
        response = self.agent_module.execute({
            "operation": "get_tickers",
            "account": "demo",
        })
        rows = response.tickers_batch.tickers
        # BTC-SPOT must be excluded (list_instruments parity).
        symbols = set(rows)
        self.assertIn("BTC-PERP", symbols)
        self.assertIn("ETH-PERP", symbols)
        self.assertIn("XYZ-PERP", symbols)
        self.assertNotIn("BTC-SPOT", symbols)

    def test_D_filter_by_symbols(self) -> None:
        self._seed_symbols()
        response = self.agent_module.execute({
            "operation": "get_tickers",
            "account": "demo",
            "symbols": ["ETH-PERP"],
        })
        self.assertEqual(list(response.tickers_batch.tickers), ["ETH-PERP"])

    def test_E_oracle_price_maps_from_all_products(self) -> None:
        self._seed_symbols()
        response = self.agent_module.execute({
            "operation": "get_tickers",
            "account": "demo",
        })
        rows = response.tickers_batch.tickers
        self.assertEqual(rows["BTC-PERP"].oracle_price, "84600")
        self.assertEqual(rows["ETH-PERP"].oracle_price, "2709")
        # XYZ-PERP oracle was 0 -> must be None.
        self.assertIsNone(rows["XYZ-PERP"].oracle_price)
        # mark_price should stay None because we did not call market_prices fan-out.
        self.assertIsNone(rows["BTC-PERP"].mark_price)

    def test_F_unavailable_fields_stay_none(self) -> None:
        self._seed_symbols()
        response = self.agent_module.execute({
            "operation": "get_tickers",
            "account": "demo",
        })
        rows = response.tickers_batch.tickers
        for row in rows.values():
            self.assertIsNone(row.turnover_24h)
            self.assertIsNone(row.volume_24h_quote)
            self.assertIsNone(row.volume_24h_base)
            self.assertIsNone(row.funding_rate)
            self.assertIsNone(row.open_interest)
            self.assertIsNone(row.change_24h_pct)
            self.assertIsNone(row.last_external_price)
            self.assertIsNone(row.last_updated_time)

    def test_G_identity_fields(self) -> None:
        self._seed_symbols()
        response = self.agent_module.execute({
            "operation": "get_tickers",
            "account": "demo",
        })
        rows = response.tickers_batch.tickers
        btc = rows["BTC-PERP"]
        self.assertEqual(btc.symbol, "BTC-PERP")
        self.assertEqual(btc.native_symbol, "BTC-PERP")
        self.assertEqual(btc.display_symbol, "BTC")
        self.assertEqual(btc.display_name, "BTC")
        self.assertEqual(btc.base, "BTC")

    def test_H_market_type_preserved(self) -> None:
        self._seed_symbols()
        response = self.agent_module.execute({
            "operation": "get_tickers",
            "account": "demo",
        })
        for row in response.tickers_batch.tickers.values():
            self.assertEqual(row.market_type, "perp")

    def test_O_zero_oracle_maps_to_none(self) -> None:
        self._seed_symbols()
        response = self.agent_module.execute({
            "operation": "get_tickers",
            "account": "demo",
        })
        rows = response.tickers_batch.tickers
        self.assertIsNone(rows["XYZ-PERP"].oracle_price)
        # Static identity preserved even when oracle is zero/unavailable.
        self.assertEqual(rows["XYZ-PERP"].display_symbol, "XYZ")

    def test_K_list_instruments_still_works(self) -> None:
        self._seed_symbols()
        response = self.agent_module.execute({
            "operation": "list_instruments",
            "account": "demo",
        })
        self.assertTrue(response.success, response.to_dict())
        data = response.data or {}
        instruments = data.get("instruments") or []
        syms = {i.get("symbol") if isinstance(i, dict) else getattr(i, "symbol", None) for i in instruments}
        self.assertIn("BTC-PERP", syms)
        self.assertNotIn("BTC-SPOT", syms)

    def test_L_market_price_still_works(self) -> None:
        self._seed_symbols()
        response = self.agent_module.execute({
            "operation": "market_price",
            "account": "demo",
            "symbol": "BTC-PERP",
        })
        self.assertTrue(response.success, response.to_dict())
        # Nado's market_price labels oracle_price and price with the oracle value.
        self.assertEqual(response.market_price.oracle_price, "84600")
        self.assertEqual(response.market_price.price, "84600")


if __name__ == "__main__":
    unittest.main()
