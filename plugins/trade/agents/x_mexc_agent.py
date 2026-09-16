"""MEXC exchange agent.

Owns all MEXC-specific behavior for the /trade stack.

Credentials (``.env`` / environment):
  ``MEXC_<ALIAS>_ACCESSKEY`` + ``MEXC_<ALIAS>_SECRETKEY``
  Aliases: APIKEY / API_KEY / KEY and SECRET / API_SECRET / SECRET_KEY.
  Optional: ``MEXC_<ALIAS>_CONTRACT_BASE`` / ``MEXC_CONTRACT_BASE``
            ``MEXC_<ALIAS>_SPOT_BASE`` / ``MEXC_SPOT_BASE``
            ``MEXC_<ALIAS>_DEFAULT_LEVERAGE`` (default 20)

Operations:
  - balance (stablecoin rollup USDT+USDC+…)
  - positions_orders / positions_management
  - new_order (limit open long/short)
  - cancel_order_group
  - resolve_instrument / list_instruments / market_price

Contract auth:
  Headers ApiKey, Request-Time, Signature
  Signature = HMAC-SHA256(accessKey + timestamp + paramString, secret)
  GET paramString = sorted query; POST paramString = raw JSON body

TradeDesk and the Telegram wizard MUST remain exchange-agnostic.
"""

from __future__ import annotations
from plugins.trade.candles import handle_candles_operation, has_native_candles

