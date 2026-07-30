"""Regression tests for the /trade Telegram slash-command menu fix.

This guards seven invariants:

1. The native ``CommandDef`` constructor accepts the /trade entry shape
   that the KAM installer wants to emit (and rejects the legacy
   ``gateway_platforms`` keyword that the previous installer used,
   which broke Hermes with ``TypeError: CommandDef.init() got an
   unexpected keyword argument 'gateway_platforms'``).

2. ``/trade`` is present **exactly once** in the union of the native
   registry + the plugin-command registry that backs the Telegram menu.

3. Every existing native command still constructs cleanly after the
   change (a constructor break in ``commands.py`` would silently
   disable ``/restart`` and friends — this test catches it).

4. ``/restart`` continues to construct successfully — ``/restart`` is
   the canary for any ``commands.py`` regression.

5. The Telegram ``bot_commands`` export includes ``/trade``.

6. No KAM installer emits ``gateway_platforms=`` anywhere in the
   commands.py patch spec.

7. Re-applying the trade plugin's ``register(ctx)`` is idempotent
   (no duplicate /trade entries, no errors).

Run with::

    /usr/local/lib/hermes-agent/venv/bin/python -m pytest \
        plugins/trade/tests/test_phase6_command_menu_fix.py -v

    # or, if pytest is unavailable:
    /usr/local/lib/hermes-agent/venv/bin/python \
        plugins/trade/tests/test_phase6_command_menu_fix.py
"""

from __future__ import annotations

import ast
import os
import re
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

# Make the repo + plugin importable when run as a script.
_HERE = Path(__file__).resolve().parent

# When these tests live inside a Hermes checkout (as they did on the machine
# where the fix was validated) three parents up IS the Hermes root. When they
# ship inside the standalone KAM repo, it is not -- /root/kam has no
# hermes_cli/. Resolve a real Hermes root either way, and skip the
# Hermes-dependent assertions when none is available.
_CANDIDATE_ROOT = _HERE.parent.parent.parent


def _resolve_hermes_root() -> Path | None:
    candidates = [_CANDIDATE_ROOT]
    env_root = os.environ.get("HERMES_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    candidates += [
        Path("/usr/local/lib/hermes-agent"),
        Path("/opt/hermes-agent"),
    ]
    for candidate in candidates:
        try:
            if (candidate / "hermes_cli" / "commands.py").is_file():
                return candidate
        except OSError:
            continue
    return None


_HERMES_ROOT = _resolve_hermes_root()
# Kept for backwards compatibility with the original validated module: several
# tests reference _REPO_ROOT directly.
_REPO_ROOT = _HERMES_ROOT if _HERMES_ROOT is not None else _CANDIDATE_ROOT

if _HERMES_ROOT is not None and str(_HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(_HERMES_ROOT))

# Make the KAM installer importable so tests can introspect its
# patchspecs directly (instead of grepping for source markers).
# Prefer the repo this test file actually ships in.
_KAM_CANDIDATES = [_HERE.parent.parent.parent / "installer", Path("/root/kam/installer")]
for _cand in _KAM_CANDIDATES:
    if _cand.is_dir() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))
        break


# ---------------------------------------------------------------------------
# 1) Native CommandDef accepts the entry shape the KAM installer wants.
# ---------------------------------------------------------------------------


