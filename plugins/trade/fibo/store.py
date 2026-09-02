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

import contextlib
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

    Phase 2.1 identity split (source vs exchange):

    * ``source_symbol`` — the symbol used to look up the MT4 entry
      in the latest ``mt4_snapshot.json``. Same spelling as the
      Observer publishes (e.g. ``"ETHUSD"``).
    * ``exchange_instrument`` — the venue's contract identifier
      (e.g. ``"ETH-USD.P"`` on Ondo). Used for exchange reads. Empty
      for legacy records created before Phase 2.1 — those need
      ``NEEDS_INSTRUMENT_SELECTION``.
    * ``symbol`` is preserved as an alias for ``source_symbol`` in
      the on-disk JSONL record so Phase 1 readers keep working.

    The ``registration_key`` is:
        ``exchange/account/exchange_instrument/variant/side`` if
        ``exchange_instrument`` is set,
        else ``exchange/account/source_symbol/variant/side``
        (legacy compat — preserves the on-disk identity of
        pre-Phase-2.1 records).
    """

    exchange: str
    account: str
    symbol: str  # kept for JSONL backward compat (= source_symbol)
    source_symbol: str
    exchange_instrument: str  # empty for legacy records
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
        """Identity string. When ``exchange_instrument`` is set, it
        replaces ``source_symbol`` in the key. For legacy records
        (empty ``exchange_instrument``) the key falls back to the
        pre-Phase-2.1 form so the on-disk identity is preserved.
        """
        venue_token = self.exchange_instrument or self.source_symbol
        return (
            f"{self.exchange}/{self.account}/"
            f"{venue_token}/{self.variant}/{self.side}"
        )

    @property
    def is_legacy(self) -> bool:
        """True when the record was created before Phase 2.1 and
        therefore has no ``exchange_instrument`` set.

        The reconciler must classify such records as
        ``NEEDS_INSTRUMENT_SELECTION`` and never call the venue.
        """
        return not self.exchange_instrument

    @property
    def is_stopped(self) -> bool:
        """True when ``status == 'stopped'``.

        Stopped registrations are persisted for audit but the
        reconciler / Running screen / Stop picker exclude them.
        Phase 2.6: only "registered" and "stopped" are recognized;
        any other value is treated as active (legacy back-compat).
        """
        return (self.status or "").strip().lower() == "stopped"

    @property
    def is_active(self) -> bool:
        """True when this registration should still be considered
        running for reconciliation / Stop choices.

        A registration is active when ``status`` is NOT
        ``"stopped"``. The default ``status="registered"`` counts
        as active. Unknown values are treated as active (legacy
        back-compat).
        """
        return not self.is_stopped

    # Normalization + construction -----------------------------------

    @staticmethod
    def normalize_exchange(value: Any) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def normalize_account(value: Any) -> str:
        return str(value or "").strip().upper()

    @staticmethod
    def normalize_symbol(value: Any) -> str:
        """Normalize a SYMBOL identifier (source or legacy ``symbol``).

        Source symbols are MT4-side identifiers (e.g. ``"ETHUSD"``).
        They are case-insensitive in matching, so we uppercase them
        for stable identity.

        For EXCHANGE contract identifiers (e.g. ``"ETH-USD.P"``),
        use ``normalize_exchange_instrument`` — these are case-
        sensitive in some venues and must be passed through verbatim.
        """
        return str(value or "").strip().upper()

    @staticmethod
    def normalize_exchange_instrument(value: Any) -> str:
        """Normalize an EXCHANGE INSTRUMENT (venue contract id).

        Passes through case and punctuation. Venue contracts like
        ``"ETH-USD.P"`` must match exactly what the venue returns.
        We only trim whitespace.
        """
        return str(value or "").strip()

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
        source_symbol: Optional[Any] = None,
        exchange_instrument: Optional[Any] = None,
        status: str = "registered",
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> "FiboRegistration":
        """Build a registration with full component normalization.

        Decimal fields are parsed via ``Decimal(str(value))`` (no float
        coercion). NaN / Infinity are rejected so they cannot be
        smuggled in via ``Decimal('Infinity')``.

        ``source_symbol`` defaults to ``symbol`` for backward compat.
        ``exchange_instrument`` is optional; an empty string means
        "legacy record, must be classified NEEDS_INSTRUMENT_SELECTION".
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

        norm_symbol = cls.normalize_symbol(symbol)
        norm_source_symbol = (
            cls.normalize_symbol(source_symbol)
            if source_symbol is not None
            else norm_symbol
        )
        # exchange_instrument: pass through verbatim. We do NOT
        # upper-case it (venue contract identifiers are
        # case-sensitive in some exchanges).
        if exchange_instrument is None:
            norm_instrument = ""
        else:
            norm_instrument = cls.normalize_exchange_instrument(
                exchange_instrument
            )
        if norm_instrument == "" and norm_source_symbol == "":
            raise ValueError(
                "registration must have at least one of source_symbol or "
                "exchange_instrument"
            )

        # Phase 2.7.1 — timestamp semantics fix:
        # Capture the current timestamp ONCE per build so both
        # ``created_at`` and ``updated_at`` defaults derive from
        # the same instant. Initial registrations (both fields
        # ``None``) get ``created_at == updated_at == current_time``.
        # Status transitions (``mark_stopped``, ``reactivate``) pass
        # the original ``created_at`` to preserve historical
        # identity, and the transition-time ``updated_at`` defaults
        # to ``current_time`` so it refreshes.
        # Explicit ``updated_at`` overrides both defaults.
        current_time = _utc_iso_now()
        effective_created_at = created_at or current_time
        effective_updated_at = updated_at or current_time
        return cls(
            exchange=cls.normalize_exchange(exchange),
            account=cls.normalize_account(account),
            symbol=norm_symbol,
            source_symbol=norm_source_symbol,
            exchange_instrument=norm_instrument,
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
            created_at=effective_created_at,
            updated_at=effective_updated_at,
        )

    # Serialization --------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "registration_key": self.registration_key,
            "exchange": self.exchange,
            "account": self.account,
            "symbol": self.symbol,
            "source_symbol": self.source_symbol,
            "exchange_instrument": self.exchange_instrument,
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
        # source_symbol is the new key. If a legacy record has only
        # ``symbol``, copy that into source_symbol so the schema is
        # uniform post-load.
        source_symbol = raw.get("source_symbol", "") or raw.get("symbol", "")
        exchange_instrument = raw.get("exchange_instrument", "")
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
            source_symbol=source_symbol,
            exchange_instrument=exchange_instrument,
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

    def _latest_status_for(
        self, registration_key: str,
    ) -> Optional[str]:
        """Return the effective status of the latest row for the key.

        Phase 2.6/2.7 transition rules: both directions of
        ``registered <-> stopped`` are allowed. Same-status
        duplicate appends raise ``DuplicateRegistrationError``.

        * latest row is ``status="registered"`` → return
          ``"registered"`` (a transition to ``"stopped"`` is
          allowed; any further ``"registered"`` append is a true
          duplicate).
        * latest row is ``status="stopped"`` → return ``"stopped"``
          (a transition to ``"registered"`` is allowed via
          ``reactivate``; any further ``"stopped"`` append is a true
          duplicate).
        * no row exists → return ``None``.

        Used by ``append`` to detect status transitions.
        """
        latest_status: Optional[str] = None
        for reg in self.load_all():
            if reg.registration_key == registration_key:
                latest_status = reg.status
        return latest_status

    def get(self, registration_key: str) -> Optional[FiboRegistration]:
        latest: Optional[FiboRegistration] = None
        for reg in self.load_all():
            if reg.registration_key == registration_key:
                latest = reg
        return latest

    # -- write --------------------------------------------------------

    def append(self, registration: FiboRegistration) -> int:
        """Append a registration as one JSON line.

        Allowed transitions:

        * ``registered -> stopped`` (Phase 2.6 ``mark_stopped``).
        * Same-status appends (e.g. ``registered -> registered``
          or ``stopped -> stopped``) raise
          ``DuplicateRegistrationError``.
        * ``stopped -> registered`` is NOT allowed via plain
          ``append`` — the canonical reactivation path is
          ``reactivate(...)`` which performs identity validation
          and a controlled status transition. Calling ``append``
          directly to reactivate would skip that identity check.

        Raises:
            DuplicateRegistrationError: a record with the same
                ``registration_key`` AND the same ``status`` already
                exists in the store (latest-per-key), OR the new
                row's status is ``"registered"`` while the existing
                latest row is ``"stopped"`` (use ``reactivate``
                instead).
            StoreBusy: another writer holds the file lock for longer
                than ``_LOCK_WAIT_SECONDS``.
            AtomicWriteError: the atomic rename failed (e.g. permission).
            OSError: I/O errors.
        """
        # Duplicate check happens UNDER the same exclusive lock
        # acquisition as the write. This closes the TOCTOU
        # window where two concurrent writers could both observe
        # the same latest-status and both proceed to write.
        with self._open_and_lock() as f:
            latest_status = self._raw_latest_status_under_lock(
                f, registration.registration_key
            )
            last = (
                (latest_status or "").strip().lower()
                if latest_status is not None else None
            )
            new = (registration.status or "").strip().lower()
            if latest_status is not None:
                if last == new:
                    # Same-status duplicate: blocked.
                    raise DuplicateRegistrationError(
                        registration.registration_key
                    )
                if last == "stopped" and new == "registered":
                    # Plain append is forbidden from reactivating;
                    # the caller must use the canonical ``reactivate``
                    # path.
                    raise DuplicateRegistrationError(
                        registration.registration_key
                    )
            # Re-check immediately before write (still under lock)
            # to defeat any racing writer that slipped in between
            # the pre-check and the write on the OLD non-locking
            # implementation. This is the authoritative guard.
            recheck_status = self._raw_latest_status_under_lock(
                f, registration.registration_key
            )
            recheck_last = (
                (recheck_status or "").strip().lower()
                if recheck_status is not None else None
            )
            if recheck_status is not None:
                if recheck_last == new:
                    raise DuplicateRegistrationError(
                        registration.registration_key
                    )
                if recheck_last == "stopped" and new == "registered":
                    raise DuplicateRegistrationError(
                        registration.registration_key
                    )
            self._write_under_lock(f, registration)
            return self._count_active_under_lock(f)

    def _write_under_lock(self, f, registration: FiboRegistration) -> None:
        """Atomic-write a registration row to the JSONL file.

        The caller MUST already hold the exclusive file lock
        (acquired via ``_open_and_lock`` or ``_acquire_lock``).
        This primitive only does the write + fsync; it does NOT
        acquire the lock and does NOT validate. It exists so
        ``append`` / ``mark_stopped`` / ``reactivate`` can compose
        read+validate+write under a single lock acquisition.

        Raises:
            OSError: I/O errors.
        """
        line = registration.to_jsonl()
        if not line.endswith("\n"):
            line += "\n"
        payload = line.encode("utf-8")

        f.write(payload)
        f.flush()
        os.fsync(f.fileno())

    @contextlib.contextmanager
    def _open_and_lock(self):
        """Open the JSONL file in append+read mode and acquire the
        exclusive file lock for the duration of the ``yield``.

        The caller MUST use ``_write_under_lock``, ``_read_*``, and
        ``_seek_and_read_*`` primitives that ASSUME the lock is
        held. The lock is released deterministically when the
        ``with`` block exits.

        Yields:
            open file handle in append+read binary mode.

        Raises:
            StoreBusy: another writer holds the file lock too long.
            AtomicWriteError: file-create / chmod failed.
            OSError: I/O errors.
        """
        ensure_dir_0700(self._path.parent)
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

        with open(self._path, "ab+", buffering=0) as f:
            self._acquire_lock(f)
            try:
                yield f
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass

    def _read_latest_under_lock(
        self, f, registration_key: str,
    ) -> Optional[FiboRegistration]:
        """Return the latest parsed ``FiboRegistration`` for
        ``registration_key`` while holding the exclusive lock.

        Reads the file from offset 0 (the writer's current EOF is
        not necessarily the reader's EOF for append+read). The
        file size at lock time determines how much we scan; lines
        are JSON-decoded in memory.

        Returns ``None`` if no row exists with that key, or if the
        file does not yet exist (caller should treat as
        initial-create case).
        """
        try:
            f.seek(0, os.SEEK_SET)
            data = f.read()
        except OSError as exc:
            logger.warning(
                "fibo_registrations: lock-held read failed: %s", exc
            )
            return None
        latest_status: Optional[str] = None
        latest_reg: Optional[FiboRegistration] = None
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            if raw.get("registration_key") != registration_key:
                continue
            latest_status = str(raw.get("status", "") or "")
            # Re-parse the full registration row from the JSON.
            try:
                latest_reg = FiboRegistration.from_dict(raw)
            except Exception:  # noqa: BLE001 - bad row is skipped
                continue
        # latest_reg is the parsed row whose status is the latest
        # status. We may have hit a malformed row and skipped it,
        # but latest_status tracks the raw status (raw is what
        # the duplicate-check rule compares against).
        # We keep latest_reg if present; otherwise the caller may
        # fall back to identity validation via _raw_latest_payload.
        del latest_status
        return latest_reg

    def _raw_latest_status_under_lock(
        self, f, registration_key: str,
    ) -> Optional[str]:
        """Return the latest ``status`` string for
        ``registration_key`` while holding the exclusive lock.

        Used by ``append`` for the fast duplicate-check rule.
        Always returns the raw status (a malformed row's
        ``status`` is still returned as the latest). The caller
        treats ``None`` as "no row exists".
        """
        try:
            f.seek(0, os.SEEK_SET)
            data = f.read()
        except OSError as exc:
            logger.warning(
                "fibo_registrations: lock-held read failed: %s", exc
            )
            return None
        latest_status: Optional[str] = None
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            if raw.get("registration_key") != registration_key:
                continue
            latest_status = str(raw.get("status", "") or "")
        return latest_status

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


    # -- status transitions ------------------------------------------

    def mark_stopped(
        self,
        registration_key: str,
        *,
        updated_at: Optional[str] = None,
    ) -> tuple:
        """Append a "stopped" status transition row for ``registration_key``.

        The store is append-only JSONL with "latest per key wins"
        semantics (``load_all``). Appending a new row that preserves
        every other field but sets ``status="stopped"`` is the
        canonical way to record the transition without rewriting
        historical JSONL rows.

        The original row remains in the file (audit history). The
        latest row with this key — now with ``status="stopped"`` —
        becomes the effective state visible to ``get``,
        ``load_all``, and the reconciler.

        Phase 2.6 scope: only Stop is implemented. Resume is
        deferred to a later phase.

        Returns:
            ``(stopped_registration, active_count)`` where
            ``active_count`` is computed under the same lock
            after the write (latest-per-key + is_active).

        Raises:
            KeyError: no registration with that key exists.
            ValueError: the registration is already stopped (no-op
                transition refused so callers cannot accidentally
                double-stop).
            DuplicateRegistrationError / StoreBusy / AtomicWriteError:
                same as ``append``.
        """
        # Atomic under ONE exclusive file lock:
        #   1. read latest row (status + identity)
        #   2. validate latest.is_stopped is False
        #   3. build stopped row
        #   4. write under the same lock
        #   5. count effective actives under the same lock
        # Concurrent mark_stopped callers are serialized; only the
        # first one observes latest.is_active and writes; the loser
        # observes latest.is_stopped=True and raises ValueError.
        with self._open_and_lock() as f:
            latest = self._read_latest_under_lock(
                f, registration_key
            )
            if latest is None:
                raise KeyError(
                    f"no registration with key {registration_key!r}"
                )
            # Atomic re-check under the same lock — closes the
            # TOCTOU window between read and write.
            if latest.is_stopped:
                raise ValueError(
                    f"registration {registration_key!r} is "
                    f"already stopped"
                )
            stopped = FiboRegistration.build(
                exchange=latest.exchange,
                account=latest.account,
                symbol=latest.symbol,
                variant=latest.variant,
                side=latest.side,
                starting_volume=latest.starting_volume,
                source=latest.source,
                source_seq=latest.source_seq,
                source_cycle_id=latest.source_cycle_id,
                source_cumulative_weight=latest.source_cumulative_weight,
                source_percentage=latest.source_percentage,
                source_snapshot_received_at=(
                    latest.source_snapshot_received_at
                ),
                desired_exchange_size=latest.desired_exchange_size,
                source_symbol=latest.source_symbol,
                exchange_instrument=latest.exchange_instrument,
                status="stopped",
                created_at=latest.created_at,
                updated_at=updated_at,
            )
            # Final atomic re-check immediately before write — if
            # some other writer slipped in between the read and
            # here (which shouldn't happen because we hold the
            # exclusive lock, but defense in depth), we'd see a
            # status mismatch and refuse.
            recheck = self._read_latest_under_lock(f, registration_key)
            if recheck is None or recheck.is_stopped:
                raise ValueError(
                    f"registration {registration_key!r} is "
                    f"already stopped"
                )
            self._write_under_lock(f, stopped)
            active_count = self._count_active_under_lock(f)
            return stopped, active_count

    def reactivate(
        self,
        registration_key: str,
        *,
        # Identity fields — must match the stopped row exactly.
        source_symbol: str,
        exchange_instrument: str,
        # Mutable / source-snapshot fields — taken from the new
        # Start wizard session so the reactivated row reflects the
        # CURRENT MT4 state.
        starting_volume: Decimal,
        desired_exchange_size: Decimal,
        source: str,
        source_seq: int,
        source_cycle_id: int,
        source_cumulative_weight: Decimal,
        source_percentage: Decimal,
        source_snapshot_received_at: str,
        updated_at: Optional[str] = None,
    ) -> tuple:
        """Append a "registered" transition row for a stopped
        registration (Phase 2.7 Restart).

        Returns ``(reactivated_registration, active_count)`` where
        ``active_count`` is computed under the same lock after write.

        The store is append-only JSONL with "latest per key wins"
        semantics. Appending a new row that preserves the original
        ``created_at`` and refreshes ``updated_at``, while setting
        ``status="registered"``, is the canonical way to record the
        restart without rewriting historical JSONL rows.

        Identity contract — the caller MUST pass:

        * ``source_symbol`` — must equal the stopped row's
          ``source_symbol``. Mismatch raises ``ValueError`` so we
          never silently restart under a different identity.
        * ``exchange_instrument`` — must equal the stopped row's
          ``exchange_instrument``. Legacy rows with empty
          ``exchange_instrument`` are accepted (the identity is
          still uniquely captured by exchange/account/variant/side
          + source_symbol).

        Mutable / snapshot fields — these come from the new Start
        wizard session:

        * ``starting_volume`` / ``desired_exchange_size``
        * ``source``, ``source_seq``, ``source_cycle_id``,
          ``source_cumulative_weight``, ``source_percentage``,
          ``source_snapshot_received_at``

        ``created_at`` is preserved (NOT refreshed) so historical
        identity is stable. ``updated_at`` defaults to the current
        UTC ISO timestamp.

        Raises:
            KeyError: no registration with that key exists.
            ValueError:
                * the registration is already registered (no-op
                  transition refused).
                * the source_symbol or exchange_instrument doesn't
                  match the stopped row.
            DuplicateRegistrationError / StoreBusy /
                AtomicWriteError: same as ``append``.
        """
        # Atomic under ONE exclusive file lock:
        #   1. read latest row (status + identity)
        #   2. validate latest.is_stopped is True
        #   3. validate identity match
        #   4. build reactivated row
        #   5. write under the same lock
        # Concurrent reactivate callers are serialized; only the
        # first one observes latest.is_stopped and writes; the
        # loser observes latest.status="registered" and raises
        # ValueError (no-op transition refused).
        with self._open_and_lock() as f:
            latest = self._read_latest_under_lock(
                f, registration_key
            )
            if latest is None:
                raise KeyError(
                    f"no registration with key {registration_key!r}"
                )
            if not latest.is_stopped:
                raise ValueError(
                    f"registration {registration_key!r} is not "
                    f"stopped; current status: {latest.status!r}"
                )
            if (source_symbol or "") != (latest.source_symbol or ""):
                raise ValueError(
                    f"source_symbol mismatch on reactivate: "
                    f"passed {source_symbol!r}, stored "
                    f"{latest.source_symbol!r}"
                )
            if (exchange_instrument or "") != (
                latest.exchange_instrument or ""
            ):
                raise ValueError(
                    f"exchange_instrument mismatch on reactivate: "
                    f"passed {exchange_instrument!r}, stored "
                    f"{latest.exchange_instrument!r}"
                )
            reactivated = FiboRegistration.build(
                exchange=latest.exchange,
                account=latest.account,
                symbol=latest.symbol,
                variant=latest.variant,
                side=latest.side,
                starting_volume=starting_volume,
                source=source,
                source_seq=source_seq,
                source_cycle_id=source_cycle_id,
                source_cumulative_weight=source_cumulative_weight,
                source_percentage=source_percentage,
                source_snapshot_received_at=source_snapshot_received_at,
                desired_exchange_size=desired_exchange_size,
                source_symbol=latest.source_symbol,
                exchange_instrument=latest.exchange_instrument,
                status="registered",
                created_at=latest.created_at,
                updated_at=updated_at,
            )
            # Final atomic re-check immediately before write — if
            # some other writer slipped in between the read and
            # here (which shouldn't happen because we hold the
            # exclusive lock, but defense in depth), we'd see the
            # status flipped and refuse.
            recheck = self._read_latest_under_lock(f, registration_key)
            if recheck is None or not recheck.is_stopped:
                raise ValueError(
                    f"registration {registration_key!r} state "
                    f"changed during reactivate; current status: "
                    f"{(recheck.status if recheck else None)!r}"
                )
            self._write_under_lock(f, reactivated)
            active_count = self._count_active_under_lock(f)
            return reactivated, active_count


    def _count_active_under_lock(self, f) -> int:
        """Count effective active registrations while holding the lock.

        Replays the same latest-per-key semantics as ``load_all``
        against the locked file handle (including the row just
        written). Historical stopped lines that lost to a later
        row do not count; a latest ``status="stopped"`` row is
        inactive via ``is_active``.
        """
        try:
            f.seek(0, os.SEEK_SET)
            data = f.read()
        except OSError as exc:
            logger.warning(
                "fibo_registrations: lock-held active count failed: %s", exc
            )
            return 0
        latest_by_key: Dict[str, FiboRegistration] = {}
        for raw_line in data.splitlines():
            if not raw_line.strip():
                continue
            try:
                obj = json.loads(
                    raw_line.decode("utf-8", errors="replace").strip()
                )
            except (json.JSONDecodeError, UnicodeError):
                continue
            if not isinstance(obj, dict):
                continue
            try:
                reg = FiboRegistration.from_dict(obj)
            except (ValueError, KeyError, TypeError):
                continue
            latest_by_key[reg.registration_key] = reg
        return sum(1 for reg in latest_by_key.values() if reg.is_active)

    def count_active(self) -> int:
        """Return the effective active-registration count (unlocked read).

        Prefer the under-lock count returned by ``append`` /
        ``mark_stopped`` / ``reactivate`` for scheduler decisions.
        """
        return sum(1 for reg in self.load_all() if reg.is_active)

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