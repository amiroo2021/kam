"""Offline tests for the OndoPerps Fibo adapter — cumulative-counter model.

The adapter was refactored to the cumulative-protection contract:

    submit_volume_market_order  → POST new_order (market, non-reduce-only)
    confirm_cumulative_position → reads position_state; row size > 0?
    set_cumulative_sl            → POST set_sl (one leg, leaves TP)
    verify_cumulative_sl         → reads position_state; sl matches?
    set_cumulative_tp            → POST set_tp (one leg, leaves SL)
    verify_cumulative_tp         → reads position_state; tp matches?
    current_protection_state     → reads position_state; returns (sl, tp)
    remove_cumulative_tp         → no-op default (POSITION-SCOPING LIMITATION)
    cleanup_counters             → no-op default (cumulative model has no
                                   per-counter state to clean up)

NO exchange protocol details are duplicated. Every call goes through
``x_ondoperps_agent.execute(...)``.
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.fibo.adapters.ondoperps import OndoPerpsFiboAdapter
from plugins.trade.fibo.engine import (  # noqa: E402
    CounterType,
    FiboConfig,
    FiboEngine,
    FiboInstance,
    FiboManager,
    RealOrderSide,
)
from plugins.trade.fibo.quote import Quote, QuoteSource  # noqa: E402
from plugins.trade.fibo.quote_ondoperps import OndoPerpsQuoteSource  # noqa: E402
import plugins.trade.agents.x_ondoperps_agent as real_ondo  # noqa: E402


# =========================================================================
# Fakes
# =========================================================================


def _ok(operation: str, exchange: str = "ondoperps", account: str = "amiroo",
        **fields: Any) -> Dict[str, Any]:
    response: Dict[str, Any] = {
        "success": True,
        "operation": operation,
        "exchange": exchange,
        "account": account,
    }
    response.update(fields)
    return response


def _fail(operation: str, exchange: str = "ondoperps", account: str = "amiroo",
          code: str = "ERR", message: str = "") -> Dict[str, Any]:
    return {
        "success": False,
        "operation": operation,
        "exchange": exchange,
        "account": account,
        "error": {"code": code, "message": message},
    }


def _with_instrument_metadata(response: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    if not isinstance(response, dict) or response.get("operation") != "position_state":
        return response
    if response.get("instrument"):
        return response
    sym = str(symbol or "US100").upper()
    price_increment = "0.00001" if sym == "ONDO" else "0.01"
    size_increment = "1" if sym == "ONDO" else "0.01"
    enriched = dict(response)
    enriched["instrument"] = {
        "requested_symbol": sym,
        "symbol": sym,
        "display_name": f"{sym}-USD.P",
        "price_increment": price_increment,
        "size_increment": size_increment,
        "minimum_size": None,
    }
    return enriched


class FakeOndoPerpsAgent:
    """Records every execute(request) call and serves scripted responses.

    State-aware: tracks the latest new_order side per symbol so
    ``position_state`` returns a row whose side matches the most recent
    market order's intended side (mirroring OndoPerps behavior).
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.scripted: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._flat_symbols: set[str] = set()
        self.default_responses: Dict[str, Dict[str, Any]] = {
            "new_order": _ok(
                "new_order",
                order={
                    "symbol": "US100", "side": "buy",
                    "order_type": "market",
                    "requested_volume": "1.3",
                    "requested_price": None,
                    "submitted_volume": "1.3",
                    "submitted_price": None,
                    "verified": True, "status": "success",
                    "exchange_order_id": 10001,
                },
            ),
            "position_state": _ok(
                "position_state",
                positions=[{
                    "symbol": "US100", "side": "long", "size": "1.3",
                    "entry_price": "100", "pnl": "0",
                    "tp": None, "sl": None,
                    "tp_count": None, "sl_count": None,
                }],
                instrument={
                    "requested_symbol": "US100", "symbol": "US100",
                    "display_name": "US100-USD.P",
                    "price_increment": "0.01",
                    "size_increment": "0.01", "minimum_size": None,
                },
            ),
            "market_price": _ok(
                "market_price",
                market_price={
                    "requested_symbol": "US100",
                    "market": "US100-USD.P",
                    "price": "100.25",
                    "markPrice": "100.25",
                    "oraclePrice": "100.20",
                    "lastExternalPrice": "100.22",
                    "lastUpdatedTime": "1712345678",
                },
            ),
            "set_tp": _ok("set_tp"),
            "set_sl": _ok("set_sl"),
            "close_position": _ok(
                "close_position",
                position_action={
                    "operation": "close_position",
                    "symbol": "US100",
                    "verified": True,
                    "status": "success",
                    "current_side": "long",
                    "current_size": "1.3",
                },
            ),
            "positions_orders": _ok("positions_orders", positions=[]),
        }
        self._order_id_seq = 10000
        self._last_side: Dict[str, str] = {}
        self._lookup_by_client_id: Dict[str, List[Dict[str, Any]]] = {}

    def script_failure(self, operation: str, *, symbol: str = "US100",
                       code: str = "ERR", message: str = "") -> None:
        self.scripted[(operation, symbol)] = _fail(
            operation, code=code, message=message
        )

    def calls_for(self, operation: str) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c.get("operation") == operation]

    def script_client_lookup_sequence(self, client_order_id: str, responses: List[Dict[str, Any]]) -> None:
        self._lookup_by_client_id[client_order_id] = list(responses)

    def execute(self, request: Dict[str, Any]) -> Any:
        if not isinstance(request, dict):
            return _fail("", code="INVALID_REQUEST", message="Request must be a dict")
        recorded = {k: v for k, v in request.items() if not callable(v)}
        self.calls.append(recorded)
        op = str(request.get("operation") or "")
        symbol = str(request.get("symbol") or "")
        if op == "get_exact_order":
            client_order_id = str(request.get("client_order_id") or request.get("clientOrderId") or "")
            scripted_lookup = self._lookup_by_client_id.get(client_order_id)
            if scripted_lookup is not None:
                if scripted_lookup:
                    payload = scripted_lookup.pop(0)
                    if isinstance(payload, dict) and payload.get("success") and isinstance(payload.get("order"), dict):
                        order = dict(payload["order"])
                        if "exchange_order_id" not in order and "orderId" in order:
                            order["exchange_order_id"] = order.get("orderId")
                        if "order_type" not in order and "type" in order:
                            order["order_type"] = order.get("type")
                        if "submitted_volume" not in order and "size" in order:
                            order["submitted_volume"] = order.get("size")
                        if "submitted_price" not in order and "price" in order:
                            order["submitted_price"] = order.get("price")
                        payload = dict(payload)
                        payload["order"] = order
                    return payload
                return _fail("get_exact_order", code="ORDER_NOT_FOUND", message="not found")
            return _ok(
                "get_exact_order",
                order={
                    "symbol": symbol or "US100",
                    "side": str(request.get("side") or self._last_side.get(symbol or "US100") or "buy").lower(),
                    "order_type": "market",
                    "requested_volume": str(request.get("volume") or "1"),
                    "requested_price": None,
                    "submitted_volume": str(request.get("volume") or "1"),
                    "submitted_price": None,
                    "verified": True,
                    "status": "fullyfilled",
                    "exchange_order_id": self._order_id_seq,
                },
            )
        if op == "new_order":
            side = str(request.get("side") or "").lower()
            self._last_side[symbol or "US100"] = side
            self._flat_symbols.discard(symbol or "US100")
        if op == "close_position":
            self._flat_symbols.add(symbol or "US100")
        scripted = self.scripted.get((op, symbol)) or self.scripted.get((op, "*"))
        if scripted is not None:
            return scripted
        default = self.default_responses.get(op)
        if default is None:
            return _fail(op, code="NOT_IMPLEMENTED",
                         message=f"Fake has no default for {op!r}")
        if op == "new_order":
            self._order_id_seq += 1
            copy = {k: v for k, v in default.items() if k != "order"}
            order = {k: v for k, v in default["order"].items()}
            order["side"] = str(request.get("side") or order.get("side") or "buy")
            if "exchange_order_id" not in order:
                order["exchange_order_id"] = self._order_id_seq
            copy["order"] = order
            return copy
        if op == "position_state":
            if (symbol or "US100") in self._flat_symbols:
                copy = {k: v for k, v in default.items() if k != "positions"}
                copy["positions"] = []
                return copy
            last_side = self._last_side.get(symbol or "US100", "buy")
            canonical = "long" if last_side == "buy" else "short"
            copy = {k: v for k, v in default.items() if k != "positions"}
            copy["positions"] = [{
                "symbol": symbol or "US100",
                "side": canonical, "size": "1.3",
                "entry_price": "100", "pnl": "0",
                "tp": None, "sl": None,
                "tp_count": None, "sl_count": None,
            }]
            return copy
        return default


