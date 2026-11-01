"""Native MT5Linux exchange agent for KAM /trade.

Architecture:
  Linux/Hermes -> mt5linux (RPyC) -> single Docker MT5 terminal (Wine).

Multiple configured accounts share ONE terminal. Every operation that
touches the terminal acquires a process-wide lock and a cross-process
file lock, switches with login(login, server=server) preferring
terminal-saved credentials and falling back to configured passwords when
needed, verifies login/server/connected, then reads or writes.

Write path (pending orders, position SL/TP/close, ladders) uses MT5
order_send over RPyC with ticket-level hedging support. Market-order
entry is not exposed as a Telegram wizard action (pending LIMIT/STOP only).

Account env convention (canonical lowercase prefix)::

    mt5linux_<ALIAS>_ACCOUNT=
    mt5linux_<ALIAS>_PASSWORD=   # optional; never logged or returned
    mt5linux_<ALIAS>_SERVER=     # optional when a known fallback exists

Uppercase ``MT5LINUX_`` keys remain supported for backward compatibility.
Legacy alias ``POTRADE5701520`` is canonicalized to ``PO5701520``.

The Windows FastAPI + EA bridge agent (`metatrader` / x_metatrader_agent)
is a separate exchange and is not modified by this module.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

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
from ..ladder_math import build_ladder_children
from . import _mt5linux_orders as _orders

logger = logging.getLogger(__name__)

name = "mt5linux"
display_name = "MT5Linux"

# mt5linux_<ALIAS>_ACCOUNT|PASSWORD|SERVER  (case-insensitive prefix/suffix)
_ACCOUNT_KEY_RE = re.compile(r"^mt5linux_([A-Za-z0-9_]+)_ACCOUNT$", re.IGNORECASE)
_PASSWORD_KEY_RE = re.compile(r"^mt5linux_([A-Za-z0-9_]+)_PASSWORD$", re.IGNORECASE)
_SERVER_KEY_RE = re.compile(r"^mt5linux_([A-Za-z0-9_]+)_SERVER$", re.IGNORECASE)
_ALIAS_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Legacy alias names folded onto the canonical set used in .env.
_ALIAS_CANONICAL = {
    "POTRADE5701520": "PO5701520",
}

# Verified broker server strings used only when _SERVER is omitted.
_SERVER_FALLBACKS = {
    "LITE7486706": "LiteFinance-MT5-Live",
    "PO5701520": "PoTrade-MT5",
    "POTRADE5701520": "PoTrade-MT5",
    "PUPRIME33849951": "PUPrime-Live2",
}

_READ_OPS = frozenset(
    {
        "ping",
        "balance",
        "positions_orders",
        "positions_management",
        "symbols",
        "list_instruments",
        "resolve_instrument",
        "ticker",
        "market_price",
        "candles",
        "get_tickers",
    }
)
_WRITE_OPS = frozenset(
    {
        "new_order",
        "modify_order",
        "cancel_order",
        "cancel_orders",
        "cancel_order_group",
        "ladder",
        "close_position",
        "set_tp",
        "set_sl",
        "position_close",
        "position_tp",
        "position_sl",
    }
)
# Market-order entry is intentionally not offered via the Telegram wizard.
_BLOCKED_WIZARD_OPS = frozenset({"place_order"})
_MUTATING_OPS = _WRITE_OPS | _BLOCKED_WIZARD_OPS

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 18812
_LOCK_TIMEOUT_S = 60.0
_LOGIN_TIMEOUT_S = 30.0
_SYNC_POLL_S = 0.25
_SYNC_STABLE_READS = 2
_POST_LOGIN_GRACE_S = 0.35

_ORDER_TYPE_NAMES = {
    0: "BUY",
    1: "SELL",
    2: "BUY_LIMIT",
    3: "SELL_LIMIT",
    4: "BUY_STOP",
    5: "SELL_STOP",
    6: "BUY_STOP_LIMIT",
    7: "SELL_STOP_LIMIT",
    8: "CLOSE_BY",
}
_POS_TYPE = {0: "BUY", 1: "SELL"}

_TIMEFRAME_MAP = {
    "M1": 1,
    "1M": 1,
    "M5": 5,
    "5M": 5,
    "M15": 15,
    "15M": 15,
    "M30": 30,
    "30M": 30,
    "H1": 16385,
    "1H": 16385,
    "H4": 16388,
    "4H": 16388,
    "D1": 16408,
    "1D": 16408,
    "W1": 32769,
    "1W": 32769,
    "MN1": 49153,
}

_THREAD_LOCK = threading.RLock()
_FILE_LOCK_FH: Any = None
_CLIENT: Any = None
_CLIENT_KEY: Optional[Tuple[str, int]] = None

# Known site-packages locations for the mt5linux client (RPyC only).
_MT5LINUX_SITE_CANDIDATES = (
    str(Path.home() / ".hermes/mt5linux/venv/lib/python3.11/site-packages"),
    str(Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "mt5linux/venv/lib/python3.11/site-packages"),
    "/root/workspace/.venv-mt5/lib/python3.11/site-packages",
    str(Path.home() / "workspace/.venv-mt5/lib/python3.11/site-packages"),
)


class MT5LinuxConfigError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class MT5LinuxTerminalError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _combined_env() -> Dict[str, str]:
    values = _load_dotenv_values(_hermes_home() / ".env")
    for key, value in os.environ.items():
        values[key] = value
    return {str(k): str(v).strip() for k, v in values.items()}


def _positive_int(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not re.fullmatch(r"[1-9][0-9]*", text):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _env_get_ci(env: Mapping[str, str], *names: str) -> str:
    """Return the first non-empty env value among exact names, then case-insensitive match."""
    for name in names:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    lower_map = {str(k).lower(): str(v).strip() for k, v in env.items()}
    for name in names:
        value = lower_map.get(name.lower(), "")
        if value:
            return value
    return ""


def _canonicalize_alias(alias: str) -> str:
    raw = str(alias or "").strip().upper()
    return _ALIAS_CANONICAL.get(raw, raw)


def connection_endpoint() -> Tuple[str, int]:
    env = _combined_env()
    host = _env_get_ci(env, "mt5linux_HOST", "MT5LINUX_HOST") or _DEFAULT_HOST
    raw_port = _env_get_ci(env, "mt5linux_PORT", "MT5LINUX_PORT") or str(_DEFAULT_PORT)
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise MT5LinuxConfigError("MT5LINUX_CONFIG_INVALID", "MT5LINUX_PORT must be an integer.") from exc
    if not (1 <= port <= 65535):
        raise MT5LinuxConfigError("MT5LINUX_CONFIG_INVALID", "MT5LINUX_PORT must be between 1 and 65535.")
    return host, port


def lock_path() -> Path:
    env = _combined_env()
    configured = _env_get_ci(env, "mt5linux_LOCK_PATH", "MT5LINUX_LOCK_PATH")
    if configured:
        return Path(configured).expanduser()
    return _hermes_home() / "mt5linux.terminal.lock"


def discover_accounts() -> Dict[str, Dict[str, Any]]:
    """Return {ALIAS: {login, server, label, has_password}} from mt5linux_*/MT5LINUX_* vars.

    Passwords are loaded for internal login only and are never included in the
    returned public metadata. ``list_accounts`` never exposes them.
    """
    env = _combined_env()
    logins: Dict[str, int] = {}
    servers: Dict[str, str] = {}
    passwords: Dict[str, str] = {}
    # Prefer lowercase-prefix keys over uppercase when both define the same alias:
    # track source priority (lower number wins).
    login_prio: Dict[str, int] = {}
    server_prio: Dict[str, int] = {}
    password_prio: Dict[str, int] = {}

    def _prio(key: str) -> int:
        # Exact lowercase prefix is preferred; mixed/upper is fallback.
        if key.startswith("mt5linux_"):
            return 0
        if key.startswith("MT5LINUX_"):
            return 1
        return 2

    for key, value in env.items():
        m_acc = _ACCOUNT_KEY_RE.fullmatch(key)
        if m_acc:
            alias = _canonicalize_alias(m_acc.group(1))
            if not _ALIAS_RE.fullmatch(alias):
                continue
            login = _positive_int(value)
            if login is None:
                continue
            p = _prio(key)
            if alias not in logins or p < login_prio.get(alias, 99):
                logins[alias] = login
                login_prio[alias] = p
            continue
        m_srv = _SERVER_KEY_RE.fullmatch(key)
        if m_srv:
            alias = _canonicalize_alias(m_srv.group(1))
            if not _ALIAS_RE.fullmatch(alias):
                continue
            server = str(value or "").strip()
            if not server:
                continue
            p = _prio(key)
            if alias not in servers or p < server_prio.get(alias, 99):
                servers[alias] = server
                server_prio[alias] = p
            continue
        m_pw = _PASSWORD_KEY_RE.fullmatch(key)
        if m_pw:
            alias = _canonicalize_alias(m_pw.group(1))
            if not _ALIAS_RE.fullmatch(alias):
                continue
            password = str(value or "")
            if not password:
                continue
            p = _prio(key)
            if alias not in passwords or p < password_prio.get(alias, 99):
                passwords[alias] = password
                password_prio[alias] = p

    # Deduplicate by broker login: keep the preferred alias (canonical name first).
    by_login: Dict[int, str] = {}
    preferred_order = ("LITE7486706", "PO5701520", "PUPRIME33849951")

    def _alias_rank(alias: str) -> Tuple[int, str]:
        try:
            return (preferred_order.index(alias), alias)
        except ValueError:
            return (len(preferred_order), alias)

    for alias, login in logins.items():
        existing = by_login.get(login)
        if existing is None or _alias_rank(alias) < _alias_rank(existing):
            by_login[login] = alias

    out: Dict[str, Dict[str, Any]] = {}
    for login, alias in sorted(by_login.items(), key=lambda item: _alias_rank(item[1])):
        server = servers.get(alias) or _SERVER_FALLBACKS.get(alias)
        if not server:
            logger.warning("mt5linux account %s missing server and has no fallback; skipped", alias)
            continue
        out[alias] = {
            "login": int(login),
            "server": str(server),
            "label": f"{alias} ({login}@{server})",
            "has_password": bool(passwords.get(alias)),
            # Internal only — never copy into CanonicalResponse.data.
            "_password": passwords.get(alias) or "",
        }
    return out


def list_accounts() -> List[Dict[str, str]]:
    accounts = discover_accounts()
    return [
        {
            "account": alias,
            "label": str(meta["label"]),
        }
        for alias, meta in accounts.items()
    ]


def capabilities() -> List[str]:
    return [
        "balance",
        "positions_orders",
        "positions_management",
        "ping",
        "symbols",
        "list_instruments",
        "resolve_instrument",
        "ticker",
        "market_price",
        "candles",
        "get_tickers",
        "new_order",
        "modify_order",
        "cancel_order",
        "cancel_orders",
        "cancel_order_group",
        "ladder",
        "close_position",
        "set_tp",
        "set_sl",
    ]


def _failure(operation: str, account: str, code: str, message: str, exchange_reason: Optional[str] = None) -> CanonicalResponse:
    return make_failure(
        operation=operation or "",
        exchange=name,
        account=str(account or "").strip().upper(),
        code=code,
        message=sanitize_error_message(message),
        exchange_reason=exchange_reason,
    )


def _readonly_failure(operation: str, account: str) -> CanonicalResponse:
    return _failure(
        operation,
        account,
        "READ_ONLY",
        f"MT5Linux agent is read-only; {operation!r} is disabled.",
    )


def _decimal_text(value: Any) -> str:
    if value is None:
        return "0"
    try:
        if isinstance(value, float):
            dec = Decimal(str(value))
        elif isinstance(value, Decimal):
            dec = value
        else:
            dec = Decimal(str(value).strip().replace(",", ""))
        if not dec.is_finite():
            return "0"
        return format(dec, "f")
    except (InvalidOperation, ValueError, TypeError):
        return "0"


def _import_mt5linux():
    """Import mt5linux, preferring known-good site-packages over a broken pip wheel.

    Some mt5linux tags ship multiline f-strings that SyntaxError on import.
    """
    errors: list[str] = []

    def _try_import(label: str):
        try:
            from mt5linux import MetaTrader5  # type: ignore

            return MetaTrader5
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            # Drop a half-broken module so the next path can retry cleanly.
            for mod_name in list(sys.modules):
                if mod_name == "mt5linux" or mod_name.startswith("mt5linux."):
                    sys.modules.pop(mod_name, None)
            return None

    # Prefer explicitly known working trees first.
    for candidate in _MT5LINUX_SITE_CANDIDATES:
        if candidate and Path(candidate).is_dir():
            if candidate in sys.path:
                sys.path.remove(candidate)
            sys.path.insert(0, candidate)
            got = _try_import(candidate)
            if got is not None:
                return got

    got = _try_import("sys.path")
    if got is not None:
        return got

    raise MT5LinuxConfigError(
        "MT5LINUX_IMPORT_FAILED",
        "mt5linux package is not importable. "
        + "; ".join(errors[:3])
        + " Install a fixed mt5linux build or use /root/workspace/.venv-mt5 site-packages.",
    )


def _get_client():
    global _CLIENT, _CLIENT_KEY
    host, port = connection_endpoint()
    key = (host, port)
    if _CLIENT is not None and _CLIENT_KEY == key:
        return _CLIENT
    if _CLIENT is not None:
        try:
            _CLIENT.shutdown()
        except Exception:  # noqa: BLE001
            pass
        _CLIENT = None
        _CLIENT_KEY = None
    MetaTrader5 = _import_mt5linux()
    client = MetaTrader5(host=host, port=port)
    ok = bool(client.initialize())
    if not ok:
        err = None
        try:
            err = client.last_error()
        except Exception:  # noqa: BLE001
            err = None
        try:
            client.shutdown()
        except Exception:  # noqa: BLE001
            pass
        raise MT5LinuxTerminalError(
            "MT5LINUX_INIT_FAILED",
            f"mt5.initialize() failed: {err!r}",
        )
    _CLIENT = client
    _CLIENT_KEY = key
    return _CLIENT


def _remote_eval(client: Any, expression: str) -> Any:
    return client.eval(expression)


def _remote_execute(client: Any, code: str) -> None:
    client.execute(code)


def _snapshot_code() -> str:
    return r"""
