"""MEXC Spot agent for /tradespot.

Uses the existing MEXC credential convention from ``x_mexc_agent.py``:
``MEXC_<ALIAS>_ACCESSKEY`` + ``MEXC_<ALIAS>_SECRETKEY`` (plus common
API-key/secret aliases). This agent is strictly separate from the futures
``x_mexc_agent.py`` used by /trade.

Read operations plus LIMIT ``new_order`` and grouped ``cancel_orders``.
Ladder, MARKET, and exchange-wide cancel-all remain disabled.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import socket
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..canonical import (
    CanonicalBalance,
    CanonicalCancelGroupResult,
    CanonicalInstrument,
    CanonicalLadderResult,
    CanonicalMarketPrice,
    CanonicalOrderGroup,
    CanonicalOrderResult,
    CanonicalResponse,
    make_failure,
    make_success,
    normalize_balance,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)

name = "mexc"

# MEXC spot enforces a per-child minimum notional of 1 USDT/USDC (cost.min
# in CCXT markets for every spot pair tested; minNotional is not always
# published in exchangeInfo filters). Stamp this on the resolved
# instrument so the exchange-neutral ladder planner can enforce it
# per-child without baking an exchange default into spot_ladder.py.
MEXC_SPOT_MIN_NOTIONAL = "1"

# MEXC `POST /api/v3/batchOrders` accepts at most 20 orders per call.
# Reference: https://www.mexc.com/api-docs/spot-v3/spot-account-trade/batch-orders
# Place and cancel share the same UID-based rate-limit bucket (12 req/s).
MEXC_SPOT_BATCH_MAX = 20

DEFAULT_SPOT_BASE = "https://api.mexc.com"
API_TIMEOUT_SECONDS = 20
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_KEY_ALIASES = ("ACCESSKEY", "ACCESS_KEY", "APIKEY", "API_KEY", "KEY")
_SECRET_ALIASES = ("SECRETKEY", "SECRET_KEY", "APISECRET", "API_SECRET", "SECRET")
_SPOT_BASE_ALIASES = ("SPOT_BASE", "SPOT_URL")
_STABLE_ASSETS = frozenset({"USDT", "USDC", "USD", "FDUSD", "BUSD", "TUSD", "DAI"})
_MARKET_CACHE: Dict[str, Any] = {"ts": 0.0, "symbols": [], "by_symbol": {}}
_MARKET_CACHE_TTL = 300.0


# ---------------------------------------------------------------------------
# Environment / credentials
# ---------------------------------------------------------------------------


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
    known = sorted(set(_KEY_ALIASES + _SECRET_ALIASES + _SPOT_BASE_ALIASES), key=len, reverse=True)
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
        elif suffix in _SPOT_BASE_ALIASES:
            slot.setdefault("spot_base", val.rstrip("/"))

    env_map = {k: v for k, (_, v, _) in _combined_mexc_env().items()}
    global_spot = (env_map.get("MEXC_SPOT_BASE") or env_map.get("MEXC_SPOT_URL") or "").strip().rstrip("/")

    complete: Dict[str, Dict[str, str]] = {}
    for alias, fields in buckets.items():
        if fields.get("access_key") and fields.get("secret_key"):
            if not fields.get("spot_base") and global_spot:
                fields["spot_base"] = global_spot
            complete[alias] = fields
    return complete


def list_accounts() -> List[str]:
    return sorted(_discover_credential_map().keys())


def capabilities() -> List[str]:
    # Phase 5: live ladder submission via MEXC spot batchOrders endpoint.
    # The agent receives the FINAL precomputed children from the wizard and
    # submits them in deterministic <=20-child batches. MARKET orders remain
    # rejected (`LIMIT_ONLY`); `cancel_order_group` / `cancel_order` /
    # `market_order` remain NOT_IMPLEMENTED.
    return [
        "balance",
        "orders",
        "open_orders",
        "list_instruments",
        "resolve_instrument",
        "market_price",
        "new_order",
        "cancel_orders",
        "ladder",
    ]


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
        "spot_base": fields.get("spot_base") or DEFAULT_SPOT_BASE,
    }


def _redact(text: Any, credentials: Optional[Mapping[str, str]] = None) -> str:
    rendered = str(text or "")
    if credentials:
        for key in ("access_key", "secret_key"):
            value = str(credentials.get(key) or "").strip()
            if len(value) >= 6:
                rendered = rendered.replace(value, "***")
    return rendered


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _json_request(method: str, url: str, headers: Mapping[str, str]) -> Any:
    req = urllib.request.Request(url, method=method.upper(), headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
    except (TimeoutError, socket.timeout) as exc:
        raise urllib.error.URLError("timed out") from exc
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except Exception:  # noqa: BLE001
            parsed = {"code": exc.code, "msg": exc.reason}
        if isinstance(parsed, dict):
            parsed.setdefault("http_status", exc.code)
            return parsed
        return {"http_status": exc.code, "msg": str(parsed)}
    parsed = json.loads(body)
    return parsed


def _public_request(path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
    qs = urllib.parse.urlencode(dict(params or {}))
    url = f"{DEFAULT_SPOT_BASE}{path}{('?' + qs) if qs else ''}"
    return _json_request("GET", url, {"Accept": "application/json", "User-Agent": _USER_AGENT})


def _signed_request(
    credentials: Mapping[str, str],
    method: str,
    path: str,
    params: Optional[Mapping[str, Any]] = None,
) -> Any:
    base = str(credentials.get("spot_base") or DEFAULT_SPOT_BASE).rstrip("/")
    payload = dict(params or {})
    payload["timestamp"] = str(int(time.time() * 1000))
    payload.setdefault("recvWindow", "5000")
    qs = urllib.parse.urlencode(payload)
    signature = hmac.new(credentials["secret_key"].encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"{base}{path}?{qs}&signature={signature}"
    return _json_request(
        method,
        url,
        {
            "X-MEXC-APIKEY": credentials["access_key"],
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )


@dataclass(frozen=True)
class BatchRequestOutcome:
    """Structured result of a single POST /api/v3/batchOrders attempt.

    ``kind`` is the critical classification used by ``_ladder`` to decide
    whether to mark a batch ACCEPTED, REJECTED, UNKNOWN, or NOT_ATTEMPTED.
    The classifier intentionally errs on the side of UNKNOWN whenever the
    network write may have reached MEXC — we never auto-retry ambiguous
    batches.
    """
    kind: str  # OK | HTTP_REJECTED | HTTP_RATE_LIMITED | HTTP_SERVER_ERROR | MALFORMED_JSON | AMBIGUOUS_TIMEOUT | LOCAL_ERROR | OTHER_LOCAL
    payload: Any = None
    http_status: Optional[int] = None
    mexc_code: Optional[Any] = None
    mexc_message: Optional[str] = None
    retry_after_seconds: Optional[int] = None


def _submit_batch_orders(
    credentials: Mapping[str, str],
    batch_payload: List[Mapping[str, Any]],
) -> BatchRequestOutcome:
    """Issue POST /api/v3/batchOrders and return a structured outcome.

    Never raises. The caller (``_ladder``) decides how to mark each child
    based on ``outcome.kind``:

    * ``OK``                  → parse ``payload`` as the MEXC per-child list.
    * ``HTTP_RATE_LIMITED``   → mark the entire batch UNKNOWN (the request
                                 may have reached MEXC; do not auto-retry).
    * ``HTTP_REJECTED``       → mark the entire batch REJECTED, preserve
                                 MEXC ``code``/``msg``.
    * ``HTTP_SERVER_ERROR``   → mark the entire batch REJECTED, preserve
                                 server-side reason if present.
    * ``AMBIGUOUS_TIMEOUT``   → mark the entire batch UNKNOWN (request may
                                 have been transmitted; do not auto-retry).
    * ``MALFORMED_JSON``      → mark the entire batch UNKNOWN (response
                                 received but unparseable).
    * ``LOCAL_ERROR``         → mark the entire batch UNKNOWN (the request
                                 may have reached MEXC; we lost visibility).
    """
    body = json.dumps(batch_payload)
    params = {
        "batchOrders": body,
        "recvWindow": "5000",
    }
    try:
        payload = _signed_request(credentials, "POST", "/api/v3/batchOrders", params)
    except (TimeoutError, socket.timeout):
        return BatchRequestOutcome(
            kind="AMBIGUOUS_TIMEOUT",
            mexc_message="connect/read timeout — request may have reached MEXC",
        )
    except urllib.error.URLError as exc:
        return BatchRequestOutcome(
            kind="AMBIGUOUS_TIMEOUT",
            mexc_message=f"URLError: {exc.reason}",
        )
    except Exception as exc:  # noqa: BLE001
        return BatchRequestOutcome(
            kind="LOCAL_ERROR",
            mexc_message=sanitize_error_message(str(exc)),
        )
    # Two paths: a list (200 OK with per-child results) OR a dict with
    # code/msg/http_status (HTTPError path inside _json_request).
    if isinstance(payload, list):
        # MEXC returns a JSON array directly when 200 OK. The caller will
        # handle it. We return the list in `payload` and tag kind=OK.
        return BatchRequestOutcome(
            kind="OK",
            payload=payload,
            http_status=200,
        )
    if not isinstance(payload, Mapping):
        # Non-dict, non-list → MALFORMED_JSON (or unexpected shape).
        return BatchRequestOutcome(
            kind="MALFORMED_JSON",
            mexc_message=f"unexpected payload type: {type(payload).__name__}",
        )
    http_status = payload.get("http_status")
    code = payload.get("code")
    msg = payload.get("msg")
    if isinstance(http_status, int) and 500 <= http_status < 600:
        # Per MEXC spot V3 docs, 5xx is "Internal error. Please try again" +
        # "Retry later after querying whether the operation already
        # completed." That explicit instruction tells us MEXC itself does
        # NOT guarantee the order was not processed. Per the conservative
        # classification rule: classify as UNKNOWN.
        return BatchRequestOutcome(
            kind="AMBIGUOUS_SERVER_ERROR",
            http_status=http_status,
            mexc_code=code,
            mexc_message=str(msg) if msg else None,
        )
    if isinstance(http_status, int) and http_status == 429:
        retry_after: Optional[int] = None
        ra = payload.get("retry_after")
        if isinstance(ra, (int, float)):
            retry_after = int(ra)
        elif isinstance(ra, str):
            try:
                retry_after = int(ra)
            except Exception:  # noqa: BLE001
                retry_after = None
        return BatchRequestOutcome(
            kind="HTTP_RATE_LIMITED",
            http_status=http_status,
            mexc_code=code,
            mexc_message=str(msg) if msg else None,
            retry_after_seconds=retry_after,
        )
    if isinstance(http_status, int) and 400 <= http_status < 600:
        return BatchRequestOutcome(
            kind="HTTP_REJECTED",
            http_status=http_status,
            mexc_code=code,
            mexc_message=str(msg) if msg else None,
        )
    if code is not None or msg:
        # dict without explicit http_status but with code/msg → treat as
        # REJECTED. This covers MEXC responses that carry only code/msg.
        return BatchRequestOutcome(
            kind="HTTP_REJECTED",
            http_status=http_status,
            mexc_code=code,
            mexc_message=str(msg) if msg else None,
        )
    # Bare dict (e.g. empty {} or unrecognized) → MALFORMED_JSON.
    return BatchRequestOutcome(
        kind="MALFORMED_JSON",
        http_status=http_status,
        mexc_message=str(msg) if msg else None,
    )


def _response_error(payload: Any, fallback: str) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    if payload.get("orderId") is not None or "balances" in payload:
        return None
    code = payload.get("code")
    if code in (None, 0, "0", 200, "200"):
        if "msg" not in payload and "message" not in payload:
            return None
        if code in (0, "0", 200, "200"):
            return None
        return None
    if "code" in payload or "msg" in payload or "message" in payload:
        return str(payload.get("msg") or payload.get("message") or payload.get("code") or fallback)
    return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0"))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _format_decimal(value: Optional[Decimal]) -> str:
    if value is None:
        return ""
    q = value.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    text = format(q.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _nonzero_balances(rows: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        asset = str(row.get("asset") or "").upper().strip()
        if not asset:
            continue
        free = _to_decimal(row.get("free"))
        locked = _to_decimal(row.get("locked"))
        total = free + locked
        if total <= 0:
            continue
        out.append(
            {
                "asset": asset,
                "free": _format_decimal(free),
                "locked": _format_decimal(locked),
                "total": _format_decimal(total),
            }
        )
    return sorted(out, key=lambda x: (x["asset"] not in _STABLE_ASSETS, x["asset"]))


def _parse_order(row: Mapping[str, Any]) -> Dict[str, Any]:
    orig = _to_decimal(row.get("origQty"))
    executed = _to_decimal(row.get("executedQty"))
    remaining = max(orig - executed, Decimal("0"))
    symbol = str(row.get("symbol") or "").upper()
    side = str(row.get("side") or "").upper()
    order_type = str(row.get("type") or "").upper()
    price = _to_decimal(row.get("price"))
    return {
        "symbol": symbol,
        "pair": _display_pair(symbol),
        "side": side,
        "type": order_type,
        "price": _format_decimal(price),
        "orig_qty": _format_decimal(orig),
        "executed_qty": _format_decimal(executed),
        "remaining_qty": _format_decimal(remaining),
        "status": str(row.get("status") or "").upper(),
        "order_id": str(row.get("orderId") or row.get("order_id") or ""),
        "client_order_id": str(row.get("clientOrderId") or ""),
    }


def _display_pair(symbol: str) -> str:
    sym = str(symbol or "").upper()
    for quote in ("USDT", "USDC", "FDUSD", "BTC", "ETH", "MX", "USD"):
        if sym.endswith(quote) and len(sym) > len(quote):
            return f"{sym[:-len(quote)]}/{quote}"
    return sym


def _group_orders(parsed: List[Dict[str, Any]]) -> List[CanonicalOrderGroup]:
    buckets: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for row in parsed:
        symbol = str(row.get("pair") or row.get("symbol") or "")
        side = str(row.get("side") or "").lower()
        order_type = str(row.get("type") or "")
        if not symbol or not side:
            continue
        buckets.setdefault((symbol, side, order_type), []).append(row)
    groups: List[CanonicalOrderGroup] = []
    for (symbol, side, order_type), rows in sorted(buckets.items()):
        total_size = sum((_to_decimal(r.get("remaining_qty")) for r in rows), Decimal("0"))
        prices = [_to_decimal(r.get("price")) for r in rows if _to_decimal(r.get("price")) > 0]
        notional = sum((_to_decimal(r.get("price")) * _to_decimal(r.get("remaining_qty")) for r in rows), Decimal("0"))
        vwap = notional / total_size if total_size > 0 and prices else Decimal("0")
        groups.append(
            CanonicalOrderGroup(
                symbol=symbol,
                side=side,
                order_count=len(rows),
                total_size=_format_decimal(total_size),
                vwap=_format_decimal(vwap) if vwap > 0 else "",
                min_price=_format_decimal(min(prices)) if prices else "",
                max_price=_format_decimal(max(prices)) if prices else "",
                display_type=order_type,
            )
        )
    return groups


# ---------------------------------------------------------------------------
# Market metadata / eligibility
# ---------------------------------------------------------------------------


def _exchange_info() -> Dict[str, Any]:
    now = time.time()
    if _MARKET_CACHE["symbols"] and now - float(_MARKET_CACHE["ts"]) < _MARKET_CACHE_TTL:
        return {"symbols": list(_MARKET_CACHE["symbols"]), "by_symbol": dict(_MARKET_CACHE["by_symbol"])}
    payload = _public_request("/api/v3/exchangeInfo")
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise RuntimeError(str(payload.get("msg") if isinstance(payload, dict) else "exchangeInfo failed"))
    symbols = [row for row in payload.get("symbols") or [] if isinstance(row, Mapping)]
    by_symbol = {str(row.get("symbol") or "").upper(): dict(row) for row in symbols if row.get("symbol")}
    _MARKET_CACHE.update({"ts": now, "symbols": symbols, "by_symbol": by_symbol})
    return {"symbols": list(symbols), "by_symbol": dict(by_symbol)}


def _self_symbols(credentials: Mapping[str, str]) -> Tuple[Optional[set[str]], Optional[str]]:
    payload = _signed_request(credentials, "GET", "/api/v3/selfSymbols")
    err = _response_error(payload, "selfSymbols failed")
    if err:
        return None, err
    raw: Any
    if isinstance(payload, dict):
        raw = payload.get("symbols") or payload.get("data") or payload.get("symbol")
    else:
        raw = payload
    if isinstance(raw, list):
        return {str(x).upper() for x in raw if str(x).strip()}, None
    return None, "selfSymbols returned an unsupported shape"


def _market_row(symbol: str) -> Optional[Dict[str, Any]]:
    key = str(symbol or "").upper().replace("/", "").replace("-", "").replace("_", "")
    return _exchange_info()["by_symbol"].get(key)


def _instrument_from_market(row: Mapping[str, Any]) -> Dict[str, Any]:
    symbol = str(row.get("symbol") or "").upper()
    raw_filters = row.get("filters")
    filters: List[Any] = raw_filters if isinstance(raw_filters, list) else []
    min_qty = ""
    max_qty = ""
    lot_step = ""
    tick_size = ""
    min_notional = ""
    for f in filters:
        if not isinstance(f, Mapping):
            continue
        ftype = str(f.get("filterType") or "")
        if ftype == "LOT_SIZE":
            lot_step = str(f.get("stepSize") or "")
            min_qty = str(f.get("minQty") or "")
            max_qty = str(f.get("maxQty") or "")
        elif ftype == "PRICE_FILTER":
            tick_size = str(f.get("tickSize") or "")
        elif ftype in {"MIN_NOTIONAL", "NOTIONAL"}:
            min_notional = str(f.get("minNotional") or f.get("notional") or "")
    size_step = _decimal_step(lot_step)
    if size_step <= 0:
        size_step = _decimal_step(row.get("baseSizePrecision"))
    if size_step <= 0:
        size_step = _places_increment(row.get("baseAssetPrecision"))
    price_tick = _decimal_step(tick_size)
    if price_tick <= 0:
        for key in ("quotePrecision", "quoteAssetPrecision"):
            price_tick = _places_increment(row.get(key))
            if price_tick > 0:
                break
    base = str(row.get("baseAsset") or "")
    quote = str(row.get("quoteAsset") or "")
    order_types = [str(x) for x in row.get("orderTypes") or []]
    is_allowed = bool(row.get("isSpotTradingAllowed"))
    status = str(row.get("status") or "")
    # MEXC spot enforces a per-child minimum of 1 USDT/USDC; exchangeInfo
    # does NOT publish MIN_NOTIONAL consistently, so the agent stamps the
    # exchange's policy on the resolved instrument for the planner to
    # consume. Callers can override via the dict before passing into
    # spot_ladder.compute_ladder_with_min_notional.
    resolved_min_notional = _decimal_step(min_notional)
    if resolved_min_notional <= 0:
        resolved_min_notional = _decimal_step(MEXC_SPOT_MIN_NOTIONAL)
    return {
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "baseAsset": base,
        "quoteAsset": quote,
        "display_name": f"{base}/{quote}" if base and quote else symbol,
        "status": status,
        "isSpotTradingAllowed": is_allowed,
        "orderTypes": order_types,
        "size_step": _format_step(size_step),
        "price_tick": _format_step(price_tick),
        "min_qty": _format_step(_decimal_step(min_qty)),
        "max_qty": _format_step(_decimal_step(max_qty)),
        "min_notional": _format_step(resolved_min_notional),
        "tick_size": tick_size,
        "step_size": lot_step,
        "api_eligible": status.upper() in {"1", "ENABLED", "TRADING"} and is_allowed,
    }


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _missing_account(operation: str, account: str) -> CanonicalResponse:
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code="ACCOUNT_NOT_FOUND",
        message="Set MEXC_<ACCOUNT>_ACCESSKEY and MEXC_<ACCOUNT>_SECRETKEY.",
    )


def _balance(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return _missing_account("balance", account)
    try:
        payload = _signed_request(credentials, "GET", "/api/v3/account")
        err = _response_error(payload, "account failed")
        if err:
            return make_failure(
                operation="balance",
                exchange=name,
                account=credentials["account"],
                code="MEXC_SPOT_AUTH_FAILED",
                message=_redact(sanitize_error_message(err), credentials),
                exchange_reason=_redact(sanitize_error_message(err), credentials),
            )
        balances = _nonzero_balances(payload.get("balances") if isinstance(payload, dict) else [])
        stable_total = sum((_to_decimal(x["total"]) for x in balances if x["asset"] in _STABLE_ASSETS), Decimal("0"))
        unit = "USDT"
        value = stable_total
        if value <= 0 and balances:
            unit = balances[0]["asset"]
            value = _to_decimal(balances[0]["total"])
        return make_success(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            balance=normalize_balance(value, unit),
            data={
                "source": "mexc_spot_account",
                "accountType": payload.get("accountType") if isinstance(payload, dict) else None,
                "permissions": payload.get("permissions") if isinstance(payload, dict) else None,
                "canTrade": payload.get("canTrade") if isinstance(payload, dict) else None,
                "canWithdraw": payload.get("canWithdraw") if isinstance(payload, dict) else None,
                "canDeposit": payload.get("canDeposit") if isinstance(payload, dict) else None,
                "assets": balances[:50],
                "nonzero_asset_count": len(balances),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            code="MEXC_SPOT_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _orders(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return _missing_account("orders", account)
    try:
        payload = _signed_request(credentials, "GET", "/api/v3/openOrders")
        err = _response_error(payload, "openOrders failed")
        if err:
            return make_failure(
                operation="orders",
                exchange=name,
                account=credentials["account"],
                code="MEXC_SPOT_ORDERS_FAILED",
                message=_redact(sanitize_error_message(err), credentials),
                exchange_reason=_redact(sanitize_error_message(err), credentials),
            )
        rows = payload if isinstance(payload, list) else payload.get("orders", []) if isinstance(payload, dict) else []
        parsed = [_parse_order(row) for row in rows if isinstance(row, Mapping)]
        groups = _group_orders(parsed)
        return make_success(
            operation="orders",
            exchange=name,
            account=credentials["account"],
            open_order_count=len(parsed),
            order_groups=groups,
            data={"source": "mexc_spot_openOrders", "orders": parsed[:100]},
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="orders",
            exchange=name,
            account=credentials["account"],
            code="MEXC_SPOT_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _list_instruments(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    self_set: Optional[set[str]] = None
    self_error: Optional[str] = None
    if credentials is not None:
        try:
            self_set, self_error = _self_symbols(credentials)
        except Exception as exc:  # noqa: BLE001
            self_error = _redact(sanitize_error_message(str(exc)), credentials)
    try:
        q = str(request.get("query") or request.get("symbol") or "").strip().upper().replace("/", "")
        rows = _exchange_info()["symbols"]
        out: List[Dict[str, Any]] = []
        for row in rows:
            info = _instrument_from_market(row)
            symbol = str(info["symbol"]).upper()
            if q and q not in symbol and q not in str(info.get("baseAsset") or "").upper():
                continue
            if self_set is not None:
                info["api_enabled_for_key"] = symbol in self_set
                info["api_eligible"] = bool(info["api_eligible"] and symbol in self_set)
            out.append(info)
            if len(out) >= int(request.get("limit") or 100):
                break
        instruments = [
            CanonicalInstrument(
                requested_symbol=str(item["symbol"]),
                symbol=str(item["symbol"]),
                display_name=str(item.get("display_name") or item["symbol"]),
                price_increment=str(item.get("price_tick") or ""),
                size_increment=str(item.get("size_step") or ""),
                minimum_size=str(item.get("min_qty") or ""),
            ).to_dict()
            for item in out
        ]
        for idx, item in enumerate(out):
            instruments[idx].update(item)
        return make_success(
            operation="list_instruments",
            exchange=name,
            account=str(account or ""),
            data={
                "source": "mexc_spot_exchangeInfo",
                "instruments": instruments,
                "count": len(instruments),
                "selfSymbols_available": self_set is not None,
                "selfSymbols_count": len(self_set) if self_set is not None else None,
                "selfSymbols_error": self_error,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=str(account or ""),
            code="MEXC_SPOT_MARKETS_FAILED",
            message=sanitize_error_message(str(exc)),
        )


def _resolve_instrument(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or request.get("query") or "").strip().upper()
    if not requested:
        base = str(request.get("base") or request.get("baseAsset") or "").strip().upper()
        quote = str(request.get("quote") or request.get("quoteAsset") or "").strip().upper()
        if base and quote:
            requested = f"{base}{quote}"
    row = _market_row(requested)
    if row is None:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=str(account or ""),
            code="INSTRUMENT_NOT_FOUND",
            message="MEXC spot symbol not found.",
        )
    info = _instrument_from_market(row)
    if not info.get("size_step") or not info.get("price_tick"):
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=str(account or ""),
            code="INSTRUMENT_CONSTRAINTS_UNAVAILABLE",
            message="MEXC spot instrument is missing size or price constraints.",
        )
    inst = CanonicalInstrument(
        requested_symbol=requested,
        symbol=str(info["symbol"]),
        display_name=str(info.get("display_name") or info["symbol"]),
        price_increment=str(info.get("price_tick") or ""),
        size_increment=str(info.get("size_step") or ""),
        minimum_size=str(info.get("min_qty") or ""),
    )
    return make_success(
        operation="resolve_instrument",
        exchange=name,
        account=str(account or ""),
        instrument=inst,
        data={"instrument": {**inst.to_dict(), **info}},
    )


def _market_price(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or "").strip().upper()
    row = _market_row(requested)
    if row is None:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=str(account or ""),
            code="INSTRUMENT_NOT_FOUND",
            message="MEXC spot symbol not found.",
        )
    symbol = str(row.get("symbol") or requested).upper()
    payload = _public_request("/api/v3/ticker/price", {"symbol": symbol})
    if not isinstance(payload, dict) or payload.get("price") is None:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=str(account or ""),
            code="MEXC_SPOT_PRICE_FAILED",
            message=sanitize_error_message(str(payload)),
        )
    price = str(payload.get("price"))
    return make_success(
        operation="market_price",
        exchange=name,
        account=str(account or ""),
        market_price=CanonicalMarketPrice(
            requested_symbol=requested,
            market=symbol,
            mark_price=price,
            price=price,
            last_external_price=price,
        ),
        data={"symbol": symbol, "price": price},
    )


def _unsupported(operation: str, account: str) -> CanonicalResponse:
    return make_failure(
        operation=operation,
        exchange=name,
        account=str(account or ""),
        code="NOT_IMPLEMENTED",
        message="MEXC Spot does not enable this write operation.",
    )


def _ladder_validate_child(child: Mapping[str, Any]) -> Optional[str]:
    if not isinstance(child, Mapping):
        return "CHILD_NOT_MAPPING"
    required = ("symbol", "side", "quantity", "price", "client_order_id")
    for key in required:
        if not str(child.get(key) or "").strip():
            return f"MISSING_{key.upper()}"
    side = str(child.get("side") or "").strip().upper()
    if side not in {"BUY", "SELL"}:
        return "INVALID_SIDE"
    order_type = str(child.get("type") or child.get("order_type") or "LIMIT").strip().upper()
    if order_type != "LIMIT":
        return "MARKET_NOT_ALLOWED"  # surfaced as LIMIT_ONLY
    return None


def _ladder_plan_batches(children: List[Mapping[str, Any]]) -> List[List[Mapping[str, Any]]]:
    """Split children into deterministic <=20-child batches, oldest-first."""
    if not children:
        return []
    batches: List[List[Mapping[str, Any]]] = []
    for i in range(0, len(children), MEXC_SPOT_BATCH_MAX):
        batches.append(list(children[i : i + MEXC_SPOT_BATCH_MAX]))
    return batches


def _ladder_child_status(child_result: Mapping[str, Any]) -> str:
    """Classify a single child as ACCEPTED / REJECTED / UNKNOWN.

    MEXC batchOrders returns per-order dicts with either an ``orderId``
    (success) or a ``code``/``msg`` (rejection). Anything that is not a
    dict or is missing both is treated as UNKNOWN.
    """
    if not isinstance(child_result, Mapping):
        return "UNKNOWN"
    if child_result.get("orderId") is not None:
        return "ACCEPTED"
    if child_result.get("code") is not None or child_result.get("msg"):
        return "REJECTED"
    return "UNKNOWN"


# MEXC spot V3 docs: clientOrderId must be 8-32 chars. We pick a
# deterministic, collision-resistant format that survives 500 children
# across multiple execution IDs without needing the [:32] truncation slice.
#
# Format:    ts_<8hex_exec>_<6decimal_idx>
# Length:    3 + 8 + 1 + 6 = 18 chars     (within 8-32 constraint)
# Capacity:  256 execs  ×  1,000,000 children per exec
#
# The hex execution_id suffix prevents accidental collisions across
# different ladder sessions of the same minute.
MEXC_SPOT_MAX_CLIENT_ORDER_ID_LEN = 32
MEXC_SPOT_MIN_CLIENT_ORDER_ID_LEN = 8
_LADDER_CLIENT_ID_PREFIX = "ts_"
_LADDER_EXEC_ID_HEX_LEN = 8
_LADDER_INDEX_DEC_LEN = 6


def _ladder_new_execution_id() -> str:
    """Return an 8-hex-char execution ID suitable for client_order_ids.

    Combined with the ``_LADDER_EXEC_ID_HEX_LEN`` / ``_LADDER_INDEX_DEC_LEN``
    constants this format gives 256 unique executions × 1M children per exec
    without collisions.
    """
    return secrets.token_hex(_LADDER_EXEC_ID_HEX_LEN // 2)[:_LADDER_EXEC_ID_HEX_LEN]


def _ladder_new_client_order_id(execution_id: str, child_index: int) -> str:
    """Return a deterministic client_order_id in MEXC-valid 8-32 char range.

    The format ``ts_<8hex_exec>_<6decimal_idx>`` keeps every ID unique
    across up to 1M children per execution without relying on a
    truncation slice. Callers MUST use this generator instead of the
    raw ``client_order_id`` from the upstream request so we never depend
    on ``[:32]`` to remove ambiguity.
    """
    if not (isinstance(execution_id, str) and len(execution_id) == _LADDER_EXEC_ID_HEX_LEN):
        raise ValueError(f"execution_id must be {_LADDER_EXEC_ID_HEX_LEN} hex chars")
    idx_str = f"{int(child_index):0{_LADDER_INDEX_DEC_LEN}d}"
    cid = f"{_LADDER_CLIENT_ID_PREFIX}{execution_id}_{idx_str}"
    if not (MEXC_SPOT_MIN_CLIENT_ORDER_ID_LEN <= len(cid) <= MEXC_SPOT_MAX_CLIENT_ORDER_ID_LEN):
        raise ValueError(f"client_order_id len {len(cid)} out of MEXC bounds")
    return cid


def _ladder_record_dir(account: str) -> Path:
    """Return the durable-record directory for ``account``."""
    base = Path(os.environ.get("HERMES_HOME") or "/root/.hermes") / "cache" / "tradespot" / "mexc-spot"
    # Sanitize the account alias to keep it filesystem-safe.
    safe_account = re.sub(r"[^A-Za-z0-9_.-]", "_", account or "unknown") or "unknown"
    return base / safe_account


def _ladder_record_path(account: str, execution_id: str) -> Path:
    """Return the atomic-record JSON path for ``account/execution_id``."""
    return _ladder_record_dir(account) / f"{execution_id}.json"


def _ladder_persist_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically (tmp + fsync + os.replace).

    Never raises. Any error during the write is captured in the parent
    function's error log; we never crash the ladder pipeline on a
    persistence failure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:  # noqa: BLE001
                # Some filesystems (e.g. tmpfs on some kernels) do not
                # support fsync. The atomic rename still gives us
                # crash-consistency at the inode level.
                pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:  # noqa: BLE001
            pass
        raise


def _ladder_load_record(account: str, execution_id: str) -> Optional[Dict[str, Any]]:
    """Read a durable record from disk. None if missing or malformed."""
    path = _ladder_record_path(account, execution_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    return data


def list_unresolved_ladders(account: str) -> List[Dict[str, Any]]:
    """List durable ladder records for ``account`` whose submission contains
    at least one UNKNOWN or NOT_ATTEMPTED child. Read-only; never submits.

    Returned records are stable, JSON-clean dicts (NOT the live
    CanonicalLadderResult dataclass). The wizard uses this to rebuild the
    reconciliation UI after a gateway restart.

    A record is "unresolved" iff:
      * at least one child has ``submission_classification`` ∈ {UNKNOWN,
        NOT_ATTEMPTED}, OR
      * at least one child has ``reconciliation_classification``
        == QUERY_UNKNOWN
    """
    base = _ladder_record_dir(account)
    if not base.exists():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(base.glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(rec, dict):
            continue
        children = rec.get("children") or []
        unresolved = any(
            isinstance(c, dict)
            and (
                c.get("submission_classification") in {"UNKNOWN", "NOT_ATTEMPTED"}
                or c.get("reconciliation_classification") == "QUERY_UNKNOWN"
            )
            for c in children
        )
        if not unresolved:
            continue
        out.append(rec)
    return out


def reconcile_batch(
    account: str,
    symbol: str,
    expected_client_order_ids: List[str],
    known_exchange_order_ids: Optional[List[str]] = None,
    execution_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """GET-only reconciliation against the live MEXC spot account.

    Algorithm:
      1. GET /api/v3/openOrders?symbol=<sym>
         (open orders, server-side filter by symbol)
      2. GET /api/v3/allOrders?symbol=<sym>&limit=1000
         (recent history, filtered by symbol)
      3. For every expected child still unresolved after steps 1+2:
         GET /api/v3/order?symbol=<sym>&origClientOrderId=<cid>
         (single-order lookup, per MEXC spot V3 Query Order docs)

    Classification (exactly one per expected child):
      FOUND_OPEN              matched in /openOrders
      FOUND_FILLED            matched in /allOrders with status FILLED
      FOUND_CANCELED          matched in /allOrders with status CANCELED / EXPIRED
      FOUND_OTHER_TERMINAL    matched in /allOrders with another terminal state
      NOT_FOUND               every applicable GET returned success and the
                              expected client ID was absent in the response
      QUERY_UNKNOWN           any required GET failed/timed out/malformed;
                              reconciliation itself is incomplete

    NEVER performs POST / DELETE. NEVER retries. NEVER re-submits.
    """
    credentials = _lookup_credentials(account)
    if credentials is None:
        raise ValueError(f"unknown MEXC account '{account}'")

    # Track which expected IDs are still unresolved as we walk the GETs.
    remaining = {str(cid): i for i, cid in enumerate(expected_client_order_ids)}
    rows: Dict[int, Dict[str, Any]] = {}
    # Pre-populate with NOT_FOUND placeholders so the result has one row
    # per expected child even if reconciliation times out.
    for i, cid in enumerate(expected_client_order_ids):
        rows[i] = {
            "index": i,
            "client_order_id": str(cid),
            "exchange_order_id": None,
            "classification": "NOT_FOUND",
            "exchange_status": None,
            "executed_qty": None,
            "cumulative_quote_qty": None,
            "query_source": None,
            "query_error": None,
        }
    # Optional: short-circuit IDs we already know.
    for kid in known_exchange_order_ids or []:
        kid_s = str(kid or "")
        if kid_s and kid_s in remaining:
            rows[remaining[kid_s]]["exchange_order_id"] = kid_s

    def _status_from_open_or_all(payload: Any, kind: str) -> List[Mapping[str, Any]]:
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, Mapping)]
        if isinstance(payload, Mapping):
            for k in ("orders", "rows", "data"):
                v = payload.get(k)
                if isinstance(v, list):
                    return [r for r in v if isinstance(r, Mapping)]
        return []

    def _classify_status(status: str) -> str:
        s = (status or "").upper()
        if s in {"NEW", "PARTIALLY_FILLED", "PENDING_NEW"}:
            return "FOUND_OPEN"
        if s in {"FILLED"}:
            return "FOUND_FILLED"
        if s in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}:
            return "FOUND_CANCELED"
        return "FOUND_OTHER_TERMINAL"

    def _match_order_by_cid(order: Mapping[str, Any], cid: str) -> bool:
        return str(order.get("clientOrderId") or "") == cid or str(order.get("origClientOrderId") or "") == cid

    # Step 1+2: openOrders + allOrders, filtered by symbol.
    query_failed = False
    query_error_msg: Optional[str] = None

    try:
        open_payload = _signed_request(credentials, "GET", "/api/v3/openOrders", {"symbol": symbol})
        for order in _status_from_open_or_all(open_payload, "openOrders"):
            cid = str(order.get("clientOrderId") or order.get("origClientOrderId") or "")
            if cid in remaining:
                idx = remaining.pop(cid)
                rows[idx].update({
                    "classification": _classify_status(str(order.get("status") or "")),
                    "exchange_status": str(order.get("status") or ""),
                    "exchange_order_id": str(order.get("orderId") or ""),
                    "executed_qty": str(order.get("executedQty") or ""),
                    "cumulative_quote_qty": str(order.get("cumulativeQuoteQty") or ""),
                    "query_source": "openOrders",
                    "query_error": None,
                })
                # best-effort: also capture orderId for FILLED if same id
                # appears in allOrders later.
    except Exception as exc:  # noqa: BLE001
        query_failed = True
        query_error_msg = sanitize_error_message(str(exc))

    try:
        all_payload = _signed_request(credentials, "GET", "/api/v3/allOrders", {"symbol": symbol, "limit": 1000})
        for order in _status_from_open_or_all(all_payload, "allOrders"):
            cid = str(order.get("clientOrderId") or order.get("origClientOrderId") or "")
            if cid in remaining:
                idx = remaining.pop(cid)
                rows[idx].update({
                    "classification": _classify_status(str(order.get("status") or "")),
                    "exchange_status": str(order.get("status") or ""),
                    "exchange_order_id": str(order.get("orderId") or ""),
                    "executed_qty": str(order.get("executedQty") or ""),
                    "cumulative_quote_qty": str(order.get("cumulativeQuoteQty") or ""),
                    "query_source": "allOrders",
                    "query_error": None,
                })
    except Exception as exc:  # noqa: BLE001
        query_failed = True
        query_error_msg = sanitize_error_message(str(exc))

    # Step 3: per-ID single-order lookup for unresolved.
    if remaining:
        for cid, idx in list(remaining.items()):
            try:
                single = _signed_request(
                    credentials,
                    "GET",
                    "/api/v3/order",
                    {"symbol": symbol, "origClientOrderId": cid},
                )
                if isinstance(single, Mapping) and _match_order_by_cid(single, cid):
                    rows[idx].update({
                        "classification": _classify_status(str(single.get("status") or "")),
                        "exchange_status": str(single.get("status") or ""),
                        "exchange_order_id": str(single.get("orderId") or ""),
                        "executed_qty": str(single.get("executedQty") or ""),
                        "cumulative_quote_qty": str(single.get("cumulativeQuoteQty") or ""),
                        "query_source": "single",
                        "query_error": None,
                    })
                    remaining.pop(cid, None)
            except Exception as exc:  # noqa: BLE001
                # Single-order failure does NOT mark the whole query as
                # QUERY_UNKNOWN; only bulk failures do. We still note the
                # error so the wizard can surface it.
                rows[idx]["query_error"] = sanitize_error_message(str(exc))
                query_failed = True
                if query_error_msg is None:
                    query_error_msg = rows[idx]["query_error"]

    # If any of the bulk queries failed, unresolved rows that we did not
    # already classify are QUERY_UNKNOWN (NOT NOT_FOUND). The user's spec
    # requires "all successful applicable GET reconciliation sources were
    # checked" for NOT_FOUND — if some failed, the source set is incomplete.
    if query_failed:
        for cid, idx in remaining.items():
            if rows[idx]["classification"] == "NOT_FOUND":
                rows[idx]["classification"] = "QUERY_UNKNOWN"
                if rows[idx]["query_error"] is None:
                    rows[idx]["query_error"] = query_error_msg

    # Return rows in input order. Caller can group by classification.
    return [rows[i] for i in range(len(expected_client_order_ids))]


def update_ladder_reconciliation(
    account: str,
    execution_id: str,
    reconciled: List[Dict[str, Any]],
    query_error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Update the durable record with reconciliation_classification per child.

    Read-modify-write. Returns the updated record (or None if no record
    exists for this account/execution_id). NEVER raises.
    """
    rec = _ladder_load_record(account, execution_id)
    if rec is None:
        return None
    children = rec.get("children") or []
    if not isinstance(children, list):
        return rec
    rec_index = {c.get("client_order_id"): c for c in children if isinstance(c, dict)}
    now = int(time.time())
    for row in reconciled:
        cid = row.get("client_order_id")
        if cid in rec_index:
            rec_index[cid]["reconciliation_classification"] = row.get("classification")
            rec_index[cid]["exchange_order_id"] = row.get("exchange_order_id") or rec_index[cid].get("exchange_order_id")
            rec_index[cid]["last_reconciled_status"] = row.get("exchange_status")
    rec["updated_at"] = now
    rec["last_reconciliation_at"] = now
    if query_error:
        rec["last_reconciliation_error"] = query_error
    try:
        _ladder_persist_atomic(_ladder_record_path(account, execution_id), rec)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).error(
            "ladder_persist_failed account=%s execution_id=%s err=%s",
            account, execution_id, sanitize_error_message(str(exc)),
        )
    return rec