class SequencedPositionAgent(FakeOndoPerpsAgent):
    """Fake agent whose position_state visibility can lag behind fills."""

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        super().__init__()
        self._position_sequence = list(responses)

    def execute(self, request: Dict[str, Any]) -> Any:
        if not isinstance(request, dict):
            return _fail("", code="INVALID_REQUEST", message="Request must be a dict")
        op = str(request.get("operation") or "")
        if op == "position_state" and self._position_sequence:
            recorded = {k: v for k, v in request.items() if not callable(v)}
            self.calls.append(recorded)
            return _with_instrument_metadata(self._position_sequence.pop(0), request.get("symbol") or "US100")
        return super().execute(request)


class ProtectionStateSequenceAgent(FakeOndoPerpsAgent):
    """Fake agent whose position_state returns a scripted SL/TP sequence."""

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        super().__init__()
        self._position_sequence = list(responses)

    def execute(self, request: Dict[str, Any]) -> Any:
        if not isinstance(request, dict):
            return _fail("", code="INVALID_REQUEST", message="Request must be a dict")
        op = str(request.get("operation") or "")
        if op == "position_state" and self._position_sequence:
            recorded = {k: v for k, v in request.items() if not callable(v)}
            self.calls.append(recorded)
            return _with_instrument_metadata(self._position_sequence.pop(0), request.get("symbol") or "US100")
        return super().execute(request)


class OndoAgentMarketPriceTests(unittest.TestCase):

    def setUp(self) -> None:
        self.p_lookup = mock.patch.object(real_ondo, "_lookup_credentials", return_value={"api_key": "x"})
        self.p_resolve = mock.patch.object(
            real_ondo,
            "_resolve_market_metadata",
            return_value=({
                "market": "US100-USD.P",
                "quote_increment": Decimal("0.01"),
                "base_increment": Decimal("0.01"),
            }, None),
        )
        self.p_lookup.start()
        self.p_resolve.start()
        self.addCleanup(self.p_lookup.stop)
        self.addCleanup(self.p_resolve.stop)

    def test_capabilities_advertise_market_price(self):
        self.assertIn("market_price", real_ondo.capabilities())

    def test_market_price_reads_mark_prices_endpoint(self):
        seen = []
        def fake_get(credentials, path):
            seen.append(path)
            return {
                "success": True,
                "result": {
                    "US100-USD.P": {
                        "price": "100.25",
                        "markPrice": "100.25",
                        "oraclePrice": "100.20",
                        "lastExternalPrice": "100.22",
                        "lastUpdatedTime": "1712345678",
                    }
                },
            }
        with mock.patch.object(real_ondo, "_signed_get", side_effect=fake_get):
            resp = real_ondo.execute({
                "operation": "market_price",
                "exchange": "ondoperps",
                "account": "amiroo",
                "symbol": "US100",
            })
        self.assertTrue(resp.success)
        self.assertIn("/v1/perps/mark_prices", seen)
        payload = getattr(resp, "market_price", None) or getattr(resp, "extra", None)
        self.assertIsNotNone(payload)

    def test_market_price_works_without_position_state(self):
        def fake_get(credentials, path):
            if path == "/v1/perps/mark_prices":
                return {
                    "success": True,
                    "result": {
                        "SOL-USD.P": {
                            "price": "150.1",
                            "markPrice": "150.2",
                            "oraclePrice": "150.0",
                            "lastExternalPrice": "150.05",
                            "lastUpdatedTime": "1712349999",
                        }
                    },
                }
            raise AssertionError(f"unexpected path {path}")
        with mock.patch.object(
            real_ondo, "_resolve_market_metadata",
            return_value=({"market": "SOL-USD.P", "quote_increment": Decimal("0.01"), "base_increment": Decimal("0.1")}, None),
        ), mock.patch.object(real_ondo, "_signed_get", side_effect=fake_get):
            resp = real_ondo.execute({
                "operation": "market_price",
                "exchange": "ondoperps",
                "account": "amiroo",
                "symbol": "SOL",
            })
        self.assertTrue(resp.success)

    def test_market_price_missing_market_returns_failure(self):
        with mock.patch.object(real_ondo, "_signed_get", return_value={"success": True, "result": {}}):
            resp = real_ondo.execute({
                "operation": "market_price",
                "exchange": "ondoperps",
                "account": "amiroo",
                "symbol": "US100",
            })
        self.assertFalse(resp.success)


class FakeQuoteSource:
    def __init__(self, quotes_by_symbol: Optional[Dict[str, List[Quote]]] = None):
        self._by_symbol: Dict[str, List[Quote]] = {}
        for sym, lst in (quotes_by_symbol or {}).items():
            self._by_symbol[sym] = list(lst)

    def push(self, symbol: str, quote: Quote) -> None:
        self._by_symbol.setdefault(symbol, []).append(quote)

    def current_bid_ask(self, symbol: str) -> Quote:
        lst = self._by_symbol.get(symbol)
        if not lst:
            raise LookupError(f"no quote available for {symbol}")
        return lst.pop(0)


# =========================================================================
# Adapter wiring
# =========================================================================


