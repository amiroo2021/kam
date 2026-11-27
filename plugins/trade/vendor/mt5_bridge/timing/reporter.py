"""Timing report output formatters."""

import csv
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import TextIO

from .models import TimingData, LatencyStats, TimingReport


def format_stats_line(name: str, stats: LatencyStats) -> str:
    """Format a single stats line for console output."""
    return (
        f"{name:15} | "
        f"count={stats.count:5} | "
        f"min={stats.min_ms:7.2f}ms | "
        f"avg={stats.avg_ms:7.2f}ms | "
        f"p50={stats.p50_ms:7.2f}ms | "
        f"p95={stats.p95_ms:7.2f}ms | "
        f"p99={stats.p99_ms:7.2f}ms | "
        f"max={stats.max_ms:7.2f}ms"
    )


class ConsoleReporter:
    """Outputs timing reports to console."""

    def print_report(self, report: TimingReport, title: str = "Latency Report") -> None:
        """Print a formatted timing report to console."""
        print()
        print("=" * 100)
        print(f" {title}")
        print("=" * 100)
        print()
        print(format_stats_line("Round-trip", report.round_trip))
        print(format_stats_line("EA Processing", report.ea_processing))
        print(format_stats_line("Network (est.)", report.network))
        print()
        print("=" * 100)

    def print_by_action(
        self,
        reports: dict[str, TimingReport],
        title: str = "Latency by Action"
    ) -> None:
        """Print timing reports grouped by action."""
        print()
        print("=" * 100)
        print(f" {title}")
        print("=" * 100)

        for action, report in sorted(reports.items()):
            print()
            print(f"[{action}]")
            print(format_stats_line("  Round-trip", report.round_trip))
            print(format_stats_line("  EA Process", report.ea_processing))
            print(format_stats_line("  Network", report.network))

        print()
        print("=" * 100)


class CSVReporter:
    """Outputs timing data to CSV files."""

    def write_samples(
        self,
        samples: list[TimingData],
        file_path: str | Path
    ) -> None:
        """Write raw timing samples to CSV file.

        Args:
            samples: List of TimingData samples
            file_path: Output file path
        """
        with open(file_path, "w", newline="") as f:
            self._write_samples_to_stream(samples, f)

    def samples_to_string(self, samples: list[TimingData]) -> str:
        """Convert samples to CSV string."""
        output = StringIO()
        self._write_samples_to_stream(samples, output)
        return output.getvalue()

    def _write_samples_to_stream(
        self,
        samples: list[TimingData],
        stream: TextIO
    ) -> None:
        """Write samples to a text stream."""
        writer = csv.writer(stream)
        writer.writerow([
            "request_id",
            "action",
            "t1_send_us",
            "t2_receive_us",
            "t3_execute_us",
            "t4_response_us",
            "round_trip_ms",
            "ea_processing_ms",
            "network_ms"
        ])

        for s in samples:
            writer.writerow([
                s.request_id,
                s.action,
                s.t1_send,
                s.t2_receive,
                s.t3_execute,
                s.t4_response,
                f"{s.round_trip_ms:.3f}",
                f"{s.ea_processing_ms:.3f}",
                f"{s.network_ms:.3f}"
            ])

    def write_summary(
        self,
        report: TimingReport,
        file_path: str | Path
    ) -> None:
        """Write summary statistics to CSV file.

        Args:
            report: TimingReport with statistics
            file_path: Output file path
        """
        with open(file_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "metric",
                "count",
                "min_ms",
                "avg_ms",
                "p50_ms",
                "p95_ms",
                "p99_ms",
                "max_ms"
            ])

            for name, stats in [
                ("round_trip", report.round_trip),
                ("ea_processing", report.ea_processing),
                ("network", report.network)
            ]:
                writer.writerow([
                    name,
                    stats.count,
                    f"{stats.min_ms:.3f}",
                    f"{stats.avg_ms:.3f}",
                    f"{stats.p50_ms:.3f}",
                    f"{stats.p95_ms:.3f}",
                    f"{stats.p99_ms:.3f}",
                    f"{stats.max_ms:.3f}"
                ])


def generate_filename(prefix: str = "latency", extension: str = "csv") -> str:
    """Generate a timestamped filename."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{timestamp}.{extension}"
