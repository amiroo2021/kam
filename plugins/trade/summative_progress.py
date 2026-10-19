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

from typing import Any, Callable, Dict, List, Optional

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
    ) -> None:
        self._query = query
        self._render = render
        self._metadata = metadata or {}
        self.frames: List[ProgressFrame] = []
        self._last_text: Optional[str] = None
        self._loop: Optional[Any] = None

    def __call__(self, frame: ProgressFrame) -> None:
        self.frames.append(dict(frame))
        text = self._render(frame)
        if text == self._last_text:
            return
        self._last_text = text
        self._schedule_edit(text)

    # The progress callback runs on a worker thread (asyncio.to_thread); the
    # python-telegram-bot ``edit_message_text`` coroutine must be scheduled
    # back on the event loop.  We capture the loop the first time we see
    # it and use ``call_soon_threadsafe`` to submit edits.

    def _schedule_edit(self, text: str) -> None:
        try:
            import asyncio  # local import keeps this module transport-free
            loop = asyncio.get_event_loop()
        except Exception:  # noqa: BLE001
            return
        if not loop.is_running():
            return
        coro = self._safe_edit(text)
        try:
            loop.call_soon_threadsafe(asyncio.ensure_future, coro)
        except Exception:  # noqa: BLE001
            return

    async def _safe_edit(self, text: str) -> None:
        query = self._query
        try:
            await query.edit_message_text(text=text, reply_markup=None)
        except Exception:  # noqa: BLE001
            # Editing the same message rapidly can race with itself; ignore.
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