import hashlib
import hmac
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..canonical import (
    CanonicalCancelGroupResult,
    CanonicalInstrument,
    CanonicalLadderResult,
    CanonicalMarketPrice,
    CanonicalOrderGroup,
    CanonicalOrderResult,
    CanonicalPortfolioSummary,
    CanonicalPosition,
    CanonicalPositionActionResult,
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
DEFAULT_LEVERAGE = 20
LADDER_ABSOLUTE_MAX_ORDERS = 200
# MEXC rejects "Requests are too frequent" under parallel bursts.
# Serial paced submit + retries is slower but completes large ladders.
LADDER_MAX_WORKERS = 1
LADDER_CHILD_PAUSE_SECONDS = 0.22
LADDER_RATE_LIMIT_RETRIES = 10
LADDER_RATE_LIMIT_BASE_SLEEP = 0.45
_LADDER_SUBMIT_LOCK = __import__("threading").Lock()
_LADDER_LAST_SUBMIT_TS = 0.0

# MEXC futures side codes
_SIDE_OPEN_LONG = 1
_SIDE_CLOSE_SHORT = 2
_SIDE_OPEN_SHORT = 3
_SIDE_CLOSE_LONG = 4

# order type: 1 limit, 5 market
_TYPE_LIMIT = 1
_TYPE_MARKET = 5
# openType: 1 isolated, 2 cross
_OPEN_CROSS = 2
# positionType: 1 long, 2 short
_POS_LONG = 1
_POS_SHORT = 2

_STABLE_CURRENCIES = frozenset({"USDT", "USDC", "USD", "USDE", "FDUSD", "BUSD", "TUSD", "DAI"})

_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_KEY_ALIASES = ("ACCESSKEY", "ACCESS_KEY", "APIKEY", "API_KEY", "KEY")
_SECRET_ALIASES = ("SECRETKEY", "SECRET_KEY", "APISECRET", "API_SECRET", "SECRET")
_CONTRACT_BASE_ALIASES = ("CONTRACT_BASE", "CONTRACT_URL", "FUTURES_BASE")
_SPOT_BASE_ALIASES = ("SPOT_BASE", "SPOT_URL")
_LEV_ALIASES = ("DEFAULT_LEVERAGE", "LEVERAGE")

# Akamai on contract.mexc.com blocks bare bot UAs on some write paths (403).
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_CONTRACT_CACHE: Dict[str, Any] = {"ts": 0.0, "by_symbol": {}, "by_base": {}}
_CONTRACT_CACHE_TTL = 300.0


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
        set(_KEY_ALIASES + _SECRET_ALIASES + _CONTRACT_BASE_ALIASES + _SPOT_BASE_ALIASES + _LEV_ALIASES),
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
        elif suffix in _LEV_ALIASES:
            slot.setdefault("default_leverage", val)
    env_map = {k: v for k, (_, v, _) in _combined_mexc_env().items()}
    global_contract = (
        env_map.get("MEXC_CONTRACT_BASE")
        or env_map.get("MEXC_CONTRACT_URL")
        or env_map.get("MEXC_FUTURES_BASE")
        or ""
    ).strip().rstrip("/")
    global_spot = (env_map.get("MEXC_SPOT_BASE") or env_map.get("MEXC_SPOT_URL") or "").strip().rstrip("/")
    global_lev = (env_map.get("MEXC_DEFAULT_LEVERAGE") or "").strip()

    complete: Dict[str, Dict[str, str]] = {}
    for alias, fields in buckets.items():
        if fields.get("access_key") and fields.get("secret_key"):
            if not fields.get("contract_base") and global_contract:
                fields["contract_base"] = global_contract
            if not fields.get("spot_base") and global_spot:
                fields["spot_base"] = global_spot
            if not fields.get("default_leverage") and global_lev:
                fields["default_leverage"] = global_lev
            complete[alias] = fields
    return complete


def list_accounts() -> List[str]:
    return sorted(_discover_credential_map().keys())


def capabilities() -> List[str]:
    return [
        "candles",
        "balance",
        "positions_orders",
        "positions_management",
        "new_order",
        "ladder",
        "cancel_order_group",
        "set_tp",
        "set_sl",
        "close_position",
        "resolve_instrument",
        "list_instruments",
        "market_price",
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
        "contract_base": fields.get("contract_base") or DEFAULT_CONTRACT_BASE,
        "spot_base": fields.get("spot_base") or DEFAULT_SPOT_BASE,
        "default_leverage": fields.get("default_leverage") or str(DEFAULT_LEVERAGE),
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
# HTTP
# ---------------------------------------------------------------------------


def _contract_request(
    credentials: Mapping[str, str],
    method: str,
    path: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    body: Any = None,
) -> Dict[str, Any]:
    """GET uses query params; POST signs the raw JSON body (object or array)."""
    base = str(credentials.get("contract_base") or DEFAULT_CONTRACT_BASE).rstrip("/")
    ts = str(int(time.time() * 1000))
    method_u = method.upper()
    body_bytes: Optional[bytes] = None
    qs = ""
    if body is not None:
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        param_str = body_str
        body_bytes = body_str.encode("utf-8")
    else:
        params = dict(params or {})
        items = sorted((str(k), str(v)) for k, v in params.items() if v is not None)
        param_str = "&".join(f"{k}={v}" for k, v in items)
        qs = ("?" + param_str) if param_str else ""
    sig = hmac.new(
        credentials["secret_key"].encode("utf-8"),
        f"{credentials['access_key']}{ts}{param_str}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    url = f"{base}{path}{qs}"
    req = urllib.request.Request(
        url,
        data=body_bytes,
        method=method_u,
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
            parsed = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body_err = exc.read().decode("utf-8")
            parsed = json.loads(body_err)
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
    return code in (0, "0")


# ---------------------------------------------------------------------------
# Instrument catalog
# ---------------------------------------------------------------------------


def _ensure_contracts(credentials: Mapping[str, str]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    now = time.time()
    if _CONTRACT_CACHE["by_symbol"] and now - float(_CONTRACT_CACHE["ts"]) < _CONTRACT_CACHE_TTL:
        return dict(_CONTRACT_CACHE["by_symbol"]), dict(_CONTRACT_CACHE["by_base"])
    payload = _contract_request(credentials, "GET", "/api/v1/contract/detail")
    if not _contract_ok(payload):
        raise RuntimeError(str(payload.get("message") or payload.get("code") or "contract detail failed"))
    rows = payload.get("data") or []
    if not isinstance(rows, list):
        rows = []
    by_symbol: Dict[str, Dict[str, Any]] = {}
    by_base: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        sym = str(row.get("symbol") or "").strip()
        if not sym:
            continue
        base = str(row.get("baseCoin") or row.get("baseCoinName") or "").upper()
        quote = str(row.get("quoteCoin") or row.get("settleCoin") or row.get("quoteCoinName") or "").upper()
        contract_size = _to_decimal(row.get("contractSize") or "1")
        price_unit = _to_decimal(row.get("priceUnit") or "0.1")
        vol_unit = _to_decimal(row.get("volUnit") or "1")
        min_vol = _to_decimal(row.get("minVol") or "1")
        meta = {
            "symbol": sym,
            "base": base,
            "quote": quote,
            "contract_size": contract_size,
            "price_unit": price_unit if price_unit > 0 else Decimal("0.1"),
            "vol_unit": vol_unit if vol_unit > 0 else Decimal("1"),
            "min_vol": min_vol if min_vol > 0 else Decimal("1"),
            "price_scale": int(row.get("priceScale") or 1),
            "vol_scale": int(row.get("volScale") or 0),
            "max_leverage": int(row.get("maxLeverage") or 100),
            "display": f"{base}/{quote}" if base and quote else sym,
            "raw": dict(row),
        }
        by_symbol[sym.upper()] = meta
        by_symbol[sym.replace("_", "").upper()] = meta
        if base:
            # Prefer USDC settle when multiple (BTC_USDC vs BTC_USDT)
            prev = by_base.get(base)
            if prev is None or (quote == "USDC" and str(prev.get("quote")) != "USDC"):
                by_base[base] = meta
            by_symbol[base] = by_base[base]
    _CONTRACT_CACHE["ts"] = now
    _CONTRACT_CACHE["by_symbol"] = by_symbol
    _CONTRACT_CACHE["by_base"] = by_base
    return dict(by_symbol), dict(by_base)


def _resolve_meta(credentials: Mapping[str, str], requested: str) -> Dict[str, Any]:
    by_symbol, by_base = _ensure_contracts(credentials)
    raw = str(requested or "").strip()
    if not raw:
        raise ValueError("INSTRUMENT_NOT_FOUND")
    key = raw.upper().replace("-", "_").replace("/", "_").replace(" ", "")
    if key in by_symbol:
        return dict(by_symbol[key])
    compact = key.replace("_", "")
    if compact in by_symbol:
        return dict(by_symbol[compact])
    # BTCUSDT / BTCUSDC
    for quote in ("USDC", "USDT", "USD"):
        if compact.endswith(quote) and len(compact) > len(quote):
            base = compact[: -len(quote)]
            if base in by_base:
                return dict(by_base[base])
            cand = f"{base}_{quote}"
            if cand in by_symbol:
                return dict(by_symbol[cand])
    if key in by_base:
        return dict(by_base[key])
    raise ValueError("INSTRUMENT_NOT_FOUND")


def _display_symbol(meta: Mapping[str, Any]) -> str:
    return str(meta.get("base") or meta.get("symbol") or "").upper()


def _coin_to_contracts(coin_size: Decimal, meta: Mapping[str, Any]) -> int:
    cs = meta["contract_size"] if meta.get("contract_size") and meta["contract_size"] > 0 else Decimal("1")
    vol_unit = meta["vol_unit"] if meta.get("vol_unit") and meta["vol_unit"] > 0 else Decimal("1")
    raw = (coin_size / cs).to_integral_value(rounding=ROUND_DOWN)
    # snap to vol unit
    units = int((raw / vol_unit).to_integral_value(rounding=ROUND_DOWN) * vol_unit)
    return max(units, 0)


def _contracts_to_coin(contracts: Decimal, meta: Mapping[str, Any]) -> Decimal:
    cs = meta["contract_size"] if meta.get("contract_size") and meta["contract_size"] > 0 else Decimal("1")
    return contracts * cs


def _quantize_price(price: Decimal, meta: Mapping[str, Any]) -> Decimal:
    unit = meta["price_unit"] if meta.get("price_unit") and meta["price_unit"] > 0 else Decimal("0.1")
    return (price / unit).to_integral_value(rounding=ROUND_HALF_UP) * unit


def _fetch_ticker(credentials: Mapping[str, str], symbol: str) -> Dict[str, Any]:
    payload = _contract_request(
        credentials, "GET", "/api/v1/contract/ticker", params={"symbol": symbol}
    )
    if not _contract_ok(payload):
        raise RuntimeError(str(payload.get("message") or "ticker failed"))
    data = payload.get("data")
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, Mapping):
        raise RuntimeError("ticker missing data")
    return dict(data)


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------


def _asset_metrics(row: Mapping[str, Any]) -> Dict[str, Decimal]:
    equity = _to_decimal(row.get("equity") if row.get("equity") is not None else row.get("cashBalance"))
    available = _to_decimal(
        row.get("availableBalance") if row.get("availableBalance") is not None else row.get("availableCash")
    )
    frozen = _to_decimal(row.get("frozenBalance"))
    position_margin = _to_decimal(row.get("positionMargin"))
    unrealized = _to_decimal(row.get("unrealized"))
    bonus = _to_decimal(row.get("bonus"))
    account_value = equity if equity != 0 else (available + frozen + position_margin)
    return {
        "equity": equity,
        "available": available,
        "frozen": frozen,
        "position_margin": position_margin,
        "unrealized": unrealized,
        "bonus": bonus,
        "account_value": account_value,
    }


def _balance(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set MEXC_<ACCOUNT>_ACCESSKEY and MEXC_<ACCOUNT>_SECRETKEY.",
        )
    try:
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
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            rows = [rows] if isinstance(rows, Mapping) else []

        by_ccy: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            ccy = str(row.get("currency") or row.get("displayCurrency") or "").upper().strip()
            if not ccy:
                continue
            m = _asset_metrics(row)
            if m["account_value"] == 0 and m["available"] == 0 and m["position_margin"] == 0:
                continue
            by_ccy[ccy] = {
                "currency": ccy,
                "equity": _format_decimal(m["equity"]),
                "available": _format_decimal(m["available"]),
                "frozen": _format_decimal(m["frozen"]),
                "position_margin": _format_decimal(m["position_margin"]),
                "unrealized": _format_decimal(m["unrealized"]),
                "bonus": _format_decimal(m["bonus"]),
                "_metrics": m,
            }

        stable_rows = {c: v for c, v in by_ccy.items() if c in _STABLE_CURRENCIES}
        if not stable_rows and not by_ccy:
            return make_failure(
                operation="balance",
                exchange=name,
                account=credentials["account"],
                code="MEXC_ERROR",
                message="No contract asset balances returned.",
            )

        roll_src = stable_rows if stable_rows else by_ccy
        account_value = sum((v["_metrics"]["account_value"] for v in roll_src.values()), Decimal("0"))
        available = sum((v["_metrics"]["available"] for v in roll_src.values()), Decimal("0"))
        position_margin = sum((v["_metrics"]["position_margin"] for v in roll_src.values()), Decimal("0"))
        frozen = sum((v["_metrics"]["frozen"] for v in roll_src.values()), Decimal("0"))
        unrealized = sum((v["_metrics"]["unrealized"] for v in roll_src.values()), Decimal("0"))

        dominant = "USDT"
        best = Decimal("-1")
        for ccy, v in roll_src.items():
            eq = v["_metrics"]["account_value"]
            if eq > best:
                best = eq
                dominant = ccy
        unit = dominant if dominant in _STABLE_CURRENCIES else DEFAULT_QUOTE
        margin_used = position_margin if position_margin > 0 else frozen
        withdrawable = available if available > 0 else max(account_value - margin_used, Decimal("0"))

        breakdown = []
        for ccy, v in sorted(by_ccy.items(), key=lambda kv: kv[1]["_metrics"]["account_value"], reverse=True):
            breakdown.append({k: val for k, val in v.items() if k != "_metrics"})

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
            logger.debug("mexc spot optional failed: %s", exc)

        return make_success(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            balance=normalize_balance(account_value, unit),
            portfolio_summary=CanonicalPortfolioSummary(
                account_value=normalize_balance(account_value, unit).value,
                withdrawable=normalize_balance(withdrawable, unit).value,
                margin_used=normalize_balance(margin_used, unit).value,
                total_position_value=normalize_balance(
                    max(account_value - withdrawable, Decimal("0")), unit
                ).value,
                unit=unit,
            ),
            data={
                "source": "contract_assets_rollup",
                "unit": unit,
                "stable_currencies": sorted(stable_rows.keys()),
                "equity_total": _format_decimal(account_value),
                "available_total": _format_decimal(available),
                "position_margin_total": _format_decimal(position_margin),
                "unrealized_total": _format_decimal(unrealized),
                "by_currency": breakdown[:30],
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


# ---------------------------------------------------------------------------
# Positions + orders
# ---------------------------------------------------------------------------


def _normalize_positions(
    credentials: Mapping[str, str], rows: Sequence[Mapping[str, Any]]
) -> List[CanonicalPosition]:
    by_symbol, _ = _ensure_contracts(credentials)
    out: List[CanonicalPosition] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            hold = _to_decimal(row.get("holdVol"))
        except Exception:  # noqa: BLE001
            continue
        if hold == 0:
            continue
        sym = str(row.get("symbol") or "").upper()
        meta = by_symbol.get(sym) or {
            "symbol": sym,
            "base": sym.split("_")[0] if "_" in sym else sym,
            "contract_size": Decimal("1"),
        }
        pos_type = int(row.get("positionType") or 0)
        side = "long" if pos_type == _POS_LONG else "short"
        size_coin = _contracts_to_coin(hold, meta)
        entry = _to_decimal(row.get("holdAvgPrice") or row.get("openAvgPrice") or row.get("newOpenAvgPrice"))
        pnl = _to_decimal(row.get("unRealizedPnl") or row.get("unrealised") or row.get("unrealized"))
        out.append(
            CanonicalPosition(
                symbol=_display_symbol(meta),
                side=side,
                size=_format_decimal(size_coin),
                entry_price=_format_decimal(entry) if entry > 0 else "0",
                pnl=_format_decimal(pnl),
                exchange_instrument=str(meta.get("symbol") or sym),
            )
        )
    return out


def _fetch_open_order_rows(
    credentials: Mapping[str, str], symbol: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Fetch all open orders, paging past MEXC's default 20-row page."""
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    page_size = 100  # API caps around 100; default without params is 20
    for page in range(1, 51):  # hard ceiling 5000 orders
        params: Dict[str, Any] = {
            "page_num": page,
            "page_size": page_size,
        }
        if symbol:
            params["symbol"] = symbol
        payload = _contract_request(
            credentials, "GET", "/api/v1/private/order/list/open_orders", params=params
        )
        if not _contract_ok(payload):
            # If first page fails, surface; later pages stop.
            if page == 1:
                raise RuntimeError(
                    str(payload.get("message") or payload.get("code") or "open orders failed")
                )
            break
        rows = payload.get("data") or []
        if not isinstance(rows, list) or not rows:
            break
        for r in rows:
            if not isinstance(r, Mapping):
                continue
            oid = str(r.get("orderId") or "").strip()
            if oid and oid in seen:
                continue
            if oid:
                seen.add(oid)
            out.append(dict(r))
        if len(rows) < page_size:
            break
    return out


def _side_label_from_mexc(side_code: int) -> str:
    # open long / close short → buy; open short / close long → sell
    if side_code in (_SIDE_OPEN_LONG, _SIDE_CLOSE_SHORT):
        return "buy"
    return "sell"


def _group_open_orders(
    credentials: Mapping[str, str], rows: Sequence[Mapping[str, Any]]
) -> Tuple[int, List[CanonicalOrderGroup]]:
    by_symbol, _ = _ensure_contracts(credentials)
    buckets: Dict[Tuple[str, str], List[Tuple[Decimal, Decimal]]] = {}
    for row in rows:
        try:
            side_code = int(row.get("side") or 0)
        except Exception:  # noqa: BLE001
            continue
        side = _side_label_from_mexc(side_code)
        sym = str(row.get("symbol") or "").upper()
        meta = by_symbol.get(sym) or {
            "symbol": sym,
            "base": sym.split("_")[0] if "_" in sym else sym,
            "contract_size": Decimal("1"),
        }
        vol = _to_decimal(row.get("vol")) - _to_decimal(row.get("dealVol"))
        if vol <= 0:
            vol = _to_decimal(row.get("vol"))
        if vol <= 0:
            continue
        size = _contracts_to_coin(vol, meta)
        price = _to_decimal(row.get("price") or row.get("priceStr"))
        disp = _display_symbol(meta)
        buckets.setdefault((disp, side), []).append((price, size))
    groups: List[CanonicalOrderGroup] = []
    total = 0
    for (sym, side), legs in sorted(buckets.items()):
        total += len(legs)
        sizes = [s for _, s in legs]
        prices = [p for p, _ in legs if p > 0]
        total_size = sum(sizes, Decimal("0"))
        notional = sum((p * s for p, s in legs), Decimal("0"))
        vwap = (notional / total_size) if total_size > 0 and prices else Decimal("0")
        groups.append(
            CanonicalOrderGroup(
                symbol=sym,
                side=side,
                order_count=len(legs),
                total_size=_format_decimal(total_size),
                vwap=_format_decimal(vwap) if vwap > 0 else "",
                min_price=_format_decimal(min(prices)) if prices else "",
                max_price=_format_decimal(max(prices)) if prices else "",
            )
        )
    return total, groups


def _positions_orders(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set MEXC_<ACCOUNT>_ACCESSKEY and MEXC_<ACCOUNT>_SECRETKEY.",
        )
    try:
        pos_payload = _contract_request(credentials, "GET", "/api/v1/private/position/open_positions")
        if not _contract_ok(pos_payload):
            return make_failure(
                operation="positions_orders",
                exchange=name,
                account=credentials["account"],
                code="MEXC_ERROR",
                message=_redact(
                    sanitize_error_message(str(pos_payload.get("message") or pos_payload.get("code") or "positions failed")),
                    credentials,
                ),
            )
        pos_rows = pos_payload.get("data") or []
        if not isinstance(pos_rows, list):
            pos_rows = []
        positions = _normalize_positions(credentials, pos_rows)
        positions = _enrich_positions_with_stops(credentials, positions, pos_rows)
        order_rows = _fetch_open_order_rows(credentials)
        open_count, groups = _group_open_orders(credentials, order_rows)
        return make_success(
            operation="positions_orders",
            exchange=name,
            account=credentials["account"],
            positions=positions,
            open_order_count=open_count,
            order_groups=groups,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=credentials["account"],
            code="MEXC_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


# ---------------------------------------------------------------------------
# New order
# ---------------------------------------------------------------------------


def _position_leverage(credentials: Mapping[str, str], symbol: str) -> Optional[int]:
    try:
        payload = _contract_request(credentials, "GET", "/api/v1/private/position/open_positions")
        if not _contract_ok(payload):
            return None
        for row in payload.get("data") or []:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("symbol") or "").upper() == symbol.upper():
                return int(row.get("leverage") or 0) or None
    except Exception:  # noqa: BLE001
        return None
    return None


def _new_order(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set MEXC_<ACCOUNT>_ACCESSKEY and MEXC_<ACCOUNT>_SECRETKEY.",
        )
    side_in = str(request.get("side") or "").strip().lower()
    if side_in not in {"buy", "sell"}:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_SIDE",
            message="side must be buy or sell.",
        )
    try:
        volume = Decimal(str(request.get("volume") or request.get("size") or "0"))
        price = Decimal(str(request.get("price") or "0"))
    except Exception:  # noqa: BLE001
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="volume and price must be numbers.",
        )
    if volume <= 0 or price <= 0:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="volume and price must be positive.",
        )
    try:
        meta = _resolve_meta(credentials, str(request.get("symbol") or ""))
        native = str(meta["symbol"])
        px = _quantize_price(price, meta)
        contracts = _coin_to_contracts(volume, meta)
        min_vol = int(meta.get("min_vol") or 1)
        if contracts < min_vol:
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="SIZE_TOO_SMALL",
                message=(
                    f"Size rounds below minimum contracts ({min_vol}); "
                    f"min coin ≈ {_format_decimal(_contracts_to_coin(Decimal(min_vol), meta))}."
                ),
            )
        side_code = _SIDE_OPEN_LONG if side_in == "buy" else _SIDE_OPEN_SHORT
        lev = _position_leverage(credentials, native)
        if not lev:
            try:
                lev = int(str(credentials.get("default_leverage") or DEFAULT_LEVERAGE))
            except Exception:  # noqa: BLE001
                lev = DEFAULT_LEVERAGE
        lev = max(1, min(lev, int(meta.get("max_leverage") or 200)))
        external = f"kam{int(time.time() * 1000)}"
        body = {
            "symbol": native,
            "price": float(px),
            "vol": int(contracts),
            "side": int(side_code),
            "type": int(_TYPE_LIMIT),
            "openType": int(_OPEN_CROSS),
            "leverage": int(lev),
            "externalOid": external,
        }
        payload = _contract_request(
            credentials, "POST", "/api/v1/private/order/submit", body=body
        )
        if not _contract_ok(payload):
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="ORDER_REJECTED",
                message=_redact(
                    sanitize_error_message(
                        str(payload.get("message") or payload.get("code") or "rejected")
                    ),
                    credentials,
                ),
            )
        data = payload.get("data")
        oid = None
        if isinstance(data, (int, str)):
            oid = str(data)
        elif isinstance(data, Mapping):
            oid = str(data.get("orderId") or data.get("order_id") or "").strip() or None
        time.sleep(0.35)
        verified = False
        if oid:
            try:
                opens = _fetch_open_order_rows(credentials, native)
                verified = any(str(r.get("orderId")) == oid for r in opens)
            except Exception:  # noqa: BLE001
                verified = True
        submitted_vol = _contracts_to_coin(Decimal(contracts), meta)
        return make_success(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            order=CanonicalOrderResult(
                symbol=_display_symbol(meta),
                side=side_in,
                order_type="limit",
                requested_volume=_format_decimal(volume),
                requested_price=_format_decimal(price),
                submitted_volume=_format_decimal(submitted_vol),
                submitted_price=_format_decimal(px),
                verified=bool(oid),
                status="resting" if verified else ("submitted" if oid else "unknown"),
                exchange_order_id=oid,
                client_order_id=external,
            ),
        )
    except ValueError as exc:
        code = str(exc)
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code=code if code == "INSTRUMENT_NOT_FOUND" else "INVALID_REQUEST",
            message="Instrument not found." if code == "INSTRUMENT_NOT_FOUND" else sanitize_error_message(str(exc)),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="MEXC_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


# ---------------------------------------------------------------------------
# Ladder
# ---------------------------------------------------------------------------


def _ladder_prices(start: Decimal, end: Decimal, count: int, tick: Decimal) -> List[Decimal]:
    if count <= 0:
        return []
    if count == 1:
        raw = [start]
    else:
        raw = [start + (end - start) * Decimal(i) / Decimal(count - 1) for i in range(count)]
    out: List[Decimal] = []
    for value in raw:
        if tick <= 0:
            out.append(value)
            continue
        units = (value / tick).to_integral_value(rounding=ROUND_HALF_UP)
        out.append(units * tick)
    return out


def _ladder_sizes(
    total: Decimal,
    count: int,
    increment: Decimal,
    distribution: str,
    min_size: Decimal = Decimal("0"),
) -> List[Decimal]:
    key = str(distribution or "").strip().lower().replace(" ", "_")
    if key == "uniform":
        weights = [Decimal(1)] * count
    elif key == "half_gaussian":
        if count == 1:
            weights = [Decimal(1)]
        else:
            span = Decimal(count - 1)
            weights = [
                Decimal(
                    str(
                        math.exp(
                            -(float(Decimal("3") * (span - Decimal(i)) / span) ** 2) / 2
                        )
                    )
                )
                for i in range(count)
            ]
    else:
        raise ValueError(f"Unsupported ladder distribution: {distribution}")
    if increment <= 0:
        raise ValueError("Invalid size increment")
    minimum_units = int((min_size / increment).to_integral_value(rounding=ROUND_DOWN)) if min_size > 0 else 0
    total_units = int((total / increment).to_integral_value(rounding=ROUND_DOWN))
    if total_units <= 0:
        raise ValueError("Total volume rounds to zero at the size step")
    if total_units < minimum_units * count:
        raise ValueError("Total volume is too small for the exchange minimum on every ladder child")
    distributable = total_units - minimum_units * count
    weight_sum = sum(weights)
    raw = [Decimal(distributable) * w / weight_sum for w in weights]
    allocated = [minimum_units + int(x) for x in raw]
    residual = total_units - sum(allocated)
    remainders = [raw[i] - int(raw[i]) for i in range(count)]
    order = sorted(range(count), key=lambda i: (remainders[i], -i), reverse=True)
    for i in order[: max(residual, 0)]:
        allocated[i] += 1
    return [Decimal(x) * increment for x in allocated]


def _is_mexc_rate_limit(message: Any) -> bool:
    text = str(message or "").lower()
    if not text:
        return False
    needles = (
        "too frequent",
        "too many requests",
        "rate limit",
        "requests are too",
        "try again later",
        "429",
    )
    return any(n in text for n in needles)


def _place_ladder_child(
    credentials: Mapping[str, str],
    *,
    native: str,
    side_in: str,
    price: Decimal,
    size_coin: Decimal,
    meta: Mapping[str, Any],
    leverage: int,
    child_index: int = 0,
) -> Dict[str, Any]:
    px = _quantize_price(price, meta)
    contracts = _coin_to_contracts(size_coin, meta)
    min_vol = int(meta.get("min_vol") or 1)
    if contracts < min_vol or px <= 0:
        return {
            "ok": False,
            "order_id": None,
            "error": "below_min_or_zero",
            "price": px,
            "size": _contracts_to_coin(Decimal(max(contracts, 0)), meta),
            "contracts": contracts,
            "rate_limited": False,
        }
    side_code = _SIDE_OPEN_LONG if side_in == "buy" else _SIDE_OPEN_SHORT
    last_error: Optional[str] = None
    rate_limited = False

    for attempt in range(max(1, LADDER_RATE_LIMIT_RETRIES)):
        external = f"kaml{int(time.time() * 1000)}{child_index:03d}{secrets_token()}"[:32]
        body = {
            "symbol": native,
            "price": float(px),
            "vol": int(contracts),
            "side": int(side_code),
            "type": int(_TYPE_LIMIT),
            "openType": int(_OPEN_CROSS),
            "leverage": int(leverage),
            "externalOid": external,
        }
        global _LADDER_LAST_SUBMIT_TS
        with _LADDER_SUBMIT_LOCK:
            now = time.time()
            wait = LADDER_CHILD_PAUSE_SECONDS - (now - _LADDER_LAST_SUBMIT_TS)
            if wait > 0:
                time.sleep(wait)
            try:
                payload = _contract_request(
                    credentials, "POST", "/api/v1/private/order/submit", body=body
                )
            except Exception as exc:  # noqa: BLE001
                _LADDER_LAST_SUBMIT_TS = time.time()
                last_error = str(exc)
                if _is_mexc_rate_limit(last_error):
                    rate_limited = True
                    time.sleep(LADDER_RATE_LIMIT_BASE_SLEEP * (1.6 ** attempt))
                    continue
                return {
                    "ok": False,
                    "order_id": None,
                    "error": last_error,
                    "price": px,
                    "size": _contracts_to_coin(Decimal(contracts), meta),
                    "contracts": contracts,
                    "client_order_id": external,
                    "rate_limited": False,
                }
            _LADDER_LAST_SUBMIT_TS = time.time()

        if _contract_ok(payload):
            data = payload.get("data")
            oid = None
            if isinstance(data, (int, str)):
                oid = str(data)
            elif isinstance(data, Mapping):
                oid = str(data.get("orderId") or data.get("order_id") or "").strip() or None
            return {
                "ok": True,
                "order_id": oid,
                "error": None,
                "price": px,
                "size": _contracts_to_coin(Decimal(contracts), meta),
                "contracts": contracts,
                "client_order_id": external,
                "rate_limited": False,
                "attempts": attempt + 1,
            }

        last_error = str(payload.get("message") or payload.get("code") or "rejected")
        if _is_mexc_rate_limit(last_error):
            rate_limited = True
            time.sleep(LADDER_RATE_LIMIT_BASE_SLEEP * (1.6 ** attempt))
            continue
        # Non-retriable exchange reject
        return {
            "ok": False,
            "order_id": None,
            "error": last_error,
            "price": px,
            "size": _contracts_to_coin(Decimal(contracts), meta),
            "contracts": contracts,
            "client_order_id": external,
            "rate_limited": False,
        }

    return {
        "ok": False,
        "order_id": None,
        "error": last_error or "rate_limited",
        "price": px,
        "size": _contracts_to_coin(Decimal(contracts), meta),
        "contracts": contracts,
        "rate_limited": rate_limited or _is_mexc_rate_limit(last_error),
    }


def secrets_token() -> str:
    # short unique suffix without importing secrets at module top if unused elsewhere
    import secrets as _secrets

    return _secrets.token_hex(3)


def _ladder(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set MEXC_<ACCOUNT>_ACCESSKEY and MEXC_<ACCOUNT>_SECRETKEY.",
        )
    requested_symbol = str(request.get("symbol") or "").strip()
    side_in = str(request.get("side") or "").strip().lower()
    distribution = str(request.get("distribution") or "uniform").strip().lower().replace(" ", "_")
    try:
        count = int(str(request.get("order_count") or "0").strip())
        total = Decimal(str(request.get("total_volume") or "0").strip())
        start = Decimal(str(request.get("start_price") or "0").strip())
        end = Decimal(str(request.get("end_price") or "0").strip())
    except Exception:  # noqa: BLE001
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="order_count, total_volume, start_price and end_price are required numbers.",
        )
    if side_in not in {"buy", "sell"}:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_SIDE",
            message="Side must be buy or sell.",
        )
    if count < 1:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="order_count must be >= 1.",
        )
    if count > LADDER_ABSOLUTE_MAX_ORDERS:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message=f"order_count exceeds safety cap ({LADDER_ABSOLUTE_MAX_ORDERS}).",
        )
    if total <= 0 or start <= 0 or end <= 0:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="total_volume, start_price and end_price must be positive.",
        )
    if side_in == "buy" and not (end < start):
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="For a BUY ladder, end_price must be lower than start_price.",
        )
    if side_in == "sell" and not (end > start):
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="For a SELL ladder, end_price must be higher than start_price.",
        )
    if distribution not in {"uniform", "half_gaussian"}:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="distribution must be uniform or half_gaussian.",
        )
    try:
        meta = _resolve_meta(credentials, requested_symbol)
        native = str(meta["symbol"])
        tick = meta["price_unit"]
        # coin size step = one contract
        step = meta["contract_size"] * meta["vol_unit"]
        min_coin = _contracts_to_coin(Decimal(str(int(meta.get("min_vol") or 1))), meta)
        prices = _ladder_prices(start, end, count, tick)
        sizes = _ladder_sizes(total, count, step, distribution, min_coin)

        lev = _position_leverage(credentials, native)
        if not lev:
            try:
                lev = int(str(credentials.get("default_leverage") or DEFAULT_LEVERAGE))
            except Exception:  # noqa: BLE001
                lev = DEFAULT_LEVERAGE
        lev = max(1, min(lev, int(meta.get("max_leverage") or 200)))

        submitted_children: List[Dict[str, Any]] = []
        batches: List[Optional[Dict[str, Any]]] = [None] * count
        omitted_below_minimum = 0
        first_error: Optional[str] = None
        rate_limited = False

        # Serial paced submit — parallel bursts trip MEXC "too frequent".
        for i in range(count):
            result = _place_ladder_child(
                credentials,
                native=native,
                side_in=side_in,
                price=prices[i],
                size_coin=sizes[i],
                meta=meta,
                leverage=lev,
                child_index=i,
            )
            batches[i] = result
            if result.get("rate_limited"):
                rate_limited = True
            if result.get("error") == "below_min_or_zero":
                omitted_below_minimum += 1
                continue
            if result.get("ok"):
                submitted_children.append(
                    {
                        "index": i,
                        "price": _format_decimal(result["price"]),
                        "size": _format_decimal(result["size"]),
                        "order_id": result.get("order_id"),
                        "client_order_id": result.get("client_order_id"),
                        "attempts": result.get("attempts"),
                    }
                )
            else:
                if first_error is None:
                    first_error = str(result.get("error") or "child failed")
                # Hard non-rate reject (e.g. leverage/margin) — stop early.
                if not result.get("rate_limited"):
                    # keep going for soft errors? stop on clear permanent rejects
                    err_l = str(result.get("error") or "").lower()
                    if any(
                        x in err_l
                        for x in (
                            "insufficient",
                            "leverage",
                            "precision",
                            "notional",
                            "balance",
                            "position mode",
                        )
                    ):
                        break

        ok_count = len(submitted_children)
        if ok_count == 0:
            return make_failure(
                operation="ladder",
                exchange=name,
                account=credentials["account"],
                code="LADDER_FAILED",
                message=_redact(
                    sanitize_error_message(first_error or "no ladder children accepted"),
                    credentials,
                ),
            )

        # brief pause then verify resting ids
        time.sleep(0.45)
        verified_ids: List[str] = []
        try:
            opens = _fetch_open_order_rows(credentials, native)
            open_ids = {str(r.get("orderId")) for r in opens}
            for child in submitted_children:
                oid = str(child.get("order_id") or "")
                if oid and oid in open_ids:
                    verified_ids.append(oid)
        except Exception:  # noqa: BLE001
            verified_ids = [str(c.get("order_id")) for c in submitted_children if c.get("order_id")]

        partial = ok_count < count
        submitted_volume = sum((Decimal(str(c["size"])) for c in submitted_children), Decimal("0"))
        child_ids = [str(c.get("order_id")) for c in submitted_children if c.get("order_id")]
        return make_success(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            ladder=CanonicalLadderResult(
                symbol=_display_symbol(meta),
                side=side_in,
                distribution=distribution,
                requested_order_count=count,
                submitted_order_count=ok_count,
                requested_volume=_format_decimal(total),
                submitted_volume=_format_decimal(submitted_volume),
                batch_count=ok_count,
                verified=len(verified_ids) >= max(1, int(ok_count * 0.9)) and not partial,
                partial=partial or omitted_below_minimum > 0,
                status="success" if (not partial and len(verified_ids) >= max(1, int(ok_count * 0.9))) else "partial",
                accepted_child_count=ok_count,
                omitted_order_count=count - ok_count,
                omitted_below_minimum=omitted_below_minimum or None,
                child_order_ids=child_ids or None,
                batches=submitted_children,
                rate_limited=rate_limited or None,
                exchange_reason=(
                    _redact(sanitize_error_message(first_error), credentials) if first_error else None
                ),
            ),
        )
    except ValueError as exc:
        msg = str(exc)
        code = "INSTRUMENT_NOT_FOUND" if msg == "INSTRUMENT_NOT_FOUND" else "INVALID_REQUEST"
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code=code,
            message="Instrument not found." if code == "INSTRUMENT_NOT_FOUND" else sanitize_error_message(msg),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="MEXC_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def _cancel_order_group(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set MEXC_<ACCOUNT>_ACCESSKEY and MEXC_<ACCOUNT>_SECRETKEY.",
        )
    side_in = str(request.get("side") or "").strip().lower()
    if side_in not in {"buy", "sell"}:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            code="INVALID_SIDE",
            message="side must be buy or sell.",
        )
    try:
        meta = _resolve_meta(credentials, str(request.get("symbol") or ""))
        native = str(meta["symbol"])
        rows = _fetch_open_order_rows(credentials, native)
        want_sides = (
            {_SIDE_OPEN_LONG, _SIDE_CLOSE_SHORT}
            if side_in == "buy"
            else {_SIDE_OPEN_SHORT, _SIDE_CLOSE_LONG}
        )
        targets: List[str] = []
        for row in rows:
            try:
                sc = int(row.get("side") or 0)
            except Exception:  # noqa: BLE001
                continue
            if sc not in want_sides:
                continue
            oid = str(row.get("orderId") or "").strip()
            if oid:
                targets.append(oid)
        if not targets:
            return make_success(
                operation="cancel_order_group",
                exchange=name,
                account=credentials["account"],
                cancel_group=CanonicalCancelGroupResult(
                    symbol=_display_symbol(meta),
                    side=side_in,
                    targeted_order_count=0,
                    cancelled_order_count=0,
                    confirmed_absent_count=0,
                    remaining_target_count=0,
                    verified=True,
                    status="noop",
                ),
            )
        cancelled = 0
        batch = 50
        batch_count = 0
        for i in range(0, len(targets), batch):
            chunk = targets[i : i + batch]
            # API expects JSON array of orderId strings
            payload = _contract_request(
                credentials, "POST", "/api/v1/private/order/cancel", body=chunk
            )
            batch_count += 1
            if not _contract_ok(payload):
                return make_failure(
                    operation="cancel_order_group",
                    exchange=name,
                    account=credentials["account"],
                    code="CANCEL_FAILED",
                    message=_redact(
                        sanitize_error_message(
                            str(payload.get("message") or payload.get("code") or "cancel failed")
                        ),
                        credentials,
                    ),
                    cancel_group=CanonicalCancelGroupResult(
                        symbol=_display_symbol(meta),
                        side=side_in,
                        targeted_order_count=len(targets),
                        cancelled_order_count=cancelled,
                        confirmed_absent_count=cancelled,
                        remaining_target_count=max(len(targets) - cancelled, 0),
                        verified=False,
                        partial=cancelled > 0,
                        status="failed",
                        batch_count=batch_count,
                        requested_cancel_count=len(targets),
                        verified_cancel_count=cancelled,
                        exchange_reason=_redact(
                            sanitize_error_message(str(payload.get("message") or "")),
                            credentials,
                        ),
                    ),
                )
            data = payload.get("data")
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, Mapping) and int(item.get("errorCode") or 0) == 0:
                        cancelled += 1
                    elif not isinstance(item, Mapping):
                        cancelled += 1
            else:
                cancelled += len(chunk)
            time.sleep(0.05)
        time.sleep(0.35)
        left = _fetch_open_order_rows(credentials, native)
        remaining = 0
        for row in left:
            try:
                sc = int(row.get("side") or 0)
            except Exception:  # noqa: BLE001
                continue
            if sc in want_sides:
                remaining += 1
        verified = remaining == 0
        return make_success(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            cancel_group=CanonicalCancelGroupResult(
                symbol=_display_symbol(meta),
                side=side_in,
                targeted_order_count=len(targets),
                cancelled_order_count=cancelled,
                confirmed_absent_count=len(targets) - remaining,
                remaining_target_count=remaining,
                verified=verified,
                partial=remaining > 0 and cancelled > 0,
                status="success" if verified else "partial",
                batch_count=batch_count,
                requested_cancel_count=len(targets),
                verified_cancel_count=len(targets) - remaining,
            ),
        )
    except ValueError as exc:
        code = str(exc)
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            code=code if code == "INSTRUMENT_NOT_FOUND" else "INVALID_REQUEST",
            message="Instrument not found." if code == "INSTRUMENT_NOT_FOUND" else sanitize_error_message(str(exc)),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            code="MEXC_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