class AdapterWiringTests(unittest.TestCase):

    def setUp(self) -> None:
        self.agent = FakeOndoPerpsAgent()
        self.adapter = OndoPerpsFiboAdapter(
            exchange_name="ondoperps",
            account_alias="amiroo",
            agent=self.agent,
        )

    def test_submit_emits_client_order_and_exact_verify_events(self):
        events: List[Dict[str, Any]] = []
        self.adapter.set_event_sink(events.append)
        oid = self.adapter.submit_volume_market_order(
            instance_key="ondoperps:amiroo:US100:counterSELL",
            instrument="US100",
            side=RealOrderSide.SELL,
            counter_step=1,
            volume=1,
        )
        self.assertEqual(oid, "10001")
        names = [e["event"] for e in events]
        self.assertIn("client_order_id_prepared", names)
        self.assertIn("order_create_response", names)
        self.assertIn("exact_order_verified", names)

    def test_submit_with_inline_order_id_still_uses_exact_verification_successfully(self):
        self.adapter._run_id_by_instance["ondoperps:amiroo:US100:counterSELL"] = "A7K9"
        client_id = "FIBO_amiroo_US100_CS_A7K9_Y1_C1"
        self.agent.script_client_lookup_sequence(client_id, [{
            "success": True,
            "order": {
                "orderId": "OID-123",
                "clientOrderId": client_id,
                "market": "US100-USD.P",
                "side": "sell",
                "size": "1",
                "filledSize": "1",
                "status": "fullyfilled",
            },
        }])
        oid = self.adapter.submit_volume_market_order(
            instance_key="ondoperps:amiroo:US100:counterSELL",
            instrument="US100", side=RealOrderSide.SELL, counter_step=1, volume=1,
        )
        self.assertEqual(oid, "OID-123")
        self.assertEqual(len(self.agent.calls_for("new_order")), 1)
        self.assertEqual(len(self.agent.calls_for("get_exact_order")), 1)

    def test_submit_does_not_fail_when_inline_order_id_missing_but_lookup_finds_order(self):
        self.adapter._run_id_by_instance["ondoperps:amiroo:US100:counterSELL"] = "A7K9"
        client_id = "FIBO_amiroo_US100_CS_A7K9_Y1_C1"
        self.agent.default_responses["new_order"] = _ok(
            "new_order",
            order={
                "symbol": "US100", "side": "sell", "order_type": "market",
                "requested_volume": "1", "requested_price": None,
                "submitted_volume": "1", "submitted_price": None,
                "verified": True, "status": "success",
                "exchange_order_id": None,
            },
        )
        self.agent.script_client_lookup_sequence(client_id, [{
            "success": True,
            "order": {
                "orderId": "OID-456",
                "clientOrderId": client_id,
                "market": "US100-USD.P",
                "side": "sell",
                "size": "1",
                "filledSize": "1",
                "status": "fullyfilled",
            },
        }])
        oid = self.adapter.submit_volume_market_order(
            instance_key="ondoperps:amiroo:US100:counterSELL",
            instrument="US100", side=RealOrderSide.SELL, counter_step=1, volume=1,
        )
        self.assertEqual(oid, "OID-456")
        self.assertEqual(len(self.agent.calls_for("new_order")), 1)
        self.assertEqual(len(self.agent.calls_for("get_exact_order")), 1)

    def test_submit_retries_lookup_without_duplicate_submit(self):
        self.adapter._run_id_by_instance["ondoperps:amiroo:US100:counterSELL"] = "A7K9"
        self.adapter._sleep_fn = lambda *_args, **_kwargs: None
        client_id = "FIBO_amiroo_US100_CS_A7K9_Y1_C1"
        self.agent.default_responses["new_order"] = _ok(
            "new_order",
            order={
                "symbol": "US100", "side": "sell", "order_type": "market",
                "requested_volume": "1", "requested_price": None,
                "submitted_volume": "1", "submitted_price": None,
                "verified": True, "status": "success",
                "exchange_order_id": None,
            },
        )
        self.agent.script_client_lookup_sequence(client_id, [
            _fail("get_exact_order", code="ORDER_NOT_FOUND", message="not found"),
            {"success": True, "order": {
                "orderId": "OID-789",
                "clientOrderId": client_id,
                "market": "US100-USD.P",
                "side": "sell",
                "size": "1",
                "filledSize": "1",
                "status": "fullyfilled",
            }},
        ])
        oid = self.adapter.submit_volume_market_order(
            instance_key="ondoperps:amiroo:US100:counterSELL",
            instrument="US100", side=RealOrderSide.SELL, counter_step=1, volume=1,
        )
        self.assertEqual(oid, "OID-789")
        self.assertEqual(len(self.agent.calls_for("new_order")), 1)
        self.assertEqual(len(self.agent.calls_for("get_exact_order")), 2)

    def test_submit_missing_inline_order_id_and_lookup_timeout_raises_order_verify_failed(self):
        self.adapter._run_id_by_instance["ondoperps:amiroo:US100:counterSELL"] = "A7K9"
        self.adapter._sleep_fn = lambda *_args, **_kwargs: None
        client_id = "FIBO_amiroo_US100_CS_A7K9_Y1_C1"
        self.agent.default_responses["new_order"] = _ok(
            "new_order",
            order={
                "symbol": "US100", "side": "sell", "order_type": "market",
                "requested_volume": "1", "requested_price": None,
                "submitted_volume": "1", "submitted_price": None,
                "verified": True, "status": "success",
                "exchange_order_id": None,
            },
        )
        self.agent.script_client_lookup_sequence(client_id, [
            _fail("get_exact_order", code="ORDER_NOT_FOUND", message="not found")
            for _ in range(5)
        ])
        with self.assertRaises(RuntimeError) as ctx:
            self.adapter.submit_volume_market_order(
                instance_key="ondoperps:amiroo:US100:counterSELL",
                instrument="US100", side=RealOrderSide.SELL, counter_step=1, volume=1,
            )
        self.assertIn("ORDER_VERIFY_FAILED", str(ctx.exception))
        self.assertEqual(len(self.agent.calls_for("new_order")), 1)
        self.assertEqual(len(self.agent.calls_for("get_exact_order")), 5)

    def test_submit_captures_order_id_from_lookup_when_create_omits_it(self):
        self.adapter._run_id_by_instance["ondoperps:amiroo:US100:counterSELL"] = "A7K9"
        client_id = "FIBO_amiroo_US100_CS_A7K9_Y1_C1"
        self.agent.default_responses["new_order"] = _ok(
            "new_order",
            order={
                "symbol": "US100", "side": "sell", "order_type": "market",
                "requested_volume": "1", "requested_price": None,
                "submitted_volume": "1", "submitted_price": None,
                "verified": True, "status": "success",
                "exchange_order_id": None,
            },
        )
        self.agent.script_client_lookup_sequence(client_id, [{
            "success": True,
            "order": {
                "orderId": "OID-999",
                "clientOrderId": client_id,
                "market": "US100-USD.P",
                "side": "sell",
                "size": "1",
                "filledSize": "1",
                "status": "fullyfilled",
            },
        }])
        oid = self.adapter.submit_volume_market_order(
            instance_key="ondoperps:amiroo:US100:counterSELL",
            instrument="US100", side=RealOrderSide.SELL, counter_step=1, volume=1,
        )
        self.assertEqual(oid, "OID-999")
        self.assertEqual(
            self.adapter._last_submit_by_instance["ondoperps:amiroo:US100:counterSELL"]["exchange_order_id"],
            "OID-999",
        )

    def test_submit_wrong_lookup_market_side_or_size_fails_exact_verification(self):
        self.adapter._run_id_by_instance["ondoperps:amiroo:US100:counterSELL"] = "A7K9"
        self.adapter._sleep_fn = lambda *_args, **_kwargs: None
        client_id = "FIBO_amiroo_US100_CS_A7K9_Y1_C1"
        self.agent.default_responses["new_order"] = _ok(
            "new_order",
            order={
                "symbol": "US100", "side": "sell", "order_type": "market",
                "requested_volume": "1", "requested_price": None,
                "submitted_volume": "1", "submitted_price": None,
                "verified": True, "status": "success",
                "exchange_order_id": None,
            },
        )
        self.agent.script_client_lookup_sequence(client_id, [
            _fail("get_exact_order", code="ORDER_VERIFY_FAILED", message="wrong market/side/size"),
            _fail("get_exact_order", code="ORDER_VERIFY_FAILED", message="wrong market/side/size"),
            _fail("get_exact_order", code="ORDER_VERIFY_FAILED", message="wrong market/side/size"),
        ])
        with self.assertRaises(RuntimeError) as ctx:
            self.adapter.submit_volume_market_order(
                instance_key="ondoperps:amiroo:US100:counterSELL",
                instrument="US100", side=RealOrderSide.SELL, counter_step=1, volume=1,
            )
        self.assertIn("ORDER_VERIFY_FAILED", str(ctx.exception))

    def test_submit_logs_absent_inline_id_and_lookup_progress(self):
        events: List[Dict[str, Any]] = []
        self.adapter.set_event_sink(events.append)
        self.adapter._run_id_by_instance["ondoperps:amiroo:US100:counterSELL"] = "A7K9"
        self.adapter._sleep_fn = lambda *_args, **_kwargs: None
        client_id = "FIBO_amiroo_US100_CS_A7K9_Y1_C1"
        self.agent.default_responses["new_order"] = _ok(
            "new_order",
            order={
                "symbol": "US100", "side": "sell", "order_type": "market",
                "requested_volume": "1", "requested_price": None,
                "submitted_volume": "1", "submitted_price": None,
                "verified": True, "status": "success",
                "exchange_order_id": None,
            },
        )
        self.agent.script_client_lookup_sequence(client_id, [
            _fail("get_exact_order", code="ORDER_NOT_FOUND", message="not found"),
            {"success": True, "order": {
                "orderId": "OID-LOG",
                "clientOrderId": client_id,
                "market": "US100-USD.P",
                "side": "sell",
                "size": "1",
                "filledSize": "1",
                "status": "fullyfilled",
            }},
        ])
        self.adapter.submit_volume_market_order(
            instance_key="ondoperps:amiroo:US100:counterSELL",
            instrument="US100", side=RealOrderSide.SELL, counter_step=1, volume=1,
        )
        names = [e["event"] for e in events]
        self.assertIn("post_submit_success", names)
        self.assertIn("inline_order_id_absent", names)
        self.assertIn("client_lookup_attempt", names)
        self.assertIn("exact_order_verified", names)

    def test_submit_post_rejection_keeps_market_submit_failed_classification(self):
        self.agent.script_failure("new_order", code="POST_REJECTED", message="reject")
        with self.assertRaises(RuntimeError) as ctx:
            self.adapter.submit_volume_market_order(
                instance_key="ondoperps:amiroo:US100:counterBUY",
                instrument="US100", side=RealOrderSide.BUY, counter_step=1, volume=1.3,
            )
        self.assertIn("POST_REJECTED", str(ctx.exception))

    def test_confirm_emits_each_position_confirmation_attempt(self):
        events: List[Dict[str, Any]] = []
        agent = SequencedPositionAgent([
            _ok("position_state", positions=[]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0",
                "tp": None, "sl": None,
                "tp_count": None, "sl_count": None,
            }]),
        ])
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        adapter.set_event_sink(events.append)
        ok = adapter.confirm_cumulative_position(
            instance_key="ondoperps:amiroo:US100:counterSELL",
            instrument="US100",
            side=RealOrderSide.SELL,
            expected_size=1,
        )
        self.assertTrue(ok)
        attempts = [e for e in events if e["event"] == "position_confirmation_attempt"]
        self.assertEqual(len(attempts), 2)
        self.assertFalse(attempts[0]["overall_result"])
        self.assertTrue(attempts[1]["overall_result"])

    def test_set_sl_emits_response_event(self):
        events: List[Dict[str, Any]] = []
        self.adapter.set_event_sink(events.append)
        ok = self.adapter.set_cumulative_sl(
            instance_key="ondoperps:amiroo:US100:counterSELL",
            instrument="US100",
            side=RealOrderSide.SELL,
            sl_price=99.5,
        )
        self.assertTrue(ok)
        sl_events = [e for e in events if e["event"] == "sl_set_response"]
        self.assertEqual(len(sl_events), 1)
        self.assertTrue(sl_events[0]["response_success"])

    def test_verify_sl_uses_quantized_expected_value(self):
        agent = ProtectionStateSequenceAgent([
            _ok("position_state", positions=[{
                "symbol": "ONDO", "side": "short", "size": "1",
                "entry_price": "0.32934", "pnl": "0",
                "tp": None, "sl": "0.32990",
                "tp_count": None, "sl_count": 1,
            }]),
        ])
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        adapter._sleep_fn = lambda *_args, **_kwargs: None
        ok = adapter.verify_cumulative_sl(
            instance_key="ondoperps:amiroo:ONDO:counterSELL",
            instrument="ONDO",
            side=RealOrderSide.SELL,
            sl_price=0.3298977966,
        )
        self.assertTrue(ok)

    def test_verify_sl_retries_until_quantized_value_appears_without_duplicate_set(self):
        agent = ProtectionStateSequenceAgent([
            _ok("position_state", positions=[{
                "symbol": "ONDO", "side": "short", "size": "1",
                "entry_price": "0.32934", "pnl": "0",
                "tp": None, "sl": "0.33001",
                "tp_count": None, "sl_count": 1,
            }]),
            _ok("position_state", positions=[{
                "symbol": "ONDO", "side": "short", "size": "1",
                "entry_price": "0.32934", "pnl": "0",
                "tp": None, "sl": "0.32990",
                "tp_count": None, "sl_count": 1,
            }]),
        ])
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        adapter._sleep_fn = lambda *_args, **_kwargs: None
        ok = adapter.verify_cumulative_sl(
            instance_key="ondoperps:amiroo:ONDO:counterSELL",
            instrument="ONDO",
            side=RealOrderSide.SELL,
            sl_price=0.3298977966,
        )
        self.assertTrue(ok)
        self.assertEqual(len(agent.calls_for("position_state")), 2)
        self.assertEqual(agent.calls_for("set_sl"), [])

    def test_verify_sl_fails_after_bounded_retry_when_value_never_updates(self):
        agent = ProtectionStateSequenceAgent([
            _ok("position_state", positions=[{
                "symbol": "ONDO", "side": "short", "size": "1",
                "entry_price": "0.32934", "pnl": "0",
                "tp": None, "sl": "0.33001",
                "tp_count": None, "sl_count": 1,
            }]),
            _ok("position_state", positions=[{
                "symbol": "ONDO", "side": "short", "size": "1",
                "entry_price": "0.32934", "pnl": "0",
                "tp": None, "sl": "0.33001",
                "tp_count": None, "sl_count": 1,
            }]),
            _ok("position_state", positions=[{
                "symbol": "ONDO", "side": "short", "size": "1",
                "entry_price": "0.32934", "pnl": "0",
                "tp": None, "sl": "0.33001",
                "tp_count": None, "sl_count": 1,
            }]),
            _ok("position_state", positions=[{
                "symbol": "ONDO", "side": "short", "size": "1",
                "entry_price": "0.32934", "pnl": "0",
                "tp": None, "sl": "0.33001",
                "tp_count": None, "sl_count": 1,
            }]),
            _ok("position_state", positions=[{
                "symbol": "ONDO", "side": "short", "size": "1",
                "entry_price": "0.32934", "pnl": "0",
                "tp": None, "sl": "0.33001",
                "tp_count": None, "sl_count": 1,
            }]),
        ])
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        adapter._sleep_fn = lambda *_args, **_kwargs: None
        ok = adapter.verify_cumulative_sl(
            instance_key="ondoperps:amiroo:ONDO:counterSELL",
            instrument="ONDO",
            side=RealOrderSide.SELL,
            sl_price=0.3298977966,
        )
        self.assertFalse(ok)
        self.assertEqual(len(agent.calls_for("position_state")), 5)
        self.assertEqual(agent.calls_for("set_sl"), [])

    def test_verify_tp_uses_quantized_expected_value_with_retry(self):
        agent = ProtectionStateSequenceAgent([
            _ok("position_state", positions=[{
                "symbol": "ONDO", "side": "short", "size": "1",
                "entry_price": "0.32934", "pnl": "0",
                "tp": "0.32910", "sl": "0.32990",
                "tp_count": 1, "sl_count": 1,
            }]),
            _ok("position_state", positions=[{
                "symbol": "ONDO", "side": "short", "size": "1",
                "entry_price": "0.32934", "pnl": "0",
                "tp": "0.32911", "sl": "0.32990",
                "tp_count": 1, "sl_count": 1,
            }]),
        ])
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        adapter._sleep_fn = lambda *_args, **_kwargs: None
        ok = adapter.verify_cumulative_tp(
            instance_key="ondoperps:amiroo:ONDO:counterSELL",
            instrument="ONDO",
            side=RealOrderSide.SELL,
            tp_price=0.3291098765,
        )
        self.assertTrue(ok)
        self.assertEqual(len(agent.calls_for("position_state")), 2)
        self.assertEqual(agent.calls_for("set_tp"), [])

    def test_adapter_does_not_contain_exchange_protocol(self):
        """Architecture rule: no parallel exchange protocol code.

        We scan the adapter module's actual Python AST for the names that
        would imply parallel HTTP / signing code. Mentions in docstrings
        are allowed; we're checking that the adapter is NOT a second
        Ondo implementation.
        """
        import ast
        from plugins.trade.fibo.adapters import ondoperps as adapter_module
        source = adapter_module.__file__
        with open(source, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        forbidden_calls = {
            "urllib", "requests", "hmac", "hashlib",
            "_signed_post", "_signed_get",
            "_signed_delete", "_signed_request",
            "_lookup_credentials", "_resolve_market_metadata",
            "_fetch_positions_snapshot", "_set_position_trigger",
        }
        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    target = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    target = node.func.attr
            elif isinstance(node, ast.Attribute):
                target = node.attr
            elif isinstance(node, ast.Name):
                target = node.id
            if target in forbidden_calls:
                self.fail(
                    f"adapter calls forbidden exchange-protocol symbol "
                    f"{target!r} — that belongs to x_ondoperps_agent"
                )

    def test_submit_volume_market_order_forwards_correct_shape(self):
        self.adapter._run_id_by_instance["ondoperps:amiroo:US100:counterBUY"] = "A7K9"
        oid = self.adapter.submit_volume_market_order(
            instance_key="ondoperps:amiroo:US100:counterBUY",
            instrument="US100", side=RealOrderSide.BUY,
            counter_step=1, volume=1.3,
        )
        self.assertEqual(oid, "10001")
        req = self.agent.calls[0]
        self.assertEqual(req["operation"], "new_order")
        self.assertEqual(req["exchange"], "ondoperps")
        self.assertEqual(req["account"], "amiroo")
        self.assertEqual(req["symbol"], "US100")
        self.assertEqual(req["side"], "buy")
        self.assertEqual(req["order_type"], "market")
        self.assertEqual(req["reduce_only"], False)  # non-reduce-only
        self.assertEqual(req["volume"], "1.3")
        self.assertEqual(req["client_order_id"], "FIBO_amiroo_US100_CB_A7K9_Y1_C1")

    def test_client_order_ids_are_unique_across_levels_and_cycles(self):
        self.adapter._run_id_by_instance["ondoperps:amiroo:US100:counterSELL"] = "A7K9"
        ids = []
        for step in (1, 2, 3, 4, 1, 2):
            self.adapter.submit_volume_market_order(
                instance_key="ondoperps:amiroo:US100:counterSELL",
                instrument="US100",
                side=RealOrderSide.SELL,
                counter_step=step,
                volume=1.0,
            )
            ids.append(self.agent.calls[-1]["client_order_id"])
        self.assertEqual(ids, [
            "FIBO_amiroo_US100_CS_A7K9_Y1_C1",
            "FIBO_amiroo_US100_CS_A7K9_Y1_C2",
            "FIBO_amiroo_US100_CS_A7K9_Y1_C3",
            "FIBO_amiroo_US100_CS_A7K9_Y1_C4",
            "FIBO_amiroo_US100_CS_A7K9_Y2_C1",
            "FIBO_amiroo_US100_CS_A7K9_Y2_C2",
        ])
        self.assertEqual(len(set(ids)), len(ids))

    def test_two_explicit_registration_starts_get_different_run_ids(self):
        cfg = FiboConfig(
            exchange="ondoperps", account="amiroo", instrument="US100",
            counter_type=CounterType.COUNTER_SELL,
            divide_percent=1000, counter1=1, counter2=0, counter3=0, counter4=0,
        )
        manager = FiboManager()
        with mock.patch.object(self.adapter, "_new_run_id", side_effect=["A7K9", "M4P2"]):
            manager.start(cfg, self.adapter, FakeQuoteSource())
            first = self.adapter._client_order_id_for(
                instance_key=cfg.key, instrument="US100", side=RealOrderSide.SELL, counter_step=1,
            )
            manager.stop(cfg.key)
            manager.start(cfg, self.adapter, FakeQuoteSource())
            second = self.adapter._client_order_id_for(
                instance_key=cfg.key, instrument="US100", side=RealOrderSide.SELL, counter_step=1,
            )
        self.assertEqual(first, "FIBO_amiroo_US100_CS_A7K9_Y1_C1")
        self.assertEqual(second, "FIBO_amiroo_US100_CS_M4P2_Y1_C1")
        self.assertNotEqual(first, second)

    def test_long_names_are_shortened_safely_and_stay_under_64(self):
        long_account = "amiroo_super_long_account_name_for_fibo"
        long_instrument = "VERY_LONG_INSTRUMENT_NAME_WITH_EXTRA_SEGMENTS"
        agent = FakeOndoPerpsAgent()
        adapter = OndoPerpsFiboAdapter("ondoperps", long_account, agent)
        key = f"ondoperps:{long_account}:{long_instrument}:counterSELL"
        adapter._run_id_by_instance[key] = "A7K9"
        cid = adapter._client_order_id_for(
            instance_key=key,
            instrument=long_instrument,
            side=RealOrderSide.SELL,
            counter_step=4,
        )
        self.assertLessEqual(len(cid), 64)
        self.assertRegex(cid, r"^[A-Za-z0-9_-]+$")
        self.assertIn("_CS_", cid)
        self.assertTrue(cid.endswith("_A7K9_Y1_C4"))

    def test_shortening_hash_keeps_different_registration_keys_distinct(self):
        account = "amiroo_super_long_account_name_for_fibo"
        inst_a = "VERY_LONG_INSTRUMENT_NAME_WITH_EXTRA_SEGMENTS_ALPHA"
        inst_b = "VERY_LONG_INSTRUMENT_NAME_WITH_EXTRA_SEGMENTS_BETA"
        agent = FakeOndoPerpsAgent()
        adapter = OndoPerpsFiboAdapter("ondoperps", account, agent)
        key_a = f"ondoperps:{account}:{inst_a}:counterSELL"
        key_b = f"ondoperps:{account}:{inst_b}:counterSELL"
        adapter._run_id_by_instance[key_a] = "A7K9"
        adapter._run_id_by_instance[key_b] = "A7K9"
        cid_a = adapter._client_order_id_for(instance_key=key_a, instrument=inst_a, side=RealOrderSide.SELL, counter_step=1)
        cid_b = adapter._client_order_id_for(instance_key=key_b, instrument=inst_b, side=RealOrderSide.SELL, counter_step=1)
        self.assertNotEqual(cid_a, cid_b)

    def test_position_confirmation_still_follows_successful_submit(self):
        self.adapter._run_id_by_instance["ondoperps:amiroo:US100:counterSELL"] = "A7K9"
        self.adapter.submit_volume_market_order(
            instance_key="ondoperps:amiroo:US100:counterSELL",
            instrument="US100", side=RealOrderSide.SELL, counter_step=1, volume=1.0,
        )
        ok = self.adapter.confirm_cumulative_position(
            instance_key="ondoperps:amiroo:US100:counterSELL",
            instrument="US100", side=RealOrderSide.SELL,
        )
        self.assertTrue(ok)
        self.assertEqual(
            [c["operation"] for c in self.agent.calls[:3]],
            ["new_order", "get_exact_order", "position_state"],
        )

    def test_account_alias_is_forwarded(self):
        self.adapter.account_alias = "bitget"
        self.adapter.submit_volume_market_order(
            instance_key="ondoperps:bitget:US100:counterBUY",
            instrument="US100", side=RealOrderSide.BUY,
            counter_step=1, volume=1.3,
        )
        self.assertEqual(self.agent.calls[0]["account"], "bitget")

    def test_submit_failure_raises_runtime_error_with_code(self):
        self.agent.script_failure("new_order", code="INSUFFICIENT_BALANCE",
                                  message="not enough collateral")
        with self.assertRaises(RuntimeError) as ctx:
            self.adapter.submit_volume_market_order(
                instance_key="ondoperps:amiroo:US100:counterBUY",
                instrument="US100", side=RealOrderSide.BUY,
                counter_step=1, volume=1.3,
            )
        self.assertIn("INSUFFICIENT_BALANCE", str(ctx.exception))

    def test_confirm_cumulative_position_uses_position_state(self):
        ok = self.adapter.confirm_cumulative_position(
            instance_key="ondoperps:amiroo:US100:counterBUY",
            instrument="US100", side=RealOrderSide.BUY,
        )
        self.assertTrue(ok)
        self.assertEqual(self.agent.calls[0]["operation"], "position_state")

    def test_confirm_counterSELL_maps_sell_to_short(self):
        self.agent.scripted[("position_state", "US100")] = _ok(
            "position_state",
            positions=[{
                "symbol": "US100", "side": "short",
                "size": "1", "entry_price": "100", "pnl": "0",
                "tp": None, "sl": None,
                "tp_count": None, "sl_count": None,
            }],
        )
        ok = self.adapter.confirm_cumulative_position(
            instance_key="ondoperps:amiroo:US100:counterSELL",
            instrument="US100", side=RealOrderSide.SELL,
        )
        self.assertTrue(ok)

    def test_confirm_retries_when_first_position_read_is_empty(self):
        agent = SequencedPositionAgent([
            _ok("position_state", positions=[]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0",
                "tp": None, "sl": None,
                "tp_count": None, "sl_count": None,
            }]),
        ])
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        ok = adapter.confirm_cumulative_position(
            instance_key="ondoperps:amiroo:US100:counterSELL",
            instrument="US100", side=RealOrderSide.SELL,
        )
        self.assertTrue(ok)
        self.assertEqual(len(agent.calls_for("position_state")), 2)

    def test_confirm_retries_through_several_stale_reads(self):
        agent = SequencedPositionAgent([
            _ok("position_state", positions=[]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "0",
                "entry_price": "100", "pnl": "0",
                "tp": None, "sl": None,
                "tp_count": None, "sl_count": None,
            }]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0",
                "tp": None, "sl": None,
                "tp_count": None, "sl_count": None,
            }]),
        ])
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        ok = adapter.confirm_cumulative_position(
            instance_key="ondoperps:amiroo:US100:counterSELL",
            instrument="US100", side=RealOrderSide.SELL,
        )
        self.assertTrue(ok)
        self.assertEqual(len(agent.calls_for("position_state")), 3)

    def test_confirm_returns_false_when_no_position(self):
        self.agent.scripted[("position_state", "US100")] = _ok(
            "position_state", positions=[]
        )
        ok = self.adapter.confirm_cumulative_position(
            instance_key="ondoperps:amiroo:US100:counterBUY",
            instrument="US100", side=RealOrderSide.BUY,
        )
        self.assertFalse(ok)

    def test_confirm_returns_false_on_wrong_side(self):
        self.agent.scripted[("position_state", "US100")] = _ok(
            "position_state",
            positions=[{
                "symbol": "US100", "side": "short",  # opposite side
                "size": "1.3", "entry_price": "100", "pnl": "0",
                "tp": None, "sl": None,
                "tp_count": None, "sl_count": None,
            }],
        )
        ok = self.adapter.confirm_cumulative_position(
            instance_key="ondoperps:amiroo:US100:counterBUY",
            instrument="US100", side=RealOrderSide.BUY,
        )
        self.assertFalse(ok)

    def test_confirm_returns_false_on_wrong_symbol(self):
        self.agent.scripted[("position_state", "US100")] = _ok(
            "position_state",
            positions=[{
                "symbol": "US500", "side": "short",
                "size": "1", "entry_price": "100", "pnl": "0",
                "tp": None, "sl": None,
                "tp_count": None, "sl_count": None,
            }],
        )
        ok = self.adapter.confirm_cumulative_position(
            instance_key="ondoperps:amiroo:US100:counterSELL",
            instrument="US100", side=RealOrderSide.SELL,
        )
        self.assertFalse(ok)

    def test_confirm_returns_false_on_insufficient_size(self):
        self.agent.scripted[("position_state", "US100")] = _ok(
            "position_state",
            positions=[{
                "symbol": "US100", "side": "short",
                "size": "0", "entry_price": "100", "pnl": "0",
                "tp": None, "sl": None,
                "tp_count": None, "sl_count": None,
            }],
        )
        ok = self.adapter.confirm_cumulative_position(
            instance_key="ondoperps:amiroo:US100:counterSELL",
            instrument="US100", side=RealOrderSide.SELL,
        )
        self.assertFalse(ok)

    def test_set_sl_forwards_to_set_sl_op(self):
        self.adapter.set_cumulative_sl(
            instance_key="k", instrument="US100",
            side=RealOrderSide.BUY, sl_price=99.5,
        )
        self.assertEqual(self.agent.calls[0]["operation"], "set_sl")
        self.assertEqual(self.agent.calls[0]["symbol"], "US100")
        self.assertEqual(self.agent.calls[0]["price"], "99.5")

    def test_set_tp_forwards_to_set_tp_op(self):
        self.adapter.set_cumulative_tp(
            instance_key="k", instrument="US100",
            side=RealOrderSide.BUY, tp_price=100.5,
        )
        self.assertEqual(self.agent.calls[0]["operation"], "set_tp")
        self.assertEqual(self.agent.calls[0]["price"], "100.5")

    def test_cleanup_counters_closes_position_and_removes_tp_sl(self):
        self.adapter.cleanup_counters(
            instance_key="ondoperps:amiroo:US100:counterBUY",
            instrument="US100",
        )
        ops = [c["operation"] for c in self.agent.calls]
        self.assertEqual(ops, [
            "close_position",
            "set_tp",
            "set_sl",
            "position_state",
        ])
        self.assertEqual(self.agent.calls[1]["price"], "0")
        self.assertEqual(self.agent.calls[2]["price"], "0")

    def test_remove_cumulative_tp_is_noop_default(self):
        ok = self.adapter.remove_cumulative_tp(
            instance_key="k", instrument="US100",
            side=RealOrderSide.BUY,
        )
        self.assertFalse(ok)
        self.assertEqual(self.agent.calls, [])


