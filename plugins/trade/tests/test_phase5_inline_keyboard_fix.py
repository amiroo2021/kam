"""Regression test for the /trade inline-keyboard compatibility fix.

This guards two invariants:

1. The wizard's initial ``/trade`` screen renders text containing
   ``Select Exchange`` AND a non-empty inline keyboard, with one
   callback button per discovered exchange.

2. The Telegram adapter's ``send_inline_keyboard`` helper (added to
   bridge the wizard to the existing transport) actually preserves
   every button when forwarding to ``_bot.send_message`` — so the
   regression that lost the keyboard between the wizard and the
   Telegram transport cannot silently reappear.

Run with::

    /usr/local/lib/hermes-agent/venv/bin/python -m pytest \
        plugins/trade/tests/test_phase5_inline_keyboard_fix.py -v

    # or, if pytest is unavailable:
    /usr/local/lib/hermes-agent/venv/bin/python \
        plugins/trade/tests/test_phase5_inline_keyboard_fix.py
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

# Make the plugin + repo root importable when run as a script.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent  # /usr/local/lib/hermes-agent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# 1) Wizard initial screen
# ---------------------------------------------------------------------------

from plugins.trade import wizard as _wizard_module  # noqa: E402


class _StubDesk:
    """Minimal TradeDesk stand-in: returns a fixed exchange list."""

    EXCHANGES = ["hyperliquid", "arcus"]

    def list_exchanges(self) -> List[str]:
        return list(self.EXCHANGES)

    def list_accounts(self, exchange: str) -> List[str]:
        return []


class WizardInitialScreenTests(unittest.TestCase):
    """The /trade initial screen must include text AND a keyboard."""

    def _initial_screen(self) -> Any:
        wizard = _wizard_module.TradeWizard(tradedesk=_StubDesk())  # type: ignore[arg-type]
        return wizard.open(("chat",))

    def test_initial_screen_text_contains_select_exchange(self) -> None:
        screen = self._initial_screen()
        self.assertIsNotNone(screen.text)
        self.assertIn("Select Exchange", screen.text)

    def test_initial_screen_has_non_empty_keyboard(self) -> None:
        screen = self._initial_screen()
        self.assertTrue(getattr(screen, "buttons", None), "buttons must be present")
        rows = screen.buttons
        # At least one button row (exchange rows) plus a trailing Exit row.
        self.assertGreaterEqual(
            len(rows), 2,
            f"expected exchange rows + Exit row, got {rows!r}",
        )

    def test_initial_screen_has_callback_for_every_exchange(self) -> None:
        screen = self._initial_screen()
        # Flatten every button row into a single list of (label, callback).
        flat: List[tuple] = []
        for row in screen.buttons:
            for btn in row:
                flat.append((btn.get("text", ""), btn.get("callback_data", "")))
        callbacks = {cb for (_lbl, cb) in flat if cb}
        for ex in _StubDesk.EXCHANGES:
            with self.subTest(exchange=ex):
                self.assertIn(f"exchange:{ex}", callbacks)


# ---------------------------------------------------------------------------
# 2) Adapter translation layer
# ---------------------------------------------------------------------------


class _StubKeyboardButton:
    """Tiny stand-in for telegram.InlineKeyboardButton.

    The adapter only ever reads ``.text`` and ``.callback_data`` back via
    the markup object; we don't need PTB semantics here.
    """

    def __init__(self, text: str, callback_data: str) -> None:
        self.text = text
        self.callback_data = callback_data


class _StubKeyboardMarkup:
    def __init__(self, inline_keyboard: List[List[_StubKeyboardButton]]) -> None:
        self.inline_keyboard = inline_keyboard


class _StubBot:
    """Records every send_message invocation; no network I/O."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return SimpleNamespace(message_id=len(self.calls))


class _StubAdapter:
    """Bypass ``__init__`` (matches the test convention documented in the
    adapter file: ``getattr(self, "_send_path_degraded", False)`` etc.).
    """

    name = "test-telegram"

    def __init__(self) -> None:
        self._bot = _StubBot()
        self._reply_to_mode = "off"
        self._disable_link_previews = False

    # These three are called by send_inline_keyboard; stub them to be no-ops.
    def _metadata_thread_id(self, metadata: Any) -> Any:
        if not metadata:
            return None
        return metadata.get("thread_id") or metadata.get("message_thread_id")

    def _reply_to_message_id_for_send(self, reply_to: Any, metadata: Any, reply_to_mode: Any) -> Any:
        if reply_to:
            return int(reply_to)
        return None

    def _thread_kwargs_for_send(self, chat_id: Any, thread_id: Any, metadata: Any, reply_to_message_id: Any = None, reply_to_mode: Any = None) -> Dict[str, Any]:
        if thread_id is not None:
            return {"message_thread_id": int(thread_id)}
        return {}

    def _link_preview_kwargs(self) -> Dict[str, Any]:
        return {}

    def format_message(self, content: str) -> str:
        return content or ""

    async def _send_message_with_thread_fallback(self, **kwargs: Any) -> Any:
        """Mirror the real method: call self._bot.send_message, retry
        without ``message_thread_id`` on a thread-not-found error. The
        tests use a stub bot that never raises so the retry branch is
        never taken."""
        message_thread_id = kwargs.get("message_thread_id")
        try:
            return await self._bot.send_message(**kwargs)
        except Exception as send_err:  # noqa: BLE001
            if message_thread_id is not None:
                kwargs.pop("message_thread_id", None)
                return await self._bot.send_message(**kwargs)
            raise

    # The one we actually want to exercise.
    send_inline_keyboard = None  # patched in below


