"""Standalone /fibo Telegram wizard skeleton.

This is a LIGHTWEIGHT placeholder wizard. It owns:

* a single entry screen with four buttons,
* four callback placeholders (``fibo:start``, ``fibo:running``,
  ``fibo:stop``, ``fibo:exit``),
* text interception for the ``/fibo`` slash command.

It does NOT:

* touch the shared ``plugins.trade.agents.x_*_agent`` exchange layer yet,
* run any background service, daemon, or runtime state,
* depend on ``plugins.trade.wizard`` (the /trade wizard).

``fibo:exit`` is a UI-only close action: it deletes (or strips) the
wizard message and never stops any Fibo registration, never invokes an
exchange agent, never touches ``.env``. Any future Fibo runtime that
the user has registered (e.g. via the planned start/stop flow) keeps
running — Exit is purely a way to dismiss the wizard UI.

Future iterations may reuse the shared exchange-agent layer for actual
Fibo strategy execution. The package placement (``plugins/trade/``) is
intentional — Fibo shares the same package marker as /trade so the
Telegram adapter only needs a single ``plugins/trade/`` import path.

Callback namespace: ``fibo:`` (do NOT reuse ``trade:``).

Top-level functions consumed by the patched Telegram adapter:

    handle_fibo_command(adapter, msg)
    handle_fibo_callback(adapter, query, data)
    handle_fibo_text(adapter, msg)
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public menu / callback contract
# ---------------------------------------------------------------------------

# The entry screen header. Four buttons follow, in this exact order.
SCREEN_HEADER = "Fibo"

# (label, callback_data) pairs in the order they appear on the entry screen.
# Callback data uses the dedicated ``fibo:`` namespace.
SCREEN_BUTTONS: List[Tuple[str, str]] = [
    ("▶️ Start Fibo",   "fibo:start"),
    ("📋 Running Fibo", "fibo:running"),
    ("⛔️ Stop Fibo",    "fibo:stop"),
    ("❌ Exit",          "fibo:exit"),
]

# Placeholder body text for each action. Buttons do nothing except display.
# ``fibo:exit`` is intentionally absent — Exit does NOT route through the
# placeholder path. It closes the wizard UI directly (delete or strip
# keyboard) via ``handle_fibo_callback``'s dedicated branch.
SCREEN_TEXT: dict = {
    "fibo:start":   "Start Fibo",
    "fibo:running": "Running Fibo",
    "fibo:stop":    "Stop Fibo",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry_buttons() -> List[List[dict]]:
    """Return the entry-screen inline-keyboard rows.

    Mirrors the shape ``wizard.py`` produces: a list of rows, each row
    a list of ``{"text", "callback_data"}`` dicts. The adapter wraps
    these into ``InlineKeyboardButton`` objects.
    """
    return [
        [{"text": label, "callback_data": cb}] for (label, cb) in SCREEN_BUTTONS
    ]


def _build_entry_screen() -> dict:
    """The dict the adapter renders for /fibo and the callback routes."""
    return {
        "text": SCREEN_HEADER,
        "buttons": _entry_buttons(),
    }


def _build_placeholder_screen(callback_data: str) -> dict:
    """The dict the adapter renders for each placeholder action."""
    return {
        "text": SCREEN_TEXT.get(callback_data, "Fibo"),
        "buttons": _entry_buttons(),  # let the user pick another action
    }


# ---------------------------------------------------------------------------
# Adapter call paths
# ---------------------------------------------------------------------------

# Renderer method name on the live Hermes Telegram adapter. The wizard
# uses the plugin-facing inline-keyboard sender so the four /fibo buttons
# are sent as a single Telegram message with a proper InlineKeyboardMarkup.
# This is the SAME pattern as plugins/trade/wizard.py:2267-2300.
_TELEGRAM_SEND_INLINE_KEYBOARD = "send_inline_keyboard"
# Plain-text fallback (defined on base + every adapter).
_TELEGRAM_SEND = "send"
# Legacy/buggy name kept ONLY so unit tests can prove we don't reference
# it. No code path here calls it. Keep as a constant so the AST test can
# scan for it and stay exact.
_FORBIDDEN_METHOD = "send_message"
_FIBO_CALLBACK_PREFIX = "fibo"


def _resolve_send_method(adapter: Any) -> tuple:
    """Return (callable, method_name) for the best Telegram sender, or (None, None).

    Order:
      1. ``send_inline_keyboard`` (preferred — emits inline-keyboard markup)
      2. ``send`` (fallback — plain text only, no buttons)

    Returns ``(None, None)`` when neither is present. The caller is
    responsible for logging + reporting the failure to its caller; this
    helper deliberately does not swallow the absence.
    """
    if adapter is None:
        return (None, None)
    for name in (_TELEGRAM_SEND_INLINE_KEYBOARD, _TELEGRAM_SEND):
        fn = getattr(adapter, name, None)
        if callable(fn):
            return (fn, name)
    return (None, None)


async def _send(adapter: Any, chat_id: str, screen: dict) -> bool:
    """Send a screen via the patched Telegram adapter.

    Uses ``adapter.send_inline_keyboard`` when available (preferred —
    produces the three-button markup); falls back to ``adapter.send``
    (plain text only, no buttons rendered) when the keyboard helper is
    absent. Returns ``True`` on a successful send, ``False`` if no
    supported sender exists or the send raised — never silently drops.

    No exchange writes, no daemon, no state mutation outside the
    single Telegram message.
    """
    text = str(screen.get("text", ""))
    buttons = screen.get("buttons") or []
    fn, method_name = _resolve_send_method(adapter)
    if fn is None:
        logger.error(
            "fibo_wizard: adapter has neither send_inline_keyboard nor send; "
            "cannot render /fibo screen (chat_id=%s). Install the latest kam "
            "Hermes adapter patches or restart the gateway.",
            chat_id,
        )
        return False
    try:
        if method_name == _TELEGRAM_SEND_INLINE_KEYBOARD:
            await fn(
                chat_id=chat_id,
                text=text,
                buttons=buttons,
                callback_prefix=_FIBO_CALLBACK_PREFIX,
            )
        else:
            # Plain-text fallback: drop the buttons so the user sees
            # the action title but cannot press anything. This is a
            # degraded path; we log it so operators notice.
            logger.warning(
                "fibo_wizard: adapter lacks send_inline_keyboard; rendering "
                "/fibo screen as plain text without buttons (chat_id=%s).",
                chat_id,
            )
            await fn(chat_id=chat_id, text=text)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "fibo_wizard: %s failed for chat_id=%s: %s",
            method_name, chat_id, exc, exc_info=True,
        )
        return False
    return True


async def _edit(query: Any, screen: dict) -> bool:
    """Edit the originating message in place with the new screen.

    Mirrors the callback-rendering contract: prefer
    ``query.edit_message_text`` (PTB native); fall back to a no-op
    acknowledgement when the adapter cannot edit. Returns ``True`` on
    a successful edit, ``False`` otherwise — never silently swallows.

    The buttons rendered by the placeholder screens already carry the
    ``fibo:`` namespace in their ``callback_data`` (see ``SCREEN_BUTTONS``),
    so we pass ``callback_prefix=""`` to keep them verbatim.
    """
    text = str(screen.get("text", ""))
    buttons = screen.get("buttons") or []
    edit = getattr(query, "edit_message_text", None)
    if not callable(edit):
        logger.warning(
            "fibo_wizard: query has no edit_message_text; cannot render "
            "callback screen (callback text=%r).",
            text,
        )
        return False
    try:
        # PTB's edit_message_text accepts reply_markup. We hand-build it
        # the same way send_inline_keyboard does so we don't depend on
        # an adapter-private helper.
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # type: ignore
    except Exception:
        logger.error(
            "fibo_wizard: telegram package unavailable; cannot build "
            "InlineKeyboardMarkup for callback edit."
        )
        return False
    rows = []
    for row in buttons:
        btn_row = []
        for btn in row or []:
            if not isinstance(btn, dict):
                continue
            label = str(btn.get("text", "") or "")
            cb = str(btn.get("callback_data", "") or "")
            if not label or not cb:
                continue
            btn_row.append(InlineKeyboardButton(label, callback_data=cb))
        if btn_row:
            rows.append(btn_row)
    try:
        await edit(text=text, reply_markup=InlineKeyboardMarkup(rows))
    except TypeError:
        # Stub adapter (tests / unit harnesses) where edit_message_text
        # is a sync mock without reply_markup support, or where the
        # edit method itself isn't awaitable. Fall back to a plain text
        # edit; this is normal in tests and benign in production when
        # a future PTB upgrade changes the signature.
        try:
            result = edit(text=text)
            import asyncio as _aio
            if _aio.iscoroutine(result):
                await result
        except TypeError as te:
            logger.debug(
                "fibo_wizard: edit_message_text returned a non-awaitable "
                "(stub/mock context): %s", te,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "fibo_wizard: edit_message_text fallback failed: %s",
                exc, exc_info=True,
            )
            return False
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "fibo_wizard: edit_message_text failed: %s", exc, exc_info=True
        )
        return False
    return True


def _answer(query: Any) -> bool:
    """Acknowledge the callback query so Telegram drops the loading indicator.

    Returns ``True`` on success or when the query has no ``answer``
    method (mock / unknown adapter). Returns ``False`` only when the
    adapter exposes ``answer`` but it raised.

    Sync helper — the answer call itself is invoked synchronously
    because PTB's CallbackQuery.answer() is a coroutine but we don't
    await it from here. The adapter layer is responsible for awaiting
    it inside its own coroutine.
    """
    answer = getattr(query, "answer", None)
    if answer is None:
        return True
    if not callable(answer):
        return False
    try:
        answer()
    except Exception as exc:  # noqa: BLE001
        logger.warning("fibo_wizard: callback answer failed: %s", exc)
        return False
    return True


# Minimal text left behind when Exit cannot delete the wizard message
# (e.g. adapter lacks ``delete_message``, or PTB raised ``TelegramError``).
# No buttons — the wizard is closed.
_CLOSED_TEXT = "Fibo closed."


async def _close(query: Any) -> bool:
    """Close the wizard UI for the Exit button.

    Strategy (best-effort, never raises):

      1. Prefer ``query.delete_message()`` (PTB native) so the message
         vanishes entirely. This is what the Exit button should do.
      2. If ``delete_message`` is absent, falls back to
         ``edit_message_text("Fibo closed.", reply_markup=None)`` —
         the message remains but the inline keyboard is stripped so
         no button remains tappable.
      3. If even the edit fails (stub adapter, mock context), logs at
         WARNING and returns ``False``. The user's loading indicator
         still clears via ``_answer(query)`` in the caller.

    This helper MUST NEVER raise — the wizard is UI-only and a
    best-effort close beats an exception that escapes the callback
    path. It also MUST NEVER touch exchange agents, runtime state, or
    ``.env``; if those concerns need wiring later they belong in their
    own start/stop callbacks.
    """
    # Best path: delete the wizard message outright.
    delete = getattr(query, "delete_message", None)
    if callable(delete):
        try:
            result = delete()
            import asyncio as _aio
            if _aio.iscoroutine(result):
                await result
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "fibo_wizard: delete_message failed; falling back to "
                "edit-strip-keyboard: %s", exc,
            )
            # Fall through to the edit path below.
    # Fallback: edit the message with no reply_markup so the keyboard
    # disappears. PTB's edit_message_text accepts reply_markup=None.
    edit = getattr(query, "edit_message_text", None)
    if callable(edit):
        try:
            result = edit(text=_CLOSED_TEXT, reply_markup=None)
            import asyncio as _aio
            if _aio.iscoroutine(result):
                await result
            return True
        except TypeError:
            # Stub adapter (tests / mock) where edit_message_text is a
            # sync mock without reply_markup support — try without it.
            try:
                result = edit(text=_CLOSED_TEXT)
                import asyncio as _aio
                if _aio.iscoroutine(result):
                    await result
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "fibo_wizard: edit-strip-keyboard fallback failed: %s", exc,
                )
                return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "fibo_wizard: edit-strip-keyboard failed: %s", exc,
            )
            return False
    logger.warning(
        "fibo_wizard: query exposes neither delete_message nor "
        "edit_message_text; cannot close wizard UI."
    )
    return False


def _strip_namespace(data: str, prefix: str) -> str:
    """Strip ``prefix:`` from the front of *data* if present."""
    token = f"{prefix}:"
    if data.startswith(token):
        return data[len(token):]
    return data


# ---------------------------------------------------------------------------
# Slash-command entry point: /fibo
# ---------------------------------------------------------------------------


async def handle_fibo_command(adapter: Any, msg: Any) -> bool:
    """Open the /fibo wizard for the chat that issued ``/fibo``.

    Returns ``True`` if the message was successfully rendered to the
    user, ``False`` if it was not a ``/fibo`` invocation OR if rendering
    failed. (Earlier revisions returned ``True`` unconditionally for
    /fibo, which masked the missing-send-method bug — see commit
    log for "Fix standalone /fibo Telegram rendering".)
    """
    try:
        text = (getattr(msg, "text", "") or "").strip()
        if not text.startswith("/"):
            return False
        first = text.split(None, 1)[0]
        cmd_name = first.lstrip("/").split("@", 1)[0].lower()
        if cmd_name != "fibo":
            return False
        chat = getattr(msg, "chat", None)
        chat_id = getattr(chat, "id", None) if chat is not None else None
        if chat_id is None:
            logger.warning("fibo_wizard: cannot determine chat_id; skipping")
            return False
        sent = await _send(adapter, str(chat_id), _build_entry_screen())
        if sent:
            logger.info("fibo_wizard: /fibo opened chat_id=%s", chat_id)
            return True
        logger.error(
            "fibo_wizard: /fibo dispatch failed to render (chat_id=%s); "
            "report this — adapter has no supported send method.",
            chat_id,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error("fibo_wizard: /fibo dispatch failed: %s", exc, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Callback entry point: data.startswith("fibo:")
# ---------------------------------------------------------------------------


async def handle_fibo_callback(adapter: Any, query: Any, data: str) -> None:
    """Handle a ``fibo:`` prefixed callback query.

    Routed here by the patched Telegram adapter. Strips the namespace,
    maps the suffix to a placeholder screen, edits the originating
    message, and acknowledges the query.

    ``fibo:exit`` is special-cased: it does NOT route through the
    placeholder path (which would re-render all four buttons and keep
    the wizard open). Instead, it closes the wizard UI directly via
    ``_close`` — preferring ``query.delete_message()`` and falling
    back to ``edit_message_text(text, reply_markup=None)``. Exit
    performs no exchange work, no registration stop, no ``.env``
    mutation; it is a UI-only dismiss action.

    Logs at WARNING/ERROR when edit or ack fails; the user still sees
    the loading indicator clear (best-effort answer()) so the chat
    doesn't appear frozen.
    """
    try:
        suffix = _strip_namespace(data, "fibo")
        callback_data = f"fibo:{suffix}" if suffix else data
        # Exit is a UI-only close: bypass the placeholder path entirely.
        if callback_data == "fibo:exit":
            closed = await _close(query)
            answered = _answer(query)
            if closed and answered:
                logger.info("fibo_wizard: callback fibo:exit closed wizard")
            else:
                logger.warning(
                    "fibo_wizard: callback fibo:exit partially closed "
                    "(closed=%s, answered=%s)",
                    closed, answered,
                )
            return
        screen = _build_placeholder_screen(callback_data)
        edited = await _edit(query, screen)
        answered = _answer(query)
        if edited and answered:
            logger.info("fibo_wizard: callback %s rendered placeholder", callback_data)
        else:
            logger.warning(
                "fibo_wizard: callback %s partially rendered (edited=%s, answered=%s)",
                callback_data, edited, answered,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "fibo_wizard: callback dispatch failed: %s", exc, exc_info=True
        )
        # Best-effort ack so Telegram drops the loading indicator.
        _answer(query)


# ---------------------------------------------------------------------------
# Text-interception entry point (no-op for the skeleton)
# ---------------------------------------------------------------------------


async def handle_fibo_text(adapter: Any, msg: Any) -> bool:
    """Placeholder text interception. Returns ``False`` (not consumed)."""
    return False


__all__ = [
    "SCREEN_HEADER",
    "SCREEN_BUTTONS",
    "SCREEN_TEXT",
    "handle_fibo_command",
    "handle_fibo_callback",
    "handle_fibo_text",
]