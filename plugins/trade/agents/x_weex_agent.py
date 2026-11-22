"""WEEX spot exchange agent for the /trade stack.

Credentials (``.env`` / environment):
  ``WEEX_<ALIAS>_APIKEY`` + ``WEEX_<ALIAS>_APISECRET`` +
  ``WEEX_<ALIAS>_PASSPHRASE``
  Optional: ``WEEX_<ALIAS>_BASE_URL`` / ``WEEX_SPOT_BASE``

Supported operations:
  - balance (read-only spot account balance)

WEEX spot auth:
  ACCESS-SIGN = Base64(HMAC-SHA256(secret,
    timestamp + METHOD + requestPath + ("?" + queryString if present) + body))
  Headers: ACCESS-KEY, ACCESS-SIGN, ACCESS-PASSPHRASE, ACCESS-TIMESTAMP.
"""

from __future__ import annotations

import base64
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

name = "weex"


DEFAULT_SPOT_BASE = "https://api-spot.weex.com"
API_TIMEOUT_SECONDS = 20
_STABLE_CURRENCIES = frozenset({"USDT", "USDC", "USD", "FDUSD", "BUSD", "TUSD", "DAI"})
_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_KEY_ALIASES = ("APIKEY", "API_KEY", "KEY", "ACCESS_KEY", "ACCESSKEY")
_SECRET_ALIASES = ("APISECRET", "API_SECRET", "SECRET", "SECRET_KEY", "SECRETKEY")
_PASSPHRASE_ALIASES = ("PASSPHRASE", "PASS_PHRASE")
_BASE_ALIASES = ("BASE_URL", "SPOT_BASE", "SPOT_URL", "API_BASE")
_USER_AGENT = "kam-trade-weex-agent/1.0"


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


def _combined_weex_env() -> Dict[str, Tuple[str, str, str]]:
    out: Dict[str, Tuple[str, str, str]] = {}
    for k, v in os.environ.items():
        if k.upper().startswith("WEEX_"):
            out.setdefault(k.upper(), (k, str(v), "env"))
    for k, v in _load_dotenv_values(_hermes_home() / ".env").items():
        if k.upper().startswith("WEEX_"):
            out.setdefault(k.upper(), (k, str(v), "dotenv"))
    return out


def _parse_alias_and_suffix(upper_key: str) -> Optional[Tuple[str, str]]:
    if not upper_key.startswith("WEEX_"):
        return None
    rest = upper_key[len("WEEX_") :]
    known = sorted(
        set(_KEY_ALIASES + _SECRET_ALIASES + _PASSPHRASE_ALIASES + _BASE_ALIASES),
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
    env = _combined_weex_env()
    for upper_key, (_actual, value, _src) in env.items():
        parsed = _parse_alias_and_suffix(upper_key)
        if parsed is None:
            continue
        alias_upper, suffix = parsed
        alias = alias_upper.lower()
        slot = buckets.setdefault(alias, {})
        val = value.strip()
        if suffix in _KEY_ALIASES:
            slot.setdefault("api_key", val)
        elif suffix in _SECRET_ALIASES:
            slot.setdefault("api_secret", val)
        elif suffix in _PASSPHRASE_ALIASES:
            slot.setdefault("passphrase", val)
        elif suffix in _BASE_ALIASES:
            slot["base_url"] = val.rstrip("/")

    env_map = {k: v for k, (_, v, _) in env.items()}
    global_base = (
        env_map.get("WEEX_SPOT_BASE")
        or env_map.get("WEEX_SPOT_URL")
        or env_map.get("WEEX_BASE_URL")
        or ""
    ).strip().rstrip("/")
    complete: Dict[str, Dict[str, str]] = {}
    for alias, fields in buckets.items():
        if fields.get("api_key") and fields.get("api_secret") and fields.get("passphrase"):
            if not fields.get("base_url") and global_base:
                fields["base_url"] = global_base
            complete[alias] = fields
    return complete


def list_accounts() -> list[str]:
    return sorted(_discover_credential_map().keys())


def capabilities() -> list[str]:
    # Read-only phase: activate balance in the generic /trade menu only.
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
        "api_key": fields["api_key"],
        "api_secret": fields["api_secret"],
        "passphrase": fields["passphrase"],
        "base_url": fields.get("base_url") or DEFAULT_SPOT_BASE,
    }


def _redact(text: Any, credentials: Optional[Mapping[str, str]] = None) -> str:
    rendered = str(text or "")
    if credentials:
        for k in ("api_key", "api_secret", "passphrase"):
            v = str(credentials.get(k) or "").strip()
            if len(v) >= 4:
                rendered = rendered.replace(v, "***")
    rendered = re.sub(r"(?i)(ACCESS-(?:KEY|SIGN|PASSPHRASE)\s*[:=]\s*)([^\s,;}\"']+)", r"\1***", rendered)
    return rendered


