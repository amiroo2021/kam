"""Cross-process Fibo registration↔timer lifecycle serialization.

Invariant
---------
After every Start/Stop lifecycle operation finishes:

    (canonical effective active_count > 0)  ⇔  timer desired ACTIVE

The registration store lock alone is NOT enough: a stale
``active_count`` captured under that lock can be applied to the timer
*after* another process has mutated registrations. This module owns a
dedicated ``lifecycle.lock`` that serializes:

    mutate registration
    → re-read canonical effective active set
    → reconcile fibo-converge.timer

across processes. The lock is NEVER held around exchange I/O.

Lock path: ``${HERMES_HOME}/fibo/lifecycle.lock``
"""
from __future__ import annotations

import errno
import fcntl
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence

from ._atomic import DIR_MODE, FILE_MODE, ensure_dir_0700
from .timer_lifecycle import (
    SystemctlRunner,
    TimerReconcileResult,
    count_active_registrations,
    reconcile_convergence_timer,
)

logger = logging.getLogger(__name__)

LOCK_FILENAME = "lifecycle.lock"
_LOCK_WAIT_SECONDS = 15.0
_LOCK_POLL_INTERVAL = 0.05


class LifecycleBusy(RuntimeError):
    """Another process holds the Fibo lifecycle lock too long."""


@dataclass(frozen=True)
class LifecycleResult:
    """Outcome of a Start/Stop lifecycle mutation."""

    registration: Any
    active_count: int
    timer: TimerReconcileResult
    active_keys: tuple


def _hermes_fibo_dir(hermes_home: Optional[Path] = None) -> Path:
    if hermes_home is not None:
        return Path(hermes_home) / "fibo"
    env = os.environ.get("HERMES_HOME")
    if env and env.strip():
        return Path(env).expanduser() / "fibo"
    return Path.home() / ".hermes" / "fibo"


def lifecycle_lock_path(
    hermes_home: Optional[Path] = None,
    *,
    store: Any = None,
) -> Path:
    """Return the lock path.

    Prefer the registration store's directory (``registrations.jsonl``
    sibling) so tests that place the store outside ``HERMES_HOME/fibo``
    still serialize correctly. Fall back to ``${HERMES_HOME}/fibo``.
    """
    if store is not None:
        try:
            return Path(store.path).resolve().parent / LOCK_FILENAME
        except Exception:  # noqa: BLE001
            pass
    return _hermes_fibo_dir(hermes_home) / LOCK_FILENAME


@contextmanager
def acquire_lifecycle_lock(
    *,
    hermes_home: Optional[Path] = None,
    store: Any = None,
    wait_seconds: float = _LOCK_WAIT_SECONDS,
) -> Iterator[Path]:
    """Exclusive fcntl lock for Start/Stop + timer reconciliation.

    Blocks up to ``wait_seconds`` then raises ``LifecycleBusy``.
    """
    path = lifecycle_lock_path(hermes_home, store=store)
    parent = path.parent
    # Create parent only when missing. If it exists with a non-0700
    # mode (common in temp test dirs), do not call ensure_dir_0700 —
    # the registration store already validated its own directory.
    if not parent.exists():
        ensure_dir_0700(parent)
    if not path.exists():
        fd_create = os.open(
            str(path),
            os.O_CREAT | os.O_WRONLY,
            FILE_MODE,
        )
        os.close(fd_create)
        try:
            os.chmod(path, FILE_MODE)
        except OSError:
            pass

    fd = os.open(str(path), os.O_RDWR)
    deadline = time.monotonic() + float(wait_seconds)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LifecycleBusy(
                        f"could not acquire lifecycle lock on {path} "
                        f"within {wait_seconds}s"
                    )
                time.sleep(_LOCK_POLL_INTERVAL)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise LifecycleBusy(
                        f"could not acquire lifecycle lock on {path} "
                        f"within {wait_seconds}s"
                    ) from exc
                time.sleep(_LOCK_POLL_INTERVAL)
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def _active_snapshot(store: Any) -> tuple[int, tuple]:
    """Re-read canonical effective actives (latest-per-key + is_active)."""
    regs = list(store.load_all())
    active = [r for r in regs if getattr(r, "is_active", False)]
    keys = tuple(sorted(str(getattr(r, "registration_key", "")) for r in active))
    return len(active), keys


