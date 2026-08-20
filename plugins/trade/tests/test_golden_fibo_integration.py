"""Integration tests for GoldenFibo service, wizard, and IPC.

These tests stub the Lighter adapter so they run offline. They
prove the actual /fibo wiring, the old-state quarantine, the
opposite-direction rejection, the LIST/DETAIL/STOP cycle, and the
restart safety.
"""

from __future__ import annotations

import os
import sys
import re
import json
import tempfile
from decimal import Decimal
from pathlib import Path


# Path-hook bypass (mirrors other test files in this directory)
_EDITABLE_FINDER = "__editable___hermes_agent_0_20_0_finder"
_KNOWN_EDITABLE_FINDERS = (_EDITABLE_FINDER,)
if any(name in repr(h) for h in sys.path_hooks for name in _KNOWN_EDITABLE_FINDERS):
    sys.path_hooks[:] = [
        h
        for h in sys.path_hooks
        if not any(name in repr(h) for name in _KNOWN_EDITABLE_FINDERS)
    ]

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
# NOTE: Do NOT pop plugins.trade.* from sys.modules here.
# Session-level isolation lives in conftest.py. Mid-suite pops
# create dual CanonicalResponse/TradeDesk identities and break
# later tests (INVALID_AGENT_RESPONSE / ImportError agents).


import unittest
from plugins.trade.fibo_service import (
    PersistentFiboService,
    SUPPORTED_EXCHANGES,
    _reset_fibo_service,
)
from plugins.trade.fibo_wizard import FiboWizard
from plugins.trade.golden_fibo.state import (
    STATUS_QUARANTINED_OLD_STRATEGY,
    STATUS_RUNNING,
    STRATEGY_GOLDENFIBO,
    GoldenFiboState,
)


# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------
class _StubLighterAdapter:
    """Replaces LighterGoldenFiboAdapter used by the service.

    Allows every step to fill immediately and tracks submits/cancels.
    """

    def __init__(self) -> None:
        self.position = {"symbol": "SOL", "side": None, "size": "0", "sl": None, "tp": None}
        self.orders: dict = {}
        self._next_id = 1000
        self.submit_log: list = []
        self.cancel_log: list = []

    def _gen_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def resolve_instrument(self, account: str, instrument: str) -> dict:
        return {
            "symbol": instrument,
            "market_id": 1,
            "size_decimals": 3,
            "price_decimals": 3,
            "min_base_amount": "0.001",
        }

    def position_state(self, account: str, instrument: str) -> dict:
        return dict(self.position)

    def get_order_state(self, account: str, order_index: int) -> dict:
        rec = self.orders.get(int(order_index))
        if rec is None:
            return {}
        return {
            "order_index": int(order_index),
            "client_order_index": rec.get("client_order_id"),
            "symbol": "SOL",
            "side": rec.get("side"),
            "type": rec.get("type"),
            "status": rec.get("status"),
            "taxonomy": rec.get("taxonomy"),
            "requested_price": rec.get("price"),
            "requested_size": rec.get("size"),
            "filled_size": rec.get("size"),
            "actual_fill_price": rec.get("actual_fill_price"),
            "reduce_only": rec.get("reduce_only", False),
        }

    def place_market(self, *, account, instrument, side, size, client_order_id: int) -> dict:
        oid = self._gen_id()
        rec = {
            "exchange_order_id": oid,
            "client_order_id": client_order_id,
            "side": side,
            "type": "market",
            "size": str(size),
            "price": None,
            "status": "filled",
            "taxonomy": "FILLED",
            "reduce_only": False,
            "actual_fill_price": "100.0",
        }
        self.orders[oid] = rec
        self.submit_log.append(dict(rec, role="entry"))
        prev = Decimal(str(self.position.get("size") or "0"))
        if self.position.get("side") is None:
            self.position["side"] = "long" if side == "buy" else "short"
            self.position["size"] = str(size)
        elif self.position.get("side") == side:
            self.position["size"] = str(prev + Decimal(str(size)))
        return {
            "client_order_id": client_order_id,
            "exchange_order_id": oid,
            "submitted_price": None,
            "submitted_volume": str(size),
            "status": "filled",
            "verified": True,
            "role": "entry",
        }

    def place_limit(self, *, account, instrument, side, size, price, client_order_id, reduce_only=False) -> dict:
        oid = self._gen_id()
        qp = Decimal(str(price)).quantize(Decimal("0.001"))
        rec = {
            "exchange_order_id": oid,
            "client_order_id": client_order_id,
            "side": side,
            "type": "limit",
            "size": str(size),
            "price": str(qp),
            "status": "open",
            "taxonomy": "ACTIVE",
            "reduce_only": bool(reduce_only),
        }
        self.orders[oid] = rec
        self.submit_log.append(dict(rec, role="tp" if reduce_only else "ladder"))
        if reduce_only:
            self.position["tp"] = str(qp)
        return {
            "client_order_id": client_order_id,
            "exchange_order_id": oid,
            "submitted_price": str(qp),
            "submitted_volume": str(size),
            "status": "submitted",
            "verified": True,
            "role": "tp" if reduce_only else "ladder",
        }

    def cancel_order(self, *, account, order_index: int) -> bool:
        rec = self.orders.get(int(order_index))
        if rec is None:
            return False
        rec["status"] = "canceled"
        rec["taxonomy"] = "CANCELED"
        self.cancel_log.append(int(order_index))
        if rec.get("reduce_only"):
            self.position["tp"] = None
        return True