# ---------------------------------------------------------------------------
# Instruments / market price
# ---------------------------------------------------------------------------


def _resolve_instrument(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set MEXC_<ACCOUNT>_ACCESSKEY and MEXC_<ACCOUNT>_SECRETKEY.",
        )
    requested = str(request.get("symbol") or request.get("query") or "")
    try:
        meta = _resolve_meta(credentials, requested)
        disp = _display_symbol(meta)
        min_coin = _contracts_to_coin(Decimal(str(int(meta.get("min_vol") or 1))), meta)
        return make_success(
            operation="resolve_instrument",
            exchange=name,
            account=credentials["account"],
            instrument=CanonicalInstrument(
                requested_symbol=requested,
                symbol=str(meta.get("symbol")),
                display_name=str(meta.get("display") or disp),
                price_increment=_format_decimal(meta["price_unit"]),
                size_increment=_format_decimal(meta["contract_size"] * meta["vol_unit"]),
                minimum_size=_format_decimal(min_coin),
            ),
        )
    except ValueError:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND",
            message="Instrument not found on MEXC.",
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=credentials["account"],
            code="MEXC_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _list_instruments(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set MEXC_<ACCOUNT>_ACCESSKEY and MEXC_<ACCOUNT>_SECRETKEY.",
        )
    try:
        by_symbol, by_base = _ensure_contracts(credentials)
        q = str(request.get("query") or request.get("symbol") or "").strip().upper()
        out: List[CanonicalInstrument] = []
        seen = set()
        for base, meta in sorted(by_base.items()):
            if q and q not in base and q not in str(meta.get("symbol") or "").upper():
                continue
            if base in seen:
                continue
            seen.add(base)
            min_coin = _contracts_to_coin(Decimal(str(int(meta.get("min_vol") or 1))), meta)
            out.append(
                CanonicalInstrument(
                    requested_symbol=base,
                    symbol=str(meta.get("symbol")),
                    display_name=str(meta.get("display") or base),
                    price_increment=_format_decimal(meta["price_unit"]),
                    size_increment=_format_decimal(meta["contract_size"] * meta["vol_unit"]),
                    minimum_size=_format_decimal(min_coin),
                )
            )
            if len(out) >= 50:
                break
        return make_success(
            operation="list_instruments",
            exchange=name,
            account=credentials["account"],
            data={"instruments": [i.to_dict() for i in out]},
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=credentials["account"],
            code="MEXC_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _market_price(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set MEXC_<ACCOUNT>_ACCESSKEY and MEXC_<ACCOUNT>_SECRETKEY.",
        )
    requested = str(request.get("symbol") or "")
    try:
        meta = _resolve_meta(credentials, requested)
        tick = _fetch_ticker(credentials, str(meta["symbol"]))
        last = _to_decimal(tick.get("lastPrice") or tick.get("fairPrice") or tick.get("indexPrice"))
        fair = _to_decimal(tick.get("fairPrice") or last)
        index = _to_decimal(tick.get("indexPrice") or fair)
        return make_success(
            operation="market_price",
            exchange=name,
            account=credentials["account"],
            market_price=CanonicalMarketPrice(
                requested_symbol=requested,
                market=str(meta.get("symbol")),
                mark_price=_format_decimal(fair),
                oracle_price=_format_decimal(index),
                price=_format_decimal(last),
            ),
        )
    except ValueError:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND",
            message="Instrument not found on MEXC.",
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="market_price",
            exchange=name,
            account=credentials["account"],
            code="MEXC_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