def _vwap(children: List[Mapping[str, Any]], qty_key: str, price_key: str) -> Optional[Decimal]:
    """Decimal VWAP across accepted children. None if no accepted qty."""
    total_qty = Decimal("0")
    total_notional = Decimal("0")
    for c in children:
        try:
            q = Decimal(str(c.get(qty_key) or "0"))
            p = Decimal(str(c.get(price_key) or "0"))
        except Exception:  # noqa: BLE001
            continue
        if q <= 0 or p <= 0:
            continue
        total_qty += q
        total_notional += q * p
    if total_qty <= 0:
        return None
    return total_notional / total_qty


def _ladder(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=str(account or ""),
            code="MISSING_ACCOUNT",
            message="No credentials configured for that MEXC spot account.",
        )
    children_in = request.get("children")
    if not isinstance(children_in, list) or not children_in:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="MISSING_CHILDREN",
            message="Ladder request must contain a non-empty list of FINAL precomputed children.",
        )
    if len(children_in) > 500:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="LADDER_TOO_LARGE",
            message=f"Ladder of {len(children_in)} children exceeds the 500-child safety cap.",
        )
    # Validate every child BEFORE any network write.
    for idx, child in enumerate(children_in):
        err = _ladder_validate_child(child)
        if err == "MARKET_NOT_ALLOWED":
            return make_failure(
                operation="ladder",
                exchange=name,
                account=credentials["account"],
                code="LIMIT_ONLY",
                message=f"Child {idx}: MARKET orders are not supported by /tradespot ladder. Use LIMIT only.",
            )
        if err:
            return make_failure(
                operation="ladder",
                exchange=name,
                account=credentials["account"],
                code=err,
                message=f"Child {idx}: {err.replace('_', ' ').lower()}.",
            )

    symbol = str(children_in[0].get("symbol") or "").strip().upper().replace("/", "")
    side = str(children_in[0].get("side") or "").strip().upper()
    requested_order_count = len(children_in)
    requested_volume = sum(
        (Decimal(str(c.get("quantity") or "0")) for c in children_in),
        Decimal("0"),
    )

    # PERSIST BEFORE POST.
    # Generate the durable execution_id BEFORE any network write. Use
    # the wizard's execution_id if the upstream supplied one (so the
    # confirm-token round-trip matches), otherwise mint a fresh 8-hex
    # ID via _ladder_new_execution_id().
    upstream_execution_id = str(request.get("execution_id") or "").strip()
    if upstream_execution_id:
        execution_id = upstream_execution_id[:_LADDER_EXEC_ID_HEX_LEN]
        if len(execution_id) != _LADDER_EXEC_ID_HEX_LEN:
            execution_id = _ladder_new_execution_id()
    else:
        execution_id = _ladder_new_execution_id()

    # Regenerate every child with the deterministic client_order_id
    # generator. This replaces any raw upstream client_order_id (which
    # may be too long, contain forbidden chars, or collide) and gives us
    # collision-free IDs even for 500 children.
    prepared_children: List[Dict[str, Any]] = []
    for idx, child in enumerate(children_in):
        cid = _ladder_new_client_order_id(execution_id, idx)
        prepared = dict(child)
        prepared["client_order_id"] = cid
        prepared_children.append(prepared)

    durable_record: Dict[str, Any] = {
        "version": 1,
        "execution_id": execution_id,
        "exchange": "mexc",
        "account": credentials["account"],
        "instrument": symbol,
        "exchange_symbol": symbol,
        "side": side,
        "distribution": str(request.get("distribution") or ""),
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
        "submission_state": "PREPARED",
        "planned_vwap": _format_decimal(_vwap(
            [{"quantity": c.get("quantity"), "price": c.get("price")} for c in children_in],
            "quantity", "price",
        )),
        "children": [
            {
                "index": idx,
                "client_order_id": c["client_order_id"],
                "exchange_order_id": None,
                "quantity": str(c.get("quantity") or ""),
                "price": str(c.get("price") or ""),
                "submission_classification": "NOT_ATTEMPTED",
                "reconciliation_classification": None,
                "batch_index": None,
            }
            for idx, c in enumerate(prepared_children)
        ],
        "last_reconciliation_at": None,
        "last_reconciliation_error": None,
    }
    try:
        _ladder_persist_atomic(_ladder_record_path(credentials["account"], execution_id), durable_record)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).error(
            "ladder_persist_pre_failed account=%s execution_id=%s err=%s",
            credentials["account"], execution_id, sanitize_error_message(str(exc)),
        )
        # We do NOT fail the ladder on a persistence error — the user may
        # still want to attempt the live submission. We only warn.

    batches = _ladder_plan_batches(prepared_children)
    batch_records: List[Dict[str, Any]] = []
    accepted_qty = Decimal("0")
    accepted_notional = Decimal("0")
    accepted_order_ids: List[str] = []
    accepted_count = 0
    rejected_count = 0
    unknown_count = 0
    not_attempted_count = 0
    stopped_early = False
    exchange_reason: Optional[str] = None

    for batch_index, batch in enumerate(batches):
        batch_payload = [
            {
                "symbol": str(c.get("symbol") or symbol),
                "side": str(c.get("side") or side).upper(),
                "type": "LIMIT",
                "timeInForce": "GTC",
                "quantity": str(c.get("quantity") or ""),
                "price": str(c.get("price") or ""),
                "newClientOrderId": str(c.get("client_order_id") or "")[:32],
            }
            for c in batch
        ]
        params = {"batchOrders": json.dumps(batch_payload)}
        record: Dict[str, Any] = {
            "batch_index": batch_index,
            "children_attempted": [str(c.get("client_order_id") or "") for c in batch],
            "planned_vwap": _vwap(
                [{"quantity": c.get("quantity"), "price": c.get("price")} for c in batch],
                "quantity",
                "price",
            ),
        }
        outcome = _submit_batch_orders(credentials, batch_payload)
        if outcome.kind == "OK":
            results = outcome.payload if isinstance(outcome.payload, list) else []
        elif outcome.kind in ("HTTP_RATE_LIMITED", "AMBIGUOUS_TIMEOUT",
                              "MALFORMED_JSON", "LOCAL_ERROR"):
            # Mark this batch's children UNKNOWN and STOP. Do not retry.
            record["status"] = "UNKNOWN"
            record["error_code"] = (
                "HTTP_429" if outcome.kind == "HTTP_RATE_LIMITED"
                else "MALFORMED_RESPONSE" if outcome.kind == "MALFORMED_JSON"
                else "TRANSPORT_ERROR"
            )
            record["exchange_reason"] = outcome.mexc_message
            record["http_status"] = outcome.http_status
            record["child_results"] = [
                {
                    "client_order_id": str(c.get("client_order_id") or ""),
                    "status": "UNKNOWN",
                    "quantity": str(c.get("quantity") or ""),
                    "price": str(c.get("price") or ""),
                    "message": outcome.mexc_message or outcome.kind,
                }
                for c in batch
            ]
            unknown_count += len(batch)
            stopped_early = True
            batch_records.append(record)
            break
        elif outcome.kind == "HTTP_REJECTED":
            # MEXC explicitly rejected the batch (HTTP 4xx other than 429).
            # Mark the entire batch REJECTED, preserve MEXC code/message.
            record["status"] = "REJECTED"
            record["error_code"] = "HTTP_4XX"
            record["exchange_reason"] = outcome.mexc_message or outcome.mexc_code
            record["http_status"] = outcome.http_status
            record["child_results"] = [
                {
                    "client_order_id": str(c.get("client_order_id") or ""),
                    "status": "REJECTED",
                    "quantity": str(c.get("quantity") or ""),
                    "price": str(c.get("price") or ""),
                    "error_code": outcome.mexc_code,
                    "message": outcome.mexc_message,
                }
                for c in batch
            ]
            rejected_count += len(batch)
            stopped_early = True
            exchange_reason = outcome.mexc_message or str(outcome.mexc_code)
            batch_records.append(record)
            break
        elif outcome.kind == "AMBIGUOUS_SERVER_ERROR":
            # MEXC returned 5xx. MEXC's own docs explicitly tell the caller
            # to "Retry later after querying whether the operation already
            # completed" — meaning MEXC does NOT guarantee the order was
            # not processed. Per the conservative classification rule:
            # mark UNKNOWN, stop the ladder.
            record["status"] = "UNKNOWN"
            record["error_code"] = "HTTP_5XX"
            record["exchange_reason"] = (
                outcome.mexc_message
                or outcome.mexc_code
                or "MEXC 5xx; request may have reached MEXC"
            )
            record["http_status"] = outcome.http_status
            record["child_results"] = [
                {
                    "client_order_id": str(c.get("client_order_id") or ""),
                    "status": "UNKNOWN",
                    "quantity": str(c.get("quantity") or ""),
                    "price": str(c.get("price") or ""),
                    "error_code": outcome.mexc_code,
                    "message": outcome.mexc_message or "MEXC 5xx — outcome not proven.",
                }
                for c in batch
            ]
            unknown_count += len(batch)
            stopped_early = True
            exchange_reason = record["exchange_reason"]
            batch_records.append(record)
            break
        else:
            # Unknown outcome kind — treat as UNKNOWN, stop.
            record["status"] = "UNKNOWN"
            record["error_code"] = "UNKNOWN_OUTCOME"
            record["exchange_reason"] = outcome.mexc_message or outcome.kind
            record["child_results"] = [
                {
                    "client_order_id": str(c.get("client_order_id") or ""),
                    "status": "UNKNOWN",
                    "quantity": str(c.get("quantity") or ""),
                    "price": str(c.get("price") or ""),
                    "message": "Unknown outcome kind.",
                }
                for c in batch
            ]
            unknown_count += len(batch)
            stopped_early = True
            batch_records.append(record)
            break

        if outcome.kind == "OK":
            record["exchange_order_ids"] = []
            record["child_results"] = []
            record["accepted_vwap"] = None
            accepted_qty_batch = Decimal("0")
            accepted_notional_batch = Decimal("0")
            for child_in, child_result in zip(batch, results):
                status = _ladder_child_status(child_result)
                child_record: Dict[str, Any] = {
                    "client_order_id": str(child_in.get("client_order_id") or ""),
                    "status": status,
                    "quantity": str(child_in.get("quantity") or ""),
                    "price": str(child_in.get("price") or ""),
                }
                if isinstance(child_result, Mapping):
                    if child_result.get("orderId") is not None:
                        child_record["exchange_order_id"] = str(child_result.get("orderId"))
                    if child_result.get("code") is not None:
                        child_record["error_code"] = child_result.get("code")
                    if child_result.get("msg") is not None:
                        child_record["message"] = str(child_result.get("msg"))
                record["child_results"].append(child_record)
                if status == "ACCEPTED":
                    accepted_count += 1
                    try:
                        q = Decimal(str(child_in.get("quantity") or "0"))
                        p = Decimal(str(child_in.get("price") or "0"))
                    except Exception:  # noqa: BLE001
                        q = Decimal("0")
                        p = Decimal("0")
                    accepted_qty_batch += q
                    accepted_notional_batch += q * p
                    order_id = child_result.get("orderId") if isinstance(child_result, Mapping) else None
                    if order_id is not None:
                        record["exchange_order_ids"].append(str(order_id))
                        accepted_order_ids.append(str(order_id))
                elif status == "REJECTED":
                    rejected_count += 1
                else:
                    unknown_count += 1
            # End of per-child loop. Anything below runs once per batch.
            if len(results) < len(batch):
                for child_in in batch[len(results):]:
                    record["child_results"].append({
                        "client_order_id": str(child_in.get("client_order_id") or ""),
                        "status": "UNKNOWN",
                        "quantity": str(child_in.get("quantity") or ""),
                        "price": str(child_in.get("price") or ""),
                        "message": "MEXC returned no per-child result.",
                    })
                    unknown_count += 1
            record["status"] = "ACCEPTED" if (rejected_count + unknown_count == 0) else "PARTIAL"
            record["accepted_vwap"] = (
                (accepted_notional_batch / accepted_qty_batch) if accepted_qty_batch > 0 else None
            )
            batch_records.append(record)
            accepted_qty += accepted_qty_batch
            accepted_notional += accepted_notional_batch
            # If MEXC returned any rejected child reasons, surface the latest one.
            if not exchange_reason:
                for child_result in results:
                    if isinstance(child_result, Mapping) and child_result.get("msg"):
                        exchange_reason = str(child_result.get("msg"))
                        break

    # Children in batches not attempted (after stopped_early) → NOT_ATTEMPTED.
    attempted_total = sum(len(r["children_attempted"]) for r in batch_records)
    not_attempted_count = requested_order_count - attempted_total

    # PERSIST-AFTER-ALL-BATCHES. Update the durable record with the
    # final per-child submission_classification so a gateway restart
    # can still load the execution and reconcile the exact original
    # client IDs.
    try:
        # Build a {client_order_id: classification} map from the run.
        cid_class: Dict[str, str] = {}
        for rec in batch_records:
            for child_res in rec.get("child_results", []) or []:
                if not isinstance(child_res, Mapping):
                    continue
                cid = str(child_res.get("client_order_id") or "")
                if cid:
                    cid_class[cid] = str(child_res.get("status") or "UNKNOWN")
        persisted = _ladder_load_record(credentials["account"], execution_id)
        if persisted is None:
            persisted = dict(durable_record)
        for child in persisted.get("children", []) or []:
            if not isinstance(child, dict):
                continue
            cid = str(child.get("client_order_id") or "")
            if cid in cid_class:
                child["submission_classification"] = cid_class[cid]
            else:
                child["submission_classification"] = "NOT_ATTEMPTED"
        persisted["updated_at"] = int(time.time())
        if stopped_early:
            persisted["submission_state"] = "STOPPED_EARLY"
        elif rejected_count == 0 and unknown_count == 0:
            persisted["submission_state"] = "ACCEPTED"
        elif rejected_count > 0 and unknown_count == 0:
            persisted["submission_state"] = "PARTIAL_REJECTED"
        else:
            persisted["submission_state"] = "PARTIAL_UNKNOWN"
        _ladder_persist_atomic(
            _ladder_record_path(credentials["account"], execution_id), persisted,
        )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).error(
            "ladder_persist_post_failed account=%s execution_id=%s err=%s",
            credentials["account"], execution_id, sanitize_error_message(str(exc)),
        )

    # Reconciliation: re-read open orders to match accepted/unknown children
    # by newClientOrderId. Read-only GET; no automatic retry or cancel.
    if accepted_order_ids:
        try:
            open_payload = _signed_request(
                credentials, "GET", "/api/v3/openOrders", {"symbol": symbol}
            )
        except Exception:  # noqa: BLE001
            open_payload = None
        if isinstance(open_payload, list):
            # Build a map: clientOrderId -> live row.
            live_by_cid: Dict[str, Mapping[str, Any]] = {}
            for row in open_payload:
                cid = str(row.get("clientOrderId") or "")
                if cid:
                    live_by_cid[cid] = row
            # Annotate batch records with live exchange order IDs / status.
            for rec in batch_records:
                matched: List[str] = []
                for cid in rec.get("children_attempted", []):
                    row = live_by_cid.get(str(cid))
                    if row is not None and row.get("orderId") is not None:
                        matched.append(str(row["orderId"]))
                if matched:
                    rec["verified_exchange_order_ids"] = matched

    planned_vwap = _vwap(
        [{"quantity": c.get("quantity"), "price": c.get("price")} for c in children_in],
        "quantity",
        "price",
    )
    accepted_vwap = (accepted_notional / accepted_qty) if accepted_qty > 0 else None

    partial = accepted_qty < requested_volume or rejected_count > 0 or unknown_count > 0 or stopped_early
    success = (not stopped_early) and rejected_count == 0 and unknown_count == 0 and not_attempted_count == 0

    # Phase 5: wrap CanonicalLadderResult construction in try/except so a
    # local serialization/schema failure cannot silently convert the result
    # into a retry-safe generic failure. If construction raises, we still
    # surface the ladder's batch records and child client_order_ids so the
    # user can reconcile via openOrders/allOrders.
    try:
        ladder_result = CanonicalLadderResult(
            symbol=symbol,
            side=side,
            distribution=str(request.get("distribution") or "ladder"),
            requested_order_count=requested_order_count,
            submitted_order_count=accepted_count,
            requested_volume=_format_decimal(requested_volume),
            submitted_volume=_format_decimal(accepted_qty),
            batch_count=len(batches),
            verified=bool(success and accepted_count == requested_order_count),
            partial=partial,
            status="success" if success else ("partial" if partial else "unknown"),
            accepted_child_count=accepted_count,
            omitted_order_count=rejected_count + unknown_count + not_attempted_count,
            child_order_ids=list(accepted_order_ids),
            batches=batch_records,
            exchange_reason=exchange_reason,
        )
        serialization_failed = False
    except Exception as exc:  # noqa: BLE001
        # Build a safe-failure CanonicalLadderResult using ONLY fields that
        # have been part of the live contract for many commits. This must
        # never silently swallow the batch_records (which carry the
        # child client_order_ids needed for reconciliation).
        ladder_result = CanonicalLadderResult(
            symbol=symbol,
            side=side,
            distribution=str(request.get("distribution") or "ladder"),
            requested_order_count=requested_order_count,
            submitted_order_count=accepted_count,
            requested_volume=_format_decimal(requested_volume),
            submitted_volume=_format_decimal(accepted_qty),
            batch_count=len(batches),
            verified=False,
            partial=True,
            status="serialization_failed",
            accepted_child_count=accepted_count,
            omitted_order_count=rejected_count + unknown_count + not_attempted_count,
            child_order_ids=list(accepted_order_ids),
            batches=batch_records,
            exchange_reason=(
                f"RESULT_SERIALIZATION_FAILED: {sanitize_error_message(str(exc))}"
            ),
        )
        serialization_failed = True
    # Stash the accepted/accepted-vwap/planned-vwap in data for the wizard.
    data = {
        "planned_child_count": requested_order_count,
        "accepted": accepted_count,
        "accepted_vwap": _format_decimal(accepted_vwap) if accepted_vwap is not None else None,
        "planned_vwap": _format_decimal(planned_vwap) if planned_vwap is not None else None,
        "rejected": rejected_count,
        "unknown": unknown_count,
        "not_attempted": not_attempted_count,
        "execution_id": execution_id,
        "submission_state": durable_record.get("submission_state"),
        "child_results": [
            dict(child)
            for batch in batch_records
            for child in (batch.get("child_results") or [])
            if isinstance(child, Mapping)
        ],
    }
    return make_success(
        operation="ladder",
        exchange=name,
        account=credentials["account"],
        ladder=ladder_result,
        data=data,
    )


