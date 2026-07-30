"""Approved anchor-based patch specifications for shared Hermes files.

Every spec here was validated in the Phase 1 review:

* ``anchor_before`` and ``anchor_after`` each occur EXACTLY once in the
  pristine target file,
* ``anchor_before`` always precedes ``anchor_after``,
* the inserted block must land between them.

These specs contain no exchange names. They wire the generic ``/trade``
command, the ``trade:`` callback namespace, and the wizard's free-text
interception. Everything exchange-specific lives inside
``plugins/trade/agents/x_*_agent.py`` and is discovered at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from kamlib import PatchSpec

TELEGRAM_ADAPTER = Path("plugins") / "platforms" / "telegram" / "adapter.py"
HERMES_COMMANDS = Path("hermes_cli") / "commands.py"


# --- Seam A: trade: inline-keyboard callbacks -------------------------------
_CALLBACK_BLOCK = '''\
if data.startswith("trade:"):
    try:
        from plugins.trade.wizard import handle_trade_callback

        await handle_trade_callback(self, query, data)
        return
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[%s] /trade callback dispatch failed: %s",
            self.name, exc, exc_info=True,
        )
        try:
            await query.answer()
        except Exception:
            pass
        return
'''

# --- Seam B: wizard free-text interception ---------------------------------
_TEXT_BLOCK = '''\
try:
    from plugins.trade.wizard import handle_trade_text

    if await handle_trade_text(self, msg):
        return
except Exception as exc:  # noqa: BLE001
    logger.error(
        "[%s] /trade text dispatch failed: %s",
        self.name, exc, exc_info=True,
    )
'''

# --- Seam C: /trade slash command -----------------------------------------
_COMMAND_BLOCK = '''\
# Match exactly: ``/trade``, ``/trade@botname``, or
# ``/trade<space>...`` -- but NOT ``/trader`` or ``/trades``.
raw_text = (msg.text or "").strip()
first_token = raw_text.split(None, 1)[0] if raw_text else ""
if first_token:
    cmd_body = first_token.lstrip("/").split("@", 1)[0].lower()
    if cmd_body == "trade":
        try:
            from plugins.trade.wizard import handle_trade_command

            handled = await handle_trade_command(self, msg)
            if handled:
                return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[%s] /trade command dispatch failed: %s",
                self.name, exc, exc_info=True,
            )
            # Fall through to normal dispatch rather than swallow.
'''

# --- Seam D: Telegram command-menu visibility ------------------------------
_COMMANDDEF_BLOCK = '''\
CommandDef("trade", "Open the Telegram trading console wizard", "Trading",
           gateway_only=True, gateway_platforms=("telegram",)),
'''


def adapter_specs() -> List[PatchSpec]:
    """The three Telegram adapter seams, in file order."""
    return [
        PatchSpec(
            seam="callback dispatch",
            relative_path=TELEGRAM_ADAPTER,
            anchor_before='query_user_name = getattr(query.from_user, "first_name", None)',
            anchor_after="# --- Model picker callbacks ---",
            block=_CALLBACK_BLOCK,
            native_sentinel="from plugins.trade.wizard import handle_trade_callback",
        ),
        PatchSpec(
            seam="wizard text interception",
            relative_path=TELEGRAM_ADAPTER,
            anchor_before="await self._ensure_forum_commands(update.message)",
            anchor_after=(
                "event = self._build_message_event(msg, MessageType.TEXT, "
                "update_id=update.update_id)"
            ),
            block=_TEXT_BLOCK,
            native_sentinel="from plugins.trade.wizard import handle_trade_text",
        ),
        PatchSpec(
            seam="slash command dispatch",
            relative_path=TELEGRAM_ADAPTER,
            anchor_before="await self._ensure_forum_commands(msg)",
            anchor_after=(
                "event = self._build_message_event(msg, MessageType.COMMAND, "
                "update_id=update.update_id)"
            ),
            block=_COMMAND_BLOCK,
            native_sentinel="from plugins.trade.wizard import handle_trade_command",
        ),
    ]


def commands_specs() -> List[PatchSpec]:
    """Telegram command-menu registration (cosmetic but approved)."""
    return [
        PatchSpec(
            seam="command menu entry",
            relative_path=HERMES_COMMANDS,
            anchor_before=(
                'CommandDef("new", "Start a new session (fresh session ID + history)", '
                '"Session",'
            ),
            anchor_after='CommandDef("topic", "Enable or inspect Telegram DM topic sessions", "Session",',
            block=_COMMANDDEF_BLOCK,
            native_sentinel='CommandDef("trade",',
        ),
    ]


def all_specs() -> List[PatchSpec]:
    return adapter_specs() + commands_specs()
