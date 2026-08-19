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
    systemd_dir: Optional[Path] = None,
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
        # Prefer installed tree under hermes_root.
        installed = hermes_root / probe
        if not installed.with_suffix(".py").is_file() and not installed.is_file():
            # fall back to repo AST probe
            if _try_import(probe, repo_root):
                print(f"    [ok] import(source): {probe}")
            else:
                print(f"    [FAIL] import: {probe}")
                ok = False
            continue
        path = installed if installed.is_file() else installed.with_suffix(".py")
        try:
            ast.parse(path.read_text(encoding="utf-8"))
            print(f"    [ok] import(installed): {probe}")
        except SyntaxError:
            print(f"    [FAIL] import: {probe}")
            ok = False
    template = repo_root / "installer" / "fibo.service.template"
    if template.is_file():
        print(f"    [ok] fibo.service.template present: {template}")
    else:
        print(f"    [FAIL] fibo.service.template missing: {template}")
        ok = False
    # Telegram adapter dispatch (installed tree — Lodo gate).
    from adapter_wiring import FIBO_ADAPTER_SENTINELS
    from patchspecs import TELEGRAM_ADAPTER

    adapter = hermes_root / TELEGRAM_ADAPTER
    if not adapter.is_file():
        print(f"    [FAIL] missing Telegram adapter at {adapter}")
        ok = False
    else:
        text = adapter.read_text(encoding="utf-8")
        for kind, needle in FIBO_ADAPTER_SENTINELS.items():
            if needle in text:
                print(f"    [ok] adapter fibo {kind} seam")
            else:
                print(f"    [FAIL] adapter fibo {kind} seam missing")
                ok = False

    # Real systemd unit must exist (Lodo: template-only was insufficient).
    from fibo_unit import DEFAULT_SYSTEMD_DIR, is_real_systemd_dir, verify_fibo_service_unit

    sd = Path(systemd_dir) if systemd_dir is not None else DEFAULT_SYSTEMD_DIR
    unit_ok, unit_msgs = verify_fibo_service_unit(
        hermes_root=hermes_root,
        hermes_home=hermes_home,
        systemd_dir=sd,
        require_active=is_real_systemd_dir(sd),
    )
    for m in unit_msgs:
        print(f"    {m}")
    if not unit_ok:
        ok = False
    return ok


__all__ = ["run", "FIBO_IMPORT_PROBES"]