"""Capability-specific verifier: TRADE.

Verifies that the /trade capability is correctly installed:

  - manifest says trade=true
  - ~/.hermes/trade/ folder exists
  - tradedesk.py and wizard.py AST-parse cleanly

Takes EXPLICIT ``hermes_root`` and ``hermes_home``. The two are
independent.

If any of these fail, returns False and prints a clear report.
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
    load_manifest,
)


def run(
    *,
    argv: Sequence[str],
    hermes_root: Path,
    hermes_home: Path,
) -> bool:
    repo_root = Path(__file__).resolve().parent.parent
    ok = True
    print("==> verify trade")
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
    for rel in ["plugins/trade/tradedesk.py", "plugins/trade/wizard.py"]:
        # Prefer installed tree; fall back to repo source for dry/dev.
        path = hermes_root / rel
        if not path.is_file():
            path = repo_root / rel
        if not path.is_file():
            print(f"    [FAIL] missing installed/source: {rel}")
            ok = False
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"))
            print(f"    [ok] {rel} parses cleanly ({path})")
        except SyntaxError as exc:
            print(f"    [FAIL] {rel}: {exc}")
            ok = False
    # Telegram adapter dispatch (installed tree only — this is the Lodo gate).
    from adapter_wiring import TRADE_ADAPTER_SENTINELS
    from patchspecs import TELEGRAM_ADAPTER

    adapter = hermes_root / TELEGRAM_ADAPTER
    if not adapter.is_file():
        print(f"    [FAIL] missing Telegram adapter at {adapter}")
        ok = False
    else:
        text = adapter.read_text(encoding="utf-8")
        for kind, needle in TRADE_ADAPTER_SENTINELS.items():
            if needle in text:
                print(f"    [ok] adapter trade {kind} seam")
            else:
                print(f"    [FAIL] adapter trade {kind} seam missing")
                ok = False
    return ok


__all__ = ["run"]