"""Capability-specific verifier: TRADE.

Verifies that the /trade capability is correctly installed:

  - manifest says trade=true
  - ~/.hermes/trade/ folder exists
  - tradedesk.py and wizard.py AST-parse cleanly
  - trademenu package present under hermes-root
  - trade-web.service contract + TRADE_WEB_PASSWORD + optional live health

Takes EXPLICIT ``hermes_root`` and ``hermes_home``. The two are
independent.

If any of these fail, returns False and prints a clear report.
Never prints secret values.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import (  # noqa: E402
    capability_folder_consistent,
    is_installed,
    load_manifest,
)
from trade_web_unit import verify_trade_web_unit  # noqa: E402


def run(
    *,
    argv: Sequence[str],
    hermes_root: Path,
    hermes_home: Path,
    systemd_dir: Path | None = None,
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
    for rel in [
        "plugins/trade/tradedesk.py",
        "plugins/trade/wizard.py",
        "plugins/trade/trademenu/__main__.py",
        "plugins/trade/trademenu/app.py",
        "plugins/trade/candles.py",
        "plugins/trade/instrument_picker.py",
        "plugins/trade/ladder_math.py",
    ]:
        path = hermes_root / rel
        if not path.is_file():
            path = repo_root / rel
        if not path.is_file():
            print(f"    [FAIL] missing installed/source: {rel}")
            ok = False
            continue
        if path.suffix == ".py":
            try:
                ast.parse(path.read_text(encoding="utf-8"))
                print(f"    [ok] {rel} present/parses ({path})")
            except SyntaxError as exc:
                print(f"    [FAIL] {rel}: {exc}")
                ok = False
        else:
            print(f"    [ok] {rel} present ({path})")

    # Import trade-web package with hermes-root first on sys.path
    print("==> verify trade-web import")
    sys.path.insert(0, str(hermes_root))
    for name in [m for m in list(sys.modules) if m.startswith("plugins.trade")]:
        del sys.modules[name]
    try:
        importlib.import_module("plugins.trade.trademenu")
        print("    [ok] import plugins.trade.trademenu")
    except Exception as exc:  # noqa: BLE001
        print(f"    [FAIL] import plugins.trade.trademenu: {type(exc).__name__}: {exc}")
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

    print("==> verify trade-web unit / password / health")
    sd = systemd_dir if systemd_dir is not None else Path("/etc/systemd/system")
    for name, passed, detail in verify_trade_web_unit(
        hermes_root=hermes_root,
        hermes_home=hermes_home,
        systemd_dir=sd,
        require_active_health=True,
    ):
        mark = "ok" if passed else "FAIL"
        print(f"    [{mark}] {name} - {detail}")
        if not passed:
            ok = False
    return ok


__all__ = ["run"]