def _make_service(tmpdir: str, *, with_stub: bool = True) -> PersistentFiboService:
    state_path = Path(tmpdir) / "service_state.json"
    ledger_path = Path(tmpdir) / "service_ledger.jsonl"
    event_log_path = Path(tmpdir) / "service-events.log"
    svc = PersistentFiboService(
        state_path=state_path,
        ledger_path=ledger_path,
        event_log_path=event_log_path,
        start_thread=False,
    )
    if with_stub:
        # Replace the adapter per registration with the stub.
        svc._adapters["stub"] = _StubLighterAdapter()
    return svc


class TestServiceStart(unittest.TestCase):
    def test_supported_exchanges_includes_lighter_and_arcus(self):
        self.assertEqual(SUPPORTED_EXCHANGES, ("lighter", "arcus", "rise"))

    def test_start_creates_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            svc._adapters["lighter/amiroo/SOL/BUY"] = _StubLighterAdapter()
            r = svc.execute_command({
                "op": "start",
                "exchange": "lighter",
                "account": "amiroo",
                "instrument": "SOL",
                "direction": "BUY",
                "percentage": "0.01",
                "step0_volume": "0.01",
            })
            self.assertTrue(r["ok"])
            self.assertEqual(r["registration_key"], "lighter/amiroo/SOL/BUY")
            self.assertEqual(r["status"], STATUS_RUNNING)

    def test_start_opposite_direction_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            svc._adapters["lighter/amiroo/SOL/BUY"] = _StubLighterAdapter()
            r1 = svc.execute_command({
                "op": "start",
                "exchange": "lighter",
                "account": "amiroo",
                "instrument": "SOL",
                "direction": "BUY",
                "percentage": "0.01",
                "step0_volume": "0.01",
            })
            self.assertTrue(r1["ok"])
            r2 = svc.execute_command({
                "op": "start",
                "exchange": "lighter",
                "account": "amiroo",
                "instrument": "SOL",
                "direction": "SELL",
                "percentage": "0.01",
                "step0_volume": "0.01",
            })
            self.assertFalse(r2["ok"])
            self.assertEqual(r2["error"], "OPPOSITE_DIRECTION_ACTIVE")
            self.assertEqual(r2["existing_registration_key"], "lighter/amiroo/SOL/BUY")

    def test_start_duplicate_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            svc._adapters["lighter/amiroo/SOL/BUY"] = _StubLighterAdapter()
            r1 = svc.execute_command({
                "op": "start",
                "exchange": "lighter",
                "account": "amiroo",
                "instrument": "SOL",
                "direction": "BUY",
                "percentage": "0.01",
                "step0_volume": "0.01",
            })
            self.assertTrue(r1["ok"])
            r2 = svc.execute_command({
                "op": "start",
                "exchange": "lighter",
                "account": "amiroo",
                "instrument": "SOL",
                "direction": "BUY",
                "percentage": "0.01",
                "step0_volume": "0.01",
            })
            self.assertFalse(r2["ok"])
            self.assertEqual(r2["error"], "DUPLICATE_REGISTRATION")

    def test_start_unsupported_exchange_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            r = svc.execute_command({
                "op": "start",
                "exchange": "ondoperps",
                "account": "amiroo",
                "instrument": "SOL",
                "direction": "BUY",
                "percentage": "0.01",
                "step0_volume": "0.01",
            })
            self.assertFalse(r["ok"])
            self.assertEqual(r["error"], "GOLDENFIBO_NOT_SUPPORTED")

    def test_start_invalid_inputs_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            for bad in [
                {"direction": "counterBUY"},
                {"percentage": "0"},
                {"step0_volume": "-0.01"},
                {"account": ""},
                {"instrument": ""},
            ]:
                cmd = {
                    "op": "start",
                    "exchange": "lighter",
                    "account": "amiroo",
                    "instrument": "SOL",
                    "direction": "BUY",
                    "percentage": "0.01",
                    "step0_volume": "0.01",
                }
                cmd.update(bad)
                # Normalize direction case where needed
                if "direction" in bad:
                    cmd["direction"] = bad["direction"]
                cmd["account"] = bad.get("account", "amiroo")
                cmd["instrument"] = bad.get("instrument", "SOL")
                r = svc.execute_command(cmd)
                self.assertFalse(r["ok"], f"expected failure for {bad}")