def _format_decimal(value: Decimal) -> str:
    q = value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    text = format(q.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _to_decimal(value: Any) -> Decimal:
    try:
        if value is None or value == "":
            return Decimal("0")
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _signed_request(
    credentials: Mapping[str, str],
    method: str,
    path: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    body: Any = None,
) -> Dict[str, Any]:
    base = str(credentials.get("base_url") or DEFAULT_SPOT_BASE).rstrip("/")
    method_u = method.upper().strip()
    query = urllib.parse.urlencode({k: v for k, v in dict(params or {}).items() if v is not None})
    body_str = ""
    body_bytes = None
    if body is not None:
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        body_bytes = body_str.encode("utf-8")
    ts = str(int(time.time() * 1000))
    request_path = path + (("?" + query) if query else "")
    prehash = f"{ts}{method_u}{request_path}{body_str}"
    digest = hmac.new(
        str(credentials["api_secret"]).encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature = base64.b64encode(digest).decode("ascii")
    url = f"{base}{request_path}"
    headers = {
        "ACCESS-KEY": str(credentials["api_key"]),
        "ACCESS-SIGN": signature,
        "ACCESS-PASSPHRASE": str(credentials["passphrase"]),
        "ACCESS-TIMESTAMP": ts,
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body_bytes, method=method_u, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw else {}
            if isinstance(parsed, dict):
                return parsed
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {exc.code} on WEEX {path}: {_redact(exc.reason, credentials)}") from exc
    parsed = json.loads(raw) if raw else {}
    if not isinstance(parsed, dict):
        raise RuntimeError("WEEX returned a non-object JSON payload.")
    return parsed


def _payload_ok(payload: Mapping[str, Any]) -> bool:
    code = payload.get("code")
    if code is None:
        return True
    return code in (0, "0", "00000", "success", "SUCCESS")


def _account_payload_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    if isinstance(data, Mapping) and ("balances" in data or "accountType" in data or "uid" in data):
        return data
    return payload


def _balance(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set WEEX_<ACCOUNT>_APIKEY, WEEX_<ACCOUNT>_APISECRET and WEEX_<ACCOUNT>_PASSPHRASE.",
        )
    try:
        payload = _signed_request(credentials, "GET", "/api/v3/account")
        if not _payload_ok(payload):
            return make_failure(
                operation="balance",
                exchange=name,
                account=credentials["account"],
                code="WEEX_ERROR",
                message=_redact(
                    sanitize_error_message(str(payload.get("msg") or payload.get("message") or payload.get("code") or "account failed")),
                    credentials,
                ),
            )
        account_data = _account_payload_data(payload)
        raw_balances = account_data.get("balances") or []
        if not isinstance(raw_balances, list):
            raw_balances = []

        rows = []
        stable_rows = []
        for row in raw_balances:
            if not isinstance(row, Mapping):
                continue
            asset = str(row.get("asset") or row.get("coin") or row.get("coinName") or "").upper().strip()
            free = _to_decimal(row.get("free") if row.get("free") is not None else row.get("available"))
            locked = _to_decimal(row.get("locked") if row.get("locked") is not None else row.get("frozen"))
            total = free + locked
            if not asset or total == 0:
                continue
            item = {
                "asset": asset,
                "free": _format_decimal(free),
                "locked": _format_decimal(locked),
                "total": _format_decimal(total),
                "_total_dec": total,
                "_free_dec": free,
                "_locked_dec": locked,
            }
            rows.append(item)
            if asset in _STABLE_CURRENCIES:
                stable_rows.append(item)

        if stable_rows:
            roll_rows = stable_rows
            unit = "USDT" if any(r["asset"] == "USDT" for r in stable_rows) else stable_rows[0]["asset"]
        elif rows:
            # No cross-currency conversion: expose the dominant native asset as returned.
            rows.sort(key=lambda r: r["_total_dec"], reverse=True)
            roll_rows = [rows[0]]
            unit = rows[0]["asset"]
        else:
            return make_failure(
                operation="balance",
                exchange=name,
                account=credentials["account"],
                code="WEEX_ERROR",
                message="No non-zero spot balances returned.",
            )

        total_value = sum((r["_total_dec"] for r in roll_rows), Decimal("0"))
        withdrawable = sum((r["_free_dec"] for r in roll_rows), Decimal("0"))
        locked = sum((r["_locked_dec"] for r in roll_rows), Decimal("0"))
        display_rows = [
            {k: v for k, v in r.items() if not k.startswith("_")}
            for r in sorted(rows, key=lambda r: r["_total_dec"], reverse=True)
        ]
        balance = normalize_balance(total_value, unit)
        return make_success(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            balance=balance,
            portfolio_summary=CanonicalPortfolioSummary(
                account_value=balance.value,
                withdrawable=normalize_balance(withdrawable, unit).value,
                margin_used=normalize_balance(locked, unit).value,
                total_position_value="0.00",
                unit=unit,
            ),
            data={
                "source": "spot_account_balances",
                "account_type": account_data.get("accountType"),
                "uid_present": bool(account_data.get("uid")),
                "unit": unit,
                "assets": display_rows[:30],
            },
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            code="WEEX_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _unsupported(operation: str, account: str) -> CanonicalResponse:
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code="UNSUPPORTED",
        message="WEEX agent currently supports balance only.",
    )


def execute(request: Mapping[str, Any]) -> CanonicalResponse:
    operation = str(request.get("operation") or "").strip()
    account = str(request.get("account") or "").strip()
    if operation == "balance":
        return _balance(account)
    return _unsupported(operation, account)
