"""WebBacktest FastAPI app — start-to-live replay over the shared GoldenFibo core."""

from __future__ import annotations

import html
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

_KAM_ROOT = Path("/root/kam")
_GF_ROOT = _KAM_ROOT / "GoldenFibo"
for p in (_GF_ROOT, _KAM_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from goldenfibo.engine.config import Side
from goldenfibo.session.controller import get_session
from goldenfibo.session.types import SessionMode

from .auth import LoginRateLimiter, SessionManager
from .config import WebBacktestConfig, WebBacktestConfigError, load_config

logger = logging.getLogger("webbacktest")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    config: Optional[WebBacktestConfig] = None,
) -> FastAPI:
    try:
        cfg = config or load_config()
    except WebBacktestConfigError:
        app = FastAPI(title="webbacktest", version="0.1.0")

        @app.get("/")
        async def locked_root() -> HTMLResponse:
            return HTMLResponse(
                "<!doctype html><html><body style='font-family:sans-serif;background:#0b0e11;color:#eee;padding:40px'>"
                "<h1>webbacktest unavailable</h1>"
                "<p>WEB_PASSWORD is not configured. Refusing to start unprotected.</p>"
                "</body></html>",
                status_code=503,
            )

        @app.api_route("/{full_path:path}", methods=["GET", "POST", "WS"])
        async def locked_all(full_path: str) -> HTMLResponse:
            return await locked_root()

        return app

    sessions = SessionManager(cfg)
    limiter = LoginRateLimiter(cfg.login_max_failures, cfg.login_lockout_seconds)
    app = FastAPI(title="webbacktest", version="0.1.0")
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def _client_key(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for") or ""
        if fwd:
            return fwd.split(",")[0].strip()
        if request.client:
            return request.client.host or "unknown"
        return "unknown"

    def _session_token(request: Request) -> Optional[str]:
        raw = request.cookies.get(cfg.cookie_name)
        if not raw:
            return None
        return raw.strip().strip('"')

    def _authenticated(request: Request) -> bool:
        return sessions.verify(_session_token(request))

    def _set_session_cookies(resp: Response, token: str, csrf: str) -> None:
        resp.set_cookie(
            key=cfg.cookie_name,
            value=token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=cfg.session_max_age_seconds,
            path="/",
        )
        resp.set_cookie(
            key="webbacktest_csrf",
            value=csrf,
            httponly=False,
            samesite="lax",
            secure=False,
            max_age=cfg.session_max_age_seconds,
            path="/",
        )

    def _require_auth(request: Request) -> Optional[JSONResponse]:
        if _authenticated(request):
            return None
        return JSONResponse(
            {"success": False, "error": {"code": "UNAUTHORIZED", "message": "Login required."}},
            status_code=401,
        )

    def _require_csrf(request: Request) -> Optional[JSONResponse]:
        denied = _require_auth(request)
        if denied:
            return denied
        token = _session_token(request)
        provided = request.headers.get("x-csrf-token") or request.headers.get("X-CSRF-Token")
        if not sessions.csrf_of(token):
            return JSONResponse(
                {
                    "success": False,
                    "error": {
                        "code": "CSRF_SESSION_STALE",
                        "message": "Session is missing CSRF binding. Please log in again.",
                    },
                },
                status_code=403,
            )
        if not sessions.csrf_ok(token, provided):
            return JSONResponse(
                {"success": False, "error": {"code": "CSRF_FAILED", "message": "Invalid or missing CSRF token."}},
                status_code=403,
            )
        return None

    def _login_page(error: str = "") -> HTMLResponse:
        err_html = f"<p class='error'>{html.escape(error)}</p>" if error else ""
        body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>webbacktest Login</title>
  <link rel="stylesheet" href="/static/style.css" />
</head>
<body class="login-body">
  <form class="login-card" method="post" action="/login" autocomplete="current-password">
    <h1>webbacktest</h1>
    {err_html}
    <label>Password
      <input type="password" name="password" required autofocus />
    </label>
    <p class="hint">Hint: {html.escape(cfg.hint)}</p>
    <button type="submit">Login</button>
  </form>
</body>
</html>"""
        return HTMLResponse(body)

    def _app_page() -> HTMLResponse:
        path = STATIC_DIR / "index.html"
        if path.is_file():
            html_body = path.read_text(encoding="utf-8")
            ver = str(int((STATIC_DIR / "app.js").stat().st_mtime) if (STATIC_DIR / "app.js").is_file() else 1)
            html_body = html_body.replace("/static/app.js", f"/static/app.js?v={ver}")
            html_body = html_body.replace("/static/style.css", f"/static/style.css?v={ver}")
            resp = HTMLResponse(html_body)
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            return resp
        return HTMLResponse("<h1>webbacktest</h1><p>Static UI missing.</p>", status_code=500)

    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request) -> Response:
        if not _authenticated(request):
            return _login_page()
        return _app_page()

    @app.get("/login", response_class=HTMLResponse)
    async def login_get(request: Request) -> Response:
        if _authenticated(request):
            return RedirectResponse("/", status_code=303)
        return _login_page()

    @app.post("/login")
    async def login_post(request: Request) -> Response:
        key = _client_key(request)
        gate = limiter.check(key)
        if not gate.allowed:
            return _login_page(gate.message)
        body = await request.body()
        password = ""
        try:
            from urllib.parse import parse_qs
            parsed = parse_qs(body.decode("utf-8", errors="ignore"), keep_blank_values=True)
            password = (parsed.get("password") or [""])[0]
        except Exception:
            password = ""
        if not sessions.password_ok(password):
            limiter.record_failure(key)
            logger.info("webbacktest login failure from %s", key)
            return _login_page("Invalid password.")
        limiter.record_success(key)
        token, csrf = sessions.issue()
        resp = RedirectResponse("/", status_code=303)
        _set_session_cookies(resp, token, csrf)
        return resp

    @app.post("/logout")
    async def logout() -> Response:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(cfg.cookie_name, path="/")
        resp.delete_cookie("webbacktest_csrf", path="/")
        return resp

    @app.get("/api/health")
    async def health() -> dict:
        s = get_session()
        return {"ok": True, "service": "webbacktest", "phase": s.phase.value, "mode": s.mode.value}

    @app.get("/api/session")
    async def api_session(request: Request) -> Response:
        denied = _require_auth(request)
        if denied:
            return denied
        token = _session_token(request)
        csrf = sessions.csrf_of(token)
        if not csrf:
            token, csrf = sessions.issue()
            body = {"success": True, "authenticated": True, "csrf": csrf, "rotated": True}
            resp = JSONResponse(body)
            _set_session_cookies(resp, token, csrf)
            return resp
        return JSONResponse({"success": True, "authenticated": True, "csrf": csrf, "rotated": False})

    @app.get("/api/state")
    async def api_state(request: Request) -> Response:
        denied = _require_auth(request)
        if denied:
            return denied
        return JSONResponse(get_session().snapshot_dict())

    @app.post("/api/session/start")
    async def api_session_start(request: Request, body: Optional[dict] = Body(None)) -> JSONResponse:
        denied = _require_csrf(request)
        if denied:
            return denied
        b = body or {}
        mode_s = str(b.get("mode") or "REPLAY_TO_LIVE").upper().replace("-", "_")
        if mode_s == "REPLAY":
            mode_s = "REPLAY_TO_LIVE"
        mode = SessionMode(mode_s)
        side = b.get("side")
        pct = b.get("percentage")
        try:
            sess = get_session()
            if b.get("cache_policy"):
                from goldenfibo.marketdata.kline_cache import CachePolicy
                try:
                    sess.cache_policy = CachePolicy(str(b.get("cache_policy")).upper())
                    sess.kline_source.policy = sess.cache_policy
                except Exception:
                    pass
            snap = await sess.start_run(
                mode=mode,
                symbol=b.get("symbol"),
                timeframe=b.get("timeframe"),
                market=b.get("market"),
                side=None if side is None else Side(str(side).upper()),
                percentage=None if pct is None else __import__("decimal").Decimal(str(pct)),
                start_time=b.get("start_time"),
                end_time=b.get("end_time"),
            )
        except Exception as exc:
            msg = str(exc)
            if "HTTP Error 400" in msg and b.get("market") in {"spot", "SPOT"}:
                sym = b.get("symbol") or "this symbol"
                msg = f"Market data unavailable for {sym} on Binance Spot."
            elif "HTTP Error 400" in msg and b.get("market") in {"futures", "FUTURES"}:
                sym = b.get("symbol") or "this symbol"
                msg = f"Market data unavailable for {sym} on Binance Futures."
            return JSONResponse({"ok": False, "error": msg}, status_code=400)
        return JSONResponse(snap)

    @app.post("/api/session/stop")
    async def api_session_stop(request: Request) -> JSONResponse:
        denied = _require_csrf(request)
        if denied:
            return denied
        await get_session().stop()
        return JSONResponse(get_session().snapshot_dict())

    @app.get("/api/cache/stats")
    async def api_cache_stats(request: Request, symbol: str = "BTCUSDT", timeframe: str = "1m") -> JSONResponse:
        denied = _require_auth(request)
        if denied:
            return denied
        s = get_session()
        return JSONResponse(s.kline_cache.stats_for(symbol, timeframe, market=s.market))

    @app.post("/api/cache/clear")
    async def api_cache_clear(request: Request, body: Optional[dict] = Body(None)) -> JSONResponse:
        denied = _require_csrf(request)
        if denied:
            return denied
        b = body or {}
        s = get_session()
        n = s.kline_cache.clear(str(b.get("symbol") or s.symbol), str(b.get("timeframe") or s.timeframe), market=str(b.get("market") or s.market))
        return JSONResponse({"cleared": n, "symbol": b.get("symbol") or s.symbol, "timeframe": b.get("timeframe") or s.timeframe})

    @app.post("/api/cache/validate")
    async def api_cache_validate(request: Request, body: Optional[dict] = Body(None)) -> JSONResponse:
        denied = _require_auth(request)
        if denied:
            return denied
        from goldenfibo.marketdata.kline_cache import validate_klines_sequence
        b = body or {}
        s = get_session()
        symbol = str(b.get("symbol") or s.symbol)
        tf = str(b.get("timeframe") or s.timeframe)
        start_ms = b.get("start_ms")
        end_ms = b.get("end_ms")
        if start_ms is None or end_ms is None:
            st = s.kline_cache.stats_for(symbol, tf)
            start_ms = st.get("first_open_time") or 0
            end_ms = (st.get("last_open_time") or 0) + 1
        rows = s.kline_cache.read_range(symbol, tf, int(start_ms), int(end_ms), market=str(b.get("market") or s.market))
        return JSONResponse({"stats": s.kline_cache.stats_for(symbol, tf, market=str(b.get("market") or s.market)), "validation": validate_klines_sequence(rows, timeframe=tf)})

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        token = ws.cookies.get(cfg.cookie_name) if hasattr(ws, "cookies") else None
        if not sessions.verify(token):
            await ws.close(code=4401)
            return
        await ws.accept()
        session = get_session()
        session.register_client(ws)
        try:
            await ws.send_json(session.snapshot_dict())
            while True:
                raw = await ws.receive_json()
                if not isinstance(raw, dict):
                    continue
                op = raw.get("op")
                if op in ("start", "reconfigure"):
                    mode_s = str(raw.get("mode") or ("REPLAY_TO_LIVE" if op == "start" else "LIVE")).upper().replace("-", "_")
                    if mode_s == "REPLAY":
                        mode_s = "REPLAY_TO_LIVE"
                    try:
                        mode = SessionMode(mode_s)
                        side = raw.get("side")
                        pct = raw.get("percentage")
                        await session.start_run(
                            mode=mode,
                            symbol=raw.get("symbol"),
                            timeframe=raw.get("timeframe"),
                            market=raw.get("market"),
                            side=None if side is None else Side(str(side).upper()),
                            percentage=None if pct is None else __import__("decimal").Decimal(str(pct)),
                            start_time=raw.get("start_time"),
                            end_time=raw.get("end_time"),
                        )
                        await ws.send_json(session.snapshot_dict())
                    except Exception as exc:
                        await ws.send_json({"v": 1, "type": "error", "error": str(exc)})
                elif op == "stop":
                    await session.stop()
                    await ws.send_json(session.snapshot_dict())
                elif op == "ping":
                    await ws.send_json({"v": 1, "type": "pong"})
                elif op == "snapshot":
                    await ws.send_json(session.snapshot_dict())
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.info("ws closed: %s", exc)
        finally:
            session.unregister_client(ws)

    return app


app = create_app()
app.get_session = get_session  # type: ignore[attr-defined]
app.STATIC_DIR = STATIC_DIR  # type: ignore[attr-defined]


def main() -> None:
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        "plugins.trade.webbacktest.app:app",
        host=cfg.host,
        port=cfg.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
