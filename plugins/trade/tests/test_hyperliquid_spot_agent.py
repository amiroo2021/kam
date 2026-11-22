from __future__ import annotations

import importlib
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from plugins.trade.canonical import CanonicalResponse
from plugins.trade.spotdesk import SpotDesk, _exchange_name_from_filename

spot = importlib.import_module("plugins.trade.agents.x_hyperliquid_spot_agent")


META = {
    "tokens": [
        {"name": "HYPE", "szDecimals": 2, "weiDecimals": 8},
        {"name": "USDC", "szDecimals": 2, "weiDecimals": 8},
        {"name": "HYPE", "szDecimals": 3, "weiDecimals": 8},
    ],
    "universe": [
        {"name": "HYPE/USDC", "tokens": [0, 1], "index": 0, "isCanonical": True},
        {"name": "HYPE/USDC", "tokens": [2, 1], "index": 1, "isCanonical": False},
    ],
}


ALIAS_META = {
    "tokens": [
        {"name": "USOL", "fullName": "Unit Solana", "index": 254, "szDecimals": 3, "weiDecimals": 8},
        {"name": "UBTC", "fullName": "Unit Bitcoin", "index": 197, "szDecimals": 5, "weiDecimals": 10},
        {"name": "UETH", "fullName": "Unit Ethereum", "index": 221, "szDecimals": 4, "weiDecimals": 9},
        {"name": "UZEC", "fullName": "Unit Zcash", "index": 419, "szDecimals": 4, "weiDecimals": 9},
        {"name": "HYPE", "fullName": "Hyperliquid", "index": 150, "szDecimals": 2, "weiDecimals": 8},
        {"name": "USDC", "index": 0, "szDecimals": 2, "weiDecimals": 8},
    ],
    "universe": [
        {"name": "@156", "tokens": [254, 0], "index": 156, "isCanonical": False},
        {"name": "@142", "tokens": [197, 0], "index": 142, "isCanonical": False},
        {"name": "@151", "tokens": [221, 0], "index": 151, "isCanonical": False},
        {"name": "@272", "tokens": [419, 0], "index": 272, "isCanonical": False},
        {"name": "@107", "tokens": [150, 0], "index": 107, "isCanonical": False},
    ],
}


def test_orders_use_official_open_orders_and_filter_perpetuals() -> None:
    wallet = "0x" + "2" * 40
    payload = [
        {"coin": "@156", "side": "B", "limitPx": "20", "sz": "0.5", "origSz": "1", "oid": 10},
        {"coin": "BTC", "side": "B", "limitPx": "100", "sz": "1", "origSz": "1", "oid": 11},
    ]
    with (
        patch.object(spot, "_lookup_credentials", return_value=(wallet, "secret")),
        patch.object(spot, "_fetch_spot_meta", return_value=ALIAS_META),
        patch.object(spot, "_post_info", return_value=payload) as post,
    ):
        response = spot.execute({"operation": "orders", "account": "BITGET"})
    assert response.success
    post.assert_called_once_with({"type": "openOrders", "user": wallet})
    assert response.open_order_count == 1
    assert response.data["orders"][0]["pair"] == "SOL/USDC"
    assert response.data["orders"][0]["native_coin"] == "@156"


def test_order_history_uses_official_schema_and_normalizes_wrapped_partial_fills() -> None:
    wallet = "0x" + "3" * 40
    payload = [
        {"order": {"coin": "@156", "side": "A", "limitPx": "20", "sz": "0.4", "origSz": "1", "oid": 12}, "status": "open"},
        {"order": {"coin": "@156", "side": "B", "limitPx": "21", "sz": "0", "origSz": "2", "oid": 13}, "status": "filled"},
        {"order": {"coin": "ETH", "side": "B", "limitPx": "100", "sz": "1", "origSz": "1", "oid": 14}, "status": "open"},
    ]
    with (
        patch.object(spot, "_lookup_credentials", return_value=(wallet, "secret")),
        patch.object(spot, "_fetch_spot_meta", return_value=ALIAS_META),
        patch.object(spot, "_post_info", return_value=payload) as post,
    ):
        response = spot.execute({"operation": "order_history", "account": "BITGET"})
    assert response.success
    post.assert_called_once_with({"type": "historicalOrders", "user": wallet})
    orders = response.data["orders"]
    assert len(orders) == 2
    assert orders[0]["executed_qty"] == "0.6"
    assert orders[0]["remaining_qty"] == "0.4"
    assert orders[1]["executed_qty"] == "2"
    assert orders[1]["status"] == "filled"