class TestServicePreview(unittest.TestCase):
    def test_preview_ladder(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            r = svc.execute_command({"op": "preview", "step0_volume": "0.01"})
            self.assertTrue(r["ok"])
            self.assertEqual(len(r["ladder"]), 21)
            self.assertEqual(r["ladder"][0]["size"], "0.01")
            self.assertEqual(r["ladder"][1]["size"], "0.01")
            self.assertEqual(r["ladder"][2]["size"], "0.02")
            self.assertEqual(r["ladder"][3]["size"], "0.04")
            self.assertEqual(r["ladder"][20]["size"], str(Decimal("0.01") * (Decimal(2) ** 19)))

    def test_preview_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            for bad in ["0", "-0.01", "abc"]:
                r = svc.execute_command({"op": "preview", "step0_volume": bad})
                self.assertFalse(r["ok"])


class TestServiceListDetail(unittest.TestCase):
    def test_list_separates_active_and_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Plant a quarantined record
            state_path = Path(tmp) / "service_state.json"
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "strategy": STRATEGY_GOLDENFIBO,
                "registrations": [
                    {"registration_key": "lighter/amiroo:SOL:counterBUY", "strategy": "fibonacci_counter_cascade"},
                ],
            }))
            svc = PersistentFiboService(state_path=state_path, start_thread=False)
            r = svc.execute_command({"op": "list"})
            self.assertTrue(r["ok"])
            self.assertEqual(r["registrations_count"], 0)
            self.assertEqual(r["quarantined_count"], 1)
            self.assertEqual(r["quarantined"][0]["registration_key"], "lighter/amiroo:SOL:counterBUY")
            self.assertEqual(r["quarantined"][0]["status"], STATUS_QUARANTINED_OLD_STRATEGY)

    def test_detail_returns_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            svc._adapters["lighter/amiroo/SOL/BUY"] = _StubLighterAdapter()
            svc.execute_command({
                "op": "start",
                "exchange": "lighter",
                "account": "amiroo",
                "instrument": "SOL",
                "direction": "BUY",
                "percentage": "0.01",
                "step0_volume": "0.01",
            })
            r = svc.execute_command({"op": "detail", "registration_key": "lighter/amiroo/SOL/BUY"})
            self.assertTrue(r["ok"])
            self.assertEqual(r["registration"]["registration_key"], "lighter/amiroo/SOL/BUY")
            self.assertEqual(r["registration"]["exchange"], "lighter")
            self.assertEqual(r["registration"]["direction"], "BUY")

    def test_detail_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            r = svc.execute_command({"op": "detail", "registration_key": "lighter/amiroo/SOL/BUY"})
            self.assertFalse(r["ok"])
            self.assertEqual(r["error"], "NOT_FOUND")


