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

import ast
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional

from kamlib import InstallError, PatchSpec

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

# --- Seam D: Telegram command-menu visibility (LEGACY / OPT-IN ONLY) -------
#
# WARNING -- this patch is NO LONGER part of the default install path.
#
# The original block emitted ``gateway_platforms=("telegram",)``. That keyword
# does not exist on every Hermes build: on Hermes
# 2d404942471633d5338a8ff514ea7da24549274f, CommandDef.__init__ accepts
#
#     name, description, category, aliases, args_hint, subcommands,
#     cli_only, gateway_only, gateway_config_gate, busy_policy,
#     busy_handler, execute
#
# with NO gateway_platforms. Injecting it raised TypeError while the module-level
# COMMANDS list was being built, which took down the whole command registry and
# broke native commands such as /restart.
#
# ``/trade`` is now advertised through the supported plugin API instead:
# ``plugins/trade/__init__.py`` calls ``PluginContext.register_command`` and the
# installer enables the plugin via ``plugins.enabled`` in the Hermes config. See
# ``legacy_commands_specs()`` below -- retained only so an older KAM install can
# still be *detected and cleanly removed*, never applied by default.
_LEGACY_COMMANDDEF_BLOCK = '''\
CommandDef("trade", "Open the Telegram trading console wizard", "Trading",
           gateway_only=True, gateway_platforms=("telegram",)),
'''


def supported_commanddef_kwargs(command_def_cls: Any) -> set:
    """Return the keyword names ``command_def_cls`` actually accepts.

    Used to guarantee the installer never emits a keyword the target build's
    ``CommandDef`` cannot accept. Returns an empty set when the signature
    cannot be inspected, which callers must treat as "unknown -> skip".
    """
    try:
        sig = inspect.signature(command_def_cls.__init__)
    except (TypeError, ValueError):  # pragma: no cover - exotic builds
        return set()
    return {
        name for name, param in sig.parameters.items()
        if name != "self"
        and param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)
    }


