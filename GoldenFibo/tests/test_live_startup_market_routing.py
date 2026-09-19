from __future__ import annotations

from email.message import Message
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import asyncio

from goldenfibo.session.controller import SessionController
from goldenfibo.session.types import SessionMode


def test_live_startup_routes_futures_klines(monkeypatch):
    ctrl = SessionController()

    seen = {}

    def fake_fetch(url: str):
        seen['url'] = url
        parsed = urlparse(url)
        seen['path'] = parsed.path
        seen['query'] = parse_qs(parsed.query)
        body = b'{"code":-1121,"msg":"Invalid symbol."}'
        headers = Message()
        raise HTTPError(url, 400, 'Bad Request', hdrs=headers, fp=BytesIO(body))

    async def fake_ensure_ws():
        return None

    monkeypatch.setattr(ctrl.kline_source, 'fetch', fake_fetch, raising=False)
    monkeypatch.setattr(ctrl, '_ensure_ws', fake_ensure_ws, raising=False)

    async def run():
        await ctrl.start_run(mode=SessionMode.LIVE, symbol='HYPEUSDT', market='futures')

    try:
        asyncio.run(run())
    except Exception:
        pass

    assert seen['path'] == '/fapi/v1/klines'
    assert seen['query']['symbol'] == ['HYPEUSDT']
    assert seen['query']['interval'] == ['1m']
    assert ctrl.market == 'futures'
