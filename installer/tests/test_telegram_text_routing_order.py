"""Regression coverage for capability-aware Telegram text interception order."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "installer"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adapter_wiring import apply_adapter_wiring  # noqa: E402
from patchspecs import TELEGRAM_ADAPTER  # noqa: E402
from pristine_telegram_adapter import PRISTINE_TELEGRAM_ADAPTER  # noqa: E402


class TelegramTextRoutingOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kam-text-routing-"))
        self.hermes_root = self.tmp / "hermes_root"
        self.hermes_home = self.tmp / "hermes_home"
        adapter = self.hermes_root / TELEGRAM_ADAPTER
        adapter.parent.mkdir(parents=True, exist_ok=True)
        adapter.write_text(PRISTINE_TELEGRAM_ADAPTER, encoding="utf-8")
        self.hermes_home.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_trade_text_precedes_backtest_initialization(self) -> None:
        result = apply_adapter_wiring(
            hermes_root=self.hermes_root,
            hermes_home=self.hermes_home,
            capabilities=["trade"],
        )
        self.assertTrue(result["ok"], result)
        adapter = (self.hermes_root / TELEGRAM_ADAPTER).read_text(encoding="utf-8")
        tradespot = adapter.index("handle_tradespot_text")
        trade = adapter.index("handle_trade_text")
        backtest = adapter.index("handle_backtest_text")
        self.assertLess(tradespot, trade)
        self.assertLess(trade, backtest)
        self.assertLess(backtest, adapter.index("await self.handle_message(event)"))


if __name__ == "__main__":
    unittest.main()
