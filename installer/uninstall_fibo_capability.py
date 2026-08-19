"""Capability-specific uninstaller: FIBO.

Removes the /fibo capability:
  - removes fibo-specific plugin files from <hermes_root>/plugins/trade/
  - removes ~/.hermes/fibo/ (the owned state folder)

NEVER touches:
  - trade files
  - ~/.hermes/trade/
  - shared agents
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

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
    Path("plugins") / "trade" / "golden_fibo" / "client_id_v2.py",
    Path("plugins") / "trade" / "golden_fibo" / "arcus_adapter.py",
]


def run(
    *,
    argv: Sequence[str],
    hermes_root: Path,
    hermes_home: Path,
    dry_run: bool = False,
    systemd_dir: Optional[Path] = None,
    no_restart: bool = False,
) -> Dict[str, Any]:
    from fibo_unit import DEFAULT_SYSTEMD_DIR, remove_fibo_service_unit

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
    # Remove the now-empty golden_fibo/ directory.
    golden_fibo_dir = plugin_root / "golden_fibo"
    if golden_fibo_dir.is_dir():
        # Check if empty
        try:
            is_empty = not any(golden_fibo_dir.iterdir())
        except OSError:
            is_empty = False
        if is_empty:
            record["removed_dirs"].append(str(golden_fibo_dir))
            if not dry_run:
                golden_fibo_dir.rmdir()
    # Owned folder.
    own_dir = capability_dir(hermes_home, "fibo")
    if own_dir.is_dir():
        record["removed_dirs"].append(str(own_dir))
        if not dry_run:
            shutil.rmtree(own_dir)
    # systemd unit
    sd = Path(systemd_dir) if systemd_dir is not None else DEFAULT_SYSTEMD_DIR
    record["service_unit"] = remove_fibo_service_unit(
        systemd_dir=sd, dry_run=dry_run, no_restart=no_restart
    )
    return record


__all__ = ["run", "FIBO_REL_PATHS"]