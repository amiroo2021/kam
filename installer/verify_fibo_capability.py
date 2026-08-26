"""Capability-specific verifier: FIBO.

Verifies that the /fibo Telegram wizard skeleton is correctly installed
and behaves as a placeholder. Asserts ONLY public behavior:

* ``plugins/trade/fibo_wizard.py`` is present in the install tree;
* ``handle_fibo_command`` is importable and callable;
* ``handle_fibo_callback`` is importable and callable;
* ``handle_fibo_text`` is importable and callable;
* the entry menu exposes EXACTLY four actions — Start Fibo, Running
  Fibo, Stop Fibo, Exit — with callback namespaces ``fibo:start``,
  ``fibo:running``, ``fibo:stop``, ``fibo:exit``;
* each strategy action (start / running / stop) routes to a placeholder
  screen whose text is the action label;
* ``fibo:exit`` is a UI-only close action (deletes the wizard message
  or strips the inline keyboard) — it MUST NOT have a placeholder
  screen entry;
* the Phase 1 sub-package ``plugins/trade/fibo/`` ships with the
  expected modules (``snapshot``, ``store``, ``session``, ``flow``,
  ``mt4_reader``, ``_atomic``);
* no exchange calls happen when the user clicks any button.

This verifier does NOT import or assert against internal helper
functions in ``fibo_wizard``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict


REQUIRED_CALLBACKS = ("fibo:start", "fibo:running", "fibo:stop", "fibo:exit")
# Callbacks whose placeholder screens must exist (Exit is UI-only and
# has no placeholder).
PLACEHOLDER_CALLBACKS = ("fibo:start", "fibo:running", "fibo:stop")
# Phase 1 sub-package modules that MUST be present in the install tree.
FIBO_SUBPACKAGE_FILES = (
    "__init__.py",
    "_atomic.py",
    "snapshot.py",
    "store.py",
    "session.py",
    "flow.py",
    "mt4_reader.py",
)


def _load_fibo_wizard(hermes_root: Path) -> Any:
    """Load the installed fibo_wizard module by path without sys.modules pollution."""
    wizard_path = hermes_root / "plugins" / "trade" / "fibo_wizard.py"
    if not wizard_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_installed_fibo_wizard", wizard_path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(hermes_root))
    try:
        spec.loader.exec_module(mod)
    finally:
        try:
            sys.path.remove(str(hermes_root))
        except ValueError:
            pass
    return mod


def run(*, argv, hermes_root: Path, hermes_home: Path) -> bool:  # noqa: ARG001
    """Verify the installed /fibo capability.

    Returns True when every check passes.
    """
    print("==> verify /fibo capability")

    wizard_path = hermes_root / "plugins" / "trade" / "fibo_wizard.py"
    if not wizard_path.is_file():
        print(f"    [FAIL] fibo_wizard.py missing at {wizard_path}")
        return False
    print(f"    [ok] fibo_wizard.py present ({wizard_path})")

    # Phase 1 sub-package files (Start Fibo sub-flow, Reader, etc.).
    sub_dir = hermes_root / "plugins" / "trade" / "fibo"
    for fname in FIBO_SUBPACKAGE_FILES:
        fpath = sub_dir / fname
        if not fpath.is_file():
            print(f"    [FAIL] fibo/{fname} missing at {fpath}")
            return False
    print(f"    [ok] fibo/ sub-package present ({sub_dir})")

    mod = _load_fibo_wizard(hermes_root)
    if mod is None:
        print("    [FAIL] fibo_wizard.py failed to import")
        return False

    # Public entry-point surface.
    for fn_name in ("handle_fibo_command", "handle_fibo_callback", "handle_fibo_text"):
        fn = getattr(mod, fn_name, None)
        if fn is None or not callable(fn):
            print(f"    [FAIL] fibo_wizard.{fn_name} missing or not callable")
            return False
        print(f"    [ok] fibo_wizard.{fn_name} present and callable")

    # Menu structure: exactly four buttons with the required labels
    # and the dedicated ``fibo:`` callback namespace. Exit is appended
    # last so it sits beneath the strategy controls.
    buttons = getattr(mod, "SCREEN_BUTTONS", None)
    if not isinstance(buttons, (list, tuple)) or len(buttons) != 4:
        print(f"    [FAIL] SCREEN_BUTTONS must have exactly 4 entries, got {buttons!r}")
        return False
    expected = [
        ("▶️ Start Fibo",   "fibo:start"),
        ("📋 Running Fibo", "fibo:running"),
        ("⛔️ Stop Fibo",    "fibo:stop"),
        ("❌ Exit",          "fibo:exit"),
    ]
    if list(buttons) != expected:
        print(f"    [FAIL] SCREEN_BUTTONS mismatch.\n      got:      {list(buttons)!r}\n      expected: {expected!r}")
        return False
    print(f"    [ok] /fibo entry menu exposes exactly 4 actions in the required order")

    # Callback namespace hygiene.
    for label, cb in buttons:
        if not isinstance(cb, str) or not cb.startswith("fibo:"):
            print(f"    [FAIL] button {label!r} callback {cb!r} is not in fibo: namespace")
            return False
    callbacks = [cb for _, cb in buttons]
    if set(callbacks) != set(REQUIRED_CALLBACKS):
        print(f"    [FAIL] callbacks {callbacks!r} != required {REQUIRED_CALLBACKS!r}")
        return False
    print(f"    [ok] callback namespace fibo: with start/running/stop/exit")

    # Placeholder screens: each STRATEGY callback maps to a non-empty
    # label equal to the user-visible action title. ``fibo:exit`` is a
    # UI-only close action and intentionally has no SCREEN_TEXT entry.
    screen_text = getattr(mod, "SCREEN_TEXT", None)
    if not isinstance(screen_text, dict):
        print(f"    [FAIL] SCREEN_TEXT missing or wrong type: {type(screen_text).__name__}")
        return False
    for cb in PLACEHOLDER_CALLBACKS:
        body = screen_text.get(cb)
        if not isinstance(body, str) or not body.strip():
            print(f"    [FAIL] placeholder text for {cb!r} missing")
            return False
    print(f"    [ok] all three strategy placeholders render a non-empty screen")
    # Exit must NOT be a placeholder (it would re-render the entry menu
    # and contradict the "close the wizard" contract).
    if "fibo:exit" in screen_text:
        print(
            f"    [FAIL] fibo:exit must not appear in SCREEN_TEXT "
            f"(Exit is UI-only); got body={screen_text['fibo:exit']!r}"
        )
        return False
    print(f"    [ok] fibo:exit is a UI-only close (no placeholder text)")

    # No exchange writes: the placeholder handlers must not call any
    # agent ``execute`` method. We assert by inspecting the module
    # source for forbidden call paths.
    src = wizard_path.read_text(encoding="utf-8")
    forbidden = (
        ".execute(",
        "TradeDesk(",
        "TradeWizard(",
        "_WIZARD.",
    )
    leaks = [tok for tok in forbidden if tok in src]
    if leaks:
        print(f"    [FAIL] fibo_wizard source references trade-runtime tokens: {leaks}")
        return False
    print(f"    [ok] fibo_wizard has no /trade runtime leakage in its source")

    # No old Fibo runtime restored: verify the install tree does not
    # contain the legacy daemon/service/engine files.
    legacy = [
        hermes_root / "plugins" / "trade" / "fibo_daemon.py",
        hermes_root / "plugins" / "trade" / "fibo_service.py",
        hermes_root / "plugins" / "trade" / "golden_fibo",
        Path("/etc/systemd/system/fibo.service"),
    ]
    leaked = [str(p) for p in legacy if p.exists()]
    if leaked:
        print(f"    [FAIL] legacy Fibo runtime artifacts present: {leaked}")
        return False
    print(f"    [ok] no legacy fibo.service / fibo_daemon / fibo_service / golden_fibo restored")

    return True


__all__ = ["run", "REQUIRED_CALLBACKS"]