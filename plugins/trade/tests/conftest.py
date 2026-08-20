"""Session-level isolation for plugins/trade tests.

ROOT CAUSE (2026-08-19 audit)
-----------------------------
Many test modules historically did this at import time::

    # 1. strip the hermes-agent editable-install path hook
    # 2. sys.modules.pop every plugins.trade.* entry
    # 3. re-import from the repo tree

When pytest collected module A (binding TradeDesk / CanonicalResponse),
then module B (which popped and re-imported those names into *new*
module objects), module A's already-bound classes became stale.

Symptoms under a full-suite run:
- ``isinstance(resp, CanonicalResponse)`` → False even though both
  types print as ``plugins.trade.canonical.CanonicalResponse``
  (dual identity) → TradeDesk returns INVALID_AGENT_RESPONSE
- ``import plugins.trade.agents.x_*_agent`` →
  ``ImportError: cannot import name 'agents' from 'plugins.trade'``
  after a parent-package reload without the agents attribute
- Host ``LIGHTER_*`` credentials wiped by a module that popped them at
  import and never restored them → UNKNOWN_ACCOUNT in later Lighter tests
- ``HERMES_HOME`` permanently redirected to an empty temp dir by
  hibachi tests at import → dotenv-backed credential lookup broken

FIX
---
Do the path-hook strip + one-time sys.modules clear ONCE in this
conftest (loaded before any test module in this directory). Individual
test modules must NOT pop plugins.trade.* from sys.modules again.

Also:
- reset the TradeDesk process singleton between tests
- restore Lighter agent module knobs/caches
- snapshot/restore HERMES_HOME and LIGHTER_* env between tests
- keep synthetic LIGHTER_RH_* stubs available for offline Lighter tests
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# One-time session bootstrap (runs at conftest import, before test modules)
# ---------------------------------------------------------------------------
_EDITABLE_FINDER_MARKERS = (
    "__editable___hermes_agent_0_20_0_finder",
    "__editable___hermes_agent",
)

# 1. Strip the editable-install path hook so `plugins.*` resolves from
#    sys.path (repo) rather than the installed tree under
#    /usr/local/lib/hermes-agent/plugins/...
sys.path_hooks[:] = [
    h
    for h in sys.path_hooks
    if not any(marker in repr(h) for marker in _EDITABLE_FINDER_MARKERS)
]

# 2. Put the repo root first on sys.path.
_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent.parent.parent  # .../kam
_repo = str(_REPO_ROOT)
if _repo in sys.path:
    sys.path.remove(_repo)
sys.path.insert(0, _repo)

# 3. Drop any plugins.trade.* (and bare plugins, if it was the installed
#    namespace) already cached so the first real import loads from the repo.
for _key in list(sys.modules):
    if _key == "plugins" or _key.startswith("plugins."):
        if (
            _key == "plugins"
            or _key == "plugins.trade"
            or _key.startswith("plugins.trade.")
        ):
            if not _key.startswith("plugins.trade.tests"):
                sys.modules.pop(_key, None)

# 4. Snapshot host env that other modules must not permanently steal.
_SESSION_HERMES_HOME = os.environ.get("HERMES_HOME")
_SESSION_LIGHTER_ENV = {
    k: v for k, v in os.environ.items() if k.startswith("LIGHTER_")
}

# Synthetic offline Lighter account used by test_lighter_send_tx_batch.
# Ensure these exist even if a later module briefly clears LIGHTER_*.
_LIGHTER_RH_STUBS = {
    "LIGHTER_RH_CHAIN": "ROBINHOOD",
    "LIGHTER_RH_ACCOUNT_INDEX": "42",
    "LIGHTER_RH_APIKEY_INDEX": "7",
    "LIGHTER_RH_PUBLIC_KEY": "0x" + "ab" * 32,
    "LIGHTER_RH_PRIVATE_KEY": "0x" + "cd" * 32,
}
for _k, _v in _LIGHTER_RH_STUBS.items():
    os.environ.setdefault(_k, _v)
    _SESSION_LIGHTER_ENV.setdefault(_k, _v)


def _reset_tradedesk_singleton() -> None:
    """Clear the process-wide TradeDesk singleton if the module is loaded."""
    mod = sys.modules.get("plugins.trade.tradedesk")
    if mod is None:
        return
    if hasattr(mod, "_default_desk"):
        mod._default_desk = None


def _reset_lighter_module_knobs() -> None:
    """Restore x_lighter_agent module-level knobs mutated by individual tests."""
    mod = sys.modules.get("plugins.trade.agents.x_lighter_agent")
    if mod is None:
        return
    if hasattr(mod, "LIGHTER_VERIFY_ATTEMPTS"):
        mod.LIGHTER_VERIFY_ATTEMPTS = 4
    for cache_name in (
        "_LIGHTER_LIMITERS",
        "_LIGHTER_AUTH_TOKEN_CACHE",
        "_LIGHTER_L2_TX_BUDGETS",
    ):
        cache = getattr(mod, cache_name, None)
        if isinstance(cache, dict):
            cache.clear()


def _reset_hyperliquid_perp_dex_cache() -> None:
    """Clear the process-wide HIP-3 perp DEX name cache between tests.

    HIP-3 discovery memoizes the perp DEX list in a module-global to avoid
    repeated network calls. Individual tests patch ``_post_info`` and expect
    deterministic reads, so any live discovery earlier in the process must not
    leak a populated cache into a later test. Resetting the cache back to its
    initial ``None`` keeps each test's own ``_post_info`` fake authoritative.
    """
    mod = sys.modules.get("plugins.trade.agents.x_hyperliquid_agent")
    if mod is None:
        return
    if hasattr(mod, "_perp_dex_names_cache"):
        setattr(mod, "_perp_dex_names_cache", None)


def _restore_session_env() -> None:
    """Re-apply session-start HERMES_HOME + LIGHTER_* after noisy tests."""
    if _SESSION_HERMES_HOME is None:
        # Only clear if a test left a hibachi-style empty home behind.
        current = os.environ.get("HERMES_HOME")
        if current and "hibachi_empty_home_" in current:
            os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = _SESSION_HERMES_HOME

    # Ensure every session-start LIGHTER_* key is present again. Do not
    # delete keys tests may have added intentionally for the next test
    # within the same class — only fill holes.
    for key, value in _SESSION_LIGHTER_ENV.items():
        if key not in os.environ or not str(os.environ.get(key) or "").strip():
            os.environ[key] = value
    for key, value in _LIGHTER_RH_STUBS.items():
        os.environ.setdefault(key, value)


@pytest.fixture(autouse=True)
def _trade_test_isolation():
    """Per-test isolation that does not rebind module-level imports.

    - Resets the TradeDesk singleton so discovery state cannot leak.
    - Ensures ``plugins.trade.agents`` is importable as an attribute of
      ``plugins.trade`` (some tests do ``import plugins.trade.agents.x_...``
      after other imports of ``plugins.trade`` alone).
    - Restores Lighter agent module knobs/caches between tests.
    - Restores HERMES_HOME / LIGHTER_* holes left by other modules.
    """
    _reset_tradedesk_singleton()
    _reset_lighter_module_knobs()
    _reset_hyperliquid_perp_dex_cache()
    _restore_session_env()
    # Attach agents submodule if plugins.trade is already loaded without it.
    pt = sys.modules.get("plugins.trade")
    if pt is not None and not hasattr(pt, "agents"):
        try:
            import importlib

            importlib.import_module("plugins.trade.agents")
        except Exception:
            pass
    yield
    _reset_lighter_module_knobs()
    _restore_session_env()
    _reset_tradedesk_singleton()
