#!/usr/bin/env python3
"""Offline, read-only verification of a KAM /trade installation.

Never places or cancels an order, never queries a balance or position,
never contacts an exchange, never sends a Telegram message, never prints
a secret. Exits non-zero on any failed check.
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kamlib as K  # noqa: E402
from patchspecs import adapter_specs, all_specs, commands_specs  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


class Checks:
    def __init__(self) -> None:
        self.results: List[Tuple[str, bool, str]] = []

    def run(self, name: str, fn: Callable[[], str]) -> None:
        try:
            detail = fn() or ""
            self.results.append((name, True, detail))
            print(f"  [PASS] {name}" + (f" - {detail}" if detail else ""))
        except Exception as exc:  # noqa: BLE001
            self.results.append((name, False, str(exc)))
            print(f"  [FAIL] {name} - {exc}")

    @property
    def failed(self) -> List[Tuple[str, bool, str]]:
        return [r for r in self.results if not r[1]]


def _no_network_guard():
    """Context manager that makes any socket connection raise."""
    import socket

    class _Blocked(socket.socket):
        def connect(self, *a, **k):  # noqa: ANN002, ANN003
            raise AssertionError("import-time network access attempted")

        def connect_ex(self, *a, **k):  # noqa: ANN002, ANN003
            raise AssertionError("import-time network access attempted")

    class _Guard:
        def __enter__(self):
            self._orig = socket.socket
            socket.socket = _Blocked  # type: ignore[misc]
            return self

        def __exit__(self, *exc):  # noqa: ANN002
            socket.socket = self._orig  # type: ignore[misc]
            return False

    return _Guard()


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Verify the KAM /trade add-on (offline)")
    parser.add_argument("--hermes-root", default=None)
    parser.add_argument(
        "--dry-run-source", action="store_true",
        help="Verify the repository payload instead of an installed tree",
    )
    args = parser.parse_args(argv)

    print(f"KAM /trade verifier v{K.INSTALLER_VERSION}")

    try:
        hermes_root = K.resolve_hermes_root(args.hermes_root)
    except K.InstallError as exc:
        print(f"  [FAIL] Hermes discovery - {exc}")
        print()
        print("KAM /trade installation: FAIL")
        return 1

    source_mode = args.dry_run_source
    package_parent = REPO_ROOT if source_mode else hermes_root
    trade_root = package_parent / K.PLUGIN_RELATIVE_ROOT

    print(f"  hermes_root : {hermes_root}")
    print(f"  verifying   : {trade_root}" + (" (repo payload)" if source_mode else ""))
    print()

    c = Checks()
    sys.path.insert(0, str(package_parent))
    for name in [m for m in list(sys.modules) if m.startswith("plugins.trade")]:
        del sys.modules[name]

    # --- 1-6: imports -----------------------------------------------------
    def _import(mod: str) -> Callable[[], str]:
        def inner() -> str:
            with _no_network_guard():
                importlib.import_module(mod)
            return mod
        return inner

    c.run("trade package imports", _import("plugins.trade"))
    c.run("canonical models import", _import("plugins.trade.canonical"))
    c.run("TradeDesk imports", _import("plugins.trade.tradedesk"))
    c.run("wizard imports", _import("plugins.trade.wizard"))

    def check_no_plugin_py() -> str:
        for forbidden in ("plugin.py", "router.py"):
            if (trade_root / forbidden).exists():
                raise AssertionError(f"{forbidden} must not exist (direct dispatch design)")
        return "plugin.py and router.py correctly absent"

    c.run("no invented plugin entry points", check_no_plugin_py)

    def check_register_noop() -> str:
        mod = importlib.import_module("plugins.trade")
        if not hasattr(mod, "register"):
            raise AssertionError("plugins.trade.register missing")
        mod.register(None)
        return "register(ctx) is a safe no-op"

    c.run("plugin register() is a no-op", check_register_noop)

    # --- 7-8: exchange discovery -----------------------------------------
    discovered: List[str] = []

    def check_discovery() -> str:
        from plugins.trade.tradedesk import TradeDesk

        with _no_network_guard():
            names = list(TradeDesk().list_exchanges())
        discovered.extend(names)
        if not names:
            raise AssertionError("no exchange agents discovered")
        return f"{len(names)} agents: {', '.join(sorted(names))}"

    c.run("exchange discovery returns agents", check_discovery)

    def check_no_duplicates() -> str:
        if len(discovered) != len(set(discovered)):
            raise AssertionError(f"duplicate exchange names: {discovered}")
        return "no duplicates"

    c.run("no duplicate exchange names", check_no_duplicates)

    def check_agent_files_match() -> str:
        agents_dir = trade_root / "agents"
        files = sorted(
            p.stem[2:-6] for p in agents_dir.glob("x_*_agent.py")
        )
        if sorted(discovered) != files:
            raise AssertionError(f"discovered {sorted(discovered)} != files {files}")
        return f"filesystem and registry agree ({len(files)} agents)"

    c.run("discovery matches x_*_agent.py files", check_agent_files_match)

    # --- 9-11: Telegram integration --------------------------------------
    def check_entry_points() -> str:
        from plugins.trade import wizard

        for fn in ("handle_trade_command", "handle_trade_callback", "handle_trade_text"):
            if not callable(getattr(wizard, fn, None)):
                raise AssertionError(f"wizard.{fn} missing or not callable")
        return "all 3 adapter entry points present"

    c.run("/trade entry points exist", check_entry_points)

    if not source_mode:
        def check_adapter_wired() -> str:
            adapter = hermes_root / adapter_specs()[0].relative_path
            text = adapter.read_text()
            missing = [
                s.seam for s in adapter_specs() if s.native_sentinel not in text
            ]
            if missing:
                raise AssertionError(f"adapter seams not wired: {missing}")
            return "all 3 adapter seams present"

        c.run("Telegram adapter dispatches /trade", check_adapter_wired)

        def check_no_double_registration() -> str:
            adapter = hermes_root / adapter_specs()[0].relative_path
            text = adapter.read_text()
            for spec in adapter_specs():
                n = text.count(spec.native_sentinel)
                if n > 1:
                    raise AssertionError(
                        f"{spec.seam} wired {n} times (double registration)"
                    )
            return "each seam wired exactly once"

        c.run("handlers not registered twice", check_no_double_registration)

        def check_command_menu() -> str:
            spec = commands_specs()[0]
            text = (hermes_root / spec.relative_path).read_text()
            n = text.count(spec.native_sentinel)
            if n == 0:
                raise AssertionError("CommandDef(\"trade\" missing from commands.py")
            if n > 1:
                raise AssertionError(f"CommandDef(\"trade\" appears {n} times")
            return "command menu entry present exactly once"

        c.run("/trade in Telegram command menu", check_command_menu)

        def check_other_commands_intact() -> str:
            text = (hermes_root / commands_specs()[0].relative_path).read_text()
            for required in ('CommandDef("help"', 'CommandDef("new"', 'CommandDef("status"'):
                if required not in text:
                    raise AssertionError(f"unrelated command missing: {required}")
            return "unrelated Hermes commands intact"

        c.run("existing commands preserved", check_other_commands_intact)

    # --- 12: no enable flag ----------------------------------------------
    def check_no_trade_enabled() -> str:
        hits: List[str] = []
        for rel in K.iter_payload_files(trade_root):
            path = trade_root / rel
            if path.suffix not in {".py", ".yaml", ".yml", ".md"}:
                continue
            if "TRADE_ENABLED" in path.read_text(errors="ignore"):
                hits.append(str(rel))
        if hits:
            raise AssertionError(f"TRADE_ENABLED referenced in: {hits}")
        return "no TRADE_ENABLED anywhere"

    c.run("no TRADE_ENABLED dependency", check_no_trade_enabled)

    # --- 14-15: credential independence ----------------------------------
    def check_import_without_credentials() -> str:
        import os

        prefixes = tuple(
            f"{name.upper()}_" for name in (discovered or ["hyperliquid"])
        )
        saved = {k: v for k, v in os.environ.items() if k.startswith(prefixes)}
        for key in saved:
            os.environ.pop(key, None)
        try:
            for mod in [m for m in list(sys.modules) if m.startswith("plugins.trade")]:
                del sys.modules[mod]
            with _no_network_guard():
                from plugins.trade.tradedesk import TradeDesk

                TradeDesk().list_exchanges()
        finally:
            os.environ.update(saved)
        return "plugin imports with no exchange credentials present"

    c.run("no credentials needed to import", check_import_without_credentials)

    def check_missing_credentials_no_crash() -> str:
        """Missing credentials must yield no accounts, never a crash.

        Agents legitimately read both the process environment *and*
        ``$HERMES_HOME/.env``. To prove the no-credential path we point
        HERMES_HOME at an empty directory as well as clearing the env, so a
        real operator .env on this machine cannot mask the check.
        """
        import os
        import tempfile

        from plugins.trade.tradedesk import TradeDesk

        prefixes = tuple(f"{n.upper()}_" for n in discovered) or ("HYPERLIQUID_",)
        saved = {k: v for k, v in os.environ.items() if k.startswith(prefixes)}
        saved_home = os.environ.get("HERMES_HOME")
        for key in saved:
            os.environ.pop(key, None)

        leaked: List[str] = []
        with tempfile.TemporaryDirectory(prefix="kam-nocreds-") as empty_home:
            os.environ["HERMES_HOME"] = empty_home
            try:
                desk = TradeDesk()
                for exchange in discovered:
                    try:
                        accounts = desk.list_accounts(exchange)
                    except Exception as exc:  # noqa: BLE001
                        raise AssertionError(
                            f"{exchange}.list_accounts crashed without credentials: {exc}"
                        ) from exc
                    if accounts:
                        leaked.append(exchange)
            finally:
                os.environ.pop("HERMES_HOME", None)
                if saved_home is not None:
                    os.environ["HERMES_HOME"] = saved_home
                os.environ.update(saved)

        if leaked:
            raise AssertionError(
                "accounts reported with no credentials present: " + ", ".join(leaked)
            )
        return "missing credentials yield empty account lists, no crash"

    c.run("missing credentials degrade gracefully", check_missing_credentials_no_crash)

    # --- 19: syntax ------------------------------------------------------
    def check_compiles() -> str:
        proc = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", str(trade_root)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise AssertionError(proc.stdout.strip() or proc.stderr.strip())
        return "python syntax OK"

    c.run("python compiles", check_compiles)

    # --- 17: manifest ----------------------------------------------------
    if not source_mode:
        def check_manifest() -> str:
            manifest_file = K.find_manifest(hermes_root)
            manifest = K.read_manifest(manifest_file) if manifest_file else None
            if manifest is None:
                raise AssertionError("installed manifest not found")
            mismatches: List[str] = []
            for entry in manifest.get("copied_files", []):
                target = hermes_root / entry["path"]
                if not target.is_file():
                    mismatches.append(f"{entry['path']} missing")
                    continue
                if entry.get("sha256_after") and K.sha256_file(target) != entry["sha256_after"]:
                    mismatches.append(f"{entry['path']} hash drift")
            if mismatches:
                raise AssertionError("; ".join(mismatches[:5]))
            return f"{len(manifest.get('copied_files', []))} files match manifest hashes"

        c.run("manifest matches installed files", check_manifest)

    print()
    if c.failed:
        print("Failed checks:")
        for name, _, detail in c.failed:
            print(f"  - {name}: {detail}")
        print()
        print("KAM /trade installation: FAIL")
        return 1

    print(f"{len(c.results)} checks passed")
    print()
    print("KAM /trade installation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