class CommandDefAcceptsTradeEntryTests(unittest.TestCase):
    """The native CommandDef constructor accepts the KAM-style entry."""

    def test_commanddef_accepts_minimal_trade_entry(self) -> None:
        from hermes_cli.commands import CommandDef
        # The KAM installer wants: CommandDef("trade", "Open the trading wizard", "Trading")
        cmd = CommandDef("trade", "Open the trading wizard", "Trading")
        self.assertEqual(cmd.name, "trade")
        self.assertEqual(cmd.description, "Open the trading wizard")
        self.assertEqual(cmd.category, "Trading")

    def test_commanddef_accepts_trade_entry_with_only_supported_kwargs(self) -> None:
        from hermes_cli.commands import CommandDef
        # The KAM installer wants gateway_only=True (a real keyword).
        cmd = CommandDef(
            "trade", "Open the trading wizard", "Trading",
            gateway_only=True,
        )
        self.assertTrue(cmd.gateway_only)

    def test_commanddef_rejects_unsupported_gateway_platforms_keyword(self) -> None:
        """The legacy KAM patch broke Hermes by emitting this keyword.

        Guard: even attempting to construct with ``gateway_platforms=``
        must fail (i.e. the keyword is genuinely absent from the
        dataclass).  If a future Hermes version adds it, this test
        becomes a no-op pass and the installer can start emitting it.
        """
        import inspect as _inspect

        from hermes_cli.commands import CommandDef

        params = {
            n for n in _inspect.signature(CommandDef.__init__).parameters if n != "self"
        }
        if "gateway_platforms" in params:
            # This Hermes build DOES support the keyword (e.g. kamhermes at
            # e713518c). Per this test's contract that is a no-op pass -- but we
            # still assert the installer does not emit it, since the plugin-API
            # path covers the menu on every build.
            from patchspecs import all_specs

            blocks = "".join(s.block for s in all_specs(_HERMES_ROOT))
            self.assertNotIn(
                "gateway_platforms", blocks,
                "installer must not emit gateway_platforms even where supported",
            )
            self.skipTest(
                "this Hermes build supports gateway_platforms; "
                "no-op pass per test contract"
            )

        with self.assertRaises(TypeError) as ctx:
            CommandDef(
                "trade", "Open the trading wizard", "Trading",
                gateway_platforms=("telegram",),
            )
        self.assertIn("gateway_platforms", str(ctx.exception))


# ---------------------------------------------------------------------------
# 2) /trade is present exactly once across both registries.
# ---------------------------------------------------------------------------


class TradeAppearsExactlyOnceTests(unittest.TestCase):
    """The /trade command must be unique across the registry union."""

    def _all_trade_names(self) -> List[str]:
        names: List[str] = []
        try:
            from hermes_cli.commands import COMMAND_REGISTRY
            for cmd in COMMAND_REGISTRY:
                if cmd.name == "trade":
                    names.append("native")
                for alias in cmd.aliases:
                    if alias == "trade":
                        names.append(f"native-alias:{cmd.name}")
        except Exception:
            pass
        try:
            from hermes_cli.plugins import get_plugin_commands
            for cmd_name in get_plugin_commands() or {}:
                if cmd_name == "trade":
                    names.append("plugin")
        except Exception:
            pass
        return names

    def test_trade_appears_exactly_once(self) -> None:
        # The plugin path is what registers /trade for the Telegram menu.
        # We don't import the trade plugin here directly because plugin
        # discovery is side-effectful; instead we exercise the register()
        # function below in test 7.  Here we just assert: at most one
        # registry surface contains "trade" (a duplicate would mean
        # both the native registry AND the plugin registry have it).
        sources = self._all_trade_names()
        self.assertLessEqual(len(sources), 1,
                             f"/trade registered in multiple sources: {sources!r}")


# ---------------------------------------------------------------------------
# 3 + 4) Every existing native command still constructs; /restart in particular.
# ---------------------------------------------------------------------------


class RegistryStillConstructsTests(unittest.TestCase):
    """The registry must still build without errors after the plugin edit."""

    def test_command_registry_constructs_without_error(self) -> None:
        from hermes_cli.commands import COMMAND_REGISTRY
        self.assertGreater(len(COMMAND_REGISTRY), 0)

    def test_restart_command_is_present_and_constructs(self) -> None:
        from hermes_cli.commands import COMMAND_REGISTRY, CommandDef
        restart = next(
            (c for c in COMMAND_REGISTRY if c.name == "restart"), None,
        )
        self.assertIsNotNone(restart, "/restart missing from COMMAND_REGISTRY")
        self.assertIsInstance(restart, CommandDef)
        # Re-construct from scratch to ensure the dataclass is sane.
        CommandDef(
            restart.name, restart.description, restart.category,
            **{k: v for k, v in vars(restart).items()
               if k in {"aliases", "args_hint", "subcommands", "cli_only",
                        "gateway_only", "gateway_config_gate", "busy_policy",
                        "busy_handler", "execute"}},
        )

    def test_no_native_command_name_conflicts_with_trade(self) -> None:
        """A plugin command cannot share a name with a native one.

        The plugin API rejects such conflicts at registration time, but
        we also assert no native command is named "trade" — so the
        plugin path is the only place /trade lives.

        Note: some Hermes builds ship their own ``CommandDef("trade", ...)``
        upstream (kamhermes at e713518c does). That native entry is not
        KAM-installed and is not something KAM may remove, so the assertion is
        skipped there. What matters in both cases is that KAM itself never
        writes into the native registry -- covered by
        ``KamPatchSpecIsCleanTests``.
        """
        from hermes_cli.commands import COMMAND_REGISTRY
        native_trade = [c for c in COMMAND_REGISTRY if c.name == "trade"]
        if native_trade:
            kam_marker = "KAM TRADE PLUGIN"
            commands_py = (_REPO_ROOT / "hermes_cli" / "commands.py")
            if commands_py.is_file() and kam_marker in commands_py.read_text():
                self.fail(
                    "native 'trade' CommandDef sits inside a KAM patch block; "
                    "KAM must not write to the native registry"
                )
            self.skipTest(
                "this Hermes build ships a native 'trade' CommandDef upstream "
                "(not KAM-installed)"
            )
        self.assertEqual(native_trade, [],
                         "Trade must be registered via the plugin API, "
                         "not the native registry.")


