"""Read-only AZverse spot-balance agent for KAM ``/tradespot``.

This module is deliberately separate from ``x_azverse_agent.py``: the latter
serves futures/perpetual ``/trade`` only. Credentials are shared through the
``AZVERSE_<ACCOUNT>_API_KEY``/``_KEY`` and
``AZVERSE_<ACCOUNT>_SECRET_KEY``/``_SECRET`` conventions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any, List, Mapping, Optional, Tuple

from ..canonical import make_failure, make_success, normalize_balance
from . import x_azverse_agent as futures

name = "azverse"

DEFAULT_SPOT_BASE = "https://s-api.azverse.xyz"
BALANCES_PATH = "/az/spot/balances"
API_TIMEOUT_SECONDS = 30
_USER_AGENT = "Hermes-KAM-AZverseSpotAgent/1.0"
_STABLE_ASSETS = frozenset({"USDT", "USDC", "USD", "FDUSD", "BUSD", "TUSD", "DAI"})


def list_accounts() -> List[str]:
    return sorted(futures._discover_credential_map())


def capabilities() -> List[str]:
    return ["balance"]


def _lookup_credentials(account: str) -> Optional[dict[str, str]]:
    alias = str(account or "").strip().lower()
    fields = futures._discover_credential_map().get(alias)
    if not fields:
        return None
    return {
        "account": alias,
        "api_key": fields["key"],
        "secret": fields["secret"],
        "spot_base": DEFAULT_SPOT_BASE,
    }


def _signed_headers(
    credentials: Mapping[str, str],
    path: str,
    timestamp: str,
) -> Tuple[dict[str, str], str]:
    signed_values = {
        "validate-appkey": str(credentials["api_key"]),
        "validate-timestamp": timestamp,
        "validate-recvwindow": "5000",
        "validate-algorithms": "HmacSHA256",
    }
    header_part = "&".join(f"{key}={signed_values[key]}" for key in sorted(signed_values))
    presign = f"{header_part}#GET#{path}"
    signature = hmac.new(
        str(credentials["secret"]).encode("utf-8"),
        presign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {**signed_values, "validate-signature": signature}, signature


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so authenticated headers stay on the AZverse origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _open_no_redirect(request: urllib.request.Request, timeout: int):
    opener = urllib.request.build_opener(_NoRedirectHandler())
    return opener.open(request, timeout=timeout)


def _unwrap_spot_envelope(payload: Mapping[str, Any]) -> Any:
    code = payload.get("rc")
    if code not in (None, 0, "0", 200, "200"):
        message = payload.get("mc") or payload.get("msg") or payload.get("message") or "AZverse rejected the request."
        raise RuntimeError(f"AZverse spot API {code}: {message}")
    if "result" not in payload:
        raise RuntimeError("AZverse spot response omitted result.")
    return payload.get("result")


def _signed_get(credentials: Mapping[str, str], path: str) -> Any:
    timestamp = str(int(time.time() * 1000))
    auth_headers, signature = _signed_headers(credentials, path, timestamp)
    url = f"{str(credentials.get('spot_base') or DEFAULT_SPOT_BASE).rstrip('/')}{path}"
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            **auth_headers,
        },
    )
    transport_error: Optional[str] = None
    body = ""
    try:
        with _open_no_redirect(request, API_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(error_body)
            message = str(parsed.get("mc") or parsed.get("msg") or parsed.get("message") or exc.reason)
            code = parsed.get("rc") or parsed.get("code") or exc.code
            transport_error = futures._redact(f"AZverse spot HTTP {exc.code} ({code}): {message}", credentials, (signature,))
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            transport_error = futures._redact(f"AZverse spot HTTP {exc.code}: {exc.reason}", credentials, (signature,))
        except Exception as body_exc:  # noqa: BLE001 - error-body readers are untrusted
            transport_error = futures._redact(body_exc, credentials, (signature,))
    except urllib.error.URLError as exc:
        transport_error = futures._redact(f"AZverse spot request failed: {exc.reason}", credentials, (signature,))
    except Exception as exc:  # noqa: BLE001
        transport_error = futures._redact(exc, credentials, (signature,))
    if transport_error is not None:
        raise RuntimeError(transport_error)

    processing_error: Optional[str] = None
    result: Any = None
    try:
        payload = json.loads(body)
        if not isinstance(payload, Mapping):
            processing_error = "AZverse spot returned a non-object response envelope."
        else:
            result = _unwrap_spot_envelope(payload)
    except json.JSONDecodeError:
        processing_error = "AZverse spot returned invalid JSON."
    except Exception as exc:  # noqa: BLE001
        processing_error = futures._redact(exc, credentials, (signature,))
    if processing_error is not None:
        raise RuntimeError(processing_error)
    return result


def _required_decimal(row: Mapping[str, Any], key: str) -> Decimal:
    return futures._required_decimal(row, key)


def _asset_rows(payload: Any) -> List[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise ValueError("AZverse spot balance result is not an object.")
    raw = payload.get("assets")
    if not isinstance(raw, list):
        raise ValueError("AZverse spot balance result omitted assets.")
    if any(not isinstance(row, Mapping) for row in raw):
        raise ValueError("AZverse spot balance contained a malformed asset row.")
    return list(raw)


def _handle_balance(account: str):
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            "balance",
            name,
            account,
            "MISSING_CREDENTIALS",
            "AZverse spot account credentials are not configured.",
        )
    try:
        rows = _asset_rows(_signed_get(credentials, BALANCES_PATH))
        assets: List[dict[str, str]] = []
        all_totals: List[Tuple[str, Decimal]] = []
        for row in rows:
            asset = str(row.get("currency") or row.get("coin") or "").strip().upper()
            if not asset:
                raise ValueError("AZverse spot asset row omitted currency.")
            total = _required_decimal(row, "totalAmount")
            free = _required_decimal(row, "availableAmount")
            frozen = _required_decimal(row, "frozenAmount")
            all_totals.append((asset, total))
            if total != 0 or free != 0 or frozen != 0:
                assets.append(
                    {
                        "asset": asset,
                        "free": futures._format_decimal(free),
                        "locked": futures._format_decimal(frozen),
                        "total": futures._format_decimal(total),
                    }
                )

        stable_total = sum((total for asset, total in all_totals if asset in _STABLE_ASSETS), Decimal("0"))
        if stable_total != 0 or not assets:
            primary_value, primary_unit = stable_total, "USDT"
        else:
            primary_unit = assets[0]["asset"]
            primary_value = _required_decimal(assets[0], "total")
        return make_success(
            "balance",
            name,
            credentials["account"],
            balance=normalize_balance(primary_value, primary_unit),
            data={
                "source": "azverse_spot_balances",
                "assets": assets,
                "nonzero_asset_count": len(assets),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            "balance",
            name,
            account,
            "AZVERSE_SPOT_BALANCE_ERROR",
            futures._redact(exc, credentials),
        )


def execute(request: Mapping[str, Any]):
    operation = str(request.get("operation") or "").strip().lower()
    account = str(request.get("account") or "").strip()
    if operation == "balance":
        return _handle_balance(account)
    return make_failure(
        operation or "unknown",
        name,
        account,
        "NOT_IMPLEMENTED",
        f"AZverse spot does not implement {operation!r}.",
    )
