"""Safe Hermes config handling for the KAM /trade add-on.

``/trade`` is advertised through the supported Hermes plugin API:
``plugins/trade/__init__.py`` calls ``PluginContext.register_command`` and the
plugin must be listed under ``plugins.enabled`` in the Hermes config for that
``register()`` to run.

This module owns that config edit. Its contract:

* preserve every other top-level key, byte-for-byte where possible
* preserve every other enabled plugin
* add ``trade`` exactly once, never duplicating the key or the entry
* handle a missing ``plugins`` section, a missing ``enabled`` list, block-list
  form, and inline/flow-list form
* record whether ``trade`` was already enabled *before* KAM touched anything, so
  uninstall never removes a user-owned enablement
* fail safely and loudly on malformed YAML rather than clobbering the file
* be idempotent

We deliberately do a *targeted textual* edit rather than a parse-and-redump.
``yaml.safe_dump`` would reorder keys, drop comments, and rewrite quoting across
the operator's entire config; that is unacceptable for a file holding their
Telegram and model settings. YAML is used to *validate* and to *read state*, and
the write itself is a minimal line insertion.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

PLUGIN_NAME = "trade"

CONFIG_CANDIDATES = ("config.yaml", "config.yml")


class ConfigError(RuntimeError):
    """Raised when the Hermes config cannot be safely read or edited."""


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

def find_config(hermes_home: Path) -> Optional[Path]:
    """Locate the real Hermes config file under *hermes_home*."""
    for name in CONFIG_CANDIDATES:
        candidate = hermes_home / name
        if candidate.is_file():
            return candidate
    return None


def parse_config(path: Path) -> Dict[str, Any]:
    """Safely parse the config. Raises :class:`ConfigError` when malformed."""
    if yaml is None:
        raise ConfigError("PyYAML is not available; cannot parse the Hermes config")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"malformed YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} does not contain a YAML mapping at the top level")
    return data


def enabled_plugins(config: Dict[str, Any]) -> List[str]:
    """Return the currently enabled plugin names (order preserved)."""
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        return []
    enabled = plugins.get("enabled")
    if enabled is None:
        return []
    if isinstance(enabled, str):
        return [enabled.strip()] if enabled.strip() else []
    if isinstance(enabled, list):
        return [str(item).strip() for item in enabled if str(item).strip()]
    return []


def is_trade_enabled(config: Dict[str, Any]) -> bool:
    return PLUGIN_NAME in enabled_plugins(config)


# ---------------------------------------------------------------------------
# editing
# ---------------------------------------------------------------------------

_PLUGINS_KEY = re.compile(r"^plugins:[ \t]*(.*)$")
_ENABLED_KEY = re.compile(r"^([ \t]+)enabled:[ \t]*(.*)$")
_TOP_LEVEL_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*:")


def _plugins_block_bounds(lines: List[str]) -> Optional[Tuple[int, int]]:
    """Return ``(start, end)`` line indices of the top-level ``plugins:`` block.

    ``end`` is exclusive. Raises :class:`ConfigError` if more than one
    top-level ``plugins:`` key exists (ambiguous / already corrupt).
    """
    starts = [i for i, line in enumerate(lines) if _PLUGINS_KEY.match(line)]
    if not starts:
        return None
    if len(starts) > 1:
        raise ConfigError(
            f"config has {len(starts)} top-level 'plugins:' keys; refusing to edit"
        )
    start = starts[0]
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        if _TOP_LEVEL_KEY.match(line):  # next top-level key
            end = i
            break
    return start, end


def plan_enable_trade(text: str) -> Tuple[str, str]:
    """Return ``(new_text, action)`` enabling ``trade`` in *text*.

    ``action`` is one of ``already-enabled``, ``appended-to-list``,
    ``converted-inline-list``, ``added-enabled-key``, ``added-plugins-block``.
    The input is never reordered and no unrelated line is touched.
    """
    lines = text.splitlines(keepends=True)
    bounds = _plugins_block_bounds(lines)

    # Case 1: no plugins: section at all -> append a fresh block.
    if bounds is None:
        prefix = text if text.endswith("\n") or not text else text + "\n"
        return prefix + "plugins:\n  enabled:\n    - trade\n", "added-plugins-block"

    start, end = bounds
    block = lines[start:end]

    # Inline form on the plugins: line itself, e.g. `plugins: {enabled: [a]}`.
    inline_same_line = _PLUGINS_KEY.match(lines[start]).group(1).strip()
    if inline_same_line:
        raise ConfigError(
            "config uses an inline mapping for 'plugins:'; refusing to edit "
            "automatically. Add 'trade' to plugins.enabled manually."
        )

    enabled_idx = None
    enabled_indent = "  "
    enabled_inline = ""
    for offset, line in enumerate(block[1:], start=1):
        m = _ENABLED_KEY.match(line)
        if m:
            enabled_idx = start + offset
            enabled_indent = m.group(1)
            enabled_inline = m.group(2).strip()
            break

    # Case 2: plugins: exists but has no enabled: key.
    if enabled_idx is None:
        insert_at = start + 1
        indent = "  "
        for line in block[1:]:
            if line.strip():
                indent = line[: len(line) - len(line.lstrip())]
                break
        new_lines = list(lines)
        new_lines.insert(insert_at, f"{indent}enabled:\n")
        new_lines.insert(insert_at + 1, f"{indent}  - {PLUGIN_NAME}\n")
        return "".join(new_lines), "added-enabled-key"

    # Case 3: inline/flow list, e.g. `enabled: [alpha, beta]`.
    if enabled_inline:
        if enabled_inline in ("[]", "~", "null"):
            items: List[str] = []
        elif enabled_inline.startswith("["):
            if not enabled_inline.endswith("]"):
                raise ConfigError(
                    "config has a multi-line flow sequence for plugins.enabled; "
                    "refusing to edit automatically"
                )
            body = enabled_inline[1:-1].strip()
            items = [p.strip().strip("'\"") for p in body.split(",") if p.strip()]
        else:
            items = [enabled_inline.strip().strip("'\"")]
        if PLUGIN_NAME in items:
            return text, "already-enabled"
        items.append(PLUGIN_NAME)
        rendered = ", ".join(items)
        new_lines = list(lines)
        new_lines[enabled_idx] = f"{enabled_indent}enabled: [{rendered}]\n"
        return "".join(new_lines), "converted-inline-list"

    # Case 4: block list. Collect its items and append if absent.
    item_indent = None
    last_item_idx = enabled_idx
    items = []
    for i in range(enabled_idx + 1, end):
        line = lines[i]
        if not line.strip():
            continue
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if not stripped.startswith("- "):
            break
        if item_indent is None:
            item_indent = indent
        elif len(indent) != len(item_indent):
            break
        items.append(stripped[2:].strip().strip("'\""))
        last_item_idx = i

    if PLUGIN_NAME in items:
        return text, "already-enabled"

    if item_indent is None:  # `enabled:` present but empty block
        item_indent = enabled_indent + "  "
        insert_at = enabled_idx + 1
    else:
        insert_at = last_item_idx + 1

    new_lines = list(lines)
    if new_lines and not new_lines[insert_at - 1].endswith("\n"):
        new_lines[insert_at - 1] += "\n"
    new_lines.insert(insert_at, f"{item_indent}- {PLUGIN_NAME}\n")
    return "".join(new_lines), "appended-to-list"


def plan_disable_trade(text: str) -> Tuple[str, str]:
    """Return ``(new_text, action)`` removing the ``trade`` entry from *text*.

    Removes only the ``trade`` item. Other plugins and every unrelated line are
    preserved. ``action`` is ``removed`` or ``not-present``.
    """
    lines = text.splitlines(keepends=True)
    bounds = _plugins_block_bounds(lines)
    if bounds is None:
        return text, "not-present"
    start, end = bounds

    # Block-list item.
    for i in range(start, end):
        stripped = lines[i].strip()
        if stripped in (f"- {PLUGIN_NAME}", f"- '{PLUGIN_NAME}'", f'- "{PLUGIN_NAME}"'):
            new_lines = lines[:i] + lines[i + 1:]
            return "".join(new_lines), "removed"

    # Inline list.
    for i in range(start, end):
        m = _ENABLED_KEY.match(lines[i])
        if not m:
            continue
        inline = m.group(2).strip()
        if not inline.startswith("[") or not inline.endswith("]"):
            continue
        items = [p.strip().strip("'\"") for p in inline[1:-1].split(",") if p.strip()]
        if PLUGIN_NAME not in items:
            return text, "not-present"
        items = [it for it in items if it != PLUGIN_NAME]
        new_lines = list(lines)
        new_lines[i] = f"{m.group(1)}enabled: [{', '.join(items)}]\n"
        return "".join(new_lines), "removed"

    return text, "not-present"


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def backup_config(path: Path, backup_dir: Path) -> Path:
    """Copy *path* into *backup_dir*, preserving its filename."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / path.name
    shutil.copy2(path, target)
    return target


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".kamtmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def enable_trade(
    config_path: Path, backup_dir: Optional[Path], dry_run: bool = False
) -> Dict[str, Any]:
    """Ensure ``trade`` is enabled. Returns a record for the manifest."""
    original = config_path.read_text(encoding="utf-8")
    before = parse_config(config_path)
    was_enabled = is_trade_enabled(before)
    other_before = [p for p in enabled_plugins(before) if p != PLUGIN_NAME]

    new_text, action = plan_enable_trade(original)

    if action != "already-enabled" and not dry_run:
        # Validate the edit parses and preserves everything before writing.
        if yaml is None:
            raise ConfigError("PyYAML unavailable; refusing to edit the config")
        try:
            after = yaml.safe_load(new_text) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"edit would produce malformed YAML: {exc}") from exc
        if not isinstance(after, dict):
            raise ConfigError("edit would not produce a top-level mapping")
        missing = set(before) - set(after)
        if missing:
            raise ConfigError(f"edit would drop top-level keys: {sorted(missing)}")
        after_enabled = enabled_plugins(after)
        if PLUGIN_NAME not in after_enabled:
            raise ConfigError("edit did not enable 'trade'")
        if after_enabled.count(PLUGIN_NAME) != 1:
            raise ConfigError("edit produced a duplicate 'trade' entry")
        lost = [p for p in other_before if p not in after_enabled]
        if lost:
            raise ConfigError(f"edit would drop enabled plugins: {lost}")

        if backup_dir is not None:
            backup_config(config_path, backup_dir)
        _write_atomic(config_path, new_text)

    return {
        "path": str(config_path),
        "action": "would-" + action if (dry_run and action != "already-enabled") else action,
        "trade_was_already_enabled": was_enabled,
        "other_plugins_preserved": other_before,
    }


