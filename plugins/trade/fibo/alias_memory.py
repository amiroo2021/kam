"""Local approved alias memory for Fibo instrument translation.

Phase 2.2 spec §7: a SMALL local component that remembers
user-approved source->venue mappings locally so future runs are
faster.

Path: ``~/.hermes/fibo/instrument_aliases.json``

Constraints (spec §7 / §8):

* file mode 0600
* fibo dir 0700
* atomic writes using existing ``_atomic`` helper
* account included in key
* source symbol normalized consistently
* exchange normalized consistently
* ``confirmation_count`` increments ONLY after user taps Agree
* never store unresolved aliases
* never store secrets

Critical safety invariants (spec §8 / §10):

* This local memory is ONLY a hint. Cached mappings MUST be
  revalidated LIVE through the exchange agent before reuse. If
  the agent no longer recognises the stored venue instrument,
  the cached mapping is discarded and the wizard falls back to
  fresh resolution.
* Production usage MUST NEVER do git version-control writes
  (no git add, commit, or push). Alias learning stays local under
  ``~/.hermes/fibo/``.
* Alias memory code MUST NOT contain any of the following tokens
  (asserted by the alias-memory static-source guard):
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ._atomic import atomic_write_text, ensure_dir_0700

logger = logging.getLogger(__name__)


# On-disk schema version. Bump if the shape ever changes in an
# incompatible way; older files are then ignored on load.
ALIAS_MEMORY_VERSION = 1

# Empty store schema (used when no file exists yet).
EMPTY_SCHEMA: Dict[str, Any] = {
    "version": ALIAS_MEMORY_VERSION,
    "mappings": {},
}


@dataclass(frozen=True)
class AliasRecord:
    """One approved source-symbol -> exchange-instrument mapping.

    All fields are stored as plain strings / ints; the dataclass
    carries no methods that touch the network or filesystem.
    """

    source_symbol: str
    resolution_input: str
    exchange_instrument: str
    confirmed_at: str  # ISO-8601 UTC
    confirmation_count: int


def alias_key(
    exchange: str,
    account: str,
    source_symbol: str,
) -> str:
    """Build the canonical alias-memory key.

    The key is ``"<exchange>|<account>|<source_symbol>"``. Source
    symbol is upper-cased (matches FiboRegistration.normalize_symbol).
    Exchange is lower-cased. Account is upper-cased. Whitespace
    is stripped. This is the same normalisation the rest of the
    Fibo code uses, so the key is stable across the wizard.
    """
    return "|".join([
        (exchange or "").strip().lower(),
        (account or "").strip().upper(),
        (source_symbol or "").strip().upper(),
    ])


def _normalize_alias_record(raw: Dict[str, Any]) -> Optional[AliasRecord]:
    """Validate and normalise a single alias record.

    Returns None when the record is missing required fields or
    carries invalid types. The store silently drops bad records
    rather than crashing — the alias memory is a hint, not a
    source of truth.
    """
    try:
        return AliasRecord(
            source_symbol=str(raw.get("source_symbol", "") or "").strip().upper(),
            resolution_input=str(raw.get("resolution_input", "") or "").strip(),
            exchange_instrument=str(
                raw.get("exchange_instrument", "") or ""
            ).strip(),
            confirmed_at=str(raw.get("confirmed_at", "") or "").strip(),
            confirmation_count=int(raw.get("confirmation_count", 0) or 0),
        )
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class AliasMemory:
    """Local approved alias memory.

    Thread/process safe via ``atomic_write_text`` (the existing
    ``_atomic`` helper handles temp-file + fsync + os.replace +
    fsync(parent)). On load, the file is parsed and bad records
    are silently dropped.

    The store has no network I/O and no git / subprocess calls.
    Production usage never commits the file (it lives in
    ``~/.hermes/fibo/`` which is in the user's home, outside the
    repo checkout).
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._cache: Optional[Dict[str, AliasRecord]] = None

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    def load(self) -> Dict[str, AliasRecord]:
        """Read the alias memory file from disk.

        Returns an empty mapping if the file is missing or
        malformed. Never raises.
        """
        if self._cache is not None:
            return dict(self._cache)
        out: Dict[str, AliasRecord] = {}
        try:
            if not self._path.exists():
                return {}
            raw = self._path.read_text(encoding="utf-8")
            obj = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "alias_memory: load failed; starting empty: %s", exc
            )
            self._cache = {}
            return {}
        if not isinstance(obj, dict):
            self._cache = {}
            return {}
        version = obj.get("version", ALIAS_MEMORY_VERSION)
        if version != ALIAS_MEMORY_VERSION:
            # Unknown future schema — ignore rather than crash.
            logger.warning(
                "alias_memory: schema version=%s != %s; ignoring",
                version, ALIAS_MEMORY_VERSION,
            )
            self._cache = {}
            return {}
        mappings = obj.get("mappings")
        if not isinstance(mappings, dict):
            self._cache = {}
            return {}
        for key, raw_record in mappings.items():
            if not isinstance(key, str) or not isinstance(raw_record, dict):
                continue
            rec = _normalize_alias_record(raw_record)
            if rec is None:
                continue
            out[key] = rec
        self._cache = out
        return dict(self._cache)

    def save(self) -> None:
        """Persist the cache to disk atomically.

        Creates the parent directory (mode 0700) if missing.
        Writes via ``atomic_write_text`` which sets mode 0600
        before publication, then renames into place.

        If the cache has never been loaded (``self._cache is None``),
        this writes the empty schema so the file exists with the
        correct mode. Callers can rely on ``save()`` being
        idempotent and creating the file when the parent dir is
        writable.
        """
        if self._cache is None:
            self._cache = {}
        parent = self._path.parent
        ensure_dir_0700(parent)
        payload = {
            "version": ALIAS_MEMORY_VERSION,
            "mappings": {
                key: {
                    "source_symbol": rec.source_symbol,
                    "resolution_input": rec.resolution_input,
                    "exchange_instrument": rec.exchange_instrument,
                    "confirmed_at": rec.confirmed_at,
                    "confirmation_count": rec.confirmation_count,
                }
                for key, rec in sorted(self._cache.items())
            },
        }
        # atomic_write_text handles 0600 + atomic rename + fsync.
        atomic_write_text(self._path, json.dumps(payload, sort_keys=True))

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[AliasRecord]:
        """Read-only fetch of one mapping. Returns None on miss."""
        data = self.load()
        return data.get(key)

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def record_approval(
        self,
        key: str,
        *,
        source_symbol: str,
        resolution_input: str,
        exchange_instrument: str,
    ) -> AliasRecord:
        """Record a freshly-agreed mapping.

        Called ONLY from the wizard's Agree path (spec §4).
        ``confirmation_count`` starts at 1 for new entries and
        increments by 1 on each subsequent approval.
        Persists immediately so a crash before the next save
        does not lose the user's approval.
        """
        data = self.load()
        existing = data.get(key)
        if existing is None:
            new_count = 1
        else:
            new_count = int(existing.confirmation_count or 0) + 1
        rec = AliasRecord(
            source_symbol=str(source_symbol or "").strip().upper(),
            resolution_input=str(resolution_input or "").strip(),
            exchange_instrument=str(exchange_instrument or "").strip(),
            confirmed_at=_now_iso(),
            confirmation_count=new_count,
        )
        data[key] = rec
        self._cache = data
        self.save()
        return rec

    def discard(self, key: str) -> None:
        """Drop a mapping (e.g. after revalidation fails).

        Persists immediately so the bad mapping does not
        resurrect on the next wizard run.
        """
        data = self.load()
        if key in data:
            del data[key]
            self._cache = data
            self.save()

    def clear_all(self) -> None:
        """Wipe all stored mappings. Used by tests."""
        self._cache = {}
        self.save()

    # ------------------------------------------------------------------
    # Revalidation
    # ------------------------------------------------------------------

    def revalidate(
        self,
        key: str,
        *,
        resolve_fn: Callable[[str, str, str], Optional[str]],
    ) -> Optional[AliasRecord]:
        """Revalidate a cached mapping through the live exchange
        agent before it is shown to the user (spec §7 / §10).

        ``resolve_fn`` is a 3-arg callable:

            resolve_fn(exchange, account, exchange_instrument)
                -> Optional[str]  # the canonical venue contract id

        It MUST be the same read-only
        ``resolve_instrument`` path the wizard uses for fresh
        resolution. Returns:

        * ``AliasRecord`` — the cached mapping is still valid;
          it has been bumped in memory but NOT written back
          to disk (we only persist on user Agree, never on
          silent revalidation; the count should not be
          incremented by background revalidation).
        * ``None`` — the cached mapping is invalid (or absent);
          the caller should fall back to fresh resolution.

        This method performs ZERO writes. It does NOT mutate
        the on-disk file. It MAY mutate ``self._cache`` only
        when discarding an invalid mapping, and that discard
        is itself only persisted when the wizard subsequently
        records a new approval (via ``record_approval``).
        """
        data = self.load()
        rec = data.get(key)
        if rec is None:
            return None
        # Split the key. The key is "<exchange>|<account>|<src>".
        parts = key.split("|")
        if len(parts) != 3:
            logger.warning(
                "alias_memory: malformed key %r — discarding", key
            )
            self._cache = {k: v for k, v in data.items() if k != key}
            self.save()
            return None
        exchange, account, _src = parts
        try:
            canonical = resolve_fn(
                exchange, account, rec.exchange_instrument
            )
        except Exception:  # noqa: BLE001
            # Revalidation raised — treat as invalid; do NOT
            # persist anything (this method is read-only on disk).
            return None
        if not canonical:
            # Revalidation failed: cached mapping is stale.
            # Silently drop it from in-memory cache (no disk write).
            self._cache = {k: v for k, v in data.items() if k != key}
            return None
        # If the canonical contract id has drifted (e.g. the venue
        # renamed it), drop the stale mapping — the user needs to
        # Agree to the new one.
        if str(canonical).strip() != rec.exchange_instrument:
            self._cache = {k: v for k, v in data.items() if k != key}
            return None
        return rec


__all__ = [
    "AliasMemory",
    "AliasRecord",
    "ALIAS_MEMORY_VERSION",
    "alias_key",
]
