"""Capability-specific uninstaller: TRADE.

Removes trade-only payloads and the trade-web systemd unit.

Does NOT delete operator .env keys.
Does NOT remove shared agents / tradedesk / canonical while fibo may remain
(those are owned by uninstall_shared when no capabilities remain).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import capability_dir  # noqa: E402
from trade_web_unit import uninstall_trade_web_unit  # noqa: E402

# Trade-only files/dirs relative to plugins/trade/. Shared core is left alone.
# Includes backtest_wizard.py (origin/main trade capability) plus web helpers.
TRADE_ONLY_REL_PATHS: List[Path] = [
    Path("wizard.py"),
    Path("backtest_wizard.py"),
    Path("candles.py"),
    Path("instrument_picker.py"),
    Path("ladder_math.py"),
    Path("fibolearn_wizard.py"),
    Path("fibo_wizard.py"),
]


def run(
    *,
    argv: Sequence[str],
    hermes_root: Path,
    hermes_home: Path,
    dry_run: bool = False,
    systemd_dir: Path | None = None,
) -> Dict[str, Any]:
    plugin_root = hermes_root / "plugins" / "trade"
    record: Dict[str, Any] = {
        "removed_files": [],
        "removed_dirs": [],
        "dry_run": dry_run,
    }

    # Remove trade-only files
    for rel in TRADE_ONLY_REL_PATHS:
        dst = plugin_root / rel
        if dst.is_file():
            record["removed_files"].append(str(Path("plugins") / "trade" / rel))
            if not dry_run:
                dst.unlink()

    # Remove trademenu package directory (web UI)
    trademenu_dir = plugin_root / "trademenu"
    if trademenu_dir.is_dir():
        record["removed_dirs"].append(str(trademenu_dir))
        if not dry_run:
            shutil.rmtree(trademenu_dir, ignore_errors=True)

    # tests under plugins/trade/tests are trade-capability owned
    tests_dir = plugin_root / "tests"
    if tests_dir.is_dir():
        record["removed_dirs"].append(str(tests_dir))
        if not dry_run:
            shutil.rmtree(tests_dir, ignore_errors=True)

    # fibo/ package if present under trade (fibo capability has its own installer too)
    # leave plugins/trade/fibo to fibo uninstaller when present

    own_dir = capability_dir(hermes_home, "trade")
    if own_dir.is_dir():
        record["removed_dirs"].append(str(own_dir))
        if not dry_run:
            shutil.rmtree(own_dir, ignore_errors=True)

    # Always retire trade-web + legacy trademenu units; never touch .env.
    sd = systemd_dir if systemd_dir is not None else Path("/etc/systemd/system")
    record["trade_web_unit"] = uninstall_trade_web_unit(systemd_dir=sd, dry_run=dry_run)
    return record


__all__ = ["run", "TRADE_ONLY_REL_PATHS"]
