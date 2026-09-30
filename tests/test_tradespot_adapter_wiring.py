"""Regression tests for tradespot adapter wiring upgrade scenario."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from installer.adapter_wiring import apply_adapter_wiring

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_ADAPTER = Path("/usr/local/lib/hermes-agent/plugins/platforms/telegram/adapter.py")


class TestTradeshopAdapterUpgrade(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tradespot_adapter_")
        hermes_root = Path(self.tmp.name) / "hermes"
        (hermes_root / "plugins" / "platforms" / "telegram").mkdir(parents=True)
        self.fixture = hermes_root / "plugins" / "platforms" / "telegram" / "adapter.py"
        self.fixture.write_bytes(LIVE_ADAPTER.read_bytes())
        self.before_sha = hashlib.sha256(self.fixture.read_bytes()).hexdigest()
        self.hermes_root = hermes_root

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_upgrade_from_trade_only_adds_tradespot_seams_exactly_once(self) -> None:
        result = apply_adapter_wiring(
            hermes_root=self.hermes_root,
            hermes_home=Path("/root/.hermes"),
            capabilities=["trade"],
            dry_run=False,
        )
        self.assertTrue(result.get("ok"), result)

        text = self.fixture.read_text(errors="replace")
        for needle in [
            "handle_tradespot_command",
            "handle_tradespot_callback",
            "handle_tradespot_text",
            "tradespot:",
        ]:
            self.assertEqual(text.count(needle), 1, f"{needle} count != 1")

        for needle in [
            "handle_trade_command",
            "handle_trade_callback",
            "handle_trade_text",
            "trade:",
        ]:
            self.assertEqual(text.count(needle), 1, f"{needle} count changed")

        import py_compile
        py_compile.compile(str(self.fixture), doraise=True)

    def test_second_application_is_idempotent(self) -> None:
        apply_adapter_wiring(
            hermes_root=self.hermes_root,
            hermes_home=Path("/root/.hermes"),
            capabilities=["trade"],
            dry_run=False,
        )
        after1 = hashlib.sha256(self.fixture.read_bytes()).hexdigest()

        apply_adapter_wiring(
            hermes_root=self.hermes_root,
            hermes_home=Path("/root/.hermes"),
            capabilities=["trade"],
            dry_run=False,
        )
        after2 = hashlib.sha256(self.fixture.read_bytes()).hexdigest()

        self.assertEqual(after1, after2, "second apply changed the file")

        text = self.fixture.read_text(errors="replace")
        for needle in [
            "handle_tradespot_command",
            "handle_tradespot_callback",
            "handle_tradespot_text",
            "tradespot:",
        ]:
            self.assertEqual(text.count(needle), 1)


if __name__ == "__main__":
    unittest.main()