"""WebBacktest FastAPI app — start-to-live replay over the shared GoldenFibo core."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

_KAM_ROOT = Path("/root/kam")
_GF_ROOT = _KAM_ROOT / "GoldenFibo"
for p in (_GF_ROOT, _KAM_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from goldenfibo.engine.config import Side
from goldenfibo.session.controller import get_session
from goldenfibo.session.types import SessionMode

logger = logging.getLogger("webbacktest")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="webbacktest", version="0.1.0")
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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
    async def root() -> Response:
        return _app_page()

    @app.get("/api/health")
    async def health() -> dict:
        s = get_session()
        return {"ok": True, "service": "webbacktest", "phase": s.phase.value, "mode": s.mode.value}

    @app.get("/api/state")
    async def api_state() -> Response:
        return JSONResponse(get_session().snapshot_dict())

    @app.post("/api/session/start")
    async def api_session_start(body: Optional[dict] = Body(None)) -> JSONResponse:
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
    async def api_session_stop() -> JSONResponse:
        await get_session().stop()
        return JSONResponse(get_session().snapshot_dict())

    @app.get("/api/cache/stats")
    async def api_cache_stats(symbol: str = "BTCUSDT", timeframe: str = "1m") -> JSONResponse:
        s = get_session()
        return JSONResponse(s.kline_cache.stats_for(symbol, timeframe, market=s.market))

    @app.post("/api/cache/clear")
    async def api_cache_clear(body: Optional[dict] = Body(None)) -> JSONResponse:
        b = body or {}
        s = get_session()
        n = s.kline_cache.clear(str(b.get("symbol") or s.symbol), str(b.get("timeframe") or s.timeframe), market=str(b.get("market") or s.market))
        return JSONResponse({"cleared": n, "symbol": b.get("symbol") or s.symbol, "timeframe": b.get("timeframe") or s.timeframe})

    @app.post("/api/cache/validate")
    async def api_cache_validate(body: Optional[dict] = Body(None)) -> JSONResponse:
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

    app.get_session = get_session  # type: ignore[attr-defined]
    app.STATIC_DIR = STATIC_DIR  # type: ignore[attr-defined]
    return app


app = create_app()
app.get_session = get_session  # type: ignore[attr-defined]
app.STATIC_DIR = STATIC_DIR  # type: ignore[attr-defined]


def main() -> None:
    import uvicorn

    cfg = __import__("plugins.trade.webbacktest.config", fromlist=["load_config"]).load_config()
    uvicorn.run(
        "plugins.trade.webbacktest.app:app",
        host=cfg.host,
        port=cfg.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
