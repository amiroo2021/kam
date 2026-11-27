"""Latency statistics analyzer."""

from typing import Callable

from .models import TimingData, LatencyStats, TimingReport


def percentile(sorted_values: list[int], p: float) -> int:
    """Calculate percentile from sorted values.

    Args:
        sorted_values: Pre-sorted list of values
        p: Percentile (0.0 to 1.0)

    Returns:
        Percentile value
    """
    if not sorted_values:
        return 0
    idx = int(len(sorted_values) * p)
    idx = min(idx, len(sorted_values) - 1)
    return sorted_values[idx]


def compute_stats(values: list[int]) -> LatencyStats:
    """Compute statistics from a list of values.

    Args:
        values: List of latency values in microseconds

    Returns:
        LatencyStats with computed statistics
    """
    if not values:
        return LatencyStats()

    sorted_values = sorted(values)
    count = len(sorted_values)
    total = sum(sorted_values)

    return LatencyStats(
        count=count,
        min_us=sorted_values[0],
        max_us=sorted_values[-1],
        avg_us=total / count,
        p50_us=percentile(sorted_values, 0.50),
        p95_us=percentile(sorted_values, 0.95),
        p99_us=percentile(sorted_values, 0.99)
    )


class LatencyAnalyzer:
    """Analyzes timing data and produces statistics."""

    def analyze(self, samples: list[TimingData]) -> TimingReport:
        """Analyze samples and produce a timing report.

        Args:
            samples: List of TimingData samples

        Returns:
            TimingReport with statistics
        """
        if not samples:
            return TimingReport()

        round_trip_values = [s.round_trip_us for s in samples]
        ea_processing_values = [s.ea_processing_us for s in samples]
        network_values = [s.network_us for s in samples]

        return TimingReport(
            round_trip=compute_stats(round_trip_values),
            ea_processing=compute_stats(ea_processing_values),
            network=compute_stats(network_values),
            samples=samples
        )

    def analyze_by_action(
        self,
        samples: list[TimingData]
    ) -> dict[str, TimingReport]:
        """Analyze samples grouped by action type.

        Args:
            samples: List of TimingData samples

        Returns:
            Dict mapping action names to TimingReports
        """
        by_action: dict[str, list[TimingData]] = {}
        for sample in samples:
            if sample.action not in by_action:
                by_action[sample.action] = []
            by_action[sample.action].append(sample)

        return {
            action: self.analyze(action_samples)
            for action, action_samples in by_action.items()
        }