import MetaTrader5 as m
ai = m.account_info()
ti = m.terminal_info()

def _ai(a):
    if a is None:
        return None
    return {
        'login': int(a.login),
        'server': str(a.server),
        'name': str(a.name),
        'company': str(a.company),
        'balance': float(a.balance),
        'equity': float(a.equity),
        'profit': float(a.profit),
        'currency': str(a.currency),
        'margin': float(a.margin),
        'margin_free': float(a.margin_free),
        'margin_level': float(a.margin_level) if a.margin_level is not None else None,
        'leverage': int(a.leverage),
        'trade_mode': int(a.trade_mode),
    }

def _ti(t):
    if t is None:
        return None
    return {
        'connected': bool(t.connected),
        'trade_allowed': bool(t.trade_allowed),
        'tradeapi_disabled': bool(t.tradeapi_disabled),
        'company': str(t.company),
        'name': str(t.name),
        'build': int(t.build),
    }

pos = m.positions_get() or ()
ord_ = m.orders_get() or ()
positions = []
for p in pos:
    positions.append({
        'ticket': int(p.ticket),
        'symbol': str(p.symbol),
        'type': int(p.type),
        'volume': float(p.volume),
        'price_open': float(p.price_open),
        'price_current': float(p.price_current),
        'profit': float(p.profit),
        'sl': float(p.sl),
        'tp': float(p.tp),
        'magic': int(p.magic),
        'comment': str(p.comment),
    })
orders = []
for o in ord_:
    orders.append({
        'ticket': int(o.ticket),
        'symbol': str(o.symbol),
        'type': int(o.type),
        'volume_current': float(o.volume_current),
        'price_open': float(o.price_open),
        'sl': float(o.sl),
        'tp': float(o.tp),
        'magic': int(o.magic),
        'comment': str(o.comment),
    })
