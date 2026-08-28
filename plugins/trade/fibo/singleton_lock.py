"""Fibo autonomous convergence singleton lock.

Acquires a Linux ``fcntl.flock`` advisory exclusive lock on a Fibo-owned
runtime file BEFORE any TradeDesk activity. The lock prevents any second
local process (systemd timer invocation, manual run, accidental second
shell, old gateway cron, etc.) from entering TradeDesk at the same time.

Lock acquisition semantics:

  1. The lock is opened once at the start of the convergence process.
  2. ``fcntl.flock`` is invoked with ``LOCK_EX | LOCK_NB`` — exclusive,
     non-blocking. If the lock is already held by another process,
     ``BlockingIOError`` is raised immediately.
  3. The kernel releases the lock automatically when the file descriptor
     is closed (on normal exit, exception, or process crash). No manual
     unlock, no PID-file logic, no stale-lock handling required.
  4. The locked file is NEVER deleted during the process lifetime, so
     there are no deletion races.

Path: ``${HERMES_HOME}/fibo/converge.lock``. The directory is
``~/.hermes/fibo/`` which already exists and is created with mode 0700
by ``plugins.trade.fibo._atomic.ensure_dir_0700``.

This module is dependency-free (stdlib only) and may be imported from
any Fibo context. The public API is two functions:

  - ``acquire_singleton_lock()`` — returns a ``SingletonLock`` context
    manager. ``with`` block: if acquired, the body runs. If not
    acquired, the body is skipped and ``SingletonLock.acquired`` is
    False.
  - ``is_lock_held()`` — non-blocking probe; returns True iff another
    process currently holds the lock.
"""

from __future__ import annotations

import errno
import fcntl
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


LOCK_FILENAME = "converge.lock"


def _lock_path() -> Path:
    """Return the absolute path of the Fibo singleton lock file.

    Honors ``HERMES_HOME`` (defaults to ``~/.hermes``). The parent
    directory ``${HERMES_HOME}/fibo/`` must already exist; we do not
    create it here because the directory's mode-0700 invariant is
    owned by ``plugins.trade.fibo._atomic`` and is asserted at every
    write to that directory.
    """
    hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    return Path(hermes_home) / "fibo" / LOCK_FILENAME


@dataclass
class SingletonLock:
    """Result of a singleton-lock acquisition attempt.

    Attributes:
        acquired: True iff this process holds the lock.
        path: The lock-file path that was attempted.
        fd: The open file descriptor (held while the context is
            active). Released when the ``SingletonLockHolder`` exits
            its ``__exit__``. Do NOT close this manually.
        reason: When ``acquired`` is False, a human-readable reason
            ("already running" vs "lock file not creatable" etc.).
    """

    acquired: bool
    path: Path
    fd: Optional[int]
    reason: Optional[str] = None

    def __enter__(self) -> "SingletonLock":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        # The kernel automatically releases flock on close.


@contextmanager
def acquire_singleton_lock() -> Iterator[SingletonLock]:
    """Context manager that attempts to acquire the Fibo singleton lock.

    Yields a ``SingletonLock`` whose ``acquired`` field is True iff this
    process is now the unique owner. The caller is responsible for
    inspecting ``acquired`` and skipping TradeDesk activity if False.

    The lock is released automatically when the context exits (normal
    return, exception, or process crash). The file descriptor is
    closed in ``__exit__``; closing the fd releases the kernel-level
    flock.

    Failure modes:
      - Another process holds the lock → ``acquired=False``,
        ``reason="another converge_once is in progress"``. Caller
        exits cleanly.
      - The lock file cannot be opened/created → ``acquired=False``,
        ``reason`` describes the OS error. Caller exits cleanly.

    No TradeDesk or exchange call is made by this function; it is
    strictly a kernel-level file-locking primitive.
    """
    path = _lock_path()
    parent = path.parent
    # Defensive: if the parent directory does not exist (e.g., fresh
    # HERMES_HOME that has never been written to), do NOT create it
    # here. The lock invariant requires the parent to already be a
    # mode-0700 directory (this is the standard Fibo runtime
    # directory). If it's missing, that's a configuration error, and
    # we must fail closed.
    if not parent.exists():
        yield SingletonLock(
            acquired=False, path=path, fd=None,
            reason=f"parent directory does not exist: {parent}",
        )
        return
    if not parent.is_dir():
        yield SingletonLock(
            acquired=False, path=path, fd=None,
            reason=f"parent path is not a directory: {parent}",
        )
        return
    # Open the file (create if missing) with restrictive mode.
    fd: Optional[int] = None
    try:
        fd = os.open(
            str(path),
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
    except OSError as exc:
        yield SingletonLock(
            acquired=False, path=path, fd=None,
            reason=f"failed to open lock file: {exc}",
        )
        return
    # Try the lock. LOCK_EX = exclusive, LOCK_NB = non-blocking.
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        # EWOULDBLOCK / EAGAIN: another process holds the lock.
        # Any other error: also treat as "not acquired" (fail closed).
        reason = "another converge_once is in progress" if exc.errno in (
            errno.EWOULDBLOCK, errno.EAGAIN,
        ) else f"flock failed: {exc}"
        try:
            os.close(fd)
        except OSError:
            pass
        yield SingletonLock(acquired=False, path=path, fd=None, reason=reason)
        return
    # Lock acquired. Yield the holder; the fd stays open until
    # __exit__ closes it (which releases the kernel-level flock).
    holder = SingletonLock(acquired=True, path=path, fd=fd, reason=None)
    try:
        yield holder
    finally:
        # The holder's __exit__ closes the fd. We do NOT delete the
        # lock file: deleting the file while the kernel still has the
        # flock on the inode is safe (the inode persists until the
        # last fd is closed), but deleting it during a normal run is
        # unnecessary and risks a TOCTOU race if another process
        # creates a new file at the same path before this one closes.
        if holder.fd is not None:
            try:
                os.close(holder.fd)
            except OSError:
                pass
            holder.fd = None


def is_lock_held() -> bool:
    """Return True iff another process currently holds the Fibo lock.

    Non-blocking. Uses ``LOCK_EX | LOCK_NB``; on success the lock is
    immediately released. This is a probe — it does NOT actually
    acquire the lock for any meaningful duration.
    """
    path = _lock_path()
    if not path.exists():
        return False
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return True
            return False
        # We acquired the lock; release it immediately.
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


__all__ = [
    "LOCK_FILENAME",
    "SingletonLock",
    "acquire_singleton_lock",
    "is_lock_held",
]


if __name__ == "__main__":
    # Manual smoke test: ``python -m plugins.trade.fibo.singleton_lock``.
    with acquire_singleton_lock() as lock:
        if lock.acquired:
            print(f"ACQUIRED {lock.path}")
        else:
            print(f"NOT ACQUIRED: {lock.reason}")
            sys.exit(0)
        # Hold for a few seconds; a second invocation during this
        # window should observe ``acquired=False``.
        import time
        time.sleep(3)
    print("RELEASED")