def disable_trade(
    config_path: Path,
    was_already_enabled: bool,
    backup_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Remove the ``trade`` entry, unless the user owned it before install."""
    if was_already_enabled:
        return {
            "path": str(config_path),
            "action": "preserved-user-owned",
            "detail": "trade was enabled before KAM was installed; left untouched",
        }

    original = config_path.read_text(encoding="utf-8")
    before = parse_config(config_path)
    other_before = [p for p in enabled_plugins(before) if p != PLUGIN_NAME]
    new_text, action = plan_disable_trade(original)

    if action == "removed" and not dry_run:
        if yaml is not None:
            try:
                after = yaml.safe_load(new_text) or {}
            except yaml.YAMLError as exc:
                raise ConfigError(f"edit would produce malformed YAML: {exc}") from exc
            if is_trade_enabled(after):
                raise ConfigError("edit did not remove 'trade'")
            lost = [p for p in other_before if p not in enabled_plugins(after)]
            if lost:
                raise ConfigError(f"edit would drop enabled plugins: {lost}")
        if backup_dir is not None:
            backup_config(config_path, backup_dir)
        _write_atomic(config_path, new_text)

    return {
        "path": str(config_path),
        "action": "would-" + action if (dry_run and action == "removed") else action,
        "other_plugins_preserved": other_before,
    }


DEFAULT_TELEGRAM_MENU_MAX = 60
# Hermes core slash set has grown past 60. A fixed +1 slot for /trade is no
# longer enough — plugin commands land at the end of telegram_bot_commands and
# get trimmed from setMyCommands. Use Telegram's Bot API ceiling so capacity
# is not the failure mode; pair with priority so /trade stays visible.
MINIMUM_TELEGRAM_MENU_MAX = 100
TELEGRAM_BOT_API_MAX_COMMANDS = 100
KAM_TELEGRAM_MENU_PRIORITY = ("trade", "fibo")


def get_telegram_menu_max_commands(config: Dict[str, Any]) -> int:
    """Return platforms.telegram.extra.command_menu.max_commands (default 60)."""
    platforms = config.get("platforms")
    if not isinstance(platforms, dict):
        return DEFAULT_TELEGRAM_MENU_MAX
    telegram = platforms.get("telegram")
    if not isinstance(telegram, dict):
        return DEFAULT_TELEGRAM_MENU_MAX
    extra = telegram.get("extra")
    if not isinstance(extra, dict):
        return DEFAULT_TELEGRAM_MENU_MAX
    menu = extra.get("command_menu")
    if not isinstance(menu, dict):
        return DEFAULT_TELEGRAM_MENU_MAX
    raw = menu.get("max_commands", DEFAULT_TELEGRAM_MENU_MAX)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TELEGRAM_MENU_MAX


def _telegram_menu_section(config: Dict[str, Any]) -> Dict[str, Any]:
    platforms = config.get("platforms")
    if not isinstance(platforms, dict):
        return {}
    telegram = platforms.get("telegram")
    if not isinstance(telegram, dict):
        return {}
    extra = telegram.get("extra")
    if not isinstance(extra, dict):
        return {}
    menu = extra.get("command_menu")
    return dict(menu) if isinstance(menu, dict) else {}


def _menu_needs_kam_priority(menu: Dict[str, Any]) -> bool:
    """True when trade/fibo are not guaranteed near the front of the menu."""
    mode = str(menu.get("priority_mode") or "prepend").strip().lower()
    raw = menu.get("priority")
    names: list[str] = []
    if isinstance(raw, list):
        names = [str(item).strip().lower() for item in raw if str(item).strip()]
    # prepend + trade first is the durable shape for the Telegram `/` picker.
    if mode not in {"prepend", "replace"}:
        return True
    if "trade" not in names:
        return True
    return False


def ensure_telegram_menu_capacity(
    config_path: Path,
    backup_dir: Optional[Path] = None,
    *,
    dry_run: bool = False,
    minimum: int = MINIMUM_TELEGRAM_MENU_MAX,
) -> Dict[str, Any]:
    """Ensure Telegram BotCommand menu publishes /trade (and /fibo).

    Hermes defaults to 60 slots. Core commands alone can exceed that, so
    plugin commands at the end of the menu are trimmed from ``setMyCommands``
    even though typing ``/trade`` still dispatches. This helper:

    1. Raises ``max_commands`` to at least *minimum* (default 100).
    2. Prepends ``trade`` / ``fibo`` on ``command_menu.priority`` so they
       survive the cap and appear at the top of the Telegram `/` picker.
    """
    if yaml is None:
        raise ConfigError("PyYAML unavailable; cannot adjust Telegram menu capacity")
    before = parse_config(config_path)
    current = get_telegram_menu_max_commands(before)
    menu_before = _telegram_menu_section(before)
    need_capacity = current < minimum
    need_priority = _menu_needs_kam_priority(menu_before)
    if not need_capacity and not need_priority:
        return {
            "path": str(config_path),
            "action": "already-sufficient",
            "max_commands": current,
            "minimum_max_commands": minimum,
            "priority": list(menu_before.get("priority") or []),
            "priority_mode": menu_before.get("priority_mode") or "prepend",
        }
    if dry_run:
        return {
            "path": str(config_path),
            "action": "would-set",
            "max_commands_before": current,
            "max_commands_after": max(current, int(minimum)),
            "minimum_max_commands": minimum,
            "would_set_priority": need_priority,
            "priority_commands": list(KAM_TELEGRAM_MENU_PRIORITY),
        }

    after = dict(before)
    platforms = dict(after.get("platforms") or {}) if isinstance(after.get("platforms"), dict) else {}
    telegram = dict(platforms.get("telegram") or {}) if isinstance(platforms.get("telegram"), dict) else {}
    extra = dict(telegram.get("extra") or {}) if isinstance(telegram.get("extra"), dict) else {}
    menu = dict(extra.get("command_menu") or {}) if isinstance(extra.get("command_menu"), dict) else {}
    if need_capacity:
        menu["max_commands"] = max(int(current), int(minimum), TELEGRAM_BOT_API_MAX_COMMANDS)
        # Clamp to Telegram Bot API hard limit.
        menu["max_commands"] = min(int(menu["max_commands"]), TELEGRAM_BOT_API_MAX_COMMANDS)
    if need_priority:
        existing: list[str] = []
        raw_priority = menu.get("priority")
        if isinstance(raw_priority, list):
            existing = [str(item).strip() for item in raw_priority if str(item).strip()]
        # Keep operator extras, but force KAM commands to the front.
        merged: list[str] = []
        seen: set[str] = set()
        for name in list(KAM_TELEGRAM_MENU_PRIORITY) + existing:
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(name)
        menu["priority"] = merged
        # Only set mode when missing/unknown — don't clobber an explicit replace.
        mode = str(menu.get("priority_mode") or "").strip().lower()
        if mode not in {"prepend", "append", "replace"}:
            menu["priority_mode"] = "prepend"
        elif mode == "append":
            # append puts custom names after defaults — trade would still lose
            # the race under a tight cap. Force prepend for KAM visibility.
            menu["priority_mode"] = "prepend"
    extra["command_menu"] = menu
    telegram["extra"] = extra
    platforms["telegram"] = telegram
    after["platforms"] = platforms

    missing = set(before) - set(after)
    if missing:
        raise ConfigError(f"menu capacity edit would drop top-level keys: {sorted(missing)}")
    if get_telegram_menu_max_commands(after) < minimum and need_capacity:
        raise ConfigError("menu capacity edit did not raise max_commands")

    new_text = yaml.safe_dump(after, default_flow_style=False, sort_keys=False, allow_unicode=True)
    if backup_dir is not None:
        backup_config(config_path, backup_dir)
    _write_atomic(config_path, new_text)
    return {
        "path": str(config_path),
        "action": "set",
        "max_commands_before": current,
        "max_commands_after": get_telegram_menu_max_commands(after),
        "minimum_max_commands": minimum,
        "priority": list((_telegram_menu_section(after).get("priority") or [])),
        "priority_mode": _telegram_menu_section(after).get("priority_mode"),
        "updated_capacity": need_capacity,
        "updated_priority": need_priority,
    }
