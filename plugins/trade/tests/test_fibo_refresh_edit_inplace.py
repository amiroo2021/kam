"""Refresh must edit the existing Telegram registration-detail card in place.

Regression for: every 🔄 Refresh previously called send_inline_keyboard and
stacked duplicate status messages.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional


_EDITABLE = "__editable___hermes_agent_0_20_0_finder"
if any(_EDITABLE in repr(h) for h in sys.path_hooks):
    sys.path_hooks[:] = [h for h in sys.path_hooks if _EDITABLE not in repr(h)]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from plugins.trade.fibo_service import (  # noqa: E402
    FiboSocketClient,
    PersistentFiboService,
    get_fibo_service,
    _reset_fibo_service,
)
from plugins.trade.fibo_wizard import (  # noqa: E402
    FiboWizard,
    _edit_screen,
    _is_message_not_modified_error,
    _reset_fibo_wizard_for_tests,
    get_fibo_wizard,
    handle_fibo_callback,
    handle_fibo_command,
)


class _Msg:
    def __init__(self, chat_id=1, text="/fibo"):
        self.chat = type("C", (), {"id": chat_id})()
        self.text = text
        self.message_thread_id = None


class _Query:
    def __init__(self, chat_id, data, edits, *, raise_not_modified=False, raise_other=False):
        self.data = data
        self.message = type(
            "M",
            (),
            {
                "chat": type("C", (), {"id": chat_id})(),
                "message_id": 99,
                "message_thread_id": None,
            },
        )()
        self._edits = edits
        self._answered = False
        self._raise_not_modified = raise_not_modified
        self._raise_other = raise_other

    async def answer(self, *a, **k):
        self._answered = True

    async def edit_message_text(self, text=None, reply_markup=None, **kw):
        if self._raise_not_modified:
            raise RuntimeError("Message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message")
        if self._raise_other:
            raise RuntimeError("message to edit not found")
        labels, cbs = [], []
        rows = getattr(reply_markup, "inline_keyboard", None) or []
        for row in rows:
            for btn in row:
                labels.append(getattr(btn, "text", None))
                cbs.append(getattr(btn, "callback_data", None))
        self._edits.append({"text": text, "labels": labels, "cbs": cbs})
        return True


class _Adapter:
    def __init__(self):
        self.sent: List[Dict[str, Any]] = []

    async def send_inline_keyboard(self, **kw):
        self.sent.append(dict(kw))


class _DetailService:
    """IPC-like stub that returns evolving detail payloads."""

    def __init__(self):
        self.cmds: List[Dict[str, Any]] = []
        self.tick = 0
        self.key = "lighter/amiroo/SOL/BUY"

    def execute_command(self, cmd):
        self.cmds.append(dict(cmd))
        op = cmd.get("op")
        if op == "list":
            return {
                "ok": True,
                "registrations": [
                    {
                        "registration_key": self.key,
                        "status": "running",
                        "exchange": "lighter",
                        "account": "amiroo",
                        "instrument": "SOL",
                        "direction": "BUY",
                    }
                ],
                "quarantined": [],
                "registrations_count": 1,
            }
        if op == "detail":
            self.tick += 1
            return {
                "ok": True,
                "registration": {
                    "registration_key": self.key,
                    "exchange": "lighter",
                    "account": "amiroo",
                    "instrument": "SOL",
                    "direction": "BUY",
                    "cycle_id": 1,
                    "highest_filled_step": 0 if self.tick == 1 else 1,
                    "expected_cumulative_size": "0.2" if self.tick == 1 else "0.4",
                    "current_tp_price": "77.1" if self.tick == 1 else "77.0",
                    "next_step": 1 if self.tick == 1 else 2,
                    "status": "running",
                    "freeze_reason": None,
                },
            }
        return {"ok": True}


class _Desk:
    def list_exchanges(self):
        return ["lighter"]

    def list_accounts(self, exchange):
        return ["amiroo"]


def _run(c):
    return asyncio.run(c)


class TestRefreshEditsInPlace(unittest.TestCase):
    def setUp(self):
        _reset_fibo_wizard_for_tests()
        _reset_fibo_service()
        self.svc = _DetailService()
        w = get_fibo_wizard()
        w._desk = _Desk()  # noqa: SLF001
        w._service_override = self.svc  # noqa: SLF001

    def tearDown(self):
        _reset_fibo_wizard_for_tests()
        _reset_fibo_service()

    def test_A_opening_detail_is_normal_edit_after_list_open(self):
        """A: first open of detail may create messages for /fibo + navigation;
        opening detail itself is an in-place edit of the callback message.
        """
        ad = _Adapter()
        edits: List[Dict[str, Any]] = []
        chat = 7
        _run(handle_fibo_command(ad, _Msg(chat)))
        self.assertEqual(len(ad.sent), 1)  # /fibo open = new card

        # Running list
        _run(handle_fibo_callback(ad, _Query(chat, "fibo:menu:running", edits), "fibo:menu:running"))
        self.assertEqual(len(ad.sent), 1)
        self.assertEqual(len(edits), 1)
        self.assertIn("Active GoldenFibo", edits[-1]["text"])

        # Open registration detail
        _run(
            handle_fibo_callback(
                ad,
                _Query(chat, "fibo:start_detail:lighter/amiroo/SOL/BUY", edits),
                "fibo:start_detail:lighter/amiroo/SOL/BUY",
            )
        )
        self.assertEqual(len(ad.sent), 1)
        self.assertEqual(len(edits), 2)
        d = edits[-1]
        self.assertIn("Registration: lighter/amiroo/SOL/BUY", d["text"])
        self.assertIn("Status: running", d["text"])

    def test_B_C_D_E_refresh_edits_same_message_with_latest_state_and_buttons(self):
        ad = _Adapter()
        edits: List[Dict[str, Any]] = []
        chat = 8
        _run(handle_fibo_command(ad, _Msg(chat)))
        _run(handle_fibo_callback(ad, _Query(chat, "fibo:menu:running", edits), "fibo:menu:running"))
        _run(
            handle_fibo_callback(
                ad,
                _Query(chat, "fibo:start_detail:lighter/amiroo/SOL/BUY", edits),
                "fibo:start_detail:lighter/amiroo/SOL/BUY",
            )
        )
        first_detail = edits[-1]
        self.assertIn("Highest filled step: 0", first_detail["text"])
        self.assertIn("Expected cumulative size: 0.2", first_detail["text"])
        self.assertIn("Current TP price: 77.1", first_detail["text"])
        self.assertIn("Next step: 1", first_detail["text"])

        # B/C: two refreshes — only edits, no new sends
        before_sent = len(ad.sent)
        before_edits = len(edits)
        for _ in range(2):
            q = _Query(chat, "fibo:refresh", edits)
            _run(handle_fibo_callback(ad, q, "fibo:refresh"))
            self.assertTrue(q._answered)

        self.assertEqual(len(ad.sent), before_sent)  # no new cards
        self.assertEqual(len(edits), before_edits + 2)

        # D: latest IPC state reflected (tick advanced on each detail)
        last = edits[-1]
        self.assertIn("Highest filled step: 1", last["text"])
        self.assertIn("Expected cumulative size: 0.4", last["text"])
        self.assertIn("Current TP price: 77.0", last["text"])
        self.assertIn("Next step: 2", last["text"])
        self.assertIn("Freeze reason: None", last["text"])

        # E: STOP / Refresh / Back remain
        labels = last["labels"]
        cbs = last["cbs"]
        self.assertTrue(any(l and "STOP" in l for l in labels))
        self.assertTrue(any(l and "Refresh" in l for l in labels))
        self.assertTrue(any(l and "Back" in l for l in labels))
        self.assertIn("fibo:refresh", cbs)
        self.assertIn("fibo:back", cbs)
        self.assertTrue(any(c and c.startswith("fibo:stop_pick:") for c in cbs))

        # detail ops used IPC stub only
        ops = [c.get("op") for c in self.svc.cmds]
        self.assertIn("detail", ops)
        self.assertNotIn("start", ops)

    def test_F_message_not_modified_is_successful_noop(self):
        ad = _Adapter()
        edits: List[Dict[str, Any]] = []
        chat = 9
        # Seed wizard state at running_detail
        w = get_fibo_wizard()
        st = w._state_for(("9",))
        st.state = "running_detail"
        st.registration_key = "lighter/amiroo/SOL/BUY"

        q = _Query(chat, "fibo:refresh", edits, raise_not_modified=True)
        ok = _run(handle_fibo_callback(ad, q, "fibo:refresh"))
        self.assertTrue(ok)
        self.assertTrue(q._answered)
        self.assertEqual(len(ad.sent), 0)  # no replacement message
        self.assertEqual(len(edits), 0)  # edit raised before append

        # Direct helper contract
        class Q2:
            async def edit_message_text(self, **kw):
                raise RuntimeError("Bad Request: message is not modified")

        from plugins.trade.fibo_wizard import Screen

        result = _run(_edit_screen(Q2(), Screen(text="x", buttons=[], state="running_detail")))
        self.assertEqual(result, "noop")
        self.assertTrue(_is_message_not_modified_error(RuntimeError("message is not modified")))

    def test_G_refresh_uses_ipc_only_no_persistent_service_or_poll_thread(self):
        _reset_fibo_service()
        # Default wizard service path without override must be socket client
        w = FiboWizard(tradedesk=_Desk())
        self.assertIsInstance(w._service, FiboSocketClient)
        self.assertIsInstance(get_fibo_service(), FiboSocketClient)
        self.assertNotIsInstance(get_fibo_service(), PersistentFiboService)

        # With injected detail service, refresh must not spawn poll threads
        ad = _Adapter()
        edits: List[Dict[str, Any]] = []
        chat = 10
        w2 = get_fibo_wizard()
        w2._service_override = self.svc  # noqa: SLF001
        st = w2._state_for(("10",))
        st.state = "running_detail"
        st.registration_key = self.svc.key

        before = {t.name for t in threading.enumerate()}
        _run(handle_fibo_callback(ad, _Query(chat, "fibo:refresh", edits), "fibo:refresh"))
        after = {t.name for t in threading.enumerate()}
        self.assertNotIn("golden-fibo-poll", after)
        self.assertEqual(after - before, set())
        self.assertTrue(any(c.get("op") == "detail" for c in self.svc.cmds))
        self.assertEqual(len(ad.sent), 0)

    def test_genuine_edit_failure_does_not_send_duplicate(self):
        ad = _Adapter()
        edits: List[Dict[str, Any]] = []
        chat = 11
        w = get_fibo_wizard()
        st = w._state_for(("11",))
        st.state = "running_detail"
        st.registration_key = self.svc.key
        q = _Query(chat, "fibo:refresh", edits, raise_other=True)
        _run(handle_fibo_callback(ad, q, "fibo:refresh"))
        self.assertEqual(len(ad.sent), 0)
        self.assertTrue(q._answered)


if __name__ == "__main__":
    unittest.main()