# ---------------------------------------------------------------------------
# Positions management (TP / SL / close)
# ---------------------------------------------------------------------------


def _fetch_open_stop_rows(credentials: Mapping[str, str]) -> List[Dict[str, Any]]:
    payload = _contract_request(credentials, "GET", "/api/v1/private/stoporder/open_orders")
    if not _contract_ok(payload):
        raise RuntimeError(str(payload.get("message") or payload.get("code") or "stop orders failed"))
    rows = payload.get("data") or []
    if not isinstance(rows, list):
        return []
    return [dict(r) for r in rows if isinstance(r, Mapping)]


def _cancel_stop_ids(credentials: Mapping[str, str], stop_ids: Sequence[Any]) -> int:
    cancelled = 0
    for sid in stop_ids:
        if sid is None or str(sid).strip() in {"", "0"}:
            continue
        try:
            payload = _contract_request(
                credentials,
                "POST",
                "/api/v1/private/stoporder/cancel_all",
                body={"stopPlanOrderId": int(sid) if str(sid).isdigit() else sid},
            )
            if _contract_ok(payload):
                cancelled += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("mexc stop cancel failed id=%s: %s", sid, exc)
    return cancelled


def _stops_for_position(stops: Sequence[Mapping[str, Any]], position_id: str) -> List[Dict[str, Any]]:
    pid = str(position_id)
    out: List[Dict[str, Any]] = []
    for row in stops:
        if str(row.get("positionId") or "") == pid:
            out.append(dict(row))
    return out


