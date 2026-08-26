"""MT4 Observer Reader — the SOLE consumer of ``MT4_READER_BOT_TOKEN``.

Phase 1 hardening (spec §15 + reader-hardening corrections):

* Reads updates via ``getUpdates`` on ``MT4_READER_BOT_TOKEN`` ONLY.
  Never touches the Hermes gateway bot's token.
* Authenticates the SENDER: accepts an update only when
  ``message.chat.id == MT4_READER_CHAT_ID`` AND
  ``message.from.id == MT4_OBSERVER_BOT_ID`` AND
  ``message.from.is_bot == true``.
* Validates payload shape; rejects malformed/unsupported/wrong-sender
  updates with a structured outcome.
* Persists latest snapshot atomically (temp file -> fchmod 0o600 ->
  write -> flush -> fsync -> os.replace -> fsync parent dir).
* Persists reader transport state to
  ``~/.hermes/fibo/mt4_reader_state.json`` with the SAME atomic
  helper so a restart can resume at ``last_update_id + 1``.
* Source rollover safety: same source requires ``seq > last_seq``;
  new source is accepted and the previous source is retired; a
  retired source cannot reclaim the cache (``REJECTED_RETIRED_SOURCE``).
* Single-reader OS lock via ``fcntl.flock(LOCK_EX | LOCK_NB)`` on the
  reader lock file, kept open for the lifetime of the process.
* Advances ``last_update_id`` for conclusively rejected updates so a
  poison message does not replay forever.
* Never prints either bot token.

Launch (NOT a service — the user starts it manually from their
shell when they want live MT4 data):

    nohup /usr/local/lib/hermes-agent/venv/bin/python \\
        -m plugins.trade.fibo.mt4_reader \\
        > ~/.hermes/fibo/mt4_reader.log 2>&1 &

Stop:

    pkill -f 'plugins.trade.fibo.mt4_reader'

The module's ``__main__`` block does env lookup, lock acquisition,
replay-from-cursor, and long-poll.
"""

from __future__ import annotations

import datetime as _dt
import errno
import fcntl
import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._atomic import (
    AtomicWriteError,
    DIR_MODE,
    FILE_MODE,
    atomic_write_text,
    ensure_dir_0700,
)
from .snapshot import SUPPORTED_SCHEMA_VERSION, parse_snapshot_payload

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public outcome enum
# ---------------------------------------------------------------------------


# String constants for UpdateOutcome — exposed so tests can assert on
# them without importing internal types.
ACCEPTED = "ACCEPTED"
IGNORED_OLDER = "IGNORED_OLDER"
IGNORED_DUP = "IGNORED_DUP"
REJECTED_WRONG_CHAT = "REJECTED_WRONG_CHAT"
REJECTED_WRONG_SENDER = "REJECTED_WRONG_SENDER"
REJECTED_NOT_BOT = "REJECTED_NOT_BOT"
REJECTED_MALFORMED = "REJECTED_MALFORMED"
REJECTED_VERSION = "REJECTED_VERSION"
REJECTED_SCHEMA = "REJECTED_SCHEMA"
REJECTED_RETIRED_SOURCE = "REJECTED_RETIRED_SOURCE"
REJECTED_NO_TEXT = "REJECTED_NO_TEXT"


@dataclass(frozen=True)
class UpdateOutcome:
    code: str
    update_id: int
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.code} (update_id={self.update_id}{(': ' + self.detail) if self.detail else ''})"


# ---------------------------------------------------------------------------
# Reader transport state (persisted)
# ---------------------------------------------------------------------------


# Cap on retired sources list — old source identities are forgotten
# after this many distinct sources have been observed.
MAX_RETIRED_SOURCES = 32


