"""Retired payload paths that must be pruned from a deployed KAM tree.

A deployment installed by an older KAM version still carries the files that
this repository no longer ships.  The install path only copies files, it
never deletes, so those orphans survive an upgrade:

* ``plugins/trade/agents/x_metatrader_agent.py`` and
  ``plugins/trade/agents/x_mt5linux_agent.py`` are discovered at runtime by
  ``TradeDesk._iter_agent_files()``, so a stale copy keeps exposing the
  retired venue in the /trade menu even though the repository removed it.
* ``plugins/trade/fibo_wizard.py`` and the ``plugins/trade/fibo/`` package
  are the removed /fibo capability's wizard and MT4 reader.

These paths are relative to ``plugins/trade/``.  ``None`` marks a retired
directory (removed recursively).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

# Retired files, relative to plugins/trade/.
RETIRED_PAYLOAD_FILES: List[Path] = [
    Path("agents/x_metatrader_agent.py"),
    Path("agents/x_mt5linux_agent.py"),
    Path("fibo_wizard.py"),
]

# Retired directories, relative to plugins/trade/.  Removed recursively.
RETIRED_PAYLOAD_DIRS: List[Path] = [
    Path("fibo"),
]


def retired_payload_paths() -> List[Path]:
    """Return every retired path (files and directories), relative to trade."""
    return list(RETIRED_PAYLOAD_FILES) + list(RETIRED_PAYLOAD_DIRS)


def prune_retired_payload(
    plugin_root: Path,
    *,
    dry_run: bool = False,
) -> List[Dict[str, str]]:
    """Delete retired payload paths under *plugin_root*.

    Idempotent: a path that is already absent is reported as ``absent``.
    Never touches anything outside *plugin_root*.
    """
    removed: List[Dict[str, str]] = []
    for rel in RETIRED_PAYLOAD_FILES:
        target = plugin_root / rel
        if not target.exists():
            removed.append({"path": str(rel), "action": "absent"})
            continue
        if not dry_run:
            target.unlink()
        removed.append({"path": str(rel), "action": "would-remove" if dry_run else "removed"})
    for rel in RETIRED_PAYLOAD_DIRS:
        target = plugin_root / rel
        if not target.is_dir():
            removed.append({"path": str(rel), "action": "absent"})
            continue
        if not dry_run:
            import shutil

            shutil.rmtree(target)
        removed.append({"path": str(rel), "action": "would-remove" if dry_run else "removed"})
    return removed


def retired_agent_names() -> List[str]:
    """Return the retired exchange names whose agents must never be discovered."""
    names: List[str] = []
    for rel in RETIRED_PAYLOAD_FILES:
        stem = rel.name
        if stem.startswith("x_") and stem.endswith("_agent.py"):
            names.append(stem[2:-len("_agent.py")])
    return names


__all__ = [
    "RETIRED_PAYLOAD_DIRS",
    "RETIRED_PAYLOAD_FILES",
    "prune_retired_payload",
    "retired_agent_names",
    "retired_payload_paths",
]
