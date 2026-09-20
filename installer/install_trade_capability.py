"""Capability-specific installer: TRADE.

Installs the full ``plugins/trade/`` payload into ``$HERMES_ROOT/plugins/trade``
(including wizard, backtest_wizard, agents already handled by shared when needed,
webtrade web UI, candles helpers, etc.) and stages the webtrade systemd unit.

Takes EXPLICIT ``hermes_root`` (the installed app tree) and ``hermes_home``
(the persistent state). The two are independent.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import (  # noqa: E402
    SCHEMA_VERSION,
    capability_dir,
)
import kamlib as K  # noqa: E402
from trade_web_unit import install_trade_web_unit, password_status  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
# Full tree under plugins/trade (not just wizard.py). Web UI + helpers included.
# This also covers backtest_wizard.py and any future trade-owned modules.
PAYLOAD_ROOT = REPO_ROOT / "plugins" / "trade"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _copy_full_trade_tree(
    *,
    hermes_root: Path,
    dry_run: bool,
) -> List[Dict[str, Any]]:
    """Copy every installable file under plugins/trade into hermes-root."""
    plugin_root = hermes_root / "plugins" / "trade"
    files: List[Dict[str, Any]] = []
    if not PAYLOAD_ROOT.is_dir():
        return [{"path": str(PAYLOAD_ROOT), "action": "missing-source"}]
    for rel in K.iter_payload_files(PAYLOAD_ROOT):
        src = PAYLOAD_ROOT / rel
        dst = plugin_root / rel
        entry: Dict[str, Any] = {"path": str(Path("plugins") / "trade" / rel)}
        if not src.is_file():
            entry["action"] = "missing-source"
            files.append(entry)
            continue
        if dst.is_file() and _sha256_file(src) == _sha256_file(dst):
            entry["action"] = "unchanged"
        else:
            entry["src_sha256"] = _sha256_file(src)
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            entry["action"] = "copied" if not dry_run else "would-copy"
        files.append(entry)
    return files


def run(
    *,
    argv: Sequence[str],
    hermes_root: Path,
    hermes_home: Path,
    shared: Dict[str, Any],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Install the /trade capability + webtrade unit. Idempotent."""
    plugin_root = hermes_root / "plugins" / "trade"
    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "files": [],
        "ok": True,
        "dry_run": dry_run,
        "target_plugin_root": str(plugin_root),
    }

    file_entries = _copy_full_trade_tree(hermes_root=hermes_root, dry_run=dry_run)
    record["files"] = file_entries
    if any(e.get("action") == "missing-source" for e in file_entries):
        record["ok"] = False

    # Ensure ~/.hermes/trade/ exists (owned state folder).
    own_dir = capability_dir(hermes_home, "trade")
    if dry_run:
        if not own_dir.is_dir():
            record.setdefault("actions", []).append(f"would-mkdir {own_dir}")
        else:
            record.setdefault("actions", []).append(f"keep-exists {own_dir}")
    else:
        own_dir.mkdir(parents=True, exist_ok=True)
    record["owned_dir"] = str(own_dir)

    # webtrade systemd unit
    systemd_dir_str = str(shared.get("systemd_dir", "") or "")
    pw_ok, pw_len = password_status(hermes_home)
    record["trade_web_password_present"] = pw_ok
    record["trade_web_password_length"] = pw_len
    if not pw_ok:
        print(
            "WARNING: WEB_PASSWORD is missing from env / "
            f"{hermes_home}/.env — webtrade will fail closed until set "
            "(operator must supply it; installer will not invent a password).",
            flush=True,
        )

    if systemd_dir_str:
        unit_record = install_trade_web_unit(
            hermes_root=hermes_root,
            hermes_home=hermes_home,
            systemd_dir=Path(systemd_dir_str),
            dry_run=dry_run,
            start=True,
        )
        record["trade_web_unit"] = unit_record
        if not unit_record.get("ok", False):
            # Unit write failure is fatal; missing password still ok=True with warning.
            if unit_record.get("error"):
                record["ok"] = False
    else:
        record["trade_web_unit"] = {"action": "skipped", "reason": "empty systemd_dir"}

    return record


__all__ = ["run", "PAYLOAD_ROOT"]