snap = {
    'account': _ai(ai),
    'terminal': _ti(ti),
    'positions_total': int(m.positions_total() or 0),
    'orders_total': int(m.orders_total() or 0),
    'positions': positions,
    'orders': orders,
    'last_error': tuple(m.last_error()) if m.last_error() is not None else None,
    'version': m.version(),
}
"""


def _fetch_snapshot(client: Any) -> Dict[str, Any]:
    _remote_execute(client, _snapshot_code())
    snap = _remote_eval(client, "snap")
    if not isinstance(snap, dict):
        raise MT5LinuxTerminalError("MT5LINUX_SNAPSHOT_INVALID", "Remote snapshot was not a dict.")
    return snap


def _attempt_login(client: Any, login: int, server: str, password: Optional[str] = None) -> bool:
    """One login attempt. Never logs password. Returns True on API success flag."""
    try:
        if password is None:
            try:
                return bool(client.login(int(login), server=str(server)))
            except TypeError:
                return bool(client.login(int(login), password="", server=str(server)))
        return bool(client.login(int(login), password=str(password), server=str(server)))
    except Exception as exc:  # noqa: BLE001
        raise MT5LinuxTerminalError(
            "MT5LINUX_LOGIN_EXCEPTION",
            f"login raised: {type(exc).__name__}",
        ) from exc


def _login_account(client: Any, login: int, server: str, password: str = "") -> None:
    """Prefer terminal-saved credentials; use configured password only if needed."""
    t0 = time.time()
    last_err: Any = None

    # 1) Terminal-saved / passwordless path.
    try:
        if _attempt_login(client, login, server, password=None):
            if time.time() - t0 > _LOGIN_TIMEOUT_S:
                raise MT5LinuxTerminalError("MT5LINUX_LOGIN_TIMEOUT", "login exceeded timeout.")
            return
        try:
            last_err = client.last_error()
        except Exception:  # noqa: BLE001
            last_err = None
    except MT5LinuxTerminalError:
        raise
    except Exception as exc:  # noqa: BLE001
        last_err = type(exc).__name__

    # 2) Configured password fallback (never logged).
    if password:
        try:
            if _attempt_login(client, login, server, password=password):
                if time.time() - t0 > _LOGIN_TIMEOUT_S:
                    raise MT5LinuxTerminalError("MT5LINUX_LOGIN_TIMEOUT", "login exceeded timeout.")
                return
            try:
                last_err = client.last_error()
            except Exception:  # noqa: BLE001
                last_err = None
        except MT5LinuxTerminalError:
            raise

    raise MT5LinuxTerminalError(
        "MT5LINUX_LOGIN_FAILED",
        f"login({login}, server={server!r}) failed after saved-cred"
        f"{' + password' if password else ''} attempts; last_error={last_err!r}",
    )


def _wait_synced(client: Any, login: int, server: str) -> Dict[str, Any]:
    deadline = time.time() + _LOGIN_TIMEOUT_S
    last: Optional[Dict[str, Any]] = None
    stable = 0
    time.sleep(_POST_LOGIN_GRACE_S)
    while time.time() < deadline:
        snap = _fetch_snapshot(client)
        last = snap
        ai = snap.get("account") or {}
        ti = snap.get("terminal") or {}
        match = (
            int(ai.get("login") or 0) == int(login)
            and str(ai.get("server") or "") == str(server)
            and bool(ti.get("connected"))
        )
        if match:
            stable += 1
            if stable >= _SYNC_STABLE_READS:
                return snap
        else:
            stable = 0
        time.sleep(_SYNC_POLL_S)
    ai = (last or {}).get("account") or {}
    ti = (last or {}).get("terminal") or {}
    raise MT5LinuxTerminalError(
        "MT5LINUX_SYNC_FAILED",
        "account did not synchronize after login "
        f"(got login={ai.get('login')} server={ai.get('server')!r} connected={ti.get('connected')})",
    )


def _ensure_account(client: Any, login: int, server: str, password: str = "") -> Dict[str, Any]:
    snap = _fetch_snapshot(client)
    ai = snap.get("account") or {}
    ti = snap.get("terminal") or {}
    if (
        int(ai.get("login") or 0) == int(login)
        and str(ai.get("server") or "") == str(server)
        and bool(ti.get("connected"))
    ):
        return snap
    _login_account(client, login, server, password=password)
    return _wait_synced(client, login, server)


@contextmanager
def _terminal_lock(timeout_s: float = _LOCK_TIMEOUT_S) -> Iterator[None]:
    """Thread + cross-process exclusive lock for the shared MT5 terminal."""
    import fcntl

    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    acquired_thread = _THREAD_LOCK.acquire(timeout=timeout_s)
    if not acquired_thread:
        raise MT5LinuxTerminalError("MT5LINUX_LOCK_TIMEOUT", "Timed out waiting for in-process terminal lock.")
    fh = None
    try:
        fh = open(path, "a+", encoding="utf-8")
        deadline = time.time() + timeout_s
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise MT5LinuxTerminalError(
                        "MT5LINUX_LOCK_TIMEOUT",
                        "Timed out waiting for cross-process terminal lock.",
                    )
                time.sleep(0.05)
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} ts={time.time():.3f}\n")
        fh.flush()
        yield
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
        _THREAD_LOCK.release()


def _with_account(alias: str, fn):
    accounts = discover_accounts()
    alias_upper = _canonicalize_alias(str(alias or "").strip().upper())
    meta = accounts.get(alias_upper)
    if meta is None:
        raise MT5LinuxConfigError("ACCOUNT_NOT_CONFIGURED", f"MT5Linux account alias {alias_upper!r} is not configured.")
    login = int(meta["login"])
    server = str(meta["server"])
    password = str(meta.get("_password") or "")
    with _terminal_lock():
        client = _get_client()
        snap = _ensure_account(client, login, server, password=password)
        # Re-verify immediately before user callback so no stale attribution.
        ai = snap.get("account") or {}
        ti = snap.get("terminal") or {}
        if int(ai.get("login") or 0) != login or str(ai.get("server") or "") != server:
            raise MT5LinuxTerminalError(
                "ACCOUNT_MISMATCH",
                f"Active terminal account {ai.get('login')}@{ai.get('server')} != requested {login}@{server}",
            )
        if not bool(ti.get("connected")):
            raise MT5LinuxTerminalError("MT5LINUX_DISCONNECTED", "Terminal reports connected=False after switch.")
        return fn(client, snap, alias_upper, login, server)


def _group_positions(raw_positions: Iterable[Mapping[str, Any]]) -> List[CanonicalPosition]:
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in raw_positions:
        symbol = str(row.get("symbol") or "").strip()
        side = _POS_TYPE.get(int(row.get("type") or -1), str(row.get("type")))
        if side not in {"BUY", "SELL"}:
            side = "UNKNOWN"
        key = (symbol, side)
        g = groups.setdefault(
            key,
            {
                "symbol": symbol,
                "side": side,
                "volume": Decimal("0"),
                "notional": Decimal("0"),
                "pnl": Decimal("0"),
                "count": 0,
                "tp_vals": [],
                "sl_vals": [],
                "marks": [],
            },
        )
        vol = Decimal(str(row.get("volume") or 0))
        px = Decimal(str(row.get("price_open") or 0))
        g["volume"] += vol
        g["notional"] += vol * px
        g["pnl"] += Decimal(str(row.get("profit") or 0))
        g["count"] += 1
        g["tp_vals"].append(row.get("tp"))
        g["sl_vals"].append(row.get("sl"))
        g["marks"].append(row.get("price_current"))
    out: List[CanonicalPosition] = []
    for g in groups.values():
        vol = g["volume"]
        entry = (g["notional"] / vol) if vol != 0 else Decimal("0")
        marks = [Decimal(str(m)) for m in g["marks"] if m is not None]
        mark = None
        if marks:
            mark = format(sum(marks) / len(marks), "f")
        tp = _consensus_protection(g["tp_vals"])
        sl = _consensus_protection(g["sl_vals"])
        out.append(
            CanonicalPosition(
                symbol=g["symbol"],
                side=g["side"],
                size=format(vol, "f"),
                entry_price=format(entry, "f"),
                pnl=format(g["pnl"], "f"),
                tp=tp,
                sl=sl,
                mark=mark,
                count=int(g["count"]),
                exchange_instrument=g["symbol"],
            )
        )
    out.sort(key=lambda p: (p.symbol, p.side))
    return out


def _consensus_protection(values: Iterable[Any]) -> Optional[str]:
    buckets: List[Optional[Decimal]] = []
    for value in values:
        try:
            if value is None:
                buckets.append(None)
                continue
            parsed = Decimal(str(value))
            if not parsed.is_finite() or parsed == 0:
                buckets.append(None)
            else:
                buckets.append(parsed)
        except (InvalidOperation, ValueError, TypeError):
            buckets.append(None)
    if not buckets:
        return None
    set_values = [item for item in buckets if item is not None]
    if not set_values:
        return None
    if any(item is None for item in buckets):
        return "Mixed"
    first = set_values[0]
    for other in set_values[1:]:
        if abs(first - other) > Decimal("1e-8"):
            return "Mixed"
    return format(first, "f")


def _group_orders(raw_orders: Iterable[Mapping[str, Any]]) -> List[CanonicalOrderGroup]:
    groups: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in raw_orders:
        symbol = str(row.get("symbol") or "").strip()
        otype = int(row.get("type") or -1)
        order_type = _ORDER_TYPE_NAMES.get(otype, str(otype))
        side = "buy" if order_type.startswith("BUY") else "sell" if order_type.startswith("SELL") else "buy"
        key = (symbol, side, order_type)
        g = groups.setdefault(
            key,
            {
                "symbol": symbol,
                "side": side,
                "order_type": order_type,
                "count": 0,
                "volume": Decimal("0"),
                "notional": Decimal("0"),
                "min_price": None,
                "max_price": None,
                "order_ids": [],
            },
        )
        vol = Decimal(str(row.get("volume_current") or 0))
        px = Decimal(str(row.get("price_open") or 0))
        g["count"] += 1
        g["volume"] += vol
        g["notional"] += vol * px
        g["order_ids"].append(int(row.get("ticket") or 0))
        if g["min_price"] is None or px < g["min_price"]:
            g["min_price"] = px
        if g["max_price"] is None or px > g["max_price"]:
            g["max_price"] = px
    out: List[CanonicalOrderGroup] = []
    for g in groups.values():
        vol = g["volume"]
        vwap = (g["notional"] / vol) if vol != 0 else Decimal("0")
        order_type = g["order_type"]
        classification = (
            "entry_limit"
            if "LIMIT" in order_type and "STOP" not in order_type
            else "trigger"
            if "STOP" in order_type
            else "other"
        )
        out.append(
            CanonicalOrderGroup(
                symbol=g["symbol"],
                side=g["side"],
                order_count=int(g["count"]),
                total_size=format(vol, "f"),
                vwap=format(vwap, "f"),
                min_price=format(g["min_price"] or 0, "f"),
                max_price=format(g["max_price"] or 0, "f"),
                display_type=order_type,
                classification=classification,
                order_ids=list(g["order_ids"]),
                exchange_instrument=g["symbol"],
            )
        )
    out.sort(key=lambda o: (o.symbol, o.side, o.display_type))
    return out


def _augment_account_data(snap: Mapping[str, Any]) -> Dict[str, Any]:
    ai = dict(snap.get("account") or {})
    currency = str(ai.get("currency") or "").strip()
    data = {
        "account": ai,
        "terminal": dict(snap.get("terminal") or {}),
        "positions_total": snap.get("positions_total"),
        "orders_total": snap.get("orders_total"),
        "positions_raw": list(snap.get("positions") or []),
        "orders_raw": list(snap.get("orders") or []),
        "version": snap.get("version"),
        "last_error": snap.get("last_error"),
        "currency": currency,
        "balance": ai.get("balance"),
        "equity": ai.get("equity"),
        "profit": ai.get("profit"),
        "margin": ai.get("margin"),
        "margin_free": ai.get("margin_free"),
        "free_margin": ai.get("margin_free"),
        "floating_pnl": ai.get("profit"),
        "source": "mt5linux",
    }
    # Defense in depth: never leak credential material into response payloads.
    for banned in ("password", "passwd", "_password"):
        data.pop(banned, None)
        if isinstance(data.get("account"), dict):
            data["account"].pop(banned, None)
    # Preserve native currency always; add USD-normalized convenience fields only for USC.
    if currency.upper() == "USC":
        for key in ("balance", "equity", "profit", "margin", "margin_free"):
            raw = ai.get(key)
            if raw is None:
                continue
            try:
                data[f"{key}_usd"] = float(Decimal(str(raw)) * Decimal("0.01"))
            except (InvalidOperation, ValueError, TypeError):
                pass
        data["currency_normalized"] = "USD"
        data["currency_normalization_factor"] = 0.01
    return data


def _execute_ping(account: str) -> CanonicalResponse:
    def run(client, snap, alias, login, server):
        data = _augment_account_data(snap)
        data["ping"] = "ok"
        data["login"] = login
        data["server"] = server
        return make_success(operation="ping", exchange=name, account=alias, data=data)

    return _run_op("ping", account, run)


def _execute_balance(account: str) -> CanonicalResponse:
    def run(client, snap, alias, login, server):
        data = _augment_account_data(snap)
        ai = snap.get("account") or {}
        unit = str(ai.get("currency") or "").strip()
        if not unit:
            return _failure("balance", alias, "BALANCE_MISSING_CURRENCY", "account currency missing")
        balance = normalize_balance(ai.get("balance"), unit)
        equity = normalize_balance(ai.get("equity", ai.get("balance")), unit).value
        margin = normalize_balance(ai.get("margin", 0), unit).value
        free_margin = normalize_balance(ai.get("margin_free", 0), unit).value
        return make_success(
            operation="balance",
            exchange=name,
            account=alias,
            balance=balance,
            portfolio_summary=CanonicalPortfolioSummary(
                account_value=equity,
                withdrawable=free_margin,
                margin_used=margin,
                total_position_value=equity,
                unit=unit,
            ),
            data=data,
        )

    return _run_op("balance", account, run)


def _execute_positions_orders(account: str, operation: str = "positions_orders") -> CanonicalResponse:
    def run(client, snap, alias, login, server):
        data = _augment_account_data(snap)
        positions = _group_positions(snap.get("positions") or [])
        order_groups = _group_orders(snap.get("orders") or [])
        open_count = int(snap.get("orders_total") or len(snap.get("orders") or []))
        return make_success(
            operation=operation,
            exchange=name,
            account=alias,
            positions=positions,
            open_order_count=open_count,
            order_groups=order_groups,
            data=data,
        )

    return _run_op(operation, account, run)


def _normalize_symbol(raw: str) -> str:
    return str(raw or "").strip().upper().replace("/", "").replace("-", "").replace(" ", "")


def _symbol_info_remote(client: Any, symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch symbol + tick as plain dicts (never return MT5 namedtuples over RPyC).

    Always ``symbol_select`` first — many brokers keep symbols unselected until
    MarketWatch activation; ticks are then zero/empty even when ``symbol_info``
    succeeds. Prefer live bid/ask; fall back to last M1 close when tick is stale.
    """
    code = f"""
import MetaTrader5 as m
import time
sym = {symbol!r}
m.symbol_select(sym, True)
info = m.symbol_info(sym)
tick = None
for _ in range(8):
    tick = m.symbol_info_tick(sym)
    if tick is not None and (float(getattr(tick, 'bid', 0) or 0) > 0 or float(getattr(tick, 'ask', 0) or 0) > 0 or float(getattr(tick, 'last', 0) or 0) > 0):
        break
    time.sleep(0.15)
    info = m.symbol_info(sym) or info
def _info(i):
    if i is None:
        return None
    return {{
        'name': str(i.name),
        'basis': str(getattr(i, 'basis', '') or ''),
        'category': str(getattr(i, 'category', '') or ''),
        'currency_base': str(getattr(i, 'currency_base', '') or ''),
        'currency_profit': str(getattr(i, 'currency_profit', '') or ''),
        'currency_margin': str(getattr(i, 'currency_margin', '') or ''),
        'digits': int(i.digits),
        'point': float(i.point),
        'trade_tick_size': float(getattr(i, 'trade_tick_size', 0) or 0),
        'trade_tick_value': float(getattr(i, 'trade_tick_value', 0) or 0),
        'volume_min': float(getattr(i, 'volume_min', 0) or 0),
        'volume_max': float(getattr(i, 'volume_max', 0) or 0),
        'volume_step': float(getattr(i, 'volume_step', 0) or 0),
        'trade_contract_size': float(getattr(i, 'trade_contract_size', 0) or 0),
        'visible': bool(getattr(i, 'visible', False)),
        'select': bool(getattr(i, 'select', False)),
        'bid': float(getattr(i, 'bid', 0) or 0),
        'ask': float(getattr(i, 'ask', 0) or 0),
        'last': float(getattr(i, 'last', 0) or 0),
        'time': int(getattr(i, 'time', 0) or 0),
        'trade_mode': int(getattr(i, 'trade_mode', 0) or 0),
    }}
def _tick(t, i):
    bid = ask = last = 0.0
    t_time = t_msc = 0
    vol = 0.0
    if t is not None:
        bid = float(getattr(t, 'bid', 0) or 0)
        ask = float(getattr(t, 'ask', 0) or 0)
        last = float(getattr(t, 'last', 0) or 0)
        t_time = int(getattr(t, 'time', 0) or 0)
        t_msc = int(getattr(t, 'time_msc', 0) or 0)
        vol = float(getattr(t, 'volume', 0) or 0)
    if i is not None:
        if bid <= 0:
            bid = float(getattr(i, 'bid', 0) or 0)
        if ask <= 0:
            ask = float(getattr(i, 'ask', 0) or 0)
        if last <= 0:
            last = float(getattr(i, 'last', 0) or 0)
        if t_time <= 0:
            t_time = int(getattr(i, 'time', 0) or 0)
    rate_close = None
    if bid <= 0 and ask <= 0 and last <= 0:
        rates = m.copy_rates_from_pos(sym, m.TIMEFRAME_M1, 0, 1)
        if rates is not None and len(rates) > 0:
            rate_close = float(rates[0]['close'])
            last = rate_close
            t_time = int(rates[0]['time'])
    return {{
        'bid': bid,
        'ask': ask,
        'last': last,
        'volume': vol,
        'time': t_time,
        'time_msc': t_msc,
        'rate_close': rate_close,
    }}
err = m.last_error()
sym_payload = {{
    'info': _info(info),
    'tick': _tick(tick, info),
    'last_error': [int(err[0]), str(err[1])] if err is not None else None,
}}
"""
    _remote_execute(client, code)
    payload = _remote_eval(client, "sym_payload")
    if not isinstance(payload, dict):
        return None
    return payload