class TestServiceStop(unittest.TestCase):
    def test_stop_removes_active_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            svc._adapters["lighter/amiroo/SOL/BUY"] = _StubLighterAdapter()
            svc.execute_command({
                "op": "start",
                "exchange": "lighter",
                "account": "amiroo",
                "instrument": "SOL",
                "direction": "BUY",
                "percentage": "0.01",
                "step0_volume": "0.01",
            })
            r = svc.execute_command({"op": "stop", "registration_key": "lighter/amiroo/SOL/BUY"})
            self.assertTrue(r["ok"])
            r2 = svc.execute_command({"op": "list"})
            self.assertEqual(r2["registrations_count"], 0)

    def test_stop_quarantined_old_strategy_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "service_state.json"
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "strategy": STRATEGY_GOLDENFIBO,
                "registrations": [
                    {"registration_key": "lighter/amiroo:SOL:counterBUY", "strategy": "fibonacci_counter_cascade"},
                ],
            }))
            svc = PersistentFiboService(state_path=state_path, start_thread=False)
            r = svc.execute_command({"op": "stop", "registration_key": "lighter/amiroo:SOL:counterBUY"})
            self.assertFalse(r["ok"])
            self.assertEqual(r["error"], "OLD_STRATEGY_REGISTRATION")

    def test_stop_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            r = svc.execute_command({"op": "stop", "registration_key": "lighter/amiroo/SOL/BUY"})
            self.assertFalse(r["ok"])
            self.assertEqual(r["error"], "NOT_FOUND")


class TestServiceRestartSafety(unittest.TestCase):
    def test_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc1 = _make_service(tmp)
            svc1._adapters["lighter/amiroo/SOL/BUY"] = _StubLighterAdapter()
            svc1.execute_command({
                "op": "start",
                "exchange": "lighter",
                "account": "amiroo",
                "instrument": "SOL",
                "direction": "BUY",
                "percentage": "0.01",
                "step0_volume": "0.01",
            })
            # New service instance: re-read the same state file
            svc2 = _make_service(tmp)
            r = svc2.execute_command({"op": "list"})
            self.assertEqual(r["registrations_count"], 1)
            self.assertEqual(r["registrations"][0]["registration_key"], "lighter/amiroo/SOL/BUY")

    def test_old_counter_state_quarantined_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "service_state.json"
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "strategy": STRATEGY_GOLDENFIBO,
                "registrations": [
                    {"registration_key": "lighter/amiroo:SOL:counterBUY", "strategy": "fibonacci_counter_cascade"},
                ],
            }))
            svc = PersistentFiboService(state_path=state_path, start_thread=False)
            r = svc.execute_command({"op": "list"})
            # The old counter record is quarantined, NOT loaded as a GoldenFibo registration
            self.assertEqual(r["registrations_count"], 0)
            self.assertEqual(r["quarantined_count"], 1)
            # The cheap-list report cannot show it as active
            active_keys = [x["registration_key"] for x in r["registrations"]]
            self.assertNotIn("lighter/amiroo:SOL:counterBUY", active_keys)


class TestServiceQuarantine(unittest.TestCase):
    def test_quarantined_record_no_engine_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "service_state.json"
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "strategy": STRATEGY_GOLDENFIBO,
                "registrations": [
                    {"registration_key": "lighter/amiroo:SOL:counterBUY", "strategy": "fibonacci_counter_cascade"},
                ],
            }))
            svc = PersistentFiboService(state_path=state_path, start_thread=False)
            # Provide a stub adapter that would record any calls
            stub = _StubLighterAdapter()
            svc._adapters["lighter/amiroo:SOL:counterBUY"] = stub
            # Tick the service many times — the quarantined record must
            # not trigger any adapter calls.
            for _ in range(5):
                svc._tick_once()
            self.assertEqual(stub.submit_log, [])
            self.assertEqual(stub.cancel_log, [])


