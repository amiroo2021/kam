"""Capability-specific verifier: FIBO.

Verifies that the /fibo capability is correctly installed:

  - manifest says fibo=true
  - ~/.hermes/fibo/ folder exists
  - fibo_service.py, fibo_daemon.py, fibo_wizard.py AST-parse cleanly
  - all golden_fibo/* modules AST-parse cleanly
  - the fibo.service template is present in the repo
  - no requirement that /trade exists

Takes EXPLICIT ``hermes_root`` and ``hermes_home``. The two are
independent.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import (
    capability_folder_consistent,
    is_installed,
)


FIBO_IMPORT_PROBES = [
    "plugins/trade/fibo_service",
    "plugins/trade/fibo_daemon",
    "plugins/trade/fibo_wizard",
    "plugins/trade/golden_fibo/config",
    "plugins/trade/golden_fibo/engine",
    "plugins/trade/golden_fibo/lighter_adapter",
    "plugins/trade/golden_fibo/preflight",
    "plugins/trade/golden_fibo/state",
]


def _try_import(module_name: str, repo_root: Path) -> bool:
    """Read-only verification probe.

    AST-parse only (does not execute the module body). The full module
    execution check is the job of the existing ``verify_trade.py`` runner
    which is invoked by the proven monolithic installer and runs under the
    Hermes venv where the full plugin tree is importable.

    Returns True iff the file exists and parses as valid Python.
    """
    if module_name in ("plugins.trade.agents", "plugins/trade/agents"):
        init_path = repo_root / "plugins" / "trade" / "agents" / "__init__.py"
        return init_path.is_file()
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
) -> bool:
    repo_root = Path(__file__).resolve().parent.parent
    ok = True
    print("==> verify fibo")
    if is_installed(hermes_home, "fibo"):
        print("    [ok] manifest fibo=true")
    else:
        print("    [FAIL] manifest fibo=false")
        ok = False
    consistent, msg = capability_folder_consistent(hermes_home, "fibo")
    if consistent:
        print(f"    [ok] {msg}")
    else:
        print(f"    [FAIL] {msg}")
        ok = False
    for probe in FIBO_IMPORT_PROBES:
        if _try_import(probe, repo_root):
            print(f"    [ok] import: {probe}")
        else:
            print(f"    [FAIL] import: {probe}")
            ok = False
    template = repo_root / "installer" / "fibo.service.template"
    if template.is_file():
        print(f"    [ok] fibo.service.template present: {template}")
    else:
        print(f"    [FAIL] fibo.service.template missing: {template}")
        ok = False
    return ok


__all__ = ["run", "FIBO_IMPORT_PROBES"]