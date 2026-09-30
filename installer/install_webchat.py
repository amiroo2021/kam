#!/usr/bin/env python3
"""Standalone installer for the Hermes WebUI systemd unit (webchat.service).

This is the ONLY entry point that is allowed to manage webchat.service.
Routine KAM capability install/update (install_trade.py,
install_trade_capability.py) MUST NOT touch webchat; they pass
``manage_webui=False`` and the WebUI unit is left untouched.

Use this script when you actually want to install or refresh the
WebUI on a node that does not yet have one, or to recover from a
drifted unit. The trade plugin is NOT touched here — this script
does not copy any plugins/trade files, does not patch the Telegram
adapter, does not restart hermes-gateway.

Examples:

  # Dry-run: show what would happen
  python3 installer/install_webchat.py --dry-run

  # Real install to /etc/systemd/system
  python3 installer/install_webchat.py

  # Install to a non-production directory (CI / tests)
  python3 installer/install_webchat.py --systemd-dir /tmp/test-systemd
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SYSTEMD_DIR = Path("/etc/systemd/system")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kamlib as K  # noqa: E402
from trade_web_unit import (  # noqa: E402
    DEFAULT_WEBUI_ROOT,
    UNIT_NAME,
    install_trade_web_unit,
    password_status,
)


def main(argv) -> int:
    parser = argparse.ArgumentParser(
        description="Install the Hermes WebUI systemd unit (webchat.service)."
    )
    parser.add_argument("--hermes-root", default=None)
    parser.add_argument("--hermes-home", default=None)
    parser.add_argument(
        "--systemd-dir",
        default=str(DEFAULT_SYSTEMD_DIR),
        help="systemd unit directory (default /etc/systemd/system). Tests "
             "should pass a temp dir, NEVER the production systemd dir, "
             "unless they are actually installing on this node.",
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Write the unit and enable it, but do not start it.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    print(f"KAM /webchat installer v{K.INSTALLER_VERSION} (kam {K.KAM_VERSION})")
    if args.dry_run:
        print("DRY RUN - no changes will be made")
    print()

    hermes_root = K.resolve_hermes_root(args.hermes_root)
    hermes_home = K.resolve_hermes_home() if args.hermes_home is None else Path(args.hermes_home).expanduser()

    print(f"    hermes_root : {hermes_root}")
    print(f"    hermes_home : {hermes_home}")
    print(f"    systemd_dir : {args.systemd_dir}")
    print()

    pw_ok, pw_len = password_status(hermes_home)
    if pw_ok:
        print(f"    WEB_PASSWORD present (len={pw_len})")
    else:
        print(
            "    WARNING: WEB_PASSWORD missing — set it in "
            f"{hermes_home}/.env then re-run this script (operator-supplied; "
            "this installer will not invent a password)."
        )
    print()

    record = install_trade_web_unit(
        hermes_root=hermes_root,
        hermes_home=hermes_home,
        systemd_dir=Path(args.systemd_dir),
        dry_run=args.dry_run,
        start=not args.no_start,
        manage_webui=True,
    )

    for action in record.get("actions") or []:
        print(f"    {action}")

    print()
    print(json.dumps({
        "unit": UNIT_NAME,
        "ok": record.get("ok"),
        "dry_run": record.get("dry_run"),
        "manage_webui": record.get("manage_webui"),
        "actions": record.get("actions"),
    }, indent=2))
    print()
    if record.get("ok"):
        print("KAM /webchat install: PASS")
        return 0
    print(f"KAM /webchat install: FAIL — {record.get('error')}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))