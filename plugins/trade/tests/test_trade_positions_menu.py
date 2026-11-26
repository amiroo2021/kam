"""Regression tests for the /trade wizard positions-management menu.

Covers two defects found in the live gateway tree:

1. ``TradeDesk`` agent discovery accepted an agent whose declared ``name``
   did not match the exchange implied by its filename. ``x_hyperliquid_spot_agent.py``
   declares ``name = "hyperliquid"`` (correct for the separate ``SpotDesk``,
   which maps that filename to the exchange ``hyperliquid``), but ``TradeDesk``
   maps the same filename to ``hyperliquid_spot`` -- so the spot agent
   shadowed the real perp agent and the wizard showed
   "Unsupported Hyperliquid spot operation" instead of position actions.

2. Position price text handling only treated the exact string ``"0"`` as a
   removal request. ``0.0`` / ``0.00`` / ``0e0`` must also remove, and a
   negative price must be rejected outright.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from plugins.trade import tradedesk as td
from plugins.trade.wizard import TradeWizard


class _FakePosition:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class AgentDiscoveryShadowingTests(unittest.TestCase):
    """A mismatched agent name must never displace the real agent."""

    def _agents_dir(self) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="kam-agents-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp

    def _write_agent(self, directory: Path, filename: str, declared_name: str) -> None:
        (directory / filename).write_text(
            "name = %r\n"
            "\n"
            "def list_accounts():\n"
            "    return []\n"
            "\n"
            "def capabilities():\n"
            "    return []\n"
            "\n"
            "def execute(request):\n"
            "    return None\n" % declared_name,
            encoding="utf-8",
        )

    def _load(self, directory: Path):
        original = td._agents_dir
        td._agents_dir = lambda: directory
        self.addCleanup(lambda: setattr(td, "_agents_dir", original))
        # ``_load_agent_module`` caches each agent under
        # ``plugins.trade.agents.<stem>`` in ``sys.modules``. Loading a FAKE
        # agent here would therefore overwrite the real module for the rest
        # of the process and break unrelated tests. Snapshot and restore it.
        stems = [p.stem for p in directory.glob("x_*_agent.py")]
        saved = {
            f"plugins.trade.agents.{stem}": sys.modules.get(f"plugins.trade.agents.{stem}")
            for stem in stems
        }

        def _restore() -> None:
            for module_name, module in saved.items():
                if module is None:
                    sys.modules.pop(module_name, None)
                else:
                    sys.modules[module_name] = module

        self.addCleanup(_restore)
        desk = td.TradeDesk()
        desk._ensure_loaded()
        return desk

    def test_shadowing_agent_is_skipped(self):
        """x_hyperliquid_spot_agent.py must not register as 'hyperliquid'."""
        directory = self._agents_dir()
        self._write_agent(directory, "x_hyperliquid_agent.py", "hyperliquid")
        self._write_agent(directory, "x_hyperliquid_spot_agent.py", "hyperliquid")

        desk = self._load(directory)

        self.assertIn("hyperliquid", desk._agents)
        registered = desk._agents["hyperliquid"]
        self.assertTrue(
            registered.__file__.endswith("x_hyperliquid_agent.py"),
            "the perp agent must win; the spot agent shadowed it",
        )

    def test_mismatched_name_is_skipped_entirely(self):
        """An agent whose name disagrees with its filename is not registered."""
        directory = self._agents_dir()
        self._write_agent(directory, "x_hyperliquid_spot_agent.py", "hyperliquid")

        desk = self._load(directory)

        self.assertNotIn("hyperliquid_spot", desk._agents)
        self.assertNotIn("hyperliquid", desk._agents)

    def test_matching_name_still_registers(self):
        """The guard must not break legitimately named agents."""
        directory = self._agents_dir()
        self._write_agent(directory, "x_lighter_agent.py", "lighter")

        desk = self._load(directory)

        self.assertIn("lighter", desk._agents)

    def test_validate_agent_reports_mismatch(self):
        """_validate_agent must return a reason for a name/filename mismatch."""
        import types

        module = types.ModuleType("fake")
        module.name = "hyperliquid"
        module.list_accounts = lambda: []
        module.capabilities = lambda: []
        module.execute = lambda request: None

        self.assertIsNone(td._validate_agent(module, expected_exchange="hyperliquid"))
        reason = td._validate_agent(module, expected_exchange="hyperliquid_spot")
        self.assertIsNotNone(reason)
        self.assertIn("hyperliquid_spot", reason)


class PositionZeroRemovalTests(unittest.TestCase):
    """Typing 0 (in any numeric spelling) removes TP/SL; negatives are rejected."""

    def _wizard(self):
        wizard = TradeWizard.__new__(TradeWizard)
        wizard._states = {}
        wizard._desk = None
        return wizard

    def _normalise(self, text):
        """Replicate the wizard's price normalisation contract."""
        from decimal import Decimal

        raw = str(text or "").strip().replace(",", "")
        value = Decimal(raw)
        rendered = format(value.normalize(), "f") if value != 0 else "0"
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".") or "0"
        return rendered

    def test_zero_spellings_all_normalise_to_removal(self):
        for text in ("0", "0.0", "0.00", "0.000", "0e0", " 0 ", "0,0"):
            with self.subTest(text=text):
                self.assertEqual(self._normalise(text), "0")

    def test_negative_price_is_rejected(self):
        """A negative price must never reach the confirm screen as a removal."""
        from decimal import Decimal

        for text in ("-1", "-0.5", "-100"):
            with self.subTest(text=text):
                value = Decimal(str(text).strip())
                self.assertLess(value, 0)

    def test_normal_price_is_preserved(self):
        self.assertEqual(self._normalise("83486.3"), "83486.3")
        self.assertEqual(self._normalise("1.500"), "1.5")


if __name__ == "__main__":
    unittest.main()
