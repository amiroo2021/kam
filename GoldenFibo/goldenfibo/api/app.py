"""FastAPI app with lifespan context manager."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..engine.config import Side
from .session import get_session

logger = logging.getLogger("goldenfibo.api")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    session = get_session()
    await session.start()
    logger.info("GoldenFibo live session started symbol=%s", session.symbol)
    yield
    await session.stop()


app = FastAPI(title="GoldenFibo Live", version="0.2.0", lifespan=lifespan)


@app.get("/api/health")
async def health() -> dict:
    s = get_session()
    return {
        "ok": True,
        "mode": s.mode,
        "symbol": s.symbol,
        "timeframe": s.timeframe,
        "feed_status": s.feed_status,
        "p0_seeded": s._p0_seeded,
        "clients": len(s._clients),
    }


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(get_session().snapshot_dict())


@app.post("/api/session")
async def api_session(
    side: Optional[str] = Query(None),
    percentage: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    timeframe: Optional[str] = Query(None),
) -> JSONResponse:
    """Explicit reconfigure (new paper cycle). Not used on mere reconnect."""
    s = get_session()
    await s.reconfigure(
        side=Side(side.upper()) if side else None,
        percentage=Decimal(percentage) if percentage else None,
        symbol=symbol,
        timeframe=timeframe,
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
            if op == "reconfigure":
                side = raw.get("side")
                pct = raw.get("percentage")
                await session.reconfigure(
                    side=Side(str(side).upper()) if side else None,
                    percentage=Decimal(str(pct)) if pct is not None else None,
                    symbol=raw.get("symbol"),
                    timeframe=raw.get("timeframe"),
                )
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