# ---------------------------------------------------------------------------
# Wizard tests
# ---------------------------------------------------------------------------
class TestWizardConcepts(unittest.TestCase):
    """Confirm the wizard contains no old counterBUY/counterSELL concepts."""

    def test_wizard_no_counterBUY(self):
        with open("/root/kam/plugins/trade/fibo_wizard.py") as f:
            src = f.read()
        self.assertNotIn("counterBUY", src)
        self.assertNotIn("counterSELL", src)
        self.assertNotIn("Counter BUY", src)
        self.assertNotIn("Counter SELL", src)

    def test_wizard_no_legacy_counter_fields(self):
        with open("/root/kam/plugins/trade/fibo_wizard.py") as f:
            src = f.read()
        for token in ("counter1", "counter2", "counter3", "counter4", "dividePercent", "killCycle", "kill_cycle", "progressiveSL"):
            self.assertNotIn(token, src)

    def test_wizard_volume_field_is_step0(self):
        with open("/root/kam/plugins/trade/fibo_wizard.py") as f:
            src = f.read()
        self.assertIn("step0_volume", src)
        self.assertIn("step0:", src)

    def test_wizard_percentage_present(self):
        with open("/root/kam/plugins/trade/fibo_wizard.py") as f:
            src = f.read()
        self.assertIn("percentage", src)

    def test_wizard_direction_buy_sell(self):
        with open("/root/kam/plugins/trade/fibo_wizard.py") as f:
            src = f.read()
        self.assertIn("direction:BUY", src)
        self.assertIn("direction:SELL", src)