def _positive_price(*candidates: Any) -> Optional[str]:
    """First finite price strictly greater than zero, as a plain decimal string."""
    for value in candidates:
        if value is None:
            continue
        try:
            if isinstance(value, float):
                dec = Decimal(str(value))
            elif isinstance(value, Decimal):
                dec = value
            else:
                text = str(value).strip().replace(",", "")
                if not text:
                    continue
                dec = Decimal(text)
            if dec.is_finite() and dec > 0:
                return format(dec, "f")
        except (InvalidOperation, ValueError, TypeError):
            continue
    return None


def _execute_symbols_or_list(account: str, operation: str, request: Mapping[str, Any]) -> CanonicalResponse:
    def run(client, snap, alias, login, server):
        limit = request.get("limit")
        try:
            limit_i = int(limit) if limit is not None else 500
        except (TypeError, ValueError):
            limit_i = 500
        limit_i = max(1, min(limit_i, 5000))
        query = _normalize_symbol(str(request.get("query") or request.get("symbol") or ""))
        code = f"""
import MetaTrader5 as m
syms = m.symbols_get() or ()
rows = []
q = {query!r}
for s in syms:
    name = str(s.name)
    if q and q not in name.upper():
        continue
    rows.append({{
        'name': name,
        'path': str(getattr(s, 'path', '') or ''),
        'visible': bool(getattr(s, 'visible', False)),
        'digits': int(getattr(s, 'digits', 0) or 0),
        'point': float(getattr(s, 'point', 0) or 0),
        'trade_contract_size': float(getattr(s, 'trade_contract_size', 0) or 0),
        'volume_min': float(getattr(s, 'volume_min', 0) or 0),
        'volume_step': float(getattr(s, 'volume_step', 0) or 0),
    }})
    if len(rows) >= {limit_i}:
        break
symbols_payload = {{'count': len(rows), 'symbols': rows, 'total_available': int(m.symbols_total() or 0)}}
"""
        _remote_execute(client, code)
        payload = _remote_eval(client, "symbols_payload")
        data = _augment_account_data(snap)
        data["symbols"] = payload
        instruments = []
        for row in (payload or {}).get("symbols") or []:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("name") or "")
            instruments.append(
                CanonicalInstrument(
                    requested_symbol=sym,
                    symbol=sym,
                    display_name=sym,
                    native_symbol=sym,
                    display_symbol=sym,
                    price_increment=_decimal_text(row.get("point")),
                    size_increment=_decimal_text(row.get("volume_step")),
                    minimum_size=_decimal_text(row.get("volume_min")),
                ).to_dict()
            )
        data["instruments"] = instruments
        return make_success(operation=operation, exchange=name, account=alias, data=data)

    return _run_op(operation, account, run)


