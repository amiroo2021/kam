from __future__ import annotations

FEATURE_DEFINITION_VERSION = "fibolearn_features_v1"

FEATURE_DEFINITION = {
    "version": FEATURE_DEFINITION_VERSION,
    "timeframe": "1m",
    "lookahead": "strictly uses candles with open_time <= observation timestamp",
    "VWAP": {
        "level_type": "VWAP",
        "calculation_definition": "utc_daily_cumulative_quote_volume_over_base_volume_v1",
        "window": "UTC day from 00:00 through current candle, using Binance quote_volume/base_volume",
    },
    "POC": {
        "level_type": "POC",
        "calculation_definition": "utc_daily_ohlc_volume_profile_uniform_range_allocation_70va_v1",
        "window": "UTC day from 00:00 through current candle; candle volume distributed uniformly across intersected price bins",
    },
    "VAH": {
        "level_type": "VAH",
        "calculation_definition": "utc_daily_ohlc_volume_profile_uniform_range_allocation_70va_v1",
        "window": "same as POC; upper bound of 70% value area expanded from POC by higher adjacent volume",
    },
    "VAL": {
        "level_type": "VAL",
        "calculation_definition": "utc_daily_ohlc_volume_profile_uniform_range_allocation_70va_v1",
        "window": "same as POC; lower bound of 70% value area expanded from POC by higher adjacent volume",
    },
    "swing_high": {
        "level_type": "swing_high",
        "calculation_definition": "rolling_20_closed_1m_high_v1",
        "window": "last 20 available 1m candles ending at observation timestamp",
    },
    "swing_low": {
        "level_type": "swing_low",
        "calculation_definition": "rolling_20_closed_1m_low_v1",
        "window": "last 20 available 1m candles ending at observation timestamp",
    },
    "previous_day_high": {
        "level_type": "previous_day_high",
        "calculation_definition": "previous_utc_day_high_v1",
        "window": "complete prior UTC day present in replay/cache; None when unavailable",
    },
    "previous_day_low": {
        "level_type": "previous_day_low",
        "calculation_definition": "previous_utc_day_low_v1",
        "window": "complete prior UTC day present in replay/cache; None when unavailable",
    },
}

def level_record(level_type: str, timestamp_ms: int, value):
    meta = FEATURE_DEFINITION[level_type]
    return {
        "level_type": meta["level_type"],
        "calculation_definition": meta["calculation_definition"],
        "lookback_window": meta["window"],
        "timestamp_ms": int(timestamp_ms),
        "value": str(value) if value is not None else None,
        "feature_version": FEATURE_DEFINITION_VERSION,
    }
