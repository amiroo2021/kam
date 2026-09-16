"""TradeMenu FastAPI application — discovery + chart + positions/orders + writes."""

from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .auth import LoginRateLimiter, SessionManager
from .config import TradeMenuConfig, TradeMenuConfigError, load_config
from .marketdata import SUPPORTED_TFS, fetch_candles
from .service import TradeMenuService

logger = logging.getLogger("trademenu")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    config: Optional[TradeMenuConfig] = None,
    service: Optional[TradeMenuService] = None,
) -> FastAPI:
    try:
        cfg = config or load_config()
    except TradeMenuConfigError:
        # Fail-closed app: only health-style error page, no data APIs.
        app = FastAPI(title="TradeMenu", version="0.2.0")

        @app.get("/")
        async def locked_root() -> HTMLResponse:
            return HTMLResponse(
                "<!doctype html><html><body style='font-family:sans-serif;background:#0b0e11;color:#eee;padding:40px'>"
                "<h1>TradeMenu unavailable</h1>"
                "<p>TRADE_WEB_PASSWORD is not configured. Refusing to start unprotected.</p>"
                "</body></html>",
                status_code=503,
            )

        @app.api_route("/{full_path:path}", methods=["GET", "POST"])
        async def locked_all(full_path: str) -> HTMLResponse:
            return await locked_root()

        return app

    sessions = SessionManager(cfg)
    limiter = LoginRateLimiter(cfg.login_max_failures, cfg.login_lockout_seconds)
    svc = service or TradeMenuService()

    app = FastAPI(title="TradeMenu", version="0.2.0")
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
        return request.cookies.get(cfg.cookie_name)

    def _authenticated(request: Request) -> bool:
        return sessions.verify(_session_token(request))

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
  <title>TradeMenu Login</title>
  <link rel="stylesheet" href="/static/style.css" />
