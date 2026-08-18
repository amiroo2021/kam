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

import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import capability_dir


REPO_ROOT = Path(__file__).resolve().parent.parent
TRADE_REL_PATHS = [
    Path("plugins") / "trade" / "tradedesk.py",
    Path("plugins") / "trade" / "wizard.py",
]


def run(*, argv, hermes_home: Path) -> Dict[str, Any]:
    hermes_root = hermes_home.parent
    plugin_root = hermes_root / "plugins" / "trade"
    record: Dict[str, Any] = {"removed_files": [], "removed_dirs": []}
    for rel in TRADE_REL_PATHS:
        dst = plugin_root / rel.relative_to(Path("plugins") / "trade")
        if dst.is_file():
            dst.unlink()
            record["removed_files"].append(str(rel))
    # Note: we do NOT remove plugins/trade/__init__.py because it is the
    # plugin marker that ALSO carries the fibo slash-command registration.
    # If fibo is still installed, /fibo must still route. Removing
    # __init__.py would break fibo registration.
    # The __init__.py is updated by the SHARED/registry layer in a
    # future change (out of scope for the initial cut).
    # Owned folder.
    own_dir = capability_dir(hermes_home, "trade")
    if own_dir.is_dir():
        shutil.rmtree(own_dir)
        record["removed_dirs"].append(str(own_dir))
    return record


__all__ = ["run", "TRADE_REL_PATHS"]