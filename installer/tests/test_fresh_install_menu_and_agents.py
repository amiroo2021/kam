"""Fresh-install regressions: BotCommand menu + full exchange agent payload.

Complements adapter-wiring tests with:

1. Telegram command-menu publication config (plugins.enabled + max_commands)
2. Complete x_*_agent.py payload under hermes_root after --trade
"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "installer"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import adapter_wiring as AW  # noqa: E402
import kamconfig as KC  # noqa: E402
from installer_shared import SHARED_REL_PATHS  # noqa: E402
from patchspecs import TELEGRAM_ADAPTER  # noqa: E402
from pristine_telegram_adapter import PRISTINE_TELEGRAM_ADAPTER  # noqa: E402

INST = importlib.import_module("installer.installer")

EXPECTED_AGENTS = sorted(
    rel.name
    for rel in SHARED_REL_PATHS
    if rel.name.startswith("x_") and rel.name.endswith("_agent.py")
)


def _fresh() -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="kam-menu-agents-"))
    hermes_home = tmp / "hermes_home"
    hermes_root = tmp / "hermes_root"
    hermes_home.mkdir(parents=True)
    hermes_root.mkdir(parents=True)
    adapter = hermes_root / TELEGRAM_ADAPTER
    adapter.parent.mkdir(parents=True, exist_ok=True)
    adapter.write_text(PRISTINE_TELEGRAM_ADAPTER, encoding="utf-8")
    # Simulate default Hermes menu capacity (60) + empty plugins.
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled: []\n"
        "platforms:\n  telegram:\n    extra:\n      command_menu:\n"
        "        max_commands: 60\n",
        encoding="utf-8",
    )
    return {"tmp": tmp, "hermes_home": hermes_home, "hermes_root": hermes_root}


def _install(caps: List[str], env: dict) -> int:
    argv = [f"--{c}" for c in caps]
    systemd_dir = env["hermes_root"].parent / "systemd"
    systemd_dir.mkdir(parents=True, exist_ok=True)
    argv += [
        "--hermes-root",
        str(env["hermes_root"]),
        "--hermes-home",
        str(env["hermes_home"]),
        "--systemd-dir",
        str(systemd_dir),
        "--skip-deps",
        "--no-restart",
        "--action",
        "install",
    ]
    return INST.main(argv)


def _verify(caps: List[str], env: dict) -> int:
    argv = [f"--{c}" for c in caps]
    systemd_dir = env["hermes_root"].parent / "systemd"
    argv += [
        "--hermes-root",
        str(env["hermes_root"]),
        "--hermes-home",
        str(env["hermes_home"]),
        "--systemd-dir",
        str(systemd_dir),
        "--action",
        "verify",
    ]
    return INST.main(argv)


class CommandMenuPublicationTests(unittest.TestCase):
    def tearDown(self) -> None:
        tmp = getattr(self, "_tmp", None)
        if tmp and Path(tmp).is_dir():
            shutil.rmtree(tmp, ignore_errors=True)

    def test_install_raises_menu_capacity_and_enables_plugin(self):
        env = _fresh()
        self._tmp = env["tmp"]
        before = KC.parse_config(env["hermes_home"] / "config.yaml")
        self.assertEqual(KC.get_telegram_menu_max_commands(before), 60)
        self.assertFalse(KC.is_trade_enabled(before))

        self.assertEqual(_install(["trade"], env), 0)

        after = KC.parse_config(env["hermes_home"] / "config.yaml")
        self.assertTrue(KC.is_trade_enabled(after))
        self.assertGreaterEqual(
            KC.get_telegram_menu_max_commands(after), KC.MINIMUM_TELEGRAM_MENU_MAX
        )
        ok, msgs = AW.verify_command_menu_publication(
            hermes_home=env["hermes_home"], capabilities=["trade"]
        )
        self.assertTrue(ok, msgs)
        self.assertEqual(_verify(["trade"], env), 0)

    def test_verify_fails_when_menu_capacity_too_low(self):
        env = _fresh()
        self._tmp = env["tmp"]
        self.assertEqual(_install(["trade"], env), 0)
        # Downgrade capacity after install (simulates lost menu fix).
        cfg_path = env["hermes_home"] / "config.yaml"
        data = KC.parse_config(cfg_path)
        data["platforms"]["telegram"]["extra"]["command_menu"]["max_commands"] = 60
        import yaml

        cfg_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        ok, msgs = AW.verify_command_menu_publication(
            hermes_home=env["hermes_home"], capabilities=["trade"]
        )
        self.assertFalse(ok)
        self.assertTrue(any("max_commands=60" in m for m in msgs), msgs)
        self.assertNotEqual(_verify(["trade"], env), 0)


class FullAgentPayloadTests(unittest.TestCase):
    def tearDown(self) -> None:
        tmp = getattr(self, "_tmp", None)
        if tmp and Path(tmp).is_dir():
            shutil.rmtree(tmp, ignore_errors=True)

    def test_expected_agent_set_is_complete(self):
        # Contract: main ships these 14 agents via SHARED_REL_PATHS.
        self.assertEqual(
            EXPECTED_AGENTS,
            [
                "x_apex_agent.py",
                "x_arcus_agent.py",
                "x_edgex_agent.py",
                "x_hibachi_agent.py",
                "x_hyperliquid_agent.py",
                "x_lighter_agent.py",
                "x_mexc_agent.py",
                "x_nado_agent.py",
                "x_ondoperps_agent.py",
                "x_pacifica_agent.py",
                "x_perpl_agent.py",
                "x_phemex_agent.py",
                "x_raydium_agent.py",
                "x_rise_agent.py"
            ],
        )
        for name in EXPECTED_AGENTS:
            self.assertTrue(
                (REPO_ROOT / "plugins" / "trade" / "agents" / name).is_file(),
                f"repo missing {name}",
            )

    def test_install_trade_copies_all_agents(self):
        env = _fresh()
        self._tmp = env["tmp"]
        self.assertEqual(_install(["trade"], env), 0)
        agents_dir = env["hermes_root"] / "plugins" / "trade" / "agents"
        installed = sorted(p.name for p in agents_dir.glob("x_*_agent.py"))
        self.assertEqual(installed, EXPECTED_AGENTS)
        self.assertEqual(_verify(["trade"], env), 0)

    def test_verify_fails_when_agent_file_missing(self):
        env = _fresh()
        self._tmp = env["tmp"]
        self.assertEqual(_install(["trade"], env), 0)
        victim = env["hermes_root"] / "plugins" / "trade" / "agents" / "x_edgex_agent.py"
        victim.unlink()
        self.assertNotEqual(_verify(["trade"], env), 0)


class PluginYamlDiscoveryTests(unittest.TestCase):
    """Lodo: payload without plugin.yaml → discover_plugins never loads trade."""

    def tearDown(self) -> None:
        tmp = getattr(self, "_tmp", None)
        if tmp and Path(tmp).is_dir():
            shutil.rmtree(tmp, ignore_errors=True)

    def test_install_copies_plugin_yaml(self):
        env = _fresh()
        self._tmp = env["tmp"]
        self.assertEqual(_install(["trade"], env), 0)
        yaml_path = env["hermes_root"] / "plugins" / "trade" / "plugin.yaml"
        self.assertTrue(yaml_path.is_file(), "plugin.yaml must be installed for discovery")
        text = yaml_path.read_text(encoding="utf-8")
        self.assertIn("name:", text)
        self.assertIn("trade", text)
        self.assertEqual(_verify(["trade"], env), 0)

    def test_verify_fails_without_plugin_yaml(self):
        env = _fresh()
        self._tmp = env["tmp"]
        self.assertEqual(_install(["trade"], env), 0)
        yaml_path = env["hermes_root"] / "plugins" / "trade" / "plugin.yaml"
        self.assertTrue(yaml_path.is_file())
        yaml_path.unlink()
        self.assertNotEqual(_verify(["trade"], env), 0)


class RetiredFiboCapabilityCleanupTests(unittest.TestCase):
    """The retired /fibo capability must not strand a deployed install.

    A deployment installed by an older KAM version can carry a stale
    ``capabilities.fibo = true`` manifest entry and marked /fibo seams in
    its Telegram adapter. Those seams import ``plugins.trade.fibo_wizard``,
    which no longer exists.
    """

    def tearDown(self) -> None:
        tmp = getattr(self, "_tmp", None)
        if tmp and Path(tmp).is_dir():
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stale_fibo_manifest_entry_does_not_block_shared_teardown(self):
        env = _fresh()
        self._tmp = env["tmp"]
        self.assertEqual(_install(["trade"], env), 0)

        # Simulate a manifest written by an older installer that still
        # advertises the retired fibo capability.
        manifest_path = env["hermes_home"] / "kam" / "install_state.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.setdefault("capabilities", {})["fibo"] = True
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        argv = [
            "--trade",
            "--hermes-root",
            str(env["hermes_root"]),
            "--hermes-home",
            str(env["hermes_home"]),
            "--action",
            "uninstall",
        ]
        self.assertEqual(INST.main(argv), 0)

        shared = env["hermes_root"] / "plugins" / "trade" / "tradedesk.py"
        self.assertFalse(
            shared.exists(),
            "shared teardown must not be blocked by a stale retired fibo entry",
        )

    def test_retired_fibo_manifest_entry_is_cleared_on_uninstall(self):
        """A stale ``fibo: true`` must be cleared, not just ignored.

        Leaving it marked installed makes ``installed_capabilities`` report a
        capability that no longer exists.
        """
        env = _fresh()
        self._tmp = env["tmp"]
        self.assertEqual(_install(["trade"], env), 0)

        manifest_path = env["hermes_home"] / "kam" / "install_state.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.setdefault("capabilities", {})["fibo"] = True
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        argv = [
            "--trade",
            "--hermes-root",
            str(env["hermes_root"]),
            "--hermes-home",
            str(env["hermes_home"]),
            "--action",
            "uninstall",
        ]
        self.assertEqual(INST.main(argv), 0)

        # The shared teardown removes the whole KAM state dir (including the
        # manifest). When the manifest survives, the retired entry must not
        # still read as installed.
        if manifest_path.exists():
            after = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertFalse(
                after.get("capabilities", {}).get("fibo", False),
                "the retired fibo capability must not stay marked installed",
            )
        else:
            self.assertFalse(
                (env["hermes_home"] / "kam").exists(),
                "the retired fibo capability must not stay marked installed",
            )

    def test_retired_capability_is_dropped_from_telegram_menu_priority(self):
        """A stale retired command must not stay pinned in the Telegram menu.

        Hermes only reorders the priority list by name, so a config written
        by an older KAM keeps the retired entry on every upgrade unless the
        installer strips it.
        """
        import kamconfig as KC

        tmp = tempfile.mkdtemp(prefix="kam-menu-priority-")
        self._tmp = tmp
        config = Path(tmp) / "config.yaml"
        config.write_text(
            "platforms:\n"
            "  telegram:\n"
            "    extra:\n"
            "      command_menu:\n"
            "        max_commands: 100\n"
            "        priority:\n"
            "          - trade\n"
            "          - fibo\n"
            "        priority_mode: prepend\n",
            encoding="utf-8",
        )

        KC.ensure_telegram_menu_capacity(config)

        import yaml

        after = yaml.safe_load(config.read_text(encoding="utf-8"))
        priority = (
            after["platforms"]["telegram"]["extra"]["command_menu"]["priority"]
        )
        self.assertIn("trade", priority)
        self.assertNotIn(
            "fibo",
            priority,
            "the retired capability must not stay pinned in the menu priority",
        )

    def test_upgrade_prunes_retired_payload_from_deployed_tree(self):
        """An upgrade install must remove retired payload from the tree.

        The copy step never deletes, so a deployment installed by an older
        KAM keeps x_metatrader_agent.py.  TradeDesk discovers agents at
        runtime, so a stale copy keeps exposing the retired venue.
        """
        env = _fresh()
        self._tmp = env["tmp"]
        plugin_root = env["hermes_root"] / "plugins" / "trade"

        stale_agent = plugin_root / "agents" / "x_metatrader_agent.py"
        stale_agent.parent.mkdir(parents=True, exist_ok=True)
        stale_agent.write_text("# stale agent from an older KAM\n", encoding="utf-8")

        stale_wizard = plugin_root / "fibo_wizard.py"
        stale_wizard.write_text("# stale wizard\n", encoding="utf-8")

        stale_pkg = plugin_root / "fibo"
        stale_pkg.mkdir(parents=True, exist_ok=True)
        (stale_pkg / "mt4_reader.py").write_text("# stale reader\n", encoding="utf-8")

        stale_mt5_agent = plugin_root / "agents" / "x_mt5_agent.py"
        stale_mt5_agent.write_text("# stale MT5 agent\n", encoding="utf-8")

        stale_bridge = plugin_root / "mt5_bridge"
        stale_bridge.mkdir(parents=True, exist_ok=True)
        (stale_bridge / "HermesBridge.mq5").write_text("// stale bridge\n", encoding="utf-8")

        self.assertEqual(_install(["trade"], env), 0)

        self.assertFalse(
            stale_agent.exists(),
            "the retired MetaTrader agent must be pruned on upgrade",
        )
        self.assertFalse(
            stale_wizard.exists(),
            "the retired /fibo wizard must be pruned on upgrade",
        )
        self.assertFalse(
            stale_pkg.exists(),
            "the retired /fibo package must be pruned on upgrade",
        )
        self.assertFalse(
            stale_mt5_agent.exists(),
            "the retired MT5 agent must be pruned on upgrade",
        )
        self.assertFalse(
            stale_bridge.exists(),
            "the retired MT5 bridge payload must be pruned on upgrade",
        )

    def test_retired_fibo_seams_are_stripped_on_upgrade_install(self):
        """An upgrade install must strip orphaned /fibo seams too.

        The uninstall path removes them, but a deployment that is *upgraded*
        (not uninstalled) keeps them unless the install path strips them as
        well.
        """
        env = _fresh()
        self._tmp = env["tmp"]
        adapter = env["hermes_root"] / TELEGRAM_ADAPTER
        text = adapter.read_text(encoding="utf-8")
        orphan = (
            "        # BEGIN KAM TRADE PLUGIN (fibo callback dispatch)\n"
            "        try:\n"
            "            from plugins.trade.fibo_wizard import handle_fibo_callback\n"
            "\n"
            "            await handle_fibo_callback(self, query, data)\n"
            "        except Exception:\n"
            "            pass\n"
            "        # END KAM TRADE PLUGIN (fibo callback dispatch)\n"
            "\n"
        )
        self.assertIn('data = getattr(query, "data", "") or ""', text)
        text = text.replace(
            '        data = getattr(query, "data", "") or ""',
            orphan + '        data = getattr(query, "data", "") or ""',
            1,
        )
        adapter.write_text(text, encoding="utf-8")

        self.assertEqual(_install(["trade"], env), 0)

        after = adapter.read_text(encoding="utf-8")
        self.assertNotIn(
            "fibo callback dispatch",
            after,
            "the retired /fibo seam must be stripped by the upgrade path",
        )
        self.assertNotIn(
            "plugins.trade.fibo_wizard",
            after,
            "the upgrade must not leave an import of the removed module",
        )

    def test_retired_fibo_seams_are_stripped_from_deployed_adapter(self):
        env = _fresh()
        self._tmp = env["tmp"]
        self.assertEqual(_install(["trade"], env), 0)

        adapter = env["hermes_root"] / TELEGRAM_ADAPTER
        text = adapter.read_text(encoding="utf-8")
        # Inject a marked /fibo seam exactly as an older KAM version would.
        orphan = (
            "        # BEGIN KAM TRADE PLUGIN (fibo callback dispatch)\n"
            "        if data.startswith(\"fibo:\"):\n"
            "            try:\n"
            "                from plugins.trade.fibo_wizard import handle_fibo_callback\n"
            "\n"
            "                await handle_fibo_callback(self, query, data)\n"
            "                return\n"
            "            except Exception as exc:  # noqa: BLE001\n"
            "                logger.error(\n"
            "                    \"[%s] /fibo callback dispatch failed: %s\",\n"
            "                    self.name, exc, exc_info=True,\n"
            "                )\n"
            "                try:\n"
            "                    await query.answer()\n"
            "                except Exception:\n"
            "                    pass\n"
            "                return\n"
            "        # END KAM TRADE PLUGIN (fibo callback dispatch)\n"
        )
        anchor = 'query_user_name = getattr(query.from_user, "first_name", None)'
        self.assertIn(anchor, text)
        text = text.replace(anchor, anchor + "\n" + orphan, 1)
        adapter.write_text(text, encoding="utf-8")
        self.assertIn("plugins.trade.fibo_wizard", adapter.read_text(encoding="utf-8"))

        record = AW.remove_adapter_wiring(
            hermes_root=env["hermes_root"],
            capabilities=["trade"],
            remaining_capabilities=[],
            dry_run=False,
        )
        self.assertTrue(record["ok"], record)
        after = adapter.read_text(encoding="utf-8")
        self.assertNotIn(
            "plugins.trade.fibo_wizard",
            after,
            "orphaned /fibo seam must be stripped from a deployed adapter",
        )


if __name__ == "__main__":
    unittest.main()
