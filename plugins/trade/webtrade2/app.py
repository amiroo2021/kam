"""FastAPI app for independent read-only WebTrade2 (Phase 2: write surface)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .auth import LoginRateLimiter, SessionManager
from .config import WebTrade2Config
from .phase2 import WebTrade2Phase2Service
from .service import WebTrade2Service

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    config: Optional[WebTrade2Config] = None,
    service: Optional[WebTrade2Service] = None,
    phase2: Optional[WebTrade2Phase2Service] = None,
) -> FastAPI:
    cfg = config or WebTrade2Config()
    svc = service or WebTrade2Service(session_secret=cfg.session_secret)
    p2 = phase2 or WebTrade2Phase2Service(
        desk=svc.desk,
        session_secret=cfg.session_secret,
        write_enabled=cfg.write_enabled,
        dry_run=cfg.dry_run,
        preview_ttl_seconds=cfg.preview_ttl_seconds,
        ladder_enabled=cfg.ladder_enabled,
    )
    sessions = SessionManager(cfg)
    limiter = LoginRateLimiter(cfg.login_max_failures, cfg.login_lockout_seconds)
    app = FastAPI(title="WebTrade2", version="0.2.0", docs_url=None, redoc_url=None)
    # Ensure audit-log entries for writes are visible.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app.state.config = cfg
    app.state.service = svc
    app.state.phase2 = p2
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
        # index.html must not be cached: it's the entry point that pulls in
        # all CSS/JS. When the app changes, iOS Safari will hold onto a
        # cached copy otherwise, and the user sees the old markup (e.g. a
        # removed FILLS tab) even after the disk file is updated. The
        # bundled CSS / JS files are version-controlled by content hash
        # queries and stay cacheable on their own.
        resp = FileResponse(STATIC_DIR / "index.html")
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.get("/health")
    @app.get("/api/health")
    def health() -> dict:
        return {
            "ok": True,
            "service": "webtrade2",
            "phase": 2,
            "read_only": not p2.write_enabled,
            "write_enabled": p2.write_enabled,
            "dry_run": p2.dry_run,
        }

    @app.get("/api/session")
    def api_session(token: str = Depends(require_auth)) -> dict:
        return {
            "authenticated": True,
            "csrf": sessions.csrf_of(token),
            "phase": 2,
            "read_only": not p2.write_enabled,
            "write_enabled": p2.write_enabled,
            "dry_run": p2.dry_run,
        }

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

    # ------------------------------------------------------------------
    # Phase 2: write surface.
    # ------------------------------------------------------------------

    async def _read_json(request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    def _send(result: Dict[str, Any]) -> JSONResponse:
        # If the service signalled an HTTP gate (423), honor it.
        if isinstance(result, dict) and "_http_status" in result:
            status = int(result.pop("_http_status"))
            return JSONResponse(result, status_code=status)
        ok = bool(result.get("success"))
        return JSONResponse(result, status_code=200 if ok else 400)

    @app.get("/api/phase2")
    def api_phase2(_: str = Depends(require_auth)) -> dict:
        return p2.phase2_status()

    @app.get("/api/phase2/capabilities")
    def api_phase2_caps(exchange: str = Query(...), _: str = Depends(require_auth)) -> dict:
        return p2.phase2_capabilities(exchange)

    @app.post("/api/trade/preview_order")
    async def api_preview_order(request: Request, _: str = Depends(require_csrf)) -> JSONResponse:
        body = await _read_json(request)
        out = p2.preview_order(
            exchange=str(body.get("exchange") or ""),
            account=str(body.get("account") or ""),
            symbol=str(body.get("symbol") or ""),
            side=str(body.get("side") or "buy"),
            order_type=str(body.get("order_type") or body.get("type") or "limit"),
            size=str(body.get("size") or body.get("volume") or ""),
            price=str(body.get("price") or ""),
            market_type=str(body.get("market_type") or "futures"),
            reduce_only=bool(body.get("reduce_only") or False),
        )
        return _send(out)

    @app.post("/api/trade/preview_ladder")
    async def api_preview_ladder(request: Request, _: str = Depends(require_csrf)) -> JSONResponse:
        body = await _read_json(request)
        out = p2.preview_ladder(
            exchange=str(body.get("exchange") or ""),
            account=str(body.get("account") or ""),
            symbol=str(body.get("symbol") or ""),
            side=str(body.get("side") or "buy"),
            distribution=str(body.get("distribution") or "uniform"),
            order_count=body.get("order_count"),
            total_size=str(body.get("total_size") or body.get("total_volume") or "0"),
            start_price=str(body.get("start_price") or "0"),
            end_price=str(body.get("end_price") or "0"),
            market_type=str(body.get("market_type") or "futures"),
            reduce_only=bool(body.get("reduce_only") or False),
        )
        return _send(out)

    @app.post("/api/trade/execute")
    async def api_trade_execute(request: Request, _: str = Depends(require_csrf)) -> JSONResponse:
        body = await _read_json(request)
        out = p2.execute_preview(str(body.get("preview_id") or ""))
        return _send(out)

    @app.post("/api/position/set_tp")
    async def api_set_tp(request: Request, _: str = Depends(require_csrf)) -> JSONResponse:
        body = await _read_json(request)
        out = p2.set_tp(
            exchange=str(body.get("exchange") or ""),
            account=str(body.get("account") or ""),
            symbol=str(body.get("symbol") or ""),
            price=str(body.get("price") or ""),
        )
        return _send(out)

    @app.post("/api/position/set_sl")
    async def api_set_sl(request: Request, _: str = Depends(require_csrf)) -> JSONResponse:
        body = await _read_json(request)
        out = p2.set_sl(
            exchange=str(body.get("exchange") or ""),
            account=str(body.get("account") or ""),
            symbol=str(body.get("symbol") or ""),
            price=str(body.get("price") or ""),
        )
        return _send(out)

    @app.post("/api/position/close")
    async def api_close(request: Request, _: str = Depends(require_csrf)) -> JSONResponse:
        body = await _read_json(request)
        out = p2.close_position(
            exchange=str(body.get("exchange") or ""),
            account=str(body.get("account") or ""),
            symbol=str(body.get("symbol") or ""),
        )
        return _send(out)

    @app.post("/api/orders/cancel_group")
    async def api_cancel_group(request: Request, _: str = Depends(require_csrf)) -> JSONResponse:
        body = await _read_json(request)
        out = p2.cancel_order_group(
            exchange=str(body.get("exchange") or ""),
            account=str(body.get("account") or ""),
            symbol=str(body.get("symbol") or ""),
            side=str(body.get("side") or "buy"),
            order_type=str(body.get("order_type") or body.get("type") or "limit"),
            order_ids=body.get("order_ids") if isinstance(body.get("order_ids"), list) else None,
        )
        return _send(out)

    return app


app = create_app()