class TestWizardFlow(unittest.TestCase):
    """Test the wizard's screen flow without invoking the service."""

    def setUp(self):
        # Create a fresh wizard with a stub service to avoid any persistence
        from plugins.trade.fibo_wizard import FiboWizard
        self._stub_service = _StubService()
        self.wizard = FiboWizard(service=self._stub_service)

    def test_open_shows_main_menu(self):
        s = self.wizard.open(("chat", 1))
        self.assertIn("GoldenFibo", s.text)
        # Must have Start, Running, Stop, Exit buttons
        labels = [b["text"] for btn_row in s.buttons for b in btn_row]
        self.assertIn("▶️ Start Fibo", labels)
        self.assertIn("🔵 Running Fibo", labels)
        self.assertIn("🛑 STOP Fibo", labels)
        self.assertIn("✕ Exit", labels)

    def test_open_does_not_contain_counter_button(self):
        s = self.wizard.open(("chat", 1))
        labels = [b["text"] for btn_row in s.buttons for b in btn_row]
        for label in labels:
            self.assertNotIn("Counter", label)

    def test_exchange_selection_shows_tradedesk_discovery(self):
        """Exchange list comes from TradeDesk, not SUPPORTED_EXCHANGES alone."""
        from plugins.trade.fibo_wizard import FiboWizard, _discovered_exchanges

        class _Desk:
            def list_exchanges(self):
                return ["arcus", "lighter", "ondoperps"]

            def list_accounts(self, exchange):
                return []

        w = FiboWizard(tradedesk=_Desk(), service=self._stub_service)
        s = w.handle_callback(("chat", 1), "menu:start")
        labels = [b["text"] for btn_row in s.buttons for b in btn_row]
        self.assertIn("LIGHTER", labels)
        self.assertIn("ARCUS", labels)
        self.assertIn("ONDOPERPS", labels)
        # Parity with the discovery helper itself.
        self.assertEqual(
            sorted(x.upper() for x in _discovered_exchanges(_Desk()) if x),
            sorted(lab for lab in labels if lab not in ("◀️ Back", "✕ Exit")),
        )

    def test_account_screen_uses_discovery(self):
        # After picking exchange, account is rendered via TradeDesk.list_accounts
        s = self.wizard.handle_callback(("chat", 1), "menu:start")
        s = self.wizard.handle_callback(("chat", 1), "exchange:lighter")
        labels = [b["text"] for btn_row in s.buttons for b in btn_row]
        self.assertTrue(isinstance(labels, list))
        self.assertEqual(s.state, "account")

    def test_instrument_screen_has_quick_select_and_other(self):
        s = self.wizard.handle_callback(("chat", 1), "menu:start")
        s = self.wizard.handle_callback(("chat", 1), "exchange:lighter")
        # Pick a fake account named "amiroo"
        s = self.wizard.handle_callback(("chat", 1), "account:amiroo")
        self.assertEqual(s.state, "instrument")
        labels = [b["text"] for btn_row in s.buttons for b in btn_row]
        self.assertIn("BTC", labels)
        self.assertIn("ETH", labels)
        self.assertIn("SOL", labels)
        self.assertIn("Other instrument...", labels)

    def test_step0_pick_uses_persisted_value(self):
        s = self.wizard.handle_callback(("chat", 1), "menu:start")
        s = self.wizard.handle_callback(("chat", 1), "exchange:lighter")
        s = self.wizard.handle_callback(("chat", 1), "account:amiroo")
        s = self.wizard.handle_callback(("chat", 1), "instrument:SOL")
        s = self.wizard.handle_callback(("chat", 1), "direction:BUY")
        # After direction: step0_volume first, then percentage, then review.
        self.assertEqual(s.state, "step0_volume")
        s = self.wizard.handle_callback(("chat", 1), "step0:0.01")
        self.assertEqual(s.state, "percentage")
        s = self.wizard.handle_text(("chat", 1), "0.02")
        self.assertEqual(s.state, "review")
        self.assertIn("Step0 volume: 0.01", s.text)
        self.assertIn("Percentage: 2.00%", s.text)
        self.assertIn("Ladder (V0..V20)", s.text)
        self.assertIn("Cumulative through Step20", s.text)

    def test_review_lists_v0_to_v20(self):
        s = self.wizard.handle_callback(("chat", 1), "menu:start")
        s = self.wizard.handle_callback(("chat", 1), "exchange:lighter")
        s = self.wizard.handle_callback(("chat", 1), "account:amiroo")
        s = self.wizard.handle_callback(("chat", 1), "instrument:SOL")
        s = self.wizard.handle_callback(("chat", 1), "direction:BUY")
        # step0 then percentage then review.
        s = self.wizard.handle_callback(("chat", 1), "step0:0.01")
        s = self.wizard.handle_text(("chat", 1), "0.01")
        for n in range(21):
            self.assertIn(f"Step{n:<2} =", s.text)

    def test_confirm_start_calls_service(self):
        s = self.wizard.handle_callback(("chat", 1), "menu:start")
        s = self.wizard.handle_callback(("chat", 1), "exchange:lighter")
        s = self.wizard.handle_callback(("chat", 1), "account:amiroo")
        s = self.wizard.handle_callback(("chat", 1), "instrument:SOL")
        s = self.wizard.handle_callback(("chat", 1), "direction:BUY")
        # step0 then percentage then confirm_start.
        s = self.wizard.handle_callback(("chat", 1), "step0:0.01")
        s = self.wizard.handle_text(("chat", 1), "0.01")
        s = self.wizard.handle_callback(("chat", 1), "confirm_start")
        self.assertEqual(s.state, "started")
        self.assertEqual(len(self._stub_service.start_calls), 1)
        cmd = self._stub_service.start_calls[0]
        self.assertEqual(cmd["exchange"], "lighter")
        self.assertEqual(cmd["account"], "amiroo")
        self.assertEqual(cmd["instrument"], "SOL")
        self.assertEqual(cmd["direction"], "BUY")
        self.assertEqual(cmd["percentage"], "0.01")
        self.assertEqual(cmd["step0_volume"], "0.01")

    def test_opposite_direction_rejected_shows_error(self):
        # First start BUY
        self.wizard.handle_callback(("chat", 1), "menu:start")
        self.wizard.handle_callback(("chat", 1), "exchange:lighter")
        self.wizard.handle_callback(("chat", 1), "account:amiroo")
        self.wizard.handle_callback(("chat", 1), "instrument:SOL")
        self.wizard.handle_callback(("chat", 1), "direction:BUY")
        self.wizard.handle_text(("chat", 1), "0.01")
        self.wizard.handle_callback(("chat", 1), "step0:0.01")
        self.wizard.handle_callback(("chat", 1), "confirm_start")
        # Now try SELL on the same key
        self.wizard.handle_callback(("chat", 2), "menu:start")
        self.wizard.handle_callback(("chat", 2), "exchange:lighter")
        self.wizard.handle_callback(("chat", 2), "account:amiroo")
        self.wizard.handle_callback(("chat", 2), "instrument:SOL")
        self.wizard.handle_callback(("chat", 2), "direction:SELL")
        self.wizard.handle_text(("chat", 2), "0.01")
        self.wizard.handle_callback(("chat", 2), "step0:0.01")
        s = self.wizard.handle_callback(("chat", 2), "confirm_start")
        # Service stub returns OPPOSITE_DIRECTION_ACTIVE for opposing direction
        self.assertIn("OPPOSITE_DIRECTION_ACTIVE", s.text)

    def test_stop_only_stops_selected_registration(self):
        # Start two registrations
        for inst in ("SOL", "ETH"):
            self.wizard.handle_callback(("chat", 1), "menu:start")
            self.wizard.handle_callback(("chat", 1), "exchange:lighter")
            self.wizard.handle_callback(("chat", 1), "account:amiroo")
            self.wizard.handle_callback(("chat", 1), f"instrument:{inst}")
            self.wizard.handle_callback(("chat", 1), "direction:BUY")
            self.wizard.handle_text(("chat", 1), "0.01")
            self.wizard.handle_callback(("chat", 1), "step0:0.01")
            self.wizard.handle_callback(("chat", 1), "confirm_start")
        # Stop only SOL via Emergency STOP mode (explicit two-mode UI)
        s = self.wizard.handle_callback(("chat", 1), "stop_pick:lighter/amiroo/SOL/BUY")
        self.assertEqual(s.state, "stop_mode")
        s = self.wizard.handle_callback(("chat", 1), "emergency_confirm:lighter/amiroo/SOL/BUY")
        self.assertEqual(s.state, "stop_emergency_confirm")
        s = self.wizard.handle_callback(("chat", 1), "confirm_emergency:lighter/amiroo/SOL/BUY")
        sol_stop_calls = [k for k in self._stub_service.stop_calls if k == "lighter/amiroo/SOL/BUY"]
        eth_stop_calls = [k for k in self._stub_service.stop_calls if k == "lighter/amiroo/ETH/BUY"]
        self.assertEqual(len(sol_stop_calls), 1)
        self.assertEqual(len(eth_stop_calls), 0)