def test_orders_use_selected_account_wallet_not_signer_or_other_alias() -> None:
    bitget_wallet = "0x" + "4" * 40
    other_wallet = "0x" + "5" * 40
    with (
        patch.object(spot, "_lookup_credentials", side_effect=lambda alias: (bitget_wallet, "secret") if alias == "BITGET" else (other_wallet, "secret")),
        patch.object(spot, "_fetch_spot_meta", return_value=ALIAS_META),
        patch.object(spot, "_post_info", return_value=[]) as post,
    ):
        response = spot.execute({"operation": "orders", "account": "BITGET"})
    assert response.success
    post.assert_called_once_with({"type": "openOrders", "user": bitget_wallet})



def test_order_rows_reverse_native_ids_to_display_fields_and_group_by_display_symbol() -> None:
    wallet = "0x" + "7" * 40
    payload = [
        {"coin": "@107", "side": "B", "limitPx": "50", "sz": "1", "origSz": "1", "oid": 20},
        {"coin": "@107", "side": "B", "limitPx": "51", "sz": "2", "origSz": "2", "oid": 21},
    ]
    with (
        patch.object(spot, "_lookup_credentials", return_value=(wallet, "secret")),
        patch.object(spot, "_fetch_spot_meta", return_value=ALIAS_META),
        patch.object(spot, "_post_info", return_value=payload),
    ):
        response = spot.execute({"operation": "orders", "account": "BITGET"})
    assert response.success
    row = response.data["orders"][0]
    assert row["symbol"] == "HYPE/USDC"
    assert row["pair"] == "HYPE/USDC"
    assert row["display_symbol"] == "HYPE/USDC"
    assert row["base_asset"] == "HYPE"
    assert row["quote_asset"] == "USDC"
    assert row["native_coin"] == "@107"
    assert row["quantity_unit"] == "HYPE"
    assert row["price_unit"] == "USDC"
    assert response.order_groups[0].symbol == "HYPE/USDC"



def test_cancel_selection_displays_alias_but_sends_native_coin() -> None:
    wallet = "0x" + "8" * 40
    client = MagicMock()
    client.bulk_cancel.return_value = {"response": {"data": {"statuses": ["success"]}}}
    rows = [{
        "symbol": "SOL/USDC", "pair": "SOL/USDC", "display_symbol": "SOL/USDC",
        "native_coin": "@156", "base_asset": "SOL", "quote_asset": "USDC",
        "side": "BUY", "order_id": "99", "remaining_qty": "1", "price": "20",
        "orig_qty": "1", "executed_qty": "0", "status": "NEW", "type": "LIMIT",
    }]
    with (
        patch.object(spot, "_lookup_credentials", return_value=(wallet, "secret")),
        patch.object(spot, "_fetch_spot_meta", return_value=ALIAS_META),
        patch.object(spot, "_fetch_orders", return_value=rows),
        patch.object(spot, "_build_exchange_client", return_value=(client, wallet, "secret")),
    ):
        response = spot.execute({
            "operation": "cancel_orders", "account": "BITGET", "symbol": "SOL/USDC",
            "side": "BUY", "order_ids": ["99"],
        })
    assert response.success
    client.bulk_cancel.assert_called_once_with([{"coin": "@156", "oid": 99}])



def test_orders_propagate_api_failure_as_canonical_spot_error() -> None:
    with (
        patch.object(spot, "_lookup_credentials", return_value=("0x" + "6" * 40, "secret")),
        patch.object(spot, "_post_info", side_effect=RuntimeError("Hyperliquid HTTP 503: upstream unavailable")),
    ):
        response = spot.execute({"operation": "orders", "account": "BITGET"})
    assert not response.success
    assert response.error.code == "SPOT_ORDERS_FAILED"
    assert response.error.message == "Hyperliquid HTTP 503: upstream unavailable"



