"""FastAPI app — backtest-web LIVE / BACKTEST / REPLAY_TO_LIVE."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..engine.config import Side
from ..session.controller import get_session
from ..session.types import SessionMode

logger = logging.getLogger("goldenfibo.api")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    session = get_session()
    await session.start()
    logger.info("backtest-web default LIVE session starting symbol=%s", session.symbol)
    yield
    await session.stop()


app = FastAPI(title="backtest-web", version="0.3.0", lifespan=lifespan)


@app.get("/api/health")
async def health() -> dict:
    s = get_session()
    return {
        "ok": True,
        "mode": s.mode.value,
        "phase": s.phase.value,
        "symbol": s.symbol,
        "timeframe": s.timeframe,
        "feed_status": s.feed_status,
        "p0_seeded": s._p0_seeded,
        "clients": len(s._clients),
        "ambiguity_count": s.ambiguity_count,
        "bars_processed": s.bars_processed,
        "run_id": s.run_id,
    }


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(get_session().snapshot_dict())


def _parse_start_body(body: Optional[dict], **query: Any) -> dict:
    body = body or {}
    merged = {**query, **{k: v for k, v in body.items() if v is not None}}
    return merged


@app.post("/api/session/start")
async def api_session_start(body: Optional[dict] = Body(None)) -> JSONResponse:
    """Start LIVE | BACKTEST | REPLAY_TO_LIVE run."""
    b = body or {}
    mode_s = str(b.get("mode") or "LIVE").upper().replace("-", "_")
    if mode_s == "REPLAY":
        mode_s = "REPLAY_TO_LIVE"
    mode = SessionMode(mode_s)
    side = b.get("side")
    pct = b.get("percentage")
    try:
        sess = get_session()
        if b.get("cache_policy"):
            from ..marketdata.kline_cache import CachePolicy
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
            side=Side(str(side).upper()) if side else None,
            percentage=Decimal(str(pct)) if pct is not None else None,
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
    from ..marketdata.kline_cache import validate_klines_sequence
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


@app.post("/api/session")
async def api_session_legacy(
    side: Optional[str] = Query(None),
    percentage: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    timeframe: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
) -> JSONResponse:
    """Legacy LIVE reconfigure."""
    s = get_session()
    await s.reconfigure(
        side=Side(side.upper()) if side else None,
        percentage=Decimal(percentage) if percentage else None,
        symbol=symbol,
        timeframe=timeframe,
        market=market,
    )
    return JSONResponse(s.snapshot_dict())


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
                mode_s = str(raw.get("mode") or ("LIVE" if op == "reconfigure" else "LIVE")).upper()
                mode_s = mode_s.replace("-", "_")
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
                        side=Side(str(side).upper()) if side else None,
                        percentage=Decimal(str(pct)) if pct is not None else None,
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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "goldenfibo.api.app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
