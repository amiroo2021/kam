"""TradeDesk — exchange-agnostic dispatcher for the /trade wizard.

TradeDesk is responsible for:

1. **Agent discovery** — scanning the ``agents/`` directory for files
   matching ``x_<exchange>_agent.py`` and dynamically importing them.
2. **Listing exchanges** — ``list_exchanges()`` returns the canonical
   exchange names of all loaded agents.
3. **Listing accounts** — ``list_accounts(exchange)`` delegates to the
   resolved agent.
4. **Routing operations** — ``execute(request)`` delegates to the
   resolved agent and returns its canonical response.

TradeDesk deliberately contains NO exchange-specific code. It does NOT
know about Hyperliquid, Rise, Pacifica, or any other exchange field set,
credential naming convention, or rounding rule. All exchange-native
behavior lives in the corresponding ``x_<exchange>_agent.py`` module.

The presence of an agent file determines whether the exchange is
available. We do NOT maintain a hard-coded registry.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .canonical import (
    CanonicalResponse,
    make_failure,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent discovery
# ---------------------------------------------------------------------------

# Filename convention: x_<exchange>_agent.py.
# Exchange name must start with a letter and contain only lowercase
# letters, digits, and underscores. Aliases like "x_42_invalid_agent.py"
# (starting with a digit) would not match — a deliberate restriction
# so test/scratch files don't accidentally become "agents".
_AGENT_FILENAME_PATTERN = re.compile(r"^x_(?P<exchange>[a-z][a-z0-9_]*_agent)\.py$")

# Files in the agents/ directory that are NOT agents. Excluded safely.
_EXCLUDED_PATTERNS = (
    re.compile(r"^__init__\.py$"),
    re.compile(r"^[._].+"),  # dotfiles / private modules
)

# Required contract on each agent module. We use duck typing intentionally
# — no abstract base class imposed — so an agent can be a simple module
# or a richer class-based implementation.
_REQUIRED_AGENT_ATTRS = (
    "name",
    "list_accounts",
    "capabilities",
    "execute",
)


def _agents_dir() -> Path:
    """Return the directory where x_*_agent.py files are located."""
    return Path(__file__).resolve().parent / "agents"


def _iter_agent_files(directory: Path) -> List[Path]:
    """List all candidate agent files in ``directory``, excluding tests,
    support modules, and dotfiles. Deterministic order — sorted by name."""
    if not directory.is_dir():
        return []
    candidates: List[Path] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        if not path.name.endswith(".py"):
            continue
        if any(pattern.match(path.name) for pattern in _EXCLUDED_PATTERNS):
            continue
        if not _AGENT_FILENAME_PATTERN.match(path.name):
            continue
        candidates.append(path)
    return candidates


def _exchange_name_from_filename(filename: str) -> Optional[str]:
    """Extract the exchange name from an ``x_<name>_agent.py`` filename.

    Returns the bare exchange name (e.g. ``"hyperliquid"`` from
    ``"x_hyperliquid_agent.py"``), or None if the filename doesn't
    match the canonical pattern.
    """
    match = _AGENT_FILENAME_PATTERN.match(filename)
    if not match:
        return None
    # The regex captures ``<name>_agent``; strip the trailing "_agent".
    captured = match.group("exchange")
    if not captured.endswith("_agent"):
        return None
    return captured[: -len("_agent")]


def _load_agent_module(path: Path) -> Optional[Any]:
    """Import a single agent module by file path.

    Returns the module object on success, or None on failure (with a
    logger.warning). Import errors are not fatal — a broken agent must
    not crash the wizard.
    """
    exchange = _exchange_name_from_filename(path.name)
    if exchange is None:
        return None

    # Build a stable module name: plugins.trade.agents.x_<exchange>_agent
    package_name = __name__.rsplit(".", 1)[0]  # "plugins.trade"
    module_name = f"{package_name}.agents.{path.stem}"

    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            logger.warning("Cannot build import spec for %s", path)
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception as exc:  # noqa: BLE001 — discovery must keep going
        logger.warning("Failed to load agent %s: %s", path.name, exc)
        # Drop any half-imported entry so we don't leave a broken stub.
        sys.modules.pop(module_name, None)
        return None


def _validate_agent(module: Any) -> Optional[str]:
    """Verify that the loaded module exposes the required contract.

    Returns None if valid, or a human-readable reason string if not.
    """
    if module is None:
        return "module is None"
    for attr in _REQUIRED_AGENT_ATTRS:
        if not hasattr(module, attr):
            return f"missing required attribute {attr!r}"
    name = getattr(module, "name", None)
    if not isinstance(name, str) or not name.strip():
        return "name must be a non-empty string"
    return None


# ---------------------------------------------------------------------------
# Public TradeDesk interface
# ---------------------------------------------------------------------------

class TradeDesk:
    """Exchange-agnostic dispatcher.

    Holds a cached mapping of exchange name -> loaded agent module. The
    cache is initialized lazily on first use so that import errors
    don't block unrelated code.
    """

    def __init__(self) -> None:
        self._agents: Dict[str, Any] = {}
        self._loaded: bool = False

    # -- discovery --------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        for path in _iter_agent_files(_agents_dir()):
            exchange = _exchange_name_from_filename(path.name)
            module = _load_agent_module(path)
            reason = _validate_agent(module)
            if reason is not None:
                logger.warning(
                    "Skipping agent %s: %s", path.name, reason
                )
                continue
            # Trust the module's self-declared name as the canonical
            # exchange identifier. The filename pattern only constrains
            # which files we consider; the agent's own ``name`` is what
            # the rest of the system addresses it by.
            self._agents[module.name] = module
            logger.debug(
                "TradeDesk registered agent: %s (from %s)",
                module.name, path.name,
            )

    def list_exchanges(self) -> List[str]:
        """Return sorted list of currently loaded exchange names."""
        self._ensure_loaded()
        return sorted(self._agents.keys())

    # -- per-exchange operations -----------------------------------------

    def list_accounts(self, exchange: str) -> List[Any]:
        """Return normalized account entries for ``exchange``.

        Delegates to the agent's ``list_accounts()``. Returns an empty
        list if the exchange is unknown or the agent raises.
        """
        self._ensure_loaded()
        agent = self._agents.get(exchange)
        if agent is None:
            return []
        try:
            accounts = agent.list_accounts()
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_accounts(%s) failed: %s", exchange, exc)
            return []
        if not isinstance(accounts, list):
            return []

        normalized: List[Any] = []
        seen_strings: set[str] = set()
        seen_structured: set[tuple[str, str]] = set()
        for entry in accounts:
            if isinstance(entry, str):
                alias = entry.strip()
                if not alias or alias in seen_strings:
                    continue
                seen_strings.add(alias)
                normalized.append(alias)
                continue
            if isinstance(entry, dict):
                alias = str(entry.get("account", "")).strip()
                if not alias:
                    continue
                label = str(entry.get("label", alias)).strip() or alias
                chain = str(entry.get("chain", "")).strip()
                key = (alias, chain)
                if key in seen_structured:
                    continue
                seen_structured.add(key)
                item = {"account": alias, "label": label}
                if chain:
                    item["chain"] = chain
                normalized.append(item)

        def _sort_key(item: Any) -> tuple[str, str]:
            if isinstance(item, str):
                return (item.lower(), item.lower())
            if isinstance(item, dict):
                return (str(item.get("account", "")).lower(), str(item.get("chain", "")).lower())
            return ("", "")

        return sorted(normalized, key=_sort_key)

    def capabilities(self, exchange: str) -> List[str]:
        """Return the list of operations the agent supports."""
        self._ensure_loaded()
        agent = self._agents.get(exchange)
        if agent is None:
            return []
        try:
            caps = agent.capabilities()
        except Exception as exc:  # noqa: BLE001
            logger.warning("capabilities(%s) failed: %s", exchange, exc)
            return []
        if not isinstance(caps, list):
            return []
        return [c for c in caps if isinstance(c, str)]

    def ladder_max_orders_per_instrument(self, exchange: str) -> Optional[int]:
        """Return the exchange's per-instrument open-order cap, or None.

        Surfaced for the wizard's ladder order-count screen so the
        operator can see ``MAX ORDERS PER INSTRUMENT = N`` before typing
        a ladder size. **Informational only** — the wizard does not
        clamp, reject, or otherwise act on this number.

        Returns ``None`` when:

        - ``exchange`` is not a registered agent (unknown / typo'd);
        - the agent does not expose ``ladder_max_orders_per_instrument``;
        - the accessor raises for any reason (logged at WARNING).

        Callers should treat ``None`` as "unknown — show ``?`` in the
        wizard" rather than as an error.
        """
        self._ensure_loaded()
        agent = self._agents.get(exchange)
        if agent is None:
            return None
        accessor = getattr(agent, "ladder_max_orders_per_instrument", None)
        if not callable(accessor):
            return None
        try:
            value = accessor()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ladder_max_orders_per_instrument(%s) failed: %s", exchange, exc
            )
            return None
        if isinstance(value, bool):
            # ``bool`` is a subclass of ``int`` in Python; reject ``True``
            # (which would otherwise pass the ``isinstance(value, int)``
            # check and mean "1 order"). Operators should never enable
            # a per-instrument cap at 1.
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        return None

    def execute(self, request: Dict[str, Any]) -> CanonicalResponse:
        """Dispatch a canonical request to the resolved agent.

        Expected request keys: ``operation``, ``exchange``, ``account``.
        Anything else is forwarded to the agent as-is. Returns a
        CanonicalResponse regardless of exchange availability.
        """
        operation = request.get("operation") if isinstance(request, dict) else None
        exchange = request.get("exchange") if isinstance(request, dict) else None
        account = request.get("account") if isinstance(request, dict) else None

        if not operation:
            return make_failure(
                operation="",
                exchange=exchange or "",
                account=account or "",
                code="INVALID_REQUEST",
                message="Missing 'operation' in request.",
            )
        if not exchange:
            return make_failure(
                operation=operation,
                exchange="",
                account=account or "",
                code="MISSING_EXCHANGE",
                message="Missing 'exchange' in request.",
            )
        if not account:
            return make_failure(
                operation=operation,
                exchange=exchange,
                account="",
                code="MISSING_ACCOUNT",
                message="Missing 'account' in request.",
            )

        self._ensure_loaded()
        agent = self._agents.get(exchange)
        if agent is None:
            return make_failure(
                operation=operation,
                exchange=exchange,
                account=account,
                code="UNKNOWN_EXCHANGE",
                message=f"Exchange '{exchange}' is not available.",
            )

        # Delegate to the agent. Any exception is caught and wrapped in
        # a canonical error — TradeDesk never lets an exchange-native
        # exception escape.
        try:
            response = agent.execute(request)
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation=operation,
                exchange=exchange,
                account=account,
                code="AGENT_EXCEPTION",
                message=sanitize_error_message(str(exc)),
            )

        if not isinstance(response, CanonicalResponse):
            return make_failure(
                operation=operation,
                exchange=exchange,
                account=account,
                code="INVALID_AGENT_RESPONSE",
                message="Agent returned a malformed response.",
            )

        return response


# A single module-level instance is enough — TradeDesk is stateless.
_default_desk: Optional[TradeDesk] = None


def get_tradedesk() -> TradeDesk:
    """Return the process-wide TradeDesk singleton."""
    global _default_desk
    if _default_desk is None:
        _default_desk = TradeDesk()
    return _default_desk


__all__ = [
    "TradeDesk",
    "get_tradedesk",
]
