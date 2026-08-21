"""Canonical instrument resolution: Rise + Lighter (offline).

Resolution failure / fetch failure / malformed response must never
become FLAT. FLAT is only a successful query of a resolved market
with no position row.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List
from unittest import mock

from plugins.trade.agents import x_lighter_agent as lighter
from plugins.trade.agents import x_rise_agent as rise
from plugins.trade.fibo_service import PersistentFiboService, STATUS_NEEDS_RECOVERY
from plugins.trade.golden_fibo.engine import GoldenFiboEngine
from plugins.trade.golden_fibo.config import GoldenFiboConfig
from plugins.trade.golden_fibo.lighter_adapter import LighterGoldenFiboAdapter
from plugins.trade.golden_fibo.rise_adapter import RiseGoldenFiboAdapter
from plugins.trade.golden_fibo.state import (
    ROLE_ENTRY,
    ROLE_LADDER,
    STATUS_RUNNING,
    SUBMISSION_CONFIRMED,
    GoldenFiboState,
)


IDENT = "0x" + "ab" * 20
SIGNER = "0x" + "11" * 32


def _rise_markets(symbol: str = "SOL", market_id: str = "4") -> Dict[str, Any]:
    return {
        "markets": [
            {
                "market_id": market_id,
                "display_name": f"{symbol}/USDC",
                "base_asset_symbol": symbol,
                "active": True,
                "config": {
                    "name": f"{symbol}/USDC",
                    "step_size": "0.001",
                    "step_price": "0.01",
                    "min_order_size": "0.15",
                },
            }
        ]
    }


def _rise_portfolio(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"data": {"positions": positions}}


class RiseCanonicalIdentityTests(unittest.TestCase):
    def _creds(self):
        return mock.patch.object(rise, "_lookup_credentials", return_value=(IDENT, SIGNER))

    def test_sol_and_sol_usdc_resolve_same_market(self):
        with mock.patch.object(rise, "_fetch_markets_payload", return_value=_rise_markets()), self._creds():
            a = rise.execute({"operation": "resolve_instrument", "account": "BASED", "symbol": "SOL"})
            b = rise.execute({"operation": "resolve_instrument", "account": "BASED", "symbol": "SOL/USDC"})
            c = rise.execute({"operation": "resolve_instrument", "account": "BASED", "symbol": "SOL-USD"})
        self.assertTrue(a.success)
        self.assertTrue(b.success)
        self.assertTrue(c.success)
        self.assertEqual(a.instrument.symbol, "SOL")
        self.assertEqual(b.instrument.symbol, "SOL")
        self.assertEqual(c.instrument.symbol, "SOL")

    def test_position_state_user_symbol_sees_native_row(self):
        payload = _rise_portfolio(
            [{"market_name": "SOL/USDC", "market_id": "4", "side": 0, "size": "0.2", "avg_entry_price": "80"}]
        )
        with self._creds(), mock.patch.object(rise, "_fetch_markets_payload", return_value=_rise_markets()), mock.patch.object(
            rise, "_fetch_portfolio", return_value=payload
        ):
            r = rise.execute({"operation": "position_state", "account": "BASED", "symbol": "SOL"})
        self.assertTrue(r.success)
        pos = (r.positions or [None])[0]
        self.assertEqual(pos.side, "long")
        self.assertEqual(str(pos.size), "0.2")

    def test_position_state_native_symbol_same_row(self):
        payload = _rise_portfolio(
            [{"market_name": "SOL/USDC", "market_id": "4", "side": 0, "size": "0.2", "avg_entry_price": "80"}]
        )
        with self._creds(), mock.patch.object(rise, "_fetch_markets_payload", return_value=_rise_markets()), mock.patch.object(
            rise, "_fetch_portfolio", return_value=payload
        ):
            r = rise.execute({"operation": "position_state", "account": "BASED", "symbol": "SOL/USDC"})
        self.assertTrue(r.success)
        self.assertEqual((r.positions or [None])[0].side, "long")

    def test_true_flat_after_successful_resolve(self):
        with self._creds(), mock.patch.object(rise, "_fetch_markets_payload", return_value=_rise_markets()), mock.patch.object(
            rise, "_fetch_portfolio", return_value=_rise_portfolio([])
        ):
            r = rise.execute({"operation": "position_state", "account": "BASED", "symbol": "SOL"})
        self.assertTrue(r.success)
        pos = (r.positions or [None])[0]
        self.assertIn(str(pos.side or "").lower(), {"flat", "", "none"})
        self.assertEqual(str(pos.size), "0")

    def test_unresolved_is_error_not_flat(self):
        with self._creds(), mock.patch.object(rise, "_fetch_markets_payload", return_value=_rise_markets()), mock.patch.object(
            rise, "_fetch_portfolio", return_value=_rise_portfolio([])
        ):
            r = rise.execute({"operation": "position_state", "account": "BASED", "symbol": "NOTAMARKET"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "INSTRUMENT_NOT_FOUND")

    def test_fetch_exception_is_error_not_flat(self):
        with self._creds(), mock.patch.object(rise, "_fetch_markets_payload", return_value=_rise_markets()), mock.patch.object(
            rise, "_fetch_portfolio", side_effect=RuntimeError("boom")
        ):
            r = rise.execute({"operation": "position_state", "account": "BASED", "symbol": "SOL"})
        self.assertFalse(r.success)
        self.assertNotEqual(getattr(r.error, "code", ""), "")
        self.assertTrue(not r.positions or str((r.positions[0].size if r.positions else "0")) == "0")
        # Must not be a successful FLAT envelope.
        self.assertFalse(r.success)

    def test_malformed_portfolio_is_error_not_flat(self):
        with self._creds(), mock.patch.object(rise, "_fetch_markets_payload", return_value=_rise_markets()), mock.patch.object(
            rise, "_fetch_portfolio", return_value="not-an-object"
        ):
            r = rise.execute({"operation": "position_state", "account": "BASED", "symbol": "SOL"})
        self.assertFalse(r.success)

    def test_trade_and_fibo_resolve_same_identity(self):
        adapter = RiseGoldenFiboAdapter()
        with mock.patch.object(rise, "_fetch_markets_payload", return_value=_rise_markets()), self._creds():
            trade = rise.execute({"operation": "resolve_instrument", "account": "BASED", "symbol": "SOL"})
            fibo = adapter.resolve_instrument("BASED", "SOL")
        self.assertTrue(trade.success)
        self.assertEqual(trade.instrument.symbol, fibo.get("symbol") or fibo.get("display_name"))


class LighterCanonicalIdentityTests(unittest.TestCase):
    def _creds(self):
        return mock.patch.object(
            lighter,
            "_lookup_credentials",
            return_value={"account": "amiroo", "base_url": "https://example.invalid", "account_index": 1},
        )

    def _market(self, symbol="SOL", market_id=1):
        return {
            "symbol": symbol,
            "market_id": market_id,
            "size_decimals": 2,
            "price_decimals": 2,
            "min_base_amount": "0.1",
            "tick_size": "0.01",
            "market_type": "perp",
            "status": "active",
        }

    def test_resolve_sol_and_sol_usd_same_when_catalog_is_sol(self):
        catalog = [self._market("SOL", 1)]
        with self._creds(), mock.patch.object(lighter, "_fetch_market_catalog", return_value=catalog):
            a = lighter.execute({"operation": "resolve_instrument", "account": "amiroo", "symbol": "SOL"})
            b = lighter.execute({"operation": "resolve_instrument", "account": "amiroo", "symbol": "SOL-USD"})
        self.assertTrue(a.success)
        self.assertTrue(b.success)
        self.assertEqual(int(a.instrument.symbol and 1 or 0), 1)
        self.assertEqual(a.instrument.symbol, "SOL")
        self.assertEqual(b.instrument.symbol, "SOL")

    def test_position_state_alias_sees_native_row(self):
        fetched = {
            "credentials": {"base_url": "https://example.invalid", "account": "amiroo"},
            "target": {"positions": [{"symbol": "SOL", "position": "0.5", "sign": 1, "entry_quote": "80", "market_id": 1}]},
            "auth_token": "",
        }
        with self._creds(), mock.patch.object(lighter, "_resolve_market", return_value=self._market()), mock.patch.object(
            lighter, "_fetch_account_entry", return_value=fetched
        ), mock.patch.object(lighter, "_fetch_active_orders", return_value=[]):
            r = lighter.execute({"operation": "position_state", "account": "amiroo", "symbol": "SOL-USD"})
        self.assertTrue(r.success)
        self.assertEqual(len(r.positions or []), 1)
        self.assertEqual(str(r.positions[0].side).lower(), "long")
        self.assertEqual(str(r.positions[0].size), "0.5")

    def test_true_flat_after_resolve(self):
        fetched = {"credentials": {"base_url": "x", "account": "amiroo"}, "target": {"positions": []}, "auth_token": ""}
        with self._creds(), mock.patch.object(lighter, "_resolve_market", return_value=self._market()), mock.patch.object(
            lighter, "_fetch_account_entry", return_value=fetched
        ), mock.patch.object(lighter, "_fetch_active_orders", return_value=[]):
            r = lighter.execute({"operation": "position_state", "account": "amiroo", "symbol": "SOL"})
        self.assertTrue(r.success)
        pos = (r.positions or [None])[0]
        self.assertEqual(str(pos.size), "0")

    def test_unresolved_is_error_not_flat(self):
        with self._creds(), mock.patch.object(lighter, "_resolve_market", return_value=None), mock.patch.object(
            lighter, "_fetch_account_entry", return_value={"target": {"positions": []}, "auth_token": "", "credentials": {}}
        ):
            r = lighter.execute({"operation": "position_state", "account": "amiroo", "symbol": "NOTAMARKET"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "INSTRUMENT_NOT_FOUND")

    def test_fetch_exception_is_error_not_flat(self):
        with self._creds(), mock.patch.object(lighter, "_resolve_market", return_value=self._market()), mock.patch.object(
            lighter, "_fetch_account_entry", side_effect=RuntimeError("http down")
        ):
            r = lighter.execute({"operation": "position_state", "account": "amiroo", "symbol": "SOL"})
        self.assertFalse(r.success)

    def test_malformed_positions_is_error_not_flat(self):
        fetched = {"credentials": {"base_url": "x", "account": "amiroo"}, "target": {"positions": "bad"}, "auth_token": ""}
        with self._creds(), mock.patch.object(lighter, "_resolve_market", return_value=self._market()), mock.patch.object(
            lighter, "_fetch_account_entry", return_value=fetched
        ), mock.patch.object(lighter, "_fetch_active_orders", return_value=[]):
            r = lighter.execute({"operation": "position_state", "account": "amiroo", "symbol": "SOL"})
        self.assertFalse(r.success)

    def test_trade_and_fibo_resolve_same_identity(self):
        adapter = LighterGoldenFiboAdapter()
        catalog = [self._market("SOL", 1)]
        with self._creds(), mock.patch.object(lighter, "_fetch_market_catalog", return_value=catalog):
            trade = lighter.execute({"operation": "resolve_instrument", "account": "amiroo", "symbol": "SOL"})
            fibo = adapter.resolve_instrument("amiroo", "SOL")
        self.assertTrue(trade.success)
        self.assertEqual(trade.instrument.symbol, fibo.get("symbol") or fibo.get("requested_symbol"))


class _UnknownAdapter:
    def __init__(self, name: str) -> None:
        self.name = name
        self.place_market_calls: List[Any] = []
        self.place_limit_calls: List[Any] = []

    def position_state(self, account, instrument):
        raise RuntimeError("position_state UNKNOWN")

    def place_market(self, **kw):
        self.place_market_calls.append(kw)
        return {"client_order_id": kw["client_order_id"], "exchange_order_id": 1, "submitted_volume": str(kw["size"]), "status": "filled", "verified": True}

    def get_order_state(self, account, oid):
        return {}

    def get_order_state_by_client_id(self, account, instrument, cid):
        return {}

    def place_limit(self, **kw):
        self.place_limit_calls.append(kw)
        return kw


class EngineUnknownDoesNotRepeatTests(unittest.TestCase):
    def _tick(self, exchange: str, adapter):
        cfg = GoldenFiboConfig(
            exchange=exchange,
            account="acct",
            instrument="SOL",
            direction="BUY",
            percentage=Decimal("0.01"),
            step0_volume=Decimal("0.2"),
        )
        st = GoldenFiboState()
        st.exchange = exchange
        st.account = "acct"
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
        engine = GoldenFiboEngine(cfg, st, adapter, None)
        engine.tick()
        return st

    def test_rise_unknown_does_not_replace_step0(self):
        ad = _UnknownAdapter("golden_fibo_rise")
        st = self._tick("rise", ad)
        self.assertEqual(ad.place_market_calls, [])
        self.assertEqual(ad.place_limit_calls, [])
        self.assertEqual(st.status, STATUS_NEEDS_RECOVERY)

    def test_lighter_unknown_does_not_replace_step0(self):
        ad = _UnknownAdapter("golden_fibo_lighter")
        st = self._tick("lighter", ad)
        self.assertEqual(ad.place_market_calls, [])
        self.assertEqual(st.status, STATUS_NEEDS_RECOVERY)


class EmergencyStopUnknownTests(unittest.TestCase):
    def _run(self, exchange: str):
        class Boom:
            name = f"golden_fibo_{exchange}"

            def position_state(self, account, instrument):
                raise RuntimeError("read failed")

            def cancel_order(self, **kw):
                return True

            def close_position(self, **kw):
                raise AssertionError("must not close after unknown read")

        with TemporaryDirectory() as tmp:
            svc = PersistentFiboService(
                state_path=Path(tmp) / "s.json",
                ledger_path=Path(tmp) / "l.jsonl",
                event_log_path=Path(tmp) / "e.log",
                start_thread=False,
            )
            key = f"{exchange}/acct/SOL/BUY"
            st = GoldenFiboState(
                registration_key=key,
                exchange=exchange,
                account="acct",
                instrument="SOL",
                direction="BUY",
                status=STATUS_RUNNING,
                submission_phase=SUBMISSION_CONFIRMED,
                cycle_id=1,
                pending_order_exchange_id=11,
                pending_order_role=ROLE_ENTRY,
            )
            svc._states[key] = st
            svc._adapters[key] = Boom()
            return svc.execute_command({"op": "emergency_stop", "registration_key": key}), svc, key

    def test_rise_unknown_does_not_deregister(self):
        resp, svc, key = self._run("rise")
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error"), "NEEDS_RECOVERY")
        self.assertNotIn("deregistered", resp.get("actions") or [])
        self.assertIn(key, svc._states)

    def test_lighter_unknown_does_not_deregister(self):
        resp, svc, key = self._run("lighter")
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error"), "NEEDS_RECOVERY")
        self.assertIn(key, svc._states)


if __name__ == "__main__":
    unittest.main()