# =========================================================================
# Step math (unchanged) + Counter1..4 directional relationships
# =========================================================================


class StepMathTests(unittest.TestCase):
    def test_counterBUY_step0_through_step5_descend(self):
        from plugins.trade.fibo.engine import step_price
        cfg = FiboConfig(exchange="ondoperps", account="amiroo", instrument="US100",
                         counter_type=CounterType.COUNTER_BUY)
        FiboInstance(cfg)
        p0 = 100.0
        for n in range(1, 6):
            p = step_price(p0, n, is_buy_cascade=False, divide_percent=1000)
            self.assertLess(p, p0, f"step{n} must descend for counterBUY")

    def test_counterSELL_step0_through_step5_ascend(self):
        from plugins.trade.fibo.engine import step_price
        cfg = FiboConfig(exchange="ondoperps", account="amiroo", instrument="US100",
                         counter_type=CounterType.COUNTER_SELL, divide_percent=1000)
        FiboInstance(cfg)
        p0 = 100.0
        for n in range(1, 6):
            p = step_price(p0, n, is_buy_cascade=True, divide_percent=1000)
            self.assertGreater(p, p0, f"step{n} must ascend for counterSELL")

    def test_counterBUY_counterN_trigger_sl_tp_match_mq4(self):
        from plugins.trade.fibo.engine import step_price, step_tp
        p0 = 100.0
        for n in range(1, 5):
            trig = step_price(p0, n, is_buy_cascade=False, divide_percent=1000)
            sl = step_tp(p0, n, is_buy_cascade=False, divide_percent=1000)
            tp = step_price(p0, n + 1, is_buy_cascade=False, divide_percent=1000)
            self.assertAlmostEqual(trig, 99.966000 if n == 1 else
                                   (99.9110187 if n == 2 else
                                    (99.8220978934 if n == 3 else 99.6783540724)),
                                   places=4)
            self.assertAlmostEqual(sl, 100.000 if n == 1 else
                                   (99.966 if n == 2 else
                                    (99.9110187 if n == 3 else 99.8220978934)),
                                   places=4)
            self.assertGreater(trig, tp)  # descending cascade: trigger > next level
            self.assertLess(trig, sl)

    def test_counterSELL_counterN_trigger_sl_tp_match_mq4(self):
        from plugins.trade.fibo.engine import step_price, step_tp
        p0 = 100.0
        for n in range(1, 5):
            trig = step_price(p0, n, is_buy_cascade=True, divide_percent=1000)
            sl = step_tp(p0, n, is_buy_cascade=True, divide_percent=1000)
            tp = step_price(p0, n + 1, is_buy_cascade=True, divide_percent=1000)
            self.assertLess(trig, tp)
            self.assertGreater(trig, sl)


