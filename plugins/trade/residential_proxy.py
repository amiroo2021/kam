"""Application-level HTTP CONNECT residential proxy for KAM /trade.

Proxy credentials are read from RESIDENTIAL_PROXY_{HOST,PORT,USER,PASS}
and are never written to the persist file, Telegram screens, or logs.
"""

from __future__ import annotations

import base64
import contextvars
import datetime
import fcntl
import hashlib
import json
import logging
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple


logger = logging.getLogger(__name__)

ENV_HOST = "RESIDENTIAL_PROXY_HOST"
ENV_PORT = "RESIDENTIAL_PROXY_PORT"
ENV_USER = "RESIDENTIAL_PROXY_USER"
ENV_PASS = "RESIDENTIAL_PROXY_PASS"

MODE_LABEL = "Application-level HTTP CONNECT"
DEFAULT_IP_PROBE_URL = "https://api.ipify.org"
_STATE_NAME = "residential_ip.json"
_CONNECT_TIMEOUT = 8.0
# Active Trading IP is the proxy egress IP associated with a successful
# authenticated HTTP CONNECT to the exchange, not an IP reported by the venue.
DEFAULT_VERIFICATION_TTL_SECONDS = 24 * 60 * 60

CAP_VERIFIED = "verified"
CAP_PARTIAL = "partial"
CAP_UNSUPPORTED = "unsupported"
CAP_UNVERIFIED = "unverified"
STATUS_PENDING = "UNSUPPORTED — Integration Pending"

_declared_capabilities: Dict[str, str] = {}
_declared_lock = threading.Lock()

_route: contextvars.ContextVar[Optional[Tuple[str, str]]] = contextvars.ContextVar(
    "kam_residential_route", default=None
)
_in_ip_probe: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "kam_residential_ip_probe", default=False
)
_manager_lock = threading.RLock()
_MANAGER: Optional["ResidentialProxyManager"] = None
_HOOKS_INSTALLED = False
_ORIG_URLOPEN = urllib.request.urlopen
_ORIG_BUILD_OPENER = urllib.request.build_opener
_ORIG_SESSION_REQUEST = None
_WS_WRAPPED: set[int] = set()


class ProxyRouteError(RuntimeError):
    """Fail-closed error: residential mode is on but the proxy route failed."""


@dataclass
class ProxyConfig:
    host: str
    port: int
    user: str
    password: str

    def __repr__(self) -> str:  # pragma: no cover - repr must never leak
        return "ProxyConfig(host=***, port=%s, user=***, password=***)" % (self.port,)


@dataclass
class ResidentialStatus:
    exchange: str
    account: str
    mode: str = MODE_LABEL
    supported: bool = True
    enabled: bool = False
    proxy: str = "Missing"
    connection: str = "Disconnected"
    status: str = "OFF"
    exit_ip: Optional[str] = None
    residential_ip: Optional[str] = None
    direct_vps_ip: Optional[str] = None
    route_verified: bool = False
    last_verified_at: Optional[float] = None
    message: str = ""
    reconnect_required: bool = False
    compatibility: str = CAP_UNVERIFIED
    activatable: bool = False

    def _status_label(self) -> str:
        if self.status == "ON":
            return "✅ ON"
        if self.status == "ERROR":
            return "⚠️ ERROR"
        return "❌ OFF"

    def as_text(self) -> str:
        direct = self.direct_vps_ip or "Unknown"
        residential = self.residential_ip or self.exit_ip or "Unknown"
        # Active Trading IP is the proxy egress associated with a verified
        # CONNECT route to the exchange, not an IP the exchange reported.
        if self.status == "ON" and self.route_verified:
            active = residential if residential not in {"", "Unknown"} else "Unverified"
        elif self.status not in {"ON", "ERROR"}:
            active = direct if self.direct_vps_ip else "Unverified"
        else:
            active = "Unverified"
        lines = [
            "🌐 Residential IP",
            "",
            f"Exchange: {self.exchange}",
            f"Account: {self.account}",
            "",
            f"Mode: {self.mode}",
            f"Status: {self._status_label()}",
            f"Compatibility: {self.compatibility.upper()}",
            "",
            f"Direct VPS IP: {direct}",
            f"Residential IP: {residential}",
            f"Active Trading IP: {active}",
        ]
        if self.status == "ON":
            if self.route_verified:
                lines.append("Route Verification: ✅ Verified")
                if self.last_verified_at:
                    ts = datetime.datetime.fromtimestamp(
                        float(self.last_verified_at), tz=datetime.timezone.utc
                    ).strftime("%Y-%m-%d %H:%M:%S UTC")
                    lines.append(f"Last Verified: {ts}")
            else:
                lines.append("Route Verification: ⚠️ Unverified")
        lines.extend(
            [
                "",
                f"Proxy: {self.proxy}",
                f"Connection: {self.connection}",
            ]
        )
        if self.message:
            lines.extend(["", self.message])
        return "\n".join(lines)


