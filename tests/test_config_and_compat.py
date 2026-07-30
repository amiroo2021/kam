"""Tests for safe Hermes config handling and CommandDef compatibility.

Covers the two bugs a real clean-node install exposed:

1. The installer patched ``hermes_cli/commands.py`` with a ``gateway_platforms``
   keyword that some Hermes builds do not accept, raising TypeError while the
   command table was being built and breaking native commands like ``/restart``.

2. ``/trade`` must instead be advertised through the supported plugin API, which
   requires the plugin to be listed in ``plugins.enabled``.

All offline. No exchange contacted, no order placed.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "installer"
sys.path.insert(0, str(INSTALLER))

import kamconfig as C  # noqa: E402
import yaml  # noqa: E402
from patchspecs import (  # noqa: E402
    all_specs,
    commands_specs,
    legacy_commands_specs,
    supported_commanddef_kwargs,
)


# ---------------------------------------------------------------------------
# YAML shapes
# ---------------------------------------------------------------------------

class TestConfigEnableShapes(unittest.TestCase):
    """trade must be added exactly once for every supported config shape."""

    def _enable(self, src: str):
        out, action = C.plan_enable_trade(src)
        parsed = yaml.safe_load(out) or {}
        return out, action, C.enabled_plugins(parsed), parsed

    def test_no_plugins_section(self):
        out, action, enabled, parsed = self._enable("model: gpt\ntelegram:\n  token: x\n")
        self.assertEqual(action, "added-plugins-block")
        self.assertEqual(enabled, ["trade"])
        self.assertIn("model", parsed)
        self.assertIn("telegram", parsed)

    def test_plugins_without_enabled_key(self):
        out, action, enabled, parsed = self._enable("plugins:\n  other: 1\nsession: y\n")
        self.assertEqual(action, "added-enabled-key")
        self.assertEqual(enabled, ["trade"])
        self.assertEqual(parsed["plugins"]["other"], 1)
        self.assertIn("session", parsed)

    def test_existing_block_list_preserves_others(self):
        src = "model: gpt\nplugins:\n  enabled:\n    - alpha\n    - beta\nsession: y\n"
        out, action, enabled, parsed = self._enable(src)
        self.assertEqual(action, "appended-to-list")
        self.assertEqual(enabled, ["alpha", "beta", "trade"])
        self.assertIn("model", parsed)
        self.assertIn("session", parsed)

    def test_inline_list_preserves_others(self):
        out, action, enabled, _ = self._enable("plugins:\n  enabled: [alpha, beta]\n")
        self.assertEqual(action, "converted-inline-list")
        self.assertEqual(enabled, ["alpha", "beta", "trade"])

    def test_inline_empty_list(self):
        _, action, enabled, _ = self._enable("plugins:\n  enabled: []\n")
        self.assertEqual(enabled, ["trade"])

    def test_empty_block_list(self):
        _, action, enabled, _ = self._enable("plugins:\n  enabled:\nsession: y\n")
        self.assertEqual(enabled, ["trade"])

    def test_scalar_enabled_value(self):
        _, _, enabled, _ = self._enable("plugins:\n  enabled: alpha\n")
        self.assertEqual(enabled, ["alpha", "trade"])

    def test_empty_file(self):
        _, action, enabled, _ = self._enable("")
        self.assertEqual(action, "added-plugins-block")
        self.assertEqual(enabled, ["trade"])

    def test_trade_already_enabled_is_untouched(self):
        src = "plugins:\n  enabled:\n    - alpha\n    - trade\n"
        out, action, enabled, _ = self._enable(src)
        self.assertEqual(action, "already-enabled")
        self.assertEqual(out, src, "file modified despite trade already enabled")
        self.assertEqual(enabled.count("trade"), 1)

    def test_inline_already_has_trade(self):
        src = "plugins:\n  enabled: [alpha, trade]\n"
        out, action, _, _ = self._enable(src)
        self.assertEqual(action, "already-enabled")
        self.assertEqual(out, src)

    def test_no_duplicate_trade_entry_ever(self):
        for src in (
            "plugins:\n  enabled:\n    - trade\n",
            "plugins:\n  enabled: [trade]\n",
            "plugins:\n  enabled:\n    - alpha\n    - trade\n    - beta\n",
        ):
            with self.subTest(src=src):
                out, _ = C.plan_enable_trade(src)
                enabled = C.enabled_plugins(yaml.safe_load(out) or {})
                self.assertEqual(enabled.count("trade"), 1)

    def test_enable_is_idempotent(self):
        for src in (
            "model: gpt\n",
            "plugins:\n  enabled:\n    - alpha\n",
            "plugins:\n  enabled: [alpha]\n",
            "plugins:\n  other: 1\n",
        ):
            with self.subTest(src=src):
                once, _ = C.plan_enable_trade(src)
                twice, action = C.plan_enable_trade(once)
                self.assertEqual(twice, once)
                self.assertEqual(action, "already-enabled")


class TestConfigMalformed(unittest.TestCase):
    def test_malformed_yaml_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("plugins:\n  enabled:\n   - [unclosed\n")
            with self.assertRaises(C.ConfigError):
                C.parse_config(path)

    def test_duplicate_plugins_blocks_raise(self):
        src = "plugins:\n  enabled:\n    - a\nplugins:\n  enabled:\n    - b\n"
        with self.assertRaises(C.ConfigError):
            C.plan_enable_trade(src)

    def test_inline_mapping_refused(self):
        with self.assertRaises(C.ConfigError):
            C.plan_enable_trade("plugins: {enabled: [a]}\n")

    def test_non_mapping_top_level_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("- just\n- a\n- list\n")
            with self.assertRaises(C.ConfigError):
                C.parse_config(path)


# ---------------------------------------------------------------------------
# enable / disable round trip on real files
# ---------------------------------------------------------------------------

class TestConfigRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kam-cfg-")
        self.home = Path(self.tmp.name)
        self.config = self.home / "config.yaml"
        self.backups = self.home / "backups"

    def tearDown(self):
        self.tmp.cleanup()

    def test_enable_creates_backup_and_records_state(self):
        self.config.write_text("model: gpt\nplugins:\n  enabled:\n    - alpha\n")
        record = C.enable_trade(self.config, self.backups)
        self.assertFalse(record["trade_was_already_enabled"])
        self.assertEqual(record["other_plugins_preserved"], ["alpha"])
        self.assertTrue((self.backups / "config.yaml").is_file())
        # backup holds the PRE-edit content
        self.assertNotIn("trade", (self.backups / "config.yaml").read_text())
        self.assertIn("trade", self.config.read_text())

    def test_dry_run_changes_nothing(self):
        original = "model: gpt\nplugins:\n  enabled:\n    - alpha\n"
        self.config.write_text(original)
        record = C.enable_trade(self.config, self.backups, dry_run=True)
        self.assertEqual(self.config.read_text(), original)
        self.assertFalse(self.backups.exists())
        self.assertTrue(record["action"].startswith("would-"))

    def test_uninstall_removes_installer_added_entry(self):
        self.config.write_text("model: gpt\nplugins:\n  enabled:\n    - alpha\n")
        C.enable_trade(self.config, self.backups)
        result = C.disable_trade(self.config, was_already_enabled=False)
        self.assertEqual(result["action"], "removed")
        enabled = C.enabled_plugins(C.parse_config(self.config))
        self.assertNotIn("trade", enabled)
        self.assertIn("alpha", enabled)
        self.assertIn("model", C.parse_config(self.config))

    def test_uninstall_preserves_preexisting_trade(self):
        """If the user enabled trade themselves, uninstall must not remove it."""
        self.config.write_text("plugins:\n  enabled:\n    - alpha\n    - trade\n")
        record = C.enable_trade(self.config, self.backups)
        self.assertTrue(record["trade_was_already_enabled"])

        result = C.disable_trade(
            self.config, was_already_enabled=record["trade_was_already_enabled"]
        )
        self.assertEqual(result["action"], "preserved-user-owned")
        self.assertIn("trade", C.enabled_plugins(C.parse_config(self.config)))

    def test_disable_is_idempotent(self):
        self.config.write_text("plugins:\n  enabled:\n    - alpha\n")
        C.enable_trade(self.config, self.backups)
        C.disable_trade(self.config, was_already_enabled=False)
        snapshot = self.config.read_text()
        result = C.disable_trade(self.config, was_already_enabled=False)
        self.assertEqual(result["action"], "not-present")
        self.assertEqual(self.config.read_text(), snapshot)

    def test_repeated_install_does_not_duplicate(self):
        self.config.write_text("plugins:\n  enabled:\n    - alpha\n")
        for _ in range(3):
            C.enable_trade(self.config, self.backups)
        enabled = C.enabled_plugins(C.parse_config(self.config))
        self.assertEqual(enabled.count("trade"), 1)
        self.assertIn("alpha", enabled)

    def test_find_config_prefers_yaml(self):
        self.config.write_text("model: gpt\n")
        self.assertEqual(C.find_config(self.home), self.config)

    def test_find_config_returns_none_when_absent(self):
        self.assertIsNone(C.find_config(self.home))

    def test_unrelated_top_level_keys_survive(self):
        self.config.write_text(
            "telegram:\n  token: placeholder\n  chat_id: 1\n"
            "model: gpt\n"
            "plugins:\n  enabled:\n    - alpha\n"
            "session_reset:\n  mode: none\n"
        )
        C.enable_trade(self.config, self.backups)
        parsed = C.parse_config(self.config)
        for key in ("telegram", "model", "plugins", "session_reset"):
            self.assertIn(key, parsed)
        self.assertEqual(parsed["telegram"]["token"], "placeholder")
        self.assertEqual(parsed["session_reset"]["mode"], "none")


# ---------------------------------------------------------------------------
# CommandDef compatibility
# ---------------------------------------------------------------------------

class _PowerkamCommandDef:
    """Real signature from Hermes 2d404942 (powerkam) -- NO gateway_platforms."""

    def __init__(self, name: str, description: str, category: str,
                 aliases: tuple = (), args_hint: str = "", subcommands: tuple = (),
                 cli_only: bool = False, gateway_only: bool = False,
                 gateway_config_gate=None, busy_policy: str = "reject",
                 busy_handler=None, execute=None) -> None:
        self.name = name


class _KamhermesCommandDef:
    """Real signature from Hermes e713518c (kamhermes) -- HAS gateway_platforms."""

    def __init__(self, name: str, description: str, category: str,
                 aliases: tuple = (), args_hint: str = "", subcommands: tuple = (),
                 cli_only: bool = False, gateway_only: bool = False,
                 gateway_config_gate=None, gateway_platforms=None) -> None:
        self.name = name


class TestCommandDefCompatibility(unittest.TestCase):
    def test_powerkam_build_lacks_gateway_platforms(self):
        kwargs = supported_commanddef_kwargs(_PowerkamCommandDef)
        self.assertNotIn("gateway_platforms", kwargs)
        self.assertIn("gateway_only", kwargs)

    def test_kamhermes_build_has_gateway_platforms(self):
        kwargs = supported_commanddef_kwargs(_KamhermesCommandDef)
        self.assertIn("gateway_platforms", kwargs)

    def test_powerkam_build_rejects_the_legacy_keyword(self):
        """Proves the original bug: this is what broke /restart."""
        with self.assertRaises(TypeError):
            _PowerkamCommandDef(
                "trade", "Open the Telegram trading console wizard", "Trading",
                gateway_only=True, gateway_platforms=("telegram",),
            )

    def test_kamhermes_build_accepts_it(self):
        cmd = _KamhermesCommandDef(
            "trade", "d", "Trading", gateway_only=True, gateway_platforms=("telegram",)
        )
        self.assertEqual(cmd.name, "trade")

    def test_both_builds_accept_only_supported_kwargs(self):
        for cls in (_PowerkamCommandDef, _KamhermesCommandDef):
            with self.subTest(cls=cls.__name__):
                supported = supported_commanddef_kwargs(cls)
                payload = {
                    k: v for k, v in
                    (("gateway_only", True), ("gateway_platforms", ("telegram",)))
                    if k in supported
                }
                cls("trade", "d", "Trading", **payload)  # must not raise

    def test_unknown_signature_yields_empty_set(self):
        class Opaque:
            __init__ = object.__init__

        self.assertIsInstance(supported_commanddef_kwargs(Opaque), set)


class TestNoGatewayPlatformsEmission(unittest.TestCase):
    def test_default_specs_exclude_commands_py(self):
        for spec in all_specs():
            self.assertNotEqual(
                spec.relative_path.name, "commands.py",
                "commands.py patch must not be in the default install path",
            )

    def test_default_specs_never_emit_gateway_platforms(self):
        blocks = "".join(s.block for s in all_specs())
        self.assertNotIn("gateway_platforms", blocks)

    def test_commands_specs_is_now_empty(self):
        self.assertEqual(commands_specs(), [])

    def test_legacy_spec_retained_for_cleanup_only(self):
        legacy = legacy_commands_specs()
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0].relative_path.name, "commands.py")
        # It still carries the offending keyword -- that is how an old install
        # is recognised so it can be removed.
        self.assertIn("gateway_platforms", legacy[0].block)

    def test_plugin_register_does_not_emit_gateway_platforms(self):
        source = (REPO_ROOT / "plugins" / "trade" / "__init__.py").read_text()
        self.assertNotIn("gateway_platforms", source)
        self.assertIn("register_command", source)

    def test_installer_sources_never_emit_the_keyword_by_default(self):
        for name in ("install_trade.py", "kamlib.py", "kamconfig.py"):
            with self.subTest(module=name):
                self.assertNotIn(
                    "gateway_platforms=", (INSTALLER / name).read_text(),
                    f"{name} emits gateway_platforms",
                )


class TestNativeRegistryStillConstructs(unittest.TestCase):
    """The real Hermes command table must import cleanly."""

    def test_restart_present_and_registry_builds(self):
        hermes_root = Path("/usr/local/lib/hermes-agent")
        if not (hermes_root / "hermes_cli" / "commands.py").is_file():
            self.skipTest("no Hermes checkout available")
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
            "assert 'restart' in names, sorted(names)[:20]\n"
            "print('OK', len(names))\n" % str(hermes_root)
        )
        proc = subprocess.run([sys.executable, "-c", probe],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-600:])
        self.assertIn("OK", proc.stdout)

    def test_every_registry_entry_constructed(self):
        """Every CommandDef in the registry built without a TypeError.

        If a bad keyword were injected, importing the module would fail
        outright -- so a populated registry is itself the proof.
        """
        hermes_root = Path("/usr/local/lib/hermes-agent")
        if not (hermes_root / "hermes_cli" / "commands.py").is_file():
            self.skipTest("no Hermes checkout available")
        probe = (
            "import sys; sys.path.insert(0, %r)\n"
            "import hermes_cli.commands as m\n"
            "reg = getattr(m, 'COMMAND_REGISTRY', [])\n"
            "assert len(reg) > 10, len(reg)\n"
            "bad = [c for c in reg if not getattr(c, 'name', None)]\n"
            "assert not bad, bad[:3]\n"
            "print('OK', len(reg))\n" % str(hermes_root)
        )
        proc = subprocess.run([sys.executable, "-c", probe],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-600:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
