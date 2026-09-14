"""QFEX exchange agent.

Credentials (``.env`` / environment):
  ``QFEX_<ALIAS>_PUBLIC_KEY`` + ``QFEX_<ALIAS>_SECRET_KEY``
  Optional: ``QFEX_<ALIAS>_BASE_URL`` (default https://api.qfex.com)
            ``QFEX_<ALIAS>_ACCOUNT_ID`` for x-qfex-requested-account-id

Current scope:
  - balance via GET /user/subaccounts/balance

QFEX HMAC auth per docs:
  signature = HMAC-SHA256(secret, f"{nonce}:{unix_ts}").hexdigest()
  headers: x-qfex-public-key, x-qfex-hmac-signature,
           x-qfex-nonce, x-qfex-timestamp
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from ..canonical import (
    CanonicalPortfolioSummary,
    CanonicalResponse,
    make_failure,
    make_success,
    normalize_balance,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)

name = "qfex"
DEFAULT_API_BASE = "https://api.qfex.com"
API_TIMEOUT_SECONDS = 20
DEFAULT_UNIT = "USDT"

_PATH_SUBACCOUNT_BALANCE = "/user/subaccounts/balance"

_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PUBLIC_KEY_ALIASES = ("PUBLIC_KEY", "PUBLICKEY", "API_KEY", "APIKEY", "KEY")
_SECRET_KEY_ALIASES = ("SECRET_KEY", "SECRETKEY", "API_SECRET", "APISECRET", "SECRET")
_BASE_URL_ALIASES = ("BASE_URL", "API_BASE", "URL")
_ACCOUNT_ID_ALIASES = ("ACCOUNT_ID", "ACCOUNTID", "SUBACCOUNT_ID", "SUBACCOUNTID")


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


def _combined_qfex_env() -> Dict[str, Tuple[str, str, str]]:
    out: Dict[str, Tuple[str, str, str]] = {}
    for key, value in os.environ.items():
        if key.upper().startswith("QFEX_"):
            out.setdefault(key.upper(), (key, str(value), "env"))
    for key, value in _load_dotenv_values(_hermes_home() / ".env").items():
        if key.upper().startswith("QFEX_"):
            out.setdefault(key.upper(), (key, str(value), "dotenv"))
    return out


def _parse_alias_and_suffix(upper_key: str) -> Optional[Tuple[str, str]]:
    if not upper_key.startswith("QFEX_"):
        return None
    rest = upper_key[len("QFEX_") :]
    suffixes = sorted(
        set(_PUBLIC_KEY_ALIASES + _SECRET_KEY_ALIASES + _BASE_URL_ALIASES + _ACCOUNT_ID_ALIASES),
        key=len,
        reverse=True,
    )
    for suffix in suffixes:
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
    for upper_key, (_actual, value, _src) in _combined_qfex_env().items():
        parsed = _parse_alias_and_suffix(upper_key)
        if parsed is None:
            continue
        alias_upper, suffix = parsed
        alias = alias_upper.lower()
        slot = buckets.setdefault(alias, {})
        val = value.strip()
        if suffix in _PUBLIC_KEY_ALIASES:
            slot.setdefault("public_key", val)
        elif suffix in _SECRET_KEY_ALIASES:
            slot.setdefault("secret_key", val)
        elif suffix in _BASE_URL_ALIASES:
            slot.setdefault("base_url", val.rstrip("/"))
        elif suffix in _ACCOUNT_ID_ALIASES:
            slot.setdefault("account_id", val)
    return {
        alias: fields
        for alias, fields in buckets.items()
        if fields.get("public_key") and fields.get("secret_key")
    }


def list_accounts() -> list[str]:
    return sorted(_discover_credential_map().keys())


def capabilities() -> list[str]:
    return ["balance"]


def _lookup_credentials(account: str) -> Optional[Dict[str, str]]:
    alias = str(account or "").strip().lower()
    if not alias:
        return None
    fields = _discover_credential_map().get(alias)
    if not fields:
        return None
    out = {
        "account": alias,
        "public_key": fields["public_key"],
        "secret_key": fields["secret_key"],
        "base_url": fields.get("base_url") or DEFAULT_API_BASE,
    }
    if fields.get("account_id"):
        out["account_id"] = fields["account_id"]
    return out


def _redact(text: Any, credentials: Optional[Mapping[str, str]] = None) -> str:
    rendered = str(text or "")
    if credentials:
        for key in ("public_key", "secret_key", "account_id"):
            value = str(credentials.get(key) or "").strip()
            if len(value) >= 6:
                rendered = rendered.replace(value, "***")
    rendered = re.sub(r"(?i)(x-qfex-public-key\s*[:=]\s*)([^\s,;}\"']+)", r"\1***", rendered)
    rendered = re.sub(r"(?i)(x-qfex-hmac-signature\s*[:=]\s*)([^\s,;}\"']+)", r"\1***", rendered)
    return sanitize_error_message(rendered)


def _auth_headers(credentials: Mapping[str, str]) -> Dict[str, str]:
    nonce = secrets.token_hex(16)
    unix_ts = str(int(time.time()))
    signed = f"{nonce}:{unix_ts}".encode("utf-8")
    secret_key = str(credentials.get("secret_key") or "").encode("utf-8")
    signature = hmac.new(secret_key, signed, "sha256").hexdigest()
    headers = {
        "Accept": "application/json",
        "User-Agent": "Hermes-KAM-QFEXAgent/1.0",
        "x-qfex-public-key": str(credentials.get("public_key") or ""),
        "x-qfex-hmac-signature": signature,
        "x-qfex-nonce": nonce,
        "x-qfex-timestamp": unix_ts,
    }
    account_id = str(credentials.get("account_id") or "").strip()
    if account_id:
        headers["x-qfex-requested-account-id"] = account_id
    return headers


def _signed_request(
    credentials: Mapping[str, str], method: str, path: str, query: str = ""
) -> Dict[str, Any]:
    base = str(credentials.get("base_url") or DEFAULT_API_BASE).rstrip("/")
    method_u = method.upper().strip()
    query = query.lstrip("?")
    url = f"{base}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, method=method_u, headers=_auth_headers(credentials))
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:  # noqa: S310 HTTPS API
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"detail": raw or str(exc)}
        detail = payload.get("detail") or payload.get("title") or payload.get("message") or str(exc)
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason or exc)) from exc


def _decimal_or_zero(value: Any) -> Decimal:
    try:
        if value is None or value == "":
            return Decimal("0")
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _balance(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set QFEX_<ACCOUNT>_PUBLIC_KEY and QFEX_<ACCOUNT>_SECRET_KEY.",
        )
    try:
        payload = _signed_request(credentials, "GET", _PATH_SUBACCOUNT_BALANCE)
        rows = payload.get("accounts") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            rows = []
        total_available = Decimal("0")
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            total_available += _decimal_or_zero(row.get("available_balance"))
        balance = normalize_balance(total_available, DEFAULT_UNIT)
        portfolio = CanonicalPortfolioSummary(
            account_value=balance.value,
            withdrawable=balance.value,
            margin_used="0.00",
            total_position_value="0.00",
            unit=DEFAULT_UNIT,
        )
        return make_success(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            balance=balance,
            portfolio_summary=portfolio,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            code="QFEX_ERROR",
            message=_redact(exc, credentials),
        )


def execute(request: Mapping[str, Any]) -> CanonicalResponse:
    operation = str(request.get("operation") or "").strip().lower()
    account = str(request.get("account") or "").strip()
    if operation == "balance":
        return _balance(account)
    credentials = _lookup_credentials(account)
    canonical_account = credentials["account"] if credentials else account
    return make_failure(
        operation=operation or "unknown",
        exchange=name,
        account=canonical_account,
        code="NOT_IMPLEMENTED",
        message=f"QFEX agent does not implement operation: {operation or 'unknown'}.",
    )


__all__ = [
    "name",
    "list_accounts",
    "capabilities",
    "execute",
]
