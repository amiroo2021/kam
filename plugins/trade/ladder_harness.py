"""Shared harness primitives for Lighter live ladder validation.

Two lessons from the Phase 6 run are encoded here so a future harness
cannot repeat them:

1. **Persist evidence INCREMENTALLY.** The Phase 6 harness crashed in
   its reporting code *after* all five WS batches had been placed and
   reconciled, but *before* it wrote ``execute.json``. The per-batch
   timings, read counts and exchange order ids lived only in memory,
   so the crash destroyed the run's primary evidence. :class:`EvidenceLog`
   appends each batch to a JSONL file as it completes, so a crash later
   in the run still leaves every completed batch on disk.

2. **Capture ACTUAL async cancellation chunk timings.** The Phase 6
   harness patched the sync ``_submit_cancel_tx_batch`` wrapper, but the
   production group runner (``_run_cancel_tx_group``) calls the async
   core ``_submit_cancel_tx_batch_async`` directly -- so the patch never
   fired and the chunk timings were silently absent. :func:`time_cancel_chunks`
   patches the ASYNC core instead, which is the only seam every chunk
   actually flows through.

Neither helper performs any exchange write by itself: the caller supplies
the already-built coroutine function and the group runner.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence


class EvidenceLog:
    """Append-only JSONL evidence log, flushed after every record.

    Each :meth:`record` call appends one JSON object and flushes it to
    disk immediately, so evidence survives a crash that happens after the
    record was written but before the run finished.
    """

    def __init__(self, path: os.PathLike | str, *, header: Optional[Dict[str, Any]] = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        if header:
            self.record("header", **header)

    def record(self, kind: str, **fields: Any) -> Dict[str, Any]:
        """Write one evidence record and flush it to disk."""
        row: Dict[str, Any] = {"kind": kind, "wall": time.time()}
        row.update(fields)
        self._fh.write(json.dumps(row, default=str) + "\n")
        self._fh.flush()
        try:
            os.fsync(self._fh.fileno())
        except OSError:
            # fsync is a durability nicety; the flush above already put the
            # bytes in the OS page cache, which survives a process crash.
            pass
        return row

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass

    def __enter__(self) -> "EvidenceLog":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @staticmethod
    def read(path: os.PathLike | str) -> List[Dict[str, Any]]:
        """Read back every record written to a JSONL evidence log."""
        p = Path(path)
        if not p.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # A torn final line from a hard kill: ignore it rather
                # than losing every complete record before it.
                continue
        return rows


def time_cancel_chunks(
    async_core: Callable[..., Any],
    *,
    group_runner: Callable[..., Any],
    group_kwargs: Optional[Dict[str, Any]] = None,
    timings: Optional[List[Dict[str, Any]]] = None,
) -> Any:
    """Run a cancel group and capture REAL per-chunk elapsed times.

    ``async_core`` must be the ASYNC core the group runner awaits (e.g.
    ``x_lighter_agent._submit_cancel_tx_batch_async``). Patching the sync
    wrapper is not enough -- the group runner calls the async core
    directly, so a sync-only patch silently records nothing.

    ``group_runner`` is called with ``**group_kwargs`` (e.g.
    ``credentials=``, ``market_id=``, ``target_ids=``).

    Returns the group runner's result unchanged.
    """
    sink = timings if timings is not None else []
    kwargs = dict(group_kwargs or {})

    async def _timed(*args: Any, **kw: Any) -> Any:
        chunk_ids = kw.get("order_ids") or []
        started = time.monotonic()
        outcome = await async_core(*args, **kw)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        sink.append({
            "order_ids": [int(i) for i in chunk_ids],
            "chunk_size": len(chunk_ids),
            "elapsed_ms": round(elapsed_ms, 3),
            "outcome": outcome.get("outcome") if isinstance(outcome, dict) else None,
            "api_code": outcome.get("api_code") if isinstance(outcome, dict) else None,
            "submitted": outcome.get("submitted_count") if isinstance(outcome, dict) else None,
            "accepted": outcome.get("accepted_count") if isinstance(outcome, dict) else None,
            "rejected": outcome.get("rejected_count") if isinstance(outcome, dict) else None,
            "unknown": outcome.get("unknown_count") if isinstance(outcome, dict) else None,
        })
        return outcome

    import plugins.trade.agents.x_lighter_agent as agent  # noqa: PLC0415

    saved = agent._submit_cancel_tx_batch_async
    agent._submit_cancel_tx_batch_async = _timed  # type: ignore[assignment]
    try:
        result = group_runner(**kwargs)
    finally:
        # Restore the ORIGINAL object, never the local reference, so a
        # re-entrant call cannot leave a wrapper installed.
        agent._submit_cancel_tx_batch_async = saved  # type: ignore[assignment]

    # A group that ran chunks but produced NO timings means the patch
    # was on the wrong seam -- the exact defect the Phase 6 run hit
    # (it wrapped the sync entry point, which the group runner never
    # calls). Reporting "0 chunks" would read as "the cancel was
    # instant", which is false. Fail loudly instead.
    chunk_count = 0
    if isinstance(result, dict):
        batches = result.get("batches")
        if isinstance(batches, list):
            chunk_count = len(batches)
    if chunk_count and not sink:
        raise RuntimeError(
            "no chunk timings captured for "
            f"{chunk_count} cancel chunk(s): the patch must wrap "
            "_submit_cancel_tx_batch_async (the async core the group "
            "runner awaits), not the sync wrapper"
        )
    return result


def ladder_price_band(
    *,
    top: str,
    bottom: str,
    step: str,
    count: int,
) -> List[str]:
    """Contiguous, strictly descending price band as decimal strings.

    Raises ``ValueError`` when ``count`` prices cannot be laid out
    between ``top`` and ``bottom`` at ``step`` -- a ladder with a gap is
    worse than no ladder, because it silently mis-states the total
    notional.
    """
    from decimal import Decimal  # noqa: PLC0415

    t = Decimal(top)
    b = Decimal(bottom)
    s = Decimal(step)
    if s <= 0:
        raise ValueError("step must be positive")
    if t < b:
        raise ValueError("top must be >= bottom")
    span = (t - b) / s
    if int(span) + 1 < count:
        raise ValueError(
            f"band {t}..{b} step {s} holds only {int(span) + 1} prices, need {count}"
        )
    out: List[str] = []
    for i in range(count):
        out.append(format(t - s * i, "f"))
    if out[-1] != format(b, "f"):
        raise ValueError("band does not land exactly on the bottom price")
    return out


def minimum_order_size(
    *,
    min_base_amount: str,
    min_quote_amount: str,
    lowest_price: str,
    size_decimals: int,
) -> str:
    """Smallest size (as a decimal string) whose notional clears both
    exchange minimums at ``lowest_price``.

    ``min_quote_amount`` is the binding constraint for a ladder: the
    LOWEST price in the band carries the smallest notional, so the size
    must be sized for that price or the bottom of the ladder gets
    rejected while the top of the ladder sails through.
    """
    from decimal import Decimal, ROUND_UP  # noqa: PLC0415

    price = Decimal(lowest_price)
    if price <= 0:
        raise ValueError("lowest_price must be positive")
    quote = Decimal(min_quote_amount)
    base = Decimal(min_base_amount)
    needed = quote / price
    size = max(base, needed)
    quantum = Decimal(1).scaleb(-int(size_decimals))
    return format(size.quantize(quantum, rounding=ROUND_UP), "f")


def summarise_chunks(timings: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-chunk timings into a reportable summary."""
    if not timings:
        return {"chunks": 0, "total_ms": 0.0, "max_ms": 0.0, "min_ms": 0.0}
    times = [float(t.get("elapsed_ms") or 0.0) for t in timings]
    return {
        "chunks": len(timings),
        "total_ms": round(sum(times), 3),
        "max_ms": round(max(times), 3),
        "min_ms": round(min(times), 3),
        "sizes": [int(t.get("chunk_size") or 0) for t in timings],
    }
