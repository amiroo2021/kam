"""Tests for inline-keyboard idempotency regression."""
from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "installer"
sys.path.insert(0, str(INSTALLER))

import kamlib as K  # noqa: E402

PY = sys.executable


# Reuse the existing clean Hermes fixture from test_installation.py so the
# adapter satisfies the installer's anchor requirements.
from test_installation import make_clean_hermes as _make_clean_hermes


def _run_installer(hermes_root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(INSTALLER / "install_trade.py"),
         "--hermes-root", str(hermes_root), "--skip-deps", "--no-restart", *extra],
        capture_output=True, text=True,
    )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix="kam-ik-itest-")
        self.hermes = _make_clean_hermes(Path(self.tmp.name) / "hermes")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @property
    def adapter(self) -> Path:
        return self.hermes / "plugins" / "platforms" / "telegram" / "adapter.py"


def _insert_after(text: str, anchor: str, snippet: str) -> str:
    """Insert snippet after anchor line; raise if anchor missing."""
    assert anchor in text, f"anchor not found: {anchor!r}"
    return text.replace(anchor, anchor + "\n" + snippet)


def _replace_with(text: str, anchor: str, replacement: str) -> str:
    assert anchor in text, f"anchor not found: {anchor!r}"
    return text.replace(anchor, replacement)


class TestInlineKeyboardIdempotencyClean(_Base):
    def test_clean_adapter_gets_helper_inserted_at_class_scope(self):
        text_before = self.adapter.read_text()
        self.assertNotIn("send_inline_keyboard", text_before)
        proc = _run_installer(self.hermes)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        text_after = self.adapter.read_text()
        self.assertIn("BEGIN KAM TRADE PLUGIN (inline keyboard helper)", text_after)
        # Direct class scope, not nested.
        self.assertIn("\n    async def send_inline_keyboard(", text_after)
        self.assertNotIn("\n        async def send_inline_keyboard(", text_after)


COMPATIBLE_DIRECT_HELPER = (
    "    async def send_inline_keyboard(\n"
    "        self,\n"
    "        chat_id,\n"
    "        text,\n"
    "        buttons,\n"
    "        callback_prefix,\n"
    "        *,\n"
    "        metadata=None,\n"
    "        parse_mode=None,\n"
    "    ):\n"
    "        rows = []\n"
    "        for row in buttons:\n"
    "            rb = []\n"
    "            for btn in row:\n"
    "                rb.append((btn[\"text\"], f\"{callback_prefix}:{btn[\'callback_data\']}\"))\n"
    "            if rb:\n"
    "                rows.append(rb)\n"
    "        markup = InlineKeyboardMarkup(rows)\n"
    "        kwargs = {\"chat_id\": chat_id, \"text\": text, \"reply_markup\": markup}\n"
    "        return kwargs"
)

COMPATIBLE_DIRECT_HELPER_LINES_5771 = (
    "    async def send_inline_keyboard(\n"
    "        self,\n"
    "        chat_id,\n"
    "        text,\n"
    "        buttons,\n"
    "        callback_prefix,\n"
    "        *,\n"
    "        metadata=None,\n"
    "        parse_mode=None,\n"
    "    ):\n"
    "        rows = []\n"
    "        for row in buttons:\n"
    "            rb = []\n"
    "            for btn in row:\n"
    "                rb.append((btn[\"text\"], f\"{callback_prefix}:{btn[\'callback_data\']}\"))\n"
    "            if rb:\n"
    "                rows.append(rb)\n"
    "        markup = InlineKeyboardMarkup(rows)\n"
    "        kwargs = {\"chat_id\": chat_id, \"text\": text, \"reply_markup\": markup}\n"
    "        _MODEL_PAGE_SIZE = 8\n"
    "        return kwargs"
)

NESTED_HELPER = (
    "        async def send_inline_keyboard(\n"
    "            self,\n"
    "            chat_id,\n"
    "            text,\n"
    "            buttons,\n"
    "            callback_prefix,\n"
    "            *,\n"
    "            metadata=None,\n"
    "            parse_mode=None,\n"
    "        ):\n"
    "            return None"
)


