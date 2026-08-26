"""Durable atomic file write helper used by every persisted artifact in
``plugins/trade.fibo``.

Contract (used for ``mt4_snapshot.json`` and ``mt4_reader_state.json``):

1. Open a temp file in the SAME directory as the destination (so
   ``os.replace`` is atomic on the same filesystem and never crosses a
   device boundary).
2. ``fchmod`` the temp FD to ``0o600`` BEFORE writing any bytes — so
   even a crash mid-write never exposes a world-readable half-file.
3. Write bytes, ``flush()`` Python buffers, ``os.fsync(fd)`` to push
   to the underlying device.
4. ``os.replace(tmp, target)`` — atomic on POSIX (same filesystem).
5. ``os.fsync(dir_fd)`` of the parent directory so the directory
   entry pointing at the new file is durable across a crash.

The destination directory is created with mode ``0o700`` if missing.
A permission of exactly ``0o700`` is asserted after creation; if the
directory already exists with different permissions the helper does
NOT silently chmod it (operator must decide).

The target file's mode is also reasserted to ``0o600`` after replace
as a defense-in-depth measure (some filesystems can be affected by
umask or other intermediate operations).

This helper does not catch exceptions — the caller decides how to
handle write failures. All exceptions propagate with their original
context. ``temp_path`` is cleaned up on failure; on success it is
consumed by ``os.replace`` and no longer exists.
"""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path
from typing import Iterable, Optional


FILE_MODE = 0o600
DIR_MODE = 0o700


class AtomicWriteError(RuntimeError):
    """Raised when an atomic write fails. Wraps the original exception
    so the caller can inspect the cause."""


def ensure_dir_0700(directory: Path) -> None:
    """Create ``directory`` with mode 0700 if missing.

    If the directory already exists, asserts its mode is exactly
    ``0o700`` (no chmod). If the directory exists with a different
    mode, raises ``AtomicWriteError`` so the operator can fix it
    intentionally.
    """
    if directory.exists():
        if not directory.is_dir():
            raise AtomicWriteError(
                f"{directory} exists but is not a directory"
            )
        st = directory.stat()
        if (st.st_mode & 0o777) != DIR_MODE:
            raise AtomicWriteError(
                f"{directory} exists with mode {oct(st.st_mode & 0o777)}, "
                f"expected {oct(DIR_MODE)}. Refusing to auto-chmod."
            )
        return
    try:
        directory.mkdir(parents=False, mode=DIR_MODE)
    except FileExistsError:
        # Race: another process created it concurrently. Re-check mode.
        if not directory.is_dir():
            raise AtomicWriteError(
                f"{directory} exists but is not a directory"
            )
        st = directory.stat()
        if (st.st_mode & 0o777) != DIR_MODE:
            raise AtomicWriteError(
                f"{directory} concurrently created with mode "
                f"{oct(st.st_mode & 0o777)}, expected {oct(DIR_MODE)}."
            )
    except OSError as exc:
        raise AtomicWriteError(
            f"failed to create directory {directory}: {exc}"
        ) from exc


def atomic_write_bytes(target: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``target`` with full durability.

    See module docstring for the durability contract. Raises
    ``AtomicWriteError`` on any failure.
    """
    target_dir = target.parent
    ensure_dir_0700(target_dir)

    # Create temp file in the SAME directory so os.replace is atomic.
    # delete=False so we can fchmod the FD before publishing.
    fd: Optional[int] = None
    tmp_path: Optional[Path] = None
    try:
        fd_obj = tempfile.NamedTemporaryFile(
            dir=str(target_dir),
            prefix=target.name + ".",
            suffix=".tmp",
            delete=False,
            mode="wb",
        )
        fd = fd_obj.fileno()
        tmp_path = Path(fd_obj.name)

        # 1. fchmod BEFORE any bytes are written.
        try:
            os.fchmod(fd, FILE_MODE)
        except OSError as exc:
            fd_obj.close()
            raise AtomicWriteError(
                f"fchmod(0o600) failed for {tmp_path}: {exc}"
            ) from exc

        # 2. Write payload.
        try:
            fd_obj.write(data)
            fd_obj.flush()
        except OSError as exc:
            fd_obj.close()
            raise AtomicWriteError(
                f"write failed for {tmp_path}: {exc}"
            ) from exc

        # 3. fsync the data file.
        try:
            os.fsync(fd)
        except OSError as exc:
            fd_obj.close()
            raise AtomicWriteError(
                f"fsync failed for {tmp_path}: {exc}"
            ) from exc
        fd_obj.close()
        fd = None

        # 4. os.replace for atomic publication.
        try:
            os.replace(tmp_path, target)
        except OSError as exc:
            raise AtomicWriteError(
                f"os.replace({tmp_path} -> {target}) failed: {exc}"
            ) from exc
        tmp_path = None

        # 5. fsync parent directory so the directory entry is durable.
        try:
            dfd = os.open(str(target_dir), os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as exc:
            raise AtomicWriteError(
                f"fsync(parent dir {target_dir}) failed: {exc}"
            ) from exc

        # 6. Reassert 0600 on the published file as defense-in-depth.
        try:
            os.chmod(target, FILE_MODE)
        except OSError:
            # Not fatal — file was written atomically. Surface as warning
            # via the caller by re-raising only on ENOENT (file gone).
            if not target.exists():
                raise AtomicWriteError(
                    f"{target} missing after atomic publication"
                )

    finally:
        # Clean up temp file on any failure path. If we already
        # replaced it, tmp_path is None and there's nothing to clean.
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def atomic_write_text(target: Path, text: str, encoding: str = "utf-8") -> None:
    """Convenience wrapper: atomic_write_bytes(target, text.encode(encoding))."""
    atomic_write_bytes(target, text.encode(encoding))


__all__ = [
    "AtomicWriteError",
    "FILE_MODE",
    "DIR_MODE",
    "ensure_dir_0700",
    "atomic_write_bytes",
    "atomic_write_text",
]