class _StubService:
    """In-process service stub for wizard tests."""

    def __init__(self):
        self.start_calls: list = []
        self.list_calls: int = 0
        self.detail_calls: list = []
        self.stop_calls: list = []
        self._active: dict = {}
        self._quarantined: list = []

    def execute_command(self, command):
        op = command.get("op")
        if op == "start":
            key = f"{command['exchange']}/{command['account']}/{command['instrument']}/{command['direction']}"
            self.start_calls.append(command)
            # Check opposite direction
            opposite = "SELL" if command["direction"] == "BUY" else "BUY"
            opp_key = f"{command['exchange']}/{command['account']}/{command['instrument']}/{opposite}"
            if opp_key in self._active:
                return {"ok": False, "error": "OPPOSITE_DIRECTION_ACTIVE", "existing_registration_key": opp_key}
            if key in self._active:
                return {"ok": False, "error": "DUPLICATE_REGISTRATION", "registration_key": key}
            self._active[key] = command
            return {"ok": True, "registration_key": key, "status": "running"}
        if op == "list":
            self.list_calls += 1
            return {
                "ok": True,
                "registrations": [
                    {"registration_key": k, "exchange": v["exchange"], "account": v["account"],
                     "instrument": v["instrument"], "direction": v["direction"],
                     "cycle_id": 0, "highest_filled_step": -1,
                     "expected_cumulative_size": "0", "current_tp_price": None,
                     "next_step": 0, "status": "running", "freeze_reason": None}
                    for k, v in self._active.items()
                ],
                "quarantined": self._quarantined,
                "registrations_count": len(self._active),
                "quarantined_count": len(self._quarantined),
            }
        if op == "detail":
            self.detail_calls.append(command.get("registration_key"))
            key = command.get("registration_key")
            if key in self._active:
                return {"ok": True, "registration": {"registration_key": key, "status": "running", "exchange": self._active[key]["exchange"], "account": self._active[key]["account"], "instrument": self._active[key]["instrument"], "direction": self._active[key]["direction"], "cycle_id": 0, "highest_filled_step": -1, "expected_cumulative_size": "0", "current_tp_price": None, "next_step": 0, "freeze_reason": None}}
            return {"ok": False, "error": "NOT_FOUND"}
        if op == "stop":
            self.stop_calls.append(command.get("registration_key"))
            key = command.get("registration_key")
            if key in self._quarantined:
                return {"ok": False, "error": "OLD_STRATEGY_REGISTRATION"}
            self._active.pop(key, None)
            return {"ok": True, "registration_key": key, "status": "stopped"}
        if op == "emergency_stop":
            self.stop_calls.append(command.get("registration_key"))
            key = command.get("registration_key")
            if key in self._quarantined:
                return {"ok": False, "error": "OLD_STRATEGY_REGISTRATION"}
            self._active.pop(key, None)
            return {
                "ok": True,
                "registration_key": key,
                "status": "stopped",
                "mode": "emergency",
                "actions": ["deregistered"],
            }
        if op == "smooth_shutdown":
            key = command.get("registration_key")
            return {
                "ok": True,
                "registration_key": key,
                "status": "smooth_shutdown",
                "mode": "smooth",
                "immediate": False,
            }
        if op == "preview":
            return {"ok": True, "step0_volume": command.get("step0_volume"), "ladder": [{"step": n, "size": "0.01", "cumulative_through_step": "0.01"} for n in range(21)], "cumulative_through_step20": "0.01"}
        return {"ok": False, "error": "UNKNOWN"}