class ReaderState:
    """Persistent reader transport state.

    Persisted to ``~/.hermes/fibo/mt4_reader_state.json`` after every
    update with the atomic write helper. Replayed on startup so the
    reader resumes at ``last_update_id + 1``.
    """

    def __init__(
        self,
        *,
        last_update_id: int = 0,
        current_source: str = "",
        last_seq: int = 0,
        retired_sources: Optional[List[str]] = None,
    ) -> None:
        self.last_update_id = int(last_update_id)
        self.current_source = str(current_source or "")
        self.last_seq = int(last_seq)
        self.retired_sources: List[str] = list(retired_sources or [])

    # -- mutators -----------------------------------------------------

    def advance_update_id(self, update_id: int) -> None:
        if int(update_id) > self.last_update_id:
            self.last_update_id = int(update_id)

    def accept_initial_source(self, source: str) -> None:
        self.current_source = str(source)
        self.last_seq = 0

    def accept_newer_seq(self, seq: int) -> None:
        self.last_seq = int(seq)

    def retire_current_and_adopt_new(self, new_source: str) -> None:
        if self.current_source and self.current_source != new_source:
            if new_source not in self.retired_sources:
                self.retired_sources.append(self.current_source)
                if len(self.retired_sources) > MAX_RETIRED_SOURCES:
                    self.retired_sources = self.retired_sources[
                        -MAX_RETIRED_SOURCES:
                    ]
        self.current_source = str(new_source)
        self.last_seq = 0

    def is_retired(self, source: str) -> bool:
        return str(source) in self.retired_sources

    # -- serialization ------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "last_update_id": self.last_update_id,
            "current_source": self.current_source,
            "last_seq": self.last_seq,
            "retired_sources": list(self.retired_sources),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def load(cls, path: Path) -> "ReaderState":
        if not path.is_file():
            return cls()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("mt4_reader_state: read failed at %s: %s", path, exc)
            return cls()
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning(
                "mt4_reader_state: malformed JSON at %s: %s", path, exc
            )
            return cls()
        if not isinstance(raw, dict):
            return cls()
        retired = raw.get("retired_sources", [])
        if not isinstance(retired, list):
            retired = []
        retired_clean = [str(s) for s in retired if isinstance(s, str)]
        return cls(
            last_update_id=_safe_int(raw.get("last_update_id", 0)),
            current_source=str(raw.get("current_source", "") or ""),
            last_seq=_safe_int(raw.get("last_seq", 0)),
            retired_sources=retired_clean,
        )

    def save(self, path: Path) -> None:
        """Persist atomically with fchmod + fsync guarantees."""
        try:
            atomic_write_text(path, self.to_json() + "\n")
        except AtomicWriteError as exc:
            logger.error(
                "mt4_reader_state: atomic write to %s failed: %s",
                path, exc,
            )
            raise


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Update inspection — pure (no I/O)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InspectionResult:
    """Outcome of inspecting a single Telegram ``update``.

    ``snapshot_to_publish`` is non-None iff the update was ACCEPTED.
    ``update_id_for_cursor`` is always non-None so the caller can
    advance the transport cursor (including for rejected updates,
    per spec §15 hardening #7).
    """

    outcome: UpdateOutcome
    update_id_for_cursor: int
    snapshot_to_publish: Optional["AcceptedPublish"] = None


@dataclass(frozen=True)
class AcceptedPublish:
    """Bundle the data the Reader needs to atomically publish a snapshot."""

    snapshot_payload: Dict[str, Any]  # raw observer body for parse_snapshot_payload
    received_at: str
    telegram_update_id: int
    telegram_message_id: int
    reader_chat_id: int


def _redact_token(token: str) -> str:
    """Return a redacted form of a bot token for logging.

    Telegram bot tokens look like ``123456789:ABCDefGhI...``. We keep
    only the first 6 chars after the colon and the trailing 4 chars,
    separated by an ellipsis.
    """
    if not token:
        return ""
    if ":" not in token:
        return "<redacted>"
    head, _, tail = token.partition(":")
    if len(tail) <= 10:
        return f"{head}:<redacted>"
    return f"{head}:{tail[:6]}...{tail[-4:]}"


