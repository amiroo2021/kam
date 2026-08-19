"""Fibo service control plane: gateway uses IPC client only.

A. /fibo open while service down → menu works, no poll thread
B. START while service down → SERVICE_UNAVAILABLE, zero exchange calls
C. service up → START reaches daemon via socket
D. LIST after \"gateway restart\" (new client) still sees daemon regs
E. STOP goes through daemon IPC
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock


_EDITABLE = "__editable___hermes_agent_0_20_0_finder"
if any(_EDITABLE in repr(h) for h in sys.path_hooks):
    sys.path_hooks[:] = [h for h in sys.path_hooks if _EDITABLE not in repr(h)]

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent.parent))

from plugins.trade.fibo_service import (  # noqa: E402
    FiboSocketClient,
    FiboSocketServiceHost,
    PersistentFiboService,
    _reset_fibo_service,
    get_fibo_service,
)
from plugins.trade.fibo_wizard import (  # noqa: E402
    FiboWizard,
    _reset_fibo_wizard_for_tests,
    get_fibo_wizard,
)


class _CountingDesk:
    """TradeDesk stand-in that records exchange execute attempts."""

    def __init__(self):
        self.execute_calls: List[Dict[str, Any]] = []
        self.list_exchanges_calls = 0

    def list_exchanges(self):
        self.list_exchanges_calls += 1
        return ["lighter"]

    def list_accounts(self, exchange):
        return ["amiroo"]

    def execute(self, request):
        self.execute_calls.append(dict(request))
        raise AssertionError("TradeDesk.execute must not run when service is down")


class _NoExchangeAdapter:
    """Adapter that fails loudly if any venue method is called."""

    def __init__(self):
        self.calls: List[str] = []

    def _bang(self, name, *a, **k):
        self.calls.append(name)
        raise AssertionError(f"exchange adapter {name} must not be called")

    def position_state(self, *a, **k):
        return self._bang("position_state", *a, **k)

    def get_venue_constraints(self, *a, **k):
        return self._bang("get_venue_constraints", *a, **k)

    def place_market(self, *a, **k):
        return self._bang("place_market", *a, **k)

    def place_limit(self, *a, **k):
        return self._bang("place_limit", *a, **k)

    def set_shared_tp(self, *a, **k):
        return self._bang("set_shared_tp", *a, **k)

    def get_order_state(self, *a, **k):
        return self._bang("get_order_state", *a, **k)

    def get_order_state_by_client_id(self, *a, **k):
        return self._bang("get_order_state_by_client_id", *a, **k)

    def cancel_order(self, *a, **k):
        return self._bang("cancel_order", *a, **k)


class _DaemonHarness:
    """Run PersistentFiboService + FiboSocketServiceHost on a temp socket."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.socket_path = tmp / "service.sock"
        self.state_path = tmp / "service_state.json"
        self.ledger_path = tmp / "ledger.jsonl"
        self.event_path = tmp / "events.log"
        self.service = PersistentFiboService(
            state_path=self.state_path,
            ledger_path=self.ledger_path,
            event_log_path=self.event_path,
            start_thread=True,
        )
        self.adapter = _NoExchangeAdapter()
        # Preflight/start will need adapter — for LIST/STOP we may not start.
        # For START tests we mock preflight + engine path carefully.
        self.host = FiboSocketServiceHost(
            service=self.service, socket_path=self.socket_path
        )
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(
            target=self.host.serve_forever, name="fibo-ipc-test", daemon=True
        )
        self._thread.start()
        # Wait until socket exists
        for _ in range(50):
            if self.socket_path.exists():
                # accept connection
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                        s.settimeout(0.2)
                        s.connect(str(self.socket_path))
                    return
                except OSError:
                    pass
            time.sleep(0.05)
        raise RuntimeError("daemon socket did not become ready")

    def stop(self):
        try:
            self.host.shutdown()
        except Exception:
            pass
        try:
            self.service.shutdown()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)


class TestGetFiboServiceIsIpcClient(unittest.TestCase):
    def setUp(self):
        _reset_fibo_service()

    def tearDown(self):
        _reset_fibo_service()

    def test_get_fibo_service_returns_socket_client_not_persistent(self):
        svc = get_fibo_service()
        self.assertIsInstance(svc, FiboSocketClient)
        self.assertIs(get_fibo_service(), svc)
        # Must not start a poll thread
        names = [t.name for t in threading.enumerate()]
        self.assertNotIn("golden-fibo-poll", names)


