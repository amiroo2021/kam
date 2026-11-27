"""Timing data models for latency measurement."""

from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class TimingData:
    """Timing information from a single request/response cycle.

    Timestamp points:
    - t1_send: Python sends command (microseconds since epoch)
    - t2_receive: EA receives message (MT5 GetMicrosecondCount)
    - t3_execute: EA finishes processing (MT5 GetMicrosecondCount)
    - t4_response: Python receives response (microseconds since epoch)
    """
    request_id: str
    action: str
    t1_send: int = 0  # Python send timestamp (microseconds)
    t2_receive: int = 0  # EA receive timestamp (microseconds, relative)
    t3_execute: int = 0  # EA execute complete timestamp (microseconds, relative)
    t4_response: int = 0  # Python receive timestamp (microseconds)

    @property
    def round_trip_us(self) -> int:
        """Total round-trip latency in microseconds."""
        return self.t4_response - self.t1_send

    @property
    def round_trip_ms(self) -> float:
        """Total round-trip latency in milliseconds."""
        return self.round_trip_us / 1000.0

    @property
    def ea_processing_us(self) -> int:
        """EA processing time in microseconds."""
        return self.t3_execute - self.t2_receive

    @property
    def ea_processing_ms(self) -> float:
        """EA processing time in milliseconds."""
        return self.ea_processing_us / 1000.0

    @property
    def network_us(self) -> int:
        """Estimated network latency in microseconds (one-way estimate)."""
        return (self.round_trip_us - self.ea_processing_us) // 2

    @property
    def network_ms(self) -> float:
        """Estimated network latency in milliseconds (one-way estimate)."""
        return self.network_us / 1000.0


@dataclass
class LatencyStats:
    """Aggregated latency statistics."""
    count: int = 0
    min_us: int = 0
    max_us: int = 0
    avg_us: float = 0.0
    p50_us: int = 0  # Median
    p95_us: int = 0
    p99_us: int = 0

    @property
    def min_ms(self) -> float:
        return self.min_us / 1000.0

    @property
    def max_ms(self) -> float:
        return self.max_us / 1000.0

    @property
    def avg_ms(self) -> float:
        return self.avg_us / 1000.0

    @property
    def p50_ms(self) -> float:
        return self.p50_us / 1000.0

    @property
    def p95_ms(self) -> float:
        return self.p95_us / 1000.0

    @property
    def p99_ms(self) -> float:
        return self.p99_us / 1000.0


@dataclass
class TimingReport:
    """Complete timing analysis report."""
    round_trip: LatencyStats = field(default_factory=LatencyStats)
    ea_processing: LatencyStats = field(default_factory=LatencyStats)
    network: LatencyStats = field(default_factory=LatencyStats)
    samples: list[TimingData] = field(default_factory=list)


def get_timestamp_us() -> int:
    """Get current timestamp in microseconds."""
    return int(time.time() * 1_000_000)