def inspect_update(
    update: Dict[str, Any],
    *,
    expected_chat_id: int,
    expected_sender_id: int,
) -> InspectionResult:
    """Classify a single Telegram update without performing any I/O.

    Pure function: easy to unit-test exhaustively. All branching is
    driven by the spec's authentication / validation rules:

      * chat.id must match expected_chat_id
      * message.from.id must match expected_sender_id
      * message.from.is_bot must be true
      * the message must contain a parseable JSON text body
      * the body must pass ``parse_snapshot_payload``
      * same source + non-increasing seq is rejected
      * new source not yet retired is accepted; the prior source is
        retired at the caller's layer
      * retired source cannot reclaim the cache
    """
    if not isinstance(update, dict):
        return InspectionResult(
            outcome=UpdateOutcome(
                REJECTED_MALFORMED, 0, "update is not a JSON object"
            ),
            update_id_for_cursor=0,
        )
    update_id = _safe_int(update.get("update_id", 0))
    if update_id <= 0:
        return InspectionResult(
            outcome=UpdateOutcome(
                REJECTED_MALFORMED, 0, "missing update_id"
            ),
            update_id_for_cursor=0,
        )

    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return InspectionResult(
            outcome=UpdateOutcome(
                REJECTED_MALFORMED, update_id, "no message payload"
            ),
            update_id_for_cursor=update_id,
        )

    chat = message.get("chat")
    if not isinstance(chat, dict):
        return InspectionResult(
            outcome=UpdateOutcome(
                REJECTED_MALFORMED, update_id, "no chat object"
            ),
            update_id_for_cursor=update_id,
        )
    chat_id = _safe_int(chat.get("id", 0))
    if chat_id != expected_chat_id:
        return InspectionResult(
            outcome=UpdateOutcome(
                REJECTED_WRONG_CHAT, update_id,
                f"got chat_id={chat_id}",
            ),
            update_id_for_cursor=update_id,
        )

    sender = message.get("from")
    if not isinstance(sender, dict):
        return InspectionResult(
            outcome=UpdateOutcome(
                REJECTED_WRONG_SENDER, update_id, "no from object"
            ),
            update_id_for_cursor=update_id,
        )
    is_bot = sender.get("is_bot", False)
    if not is_bot:
        return InspectionResult(
            outcome=UpdateOutcome(
                REJECTED_NOT_BOT, update_id,
                f"sender id={_safe_int(sender.get('id', 0))}",
            ),
            update_id_for_cursor=update_id,
        )
    sender_id = _safe_int(sender.get("id", 0))
    if sender_id != expected_sender_id:
        return InspectionResult(
            outcome=UpdateOutcome(
                REJECTED_WRONG_SENDER, update_id,
                f"sender_id={sender_id}",
            ),
            update_id_for_cursor=update_id,
        )

    text = message.get("text") or message.get("caption")
    if not isinstance(text, str) or not text.strip():
        return InspectionResult(
            outcome=UpdateOutcome(
                REJECTED_NO_TEXT, update_id, "empty text/caption"
            ),
            update_id_for_cursor=update_id,
        )

    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        return InspectionResult(
            outcome=UpdateOutcome(
                REJECTED_MALFORMED, update_id, f"JSON parse: {exc.msg}"
            ),
            update_id_for_cursor=update_id,
        )

    if not isinstance(body, dict):
        return InspectionResult(
            outcome=UpdateOutcome(
                REJECTED_MALFORMED, update_id,
                "body is not a JSON object",
            ),
            update_id_for_cursor=update_id,
        )

    v = _safe_int(body.get("v", 0))
    if v != SUPPORTED_SCHEMA_VERSION:
        return InspectionResult(
            outcome=UpdateOutcome(
                REJECTED_VERSION, update_id, f"v={v}"
            ),
            update_id_for_cursor=update_id,
        )

    source = str(body.get("source", "") or "").strip()
    if not source:
        return InspectionResult(
            outcome=UpdateOutcome(
                REJECTED_SCHEMA, update_id, "missing source"
            ),
            update_id_for_cursor=update_id,
        )

    seq = _safe_int(body.get("seq", 0))
    if seq <= 0:
        return InspectionResult(
            outcome=UpdateOutcome(
                REJECTED_SCHEMA, update_id, f"seq={seq}"
            ),
            update_id_for_cursor=update_id,
        )

    # Source / seq handling is left to the caller (Reader) because the
    # current ``current_source`` and ``last_seq`` live there. We return
    # the parsed body and let the caller decide ACCEPTED / IGNORED /
    # RETIRED. To keep ``inspect_update`` pure, we surface the parsed
    # body in the outcome detail via a sentinel dict.
    return InspectionResult(
        outcome=UpdateOutcome(ACCEPTED, update_id),
        update_id_for_cursor=update_id,
        snapshot_to_publish=AcceptedPublish(
            snapshot_payload=body,
            received_at=_utc_iso_now(),
            telegram_update_id=update_id,
            telegram_message_id=_safe_int(message.get("message_id", 0)),
            reader_chat_id=chat_id,
        ),
    )