def _aggregate_tp_sl(stops: Sequence[Mapping[str, Any]]) -> Tuple[Optional[str], Optional[str], int, int]:
    tps: List[Decimal] = []
    sls: List[Decimal] = []
    for row in stops:
        tp = _to_decimal(row.get("takeProfitPrice"))
        sl = _to_decimal(row.get("stopLossPrice"))
        if tp > 0:
            tps.append(tp)
        if sl > 0:
            sls.append(sl)
    tp_s = _format_decimal(tps[0]) if len(tps) == 1 else (_format_decimal(min(tps)) if tps else None)
    # multiple TPs: show min for long-ish display; wizard shows one value
    if len(tps) > 1:
        tp_s = _format_decimal(min(tps))
    sl_s = _format_decimal(sls[0]) if len(sls) == 1 else (_format_decimal(max(sls)) if sls else None)
    if len(sls) > 1:
        sl_s = _format_decimal(max(sls))
    return tp_s, sl_s, len(tps), len(sls)


def _enrich_positions_with_stops(
    credentials: Mapping[str, str],
    positions: List[CanonicalPosition],
    pos_rows: Sequence[Mapping[str, Any]],
) -> List[CanonicalPosition]:
    try:
        stops = _fetch_open_stop_rows(credentials)
    except Exception as exc:  # noqa: BLE001
        logger.debug("mexc stop enrich skipped: %s", exc)
        return positions
    # map exchange_instrument+side -> positionId
    id_by_key: Dict[Tuple[str, str], str] = {}
    for row in pos_rows:
        if not isinstance(row, Mapping):
            continue
        sym = str(row.get("symbol") or "").upper()
        side = "long" if int(row.get("positionType") or 0) == _POS_LONG else "short"
        pid = str(row.get("positionId") or "")
        if sym and pid:
            id_by_key[(sym, side)] = pid
    out: List[CanonicalPosition] = []
    for pos in positions:
        native = str(pos.exchange_instrument or "").upper()
        pid = id_by_key.get((native, pos.side))
        if not pid:
            out.append(pos)
            continue
        matched = _stops_for_position(stops, pid)
        tp_s, sl_s, tp_n, sl_n = _aggregate_tp_sl(matched)
        out.append(
            CanonicalPosition(
                symbol=pos.symbol,
                side=pos.side,
                size=pos.size,
                entry_price=pos.entry_price,
                pnl=pos.pnl,
                tp=tp_s,
                sl=sl_s,
                tp_count=tp_n or None,
                sl_count=sl_n or None,
                exchange_instrument=pos.exchange_instrument,
            )
        )
    return out