</head>
<body class="login-body">
  <form class="login-card" method="post" action="/login" autocomplete="current-password">
    <h1>TradeMenu</h1>
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
            return HTMLResponse(path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>TradeMenu</h1><p>Static UI missing.</p>", status_code=500)

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
    async def login_post(request: Request, password: str = Form(...)) -> Response:
        key = _client_key(request)
        gate = limiter.check(key)
        if not gate.allowed:
            return _login_page(gate.message)
        if not sessions.password_ok(password):
            limiter.record_failure(key)
            logger.info("TradeMenu login failure from %s", key)
            return _login_page("Invalid password.")
        limiter.record_success(key)
        token, csrf = sessions.issue()
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            key=cfg.cookie_name,
            value=token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=cfg.session_max_age_seconds,
            path="/",
        )
        # Readable by JS for double-submit CSRF header (not HttpOnly).
        resp.set_cookie(
            key="trademenu_csrf",
            value=csrf,
            httponly=False,
            samesite="lax",
            secure=False,
            max_age=cfg.session_max_age_seconds,
            path="/",
        )
        return resp

    @app.post("/logout")
    async def logout() -> Response:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(cfg.cookie_name, path="/")
        resp.delete_cookie("trademenu_csrf", path="/")
        return resp

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "service": "trademenu", "phase": 2}

    @app.get("/api/session")
    async def api_session(request: Request) -> JSONResponse:
        denied = _require_auth(request)
        if denied:
            return denied
        csrf = sessions.csrf_of(_session_token(request))
        return JSONResponse({"success": True, "authenticated": True, "csrf": csrf})

    @app.get("/api/exchanges")
    async def api_exchanges(request: Request) -> JSONResponse:
        denied = _require_auth(request)
        if denied:
            return denied
        return JSONResponse({"success": True, "exchanges": svc.list_exchanges()})

    @app.get("/api/accounts")
    async def api_accounts(request: Request, exchange: str = Query(...)) -> JSONResponse:
        denied = _require_auth(request)
        if denied:
            return denied
        accounts = svc.list_accounts(exchange)
        return JSONResponse({"success": True, "exchange": exchange, "accounts": accounts})

    @app.get("/api/instruments/resolve")
    async def api_resolve(
        request: Request,
        exchange: str = Query(...),
        account: str = Query(...),
        symbol: str = Query(...),
    ) -> JSONResponse:
        denied = _require_auth(request)
        if denied:
            return denied
        return JSONResponse(svc.resolve_instrument(exchange, account, symbol))

    @app.get("/api/quote")
    async def api_quote(
        request: Request,
        exchange: str = Query(...),
        account: str = Query(...),
        symbol: str = Query(...),
    ) -> JSONResponse:
        denied = _require_auth(request)
        if denied:
            return denied
        return JSONResponse(svc.market_price(exchange, account, symbol))

    @app.get("/api/candles")
    async def api_candles(
        request: Request,
        exchange: str = Query(...),
        account: str = Query(...),
        symbol: str = Query(...),
        tf: str = Query("15m"),
        limit: int = Query(300, ge=10, le=1000),
    ) -> JSONResponse:
        denied = _require_auth(request)
        if denied:
            return denied
        err = svc.validate_exchange_account(exchange, account)
        if err:
            return JSONResponse(
                {"success": False, "error": {"code": err, "message": err.replace("_", " ").title()}, "candles": []},
                status_code=400,
            )
        resolved = svc.resolve_instrument(exchange, account, symbol)
        if not resolved.get("success") or not isinstance(resolved.get("instrument"), dict):
            return JSONResponse(
                {
                    "success": False,
                    "error": resolved.get("error")
                    or {"code": "INSTRUMENT_NOT_FOUND", "message": "Instrument unresolved."},
                    "requested_symbol": symbol,
                    "display": resolved.get("display") or f"{symbol} → unresolved",
                    "candles": [],
                    "timeframes": list(SUPPORTED_TFS),
                    "timing_ms": resolved.get("timing_ms"),
                },
                status_code=400,
            )
        native = str(resolved["instrument"].get("symbol") or "").strip()
        if not native:
            return JSONResponse(
                {
                    "success": False,
                    "error": {"code": "INSTRUMENT_NOT_FOUND", "message": "Resolver returned empty native symbol."},
                    "requested_symbol": symbol,
                    "display": f"{symbol} → unresolved",
                    "candles": [],
                },
                status_code=400,
            )
        payload = fetch_candles(exchange, account, native, tf, limit=limit)
        payload["requested_symbol"] = symbol
        payload["native_symbol"] = native
        payload["display"] = resolved.get("display") or f"{symbol} → {native}"
        payload["resolved"] = True
        payload["format_meta"] = resolved.get("format_meta") or {}
        payload["timeframes"] = list(SUPPORTED_TFS)
        payload["resolve_timing_ms"] = resolved.get("timing_ms")
        payload["resolve_cache_hit"] = bool(resolved.get("cache_hit"))
        status = 200 if payload.get("success") else 400
        return JSONResponse(payload, status_code=status)

    @app.get("/api/positions")
    async def api_positions(
        request: Request,
        exchange: str = Query(...),
        account: str = Query(...),
    ) -> JSONResponse:
        denied = _require_auth(request)
        if denied:
            return denied
        return JSONResponse(svc.positions(exchange, account))

    @app.get("/api/orders")
    async def api_orders(
        request: Request,
        exchange: str = Query(...),
        account: str = Query(...),
    ) -> JSONResponse:
        denied = _require_auth(request)
        if denied:
            return denied
        return JSONResponse(svc.orders(exchange, account))

    async def _read_json(request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    @app.post("/api/position/set_tp")
    async def api_set_tp(request: Request) -> JSONResponse:
        denied = _require_csrf(request)
        if denied:
            return denied
        body = await _read_json(request)
        out = svc.set_tp(
            str(body.get("exchange") or ""),
            str(body.get("account") or ""),
            str(body.get("symbol") or ""),
            str(body.get("price") or ""),
        )
        return JSONResponse(out, status_code=200 if out.get("success") else 400)

    @app.post("/api/position/set_sl")
    async def api_set_sl(request: Request) -> JSONResponse:
        denied = _require_csrf(request)
        if denied:
            return denied
        body = await _read_json(request)
        out = svc.set_sl(
            str(body.get("exchange") or ""),
            str(body.get("account") or ""),
            str(body.get("symbol") or ""),
            str(body.get("price") or ""),
        )
        return JSONResponse(out, status_code=200 if out.get("success") else 400)

    @app.post("/api/position/close")
    async def api_close(request: Request) -> JSONResponse:
        denied = _require_csrf(request)
        if denied:
            return denied
        body = await _read_json(request)
        out = svc.close_position(
            str(body.get("exchange") or ""),
            str(body.get("account") or ""),
            str(body.get("symbol") or ""),
        )
        return JSONResponse(out, status_code=200 if out.get("success") else 400)

    @app.post("/api/orders/cancel_group")
    async def api_cancel_group(request: Request) -> JSONResponse:
        denied = _require_csrf(request)
        if denied:
            return denied
        body = await _read_json(request)
        out = svc.cancel_order_group(
            str(body.get("exchange") or ""),
            str(body.get("account") or ""),
            str(body.get("symbol") or ""),
            str(body.get("side") or ""),
            str(body.get("type") or body.get("order_type") or "limit"),
        )
        return JSONResponse(out, status_code=200 if out.get("success") else 400)

    return app


# Default app instance for uvicorn
try:
    app = create_app()
except Exception:  # pragma: no cover
    app = create_app()


def main() -> None:
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        "plugins.trade.trademenu.app:app",
        host=cfg.host,
        port=cfg.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
