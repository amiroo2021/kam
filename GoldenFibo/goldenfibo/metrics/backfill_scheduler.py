"""Latest-wins/coalescing metric backfill scheduler."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DesiredWindow:
    market: str
    symbol: str
    ladder_start_ms: int
    step_start_ms: int
    endpoint_ts_ms: int
    session_id: str

    def same_identity(self, other: "DesiredWindow") -> bool:
        return (
            self.market == other.market
            and self.symbol == other.symbol
            and self.ladder_start_ms == other.ladder_start_ms
            and self.step_start_ms == other.step_start_ms
            and self.session_id == other.session_id
        )

    def same_material_window(self, other: "DesiredWindow") -> bool:
        return (
            self.market == other.market
            and self.symbol == other.symbol
            and self.ladder_start_ms == other.ladder_start_ms
            and self.step_start_ms == other.step_start_ms
            and self.endpoint_ts_ms == other.endpoint_ts_ms
            and self.session_id == other.session_id
        )

    def step_only_relative_to(self, other: "DesiredWindow") -> bool:
        return (
            self.market == other.market
            and self.symbol == other.symbol
            and self.ladder_start_ms == other.ladder_start_ms
            and self.step_start_ms != other.step_start_ms
            and self.session_id == other.session_id
        )


@dataclass
class SchedulerStats:
    requests_received: int = 0
    requests_coalesced: int = 0
    workers_launched: int = 0
    reevaluate_runs: int = 0
    pending_superseded_by_p0: int = 0
    max_concurrent_workers_observed: int = 0
    session_invalidations: int = 0


WorkerFn = Callable[[DesiredWindow], Awaitable[None]]


class LatestWinsScheduler:
    """Latest-wins scheduler: one running worker, one pending window."""

    def __init__(
        self,
        worker_fn: WorkerFn,
        *,
        on_idle: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self._worker_fn = worker_fn
        self._on_idle = on_idle
        self._running_task: Optional[asyncio.Task] = None
        self._running_request: Optional[DesiredWindow] = None
        self._pending: Optional[DesiredWindow] = None
        self._pending_kind: str = "full"
        self._last_finished_request: Optional[DesiredWindow] = None
        self._current_concurrent = 0
        self._stats = SchedulerStats()
        self._lock = asyncio.Lock()
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self._worker_outcome: Optional[str] = None

    @property
    def stats(self) -> SchedulerStats:
        return self._stats

    def has_pending(self) -> bool:
        return self._pending is not None

    def pending(self) -> Optional[DesiredWindow]:
        return self._pending

    def pending_kind(self) -> str:
        return self._pending_kind

    def is_running(self) -> bool:
        return self._running_task is not None and not self._running_task.done()

    def current_concurrent(self) -> int:
        return self._current_concurrent

    async def wait_idle(self) -> None:
        await self._idle_event.wait()

    async def request_window(self, window: DesiredWindow, *, kind: str = "full") -> None:
        if kind not in ("full", "step"):
            raise ValueError(f"kind must be 'full' or 'step', got {kind!r}")
        self._stats.requests_received += 1
        self._idle_event.clear()
        async with self._lock:
            if (
                self._running_task is None
                and self._pending is None
                and self._last_finished_request is not None
                and window.same_material_window(self._last_finished_request)
            ):
                self._idle_event.set()
                return
            self._absorb_request_locked(window, kind=kind)
            await self._kick_locked()

    def _window_matches(self, a: DesiredWindow, b: DesiredWindow) -> bool:
        return a.same_identity(b)

    def _kind_for_window(self, latest: DesiredWindow, installed: Optional[DesiredWindow]) -> str:
        if installed is None:
            return "full"
        if latest.session_id != installed.session_id:
            return "full"
        if latest.ladder_start_ms != installed.ladder_start_ms:
            return "full"
        if latest.step_start_ms != installed.step_start_ms:
            return "step"
        return "full"

    def _absorb_request_locked(self, window: DesiredWindow, *, kind: str) -> None:
        existing = self._pending
        if existing is None:
            self._pending = window
            self._pending_kind = kind
            return
        if existing.session_id != window.session_id:
            self._pending = window
            self._pending_kind = kind
            return
        if existing.ladder_start_ms != window.ladder_start_ms:
            if window.ladder_start_ms > existing.ladder_start_ms:
                self._stats.pending_superseded_by_p0 += 1
            self._pending = window
            self._pending_kind = "full"
            return
        if existing.step_start_ms == window.step_start_ms:
            if window.endpoint_ts_ms > existing.endpoint_ts_ms:
                self._pending = DesiredWindow(
                    market=window.market,
                    symbol=window.symbol,
                    ladder_start_ms=existing.ladder_start_ms,
                    step_start_ms=existing.step_start_ms,
                    endpoint_ts_ms=window.endpoint_ts_ms,
                    session_id=existing.session_id,
                )
            else:
                self._stats.requests_coalesced += 1
            return
        if window.step_start_ms > existing.step_start_ms:
            self._pending = window
            self._pending_kind = "step"
            return
        self._stats.requests_coalesced += 1

    async def _kick_locked(self) -> None:
        if self._running_task is not None and not self._running_task.done():
            return
        if self._pending is None:
            self._idle_event.set()
            return
        request = self._pending
        self._pending = None
        self._pending_kind = "full"
        self._running_request = request
        self._stats.workers_launched += 1
        self._current_concurrent += 1
        self._stats.max_concurrent_workers_observed = max(
            self._stats.max_concurrent_workers_observed,
            self._current_concurrent,
        )
        self._running_task = asyncio.create_task(self._run(request), name="gf-metric-scheduler")

    async def _run(self, request: DesiredWindow) -> None:
        try:
            await self._worker_fn(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("backfill worker raised: %s", exc)
        should_idle = False
        async with self._lock:
            self._current_concurrent = max(0, self._current_concurrent - 1)
            self._last_finished_request = request
            self._running_request = None
            if self._pending is not None:
                candidate = self._pending
                self._pending = None
                self._pending_kind = "full"
                if candidate.same_material_window(request):
                    self._running_task = None
                    self._idle_event.set()
                    should_idle = True
                else:
                    self._running_request = candidate
                    self._stats.workers_launched += 1
                    self._current_concurrent += 1
                    self._stats.max_concurrent_workers_observed = max(
                        self._stats.max_concurrent_workers_observed,
                        self._current_concurrent,
                    )
                    self._running_task = asyncio.create_task(self._run(candidate), name="gf-metric-scheduler")
                    return
            else:
                self._running_task = None
                self._idle_event.set()
                should_idle = True
        if should_idle and self._on_idle is not None:
            try:
                await self._on_idle()
            except Exception:
                logger.exception("scheduler on_idle callback failed")

    async def reevaluate_after_worker(
        self,
        *,
        latest_window_provider: Callable[[], Optional[DesiredWindow]],
        installed_window_provider: Callable[[], Optional[DesiredWindow]],
    ) -> None:
        latest = latest_window_provider()
        installed = installed_window_provider()
        async with self._lock:
            self._stats.reevaluate_runs += 1
            if self._running_task is not None and self._running_task.done():
                self._running_task = None
                self._running_request = None
            if latest is None:
                return
            if installed is not None and self._window_matches(latest, installed) and self._pending is None:
                self._idle_event.set()
                return
            if (
                self._running_task is None
                and self._pending is None
                and self._last_finished_request is not None
                and latest.same_material_window(self._last_finished_request)
            ):
                self._idle_event.set()
                return
            kind = self._kind_for_window(latest, installed)
            self._absorb_request_locked(latest, kind=kind)
            await self._kick_locked()

    async def invalidate_session(self, new_session_id: str) -> None:
        async with self._lock:
            self._stats.session_invalidations += 1
            if self._pending is not None and self._pending.session_id != new_session_id:
                self._pending = None
                self._pending_kind = "full"
            if self._running_request is not None and self._running_request.session_id != new_session_id:
                # Do not cancel active work; let it finish and be rejected by
                # the controller's identity checks. Session changes are handled
                # by stale-result rejection plus latest-wins requeue.
                pass

    def reset_for_tests(self) -> None:
        self._running_task = None
        self._running_request = None
        self._pending = None
        self._pending_kind = "full"
        self._current_concurrent = 0
        self._stats = SchedulerStats()
        self._idle_event = asyncio.Event()
        self._idle_event.set()