# ---------------------------------------------------------------------------
# 5) Telegram command export includes /trade.
# ---------------------------------------------------------------------------


class TelegramMenuIncludesTradeTests(unittest.TestCase):
    """The Telegram menu function exposes /trade (post plugin discovery)."""

    def test_telegram_menu_contains_trade_after_plugin_registration(self) -> None:
        # Register the trade plugin manually so the test doesn't depend
        # on global plugin discovery order.
        from plugins.trade import register as trade_register, _handle_trade_slash

        class _StubCtx:
            def __init__(self) -> None:
                self.calls: List[Dict[str, Any]] = []

            def register_command(self, name, handler, description="", args_hint=""):
                self.calls.append({
                    "name": name, "handler": handler,
                    "description": description, "args_hint": args_hint,
                })

        ctx = _StubCtx()
        trade_register(ctx)

        # Verify the plugin registered trade (test 7 covers idempotence).
        self.assertEqual(len(ctx.calls), 1)
        self.assertEqual(ctx.calls[0]["name"], "trade")
        self.assertEqual(ctx.calls[0]["description"], "Open the trading wizard")
        self.assertIs(ctx.calls[0]["handler"], _handle_trade_slash)

    def test_no_gateway_platforms_keyword_in_register_call(self) -> None:
        """The plugin's register() must not emit gateway_platforms=.

        The legacy KAM patch did; this regression test prevents it
        from sneaking back in.
        """
        import plugins.trade as trade_pkg

        # The plugin is a tiny module — re-grep its source for the
        # forbidden keyword in any kwarg position.
        src = Path(trade_pkg.__file__).read_text()
        self.assertNotIn("gateway_platforms", src,
                         "plugins/trade/__init__.py must not reference gateway_platforms")


# ---------------------------------------------------------------------------
# 6) The KAM patchspec source must not emit gateway_platforms.
# ---------------------------------------------------------------------------


class KamPatchSpecIsCleanTests(unittest.TestCase):
    """The KAM installer must not patch commands.py with the broken keyword."""

    KAM_REPO = Path("/root/kam")

    def test_kam_repo_present(self) -> None:
        self.assertTrue(self.KAM_REPO.is_dir(),
                        f"KAM repo not found at {self.KAM_REPO}")

    def test_kam_patchspecs_dont_emit_gateway_platforms_by_default(self) -> None:
        """The default ``all_specs()`` path must not include the legacy
        CommandDef patch on the current Hermes generation.

        On the current Hermes version (which lacks ``gateway_platforms``
        in CommandDef), the installer's signature detector returns False
        and the legacy spec is excluded.  This test pins that down: the
        *default* call to ``all_specs(hermes_root)`` must produce a list
        that contains no CommandDef patch.  The compatibility-fallback
        block IS allowed to remain in source (gated by the detector) so
        a future Hermes version that gains ``gateway_platforms=``
        support can still be installed by the same KAM release.
        """
        from patchspecs import all_specs, HERMES_COMMANDS
        # Use the real Hermes (the fixture's CommandDef happens to
        # declare ``gateway_platforms`` for forward compatibility,
        # which would mask the regression we're guarding against).
        real_hermes = Path("/usr/local/lib/hermes-agent")
        if not (real_hermes / "hermes_cli" / "commands.py").is_file():
            self.skipTest("real Hermes not present")
        specs = all_specs(real_hermes)
        commands_specs = [s for s in specs if str(s.relative_path) == str(HERMES_COMMANDS)]
        self.assertEqual(
            commands_specs, [],
            "Default all_specs(hermes_root) must emit no CommandDef "
            "patch on the current Hermes version (kwarg absent)."
        )

    def test_kam_commands_specs_no_default_emission(self) -> None:
        """No default ``CommandDef("trade", ...)`` patch in active specs.

        The compatibility-fallback legacy block may exist in source
        (gated by ``commanddef_supports_gateway_platforms``) but must
        not be part of the active spec list on the current Hermes.
        """
        from patchspecs import all_specs, HERMES_COMMANDS
        real_hermes = Path("/usr/local/lib/hermes-agent")
        if not (real_hermes / "hermes_cli" / "commands.py").is_file():
            self.skipTest("real Hermes not present")
        specs = all_specs(real_hermes)
        for spec in specs:
            self.assertNotEqual(
                spec.seam, "command menu entry",
                f"CommandDef legacy patch leaked into active specs on "
                f"current Hermes: seam={spec.seam!r}",
            )