def _decimal_step(value: Any) -> Decimal:
    text = str(value or "").strip()
    if not text:
        return Decimal("0")
    try:
        inc = Decimal(text)
    except Exception:  # noqa: BLE001
        return Decimal("0")
    return inc if inc > 0 else Decimal("0")


def _places_increment(value: Any) -> Decimal:
    text = str(value or "").strip()
    if not text:
        return Decimal("0")
    try:
        places = int(text)
    except Exception:  # noqa: BLE001
        return Decimal("0")
    if places < 0:
        return Decimal("0")
    return Decimal("1").scaleb(-places)


def _format_step(value: Decimal) -> str:
    if value <= 0:
        return ""
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _quantize_down(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    steps = (value / increment).to_integral_value(rounding=ROUND_DOWN)
    return steps * increment


def _size_increment(info: Mapping[str, Any]) -> Decimal:
    inc = _decimal_step(info.get("size_step"))
    if inc > 0:
        return inc
    inc = _decimal_step(info.get("step_size"))
    if inc > 0:
        return inc
    inc = _decimal_step(info.get("baseSizePrecision"))
    if inc > 0:
        return inc
    return _places_increment(info.get("baseAssetPrecision"))


def _price_increment(info: Mapping[str, Any]) -> Decimal:
    inc = _decimal_step(info.get("price_tick"))
    if inc > 0:
        return inc
    inc = _decimal_step(info.get("tick_size"))
    if inc > 0:
        return inc
    for key in ("quotePrecision", "quoteAssetPrecision"):
        inc = _places_increment(info.get(key))
        if inc > 0:
            return inc
    return Decimal("0")


def _is_timeout_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    reason = str(getattr(exc, "reason", "")).lower()
    return "timed out" in text or "timeout" in text or "timed out" in reason or "timeout" in reason


def _new_order(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return _missing_account("new_order", account)
    side = str(request.get("side") or "").strip().upper()
    order_type = str(request.get("order_type") or request.get("type") or "LIMIT").strip().upper()
    symbol = str(request.get("symbol") or "").strip().upper().replace("/", "")
    if side not in {"BUY", "SELL"}:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_SIDE",
            message="Side must be BUY or SELL.",
        )
    if order_type != "LIMIT":
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="LIMIT_ONLY",
            message="MEXC Spot /tradespot accepts LIMIT orders only.",
        )
    qty = _to_decimal(request.get("quantity") or request.get("qty") or request.get("volume"))
    price = _to_decimal(request.get("price") or request.get("limit_price"))
    if qty <= 0 or price <= 0:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_ORDER",
            message="Quantity and limit price must be positive.",
        )
    row = _market_row(symbol)
    if row is None:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND",
            message="MEXC spot symbol not found.",
        )
    info = _instrument_from_market(row)
    order_types = {str(x).upper() for x in info.get("orderTypes") or []}
    if order_types and "LIMIT" not in order_types:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="LIMIT_NOT_ALLOWED",
            message="LIMIT is not an allowed order type for this symbol.",
        )
    if not info.get("isSpotTradingAllowed") or not info.get("api_eligible"):
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="SYMBOL_NOT_TRADABLE",
            message="Symbol is not API-enabled for spot trading.",
        )
    try:
        self_set, _self_err = _self_symbols(credentials)
    except Exception:  # noqa: BLE001
        self_set = None
    if self_set is not None and str(info["symbol"]) not in self_set:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="SYMBOL_NOT_API_ENABLED",
            message="Symbol is not API-enabled for this key.",
        )
    qty = _quantize_down(qty, _size_increment(info))
    price = _quantize_down(price, _price_increment(info))
    if qty <= 0 or price <= 0:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_ORDER",
            message="Normalized quantity or price is not positive.",
        )
    qty_text = _format_decimal(qty)
    price_text = _format_decimal(price)
    client_id = str(request.get("client_order_id") or request.get("newClientOrderId") or "").strip()
    params: Dict[str, Any] = {
        "symbol": str(info["symbol"]),
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": qty_text,
        "price": price_text,
    }
    if client_id:
        params["newClientOrderId"] = client_id[:32]
    try:
        payload = _signed_request(credentials, "POST", "/api/v3/order", params)
        err = _response_error(payload, "order rejected")
        if err:
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="EXCHANGE_REJECTED",
                message=_redact(sanitize_error_message(err), credentials),
                exchange_reason=_redact(sanitize_error_message(err), credentials),
            )
        if not isinstance(payload, dict) or payload.get("orderId") is None:
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="ORDER_STATUS_UNKNOWN",
                message="MEXC did not return an order id; status is unknown. Not retried.",
            )
        order_id = payload.get("orderId")
        status = str(payload.get("status") or "NEW")
        return make_success(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            order=CanonicalOrderResult(
                symbol=str(info["symbol"]),
                side=side,
                order_type="LIMIT",
                requested_volume=qty_text,
                requested_price=price_text,
                submitted_volume=str(payload.get("origQty") or qty_text),
                submitted_price=str(payload.get("price") or price_text),
                verified=True,
                status=status,
                exchange_order_id=order_id,
                client_order_id=payload.get("clientOrderId") or (client_id or None),
            ),
            data={
                "source": "mexc_spot_order",
                "orderId": order_id,
                "status": status,
                "symbol": str(info["symbol"]),
                "side": side,
                "quantity": qty_text,
                "price": price_text,
            },
        )
    except urllib.error.URLError as exc:
        if _is_timeout_error(exc):
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="ORDER_STATUS_UNKNOWN",
                message="MEXC request timed out after transmission. Order status is unknown; not retried.",
            )
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="MEXC_SPOT_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )
    except Exception as exc:  # noqa: BLE001
        if _is_timeout_error(exc):
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="ORDER_STATUS_UNKNOWN",
                message="MEXC request timed out after transmission. Order status is unknown; not retried.",
            )
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="MEXC_SPOT_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _open_order_rows(credentials: Mapping[str, str]) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], bool]:
    try:
        payload = _signed_request(credentials, "GET", "/api/v3/openOrders")
        err = _response_error(payload, "openOrders failed") if isinstance(payload, dict) else None
        if err:
            return None, err, False
        rows = payload if isinstance(payload, list) else payload.get("orders", []) if isinstance(payload, dict) else []
        parsed = [_parse_order(row) for row in rows if isinstance(row, Mapping)]
        return parsed, None, False
    except urllib.error.URLError as exc:
        if _is_timeout_error(exc):
            return None, "MEXC openOrders timed out.", True
        return None, _redact(sanitize_error_message(str(exc)), credentials), False
    except Exception as exc:  # noqa: BLE001
        if _is_timeout_error(exc):
            return None, "MEXC openOrders timed out.", True
        return None, _redact(sanitize_error_message(str(exc)), credentials), False


