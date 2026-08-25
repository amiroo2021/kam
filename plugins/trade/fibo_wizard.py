"""Standalone /fibo Telegram wizard skeleton.

This is a LIGHTWEIGHT placeholder wizard. It owns:

* a single entry screen with three buttons,
* three callback placeholders (``fibo:start``, ``fibo:running``, ``fibo:stop``),
* text interception for the ``/fibo`` slash command.

It does NOT:

* touch the shared ``plugins.trade.agents.x_*_agent`` exchange layer yet,
* run any background service, daemon, or runtime state,
* depend on ``plugins.trade.wizard`` (the /trade wizard).

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

# The entry screen header. Three buttons follow, in this exact order.
SCREEN_HEADER = "Fibo"

# (label, callback_data) pairs in the order they appear on the entry screen.
# Callback data uses the dedicated ``fibo:`` namespace.
SCREEN_BUTTONS: List[Tuple[str, str]] = [
    ("▶️ Start Fibo",   "fibo:start"),
    ("📋 Running Fibo", "fibo:running"),
    ("⛔️ Stop Fibo",    "fibo:stop"),
]

# Placeholder body text for each action. Buttons do nothing except display.
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


async def _send(adapter: Any, chat_id: str, screen: dict) -> None:
    """Send a screen to the given chat via the patched Telegram adapter.

    Falls back to a no-op if the adapter lacks ``send_message`` so that
    offline unit tests can drive the wizard with a stub adapter.
    """
    send = getattr(adapter, "send_message", None)
    if not callable(send):
        logger.debug("fibo_wizard: adapter has no send_message; skipping send")
        return
    text = str(screen.get("text", ""))
    buttons = screen.get("buttons") or []
    try:
        await send(chat_id=chat_id, text=text, buttons=buttons)
    except Exception as exc:  # noqa: BLE001
        logger.error("fibo_wizard: send failed: %s", exc, exc_info=True)


def _edit(query: Any, screen: dict) -> None:
    """Edit the originating message in place. Mirrors wizard.py's pattern."""
    edit = getattr(query, "edit_message_text", None)
    if not callable(edit):
        return
    text = str(screen.get("text", ""))
    buttons = screen.get("buttons") or []
    try:
        # Lightweight edit; the adapter helper builds the markup.
        edit(text=text, buttons=buttons)
    except Exception:  # noqa: BLE001
        pass


def _answer(query: Any) -> None:
    answer = getattr(query, "answer", None)
    if callable(answer):
        try:
            answer()
        except Exception:  # noqa: BLE001
            pass


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

    Returns ``True`` if the message was consumed, ``False`` if it was
    not a ``/fibo`` invocation (in which case the adapter should
    continue with normal dispatch).
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
            logger.warning("fibo wizard: cannot determine chat_id; skipping")
            return True
        await _send(adapter, str(chat_id), _build_entry_screen())
        logger.info("fibo wizard: /fibo opened chat_id=%s", chat_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("fibo wizard: /fibo dispatch failed: %s", exc, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Callback entry point: data.startswith("fibo:")
# ---------------------------------------------------------------------------


async def handle_fibo_callback(adapter: Any, query: Any, data: str) -> None:
    """Handle a ``fibo:`` prefixed callback query.

    Routed here by the patched Telegram adapter. Strips the namespace,
    maps the suffix to a placeholder screen, edits the originating
    message, and acknowledges the query.
    """
    try:
        suffix = _strip_namespace(data, "fibo")
        callback_data = f"fibo:{suffix}" if suffix else data
        screen = _build_placeholder_screen(callback_data)
        _edit(query, screen)
        _answer(query)
        logger.info("fibo wizard: callback %s rendered placeholder", callback_data)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "fibo wizard: callback dispatch failed: %s", exc, exc_info=True
        )
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