def test_frontend_aliases_resolve_to_live_native_spot_markets() -> None:
    markets = spot._parse_spot_meta(ALIAS_META)
    expected = {
        "SOL/USDC": ("USOL", "@156"), "BTC/USDC": ("UBTC", "@142"),
        "ETH/USDC": ("UETH", "@151"), "ZEC/USDC": ("UZEC", "@272"),
        "HYPE/USDC": ("HYPE", "@107"),
    }
    for frontend, (native_base, coin) in expected.items():
        resolved = spot._resolve_market(frontend, markets)
        assert resolved["base"] == native_base
        assert resolved["coin"] == coin
        bare = frontend.split("/", 1)[0]
        assert spot._resolve_market(bare, markets)["coin"] == coin


def test_frontend_alias_compact_and_list_discovery_expose_display_identity() -> None:
    markets = spot._parse_spot_meta(ALIAS_META)
    resolved = spot._resolve_market("SOLUSDC", markets)
    assert resolved["coin"] == "@156"
    with patch.object(spot, "_fetch_spot_meta", return_value=ALIAS_META):
        response = spot.execute({"operation": "list_instruments", "account": "demo", "query": "SOL"})
    assert response.success
    assert response.data["instruments"][0]["symbol"] == "SOLUSDC"
    assert response.data["instruments"][0]["native_symbol"] == "USOL/USDC"


def test_alias_resolution_returns_display_and_native_identities() -> None:
    with patch.object(spot, "_fetch_spot_meta", return_value=ALIAS_META):
        response = spot.execute({"operation": "resolve_instrument", "account": "demo", "symbol": "SOL/USDC"})
    assert response.success
    assert response.instrument.display_symbol == "SOL/USDC"
    assert response.instrument.native_symbol == "USOL/USDC"
    # The alias identity keys must be exactly these.
    assert response.data["asset_id"] == 10156
    assert response.data["coin"] == "@156"
    assert response.data["base"] == "USOL"
    assert response.data["quote"] == "USDC"
    assert response.data["native_symbol"] == "USOL/USDC"
    assert response.data["display_symbol"] == "SOL/USDC"
    assert response.data["base_token"] == "USOL"
    assert response.data["quote_token"] == "USDC"
    # Phase 7B additionally publishes the ladder-planner aliases. The
    # wizard re-resolves the instrument at confirmation time and
    # requires ``size_step`` to be present before it will submit.
    assert response.data["size_step"] == "0.001"
    assert response.data["min_notional"] == "10"


def test_sell_preview_uses_native_usol_balance_for_frontend_sol() -> None:
    with (
        patch.object(spot, "_lookup_credentials", return_value=("0x" + "1" * 40, "secret")),
        patch.object(spot, "_fetch_spot_meta", return_value=ALIAS_META),
        patch.object(spot, "_fetch_spot_balances", return_value=[{"coin": "USOL", "total": "1", "hold": "0.1"}]),
    ):
        response = spot.execute({
            "operation": "preview_order", "account": "demo", "side": "SELL",
            "symbol": "SOL/USDC", "quantity": "0.5", "price": "20",
        })
    assert response.success
    assert response.data["required_asset"] == "USOL"
    assert response.data["available"] == "0.9"


def test_alias_resolution_rejects_missing_native_market() -> None:
    meta = {**ALIAS_META, "universe": [row for row in ALIAS_META["universe"] if row["index"] != 156]}
    markets = spot._parse_spot_meta(meta)
    try:
        spot._resolve_market("SOL/USDC", markets)
    except spot.SpotInstrumentError as exc:
        assert exc.code == "INSTRUMENT_NOT_FOUND"
    else:
        raise AssertionError("alias resolved without a live native market")


def test_alias_collision_is_rejected_as_ambiguous() -> None:
    meta = {
        **ALIAS_META,
        "universe": [
            *ALIAS_META["universe"],
            {"name": "@999", "tokens": [254, 0], "index": 999, "isCanonical": False},
        ],
    }
    markets = spot._parse_spot_meta(meta)
    try:
        spot._resolve_market("SOL/USDC", markets)
    except spot.SpotInstrumentError as exc:
        assert exc.code == "INSTRUMENT_AMBIGUOUS"
    else:
        raise AssertionError("ambiguous display alias was accepted")


