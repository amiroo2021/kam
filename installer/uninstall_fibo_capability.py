"""Capability-specific uninstaller: FIBO.

Removes the /fibo capability:
  - removes fibo-specific plugin files from <hermes_root>/plugins/trade/
  - removes ~/.hermes/fibo/ (the owned state folder)
  - stops and removes fibo.service (if --systemd-dir flag is honoured by the
    service uninstall helper; out of scope for the initial cut -- the
    installer writes the unit, the operator is responsible for stopping it
    before uninstall, mirroring the proven monolithic installer behavior)

NEVER touches:
  - trade files
  - ~/.hermes/trade/
  - shared agents
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import capability_dir


REPO_ROOT = Path(__file__).resolve().parent.parent
FIBO_REL_PATHS = [
    Path("plugins") / "trade" / "fibo_service.py",
    Path("plugins") / "trade" / "fibo_daemon.py",
    Path("plugins") / "trade" / "fibo_wizard.py",
    Path("plugins") / "trade" / "golden_fibo" / "__init__.py",
    Path("plugins") / "trade" / "golden_fibo" / "config.py",
    Path("plugins") / "trade" / "golden_fibo" / "engine.py",
    Path("plugins") / "trade" / "golden_fibo" / "lighter_adapter.py",
    Path("plugins") / "trade" / "golden_fibo" / "preflight.py",
    Path("plugins") / "trade" / "golden_fibo" / "state.py",
]


def run(*, argv, hermes_home: Path) -> Dict[str, Any]:
    hermes_root = hermes_home.parent
    plugin_root = hermes_root / "plugins" / "trade"
    record: Dict[str, Any] = {"removed_files": [], "removed_dirs": []}
    for rel in FIBO_REL_PATHS:
        dst = plugin_root / rel.relative_to(Path("plugins") / "trade")
        if dst.is_file():
            dst.unlink()
            record["removed_files"].append(str(rel))
    # Remove the now-empty golden_fibo/ directory.
    golden_fibo_dir = plugin_root / "golden_fibo"
    if golden_fibo_dir.is_dir() and not any(golden_fibo_dir.iterdir()):
        golden_fibo_dir.rmdir()
        record["removed_dirs"].append(str(golden_fibo_dir))
    # Owned folder.
    own_dir = capability_dir(hermes_home, "fibo")
    if own_dir.is_dir():
        shutil.rmtree(own_dir)
        record["removed_dirs"].append(str(own_dir))
    return record


__all__ = ["run", "FIBO_REL_PATHS"]