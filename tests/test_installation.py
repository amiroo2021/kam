"""Installation tests for the KAM /trade add-on.

These build a synthetic *clean* Hermes tree — one whose adapter has the
approved anchors but no /trade wiring — so the real patch path is exercised.
The production node cannot test this, because its seams are already native.

Everything here is offline. No exchange is contacted, no order is placed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "installer"
sys.path.insert(0, str(INSTALLER))

import kamlib as K  # noqa: E402
from patchspecs import adapter_specs, all_specs  # noqa: E402

PY = sys.executable


def _interpreter_with_agent_deps() -> str:
    """Return an interpreter that can import the agents' third-party deps.

    The fixture's ``venv/bin/python`` must point at a real environment,
    otherwise every agent fails to import for reasons unrelated to the
    installer and the tests measure nothing. Prefers the interpreter running
    the tests; falls back to a discovered Hermes venv.
    """
    probe = (
        "import requests, eth_abi, eth_account, eth_utils, hyperliquid, cryptography"
    )
    candidates = [sys.executable]
    for root in K.DEFAULT_HERMES_CANDIDATES:
        candidate = Path(root) / "venv" / "bin" / "python"
        if candidate.exists():
            candidates.append(str(candidate))
    for candidate in candidates:
        proc = subprocess.run([candidate, "-c", probe], capture_output=True, text=True)
        if proc.returncode == 0:
            return candidate
    raise unittest.SkipTest(
        "no interpreter available with the agents' third-party dependencies"
    )


FIXTURE_PY = _interpreter_with_agent_deps()


# ---------------------------------------------------------------------------
# synthetic clean Hermes fixture
# ---------------------------------------------------------------------------

CLEAN_ADAPTER = '''\
"""Synthetic Telegram adapter fixture (clean: no /trade wiring)."""

import logging

logger = logging.getLogger(__name__)


class MessageType:
    TEXT = "text"
    COMMAND = "command"


class TelegramAdapter:
    name = "telegram"

    async def _handle_callback_query(self, update, context):
        query = update.callback_query
        data = query.data
        query_message = getattr(query, "message", None)
        query_chat = getattr(query_message, "chat", None)
        query_chat_type = getattr(query_chat, "type", None)
        query_thread_id = getattr(query_message, "message_thread_id", None)
        query_user_name = getattr(query.from_user, "first_name", None)

        # --- Model picker callbacks ---
        if data.startswith(("mp:", "mpg:", "mpv:", "mm:", "mc:", "mb", "mx", "mg:")):
            return

    async def _handle_text_message(self, update, context):
        msg = update.message
        if not self._should_process_message(msg):
            return
        await self._ensure_forum_commands(update.message)

        event = self._build_message_event(msg, MessageType.TEXT, update_id=update.update_id)
        event.text = self._clean_bot_trigger_text(event.text)
        self._enqueue_text_event(event)

    async def _handle_command(self, update, context):
        msg = update.message
        if not self._should_process_message(msg, is_command=True):
            return
        await self._ensure_forum_commands(msg)

        event = self._build_message_event(msg, MessageType.COMMAND, update_id=update.update_id)
        event.text = self._clean_bot_trigger_text(event.text)
        await self.handle_message(event)

    def _should_process_message(self, msg, is_command=False):
        return True

    async def _ensure_forum_commands(self, msg):
        return None

    def _build_message_event(self, msg, kind, update_id=None):
        raise NotImplementedError

    def _clean_bot_trigger_text(self, text):
        return text

    def _enqueue_text_event(self, event):
        return None

    async def handle_message(self, event):
        return None
'''

CLEAN_COMMANDS = '''\
"""Synthetic Hermes command registry fixture (clean: no trade entry)."""

from dataclasses import dataclass


@dataclass
class CommandDef:
    name: str
    description: str
    category: str
    aliases: tuple = ()
    args_hint: str = ""
    subcommands: tuple = ()
    cli_only: bool = False
    gateway_only: bool = False
    gateway_config_gate: str | None = None


COMMAND_REGISTRY = [
    CommandDef("help", "Show help", "Session"),
    CommandDef("new", "Start a new session (fresh session ID + history)", "Session",
               aliases=("reset",), args_hint="[name]"),
    CommandDef("topic", "Enable or inspect Telegram DM topic sessions", "Session",
               gateway_only=True, args_hint="[off|help|session-id]"),
    CommandDef("status", "Show status", "Session"),
    CommandDef("restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True),
]

COMMANDS = {f"/{cmd.name}": cmd.description for cmd in COMMAND_REGISTRY if not cmd.gateway_only}
_TELEGRAM_MENU_PRIORITY = ("help", "new", "stop", "status")
'''


def _make_fixture_venv(root: Path) -> None:
    """Give the fixture a ``venv/bin/python`` that can import agent deps.

    A bare symlink is not enough: CPython derives ``sys.prefix`` from the
    location of the executable, so a venv python symlinked into another
    directory loses access to its own site-packages. Instead we write a tiny
    exec shim that hands off to the real interpreter, which keeps that
    interpreter's environment intact while still living at the path the
    installer expects.
    """
    bindir = root / "venv" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / "python"
    if shim.exists():
        return
    shim.write_text(f'#!/bin/sh\nexec "{FIXTURE_PY}" "$@"\n')
    shim.chmod(0o755)


def make_clean_hermes(root: Path) -> Path:
    """Create a minimal but *valid* Hermes tree with pristine anchors.

    Includes a working ``venv/bin/python`` so the installer resolves an
    interpreter that can actually import the agents' third-party
    dependencies. Without it every agent would fail to load for reasons
    unrelated to what these tests cover.
    """
    (root / "hermes_cli").mkdir(parents=True, exist_ok=True)
    (root / "plugins" / "platforms" / "telegram").mkdir(parents=True, exist_ok=True)

    (root / "hermes_cli" / "main.py").write_text("# synthetic hermes entrypoint\n")
    (root / "hermes_cli" / "commands.py").write_text(CLEAN_COMMANDS)
    (root / "plugins" / "platforms" / "telegram" / "adapter.py").write_text(CLEAN_ADAPTER)
    (root / "plugins" / "__init__.py").write_text("")

    _make_fixture_venv(root)
    return root


def run_installer(hermes_root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(INSTALLER / "install_trade.py"),
         "--hermes-root", str(hermes_root), "--skip-deps", "--no-restart", *extra],
        capture_output=True, text=True,
    )


def run_uninstaller(hermes_root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(INSTALLER / "uninstall_trade.py"),
         "--hermes-root", str(hermes_root), "--no-restart", *extra],
        capture_output=True, text=True,
    )


class FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="kam-itest-")
        self.hermes = make_clean_hermes(Path(self.tmp.name) / "hermes")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @property
    def adapter(self) -> Path:
        return self.hermes / adapter_specs()[0].relative_path

    @property
    def commands(self) -> Path:
        return self.hermes / "hermes_cli" / "commands.py"


# ---------------------------------------------------------------------------
# 1. fresh install
# ---------------------------------------------------------------------------

class TestFreshInstall(FixtureCase):
    def test_fresh_install_copies_payload_and_patches_all_seams(self):
        proc = run_installer(self.hermes)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("KAM /trade installation: PASS", proc.stdout)

        # payload landed
        for rel in ("wizard.py", "tradedesk.py", "canonical.py", "__init__.py", "plugin.yaml"):
            self.assertTrue((self.hermes / "plugins" / "trade" / rel).is_file(), rel)

        # all five agents shipped
        agents = sorted(
            p.name for p in (self.hermes / "plugins" / "trade" / "agents").glob("x_*_agent.py")
        )
        self.assertEqual(len(agents), 5, agents)

        # all three adapter seams wired, each exactly once
        text = self.adapter.read_text()
        for spec in adapter_specs():
            self.assertEqual(text.count(spec.native_sentinel), 1, spec.seam)
            self.assertIn(spec.marker_begin(), text)
            self.assertIn(spec.marker_end(), text)

        # commands.py must remain untouched by the default install path
        self.assertNotIn('CommandDef("trade"', self.commands.read_text())
        self.assertNotIn("gateway_platforms", self.commands.read_text())

    def test_patched_adapter_preserves_seam_ordering(self):
        run_installer(self.hermes)
        text = self.adapter.read_text()
        self.assertLess(
            text.index('if data.startswith("trade:")'),
            text.index("# --- Model picker callbacks ---"),
        )
        self.assertLess(
            text.index("from plugins.trade.wizard import handle_trade_text"),
            text.index("event = self._build_message_event(msg, MessageType.TEXT"),
        )
        self.assertLess(
            text.index("from plugins.trade.wizard import handle_trade_command"),
            text.index("event = self._build_message_event(msg, MessageType.COMMAND"),
        )

    def test_patched_files_are_valid_python(self):
        run_installer(self.hermes)
        for path in (self.adapter, self.commands):
            proc = subprocess.run(
                [PY, "-m", "py_compile", str(path)], capture_output=True, text=True
            )
            self.assertEqual(proc.returncode, 0, f"{path}: {proc.stderr}")

    def test_manifest_written_with_required_fields(self):
        run_installer(self.hermes)
        manifest = K.read_manifest(K.installed_manifest_path(self.hermes))
        self.assertIsNotNone(manifest)
        for key in ("kam_version", "installer_version", "timestamp", "hermes_root",
                    "compatible_hermes", "copied_files", "patched_files", "backup_dir"):
            self.assertIn(key, manifest, key)
        for entry in manifest["copied_files"]:
            self.assertIn("sha256_after", entry)
        patched = [p for p in manifest["patched_files"] if p["action"] == "patched"]
        self.assertEqual(len(patched), 4, patched)
        seams = {p["seam"] for p in patched}
        self.assertEqual(
            seams,
            {"callback dispatch", "wizard text interception", "slash command dispatch", "inline keyboard helper"},
        )
        for entry in patched:
            self.assertTrue(entry["sha256_before"])
            self.assertTrue(entry["sha256_after"])
            self.assertNotEqual(entry["sha256_before"], entry["sha256_after"])

    def test_backup_created_for_patched_files(self):
        run_installer(self.hermes)
        backups = list(K.backups_root(self.hermes).glob("*/plugins/platforms/telegram/adapter.py"))
        self.assertEqual(len(backups), 1, backups)
        # backup is the PRE-patch content
        self.assertNotIn("handle_trade_command", backups[0].read_text())

    def test_state_layout_is_manifest_plus_backups_subdir(self):
        run_installer(self.hermes)
        state = K.state_dir(self.hermes)
        self.assertTrue(state.is_dir(), f"{state} missing")
        self.assertEqual(state.name, ".kam-trade")
        self.assertTrue((state / "manifest.json").is_file())
        self.assertTrue((state / "backups").is_dir())
        # backups are timestamped snapshot dirs
        stamps = [p for p in (state / "backups").iterdir() if p.is_dir()]
        self.assertEqual(len(stamps), 1, stamps)

    def test_legacy_manifest_location_is_still_readable(self):
        """An upgrade from the old layout must still find the manifest."""
        run_installer(self.hermes)
        current = K.installed_manifest_path(self.hermes)
        payload = current.read_text()

        # Simulate a pre-existing install that used the legacy path.
        legacy = K.legacy_manifest_path(self.hermes)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(payload)
        current.unlink()

        found = K.find_manifest(self.hermes)
        self.assertIsNotNone(found)
        self.assertEqual(found, legacy)


# ---------------------------------------------------------------------------
# 2. idempotency
# ---------------------------------------------------------------------------

class TestIdempotency(FixtureCase):
    def test_second_install_does_not_duplicate_anything(self):
        first = run_installer(self.hermes)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        after_first = self.adapter.read_text()

        second = run_installer(self.hermes)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        after_second = self.adapter.read_text()

        self.assertEqual(after_first, after_second, "adapter changed on reinstall")
        for spec in adapter_specs():
            self.assertEqual(after_second.count(spec.marker_begin()), 1, spec.seam)
            self.assertEqual(after_second.count(spec.native_sentinel), 1, spec.seam)
        self.assertNotIn('CommandDef("trade"', self.commands.read_text())
        self.assertIn("already-installed", second.stdout)

    def test_third_install_still_stable(self):
        run_installer(self.hermes)
        run_installer(self.hermes)
        snapshot = self.adapter.read_text()
        run_installer(self.hermes)
        self.assertEqual(snapshot, self.adapter.read_text())


# ---------------------------------------------------------------------------
# 3. dry run
# ---------------------------------------------------------------------------

class TestDryRun(FixtureCase):
    def test_dry_run_changes_nothing(self):
        before_adapter = self.adapter.read_text()
        before_commands = self.commands.read_text()

        proc = run_installer(self.hermes, "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        self.assertEqual(self.adapter.read_text(), before_adapter)
        self.assertEqual(self.commands.read_text(), before_commands)
        self.assertFalse((self.hermes / "plugins" / "trade").exists())
        self.assertFalse(K.state_dir(self.hermes).exists())
        self.assertIn("would-patch", proc.stdout)

    def test_dry_run_reports_planned_operations(self):
        proc = run_installer(self.hermes, "--dry-run")
        self.assertIn("DRY RUN", proc.stdout)
        self.assertIn("would copy", proc.stdout)


# ---------------------------------------------------------------------------
# 4-6. discovery failure modes
# ---------------------------------------------------------------------------

class TestHermesDiscovery(unittest.TestCase):
    def test_missing_hermes_root_fails_cleanly(self):
        proc = subprocess.run(
            [PY, str(INSTALLER / "install_trade.py"),
             "--hermes-root", "/nonexistent/path/xyz", "--skip-deps", "--no-restart"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not look like a Hermes installation", proc.stdout)
        self.assertIn("KAM /trade installation: FAIL", proc.stdout)

    def test_invalid_hermes_root_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bogus = Path(tmp) / "hermes"   # right name, wrong contents
            bogus.mkdir()
            proc = subprocess.run(
                [PY, str(INSTALLER / "install_trade.py"),
                 "--hermes-root", str(bogus), "--skip-deps", "--no-restart"],
                capture_output=True, text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("KAM /trade installation: FAIL", proc.stdout)

    def test_directory_named_hermes_is_not_sufficient(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "hermes-agent"
            (fake / "hermes_cli").mkdir(parents=True)
            # main.py present but adapter.py missing -> must be rejected
            (fake / "hermes_cli" / "main.py").write_text("")
            self.assertFalse(K.looks_like_hermes_root(fake))

    def test_valid_root_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_clean_hermes(Path(tmp) / "h")
            self.assertTrue(K.looks_like_hermes_root(root))


# ---------------------------------------------------------------------------
# 7-8. patch guard rails
# ---------------------------------------------------------------------------

class TestPatchGuards(FixtureCase):
    def test_existing_registration_block_is_left_alone(self):
        run_installer(self.hermes)
        marked = self.adapter.read_text()
        proc = run_installer(self.hermes)
        self.assertEqual(self.adapter.read_text(), marked)
        self.assertIn("already-installed", proc.stdout)

    def test_modified_adapter_missing_anchor_aborts(self):
        text = self.adapter.read_text().replace(
            'query_user_name = getattr(query.from_user, "first_name", None)',
            "query_user_name = None  # refactored away",
        )
        self.adapter.write_text(text)
        before = self.adapter.read_text()

        proc = run_installer(self.hermes)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Refusing to patch", proc.stdout)
        self.assertIn("KAM /trade installation: FAIL", proc.stdout)
        self.assertEqual(self.adapter.read_text(), before, "file mutated despite abort")

    def test_duplicated_anchor_aborts(self):
        text = self.adapter.read_text()
        text += "\n\n# --- Model picker callbacks ---\n"   # now 2 occurrences
        self.adapter.write_text(text)
        before = self.adapter.read_text()

        proc = run_installer(self.hermes)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("expected exactly 1", proc.stdout)
        self.assertEqual(self.adapter.read_text(), before)

    def test_native_unmarked_seam_is_not_double_patched(self):
        """A node whose adapter already dispatches /trade natively.

        The injected block must be indented to match the method body it is
        spliced into (8 spaces), otherwise the fixture itself would be
        syntactically invalid and we would be testing the wrong thing.
        """
        native_block = (
            '        if data.startswith("trade:"):\n'
            "            from plugins.trade.wizard import handle_trade_callback\n"
            "\n"
            "            await handle_trade_callback(self, query, data)\n"
            "            return\n"
            "\n"
            "        # --- Model picker callbacks ---"
        )
        text = self.adapter.read_text().replace(
            "        # --- Model picker callbacks ---", native_block
        )
        self.adapter.write_text(text)

        # Sanity: the fixture must still be valid Python before we install.
        proc = subprocess.run(
            [PY, "-m", "py_compile", str(self.adapter)], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, f"fixture invalid: {proc.stderr}")

        proc = run_installer(self.hermes)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("native-present", proc.stdout)
        self.assertEqual(
            self.adapter.read_text().count(
                "from plugins.trade.wizard import handle_trade_callback"
            ), 1,
        )


# ---------------------------------------------------------------------------
# 9. verification gate
# ---------------------------------------------------------------------------

class TestVerificationGate(FixtureCase):
    def test_verifier_passes_after_install(self):
        run_installer(self.hermes)
        proc = subprocess.run(
            [PY, str(INSTALLER / "verify_trade.py"), "--hermes-root", str(self.hermes)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("KAM /trade installation: PASS", proc.stdout)

    def test_verifier_fails_when_plugin_absent(self):
        proc = subprocess.run(
            [PY, str(INSTALLER / "verify_trade.py"), "--hermes-root", str(self.hermes)],
            capture_output=True, text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("KAM /trade installation: FAIL", proc.stdout)

    def test_verifier_never_mentions_live_actions(self):
        run_installer(self.hermes)
        proc = subprocess.run(
            [PY, str(INSTALLER / "verify_trade.py"), "--hermes-root", str(self.hermes)],
            capture_output=True, text=True,
        )
        lowered = proc.stdout.lower()
        for forbidden in ("order placed", "cancelled order", "balance:", "position:"):
            self.assertNotIn(forbidden, lowered)


# ---------------------------------------------------------------------------
# 10-11. uninstall
# ---------------------------------------------------------------------------

class TestUninstall(FixtureCase):
    def test_uninstall_removes_files_and_markers(self):
        run_installer(self.hermes)
        proc = run_uninstaller(self.hermes)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        self.assertFalse((self.hermes / "plugins" / "trade" / "wizard.py").exists())
        text = self.adapter.read_text()
        for spec in adapter_specs():
            self.assertNotIn(spec.marker_begin(), text)
            self.assertNotIn(spec.native_sentinel, text)

    def test_uninstalled_adapter_still_compiles(self):
        run_installer(self.hermes)
        run_uninstaller(self.hermes)
        proc = subprocess.run(
            [PY, "-m", "py_compile", str(self.adapter)], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_second_uninstall_is_safe(self):
        run_installer(self.hermes)
        run_uninstaller(self.hermes)
        snapshot = self.adapter.read_text()
        proc = run_uninstaller(self.hermes)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.adapter.read_text(), snapshot)

    def test_uninstall_dry_run_changes_nothing(self):
        run_installer(self.hermes)
        before = self.adapter.read_text()
        proc = run_uninstaller(self.hermes, "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.adapter.read_text(), before)
        self.assertTrue((self.hermes / "plugins" / "trade" / "wizard.py").exists())

    def test_uninstall_preserves_backups_by_default(self):
        run_installer(self.hermes)
        run_uninstaller(self.hermes)
        self.assertTrue(K.backups_root(self.hermes).is_dir())

    def test_uninstall_restores_adapter_equivalent_to_clean(self):
        original = self.adapter.read_text()
        run_installer(self.hermes)
        run_uninstaller(self.hermes)
        cleaned = self.adapter.read_text()
        self.assertEqual(
            [l.rstrip() for l in cleaned.splitlines() if l.strip()],
            [l.rstrip() for l in original.splitlines() if l.strip()],
        )

    def test_uninstall_restores_files_byte_for_byte(self):
        """Regression: unpatch must consume the blank line apply_patch added.

        apply_patch inserts "\\n" + markers + code. An earlier remove_patch
        collapsed that region to "\\n", leaving one stray blank line per seam
        so the file was never restored byte-for-byte. Caught by the real-Hermes
        fixture; pinned here.
        """
        adapter_clean = self.adapter.read_bytes()
        commands_clean = self.commands.read_bytes()

        run_installer(self.hermes)
        self.assertNotEqual(self.adapter.read_bytes(), adapter_clean,
                            "install did not change adapter.py")

        run_uninstaller(self.hermes)
        self.assertEqual(self.adapter.read_bytes(), adapter_clean,
                         "adapter.py not restored byte-for-byte")
        self.assertEqual(self.commands.read_bytes(), commands_clean,
                         "commands.py not restored byte-for-byte")

    def test_repeated_cycles_do_not_accumulate_whitespace(self):
        adapter_clean = self.adapter.read_bytes()
        for _ in range(3):
            run_installer(self.hermes)
            run_uninstaller(self.hermes)
            self.assertEqual(self.adapter.read_bytes(), adapter_clean)


# ---------------------------------------------------------------------------
# 16-18. non-interference
# ---------------------------------------------------------------------------

class TestNonInterference(FixtureCase):
    def test_env_file_untouched_byte_for_byte(self):
        env = self.hermes / ".env"
        payload = "TELEGRAM_BOT_TOKEN=placeholder\nHYPERLIQUID_X_WALLET=0xabc\n"
        env.write_text(payload)
        digest = K.sha256_file(env)

        run_installer(self.hermes)
        self.assertEqual(K.sha256_file(env), digest)
        self.assertEqual(env.read_text(), payload)

        run_uninstaller(self.hermes)
        self.assertTrue(env.is_file(), ".env deleted by uninstall")
        self.assertEqual(env.read_text(), payload)

    def test_unrelated_plugin_untouched(self):
        other = self.hermes / "plugins" / "other_plugin"
        other.mkdir(parents=True)
        marker = other / "keep.py"
        marker.write_text("# unrelated plugin\n")
        digest = K.sha256_file(marker)

        run_installer(self.hermes)
        self.assertEqual(K.sha256_file(marker), digest)
        run_uninstaller(self.hermes)
        self.assertTrue(marker.is_file())
        self.assertEqual(K.sha256_file(marker), digest)

    def test_unrelated_commands_preserved(self):
        run_installer(self.hermes)
        text = self.commands.read_text()
        for required in ('CommandDef("help"', 'CommandDef("new"',
                         'CommandDef("topic"', 'CommandDef("status"'):
            self.assertIn(required, text)

    def test_installer_does_not_create_env(self):
        run_installer(self.hermes)
        self.assertFalse((self.hermes / ".env").exists())


# ---------------------------------------------------------------------------
# 12-15. plugin-level invariants
# ---------------------------------------------------------------------------

class TestPluginInvariants(unittest.TestCase):
    def test_no_trade_enabled_flag_anywhere(self):
        """No enable flag may gate /trade.

        The literal is assembled at runtime so this test and the verifier
        (which must name the flag in order to check for it) do not count as
        hits. README.md is allowed to *document* that the flag does not
        exist. Everything else must be free of it.
        """
        needle = "TRADE" + "_ENABLED"
        allowed = {
            Path("tests/test_installation.py"),
            Path("installer/verify_trade.py"),
            Path("README.md"),
        }
        hits = []
        for path in REPO_ROOT.rglob("*"):
            if path.is_dir() or "__pycache__" in path.parts or ".git" in path.parts:
                continue
            if path.suffix not in {".py", ".sh", ".yaml", ".yml", ".md", ".txt", ".json"}:
                continue
            rel = path.relative_to(REPO_ROOT)
            if rel in allowed:
                continue
            if needle in path.read_text(errors="ignore"):
                hits.append(str(rel))
        self.assertEqual(hits, [], f"{needle} found in {hits}")

    def test_readme_only_mentions_flag_to_deny_it(self):
        """README may name the flag, but only to say it is unsupported."""
        needle = "TRADE" + "_ENABLED"
        text = (REPO_ROOT / "README.md").read_text()
        for line in text.splitlines():
            if needle not in line:
                continue
            lowered = line.lower()
            self.assertTrue(
                any(word in lowered for word in ("no ", "not ", "none", "without")),
                f"README mentions {needle} without denying it: {line.strip()}",
            )

    def test_shipped_plugin_has_no_enable_flag(self):
        """The installed payload itself must be flag-free."""
        needle = "TRADE" + "_ENABLED"
        for path in (REPO_ROOT / "plugins" / "trade").rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            self.assertNotIn(needle, path.read_text(errors="ignore"), str(path))

    def test_no_plugin_py_or_router_py(self):
        trade = REPO_ROOT / "plugins" / "trade"
        self.assertFalse((trade / "plugin.py").exists())
        self.assertFalse((trade / "router.py").exists())

    def test_all_five_agents_present(self):
        agents = sorted(
            p.stem[2:-6]
            for p in (REPO_ROOT / "plugins" / "trade" / "agents").glob("x_*_agent.py")
        )
        self.assertEqual(agents, ["arcus", "hyperliquid", "lighter", "raydium", "rise"])

    def test_installer_contains_no_exchange_names(self):
        """The installer must stay exchange-agnostic."""
        names = ("hyperliquid", "arcus", "lighter", "raydium", "rise", "orderly")
        for module in ("install_trade.py", "uninstall_trade.py", "kamlib.py", "patchspecs.py"):
            text = (INSTALLER / module).read_text().lower()
            for name in names:
                self.assertNotIn(
                    name, text, f"{module} references exchange '{name}'"
                )

    def test_requirements_excludes_unimported_packages(self):
        text = (INSTALLER / "requirements.txt").read_text()
        active = [
            l.strip() for l in text.splitlines()
            if l.strip() and not l.strip().startswith("#")
        ]
        joined = " ".join(active).lower()
        self.assertNotIn("pynacl", joined)
        self.assertNotIn("base58", joined)

    def test_every_requirement_is_actually_imported(self):
        import re

        trade = REPO_ROOT / "plugins" / "trade"
        sources = "\n".join(
            p.read_text(errors="ignore")
            for p in trade.rglob("*.py")
            if "tests" not in p.parts
        )
        module_for = {
            "requests": "requests",
            "cryptography": "cryptography",
            "eth-abi": "eth_abi",
            "eth-account": "eth_account",
            "eth-utils": "eth_utils",
            "hyperliquid-python-sdk": "hyperliquid",
        }
        for line in (INSTALLER / "requirements.txt").read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pkg = re.split(r"[><=!\[]", line)[0].strip()
            mod = module_for.get(pkg)
            self.assertIsNotNone(mod, f"unmapped requirement: {pkg}")
            self.assertRegex(
                sources, rf"\b{re.escape(mod)}\b",
                f"requirement {pkg} not imported by any shipped agent",
            )

    def test_env_example_names_match_agent_code(self):
        """Documented variable names must exist in the agents.

        Guards against .env.example drifting away from reality (e.g.
        documenting RISE_<A>_PRIVATE_KEY when the agent reads
        RISE_<A>_APISIGNERPRIVATE).
        """
        import re

        example = (REPO_ROOT / ".env.example").read_text()
        agents_dir = REPO_ROOT / "plugins" / "trade" / "agents"
        agent_sources = {
            p.stem[2:-6]: p.read_text(errors="ignore")
            for p in agents_dir.glob("x_*_agent.py")
        }

        documented = set(
            re.findall(r"^#\s*([A-Z][A-Z0-9]*)_<ACCOUNT>_([A-Z_]+)=", example, re.M)
        )
        self.assertTrue(documented, "no variables documented in .env.example")

        unknown = []
        for prefix, suffix in sorted(documented):
            exchange = prefix.lower()
            source = agent_sources.get(exchange)
            self.assertIsNotNone(
                source, f".env.example documents unknown exchange '{exchange}'"
            )
            if suffix not in source:
                unknown.append(f"{prefix}_<ACCOUNT>_{suffix}")
        self.assertEqual(
            unknown, [], f"documented but not read by any agent: {unknown}"
        )

    def test_env_example_documents_every_exchange(self):
        example = (REPO_ROOT / ".env.example").read_text()
        for path in (REPO_ROOT / "plugins" / "trade" / "agents").glob("x_*_agent.py"):
            prefix = path.stem[2:-6].upper()
            self.assertIn(
                f"{prefix}_<ACCOUNT>_", example,
                f"{prefix} has an agent but no .env.example documentation",
            )

    def test_env_example_has_no_real_values(self):
        """Every credential line must stay commented and placeholder-only."""
        for raw in (REPO_ROOT / ".env.example").read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            self.fail(f".env.example contains an uncommented assignment: {line}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
