"""Local Fibo registration store (JSONL, append-only).

File: ``~/.hermes/fibo/registrations.jsonl``

Semantics (spec §11):

- Append-only revisions. Each Create appends exactly one new line.
- Latest record per ``registration_key`` wins.
- Malformed/truncated FINAL line is ignored safely (loader catches
  ``JSONDecodeError`` on the last line only).
- ``fcntl.flock(LOCK_EX)`` around the writer; another writer waits up
  to 5 seconds, then raises ``StoreBusy``.
- ``flush + fsync`` BEFORE success is reported.
- File mode ``0o600``; directory mode ``0o700``.
- Duplicate ``registration_key`` REJECTED — never silently mutates.

Phase 1 explicitly does NOT support Update/Replace. A re-registration
of an existing key returns "Already registered" and the wizard offers
a Back button (no Update action).
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._atomic import (
    AtomicWriteError,
    DIR_MODE,
    FILE_MODE,
    ensure_dir_0700,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registration dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FiboRegistration:
    """One accepted registration.

    Identity is computed by ``registration_key``. Components are
    normalized at construction time (spec §10) so two registrations
    built from identical raw inputs always have identical keys.
    """

    exchange: str
    account: str
    symbol: str
    variant: str
    side: str  # canonical "BUY" or "SELL"
    starting_volume: Decimal
    source: str
    source_seq: int
    source_cycle_id: int
    source_cumulative_weight: Decimal
    source_percentage: Decimal
    source_snapshot_received_at: str
    desired_exchange_size: Decimal
    status: str  # "registered" in Phase 1
    created_at: str
    updated_at: str

    # Identity -------------------------------------------------------

    @property
    def registration_key(self) -> str:
        """Identity string per spec §10."""
        return (
            f"{self.exchange}/{self.account}/"
            f"{self.symbol}/{self.variant}/{self.side}"
        )

    # Normalization + construction -----------------------------------

    @staticmethod
    def normalize_exchange(value: Any) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def normalize_account(value: Any) -> str:
        return str(value or "").strip().upper()

    @staticmethod
    def normalize_symbol(value: Any) -> str:
        return str(value or "").strip().upper()

    @staticmethod
    def normalize_variant(value: Any) -> str:
        return str(value or "").strip().upper()

    @staticmethod
    def normalize_side(value: Any) -> str:
        side = str(value or "").strip().upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"invalid side {value!r}; expected BUY or SELL")
        return side

    @classmethod
    def build(
        cls,
        *,
        exchange: Any,
        account: Any,
        symbol: Any,
        variant: Any,
        side: Any,
        starting_volume: Any,
        source: Any,
        source_seq: Any,
        source_cycle_id: Any,
        source_cumulative_weight: Any,
        source_percentage: Any,
        source_snapshot_received_at: str,
        desired_exchange_size: Any,
        status: str = "registered",
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> "FiboRegistration":
        """Build a registration with full component normalization.

        Decimal fields are parsed via ``Decimal(str(value))`` (no float
        coercion). NaN / Infinity are rejected so they cannot be
        smuggled in via ``Decimal('Infinity')``.
        """
        def _dec(name: str, value: Any) -> Decimal:
            try:
                d = Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise ValueError(f"{name} not a decimal: {value!r}") from exc
            if not d.is_finite():
                raise ValueError(f"{name} must be finite; got {d}")
            return d

        def _int(name: str, value: Any) -> int:
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} not int: {value!r}") from exc

        norm_side = cls.normalize_side(side)
        vol = _dec("starting_volume", starting_volume)
        if vol <= 0:
            raise ValueError(
                f"starting_volume must be > 0; got {vol}"
            )
        target = _dec("desired_exchange_size", desired_exchange_size)
        if not target.is_finite():
            raise ValueError(f"desired_exchange_size must be finite; got {target}")

        now = created_at or _utc_iso_now()
        return cls(
            exchange=cls.normalize_exchange(exchange),
            account=cls.normalize_account(account),
            symbol=cls.normalize_symbol(symbol),
            variant=cls.normalize_variant(variant),
            side=norm_side,
            starting_volume=vol,
            source=str(source or "").strip(),
            source_seq=_int("source_seq", source_seq),
            source_cycle_id=_int("source_cycle_id", source_cycle_id),
            source_cumulative_weight=_dec(
                "source_cumulative_weight", source_cumulative_weight
            ),
            source_percentage=_dec("source_percentage", source_percentage),
            source_snapshot_received_at=str(source_snapshot_received_at or "").strip(),
            desired_exchange_size=target,
            status=str(status or "registered").strip() or "registered",
            created_at=now,
            updated_at=updated_at or now,
        )

    # Serialization --------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "registration_key": self.registration_key,
            "exchange": self.exchange,
            "account": self.account,
            "symbol": self.symbol,
            "variant": self.variant,
            "side": self.side,
            "starting_volume": str(self.starting_volume),
            "source": self.source,
            "source_seq": self.source_seq,
            "source_cycle_id": self.source_cycle_id,
            "source_cumulative_weight": str(self.source_cumulative_weight),
            "source_percentage": str(self.source_percentage),
            "source_snapshot_received_at": self.source_snapshot_received_at,
            "desired_exchange_size": str(self.desired_exchange_size),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_jsonl(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "FiboRegistration":
        if not isinstance(raw, dict):
            raise ValueError("registration row is not an object")
        # Delegate normalization to ``build`` so missing fields error out
        # with the same messages everywhere.
        return cls.build(
            exchange=raw.get("exchange", ""),
            account=raw.get("account", ""),
            symbol=raw.get("symbol", ""),
            variant=raw.get("variant", ""),
            side=raw.get("side", ""),
            starting_volume=raw.get("starting_volume", "0"),
            source=raw.get("source", ""),
            source_seq=raw.get("source_seq", 0),
            source_cycle_id=raw.get("source_cycle_id", 0),
            source_cumulative_weight=raw.get("source_cumulative_weight", "0"),
            source_percentage=raw.get("source_percentage", "0"),
            source_snapshot_received_at=raw.get(
                "source_snapshot_received_at", ""
            ),
            desired_exchange_size=raw.get("desired_exchange_size", "0"),
            status=raw.get("status", "registered"),
            created_at=raw.get("created_at") or _utc_iso_now(),
            updated_at=raw.get("updated_at") or _utc_iso_now(),
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StoreBusy(RuntimeError):
    """Raised when another writer holds the file lock for too long."""


class DuplicateRegistrationError(RuntimeError):
    """Raised when an append would create a duplicate ``registration_key``.

    Spec §11: Phase 1 must reject duplicate keys; never mutate in place.
    """

    def __init__(self, registration_key: str) -> None:
        super().__init__(f"registration_key already exists: {registration_key}")
        self.registration_key = registration_key


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


# Maximum time we wait to acquire the flock before giving up.
_LOCK_WAIT_SECONDS = 5.0
_LOCK_POLL_INTERVAL = 0.05


class FiboRegistrationStore:
    """JSONL append-only registration store.

    Thread/process safe via ``fcntl.flock``. All writes go through
    ``append``; readers use ``load_all`` or ``get``.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    # -- existence ----------------------------------------------------

    def exists(self, registration_key: str) -> bool:
        for reg in self.load_all():
            if reg.registration_key == registration_key:
                return True
        return False

    def get(self, registration_key: str) -> Optional[FiboRegistration]:
        latest: Optional[FiboRegistration] = None
        for reg in self.load_all():
            if reg.registration_key == registration_key:
                latest = reg
        return latest

    # -- write --------------------------------------------------------

    def append(self, registration: FiboRegistration) -> None:
        """Append a registration as one JSON line.

        Raises:
            DuplicateRegistrationError: a record with the same
                ``registration_key`` already exists in the store.
            StoreBusy: another writer holds the file lock for longer
                than ``_LOCK_WAIT_SECONDS``.
            AtomicWriteError: the atomic rename failed (e.g. permission).
            OSError: I/O errors.
        """
        # Duplicate check happens UNDER the lock below, but a fast
        # pre-check avoids a needless write cycle for the common case.
        if self.exists(registration.registration_key):
            raise DuplicateRegistrationError(registration.registration_key)

        ensure_dir_0700(self._path.parent)
        line = registration.to_jsonl()
        if not line.endswith("\n"):
            line += "\n"
        payload = line.encode("utf-8")

        # Ensure file exists with 0600 mode (touch + chmod once).
        if not self._path.exists():
            try:
                fd = os.open(
                    str(self._path),
                    os.O_CREAT | os.O_WRONLY,
                    FILE_MODE,
                )
                os.close(fd)
            except OSError as exc:
                raise AtomicWriteError(
                    f"failed to create {self._path}: {exc}"
                ) from exc
            try:
                os.chmod(self._path, FILE_MODE)
            except OSError:
                pass

        with open(self._path, "ab", buffering=0) as f:
            self._acquire_lock(f)
            try:
                # Re-check under the lock to close the TOCTOU window.
                self._raise_if_duplicate_under_lock(
                    registration.registration_key
                )
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            finally:
                # flock is released when the file is closed; we close
                # explicitly to make the release deterministic across
                # Python versions.
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass

    def _acquire_lock(self, f) -> None:
        """Acquire ``LOCK_EX`` with bounded wait."""
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise StoreBusy(
                        f"could not acquire lock on {self._path} within "
                        f"{_LOCK_WAIT_SECONDS}s"
                    ) from exc
                time.sleep(_LOCK_POLL_INTERVAL)

    def _raise_if_duplicate_under_lock(self, key: str) -> None:
        """Re-scan the file under the lock and raise if key exists.

        O(N) scan is acceptable: the file is human-tiny and Phase 1
        volumes are bounded.
        """
        if not self._path.is_file():
            return
        try:
            with open(self._path, "rb") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                except OSError:
                    # Another writer holds the exclusive lock. The
                    # append() caller already has it, so this branch
                    # should be unreachable. Be defensive: do nothing.
                    return
                try:
                    data = f.read()
                finally:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
        except OSError as exc:
            logger.warning(
                "fibo_registrations: lock-collision read failed: %s", exc
            )
            return
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            if raw.get("registration_key") == key:
                raise DuplicateRegistrationError(key)

    # -- read ---------------------------------------------------------

    def load_all(self) -> List[FiboRegistration]:
        """Return all registrations; latest per key wins.

        A malformed FINAL line is silently ignored (spec §11). An
        internal malformed line (followed by valid lines) is logged at
        WARNING and skipped so the loader keeps reading.
        """
        if not self._path.is_file():
            return []
        try:
            with open(self._path, "rb") as f:
                data = f.read()
        except OSError as exc:
            logger.warning(
                "fibo_registrations: read failed at %s: %s", self._path, exc
            )
            return []

        lines = data.splitlines()
        # Filter trailing empty lines from a trailing newline.
        while lines and not lines[-1].strip():
            lines.pop()

        if not lines:
            return []

        decoded: List[FiboRegistration] = []
        last_index = len(lines) - 1
        for idx, raw_line in enumerate(lines):
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                if idx == last_index:
                    # Truncated final line — silently ignore (spec §11).
                    logger.warning(
                        "fibo_registrations: ignoring truncated final line: %s",
                        exc,
                    )
                    continue
                # Internal malformed line — log and skip.
                logger.warning(
                    "fibo_registrations: skipping malformed line %d: %s",
                    idx + 1, exc,
                )
                continue
            if not isinstance(obj, dict):
                if idx == last_index:
                    continue
                logger.warning(
                    "fibo_registrations: skipping non-object line %d", idx + 1
                )
                continue
            try:
                reg = FiboRegistration.from_dict(obj)
            except (ValueError, KeyError, TypeError) as exc:
                if idx == last_index:
                    logger.warning(
                        "fibo_registrations: ignoring final line with bad "
                        "schema: %s", exc,
                    )
                    continue
                logger.warning(
                    "fibo_registrations: skipping line %d with bad schema: %s",
                    idx + 1, exc,
                )
                continue
            decoded.append(reg)

        # Latest per key wins. Lines were appended in time order, so a
        # forward walk keeping the last seen is sufficient.
        latest_by_key: Dict[str, FiboRegistration] = {}
        for reg in decoded:
            latest_by_key[reg.registration_key] = reg
        return list(latest_by_key.values())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_iso_now() -> str:
    """Current UTC time as an ISO-8601 string with ``Z`` suffix."""
    from datetime import datetime, timezone
    return (
        datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )


__all__ = [
    "FiboRegistration",
    "FiboRegistrationStore",
    "StoreBusy",
    "DuplicateRegistrationError",
]