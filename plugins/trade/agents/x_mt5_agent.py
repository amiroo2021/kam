"""MetaTrader 5 agent for /trade via the huy-bui-tech socket bridge.

On-demand TCP server on shared port 5555: lock → listen → accept every EA
→ GET_ACCOUNT each → bind the unique login match → one command → close.
No systemd unit, no background poll loop.

Windows EA installs are not modified here. Routing requires GET_ACCOUNT.login.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..canonical import (
    CanonicalBalance,
    CanonicalInstrument,
    CanonicalMarketPrice,
    CanonicalOrderGroup,
    CanonicalOrderResult,
    CanonicalCancelGroupResult,
    CanonicalLadderResult,
    CanonicalPortfolioSummary,
    CanonicalPosition,
    CanonicalPositionActionResult,
    CanonicalResponse,
    make_failure,
    make_success,
    sanitize_error_message,
)

name = "mt5"

logger = logging.getLogger(__name__)

_ALIAS_RE = re.compile(r"^[A-Z0-9]+$")
_ACCOUNT_KEY_RE = re.compile(r"^MT5_([A-Za-z0-9]+)_ACCOUNT$", re.IGNORECASE)
_SESSION_LOCK = threading.Lock()
# Test seam: override to inject a stub shared session.
_SESSION_FACTORY: Optional[Callable[[Dict[str, Any]], Any]] = None
_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_WAIT_SECONDS = 12.0
_SHARED_PORT = 5555
_IDENTITY_SETTLE_SECONDS = 0.5

_TF_MAP = {
    "m1": "M1",
    "1m": "M1",
    "m5": "M5",
    "5m": "M5",
    "m15": "M15",
    "15m": "M15",
    "m30": "M30",
    "30m": "M30",
    "h1": "H1",
    "1h": "H1",
    "h4": "H4",
    "4h": "H4",
    "d1": "D1",
    "1d": "D1",
    "w1": "W1",
    "1w": "W1",
    "mn1": "MN1",
}

_WRITE_OPS = frozenset({
    "new_order",
    "close_position",
    "set_tp",
    "set_sl",
    "cancel_order",
    "cancel_orders",
    "cancel_order_group",
    "modify_order",
})


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
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
        values[key] = value
    return values


def _combined_mt5_env() -> Dict[str, str]:
    """Live env wins; .env fills gaps. Keys stored with original casing."""
    values: Dict[str, str] = {}
    lower_index: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key.upper().startswith("MT5_"):
            values[key] = (value or "").strip()
            lower_index[key.upper()] = key
    for key, value in _load_dotenv_values(_hermes_home() / ".env").items():
        if not key.upper().startswith("MT5_"):
            continue
        if key.upper() in lower_index:
            continue
        values[key] = (value or "").strip()
        lower_index[key.upper()] = key
    return values


def _env_get(env: Dict[str, str], want: str) -> str:
    want_u = want.upper()
    for key, value in env.items():
        if key.upper() == want_u:
            return (value or "").strip()
    return ""


def _shared_listen_port(env: Dict[str, str]) -> int:
    raw = _env_get(env, "MT5_BIND_PORT") or _env_get(env, "MT5_PORT")
    if raw:
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                return port
        except ValueError:
            pass
    return _SHARED_PORT


def _discover_accounts() -> List[str]:
    env = _combined_mt5_env()
    aliases: List[str] = []
    seen = set()
    for key, value in env.items():
        match = _ACCOUNT_KEY_RE.match(key)
        if not match or not (value or "").strip():
            continue
        alias = match.group(1).upper()
        if not _ALIAS_RE.match(alias) or alias in seen:
            continue
        seen.add(alias)
        aliases.append(alias.lower())
    return sorted(aliases)


def list_accounts() -> List[str]:
    return _discover_accounts()


def _lookup_account(alias: str) -> Optional[Dict[str, Any]]:
    alias_u = str(alias or "").strip().upper()
    if not alias_u or not _ALIAS_RE.match(alias_u):
        return None
    env = _combined_mt5_env()
    account_no = _env_get(env, f"MT5_{alias_u}_ACCOUNT")
    if not account_no:
        return None
    port = _shared_listen_port(env)
    host = _env_get(env, "MT5_BIND_HOST") or _DEFAULT_HOST
    wait_raw = _env_get(env, "MT5_WAIT_SECONDS")
    try:
        wait_s = float(wait_raw) if wait_raw else _DEFAULT_WAIT_SECONDS
    except ValueError:
        wait_s = _DEFAULT_WAIT_SECONDS
    symbols: Dict[str, str] = {}
    prefix = f"MT5_{alias_u}_SYMBOL_"
    for key, value in env.items():
        if key.upper().startswith(prefix) and (value or "").strip():
            user_sym = key[len(prefix) :].strip().upper()
            if user_sym:
                symbols[user_sym] = value.strip()
    return {
        "alias": alias_u.lower(),
        "account_no": account_no.strip(),
        "password": _env_get(env, f"MT5_{alias_u}_PASSWORD"),
        "server": _env_get(env, f"MT5_{alias_u}_SERVER"),
        "host": host,
        "port": port,
        "wait_seconds": wait_s,
        "symbols": symbols,
    }


def capabilities() -> List[str]:
    return [
        "balance",
        "positions_orders",
        "positions_management",
        "new_order",
        "close_position",
        "set_tp",
        "set_sl",
        "cancel_order",
        "cancel_orders",
        "candles",
        "list_instruments",
        "resolve_instrument",
        "market_price",
        "ladder",
    ]


def _dec(value: Any, places: int = 2) -> str:
    try:
        quant = Decimal("1").scaleb(-places)
        return str(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))
    except Exception:
        return "0.00" if places == 2 else "0"


def _compact_decimal(value: Any) -> str:
    try:
        decimal_value = Decimal(str(value))
    except Exception:
        return "0"
    rendered = format(decimal_value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".") or "0"
    return rendered


def _as_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def _resolve_symbol(creds: Dict[str, Any], requested: str) -> str:
    raw = str(requested or "").strip()
    if not raw:
        return ""
    mapped = creds.get("symbols") or {}
    return mapped.get(raw.upper()) or mapped.get(raw) or raw


def _normalize_broker_symbol(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _quote_like_suffix(rest: str) -> bool:
    rest_u = (rest or "").upper()
    if not rest_u:
        return False
    if rest_u in {"USD", "USDT", "USDC", "EUR", "GBP", "JPY", "C"}:
        return True
    return rest_u.startswith("USD") or rest_u.endswith("USD") or rest_u.endswith("USDT")


def _match_broker_symbols(
    requested: str,
    available: List[str],
    mapped: str = "",
) -> Tuple[Optional[str], List[str]]:
    """Match a user symbol to Market Watch names. Never invent a broker symbol."""
    items = [str(s).strip() for s in available if str(s).strip()]
    req = str(requested or "").strip()
    if not req or not items:
        return None, []

    def _exact(target: str) -> Optional[str]:
        want_u = str(target or "").strip().upper()
        want_n = _normalize_broker_symbol(target)
        if not want_u:
            return None
        for item in items:
            if item.upper() == want_u or _normalize_broker_symbol(item) == want_n:
                return item
        return None

    exact = _exact(req)
    req_n = _normalize_broker_symbol(req)
    extra: List[str] = []
    for item in items:
        norm = _normalize_broker_symbol(item)
        if not req_n:
            continue
        if norm.startswith(req_n) and len(norm) > len(req_n) and _quote_like_suffix(norm[len(req_n):]):
            if item not in extra:
                extra.append(item)
    if exact:
        siblings = [item for item in extra if item != exact]
        if siblings:
            return None, [exact] + siblings
        return exact, [exact]
    if mapped:
        hit = _exact(mapped)
        if hit:
            return hit, [hit]
    hits = list(extra)
    if len(hits) == 1:
        return hits[0], hits
    if len(hits) > 1:
        return None, hits
    starts = [
        item
        for item in items
        if _normalize_broker_symbol(item).startswith(req_n) and _normalize_broker_symbol(item) != req_n
    ]
    if len(starts) == 1:
        return starts[0], starts
    if len(starts) > 1:
        return None, starts
    return None, []


class _OrderError(RuntimeError):
    def __init__(self, message: str, code: str = "INVALID_REQUEST"):
        super().__init__(message)
        self.code = code


def _info_dec(info: Dict[str, Any], key: str) -> Optional[Decimal]:
    raw = info.get(key)
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw))
    except Exception:
        return None
    if not value.is_finite():
        return None
    return value


def _token_qty_to_lots(token_qty: Decimal, info: Dict[str, Any]) -> Decimal:
    contract = _info_dec(info, "contract_size")
    if contract is None or contract <= 0:
        raise _OrderError(
            "Broker contract size is unavailable; cannot convert token volume to lots.",
            "SYMBOL_INFO_UNAVAILABLE",
        )
    lots = token_qty / contract
    step = _info_dec(info, "volume_step")
    vmin = _info_dec(info, "volume_min")
    vmax = _info_dec(info, "volume_max")
    if step is None or step <= 0 or vmin is None or vmin <= 0:
        raise _OrderError(
            "Broker volume min/step is unavailable; cannot size the order.",
            "SYMBOL_INFO_UNAVAILABLE",
        )
    steps = (lots / step).to_integral_value(rounding=ROUND_DOWN)
    snapped = steps * step
    if snapped != lots:
        raise _OrderError(
            f"Volume { _compact_decimal(token_qty) } is not a valid lot step. "
            f"Min { _compact_decimal(vmin * contract) }, step { _compact_decimal(step * contract) } "
            f"(lots min {_compact_decimal(vmin)} step {_compact_decimal(step)}).",
            "INVALID_VOLUME",
        )
    if lots < vmin:
        raise _OrderError(
            f"Volume is below broker minimum {_compact_decimal(vmin * contract)} "
            f"({_compact_decimal(vmin)} lots).",
            "INVALID_VOLUME",
        )
    if vmax is not None and vmax > 0 and lots > vmax:
        raise _OrderError(
            f"Volume exceeds broker maximum {_compact_decimal(vmax * contract)} "
            f"({_compact_decimal(vmax)} lots).",
            "INVALID_VOLUME",
        )
    return lots


def _validate_limit_price(
    side: str,
    price: Decimal,
    info: Dict[str, Any],
    bid: Decimal,
    ask: Decimal,
) -> None:
    tick = _info_dec(info, "tick_size") or _info_dec(info, "point")
    if tick is not None and tick > 0:
        ratio = price / tick
        if ratio != ratio.to_integral_value(rounding=ROUND_DOWN):
            raise _OrderError(
                f"Price { _compact_decimal(price) } is not a valid tick size "
                f"({_compact_decimal(tick)}). Enter a revised limit price.",
                "INVALID_PRICE",
            )
    stops = _info_dec(info, "stops_level") or Decimal("0")
    point = _info_dec(info, "point") or Decimal("0")
    min_distance = stops * point if stops > 0 and point > 0 else Decimal("0")
    if side == "buy":
        if ask > 0 and price >= ask:
            raise _OrderError(
                f"BUY LIMIT price must be below ask ({_compact_decimal(ask)}). "
                "Enter a revised limit price; it will not be converted to a market order.",
                "INVALID_PRICE",
            )
        if ask > 0 and min_distance > 0 and (ask - price) < min_distance:
            raise _OrderError(
                f"BUY LIMIT price is too close to ask ({_compact_decimal(ask)}); "
                f"minimum distance is {_compact_decimal(min_distance)}. Enter a revised limit price.",
                "INVALID_PRICE",
            )
        return
    if bid > 0 and price <= bid:
        raise _OrderError(
            f"SELL LIMIT price must be above bid ({_compact_decimal(bid)}). "
            "Enter a revised limit price; it will not be converted to a market order.",
            "INVALID_PRICE",
        )
    if bid > 0 and min_distance > 0 and (price - bid) < min_distance:
        raise _OrderError(
            f"SELL LIMIT price is too close to bid ({_compact_decimal(bid)}); "
            f"minimum distance is {_compact_decimal(min_distance)}. Enter a revised limit price.",
            "INVALID_PRICE",
        )


def _extract_login(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, dict):
        for key in ("login", "account", "account_id", "account_number", "number"):
            val = payload.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
    for attr in ("login", "account", "account_id"):
        val = getattr(payload, attr, None)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _identity_ok(creds: Dict[str, Any], payload: Any) -> bool:
    login = _extract_login(payload)
    expected = creds.get("account_no") or ""
    if not login or not expected:
        return False
    login_d, expected_d = _digits(login), _digits(expected)
    if login_d and expected_d:
        return login_d == expected_d
    return login.strip().upper() == expected.strip().upper()


def _bridge_factory():
    from plugins.trade.vendor.mt5_bridge import MT5Bridge

    return MT5Bridge


def _run_async(coro: Any) -> Any:
    return asyncio.run(coro)


class _IdentityError(RuntimeError):
    def __init__(self, message: str, code: str = "ACCOUNT_IDENTITY_UNVERIFIED"):
        super().__init__(message)
        self.code = code


def _login_key(value: str) -> str:
    digits = _digits(value)
    return digits or (value or "").strip().upper()


async def _probe_login(conn: Any) -> tuple[str, dict]:
    from plugins.trade.vendor.mt5_bridge.protocol import Action, Request

    request = Request(action=Action.GET_ACCOUNT, params={})
    response = await conn.send_request(request)
    raw = response.data if getattr(response, "success", False) else {}
    if not isinstance(raw, dict):
        raw = {}
    return _extract_login(raw), raw


async def _bind_matching_ea(bridge: Any, creds: Dict[str, Any], timeout: float) -> dict:
    expected = _login_key(str(creds.get("account_no") or ""))
    if not expected:
        raise _IdentityError("Selected MT5 account has no login.", "ACCOUNT_IDENTITY_MISSING")
    deadline = asyncio.get_event_loop().time() + float(timeout)
    t0 = time.perf_counter()
    identified: Dict[str, tuple] = {}
    # Track the last probe outcome per connection. A connection whose probe
    # FAILED must stay retryable: the EA may not have been ready yet, and the
    # old code added it to `probed` permanently, so a late-arriving EA was
    # never re-identified and the request failed with a bogus identity error.
    probe_state: Dict[int, str] = {}
    matched_at: Optional[float] = None
    while True:
        now = asyncio.get_event_loop().time()
        if now > deadline:
            break
        if matched_at is not None and (now - matched_at) >= _IDENTITY_SETTLE_SECONDS:
            break
        for conn in list(getattr(bridge, "connections", []) or []):
            cid = id(conn)
            if not getattr(conn, "is_connected", True):
                probe_state.pop(cid, None)
                continue
            last = probe_state.get(cid)
            if last == "ok":
                continue
            try:
                login, raw = await _probe_login(conn)
            except Exception as exc:  # noqa: BLE001
                logger.debug("GET_ACCOUNT probe failed: %s", exc)
                probe_state[cid] = "fail"
                continue
            probe_state[cid] = "ok"
            if not login:
                # EA answered but reported no login yet — keep waiting for it.
                probe_state[cid] = "fail"
                continue
            key = _login_key(login)
            prev = identified.get(key)
            if prev is not None and prev[0] is not conn:
                raise _IdentityError(
                    "Duplicate EA login on shared port.",
                    "ACCOUNT_IDENTITY_DUPLICATE",
                )
            identified[key] = (conn, raw)
            setattr(conn, "login", login)
        if expected in identified and matched_at is None:
            matched_at = asyncio.get_event_loop().time()
        await asyncio.sleep(0.05)
    if expected not in identified:
        logger.info(
            "mt5 timing bind account=%s bind_ms=%.0f identified=%s",
            creds.get("alias"),
            (time.perf_counter() - t0) * 1000,
            list(identified),
        )
        if not identified:
            raise _IdentityError(
                "No EA reported GET_ACCOUNT.login matching the selected account.",
                "ACCOUNT_IDENTITY_MISSING",
            )
        # Another EA (a different account) answered. Do not trade on it and do
        # not claim a hard mismatch: the wanted EA may simply not be attached
        # yet. Report it as a routing/no-match condition so the user is told
        # which account actually answered.
        others = ", ".join(sorted(identified))
        raise _IdentityError(
            f"MT5 EA for the selected account did not respond. "
            f"Connected EA login(s): {others}.",
            "ACCOUNT_IDENTITY_MISMATCH",
        )
    conn, raw = identified[expected]
    bridge.bind(conn)
    logger.info(
        "mt5 timing bind account=%s bind_ms=%.0f",
        creds.get("alias"),
        (time.perf_counter() - t0) * 1000,
    )
    return raw


def _session_call(
    creds: Dict[str, Any],
    operation: str,
    fn: Callable[..., Any],
) -> Any:
    """Run one operation against the shared MT5 listener.

    The listener is persistent: binding 5555 per operation forced every EA to
    reconnect on each button press and raced the EA's 5s reconnect timer,
    which produced spurious ACCOUNT_IDENTITY_* errors. The shared session
    keeps the EAs attached and only re-binds the target login per call.
    """
    session = _session_for(creds)
    session.ensure_started()
    raw_account = session.bind_account(creds)
    return session.run(session.bridge, raw_account, True, fn)


async def _probe_account_payload(bridge: Any, _raw: Any, _verified: Any) -> Any:
    """Return the bound EA's raw GET_ACCOUNT payload."""
    conn = getattr(bridge, "_bound", None)
    if conn is None:
        raise RuntimeError("No MT5 EA bound")
    login, raw = await _probe_login(conn)
    return login, raw


