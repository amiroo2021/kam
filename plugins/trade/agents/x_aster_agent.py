"""Aster exchange agent for KAM /trade.

Credentials (``$HERMES_HOME/.env`` / environment):
  ``ASTER_<ALIAS>_APIPRIVATE`` + ``ASTER_<ALIAS>_WALLET``

``APIPRIVATE`` is the Aster API/agent wallet private key used for EIP-712
request signing. ``WALLET`` is the login wallet address that owns the account.
Secrets are never logged or returned.

Phase 1: read-only balance through Aster Spot V3 ``GET /api/v3/account``.
Mutating operations are intentionally not advertised.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from eth_account import Account  # type: ignore[import-untyped]
try:  # eth-account >= 0.12
    from eth_account.messages import encode_typed_data  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - older dependency fallback
    encode_typed_data = None  # type: ignore[assignment]
try:  # eth-account <= 0.11 compatibility
    from eth_account.messages import encode_structured_data  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - current dependency path
    encode_structured_data = None  # type: ignore[assignment]

from ..canonical import (
    CanonicalBalance,
    CanonicalPortfolioSummary,
    make_failure,
    make_success,
    normalize_balance,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)

name = "aster"


DEFAULT_API_BASE = "https://sapi.asterdex.com"
API_TIMEOUT_SECONDS = 20
_USER_AGENT = "Hermes-KAM-AsterAgent/1.0"

_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
# Keep discovery narrow: the live .env contains unrelated ASTER_BITGET_* keys.
# The user-specified Aster account shape is ASTER_<ACCOUNT>_APIPRIVATE +
# ASTER_<ACCOUNT>_WALLET; broader aliases can accidentally activate non-Aster
# helper credentials as Telegram accounts.
_PRIVATE_ALIASES = ("APIPRIVATE",)
_WALLET_ALIASES = ("WALLET",)
_BASE_ALIASES = ("API_BASE", "BASE_URL", "SPOT_BASE", "SPOT_URL")
_STABLE_ASSETS = frozenset({"USDT", "USDC", "USD", "FDUSD", "BUSD", "TUSD", "DAI"})

_nonce_lock = threading.Lock()
_last_second = 0
_nonce_suffix = 0


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _load_dotenv_values(path: Path) -> Dict[str, str]:
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        try:
            text = path.read_text(encoding="latin-1")
        except OSError:
            return {}
    values: Dict[str, str] = {}
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


def _combined_aster_env() -> Dict[str, Tuple[str, str, str]]:
    out: Dict[str, Tuple[str, str, str]] = {}
    for key, value in os.environ.items():
        if key.upper().startswith("ASTER_"):
            out.setdefault(key.upper(), (key, str(value), "env"))
    for key, value in _load_dotenv_values(_hermes_home() / ".env").items():
        if key.upper().startswith("ASTER_"):
            out.setdefault(key.upper(), (key, str(value), "dotenv"))
    return out


def _parse_alias_and_suffix(upper_key: str) -> Optional[Tuple[str, str]]:
    if not upper_key.startswith("ASTER_"):
        return None
    rest = upper_key[len("ASTER_") :]
    known = sorted(set(_PRIVATE_ALIASES + _WALLET_ALIASES + _BASE_ALIASES), key=len, reverse=True)
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
    for upper_key, (_actual, value, _src) in _combined_aster_env().items():
        parsed = _parse_alias_and_suffix(upper_key)
        if parsed is None:
            continue
        alias_upper, suffix = parsed
        alias = alias_upper.lower()
        slot = buckets.setdefault(alias, {})
        val = value.strip()
        if suffix in _PRIVATE_ALIASES:
            slot.setdefault("api_private", val)
        elif suffix in _WALLET_ALIASES:
            slot.setdefault("wallet", val)
        elif suffix in _BASE_ALIASES:
            slot.setdefault("api_base", val.rstrip("/"))

    env_map = {key: value for key, (_actual, value, _src) in _combined_aster_env().items()}
    global_base = (env_map.get("ASTER_API_BASE") or env_map.get("ASTER_BASE_URL") or "").strip().rstrip("/")

    complete: Dict[str, Dict[str, str]] = {}
    for alias, fields in buckets.items():
        if fields.get("api_private") and fields.get("wallet"):
            if not fields.get("api_base") and global_base:
                fields["api_base"] = global_base
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
    if not fields:
        return None
    private_key = fields["api_private"].strip()
    signer = Account.from_key(private_key).address
    return {
        "account": alias,
        "api_private": private_key,
        "wallet": fields["wallet"].strip(),
        "signer": signer,
        "api_base": fields.get("api_base") or DEFAULT_API_BASE,
    }


def _redact(text: Any, credentials: Optional[Mapping[str, str]] = None) -> str:
    rendered = str(text or "")
    if credentials:
        for key in ("api_private", "wallet", "signer"):
            value = str(credentials.get(key) or "").strip()
            if len(value) >= 6:
                rendered = rendered.replace(value, "***")
    rendered = re.sub(r"signature=0x[0-9a-fA-F]+", "signature=***", rendered)
    return rendered


def _nonce() -> str:
    """Aster V3 nonce: current timestamp in microseconds, unique per signer."""
    global _last_second, _nonce_suffix
    with _nonce_lock:
        now_second = int(time.time())
        if now_second == _last_second:
            _nonce_suffix += 1
        else:
            _last_second = now_second
            _nonce_suffix = 0
        return str(now_second * 1_000_000 + _nonce_suffix)


def _typed_data(message: str) -> Dict[str, Any]:
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Message": [{"name": "msg", "type": "string"}],
        },
        "primaryType": "Message",
        "domain": {
            "name": "AsterSignTransaction",
            "version": "1",
            "chainId": 1666,
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        },
        "message": {"msg": message},
    }


def _sign_message(private_key: str, message: str) -> str:
    typed = _typed_data(message)
    if encode_typed_data is not None:
        signable = encode_typed_data(full_message=typed)
    elif encode_structured_data is not None:  # pragma: no cover
        signable = encode_structured_data(typed)
    else:  # pragma: no cover
        raise RuntimeError("eth_account does not expose an EIP-712 encoder")
    signed = Account.sign_message(signable, private_key=private_key)
    return signed.signature.hex()


def _signed_request(
    credentials: Mapping[str, str],
    method: str,
    path: str,
    params: Optional[Mapping[str, Any]] = None,
) -> Any:
    request_params: Dict[str, Any] = {k: v for k, v in dict(params or {}).items() if v is not None}
    # Docs show signer+nonce signed in the URL query. Include user/login wallet
    # when available so account-scoped USER_DATA requests are unambiguous.
    request_params.setdefault("user", str(credentials.get("wallet") or ""))
    request_params["nonce"] = _nonce()
    request_params["signer"] = str(credentials.get("signer") or "")
    param = urllib.parse.urlencode(request_params)
    signature = _sign_message(str(credentials["api_private"]), param)
    url = f"{str(credentials.get('api_base') or DEFAULT_API_BASE).rstrip('/')}{path}?{param}&signature={signature}"
    req = urllib.request.Request(
        url,
        method=method.upper(),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:  # noqa: S310 HTTPS API
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                parsed.setdefault("http_status", exc.code)
                return parsed
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {exc.code} on Aster {path}: {exc.reason}") from exc
    parsed = json.loads(body)
    return parsed


def _decimal_or_zero(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0"))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _balance_assets(payload: Mapping[str, Any]) -> List[Dict[str, str]]:
    rows = payload.get("balances") or []
    assets: List[Dict[str, str]] = []
    if not isinstance(rows, list):
        return assets
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        asset = str(row.get("asset") or row.get("symbol") or "").strip().upper()
        if not asset:
            continue
        free = _decimal_or_zero(row.get("free"))
        locked = _decimal_or_zero(row.get("locked"))
        total = free + locked
        assets.append(
            {
                "asset": asset,
                "free": _format_decimal(free),
                "locked": _format_decimal(locked),
                "total": _format_decimal(total),
            }
        )
    return assets


def _balance_total(assets: List[Dict[str, str]]) -> Tuple[Decimal, str]:
    for preferred in ("USDT", "USDC", "USD"):
        total = sum(_decimal_or_zero(row.get("total")) for row in assets if str(row.get("asset") or "").upper() == preferred)
        if total != 0:
            return total, preferred
    stable_total = sum(
        _decimal_or_zero(row.get("total"))
        for row in assets
        if str(row.get("asset") or "").upper() in _STABLE_ASSETS
    )
    if stable_total != 0:
        return stable_total, "USD"
    return Decimal("0"), "USDT"


def _handle_balance(account: str):
    operation = "balance"
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation, name, str(account or ""), "MISSING_CREDENTIALS", "Aster account credentials are not configured.")
    try:
        payload = _signed_request(credentials, "GET", "/api/v3/account")
        if not isinstance(payload, Mapping):
            return make_failure(operation, name, account, "ASTER_BAD_RESPONSE", "Aster account response was not a JSON object.")
        if payload.get("code") not in (None, 0, "0", 200, "200") and payload.get("msg"):
            return make_failure(
                operation,
                name,
                account,
                "ASTER_API_ERROR",
                sanitize_error_message(_redact(str(payload.get("msg") or "Aster rejected the balance request."), credentials)),
                exchange_reason=sanitize_error_message(_redact(json.dumps({"code": payload.get("code"), "msg": payload.get("msg")}), credentials)),
            )
        assets = _balance_assets(payload)
        total, unit = _balance_total(assets)
        balance: CanonicalBalance = normalize_balance(total, unit)
        summary = CanonicalPortfolioSummary(
            account_value=balance.value,
            withdrawable=balance.value,
            margin_used="0.00",
            total_position_value="0.00",
            unit=balance.unit,
        )
        return make_success(
            operation,
            name,
            account,
            balance=balance,
            portfolio_summary=summary,
            data={"assets": assets, "canTrade": payload.get("canTrade"), "updateTime": payload.get("updateTime")},
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation,
            name,
            account,
            "ASTER_BALANCE_ERROR",
            sanitize_error_message(_redact(str(exc), credentials)),
        )


def execute(request: Mapping[str, Any]):
    operation = str(request.get("operation") or "").strip().lower()
    account = str(request.get("account") or "").strip()
    if operation == "balance":
        return _handle_balance(account)
    return make_failure(operation or "unknown", name, account, "NOT_IMPLEMENTED", f"Aster does not implement {operation!r}.")
