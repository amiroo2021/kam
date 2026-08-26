"""MT4 observer snapshot schema + loader.

The MT4 Reader writes ``~/.hermes/fibo/mt4_snapshot.json`` whenever it
accepts a new observer payload. This module:

* defines the dataclasses that mirror the on-disk schema,
* validates the JSON shape defensively,
* exposes ``Mt4SnapshotStore.load(path)`` for the wizard to read the
  latest snapshot synchronously.

The wizard NEVER polls Telegram. It only reads the file the Reader
populated.

Schema (file):

    {
      "v": 1,
      "source": "<observer instance id>",
      "seq": <int>,
      "ts": <observer-supplied timestamp>,
      "fibos": [
        {
          "symbol": "BTCUSD",
          "variant": "FASTFib",
          "percentage": 0.001,
          "buy_cycle_id": <int>,
          "cumulative_buy_weight": <number>,
          "sell_cycle_id": <int>,
          "cumulative_sell_weight": <number>
        },
        ...
      ],
      "received_at": "<ISO-8601 UTC>",
      "telegram_update_id": <int>,
      "telegram_message_id": <int>,
      "reader_chat_id": <int>
    }

Source fields (``v``, ``source``, ``seq``, ``ts``, ``fibos``) are
preserved verbatim from the observer. The four envelope fields
(``received_at``, ``telegram_update_id``, ``telegram_message_id``,
``reader_chat_id``) are ADDED by the Reader on accept — they never
replace source fields.

The wizard derives side-specific fields at registration time:

    BUY  -> source_cycle_id = buy_cycle_id,
            source_cumulative_weight = cumulative_buy_weight
    SELL -> source_cycle_id = sell_cycle_id,
            source_cumulative_weight = cumulative_sell_weight

The loader tolerates a missing file (``load`` returns ``None``) so
the wizard can render a "no MT4 data yet" screen instead of crashing.
A malformed file is logged at WARNING and treated as missing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


SUPPORTED_SCHEMA_VERSION = 1


# Side constants — canonicalized by the wizard.
SIDE_BUY = "BUY"
SIDE_SELL = "SELL"


@dataclass(frozen=True)
class Mt4Fibo:
    """One fibo record from the observer payload.

    Mirrors the observer's fibos[] entry exactly; the wizard derives
    side-specific fields at consumption time.
    """

    symbol: str
    variant: str
    percentage: Decimal
    buy_cycle_id: int
    cumulative_buy_weight: Decimal
    sell_cycle_id: int
    cumulative_sell_weight: Decimal

    def side_cycle_id(self, side: str) -> int:
        if side == SIDE_BUY:
            return self.buy_cycle_id
        if side == SIDE_SELL:
            return self.sell_cycle_id
        raise ValueError(f"unknown side {side!r}")

    def side_cumulative_weight(self, side: str) -> Decimal:
        if side == SIDE_BUY:
            return self.cumulative_buy_weight
        if side == SIDE_SELL:
            return self.cumulative_sell_weight
        raise ValueError(f"unknown side {side!r}")

    def is_side_active(self, side: str) -> bool:
        """Active means cycle_id > 0 AND cumulative_weight > 0.

        Per spec §7 — used by the Start-Fibo flow to disable the
        inactive side at the BUY/SELL screen.
        """
        return self.side_cycle_id(side) > 0 and self.side_cumulative_weight(side) > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "variant": self.variant,
            "percentage": str(self.percentage),
            "buy_cycle_id": self.buy_cycle_id,
            "cumulative_buy_weight": str(self.cumulative_buy_weight),
            "sell_cycle_id": self.sell_cycle_id,
            "cumulative_sell_weight": str(self.cumulative_sell_weight),
        }


@dataclass(frozen=True)
class Mt4Snapshot:
    """The accepted observer payload, plus the Reader's envelope fields.

    All source fields (``v``, ``source``, ``seq``, ``ts``, ``fibos``)
    are preserved verbatim. Envelope fields (``received_at``,
    ``telegram_update_id``, ``telegram_message_id``,
    ``reader_chat_id``) are added by the Reader at accept time.
    """

    v: int
    source: str
    seq: int
    ts: Any  # observer-supplied timestamp; preserved as-is
    fibos: List[Mt4Fibo]
    received_at: str  # ISO-8601 UTC string added by Reader
    telegram_update_id: int
    telegram_message_id: int
    reader_chat_id: int

    # ----- side-keyed lookup helpers -----------------------------------

    def find_fibo(self, symbol: str, variant: str) -> Optional[Mt4Fibo]:
        """Return the fibo matching ``(symbol, variant)`` (case-insensitive
        on both), or ``None`` if absent."""
        sym = (symbol or "").strip().upper()
        var = (variant or "").strip().upper()
        for fibo in self.fibos:
            if fibo.symbol.upper() == sym and fibo.variant.upper() == var:
                return fibo
        return None

    def unique_symbol_variant_pairs(self) -> List[Dict[str, str]]:
        """Return unique ``{symbol, variant}`` pairs in snapshot order.

        Per spec §7, the Start Fibo menu derives one button per unique
        pair. Side is selected next, not on this screen.
        """
        seen = set()
        out: List[Dict[str, str]] = []
        for fibo in self.fibos:
            key = (fibo.symbol.upper(), fibo.variant.upper())
            if key in seen:
                continue
            seen.add(key)
            out.append({"symbol": fibo.symbol, "variant": fibo.variant})
        return out

    # ----- staleness ---------------------------------------------------

    def age_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        """Compute seconds elapsed since ``received_at``.

        Returns ``None`` if ``received_at`` is malformed. The wizard uses
        this to decide whether to enable the Create button at the
        confirmation screen (spec §4).
        """
        try:
            # Accept both "Z" suffix and explicit timezone offsets.
            ts_str = self.received_at.replace("Z", "+00:00")
            received = datetime.fromisoformat(ts_str)
        except (TypeError, ValueError):
            return None
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        cur = now or datetime.now(timezone.utc)
        return (cur - received).total_seconds()

    # ----- serialization ----------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "v": self.v,
            "source": self.source,
            "seq": self.seq,
            "ts": self.ts,
            "fibos": [f.to_dict() for f in self.fibos],
            "received_at": self.received_at,
            "telegram_update_id": self.telegram_update_id,
            "telegram_message_id": self.telegram_message_id,
            "reader_chat_id": self.reader_chat_id,
        }


# ---------------------------------------------------------------------------
# Validation helpers (used by both the Reader and the wizard loader)
# ---------------------------------------------------------------------------


def _parse_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field_name} not a decimal: {value!r}") from exc


def _parse_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):  # bool is subclass of int — reject explicitly
        raise ValueError(f"{field_name} is bool, not int: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{field_name} not int: {value!r}") from exc
    raise ValueError(f"{field_name} not int: {value!r}")


def parse_fibo_entry(raw: Dict[str, Any]) -> Mt4Fibo:
    """Validate + parse a single ``fibos[]`` entry."""
    if not isinstance(raw, dict):
        raise ValueError("fibos entry is not an object")
    for required in (
        "symbol",
        "variant",
        "percentage",
        "buy_cycle_id",
        "cumulative_buy_weight",
        "sell_cycle_id",
        "cumulative_sell_weight",
    ):
        if required not in raw:
            raise ValueError(f"fibos entry missing {required!r}")
    symbol = str(raw["symbol"]).strip()
    variant = str(raw["variant"]).strip()
    if not symbol:
        raise ValueError("fibos entry symbol is empty")
    if not variant:
        raise ValueError("fibos entry variant is empty")
    return Mt4Fibo(
        symbol=symbol,
        variant=variant,
        percentage=_parse_decimal(raw["percentage"], "percentage"),
        buy_cycle_id=_parse_int(raw["buy_cycle_id"], "buy_cycle_id"),
        cumulative_buy_weight=_parse_decimal(
            raw["cumulative_buy_weight"], "cumulative_buy_weight"
        ),
        sell_cycle_id=_parse_int(raw["sell_cycle_id"], "sell_cycle_id"),
        cumulative_sell_weight=_parse_decimal(
            raw["cumulative_sell_weight"], "cumulative_sell_weight"
        ),
    )


def parse_snapshot_payload(
    raw: Dict[str, Any],
    *,
    received_at: str,
    telegram_update_id: int,
    telegram_message_id: int,
    reader_chat_id: int,
) -> Mt4Snapshot:
    """Validate + parse a complete observer payload.

    ``raw`` is the parsed JSON of the observer message body. The four
    envelope parameters are added by the Reader (spec §2).
    """
    if not isinstance(raw, dict):
        raise ValueError("observer payload is not a JSON object")
    v = _parse_int(raw.get("v", 0), "v")
    if v != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schema version {v}; expected {SUPPORTED_SCHEMA_VERSION}"
        )
    source = str(raw.get("source", "")).strip()
    if not source:
        raise ValueError("observer payload missing source")
    seq = _parse_int(raw.get("seq", 0), "seq")
    if seq <= 0:
        raise ValueError(f"observer seq must be > 0, got {seq}")
    ts = raw.get("ts")
    # We do NOT validate ts; the observer owns its timestamp shape.
    fibos_raw = raw.get("fibos")
    if not isinstance(fibos_raw, list):
        raise ValueError("observer fibos must be a list")
    if len(fibos_raw) == 0:
        # Empty fibos is allowed (e.g. observer warming up) but logged.
        logger.info("mt4_snapshot: observer payload has empty fibos[] (seq=%s)", seq)
    fibos: List[Mt4Fibo] = []
    for entry in fibos_raw:
        fibos.append(parse_fibo_entry(entry))
    return Mt4Snapshot(
        v=v,
        source=source,
        seq=seq,
        ts=ts,
        fibos=fibos,
        received_at=received_at,
        telegram_update_id=telegram_update_id,
        telegram_message_id=telegram_message_id,
        reader_chat_id=reader_chat_id,
    )


# ---------------------------------------------------------------------------
# On-disk loader (used by the wizard)
# ---------------------------------------------------------------------------


class Mt4SnapshotStore:
    """Read-only access to the MT4 observer snapshot cache file."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Optional[Mt4Snapshot]:
        """Load the latest snapshot, or ``None`` if missing/malformed.

        The wizard renders a "no MT4 data yet" screen when this
        returns None. A malformed file is logged at WARNING but never
        raises.
        """
        if not self._path.is_file():
            return None
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("mt4_snapshot: read failed at %s: %s", self._path, exc)
            return None
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning(
                "mt4_snapshot: malformed JSON at %s: %s", self._path, exc
            )
            return None
        if not isinstance(raw, dict):
            logger.warning("mt4_snapshot: top-level JSON is not an object")
            return None
        try:
            return parse_snapshot_payload(
                raw,
                received_at=str(raw.get("received_at", "")),
                telegram_update_id=_safe_int(raw.get("telegram_update_id", 0)),
                telegram_message_id=_safe_int(raw.get("telegram_message_id", 0)),
                reader_chat_id=_safe_int(raw.get("reader_chat_id", 0)),
            )
        except ValueError as exc:
            logger.warning(
                "mt4_snapshot: schema validation failed at %s: %s",
                self._path, exc,
            )
            return None


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "SUPPORTED_SCHEMA_VERSION",
    "SIDE_BUY",
    "SIDE_SELL",
    "Mt4Fibo",
    "Mt4Snapshot",
    "Mt4SnapshotStore",
    "parse_fibo_entry",
    "parse_snapshot_payload",
]