def _find_open_position_row(
    credentials: Mapping[str, str],
    requested_symbol: str,
    side_hint: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    meta = _resolve_meta(credentials, requested_symbol)
    native = str(meta["symbol"]).upper()
    payload = _contract_request(credentials, "GET", "/api/v1/private/position/open_positions")
    if not _contract_ok(payload):
        raise RuntimeError(str(payload.get("message") or "positions failed"))
    rows = payload.get("data") or []
    if not isinstance(rows, list):
        rows = []
    matches: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("symbol") or "").upper() != native:
            continue
        if _to_decimal(row.get("holdVol")) <= 0:
            continue
        matches.append(dict(row))
    if not matches:
        raise ValueError("POSITION_NOT_FOUND")
    if side_hint:
        want =  _POS_LONG if side_hint.lower() == "long" else _POS_SHORT
        sided = [m for m in matches if int(m.get("positionType") or 0) == want]
        if sided:
            matches = sided
    if len(matches) > 1 and not side_hint:
        # Prefer long if both (unusual on MEXC one-way)
        matches = sorted(matches, key=lambda r: int(r.get("positionType") or 0))
    return matches[0], meta


def _set_protection(account: str, request: Mapping[str, Any], kind: str) -> CanonicalResponse:
    """kind is 'tp' or 'sl'."""
    op = "set_tp" if kind == "tp" else "set_sl"
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation=op,
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set MEXC_<ACCOUNT>_ACCESSKEY and MEXC_<ACCOUNT>_SECRETKEY.",
        )
    try:
        price_raw = request.get("price")
        if price_raw is None or str(price_raw).strip() == "":
            return make_failure(
                operation=op,
                exchange=name,
                account=credentials["account"],
                code="INVALID_REQUEST",
                message="price is required (0 removes protection).",
            )
        price = _to_decimal(price_raw)
        side_hint = str(request.get("side") or request.get("position_side") or "").strip().lower() or None
        if side_hint in {"buy", "long"}:
            side_hint = "long"
        elif side_hint in {"sell", "short"}:
            side_hint = "short"
        else:
            side_hint = None

        row, meta = _find_open_position_row(
            credentials, str(request.get("symbol") or ""), side_hint=side_hint
        )
        native = str(meta["symbol"])
        pid = str(row.get("positionId") or "")
        hold = int(_to_decimal(row.get("holdVol")))
        if hold <= 0 or not pid:
            return make_failure(
                operation=op,
                exchange=name,
                account=credentials["account"],
                code="POSITION_NOT_FOUND",
                message="No open position for symbol.",
            )
        pos_type = int(row.get("positionType") or _POS_LONG)
        close_side = _SIDE_CLOSE_LONG if pos_type == _POS_LONG else _SIDE_CLOSE_SHORT
        leverage = int(row.get("leverage") or credentials.get("default_leverage") or DEFAULT_LEVERAGE)
        open_type = int(row.get("openType") or _OPEN_CROSS)
        disp = _display_symbol(meta)
        side_label = "long" if pos_type == _POS_LONG else "short"
        size_coin = _contracts_to_coin(Decimal(hold), meta)

        stops = _stops_for_position(_fetch_open_stop_rows(credentials), pid)
        existing_tp, existing_sl, _, _ = _aggregate_tp_sl(stops)

        # Cancel existing stop plans for this position so we can replace cleanly.
        if stops:
            _cancel_stop_ids(credentials, [s.get("id") for s in stops])
            time.sleep(0.2)

        if price <= 0:
            # Removal only — do not re-place the other leg unless it existed.
            other_price = existing_sl if kind == "tp" else existing_tp
            if other_price and _to_decimal(other_price) > 0:
                body: Dict[str, Any] = {
                    "symbol": native,
                    "vol": hold,
                    "side": close_side,
                    "openType": open_type,
                    "leverage": leverage,
                    "positionId": int(pid) if pid.isdigit() else pid,
                }
                if kind == "tp":
                    body["stopLossPrice"] = float(_quantize_price(_to_decimal(other_price), meta))
                else:
                    body["takeProfitPrice"] = float(_quantize_price(_to_decimal(other_price), meta))
                place = _contract_request(
                    credentials, "POST", "/api/v1/private/stoporder/place", body=body
                )
                if not _contract_ok(place):
                    return make_failure(
                        operation=op,
                        exchange=name,
                        account=credentials["account"],
                        code="PROTECTION_FAILED",
                        message=_redact(
                            sanitize_error_message(str(place.get("message") or place.get("code") or "place failed")),
                            credentials,
                        ),
                    )
            time.sleep(0.35)
            left = _stops_for_position(_fetch_open_stop_rows(credentials), pid)
            tp_s, sl_s, _, _ = _aggregate_tp_sl(left)
            removed_ok = (tp_s is None) if kind == "tp" else (sl_s is None)
            return make_success(
                operation=op,
                exchange=name,
                account=credentials["account"],
                position_action=CanonicalPositionActionResult(
                    operation=op,
                    symbol=disp,
                    verified=removed_ok,
                    price="0",
                    removed=True,
                    status="success" if removed_ok else "partial",
                    current_side=side_label,
                    current_size=_format_decimal(size_coin),
                    message="protection removed",
                ),
            )

        px = _quantize_price(price, meta)
        body = {
            "symbol": native,
            "vol": hold,
            "side": close_side,
            "openType": open_type,
            "leverage": leverage,
            "positionId": int(pid) if pid.isdigit() else pid,
        }
        if kind == "tp":
            body["takeProfitPrice"] = float(px)
            if existing_sl and _to_decimal(existing_sl) > 0:
                body["stopLossPrice"] = float(_quantize_price(_to_decimal(existing_sl), meta))
        else:
            body["stopLossPrice"] = float(px)
            if existing_tp and _to_decimal(existing_tp) > 0:
                body["takeProfitPrice"] = float(_quantize_price(_to_decimal(existing_tp), meta))

        place = _contract_request(
            credentials, "POST", "/api/v1/private/stoporder/place", body=body
        )
        if not _contract_ok(place):
            return make_failure(
                operation=op,
                exchange=name,
                account=credentials["account"],
                code="PROTECTION_FAILED",
                message=_redact(
                    sanitize_error_message(str(place.get("message") or place.get("code") or "place failed")),
                    credentials,
                ),
            )
        oid = place.get("data")
        time.sleep(0.4)
        left = _stops_for_position(_fetch_open_stop_rows(credentials), pid)
        tp_s, sl_s, _, _ = _aggregate_tp_sl(left)
        got = tp_s if kind == "tp" else sl_s
        verified = got is not None and abs(_to_decimal(got) - px) / px < Decimal("0.002")
        return make_success(
            operation=op,
            exchange=name,
            account=credentials["account"],
            position_action=CanonicalPositionActionResult(
                operation=op,
                symbol=disp,
                verified=bool(verified or got),
                price=_format_decimal(px),
                removed=False,
                status="success" if (verified or got) else "submitted",
                exchange_order_id=oid,
                current_side=side_label,
                current_size=_format_decimal(size_coin),
            ),
        )
    except ValueError as exc:
        code = str(exc)
        return make_failure(
            operation=op,
            exchange=name,
            account=credentials["account"],
            code=code if code in {"POSITION_NOT_FOUND", "INSTRUMENT_NOT_FOUND"} else "INVALID_REQUEST",
            message=(
                "No open position for symbol."
                if code == "POSITION_NOT_FOUND"
                else ("Instrument not found." if code == "INSTRUMENT_NOT_FOUND" else sanitize_error_message(str(exc)))
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation=op,
            exchange=name,
            account=credentials["account"],
            code="MEXC_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _close_position(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set MEXC_<ACCOUNT>_ACCESSKEY and MEXC_<ACCOUNT>_SECRETKEY.",
        )
    try:
        side_hint = str(request.get("side") or request.get("position_side") or "").strip().lower() or None
        if side_hint in {"buy", "long"}:
            side_hint = "long"
        elif side_hint in {"sell", "short"}:
            side_hint = "short"
        else:
            side_hint = None
        row, meta = _find_open_position_row(
            credentials, str(request.get("symbol") or ""), side_hint=side_hint
        )
        native = str(meta["symbol"])
        hold = int(_to_decimal(row.get("holdVol")))
        if hold <= 0:
            return make_failure(
                operation="close_position",
                exchange=name,
                account=credentials["account"],
                code="POSITION_NOT_FOUND",
                message="No open position for symbol.",
            )
        # Optional partial size in coin units
        size_req = request.get("size") or request.get("volume")
        if size_req is not None and str(size_req).strip() not in {"", "0"}:
            contracts = _coin_to_contracts(_to_decimal(size_req), meta)
            if contracts <= 0:
                return make_failure(
                    operation="close_position",
                    exchange=name,
                    account=credentials["account"],
                    code="SIZE_TOO_SMALL",
                    message="Close size rounds to zero contracts.",
                )
            hold = min(hold, contracts)
        pos_type = int(row.get("positionType") or _POS_LONG)
        close_side = _SIDE_CLOSE_LONG if pos_type == _POS_LONG else _SIDE_CLOSE_SHORT
        leverage = int(row.get("leverage") or DEFAULT_LEVERAGE)
        open_type = int(row.get("openType") or _OPEN_CROSS)
        disp = _display_symbol(meta)
        side_label = "long" if pos_type == _POS_LONG else "short"
        # Cancel stops first so they don't fight the close
        pid = str(row.get("positionId") or "")
        if pid:
            stops = _stops_for_position(_fetch_open_stop_rows(credentials), pid)
            if stops:
                _cancel_stop_ids(credentials, [s.get("id") for s in stops])

        body = {
            "symbol": native,
            "vol": hold,
            "side": close_side,
            "type": _TYPE_MARKET,
            "openType": open_type,
            "leverage": leverage,
            "externalOid": f"kamc{int(time.time() * 1000)}",
        }
        payload = _contract_request(
            credentials, "POST", "/api/v1/private/order/submit", body=body
        )
        if not _contract_ok(payload):
            return make_failure(
                operation="close_position",
                exchange=name,
                account=credentials["account"],
                code="CLOSE_FAILED",
                message=_redact(
                    sanitize_error_message(str(payload.get("message") or payload.get("code") or "close failed")),
                    credentials,
                ),
            )
        oid = payload.get("data")
        time.sleep(0.5)
        # verify remaining
        left_row = None
        try:
            left_row, _ = _find_open_position_row(credentials, native, side_hint=side_label)
        except ValueError:
            left_row = None
        remaining = int(_to_decimal(left_row.get("holdVol"))) if left_row else 0
        closed = remaining == 0
        return make_success(
            operation="close_position",
            exchange=name,
            account=credentials["account"],
            position_action=CanonicalPositionActionResult(
                operation="close_position",
                symbol=disp,
                verified=closed,
                status="success" if closed else "partial",
                exchange_order_id=oid,
                current_side=side_label if remaining > 0 else None,
                current_size=_format_decimal(_contracts_to_coin(Decimal(remaining), meta)) if remaining > 0 else "0",
                message="closed" if closed else f"remaining_contracts={remaining}",
            ),
        )
    except ValueError as exc:
        code = str(exc)
        return make_failure(
            operation="close_position",
            exchange=name,
            account=credentials["account"],
            code=code if code in {"POSITION_NOT_FOUND", "INSTRUMENT_NOT_FOUND"} else "INVALID_REQUEST",
            message=(
                "No open position for symbol."
                if code == "POSITION_NOT_FOUND"
                else ("Instrument not found." if code == "INSTRUMENT_NOT_FOUND" else sanitize_error_message(str(exc)))
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="close_position",
            exchange=name,
            account=credentials["account"],
            code="MEXC_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


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
        if operation in {"positions_orders", "positions_management"}:
            return _positions_orders(account)
        if operation == "new_order":
            return _new_order(account, request)
        if operation == "ladder":
            return _ladder(account, request)
        if operation == "cancel_order_group":
            return _cancel_order_group(account, request)
        if operation == "set_tp":
            return _set_protection(account, request, "tp")
        if operation == "set_sl":
            return _set_protection(account, request, "sl")
        if operation == "close_position":
            return _close_position(account, request)
        if operation == "resolve_instrument":
            return _resolve_instrument(account, request)
        if operation == "list_instruments":
            return _list_instruments(account, request)
        if operation == "market_price":
            return _market_price(account, request)
        if operation == "candles":
            return handle_candles_operation(name, account, request)
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
