"""Telegram callback-dispatch path for /fibo (not direct FiboWizard calls).

Reproduces the live bug where get_fibo_wizard() returned a NEW instance
per callback, dropping WizardState so LIGHTER → account screen lost the
selected exchange.

Also covers in-place edit for callbacks (🔄 Refresh etc.).
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional


_EDITABLE_FINDER = "__editable___hermes_agent_0_20_0_finder"
_KNOWN = (_EDITABLE_FINDER,)
if any(n in repr(h) for h in sys.path_hooks for n in _KNOWN):
    sys.path_hooks[:] = [h for h in sys.path_hooks if not any(n in repr(h) for n in _KNOWN)]

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent.parent
sys.path.insert(0, str(_REPO))


from plugins.trade import fibo_wizard as fw  # noqa: E402
from plugins.trade.fibo_wizard import (  # noqa: E402
    _reset_fibo_wizard_for_tests,
    get_fibo_wizard,
    handle_fibo_callback,
    handle_fibo_command,
)


class _Msg:
    def __init__(self, chat_id: Any, text: str = "/fibo", thread_id=None):
        self.chat = type("C", (), {"id": chat_id})()
        self.text = text
        self.message_thread_id = thread_id


class _Query:
    """Callback query with edit_message_text (Telegram in-place edit)."""

    def __init__(self, chat_id: Any, data: str, thread_id=None, edits: Optional[List] = None):
        self.data = data
        self.message = type(
            "M",
            (),
            {
                "chat": type("C", (), {"id": chat_id})(),
                "chat_id": chat_id,
                "message_thread_id": thread_id,
                "message_id": 42,
            },
        )()
        self._answered = False
        self._edits = edits if edits is not None else []

    async def answer(self, *a, **k):
        self._answered = True

    async def edit_message_text(self, text=None, reply_markup=None, **kw):
        # Capture keyboard labels/callbacks from markup
        labels = []
        cbs = []
        if reply_markup is not None:
            rows = getattr(reply_markup, "inline_keyboard", None) or []
            for row in rows:
                for btn in row:
                    labels.append(getattr(btn, "text", None) or (btn.get("text") if isinstance(btn, dict) else None))
                    cbs.append(
                        getattr(btn, "callback_data", None)
                        or (btn.get("callback_data") if isinstance(btn, dict) else None)
                    )
        self._edits.append({"text": text, "labels": labels, "cbs": cbs, "reply_markup": reply_markup})
        return True


class _Adapter:
    """Mirrors telegram adapter prefixing of callback_data for NEW sends only."""

    def __init__(self):
        self.sent: List[Dict[str, Any]] = []
        self.edits: List[Dict[str, Any]] = []

    async def send_inline_keyboard(self, **kw):
        prefix = kw.get("callback_prefix") or ""
        buttons = []
        for row in kw.get("buttons") or []:
            nr = []
            for b in row:
                suf = str(b.get("callback_data") or "")
                if prefix and not suf.startswith(f"{prefix}:"):
                    suf = f"{prefix}:{suf}"
                nr.append({"text": b.get("text"), "callback_data": suf})
            buttons.append(nr)
        out = dict(kw)
        out["buttons"] = buttons
        self.sent.append(out)


class _FakeDesk:
    def list_exchanges(self):
        return ["arcus", "lighter", "ondoperps"]

    def list_accounts(self, exchange):
        if exchange == "lighter":
            return [
                {"account": "amiroo", "label": "amiroo — Arbitrum", "chain": "ARBITRUM"},
                {"account": "robin", "label": "robin — Robinhood", "chain": "ROBINHOOD"},
            ]
        if exchange == "arcus":
            return ["amiroo", "bitget"]
        return []


class _StubSvc:
    def __init__(self):
        self.cmds = []

    def execute_command(self, c):
        self.cmds.append(dict(c))
        return {"ok": True, "registrations": [], "registrations_count": 0}


def _run(coro):
    return asyncio.run(coro)


class TestTelegramCallbackPathLighterAccounts(unittest.TestCase):
    def setUp(self):
        _reset_fibo_wizard_for_tests()
        # Inject fake desk/service into the singleton.
        w = get_fibo_wizard()
        w._desk = _FakeDesk()  # noqa: SLF001
        w._service_override = _StubSvc()  # noqa: SLF001

    def tearDown(self):
        _reset_fibo_wizard_for_tests()

    def test_singleton_identity_across_get_calls(self):
        a = get_fibo_wizard()
        b = get_fibo_wizard()
        self.assertIs(a, b)

    def test_lighter_callback_shows_accounts_via_telegram_dispatch(self):
        """Start Fibo → click LIGHTER → account buttons from TradeDesk.

        Uses handle_fibo_command / handle_fibo_callback exactly as the
        Telegram adapter does (fresh get_fibo_wizard() each call).
        Callbacks edit in place; command still sends a new message.
        """
        ad = _Adapter()
        chat = 64620303
        edits: List[Dict[str, Any]] = []

        self.assertTrue(_run(handle_fibo_command(ad, _Msg(chat, "/fibo"))))
        self.assertEqual(len(ad.sent), 1)  # open = new message

        q_start = _Query(chat, "fibo:menu:start", edits=edits)
        self.assertTrue(
            _run(handle_fibo_callback(ad, q_start, "fibo:menu:start"))
        )
        self.assertEqual(len(ad.sent), 1)  # callback must NOT send new
        self.assertEqual(len(edits), 1)

        # Exchange screen callbacks must be fibo:exchange:<name>
        ex = edits[-1]
        flat = list(ex["cbs"])
        self.assertIn("fibo:exchange:lighter", flat)
        self.assertIn("fibo:exchange:arcus", flat)

        # Click LIGHTER — the live bug path
        q = _Query(chat, "fibo:exchange:lighter", edits=edits)
        self.assertTrue(_run(handle_fibo_callback(ad, q, "fibo:exchange:lighter")))
        self.assertTrue(q._answered)
        self.assertEqual(len(ad.sent), 1)  # still no new send

        acc = edits[-1]
        text = acc["text"]
        labels = acc["labels"]
        cbs = acc["cbs"]

        # Must NOT be the blank-exchange empty-accounts screen
        self.assertNotIn("No accounts are configured for this exchange", text)
        self.assertIn("Exchange: LIGHTER", text)
        self.assertIn("amiroo — Arbitrum", labels)
        self.assertIn("robin — Robinhood", labels)
        self.assertIn("fibo:account:amiroo", cbs)
        self.assertIn("fibo:account:robin", cbs)

        # State retained on singleton
        w = get_fibo_wizard()
        key = fw._chat_key(_Query(chat, "x").message)
        st = w._state_for(key)
        self.assertEqual(st.exchange, "lighter")
        self.assertEqual(st.state, "account")

    def test_account_selection_retains_exchange_across_callbacks(self):
        ad = _Adapter()
        chat = 42
        edits: List[Dict[str, Any]] = []
        _run(handle_fibo_command(ad, _Msg(chat)))
        _run(handle_fibo_callback(ad, _Query(chat, "fibo:menu:start", edits=edits), "fibo:menu:start"))
        _run(
            handle_fibo_callback(
                ad, _Query(chat, "fibo:exchange:lighter", edits=edits), "fibo:exchange:lighter"
            )
        )
        _run(
            handle_fibo_callback(
                ad, _Query(chat, "fibo:account:amiroo", edits=edits), "fibo:account:amiroo"
            )
        )
        final = edits[-1]
        self.assertIn("Exchange: LIGHTER", final["text"])
        self.assertIn("Account: amiroo", final["text"])
        self.assertIn("Select instrument", final["text"])
        # No service mutation on discovery path
        w = get_fibo_wizard()
        self.assertEqual(w._service_override.cmds, [])  # noqa: SLF001
        # callbacks never stacked new messages
        self.assertEqual(len(ad.sent), 1)

    def test_broken_non_singleton_would_blank_exchange_on_account_callback(self):
        """Document the failure mode: fresh wizard + account: cb → blank exchange."""
        from plugins.trade.fibo_wizard import FiboWizard

        w = FiboWizard(tradedesk=_FakeDesk(), service=_StubSvc())
        # Fresh state, no exchange selected — simulates lost singleton state
        screen = w.handle_callback(("1",), "account:amiroo")
        self.assertIn("No accounts are configured for this exchange", screen.text)
        self.assertIn("Exchange:", screen.text)
        # Exchange value blank between "Exchange:" and next content
        # (exact live symptom)
        self.assertRegex(screen.text, r"Exchange:\s*\n")


if __name__ == "__main__":
    unittest.main()
