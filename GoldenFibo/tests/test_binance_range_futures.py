from __future__ import annotations

import json
import urllib.parse
import urllib.request

from goldenfibo.marketdata.kline_cache import CachePolicy, KlineCache, fetch_range_cached


def _k(ts, o="100", h="101", l="99", c="100.5", v="1.5", q="150.75"):
    return [ts, o, h, l, c, v, ts + 59_999, q, 10, "0.7", "70.0", "0"]


def test_spot_and_futures_cache_keys_do_not_collide(tmp_path):
    cache = KlineCache(tmp_path / "t.sqlite")
    t0 = 1_700_000_000_000
    step = 60_000
    spot_rows = [_k(t0 + i * step, o=str(100 + i)) for i in range(3)]
    fut_rows = [_k(t0 + i * step, o=str(200 + i)) for i in range(3)]
    cache.upsert_klines("BTCUSDT", "1m", spot_rows, market="spot")
    cache.upsert_klines("BTCUSDT", "1m", fut_rows, market="futures")
    spot = cache.read_range("BTCUSDT", "1m", t0, t0 + 3 * step, market="spot")
    fut = cache.read_range("BTCUSDT", "1m", t0, t0 + 3 * step, market="futures")
    assert [r[1] for r in spot] == ["100", "101", "102"]
    assert [r[1] for r in fut] == ["200", "201", "202"]


def test_fetch_range_cached_uses_usdm_endpoint_for_futures(monkeypatch, tmp_path):
    cache = KlineCache(tmp_path / "t.sqlite")
    seen = []

    def fetch(url: str):
        seen.append(url)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        st = int(qs["startTime"][0])
        et = int(qs["endTime"][0])
        if "fapi.binance.com" not in url:
            raise AssertionError(f"wrong endpoint: {url}")
        if qs["symbol"][0] != "HYPEUSDT":
            raise AssertionError(f"wrong symbol: {url}")
        rows = [_k(st + i * 60_000, o=str(44 + i)) for i in range(3) if st + i * 60_000 <= et]
        return rows

    res = fetch_range_cached(
        "HYPEUSDT",
        "1m",
        1_756_684_860_000,
        1_756_684_860_000 + 3 * 60_000,
        cache=cache,
        policy=CachePolicy.BYPASS,
        fetch=fetch,
        market="futures",
        sleep_s=0,
    )
    assert seen and seen[0].startswith("https://fapi.binance.com/fapi/v1/klines?")
    assert len(res.klines) >= 1
    spot = cache.read_range("HYPEUSDT", "1m", 1_756_684_860_000, 1_756_684_860_000 + 3 * 60_000, market="spot")
    fut = cache.read_range("HYPEUSDT", "1m", 1_756_684_860_000, 1_756_684_860_000 + 3 * 60_000, market="futures")
    assert spot == [] or spot != fut


def test_hype_futures_real_endpoint_shape():
    url = "https://fapi.binance.com/fapi/v1/klines?" + urllib.parse.urlencode({
        "symbol": "HYPEUSDT",
        "interval": "1m",
        "startTime": 1756684860000,
        "endTime": 1758223734000,
        "limit": 1000,
    })
    req = urllib.request.Request(url, headers={"User-Agent": "Hermes-debug/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    assert isinstance(body, list)
    assert body and isinstance(body[0], list)
