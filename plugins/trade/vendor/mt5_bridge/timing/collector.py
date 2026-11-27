"""Timing data collection for latency measurement."""

from typing import Optional
from threading import Lock

from .models import TimingData, get_timestamp_us


class TimingCollector:
    """Collects and stores timing data from request/response cycles."""

    def __init__(self, max_samples: int = 10000):
        """Initialize the collector.

        Args:
            max_samples: Maximum number of samples to keep (oldest are dropped)
        """
        self._samples: list[TimingData] = []
        self._max_samples = max_samples
        self._lock = Lock()
        self._pending: dict[str, TimingData] = {}

    def start_request(self, request_id: str, action: str) -> None:
        """Record the start of a request.

        Args:
            request_id: Unique request identifier
            action: The action being performed (e.g., "TRADE", "GET_TICK")
        """
        timing = TimingData(
            request_id=request_id,
            action=action,
            t1_send=get_timestamp_us()
        )
        with self._lock:
            self._pending[request_id] = timing

    def complete_request(
        self,
        request_id: str,
        t2_receive: int,
        t3_execute: int
    ) -> Optional[TimingData]:
        """Complete a request with EA timing data.

        Args:
            request_id: Unique request identifier
            t2_receive: EA receive timestamp from response
            t3_execute: EA execute timestamp from response

        Returns:
            Completed TimingData or None if request not found
        """
        with self._lock:
            timing = self._pending.pop(request_id, None)
            if timing is None:
                return None

            timing.t4_response = get_timestamp_us()
            timing.t2_receive = t2_receive
            timing.t3_execute = t3_execute

            self._samples.append(timing)

            # Trim oldest samples if over limit
            if len(self._samples) > self._max_samples:
                self._samples = self._samples[-self._max_samples:]

            return timing

    def get_samples(self) -> list[TimingData]:
        """Get a copy of all collected samples."""
        with self._lock:
            return list(self._samples)

    def get_samples_by_action(self, action: str) -> list[TimingData]:
        """Get samples filtered by action type."""
        with self._lock:
            return [s for s in self._samples if s.action == action]

    def clear(self) -> None:
        """Clear all collected samples."""
        with self._lock:
            self._samples.clear()
            self._pending.clear()

    @property
    def sample_count(self) -> int:
        """Number of collected samples."""
        with self._lock:
            return len(self._samples)