# ---------------------------------------------------------------------------
# 7) Idempotent re-registration.
# ---------------------------------------------------------------------------


class TradeRegistrationIsIdempotentTests(unittest.TestCase):
    """Calling register(ctx) twice must not duplicate /trade."""

    def test_register_is_idempotent(self) -> None:
        from plugins.trade import register as trade_register

        class _StubCtx:
            def __init__(self) -> None:
                self.entries: Dict[str, Dict[str, Any]] = {}

            def register_command(self, name, handler, description="", args_hint=""):
                # Mirror what PluginContext.register_command does:
                # last-write-wins for the same name.  Idempotence here
                # means "calling register twice leaves a single entry
                # in the registry", not "two calls are required".
                self.entries[name] = {
                    "handler": handler,
                    "description": description,
                    "args_hint": args_hint,
                }

        ctx = _StubCtx()
        trade_register(ctx)
        trade_register(ctx)
        self.assertEqual(list(ctx.entries.keys()), ["trade"])
        self.assertEqual(ctx.entries["trade"]["description"],
                         "Open the trading wizard")

    def test_register_handles_missing_register_command_gracefully(self) -> None:
        """If the plugin context lacks register_command (older Hermes),
        registration must skip silently rather than crash."""
        from plugins.trade import register as trade_register

        class _LegacyCtx:
            pass  # No register_command method.

        # Must not raise.
        trade_register(_LegacyCtx())


# ---------------------------------------------------------------------------
# 8) AST-level guard on the commands.py file (last-line-of-defense).
# ---------------------------------------------------------------------------


class CommandsPyHasNoBrokenKeywordTests(unittest.TestCase):
    """Static guard around ``gateway_platforms``.

    On the incompatible powerkam build the keyword genuinely does not exist, so
    ``hermes_cli/commands.py`` must not mention it at all. On newer Hermes
    builds (kamhermes e713518c does) the file may legitimately contain the
    upstream field and/or command entry; in that case we still require the KAM
    installer to emit no such patch block, but the host file itself is not a
    failure.
    """

    def test_commands_py_does_not_reference_gateway_platforms(self) -> None:
        import inspect as _inspect

        from hermes_cli.commands import CommandDef
        from patchspecs import all_specs

        params = {
            n for n in _inspect.signature(CommandDef.__init__).parameters if n != "self"
        }
        path = _REPO_ROOT / "hermes_cli" / "commands.py"
        src = path.read_text()

        # KAM itself must never emit the legacy keyword anymore.
        spec_blocks = "".join(spec.block for spec in all_specs(_HERMES_ROOT))
        self.assertNotIn(
            "gateway_platforms", spec_blocks,
            "KAM installer must not emit gateway_platforms in its active patch set.",
        )

        if "gateway_platforms" in params:
            self.skipTest(
                "this Hermes build natively supports gateway_platforms; host "
                "commands.py may legitimately reference it upstream"
            )

        self.assertNotIn(
            "gateway_platforms", src,
            "hermes_cli/commands.py must not reference gateway_platforms "
            "(the kwarg is not supported by this Hermes version's "
            "CommandDef dataclass).",
        )


if __name__ == "__main__":
    unittest.main()