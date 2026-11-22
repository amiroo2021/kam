"""SpotDesk — spot-agent dispatcher for the /tradespot wizard.

Spot agents are deliberately separate from the existing /trade agents.
Only files named ``x_<exchange>_agent_spot.py`` or
``x_<exchange>_spot_agent.py`` are considered here; normal
``x_<exchange>_agent.py`` files are ignored.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .canonical import CanonicalResponse, make_failure, sanitize_error_message

logger = logging.getLogger(__name__)


def _log_identity_boundary(
    request: Dict[str, Any],
    response: Any,
    agent: Any,
    mismatched_fields: List[str],
    *,
    reason: str,
) -> None:
    error = getattr(response, "error", None)
    record = {
        "event": "spotdesk_identity_validation_failure",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "correlation_id": uuid.uuid4().hex[:16],
        "reason": reason,
        "request": {
            "operation": request.get("operation"),
            "exchange": request.get("exchange"),
            "account": request.get("account"),
        },
        "response": {
            "operation": getattr(response, "operation", None),
            "exchange": getattr(response, "exchange", None),
            "account": getattr(response, "account", None),
            "success": getattr(response, "success", None),
            "error_code": getattr(error, "code", None),
        },
        "mismatched_fields": list(mismatched_fields),
        "response_type": f"{type(response).__module__}.{type(response).__qualname__}",
        "agent_module": getattr(agent, "__name__", None),
        "agent_path": str(getattr(agent, "__file__", "") or ""),
    }
    logger.warning("%s", json.dumps(record, sort_keys=True, separators=(",", ":")))

_SPOT_AGENT_FILENAME_PATTERNS = (
    re.compile(r"^x_(?P<exchange>[a-z][a-z0-9_]*)_agent_spot\.py$"),
    re.compile(r"^x_(?P<exchange>[a-z][a-z0-9_]*)_spot_agent\.py$"),
)
_EXCLUDED_PATTERNS = (
    re.compile(r"^__init__\.py$"),
    re.compile(r"^[._].+"),
)
_REQUIRED_AGENT_ATTRS = ("name", "list_accounts", "capabilities", "execute")


def _agents_dir() -> Path:
    return Path(__file__).resolve().parent / "agents"


def _exchange_name_from_filename(filename: str) -> Optional[str]:
    for pattern in _SPOT_AGENT_FILENAME_PATTERNS:
        match = pattern.match(filename)
        if match:
            return match.group("exchange")
    return None


def _iter_agent_files(directory: Path) -> List[Path]:
    if not directory.is_dir():
        return []
    out: List[Path] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".py":
            continue
        if any(pattern.match(path.name) for pattern in _EXCLUDED_PATTERNS):
            continue
        if _exchange_name_from_filename(path.name) is None:
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
        if attr != "name" and not callable(getattr(module, attr)):
            return f"required attribute {attr!r} must be callable"
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

    def agent_for(self, exchange: str) -> Optional[Any]:
        """Return a discovered agent for generic read-only adapter bridges."""
        self._ensure_loaded()
        return self._agents.get(exchange)

    def plan_ladder(self, request: Dict[str, Any]) -> CanonicalResponse:
        """Canonical exchange-neutral ladder-planning seam.

        The selected Spot agent owns precision, limits, and balance-policy
        validation. SpotDesk only routes the plan request and normalizes the
        operation name; it never embeds exchange-specific rules.
        """
        if not isinstance(request, dict):
            return make_failure("plan_ladder", "", "", "INVALID_REQUEST", "Ladder request must be a mapping.")
        routed = dict(request)
        routed["operation"] = "plan_ladder"
        return self.execute(routed)

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
            _log_identity_boundary(
                request,
                response,
                agent,
                ["response_type"],
                reason="malformed_response",
            )
            return make_failure(
                operation,
                exchange,
                account,
                "INVALID_AGENT_RESPONSE",
                "Spot agent returned a malformed response.",
            )
        mismatched_fields = [
            field
            for field in ("operation", "exchange", "account")
            if getattr(response, field, None) != request.get(field)
        ]
        if mismatched_fields:
            _log_identity_boundary(
                request,
                response,
                agent,
                mismatched_fields,
                reason="identity_mismatch",
            )
            return make_failure(
                operation,
                exchange,
                account,
                "INVALID_AGENT_RESPONSE",
                "Spot agent response identity does not match the request.",
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
