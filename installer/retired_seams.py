"""Removal-only patch specs for retired KAM Telegram seams.

The /fibo capability was removed together with its MT4 reader. A deployment
installed by an older KAM version still carries the marked /fibo dispatch
blocks in its Telegram adapter, and those blocks import a module that no
longer exists -- the deployed adapter would fail to import.

The specs in this module are NEVER applied by an install path. They exist so
the installer can strip an orphaned seam during upgrade or uninstall. Each
spec deliberately carries an empty ``native_sentinel``: an empty sentinel
makes ``apply_patch`` treat the seam as natively present and leave it
untouched, so an accidental apply is a no-op rather than a re-install of a
removed feature.

Keeping these specs in their own module means the live installer modules
(``patchspecs``, ``adapter_wiring``, ...) carry no reference to the removed
feature at all.
"""

from pathlib import Path
import sys
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kamlib import PatchSpec  # noqa: E402
from patchspecs import TELEGRAM_ADAPTER  # noqa: E402


def _read_adapter_text(hermes_root: Optional[Path]) -> str:
    if hermes_root is None:
        return ""
    try:
        adapter = Path(hermes_root) / TELEGRAM_ADAPTER
        return adapter.read_text(encoding="utf-8")
    except OSError:
        return ""


def _choose_unique(
    text: str, candidates: List[str], seam: str
) -> str:
    """Return the single candidate present in ``text``."""
    hits = [c for c in candidates if c and c in text]
    if len(hits) == 1:
        return hits[0]
    if hits:
        return hits[0]
    return candidates[0]


# The exact text of the marked seams an older KAM wrote into the deployed
# Telegram adapter. These are inert strings used purely as text-matching
# sentinels for removal; nothing imports them.
_RETIRED_FIBO_CALLBACK_BLOCK = '''\
if data.startswith("fibo:"):
    try:
        from plugins.trade.fibo_wizard import handle_fibo_callback

        await handle_fibo_callback(self, query, data)
        return
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[%s] /fibo callback dispatch failed: %s",
            self.name, exc, exc_info=True,
        )
        try:
            await query.answer()
        except Exception:
            pass
        return
'''

_RETIRED_FIBO_TEXT_BLOCK = '''\
try:
    from plugins.trade.fibo_wizard import handle_fibo_text

    if await handle_fibo_text(self, msg):
        return
except Exception as exc:  # noqa: BLE001
    logger.error(
        "[%s] /fibo text dispatch failed: %s",
        self.name, exc, exc_info=True,
    )
'''

_RETIRED_FIBO_COMMAND_BLOCK = '''\
raw_text = (msg.text or "").strip()
first_token = raw_text.split(None, 1)[0] if raw_text else ""
if first_token:
    cmd_body = first_token.lstrip("/").split("@", 1)[0].lower()
    if cmd_body == "fibo":
        try:
            from plugins.trade.fibo_wizard import handle_fibo_command

            handled = await handle_fibo_command(self, msg)
            if handled:
                return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[%s] /fibo command dispatch failed: %s",
                self.name, exc, exc_info=True,
            )
            # Fall through to normal dispatch rather than swallow.
'''


def retired_fibo_adapter_specs(hermes_root: Optional[Path] = None) -> List[PatchSpec]:
    """Return removal-only specs for the retired /fibo Telegram seams."""
    adapter_text = _read_adapter_text(hermes_root)

    cb_before = _choose_unique(
        adapter_text,
        [
            'query_user_name = getattr(query.from_user, "first_name", None)',
            "query_user_name = getattr(query.from_user, 'first_name', None)",
            'query_user_name = getattr(query.from_user, "full_name", None)',
            'query_thread_id = getattr(query_message, "message_thread_id", None)',
            'query_chat_type = getattr(query_chat, "type", None)',
        ],
        "fibo callback dispatch",
    )
    cb_after = _choose_unique(
        adapter_text,
        [
            "# --- Model picker callbacks ---",
            'if data.startswith(("mp:", "mpg:", "mpv:", "mm:", "mc:", "mb", "mx", "mg:")):',
            'if data.startswith(("mp:",',
            'if data.startswith("mp:")',
            "# --- Generic choice picker callbacks",
            'if data.startswith("cp:"):',
        ],
        "fibo callback dispatch",
    )

    text_before = _choose_unique(
        adapter_text,
        [
            'text = getattr(message, "text", None)',
            'text = getattr(message, "text", "") or ""',
            'text = getattr(update.message, "text", None)',
        ],
        "fibo text interception",
    )
    text_after = _choose_unique(
        adapter_text,
        [
            "# --- KAM text dispatch ---",
            "if text and text.startswith",
            "if text:",
        ],
        "fibo text interception",
    )

    cmd_before = _choose_unique(
        adapter_text,
        [
            'raw_text = (msg.text or "").strip()',
            'raw_text = (message.text or "").strip()',
            'first_token = raw_text.split(None, 1)[0]',
        ],
        "fibo slash command dispatch",
    )
    cmd_after = _choose_unique(
        adapter_text,
        [
            "await self._ensure_forum_commands(msg)",
            "await self._ensure_forum_commands(update.message)",
            "_ensure_forum_commands",
            "is_command=True",
        ],
        "fibo slash command dispatch",
    )

    return [
        PatchSpec(
            seam="fibo callback dispatch",
            relative_path=TELEGRAM_ADAPTER,
            anchor_before=cb_before,
            anchor_after=cb_after,
            block=_RETIRED_FIBO_CALLBACK_BLOCK,
            insertion_indent="        ",
            native_sentinel="",
            method_name="_handle_callback_query",
            method_name_candidates=[
                "_handle_callback_query",
                "handle_callback_query",
                "_on_callback_query",
            ],
            method_after_substrings=[
                "data = query.data",
                "query.data",
                "callback_query",
            ],
        ),
        PatchSpec(
            seam="fibo text interception",
            relative_path=TELEGRAM_ADAPTER,
            anchor_before=text_before,
            anchor_after=text_after,
            block=_RETIRED_FIBO_TEXT_BLOCK,
            insertion_indent="        ",
            native_sentinel="",
            method_name="_handle_text",
            method_name_candidates=[
                "_handle_text",
                "_handle_text_message",
                "_on_text",
                "handle_text",
            ],
            method_after_substrings=[
                "text = getattr(message, \"text\", None)",
                "message.text",
                "_handle_text",
            ],
        ),
        PatchSpec(
            seam="fibo slash command dispatch",
            relative_path=TELEGRAM_ADAPTER,
            anchor_before=cmd_before,
            anchor_after=cmd_after,
            block=_RETIRED_FIBO_COMMAND_BLOCK,
            insertion_indent="        ",
            native_sentinel="",
            method_name="_handle_command",
            method_name_candidates=[
                "_handle_command",
                "handle_command",
                "_on_command",
            ],
            method_after_substrings=[
                "await self._ensure_forum_commands(msg)",
                "await self._ensure_forum_commands(update.message)",
                "_ensure_forum_commands",
                "is_command=True",
            ],
        ),
    ]