class _SharedSession:
    """Persistent MT5 listener shared by every MT5 operation.

    Holds one bound MT5Server on the shared port for the lifetime of the
    process. EAs stay connected between operations, so identity resolution
    is fast and deterministic. Each operation still binds exactly one EA
    (the login it needs) and only sends commands to that bound socket.
    """

    def __init__(self, host: str, port: int, wait_seconds: float) -> None:
        self.host = host
        self.port = int(port)
        self.wait_seconds = float(wait_seconds)
        self.bridge: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready: Optional[threading.Event] = None
        self._error: Optional[BaseException] = None
        self._logins: Dict[str, Any] = {}
        self._accounts: Dict[str, dict] = {}
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------
    def ensure_started(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._error = None
            self._ready = threading.Event()
            self._thread = threading.Thread(
                target=self._run, name="mt5-shared-listener", daemon=True
            )
            self._thread.start()
        if not self._ready.wait(timeout=15.0):
            raise RuntimeError("MT5 listener failed to start")
        if self._error is not None:
            raise RuntimeError(f"MT5 listener failed to start: {self._error}")

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:  # noqa: BLE001
            self._error = exc
        finally:
            if self._ready is not None:
                self._ready.set()

    async def _serve(self) -> None:
        Bridge = _bridge_factory()
        bridge = Bridge(host=self.host, port=self.port)
        self.bridge = bridge
        self._loop = asyncio.get_event_loop()
        await bridge.start()
        logger.info("mt5 shared listener up on %s:%s", self.host, self.port)
        if self._ready is not None:
            self._ready.set()
        try:
            while True:
                await self._refresh_logins()
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await bridge.stop()
            except Exception:  # noqa: BLE001
                logger.debug("mt5 shared listener stop failed", exc_info=True)

    async def _refresh_logins(self) -> None:
        bridge = self.bridge
        if bridge is None:
            return
        for conn in list(getattr(bridge, "connections", []) or []):
            if not getattr(conn, "is_connected", True):
                continue
            cached = getattr(conn, "_mt5_login", None)
            if cached:
                self._logins[_login_key(cached)] = conn
                cached_raw = getattr(conn, "_mt5_account", None)
                if isinstance(cached_raw, dict):
                    self._accounts[_login_key(cached)] = cached_raw
                continue
            try:
                login, raw = await _probe_login(conn)
            except Exception as exc:  # noqa: BLE001
                logger.debug("mt5 listener probe failed: %s", exc)
                continue
            if not login:
                continue
            try:
                setattr(conn, "_mt5_login", login)
                setattr(conn, "_mt5_account", raw)
            except Exception:  # noqa: BLE001
                pass
            self._logins[_login_key(login)] = conn
            self._accounts[_login_key(login)] = raw

    # -- per-operation binding ---------------------------------------
    def bind_account(self, creds: Dict[str, Any]) -> dict:
        """Resolve the EA for this account and bind it (called off-loop).

        The listener thread fills ``_logins`` as EAs connect, so wait (bounded
        by ``wait_seconds``) for the wanted login to appear. The first request
        after startup must allow for the EA reconnect delay.
        """
        expected = _login_key(str(creds.get("account_no") or ""))
        if not expected:
            raise _IdentityError("Selected MT5 account has no login.", "ACCOUNT_IDENTITY_MISSING")
        t0 = time.monotonic()
        deadline = t0 + float(self.wait_seconds)
        conn: Any = None
        while True:
            candidate = self._logins.get(expected)
            if candidate is not None and getattr(candidate, "is_connected", True):
                conn = candidate
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        if conn is not None:
            self.bridge.bind(conn)
            logger.info(
                "mt5 timing bind account=%s bind_ms=%.0f",
                creds.get("alias"),
                (time.perf_counter() - t0) * 1000,
            )
            raw_account = self._accounts.get(expected)
            if isinstance(raw_account, dict):
                return raw_account
            # The cached account payload is missing (connection re-identified
            # after the cache was populated). Re-probe the bound socket so
            # balance/equity are real instead of zeros.
            try:
                login, fresh = self.run(self.bridge, {}, True, _probe_account_payload)
                if isinstance(fresh, dict) and fresh:
                    self._accounts[_login_key(login or expected)] = fresh
                    return fresh
            except Exception as exc:  # noqa: BLE001
                logger.debug("mt5 re-probe after bind failed: %s", exc)
            return {}
        others = ", ".join(sorted(self._logins))
        logger.info(
            "mt5 timing bind account=%s bind_ms=%.0f identified=%s",
            creds.get("alias"),
            (time.perf_counter() - t0) * 1000,
            others or "[]",
        )
        if not self._logins:
            raise _IdentityError(
                "No EA reported GET_ACCOUNT.login matching the selected account.",
                "ACCOUNT_IDENTITY_MISSING",
            )
        raise _IdentityError(
            f"MT5 EA for the selected account did not respond. "
            f"Connected EA login(s): {others}.",
            "ACCOUNT_IDENTITY_MISMATCH",
        )

    def run(
        self,
        bridge: Any,
        raw_account: Any,
        verified: bool,
        fn: Callable[..., Any],
    ) -> Any:
        """Run the operation coroutine on the listener's event loop."""

        async def _call() -> Any:
            return await fn(bridge, raw_account, verified)

        loop = self._loop
        if loop is None:
            raise RuntimeError("MT5 listener loop is not running")
        future = asyncio.run_coroutine_threadsafe(_call(), loop)
        return future.result()


_SHARED_SESSIONS: Dict[Tuple[str, int], "_SharedSession"] = {}


def _shared_session(creds: Dict[str, Any]) -> "_SharedSession":
    key = (str(creds.get("host") or ""), int(creds.get("port") or _SHARED_PORT))
    session = _SHARED_SESSIONS.get(key)
    if session is None:
        session = _SharedSession(
            host=creds.get("host") or _DEFAULT_HOST,
            port=int(creds.get("port") or _SHARED_PORT),
            wait_seconds=float(creds.get("wait_seconds") or _DEFAULT_WAIT_SECONDS),
        )
        _SHARED_SESSIONS[key] = session
    return session


def _session_for(creds: Dict[str, Any]) -> Any:
    """Return the shared session for these credentials.

    Seam for tests: override ``_session_factory`` to inject a stub session.
    """
    factory = _SESSION_FACTORY
    if factory is not None:
        return factory(creds)
    return _shared_session(creds)


def _with_bridge(creds: Dict[str, Any], operation: str, fn: Callable[..., Any]) -> Any:
    if not _SESSION_LOCK.acquire(blocking=False):
        raise RuntimeError("MT5_BUSY: another MT5 request is in flight")
    t0 = time.perf_counter()
    try:
        result = _session_call(creds, operation, fn)
        logger.info(
            "mt5 timing op=%s account=%s total_ms=%.0f",
            operation,
            creds.get("alias"),
            (time.perf_counter() - t0) * 1000,
        )
        return result
    except Exception:
        logger.info(
            "mt5 timing op=%s account=%s total_ms=%.0f error=1",
            operation,
            creds.get("alias"),
            (time.perf_counter() - t0) * 1000,
        )
        raise
    finally:
        _SESSION_LOCK.release()


def _fail(operation: str, account: str, code: str, message: str) -> CanonicalResponse:
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code=code,
        message=sanitize_error_message(message),
    )