# ---------------------------------------------------------------------------
# Telegram transport (minimal, stdlib-only)
# ---------------------------------------------------------------------------


class TelegramApiError(RuntimeError):
    """Raised when the Telegram Bot API returns a non-2xx response."""


def _http_get_json(
    url: str,
    *,
    timeout: float = 35.0,
) -> Tuple[int, Dict[str, Any], str]:
    """GET ``url`` and parse JSON. Returns ``(status, json_or_empty, raw)``.

    Network errors propagate as ``OSError`` so callers can decide.
    Raises ``TelegramApiError`` for HTTP non-2xx so callers can log
    WITHOUT leaking the bot token (we never put the token in the
    exception message; the URL has the token but we redact below).
    """
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        # Redact the token from any URL surfaced in error text.
        return (
            int(exc.code),
            {},
            raw,
        )
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {}
    return (
        int(status),
        data,
        raw,
    )


def _redact_url(url: str) -> str:
    """Redact ``bot_token`` query parameter from a URL string."""
    if "bot" not in url:
        return url
    # find bot<token>/ and redact the token
    import re as _re
    return _re.sub(
        r"(bot)([^/]+)(/)",
        lambda m: m.group(1) + "<redacted>" + m.group(3),
        url,
    )


class TelegramLongPoll:
    """Thin wrapper over ``getUpdates``.

    Never logs the bot token. Uses stdlib only.
    """

    def __init__(self, bot_token: str, *, timeout_seconds: int = 25) -> None:
        if not bot_token:
            raise ValueError("bot_token is empty")
        self._token = bot_token
        self._timeout_seconds = int(timeout_seconds)

    def get_updates(
        self,
        *,
        offset: Optional[int],
        timeout_seconds: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Call getUpdates and return the list of updates.

        ``timeout`` is the long-poll duration; ``offset`` is the
        ``last_update_id + 1`` cursor (or ``None`` to fetch from start).
        """
        params: Dict[str, Any] = {
            "timeout": int(timeout_seconds or self._timeout_seconds),
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            params["offset"] = int(offset)
        qs = urllib.parse.urlencode(params)
        url = (
            f"https://api.telegram.org/bot{self._token}/getUpdates?{qs}"
        )
        status, body, _raw = _http_get_json(url, timeout=float(params["timeout"]) + 10)
        if status != 200 or not isinstance(body, dict):
            raise TelegramApiError(
                f"getUpdates HTTP {status} (url={_redact_url(url)})"
            )
        if not body.get("ok", False):
            raise TelegramApiError(
                f"getUpdates ok=False; description={body.get('description')!r} "
                f"(url={_redact_url(url)})"
            )
        result = body.get("result")
        if not isinstance(result, list):
            return []
        return [u for u in result if isinstance(u, dict)]


# ---------------------------------------------------------------------------
# Single-reader OS lock
# ---------------------------------------------------------------------------


class ReaderLock:
    """OS-level flock wrapper for the MT4 Reader.

    Spec §15 hardening #3: ``fcntl.flock(LOCK_EX | LOCK_NB)`` on the
    reader lock file, FD kept open for the lifetime of the process.
    A stale lock filename alone must not block startup — ``LOCK_NB``
    means a crashed reader's lock is released the moment its FD is
    closed by the kernel, regardless of whether the file still
    exists on disk.
    """

    def __init__(self, lock_path: Path) -> None:
        self._path = Path(lock_path)
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        ensure_dir_0700(self._path.parent)
        # Create the lock file if missing (mode 0600).
        if not self._path.exists():
            try:
                fd = os.open(
                    str(self._path),
                    os.O_CREAT | os.O_RDWR,
                    FILE_MODE,
                )
                os.close(fd)
            except OSError as exc:
                raise ReaderLockError(
                    f"failed to create reader lock {self._path}: {exc}"
                ) from exc
            try:
                os.chmod(self._path, FILE_MODE)
            except OSError:
                pass
        # Open RDWR; we never write to it but RDWR is the convention
        # for flock.
        fd = os.open(str(self._path), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise ReaderLockError(
                    f"another reader holds the lock at {self._path}"
                ) from exc
            raise ReaderLockError(
                f"failed to lock {self._path}: {exc}"
            ) from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    @property
    def path(self) -> Path:
        return self._path


class ReaderLockError(RuntimeError):
    """Raised when the reader lock cannot be acquired."""


# ---------------------------------------------------------------------------
# Reader process
# ---------------------------------------------------------------------------


class Mt4ReaderProcess:
    """In-process MT4 Reader.

    ``run_once()`` performs one full getUpdates cycle (replay +
    cursor-advancing + publishing). Testable without sleeping.
    ``poll_forever()`` is the long-poll loop used by ``__main__``.
    """

    def __init__(
        self,
        *,
        bot_token: str,
        expected_chat_id: int,
        expected_sender_id: int,
        snapshot_path: Path,
        reader_state_path: Path,
        reader_lock_path: Path,
        api: Optional[TelegramLongPoll] = None,
        state: Optional[ReaderState] = None,
        lock: Optional[ReaderLock] = None,
        sleep_fn=time.sleep,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token is empty")
        self._bot_token = bot_token
        self._expected_chat_id = int(expected_chat_id)
        self._expected_sender_id = int(expected_sender_id)
        self._snapshot_path = Path(snapshot_path)
        self._reader_state_path = Path(reader_state_path)
        self._reader_lock_path = Path(reader_lock_path)
        self._api = api or TelegramLongPoll(bot_token)
        self._state = state or ReaderState.load(self._reader_state_path)
        self._lock = lock or ReaderLock(self._reader_lock_path)
        self._sleep = sleep_fn
        self._stop_requested = False

    # -- properties ---------------------------------------------------

    @property
    def state(self) -> ReaderState:
        return self._state

    # -- one cycle ---------------------------------------------------

    def run_once(self, *, long_poll_seconds: int = 1) -> List[UpdateOutcome]:
        """One getUpdates cycle.

        Returns the list of ``UpdateOutcome`` objects in order. The
        state is persisted to ``mt4_reader_state.json`` at the end
        of the cycle (after every update is processed).
        """
        offset = self._state.last_update_id + 1 if self._state.last_update_id else None
        try:
            updates = self._api.get_updates(
                offset=offset, timeout_seconds=long_poll_seconds
            )
        except TelegramApiError as exc:
            # Transport-level Telegram API error: 5xx, 4xx with
            # ok=False, etc. We MUST NOT advance the cursor on a
            # transport failure (the server may have actually delivered
            # updates that we never saw). Just log and return; the
            # next cycle will ask for the same offset.
            logger.warning("mt4_reader: getUpdates API error: %s", exc)
            return []
        except OSError as exc:
            # DNS / connect / timeout / socket errors. Same invariant:
            # never advance the cursor. The next cycle retries the
            # same offset.
            logger.warning(
                "mt4_reader: getUpdates transport failure: %s", exc
            )
            return []
        except Exception as exc:  # noqa: BLE001
            # Defensive: any unexpected error from the transport layer
            # must not advance the cursor. The Reader logs and skips
            # this cycle.
            logger.error(
                "mt4_reader: unexpected getUpdates error: %s",
                exc, exc_info=True,
            )
            return []
        outcomes: List[UpdateOutcome] = []
        for update in updates:
            try:
                outcome = self._process_update(update)
            except Exception as exc:  # noqa: BLE001
                # If snapshot publish (or any step in the ACCEPTED
                # path) raises, we MUST stop processing this update
                # and let it replay on restart. The cursor has NOT
                # advanced (see _process_update contract). We stop
                # the loop so subsequent updates in this batch do
                # not pile up against a broken disk.
                logger.error(
                    "mt4_reader: processing update failed; update will "
                    "replay on restart: %s", exc, exc_info=True,
                )
                break
            outcomes.append(outcome)
        # Persist state once per cycle. State has either not advanced
        # (publish failure) or has every accepted update's cursor /
        # source / seq set. A failure here is logged but the loop
        # continues — restart will replay from the last persisted
        # cursor (the previous successful save), which is correct.
        try:
            self._state.save(self._reader_state_path)
        except AtomicWriteError as exc:
            logger.error(
                "mt4_reader: failed to persist reader state to %s: %s; "
                "the next restart may replay up to one cycle of "
                "updates",
                self._reader_state_path, exc,
            )
        return outcomes

    def _process_update(self, update: Dict[str, Any]) -> UpdateOutcome:
        """Inspect one Telegram update and (conditionally) publish a
        snapshot and advance the transport cursor.

        Ordering (spec, hardening #1):

            validate update
              → ACCEPTED: atomically publish + fsync mt4_snapshot.json
                (NO in-memory state mutation yet)
              → THEN mutate in-memory state (advance last_update_id,
                set current_source, last_seq)
              → outcome returned; cycle end-of-loop persists reader-state
            IGNORED / REJECTED (no snapshot):
              → just advance in-memory last_update_id
              → outcome returned; cycle end-of-loop persists reader-state

        If snapshot publish raises, we leave BOTH last_update_id and
        the source/seq unchanged. The Telegram cursor will replay this
        update on restart.
        """
        inspection = inspect_update(
            update,
            expected_chat_id=self._expected_chat_id,
            expected_sender_id=self._expected_sender_id,
        )
        update_id = inspection.update_id_for_cursor

        if inspection.outcome.code != ACCEPTED:
            # Reject / ignore path. No snapshot write needed. The
            # cycle's end-of-loop save persists the advanced cursor.
            if update_id > 0:
                self._state.advance_update_id(update_id)
            logger.warning("mt4_reader: %s", inspection.outcome)
            return inspection.outcome

        # ACCEPTED path. Publish the snapshot FIRST. Only after the
        # snapshot is durable on disk do we touch in-memory state.
        assert inspection.snapshot_to_publish is not None
        body = inspection.snapshot_to_publish.snapshot_payload
        source = str(body.get("source", "") or "").strip()
        seq = _safe_int(body.get("seq", 0))

        # Retired source cannot reclaim — no snapshot publish, no
        # state mutation, just advance cursor so the poison message
        # does not replay forever.
        if self._state.current_source and self._state.is_retired(source):
            logger.warning(
                "mt4_reader: retired source attempted to reclaim cache: "
                "source=%s update_id=%s",
                source, update_id,
            )
            if update_id > 0:
                self._state.advance_update_id(update_id)
            return UpdateOutcome(
                REJECTED_RETIRED_SOURCE, update_id,
                f"source={source} is retired",
            )

        # Same source, seq gate.
        if self._state.current_source and source == self._state.current_source:
            if seq <= self._state.last_seq:
                # Older or duplicate. No snapshot publish. Just
                # advance cursor so we don't replay the older
                # message on restart.
                if update_id > 0:
                    self._state.advance_update_id(update_id)
                if seq == self._state.last_seq:
                    return UpdateOutcome(
                        IGNORED_DUP, update_id, f"seq={seq}"
                    )
                return UpdateOutcome(
                    IGNORED_OLDER, update_id,
                    f"seq={seq} <= {self._state.last_seq}",
                )
            # seq > last_seq: ACCEPT a newer seq. Publish first.
            self._publish_snapshot(body, inspection.snapshot_to_publish)
            self._state.accept_newer_seq(seq)
            if update_id > 0:
                self._state.advance_update_id(update_id)
            return UpdateOutcome(
                ACCEPTED, update_id, f"seq={seq}",
            )

        # New source — initial accept or rollover. Publish first.
        # If publish raises, both source and last_seq stay unchanged,
        # and the cursor is NOT advanced. The update replays.
        self._publish_snapshot(body, inspection.snapshot_to_publish)
        if self._state.current_source:
            self._state.retire_current_and_adopt_new(source)
        else:
            self._state.accept_initial_source(source)
        self._state.accept_newer_seq(seq)
        if update_id > 0:
            self._state.advance_update_id(update_id)
        return UpdateOutcome(
            ACCEPTED, update_id,
            f"source={source} seq={seq}",
        )

    def _publish_snapshot(
        self,
        body: Dict[str, Any],
        publish: AcceptedPublish,
    ) -> None:
        """Atomically publish the snapshot for an ACCEPTED update.

        Contract: raises on ANY failure so the caller's in-memory
        state mutation (cursor + source + seq) does NOT happen. This
        preserves the spec invariant:

            snapshot publish + fsync → THEN advance cursor

        If this raises, the Telegram update will be re-delivered on
        restart and we'll try again.
        """
        # Build the envelope: source fields preserved verbatim, then
        # the four envelope fields appended.
        envelope = dict(body)
        envelope["received_at"] = publish.received_at
        envelope["telegram_update_id"] = publish.telegram_update_id
        envelope["telegram_message_id"] = publish.telegram_message_id
        envelope["reader_chat_id"] = publish.reader_chat_id
        # Validate via the canonical parser so a malformed shape is
        # caught here (and we do NOT persist a corrupt cache).
        parse_snapshot_payload(
            envelope,
            received_at=publish.received_at,
            telegram_update_id=publish.telegram_update_id,
            telegram_message_id=publish.telegram_message_id,
            reader_chat_id=publish.reader_chat_id,
        )
        payload = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
        # No try/except here — atomic_write_text already wraps OS errors
        # as AtomicWriteError. Propagate so the caller can decide to
        # leave the cursor at its old position.
        atomic_write_text(self._snapshot_path, payload + "\n")

    # -- long-poll loop ----------------------------------------------

    def request_stop(self) -> None:
        self._stop_requested = True

    def poll_forever(self) -> None:
        # NOTE: do NOT reset _stop_requested here. If the caller
        # already set it (e.g. a SIGTERM arrived just before the
        # call), we want to honor that and exit immediately.
        # SIGTERM handler for graceful shutdown.
        def _sigterm(_signum, _frame):
            self._stop_requested = True
        try:
            signal.signal(signal.SIGTERM, _sigterm)
        except (ValueError, OSError):
            # signal() may fail when not on main thread (tests).
            pass
        try:
            signal.signal(signal.SIGINT, _sigterm)
        except (ValueError, OSError):
            pass
        # Bounded backoff for transient transport failures: when a
        # cycle returns immediately (no updates fetched, transport
        # error), we sleep before the next attempt. The sleep grows
        # up to a ceiling, then resets after a successful cycle that
        # actually returned updates.
        backoff_seconds = 0.0
        backoff_ceiling = 30.0
        backoff_initial = 1.0
        while not self._stop_requested:
            outcomes = self.run_once(long_poll_seconds=25)
            if self._stop_requested:
                break
            if outcomes:
                # Successful cycle with at least one update; reset.
                backoff_seconds = 0.0
                continue
            # No outcomes this cycle (transport failure or empty
            # batch). Sleep with bounded exponential backoff so we
            # never busy-loop on a flaky network.
            if backoff_seconds == 0.0:
                backoff_seconds = backoff_initial
            else:
                backoff_seconds = min(backoff_seconds * 2, backoff_ceiling)
            # Sleep in small chunks so SIGTERM is observed promptly.
            slept = 0.0
            while slept < backoff_seconds and not self._stop_requested:
                step = min(1.0, backoff_seconds - slept)
                self._sleep(step)
                slept += step
        logger.info("mt4_reader: stop requested; exiting")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _default_paths() -> Tuple[Path, Path, Path]:
    """Return (snapshot, state, lock) paths under ``~/.hermes/fibo/``."""
    hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    fibo_dir = hermes_home / "fibo"
    return (
        fibo_dir / "mt4_snapshot.json",
        fibo_dir / "mt4_reader_state.json",
        fibo_dir / "mt4_reader.lock",
    )


def _build_from_env() -> Mt4ReaderProcess:
    """Build an ``Mt4ReaderProcess`` from process env (for ``__main__``)."""
    bot_token = os.environ.get("MT4_READER_BOT_TOKEN", "").strip()
    chat_id = int(os.environ.get("MT4_READER_CHAT_ID", "0"))
    sender_id = int(os.environ.get("MT4_OBSERVER_BOT_ID", "0"))
    if not bot_token:
        raise SystemExit(
            "MT4_READER_BOT_TOKEN is not set; refusing to start the reader"
        )
    if chat_id == 0:
        raise SystemExit(
            "MT4_READER_CHAT_ID is not set; refusing to start the reader"
        )
    if sender_id == 0:
        raise SystemExit(
            "MT4_OBSERVER_BOT_ID is not set; refusing to start the reader"
        )
    snap, state, lock = _default_paths()
    return Mt4ReaderProcess(
        bot_token=bot_token,
        expected_chat_id=chat_id,
        expected_sender_id=sender_id,
        snapshot_path=snap,
        reader_state_path=state,
        reader_lock_path=lock,
    )


__all__ = [
    # outcomes
    "ACCEPTED",
    "IGNORED_OLDER",
    "IGNORED_DUP",
    "REJECTED_WRONG_CHAT",
    "REJECTED_WRONG_SENDER",
    "REJECTED_NOT_BOT",
    "REJECTED_MALFORMED",
    "REJECTED_VERSION",
    "REJECTED_SCHEMA",
    "REJECTED_RETIRED_SOURCE",
    "REJECTED_NO_TEXT",
    "UpdateOutcome",
    # transport state
    "ReaderState",
    # inspection
    "InspectionResult",
    "AcceptedPublish",
    "inspect_update",
    # transport
    "TelegramLongPoll",
    "TelegramApiError",
    # lock
    "ReaderLock",
    "ReaderLockError",
    # process
    "Mt4ReaderProcess",
    "_default_paths",
    "_build_from_env",
]


# ---------------------------------------------------------------------------
# Module entry point — used by ``python -m plugins.trade.fibo.mt4_reader``
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("FIBO_READER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: Optional[List[str]] = None) -> int:
    _configure_logging()
    reader = _build_from_env()
    try:
        reader._lock.acquire()  # noqa: SLF001 - explicit access is fine here
    except ReaderLockError as exc:
        logger.error("mt4_reader: cannot start: %s", exc)
        return 2
    try:
        logger.info(
            "mt4_reader: started; snapshot=%s state=%s lock=%s",
            reader._snapshot_path, reader._reader_state_path,
            reader._reader_lock_path,
        )
        reader.poll_forever()
    finally:
        reader._lock.release()
        logger.info("mt4_reader: lock released; exiting")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))