def _execute_ticker_or_price(account: str, operation: str, request: Mapping[str, Any]) -> CanonicalResponse:
    symbol = _normalize_symbol(str(request.get("symbol") or request.get("query") or ""))
    if not symbol:
        return _failure(operation, account, "MISSING_SYMBOL", "symbol is required")

    def run(client, snap, alias, login, server):
        payload = _symbol_info_remote(client, symbol)
        info = (payload or {}).get("info")
        tick = (payload or {}).get("tick") or {}
        if not info:
            return _failure(operation, alias, "INSTRUMENT_NOT_FOUND", f"Symbol {symbol!r} not found on this account/server.")
        native = str(info.get("name") or symbol)
        bid = tick.get("bid")
        ask = tick.get("ask")
        last = tick.get("last")
        rate_close = tick.get("rate_close")
        # CFDs often report last=0 while bid/ask are live — never prefer a zero last.
        price_s = _positive_price(bid, ask, last, rate_close, info.get("bid"), info.get("ask"), info.get("last"))
        mark_s = _positive_price(bid, ask, last, rate_close, info.get("bid"), info.get("ask"))
        last_s = _positive_price(last, rate_close)
        meta = {
            "price_increment": _decimal_text(info.get("trade_tick_size") or info.get("point")),
            "size_increment": _decimal_text(info.get("volume_step")),
            "minimum_size": _decimal_text(info.get("volume_min")),
            "contract_size": _decimal_text(info.get("trade_contract_size")),
            "digits": info.get("digits"),
            "bid": bid,
            "ask": ask,
            "last": last,
            "rate_close": rate_close,
            "tick_time": tick.get("time"),
            "tick_time_msc": tick.get("time_msc"),
            "quote_available": price_s is not None,
        }
        data = _augment_account_data(snap)
        data.update({"symbol": native, "info": info, "tick": tick, **meta})
        if price_s is not None:
            data["price"] = price_s
            data["mark_price"] = mark_s or price_s
        instrument = CanonicalInstrument(
            requested_symbol=symbol,
            symbol=native,
            display_name=native,
            native_symbol=native,
            display_symbol=native,
            price_increment=meta["price_increment"],
            size_increment=meta["size_increment"],
            minimum_size=meta["minimum_size"],
        )
        if operation == "resolve_instrument":
            return make_success(
                operation=operation,
                exchange=name,
                account=alias,
                instrument=instrument,
                data=data,
            )
        if operation in {"market_price", "ticker"}:
            if price_s is None:
                return _failure(
                    operation,
                    alias,
                    "QUOTE_UNAVAILABLE",
                    f"No live bid/ask/last for {native!r} on this account (symbol present but quote is zero/stale).",
                )
            market_price = CanonicalMarketPrice(
                requested_symbol=symbol,
                market=native,
                symbol=native,
                native_symbol=native,
                display_symbol=native,
                display_name=native,
                price=price_s,
                mark_price=mark_s or price_s,
                last_external_price=last_s or price_s,
                price_increment=meta["price_increment"],
                size_increment=meta["size_increment"],
                minimum_size=meta["minimum_size"],
            )
            return make_success(
                operation=operation,
                exchange=name,
                account=alias,
                market_price=market_price,
                instrument=instrument,
                data=data,
            )
        return make_success(operation=operation, exchange=name, account=alias, data=data)

    return _run_op(operation, account, run)


def _timeframe_code(value: Any) -> int:
    text = str(value or "M1").strip().upper()
    if text in _TIMEFRAME_MAP:
        return _TIMEFRAME_MAP[text]
    # bare int passthrough for MT5 codes
    try:
        return int(text)
    except ValueError:
        return _TIMEFRAME_MAP["M1"]