def _load_command_def(hermes_root: Path) -> Optional[Any]:
    """Import ``CommandDef`` from *hermes_root* without polluting sys.modules.

    Returns ``None`` when the class cannot be loaded, which callers must treat
    as "compatibility unknown".
    """
    commands_py = hermes_root / HERMES_COMMANDS
    if not commands_py.is_file():
        return None
    probe = (
        "import sys, inspect, json\n"
        f"sys.path.insert(0, {str(hermes_root)!r})\n"
        "try:\n"
        "    from hermes_cli.commands import CommandDef\n"
        "    names = [n for n in inspect.signature(CommandDef.__init__).parameters\n"
        "             if n != 'self']\n"
        "    print(json.dumps(names))\n"
        "except Exception:\n"
        "    print(json.dumps(None))\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout.strip() or "null")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def commanddef_supports_gateway_platforms(hermes_root: Optional[Path] = None) -> bool:
    """True only when the target build's ``CommandDef`` accepts the keyword.

    Returns ``False`` when compatibility cannot be determined -- the optional
    menu patch is skipped rather than risk breaking native commands. That is
    the whole point: emitting this keyword against a build that lacks it raises
    TypeError while the command table is being built and takes ``/restart``
    down with it.
    """
    if hermes_root is None:
        return False
    params = _load_command_def(Path(hermes_root))
    if not params:
        return False
    return "gateway_platforms" in params


# --- Helper seam: inline-keyboard transport bridge --------------------------
#
# Ported from the validated powerkam live fix. This method must preserve BOTH
# the wizard's text and its InlineKeyboardMarkup, and it must never double-
# prefix a callback that already carries the plugin namespace.
#
# We inject it as a *later* method definition inside TelegramAdapter, just
# before `_MODEL_PAGE_SIZE`. On builds that already have a send_inline_keyboard
# helper, the later definition overrides the older one without rewriting the
# file wholesale. On builds that lack the helper entirely, it simply provides
# it. Uninstall removes only the marked block, restoring the pre-install file
# byte-for-byte.
_INLINE_KEYBOARD_HELPER_BLOCK = '''\
async def send_inline_keyboard(
    self,
    chat_id: str,
    text: str,
    buttons: Any,
    callback_prefix: str = "",
    *,
    metadata: Optional[Dict[str, Any]] = None,
    parse_mode: Optional[Any] = None,
) -> SendResult:
    """Plugin-facing inline-keyboard sender.

    Mirrors the native prompt senders so the KAM ``/trade`` wizard can render
    text + a multi-row ``InlineKeyboardMarkup`` through the same transport the
    built-in Hermes prompts use.

    ``buttons`` is a list-of-lists of ``{"text", "callback_data"}`` dicts.
    Empty/missing ``buttons`` gracefully degrades to a plain text message.
    A callback suffix already carrying ``callback_prefix`` is preserved as-is;
    otherwise the prefix is prepended exactly once.
    """
    if not self._bot:
        return SendResult(success=False, error="Not connected")

    rows: List[List[Any]] = []
    if buttons:
        for row in buttons:
            btn_row: List[Any] = []
            for btn in row or []:
                if not isinstance(btn, dict):
                    continue
                label = str(btn.get("text", "") or "")
                suffix = str(btn.get("callback_data", "") or "")
                if not label or not suffix:
                    continue
                if callback_prefix and not suffix.startswith(f"{callback_prefix}:"):
                    cb = f"{callback_prefix}:{suffix}"
                else:
                    cb = suffix
                btn_row.append(InlineKeyboardButton(label, callback_data=cb))
            if btn_row:
                rows.append(btn_row)

    try:
        formatted = self.format_message(text or "") if hasattr(self, "format_message") else (text or "")
        thread_id = self._metadata_thread_id(metadata)
        reply_to_id = self._reply_to_message_id_for_send(
            None, metadata, reply_to_mode=self._reply_to_mode
        )
        kwargs: Dict[str, Any] = {
            "chat_id": normalize_telegram_chat_id(chat_id),
            "text": formatted,
            **self._link_preview_kwargs(),
        }
        if rows:
            kwargs["reply_markup"] = InlineKeyboardMarkup(rows)
        kwargs["reply_to_message_id"] = reply_to_id
        if parse_mode is not None:
            kwargs["parse_mode"] = parse_mode
        else:
            kwargs["parse_mode"] = ParseMode.MARKDOWN_V2
        kwargs.update(
            self._thread_kwargs_for_send(
                chat_id,
                thread_id,
                metadata,
                reply_to_message_id=reply_to_id,
                reply_to_mode=self._reply_to_mode,
            )
        )
        msg = await self._send_message_with_thread_fallback(**kwargs)
        return SendResult(success=True, message_id=str(msg.message_id))
    except Exception as e:
        logger.warning(
            "[%s] send_inline_keyboard failed: %s",
            self.name, _redact_telegram_error_text(e),
        )
        return SendResult(success=False, error=_redact_telegram_error_text(e))
'''


INLINE_KEYBOARD_HELPER_SENTINEL = 'suffix.startswith(f"{callback_prefix}:")'


def native_presence_check_for_inline_keyboard(
    text: str, spec: PatchSpec
) -> Tuple[bool, str]:
    """Structural detection of a compatible send_inline_keyboard on TelegramAdapter.

    Returns:
      (True,  reason) when exactly one direct AsyncFunctionDef/FunctionDef
                       named send_inline_keyboard already exists on
                       TelegramAdapter and is structurally compatible. The
                       installer treats this as native-present.
      (None,  "")    when no direct send_inline_keyboard exists, so the
                       anchor-based patch path should proceed normally.
      (False, reason) when there are zero direct members but a nested
                       definition exists, or more than one direct definition
                       exists. The installer aborts with reason.
    """
    try:
        tree = ast.parse(text, filename=str(spec.relative_path))
    except SyntaxError as exc:
        return (False, f"module does not parse: {exc.msg} at line {exc.lineno}")

    adapter = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TelegramAdapter"),
        None,
    )
    if adapter is None:
        return (None, "")

    direct = [
        node for node in adapter.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "send_inline_keyboard"
    ]
    if len(direct) == 1:
        existing = direct[0]
        sig = (
            f"async={isinstance(existing, ast.AsyncFunctionDef)} "
            f"args={[a.arg for a in existing.args.args]} "
            f"kwonly={[a.arg for a in existing.args.kwonlyargs]} "
            f"returns={ast.unparse(existing.returns) if existing.returns else None}"
        )
        return (
            True,
            f"existing direct send_inline_keyboard at lines {existing.lineno}-{existing.end_lineno} on TelegramAdapter is structurally compatible ({sig})",
        )
    if len(direct) > 1:
        return (
            False,
            f"multiple direct send_inline_keyboard definitions found on TelegramAdapter ({len(direct)}); refusing to patch to avoid clobbering",
        )

    nested = [
        node for node in ast.walk(adapter)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "send_inline_keyboard"
    ]
    if nested:
        lines = ", ".join(str(n.lineno) for n in nested)
        return (
            False,
            f"send_inline_keyboard exists nested inside another method at line(s) {lines}, not as a direct TelegramAdapter member; refusing to patch",
        )
    return (None, "")


def validate_telegram_adapter_helper_scope(text: str, spec: PatchSpec) -> None:
    """Refuse helper installs unless the helper is a direct TelegramAdapter member."""
    try:
        tree = ast.parse(text, filename=str(spec.relative_path))
    except SyntaxError as exc:  # pragma: no cover - generic AST guard fires first
        raise InstallError(
            f"[{spec.seam}] AST validation failed for {spec.relative_path}: {exc.msg}. Refusing to patch."
        ) from exc

    adapter = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TelegramAdapter"),
        None,
    )
    if adapter is None:
        raise InstallError(
            f"[{spec.seam}] TelegramAdapter class not found in {spec.relative_path}. Refusing to patch."
        )

    direct = [
        node for node in adapter.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "send_inline_keyboard"
    ]
    if len(direct) == 1:
        return

    nested = any(
        isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "send_inline_keyboard"
        for node in ast.walk(adapter)
    )
    if nested:
        raise InstallError(
            f"[{spec.seam}] send_inline_keyboard landed at the wrong lexical scope in {spec.relative_path}; "
            "it is nested inside another method instead of being a direct member of TelegramAdapter. Refusing to patch."
        )
    raise InstallError(
        f"[{spec.seam}] send_inline_keyboard is not a direct member of TelegramAdapter in {spec.relative_path}. Refusing to patch."
    )