def _hermes_home(environ: Mapping[str, str]) -> Path:
    env = str(environ.get("HERMES_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def _norm_exchange(exchange: str) -> str:
    return str(exchange or "").strip().lower()


def _norm_account(account: str) -> str:
    return str(account or "").strip()


def _pair_key(exchange: str, account: str) -> str:
    return f"{_norm_exchange(exchange)}|{_norm_account(account)}"


def redact(text: str, cfg: Optional[ProxyConfig] = None) -> str:
    raw = str(text or "")
    secrets: List[str] = []
    configs = [cfg] if cfg is not None else []
    mgr = _MANAGER
    if mgr is not None and mgr._config is not None:
        configs.append(mgr._config)
    for item in configs:
        for value in (item.user, item.password, item.host):
            if value and value not in secrets:
                secrets.append(value)
        try:
            token = base64.b64encode(f"{item.user}:{item.password}".encode("utf-8")).decode("ascii")
        except Exception:
            token = ""
        if token:
            secrets.append(token)
    secrets.sort(key=len, reverse=True)
    for secret in secrets:
        if secret and secret in raw:
            raw = raw.replace(secret, "***")
    return raw


def _read_http_headers(sock: socket.socket) -> bytes:
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < 65536:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def _basic_token(cfg: ProxyConfig) -> str:
    return base64.b64encode(f"{cfg.user}:{cfg.password}".encode("utf-8")).decode("ascii")


def open_http_connect(
    cfg: ProxyConfig,
    dest_host: str,
    dest_port: int,
    *,
    timeout: float = _CONNECT_TIMEOUT,
) -> socket.socket:
    """Open a TCP tunnel via HTTP CONNECT. Destination DNS is the proxy's job."""
    dest_host = str(dest_host or "").strip()
    if not dest_host:
        raise ProxyRouteError("HTTP CONNECT destination missing")
    try:
        sock = socket.create_connection((cfg.host, int(cfg.port)), timeout=timeout)
    except Exception as exc:
        raise ProxyRouteError("Residential proxy unreachable") from None
    try:
        sock.settimeout(timeout)
        authority = f"{dest_host}:{int(dest_port)}"
        payload = (
            f"CONNECT {authority} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            f"Proxy-Authorization: Basic {_basic_token(cfg)}\r\n"
            "Proxy-Connection: Keep-Alive\r\n"
            "\r\n"
        )
        sock.sendall(payload.encode("ascii"))
        header_blob = _read_http_headers(sock)
        status_line = header_blob.split(b"\r\n", 1)[0].decode("latin1", "replace")
        if " 200 " not in f" {status_line} " and not status_line.endswith(" 200"):
            raise ProxyRouteError("HTTP CONNECT failed")
        leftover = header_blob.split(b"\r\n\r\n", 1)[1]
        if leftover:
            raise ProxyRouteError("HTTP CONNECT returned unexpected extra data")
        return sock
    except ProxyRouteError:
        try:
            sock.close()
        except Exception:
            pass
        raise
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise ProxyRouteError("HTTP CONNECT failed") from None


class _HTTPResponse:
    def __init__(self, body: bytes, status: int, headers: Mapping[str, str]) -> None:
        self._body = body
        self.status = status
        self.headers = headers
        self.code = status

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        data = self._body
        self._body = b""
        return data

    def __enter__(self) -> "_HTTPResponse":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


def _parse_request(url_or_req: Any, data: Any = None) -> urllib.request.Request:
    if isinstance(url_or_req, urllib.request.Request):
        req = url_or_req
        if data is not None and req.data is None:
            req.data = data
        return req
    return urllib.request.Request(url_or_req, data=data)


def _http_on_tunnel(
    sock: socket.socket,
    req: urllib.request.Request,
    *,
    timeout: float,
) -> _HTTPResponse:
    parsed = urllib.parse.urlparse(req.full_url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    method = req.get_method() or "GET"
    body = req.data or b""
    if isinstance(body, str):
        body = body.encode("utf-8")
    header_map = {str(k): str(v) for k, v in req.header_items()}
    header_map.setdefault("Host", parsed.hostname or "")
    header_map.setdefault("Connection", "close")
    header_map.setdefault("Accept", "*/*")
    if body and "Content-Length" not in {k.title(): v for k, v in header_map.items()}:
        header_map["Content-Length"] = str(len(body))
    lines = [f"{method} {path} HTTP/1.1"]
    for key, value in header_map.items():
        if key.lower().startswith("proxy-"):
            continue
        lines.append(f"{key}: {value}")
    blob = ("\r\n".join(lines) + "\r\n\r\n").encode("latin1", "replace") + body
    sock.settimeout(timeout)
    sock.sendall(blob)
    raw = b""
    while True:
        try:
            chunk = sock.recv(8192)
        except socket.timeout:
            break
        if not chunk:
            break
        raw += chunk
        if b"\r\n\r\n" in raw and (method or "GET").upper() in {"GET", "HEAD"}:
            # keep reading until close for simplicity
            pass
    if b"\r\n\r\n" not in raw:
        raise ProxyRouteError("Empty response through residential proxy")
    head, rest = raw.split(b"\r\n\r\n", 1)
    status_line = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
    parts = status_line.split(" ", 2)
    try:
        status = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        status = 0
    headers: Dict[str, str] = {}
    for line in head.decode("latin1", "replace").split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
    length = headers.get("Content-Length")
    if length and length.isdigit():
        rest = rest[: int(length)]
    if status >= 400:
        raise urllib.error.HTTPError(req.full_url, status, "HTTP error", hdrs=None, fp=None)  # type: ignore[arg-type]
    return _HTTPResponse(rest, status, headers)


def proxied_urlopen(url_or_req: Any, data: Any = None, timeout: Any = None, **kwargs: Any) -> Any:
    cfg = _require_config_if_enabled()
    req = _parse_request(url_or_req, data)
    parsed = urllib.parse.urlparse(req.full_url)
    host = parsed.hostname
    if not host:
        raise ProxyRouteError("Residential proxy request missing host")
    scheme = (parsed.scheme or "https").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    tmt = _CONNECT_TIMEOUT if timeout in (None, socket._GLOBAL_DEFAULT_TIMEOUT) else float(timeout)
    sock = open_http_connect(cfg, host, int(port), timeout=tmt)
    try:
        if scheme == "https":
            ctx = kwargs.get("context") or ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        return _http_on_tunnel(sock, req, timeout=tmt)
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _should_proxy() -> bool:
    route = _route.get()
    mgr = get_manager()
    if route is None or mgr is None:
        return False
    exchange, account = route
    return mgr.is_enabled(exchange, account)


def _require_config_if_enabled() -> ProxyConfig:
    mgr = get_manager()
    route = _route.get()
    if mgr is None or route is None:
        raise ProxyRouteError("Residential proxy route is not bound")
    exchange, account = route
    if not mgr.is_enabled(exchange, account):
        raise ProxyRouteError("Residential proxy is not enabled")
    if not mgr.is_supported(exchange):
        raise ProxyRouteError("Residential IP is not supported for this exchange")
    cfg = mgr._config
    if cfg is None:
        raise ProxyRouteError("Residential proxy is not configured")
    return cfg


def _proxied_urlopen(url_or_req: Any, data: Any = None, timeout: Any = None, **kwargs: Any) -> Any:
    if not _should_proxy():
        return _ORIG_URLOPEN(url_or_req, data=data, timeout=timeout, **kwargs)
    try:
        resp = proxied_urlopen(url_or_req, data=data, timeout=timeout, **kwargs)
        _note_proxied_exchange_success()
        return resp
    except ProxyRouteError:
        raise
    except Exception as exc:
        raise ProxyRouteError(redact(str(exc))) from None


def apply_aiohttp_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(kwargs)
    if not _should_proxy():
        return out
    cfg = _require_config_if_enabled()
    out["proxy"] = f"http://{cfg.host}:{int(cfg.port)}"
    try:
        from aiohttp import BasicAuth

        out["proxy_auth"] = BasicAuth(cfg.user, cfg.password)
    except Exception:
        out["proxy"] = f"http://{cfg.user}:{cfg.password}@{cfg.host}:{int(cfg.port)}"
    return out


def note_agent_capability(exchange: str, module: Any = None, capability: Optional[str] = None) -> str:
    """Record an agent's declared residential capability. Default UNVERIFIED."""
    cap = capability
    if cap is None and module is not None:
        raw = getattr(module, "residential_capability", None)
        cap = raw if isinstance(raw, str) else None
    normalized = str(cap or CAP_UNVERIFIED).strip().lower()
    if normalized not in {CAP_VERIFIED, CAP_PARTIAL, CAP_UNSUPPORTED, CAP_UNVERIFIED}:
        normalized = CAP_UNVERIFIED
    key = _norm_exchange(exchange)
    if not key:
        return CAP_UNVERIFIED
    with _declared_lock:
        _declared_capabilities[key] = normalized
    return normalized


def capability_for(exchange: str, agent: Any = None) -> str:
    if agent is not None:
        raw = getattr(agent, "residential_capability", None)
        if isinstance(raw, str) and raw.strip():
            value = raw.strip().lower()
            if value in {CAP_VERIFIED, CAP_PARTIAL, CAP_UNSUPPORTED, CAP_UNVERIFIED}:
                return value
    with _declared_lock:
        return _declared_capabilities.get(_norm_exchange(exchange), CAP_UNVERIFIED)


def can_activate(exchange: str, agent: Any = None) -> bool:
    return capability_for(exchange, agent) == CAP_VERIFIED


def apply_requests_kwargs(session: Any, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(kwargs)
    if not _should_proxy():
        return out
    cfg = _require_config_if_enabled()
    token_url = f"http://{cfg.user}:{cfg.password}@{cfg.host}:{cfg.port}"
    out["proxies"] = {"http": token_url, "https": token_url}
    if session is not None:
        session.trust_env = False
    return out


def _proxied_session_request(self: Any, method: Any, url: Any, **kwargs: Any) -> Any:
    if not _should_proxy():
        return _ORIG_SESSION_REQUEST(self, method, url, **kwargs)
    try:
        kwargs = apply_requests_kwargs(self, kwargs)
        resp = _ORIG_SESSION_REQUEST(self, method, url, **kwargs)
        _note_proxied_exchange_success()
        return resp
    except ProxyRouteError:
        raise
    except Exception as exc:
        raise ProxyRouteError(redact(str(exc))) from None


def _websocket_proxy_kwargs() -> Dict[str, Any]:
    cfg = _require_config_if_enabled()
    return {
        "http_proxy_host": cfg.host,
        "http_proxy_port": int(cfg.port),
        "http_proxy_auth": (cfg.user, cfg.password),
        "proxy_type": "http",
    }


def _wrap_websocket_module(mod: Any) -> None:
    ident = id(mod)
    orig_cc = getattr(mod, "create_connection", None)
    if callable(orig_cc) and not getattr(orig_cc, "_rip_wrapped", False):
        def create_connection(url: Any, **kwargs: Any) -> Any:
            if _should_proxy():
                kwargs.update(_websocket_proxy_kwargs())
            return orig_cc(url, **kwargs)

        create_connection._rip_wrapped = True  # type: ignore[attr-defined]
        mod.create_connection = create_connection
    app = getattr(mod, "WebSocketApp", None)
    orig_rf = getattr(app, "run_forever", None) if app is not None else None
    if callable(orig_rf) and not getattr(orig_rf, "_rip_wrapped", False):
        def run_forever(self: Any, *args: Any, **kwargs: Any) -> Any:
            if _should_proxy():
                kwargs.update(_websocket_proxy_kwargs())
            return orig_rf(self, *args, **kwargs)

        run_forever._rip_wrapped = True  # type: ignore[attr-defined]
        app.run_forever = run_forever
    _WS_WRAPPED.add(ident)


def ensure_hooks() -> None:
    global _HOOKS_INSTALLED, _ORIG_SESSION_REQUEST
    urllib.request.urlopen = _proxied_urlopen

    def _proxied_build_opener(*handlers: Any) -> Any:
        if _should_proxy():
            class _ResidentialHTTPHandler(urllib.request.BaseHandler):
                def http_open(self, req: urllib.request.Request) -> Any:
                    return proxied_urlopen(req)

                def https_open(self, req: urllib.request.Request) -> Any:
                    return proxied_urlopen(req)

            return _ORIG_BUILD_OPENER(_ResidentialHTTPHandler(), *handlers)
        return _ORIG_BUILD_OPENER(*handlers)

    urllib.request.build_opener = _proxied_build_opener
    try:
        import requests.sessions as sessions

        if _ORIG_SESSION_REQUEST is None:
            _ORIG_SESSION_REQUEST = sessions.Session.request
        sessions.Session.request = _proxied_session_request  # type: ignore[method-assign]
    except Exception:
        logger.debug("residential_proxy: requests hook skipped", exc_info=True)
    try:
        import websocket  # type: ignore

        _wrap_websocket_module(websocket)
    except Exception:
        logger.debug("residential_proxy: websocket hook skipped", exc_info=True)
    try:
        import aiohttp

        orig_aio = getattr(aiohttp.ClientSession, "_request", None)
        if callable(orig_aio) and not getattr(orig_aio, "_rip_wrapped", False):
            async def _aio_request(self: Any, method: Any, str_or_url: Any, **kwargs: Any) -> Any:
                if _should_proxy():
                    kwargs = apply_aiohttp_kwargs(kwargs)
                result = await orig_aio(self, method, str_or_url, **kwargs)
                if _should_proxy():
                    _note_proxied_exchange_success()
                return result

            _aio_request._rip_wrapped = True  # type: ignore[attr-defined]
            aiohttp.ClientSession._request = _aio_request  # type: ignore[method-assign]
    except Exception:
        logger.debug("residential_proxy: aiohttp hook skipped", exc_info=True)
    try:
        import httpx

        orig_send = getattr(httpx.Client, "send", None)
        if callable(orig_send) and not getattr(orig_send, "_rip_wrapped", False):
            def _httpx_send(self: Any, request: Any, **kwargs: Any) -> Any:
                if getattr(self, "_rip_proxy_client", False) or not _should_proxy():
                    return orig_send(self, request, **kwargs)
                cfg = _require_config_if_enabled()
                proxy = f"http://{cfg.user}:{cfg.password}@{cfg.host}:{cfg.port}"
                inner = httpx.Client(proxy=proxy, timeout=self.timeout, verify=False)
                inner._rip_proxy_client = True  # type: ignore[attr-defined]
                try:
                    result = inner.send(request, **kwargs)
                    _note_proxied_exchange_success()
                    return result
                finally:
                    inner.close()

            _httpx_send._rip_wrapped = True  # type: ignore[attr-defined]
            httpx.Client.send = _httpx_send  # type: ignore[method-assign]
    except Exception:
        logger.debug("residential_proxy: httpx hook skipped", exc_info=True)
    try:
        import websockets

        orig_ws_connect = getattr(websockets, "connect", None)
        if callable(orig_ws_connect) and not getattr(orig_ws_connect, "_rip_wrapped", False):
            def _ws_connect(*args: Any, **kwargs: Any) -> Any:
                if _should_proxy():
                    raise ProxyRouteError("websockets-library HTTP CONNECT adapter is not verified")
                return orig_ws_connect(*args, **kwargs)

            _ws_connect._rip_wrapped = True  # type: ignore[attr-defined]
            websockets.connect = _ws_connect  # type: ignore[misc]
    except Exception:
        logger.debug("residential_proxy: websockets hook skipped", exc_info=True)
    _HOOKS_INSTALLED = True


class ResidentialProxyManager:
    def __init__(
        self,
        *,
        hermes_home: Optional[Path] = None,
        environ: Optional[Mapping[str, str]] = None,
        ip_probe_url: str = DEFAULT_IP_PROBE_URL,
        direct_ip: Optional[str] = None,
    ) -> None:
        self._environ = dict(environ if environ is not None else os.environ)
        self._home = Path(hermes_home) if hermes_home is not None else _hermes_home(self._environ)
        self._lock = threading.RLock()
        self._ip_probe_url = ip_probe_url
        self._direct_ip_override = direct_ip
        self._verification_ttl_seconds = DEFAULT_VERIFICATION_TTL_SECONDS
        self._config = self._load_config()
        self._enabled: Dict[str, set[str]] = {}
        self._verified: Dict[str, Dict[str, Any]] = {}
        self._last_proxy_egress_ip: Optional[str] = None
        self._load_state()

    def __repr__(self) -> str:
        return f"ResidentialProxyManager(configured={self._config is not None})"

    def _state_path(self) -> Path:
        return self._home / "kam" / _STATE_NAME

    def _load_config(self) -> Optional[ProxyConfig]:
        host = str(self._environ.get(ENV_HOST) or "").strip()
        port_raw = str(self._environ.get(ENV_PORT) or "").strip()
        user = str(self._environ.get(ENV_USER) or "").strip()
        password = str(self._environ.get(ENV_PASS) or "").strip()
        if not (host and port_raw and user and password):
            return None
        try:
            port = int(port_raw)
        except ValueError:
            return None
        if not (1 <= port <= 65535):
            return None
        return ProxyConfig(host=host, port=port, user=user, password=password)

    def _config_fingerprint(self) -> str:
        cfg = self._config
        if cfg is None:
            return ""
        blob = f"{cfg.host}|{cfg.port}|{cfg.user}|{cfg.password}".encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def _load_state(self) -> None:
        path = self._state_path()
        enabled: Dict[str, set[str]] = {}
        verified: Dict[str, Dict[str, Any]] = {}
        stored_fp = ""
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    stored_fp = str(data.get("config_fp") or "")
                    raw = data.get("enabled")
                    if isinstance(raw, dict):
                        for ex, accounts in raw.items():
                            if not isinstance(accounts, list):
                                continue
                            bucket = {
                                _norm_account(a) for a in accounts if isinstance(a, str) and _norm_account(a)
                            }
                            if bucket:
                                enabled[_norm_exchange(str(ex))] = bucket
                    raw_v = data.get("verified")
                    if isinstance(raw_v, dict) and stored_fp == self._config_fingerprint():
                        for key, rec in raw_v.items():
                            if not isinstance(key, str) or not isinstance(rec, dict):
                                continue
                            at = rec.get("at")
                            try:
                                at_f = float(at)
                            except (TypeError, ValueError):
                                continue
                            egress = rec.get("egress_ip")
                            verified[key] = {
                                "at": at_f,
                                "egress_ip": str(egress).strip() if isinstance(egress, str) else "",
                            }
        except Exception:
            logger.warning("residential_proxy: failed to load state file")
        self._enabled = enabled
        self._verified = verified

    def _write_state(self) -> None:
        path = self._state_path()
        path.parent.mkdir(mode=0o700, exist_ok=True)
        payload = {
            "enabled": {
                ex: sorted(accounts) for ex, accounts in sorted(self._enabled.items()) if accounts
            },
            "verified": {
                key: {"at": rec.get("at"), "egress_ip": rec.get("egress_ip") or ""}
                for key, rec in sorted(self._verified.items())
                if isinstance(rec, dict)
            },
            "config_fp": self._config_fingerprint(),
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        tmp_path = path.with_suffix(".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(str(tmp_path), flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(tmp_path), str(path))
            os.chmod(path, 0o600)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def _locked_state(self) -> Iterator[None]:
        path = self._state_path()
        path.parent.mkdir(mode=0o700, exist_ok=True)
        lock_path = path.with_suffix(".lock")
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)

    def is_supported(self, exchange: str, agent: Any = None) -> bool:
        return can_activate(exchange, agent) and bool(_norm_exchange(exchange))

    def is_enabled(self, exchange: str, account: str) -> bool:
        with self._lock:
            accounts = self._enabled.get(_norm_exchange(exchange)) or set()
            return _norm_account(account) in accounts

    def _set_enabled(self, exchange: str, account: str, enabled: bool) -> None:
        ex = _norm_exchange(exchange)
        acc = _norm_account(account)
        with self._lock:
            for _ in self._locked_state():
                self._load_state()
                bucket = self._enabled.setdefault(ex, set())
                key = _pair_key(ex, acc)
                self._verified.pop(key, None)
                if enabled:
                    bucket.add(acc)
                else:
                    bucket.discard(acc)
                    if not bucket:
                        self._enabled.pop(ex, None)
                self._write_state()

    def _live_verification(self, exchange: str, account: str) -> Tuple[bool, Optional[float], Optional[str]]:
        rec = self._verified.get(_pair_key(exchange, account))
        if not isinstance(rec, dict):
            return False, None, None
        try:
            at = float(rec.get("at") or 0)
        except (TypeError, ValueError):
            return False, None, None
        ttl = float(self._verification_ttl_seconds)
        if at <= 0 or (time.time() - at) > ttl:
            return False, None, None
        egress = str(rec.get("egress_ip") or "").strip() or None
        return True, at, egress

    def note_exchange_http_success(self, exchange: str, account: str) -> None:
        if _in_ip_probe.get():
            return
        if not self.is_enabled(exchange, account):
            return
        ex = _norm_exchange(exchange)
        acc = _norm_account(account)
        if not ex or not acc:
            return
        rec = {
            "at": time.time(),
            "egress_ip": str(self._last_proxy_egress_ip or "").strip(),
        }
        with self._lock:
            for _ in self._locked_state():
                self._load_state()
                self._verified[_pair_key(ex, acc)] = rec
                self._write_state()

    def _apply_verification(self, st: ResidentialStatus) -> ResidentialStatus:
        ok, at, egress = self._live_verification(st.exchange, st.account)
        st.route_verified = bool(ok and st.status == "ON")
        st.last_verified_at = at if st.route_verified else None
        if st.route_verified and egress and not st.residential_ip:
            st.residential_ip = egress
        return st

    def _extract_ip(self, body: bytes) -> Optional[str]:
        text = body.decode("ascii", "replace").strip()
        parts = text.replace(",", " ").split()
        for token in parts:
            bits = token.split(".")
            if len(bits) != 4:
                continue
            try:
                nums = [int(b) for b in bits]
            except ValueError:
                continue
            if all(0 <= n <= 255 for n in nums):
                return token
        return None

    def _probe_via_proxy(self) -> Tuple[str, Optional[str]]:
        cfg = self._config
        if cfg is None:
            return "Disconnected", None
        parsed = urllib.parse.urlparse(self._ip_probe_url)
        host = parsed.hostname
        if not host:
            return "Disconnected", None
        scheme = (parsed.scheme or "https").lower()
        port = parsed.port or (443 if scheme == "https" else 80)
        token = _in_ip_probe.set(True)
        try:
            sock = open_http_connect(cfg, host, int(port), timeout=_CONNECT_TIMEOUT)
            try:
                if scheme == "https":
                    ctx = ssl.create_default_context()
                    sock = ctx.wrap_socket(sock, server_hostname=host)
                req = urllib.request.Request(self._ip_probe_url, method="GET")
                resp = _http_on_tunnel(sock, req, timeout=_CONNECT_TIMEOUT)
                ip = self._extract_ip(resp.read())
            finally:
                try:
                    sock.close()
                except Exception:
                    pass
        except Exception:
            return "Disconnected", None
        finally:
            _in_ip_probe.reset(token)
        if ip:
            self._last_proxy_egress_ip = ip
        if not ip:
            return "Connected", None
        return "Connected", ip

    def _resolve_direct_ip(self) -> Optional[str]:
        if self._direct_ip_override:
            return str(self._direct_ip_override).strip() or None
        opener = _ORIG_URLOPEN or urllib.request.urlopen
        try:
            resp = opener(self._ip_probe_url, timeout=_CONNECT_TIMEOUT)
            try:
                return self._extract_ip(resp.read())
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        except Exception:
            return None

    def inspect(self, exchange: str, account: str, *, probe: bool = True, agent: Any = None) -> ResidentialStatus:
        cap = capability_for(exchange, agent)
        activatable = cap == CAP_VERIFIED
        enabled = self.is_enabled(exchange, account)
        proxy = "Configured" if self._config is not None else "Missing"
        st = ResidentialStatus(
            exchange=str(exchange or ""),
            account=str(account or ""),
            supported=activatable,
            enabled=enabled,
            proxy=proxy,
            compatibility=cap,
            activatable=activatable,
            direct_vps_ip=self._resolve_direct_ip(),
            route_verified=False,
        )
        if not activatable:
            st.status = STATUS_PENDING
            st.connection = "Disconnected"
            if cap == CAP_UNSUPPORTED:
                st.message = "Residential routing cannot currently be guaranteed for this venue."
            elif cap == CAP_PARTIAL:
                st.message = "Some required transports still need integration."
            else:
                st.message = "Activation is blocked until compatibility is verified."
            return self._apply_verification(st)
        if proxy == "Missing":
            st.status = "ERROR" if enabled else "OFF"
            st.message = "Proxy is not configured."
            return self._apply_verification(st)
        if not probe:
            st.status = "OFF" if not enabled else "ON"
            if not enabled:
                st.connection = "Disconnected"
            return self._apply_verification(st)
        connection, exit_ip = self._probe_via_proxy()
        st.connection = connection
        st.exit_ip = exit_ip
        st.residential_ip = exit_ip
        if not enabled:
            st.status = "OFF"
            return self._apply_verification(st)
        if connection == "Connected" and exit_ip:
            direct = self._direct_ip_override
            if direct and direct == exit_ip:
                st.status = "ERROR"
                st.message = "Verified egress matches the VPS address."
                return self._apply_verification(st)
            st.status = "ON"
            st.message = "Existing WebSocket sessions keep their current route until they reconnect."
            st.reconnect_required = True
            return self._apply_verification(st)
        st.status = "ERROR"
        st.message = "Residential proxy is enabled but the egress route is not verified."
        return self._apply_verification(st)

    def enable(self, exchange: str, account: str, agent: Any = None) -> ResidentialStatus:
        if not can_activate(exchange, agent):
            return self.inspect(exchange, account, probe=False, agent=agent)
        if self._config is None:
            return self.inspect(exchange, account, probe=True, agent=agent)
        connection, exit_ip = self._probe_via_proxy()
        if connection != "Connected" or not exit_ip:
            st = self.inspect(exchange, account, probe=False, agent=agent)
            st.connection = connection
            st.exit_ip = exit_ip
            st.residential_ip = exit_ip
            st.status = "ERROR"
            st.message = "Proxy connectivity check failed."
            return self._apply_verification(st)
        self._set_enabled(exchange, account, True)
        return self.inspect(exchange, account, probe=True, agent=agent)

    def disable(self, exchange: str, account: str) -> ResidentialStatus:
        self._set_enabled(exchange, account, False)
        return self.inspect(exchange, account, probe=True)

    @contextmanager
    def bind(self, exchange: str, account: str) -> Iterator[None]:
        token = _route.set((_norm_exchange(exchange), _norm_account(account)))
        try:
            yield
        finally:
            _route.reset(token)


def _note_proxied_exchange_success() -> None:
    if _in_ip_probe.get():
        return
    route = _route.get()
    mgr = get_manager()
    if route is None or mgr is None:
        return
    exchange, account = route
    try:
        mgr.note_exchange_http_success(exchange, account)
    except Exception:
        logger.warning("residential_proxy: failed to persist route verification")


def get_manager() -> Optional[ResidentialProxyManager]:
    global _MANAGER
    with _manager_lock:
        if _MANAGER is None:
            _MANAGER = ResidentialProxyManager()
            ensure_hooks()
        return _MANAGER


def set_manager(manager: Optional[ResidentialProxyManager]) -> None:
    global _MANAGER
    with _manager_lock:
        _MANAGER = manager
        if manager is not None:
            ensure_hooks()


@contextmanager
def bind_route(exchange: str, account: str) -> Iterator[None]:
    mgr = get_manager()
    if mgr is None:
        yield
        return
    with mgr.bind(exchange, account):
        yield


def urlopen(url_or_req: Any, data: Any = None, timeout: Any = None, **kwargs: Any) -> Any:
    return _proxied_urlopen(url_or_req, data=data, timeout=timeout, **kwargs)