class TestServiceDownBehavior(unittest.TestCase):
    def setUp(self):
        _reset_fibo_service()
        _reset_fibo_wizard_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.sock = Path(self.tmp.name) / "missing.sock"
        # Point client at missing socket
        self.client = FiboSocketClient(socket_path=self.sock, timeout=0.5)
        self.desk = _CountingDesk()
        self.wizard = FiboWizard(tradedesk=self.desk, service=self.client)

    def tearDown(self):
        _reset_fibo_service()
        _reset_fibo_wizard_for_tests()
        self.tmp.cleanup()

    def test_A_open_menu_works_no_poll_thread(self):
        before = {t.ident for t in threading.enumerate()}
        screen = self.wizard.open(("chat", 1))
        self.assertIn("GoldenFibo", screen.text)
        after = {t.name for t in threading.enumerate()}
        self.assertNotIn("golden-fibo-poll", after)
        # open must not touch service / exchange
        self.assertEqual(self.desk.execute_calls, [])

    def test_B_start_while_down_service_unavailable_zero_exchange(self):
        s = self.wizard.open(("c", 1))
        # drive to review-ish state directly
        st = self.wizard._state_for(("c", 1))
        st.exchange = "lighter"
        st.account = "amiroo"
        st.instrument = "SOL"
        st.direction = "BUY"
        st.percentage = "0.001"
        st.step0_volume = "0.2"
        st.state = "review"
        screen = self.wizard._on_confirm_start(("c", 1), st)
        self.assertEqual(screen.state, "start_failed")
        self.assertIn("SERVICE_UNAVAILABLE", screen.text)
        self.assertEqual(self.desk.execute_calls, [])
        # client returns unavailable
        resp = self.client.execute_command({"op": "start", "exchange": "lighter"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "SERVICE_UNAVAILABLE")


class TestServiceUpIpcPath(unittest.TestCase):
    def setUp(self):
        _reset_fibo_service()
        self.tmp = tempfile.TemporaryDirectory()
        self.harness = _DaemonHarness(Path(self.tmp.name))
        self.harness.start()
        self.client = FiboSocketClient(
            socket_path=self.harness.socket_path, timeout=2.0
        )

    def tearDown(self):
        self.harness.stop()
        self.tmp.cleanup()
        _reset_fibo_service()

    def test_C_list_reaches_daemon(self):
        resp = self.client.execute_command({"op": "list"})
        self.assertTrue(resp.get("ok"), resp)
        self.assertIn("registrations", resp)

    def test_C_start_reaches_daemon_not_gateway_process(self):
        # Patch adapter construction + preflight so we don't hit live venue.
        # We only need to prove the command crosses IPC and daemon handles it.
        key = "lighter/amiroo/SOL/BUY"

        def fake_start(command):
            # Record that daemon-side execute_command got the op
            return {
                "ok": True,
                "registration_key": key,
                "status": "running",
            }

        with mock.patch.object(
            self.harness.service, "execute_command", side_effect=fake_start
        ) as m:
            resp = self.client.execute_command(
                {
                    "op": "start",
                    "exchange": "lighter",
                    "account": "amiroo",
                    "instrument": "SOL",
                    "direction": "BUY",
                    "percentage": "0.001",
                    "step0_volume": "0.2",
                }
            )
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(resp["registration_key"], key)
            self.assertEqual(m.call_count, 1)
            self.assertEqual(m.call_args[0][0]["op"], "start")

    def test_D_new_client_after_gateway_restart_still_sees_daemon(self):
        # Seed a registration directly on daemon service state
        from plugins.trade.golden_fibo.config import GoldenFiboConfig
        from plugins.trade.golden_fibo.state import GoldenFiboState, STATUS_RUNNING

        cfg = GoldenFiboConfig(
            exchange="lighter",
            account="amiroo",
            instrument="SOL",
            direction="BUY",
            percentage=__import__("decimal").Decimal("0.001"),
            step0_volume=__import__("decimal").Decimal("0.2"),
        )
        st = GoldenFiboState(
            registration_key=cfg.registration_key,
            exchange=cfg.exchange,
            account=cfg.account,
            instrument=cfg.instrument,
            direction=cfg.direction,
            percentage=cfg.percentage,
            step0_volume=cfg.step0_volume,
        )
        st.status = STATUS_RUNNING
        self.harness.service._states[cfg.registration_key] = st
        self.harness.service._configs[cfg.registration_key] = cfg
        self.harness.service._save_state()

        # Simulate gateway restart: new client instance
        client2 = FiboSocketClient(
            socket_path=self.harness.socket_path, timeout=2.0
        )
        resp = client2.execute_command({"op": "list"})
        self.assertTrue(resp.get("ok"), resp)
        keys = [r.get("registration_key") for r in (resp.get("registrations") or [])]
        self.assertIn(cfg.registration_key, keys)

    def test_E_stop_goes_through_ipc(self):
        seen = []

        def fake_exec(command):
            seen.append(dict(command))
            if command.get("op") == "stop":
                return {"ok": True, "registration_key": command.get("registration_key")}
            if command.get("op") == "list":
                return {
                    "ok": True,
                    "registrations": [
                        {"registration_key": "lighter/amiroo/SOL/BUY", "status": "running"}
                    ],
                    "quarantined": [],
                }
            return {"ok": True}

        with mock.patch.object(
            self.harness.service, "execute_command", side_effect=fake_exec
        ):
            wizard = FiboWizard(tradedesk=_CountingDesk(), service=self.client)
            # stop pick
            screen = wizard.handle_callback(("x",), "menu:stop")
            self.assertEqual(screen.state, "stop_pick")
            self.assertIn("lighter/amiroo/SOL/BUY", screen.text + str(screen.buttons))
            # confirm stop
            screen = wizard.handle_callback(
                ("x",), "confirm_stop:lighter/amiroo/SOL/BUY"
            )
            ops = [c.get("op") for c in seen]
            self.assertIn("stop", ops)
            self.assertTrue(
                any(
                    c.get("op") == "stop"
                    and c.get("registration_key") == "lighter/amiroo/SOL/BUY"
                    for c in seen
                )
            )


class TestWizardUsesGetFiboServiceClient(unittest.TestCase):
    def setUp(self):
        _reset_fibo_service()
        _reset_fibo_wizard_for_tests()

    def tearDown(self):
        _reset_fibo_service()
        _reset_fibo_wizard_for_tests()

    def test_wizard_default_service_is_socket_client(self):
        w = FiboWizard(tradedesk=_CountingDesk())
        self.assertIsInstance(w._service, FiboSocketClient)
        # get_fibo_service must not create PersistentFiboService
        import plugins.trade.fibo_service as fs

        src = Path(fs.__file__).read_text(encoding="utf-8")
        # The get_fibo_service body must construct FiboSocketClient
        self.assertIn("FiboSocketClient()", src)
        # Guard: get_fibo_service must not instantiate PersistentFiboService()
        # Extract function body roughly
        start = src.index("def get_fibo_service")
        end = src.index("\ndef _reset_fibo_service", start)
        body = src[start:end]
        self.assertNotIn("PersistentFiboService()", body)


class TestInstallerSystemdUnit(unittest.TestCase):
    def test_template_has_required_paths(self):
        tpl = Path("/root/kam/installer/fibo.service.template").read_text()
        self.assertIn("plugins.trade.fibo_daemon", tpl)
        self.assertIn("{{SOCKET_PATH}}", tpl)
        self.assertIn("{{STATE_PATH}}", tpl)
        self.assertIn("Restart=always", tpl)
        self.assertIn("HERMES_HOME={{HERMES_HOME}}", tpl)

    def test_live_unit_points_at_hermes_fibo_paths(self):
        unit = Path("/etc/systemd/system/fibo.service").read_text()
        self.assertIn("plugins.trade.fibo_daemon", unit)
        self.assertIn("/root/.hermes/fibo/service.sock", unit)
        self.assertIn("/root/.hermes/fibo/service_state.json", unit)
        self.assertIn("Restart=always", unit)


if __name__ == "__main__":
    unittest.main()
