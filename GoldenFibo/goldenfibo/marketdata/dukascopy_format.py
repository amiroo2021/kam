from __future__ import annotations

import lzma
import math
import struct
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable

_RECORD = struct.Struct(">5If")


class DukascopyFormatError(ValueError):
    pass


@dataclass(frozen=True)
class DukascopyCandle:
    open_time_ms: int
    ohlc: tuple[float, float, float, float]
    volume: float
    price_side: str = "bid"

    @property
    def bid_ohlc(self) -> tuple[float, float, float, float]:
        return self.ohlc


def parse_bi5_m1(payload: bytes, day: date, *, price_scale: float, price_side: str = "bid") -> list[DukascopyCandle]:
    if price_scale <= 0:
        raise DukascopyFormatError("price_scale must be positive")
    try:
        raw = lzma.decompress(payload)
    except lzma.LZMAError as exc:
        raise DukascopyFormatError("invalid Dukascopy LZMA payload") from exc
    if len(raw) % _RECORD.size:
        raise DukascopyFormatError("truncated Dukascopy candle record")
    if not raw:
        raise DukascopyFormatError("empty Dukascopy artifact is not verified data")
    day_ms = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
    out: list[DukascopyCandle] = []
    for offset_s, op, close, lo, hi, volume in (_RECORD.unpack_from(raw, i) for i in range(0, len(raw), _RECORD.size)):
        out.append(DukascopyCandle(day_ms + int(offset_s) * 1000,
                                   (op / price_scale, hi / price_scale, lo / price_scale, close / price_scale),
                                   float(volume), price_side))
    validate_candles(out, day)
    return out


def validate_candles(candles: Iterable[DukascopyCandle], day: date) -> None:
    expected_day = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).date()
    previous = None
    seen: set[int] = set()
    for candle in candles:
        ts = int(candle.open_time_ms)
        if ts in seen or (previous is not None and ts <= previous) or ts % 60_000:
            raise DukascopyFormatError("candles are not unique, ordered, and M1-aligned")
        if datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date() != expected_day:
            raise DukascopyFormatError("candle timestamp is outside artifact day")
        open_, high, low, close = candle.ohlc
        if any(not math.isfinite(v) or v <= 0 for v in candle.ohlc):
            raise DukascopyFormatError("invalid Dukascopy candle values")
        if high < max(open_, close) or low > min(open_, close) or high < low:
            raise DukascopyFormatError("invalid Dukascopy OHLC invariants")
        if not math.isfinite(candle.volume) or candle.volume < 0:
            raise DukascopyFormatError("invalid Dukascopy volume")
        previous = ts
        seen.add(ts)
