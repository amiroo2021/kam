"""Shared verification helpers.

Verifies that the SHARED core is present (regardless of which
capabilities are selected):

  - shared agent modules exist and AST-parse
  - the plugins/trade/agents/ folder exists
  - canonical.py AST-parses
  - ~/.hermes/kam/ directory exists

Takes EXPLICIT ``hermes_root`` and ``hermes_home``. The two are
independent (the application tree is at hermes_root; the persistent
state is at hermes_home).

This module NEVER fails because of a missing /trade. That is
the job of the per-capability verifier.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import (
    KNOWN_CAPABILITIES,
    kam_root,
)


SHARED_IMPORT_PROBES = [
    "plugins.trade.canonical",
    "plugins.trade.agents",
]


def _try_import(module_name: str, repo_root: Path) -> bool:
    """Read-only verification probe.

    AST-parse only (does not execute the module body). The full module
    execution check is the job of the existing ``verify_trade.py`` runner
    which is invoked by the proven monolithic installer and runs under the
    Hermes venv where the full plugin tree is importable.

    Returns True iff the file exists and parses as valid Python.
    """
    if module_name == "plugins.trade.agents" or module_name == "plugins/trade/agents":
        init_path = repo_root / "plugins" / "trade" / "agents" / "__init__.py"
        return init_path.is_file()
    # Accept both dot-style and slash-style paths.
    parts = module_name.replace("/", ".").split(".")
    if parts[0] != "plugins":
        return False
    path = repo_root.joinpath(*parts).with_suffix(".py")
    if not path.is_file():
        return False
    try:
        ast.parse(path.read_text(encoding="utf-8"))
        return True
    except SyntaxError:
        return False


def run(
    *,
    argv: Sequence[str],
    hermes_root: Path,
    hermes_home: Path,
    capabilities: List[str],
    dry_run: bool = False,
) -> bool:
    """Verify the SHARED core. Returns True on success."""
    repo_root = Path(__file__).resolve().parent.parent
    ok = True
    print("==> verify shared")
    for probe in SHARED_IMPORT_PROBES:
        if _try_import(probe, repo_root):
            print(f"    [ok] shared import: {probe}")
        else:
            print(f"    [FAIL] shared import: {probe}")
            ok = False
    # Every shipped exchange agent must be present under the *installed*
    # hermes_root (Lodo partial-agent regression).
    from installer_shared import SHARED_REL_PATHS

    expected_agents = [
        rel for rel in SHARED_REL_PATHS
        if rel.name.startswith("x_") and rel.name.endswith("_agent.py")
    ]
    agents_dir = hermes_root / "plugins" / "trade" / "agents"
    if not agents_dir.is_dir():
        print(f"    [FAIL] missing agents dir: {agents_dir}")
        ok = False
    else:
        installed = {p.name for p in agents_dir.glob("x_*_agent.py")}
        expected_names = {rel.name for rel in expected_agents}
        missing = sorted(expected_names - installed)
        extra = sorted(installed - expected_names)
        if missing:
            print(f"    [FAIL] missing agent files under {agents_dir}: {missing}")
            ok = False
        else:
            print(f"    [ok] all {len(expected_names)} exchange agents installed: {sorted(expected_names)}")
        if extra:
            print(f"    [note] extra agent files present: {extra}")
        # AST-parse each *present* installed agent (import may need SDKs).
        for name in sorted(expected_names & installed):
            path = agents_dir / name
            try:
                ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                print(f"    [FAIL] {name} syntax: {exc}")
                ok = False
            except OSError as exc:
                print(f"    [FAIL] {name} read: {exc}")
                ok = False

    # Hermes PluginManager only discovers directories that contain plugin.yaml.
    # Payload + __init__.py alone is NOT enough for slash-command registration.
    plugin_yaml = hermes_root / "plugins" / "trade" / "plugin.yaml"
    if plugin_yaml.is_file():
        text = plugin_yaml.read_text(encoding="utf-8", errors="replace")
        if "name:" in text and "trade" in text:
            print(f"    [ok] plugin.yaml present for discovery: {plugin_yaml}")
        else:
            print(f"    [FAIL] plugin.yaml missing name: trade marker: {plugin_yaml}")
            ok = False
    else:
        print(
            f"    [FAIL] missing {plugin_yaml} "
            "(Hermes will not discover plugins.trade → empty get_plugin_commands)"
        )
        ok = False
    # kam/ directory.
    kam_dir = kam_root(hermes_home)
    if kam_dir.is_dir():
        print(f"    [ok] kam/ directory: {kam_dir}")
    else:
        print(f"    [FAIL] kam/ directory missing: {kam_dir}")
        ok = False
    return ok


__all__ = ["run", "SHARED_IMPORT_PROBES"]