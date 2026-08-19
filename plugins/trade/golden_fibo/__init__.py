"""GoldenFibo v1: Fibonacci ladder strategy for DEX perpetuals.

Reproduces the essential behavior of GoldenFiboCounterTrend.mq4 on a
DEX venue. One shared TP for the accumulated position; one pending
LIMIT at a time; logical step fill prices persisted from the
venue-confirmed accepted limit price (NOT historical execution VWAP).
"""

from plugins.trade.golden_fibo.config import (
    FIBO_RATIO,
    GOLDENFIBO_SCHEMA_VERSION,
    MAX_STEP,
    GoldenFiboConfig,
    golden_fibo_volume,
)
from plugins.trade.golden_fibo.state import GoldenFiboState
from plugins.trade.golden_fibo.client_id_v2 import (
    MAGIC,
    VERSION,
    decode_golden_fibo_client_id,
    encode_golden_fibo_client_id,
    is_golden_fibo_v2_client_id,
)


__all__ = [
    "FIBO_RATIO",
    "GOLDENFIBO_SCHEMA_VERSION",
    "MAX_STEP",
    "GoldenFiboConfig",
    "GoldenFiboState",
    "golden_fibo_volume",
    "MAGIC",
    "VERSION",
    "decode_golden_fibo_client_id",
    "encode_golden_fibo_client_id",
    "is_golden_fibo_v2_client_id",
]
