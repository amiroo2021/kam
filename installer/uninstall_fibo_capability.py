"""Capability-specific uninstaller: FIBO.

Removes the /fibo capability payload:

* ``plugins/trade/fibo_wizard.py`` (only this wizard file).

It does NOT:

* remove ``plugins/trade/__init__.py`` (still the /trade marker),
* remove any ``plugins/trade/agents/x_*_agent.py`` file (shared with
  /trade),
* remove any other /trade-owned file.

In this lightweight-skeleton phase there is no ``~/.hermes/fibo/``
runtime directory to delete — none has been created. Future iterations
that introduce Fibo runtime state may extend this module to remove
that directory when ``--purge-state`` is supplied.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))


# Files owned exclusively by the /fibo capability.
# Must mirror ``FIBO_REL_PATHS`` in install_fibo_capability.py.
FIBO_REL_PATHS = [
    Path("plugins") / "trade" / "fibo_wizard.py",
]


def run(
    *,
    argv: Sequence[str],  # noqa: ARG001
    hermes_root: Path,
    hermes_home: Path,  # noqa: ARG001
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Uninstall the /fibo capability payload.

    Returns a record listing the removed files. Does NOT remove
    ``~/.hermes/fibo/`` (none exists in this phase).
    """
    plugin_root = hermes_root / "plugins" / "trade"
    record: Dict[str, Any] = {
        "removed_files": [],
        "removed_dirs": [],
        "dry_run": dry_run,
    }
    for rel in FIBO_REL_PATHS:
        try:
            rel_under_plugin_trade = rel.relative_to(Path("plugins") / "trade")
        except ValueError:
            rel_under_plugin_trade = rel
        dst = plugin_root / rel_under_plugin_trade
        if dst.is_file():
            record["removed_files"].append(str(rel))
            if not dry_run:
                dst.unlink()
    return record


__all__ = ["run", "FIBO_REL_PATHS"]