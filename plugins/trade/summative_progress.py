"""Transport-agnostic bridge between the Summative Report and chat adapters.

The wizard emits progress frames through a sink installed on
``state.report['progress_sink']``.  This module provides:

* :class:`TelegramProgressSink` — a sink that calls ``query.edit_message_text``
  for every progress frame so the user sees an in-place progress bar during
  multi-account fetches.  The wizard itself stays Telegram-free; this class
  is the only place that imports python-telegram-bot types.

* :func:`await_progress` — a small helper adapters can use to keep a sink
  feedable from a background worker thread (e.g. ``asyncio.to_thread``).

The bridge is intentionally minimal: it never calls exchange writes, never
overrides the report rendering, and never assumes a particular threading
model.  Adapters that don't render progress can simply omit the sink and
rely on the default in-memory :class:`ProgressCollector`.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

ProgressFrame = Dict[str, Any]


class TelegramProgressSink:
    """Render :class:`ProgressFrame` objects via Telegram's editMessageText.

    Usage::

        sink = TelegramProgressSink(
            query=query,           # python-telegram-bot CallbackQuery
            render=lambda frame: frame["text"],
            metadata=metadata,     # optional adapter metadata for thread routing
        )
        state.report["progress_sink"] = sink
        screen = await asyncio.to_thread(wizard.handle_callback, chat_key, suffix)

    The sink fires ``render(frame)`` for every progress frame and writes
    the result to the same message that the wizard's final view will
    replace.  Failures are swallowed so a flaky network never breaks the
    fetch.
    """

    def __init__(
        self,
        *,
        query: Any,
        render: Callable[[ProgressFrame], str],
        metadata: Optional[Dict[str, Any]] = None,
        loop: Optional[Any] = None,
    ) -> None:
        self._query = query
        self._render = render
        self._metadata = metadata or {}
        self.frames: List[ProgressFrame] = []
        self._last_text: Optional[str] = None
        self._lifecycle: str = "fetching"  # fetching | finalizing | completed
        # Single-writer lock so progress edits never race each other.
        self._lock: Optional[Any] = None
        # Capture the running event loop on the calling thread (which is
        # the gateway loop, since the sink is constructed from
        # handle_trade_callback).  When invoked from a worker thread,
        # asyncio.get_event_loop() is deprecated and unreliable; the
        # captured loop is the correct one.
        self._loop: Optional[Any] = loop
        if self._loop is None:
            try:
                import asyncio  # local import keeps this module transport-free
                self._loop = asyncio.get_running_loop()
            except Exception:  # noqa: BLE001
                self._loop = None

    def __call__(self, frame: ProgressFrame) -> None:
        # Lifecycle gate.  Once the dispatcher enters FINALIZING / COMPLETED
        # we drop every further frame so a stale progress edit cannot
        # overwrite the rendered dashboard.
        if self._lifecycle != "fetching":
            self.frames.append(dict(frame))
            return
        self.frames.append(dict(frame))
        text = self._render(frame)
        if text == self._last_text:
            return
        self._last_text = text
        self._schedule_edit(text)

    def begin_finalizing(self) -> None:
        """Stop accepting progress edits.  Idempotent."""
        if self._lifecycle == "fetching":
            self._lifecycle = "finalizing"
            logger.debug(
                "summative_progress.sink begin_finalizing frames=%d", len(self.frames)
            )

    def complete(self) -> None:
        """Mark the sink as done.  No further edits will ever be sent."""
        if self._lifecycle != "completed":
            self._lifecycle = "completed"

    @property
    def lifecycle(self) -> str:
        return self._lifecycle

    # The progress callback runs on a worker thread (asyncio.to_thread); the
    # python-telegram-bot edit_message_text coroutine must be scheduled
    # back on the event loop.  We use the loop captured in __init__.

    def _schedule_edit(self, text: str) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            running = loop.is_running()
        except Exception:  # noqa: BLE001
            return
        if not running:
            return
        try:
            import asyncio
            loop.call_soon_threadsafe(
                asyncio.ensure_future, self._queued_edit(text)
            )
        except Exception:  # noqa: BLE001
            return

    async def _queued_edit(self, text: str) -> None:
        """Serialize every edit for this message through a single writer.

        Without the lock a scheduled progress edit and the dispatcher's
        final dashboard edit can both be in flight; Telegram then applies
        them in arbitrary order and the dashboard is lost.
        """
        import asyncio
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            # Re-check the lifecycle inside the lock: the dispatcher may
            # have flipped to finalizing/completed while we waited.
            if self._lifecycle != "fetching":
                return
            await self._safe_edit(text)

    async def _safe_edit(self, text: str) -> None:
        try:
            await self._query.edit_message_text(text=text, reply_markup=None)
        except Exception as exc:  # noqa: BLE001
            # Editing the same message rapidly can race with itself; log
            # the Telegram error so real failures are visible.
            logger.warning(
                "summative.trace sink_edit_failed lifecycle=%s error_type=%s error=%s",
                self._lifecycle, type(exc).__name__, exc,
            )
            return


class ConsoleProgressSink:
    """Sink that prints frames to stdout — useful for tests and CLIs."""

    def __init__(self) -> None:
        self.frames: List[ProgressFrame] = []

    def __call__(self, frame: ProgressFrame) -> None:
        self.frames.append(dict(frame))
        try:
            print(frame["text"], flush=True)
        except Exception:  # noqa: BLE001
            pass


def await_progress(
    predicate: Callable[[], bool],
    *,
    timeout: float = 5.0,
    poll_interval: float = 0.05,
) -> bool:
    """Block until ``predicate()`` returns true (used by sync tests)."""
    import time
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(max(0.0, poll_interval))
    return predicate()


__all__ = [
    "TelegramProgressSink",
    "ConsoleProgressSink",
    "await_progress",
]