def _execute_candles(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    symbol = _normalize_symbol(str(request.get("symbol") or request.get("query") or ""))
    if not symbol:
        return _failure("candles", account, "MISSING_SYMBOL", "symbol is required")
    timeframe = _timeframe_code(request.get("timeframe") or request.get("interval") or "M1")
    try:
        count = int(request.get("limit") or request.get("count") or 100)
    except (TypeError, ValueError):
        count = 100
    count = max(1, min(count, 5000))

    def run(client, snap, alias, login, server):
        # Ensure symbol selected
        _symbol_info_remote(client, symbol)
        code = f"""
import MetaTrader5 as m
rates = m.copy_rates_from_pos({symbol!r}, {int(timeframe)}, 0, {int(count)})
candles = []
if rates is not None:
    for r in rates:
        candles.append({{
            'time': int(r['time']),
            'open': float(r['open']),
            'high': float(r['high']),
            'low': float(r['low']),
            'close': float(r['close']),
            'tick_volume': int(r['tick_volume']) if 'tick_volume' in r.dtype.names else 0,
            'spread': int(r['spread']) if 'spread' in r.dtype.names else 0,
            'real_volume': int(r['real_volume']) if 'real_volume' in r.dtype.names else 0,
        }})
candles_payload = {{'symbol': {symbol!r}, 'timeframe': {int(timeframe)}, 'candles': candles, 'count': len(candles)}}
"""
        _remote_execute(client, code)
        payload = _remote_eval(client, "candles_payload")
        data = _augment_account_data(snap)
        data["candles"] = payload
        return make_success(operation="candles", exchange=name, account=alias, data=data)

    return _run_op("candles", account, run)


def _execute_get_tickers(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    symbols_raw = request.get("symbols") or request.get("symbol") or []
    if isinstance(symbols_raw, str):
        symbols = [_normalize_symbol(s) for s in symbols_raw.split(",") if s.strip()]
    elif isinstance(symbols_raw, list):
        symbols = [_normalize_symbol(str(s)) for s in symbols_raw if str(s).strip()]
    else:
        symbols = []
    symbols = [s for s in symbols if s][:50]

    def run(client, snap, alias, login, server):
        rows = []
        for sym in symbols:
            payload = _symbol_info_remote(client, sym) or {}
            info = payload.get("info") or {}
            tick = payload.get("tick") or {}
            if not info:
                continue
            native = str(info.get("name") or sym)
            bid = tick.get("bid")
            ask = tick.get("ask")
            last = tick.get("last")
            rate_close = tick.get("rate_close")
            price_s = _positive_price(bid, ask, last, rate_close, info.get("bid"), info.get("ask"), info.get("last"))
            rows.append(
                {
                    "symbol": native,
                    "bid": bid,
                    "ask": ask,
                    "last": last,
                    "price": price_s,  # None when unavailable — never "0"
                    "quote_available": price_s is not None,
                    "time": tick.get("time"),
                    "time_msc": tick.get("time_msc"),
                }
            )
        data = _augment_account_data(snap)
        data["tickers"] = rows
        return make_success(operation="get_tickers", exchange=name, account=alias, data=data)

    return _run_op("get_tickers", account, run)


def _run_op(operation: str, account: str, fn) -> CanonicalResponse:
    try:
        return _with_account(account, fn)
    except MT5LinuxConfigError as exc:
        return _failure(operation, account, exc.code, exc.message)
    except MT5LinuxTerminalError as exc:
        return _failure(operation, account, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001
        logger.exception("mt5linux operation %s failed", operation)
        return _failure(operation, account, "MT5LINUX_ERROR", sanitize_error_message(str(exc)))


def restore_account(alias: str) -> CanonicalResponse:
    """Switch terminal back to a configured alias (read-only helper)."""
    return _execute_ping(alias)


def _side_norm(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if text in {"buy", "long"}:
        return "buy"
    if text in {"sell", "short"}:
        return "sell"
    return None


def _positive_dec(value: Any) -> Optional[Decimal]:
    d = _decimal_text  # noqa: F841 — keep helper nearby
    try:
        if value is None:
            return None
        if isinstance(value, float):
            dec = Decimal(str(value))
        elif isinstance(value, Decimal):
            dec = value
        else:
            dec = Decimal(str(value).strip().replace(",", ""))
        if not dec.is_finite() or dec <= 0:
            return None
        return dec
    except (InvalidOperation, ValueError, TypeError):
        return None


def _execute_new_order(request: Mapping[str, Any]) -> CanonicalResponse:
    operation = "new_order"
    account = str(request.get("account") or "").strip()
    symbol = str(request.get("symbol") or "").strip()
    side = _side_norm(request.get("side"))
    order_type = str(request.get("order_type") or request.get("type") or "limit").strip().lower()
    volume = _positive_dec(request.get("volume", request.get("size")))
    price = _positive_dec(request.get("price"))
    sl = _positive_dec(request.get("sl", request.get("stop_loss")))
    tp = _positive_dec(request.get("tp", request.get("take_profit")))
    if not symbol:
        return _failure(operation, account, "MISSING_SYMBOL", "Symbol is required.")
    if side is None:
        return _failure(operation, account, "INVALID_SIDE", "Side must be buy or sell.")
    if order_type not in {"limit", "stop", "buy_limit", "sell_limit", "buy_stop", "sell_stop"}:
        return _failure(operation, account, "UNSUPPORTED_ORDER_TYPE", "MT5Linux supports LIMIT and STOP pending orders only.")
    if volume is None:
        return _failure(operation, account, "INVALID_VOLUME", "Volume must be a positive finite number.")
    if price is None:
        return _failure(operation, account, "INVALID_PRICE", "Price must be a positive finite number.")
    # Map bare limit/stop with side into type
    if order_type == "limit":
        order_type = "buy_limit" if side == "buy" else "sell_limit"
    if order_type == "stop":
        order_type = "buy_stop" if side == "buy" else "sell_stop"
    request_id = _orders._coerce_request_id(request.get("request_id") or request.get("client_order_id"))
    comment = _orders._short_comment(request_id)

    def run(client, snap, alias, login, server):
        # Verify account still matches
        ai = (snap.get("account") or {})
        if int(ai.get("login") or 0) != int(login) or str(ai.get("server") or "") != str(server):
            return _failure(operation, alias, "ACCOUNT_MISMATCH", "Account changed under lock.")
        result = _orders.order_send_pending(
            client,
            symbol=symbol,
            side=side,
            order_type=order_type,
            volume=volume,
            price=price,
            sl=sl,
            tp=tp,
            comment=comment,
        )
        if result.get("ambiguous"):
            return _failure(operation, alias, "AMBIGUOUS_EXECUTION", "Order outcome unknown; reconcile before retry.")
        ticket = int(result.get("order") or 0)
        if result.get("ok") and ticket > 0:
            recon = _orders.reconcile_order(client, ticket)
            if recon is not None and not recon.get("present"):
                # Placed then immediately gone — still report success with note
                result["reconcile"] = recon
        if not result.get("ok"):
            code = str(result.get("error") or "ORDER_REJECTED")
            return _failure(
                operation,
                alias,
                code,
                sanitize_error_message(str(result.get("comment") or code)),
                exchange_reason=str(result.get("retcode")),
            )
        order = CanonicalOrderResult(
            symbol=str(result.get("symbol") or symbol),
            side=side,
            order_type="limit" if "limit" in order_type else "stop",
            requested_volume=_orders._dec_text(volume),
            requested_price=_orders._dec_text(price),
            submitted_volume=_orders._dec_text(result.get("submitted_volume") or volume),
            submitted_price=_orders._dec_text(result.get("submitted_price") or price),
            exchange_order_id=str(ticket) if ticket else None,
            client_order_id=request_id,
            status="open",
            verified=bool(ticket),
        )
        data = dict(result)
        data["request_id"] = request_id
        data["login"] = login
        data["server"] = server
        return make_success(operation=operation, exchange=name, account=alias, order=order, data=data)

    return _run_op(operation, account, run)


def _execute_modify_order(request: Mapping[str, Any]) -> CanonicalResponse:
    operation = "modify_order"
    account = str(request.get("account") or "").strip()
    try:
        ticket = int(str(request.get("order_ticket") or request.get("ticket") or "0"))
    except (TypeError, ValueError):
        ticket = 0
    if ticket <= 0:
        return _failure(operation, account, "INVALID_ORDER_TICKET", "order_ticket must be a positive integer.")
    price = _positive_dec(request.get("price"))
    sl = request.get("sl", request.get("stop_loss"))
    tp = request.get("tp", request.get("take_profit"))
    sl_d = _positive_dec(sl) if sl is not None and str(sl).strip() not in {"", "0", "0.0"} else (Decimal("0") if sl is not None and str(sl).strip() in {"0", "0.0"} else None)
    tp_d = _positive_dec(tp) if tp is not None and str(tp).strip() not in {"", "0", "0.0"} else (Decimal("0") if tp is not None and str(tp).strip() in {"0", "0.0"} else None)
    volume = request.get("volume", request.get("size"))
    if volume is not None and str(volume).strip() != "":
        return _failure(
            operation,
            account,
            "UNSUPPORTED_PARAMETER",
            "Pending order volume modification is not supported; cancel and replace the order.",
        )

    def run(client, snap, alias, login, server):
        result = _orders.order_modify_pending(client, ticket=ticket, price=price, sl=sl_d, tp=tp_d)
        if result.get("ambiguous"):
            return _failure(operation, alias, "AMBIGUOUS_EXECUTION", "Modify outcome unknown; reconcile before retry.")
        if not result.get("ok"):
            return _failure(
                operation,
                alias,
                str(result.get("error") or "ORDER_MODIFY_FAILED"),
                sanitize_error_message(str(result.get("comment") or "modify failed")),
            )
        recon = _orders.reconcile_order(client, ticket)
        data = dict(result)
        data["reconcile"] = recon
        data["login"] = login
        data["server"] = server
        return make_success(operation=operation, exchange=name, account=alias, data=data)

    return _run_op(operation, account, run)


def _execute_cancel_order(request: Mapping[str, Any]) -> CanonicalResponse:
    operation = "cancel_order"
    account = str(request.get("account") or "").strip()
    try:
        ticket = int(str(request.get("order_ticket") or request.get("ticket") or "0"))
    except (TypeError, ValueError):
        ticket = 0
    if ticket <= 0:
        return _failure(operation, account, "INVALID_ORDER_TICKET", "order_ticket must be a positive integer.")

    def run(client, snap, alias, login, server):
        result = _orders.order_cancel(client, ticket=ticket)
        if result.get("ambiguous"):
            return _failure(operation, alias, "AMBIGUOUS_EXECUTION", "Cancel outcome unknown; reconcile before retry.")
        if not result.get("ok"):
            return _failure(
                operation,
                alias,
                str(result.get("error") or "CANCEL_FAILED"),
                sanitize_error_message(str(result.get("comment") or "cancel failed")),
            )
        recon = _orders.reconcile_order(client, ticket)
        data = dict(result)
        data["reconcile"] = recon
        data["verified_absent"] = bool(recon and not recon.get("present"))
        return make_success(operation=operation, exchange=name, account=alias, data=data)

    return _run_op(operation, account, run)


def _execute_cancel_orders(request: Mapping[str, Any]) -> CanonicalResponse:
    operation = str(request.get("operation") or "cancel_orders").strip() or "cancel_orders"
    account = str(request.get("account") or "").strip()
    symbol = str(request.get("symbol") or "").strip()
    if not symbol:
        return _failure(operation, account, "MISSING_SYMBOL", "Symbol is required.")
    side = _side_norm(request.get("side"))
    raw_type = str(request.get("order_type") or request.get("type") or request.get("display_type") or "").strip().upper()
    ladder_id = str(request.get("ladder_id") or "").strip()

    def run(client, snap, alias, login, server):
        orders = _orders.list_account_orders(client, symbol=symbol)
        matched = []
        for o in orders:
            otype = int(o.get("type") or -1)
            o_side = "buy" if otype in (2, 4) else "sell" if otype in (3, 5) else ""
            type_name = _ORDER_TYPE_NAMES.get(otype, "")
            if side and o_side != side:
                continue
            if raw_type:
                base = raw_type.replace("BUY_", "").replace("SELL_", "")
                if base == "LIMIT" and "LIMIT" not in type_name:
                    continue
                if base == "STOP" and "STOP" not in type_name:
                    continue
                if raw_type in {"BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"} and type_name != raw_type:
                    continue
            if ladder_id:
                comment = str(o.get("comment") or "")
                if ladder_id.replace("-", "")[:12] not in comment.replace("-", ""):
                    # also match XL: prefix short form
                    short = ladder_id.replace("-", "")[:12]
                    if short not in comment:
                        continue
            matched.append(o)
        succeeded = 0
        failed = 0
        results = []
        for o in matched:
            ticket = int(o["ticket"])
            r = _orders.order_cancel(client, ticket=ticket)
            if r.get("ambiguous"):
                return _failure(operation, alias, "AMBIGUOUS_EXECUTION", "Cancel batch interrupted; reconcile before retry.")
            ok = bool(r.get("ok"))
            succeeded += 1 if ok else 0
            failed += 0 if ok else 1
            results.append({"ticket": ticket, "ok": ok, "retcode": r.get("retcode"), "comment": r.get("comment")})
        requested = len(matched)
        cancel_verified = requested > 0 and succeeded == requested and failed == 0
        cancel_status = "no_match" if requested == 0 else ("partial" if failed else "success")
        data = {
            "symbol": symbol,
            "side": side or "",
            "requested": requested,
            "matched": requested,
            "succeeded": succeeded,
            "failed": failed,
            "results": results[:50],
            "login": login,
            "server": server,
            "ladder_id": ladder_id or None,
        }
        cg = CanonicalCancelGroupResult(
            symbol=symbol,
            side=side or "",
            targeted_order_count=requested,
            cancelled_order_count=succeeded,
            confirmed_absent_count=succeeded,
            remaining_target_count=max(0, requested - succeeded),
            verified=cancel_verified,
            partial=bool(failed and succeeded),
            status=cancel_status,
            requested_cancel_count=requested,
            verified_cancel_count=succeeded,
        )
        return make_success(operation=operation, exchange=name, account=alias, cancel_group=cg, data=data)

    return _run_op(operation, account, run)


def _execute_ladder(request: Mapping[str, Any]) -> CanonicalResponse:
    operation = "ladder"
    account = str(request.get("account") or "").strip()
    symbol = str(request.get("symbol") or "").strip()
    side = _side_norm(request.get("side"))
    order_type = str(request.get("order_type") or request.get("type") or "limit").strip().lower()
    if not symbol:
        return _failure(operation, account, "MISSING_SYMBOL", "Symbol is required.")
    if side is None:
        return _failure(operation, account, "INVALID_SIDE", "Side must be buy or sell.")
    if order_type not in {"limit", "stop"}:
        return _failure(operation, account, "UNSUPPORTED_ORDER_TYPE", "Ladder supports limit or stop pending orders only.")
    try:
        count = int(str(request.get("order_count") or request.get("count") or "0").strip())
    except (TypeError, ValueError):
        return _failure(operation, account, "INVALID_LADDER", "order_count must be a positive integer.")
    total = _positive_dec(request.get("total_volume", request.get("volume")))
    start = _positive_dec(request.get("start_price"))
    end = _positive_dec(request.get("end_price"))
    if total is None:
        return _failure(operation, account, "INVALID_VOLUME", "total_volume must be positive.")
    if start is None or end is None:
        return _failure(operation, account, "INVALID_PRICE", "start_price and end_price must be positive.")
    if count <= 0:
        return _failure(operation, account, "INVALID_LADDER", "order_count must be positive.")
    distribution = str(request.get("distribution") or "uniform").strip().lower().replace(" ", "_")
    request_id = _orders._coerce_request_id(request.get("request_id"))
    ladder_id = str(request.get("ladder_id") or request_id)

    def run(client, snap, alias, login, server):
        # Freeze account identity for the whole ladder
        locked_login, locked_server = int(login), str(server)
        meta = _orders.remote_symbol_trade_meta(client, symbol)
        if not meta:
            return _failure(operation, alias, "INSTRUMENT_NOT_FOUND", f"Symbol {symbol!r} not found.")
        size_inc = _positive_dec(request.get("size_increment", request.get("volume_step"))) or _positive_dec(meta.get("volume_step")) or Decimal("0.01")
        price_inc = _positive_dec(request.get("price_increment", request.get("tick_size"))) or _positive_dec(meta.get("trade_tick_size")) or _positive_dec(meta.get("point")) or Decimal("0.01")
        try:
            children, submitted_vol, _vwap = build_ladder_children(
                side=side,
                distribution=distribution,
                order_count=count,
                total_volume=total,
                start_price=start,
                end_price=end,
                size_increment=size_inc,
                price_increment=price_inc,
            )
        except ValueError as exc:
            code = str(exc) or "INVALID_LADDER"
            return _failure(operation, alias, code if code.startswith("INVALID") or code.startswith("INSUFFICIENT") else "INVALID_LADDER", sanitize_error_message(code))

        # Persist intent before any send
        record = {
            "ladder_id": ladder_id,
            "request_id": request_id,
            "account": alias,
            "login": locked_login,
            "server": locked_server,
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "distribution": distribution,
            "children_planned": children,
            "submitted": [],
            "status": "in_progress",
        }
        _orders.save_ladder_record(record)

        ot = f"{side}_{order_type}"  # buy_limit / sell_stop
        succeeded = 0
        failed = 0
        submitted_rows = []
        comment_base = _orders._short_comment(ladder_id)
        for idx, child in enumerate(children):
            # Re-verify account before each child to prevent cross-account residual fills
            snap2 = _fetch_snapshot(client)
            ai = snap2.get("account") or {}
            if int(ai.get("login") or 0) != locked_login or str(ai.get("server") or "") != locked_server:
                record["status"] = "aborted_account_mismatch"
                record["submitted"] = submitted_rows
                _orders.save_ladder_record(record)
                return _failure(
                    operation,
                    alias,
                    "ACCOUNT_MISMATCH",
                    "Account changed mid-ladder; remaining children not submitted.",
                )
            px = _positive_dec(child.get("price"))
            vol = _positive_dec(child.get("size"))
            if px is None or vol is None:
                failed += 1
                submitted_rows.append({"index": idx, "ok": False, "error": "INVALID_CHILD"})
                continue
            result = _orders.order_send_pending(
                client,
                symbol=str(meta.get("name") or symbol),
                side=side,
                order_type=ot,
                volume=vol,
                price=px,
                comment=f"{comment_base}:{idx+1}"[:31],
            )
            if result.get("ambiguous"):
                record["status"] = "ambiguous"
                record["submitted"] = submitted_rows + [{"index": idx, "ambiguous": True, "result": result}]
                _orders.save_ladder_record(record)
                return _failure(operation, alias, "AMBIGUOUS_EXECUTION", "Ladder child outcome unknown; do not retry automatically.")
            ok = bool(result.get("ok"))
            ticket = int(result.get("order") or 0)
            if ok and ticket:
                succeeded += 1
            else:
                failed += 1
            submitted_rows.append({"index": idx, "ok": ok, "ticket": ticket, "retcode": result.get("retcode"), "price": float(px), "volume": float(vol), "comment": result.get("comment")})
            record["submitted"] = submitted_rows
            _orders.save_ladder_record(record)

        record["status"] = "partial" if failed and succeeded else ("success" if not failed else "failed")
        _orders.save_ladder_record(record)
        requested = len(children)
        ladder = CanonicalLadderResult(
            symbol=str(meta.get("name") or symbol),
            side=side,
            distribution=distribution,
            requested_order_count=requested,
            submitted_order_count=succeeded,
            requested_volume=_orders._dec_text(total),
            submitted_volume=_orders._dec_text(submitted_vol),
            batch_count=1,
            verified=failed == 0 and succeeded == requested,
            partial=bool(failed and succeeded),
            status="partial" if failed and succeeded else ("success" if not failed else "failed"),
            accepted_child_count=succeeded,
            child_order_ids=[r.get("ticket") for r in submitted_rows if r.get("ticket")],
        )
        data = {
            "ladder_id": ladder_id,
            "request_id": request_id,
            "symbol": symbol,
            "side": side,
            "requested": requested,
            "succeeded": succeeded,
            "failed": failed,
            "submitted": submitted_rows,
            "login": locked_login,
            "server": locked_server,
            "distribution": distribution,
        }
        if failed and not succeeded:
            return _failure(
                operation,
                alias,
                "LADDER_FAILED",
                f"All {failed} ladder children rejected.",
            )
        return make_success(operation=operation, exchange=name, account=alias, ladder=ladder, data=data)

    return _run_op(operation, account, run)


def _execute_position_action(request: Mapping[str, Any], operation: str) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    # Prefer explicit ticket for hedging
    raw_ticket = request.get("position_ticket") or request.get("ticket") or request.get("position")
    try:
        ticket = int(str(raw_ticket).strip()) if raw_ticket is not None else 0
    except (TypeError, ValueError):
        ticket = 0
    symbol = str(request.get("symbol") or "").strip()
    side = _side_norm(request.get("side"))

    def run(client, snap, alias, login, server):
        nonlocal ticket
        if ticket <= 0:
            # resolve single ticket by symbol+side if exactly one match
            positions = list(snap.get("positions") or [])
            matches = []
            for p in positions:
                if symbol and str(p.get("symbol")) != symbol:
                    continue
                pside = "buy" if int(p.get("type") or -1) == 0 else "sell"
                if side and pside != side:
                    continue
                matches.append(p)
            if len(matches) != 1:
                return _failure(
                    operation,
                    alias,
                    "AMBIGUOUS_POSITION",
                    "Provide position_ticket for hedged accounts when multiple positions match.",
                )
            ticket = int(matches[0]["ticket"])

        if operation in {"set_tp", "position_tp"}:
            price = request.get("tp", request.get("price", request.get("take_profit")))
            # 0 means remove
            if price is not None and str(price).strip() in {"0", "0.0"}:
                tp = Decimal("0")
            else:
                tp = _positive_dec(price)
                if tp is None and price is not None:
                    return _failure(operation, alias, "INVALID_PRICE", "tp must be non-negative.")
            result = _orders.position_set_sltp(client, ticket=ticket, sl=None, tp=tp if tp is not None else Decimal("0"))
            op_name = "set_tp"
        elif operation in {"set_sl", "position_sl"}:
            price = request.get("sl", request.get("price", request.get("stop_loss")))
            if price is not None and str(price).strip() in {"0", "0.0"}:
                sl = Decimal("0")
            else:
                sl = _positive_dec(price)
                if sl is None and price is not None:
                    return _failure(operation, alias, "INVALID_PRICE", "sl must be non-negative.")
            result = _orders.position_set_sltp(client, ticket=ticket, sl=sl if sl is not None else Decimal("0"), tp=None)
            op_name = "set_sl"
        else:
            vol = _positive_dec(request.get("volume", request.get("size")))
            result = _orders.position_close(client, ticket=ticket, volume=vol)
            op_name = "close_position"

        if result.get("ambiguous"):
            return _failure(operation, alias, "AMBIGUOUS_EXECUTION", "Position action outcome unknown; reconcile before retry.")
        if not result.get("ok"):
            return _failure(
                operation,
                alias,
                str(result.get("error") or "POSITION_ACTION_FAILED"),
                sanitize_error_message(str(result.get("comment") or "failed")),
            )
        pa = CanonicalPositionActionResult(
            operation=op_name,
            symbol=str(result.get("symbol") or symbol or ""),
            verified=True,
            price=_orders._dec_text(result.get("tp") or result.get("sl") or result.get("closed_volume")),
            removed=(op_name in {"set_tp", "set_sl"} and float(result.get("tp") or result.get("sl") or 1) == 0.0),
            current_side=str(result.get("side") or "").lower() or None,
            message=str(result.get("comment") or "ok"),
            exchange_order_id=str(result.get("deal") or result.get("order") or ticket),
        )
        data = dict(result)
        data["login"] = login
        data["server"] = server
        return make_success(operation=operation, exchange=name, account=alias, position_action=pa, data=data)

    return _run_op(operation, account, run)


def execute(request: Mapping[str, Any]) -> CanonicalResponse:
    if not isinstance(request, Mapping):
        return _failure("", "", "INVALID_REQUEST", "Request must be a dict.")
    operation = str(request.get("operation") or "").strip()
    account = str(request.get("account") or "").strip()
    if not operation:
        return _failure("", account, "INVALID_REQUEST", "Missing 'operation'.")
    if not account:
        return _failure(operation, account, "MISSING_ACCOUNT", "Account alias is required.")
    if operation in _BLOCKED_WIZARD_OPS:
        return _failure(operation, account, "UNSUPPORTED_OPERATION", "Market-order entry is not available via MT5Linux wizard path; use pending LIMIT/STOP or ladder.")
    if operation == "ping":
        return _execute_ping(account)
    if operation == "balance":
        return _execute_balance(account)
    if operation == "positions_orders":
        return _execute_positions_orders(account)
    if operation == "positions_management":
        return _execute_positions_orders(account, operation="positions_management")
    if operation in {"symbols", "list_instruments"}:
        return _execute_symbols_or_list(account, operation, request)
    if operation in {"ticker", "market_price", "resolve_instrument"}:
        return _execute_ticker_or_price(account, operation, request)
    if operation == "candles":
        return _execute_candles(account, request)
    if operation == "get_tickers":
        return _execute_get_tickers(account, request)
    if operation == "new_order":
        return _execute_new_order(request)
    if operation == "modify_order":
        return _execute_modify_order(request)
    if operation == "cancel_order":
        return _execute_cancel_order(request)
    if operation in {"cancel_orders", "cancel_order_group"}:
        return _execute_cancel_orders(request)
    if operation == "ladder":
        return _execute_ladder(request)
    if operation in {"close_position", "position_close", "set_tp", "set_sl", "position_tp", "position_sl"}:
        return _execute_position_action(request, operation)
    if operation in _READ_OPS:
        return _failure(operation, account, "NOT_IMPLEMENTED", f"MT5Linux read operation {operation!r} is not implemented.")
    return _failure(operation, account, "NOT_IMPLEMENTED", f"MT5Linux does not implement {operation!r}.")
