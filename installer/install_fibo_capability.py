"""Capability-specific installer: FIBO (lightweight UI skeleton).

Installs ONLY the /fibo Telegram wizard skeleton and the
Telegram adapter seams that route ``/fibo``, ``fibo:`` callbacks,
and (when supported) /fibo free-text interception.

This module installs the shared agent layer only via the caller's
``shared`` record (produced by ``installer_shared.install_shared``);
this module itself owns ZERO shared files. The /trade installer is
responsible for ``wizard.py``; /fibo is responsible for
``fibo_wizard.py`` only.

It does NOT install:

* ``fibo.service`` (no systemd unit),
* ``fibo_daemon.py`` (no runtime daemon),
* ``fibo_service.py`` (no runtime state machine),
* ``golden_fibo/`` (no old strategy engine),
* ``~/.hermes/fibo/`` (no runtime directory yet — empty phase).

Future iterations that bring up a real Fibo strategy may extend this
module to also install those files. For now, the wizard is a static
placeholder.

Takes EXPLICIT ``hermes_root`` (the installed app tree) and
``hermes_home`` (the persistent state). The two are independent.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))


REPO_ROOT = Path(__file__).resolve().parent.parent

# Files owned exclusively by the /fibo capability.
# Wizard lives alongside the /trade wizard so the package stays a
# single Python import path under ``plugins.trade``.
# Phase 1 also ships the ``fibo/`` sub-package (Start Fibo sub-flow,
# MT4 Reader, snapshot/store/session/flow modules). Each file is
# listed explicitly so the install/uninstall capability mirrors
# without walking the tree.
FIBO_REL_PATHS = [
    Path("plugins") / "trade" / "fibo_wizard.py",
    Path("plugins") / "trade" / "fibo" / "__init__.py",
    Path("plugins") / "trade" / "fibo" / "_atomic.py",
    Path("plugins") / "trade" / "fibo" / "snapshot.py",
    Path("plugins") / "trade" / "fibo" / "store.py",
    Path("plugins") / "trade" / "fibo" / "session.py",
    Path("plugins") / "trade" / "fibo" / "flow.py",
    Path("plugins") / "trade" / "fibo" / "mt4_reader.py",
    # Phase 2.x additions: identity split (Phase 2.1), alias
    # memory + instrument translation (Phase 2.2), ranked candidate
    # discovery + price evidence (Phase 2.3).
    Path("plugins") / "trade" / "fibo" / "alias_memory.py",
    Path("plugins") / "trade" / "fibo" / "candidates.py",
    Path("plugins") / "trade" / "fibo" / "discovery.py",
    Path("plugins") / "trade" / "fibo" / "dryrun.py",
    Path("plugins") / "trade" / "fibo" / "reconciler.py",
    # Phase 2.8 — stateless target-convergence executor.
    Path("plugins") / "trade" / "fibo" / "executor.py",
    # Phase 2.9 — shadow-mode executor wiring (read-only).
    Path("plugins") / "trade" / "fibo" / "shadow.py",
    # Phase 2.10 — controlled live target convergence (allowlist-gated).
    Path("plugins") / "trade" / "fibo" / "live.py",
    # Phase 2.3 agent-side regression tests live under
    # plugins/trade/agents/tests. They are pure unit tests and are
    # copied alongside the agent so the verifier can import them
    # from the deployed tree.
    Path("plugins") / "trade" / "agents" / "tests" / "__init__.py",
    Path("plugins") / "trade" / "agents" / "tests"
    / "test_x_ondoperps_market_price.py",
]


def run(
    *,
    argv: Sequence[str],
    hermes_root: Path,
    hermes_home: Path,
    shared: Dict[str, Any],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Install /fibo capability payload into *hermes_root*.

    The caller (dispatcher) has already invoked
    ``installer_shared.install_shared`` for the shared agent layer; we
    only copy fibo-specific files.
    """
    plugin_root = hermes_root / "plugins" / "trade"
    record: Dict[str, Any] = {
        "copied_files": [],
        "dry_run": dry_run,
        "ok": True,
    }
    for rel in FIBO_REL_PATHS:
        try:
            rel_under_plugin_trade = rel.relative_to(Path("plugins") / "trade")
        except ValueError:
            rel_under_plugin_trade = rel
        src = REPO_ROOT / rel
        dst = plugin_root / rel_under_plugin_trade
        if not src.is_file():
            record["ok"] = False
            record.setdefault("missing", []).append(str(rel))
            continue
        if dry_run:
            record["copied_files"].append({"path": str(rel), "action": "would-copy"})
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            record["copied_files"].append({"path": str(rel), "action": "copied"})
    return record


__all__ = ["run", "FIBO_REL_PATHS"]