"""Regression tests for tradespot adapter wiring upgrade scenario.

Uses the REAL production path:
apply_adapter_wiring -> specs_for_capabilities -> trade_adapter_specs
No inline/manual insertion is permitted.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

# Make installer modules importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "installer"))

from adapter_wiring import apply_adapter_wiring
from patchspecs import trade_adapter_specs

LIVE_ADAPTER = Path("/usr/local/lib/hermes-agent/plugins/platforms/telegram/adapter.py")
EXPECTED_BEFORE_SHA = "3e3876945d6fc2e4dd4192a7344e4de86a052953ca4803577a63901d2f392f5a"


def _build_hermes_tree(tmpdir: Path) -> Path:
    """Create the exact directory structure expected by apply_adapter_wiring."""
    hermes_root = tmpdir / "hermes"
    (hermes_root / "plugins" / "platforms" / "telegram").mkdir(parents=True)
    (hermes_root / "config.yaml").write_text("plugins:\n  enabled:\n    - trade\n")
    return hermes_root


class TestTradeshopAdapterUpgrade(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tradespot_adapter_")
        self.hermes_root = _build_hermes_tree(Path(self.tmp.name))
        self.fixture = (
            self.hermes_root / "plugins" / "platforms" / "telegram" / "adapter.py"
        )
        self.fixture.write_bytes(LIVE_ADAPTER.read_bytes())
        self.before_sha = hashlib.sha256(self.fixture.read_bytes()).hexdigest()
        if self.before_sha != EXPECTED_BEFORE_SHA:
            self.fail(f"Fixture SHA mismatch: {self.before_sha}")

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

        # Existing /trade seams must remain exactly 1
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


class TestTradeAdapterSpecsUniqueness(unittest.TestCase):
    def test_trade_adapter_specs_returns_no_duplicates(self) -> None:
        specs = trade_adapter_specs()
        names = [s.seam for s in specs]
        sentinels = [s.native_sentinel for s in specs]
        blocks = [s.block for s in specs]

        self.assertEqual(len(names), len(set(names)), "Duplicate PatchSpec names")
        self.assertEqual(
            len(sentinels), len(set(sentinels)), "Duplicate tradespot sentinels"
        )
        self.assertEqual(
            len(blocks), len(set(blocks)), "Duplicate tradespot insertion blocks"
        )

        tradespot_names = [n for n in names if "tradespot" in n]
        self.assertEqual(len(tradespot_names), 3)


if __name__ == "__main__":
    unittest.main()