def _cancel_orders(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return _missing_account("cancel_orders", account)
    raw_ids = request.get("order_ids") if request.get("order_ids") is not None else request.get("orderIds")
    if not isinstance(raw_ids, list) or not raw_ids:
        return make_failure(
            operation="cancel_orders",
            exchange=name,
            account=credentials["account"],
            code="MISSING_ORDER_IDS",
            message="cancel_orders requires an explicit list of MEXC order IDs.",
        )
    side = str(request.get("side") or "").strip().upper()
    symbol = str(request.get("symbol") or "").strip().upper().replace("/", "")
    if side not in {"BUY", "SELL"}:
        return make_failure(
            operation="cancel_orders",
            exchange=name,
            account=credentials["account"],
            code="INVALID_SIDE",
            message="Side must be BUY or SELL.",
        )
    if not symbol:
        return make_failure(
            operation="cancel_orders",
            exchange=name,
            account=credentials["account"],
            code="MISSING_SYMBOL",
            message="cancel_orders requires a symbol.",
        )
    order_ids = [str(oid).strip() for oid in raw_ids if str(oid).strip()]
    if not order_ids:
        return make_failure(
            operation="cancel_orders",
            exchange=name,
            account=credentials["account"],
            code="MISSING_ORDER_IDS",
            message="cancel_orders requires an explicit list of MEXC order IDs.",
        )
    timed_out = False
    for oid in order_ids:
        try:
            _signed_request(
                credentials,
                "DELETE",
                "/api/v3/order",
                {"symbol": symbol, "orderId": oid},
            )
        except urllib.error.URLError as exc:
            if _is_timeout_error(exc):
                timed_out = True
                continue
            return make_failure(
                operation="cancel_orders",
                exchange=name,
                account=credentials["account"],
                code="MEXC_SPOT_ERROR",
                message=_redact(sanitize_error_message(str(exc)), credentials),
            )
        except Exception as exc:  # noqa: BLE001
            if _is_timeout_error(exc):
                timed_out = True
                continue
            return make_failure(
                operation="cancel_orders",
                exchange=name,
                account=credentials["account"],
                code="MEXC_SPOT_ERROR",
                message=_redact(sanitize_error_message(str(exc)), credentials),
            )
    parsed, list_error, list_timeout = _open_order_rows(credentials)
    if parsed is None:
        return make_failure(
            operation="cancel_orders",
            exchange=name,
            account=credentials["account"],
            code="CANCEL_STATUS_UNKNOWN",
            message=(list_error or "MEXC open-order status is unknown after cancel.") + " Not retried.",
        )
    matching = [
        row
        for row in parsed
        if str(row.get("symbol") or "").upper().replace("/", "") == symbol
        and str(row.get("side") or "").upper() == side
        and str(row.get("status") or "").upper() not in {"FILLED", "CANCELED", "CANCELLED"}
    ]
    remaining_ids = {str(row.get("order_id") or "") for row in matching}
    requested_set = set(order_ids)
    still_open_requested = requested_set & remaining_ids
    cancelled = len(requested_set) - len(still_open_requested)
    remaining = len(matching)
    verified = len(still_open_requested) == 0
    data = {
        "source": "mexc_spot_cancel",
        "symbol": symbol,
        "side": side,
        "order_ids": order_ids,
        "requested": len(order_ids),
        "cancelled": cancelled,
        "remaining": remaining,
        "verified": verified,
    }
    cancel_group = CanonicalCancelGroupResult(
        symbol=symbol,
        side=side,
        targeted_order_count=len(order_ids),
        cancelled_order_count=cancelled,
        confirmed_absent_count=cancelled,
        remaining_target_count=len(still_open_requested),
        verified=verified,
        partial=cancelled > 0 and not verified,
        status="unknown" if timed_out or list_timeout else ("success" if verified else "partial"),
        requested_cancel_count=len(order_ids),
        verified_cancel_count=cancelled,
    )
    if timed_out or list_timeout:
        return make_failure(
            operation="cancel_orders",
            exchange=name,
            account=credentials["account"],
            code="CANCEL_STATUS_UNKNOWN",
            message="MEXC cancel request timed out after transmission. Order status is unknown; not retried.",
            cancel_group=cancel_group,
        )
    return make_success(
        operation="cancel_orders",
        exchange=name,
        account=credentials["account"],
        cancel_group=cancel_group,
        data=data,
    )


def execute(request: Mapping[str, Any]) -> CanonicalResponse:
    op = str(request.get("operation") or "").strip()
    account = str(request.get("account") or "").strip()
    if op == "balance":
        return _balance(account)
    if op in {"orders", "open_orders", "positions_orders"}:
        return _orders(account)
    if op == "list_instruments":
        return _list_instruments(account, request)
    if op == "resolve_instrument":
        return _resolve_instrument(account, request)
    if op == "market_price":
        return _market_price(account, request)
    if op == "new_order":
        return _new_order(account, request)
    if op == "cancel_orders":
        return _cancel_orders(account, request)
    if op == "ladder":
        return _ladder(account, request)
    if op == "ladder_reconcile":
        return _execute_reconcile_batch(account, request)
    if op == "ladder_list_unresolved":
        return _execute_list_unresolved(account)
    if op in {"cancel_order_group", "cancel_order"}:
        return _unsupported(op, account)
    return make_failure(
        operation=op,
        exchange=name,
        account=account,
        code="NOT_IMPLEMENTED",
        message=f"Operation '{op}' is not implemented by MEXC Spot.",
    )


def _execute_reconcile_batch(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    """Read-only GET bridge: reconcile_batch via the standard execute() entry.

    Never POSTs / DELETEs. Only GETs openOrders + allOrders + single-order.
    """
    symbol = str(request.get("symbol") or "").strip().upper()
    if not symbol:
        return make_failure(
            operation="ladder_reconcile",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="reconcile requires `symbol`.",
        )
    expected = list(request.get("expected_client_order_ids") or [])
    if not expected:
        return make_failure(
            operation="ladder_reconcile",
            exchange=name,
            account=account,
            code="MISSING_EXPECTED_IDS",
            message="reconcile requires `expected_client_order_ids`.",
        )
    known = list(request.get("known_exchange_order_ids") or [])
    exec_id = request.get("execution_id")
    try:
        rows = reconcile_batch(
            account=account,
            symbol=symbol,
            expected_client_order_ids=expected,
            known_exchange_order_ids=known,
            execution_id=exec_id,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="ladder_reconcile",
            exchange=name,
            account=account,
            code="MEXC_SPOT_RECONCILE_FAILED",
            message=sanitize_error_message(str(exc)),
        )
    by_class: Dict[str, int] = {}
    for r in rows:
        by_class[str(r.get("classification") or "NOT_FOUND")] = by_class.get(str(r.get("classification") or "NOT_FOUND"), 0) + 1
    if isinstance(exec_id, str) and exec_id:
        try:
            update_ladder_reconciliation(account, exec_id, rows)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception(
                "ladder_reconcile_update_failed account=%s execution_id=%s",
                account, exec_id,
            )
    return make_success(
        operation="ladder_reconcile",
        exchange=name,
        account=account,
        data={
            "rows": rows,
            "summary": by_class,
            "execution_id": exec_id,
        },
    )


def _execute_list_unresolved(account: str) -> CanonicalResponse:
    """Read-only bridge: list unresolved ladder records for an account."""
    try:
        records = list_unresolved_ladders(account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="ladder_list_unresolved",
            exchange=name,
            account=account,
            code="MEXC_SPOT_LIST_UNRESOLVED_FAILED",
            message=sanitize_error_message(str(exc)),
        )
    return make_success(
        operation="ladder_list_unresolved",
        exchange=name,
        account=account,
        data={"records": records, "count": len(records)},
    )


__all__ = [
    "name", "list_accounts", "capabilities", "execute",
    # Read-only helpers usable by the wizard and tests.
    "reconcile_batch", "list_unresolved_ladders", "update_ladder_reconciliation",
    "_ladder_new_execution_id", "_ladder_new_client_order_id",
]
