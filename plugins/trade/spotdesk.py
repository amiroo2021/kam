"""SpotDesk — spot-agent dispatcher for the /tradespot wizard.

Spot agents are deliberately separate from the existing /trade agents.
Only files named ``x_<exchange>_agent_spot.py`` are considered here;
normal ``x_<exchange>_agent.py`` files are ignored.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .canonical import CanonicalResponse, make_failure, sanitize_error_message

logger = logging.getLogger(__name__)

_SPOT_AGENT_FILENAME_PATTERN = re.compile(
    r"^x_(?P<exchange>[a-z][a-z0-9_]*)_agent_spot\.py$"
)
_EXCLUDED_PATTERNS = (
    re.compile(r"^__init__\.py$"),
    re.compile(r"^[._].+"),
)
_REQUIRED_AGENT_ATTRS = ("name", "list_accounts", "capabilities", "execute")


def _agents_dir() -> Path:
    return Path(__file__).resolve().parent / "agents"


def _exchange_name_from_filename(filename: str) -> Optional[str]:
    match = _SPOT_AGENT_FILENAME_PATTERN.match(filename)
    if not match:
        return None
    return match.group("exchange")


def _iter_agent_files(directory: Path) -> List[Path]:
    if not directory.is_dir():
        return []
    out: List[Path] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".py":
            continue
        if any(pattern.match(path.name) for pattern in _EXCLUDED_PATTERNS):
            continue
        if not _SPOT_AGENT_FILENAME_PATTERN.match(path.name):
            continue
        out.append(path)
    return out


def _load_agent_module(path: Path) -> Optional[Any]:
    exchange = _exchange_name_from_filename(path.name)
    if exchange is None:
        return None
    package_name = __name__.rsplit(".", 1)[0]  # plugins.trade
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
    except Exception as exc:  # noqa: BLE001 - discovery must keep going
        logger.warning("Failed to load spot agent %s: %s", path.name, exc)
        sys.modules.pop(module_name, None)
        return None


def _validate_agent(module: Any, expected_exchange: Optional[str] = None) -> Optional[str]:
    if module is None:
        return "module is None"
    for attr in _REQUIRED_AGENT_ATTRS:
        if not hasattr(module, attr):
            return f"missing required attribute {attr!r}"
    name = getattr(module, "name", None)
    if not isinstance(name, str) or not name.strip():
        return "name must be a non-empty string"
    if expected_exchange and name.strip() != expected_exchange:
        return f"name {name!r} does not match spot filename exchange {expected_exchange!r}"
    return None


class SpotDesk:
    """Exchange-agnostic dispatcher for /tradespot spot agents."""

    def __init__(self, agents_dir: Optional[Path] = None) -> None:
        self._agents: Dict[str, Any] = {}
        self._loaded = False
        self._agents_dir = Path(agents_dir) if agents_dir is not None else _agents_dir()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        for path in _iter_agent_files(self._agents_dir):
            exchange = _exchange_name_from_filename(path.name)
            module = _load_agent_module(path)
            reason = _validate_agent(module, expected_exchange=exchange)
            if reason is not None:
                logger.warning("Skipping spot agent %s: %s", path.name, reason)
                continue
            assert module is not None
            self._agents[module.name] = module
            logger.debug("SpotDesk registered agent: %s (from %s)", module.name, path.name)

    def list_exchanges(self) -> List[str]:
        self._ensure_loaded()
        return sorted(self._agents.keys())

    def list_accounts(self, exchange: str) -> List[Any]:
        self._ensure_loaded()
        agent = self._agents.get(exchange)
        if agent is None:
            return []
        try:
            accounts = agent.list_accounts()
        except Exception as exc:  # noqa: BLE001
            logger.warning("spot list_accounts(%s) failed: %s", exchange, exc)
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
                return (
                    str(item.get("account", "")).lower(),
                    str(item.get("chain", "")).lower(),
                )
            return ("", "")

        return sorted(normalized, key=_sort_key)

    def capabilities(self, exchange: str) -> List[str]:
        self._ensure_loaded()
        agent = self._agents.get(exchange)
        if agent is None:
            return []
        try:
            caps = agent.capabilities()
        except Exception as exc:  # noqa: BLE001
            logger.warning("spot capabilities(%s) failed: %s", exchange, exc)
            return []
        if not isinstance(caps, list):
            return []
        return [c for c in caps if isinstance(c, str)]

    def execute(self, request: Dict[str, Any]) -> CanonicalResponse:
        operation = request.get("operation") if isinstance(request, dict) else None
        exchange = request.get("exchange") if isinstance(request, dict) else None
        account = request.get("account") if isinstance(request, dict) else None
        if not operation:
            return make_failure("", exchange or "", account or "", "INVALID_REQUEST", "Missing 'operation' in request.")
        if not exchange:
            return make_failure(operation, "", account or "", "MISSING_EXCHANGE", "Missing 'exchange' in request.")
        if not account:
            return make_failure(operation, exchange, "", "MISSING_ACCOUNT", "Missing 'account' in request.")
        self._ensure_loaded()
        agent = self._agents.get(exchange)
        if agent is None:
            return make_failure(
                operation,
                exchange,
                account,
                "UNKNOWN_SPOT_EXCHANGE",
                f"Spot exchange '{exchange}' is not available.",
            )
        try:
            response = agent.execute(request)
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation,
                exchange,
                account,
                "SPOT_AGENT_EXCEPTION",
                sanitize_error_message(str(exc)),
            )
        if not isinstance(response, CanonicalResponse):
            return make_failure(
                operation,
                exchange,
                account,
                "INVALID_AGENT_RESPONSE",
                "Spot agent returned a malformed response.",
            )
        return response


_default_desk: Optional[SpotDesk] = None


def get_spotdesk() -> SpotDesk:
    global _default_desk
    if _default_desk is None:
        _default_desk = SpotDesk()
    return _default_desk


__all__ = [
    "SpotDesk",
    "get_spotdesk",
    "_exchange_name_from_filename",
    "_iter_agent_files",
]
