"""Canonical instrument resolution contract.

User-facing aliases (SOL, sol) and venue-native symbols (SOL-USD) must
resolve to the same market. Resolution failure must never become FLAT.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Dict, List
from unittest import mock

from plugins.trade.agents import x_arcus_agent as arcus
from plugins.trade.golden_fibo.arcus_adapter import ArcusGoldenFiboAdapter
from plugins.trade.golden_fibo.engine import GoldenFiboEngine
from plugins.trade.golden_fibo.config import GoldenFiboConfig
from plugins.trade.golden_fibo.state import (
    ROLE_ENTRY,
    STATUS_NEEDS_RECOVERY,
    STATUS_RUNNING,
    SUBMISSION_CONFIRMED,
    GoldenFiboState,
)


def _markets_payload() -> Dict[str, Any]:
    return {
        "markets": [
            {
                "marketId": 3,
                "marketDisplayName": "SOL-USD",
                "baseAsset": "SOL",
                "tickSize": "0.001",
                "stepSize": "0.000001",
                "minOrderNotional": "10",
            },
            {
                "marketId": 1,
                "marketDisplayName": "BTC-USD",
                "baseAsset": "BTC",
                "tickSize": "0.1",
                "stepSize": "0.000001",
                "minOrderNotional": "10",
            },
        ]
    }


def _sol_position_row() -> Dict[str, Any]:
    return {
        "marketDisplayName": "SOL-USD",
        "side": "LONG",
        "size": "1.8",
        "averageEntryPrice": "80",
        "unrealizedPnl": "0",
    }


class ArcusCanonicalIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        arcus.arcus_clear_get_cache()

    def _patch_markets(self):
        def fake_public_get(credentials, path, params=None):
            if str(path).endswith("/v1/markets"):
                return _markets_payload()
            if str(path).endswith("/v1/account"):
                return {"positions": {"3": _sol_position_row()}}
            if str(path).endswith("/v1/openOrders"):
                return {"orders": []}
            return {}

        return mock.patch.object(arcus, "_public_get", side_effect=fake_public_get)

    def test_sol_and_sol_usd_resolve_same_market(self):
        with self._patch_markets(), mock.patch.object(
            arcus, "_lookup_credentials", return_value={"account": "metamask", "api_key": "x"}
        ), mock.patch.object(arcus, "_market_cache_credentials", return_value={"account": "metamask"}):
            a = arcus._resolve_market("SOL")
            b = arcus._resolve_market("SOL-USD")
            c = arcus._resolve_market("sol")
        self.assertEqual(a["market_id"], b["market_id"])
        self.assertEqual(a["display_symbol"], "SOL-USD")
        self.assertEqual(b["display_symbol"], "SOL-USD")
        self.assertEqual(c["market_id"], a["market_id"])

    def test_btc_and_btc_usd_resolve_same_market(self):
        with self._patch_markets(), mock.patch.object(
            arcus, "_market_cache_credentials", return_value={"account": "metamask"}
        ):
            a = arcus._resolve_market("BTC")
            b = arcus._resolve_market("BTC-USD")
        self.assertEqual(a["market_id"], b["market_id"])
        self.assertEqual(a["display_symbol"], "BTC-USD")

    def test_position_state_sol_sees_sol_usd_row(self):
        creds = {"account": "metamask", "api_key": "x"}
        with self._patch_markets(), mock.patch.object(arcus, "_lookup_credentials", return_value=creds), mock.patch.object(
            arcus, "_market_cache_credentials", return_value=creds
        ):
            resp = arcus.execute(
                {"operation": "position_state", "account": "metamask", "symbol": "SOL"}
            )
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.positions or []), 1)
        self.assertEqual(resp.positions[0].side, "long")
        self.assertEqual(str(resp.positions[0].size), "1.8")

    def test_position_state_sol_usd_same_row(self):
        creds = {"account": "metamask", "api_key": "x"}
        with self._patch_markets(), mock.patch.object(arcus, "_lookup_credentials", return_value=creds), mock.patch.object(
            arcus, "_market_cache_credentials", return_value=creds
        ):
            resp = arcus.execute(
                {"operation": "position_state", "account": "metamask", "symbol": "SOL-USD"}
            )
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.positions or []), 1)

    def test_unresolved_symbol_is_error_not_empty_flat(self):
        creds = {"account": "metamask", "api_key": "x"}
        with self._patch_markets(), mock.patch.object(arcus, "_lookup_credentials", return_value=creds), mock.patch.object(
            arcus, "_market_cache_credentials", return_value=creds
        ):
            resp = arcus.execute(
                {"operation": "position_state", "account": "metamask", "symbol": "NOTAMARKET"}
            )
        self.assertFalse(resp.success)
        self.assertNotEqual(getattr(resp.error, "code", ""), "")
        self.assertTrue(not resp.positions)

    def test_adapter_resolution_failure_raises_not_flat(self):
        adapter = ArcusGoldenFiboAdapter()
        fail = mock.Mock(success=False, error=mock.Mock(code="INSTRUMENT_NOT_FOUND", message="x"), positions=None)
        with mock.patch("plugins.trade.agents.x_arcus_agent.execute", return_value=fail):
            with self.assertRaises(RuntimeError):
                adapter.position_state("metamask", "NOTAMARKET")

    def test_cancel_group_sol_matches_sol_usd_orders(self):
        identity = arcus._identity_from_market(
            {"display_symbol": "SOL-USD", "base_asset": "SOL", "market_id": 3},
            "SOL",
        )
        self.assertTrue(arcus._symbol_matches_identity("SOL-USD", identity))
        self.assertTrue(arcus._symbol_matches_identity("SOL", identity))
        self.assertFalse(arcus._symbol_matches_identity("BTC-USD", identity))


class OndoCanonicalIdentityTests(unittest.TestCase):
    def test_ondo_and_ondo_usd_p_match_same_market(self):
        from plugins.trade.agents import x_ondoperps_agent as ondo

        payload = {
            "perps": {
                "tradingPairs": [
                    {"market": "ONDO-USD.P", "displayName": "ONDOUSD", "baseCurrency": "ONDO"},
                ]
            }
        }
        a = ondo._match_market(payload, "ONDO")
        b = ondo._match_market(payload, "ONDO-USD.P")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(a.symbol, "ONDO-USD.P")
        self.assertEqual(b.symbol, "ONDO-USD.P")


class EngineNoRepeatStep0Tests(unittest.TestCase):
    def test_confirmed_step0_false_flat_does_not_replace_market(self):
        placed: List[str] = []

        class Adapter:
            def position_state(self, account, instrument):
                return {"symbol": instrument, "side": None, "size": "0"}

            def place_market(self, **kw):
                placed.append("market")
                return {
                    "client_order_id": kw["client_order_id"],
                    "exchange_order_id": 99,
                    "submitted_volume": str(kw["size"]),
                    "status": "filled",
                    "verified": True,
                    "role": "entry",
                }

            def get_order_state(self, account, oid):
                return {}

            def get_order_state_by_client_id(self, account, instrument, cid):
                return {}

        cfg = GoldenFiboConfig(
            exchange="arcus",
            account="metamask",
            instrument="SOL",
            direction="BUY",
            percentage=Decimal("0.01"),
            step0_volume=Decimal("0.2"),
        )
        st = GoldenFiboState()
        st.exchange = "arcus"
        st.account = "metamask"
        st.instrument = "SOL"
        st.direction = "BUY"
        st.percentage = Decimal("0.01")
        st.step0_volume = Decimal("0.2")
        st.status = STATUS_RUNNING
        st.submission_phase = SUBMISSION_CONFIRMED
        st.pending_order_role = ROLE_ENTRY
        st.pending_order_exchange_id = 99
        st.pending_order_client_id = 1
        st.cycle_id = 1
        st.highest_filled_step = -1
        st.next_step = 0
        engine = GoldenFiboEngine(cfg, st, Adapter(), None)
        engine.tick()
        self.assertEqual(placed, [])
        self.assertNotEqual(st.status, STATUS_RUNNING)  # freeze / recovery, not another market


class EmergencyStopFalseFlatTests(unittest.TestCase):
    def test_confirmed_submission_plus_adapter_flat_does_not_deregister(self):
        import tempfile
        from pathlib import Path
        from plugins.trade.fibo_service import PersistentFiboService

        class FlatAdapter:
            name = "golden_fibo_arcus"

            def position_state(self, account, instrument):
                return {"symbol": instrument, "side": None, "size": "0"}

            def cancel_order(self, **kw):
                return True

            def get_order_state(self, account, oid):
                return {}

            def close_position(self, **kw):
                raise AssertionError("should not close on untrusted flat; fail closed")

        with tempfile.TemporaryDirectory() as tmp:
            svc = PersistentFiboService(
                state_path=Path(tmp) / "s.json",
                ledger_path=Path(tmp) / "l.jsonl",
                event_log_path=Path(tmp) / "e.log",
                start_thread=False,
            )
            key = "arcus/metamask/SOL/BUY"
            st = GoldenFiboState(
                registration_key=key,
                exchange="arcus",
                account="metamask",
                instrument="SOL",
                direction="BUY",
                status=STATUS_RUNNING,
                submission_phase=SUBMISSION_CONFIRMED,
                cycle_id=3,
                pending_order_exchange_id=8901711646360358710,
                pending_order_role=ROLE_ENTRY,
            )
            svc._states[key] = st
            svc._adapters[key] = FlatAdapter()
            resp = svc.execute_command({"op": "emergency_stop", "registration_key": key})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error"), "NEEDS_RECOVERY")
        self.assertNotIn("deregistered", resp.get("actions") or [])
        self.assertNotIn("flat_confirmed", resp.get("actions") or [])


if __name__ == "__main__":
    unittest.main()