def test_spot_meta_resolves_exact_pair_and_asset_id() -> None:
    markets = spot._parse_spot_meta(META)
    resolved = spot._resolve_market("HYPE/USDC", markets)
    assert resolved["universe_index"] == 0
    assert resolved["asset_id"] == 10000
    assert resolved["coin"] == "@0"
    assert resolved["base"] == "HYPE"
    assert resolved["quote"] == "USDC"


def test_native_at_name_resolves_by_universe_index() -> None:
    markets = spot._parse_spot_meta(META)
    resolved = spot._resolve_market("@1", markets)
    assert resolved["asset_id"] == 10001
    assert resolved["coin"] == "@1"


def test_duplicate_friendly_pair_is_rejected_without_canonical_match() -> None:
    meta = {**META, "universe": [
        {"name": "HYPE/USDC", "tokens": [0, 1], "index": 0, "isCanonical": False},
        {"name": "HYPE/USDC", "tokens": [2, 1], "index": 1, "isCanonical": False},
    ]}
    markets = spot._parse_spot_meta(meta)
    try:
        spot._resolve_market("HYPE/USDC", markets)
    except spot.SpotInstrumentError as exc:
        assert exc.code == "INSTRUMENT_AMBIGUOUS"
    else:
        raise AssertionError("ambiguous pair was accepted")


def test_order_request_quantizes_size_and_price_without_perp_fallback() -> None:
    market = spot._resolve_market("HYPE/USDC", spot._parse_spot_meta(META))
    request = spot._build_order_request(market, "BUY", "1.239", "12.34567")
    assert request["coin"] == "@0"
    assert request["sz"] == 1.23
    # HYPE szDecimals=2 -> spot prices allow at most 8-2=6 decimals AND at
    # most 5 significant figures. 12.34567 carries 7 significant figures and
    # is rejected by the venue with a tick error, so the agent normalizes it
    # to the value the official SDK itself produces
    # (round(float(f"{px:.5g}"), 8 - szDecimals) -> 12.346).
    assert request["limit_px"] == 12.346
    assert request["order_type"] == {"limit": {"tif": "Gtc"}}
    assert request["reduce_only"] is False


def test_buy_rejects_insufficient_quote_balance_before_submission() -> None:
    with patch.object(spot, "_lookup_credentials", return_value=("0x" + "1" * 40, "secret")), \
         patch.object(spot, "_fetch_spot_meta", return_value=META), \
         patch.object(spot, "_fetch_spot_balances", return_value=[{"coin": "USDC", "total": "1", "hold": "0"}]), \
         patch.object(spot, "_build_exchange_client") as client:
        response = spot.execute({
            "operation": "new_order", "account": "demo", "side": "BUY",
            "symbol": "HYPE/USDC", "quantity": "1", "price": "12",
        })
    assert isinstance(response, CanonicalResponse)
    assert not response.success
    assert response.error.code == "INSUFFICIENT_BALANCE"
    client.assert_not_called()


def test_order_submission_is_not_attempted_for_invalid_spot_symbol() -> None:
    with patch.object(spot, "_lookup_credentials", return_value=("0x" + "1" * 40, "secret")), \
         patch.object(spot, "_fetch_spot_meta", return_value=META), \
         patch.object(spot, "_build_exchange_client") as client:
        response = spot.execute({
            "operation": "new_order", "account": "demo", "side": "BUY",
            "symbol": "BTC/USDC", "quantity": "1", "price": "2",
        })
    assert not response.success
    assert response.error.code == "INSTRUMENT_NOT_FOUND"
    client.assert_not_called()


def test_spot_capabilities_are_isolated_from_perpetual_agent() -> None:
    perp = importlib.import_module("plugins.trade.agents.x_hyperliquid_agent")
    assert "new_order" in spot.capabilities()
    assert spot.name == "hyperliquid"
    assert "positions_management" in perp.capabilities()
    assert "positions_management" not in spot.capabilities()


def test_spotdesk_accepts_requested_spot_agent_filename() -> None:
    assert _exchange_name_from_filename("x_hyperliquid_spot_agent.py") == "hyperliquid"
    assert _exchange_name_from_filename("x_hyperliquid_agent.py") is None
    desk = SpotDesk()
    assert "hyperliquid" in desk.list_exchanges()