def _execute_balance(creds: Dict[str, Any]) -> CanonicalResponse:
    async def _op(bridge, raw, _verified):
        return raw

    raw = _with_bridge(creds, "balance", _op)
    balance = _dec(raw.get("balance", 0) if isinstance(raw, dict) else getattr(raw, "balance", 0))
    equity = _dec(raw.get("equity", balance) if isinstance(raw, dict) else getattr(raw, "equity", balance))
    margin = _dec(raw.get("margin", 0) if isinstance(raw, dict) else getattr(raw, "margin", 0))
    free_margin = _dec(
        raw.get("free_margin", 0) if isinstance(raw, dict) else getattr(raw, "free_margin", 0)
    )
    unit = "USD"
    return make_success(
        operation="balance",
        exchange=name,
        account=creds["alias"],
        balance=CanonicalBalance(value=balance, unit=unit),
        portfolio_summary=CanonicalPortfolioSummary(
            account_value=equity,
            withdrawable=free_margin,
            margin_used=margin,
            total_position_value="0.00",
            unit=unit,
        ),
        data={"leverage": (raw.get("leverage") if isinstance(raw, dict) else getattr(raw, "leverage", None))},
    )


def _protection_text(value: Any) -> Optional[str]:
    if value in (None, 0, 0.0, "0", ""):
        return None
    return _compact_decimal(value)


