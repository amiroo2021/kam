"""Capability-aware KAM installer dispatcher.

This module is the entry point for ./install.sh, ./verify.sh, and
./uninstall.sh. It dispatches to per-capability installers based on the
CLI flags:

  --trade            install/verify/uninstall only the /trade capability
  --fibo             install/verify/uninstall only the /fibo capability
  --trade --fibo     install/verify/uninstall both capabilities

No flags = TRADE ONLY (locked backward-compatibility policy, Decision 1).

Critical contract: the dispatcher and capability installers MUST take
explicit ``--hermes-root PATH`` and use ``HERMES_HOME`` for state. The two
are conceptually independent. The Hermes root is the installed
application tree (e.g. /usr/local/lib/hermes-agent); the Hermes home
is the persistent user state (e.g. ~/.hermes).

Argument handling:

  --help            print usage, exit 0, ZERO mutation.
  --dry-run         plan and print actions, ZERO mutation.
  --hermes-root     explicit Hermes root override.
  --hermes-home     explicit Hermes home override.
  <unknown>         print error, exit non-zero, ZERO mutation.

No-flag behavior: TRADE ONLY (Decision 1). Applies ONLY when genuinely
no capability flag was provided, not when parsing failed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import (  # noqa: E402
    CAPABILITY_FLAGS,
    INSTALLER_VERSION,
    KAM_VERSION,
    KNOWN_CAPABILITIES,
    SCHEMA_VERSION,
    any_capability_installed,
    capability_dir,
    capability_folder_consistent,
    install_state_path,
    is_installed,
    is_no_flag,
    legacy_trade_state_dir,
    load_manifest,
    migrate_legacy_kam_trade,
    parse_capability_flags,
    resolve_hermes_home,
    resolve_hermes_root,
    save_manifest,
    set_capability,
    clear_capability,
)


# Recognized non-capability flags. Anything outside this set is rejected.
RECOGNIZED_FLAGS = {
    "--trade", "--fibo",
    "--hermes-root", "--hermes-home",
    "--dry-run", "--help", "-h",
    "--systemd-dir",
    "--no-restart", "--skip-deps", "--purge-state",
    "--action",
}


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args(argv: List[str], action: str) -> argparse.Namespace:
    """Parse CLI args with strict unknown-option rejection.

    ``--help`` and ``-h`` are intercepted BEFORE argparse so we don't
    print an argparse-generated usage and we can exit cleanly with 0.

    ``--dry-run`` is a boolean flag we capture explicitly.

    Unknown options raise SystemExit(2) with a clear error and perform
    NO filesystem mutation. The capability flags are extracted BEFORE
    the standard parser so we can also handle ``--hermes-root VALUE``
    pairs and capability pairs without surprises.
    """
    # Pre-check: help flag short-circuit (exits with 0).
    if "--help" in argv or "-h" in argv:
        _print_usage(action)
        raise SystemExit(0)
    # Pre-check: any unknown flag → error + exit 2, zero mutation.
    # We treat flags-with-values pairs (``--hermes-root PATH``,
    # ``--hermes-home PATH``, ``--systemd-dir DIR``) as a single token.
    # Iterate in pairs so that ``--hermes-root /path`` doesn't flag
    # ``/path`` as an unknown option.
    flags_with_value = {"--hermes-root", "--hermes-home", "--systemd-dir", "--action"}
    skip_next = False
    for a in argv:
        if skip_next:
            skip_next = False
            continue
        if a in RECOGNIZED_FLAGS:
            if a in flags_with_value:
                skip_next = True
            continue
        if "=" in a and a.split("=", 1)[0] in RECOGNIZED_FLAGS:
            continue
        if a in ("--capture", "--verbose", "-v", "-q"):
            continue
        print(f"ERROR: unknown option: {a!r}", file=sys.stderr)
        print(f"Run '{Path(sys.argv[0]).name} --help' for usage.", file=sys.stderr)
        raise SystemExit(2)
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name if sys.argv else f"installer.{action}",
        add_help=False,
    )
    parser.add_argument("--trade", action="store_true")
    parser.add_argument("--fibo", action="store_true")
    parser.add_argument("--hermes-root", default=None)
    parser.add_argument("--hermes-home", default=None)
    parser.add_argument("--systemd-dir", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument("--skip-deps", action="store_true")
    parser.add_argument("--purge-state", action="store_true")
    parser.add_argument("--action", default=action)
    return parser.parse_args(argv)


def _print_usage(action: str) -> None:
    print(f"KAM installer v{INSTALLER_VERSION} (kam {KAM_VERSION})")
    print()
    print(f"Usage: {Path(sys.argv[0]).name if sys.argv else f'installer.{action}'} [options]")
    print()
    print("Capabilities (at least one required for non-help invocations):")
    print("  --trade              install/verify/uninstall the /trade capability")
    print("  --fibo               install/verify/uninstall the /fibo capability")
    print("  (no capability flag = TRADE ONLY, legacy compat)")
    print()
    print("Target paths:")
    print("  --hermes-root PATH   installed application tree (default /usr/local/lib/hermes-agent)")
    print("  --hermes-home PATH   persistent state directory (default ~/.hermes)")
    print("  --systemd-dir DIR    systemd unit directory (default /etc/systemd/system)")
    print()
    print("Behavior:")
    print("  --dry-run            plan only, zero mutation")
    print("  --no-restart         install but don't start/restart services")
    print("  --skip-deps          skip pip dependency install")
    print("  --purge-state        uninstall: also remove capability-owned state")
    print("  --help, -h           show this help and exit 0 (zero mutation)")
    print()
    print("Exit codes: 0=ok, 2=usage error, 1=action error.")


def _resolve_capabilities(args: argparse.Namespace) -> List[str]:
    """Apply Decision 1: no-flag = TRADE ONLY.

    Requires that args has been validated (unknown flags rejected).
    """
    caps: List[str] = []
    if args.trade:
        caps.append("trade")
    if args.fibo:
        caps.append("fibo")
    if not caps:
        caps = ["trade"]  # Decision 1: legacy no-flag = trade
    # Stable order: trade before fibo (legacy stable ordering).
    return [c for c in KNOWN_CAPABILITIES if c in caps]


def _print_banner(action: str, capabilities: List[str], hermes_root: Path, hermes_home: Path, dry_run: bool) -> None:
    label = ",".join(capabilities) if capabilities else "trade (legacy)"
    suffix = " (DRY RUN, no mutations)" if dry_run else ""
    print(f"KAM installer v{INSTALLER_VERSION} (kam {KAM_VERSION}) -- action={action} capabilities=[{label}]{suffix}")
    print(f"  hermes_root: {hermes_root}")
    print(f"  hermes_home: {hermes_home}")


# ---------------------------------------------------------------------------
# Action: install
# ---------------------------------------------------------------------------

def cmd_install(args: argparse.Namespace) -> int:
    """Dispatch ./install.sh."""
    caps = _resolve_capabilities(args)
    hermes_root = resolve_hermes_root(args.hermes_root)
    hermes_home = resolve_hermes_home() if args.hermes_home is None else Path(args.hermes_home).expanduser()
    dry_run = bool(args.dry_run)
    _print_banner("install", caps, hermes_root, hermes_home, dry_run)
    print(f"  known capabilities: {', '.join(KNOWN_CAPABILITIES.keys())}")

    # Lazy imports (avoid pulling capability modules unless needed).
    from installer_shared import install_shared as _install_shared
    from install_trade_capability import run as run_trade
    from install_fibo_capability import run as run_fibo

    # Migrate legacy .kam-trade/ directory if present (once per install).
    if not dry_run:
        legacy_payload = migrate_legacy_kam_trade(hermes_home)
        if legacy_payload is not None:
            print(f"migrated legacy {legacy_trade_state_dir(hermes_home)} → retired")
    else:
        legacy_dir = legacy_trade_state_dir(hermes_home)
        if legacy_dir.is_dir():
            print(f"DRY: would migrate legacy {legacy_dir} → retired")

    # Dependencies (SDK + requirements) — required so exchange agents that
    # import third-party SDKs (edgex, hyperliquid, pacifica, rise, …) can
    # load on a fresh Hermes venv. Without this, TradeDesk silently skips
    # agents that fail import and /trade shows a partial exchange list.
    deps_record: dict = {"action": "skipped", "reason": "skip-deps" if args.skip_deps else None}
    if not args.skip_deps:
        try:
            from install_trade import install_dependencies as _install_dependencies
            from adapter_wiring import _python_exe as _py_exe
        except Exception:
            # Fallback local import of python exe helper
            from install_trade import install_dependencies as _install_dependencies

            def _py_exe(root: Path) -> Path:
                v = root / "venv" / "bin" / "python"
                return v if v.is_file() else Path(sys.executable)

        try:
            python_exe = _py_exe(hermes_root)
            print(f"dependencies: python={python_exe}")
            deps_record = _install_dependencies(python_exe, dry_run)
            print(f"dependencies: {deps_record.get('action')}")
        except Exception as exc:
            print(f"ERROR: dependency install failed: {exc}", file=sys.stderr)
            if not dry_run:
                return 1
            deps_record = {"action": "error", "error": str(exc)}

    # Shared core (idempotent).
    shared_record = _install_shared(
        argv=[], hermes_root=hermes_root, hermes_home=hermes_home,
        capabilities=caps, dry_run=dry_run,
    )
    # Per-capability install.
    results: List[dict] = []
    systemd_dir = Path(args.systemd_dir) if args.systemd_dir else Path("/etc/systemd/system")
    for cap in caps:
        if cap == "trade":
            res = run_trade(
                argv=[], hermes_root=hermes_root, hermes_home=hermes_home,
                shared=shared_record, dry_run=dry_run,
            )
        elif cap == "fibo":
            res = run_fibo(
                argv=[], hermes_root=hermes_root, hermes_home=hermes_home,
                shared=shared_record, dry_run=dry_run,
                systemd_dir=systemd_dir,
                no_restart=bool(args.no_restart),
                backup_dir=hermes_home / "kam" / "backups",
            )
            if not res.get("ok", True):
                print(f"ERROR: fibo capability install failed: {res.get('service_error') or res}", file=sys.stderr)
                if not dry_run:
                    return 1
            else:
                unit = res.get("service_unit") or {}
                act = res.get("service_activation") or {}
                print(f"fibo.service: {unit.get('action')} -> {unit.get('path')}; activation={act.get('action')}")
        else:
            raise SystemExit(f"unknown capability: {cap}")
        results.append(res)

    # Telegram adapter dispatch seams + plugins.enabled (REQUIRED for
    # /trade and /fibo to work on a fresh Hermes install). Payload copy
    # alone is not enough — the adapter must route slash/callback/text.
    from adapter_wiring import apply_adapter_wiring as _apply_adapter_wiring

    wiring = _apply_adapter_wiring(
        hermes_root=hermes_root,
        hermes_home=hermes_home,
        capabilities=caps,
        dry_run=dry_run,
    )
    if not wiring.get("ok", False):
        print(f"ERROR: Telegram adapter wiring failed: {wiring.get('error') or wiring}", file=sys.stderr)
        if not dry_run:
            return 1
    else:
        patched = sum(1 for p in wiring.get("patches") or [] if p.get("action") in ("patched", "would-patch"))
        already = sum(1 for p in wiring.get("patches") or [] if p.get("action") in ("already-installed", "native-present"))
        print(f"adapter wiring: {patched} applied, {already} already present; config={wiring.get('config')}")

    if dry_run:
        # Zero mutation. Just print the plan.
        print()
        print("DRY RUN COMPLETE -- zero mutations performed.")
        return 0

    # Update combined manifest atomically.
    manifest = load_manifest(hermes_home)
    for cap, res in zip(caps, results):
        set_capability(manifest, cap, res)
    manifest["shared"] = shared_record
    manifest["adapter_wiring"] = {
        "capabilities": caps,
        "patches": wiring.get("patches") or [],
        "config": wiring.get("config"),
        "command_menu": wiring.get("command_menu"),
    }
    manifest["dependencies"] = deps_record
    save_manifest(hermes_home, manifest)
    print(f"manifest: {install_state_path(hermes_home)}")
    print(f"OK -- installed: {', '.join(caps)}")
    return 0


# ---------------------------------------------------------------------------
# Action: verify
# ---------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace) -> int:
    """Dispatch ./verify.sh."""
    caps = _resolve_capabilities(args)
    hermes_root = resolve_hermes_root(args.hermes_root)
    hermes_home = resolve_hermes_home() if args.hermes_home is None else Path(args.hermes_home).expanduser()
    dry_run = bool(args.dry_run)
    _print_banner("verify", caps, hermes_root, hermes_home, dry_run)

    from verify_shared import run as run_verify_shared
    from verify_trade_capability import run as verify_trade
    from verify_fibo_capability import run as verify_fibo
    from adapter_wiring import verify_adapter_wiring

    systemd_dir = Path(args.systemd_dir) if args.systemd_dir else Path("/etc/systemd/system")

    shared_ok = run_verify_shared(
        argv=[], hermes_root=hermes_root, hermes_home=hermes_home,
        capabilities=caps, dry_run=dry_run,
    )
    failed: List[str] = []
    if not shared_ok:
        failed.append("shared")
    for cap in caps:
        if cap == "trade":
            ok = verify_trade(argv=[], hermes_root=hermes_root, hermes_home=hermes_home)
        elif cap == "fibo":
            ok = verify_fibo(
                argv=[],
                hermes_root=hermes_root,
                hermes_home=hermes_home,
                systemd_dir=systemd_dir,
            )
        else:
            raise SystemExit(f"unknown capability: {cap}")
        if not ok:
            failed.append(cap)

    # Layer B: Telegram adapter dispatch must actually be wired. Payload
    # presence alone is not enough (Lodo fresh-install regression).
    print("==> verify telegram adapter wiring")
    wire_ok, wire_msgs = verify_adapter_wiring(hermes_root=hermes_root, capabilities=caps)
    for msg in wire_msgs:
        print(f"    {msg}")
    if not wire_ok:
        failed.append("adapter_wiring")

    # Layer C: BotCommand publication (plugins.enabled + menu capacity).
    from adapter_wiring import verify_command_menu_publication

    print("==> verify telegram command menu publication")
    menu_ok, menu_msgs = verify_command_menu_publication(
        hermes_home=hermes_home, capabilities=caps
    )
    for msg in menu_msgs:
        print(f"    {msg}")
    if not menu_ok:
        failed.append("command_menu")

    if failed:
        print(f"VERIFY FAILED: {', '.join(failed)}")
        return 1
    print(f"OK -- verified: {', '.join(caps)}")
    return 0


# ---------------------------------------------------------------------------
# Action: uninstall
# ---------------------------------------------------------------------------

def cmd_uninstall(args: argparse.Namespace) -> int:
    """Dispatch ./uninstall.sh."""
    caps = _resolve_capabilities(args)
    hermes_root = resolve_hermes_root(args.hermes_root)
    hermes_home = resolve_hermes_home() if args.hermes_home is None else Path(args.hermes_home).expanduser()
    dry_run = bool(args.dry_run)
    purge = bool(args.purge_state)
    _print_banner("uninstall", caps, hermes_root, hermes_home, dry_run)

    from uninstall_trade_capability import run as uninstall_trade
    from uninstall_fibo_capability import run as uninstall_fibo
    from uninstall_shared import run as uninstall_shared
    from adapter_wiring import remove_adapter_wiring
    from capabilities import is_installed as _is_installed

    results: List[dict] = []
    systemd_dir = Path(args.systemd_dir) if args.systemd_dir else Path("/etc/systemd/system")
    for cap in reversed(caps):
        if cap == "trade":
            res = uninstall_trade(
                argv=[], hermes_root=hermes_root, hermes_home=hermes_home,
                dry_run=dry_run,
            )
        elif cap == "fibo":
            res = uninstall_fibo(
                argv=[], hermes_root=hermes_root, hermes_home=hermes_home,
                dry_run=dry_run,
                systemd_dir=systemd_dir,
                no_restart=bool(args.no_restart),
            )
        else:
            raise SystemExit(f"unknown capability: {cap}")
        results.append(res)

    # Compute remaining capabilities after this uninstall (from current
    # manifest, minus the ones being removed).
    remaining = []
    for known in KNOWN_CAPABILITIES:
        if known in caps:
            continue
        if _is_installed(hermes_home, known):
            remaining.append(known)

    wiring = remove_adapter_wiring(
        hermes_root=hermes_root,
        capabilities=caps,
        remaining_capabilities=remaining,
        dry_run=dry_run,
    )
    print(f"adapter unwiring: {wiring.get('patches')}")

    if dry_run:
        print()
        print("DRY RUN COMPLETE -- zero mutations performed.")
        return 0

    if not wiring.get("ok", True):
        print(f"ERROR: adapter unwiring failed: {wiring.get('error')}", file=sys.stderr)
        return 1

    manifest = load_manifest(hermes_home)
    for cap in caps:
        clear_capability(manifest, cap)
    save_manifest(hermes_home, manifest)
    if not any_capability_installed(hermes_home):
        uninstall_shared(
            argv=[], hermes_root=hermes_root, hermes_home=hermes_home,
            dry_run=False,
        )
    print(f"manifest: {install_state_path(hermes_home)}")
    print(f"OK -- uninstalled: {', '.join(caps)}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    """Dispatch based on the action (install/verify/uninstall)."""
    action = "install"
    script = Path(sys.argv[0]).name if sys.argv else ""
    if script.endswith("verify.sh") or "verify" in script:
        action = "verify"
    elif script.endswith("uninstall.sh") or "uninstall" in script:
        action = "uninstall"
    for i, a in enumerate(argv):
        if a == "--action" and i + 1 < len(argv):
            action = argv[i + 1]
    args = _parse_args(argv, action)
    if action == "install":
        return cmd_install(args)
    elif action == "verify":
        return cmd_verify(args)
    elif action == "uninstall":
        return cmd_uninstall(args)
    else:
        print(f"unknown action: {action}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))