# =========================================================================
# End-to-end through the engine (cumulative model)
# =========================================================================


class EndToEndTests(unittest.TestCase):
    """Engine → OndoPerpsFiboAdapter → FakeOndoPerpsAgent."""

    def _build(self, **kwargs):
        agent = FakeOndoPerpsAgent()
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        manager = FiboManager()
        cfg_kwargs = dict(
            exchange="ondoperps", account="amiroo", instrument="US100",
            counter_type=CounterType.COUNTER_BUY,
            divide_percent=1000,
        )
        cfg_kwargs.update(kwargs)
        cfg = FiboConfig(**cfg_kwargs)
        return manager, cfg, adapter, agent

    def test_counterBUY_end_to_end_sends_BUY_market_order_with_sl(self):
        manager, cfg, adapter, agent = self._build()
        manager.start(cfg, adapter, FakeQuoteSource())
        manager.on_quote(cfg.key, Quote(bid=99_999, ask=100_000))  # seed
        manager.on_quote(cfg.key, Quote(bid=100_050, ask=100_100))   # cross C1 upward
        no = agent.calls_for("new_order")
        self.assertEqual(len(no), 1)
        self.assertEqual(no[0]["side"], "buy")
        self.assertEqual(no[0]["order_type"], "market")
        self.assertEqual(no[0]["reduce_only"], False)
        sl = agent.calls_for("set_sl")
        self.assertEqual(len(sl), 1)
        self.assertEqual(sl[0]["price"], "99999")
        # No TP yet.
        self.assertEqual(agent.calls_for("set_tp"), [])

    def test_counterSELL_end_to_end_sends_SELL_market_order_with_sl(self):
        manager, cfg, adapter, agent = self._build(
            counter_type=CounterType.COUNTER_SELL,
        )
        manager.start(cfg, adapter, FakeQuoteSource())
        manager.on_quote(cfg.key, Quote(bid=99_999, ask=100_000))  # seed SELL cascade at ask
        manager.on_quote(cfg.key, Quote(bid=99_500, ask=99_950))  # cross C1 downward
        no = agent.calls_for("new_order")
        self.assertEqual(len(no), 1)
        self.assertEqual(no[0]["side"], "sell")
        self.assertEqual(no[0]["reduce_only"], False)

    def test_counter1_waits_for_delayed_position_then_sets_sl_once(self):
        agent = SequencedPositionAgent([
            _ok("position_state", positions=[]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0",
                "tp": None, "sl": None,
                "tp_count": None, "sl_count": None,
            }]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0",
                "tp": None, "sl": "100000",
                "tp_count": None, "sl_count": 1,
            }]),
        ])
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        manager = FiboManager()
        cfg = FiboConfig(exchange="ondoperps", account="amiroo", instrument="US100",
                         counter_type=CounterType.COUNTER_SELL, divide_percent=1000, counter1=1)
        instance = manager.start(cfg, adapter, FakeQuoteSource())
        manager.on_quote(instance.key, Quote(bid=99_999, ask=100_000))
        manager.on_quote(instance.key, Quote(bid=99_500, ask=99_950))
        self.assertEqual(len(agent.calls_for("new_order")), 1)
        self.assertEqual(len(agent.calls_for("set_sl")), 1)
        self.assertEqual(manager._engines[cfg.key].instance.cumulative_volume, Decimal("1"))

    def test_level2_zero_volume_moves_sl_to_step1_without_new_market_order(self):
        agent = SequencedPositionAgent([
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0", "tp": None, "sl": None,
                "tp_count": None, "sl_count": None,
            }]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0", "tp": None, "sl": "100000",
                "tp_count": None, "sl_count": 1,
            }]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0", "tp": None, "sl": "100000",
                "tp_count": None, "sl_count": 1,
            }]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0", "tp": None, "sl": "99966",
                "tp_count": None, "sl_count": 1,
            }]),
        ])
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        adapter._sleep_fn = lambda *_args, **_kwargs: None
        manager = FiboManager()
        cfg = FiboConfig(
            exchange="ondoperps", account="amiroo", instrument="US100",
            counter_type=CounterType.COUNTER_SELL, divide_percent=1000, counter1=1, counter2=0, counter3=0, counter4=0,
        )
        instance = manager.start(cfg, adapter, FakeQuoteSource())
        manager.on_quote(instance.key, Quote(bid=99_999, ask=100_000))
        manager.on_quote(instance.key, Quote(bid=99_940, ask=99_950))
        manager.on_quote(instance.key, Quote(bid=99_890, ask=99_900))
        self.assertEqual(len(agent.calls_for("new_order")), 1)
        self.assertEqual(len(agent.calls_for("set_sl")), 2)
        self.assertEqual(manager._engines[cfg.key].instance.cumulative_volume, Decimal("1"))
        self.assertFalse(manager._engines[cfg.key].instance.frozen)

    def test_level3_zero_volume_moves_sl_without_duplicate_market_order(self):
        agent = SequencedPositionAgent([
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0", "tp": None, "sl": None,
                "tp_count": None, "sl_count": None,
            }]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0", "tp": None, "sl": "100000",
                "tp_count": None, "sl_count": 1,
            }]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0", "tp": None, "sl": "99966",
                "tp_count": None, "sl_count": 1,
            }]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0", "tp": None, "sl": "99911.02",
                "tp_count": None, "sl_count": 1,
            }]),
        ])
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        adapter._sleep_fn = lambda *_args, **_kwargs: None
        manager = FiboManager()
        cfg = FiboConfig(
            exchange="ondoperps", account="amiroo", instrument="US100",
            counter_type=CounterType.COUNTER_SELL, divide_percent=1000, counter1=1, counter2=0, counter3=0, counter4=0,
        )
        instance = manager.start(cfg, adapter, FakeQuoteSource())
        manager.on_quote(instance.key, Quote(bid=99_999, ask=100_000))
        manager.on_quote(instance.key, Quote(bid=99_940, ask=99_950))
        manager.on_quote(instance.key, Quote(bid=99_890, ask=99_900))
        manager.on_quote(instance.key, Quote(bid=99_790, ask=99_800))
        self.assertEqual(len(agent.calls_for("new_order")), 1)
        self.assertEqual(len(agent.calls_for("set_sl")), 3)
        self.assertEqual(manager._engines[cfg.key].instance.cumulative_volume, Decimal("1"))
        self.assertFalse(manager._engines[cfg.key].instance.frozen)

    def test_counter4_tp_verification_retries_quantized_readback_without_duplicate_tp_set(self):
        agent = SequencedPositionAgent([
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0", "tp": None, "sl": None,
                "tp_count": None, "sl_count": None,
            }]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0", "tp": None, "sl": "100000",
                "tp_count": None, "sl_count": 1,
            }]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0", "tp": None, "sl": "99966",
                "tp_count": None, "sl_count": 1,
            }]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0", "tp": None, "sl": "99911.02",
                "tp_count": None, "sl_count": 1,
            }]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0", "tp": "99400", "sl": "99822.1",
                "tp_count": 1, "sl_count": 1,
            }]),
            _ok("position_state", positions=[{
                "symbol": "US100", "side": "short", "size": "1",
                "entry_price": "100", "pnl": "0", "tp": "99446.1", "sl": "99822.1",
                "tp_count": 1, "sl_count": 1,
            }]),
        ])
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        adapter._sleep_fn = lambda *_args, **_kwargs: None
        manager = FiboManager()
        cfg = FiboConfig(
            exchange="ondoperps", account="amiroo", instrument="US100",
            counter_type=CounterType.COUNTER_SELL, divide_percent=1000, counter1=1, counter2=0, counter3=0, counter4=0,
        )
        instance = manager.start(cfg, adapter, FakeQuoteSource())
        manager.on_quote(instance.key, Quote(bid=99_999, ask=100_000))
        manager.on_quote(instance.key, Quote(bid=99_940, ask=99_950))
        manager.on_quote(instance.key, Quote(bid=99_890, ask=99_900))
        manager.on_quote(instance.key, Quote(bid=99_790, ask=99_800))
        manager.on_quote(instance.key, Quote(bid=99_640, ask=99_650))
        self.assertEqual(len(agent.calls_for("new_order")), 1)
        self.assertEqual(len(agent.calls_for("set_tp")), 1)
        self.assertFalse(manager._engines[cfg.key].instance.frozen)

    def test_position_never_appears_freezes_without_duplicate_order_or_sl(self):
        agent = SequencedPositionAgent([
            _ok("position_state", positions=[]),
            _ok("position_state", positions=[]),
            _ok("position_state", positions=[]),
            _ok("position_state", positions=[]),
            _ok("position_state", positions=[]),
        ])
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        manager = FiboManager()
        cfg = FiboConfig(exchange="ondoperps", account="amiroo", instrument="US100",
                         counter_type=CounterType.COUNTER_SELL, divide_percent=1000)
        instance = manager.start(cfg, adapter, FakeQuoteSource())
        manager.on_quote(instance.key, Quote(bid=99_999, ask=100_000))
        manager.on_quote(instance.key, Quote(bid=99_500, ask=99_950))
        self.assertEqual(len(agent.calls_for("new_order")), 1)
        self.assertEqual(agent.calls_for("set_sl"), [])
        self.assertTrue(manager._engines[cfg.key].instance.frozen)

    def test_two_registrations_route_to_correct_accounts(self):
        agent_a = FakeOndoPerpsAgent()
        agent_b = FakeOndoPerpsAgent()
        adapter_a = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent_a)
        adapter_b = OndoPerpsFiboAdapter("ondoperps", "bitget", agent_b)
        sa = FakeQuoteSource(quotes_by_symbol={
            "US100": [Quote(bid=99.999, ask=100.000),
                      Quote(bid=99.500, ask=99.950)],
        })
        sb = FakeQuoteSource(quotes_by_symbol={
            "US100": [Quote(bid=99.999, ask=100.000),
                      Quote(bid=100.050, ask=100.100)],
        })
        manager = FiboManager()
        cfg_a = FiboConfig(exchange="ondoperps", account="amiroo", instrument="US100",
                           counter_type=CounterType.COUNTER_SELL, divide_percent=1000)
        cfg_b = FiboConfig(exchange="ondoperps", account="bitget", instrument="US100",
                           counter_type=CounterType.COUNTER_BUY, divide_percent=1000)
        manager.start(cfg_a, adapter_a, sa)
        manager.start(cfg_b, adapter_b, sb)
        manager.on_quote(cfg_a.key, Quote(bid=99.999, ask=100.000))
        manager.on_quote(cfg_a.key, Quote(bid=99.500, ask=99.950))
        manager.on_quote(cfg_b.key, Quote(bid=99.999, ask=100.000))
        manager.on_quote(cfg_b.key, Quote(bid=100.050, ask=100.100))
        self.assertEqual(agent_a.calls_for("new_order")[0]["account"], "amiroo")
        self.assertEqual(agent_a.calls_for("new_order")[0]["side"], "sell")
        self.assertEqual(agent_b.calls_for("new_order")[0]["account"], "bitget")
        self.assertEqual(agent_b.calls_for("new_order")[0]["side"], "buy")

    def test_different_instruments_independent(self):
        agent = FakeOndoPerpsAgent()
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        sa = FakeQuoteSource(quotes_by_symbol={
            "US100": [Quote(bid=99.999, ask=100.000),
                      Quote(bid=99.500, ask=99.950)],
            "US500": [Quote(bid=4999.999, ask=5000.000),
                      Quote(bid=4997.500, ask=4997.900)],
        })
        manager = FiboManager()
        cfg_a = FiboConfig(exchange="ondoperps", account="amiroo", instrument="US100",
                           counter_type=CounterType.COUNTER_SELL, divide_percent=1000)
        cfg_b = FiboConfig(exchange="ondoperps", account="amiroo", instrument="US500",
                           counter_type=CounterType.COUNTER_SELL, divide_percent=1000)
        manager.start(cfg_a, adapter, sa)
        manager.start(cfg_b, adapter, sa)
        manager.on_quote(cfg_a.key, Quote(bid=99.999, ask=100.000))
        manager.on_quote(cfg_a.key, Quote(bid=99.500, ask=99.950))
        manager.on_quote(cfg_b.key, Quote(bid=4999.999, ask=5000.000))
        manager.on_quote(cfg_b.key, Quote(bid=4997.500, ask=4997.900))
        symbols = [n["symbol"] for n in agent.calls_for("new_order")]
        self.assertIn("US100", symbols)
        self.assertIn("US500", symbols)


