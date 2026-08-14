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
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kamlib as K  # noqa: E402
import kamconfig as KC  # noqa: E402
from patchspecs import (  # noqa: E402
    adapter_specs,
    all_specs,
    legacy_commands_specs,
)

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
    parser.add_argument("--systemd-dir", default="/etc/systemd/system")
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

    def check_register_registers_trade() -> str:
        """register() must advertise /trade and /fibo via the supported plugin API."""
        mod = importlib.import_module("plugins.trade")
        if not hasattr(mod, "register"):
            raise AssertionError("plugins.trade.register missing")

        seen: List[Dict[str, Any]] = []

        class _Ctx:
            def register_command(self, name, handler=None, description="", args_hint=""):
                seen.append({"name": name, "handler": handler, "description": description})

        mod.register(_Ctx())
        names = [s["name"] for s in seen]
        if names.count("trade") != 1:
            raise AssertionError(f"expected one 'trade' registration, got {names}")
        if names.count("fibo") != 1:
            raise AssertionError(f"expected one 'fibo' registration, got {names}")

        class _Bare:
            pass

        mod.register(_Bare())
        mod.register(None)
        return "registers 'trade' and 'fibo' once each; tolerates old contexts"

    c.run("plugin register() advertises /trade + /fibo", check_register_registers_trade)

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
        from plugins.trade import fibo_wizard, wizard

        for fn in ("handle_trade_command", "handle_trade_callback", "handle_trade_text"):
            if not callable(getattr(wizard, fn, None)):
                raise AssertionError(f"wizard.{fn} missing or not callable")
        for fn in ("handle_fibo_command", "handle_fibo_callback", "handle_fibo_text"):
            if not callable(getattr(fibo_wizard, fn, None)):
                raise AssertionError(f"fibo_wizard.{fn} missing or not callable")
        return "all /trade and /fibo adapter entry points present"

    c.run("/trade + /fibo entry points exist", check_entry_points)

    def check_initial_screen_has_keyboard() -> str:
        """The first /trade screen must carry text AND a non-empty keyboard.

        This is the regression that lost the inline keyboard: routing worked but
        the markup never reached Telegram.
        """
        from plugins.trade.tradedesk import TradeDesk
        from plugins.trade.wizard import TradeWizard

        with _no_network_guard():
            desk = TradeDesk()
            exchanges = sorted(desk.list_exchanges())
            wizard = TradeWizard(tradedesk=desk)
            screen = wizard.open(("verify", "chat"))

        text = getattr(screen, "text", "") or ""
        buttons = getattr(screen, "buttons", None) or []
        if "exchange" not in text.lower():
            raise AssertionError(f"initial screen text lacks an exchange prompt: {text!r}")
        flat = [b for row in buttons for b in (row or [])]
        if not flat:
            raise AssertionError("initial screen has an EMPTY keyboard")

        cbs = " ".join(str(b.get("callback_data", "")) for b in flat if isinstance(b, dict))
        missing = [e for e in exchanges if e not in cbs]
        if missing:
            raise AssertionError(f"no callback_data for exchanges: {missing}")
        return f"{len(flat)} button(s), callbacks for all {len(exchanges)} exchanges"

    c.run("initial /trade screen has a non-empty keyboard", check_initial_screen_has_keyboard)

    if not source_mode:
        def check_adapter_preserves_markup() -> str:
            """The adapter must expose an inline-keyboard sender for the wizard."""
            adapter = hermes_root / adapter_specs()[0].relative_path
            text = adapter.read_text()
            if "async def send_inline_keyboard" not in text:
                raise AssertionError(
                    "adapter has no send_inline_keyboard; the wizard cannot render buttons"
                )
            start = text.index("async def send_inline_keyboard")
            body = text[start:start + 4000]
            for needed, why in (
                ("InlineKeyboardMarkup", "markup construction"),
                ("reply_markup", "markup attached to the outgoing message"),
            ):
                if needed not in body:
                    raise AssertionError(f"send_inline_keyboard missing {needed} ({why})")
            return "adapter builds InlineKeyboardMarkup and sets reply_markup"

        c.run("adapter preserves inline markup", check_adapter_preserves_markup)

    if not source_mode:
        def check_adapter_wired() -> str:
            adapter = hermes_root / adapter_specs()[0].relative_path
            text = adapter.read_text()
            missing = [
                s.seam for s in adapter_specs() if s.native_sentinel not in text
            ]
            if missing:
                raise AssertionError(f"adapter seams not wired: {missing}")
            return "all /trade and /fibo adapter seams present"

        c.run("Telegram adapter dispatches /trade + /fibo", check_adapter_wired)

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
            """/trade and /fibo must be advertised via the plugin API, not a commands.py patch."""
            import inspect as _inspect

            plugin = importlib.import_module("plugins.trade")
            src = _inspect.getsource(plugin.register)
            if "register_command" not in src:
                raise AssertionError("plugins.trade.register does not call register_command")
            if "gateway_platforms" in src:
                raise AssertionError("plugin register() emits gateway_platforms")

            recorded: List[Dict[str, Any]] = []

            class _Ctx:
                def register_command(self, name, handler=None, description="", args_hint=""):
                    recorded.append({
                        "name": name, "handler": handler,
                        "description": description, "args_hint": args_hint,
                    })

            plugin.register(_Ctx())
            names = [r["name"] for r in recorded]
            if names.count("trade") != 1:
                raise AssertionError(f"register() registered 'trade' {names.count('trade')}x")
            if names.count("fibo") != 1:
                raise AssertionError(f"register() registered 'fibo' {names.count('fibo')}x")
            missing_desc = [entry["name"] for entry in recorded if not callable(entry["handler"]) or not entry["description"].strip()]
            if missing_desc:
                raise AssertionError(f"registered commands missing callable/description: {missing_desc}")
            return "register_command('trade') and register_command('fibo') both present"

        c.run("/trade + /fibo registered via plugin API exactly once", check_command_menu)

        def check_register_tolerates_old_context() -> str:
            """An older Hermes whose context lacks register_command must not crash."""
            plugin = importlib.import_module("plugins.trade")

            class _Bare:
                pass

            plugin.register(_Bare())
            plugin.register(None)
            return "register() degrades gracefully without register_command"

        c.run("plugin registration is compatibility-safe", check_register_tolerates_old_context)

        def check_no_commands_py_patch() -> str:
            """The retired commands.py patch must not be applied."""
            from patchspecs import all_specs as _all

            for spec in _all():
                if spec.relative_path.name == "commands.py":
                    raise AssertionError("commands.py patch is in the default install path")
            blocks = "".join(s.block for s in _all())
            if "gateway_platforms" in blocks:
                raise AssertionError("a default patch block emits gateway_platforms")
            return "no commands.py patch, no gateway_platforms emitted"

        c.run("no legacy commands.py patch applied", check_no_commands_py_patch)

        def check_commanddef_signature_respected() -> str:
            """Never emit a keyword the target build's CommandDef rejects."""
            import inspect as _inspect

            sys.path.insert(0, str(hermes_root))
            try:
                from hermes_cli.commands import CommandDef  # type: ignore
            except Exception as exc:  # noqa: BLE001
                return f"CommandDef not importable ({type(exc).__name__}); nothing emitted anyway"

            params = {
                n for n, p in _inspect.signature(CommandDef.__init__).parameters.items()
                if n != "self"
            }
            blocks = "".join(s.block for s in all_specs(hermes_root))
            emitted = {
                kw for kw in ("gateway_platforms", "gateway_only", "gateway_config_gate")
                if f"{kw}=" in blocks
            }
            unsupported = emitted - params
            if unsupported:
                raise AssertionError(f"emits unsupported CommandDef kwargs: {unsupported}")
            has_gp = "gateway_platforms" in params
            return (
                f"target CommandDef {'supports' if has_gp else 'lacks'} gateway_platforms; "
                f"installer emits none"
            )

        c.run("CommandDef signature respected", check_commanddef_signature_respected)

        def check_native_registry_constructs() -> str:
            """The native command registry must still build (this is /restart).

            Importing hermes_cli.commands executes the module-level command
            table. The legacy patch injected an unsupported CommandDef keyword
            here, raising TypeError during import and taking every native
            command down with it -- which is how /restart broke.
            """
            probe = (
                "import sys; sys.path.insert(0, %r)\n"
                "import hermes_cli.commands as m\n"
                "names = set()\n"
                "reg = getattr(m, 'COMMAND_REGISTRY', None)\n"
                "if reg:\n"
                "    names |= {str(getattr(c, 'name', c)).lstrip('/') for c in reg}\n"
                "cmds = getattr(m, 'COMMANDS', None)\n"
                "if isinstance(cmds, dict):\n"
                "    names |= {str(k).lstrip('/') for k in cmds}\n"
                "elif cmds:\n"
                "    names |= {str(getattr(c, 'name', c)).lstrip('/') for c in cmds}\n"
                "assert 'restart' in names, sorted(names)[:25]\n"
                "print('OK', len(names))\n" % str(hermes_root)
            )
            proc = subprocess.run([sys.executable, "-c", probe],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                raise AssertionError(
                    "native command registry failed to construct: "
                    + (proc.stderr.strip().splitlines() or [""])[-1][:200]
                )
            return f"registry constructs, /restart present ({proc.stdout.strip()})"

        c.run("native command registry intact (/restart)", check_native_registry_constructs)

        def check_config_enables_trade() -> str:
            """trade must be enabled exactly once via the real config loader."""
            sys.path.insert(0, str(hermes_root))
            hermes_home = K.resolve_hermes_home()
            config_path = KC.find_config(hermes_home)
            if config_path is None:
                return f"no config under {hermes_home}; menu entry not asserted"

            parsed = KC.parse_config(config_path)          # raises on malformed
            enabled = KC.enabled_plugins(parsed)
            count = enabled.count("trade")
            if count != 1:
                raise AssertionError(
                    f"plugins.enabled contains 'trade' {count}x (expected 1): {enabled}"
                )

            probe = (
                "import sys; sys.path.insert(0, %r)\n"
                "from hermes_cli.config import load_config\n"
                "load_config()\n"
                "from hermes_cli.plugins import _get_enabled_plugins\n"
                "e = _get_enabled_plugins()\n"
                "assert 'trade' in e, e\n"
                "print('OK')\n" % str(hermes_root)
            )
            proc = subprocess.run([sys.executable, "-c", probe],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                raise AssertionError(
                    "load_config()/_get_enabled_plugins() rejected the config: "
                    + (proc.stderr.strip().splitlines() or [""])[-1][:200]
                )
            return f"trade enabled exactly once; {len(enabled)} plugin(s) enabled"

        c.run("Hermes config enables trade exactly once", check_config_enables_trade)

        def check_other_commands_intact() -> str:
            spec = legacy_commands_specs()[0]
            text = (hermes_root / spec.relative_path).read_text()
            for required in ('CommandDef("help"', 'CommandDef("new"', 'CommandDef("status"'):
                if required not in text:
                    raise AssertionError(f"unrelated command missing: {required}")
            return "unrelated Hermes commands intact"

        c.run("existing commands preserved", check_other_commands_intact)

        def check_fibo_service_unit() -> str:
            unit = Path(args.systemd_dir) / "fibo.service"
            if not unit.is_file():
                raise AssertionError(f"missing fibo.service unit: {unit}")
            text = unit.read_text(encoding="utf-8")
            for required in (
                "-m plugins.trade.fibo_daemon",
                "Environment=PYTHONPATH=",
                "Environment=HERMES_HOME=",
                "service.sock",
                "service_state.json",
            ):
                if required not in text:
                    raise AssertionError(f"fibo.service missing required token: {required}")
            return str(unit)

        c.run("fibo.service unit installed", check_fibo_service_unit)

        def check_fibo_service_empty_startup() -> str:
            from plugins.trade import fibo_daemon
            from plugins.trade.fibo_service import PersistentFiboService

            with tempfile.TemporaryDirectory(prefix="kam-fibo-verify-") as tmp:
                tmpdir = Path(tmp)
                service = PersistentFiboService(
                    state_path=tmpdir / "service_state.json",
                    event_log_path=tmpdir / "events.jsonl",
                    start_thread=False,
                )
                listed = service.execute_command({"op": "list"})
                if not listed.get("ok"):
                    raise AssertionError(f"list failed: {listed}")
                if listed.get("registrations") != []:
                    raise AssertionError(f"expected empty registration list, got {listed.get('registrations')}")
                service.shutdown()
                if not callable(getattr(fibo_daemon, "main", None)):
                    raise AssertionError("plugins.trade.fibo_daemon.main missing")
            return "service imports and starts empty; LIST returns []"

        c.run("fibo service imports and starts empty", check_fibo_service_empty_startup)

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
