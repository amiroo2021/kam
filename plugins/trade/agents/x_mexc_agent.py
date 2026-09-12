"""MEXC exchange agent.

Owns all MEXC-specific behavior for the /trade stack.

Credentials (``.env`` / environment):
  ``MEXC_<ALIAS>_ACCESSKEY`` + ``MEXC_<ALIAS>_SECRETKEY``
  Aliases also accepted: APIKEY / API_KEY / KEY and SECRET / API_SECRET / SECRET_KEY.
  Optional: ``MEXC_<ALIAS>_CONTRACT_BASE`` / ``MEXC_CONTRACT_BASE``
            ``MEXC_<ALIAS>_SPOT_BASE`` / ``MEXC_SPOT_BASE``

Phase 1:
  - balance (USDT-M contract account equity + available; spot dust noted in data)

Contract auth (futures):
  Headers ApiKey, Request-Time, Signature
  Signature = HMAC-SHA256(accessKey + timestamp + paramString, secret)

Spot auth (reference / future):
  Header X-MEXC-APIKEY + query signature HMAC-SHA256

Endpoints used:
  GET https://contract.mexc.com/api/v1/private/account/assets
  GET https://contract.mexc.com/api/v1/private/account/asset/USDT

TradeDesk and the Telegram wizard MUST remain exchange-agnostic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
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

name = "mexc"

DEFAULT_CONTRACT_BASE = "https://contract.mexc.com"
DEFAULT_SPOT_BASE = "https://api.mexc.com"
API_TIMEOUT_SECONDS = 20
DEFAULT_QUOTE = "USDT"

_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_KEY_ALIASES = (
    "ACCESSKEY",
    "ACCESS_KEY",
    "APIKEY",
    "API_KEY",
    "KEY",
)
_SECRET_ALIASES = (
    "SECRETKEY",
    "SECRET_KEY",
    "APISECRET",
    "API_SECRET",
    "SECRET",
)
_CONTRACT_BASE_ALIASES = ("CONTRACT_BASE", "CONTRACT_URL", "FUTURES_BASE")
_SPOT_BASE_ALIASES = ("SPOT_BASE", "SPOT_URL")

_USER_AGENT = "kam-mexc-agent/1.0 (+https://github.com/amiroo2021/kam)"


# ---------------------------------------------------------------------------
# Env
# ---------------------------------------------------------------------------


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


def _combined_mexc_env() -> Dict[str, Tuple[str, str, str]]:
    out: Dict[str, Tuple[str, str, str]] = {}
    for k, v in os.environ.items():
        if k.upper().startswith("MEXC_"):
            out.setdefault(k.upper(), (k, str(v), "env"))
    for k, v in _load_dotenv_values(_hermes_home() / ".env").items():
        if k.upper().startswith("MEXC_"):
            out.setdefault(k.upper(), (k, str(v), "dotenv"))
    return out


def _parse_alias_and_suffix(upper_key: str) -> Optional[Tuple[str, str]]:
    if not upper_key.startswith("MEXC_"):
        return None
    rest = upper_key[len("MEXC_") :]
    known = sorted(
        set(_KEY_ALIASES + _SECRET_ALIASES + _CONTRACT_BASE_ALIASES + _SPOT_BASE_ALIASES),
        key=len,
        reverse=True,
    )
    for suffix in known:
        token = "_" + suffix
        if rest.endswith(token):
            alias = rest[: -len(token)]
            if alias and _ALIAS_PATTERN.match(alias):
                return alias, suffix
    # bare MEXC_ACCESSKEY without alias → skip (need account)
    alias, _, suffix = rest.rpartition("_")
    if alias and suffix and _ALIAS_PATTERN.match(alias):
        return alias, suffix
    return None


def _discover_credential_map() -> Dict[str, Dict[str, str]]:
    buckets: Dict[str, Dict[str, str]] = {}
    for upper_key, (_actual, value, _src) in _combined_mexc_env().items():
        parsed = _parse_alias_and_suffix(upper_key)
        if parsed is None:
            continue
        alias_upper, suffix = parsed
        alias = alias_upper.lower()
        slot = buckets.setdefault(alias, {})
        val = value.strip()
        if suffix in _KEY_ALIASES:
            slot.setdefault("access_key", val)
        elif suffix in _SECRET_ALIASES:
            slot.setdefault("secret_key", val)
        elif suffix in _CONTRACT_BASE_ALIASES:
            slot.setdefault("contract_base", val.rstrip("/"))
        elif suffix in _SPOT_BASE_ALIASES:
            slot.setdefault("spot_base", val.rstrip("/"))
    # globals without alias
    env_map = {k: v for k, (_, v, _) in _combined_mexc_env().items()}
    global_contract = (
        env_map.get("MEXC_CONTRACT_BASE")
        or env_map.get("MEXC_CONTRACT_URL")
        or env_map.get("MEXC_FUTURES_BASE")
        or ""
    ).strip().rstrip("/")
    global_spot = (
        env_map.get("MEXC_SPOT_BASE") or env_map.get("MEXC_SPOT_URL") or ""
    ).strip().rstrip("/")

    complete: Dict[str, Dict[str, str]] = {}
    for alias, fields in buckets.items():
        if fields.get("access_key") and fields.get("secret_key"):
            if not fields.get("contract_base") and global_contract:
                fields["contract_base"] = global_contract
            if not fields.get("spot_base") and global_spot:
                fields["spot_base"] = global_spot
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
    return {
        "account": alias,
        "access_key": fields["access_key"],
        "secret_key": fields["secret_key"],
        "contract_base": fields.get("contract_base") or DEFAULT_CONTRACT_BASE,
        "spot_base": fields.get("spot_base") or DEFAULT_SPOT_BASE,
    }


def _redact(text: Any, credentials: Optional[Mapping[str, str]] = None) -> str:
    rendered = str(text or "")
    if credentials:
        for k in ("access_key", "secret_key"):
            v = str(credentials.get(k) or "").strip()
            if len(v) >= 6:
                rendered = rendered.replace(v, "***")
    return rendered


def _format_decimal(value: Decimal) -> str:
    q = value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    text = format(q.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0"))
    except Exception:  # noqa: BLE001
        return Decimal("0")


# ---------------------------------------------------------------------------
# HTTP — contract (futures)
# ---------------------------------------------------------------------------


def _contract_request(
    credentials: Mapping[str, str],
    method: str,
    path: str,
    params: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    base = str(credentials.get("contract_base") or DEFAULT_CONTRACT_BASE).rstrip("/")
    params = dict(params or {})
    ts = str(int(time.time() * 1000))
    items = sorted((str(k), str(v)) for k, v in params.items() if v is not None)
    param_str = "&".join(f"{k}={v}" for k, v in items)
    to_sign = f"{credentials['access_key']}{ts}{param_str}"
    sig = hmac.new(
        credentials["secret_key"].encode("utf-8"),
        to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    qs = ("?" + param_str) if param_str else ""
    url = f"{base}{path}{qs}"
    req = urllib.request.Request(
        url,
        method=method.upper(),
        headers={
            "ApiKey": credentials["access_key"],
            "Request-Time": ts,
            "Signature": sig,
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return parsed
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {exc.code} on MEXC contract {path}: {exc.reason}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("MEXC returned a non-object JSON payload.")
    return parsed


def _spot_request(
    credentials: Mapping[str, str],
    method: str,
    path: str,
    params: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    base = str(credentials.get("spot_base") or DEFAULT_SPOT_BASE).rstrip("/")
    params = dict(params or {})
    params["timestamp"] = str(int(time.time() * 1000))
    params.setdefault("recvWindow", "5000")
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(
        credentials["secret_key"].encode("utf-8"),
        qs.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    url = f"{base}{path}?{qs}&signature={sig}"
    req = urllib.request.Request(
        url,
        method=method.upper(),
        headers={
            "X-MEXC-APIKEY": credentials["access_key"],
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return parsed
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {exc.code} on MEXC spot {path}: {exc.reason}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("MEXC spot returned a non-object JSON payload.")
    return parsed


def _contract_ok(payload: Mapping[str, Any]) -> bool:
    if payload.get("success") is True:
        return True
    code = payload.get("code")
    return code in (0, "0", None) and "data" in payload and "error" not in str(payload.get("msg") or "").lower()


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------


def _pick_usdt_asset(rows: Any) -> Optional[Mapping[str, Any]]:
    if isinstance(rows, Mapping):
        return rows
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        cur = str(row.get("currency") or row.get("displayCurrency") or "").upper()
        if cur in {"USDT", "USD"}:
            return row
    return None


def _balance(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message=(
                "Unknown or incomplete MEXC account. Set "
                "MEXC_<ACCOUNT>_ACCESSKEY and MEXC_<ACCOUNT>_SECRETKEY."
            ),
        )
    try:
        # Prefer single-currency asset endpoint; fall back to full assets list.
        payload = _contract_request(
            credentials, "GET", f"/api/v1/private/account/asset/{DEFAULT_QUOTE}"
        )
        asset: Optional[Mapping[str, Any]] = None
        if _contract_ok(payload) and isinstance(payload.get("data"), Mapping):
            asset = payload["data"]
        else:
            payload = _contract_request(credentials, "GET", "/api/v1/private/account/assets")
            if not _contract_ok(payload):
                return make_failure(
                    operation="balance",
                    exchange=name,
                    account=credentials["account"],
                    code="MEXC_ERROR",
                    message=_redact(
                        sanitize_error_message(
                            str(payload.get("message") or payload.get("msg") or payload.get("code") or "assets failed")
                        ),
                        credentials,
                    ),
                )
            asset = _pick_usdt_asset(payload.get("data"))

        if not isinstance(asset, Mapping):
            return make_failure(
                operation="balance",
                exchange=name,
                account=credentials["account"],
                code="MEXC_ERROR",
                message="No USDT contract asset row returned.",
            )

        equity = _to_decimal(asset.get("equity") if asset.get("equity") is not None else asset.get("cashBalance"))
        available = _to_decimal(
            asset.get("availableBalance")
            if asset.get("availableBalance") is not None
            else asset.get("availableCash")
        )
        frozen = _to_decimal(asset.get("frozenBalance"))
        position_margin = _to_decimal(asset.get("positionMargin"))
        unrealized = _to_decimal(asset.get("unrealized"))
        bonus = _to_decimal(asset.get("bonus"))

        # account_value = equity (includes unrealized); withdrawable ≈ available
        account_value = equity if equity != 0 else (available + frozen + position_margin)
        margin_used = position_margin if position_margin > 0 else frozen
        withdrawable = available if available > 0 else max(account_value - margin_used, Decimal("0"))

        unit = DEFAULT_QUOTE
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

        # Best-effort spot dust (non-blocking)
        spot_nonzero: List[Dict[str, str]] = []
        try:
            spot = _spot_request(credentials, "GET", "/api/v3/account")
            for row in spot.get("balances") or []:
                if not isinstance(row, Mapping):
                    continue
                free = _to_decimal(row.get("free"))
                locked = _to_decimal(row.get("locked"))
                if free + locked <= 0:
                    continue
                spot_nonzero.append(
                    {
                        "asset": str(row.get("asset") or ""),
                        "free": _format_decimal(free),
                        "locked": _format_decimal(locked),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("mexc spot account optional read failed: %s", exc)

        return make_success(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            balance=balance,
            portfolio_summary=portfolio,
            data={
                "source": "contract",
                "currency": str(asset.get("currency") or unit),
                "equity": _format_decimal(equity),
                "available": _format_decimal(available),
                "frozen": _format_decimal(frozen),
                "position_margin": _format_decimal(position_margin),
                "unrealized": _format_decimal(unrealized),
                "bonus": _format_decimal(bonus),
                "spot_nonzero": spot_nonzero[:20],
            },
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            code="MEXC_ERROR",
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
            code="MEXC_ERROR",
            message=sanitize_error_message(str(exc)),
        )
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code="NOT_IMPLEMENTED",
        message=f"MEXC does not implement '{operation}' yet.",
    )
