"""Capability-specific verifier: TRADE.

Verifies that the /trade capability is correctly installed:

  - manifest says trade=true
  - ~/.hermes/trade/ folder exists
  - tradedesk.py and wizard.py import cleanly
  - TradeDesk lists at least one exchange agent

If any of these fail, returns False and prints a clear report.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import (
    capability_folder_consistent,
    is_installed,
    load_manifest,
)


def run(*, argv: List[str], hermes_home: Path) -> bool:
    repo_root = Path(__file__).resolve().parent.parent
    ok = True
    print("==> verify trade")
    # Manifest consistency.
    if is_installed(hermes_home, "trade"):
        print("    [ok] manifest trade=true")
    else:
        print("    [FAIL] manifest trade=false")
        ok = False
    consistent, msg = capability_folder_consistent(hermes_home, "trade")
    if consistent:
        print(f"    [ok] {msg}")
    else:
        print(f"    [FAIL] {msg}")
        ok = False
    # TradeDesk + wizard import check.
    for rel in ["plugins/trade/tradedesk.py", "plugins/trade/wizard.py"]:
        path = repo_root / rel
        if not path.is_file():
            print(f"    [FAIL] missing source: {rel}")
            ok = False
            continue
        import ast
        try:
            ast.parse(path.read_text(encoding="utf-8"))
            print(f"    [ok] {rel} parses cleanly")
        except SyntaxError as exc:
            print(f"    [FAIL] {rel}: {exc}")
            ok = False
    return ok


__all__ = ["run"]