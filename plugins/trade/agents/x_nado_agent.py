"""Nado exchange agent.

Owns all Nado-specific behavior for the /trade stack.

Phase 1 (balance):
  - Credential discovery from ``NADO_<ALIAS>_SUBACCOUNT_OWNER`` and
    ``NADO_<ALIAS>_PRIVATE_KEY`` (optional for read-only balance),
    plus optional ``NADO_<ALIAS>_SUBACCOUNT_NAME`` (default ``default``).
  - Public gateway query ``subaccount_info`` (no signature required).
  - Canonical balance / portfolio summary from health assets (USDT0).

Docs: https://docs.nado.xyz/developer-resources/api
Gateway: ``https://api.prod.nado.xyz/gateway/v1``

TradeDesk and the Telegram wizard MUST remain exchange-agnostic and
MUST NOT parse ``NADO_*`` environment variables or Nado-native payloads.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..canonical import (
    CanonicalPortfolioSummary,
    CanonicalResponse,
    make_failure,
    make_success,
    normalize_balance,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)

name = "nado"

DEFAULT_GATEWAY_REST = "https://api.prod.nado.xyz/gateway/v1"
DEFAULT_SUBACCOUNT_NAME = "default"
API_TIMEOUT_SECONDS = 20
X18 = Decimal("1000000000000000000")  # 1e18

_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Required for account discovery. Private key is optional for balance-only.
_OWNER_ALIASES = ("SUBACCOUNT_OWNER", "OWNER", "ADDRESS", "WALLET")
_KEY_ALIASES = ("PRIVATE_KEY", "PRIVATEKEY", "SIGNER_KEY", "SECRET")
_NAME_ALIASES = ("SUBACCOUNT_NAME", "SUBACCOUNT", "NAME")
_BASE_ALIASES = ("GATEWAY_URL", "BASE_URL", "API_URL")


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _load_dotenv_values(path: Path) -> Dict[str, str]:
    if not path.is_file():
        return {}
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        try:
            text = path.read_text(encoding="latin-1")
        except OSError:
            return {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _combined_nado_env() -> Dict[str, Tuple[str, str, str]]:
    out: Dict[str, Tuple[str, str, str]] = {}
    for k, v in os.environ.items():
        if k.upper().startswith("NADO_"):
            out.setdefault(k.upper(), (k, str(v), "env"))
    for k, v in _load_dotenv_values(_hermes_home() / ".env").items():
        if k.upper().startswith("NADO_"):
            out.setdefault(k.upper(), (k, str(v), "dotenv"))
    return out


def _parse_alias_and_suffix(upper_key: str) -> Optional[Tuple[str, str]]:
    if not upper_key.startswith("NADO_"):
        return None
    rest = upper_key[len("NADO_") :]
    known = sorted(
        set(_OWNER_ALIASES + _KEY_ALIASES + _NAME_ALIASES + _BASE_ALIASES),
        key=len,
        reverse=True,
    )
    for suffix in known:
        token = "_" + suffix
        if rest.endswith(token):
            alias = rest[: -len(token)]
            if alias and _ALIAS_PATTERN.match(alias):
                return alias, suffix
    alias, _, suffix = rest.rpartition("_")
    if alias and suffix and _ALIAS_PATTERN.match(alias):
        return alias, suffix
    return None


def _discover_credential_map() -> Dict[str, Dict[str, str]]:
    buckets: Dict[str, Dict[str, str]] = {}
    for upper_key, (_actual, value, _src) in _combined_nado_env().items():
        parsed = _parse_alias_and_suffix(upper_key)
        if parsed is None:
            continue
        alias_upper, suffix = parsed
        alias = alias_upper.lower()
        slot = buckets.setdefault(alias, {})
        val = value.strip()
        if suffix in _OWNER_ALIASES:
            slot.setdefault("owner", val)
        elif suffix in _KEY_ALIASES:
            slot.setdefault("private_key", val)
        elif suffix in _NAME_ALIASES:
            slot.setdefault("subaccount_name", val)
        elif suffix in _BASE_ALIASES:
            slot.setdefault("gateway_url", val.rstrip("/"))
    # Balance-only needs owner; private_key optional until writes.
    complete: Dict[str, Dict[str, str]] = {}
    for alias, fields in buckets.items():
        if fields.get("owner"):
            complete[alias] = fields
    return complete


def list_accounts() -> List[str]:
    return sorted(_discover_credential_map().keys())


def capabilities() -> List[str]:
    return ["balance"]


def _lookup_credentials(account: str) -> Optional[Dict[str, str]]:
    alias = str(account or "").strip().lower()
    if not alias:
        return None
    fields = _discover_credential_map().get(alias)
    if not fields or not fields.get("owner"):
        return None
    owner = fields["owner"].strip()
    if not owner.startswith("0x") and not owner.startswith("0X"):
        owner = "0x" + owner
    return {
        "account": alias,
        "owner": owner,
        "private_key": fields.get("private_key") or "",
        "subaccount_name": (fields.get("subaccount_name") or DEFAULT_SUBACCOUNT_NAME).strip()
        or DEFAULT_SUBACCOUNT_NAME,
        "gateway_url": fields.get("gateway_url") or DEFAULT_GATEWAY_REST,
    }


def _subaccount_bytes32(owner: str, subaccount_name: str) -> str:
    """Build Nado sender/subaccount hex: address(20) || name(12).

    Example default name pads as ``64656661756c740000000000``.
    """
    addr = owner.lower().replace("0x", "")
    if len(addr) != 40 or any(c not in "0123456789abcdef" for c in addr):
        raise ValueError("Invalid subaccount owner address")
    name = str(subaccount_name or DEFAULT_SUBACCOUNT_NAME)
    name_bytes = name.encode("utf-8")[:12]
    name_bytes = name_bytes + b"\x00" * (12 - len(name_bytes))
    return "0x" + addr + name_bytes.hex()


def _redact(text: Any, credentials: Optional[Mapping[str, str]] = None) -> str:
    rendered = str(text or "")
    if credentials:
        for k in ("private_key", "owner"):
            v = str(credentials.get(k) or "").strip()
            if len(v) >= 8:
                rendered = rendered.replace(v, "***")
                if v.startswith("0x") and len(v) > 10:
                    rendered = rendered.replace(v[2:], "***")
    return rendered


def _decode_body(raw: bytes, content_encoding: str = "") -> bytes:
    enc = (content_encoding or "").lower()
    if "gzip" in enc or raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except Exception:  # noqa: BLE001
            pass
    return raw


def _gateway_query(credentials: Mapping[str, str], payload: Mapping[str, Any]) -> Dict[str, Any]:
    base = str(credentials.get("gateway_url") or DEFAULT_GATEWAY_REST).rstrip("/")
    url = base + "/query"
    body = json.dumps(dict(payload)).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Nado gateway requires Accept-Encoding to include gzip/br/deflate.
            "Accept-Encoding": "gzip, deflate",
            # Cloudflare on api.prod.nado.xyz bans bare-Python urllib UA (1010).
            "User-Agent": "kam-nado-agent/1.0 (+https://github.com/amiroo2021/kam)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
            raw = _decode_body(resp.read(), resp.headers.get("Content-Encoding", ""))
            parsed = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            err_body = _decode_body(exc.read(), exc.headers.get("Content-Encoding", "") if exc.headers else "")
            parsed = json.loads(err_body.decode("utf-8"))
            if isinstance(parsed, dict):
                return parsed
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {exc.code} on Nado query: {exc.reason}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Nado returned a non-object JSON payload.")
    return parsed


def _x18_to_decimal(value: Any) -> Decimal:
    try:
        raw = Decimal(str(value or "0"))
    except Exception:  # noqa: BLE001
        return Decimal("0")
    return raw / X18


def _format_decimal(value: Decimal) -> str:
    q = value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    text = format(q.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _balance_from_subaccount_info(data: Mapping[str, Any]) -> Tuple[Decimal, Decimal, Decimal, str]:
    """Return (account_value, withdrawable_est, margin_used_est, unit).

    Prefer initial health assets as account value (USDT0-denominated).
    Spot product 0 is typically the quote (USDT0) free balance.
    """
    unit = "USDT0"
    healths = data.get("healths") or []
    assets = Decimal("0")
    liabilities = Decimal("0")
    health = Decimal("0")
    if isinstance(healths, list) and healths:
        h0 = healths[0] if isinstance(healths[0], Mapping) else {}
        assets = _x18_to_decimal(h0.get("assets"))
        liabilities = _x18_to_decimal(h0.get("liabilities"))
        health = _x18_to_decimal(h0.get("health"))
    # Quote spot free balance (product_id 0) when present.
    quote_free = Decimal("0")
    for row in data.get("spot_balances") or []:
        if not isinstance(row, Mapping):
            continue
        # product_id 0 is valid — do not use ``or`` (0 is falsy).
        if "product_id" not in row:
            continue
        try:
            pid = int(row.get("product_id"))
        except Exception:  # noqa: BLE001
            continue
        if pid != 0:
            continue
        bal = row.get("balance") if isinstance(row.get("balance"), Mapping) else {}
        quote_free = _x18_to_decimal(bal.get("amount"))
        break
    account_value = assets if assets > 0 else max(quote_free, health)
    if liabilities > 0:
        margin_used = liabilities
    else:
        margin_used = max(account_value - quote_free, Decimal("0"))
    withdrawable = quote_free if quote_free != 0 else max(min(health, account_value), Decimal("0"))
    if withdrawable < 0:
        withdrawable = Decimal("0")
    return account_value, withdrawable, margin_used, unit


def _balance(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message=(
                "Unknown or incomplete Nado account. Set "
                "NADO_<ACCOUNT>_SUBACCOUNT_OWNER (and optional "
                "NADO_<ACCOUNT>_SUBACCOUNT_NAME, default 'default')."
            ),
        )
    try:
        sub = _subaccount_bytes32(credentials["owner"], credentials["subaccount_name"])
        payload = _gateway_query(
            credentials,
            {"type": "subaccount_info", "subaccount": sub},
        )
        if str(payload.get("status") or "").lower() != "success":
            err = payload.get("error") or payload.get("error_code") or "Nado query failed"
            return make_failure(
                operation="balance",
                exchange=name,
                account=credentials["account"],
                code="NADO_ERROR",
                message=_redact(sanitize_error_message(str(err)), credentials),
            )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return make_failure(
                operation="balance",
                exchange=name,
                account=credentials["account"],
                code="NADO_ERROR",
                message="Nado subaccount_info returned no data.",
            )
        if data.get("exists") is False:
            return make_failure(
                operation="balance",
                exchange=name,
                account=credentials["account"],
                code="SUBACCOUNT_NOT_FOUND",
                message=(
                    f"Nado subaccount '{credentials['subaccount_name']}' does not exist "
                    "for this owner yet (deposit once to create it)."
                ),
            )
        account_value, withdrawable, margin_used, unit = _balance_from_subaccount_info(data)
        balance = normalize_balance(account_value, unit)
        portfolio = CanonicalPortfolioSummary(
            account_value=normalize_balance(account_value, unit).value,
            withdrawable=normalize_balance(withdrawable, unit).value,
            margin_used=normalize_balance(margin_used, unit).value,
            total_position_value=normalize_balance(
                max(account_value - withdrawable, Decimal("0")), unit
            ).value,
            unit=unit,
        )
        return make_success(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            balance=balance,
            portfolio_summary=portfolio,
            data={
                "subaccount": sub,
                "subaccount_name": credentials["subaccount_name"],
                "exists": bool(data.get("exists")),
                "spot_count": data.get("spot_count"),
                "perp_count": data.get("perp_count"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            code="NADO_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    if not isinstance(request, dict):
        return make_failure(
            operation="",
            exchange=name,
            account="",
            code="INVALID_REQUEST",
            message="Request must be a dict.",
        )
    operation = str(request.get("operation") or "").strip()
    account = str(request.get("account") or "").strip()
    if not operation:
        return make_failure(
            operation="",
            exchange=name,
            account=account,
            code="INVALID_REQUEST",
            message="Missing 'operation'.",
        )
    if not account:
        return make_failure(
            operation=operation,
            exchange=name,
            account="",
            code="MISSING_ACCOUNT",
            message="Missing 'account'.",
        )
    try:
        if operation == "balance":
            return _balance(account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="NADO_ERROR",
            message=sanitize_error_message(str(exc)),
        )
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code="NOT_IMPLEMENTED",
        message=f"Nado does not implement '{operation}' yet.",
    )
