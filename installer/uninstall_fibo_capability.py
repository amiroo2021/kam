"""Capability-specific uninstaller: FIBO.

Removes the /fibo capability payload:

* ``plugins/trade/fibo_wizard.py`` (the wizard itself)
* ``plugins/trade/fibo/`` (the Phase 1 sub-package: Start Fibo
  sub-flow, MT4 Reader, snapshot/store/session modules, atomic
  write helper, package marker)

It does NOT:

* remove ``plugins/trade/__init__.py`` (still the /trade marker),
* remove any ``plugins/trade/agents/x_*_agent.py`` file (shared with
  /trade),
* remove any other /trade-owned file.

In Phase 1 there is no ``~/.hermes/fibo/`` runtime directory
created by the capability — the snapshot/state/registrations files
under ``~/.hermes/fibo/`` are produced by the Reader process (which
the user launches manually) and by the wizard as user data. The
uninstall does NOT delete those files — they are user data.
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
    Path("plugins") / "trade" / "fibo" / "__init__.py",
    Path("plugins") / "trade" / "fibo" / "_atomic.py",
    Path("plugins") / "trade" / "fibo" / "snapshot.py",
    Path("plugins") / "trade" / "fibo" / "store.py",
    Path("plugins") / "trade" / "fibo" / "session.py",
    Path("plugins") / "trade" / "fibo" / "flow.py",
    Path("plugins") / "trade" / "fibo" / "mt4_reader.py",
    # Phase 2.x additions (must mirror install_fibo_capability).
    Path("plugins") / "trade" / "fibo" / "alias_memory.py",
    Path("plugins") / "trade" / "fibo" / "candidates.py",
    Path("plugins") / "trade" / "fibo" / "discovery.py",
    Path("plugins") / "trade" / "fibo" / "dryrun.py",
    Path("plugins") / "trade" / "fibo" / "reconciler.py",
    Path("plugins") / "trade" / "agents" / "tests" / "__init__.py",
    Path("plugins") / "trade" / "agents" / "tests"
    / "test_x_ondoperps_market_price.py",
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