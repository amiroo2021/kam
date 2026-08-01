"""Canonical response contracts for the /trade wizard.

These dataclasses define the boundary between exchange-specific agents and
the rest of the stack (TradeDesk, Telegram wizard). Nothing exchange-native
may escape the exchange agent using these types.

Monetary values are represented as ``Decimal`` strings to avoid binary
float drift. The wizard renders them to exactly 2 decimal places per the
spec, but the canonical value preserves the source currency unit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Generic wizard actions — exchange-agnostic, defined once.
# ---------------------------------------------------------------------------

GENERIC_ACTIONS: tuple[str, ...] = (
    "balance",
    "positions_orders",
    "new_order",
    "ladder",
    "cancel_orders",
    "positions_management",
)

GENERIC_ACTION_LABELS: Dict[str, str] = {
    "balance": "Balance",
    "positions_orders": "Positions & Orders",
    "new_order": "New Order",
    "ladder": "Ladder",
    "cancel_orders": "Cancel Orders",
    "positions_management": "Positions Management",
}

# Phase 2: Balance and Positions & Orders are implemented by the Hyperliquid
# agent. The remaining generic actions still return a generic "not
# implemented" error from the agent.
PHASE1_IMPLEMENTED_ACTIONS: frozenset[str] = frozenset({"balance", "positions_orders"})


# ---------------------------------------------------------------------------
# Canonical balance
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CanonicalBalance:
    """Canonical monetary balance.

    Attributes:
        value: Decimal normalized to exactly 2 decimal places, as a string.
        unit: Native currency unit from the exchange (e.g. "USDC", "USDT",
            "USD", "USDG"). Never converted across currencies.
    """

    value: str
    unit: str

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "unit": self.unit}


@dataclass(frozen=True)
class CanonicalPortfolioSummary:
    """Canonical portfolio summary for a balance-style screen."""

    account_value: str
    withdrawable: str
    margin_used: str
    total_position_value: str
    unit: str = "USDC"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_value": self.account_value,
            "withdrawable": self.withdrawable,
            "margin_used": self.margin_used,
            "total_position_value": self.total_position_value,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class CanonicalPosition:
    """Canonical read-only position row."""

    symbol: str
    side: str
    size: str
    entry_price: str
    pnl: str
    tp: Optional[str] = None
    sl: Optional[str] = None
    tp_count: Optional[int] = None
    sl_count: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "entry_price": self.entry_price,
            "pnl": self.pnl,
            "tp": self.tp,
            "sl": self.sl,
            "tp_count": self.tp_count,
            "sl_count": self.sl_count,
        }


@dataclass(frozen=True)
class CanonicalOrderGroup:
    """Canonical aggregated open-order row."""

    symbol: str
    side: str
    order_count: int
    total_size: str
    vwap: str
    min_price: str
    max_price: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "order_count": self.order_count,
            "total_size": self.total_size,
            "vwap": self.vwap,
            "min_price": self.min_price,
            "max_price": self.max_price,
        }


@dataclass(frozen=True)
class CanonicalInstrument:
    """Canonical resolved instrument descriptor."""

    requested_symbol: str
    symbol: str
    display_name: str
    price_increment: Optional[str] = None
    size_increment: Optional[str] = None
    minimum_size: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_symbol": self.requested_symbol,
            "symbol": self.symbol,
            "display_name": self.display_name,
            "price_increment": self.price_increment,
            "size_increment": self.size_increment,
            "minimum_size": self.minimum_size,
        }


@dataclass(frozen=True)
class CanonicalOrderResult:
    """Canonical write summary for a single new order submission."""

    symbol: str
    side: str
    order_type: str
    requested_volume: str
    requested_price: str
    submitted_volume: str
    submitted_price: str
    verified: bool
    status: str = "success"
    exchange_order_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "requested_volume": self.requested_volume,
            "requested_price": self.requested_price,
            "submitted_volume": self.submitted_volume,
            "submitted_price": self.submitted_price,
            "verified": self.verified,
            "status": self.status,
        }
        if self.exchange_order_id is not None:
            data["exchange_order_id"] = self.exchange_order_id
        return data


@dataclass(frozen=True)
class CanonicalPositionActionResult:
    """Canonical write summary for a position-management action."""

    operation: str
    symbol: str
    verified: bool
    price: Optional[str] = None
    removed: Optional[bool] = None
    status: str = "success"
    exchange_order_id: Optional[int] = None
    current_side: Optional[str] = None
    current_size: Optional[str] = None
    message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "operation": self.operation,
            "symbol": self.symbol,
            "verified": self.verified,
            "status": self.status,
        }
        if self.price is not None:
            data["price"] = self.price
        if self.removed is not None:
            data["removed"] = self.removed
        if self.exchange_order_id is not None:
            data["exchange_order_id"] = self.exchange_order_id
        if self.current_side is not None:
            data["current_side"] = self.current_side
        if self.current_size is not None:
            data["current_size"] = self.current_size
        if self.message is not None:
            data["message"] = self.message
        return data


@dataclass(frozen=True)
class CanonicalLadderResult:
    """Canonical write summary for a ladder submission."""

    symbol: str
    side: str
    distribution: str
    requested_order_count: int
    submitted_order_count: int
    requested_volume: str
    submitted_volume: str
    batch_count: int
    verified: bool
    partial: bool = False
    status: str = "success"
    accepted_child_count: Optional[int] = None
    omitted_order_count: Optional[int] = None
    omitted_below_minimum: Optional[int] = None
    child_order_ids: Optional[list[int]] = None
    batches: Optional[list[Dict[str, Any]]] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "symbol": self.symbol,
            "side": self.side,
            "distribution": self.distribution,
            "requested_order_count": self.requested_order_count,
            "submitted_order_count": self.submitted_order_count,
            "requested_volume": self.requested_volume,
            "submitted_volume": self.submitted_volume,
            "batch_count": self.batch_count,
            "verified": self.verified,
            "partial": self.partial,
            "status": self.status,
            "accepted_child_count": self.accepted_child_count,
            "omitted_order_count": self.omitted_order_count,
            "omitted_below_minimum": self.omitted_below_minimum,
        }
        if self.child_order_ids is not None:
            data["child_order_ids"] = list(self.child_order_ids)
        if self.batches is not None:
            data["batches"] = [dict(batch) for batch in self.batches]
        return data


@dataclass(frozen=True)
class CanonicalCancelGroupResult:
    """Canonical exact-scope cancellation result."""

    symbol: str
    side: str
    targeted_order_count: int
    cancelled_order_count: int
    confirmed_absent_count: int
    remaining_target_count: int
    verified: bool
    partial: bool = False
    status: str = "success"
    batch_count: int = 0
    batches: Optional[list[Dict[str, Any]]] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "symbol": self.symbol,
            "side": self.side,
            "targeted_order_count": self.targeted_order_count,
            "cancelled_order_count": self.cancelled_order_count,
            "confirmed_absent_count": self.confirmed_absent_count,
            "remaining_target_count": self.remaining_target_count,
            "verified": self.verified,
            "partial": self.partial,
            "status": self.status,
            "batch_count": self.batch_count,
        }
        if self.batches is not None:
            data["batches"] = [dict(batch) for batch in self.batches]
        return data


def normalize_balance(amount: Any, unit: str) -> CanonicalBalance:
    """Normalize a raw monetary value to exactly 2 decimal places.

    Accepts ``int``, ``float``, ``str``, or ``Decimal``. Float input is
    converted via ``Decimal(str(amount))`` first to avoid binary-float
    drift (e.g. ``0.1 + 0.2`` would otherwise yield 0.30000000000000004).

    The unit is preserved verbatim — never converted across currencies.
    Negative values are preserved (e.g. -12.345 -> "-12.34").
    """
    if amount is None:
        raise ValueError("balance amount is None")
    if isinstance(amount, float):
        # Convert via string to avoid float repr drift.
        decimal_value = Decimal(str(amount))
    elif isinstance(amount, Decimal):
        decimal_value = amount
    else:
        decimal_value = Decimal(str(amount))

    quantized = decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    normalized_unit = (unit or "").strip()
    if not normalized_unit:
        raise ValueError("balance unit is empty")
    return CanonicalBalance(value=str(quantized), unit=normalized_unit)


# ---------------------------------------------------------------------------
# Canonical success / error envelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CanonicalError:
    code: str
    message: str
    exchange_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.exchange_reason is not None:
            data["exchange_reason"] = self.exchange_reason
        return data


@dataclass(frozen=True)
class CanonicalResponse:
    """Top-level envelope returned by every exchange agent operation.

    On success: ``success=True``, ``balance`` populated (for operation
    "balance"), ``error`` is None.

    On failure: ``success=False``, ``error`` populated, ``balance`` is None.
    The ``error`` message is sanitized inside the agent — no real secret
    values (API keys, signatures, auth tokens, private keys) leak.
    """

    success: bool
    operation: str
    exchange: str
    account: str
    balance: Optional[CanonicalBalance] = None
    portfolio_summary: Optional[CanonicalPortfolioSummary] = None
    positions: Optional[list[CanonicalPosition]] = None
    open_order_count: Optional[int] = None
    order_groups: Optional[list[CanonicalOrderGroup]] = None
    instrument: Optional[CanonicalInstrument] = None
    order: Optional[CanonicalOrderResult] = None
    ladder: Optional[CanonicalLadderResult] = None
    cancel_group: Optional[CanonicalCancelGroupResult] = None
    position_action: Optional[CanonicalPositionActionResult] = None
    error: Optional[CanonicalError] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "success": self.success,
            "operation": self.operation,
            "exchange": self.exchange,
            "account": self.account,
        }
        if self.balance is not None:
            data["balance"] = self.balance.to_dict()
        if self.portfolio_summary is not None:
            data["portfolio_summary"] = self.portfolio_summary.to_dict()
        if self.positions is not None:
            data["positions"] = [position.to_dict() for position in self.positions]
        if self.open_order_count is not None:
            data["open_order_count"] = self.open_order_count
        if self.order_groups is not None:
            data["order_groups"] = [group.to_dict() for group in self.order_groups]
        if self.instrument is not None:
            data["instrument"] = self.instrument.to_dict()
        if self.order is not None:
            data["order"] = self.order.to_dict()
        if self.ladder is not None:
            data["ladder"] = self.ladder.to_dict()
        if self.cancel_group is not None:
            data["cancel_group"] = self.cancel_group.to_dict()
        if self.position_action is not None:
            data["position_action"] = self.position_action.to_dict()
        if self.error is not None:
            data["error"] = self.error.to_dict()
        return data


def make_success(
    operation: str,
    exchange: str,
    account: str,
    balance: Optional[CanonicalBalance] = None,
    portfolio_summary: Optional[CanonicalPortfolioSummary] = None,
    positions: Optional[list[CanonicalPosition]] = None,
    open_order_count: Optional[int] = None,
    order_groups: Optional[list[CanonicalOrderGroup]] = None,
    instrument: Optional[CanonicalInstrument] = None,
    order: Optional[CanonicalOrderResult] = None,
    ladder: Optional[CanonicalLadderResult] = None,
    cancel_group: Optional[CanonicalCancelGroupResult] = None,
    position_action: Optional[CanonicalPositionActionResult] = None,
) -> CanonicalResponse:
    return CanonicalResponse(
        success=True,
        operation=operation,
        exchange=exchange,
        account=account,
        balance=balance,
        portfolio_summary=portfolio_summary,
        positions=positions,
        open_order_count=open_order_count,
        order_groups=order_groups,
        instrument=instrument,
        order=order,
        ladder=ladder,
        cancel_group=cancel_group,
        position_action=position_action,
        error=None,
    )


def make_failure(
    operation: str,
    exchange: str,
    account: str,
    code: str,
    message: str,
    portfolio_summary: Optional[CanonicalPortfolioSummary] = None,
    positions: Optional[list[CanonicalPosition]] = None,
    open_order_count: Optional[int] = None,
    order_groups: Optional[list[CanonicalOrderGroup]] = None,
    instrument: Optional[CanonicalInstrument] = None,
    order: Optional[CanonicalOrderResult] = None,
    ladder: Optional[CanonicalLadderResult] = None,
    cancel_group: Optional[CanonicalCancelGroupResult] = None,
    position_action: Optional[CanonicalPositionActionResult] = None,
    exchange_reason: Optional[str] = None,
) -> CanonicalResponse:
    """Wrap an exchange-native failure in the canonical envelope.

    The message MUST already be sanitized — never pass raw exception text
    that may contain credentials, signatures, URLs with secrets, or
    authentication material. Use :func:`sanitize_error_message` to redact
    credential values while preserving diagnostic context.
    """
    sanitized_message = (message or "").strip() or "Unknown error"
    sanitized_exchange_reason = (exchange_reason or "").strip() or None
    return CanonicalResponse(
        success=False,
        operation=operation,
        exchange=exchange,
        account=account,
        balance=None,
        portfolio_summary=portfolio_summary,
        positions=positions,
        open_order_count=open_order_count,
        order_groups=order_groups,
        instrument=instrument,
        order=order,
        ladder=ladder,
        cancel_group=cancel_group,
        position_action=position_action,
        error=CanonicalError(code=code, message=sanitized_message, exchange_reason=sanitized_exchange_reason),
    )


# ---------------------------------------------------------------------------
# Sanitization helpers
# ---------------------------------------------------------------------------
# Redact credential VALUES from an error message while preserving the
# surrounding diagnostic context.
#
# Strategy: scan for credential-bearing field names and redact the value that
# follows. Wallet addresses, orderIds, hashes, timestamps, and generic URLs are
# NOT secrets and must remain visible. A 64-char hex string is NOT a secret by
# itself; only when paired with a credential name does it become one. A final
# defense-in-depth scan catches obviously-shaped private-key/seed values.
#
# This is a defense-in-depth check; agents should never pass raw exception
# text into the canonical error in the first place. When a real secret
# does leak, this helper strips the offending value while keeping the
# surrounding diagnostic context visible.
_CREDENTIAL_VALUE_PATTERNS = (
    # JSON-style quoted credential values
    (re.compile(r'("X-API-Key"\s*:\s*")([^"]+)(")'), r'\1[REDACTED_API_KEY]\3'),
    (re.compile(r'("X-Signature"\s*:\s*")([^"]+)(")'), r'\1[REDACTED_SIGNATURE]\3'),
    (re.compile(r'("X-Auth[^"]*"\s*:\s*")([^"]+)(")'), r'\1[REDACTED_AUTH]\3'),
    (re.compile(r'("X-API-Key"\s*:\s*\'?)(\b[A-Fa-f0-9]{32,})(\b\'?)'), r'\1[REDACTED_API_KEY]\3'),
    (re.compile(r'("X-Signature"\s*:\s*\'?)(\b[A-Fa-f0-9]{64,128})(\b\'?)'), r'\1[REDACTED_SIGNATURE]\3'),
    (re.compile(r'("signed[Pp]ayload"\s*:\s*")([^"]+)(?<!\\)(")'), r'\1[REDACTED_PAYLOAD]\3'),
    # Bare key:value: capture the key=value prefix, retain it in output.
    # The negative lookbehind rejects alphanumerics, underscores, AND hyphens,
    # so 'X-API-Key' is not confused with 'api_key' or 'api-key'.
    (re.compile(r'(?<![A-Za-z0-9_-])(signature[\'"]?\s*:\s*[\'"]?)\b([A-Fa-f0-9]{64,128})\b(?=[\'"\s,;})\)]|$)', re.IGNORECASE), r'\1[REDACTED_SIGNATURE]'),
    (re.compile(r'(?<![A-Za-z0-9_-])(api[_-]?key[\'"]?\s*:\s*[\'"]?)\b([A-Fa-f0-9]{32,})\b(?=[\'"\s,;})\)]|$)', re.IGNORECASE), r'\1[REDACTED_API_KEY]'),
    (re.compile(r'(?<![A-Za-z0-9_-])(private[_-]?key[\'"]?\s*:\s*[\'"]?)\b([A-Fa-f0-9]{32,})\b(?=[\'"\s,;})\)]|$)', re.IGNORECASE), r'\1[REDACTED_PRIVATE_KEY]'),
    (re.compile(r'(?<![A-Za-z0-9_-])(token[\'"]?\s*:\s*[\'"]?)\b([A-Fa-f0-9]{20,})\b(?=[\'"\s,;})\)]|$)', re.IGNORECASE), r'\1[REDACTED_TOKEN]'),
    (re.compile(r'(?<![A-Za-z0-9_-])(secret[\'"]?\s*:\s*[\'"]?)([A-Za-z0-9._\-+/=]{16,})(?=[\'"\s,;})\)]|$)', re.IGNORECASE), r'\1[REDACTED_SECRET]'),
    # HTTP header style: "Header: value" with a value boundary so we only
    # match the value, not the rest of the message.
    (re.compile(r'(X-API-Key\s*:\s*)([A-Fa-f0-9]{32,})(?=\b|\s|$)', re.IGNORECASE), r'\1[REDACTED_API_KEY]'),
    (re.compile(r'(X-Signature\s*:\s*)([A-Fa-f0-9]{64,128})(?=\b|\s|$)', re.IGNORECASE), r'\1[REDACTED_SIGNATURE]'),
    (re.compile(r'(X-Auth[^:]*\s*:\s*)([A-Za-z0-9._\-+/=]+)(?=\b|\s|$)', re.IGNORECASE), r'\1[REDACTED_AUTH]'),
    (re.compile(r'(Authorization\s*:\s*Bearer\s+)([A-Za-z0-9._\-+/=]+)', re.IGNORECASE), r'\1[REDACTED_BEARER]'),
    (re.compile(r'(Authorization\s*:\s*Basic\s+)([A-Za-z0-9._\-+/=]+)', re.IGNORECASE), r'\1[REDACTED_BASIC]'),
    # URL query parameter (?token=, ?signature=, ?apikey=, ?sig=, etc.). Capture name
    # and value separately so the replacement does not duplicate the name.
    (re.compile(r'([?&](?:token|signature|api[_-]?key|access[_-]?token|sig)=)([^&\s"\'">]+)', re.IGNORECASE), r'\1[REDACTED]'),
    # env var style: ARCUS_***_APISIGNINGKEY=<value>, ARCUS_***_PRIVATE_KEY=<value>,
    # *_API_KEY=<value>, etc.
    (re.compile(r'((?:ARCUS_[A-Z0-9_]+_(?:APISIGNINGKEY|PRIVATE_KEY)|[A-Z_]+_API_KEY|[A-Z_]+_SECRET|[A-Z_]+_TOKEN)\s*=\s*)([A-Za-z0-9._\-+/=]+)'), r'\1[REDACTED]'),
    # ed25519 seed explicit prefix
    (re.compile(r'(ed25519:)([A-Fa-f0-9]{32,})'), r'\1[REDACTED_ED25519_SEED]'),
    # Defense-in-depth: 64-char hex adjacent to a credential-shaped keyword
    # (signer, signing_key, api_key, private_key, secret, seed, signature).
    (re.compile(r'((?:seed|signing[_-]?key|api[_-]?key|private[_-]?key|secret|signer|signature)\s*[:=]\s*)([A-Fa-f0-9]{64,128})', re.IGNORECASE), r'\1[REDACTED]'),
)


def sanitize_error_message(message: str) -> str:
    """Redact credential VALUES from an error message while preserving context.

    This is a defense-in-depth check; agents should never pass raw exception
    text into the canonical error in the first place. When a real secret does
    leak, this helper strips the offending value while keeping the surrounding
    diagnostic context visible. Wallet addresses, orderIds, hashes, timestamps,
    and generic URLs are NOT secrets and remain in the message untouched.
    """
    if not message:
        return "Unknown error"
    result = message
    for pattern, replacement in _CREDENTIAL_VALUE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


__all__ = [
    "GENERIC_ACTIONS",
    "GENERIC_ACTION_LABELS",
    "PHASE1_IMPLEMENTED_ACTIONS",
    "CanonicalBalance",
    "CanonicalPortfolioSummary",
    "CanonicalError",
    "CanonicalInstrument",
    "CanonicalResponse",
    "CanonicalOrderGroup",
    "CanonicalOrderResult",
    "CanonicalLadderResult",
    "CanonicalCancelGroupResult",
    "CanonicalPosition",
    "normalize_balance",
    "make_success",
    "make_failure",
    "sanitize_error_message",
    "asdict",
]
