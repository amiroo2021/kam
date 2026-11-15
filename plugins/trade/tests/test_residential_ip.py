"""Residential IP (HTTP CONNECT) — local tests, no live orders.

Run::

    /usr/local/lib/hermes-agent/venv/bin/python -m unittest \\
        plugins.trade.tests.test_residential_ip -v
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock


_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


SECRET_USER = "rip-user"
SECRET_PASS = "rip-s3cret-pass"


class _FakeDesk:
    def list_exchanges(self) -> List[str]:
        return ["bulk", "mt5"]

    def list_accounts(self, exchange: str) -> List[str]:
        if exchange == "bulk":
            return ["amiroo", "other"]
        if exchange == "mt5":
            return ["term1"]
        return []

    def execute(self, request: Dict[str, Any]) -> Any:
        raise AssertionError("trading execute must not run during Residential IP tests")


class _ConnectProxy(threading.Thread):
    """Minimal HTTP CONNECT proxy that records targets and optional Basic auth."""

    def __init__(
        self,
        *,
        user: str = SECRET_USER,
        password: str = SECRET_PASS,
        require_auth: bool = True,
        backend: Tuple[str, int] | None = None,
        reject: bool = False,
    ) -> None:
        super().__init__(daemon=True)
        self.user = user
        self.password = password
        self.require_auth = require_auth
        self.backend = backend
        self.reject = reject
        self.targets: List[str] = []
        self.auth_attempts = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.port = int(self._sock.getsockname()[1])
        self._stop = threading.Event()
        self.errors: List[str] = []

    def run(self) -> None:
        self._sock.listen(16)
        self._sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                client, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def _handle(self, client: socket.socket) -> None:
        client.settimeout(5)
        try:
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 8192:
                chunk = client.recv(1024)
                if not chunk:
                    break
                buf += chunk
            head = buf.split(b"\r\n\r\n", 1)[0].decode("latin1", "replace")
            first = head.split("\r\n", 1)[0]
            if first.upper().startswith("CONNECT "):
                target = first.split(" ", 2)[1]
                self.targets.append(target)
                if self.require_auth:
                    expected = "Basic " + base64.b64encode(
                        f"{self.user}:{self.password}".encode()
                    ).decode()
                    authed = any(
                        line.lower().startswith("proxy-authorization:")
                        and expected in line
                        for line in head.split("\r\n")
                    )
                    self.auth_attempts += 1
                    if not authed:
                        client.sendall(
                            b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                            b"Proxy-Authenticate: Basic realm=\"p\"\r\n"
                            b"\r\n"
                        )
                        return
                if self.reject:
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    return
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if self.backend is None:
                    return
                backend = socket.create_connection(self.backend, timeout=5)
                leftover = buf.split(b"\r\n\r\n", 1)[1]
                if leftover:
                    backend.sendall(leftover)
                self._pump(client, backend)
                return
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
        except Exception as exc:  # noqa: BLE001
            self.errors.append(type(exc).__name__)
        finally:
            try:
                client.close()
            except OSError:
                pass

    @staticmethod
    def _pump(a: socket.socket, b: socket.socket) -> None:
        a.setblocking(False)
        b.setblocking(False)
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                idle = True
                for src, dst in ((a, b), (b, a)):
                    try:
                        data = src.recv(8192)
                    except (BlockingIOError, ssl.SSLWantReadError):
                        continue
                    except OSError:
                        return
                    if data:
                        idle = False
                        try:
                            dst.sendall(data)
                        except OSError:
                            return
                    elif data == b"":
                        return
                if idle:
                    time.sleep(0.01)
        finally:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass


class _IpHandler(BaseHTTPRequestHandler):
    origin_ip = "203.0.113.9"

    def do_GET(self) -> None:  # noqa: N802
        body = self.origin_ip.encode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return


def _start_origin() -> Tuple[ThreadingHTTPServer, int]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _IpHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, int(httpd.server_address[1])


def _env_for(proxy: _ConnectProxy, home: Path) -> Dict[str, str]:
    return {
        "HERMES_HOME": str(home),
        "RESIDENTIAL_PROXY_HOST": "127.0.0.1",
        "RESIDENTIAL_PROXY_PORT": str(proxy.port),
        "RESIDENTIAL_PROXY_USER": SECRET_USER,
        "RESIDENTIAL_PROXY_PASS": SECRET_PASS,
    }


class ResidentialTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rip_home_")
        self.home = Path(self._tmp.name)
        (self.home / "kam").mkdir()
        self.proxy = _ConnectProxy()
        self.proxy.start()
        time.sleep(0.05)
        self.origin, self.origin_port = _start_origin()
        self.proxy.backend = ("127.0.0.1", self.origin_port)
        from plugins.trade import residential_proxy as rp

        self.rp = rp
        self.mgr = rp.ResidentialProxyManager(
            hermes_home=self.home,
            environ=_env_for(self.proxy, self.home),
            ip_probe_url=f"http://127.0.0.1:{self.origin_port}/",
            direct_ip="198.51.100.1",
        )
        rp.set_manager(self.mgr)
        rp.ensure_hooks()
        for _ex in ("bulk", "hyperliquid", "qfex", "arcus", "apex", "edgex", "lighter", "perpl"):
            rp.note_agent_capability(_ex, capability="verified")
        for _ex in ("mt5", "mt5linux", "metatrader"):
            rp.note_agent_capability(_ex, capability="unsupported")

    def tearDown(self) -> None:
        try:
            self.rp.set_manager(None)
        except Exception:
            pass
        try:
            self.origin.shutdown()
            self.origin.server_close()
        except Exception:
            pass
        self.proxy.stop()
        self._tmp.cleanup()

    def _assert_no_secrets(self, text: str) -> None:
        self.assertNotIn(SECRET_USER, text)
        self.assertNotIn(SECRET_PASS, text)
        self.assertNotIn("rip-s3cret", text)


class TestManagerBasics(ResidentialTestCase):
    def test_missing_config(self) -> None:
        mgr = self.rp.ResidentialProxyManager(
            hermes_home=self.home,
            environ={"HERMES_HOME": str(self.home)},
        )
        st = mgr.inspect("bulk", "amiroo", probe=False)
        self.assertEqual(st.proxy, "Missing")
        self.assertEqual(st.status, "OFF")
        self.assertEqual(st.connection, "Disconnected")
        self._assert_no_secrets(st.as_text())
        self._assert_no_secrets(repr(st))
        self._assert_no_secrets(repr(mgr))

    def test_configured_but_off(self) -> None:
        st = self.mgr.inspect("bulk", "amiroo", probe=True)
        self.assertEqual(st.proxy, "Configured")
        self.assertEqual(st.status, "OFF")
        self.assertTrue(st.supported)
        self.assertEqual(st.mode, "Application-level HTTP CONNECT")
        text = st.as_text()
        self.assertIn("Status: ❌ OFF", text)
        self.assertIn("Direct VPS IP: 198.51.100.1", text)
        self.assertIn("Active Trading IP: 198.51.100.1", text)
        self.assertNotIn("Status: ✅ ON", text)
        self.assertNotIn("45.91.169.191", text)
        self.assertNotIn("217.67.78.192", text)

    def test_as_text_on_error_and_unverified_active_ip(self) -> None:
        on = self.mgr.enable("bulk", "amiroo")
        self.assertEqual(on.status, "ON")
        text = on.as_text()
        self.assertIn("Status: ✅ ON", text)
        self.assertIn("Compatibility: VERIFIED", text)
        self.assertIn("Direct VPS IP: 198.51.100.1", text)
        self.assertIn("Residential IP:", text)
        self.assertNotIn("Exit IP:", text)
        self.assertIn("Active Trading IP: Unverified", text)
        on.route_verified = True
        verified = on.as_text()
        rip = on.residential_ip or on.exit_ip
        self.assertIsNotNone(rip)
        self.assertIn(f"Active Trading IP: {rip}", verified)
        err = self.rp.ResidentialStatus(
            exchange="bulk",
            account="amiroo",
            status="ERROR",
            enabled=True,
            activatable=True,
            compatibility="verified",
            direct_vps_ip="198.51.100.1",
            residential_ip=None,
            route_verified=False,
        )
        err_text = err.as_text()
        self.assertIn("Status: ⚠️ ERROR", err_text)
        self.assertIn("Active Trading IP: Unverified", err_text)

    def test_isolation_and_persistence(self) -> None:
        on = self.mgr.enable("bulk", "amiroo")
        self.assertEqual(on.status, "ON")
        self.assertIsNotNone(on.exit_ip)
        self.assertTrue(self.mgr.is_enabled("bulk", "amiroo"))
        self.assertFalse(self.mgr.is_enabled("bulk", "other"))
        self.assertFalse(self.mgr.is_enabled("hyperliquid", "amiroo"))
        path = self.home / "kam" / "residential_ip.json"
        self.assertTrue(path.is_file())
        self.assertEqual(stat_mode := (path.stat().st_mode & 0o777), 0o600, oct(stat_mode))
        raw = path.read_text(encoding="utf-8")
        self._assert_no_secrets(raw)
        self.assertNotIn("RESIDENTIAL_PROXY_PASS", raw)
        other = self.rp.ResidentialProxyManager(
            hermes_home=self.home,
            environ=_env_for(self.proxy, self.home),
        )
        self.assertTrue(other.is_enabled("bulk", "amiroo"))
        self.assertFalse(other.is_enabled("bulk", "other"))
        other.disable("bulk", "amiroo")
        self.assertFalse(other.is_enabled("bulk", "amiroo"))
        third = self.rp.ResidentialProxyManager(
            hermes_home=self.home,
            environ=_env_for(self.proxy, self.home),
        )
        self.assertFalse(third.is_enabled("bulk", "amiroo"))

    def test_unsupported_mt5_cannot_enable(self) -> None:
        st = self.mgr.enable("mt5", "term1")
        self.assertNotEqual(st.status, "ON")
        self.assertFalse(self.mgr.is_enabled("mt5", "term1"))
        self.assertFalse(self.mgr.is_supported("mt5"))
        self.assertFalse(self.mgr.is_supported("mt5linux"))
        self.assertFalse(self.mgr.is_supported("metatrader"))
        self.assertTrue(self.mgr.is_supported("bulk"))
        self.assertTrue(self.mgr.is_supported("hyperliquid"))
        self.assertTrue(self.mgr.is_supported("edgex"))
        self.assertTrue(self.mgr.is_supported("apex"))

    def test_invalid_credentials(self) -> None:
        bad = self.rp.ResidentialProxyManager(
            hermes_home=self.home,
            environ={
                **_env_for(self.proxy, self.home),
                "RESIDENTIAL_PROXY_PASS": "wrong-pass",
            },
        )
        st = bad.enable("bulk", "amiroo")
        self.assertEqual(st.status, "ERROR")
        self.assertFalse(bad.is_enabled("bulk", "amiroo"))
        self._assert_no_secrets(st.as_text())
        self._assert_no_secrets(st.message)

    def test_unavailable_proxy(self) -> None:
        dead = self.rp.ResidentialProxyManager(
            hermes_home=self.home,
            environ={
                **_env_for(self.proxy, self.home),
                "RESIDENTIAL_PROXY_PORT": "1",
            },
        )
        st = dead.enable("bulk", "amiroo")
        self.assertEqual(st.status, "ERROR")
        self.assertFalse(dead.is_enabled("bulk", "amiroo"))

    def test_enable_reports_on_only_after_verified_connect(self) -> None:
        rejecting = _ConnectProxy(reject=True)
        rejecting.start()
        time.sleep(0.05)
        try:
            mgr = self.rp.ResidentialProxyManager(
                hermes_home=self.home,
                environ={
                    **_env_for(self.proxy, self.home),
                    "RESIDENTIAL_PROXY_PORT": str(rejecting.port),
                },
            )
            st = mgr.enable("bulk", "amiroo")
            self.assertEqual(st.status, "ERROR")
            self.assertFalse(mgr.is_enabled("bulk", "amiroo"))
        finally:
            rejecting.stop()


class TestFailClosedTransport(ResidentialTestCase):
    def test_connect_uses_hostname_not_pre_resolved_ip(self) -> None:
        self.mgr.enable("bulk", "amiroo")
        url = f"http://residential-ip.test:{self.origin_port}/"
        real_getaddrinfo = socket.getaddrinfo

        def _gai(host, *args, **kwargs):
            if host in {"residential-ip.test"}:
                raise socket.gaierror("local dns should not run for CONNECT host")
            return real_getaddrinfo(host, *args, **kwargs)

        with self.mgr.bind("bulk", "amiroo"):
            with mock.patch("socket.getaddrinfo", side_effect=_gai):
                try:
                    self.rp.urlopen(url, timeout=5)
                except Exception as exc:  # noqa: BLE001
                    self._assert_no_secrets(str(exc))
        self.assertTrue(
            any(t.startswith("residential-ip.test:") for t in self.proxy.targets),
            self.proxy.targets,
        )

    def test_fail_closed_no_direct_fallback(self) -> None:
        self.mgr.enable("bulk", "amiroo")
        dead = self.rp.ResidentialProxyManager(
            hermes_home=self.home,
            environ={
                **_env_for(self.proxy, self.home),
                "RESIDENTIAL_PROXY_PORT": "1",
            },
        )
        self.rp.set_manager(dead)
        # Persist enabled flag into dead manager's store (same home).
        dead._set_enabled("bulk", "amiroo", True)
        orig_hits = {"n": 0}
        real = self.rp._ORIG_URLOPEN

        def _count(*a, **k):
            orig_hits["n"] += 1
            return real(*a, **k)

        with dead.bind("bulk", "amiroo"):
            with mock.patch.object(self.rp, "_ORIG_URLOPEN", side_effect=_count):
                with self.assertRaises(self.rp.ProxyRouteError) as ctx:
                    self.rp.urlopen("http://example.invalid/", timeout=2)
        self.assertEqual(orig_hits["n"], 0)
        self._assert_no_secrets(str(ctx.exception))

    def test_disabled_uses_direct(self) -> None:
        hits = {"n": 0}

        def _fake_open(req, timeout=None, **kwargs):
            hits["n"] += 1
            class _R:
                def read(self):
                    return b"ok"

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return _R()

        with self.mgr.bind("bulk", "amiroo"):
            with mock.patch.object(self.rp, "_ORIG_URLOPEN", side_effect=_fake_open):
                self.rp.urlopen("http://example.invalid/", timeout=2)
        self.assertEqual(hits["n"], 1)

    def test_tradedesk_bind_fail_closed(self) -> None:
        from plugins.trade.canonical import CanonicalResponse
        from plugins.trade.tradedesk import TradeDesk

        class _Agent:
            name = "bulk"

            def list_accounts(self):
                return ["amiroo"]

            def capabilities(self):
                return []

            def execute(self, request):
                urllib.request.urlopen("http://example.invalid/", timeout=2)
                return CanonicalResponse(success=True, operation="balance", exchange="bulk", account="amiroo")

        self.mgr.enable("bulk", "amiroo")
        dead = self.rp.ResidentialProxyManager(
            hermes_home=self.home,
            environ={
                **_env_for(self.proxy, self.home),
                "RESIDENTIAL_PROXY_PORT": "1",
            },
        )
        dead._set_enabled("bulk", "amiroo", True)
        self.rp.set_manager(dead)
        desk = TradeDesk()
        desk._loaded = True
        desk._agents = {"bulk": _Agent()}
        orig_hits = {"n": 0}

        def _count(*a, **k):
            orig_hits["n"] += 1
            raise AssertionError("direct fallback")

        with mock.patch.object(self.rp, "_ORIG_URLOPEN", side_effect=_count):
            resp = desk.execute({"operation": "balance", "exchange": "bulk", "account": "amiroo"})
        self.assertFalse(resp.success)
        self.assertEqual(orig_hits["n"], 0)
        self._assert_no_secrets(str(resp.error.message if resp.error else resp))

    def test_websocket_kwargs_injected(self) -> None:
        self.mgr.enable("qfex", "amiroo")
        captured: Dict[str, Any] = {}

        def _fake_cc(url, **kwargs):
            captured.update(kwargs)
            captured["url"] = url
            return mock.Mock()

        fake_ws = mock.Mock()
        fake_ws.create_connection = _fake_cc
        with self.mgr.bind("qfex", "amiroo"):
            self.rp._wrap_websocket_module(fake_ws)
            fake_ws.create_connection("wss://example.test/ws", timeout=5)
        self.assertEqual(captured.get("http_proxy_host"), "127.0.0.1")
        self.assertEqual(int(captured.get("http_proxy_port")), self.proxy.port)
        auth = captured.get("http_proxy_auth")
        self.assertEqual(auth, (SECRET_USER, SECRET_PASS))

    def test_requests_uses_proxy_and_trust_env_false(self) -> None:
        self.mgr.enable("arcus", "amiroo")
        session = mock.Mock()
        with self.mgr.bind("arcus", "amiroo"):
            kwargs = self.rp.apply_requests_kwargs(session, {"timeout": 5})
        self.assertIs(session.trust_env, False)
        proxies = kwargs.get("proxies") or {}
        self.assertIn("https", proxies)
        self.assertIn("http", proxies)

    def test_concurrency_does_not_corrupt_state(self) -> None:
        errors: List[str] = []

        def _one(alias: str) -> None:
            try:
                self.mgr.enable("bulk", alias)
                self.mgr.disable("bulk", alias)
                self.mgr.enable("bulk", alias)
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__)

        threads = [
            threading.Thread(target=_one, args=("amiroo",)),
            threading.Thread(target=_one, args=("other",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(errors, [])
        self.assertTrue(self.mgr.is_enabled("bulk", "amiroo"))
        self.assertTrue(self.mgr.is_enabled("bulk", "other"))


class TestWizardResidentialMenu(ResidentialTestCase):
    def _open_action(self, exchange: str = "bulk", account: str = "amiroo"):
        from plugins.trade.wizard import TradeWizard

        wiz = TradeWizard(tradedesk=_FakeDesk())
        key = ("chat-rip",)
        wiz.open(key)
        wiz.handle_callback(key, f"exchange:{exchange}")
        screen = wiz.handle_callback(key, f"account:{account}")
        return wiz, key, screen

    def test_button_above_back_exit_and_existing_actions_preserved(self) -> None:
        wiz, key, screen = self._open_action()
        self.assertEqual(screen.state, "action")
        labels = [b["text"] for row in screen.buttons for b in row]
        self.assertIn("🌐 Residential IP", labels)
        self.assertEqual(labels[-3], "🌐 Residential IP")
        self.assertEqual(labels[-2], "◀️ Back")
        self.assertEqual(labels[-1], "✕ Exit")
        action_cbs = [
            b["callback_data"]
            for row in screen.buttons
            for b in row
            if b["callback_data"].startswith("action:")
        ]
        self.assertEqual(len(action_cbs), 6)
        self.assertEqual(screen.buttons[-2], [{"text": "🌐 Residential IP", "callback_data": "rip"}])

    def test_submenu_and_navigation(self) -> None:
        wiz, key, _screen = self._open_action()
        sub = wiz.handle_callback(key, "rip")
        self.assertEqual(sub.state, "residential_ip")
        self.assertIn("🌐 Residential IP", sub.text)
        self.assertIn("Exchange: bulk", sub.text)
        self.assertIn("Account: amiroo", sub.text)
        self.assertIn("Mode: Application-level HTTP CONNECT", sub.text)
        self.assertIn("Status: ❌ OFF", sub.text)
        self.assertIn("Proxy: Configured", sub.text)
        self._assert_no_secrets(sub.text)
        labels = [b["text"] for row in sub.buttons for b in row]
        self.assertEqual(
            labels,
            [
                "🟢 Enable Residential IP",
                "🔴 Disable Residential IP",
                "🔄 Refresh Status",
                "◀️ Back",
            ],
        )
        for row in sub.buttons:
            for b in row:
                self.assertLessEqual(len(f"trade:{b['callback_data']}".encode("utf-8")), 64)
        back = wiz.handle_callback(key, "back")
        self.assertEqual(back.state, "action")
        self.assertIn("Exchange: bulk", back.text)
        closed = wiz.handle_callback(key, "exit")
        self.assertEqual(closed.state, "closed")

    def test_enable_disable_refresh(self) -> None:
        wiz, key, _screen = self._open_action()
        wiz.handle_callback(key, "rip")
        on = wiz.handle_callback(key, "rip:on")
        self.assertIn("Status: ✅ ON", on.text)
        self.assertIn("Connection: Connected", on.text)
        self.assertIn("Residential IP:", on.text)
        self.assertNotIn("Exit IP:", on.text)
        self.assertIn("Active Trading IP: Unverified", on.text)
        self._assert_no_secrets(on.text)
        off = wiz.handle_callback(key, "rip:off")
        self.assertIn("Status: ❌ OFF", off.text)
        wiz.handle_callback(key, "rip:on")
        refreshed = wiz.handle_callback(key, "rip:ref")
        self.assertIn("Status: ✅ ON", refreshed.text)

    def test_existing_trade_action_still_dispatched_not_executed_here(self) -> None:
        from plugins.trade.canonical import make_success
        from plugins.trade.wizard import TradeWizard

        class Desk(_FakeDesk):
            def __init__(self) -> None:
                self.requests: List[Dict[str, Any]] = []

            def execute(self, request):
                self.requests.append(dict(request))
                from plugins.trade.canonical import normalize_balance

                return make_success(
                    operation="balance",
                    exchange="bulk",
                    account="amiroo",
                    balance=normalize_balance("1.00", "USDC"),
                )

        desk = Desk()
        wiz = TradeWizard(tradedesk=desk)
        key = ("chat-bal",)
        wiz.open(key)
        wiz.handle_callback(key, "exchange:bulk")
        wiz.handle_callback(key, "account:amiroo")
        screen = wiz.handle_callback(key, "action:balance")
        self.assertEqual(screen.state, "balance")
        self.assertEqual(desk.requests[-1]["operation"], "balance")

    def test_mt5_enable_stays_off(self) -> None:
        wiz, key, _screen = self._open_action("mt5", "term1")
        wiz.handle_callback(key, "rip")
        st = wiz.handle_callback(key, "rip:on")
        self.assertNotIn("Status: ✅ ON", st.text)
        self.assertIn("Status: ❌ OFF", st.text)
        self.assertIn("Compatibility: UNSUPPORTED", st.text)
        self.assertFalse(self.mgr.is_enabled("mt5", "term1"))


class TestHttpsAndIsolation(ResidentialTestCase):
    def _tls_files(self) -> Tuple[Path, Path]:
        cert = self.home / "tls-cert.pem"
        key = self.home / "tls-key.pem"
        if not cert.exists():
            subprocess.check_call(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-keyout",
                    str(key),
                    "-out",
                    str(cert),
                    "-days",
                    "1",
                    "-nodes",
                    "-subj",
                    "/CN=residential-ip.test",
                    "-addext",
                    "subjectAltName=DNS:residential-ip.test",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return cert, key

    def _start_tls_http_origin(self) -> Tuple[ThreadingHTTPServer, int, ssl.SSLContext]:
        cert, key = self._tls_files()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _IpHandler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_ctx.check_hostname = False
        client_ctx.verify_mode = ssl.CERT_NONE
        return httpd, int(httpd.server_address[1]), client_ctx

    def test_https_rest_through_connect_urllib(self) -> None:
        self.mgr.enable("bulk", "amiroo")
        httpd, port, client_ctx = self._start_tls_http_origin()
        self.proxy.backend = ("127.0.0.1", port)
        url = f"https://residential-ip.test:{port}/"
        try:
            with self.mgr.bind("bulk", "amiroo"):
                with self.rp.urlopen(url, timeout=8, context=client_ctx) as resp:
                    body = resp.read().decode("ascii", "replace")
            self.assertIn("203.0.113.9", body)
            self.assertTrue(
                any(t.startswith("residential-ip.test:") for t in self.proxy.targets),
                self.proxy.targets,
            )
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_https_rest_through_connect_requests(self) -> None:
        import requests

        self.mgr.enable("arcus", "amiroo")
        httpd, port, client_ctx = self._start_tls_http_origin()
        self.proxy.backend = ("127.0.0.1", port)
        url = f"https://residential-ip.test:{port}/"
        try:
            with self.mgr.bind("arcus", "amiroo"):
                session = requests.Session()
                session.verify = False
                kwargs = self.rp.apply_requests_kwargs(session, {"timeout": 8, "verify": False})
                resp = self.rp._ORIG_SESSION_REQUEST(session, "GET", url, **kwargs)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("203.0.113.9", resp.text)
            self.assertTrue(
                any(t.startswith("residential-ip.test:") for t in self.proxy.targets),
                self.proxy.targets,
            )
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_wss_through_connect(self) -> None:
        try:
            import websocket
        except ImportError:
            self.skipTest("websocket-client missing")
        cert, key = self._tls_files()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        port = int(srv.getsockname()[1])
        ready = threading.Event()
        errors: List[str] = []

        def _serve() -> None:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(cert), str(key))
            try:
                client, _addr = srv.accept()
                tls = ctx.wrap_socket(client, server_side=True)
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = tls.recv(1024)
                    if not chunk:
                        break
                    buf += chunk
                key_hdr = ""
                for line in buf.decode("latin1", "replace").split("\r\n"):
                    if line.lower().startswith("sec-websocket-key:"):
                        key_hdr = line.split(":", 1)[1].strip()
                guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                accept = base64.b64encode(
                    hashlib.sha1((key_hdr + guid).encode("ascii")).digest()
                ).decode("ascii")
                tls.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n"
                        "\r\n"
                    ).encode("ascii")
                )
                ready.set()
                time.sleep(0.4)
                tls.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__)
            finally:
                try:
                    srv.close()
                except OSError:
                    pass

        threading.Thread(target=_serve, daemon=True).start()
        self.mgr.enable("qfex", "amiroo")
        self.proxy.backend = ("127.0.0.1", port)
        url = f"wss://residential-ip.test:{port}/ws"
        try:
            with self.mgr.bind("qfex", "amiroo"):
                ws = websocket.create_connection(
                    url,
                    timeout=8,
                    sslopt={"cert_reqs": ssl.CERT_NONE, "check_hostname": False},
                )
                try:
                    ws.close()
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            self._assert_no_secrets(str(exc))
            if not any(t.startswith("residential-ip.test:") for t in self.proxy.targets):
                raise
        self.assertTrue(
            any(t.startswith("residential-ip.test:") for t in self.proxy.targets),
            self.proxy.targets,
        )
        self._assert_no_secrets(" ".join(errors))

    def test_unrelated_account_and_unbound_stay_direct(self) -> None:
        self.mgr.enable("bulk", "amiroo")
        hits = {"n": 0}

        class _R:
            def read(self):
                return b"direct"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_open(*a, **k):
            hits["n"] += 1
            return _R()

        with mock.patch.object(self.rp, "_ORIG_URLOPEN", side_effect=_fake_open):
            with self.mgr.bind("bulk", "other"):
                self.rp.urlopen("http://example.invalid/", timeout=2)
            self.assertEqual(hits["n"], 1)
            hits["n"] = 0
            self.rp.urlopen("http://example.invalid/", timeout=2)
            self.assertEqual(hits["n"], 1)
            hits["n"] = 0
            before = list(self.proxy.targets)
            with self.mgr.bind("bulk", "amiroo"):
                try:
                    self.rp.urlopen("http://example.invalid/", timeout=2)
                except Exception as exc:  # noqa: BLE001
                    self._assert_no_secrets(str(exc))
            self.assertEqual(hits["n"], 0)
            self.assertGreater(len(self.proxy.targets), len(before))

    def test_requests_fail_closed_dead_proxy(self) -> None:
        self.mgr.enable("arcus", "amiroo")
        dead = self.rp.ResidentialProxyManager(
            hermes_home=self.home,
            environ={
                **_env_for(self.proxy, self.home),
                "RESIDENTIAL_PROXY_PORT": "1",
            },
        )
        dead._set_enabled("arcus", "amiroo", True)
        self.rp.set_manager(dead)
        with dead.bind("arcus", "amiroo"):
            with self.assertRaises(self.rp.ProxyRouteError):
                import requests

                requests.Session().request("GET", "https://example.invalid/", timeout=2)

    def test_connect_authorization_is_http_basic(self) -> None:
        """Document: CONNECT auth is HTTP Basic to the proxy TCP port (not TLS)."""
        self.mgr.enable("bulk", "amiroo")
        with self.mgr.bind("bulk", "amiroo"):
            try:
                self.rp.urlopen("http://residential-ip.test/", timeout=3)
            except Exception as exc:  # noqa: BLE001
                self._assert_no_secrets(str(exc))
        self.assertGreaterEqual(self.proxy.auth_attempts, 1)

    def test_aiohttp_kwargs_inject_proxy(self) -> None:
        self.mgr.enable("edgex", "amiroo")
        with self.mgr.bind("edgex", "amiroo"):
            kw = self.rp.apply_aiohttp_kwargs({})
        self.assertIn("proxy", kw)
        self.assertIn("127.0.0.1", kw["proxy"])
        self.assertNotIn(self.proxy.password, kw.get("proxy", ""))

    def test_unverified_agent_cannot_enable(self) -> None:
        self.rp.note_agent_capability("futurex", capability="unverified")
        self.assertFalse(self.mgr.is_supported("futurex"))
        st = self.mgr.enable("futurex", "acct")
        self.assertEqual(st.status, self.rp.STATUS_PENDING)
        self.assertFalse(self.mgr.is_enabled("futurex", "acct"))

    def test_new_agent_gets_menu_without_wizard_change(self) -> None:
        tmp = self.home / "future_agents"
        tmp.mkdir()
        (tmp / "x_futurex_agent.py").write_text(
            "name = \"futurex\"\n"
            "def list_accounts():\n"
            "    return [\"acct\"]\n"
            "def capabilities():\n"
            "    return [\"balance\"]\n"
            "def execute(payload):\n"
            "    return {\"success\": True, \"data\": {}}\n",
            encoding="utf-8",
        )
        from plugins.trade import tradedesk as td

        with mock.patch.object(td, "_agents_dir", return_value=tmp):
            desk = td.TradeDesk()
            names = desk.list_exchanges()
        self.assertIn("futurex", names)
        self.assertEqual(self.rp.capability_for("futurex"), "unverified")
        from plugins.trade.wizard import TradeWizard

        class _FutureDesk:
            def list_exchanges(self) -> List[str]:
                return ["futurex"]

            def list_accounts(self, exchange: str) -> List[str]:
                return ["acct"]

            def execute(self, request: Dict[str, Any]) -> Any:
                raise AssertionError("trading execute must not run")

        wiz = TradeWizard(tradedesk=_FutureDesk())
        key = ("chat-future",)
        wiz.open(key)
        wiz.handle_callback(key, "exchange:futurex")
        screen = wiz.handle_callback(key, "account:acct")
        labels = [b["text"] for row in screen.buttons for b in row]
        self.assertIn("🌐 Residential IP", labels)
        screen = wiz.handle_callback(key, "rip")
        self.assertIn("Status: ❌ OFF", screen.text)
        self.assertIn("Compatibility: UNVERIFIED", screen.text)
        screen = wiz.handle_callback(key, "rip:on")
        self.assertNotIn("Status: ✅ ON", screen.text)
        self.assertFalse(self.mgr.is_enabled("futurex", "acct"))


class TestRouteVerification(ResidentialTestCase):
    def _exchange_get(self) -> None:
        url = f"http://127.0.0.1:{self.origin_port}/"
        with self.mgr.bind("bulk", "amiroo"):
            urllib.request.urlopen(url, timeout=2).read()

    def test_probe_alone_does_not_verify(self) -> None:
        self.mgr.enable("bulk", "amiroo")
        st = self.mgr.inspect("bulk", "amiroo", probe=True)
        self.assertEqual(st.status, "ON")
        self.assertFalse(st.route_verified)
        self.assertIn("Active Trading IP: Unverified", st.as_text())
        self.assertIn("Route Verification: ⚠️ Unverified", st.as_text())
        self.assertNotIn("Last Verified:", st.as_text())

    def test_proxied_exchange_request_verifies_account(self) -> None:
        self.mgr.enable("bulk", "amiroo")
        self._exchange_get()
        st = self.mgr.inspect("bulk", "amiroo", probe=True)
        self.assertTrue(st.route_verified)
        text = st.as_text()
        self.assertIn("Route Verification: ✅ Verified", text)
        self.assertIn("Last Verified:", text)
        self.assertIn("Active Trading IP: 203.0.113.9", text)
        self.mgr.enable("bulk", "other")
        other = self.mgr.inspect("bulk", "other", probe=True)
        self.assertFalse(other.route_verified)
        self.assertIn("Active Trading IP: Unverified", other.as_text())

    def test_disable_and_reenable_clears_verification(self) -> None:
        self.mgr.enable("bulk", "amiroo")
        self._exchange_get()
        self.assertTrue(self.mgr.inspect("bulk", "amiroo", probe=False).route_verified)
        self.mgr.disable("bulk", "amiroo")
        self.mgr.enable("bulk", "amiroo")
        st = self.mgr.inspect("bulk", "amiroo", probe=True)
        self.assertFalse(st.route_verified)
        self.assertIn("Active Trading IP: Unverified", st.as_text())

    def test_config_change_invalidates_verification(self) -> None:
        self.mgr.enable("bulk", "amiroo")
        self._exchange_get()
        other = self.rp.ResidentialProxyManager(
            hermes_home=self.home,
            environ={
                **_env_for(self.proxy, self.home),
                "RESIDENTIAL_PROXY_HOST": "127.0.0.2",
            },
            ip_probe_url=f"http://127.0.0.1:{self.origin_port}/",
            direct_ip="198.51.100.1",
        )
        other._set_enabled("bulk", "amiroo", True)
        st = other.inspect("bulk", "amiroo", probe=False)
        self.assertFalse(st.route_verified)

    def test_stale_verification_expires(self) -> None:
        self.mgr._verification_ttl_seconds = 1
        self.mgr.enable("bulk", "amiroo")
        self._exchange_get()
        rec = self.mgr._verified[self.rp._pair_key("bulk", "amiroo")]
        rec["at"] = rec["at"] - 10
        self.mgr._write_state()
        st = self.mgr.inspect("bulk", "amiroo", probe=False)
        self.assertFalse(st.route_verified)

    def test_concurrent_verify_is_atomic(self) -> None:
        self.mgr.enable("bulk", "amiroo")
        self.mgr.enable("bulk", "other")
        errors: List[str] = []

        def _one(account: str) -> None:
            try:
                url = f"http://127.0.0.1:{self.origin_port}/"
                with self.mgr.bind("bulk", account):
                    urllib.request.urlopen(url, timeout=2).read()
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__)

        t1 = threading.Thread(target=_one, args=("amiroo",))
        t2 = threading.Thread(target=_one, args=("other",))
        t1.start()
        t2.start()
        t1.join(10)
        t2.join(10)
        self.assertEqual(errors, [])
        self.assertTrue(self.mgr.inspect("bulk", "amiroo", probe=False).route_verified)
        self.assertTrue(self.mgr.inspect("bulk", "other", probe=False).route_verified)
        raw = (self.home / "kam" / "residential_ip.json").read_text(encoding="utf-8")
        self.assertIn("amiroo", raw)
        self.assertIn("other", raw)
        self._assert_no_secrets(raw)


class TestWizardSourceInvariants(unittest.TestCase):
    def test_wizard_source_has_no_os_environ_or_proxy_secrets(self) -> None:
        from plugins.trade import wizard

        text = Path(wizard.__file__).read_text(encoding="utf-8")
        self.assertNotIn("os.environ", text)
        self.assertNotIn("os.getenv", text)
        self.assertNotIn("RESIDENTIAL_PROXY_PASS", text)
        self.assertNotIn("RESIDENTIAL_PROXY_USER", text)


if __name__ == "__main__":
    unittest.main()