def _map_position_leg(row: Any) -> Dict[str, Any]:
    if hasattr(row, "symbol"):
        symbol = str(row.symbol)
        side = str(getattr(row, "order_type", getattr(row, "type", "")) or "").lower()
        volume = getattr(row, "volume", 0)
        open_price = getattr(row, "open_price", 0)
        profit = getattr(row, "profit", 0)
        sl = getattr(row, "stop_loss", getattr(row, "sl", None))
        tp = getattr(row, "take_profit", getattr(row, "tp", None))
        ticket = _normalize_ticket(getattr(row, "ticket", None))
    else:
        symbol = str(row.get("symbol") or "")
        side = str(row.get("type") or row.get("side") or "").lower()
        volume = row.get("volume", 0)
        open_price = row.get("open_price", 0)
        profit = row.get("profit", 0)
        sl = row.get("sl")
        tp = row.get("tp")
        ticket = _normalize_ticket(row.get("ticket"))
    if side in {"buy", "long"}:
        side = "long"
    elif side in {"sell", "short"}:
        side = "short"
    return {
        "ticket": ticket,
        "symbol": symbol,
        "side": side,
        "volume": _as_decimal(volume),
        "open_price": _as_decimal(open_price),
        "profit": _as_decimal(profit),
        "tp": _protection_text(tp),
        "sl": _protection_text(sl),
        "size": _compact_decimal(volume),
        "entry_price": _compact_decimal(open_price),
        "pnl": _dec(profit, 2),
    }


def _aggregate_positions(rows: List[Any]) -> Tuple[List[CanonicalPosition], List[Dict[str, Any]], List[Any]]:
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    legs: List[Dict[str, Any]] = []
    tickets: List[Any] = []
    for row in rows:
        leg = _map_position_leg(row)
        tickets.append(leg["ticket"])
        public = {k: v for k, v in leg.items() if k not in {"volume", "open_price", "profit"}}
        legs.append(public)
        grouped.setdefault((leg["symbol"], leg["side"]), []).append(leg)

    positions: List[CanonicalPosition] = []
    for (symbol, side), items in grouped.items():
        total_vol = sum((item["volume"] for item in items), Decimal("0"))
        notional = sum((item["volume"] * item["open_price"] for item in items), Decimal("0"))
        vwap = (notional / total_vol) if total_vol else Decimal("0")
        prices = [item["open_price"] for item in items]
        pnl = sum((item["profit"] for item in items), Decimal("0"))
        tp_values = {item["tp"] for item in items}
        sl_values = {item["sl"] for item in items}
        positions.append(
            CanonicalPosition(
                symbol=symbol,
                side=side,
                size=_compact_decimal(total_vol),
                entry_price=_compact_decimal(vwap),
                pnl=_dec(pnl, 2),
                tp=next(iter(tp_values)) if len(tp_values) == 1 else None,
                sl=next(iter(sl_values)) if len(sl_values) == 1 else None,
                exchange_instrument=symbol,
                count=len(items),
                min_price=_compact_decimal(min(prices)) if prices else None,
                max_price=_compact_decimal(max(prices)) if prices else None,
                tickets=[item["ticket"] for item in items],
            )
        )
    return positions, legs, tickets


def _pending_side_and_type(raw_type: Any) -> Tuple[str, str]:
    text = str(raw_type or "").strip().upper()
    if "BUY" in text:
        side = "buy"
    elif "SELL" in text:
        side = "sell"
    else:
        side = text.lower() or "buy"
    if "LIMIT" in text:
        display = "LIMIT"
    elif "STOP" in text:
        display = "STOP"
    else:
        display = text or "LIMIT"
    return side, display


def _map_pending_row(row: Any) -> Dict[str, Any]:
    if isinstance(row, dict):
        ticket = _normalize_ticket(row.get("ticket"))
        symbol = str(row.get("symbol") or "")
        raw_type = row.get("type") or row.get("order_type") or ""
        volume = _as_decimal(row.get("volume") or 0)
        price = _as_decimal(row.get("price") or row.get("open_price") or 0)
    else:
        ticket = _normalize_ticket(getattr(row, "ticket", None))
        symbol = str(getattr(row, "symbol", "") or "")
        raw_type = getattr(row, "order_type", None) or getattr(row, "type", "") or ""
        volume = _as_decimal(getattr(row, "volume", 0))
        price = _as_decimal(getattr(row, "price", None) or getattr(row, "open_price", 0) or 0)
    side, display = _pending_side_and_type(raw_type)
    return {
        "ticket": ticket,
        "symbol": symbol,
        "side": side,
        "display_type": display,
        "volume": volume,
        "price": price,
        "size": _compact_decimal(volume),
        "entry_price": _compact_decimal(price),
    }


def _aggregate_orders(rows: List[Any]) -> Tuple[List[CanonicalOrderGroup], List[Dict[str, Any]]]:
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    legs: List[Dict[str, Any]] = []
    for row in rows:
        leg = _map_pending_row(row)
        public = {k: v for k, v in leg.items() if k not in {"volume", "price"}}
        legs.append(public)
        grouped.setdefault((leg["symbol"], leg["side"]), []).append(leg)
    groups: List[CanonicalOrderGroup] = []
    for (symbol, side), items in grouped.items():
        total_vol = sum((item["volume"] for item in items), Decimal("0"))
        notional = sum((item["volume"] * item["price"] for item in items), Decimal("0"))
        vwap = (notional / total_vol) if total_vol else Decimal("0")
        prices = [item["price"] for item in items]
        types = {item["display_type"] for item in items}
        groups.append(
            CanonicalOrderGroup(
                symbol=symbol,
                side=side,
                order_count=len(items),
                total_size=_compact_decimal(total_vol),
                vwap=_compact_decimal(vwap),
                min_price=_compact_decimal(min(prices)) if prices else "0",
                max_price=_compact_decimal(max(prices)) if prices else "0",
                classification="entry_limit",
                display_type=next(iter(types)) if len(types) == 1 else "LIMIT",
                order_ids=[item["ticket"] for item in items],
                exchange_instrument=symbol,
            )
        )
    return groups, legs


def _execute_positions(creds: Dict[str, Any], operation: str) -> CanonicalResponse:
    async def _op(bridge, _raw, _verified):
        positions = await bridge.get_positions()
        try:
            orders = await bridge.get_orders()
        except Exception:  # noqa: BLE001
            orders = []
        return positions, orders

    rows, pending = _with_bridge(creds, operation, _op) or ([], [])
    positions, legs, tickets = _aggregate_positions(list(rows or []))
    pending_rows = list(pending or [])
    order_groups, order_legs = _aggregate_orders(pending_rows)
    pending_list = []
    for row in pending_rows:
        mapped = _map_pending_row(row)
        pending_list.append(
            {
                "ticket": mapped["ticket"],
                "symbol": mapped["symbol"],
                "type": mapped["display_type"],
                "side": mapped["side"],
                "volume": mapped["size"],
                "price": mapped["entry_price"],
                "size": mapped["size"],
                "entry_price": mapped["entry_price"],
            }
        )
    return make_success(
        operation=operation,
        exchange=name,
        account=creds["alias"],
        positions=positions,
        open_order_count=len(pending_list),
        order_groups=order_groups,
        data={
            "position_tickets": tickets,
            "position_legs": legs,
            "pending_orders": pending_list,
            "order_legs": order_legs,
        },
    )


def _execute_list_instruments(creds: Dict[str, Any]) -> CanonicalResponse:
    async def _op(bridge, _raw, _verified):
        return await bridge.get_symbols()

    symbols = _with_bridge(creds, "list_instruments", _op) or []
    items = []
    for sym in symbols:
        text = str(sym)
        items.append(
            {
                "symbol": text,
                "native_symbol": text,
                "display_name": text,
            }
        )
    return make_success(
        operation="list_instruments",
        exchange=name,
        account=creds["alias"],
        data={"instruments": items, "count": len(items)},
    )


