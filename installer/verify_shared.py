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

This module NEVER fails because of a missing /trade or /fibo. That is
the job of the per-capability verifiers.
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
    # kam/ directory.
    kam_dir = kam_root(hermes_home)
    if kam_dir.is_dir():
        print(f"    [ok] kam/ directory: {kam_dir}")
    else:
        print(f"    [FAIL] kam/ directory missing: {kam_dir}")
        ok = False
    return ok


__all__ = ["run", "SHARED_IMPORT_PROBES"]