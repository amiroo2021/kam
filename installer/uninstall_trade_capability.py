"""Capability-specific uninstaller: TRADE.

Removes trade-only payloads and the webtrade systemd unit.

Does NOT delete operator .env keys.
Shared agents, TradeDesk, and canonical models are owned by uninstall_shared.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import capability_dir  # noqa: E402
from retired_payload import prune_retired_payload  # noqa: E402
from retired_units import retire_retired_systemd_units  # noqa: E402
from trade_web_unit import uninstall_trade_web_unit  # noqa: E402
from webbacktest_unit import uninstall_webbacktest_unit  # noqa: E402

# Trade-only files/dirs relative to plugins/trade/. Shared core is left alone.
# Includes backtest_wizard.py (origin/main trade capability) plus web helpers.
TRADE_ONLY_REL_PATHS: List[Path] = [
    Path("wizard.py"),
    Path("backtest_wizard.py"),
    Path("candles.py"),
    Path("instrument_picker.py"),
    Path("ladder_math.py"),
    Path("fibolearn_wizard.py"),
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

    # Remove webtrade package directory (web UI)
    webtrade_dir = plugin_root / "webtrade"
    if webtrade_dir.is_dir():
        record["removed_dirs"].append(str(webtrade_dir))
        if not dry_run:
            shutil.rmtree(webtrade_dir, ignore_errors=True)

    # tests under plugins/trade/tests are trade-capability owned
    tests_dir = plugin_root / "tests"
    if tests_dir.is_dir():
        record["removed_dirs"].append(str(tests_dir))
        if not dry_run:
            shutil.rmtree(tests_dir, ignore_errors=True)


    # Prune payload owned by retired capabilities (their wizard and reader
    # package plus their agent modules).  These are not in
    # TRADE_ONLY_REL_PATHS because they are no longer shipped at all.  The
    # retired path list lives in retired_payload.py so this module stays
    # free of retired names.
    record["retired_payload"] = prune_retired_payload(plugin_root, dry_run=dry_run)

    own_dir = capability_dir(hermes_home, "trade")
    if own_dir.is_dir():
        record["removed_dirs"].append(str(own_dir))
        if not dry_run:
            shutil.rmtree(own_dir, ignore_errors=True)

    # Always retire webtrade + legacy webtrade units; never touch .env.
    # WebUI management is OFF by default — routine trade uninstall must
    # NOT disable/stop/daemon-reload webchat.service. Operators remove
    # the WebUI explicitly via installer/uninstall_webchat.py.
    sd = systemd_dir if systemd_dir is not None else Path("/etc/systemd/system")
    record["trade_web_unit"] = uninstall_trade_web_unit(
        systemd_dir=sd, dry_run=dry_run, manage_webui=False,
    )

    # Retire the systemd units that belonged to the removed capabilities.
    # A deployment that installed the retired capability keeps these units
    # armed (a per-minute timer whose ExecStart points at a module that no
    # longer exists), so they must be disabled, stopped and removed.
    record["retired_units"] = retire_retired_systemd_units(
        systemd_dir=sd, dry_run=dry_run,
    )

    return record


__all__ = ["run", "TRADE_ONLY_REL_PATHS"]