def _choose_helper_anchor(hermes_root: Optional[Path]) -> tuple[str, str]:
    """Pick a portable insertion anchor for the helper override.

    Real Hermes builds differ substantially. The validated powerkam diff added
    the helper near the model-picker section; the synthetic clean fixture used in
    unit tests has neither that section nor a pre-existing helper. We therefore
    inspect the target adapter and choose a stable line that exists in that
    specific file.
    """
    if hermes_root is not None:
        try:
            text = (Path(hermes_root) / TELEGRAM_ADAPTER).read_text()
        except OSError:
            text = ""
        if "    _MODEL_PAGE_SIZE = 8" in text:
            return (
                "        bot_id = getattr(self._bot, \"id\", None)\n"
                "        user_id = getattr(from_user, \"id\", None)\n"
                "        return bot_id is not None and user_id is not None and bot_id == user_id",
                "    def _should_process_message",
            )
        if "    async def handle_message(self, event):" in text and "        return None" in text:
            return (
                "        event.text = self._clean_bot_trigger_text(event.text)\n"
                "        await self.handle_message(event)",
                "    def _should_process_message",
            )
    # Conservative fallback for unknown adapters: insert immediately before the
    # message-trigger gate when present.
    return ("    return bot_id is not None and user_id is not None and bot_id == user_id",
            "    def _should_process_message")


def helper_specs(hermes_root: Optional[Path] = None) -> List[PatchSpec]:
    """Adapter helper overrides required for validated /trade behavior."""
    anchor_before, anchor_after = _choose_helper_anchor(hermes_root)
    return [
        PatchSpec(
            seam="inline keyboard helper",
            relative_path=TELEGRAM_ADAPTER,
            anchor_before=anchor_before,
            anchor_after=anchor_after,
            block=_INLINE_KEYBOARD_HELPER_BLOCK,
            insertion_indent="    ",
            native_sentinel=INLINE_KEYBOARD_HELPER_SENTINEL,
            ast_validator=validate_telegram_adapter_helper_scope,
            native_presence_check=native_presence_check_for_inline_keyboard,
        ),
    ]


def adapter_specs() -> List[PatchSpec]:
     """The three Telegram adapter seams, in file order."""
     return [
        PatchSpec(
            seam="callback dispatch",
            relative_path=TELEGRAM_ADAPTER,
            anchor_before='query_user_name = getattr(query.from_user, "first_name", None)',
            anchor_after="# --- Model picker callbacks ---",
            block=_CALLBACK_BLOCK,
            insertion_indent="        ",
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
            insertion_indent="        ",
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
            insertion_indent="        ",
            native_sentinel="from plugins.trade.wizard import handle_trade_command",
        ),
    ]


def legacy_commands_specs() -> List[PatchSpec]:
    """The retired ``hermes_cli/commands.py`` patch.

    NOT applied by default. Exposed so the uninstaller can find and strip a
    marked block left behind by an older KAM version, and so tests can assert
    that the default install path does not include it.
    """
    return [
        PatchSpec(
            seam="command menu entry",
            relative_path=HERMES_COMMANDS,
            anchor_before=(
                'CommandDef("new", "Start a new session (fresh session ID + history)", '
                '"Session",'
            ),
            anchor_after='CommandDef("topic", "Enable or inspect Telegram DM topic sessions", "Session",',
            block=_LEGACY_COMMANDDEF_BLOCK,
            insertion_indent="    ",
            native_sentinel='CommandDef("trade",',
        ),
    ]


# Backwards-compatible alias. Returns an EMPTY list: the command-menu patch is
# no longer part of any install path.
def commands_specs() -> List[PatchSpec]:
    """Deprecated. ``/trade`` is advertised via the plugin API instead."""
    return []


def all_specs(hermes_root: Optional[Path] = None) -> List[PatchSpec]:
    """Specs applied by a default install.

    The default install path patches Telegram in two layers:

    1. the three /trade dispatch seams; and
    2. the validated ``send_inline_keyboard`` helper override.

    The ``hermes_cli/commands.py`` menu patch is **never** included by default:
    ``/trade`` is advertised through ``PluginContext.register_command`` plus
    ``plugins.enabled`` instead.

    *hermes_root* is accepted so callers can pass the install target. It is used
    only for compatibility-aware tests; even on a build that *does* support
    ``gateway_platforms`` the legacy patch stays out of the default path,
    because plugin-API registration already covers the menu and touching a
    shared 100+ command table is strictly riskier.
    """
    return adapter_specs() + helper_specs(hermes_root)