class TestInlineKeyboardIdempotencyExisting(_Base):
    """Existing direct helper scenarios."""

    def test_compatible_direct_helper_recognised_as_native_present(self):
        text = self.adapter.read_text()
        # Insert a direct helper after _should_process_message.
        anchor = "    def _should_process_message(self, msg, is_command=False):\n        return True\n"
        text = _replace_with(text, anchor, anchor + "\n" + COMPATIBLE_DIRECT_HELPER + "\n")
        self.adapter.write_text(text)
        sha_before = K.sha256_file(self.adapter)

        proc = _run_installer(self.hermes)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("BEGIN KAM TRADE PLUGIN (inline keyboard helper)", self.adapter.read_text())
        self.assertIn("native-present", proc.stdout)
        self.assertIn("structurally compatible", proc.stdout)
        # The inline-keyboard seam must be the only one left alone; the other
        # three integration patches legitimately apply to CLEAN_ADAPTER.
        self.assertIn("[inline keyboard helper]", proc.stdout)
        self.assertIn("[callback dispatch]", proc.stdout)
        self.assertIn("[wizard text interception]", proc.stdout)
        self.assertIn("[slash command dispatch]", proc.stdout)

    def test_model_page_size_inside_existing_helper_is_not_re_patched(self):
        """_MODEL_PAGE_SIZE = 8 inside the helper body must not trigger nested insertion."""
        text = self.adapter.read_text()
        anchor = "    def _should_process_message(self, msg, is_command=False):\n        return True\n"
        text = _replace_with(text, anchor, anchor + "\n" + COMPATIBLE_DIRECT_HELPER_LINES_5771 + "\n")
        self.adapter.write_text(text)
        sha_before = K.sha256_file(self.adapter)

        proc = _run_installer(self.hermes)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("native-present", proc.stdout)
        self.assertIn("structurally compatible", proc.stdout)
        # No KAM helper marker means the installer left the existing method alone.
        self.assertNotIn("BEGIN KAM TRADE PLUGIN (inline keyboard helper)", self.adapter.read_text())
        # No nested insertion: helper must still be at class indentation.
        self.assertIn("\n    async def send_inline_keyboard(", self.adapter.read_text())

    def test_nested_helper_is_rejected_with_clear_diagnostic(self):
        """Helper nested inside another method -> explicit failure, file unchanged."""
        text = self.adapter.read_text()
        anchor = "    def _should_process_message(self, msg, is_command=False):\n        return True\n"
        text = _replace_with(text, anchor, anchor + "\n" + NESTED_HELPER + "\n")
        self.adapter.write_text(text)
        sha_before = K.sha256_file(self.adapter)

        proc = _run_installer(self.hermes)
        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertIn("nested inside another method", combined)
        self.assertEqual(K.sha256_file(self.adapter), sha_before)

    def test_two_direct_helper_definitions_are_rejected(self):
        """Two direct helpers -> explicit ambiguous/duplicate failure."""
        text = self.adapter.read_text()
        anchor = "    def _should_process_message(self, msg, is_command=False):\n        return True\n"
        helper_a = "    async def send_inline_keyboard(self, *a, **kw):\n        return None"
        helper_b = "    async def send_inline_keyboard(self, *a, **kw):\n        return None  # second"
        text = _replace_with(text, anchor, anchor + "\n" + helper_a + "\n\n" + helper_b + "\n")
        self.adapter.write_text(text)
        sha_before = K.sha256_file(self.adapter)

        proc = _run_installer(self.hermes)
        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertIn("multiple direct", combined)
        # No KAM marker means no insertion happened; abort left file alone for this seam.
        self.assertNotIn("BEGIN KAM TRADE PLUGIN (inline keyboard helper)", self.adapter.read_text())

    def test_existing_compatible_helper_plus_unrelated_diff_preserved(self):
        """Compatible helper + unrelated adapter diffs must survive install."""
        text = self.adapter.read_text()
        # Insert an unrelated upstream comment.
        text = _insert_after(text, "    name = \"telegram\"", "# unrelated upstream change\n")
        # Add compatible helper.
        anchor = "    def _should_process_message(self, msg, is_command=False):\n        return True\n"
        text = _replace_with(text, anchor, anchor + "\n" + COMPATIBLE_DIRECT_HELPER + "\n")
        self.adapter.write_text(text)
        sha_before = K.sha256_file(self.adapter)

        proc = _run_installer(self.hermes)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("native-present", proc.stdout)
        self.assertIn("structurally compatible", proc.stdout)
        self.assertIn("# unrelated upstream change", self.adapter.read_text())
        # No KAM helper marker means the installer left the existing helper alone.
        self.assertNotIn("BEGIN KAM TRADE PLUGIN (inline keyboard helper)", self.adapter.read_text())

    def test_installer_run_twice_is_idempotent(self):
        """First install inserts; second install is a no-op on this seam."""
        proc = _run_installer(self.hermes)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        text_after_first = self.adapter.read_text()
        self.assertIn("BEGIN KAM TRADE PLUGIN (inline keyboard helper)", text_after_first)

        proc2 = _run_installer(self.hermes)
        self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
        text_after_second = self.adapter.read_text()
        self.assertEqual(text_after_first, text_after_second)
        self.assertIn("already-installed", proc2.stdout)