"""Capability-aware KAM installer dispatcher.

This module is the entry point for `./install.sh`, `./verify.sh`, and
`./uninstall.sh`. It dispatches to per-capability installers based on the
CLI flags:

  --trade            install/verify/uninstall only the /trade capability
  --fibo             install/verify/uninstall only the /fibo capability
  --trade --fibo     install/verify/uninstall both capabilities

No flags = TRADE ONLY (locked backward-compatibility policy, Decision 1).

The dispatcher never mutates the live Hermes install directly. It
delegates to capability-specific installer modules which:
  - copy capability-specific plugin files
  - ensure the capability's owned folder (~/.hermes/<cap>/) exists
  - install/remove capability-specific services
  - update ~/.hermes/kam/install_state.json atomically

The shared core (shared agents, dependencies, patched seams) is
installed once, regardless of which capabilities are selected.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import (  # noqa: E402
    CAPABILITY_FLAGS,
    INSTALLER_VERSION,
    KAM_VERSION,
    KNOWN_CAPABILITIES,
    any_capability_installed,
    capability_dir,
    install_state_path,
    is_installed,
    is_no_flag,
    legacy_trade_state_dir,
    load_manifest,
    migrate_legacy_kam_trade,
    parse_capability_flags,
    resolve_hermes_home,
    save_manifest,
)


def _print_banner(action: str, capabilities: List[str]) -> None:
    label = ",".join(capabilities) if capabilities else "trade (legacy)"
    print(f"KAM installer v{INSTALLER_VERSION} (kam {KAM_VERSION}) -- action={action} capabilities=[{label}]")


def _resolve_capabilities(argv: List[str]) -> List[str]:
    """Apply Decision 1: no-flag = TRADE ONLY."""
    caps, _rest = parse_capability_flags(argv)
    if not caps:
        caps = ["trade"]
    # Stable order: trade before fibo (legacy stable ordering).
    ordered = [c for c in KNOWN_CAPABILITIES if c in caps]
    return ordered


def _print_known_capabilities() -> None:
    print(f"known capabilities: {', '.join(KNOWN_CAPABILITIES.keys())}")


def cmd_install(argv: List[str]) -> int:
    """Dispatch `./install.sh`."""
    caps = _resolve_capabilities(argv)
    _print_banner("install", caps)
    hermes_home = resolve_hermes_home()
    # Migrate legacy .kam-trade/ directory if present (once per install).
    legacy_payload = migrate_legacy_kam_trade(hermes_home)
    if legacy_payload is not None:
        print(f"migrated legacy {legacy_trade_state_dir(hermes_home)} → retired")
    # Lazy imports (avoid pulling capability modules unless needed).
    results: List[dict] = []
    # Shared is installed once (when ANY capability needs it).
    from installer_shared import install_shared as _install_shared
    from install_trade_capability import run as run_trade
    from install_fibo_capability import run as run_fibo
    _print_known_capabilities()
    print(f"hermes_home: {hermes_home}")
    # Shared core (idempotent).
    shared_record = _install_shared(argv=argv, hermes_home=hermes_home, capabilities=caps)
    # Per-capability install.
    for cap in caps:
        if cap == "trade":
            res = run_trade(argv=argv, hermes_home=hermes_home, shared=shared_record)
        elif cap == "fibo":
            res = run_fibo(argv=argv, hermes_home=hermes_home, shared=shared_record)
        else:
            raise SystemExit(f"unknown capability: {cap}")
        results.append(res)
    # Update combined manifest atomically.
    manifest = load_manifest(hermes_home)
    for cap, res in zip(caps, results):
        manifest["capabilities"][cap] = True
        manifest["by_capability"][cap] = res
    manifest["shared"] = shared_record
    save_manifest(hermes_home, manifest)
    print(f"manifest: {install_state_path(hermes_home)}")
    print(f"OK -- installed: {', '.join(caps)}")
    return 0


def cmd_verify(argv: List[str]) -> int:
    """Dispatch `./verify.sh`."""
    caps = _resolve_capabilities(argv)
    _print_banner("verify", caps)
    hermes_home = resolve_hermes_home()
    from verify_shared import run as run_verify_shared
    from verify_trade_capability import run as verify_trade
    from verify_fibo_capability import run as verify_fibo
    failed = []
    # Verify shared.
    shared_ok = run_verify_shared(argv=argv, hermes_home=hermes_home, capabilities=caps)
    if not shared_ok:
        failed.append("shared")
    for cap in caps:
        if cap == "trade":
            ok = verify_trade(argv=argv, hermes_home=hermes_home)
        elif cap == "fibo":
            ok = verify_fibo(argv=argv, hermes_home=hermes_home)
        else:
            raise SystemExit(f"unknown capability: {cap}")
        if not ok:
            failed.append(cap)
    if failed:
        print(f"VERIFY FAILED: {', '.join(failed)}")
        return 1
    print(f"OK -- verified: {', '.join(caps)}")
    return 0


def cmd_uninstall(argv: List[str]) -> int:
    """Dispatch `./uninstall.sh`."""
    caps = _resolve_capabilities(argv)
    _print_banner("uninstall", caps)
    hermes_home = resolve_hermes_home()
    from uninstall_trade_capability import run as uninstall_trade
    from uninstall_fibo_capability import run as uninstall_fibo
    from uninstall_shared import run as uninstall_shared
    results: List[dict] = []
    # Uninstall capabilities in REVERSE order (fibo first if present).
    for cap in reversed(caps):
        if cap == "trade":
            res = uninstall_trade(argv=argv, hermes_home=hermes_home)
        elif cap == "fibo":
            res = uninstall_fibo(argv=argv, hermes_home=hermes_home)
        else:
            raise SystemExit(f"unknown capability: {cap}")
        results.append(res)
    # Update manifest: clear uninstalled capabilities.
    manifest = load_manifest(hermes_home)
    for cap in caps:
        manifest["capabilities"][cap] = False
        manifest["by_capability"][cap] = {}
    # Save the cleared manifest BEFORE checking, so the disk reflects the
    # post-clear state when any_capability_installed re-reads it.
    save_manifest(hermes_home, manifest)
    # If no capabilities remain installed, remove shared too. The shared
    # uninstall removes the kam/ directory entirely, so do NOT re-save the
    # manifest after that (which would recreate the kam dir).
    if not any_capability_installed(hermes_home):
        uninstall_shared(argv=argv, hermes_home=hermes_home)
        # Don't save manifest — kam/ is gone.
    print(f"manifest: {install_state_path(hermes_home)}")
    print(f"OK -- uninstalled: {', '.join(caps)}")
    return 0


def main(argv: List[str]) -> int:
    """Dispatch based on the script invocation (install/verify/uninstall)."""
    # The action is encoded by which script invoked us. We detect it from
    # the script path or argv[0].
    action = "install"
    script = Path(sys.argv[0]).name if sys.argv else ""
    if script.endswith("verify.sh") or "verify" in script:
        action = "verify"
    elif script.endswith("uninstall.sh") or "uninstall" in script:
        action = "uninstall"
    # Allow --action override for testing.
    for i, a in enumerate(argv):
        if a == "--action" and i + 1 < len(argv):
            action = argv[i + 1]
    if action == "install":
        return cmd_install(argv)
    elif action == "verify":
        return cmd_verify(argv)
    elif action == "uninstall":
        return cmd_uninstall(argv)
    else:
        raise SystemExit(f"unknown action: {action}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))