# ---------------------------------------------------------------------------
# Old engine cannot be reached
# ---------------------------------------------------------------------------
class TestOldEngineNotReachable(unittest.TestCase):
    """Confirm the legacy counter-cascade engine is not reachable via /fibo."""

    def test_fibo_directory_does_not_exist(self):
        self.assertFalse(Path("/root/kam/plugins/trade/fibo").exists())

    def test_no_old_engine_imports(self):
        # The service module should not import the legacy engine.
        # Scan only the code (strip docstrings) — module/function
        # docstrings narrate the historical quarantine for human
        # readers and may legitimately mention legacy class names
        # like CounterType / step0_tp to describe what the old
        # engine used to do.
        import ast
        with open("/root/kam/plugins/trade/fibo_service.py") as f:
            src = f.read()
        lines = src.splitlines()
        tree = ast.parse(src)
        # Build the set of line numbers covered by docstrings (any
        # expression-statement string constant that is the first
        # statement of a module/function/class body).
        docstring_lines = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.body:
                    continue
                first = node.body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    start = first.lineno
                    end = getattr(first, "end_lineno", start)
                    for ln in range(start, end + 1):
                        docstring_lines.add(ln)
        for token in ("CounterType", "FiboInstance", "FiboManager", "step0_tp", "step_price", "FiboLiveRunner", "RuntimeBundle"):
            pattern = re.compile(r"\b" + re.escape(token) + r"\b")
            for idx, line in enumerate(lines, start=1):
                if idx in docstring_lines:
                    continue
                self.assertNotRegex(
                    line,
                    pattern,
                    f"legacy token {token!r} found in code line {idx}: {line!r}",
                )

    def test_wizard_no_counter_concepts(self):
        with open("/root/kam/plugins/trade/fibo_wizard.py") as f:
            src = f.read()
        for token in ("counterBUY", "counterSELL", "Counter BUY", "Counter SELL", "counter1", "counter2", "counter3", "counter4", "dividePercent", "killCycle"):
            self.assertNotIn(token, src)


if __name__ == "__main__":
    unittest.main()