# =========================================================================
# Quote source
# =========================================================================


class QuoteSourceTests(unittest.TestCase):

    def test_quote_source_uses_market_price_not_position_state(self):
        agent = FakeOndoPerpsAgent()
        source = OndoPerpsQuoteSource("ondoperps", "amiroo", agent)
        quote = source.current_bid_ask("US100")
        self.assertEqual(quote.bid, 100.25)
        self.assertEqual(quote.ask, 100.25)
        self.assertEqual(agent.calls[0]["operation"], "market_price")

    def test_quote_source_returns_mark_price_when_flat(self):
        agent = FakeOndoPerpsAgent()
        agent.scripted[("market_price", "SOL")] = _ok(
            "market_price",
            market_price={
                "requested_symbol": "SOL",
                "market": "SOL-USD.P",
                "markPrice": "151.5",
                "oraclePrice": "151.4",
                "lastExternalPrice": "151.45",
                "lastUpdatedTime": "1712351111",
            },
        )
        source = OndoPerpsQuoteSource("ondoperps", "amiroo", agent)
        quote = source.current_bid_ask("SOL")
        self.assertEqual(quote.bid, 151.5)
        self.assertEqual(quote.ask, 151.5)

    def test_two_accounts_same_instrument_can_consume_same_market_price_independently(self):
        a1 = FakeOndoPerpsAgent()
        a2 = FakeOndoPerpsAgent()
        s1 = OndoPerpsQuoteSource("ondoperps", "amiroo", a1)
        s2 = OndoPerpsQuoteSource("ondoperps", "bitget", a2)
        q1 = s1.current_bid_ask("US100")
        q2 = s2.current_bid_ask("US100")
        self.assertEqual((q1.bid, q1.ask), (100.25, 100.25))
        self.assertEqual((q2.bid, q2.ask), (100.25, 100.25))
        self.assertEqual(a1.calls[0]["account"], "amiroo")
        self.assertEqual(a2.calls[0]["account"], "bitget")

    def test_quote_source_routes_to_correct_account(self):
        agent = FakeOndoPerpsAgent()
        agent.scripted[("market_price", "US100")] = _ok(
            "market_price",
            market_price={
                "requested_symbol": "US100",
                "market": "US100-USD.P",
                "markPrice": "100.25",
            },
        )
        source = OndoPerpsQuoteSource("ondoperps", "bitget", agent)
        source.current_bid_ask("US100")
        self.assertEqual(agent.calls[0]["account"], "bitget")


