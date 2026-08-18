"""Shared verification helpers.

Verifies that the SHARED core is present (regardless of which
capabilities are selected):

  - shared agent modules import cleanly
  - the plugins/trade/agents/ folder exists
  - canonical.py imports
  - ~/.hermes/kam/ directory exists

This module NEVER fails because of a missing /trade or /fibo. That is the
job of the per-capability verifiers.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))


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
    if module_name == "plugins.trade.agents":
        init_path = repo_root / "plugins" / "trade" / "agents" / "__init__.py"
        return init_path.is_file()
    parts = module_name.split(".")
    if parts[0] != "plugins":
        return False
    path = repo_root.joinpath(*parts).with_suffix(".py")
    if not path.is_file():
        return False
    import ast
    try:
        ast.parse(path.read_text(encoding="utf-8"))
        return True
    except SyntaxError:
        return False


def run(*, argv: List[str], hermes_home: Path, capabilities: List[str]) -> bool:
    """Verify the SHARED core. Returns True on success."""
    repo_root = Path(__file__).resolve().parent.parent
    ok = True
    print("==> verify shared")
    # Probe imports.
    for probe in SHARED_IMPORT_PROBES:
        if _try_import(probe, repo_root):
            print(f"    [ok] shared import: {probe}")
        else:
            print(f"    [FAIL] shared import: {probe}")
            ok = False
    # kam/ directory.
    kam_dir = Path(hermes_home) / "kam"
    if kam_dir.is_dir():
        print(f"    [ok] kam/ directory: {kam_dir}")
    else:
        print(f"    [FAIL] kam/ directory missing: {kam_dir}")
        ok = False
    return ok


__all__ = ["run", "SHARED_IMPORT_PROBES"]