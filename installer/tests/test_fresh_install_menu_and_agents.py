"""Fresh-install regressions: BotCommand menu + full exchange agent payload.

Complements adapter-wiring tests with:

1. Telegram command-menu publication config (plugins.enabled + max_commands)
2. Complete x_*_agent.py payload under hermes_root after --trade/--fibo
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

        self.assertEqual(_install(["trade", "fibo"], env), 0)

        after = KC.parse_config(env["hermes_home"] / "config.yaml")
        self.assertTrue(KC.is_trade_enabled(after))
        self.assertGreaterEqual(
            KC.get_telegram_menu_max_commands(after), KC.MINIMUM_TELEGRAM_MENU_MAX
        )
        ok, msgs = AW.verify_command_menu_publication(
            hermes_home=env["hermes_home"], capabilities=["trade", "fibo"]
        )
        self.assertTrue(ok, msgs)
        self.assertEqual(_verify(["trade", "fibo"], env), 0)

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
        # Contract: main ships these 10 agents via SHARED_REL_PATHS.
        self.assertEqual(
            EXPECTED_AGENTS,
            [
                "x_apex_agent.py",
                "x_arcus_agent.py",
                "x_edgex_agent.py",
                "x_hibachi_agent.py",
                "x_hyperliquid_agent.py",
                "x_lighter_agent.py",
                "x_ondoperps_agent.py",
                "x_pacifica_agent.py",
                "x_raydium_agent.py",
                "x_rise_agent.py",
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

    def test_install_fibo_also_installs_shared_agents(self):
        env = _fresh()
        self._tmp = env["tmp"]
        self.assertEqual(_install(["fibo"], env), 0)
        agents_dir = env["hermes_root"] / "plugins" / "trade" / "agents"
        installed = sorted(p.name for p in agents_dir.glob("x_*_agent.py"))
        self.assertEqual(installed, EXPECTED_AGENTS)

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
        self.assertEqual(_install(["trade", "fibo"], env), 0)
        yaml_path = env["hermes_root"] / "plugins" / "trade" / "plugin.yaml"
        self.assertTrue(yaml_path.is_file(), "plugin.yaml must be installed for discovery")
        text = yaml_path.read_text(encoding="utf-8")
        self.assertIn("name:", text)
        self.assertIn("trade", text)
        self.assertEqual(_verify(["trade", "fibo"], env), 0)

    def test_verify_fails_without_plugin_yaml(self):
        env = _fresh()
        self._tmp = env["tmp"]
        self.assertEqual(_install(["trade"], env), 0)
        yaml_path = env["hermes_root"] / "plugins" / "trade" / "plugin.yaml"
        self.assertTrue(yaml_path.is_file())
        yaml_path.unlink()
        self.assertNotEqual(_verify(["trade"], env), 0)


if __name__ == "__main__":
    unittest.main()
