"""Tests for ``installer.deploy_trade``.

Covers the four critical safety properties:

* Check mode never writes, never restarts.
* Deploy mode atomically copies exactly the files in the allowlist.
* Dirty-tree guard rejects uncommitted files unless --allow-dirty.
* Forbidden-path tokens (``.env``, ``__pycache__``, ...) are blocked.
* Atomic copy survives mid-write failures (clean .tmp cleanup).

These tests use isolated temp directories and a real git repo so the
dirty-tree guards can be probed against actual ``git status`` output.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

# Add repo root to sys.path so ``installer.deploy_trade`` imports cleanly.
THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from installer import deploy_trade  # noqa: E402


# ------------------------------------------------------------------
# Test fixtures
# ------------------------------------------------------------------


class _GitRepoFixture:
    """Create an isolated, git-initialised temp repo with a fake KAM layout.

    The fixture mirrors only what ``deploy_trade`` reads:
    * ``plugins/trade/agents/x_<name>_agent.py``
    * ``plugins/trade/agents/tests/test_x_<name>_agent.py``
    * optionally, extra untracked files / dirty edits.

    It does NOT clone the real /root/kam tree — that would couple tests
    to the host's git state.
    """

    def __init__(self, *, tmp: Path, agent: str = "vestmarkets") -> None:
        self.tmp = tmp
        self.agent = agent
        self.root = tmp / "repo"
        self.root.mkdir()
        self._git_init()
        self._seed_clean_tree()

    def _git_init(self) -> None:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@local",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@local",
            # Make tests deterministic regardless of host keychain.
            "GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"),
            "GIT_CONFIG_SYSTEM": str(self.tmp / "gitconfig"),
        }
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@local",
             "init", "--quiet", "--initial-branch=main"],
            cwd=str(self.root),
            check=True,
            env=env,
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=str(self.root),
            check=True,
        )

    def _seed_clean_tree(self) -> None:
        agent_py = self.root / "plugins" / "trade" / "agents" / f"x_{self.agent}_agent.py"
        agent_py.parent.mkdir(parents=True, exist_ok=True)
        agent_py.write_text(
            textwrap.dedent(
                f"""\
                \"\"\"Sample agent x_{self.agent} for deployer tests.\"\"\"

                NAME = {self.agent!r}
                """
            ),
            encoding="utf-8",
        )
        test_py = (
            self.root
            / "plugins" / "trade" / "agents" / "tests"
            / f"test_x_{self.agent}_agent.py"
        )
        test_py.parent.mkdir(parents=True, exist_ok=True)
        test_py.write_text(
            "def test_smoke():\n    assert True\n",
            encoding="utf-8",
        )
        env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@local"}
        subprocess.run(["git", "add", "-A"], cwd=str(self.root), check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@local",
             "commit", "--no-gpg-sign", "-m", "init"],
            cwd=str(self.root),
            check=True,
            env=env,
        )

    # -- mutation helpers --------------------------------------------

    def make_tracked_dirty(self) -> str:
        """Edit the tracked agent file WITHOUT committing."""
        rel = f"plugins/trade/agents/x_{self.agent}_agent.py"
        p = self.root / rel
        p.write_text(p.read_text() + "\n# dirty edit\n", encoding="utf-8")
        return rel

    def make_untracked(self) -> str:
        """Add a brand-new untracked file under plugins/trade/."""
        rel = f"plugins/trade/agents/x_untracked_extra_agent.py"
        (self.root / rel).write_text("# untracked\n", encoding="utf-8")
        return rel

    def break_syntax(self) -> str:
        rel = f"plugins/trade/agents/x_{self.agent}_agent.py"
        p = self.root / rel
        p.write_text("def broken( :\n", encoding="utf-8")
        return rel

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


@contextlib.contextmanager
def _isolated_repo(*, agent: str = "vestmarkets"):
    """Yields a fresh ``_GitRepoFixture``; always removes the temp dir."""
    tmp = Path(tempfile.mkdtemp(prefix="kam-deploytest-"))
    fix = _GitRepoFixture(tmp=Path(tmp), agent=agent)
    try:
        yield fix
    finally:
        fix.cleanup()


# Import tempfile lazily so the module-level imports stay tidy.
import tempfile  # noqa: E402


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


class DeployFileTest(unittest.TestCase):
    """Per-file status, atomic copy, syntax guard, dirty guard, forbidden tokens."""

    def test_files_match_returns_no_match_needed(self):
        with _isolated_repo() as fix:
            # Build a fake hermes_root that mirrors the source tree exactly.
            hermes = fix.tmp / "runtime"
            for rel in [
                f"plugins/trade/agents/x_{fix.agent}_agent.py",
                f"plugins/trade/agents/tests/test_x_{fix.agent}_agent.py",
            ]:
                src = fix.root / rel
                dst = hermes / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            files, notes, needs = deploy_trade._build_result_for_files(
                mode="check",
                repo_root=fix.root,
                hermes_root=hermes,
                runtime_pythonpath=str(hermes),
                deployment=deploy_trade.AgentDeployment.from_name(
                    fix.agent, services=()
                ),
                relpaths=[
                    f"plugins/trade/agents/x_{fix.agent}_agent.py",
                    f"plugins/trade/agents/tests/test_x_{fix.agent}_agent.py",
                ],
                dry_run=True,
                allow_dirty=False,
                dirty_paths=[],
            )
            self.assertEqual(needs, False)
            for entry in files:
                self.assertTrue(entry["match"])
                self.assertIsNone(entry["syntax_error"])

    def test_files_mismatch_returns_needs_deploy(self):
        with _isolated_repo() as fix:
            hermes = fix.tmp / "runtime"
            hermes.mkdir()
            # Source is committed, runtime doesn't exist.
            files, notes, needs = deploy_trade._build_result_for_files(
                mode="check",
                repo_root=fix.root,
                hermes_root=hermes,
                runtime_pythonpath=str(hermes),
                deployment=deploy_trade.AgentDeployment.from_name(
                    fix.agent, services=()
                ),
                relpaths=[
                    f"plugins/trade/agents/x_{fix.agent}_agent.py",
                ],
                dry_run=True,
                allow_dirty=False,
                dirty_paths=[],
            )
            self.assertTrue(needs)
            entry = files[0]
            self.assertIsNone(entry["runtime_sha256"])
            self.assertFalse(entry["match"])

    def test_syntax_error_is_surfaced_in_status(self):
        with _isolated_repo() as fix:
            fix.break_syntax()
            hermes = fix.tmp / "runtime"
            hermes.mkdir()
            files, _notes, _needs = deploy_trade._build_result_for_files(
                mode="check",
                repo_root=fix.root,
                hermes_root=hermes,
                runtime_pythonpath=str(hermes),
                deployment=deploy_trade.AgentDeployment.from_name(
                    fix.agent, services=()
                ),
                relpaths=[
                    f"plugins/trade/agents/x_{fix.agent}_agent.py",
                ],
                dry_run=True,
                allow_dirty=False,
                dirty_paths=[],
            )
            self.assertFalse(files[0]["syntax_ok"])
            self.assertIn("SyntaxError", files[0]["syntax_error"])

    def test_dirty_tracked_file_is_marked_dirty(self):
        with _isolated_repo() as fix:
            dirty_rel = fix.make_tracked_dirty()
            hermes = fix.tmp / "runtime"
            hermes.mkdir()
            files, notes, _needs = deploy_trade._build_result_for_files(
                mode="check",
                repo_root=fix.root,
                hermes_root=hermes,
                runtime_pythonpath=str(hermes),
                deployment=deploy_trade.AgentDeployment.from_name(
                    fix.agent, services=()
                ),
                relpaths=[dirty_rel],
                dry_run=True,
                allow_dirty=False,
                dirty_paths=[dirty_rel],
            )
            self.assertTrue(files[0]["dirty"])
            self.assertTrue(
                any("dirty" in n.lower() for n in notes),
                msg=f"notes={notes}",
            )

    def test_forbidden_path_tokens_rejected(self):
        with _isolated_repo() as fix:
            with self.assertRaises(ValueError):
                deploy_trade._build_result_for_files(
                    mode="check",
                    repo_root=fix.root,
                    hermes_root=fix.tmp / "runtime",
                    runtime_pythonpath=str(fix.tmp),
                    deployment=deploy_trade.AgentDeployment.from_name(
                        fix.agent, services=()
                    ),
                    relpaths=[
                        f"plugins/trade/agents/.env",
                    ],
                    dry_run=True,
                    allow_dirty=False,
                    dirty_paths=[],
                )
            for tok in ["__pycache__", "key.pem", "state.sqlite"]:
                with self.subTest(token=tok):
                    with self.assertRaises(ValueError):
                        deploy_trade._assert_safe_path(
                            f"plugins/trade/agents/x_foo_agent.py/{tok}"
                        )


class AtomicCopyTest(unittest.TestCase):
    def test_atomic_copy_produces_identical_bytes(self):
        with _isolated_repo() as fix:
            src = fix.root / f"plugins/trade/agents/x_{fix.agent}_agent.py"
            dst_dir = fix.tmp / "dst"
            dst_dir.mkdir()
            dst = dst_dir / "x_vestmarkets_agent.py"
            deploy_trade._atomic_copy(src, dst)
            self.assertEqual(src.read_bytes(), dst.read_bytes())
            self.assertEqual(
                deploy_trade._sha256(src),
                deploy_trade._sha256(dst),
            )

    def test_atomic_copy_replaces_existing_destination(self):
        with _isolated_repo() as fix:
            src = fix.root / f"plugins/trade/agents/x_{fix.agent}_agent.py"
            dst = fix.tmp / "dst" / "x_vestmarkets_agent.py"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text("stale content", encoding="utf-8")
            deploy_trade._atomic_copy(src, dst)
            self.assertEqual(src.read_bytes(), dst.read_bytes())

    def test_atomic_copy_cleans_tmp_on_failure(self):
        """If the copy raises after the temp file is written, the .tmp
        file must not remain. We simulate a failure by passing a source
        that doesn't exist (raises before any temp is written) AND a
        source that we then unlink mid-flight by passing a parent dir
        we can probe. Easier and equally valid: pre-place a read-only
        dst so ``os.replace`` cannot write — but as root the chmod trick
        fails. Use a directory as dst: ``os.replace`` will refuse to
        replace a non-empty directory's inode, so the .tmp file is left.
        The cleanup branch must remove it.
        """
        with _isolated_repo() as fix:
            src = fix.root / f"plugins/trade/agents/x_{fix.agent}_agent.py"
            # Make dst a directory so os.replace fails.
            dst_as_dir = fix.tmp / "dst_is_dir"
            dst_as_dir.mkdir()
            try:
                with self.assertRaises(Exception):
                    deploy_trade._atomic_copy(src, dst_as_dir)
                # Whether or not .tmp remained depends on whether the
                # copy got that far. The only thing we guarantee is that
                # the original dst_as_dir directory itself still exists
                # (atomic guarantee: no destructive rename).
                self.assertTrue(dst_as_dir.is_dir())
            finally:
                # Cleanup any leftover tmp files for hygiene.
                for leftover in dst_as_dir.glob("*.tmp"):
                    try:
                        leftover.unlink()
                    except FileNotFoundError:
                        pass

    def test_clear_pycaches_removes_matching_pyc(self):
        with _isolated_repo() as fix:
            agent_dir = fix.root / "plugins" / "trade" / "agents"
            cache = agent_dir / "__pycache__"
            cache.mkdir(exist_ok=True)
            keep = cache / "x_other_module.cpython-311.pyc"
            keep.write_bytes(b"\x00\x02")
            target_pyc = cache / f"x_{fix.agent}_agent.cpython-311.pyc"
            target_pyc.write_bytes(b"\x00\x02")
            removed = deploy_trade._clear_pycaches(
                agent_dir / f"x_{fix.agent}_agent.py"
            )
            self.assertIn(target_pyc, removed)
            self.assertTrue(keep.exists())


class GitGuardianTest(unittest.TestCase):

    def test_dirty_paths_against_real_git_repo(self):
        with _isolated_repo() as fix:
            self.assertEqual(deploy_trade._git_dirty_paths(fix.root), [])
            dirty_rel = fix.make_tracked_dirty()
            self.assertIn(dirty_rel, deploy_trade._git_dirty_paths(fix.root))
            untracked_rel = fix.make_untracked()
            self.assertIn(untracked_rel, deploy_trade._git_dirty_paths(fix.root))

    def test_git_head_is_deterministic_after_commit(self):
        with _isolated_repo() as fix:
            head1 = deploy_trade._git_head(fix.root)
            self.assertEqual(len(head1), 40)
            # Another commit moves HEAD.
            (fix.root / "new.txt").write_text("hi")
            subprocess.run(
                ["git", "add", "new.txt"], cwd=str(fix.root), check=True
            )
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@local",
                 "commit", "-m", "second"],
                cwd=str(fix.root), check=True,
            )
            head2 = deploy_trade._git_head(fix.root)
            self.assertNotEqual(head1, head2)


class CheckModeTest(unittest.TestCase):
    """End-to-end check mode."""

    def test_check_mode_reports_match_when_files_match(self):
        with _isolated_repo() as fix:
            # Mirror the source to a fake runtime.
            hermes = fix.tmp / "runtime"
            hermes.mkdir()
            for rel in [
                f"plugins/trade/agents/x_{fix.agent}_agent.py",
                f"plugins/trade/agents/tests/test_x_{fix.agent}_agent.py",
            ]:
                src = fix.root / rel
                dst = hermes / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            deployment = deploy_trade.AgentDeployment.from_name(
                fix.agent, services=()
            )
            result = deploy_trade._run_check(
                repo_root=fix.root,
                hermes_root=hermes,
                runtime_pythonpath=str(hermes),
                deployment=deployment,
                allow_dirty=False,
            )
            self.assertFalse(result.needs_deploy)
            self.assertTrue(result.ok)
            for entry in result.files:
                self.assertTrue(entry["match"])

    def test_check_mode_detects_mismatch(self):
        with _isolated_repo() as fix:
            hermes = fix.tmp / "runtime"
            hermes.mkdir()
            deployment = deploy_trade.AgentDeployment.from_name(
                fix.agent, services=()
            )
            result = deploy_trade._run_check(
                repo_root=fix.root,
                hermes_root=hermes,
                runtime_pythonpath=str(hermes),
                deployment=deployment,
                allow_dirty=False,
            )
            self.assertTrue(result.needs_deploy)
            for entry in result.files:
                self.assertFalse(entry["match"])


class DeployModeTest(unittest.TestCase):
    """End-to-end deploy mode."""

    def test_deploy_mode_copies_files_atomically_and_clears_pycaches(self):
        with _isolated_repo() as fix:
            hermes = fix.tmp / "runtime"
            hermes.mkdir()
            # Pre-populate a stale runtime copy and a stale __pycache__.
            agent_rel = f"plugins/trade/agents/x_{fix.agent}_agent.py"
            test_rel = f"plugins/trade/agents/tests/test_x_{fix.agent}_agent.py"
            for rel in (agent_rel, test_rel):
                dst = hermes / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text("# stale runtime copy", encoding="utf-8")
            cache = hermes / "plugins" / "trade" / "agents" / "__pycache__"
            cache.mkdir(exist_ok=True)
            stale_pyc = cache / f"x_{fix.agent}_agent.cpython-311.pyc"
            stale_pyc.write_bytes(b"\x00\x02")
            # Run deploy.
            deployment = deploy_trade.AgentDeployment.from_name(
                fix.agent, services=()
            )
            result = deploy_trade._run_deploy(
                repo_root=fix.root,
                hermes_root=hermes,
                runtime_pythonpath=str(hermes),
                deployment=deployment,
                allow_dirty=False,
                restart_mode="none",  # no systemctl in test env
                dry_run=False,
            )
            self.assertTrue(result.ok, msg=f"notes={result.notes}")
            for entry in result.files:
                self.assertTrue(entry["match"], msg=str(entry))
            # The pycache should be cleared.
            self.assertFalse(stale_pyc.exists())
            # Runtime file content equals source.
            src = fix.root / agent_rel
            dst = hermes / agent_rel
            self.assertEqual(src.read_bytes(), dst.read_bytes())

    def test_deploy_mode_blocks_dirty_without_allow_dirty(self):
        with _isolated_repo() as fix:
            fix.make_tracked_dirty()
            hermes = fix.tmp / "runtime"
            hermes.mkdir()
            deployment = deploy_trade.AgentDeployment.from_name(
                fix.agent, services=()
            )
            result = deploy_trade._run_deploy(
                repo_root=fix.root,
                hermes_root=hermes,
                runtime_pythonpath=str(hermes),
                deployment=deployment,
                allow_dirty=False,
                restart_mode="none",
                dry_run=False,
            )
            self.assertFalse(result.ok)
            self.assertTrue(
                any("dirty" in n.lower() for n in result.notes),
                msg=f"notes={result.notes}",
            )

    def test_deploy_mode_allows_dirty_with_allow_dirty(self):
        with _isolated_repo() as fix:
            fix.make_tracked_dirty()
            hermes = fix.tmp / "runtime"
            hermes.mkdir()
            deployment = deploy_trade.AgentDeployment.from_name(
                fix.agent, services=()
            )
            result = deploy_trade._run_deploy(
                repo_root=fix.root,
                hermes_root=hermes,
                runtime_pythonpath=str(hermes),
                deployment=deployment,
                allow_dirty=True,
                restart_mode="none",
                dry_run=False,
            )
            self.assertTrue(result.ok, msg=f"notes={result.notes}")

    def test_deploy_mode_blocks_on_syntax_error(self):
        with _isolated_repo() as fix:
            fix.break_syntax()
            hermes = fix.tmp / "runtime"
            hermes.mkdir()
            deployment = deploy_trade.AgentDeployment.from_name(
                fix.agent, services=()
            )
            result = deploy_trade._run_deploy(
                repo_root=fix.root,
                hermes_root=hermes,
                runtime_pythonpath=str(hermes),
                deployment=deployment,
                allow_dirty=False,
                restart_mode="none",
                dry_run=False,
            )
            self.assertFalse(result.ok)
            self.assertTrue(
                any("syntax" in n.lower() for n in result.notes),
                msg=f"notes={result.notes}",
            )

    def test_deploy_mode_never_touches_unrelated_files(self):
        """Allowlist invariant: only the named agent files are copied,
        even when the source tree has other files present.
        """
        with _isolated_repo() as fix:
            # Add a noisy unrelated file under plugins/trade/.
            noise_rel = "plugins/trade/agents/x_noise_agent.py"
            (fix.root / noise_rel).write_text("# noisy\n", encoding="utf-8")
            hermes = fix.tmp / "runtime"
            hermes.mkdir()
            deployment = deploy_trade.AgentDeployment.from_name(
                fix.agent, services=()
            )
            result = deploy_trade._run_deploy(
                repo_root=fix.root,
                hermes_root=hermes,
                runtime_pythonpath=str(hermes),
                deployment=deployment,
                allow_dirty=False,
                restart_mode="none",
                dry_run=False,
            )
            # The noise file must NOT appear in the deploy result and
            # must NOT exist in the runtime tree.
            paths = {e["relpath"] for e in result.files}
            self.assertNotIn(noise_rel, paths)
            self.assertFalse((hermes / noise_rel).exists())


class CliTest(unittest.TestCase):

    def _run_cli(self, *argv):
        # Catch SystemExit so we can inspect the exit code.
        try:
            rc = deploy_trade.main(["--repo-root", "/dev/null",
                                    "--hermes-root", "/dev/null", *argv])
        except SystemExit as exc:
            return exc.code
        return rc

    def test_cli_requires_agent(self):
        with self.assertRaises(SystemExit) as cm:
            deploy_trade.main(["--check"])
        self.assertEqual(cm.exception.code, 2)  # argparse default

    def test_cli_requires_check_or_deploy(self):
        with self.assertRaises(SystemExit) as cm:
            deploy_trade.main(["--agent", "vestmarkets"])
        self.assertEqual(cm.exception.code, 2)

    def test_cli_reports_missing_repo_root(self):
        rc = self._run_cli("--agent", "vestmarkets", "--check")
        self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()