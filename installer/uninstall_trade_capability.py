"""Capability-specific uninstaller: TRADE.

Removes the /trade capability:
  - removes the trade-specific plugin files from <hermes_root>/plugins/trade/
  - removes ~/.hermes/trade/ (the owned state folder)

NEVER touches:
  - fibo files
  - ~/.hermes/fibo/
  - fibo.service
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import capability_dir


REPO_ROOT = Path(__file__).resolve().parent.parent
TRADE_REL_PATHS = [
    Path("plugins") / "trade" / "wizard.py",
]


def run(
    *,
    argv: Sequence[str],
    hermes_root: Path,
    hermes_home: Path,
    dry_run: bool = False,
) -> Dict[str, Any]:
    plugin_root = hermes_root / "plugins" / "trade"
    record: Dict[str, Any] = {
        "removed_files": [],
        "removed_dirs": [],
        "dry_run": dry_run,
    }
    for rel in TRADE_REL_PATHS:
        try:
            rel_under_plugin_trade = rel.relative_to(Path("plugins") / "trade")
        except ValueError:
            rel_under_plugin_trade = rel
        dst = plugin_root / rel_under_plugin_trade
        if dst.is_file():
            record["removed_files"].append(str(rel))
            if not dry_run:
                dst.unlink()
    # Note: we do NOT remove plugins/trade/__init__.py because it is the
    # plugin marker that ALSO carries the fibo slash-command registration.
    # If fibo is still installed, /fibo must still route. Removing
    # __init__.py would break fibo registration.
    # Owned folder.
    own_dir = capability_dir(hermes_home, "trade")
    if own_dir.is_dir():
        record["removed_dirs"].append(str(own_dir))
        if not dry_run:
            shutil.rmtree(own_dir)
    return record


__all__ = ["run", "TRADE_REL_PATHS"]