class AdapterTranslationTests(unittest.TestCase):
    """The adapter must not silently drop the keyboard on the way out."""

    def setUp(self) -> None:
        # Late import so the adapter has already been loaded with PTB
        # installed.  We grab the bound method after monkey-patching
        # InlineKeyboardButton / InlineKeyboardMarkup onto the adapter
        # module to the stub classes above.
        from plugins.platforms.telegram import adapter as _adapter_mod

        # Replace the module-level names the new helper references.
        _adapter_mod.InlineKeyboardButton = _StubKeyboardButton  # type: ignore[attr-defined]
        _adapter_mod.InlineKeyboardMarkup = _StubKeyboardMarkup  # type: ignore[attr-defined]

        self.adapter_mod = _adapter_mod
        self.adapter = _StubAdapter()

        # Bind the real live helper when the installed Hermes already has the
        # validated no-double-prefix guard. Otherwise bind the exact helper block
        # that KAM ships and injects into TelegramAdapter during install.
        live_src = inspect.getsource(_adapter_mod.TelegramAdapter.send_inline_keyboard)
        from patchspecs import INLINE_KEYBOARD_HELPER_SENTINEL, _INLINE_KEYBOARD_HELPER_BLOCK

        if INLINE_KEYBOARD_HELPER_SENTINEL in live_src:
            fn = _adapter_mod.TelegramAdapter.send_inline_keyboard
        else:
            ns = dict(_adapter_mod.__dict__)
            exec(_INLINE_KEYBOARD_HELPER_BLOCK, ns)  # noqa: S102 - test-only, trusted local source
            fn = ns["send_inline_keyboard"]

        self.adapter.send_inline_keyboard = fn.__get__(self.adapter, _adapter_mod.TelegramAdapter)

    def _run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def test_keyboard_preserved_when_buttons_provided(self) -> None:
        buttons = [
            [{"text": "Hyperliquid", "callback_data": "exchange:hyperliquid"}],
            [{"text": "Arcus", "callback_data": "exchange:arcus"}],
            [{"text": "Exit", "callback_data": "exit"}],
        ]
        result = self._run(self.adapter.send_inline_keyboard(
            chat_id="123",
            text="Trade\n\nSelect Exchange:",
            buttons=buttons,
            callback_prefix="trade",
        ))
        self.assertTrue(result.success, f"expected success, got {result!r}")
        self.assertEqual(len(self.adapter._bot.calls), 1, "send_message must be called once")
        kwargs = self.adapter._bot.calls[0]
        self.assertIn("reply_markup", kwargs, "reply_markup must reach send_message")
        markup = kwargs["reply_markup"]
        self.assertIsInstance(markup, _StubKeyboardMarkup)
        # Every input row becomes one markup row, with the prefix applied
        # to each callback_data suffix.
        flat: List[tuple] = []
        for row in markup.inline_keyboard:
            for btn in row:
                flat.append((btn.text, btn.callback_data))
        self.assertEqual(
            flat,
            [
                ("Hyperliquid", "trade:exchange:hyperliquid"),
                ("Arcus", "trade:exchange:arcus"),
                ("Exit", "trade:exit"),
            ],
        )

    def test_empty_buttons_degrades_to_text_only(self) -> None:
        """No buttons → no reply_markup key, message still sends."""
        result = self._run(self.adapter.send_inline_keyboard(
            chat_id="123",
            text="Trade\n\nNo exchanges available.",
            buttons=[],
            callback_prefix="trade",
        ))
        self.assertTrue(result.success)
        kwargs = self.adapter._bot.calls[0]
        self.assertNotIn("reply_markup", kwargs)
        self.assertIn("No exchanges available.", kwargs["text"])

    def test_prefix_not_double_applied(self) -> None:
        """A callback already carrying the prefix must not be re-prefixed."""
        buttons = [
            [{"text": "Hyperliquid", "callback_data": "trade:exchange:hyperliquid"}],
        ]
        result = self._run(self.adapter.send_inline_keyboard(
            chat_id="123",
            text="x",
            buttons=buttons,
            callback_prefix="trade",
        ))
        self.assertTrue(result.success)
        kwargs = self.adapter._bot.calls[0]
        btn = kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(btn.callback_data, "trade:exchange:hyperliquid")

    def test_text_reaches_send_message(self) -> None:
        """The wizard's text MUST reach Telegram alongside the keyboard."""
        text = "Trade\n\nSelect Exchange:"
        result = self._run(self.adapter.send_inline_keyboard(
            chat_id="123",
            text=text,
            buttons=[[{"text": "X", "callback_data": "x:1"}]],
            callback_prefix="trade",
        ))
        self.assertTrue(result.success)
        self.assertIn("Select Exchange", self.adapter._bot.calls[0]["text"])


if __name__ == "__main__":
    unittest.main()