def run_lifecycle_mutation(
    *,
    store: Any,
    mutate_fn: Callable[[], Any],
    systemctl_runner: Optional[SystemctlRunner] = None,
    hermes_home: Optional[Path] = None,
    wait_seconds: float = _LOCK_WAIT_SECONDS,
) -> LifecycleResult:
    """Serialize registration mutation + fresh active recount + timer reconcile.

    ``mutate_fn`` must perform the store write (append / mark_stopped /
    reactivate) and may return the registration object (or a
    ``(registration, ignored_count)`` tuple from legacy store methods).
    The active count used for timer reconciliation is ALWAYS recomputed
    under this lifecycle lock *after* the mutation, never the stale
    count returned by the store method alone.
    """
    with acquire_lifecycle_lock(
        hermes_home=hermes_home, store=store, wait_seconds=wait_seconds
    ):
        raw = mutate_fn()
        registration = raw
        if isinstance(raw, tuple) and len(raw) >= 1:
            registration = raw[0]
        active_count, active_keys = _active_snapshot(store)
        timer = reconcile_convergence_timer(
            active_count,
            runner=systemctl_runner,
        )
        return LifecycleResult(
            registration=registration,
            active_count=active_count,
            timer=timer,
            active_keys=active_keys,
        )


def lifecycle_append(
    store: Any,
    registration: Any,
    *,
    systemctl_runner: Optional[SystemctlRunner] = None,
    hermes_home: Optional[Path] = None,
) -> LifecycleResult:
    """Start-Fibo path: append registration then reconcile timer."""

    def _mutate():
        # store.append returns active_count under its own lock; we
        # discard that count and re-read under lifecycle lock.
        store.append(registration)
        return registration

    return run_lifecycle_mutation(
        store=store,
        mutate_fn=_mutate,
        systemctl_runner=systemctl_runner,
        hermes_home=hermes_home,
    )


def lifecycle_reactivate(
    store: Any,
    *,
    registration_key: str,
    systemctl_runner: Optional[SystemctlRunner] = None,
    hermes_home: Optional[Path] = None,
    **reactivate_kwargs: Any,
) -> LifecycleResult:
    """Restart path: reactivate then reconcile timer."""

    def _mutate():
        out = store.reactivate(registration_key, **reactivate_kwargs)
        if isinstance(out, tuple):
            return out[0]
        return out

    return run_lifecycle_mutation(
        store=store,
        mutate_fn=_mutate,
        systemctl_runner=systemctl_runner,
        hermes_home=hermes_home,
    )


def lifecycle_mark_stopped(
    store: Any,
    registration_key: str,
    *,
    systemctl_runner: Optional[SystemctlRunner] = None,
    hermes_home: Optional[Path] = None,
    updated_at: Optional[str] = None,
) -> LifecycleResult:
    """Stop-Fibo path: mark stopped then reconcile timer."""

    def _mutate():
        kwargs = {}
        if updated_at is not None:
            kwargs["updated_at"] = updated_at
        out = store.mark_stopped(registration_key, **kwargs)
        if isinstance(out, tuple):
            return out[0]
        return out

    return run_lifecycle_mutation(
        store=store,
        mutate_fn=_mutate,
        systemctl_runner=systemctl_runner,
        hermes_home=hermes_home,
    )


__all__ = [
    "LOCK_FILENAME",
    "LifecycleBusy",
    "LifecycleResult",
    "acquire_lifecycle_lock",
    "lifecycle_lock_path",
    "run_lifecycle_mutation",
    "lifecycle_append",
    "lifecycle_reactivate",
    "lifecycle_mark_stopped",
    "count_active_registrations",
]
