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
