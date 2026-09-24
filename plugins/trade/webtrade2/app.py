"""FastAPI app for independent read-only WebTrade2."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .auth import LoginRateLimiter, SessionManager
from .config import WebTrade2Config
from .service import WebTrade2Service

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(config: Optional[WebTrade2Config] = None, service: Optional[WebTrade2Service] = None) -> FastAPI:
    cfg = config or WebTrade2Config()
    svc = service or WebTrade2Service(session_secret=cfg.session_secret)
    sessions = SessionManager(cfg)
    limiter = LoginRateLimiter(cfg.login_max_failures, cfg.login_lockout_seconds)
    app = FastAPI(title="WebTrade2", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.config = cfg
    app.state.service = svc
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def session_token(request: Request) -> Optional[str]:
        return request.cookies.get(cfg.cookie_name)

    def require_auth(request: Request) -> str:
        token = session_token(request)
        if not sessions.verify(token):
            raise HTTPException(status_code=401, detail="AUTH_REQUIRED")
        return str(token)

    def require_csrf(request: Request, x_csrf_token: Optional[str] = Header(default=None)) -> str:
        token = require_auth(request)
        supplied = x_csrf_token or request.cookies.get(cfg.csrf_cookie_name)
        if not sessions.csrf_ok(token, supplied):
            raise HTTPException(status_code=403, detail="CSRF_FAILED")
        return token

    @app.get("/")
    def root() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health")
    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "service": "webtrade2", "phase": 1, "read_only": True}

    @app.get("/api/session")
    def api_session(token: str = Depends(require_auth)) -> dict:
        return {"authenticated": True, "csrf": sessions.csrf_of(token), "phase": 1, "read_only": True}

    @app.post("/login")
    def login(request: Request, password: str = Form(...)) -> Response:
        client_key = request.client.host if request.client else "unknown"
        gate = limiter.check(client_key)
        if not gate.allowed:
            return JSONResponse({"success": False, "error": gate.message}, status_code=429)
        if not cfg.password or not sessions.password_ok(password):
            limiter.record_failure(client_key)
            return JSONResponse({"success": False, "error": "INVALID_PASSWORD"}, status_code=401)
        limiter.record_success(client_key)
        token, csrf = sessions.issue()
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(cfg.cookie_name, token, httponly=True, secure=False, samesite="lax", max_age=cfg.session_max_age_seconds)
        response.set_cookie(cfg.csrf_cookie_name, csrf, httponly=False, secure=False, samesite="lax", max_age=cfg.session_max_age_seconds)
        return response

    @app.post("/logout")
    def logout() -> Response:
        response = RedirectResponse(url="/", status_code=303)
        response.delete_cookie(cfg.cookie_name)
        response.delete_cookie(cfg.csrf_cookie_name)
        return response

    @app.get("/api/exchanges")
    def api_exchanges(_: str = Depends(require_auth)) -> dict:
        return svc.exchanges()

    @app.get("/api/exchanges/{exchange}/accounts")
    def api_accounts(exchange: str, _: str = Depends(require_auth)) -> dict:
        return svc.accounts(exchange)

    @app.get("/api/exchanges/{exchange}/capabilities")
    def api_caps(exchange: str, _: str = Depends(require_auth)) -> dict:
        return svc.capability_description(exchange)

    @app.get("/api/markets")
    def api_markets(exchange: str, account: str, market_type: str = "futures", search: str = "", _: str = Depends(require_auth)) -> dict:
        return svc.markets(exchange, account, market_type, search)

    @app.get("/api/account/state")
    def api_account_state(exchange: str, account: str, _: str = Depends(require_auth)) -> dict:
        return svc.account_state(exchange, account)

    @app.get("/api/market_price")
    def api_market_price(exchange: str, account: str, symbol: str, market_type: str = "futures", _: str = Depends(require_auth)) -> dict:
        return svc.market_price(exchange, account, symbol, market_type)

    @app.get("/api/candles")
    def api_candles(exchange: str, account: str, symbol: str, interval: str = "1h", limit: int = 120, market_type: str = "futures", _: str = Depends(require_auth)) -> dict:
        return svc.candles(exchange, account, symbol, interval, limit, market_type)

    @app.post("/api/ladder/preview")
    async def api_ladder_preview(request: Request, _: str = Depends(require_csrf)) -> dict:
        body = await request.json()
        return svc.preview_ladder(
            exchange=str(body.get("exchange") or ""),
            account=str(body.get("account") or ""),
            market_type=str(body.get("market_type") or "futures"),
            symbol=str(body.get("symbol") or ""),
            side=str(body.get("side") or "buy"),
            distribution=str(body.get("distribution") or "uniform"),
            order_count=int(body.get("order_count") or 0),
            total_size=str(body.get("total_size") or "0"),
            start_price=str(body.get("start_price") or "0"),
            end_price=str(body.get("end_price") or "0"),
        )

    return app


app = create_app()