# =========================================================================
# Freeze-doesn't-mutate-exchange tests
# =========================================================================


class FreezeDoesNotMutateExchangeTests(unittest.TestCase):

    def test_freeze_only_adds_sl_calls_no_close_no_cancel(self):
        """A freeze (strategy-driven) MUST NOT cancel, close, or modify
        the exchange state. STOP also MUST NOT touch exchange state.
        This test pins both contracts together.
        """
        agent = FakeOndoPerpsAgent()
        agent.script_failure("set_sl", code="SL_FAILED", message="")
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        manager = FiboManager()
        cfg = FiboConfig(exchange="ondoperps", account="amiroo", instrument="US100",
                         counter_type=CounterType.COUNTER_SELL, divide_percent=1000)
        instance = manager.start(cfg, adapter, FakeQuoteSource())
        manager.on_quote(instance.key, Quote(bid=99.999, ask=100.000))
        before = len(agent.calls)
        manager.on_quote(instance.key, Quote(bid=99.500, ask=99.950))
        after = len(agent.calls)
        cancel_or_close_ops = {"cancel_order_group", "close_position"}
        ops_added = {c["operation"] for c in agent.calls[before:after]}
        self.assertFalse(cancel_or_close_ops & ops_added,
                         f"freeze triggered exchange mutations: {ops_added}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
