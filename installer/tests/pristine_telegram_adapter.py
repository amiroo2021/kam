"""Minimal pristine TelegramAdapter fixture for installer patch tests.

Contains EXACTLY the anchor lines required by installer/patchspecs.py and
NO pre-existing /trade dispatch seams. Used to prove fresh-install
wiring on a clean Hermes tree (Lodo regression).
"""

PRISTINE_TELEGRAM_ADAPTER = '''\
"""Synthetic pristine Hermes Telegram adapter (test fixture only)."""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class MessageType:
    TEXT = "text"
    COMMAND = "command"


class TelegramAdapter:
    """Minimal adapter body with the anchors KAM patches require."""

    name = "telegram"
    _bot = None
    _MODEL_PAGE_SIZE = 8

    async def _ensure_forum_commands(self, msg: Any) -> None:
        return None

    def _build_message_event(self, msg: Any, mtype: Any, update_id: Any = None) -> Any:
        return {"msg": msg, "type": mtype, "update_id": update_id}

    def _clean_bot_trigger_text(self, text: str) -> str:
        return text

    async def handle_message(self, event: Any) -> None:
        return None

    def _is_from_self(self, from_user: Any) -> bool:
        bot_id = getattr(self._bot, "id", None)
        user_id = getattr(from_user, "id", None)
        return bot_id is not None and user_id is not None and bot_id == user_id

    def _should_process_message(self, event: Any) -> bool:
        return True

    async def on_callback_query(self, update: Any) -> None:
        query = update.callback_query
        data = getattr(query, "data", "") or ""
        query_user_name = getattr(query.from_user, "first_name", None)
        # --- Model picker callbacks ---
        _ = (data, query_user_name)
        return None

    async def on_text_message(self, update: Any) -> None:
        msg = update.message
        await self._ensure_forum_commands(update.message)
        event = self._build_message_event(msg, MessageType.TEXT, update_id=update.update_id)
        event.text = self._clean_bot_trigger_text(getattr(msg, "text", "") or "")
        await self.handle_message(event)
        return None

    async def on_command_message(self, update: Any) -> None:
        msg = update.message
        await self._ensure_forum_commands(msg)
        event = self._build_message_event(msg, MessageType.COMMAND, update_id=update.update_id)
        await self.handle_message(event)
        return None
'''
