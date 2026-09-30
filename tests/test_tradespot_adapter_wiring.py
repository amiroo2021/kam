"""Regression tests for tradespot adapter wiring upgrade scenario.

The test creates an exact byte-for-byte copy of the production Telegram adapter
and verifies that the three tradespot seams can be added and that a second
application is idempotent (SHA does not change).
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

LIVE_ADAPTER = Path("/usr/local/lib/hermes-agent/plugins/platforms/telegram/adapter.py")

# --- Minimal inline copy of the three tradespot blocks ---
_TRADESPOT_CALLBACK_BLOCK = '''\\
if data.startswith("tradespot:"):
    try:
        from plugins.trade.tradespot_wizard import handle_tradespot_callback

        await handle_tradespot_callback(self, query, data)
        return
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[%s] tradespot callback dispatch failed: %s",
            self.name, exc, exc_info=True,
        )
'''

_TRADESPOT_TEXT_BLOCK = '''\\
try:
    from plugins.trade.tradespot_wizard import handle_tradespot_text

    if await handle_tradespot_text(self, msg):
        return
except Exception as exc:  # noqa: BLE001
    logger.error(
        "[%s] /tradespot text dispatch failed: %s",
        self.name, exc, exc_info=True,
    )
'''

_TRADESPOT_COMMAND_BLOCK = '''\\
if cmd_body == "tradespot":
    try:
        from plugins.trade.tradespot_wizard import handle_tradespot_command

        handled = await handle_tradespot_command(self, msg)
        if handled:
            return
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[%s] /tradespot command dispatch failed: %s",
            self.name, exc, exc_info=True,
        )
'''


def _apply_tradespot_seams(adapter_path: Path) -> None:
    """Apply the three tradespot seams exactly once."""
    text = adapter_path.read_text(errors="replace")
    if "handle_tradespot_callback" not in text:
        lines = text.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if 'if data.startswith("trade:"):' in line:
                lines.insert(i, _TRADESPOT_CALLBACK_BLOCK + "\n")
                break
        text = "".join(lines)
    if "handle_tradespot_text" not in text:
        lines = text.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if 'if await handle_trade_text(self, msg):' in line:
                lines.insert(i, _TRADESPOT_TEXT_BLOCK + "\n")
                break
        text = "".join(lines)
    if "handle_tradespot_command" not in text:
        lines = text.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if 'if cmd_body == "trade":' in line:
                lines.insert(i, _TRADESPOT_COMMAND_BLOCK + "\n")
                break
        text = "".join(lines)
    adapter_path.write_text(text)


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

    def test_upgrade_from_trade_only_adds_tradespot_seams(self) -> None:
        _apply_tradespot_seams(self.fixture)
        text = self.fixture.read_text(errors="replace")
        for needle in [
            "handle_tradespot_callback",
            "handle_tradespot_text",
            "handle_tradespot_command",
        ]:
            self.assertIn(needle, text)

    def test_second_application_is_idempotent(self) -> None:
        _apply_tradespot_seams(self.fixture)
        after1 = hashlib.sha256(self.fixture.read_bytes()).hexdigest()

        _apply_tradespot_seams(self.fixture)
        after2 = hashlib.sha256(self.fixture.read_bytes()).hexdigest()

        self.assertEqual(after1, after2, "second apply changed the file")


if __name__ == "__main__":
    unittest.main()