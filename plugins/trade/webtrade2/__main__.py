from __future__ import annotations

import uvicorn

from .app import create_app
from .config import WebTrade2Config

cfg = WebTrade2Config()
uvicorn.run(create_app(config=cfg), host=cfg.host, port=cfg.port)