async def _quote_candidate(bridge: Any, symbol: str) -> Dict[str, Any]:
    row: Dict[str, Any] = {"symbol": symbol, "native_symbol": symbol}
    try:
        tick = await bridge.get_tick(symbol)
        bid = _tick_price(tick, "bid")
        ask = _tick_price(tick, "ask")
        if bid > 0:
            row["bid"] = _compact_decimal(bid)
        if ask > 0:
            row["ask"] = _compact_decimal(ask)
        if bid > 0 and ask > 0:
            row["price"] = _compact_decimal((bid + ask) / Decimal("2"))
        elif ask > 0:
            row["price"] = _compact_decimal(ask)
        elif bid > 0:
            row["price"] = _compact_decimal(bid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("GET_TICK skipped for %s: %s", symbol, exc)
    try:
        info = await bridge.get_symbol_info(symbol)
        if isinstance(info, dict):
            contract = _info_dec(info, "contract_size")
            step = _info_dec(info, "volume_step")
            vmin = _info_dec(info, "volume_min")
            vmax = _info_dec(info, "volume_max")
            tick = _info_dec(info, "tick_size") or _info_dec(info, "point")
            if contract is not None:
                row["contract_size"] = _compact_decimal(contract)
            if step is not None:
                row["volume_step"] = _compact_decimal(step)
            if vmin is not None:
                row["volume_min"] = _compact_decimal(vmin)
            if vmax is not None:
                row["volume_max"] = _compact_decimal(vmax)
            if tick is not None:
                row["tick_size"] = _compact_decimal(tick)
            base = str(info.get("currency_base") or "").strip()
            if base:
                row["currency_base"] = base
            row["digits"] = info.get("digits")
            row["stops_level"] = info.get("stops_level")
            row["_info"] = info
    except Exception as exc:  # noqa: BLE001
        logger.debug("GET_SYMBOL_INFO skipped for %s: %s", symbol, exc)
    return row


def _execute_resolve_instrument(creds: Dict[str, Any], request: Dict[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or request.get("instrument") or "").strip()
    if not requested:
        return _fail("resolve_instrument", creds["alias"], "MISSING_SYMBOL", "Symbol is required.")
    mapped = _resolve_symbol(creds, requested)
    mapped = mapped if mapped and mapped.upper() != requested.upper() else ""

    async def _op(bridge, _raw, _verified):
        symbols = [str(s) for s in (await bridge.get_symbols() or [])]
        match, candidates = _match_broker_symbols(requested, symbols, mapped)
        names = list(candidates or ([] if not match else [match]))
        quoted = []
        for name in names[:12]:
            quoted.append(await _quote_candidate(bridge, name))
        info = {}
        if match:
            for row in quoted:
                if row.get("symbol") == match:
                    info = row.get("_info") or {}
                    break
        return {"match": match, "candidates": quoted, "info": info}

    try:
        payload = _with_bridge(creds, "resolve_instrument", _op)
    except Exception as exc:  # noqa: BLE001
        return _fail("resolve_instrument", creds["alias"], "MT5_ERROR", sanitize_error_message(str(exc)))
    match = payload.get("match")
    quoted = [row for row in (payload.get("candidates") or []) if isinstance(row, dict) and row.get("symbol")]
    for row in quoted:
        row.pop("_info", None)
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    if match is None:
        if len(quoted) > 1:
            return make_failure(
                operation="resolve_instrument",
                exchange=name,
                account=creds["alias"],
                code="INSTRUMENT_AMBIGUOUS",
                message=sanitize_error_message(f"Symbol {requested} matches multiple broker symbols."),
                data={"candidates": quoted},
            )
        if len(quoted) == 1:
            match = quoted[0]["symbol"]
        else:
            return _fail(
                "resolve_instrument",
                creds["alias"],
                "INSTRUMENT_NOT_FOUND",
                f"Symbol {requested} is not in Market Watch.",
            )
    contract = _info_dec(info, "contract_size") if info else None
    if contract is None:
        for row in quoted:
            if row.get("symbol") == match and row.get("contract_size"):
                try:
                    contract = Decimal(str(row["contract_size"]))
                except Exception:
                    contract = None
                break
    step = _info_dec(info, "volume_step") if info else None
    vmin = _info_dec(info, "volume_min") if info else None
    tick = (_info_dec(info, "tick_size") or _info_dec(info, "point")) if info else None
    base = str((info or {}).get("currency_base") or requested).strip() or requested
    for row in quoted:
        if row.get("symbol") == match and row.get("currency_base"):
            base = str(row.get("currency_base"))
    instrument = CanonicalInstrument(
        requested_symbol=requested,
        symbol=str(match),
        display_name=str(match),
        native_symbol=str(match),
        display_symbol=requested,
        base=base,
        market_type="cfd",
        price_increment=_compact_decimal(tick) if tick and tick > 0 else None,
        size_increment=_compact_decimal(step * contract) if step and contract and step > 0 and contract > 0 else None,
        minimum_size=_compact_decimal(vmin * contract) if vmin and contract and vmin > 0 and contract > 0 else None,
    )
    data = {
        "contract_size": str(contract) if contract is not None else None,
        "volume_min": str(vmin) if vmin is not None else None,
        "volume_max": str(_info_dec(info, "volume_max")) if info and _info_dec(info, "volume_max") is not None else None,
        "volume_step": str(step) if step is not None else None,
        "digits": (info or {}).get("digits"),
        "tick_size": str(tick) if tick is not None else None,
        "stops_level": (info or {}).get("stops_level"),
        "currency_base": base,
        "candidates": quoted,
    }
    for row in quoted:
        if row.get("symbol") == match:
            for key in ("bid", "ask", "price", "contract_size", "volume_step", "volume_min", "tick_size"):
                if row.get(key) not in (None, "") and data.get(key) in (None, ""):
                    data[key] = row.get(key)
            break
    return make_success(
        operation="resolve_instrument",
        exchange=name,
        account=creds["alias"],
        instrument=instrument,
        data=data,
    )


def _execute_market_price(creds: Dict[str, Any], request: Dict[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or request.get("instrument") or "").strip()
    if not requested:
        return _fail("market_price", creds["alias"], "MISSING_SYMBOL", "Symbol is required.")
    native = _resolve_symbol(creds, requested)

    async def _op(bridge, _raw, _verified):
        return await bridge.get_tick(native)

    tick = _with_bridge(creds, "market_price", _op)
    bid = getattr(tick, "bid", None)
    ask = getattr(tick, "ask", None)
    last = getattr(tick, "last", None)
    mid = None
    try:
        if bid is not None and ask is not None:
            mid = (Decimal(str(bid)) + Decimal(str(ask))) / Decimal("2")
    except Exception:
        mid = None
    price = _dec(mid if mid is not None else (last or bid or ask), 5)
    snapshot = CanonicalMarketPrice(
        requested_symbol=requested,
        market=native,
        symbol=native,
        display_name=native,
        native_symbol=native,
        price=price,
        mark_price=price,
        last_external_price=_dec(last, 5) if last not in (None, 0, 0.0) else None,
    )
    return make_success(
        operation="market_price",
        exchange=name,
        account=creds["alias"],
        market_price=snapshot,
        data={"bid": bid, "ask": ask},
    )


def _execute_candles(creds: Dict[str, Any], request: Dict[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or request.get("instrument") or "").strip()
    if not requested:
        return _fail("candles", creds["alias"], "MISSING_SYMBOL", "Symbol is required.")
    native = _resolve_symbol(creds, requested)
    tf_raw = str(request.get("timeframe") or request.get("tf") or "H1").strip().lower()
    tf_name = _TF_MAP.get(tf_raw, tf_raw.upper())
    try:
        limit = int(request.get("limit") or request.get("count") or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 1000))

    async def _op(bridge, _raw, _verified):
        from plugins.trade.vendor.mt5_bridge import Timeframe

        try:
            tf = Timeframe(tf_name)
        except ValueError:
            tf = Timeframe.H1
        return await bridge.get_rates(native, tf, limit)

    rates = _with_bridge(creds, "candles", _op) or []
    candles = []
    for row in rates:
        if not isinstance(row, dict):
            continue
        candles.append(
            {
                "time": row.get("time") or row.get("timestamp"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("tick_volume") or row.get("volume"),
            }
        )
    return make_success(
        operation="candles",
        exchange=name,
        account=creds["alias"],
        data={"candles": candles, "symbol": native, "timeframe": tf_name},
    )


def _order_type_from_request(request: Dict[str, Any]):
    from plugins.trade.vendor.mt5_bridge import OrderType

    raw = str(request.get("order_type") or request.get("type") or "limit").strip().upper()
    side = str(request.get("side") or "").strip().lower()
    if raw in {"MARKET", "STOP", "BUY", "SELL", "BUY_STOP", "SELL_STOP"}:
        raise ValueError("Only LIMIT orders are supported.")
    if raw not in {"", "LIMIT", "BUY_LIMIT", "SELL_LIMIT"}:
        raise ValueError(f"Unsupported order type {raw}/{side}")
    if side in {"buy", "long"}:
        return OrderType.BUY_LIMIT
    if side in {"sell", "short"}:
        return OrderType.SELL_LIMIT
    raise ValueError(f"Unsupported order type {raw}/{side}")


def _tick_price(tick: Any, field: str) -> Decimal:
    if isinstance(tick, dict):
        raw = tick.get(field)
    else:
        raw = getattr(tick, field, None)
    return _as_decimal(raw)


def _execute_new_order(creds: Dict[str, Any], request: Dict[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or "").strip()
    if not requested:
        return _fail("new_order", creds["alias"], "MISSING_SYMBOL", "Symbol is required.")
    try:
        token_qty = Decimal(str(request.get("volume") or request.get("size") or request.get("qty") or "0"))
    except Exception:
        token_qty = Decimal("0")
    if token_qty <= 0:
        return _fail("new_order", creds["alias"], "INVALID_VOLUME", "Volume must be positive.")
    try:
        order_type = _order_type_from_request(request)
    except ValueError as exc:
        return _fail("new_order", creds["alias"], "INVALID_ORDER_TYPE", str(exc))
    price_raw = request.get("price")
    if price_raw in (None, "", 0, 0.0):
        return _fail("new_order", creds["alias"], "MISSING_PRICE", "LIMIT orders require a price.")
    try:
        limit_price = Decimal(str(price_raw))
    except Exception:
        return _fail("new_order", creds["alias"], "INVALID_PRICE", "Price must be positive.")
    if limit_price <= 0:
        return _fail("new_order", creds["alias"], "INVALID_PRICE", "Price must be positive.")
    side = str(request.get("side") or "").strip().lower()
    if side in {"long"}:
        side = "buy"
    elif side in {"short"}:
        side = "sell"
    sl = request.get("sl") or request.get("stop_loss")
    tp = request.get("tp") or request.get("take_profit")
    mapped = _resolve_symbol(creds, requested)
    mapped = mapped if mapped and mapped.upper() != requested.upper() else ""

    async def _op(bridge, _raw, _verified):
        symbols = [str(s) for s in (await bridge.get_symbols() or [])]
        match, candidates = _match_broker_symbols(requested, symbols, mapped)
        if match is None:
            if len(candidates) > 1:
                raise _OrderError(
                    f"Symbol {requested} matches multiple broker symbols: {', '.join(candidates)}.",
                    "INSTRUMENT_AMBIGUOUS",
                )
            raise _OrderError(f"Symbol {requested} is not in Market Watch.", "INSTRUMENT_NOT_FOUND")
        try:
            info = await bridge.get_symbol_info(match)
        except Exception as exc:  # noqa: BLE001
            raise _OrderError(
                "GET_SYMBOL_INFO is required to size LIMIT orders. Recompile the EA with GET_SYMBOL_INFO.",
                "SYMBOL_INFO_UNAVAILABLE",
            ) from exc
        if not isinstance(info, dict) or _info_dec(info, "contract_size") in (None, Decimal("0")):
            raise _OrderError(
                "Broker contract size is unavailable; cannot convert token volume to lots.",
                "SYMBOL_INFO_UNAVAILABLE",
            )
        tick = await bridge.get_tick(match)
        bid = _tick_price(tick, "bid")
        ask = _tick_price(tick, "ask")
        lots = _token_qty_to_lots(token_qty, info)
        _validate_limit_price(side, limit_price, info, bid, ask)
        sl_f = float(sl) if sl not in (None, "") else None
        tp_f = float(tp) if tp not in (None, "") else None
        result = await bridge.place_pending_order(
            match,
            order_type,
            float(limit_price),
            float(lots),
            stop_loss=sl_f,
            take_profit=tp_f,
        )
        return {"result": result, "native": match, "lots": lots, "bid": bid, "ask": ask}

    try:
        payload = _with_bridge(creds, "new_order", _op)
    except _OrderError as exc:
        return _fail("new_order", creds["alias"], exc.code, str(exc))
    result = payload["result"]
    native = payload["native"]
    lots = payload["lots"]
    submitted_price = _dec(getattr(result, "price_executed", limit_price), 5)
    return make_success(
        operation="new_order",
        exchange=name,
        account=creds["alias"],
        order=CanonicalOrderResult(
            symbol=native,
            side=side,
            order_type=order_type.value.lower(),
            requested_volume=_compact_decimal(token_qty),
            requested_price=_compact_decimal(limit_price),
            submitted_volume=_compact_decimal(getattr(result, "volume_executed", lots)),
            submitted_price=submitted_price,
            verified=True,
            status="success",
            exchange_order_id=getattr(result, "ticket", None),
        ),
        data={"lots": _compact_decimal(lots), "bid": _compact_decimal(payload["bid"]), "ask": _compact_decimal(payload["ask"])},
    )


def _execute_ladder(creds: Dict[str, Any], request: Dict[str, Any]) -> CanonicalResponse:
    from plugins.trade.ladder_math import build_ladder_children
    from plugins.trade.vendor.mt5_bridge import OrderType

    requested = str(request.get("symbol") or "").strip()
    side = str(request.get("side") or "").strip().lower()
    if side in {"long"}:
        side = "buy"
    elif side in {"short"}:
        side = "sell"
    distribution = str(request.get("distribution") or "half_gaussian").strip().lower() or "half_gaussian"
    try:
        order_count = int(str(request.get("order_count") or "0").strip())
    except Exception:
        order_count = 0
    try:
        token_qty = Decimal(str(request.get("total_volume") or request.get("volume") or "0"))
    except Exception:
        token_qty = Decimal("0")
    try:
        start_price = Decimal(str(request.get("start_price") or "0"))
        end_price = Decimal(str(request.get("end_price") or "0"))
    except Exception:
        start_price = end_price = Decimal("0")
    if not requested:
        return _fail("ladder", creds["alias"], "MISSING_SYMBOL", "Symbol is required.")
    if side not in {"buy", "sell"}:
        return _fail("ladder", creds["alias"], "INVALID_SIDE", "Side must be buy or sell.")
    if order_count <= 0:
        return _fail("ladder", creds["alias"], "INVALID_ORDER_COUNT", "Order count must be positive.")
    if token_qty <= 0:
        return _fail("ladder", creds["alias"], "INVALID_VOLUME", "Total volume must be positive.")
    if start_price <= 0 or end_price <= 0:
        return _fail("ladder", creds["alias"], "INVALID_PRICE", "Start and end price are required.")
    mapped = _resolve_symbol(creds, requested)
    mapped = mapped if mapped and mapped.upper() != requested.upper() else ""
    order_type = OrderType.BUY_LIMIT if side == "buy" else OrderType.SELL_LIMIT

    async def _op(bridge, _raw, _verified):
        symbols = [str(s) for s in (await bridge.get_symbols() or [])]
        match, candidates = _match_broker_symbols(requested, symbols, mapped)
        if match is None:
            if len(candidates) > 1:
                raise _OrderError(
                    f"Symbol {requested} matches multiple broker symbols: {', '.join(candidates)}.",
                    "INSTRUMENT_AMBIGUOUS",
                )
            raise _OrderError(f"Symbol {requested} is not in Market Watch.", "INSTRUMENT_NOT_FOUND")
        info = await bridge.get_symbol_info(match)
        if not isinstance(info, dict) or _info_dec(info, "contract_size") in (None, Decimal("0")):
            raise _OrderError("Broker contract size is unavailable; cannot convert token volume to lots.", "SYMBOL_INFO_UNAVAILABLE")
        tick = await bridge.get_tick(match)
        bid = _tick_price(tick, "bid")
        ask = _tick_price(tick, "ask")
        lots = _token_qty_to_lots(token_qty, info)
        step = _info_dec(info, "volume_step") or Decimal("0")
        tick_size = _info_dec(info, "tick_size") or _info_dec(info, "point") or Decimal("0")
        try:
            children, submitted_lots, vwap = build_ladder_children(
                side=side,
                distribution=distribution,
                order_count=order_count,
                total_volume=lots,
                start_price=start_price,
                end_price=end_price,
                size_increment=step,
                price_increment=tick_size if tick_size > 0 else Decimal("0.01"),
            )
        except ValueError as exc:
            raise _OrderError(str(exc).replace("_", " ").title(), str(exc) or "INVALID_LADDER_REQUEST") from exc
        ok = []
        failed = []
        for child in children:
            child_price = Decimal(str(child["price"]))
            child_lots = Decimal(str(child["size"]))
            try:
                _validate_limit_price(side, child_price, info, bid, ask)
                result = await bridge.place_pending_order(
                    match,
                    order_type,
                    float(child_price),
                    float(child_lots),
                )
                ticket = getattr(result, "ticket", None)
                if ticket is None and isinstance(result, dict):
                    ticket = result.get("ticket")
                ok.append({"ticket": _normalize_ticket(ticket), "price": str(child["price"]), "lots": str(child["size"])})
            except Exception as exc:  # noqa: BLE001
                failed.append(
                    {
                        "price": str(child["price"]),
                        "lots": str(child["size"]),
                        "error": sanitize_error_message(str(exc)),
                        "retcode": getattr(exc, "error_code", None),
                    }
                )
        return {
            "native": match,
            "children": children,
            "ok": ok,
            "failed": failed,
            "lots": lots,
            "submitted_lots": submitted_lots,
            "vwap": vwap,
            "bid": bid,
            "ask": ask,
            "contract": _info_dec(info, "contract_size"),
        }

    try:
        payload = _with_bridge(creds, "ladder", _op)
    except _OrderError as exc:
        return _fail("ladder", creds["alias"], exc.code, str(exc))
    ok = payload.get("ok") or []
    failed = payload.get("failed") or []
    targeted = len(payload.get("children") or [])
    ok_n = len(ok)
    fail_n = len(failed)
    contract = payload.get("contract") or Decimal("1")
    submitted_tokens = (payload.get("submitted_lots") or Decimal("0")) * contract
    partial = ok_n > 0 and fail_n > 0
    verified = fail_n == 0 and ok_n == targeted and targeted > 0
    status = "success" if verified else ("partial" if ok_n else "failed")
    native = payload["native"]
    if ok_n == 0:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=creds["alias"],
            code="LADDER_NO_VALID_CHILDREN" if targeted == 0 else "LADDER_SUBMIT_FAILED",
            message="No ladder LIMIT orders were accepted.",
            ladder=CanonicalLadderResult(
                symbol=native,
                side=side,
                distribution=distribution,
                requested_order_count=order_count,
                submitted_order_count=0,
                requested_volume=_compact_decimal(token_qty),
                submitted_volume="0",
                batch_count=0,
                verified=False,
                partial=False,
                status="failed",
            ),
            data={"failed": failed, "lots": _compact_decimal(payload.get("lots"))},
        )
    return make_success(
        operation="ladder",
        exchange=name,
        account=creds["alias"],
        ladder=CanonicalLadderResult(
            symbol=native,
            side=side,
            distribution=distribution,
            requested_order_count=order_count,
            submitted_order_count=ok_n,
            requested_volume=_compact_decimal(token_qty),
            submitted_volume=_compact_decimal(submitted_tokens),
            batch_count=1,
            verified=verified,
            partial=partial,
            status=status,
            accepted_child_count=ok_n,
            omitted_order_count=(targeted - ok_n) or None,
            child_order_ids=[row.get("ticket") for row in ok if row.get("ticket")],
            exchange_reason="; ".join(str(row.get("error") or "") for row in failed if row.get("error")) or None,
        ),
        data={
            "lots": _compact_decimal(payload.get("lots")),
            "ok": ok,
            "failed": failed,
            "bid": _compact_decimal(payload.get("bid")),
            "ask": _compact_decimal(payload.get("ask")),
            "vwap": _compact_decimal(payload.get("vwap")),
        },
    )


def _execute_close_position(creds: Dict[str, Any], request: Dict[str, Any]) -> CanonicalResponse:
    expected = _tickets_from_request(request)
    single = _normalize_ticket(request.get("ticket") or request.get("exchange_order_id"))
    symbol = str(request.get("symbol") or "").strip()
    side = str(request.get("side") or "").strip().lower()
    if expected:
        return _execute_group_position_op(creds, "close_position", expected, symbol, side, None)
    if single in (None, ""):
        return _fail("close_position", creds["alias"], "MISSING_TICKET", "Ticket is required.")

    async def _op(bridge, _raw, _verified):
        return await bridge.close_position(single)

    result = _with_bridge(creds, "close_position", _op)
    return make_success(
        operation="close_position",
        exchange=name,
        account=creds["alias"],
        position_action=CanonicalPositionActionResult(
            operation="close_position",
            symbol=symbol or "",
            verified=True,
            price=_dec(getattr(result, "close_price", None), 5) if getattr(result, "close_price", None) is not None else None,
            removed=True,
            status="success",
        ),
    )


def _ticket_from_request(request: Dict[str, Any]) -> Any:
    return request.get("ticket") or request.get("exchange_order_id")


def _tickets_from_request(request: Dict[str, Any]) -> List[str]:
    raw = request.get("tickets") or request.get("order_ids") or []
    if isinstance(raw, (str, int)):
        raw = [raw]
    out = [_normalize_ticket(item) for item in raw]
    return [item for item in out if item]


def _filter_position_group(rows: List[Any], symbol: str, side: str) -> List[Dict[str, Any]]:
    want_symbol = str(symbol or "").strip()
    want_side = str(side or "").strip().lower()
    if want_side in {"buy", "long"}:
        want_side = "long"
    elif want_side in {"sell", "short"}:
        want_side = "short"
    matched: List[Dict[str, Any]] = []
    for row in rows or []:
        leg = _map_position_leg(row)
        if leg["symbol"] != want_symbol:
            continue
        if leg["side"] != want_side:
            continue
        matched.append(leg)
    return matched


def _execute_group_position_op(
    creds: Dict[str, Any],
    operation: str,
    expected: List[str],
    symbol: str,
    side: str,
    price: Optional[float],
) -> CanonicalResponse:
    if not symbol or side not in {"buy", "sell", "long", "short"}:
        return _fail(operation, creds["alias"], "INVALID_REQUEST", "Symbol and side are required for a group action.")
    expected_set = set(expected)

    async def _op(bridge, _raw, _verified):
        positions = await bridge.get_positions()
        live = _filter_position_group(list(positions or []), symbol, side)
        live_keys = [_normalize_ticket(item["ticket"]) for item in live]
        live_keys = [key for key in live_keys if key]
        if set(live_keys) != expected_set:
            raise _OrderError("Position group changed. Confirm again.", "GROUP_CHANGED")
        ok: List[str] = []
        failed: List[Dict[str, Any]] = []
        for item in live:
            key = _normalize_ticket(item["ticket"])
            try:
                if operation == "close_position":
                    await bridge.close_position(key)
                elif operation == "set_tp":
                    await bridge.modify_position(key, take_profit=price)
                else:
                    await bridge.modify_position(key, stop_loss=price)
                if key:
                    ok.append(key)
            except Exception as exc:  # noqa: BLE001
                failed.append(
                    {
                        "ticket": key,
                        "error": sanitize_error_message(str(exc)),
                        "retcode": getattr(exc, "error_code", None),
                    }
                )
        return {"ok": ok, "failed": failed}

    try:
        payload = _with_bridge(creds, operation, _op)
    except _OrderError as exc:
        return _fail(operation, creds["alias"], exc.code, str(exc))
    ok_n = len(payload.get("ok") or [])
    fail_n = len(payload.get("failed") or [])
    targeted = len(expected)
    verified = fail_n == 0 and ok_n == targeted
    removed = operation != "close_position" and price == 0.0
    reasons = [
        str(item.get("error") or "").strip()
        for item in (payload.get("failed") or [])
        if str(item.get("error") or "").strip()
    ]
    return make_success(
        operation=operation,
        exchange=name,
        account=creds["alias"],
        position_action=CanonicalPositionActionResult(
            operation=operation,
            symbol=symbol,
            verified=verified,
            price=None if removed or price is None else _dec(price, 5),
            removed=removed,
            status="success" if verified else ("partial" if ok_n else "failed"),
        ),
        data={
            "ok_tickets": payload.get("ok") or [],
            "failed": payload.get("failed") or [],
            "targeted": targeted,
            "succeeded": ok_n,
            "failed_count": fail_n,
            "exchange_reason": "; ".join(reasons) if reasons else None,
        },
    )


def _execute_set_tpsl(creds: Dict[str, Any], request: Dict[str, Any], operation: str) -> CanonicalResponse:
    expected = _tickets_from_request(request)
    ticket = _normalize_ticket(_ticket_from_request(request))
    raw_price = request.get("price")
    if raw_price is None:
        raw_price = request.get("tp") if operation == "set_tp" else request.get("sl")
    try:
        value = 0.0 if raw_price in (None, "", "0") else float(raw_price)
    except (TypeError, ValueError):
        return _fail(operation, creds["alias"], "INVALID_PRICE", "Price must be numeric.")
    symbol = str(request.get("symbol") or "").strip()
    side = str(request.get("side") or "").strip().lower()
    if expected:
        return _execute_group_position_op(creds, operation, expected, symbol, side, value)
    if ticket in (None, ""):
        return _fail(operation, creds["alias"], "MISSING_TICKET", "Ticket is required.")

    async def _op(bridge, _raw, _verified):
        if operation == "set_tp":
            return await bridge.modify_position(ticket, take_profit=value)
        return await bridge.modify_position(ticket, stop_loss=value)

    result = _with_bridge(creds, operation, _op)
    removed = value == 0.0
    return make_success(
        operation=operation,
        exchange=name,
        account=creds["alias"],
        position_action=CanonicalPositionActionResult(
            operation=operation,
            symbol=symbol,
            verified=True,
            price=None if removed else _dec(value, 5),
            removed=removed,
            status="success",
        ),
        data=result if isinstance(result, dict) else {"ticket": ticket},
    )


def _execute_cancel_order(creds: Dict[str, Any], request: Dict[str, Any]) -> CanonicalResponse:
    ticket = _ticket_from_request(request)
    if ticket in (None, ""):
        return _fail("cancel_order", creds["alias"], "MISSING_TICKET", "Ticket is required.")

    async def _op(bridge, _raw, _verified):
        return await bridge.cancel_order(ticket)

    result = _with_bridge(creds, "cancel_order", _op)
    return make_success(
        operation="cancel_order",
        exchange=name,
        account=creds["alias"],
        data=result if isinstance(result, dict) else {"ticket": ticket, "cancelled": True},
    )


def _normalize_ticket(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        return None
    if isinstance(value, int):
        return str(value)
    text = str(value).strip().strip('"')
    if not text:
        return None
    if text.isdigit():
        return str(int(text))
    try:
        return str(int(text))
    except (TypeError, ValueError):
        return None


def _filter_pending_group(rows: List[Any], symbol: str, side: str) -> List[Dict[str, Any]]:
    want_symbol = str(symbol or "").strip()
    want_side = str(side or "").strip().lower()
    if want_side in {"long"}:
        want_side = "buy"
    elif want_side in {"short"}:
        want_side = "sell"
    matched: List[Dict[str, Any]] = []
    for row in rows or []:
        mapped = _map_pending_row(row)
        if mapped["symbol"] != want_symbol:
            continue
        if mapped["side"] != want_side:
            continue
        matched.append(mapped)
    return matched


def _execute_cancel_orders(creds: Dict[str, Any], request: Dict[str, Any], operation: str) -> CanonicalResponse:
    ticket = _ticket_from_request(request)
    if ticket not in (None, ""):
        return _execute_cancel_order(creds, request)

    symbol = str(request.get("symbol") or "").strip()
    side = str(request.get("side") or "").strip().lower()
    if side in {"long"}:
        side = "buy"
    elif side in {"short"}:
        side = "sell"
    if not symbol or side not in {"buy", "sell"}:
        return _fail(operation, creds["alias"], "INVALID_REQUEST", "Symbol and side are required to cancel a group.")

    raw_tickets = request.get("tickets") or request.get("order_ids") or []
    if isinstance(raw_tickets, (str, int)):
        raw_tickets = [raw_tickets]
    expected = [_normalize_ticket(item) for item in raw_tickets]
    expected = [item for item in expected if item]
    if not expected:
        return _fail(
            operation,
            creds["alias"],
            "MISSING_TICKET",
            "Group cancel requires the confirmed ticket list.",
        )
    expected_set = set(expected)

    async def _op(bridge, _raw, _verified):
        orders = await bridge.get_orders()
        live = _filter_pending_group(list(orders or []), symbol, side)
        live_keys = [_normalize_ticket(item["ticket"]) for item in live]
        live_keys = [key for key in live_keys if key]
        if set(live_keys) != expected_set:
            raise _OrderError("Pending group changed. Confirm again.", "GROUP_CHANGED")
        cancelled: List[str] = []
        failed: List[Dict[str, Any]] = []
        for item in live:
            key = _normalize_ticket(item["ticket"])
            try:
                await bridge.cancel_order(key)
                if key:
                    cancelled.append(key)
            except Exception as exc:  # noqa: BLE001
                failed.append(
                    {
                        "ticket": key,
                        "error": sanitize_error_message(str(exc)),
                        "retcode": getattr(exc, "error_code", None),
                    }
                )
        return {"cancelled": cancelled, "failed": failed}

    try:
        payload = _with_bridge(creds, operation, _op)
    except _OrderError as exc:
        return _fail(operation, creds["alias"], exc.code, str(exc))
    cancelled = list(payload.get("cancelled") or [])
    failed = list(payload.get("failed") or [])
    targeted = len(expected)
    ok_n = len(cancelled)
    fail_n = len(failed)
    verified = fail_n == 0 and ok_n == targeted
    reasons = [str(item.get("error") or "").strip() for item in failed if str(item.get("error") or "").strip()]
    return make_success(
        operation=operation,
        exchange=name,
        account=creds["alias"],
        cancel_group=CanonicalCancelGroupResult(
            symbol=symbol,
            side=side,
            targeted_order_count=targeted,
            cancelled_order_count=ok_n,
            confirmed_absent_count=ok_n,
            remaining_target_count=fail_n,
            verified=verified,
            partial=ok_n > 0 and fail_n > 0,
            status="success" if verified else ("partial" if ok_n else "failed"),
            requested_cancel_count=targeted,
            verified_cancel_count=ok_n,
            exchange_reason="; ".join(reasons) if reasons else None,
        ),
        data={"cancelled_tickets": cancelled, "failed": failed},
    )


def _execute_modify_order(creds: Dict[str, Any], request: Dict[str, Any]) -> CanonicalResponse:
    ticket = _ticket_from_request(request)
    if ticket in (None, ""):
        return _fail("modify_order", creds["alias"], "MISSING_TICKET", "Ticket is required.")
    price = request.get("price")
    sl = request.get("sl") if "sl" in request else request.get("stop_loss")
    tp = request.get("tp") if "tp" in request else request.get("take_profit")
    volume = request.get("volume")

    async def _op(bridge, _raw, _verified):
        return await bridge.modify_order(
            ticket,
            price=None if price in (None, "") else float(price),
            stop_loss=None if sl in (None, "") else float(sl),
            take_profit=None if tp in (None, "") else float(tp),
            volume=None if volume in (None, "") else float(volume),
        )

    result = _with_bridge(creds, "modify_order", _op)
    return make_success(
        operation="modify_order",
        exchange=name,
        account=creds["alias"],
        data=result if isinstance(result, dict) else {"ticket": ticket},
    )


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    if not isinstance(request, dict):
        return _fail("", "", "INVALID_REQUEST", "Request must be a dict.")
    operation = str(request.get("operation") or "").strip()
    account = str(request.get("account") or "").strip()
    if not operation:
        return _fail("", account, "INVALID_REQUEST", "Missing 'operation'.")
    if not account:
        return _fail(operation, "", "MISSING_ACCOUNT", "Missing 'account'.")
    creds = _lookup_account(account)
    if creds is None:
        return _fail(
            operation,
            account,
            "ACCOUNT_NOT_FOUND",
            "Set MT5_<ALIAS>_ACCOUNT (optional PASSWORD/SERVER/PORT).",
        )
    unimplemented = {
        "market_immediate": "Not implemented for MT5.",
    }
    if operation in unimplemented:
        return _fail(operation, creds["alias"], "NOT_IMPLEMENTED", unimplemented[operation])
    try:
        if operation == "balance":
            return _execute_balance(creds)
        if operation in {"positions_orders", "positions_management"}:
            return _execute_positions(creds, operation)
        if operation == "list_instruments":
            return _execute_list_instruments(creds)
        if operation == "resolve_instrument":
            return _execute_resolve_instrument(creds, request)
        if operation == "market_price":
            return _execute_market_price(creds, request)
        if operation == "candles":
            return _execute_candles(creds, request)
        if operation == "new_order":
            return _execute_new_order(creds, request)
        if operation == "ladder":
            return _execute_ladder(creds, request)
        if operation == "close_position":
            return _execute_close_position(creds, request)
        if operation in {"set_tp", "set_sl"}:
            return _execute_set_tpsl(creds, request, operation)
        if operation == "cancel_order":
            return _execute_cancel_order(creds, request)
        if operation in {"cancel_orders", "cancel_order_group"}:
            return _execute_cancel_orders(creds, request, operation)
        if operation == "modify_order":
            return _execute_modify_order(creds, request)
        if operation in {"open_orders", "list_orders", "pending_orders"}:
            return _execute_positions(creds, "positions_orders")
        return _fail(operation, creds["alias"], "NOT_IMPLEMENTED", "Not implemented yet.")
    except _IdentityError as exc:
        return _fail(operation, creds["alias"], getattr(exc, "code", None) or "ACCOUNT_IDENTITY_UNVERIFIED", str(exc))
    except _OrderError as exc:
        return _fail(operation, creds["alias"], getattr(exc, "code", None) or "INVALID_REQUEST", str(exc))
    except RuntimeError as exc:
        text = str(exc)
        code = "MT5_BUSY" if text.startswith("MT5_BUSY") else "MT5_ERROR"
        return _fail(operation, creds["alias"], code, text)
    except Exception as exc:  # noqa: BLE001
        reason = None
        code_attr = getattr(exc, "error_code", None)
        if code_attr not in (None, 0, ""):
            reason = str(code_attr)
        return make_failure(
            operation=operation,
            exchange=name,
            account=creds["alias"],
            code="MT5_ERROR",
            message=sanitize_error_message(str(exc)),
            exchange_reason=reason,
        )


__all__ = ["name", "list_accounts", "capabilities", "execute"]
