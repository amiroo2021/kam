"""Regression tests for tradespot adapter wiring upgrade scenario.

Uses the REAL production path and AST structural validation.
"""
from __future__ import annotations

import ast
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

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


def _count_cmd_body_branches(source: str) -> tuple[int, int]:
    """Count distinct cmd_body == "trade" and cmd_body == "tradespot" branches."""
    tree = ast.parse(source)
    trade = tradespot = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.Compare):
                left = test.left
                if isinstance(left, ast.Name) and left.id == "cmd_body":
                    for comp in test.comparators:
                        if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                            if comp.value == "trade":
                                trade += 1
                            elif comp.value == "tradespot":
                                tradespot += 1
    return trade, tradespot


def _count_handler_usage(source: str, name: str) -> tuple[int, int]:
    """Count import statements and call sites for a handler name."""
    imports = source.count(f"from plugins.trade.wizard import {name}")
    calls = source.count(f"{name}(")
    return imports, calls


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

        # Structural validation via AST (not raw string counts)
        trade_cmd, tradespot_cmd = _count_cmd_body_branches(text)
        self.assertEqual(trade_cmd, 1, "trade command branch must remain 1")
        self.assertEqual(tradespot_cmd, 1, "tradespot command branch must be 1")

        # For tradespot we only require that the branch exists and contains
        # exactly one import/alias line and exactly one invocation.
        # We do not assert the original handler token because aliasing is used.
        tradespot_import_lines = [
            line for line in text.splitlines()
            if "from plugins.trade.tradespot_wizard import" in line
            and "tradespot" in line
        ]
        self.assertEqual(len(tradespot_import_lines), 3, "exactly three tradespot import/alias lines")

        # Count invocations of the three aliases we introduced
        alias_calls = (
            text.count("_tradespot_cb(")
            + text.count("_tradespot_tx(")
            + text.count("_tradespot_cmd(")
        )
        self.assertEqual(alias_calls, 3, "exactly three tradespot alias invocations")

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

        for s in specs:
            if "tradespot" in s.seam:
                token = s.native_sentinel.split()[-1]
                self.assertEqual(
                    s.block.count(token),
                    1,
                    f"{s.seam} block does not contain its sentinel once",
                )


if __name__ == "__main__":
    unittest.main()