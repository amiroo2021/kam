"""GoldenFibo V2 client_order_index encode/decode + allocation.

48-bit layout (MSB first)::

    [47:40] MAGIC      8 bits   = 0x4B
    [39:38] VERSION    2 bits   = 1
    [37]    DIRECTION  1 bit    0=BUY 1=SELL
    [36:34] ROLE       3 bits
    [33:10] CYCLE_UID 24 bits
    [9:5]   STEP       5 bits
    [4:0]   SEQ        5 bits

Total = 48 bits. Must stay <= Lighter max (2**48 - 1).

Legacy IDs (100001, 1100001, ...) must NOT decode as V2.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, MutableMapping, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAGIC: int = 0x4B
VERSION: int = 1

DIRECTION_BUY: int = 0
DIRECTION_SELL: int = 1

ROLE_STEP0: int = 0
ROLE_LADDER_ENTRY: int = 1
ROLE_SHARED_TP: int = 2
ROLE_EMERGENCY_CLOSE: int = 3
ROLE_REPAIR_ENTRY: int = 4
# 5..7 reserved

ROLE_NAMES = {
    ROLE_STEP0: "STEP0",
    ROLE_LADDER_ENTRY: "LADDER_ENTRY",
    ROLE_SHARED_TP: "SHARED_TP",
    ROLE_EMERGENCY_CLOSE: "EMERGENCY_CLOSE",
    ROLE_REPAIR_ENTRY: "REPAIR_ENTRY",
}

# Map engine string roles -> V2 role codes
ENGINE_ROLE_TO_V2 = {
    "entry": ROLE_STEP0,
    "ladder": ROLE_LADDER_ENTRY,
    "tp": ROLE_SHARED_TP,
    "emergency_close": ROLE_EMERGENCY_CLOSE,
    "repair": ROLE_REPAIR_ENTRY,
}

MAX_STEP_NORMAL: int = 20
STEP_UNKNOWN: int = 31  # emergency only when truly unknown
MAX_SEQ: int = 31
MAX_CYCLE_UID: int = (1 << 24) - 1  # 24 bits
LIGHTER_MAX_CLIENT_ORDER_INDEX: int = (1 << 48) - 1

# Fixed epoch for fresh-server-safe cycle_uid seed (UTC).
GOLDEN_FIBO_EPOCH = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

# Bit widths / shifts
_SEQ_BITS = 5
_STEP_BITS = 5
_CYCLE_BITS = 24
_ROLE_BITS = 3
_DIR_BITS = 1
_VER_BITS = 2
_MAGIC_BITS = 8

_SEQ_SHIFT = 0
_STEP_SHIFT = _SEQ_BITS  # 5
_CYCLE_SHIFT = _STEP_SHIFT + _STEP_BITS  # 10
_ROLE_SHIFT = _CYCLE_SHIFT + _CYCLE_BITS  # 34
_DIR_SHIFT = _ROLE_SHIFT + _ROLE_BITS  # 37
_VER_SHIFT = _DIR_SHIFT + _DIR_BITS  # 38
_MAGIC_SHIFT = _VER_SHIFT + _VER_BITS  # 40

_SEQ_MASK = (1 << _SEQ_BITS) - 1
_STEP_MASK = (1 << _STEP_BITS) - 1
_CYCLE_MASK = (1 << _CYCLE_BITS) - 1
_ROLE_MASK = (1 << _ROLE_BITS) - 1
_DIR_MASK = (1 << _DIR_BITS) - 1
_VER_MASK = (1 << _VER_BITS) - 1
_MAGIC_MASK = (1 << _MAGIC_BITS) - 1


class ClientIdError(ValueError):
    """Raised for invalid encode/decode/allocation."""


class SeqExhaustedError(ClientIdError):
    """SEQ 0..31 exhausted for a (cycle, role, step) key — freeze required."""


@dataclass(frozen=True)
class DecodedClientId:
    version: int
    direction: int  # 0 BUY / 1 SELL
    direction_name: str
    role: int
    role_name: str
    cycle_uid: int
    step: int
    seq: int
    raw: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "direction": self.direction,
            "direction_name": self.direction_name,
            "role": self.role,
            "role_name": self.role_name,
            "cycle_uid": self.cycle_uid,
            "step": self.step,
            "seq": self.seq,
            "raw": self.raw,
        }


def direction_from_str(direction: str) -> int:
    d = str(direction or "").strip().upper()
    if d in ("BUY", "BID", "LONG"):
        return DIRECTION_BUY
    if d in ("SELL", "ASK", "SHORT"):
        return DIRECTION_SELL
    raise ClientIdError(f"invalid direction: {direction!r}")


def direction_to_str(bit: int) -> str:
    return "BUY" if int(bit) == DIRECTION_BUY else "SELL"


def is_golden_fibo_v2_client_id(value: Any) -> bool:
    """True iff *value* is a well-formed V2 GoldenFibo client_order_index."""
    try:
        decode_golden_fibo_client_id(value)
        return True
    except ClientIdError:
        return False


def encode_golden_fibo_client_id(
    *,
    direction: int | str,
    role: int,
    cycle_uid: int,
    step: int,
    seq: int,
    version: int = VERSION,
    magic: int = MAGIC,
) -> int:
    """Encode fields into a 48-bit Lighter-safe client_order_index."""
    if isinstance(direction, str):
        direction = direction_from_str(direction)
    direction = int(direction)
    role = int(role)
    cycle_uid = int(cycle_uid)
    step = int(step)
    seq = int(seq)
    version = int(version)
    magic = int(magic)

    if magic != MAGIC:
        raise ClientIdError(f"magic must be 0x{MAGIC:02X}, got 0x{magic:02X}")
    if version != VERSION:
        raise ClientIdError(f"unsupported version {version}")
    if direction not in (DIRECTION_BUY, DIRECTION_SELL):
        raise ClientIdError(f"invalid direction bit {direction}")
    if role < 0 or role > 7:
        raise ClientIdError(f"invalid role {role}")
    if role not in ROLE_NAMES and role not in (5, 6, 7):
        raise ClientIdError(f"invalid role {role}")
    if cycle_uid < 0 or cycle_uid > MAX_CYCLE_UID:
        raise ClientIdError(f"cycle_uid out of 24-bit range: {cycle_uid}")
    if step < 0 or step > _STEP_MASK:
        raise ClientIdError(f"step out of 5-bit range: {step}")
    if role in (ROLE_STEP0, ROLE_LADDER_ENTRY, ROLE_SHARED_TP, ROLE_REPAIR_ENTRY):
        if step > MAX_STEP_NORMAL and step != STEP_UNKNOWN:
            raise ClientIdError(f"step {step} > {MAX_STEP_NORMAL} for role {role}")
    if role == ROLE_STEP0 and step != 0:
        raise ClientIdError("STEP0 requires step=0")
    if seq < 0 or seq > MAX_SEQ:
        raise ClientIdError(f"seq out of 0..{MAX_SEQ}: {seq}")

    value = 0
    value |= (magic & _MAGIC_MASK) << _MAGIC_SHIFT
    value |= (version & _VER_MASK) << _VER_SHIFT
    value |= (direction & _DIR_MASK) << _DIR_SHIFT
    value |= (role & _ROLE_MASK) << _ROLE_SHIFT
    value |= (cycle_uid & _CYCLE_MASK) << _CYCLE_SHIFT
    value |= (step & _STEP_MASK) << _STEP_SHIFT
    value |= (seq & _SEQ_MASK) << _SEQ_SHIFT

    if value < 0 or value > LIGHTER_MAX_CLIENT_ORDER_INDEX:
        raise ClientIdError(f"encoded id exceeds 48-bit max: {value}")
    return int(value)


def decode_golden_fibo_client_id(value: Any) -> DecodedClientId:
    """Decode a V2 id or raise ClientIdError (never false-positive on legacy)."""
    try:
        raw = int(value)
    except (TypeError, ValueError) as exc:
        raise ClientIdError(f"not an integer client id: {value!r}") from exc
    if raw < 0 or raw > LIGHTER_MAX_CLIENT_ORDER_INDEX:
        raise ClientIdError(f"out of Lighter range: {raw}")

    magic = (raw >> _MAGIC_SHIFT) & _MAGIC_MASK
    if magic != MAGIC:
        raise ClientIdError(f"bad magic 0x{magic:02X}")

    version = (raw >> _VER_SHIFT) & _VER_MASK
    if version != VERSION:
        raise ClientIdError(f"unsupported version {version}")

    direction = (raw >> _DIR_SHIFT) & _DIR_MASK
    role = (raw >> _ROLE_SHIFT) & _ROLE_MASK
    cycle_uid = (raw >> _CYCLE_SHIFT) & _CYCLE_MASK
    step = (raw >> _STEP_SHIFT) & _STEP_MASK
    seq = (raw >> _SEQ_SHIFT) & _SEQ_MASK

    if role not in ROLE_NAMES and role not in (5, 6, 7):
        raise ClientIdError(f"invalid role {role}")
    if role in (ROLE_STEP0, ROLE_LADDER_ENTRY, ROLE_SHARED_TP, ROLE_REPAIR_ENTRY):
        if step > MAX_STEP_NORMAL and step != STEP_UNKNOWN:
            raise ClientIdError(f"invalid step {step} for role {role}")
    if role == ROLE_STEP0 and step != 0:
        raise ClientIdError("STEP0 requires step=0")

    return DecodedClientId(
        version=version,
        direction=direction,
        direction_name=direction_to_str(direction),
        role=role,
        role_name=ROLE_NAMES.get(role, f"RESERVED_{role}"),
        cycle_uid=cycle_uid,
        step=step,
        seq=seq,
        raw=raw,
    )


def epoch_minute_now(
    *,
    now: Optional[datetime] = None,
    epoch: datetime = GOLDEN_FIBO_EPOCH,
) -> int:
    """Minutes since GoldenFibo epoch, clamped to 24 bits."""
    ts = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = ts - epoch
    minutes = int(delta.total_seconds() // 60)
    if minutes < 0:
        minutes = 0
    return minutes & MAX_CYCLE_UID


def allocate_cycle_uid(
    *,
    previous_local_cycle_uid: Optional[int] = None,
    highest_exchange_cycle_uid: Optional[int] = None,
    now: Optional[datetime] = None,
) -> int:
    """Fresh-server-safe monotonic CYCLE_UID.

    candidate = max(
        current_epoch_minute,
        previous_local_cycle_uid + 1 if available,
        highest_exchange_cycle_uid + 1 if recoverable,
    )
    Never silently reuses a known V2 CYCLE_UID.
    """
    cand = epoch_minute_now(now=now)
    if previous_local_cycle_uid is not None:
        cand = max(cand, int(previous_local_cycle_uid) + 1)
    if highest_exchange_cycle_uid is not None:
        cand = max(cand, int(highest_exchange_cycle_uid) + 1)
    if cand < 0:
        cand = 0
    if cand > MAX_CYCLE_UID:
        # Wrap within 24 bits but stay above previous when possible
        cand = cand & MAX_CYCLE_UID
        if previous_local_cycle_uid is not None and cand <= int(previous_local_cycle_uid):
            raise ClientIdError(
                f"cycle_uid space exhausted near {previous_local_cycle_uid}"
            )
    return int(cand)


def _seq_key(role: int, step: int) -> str:
    return f"{int(role)}:{int(step)}"


def next_seq_for(
    seq_map: MutableMapping[str, int],
    *,
    role: int,
    step: int,
) -> int:
    """Return next SEQ for (role, step), mutating *seq_map*. Never wraps."""
    key = _seq_key(role, step)
    # stored value = last used seq; first allocation => 0
    if key not in seq_map:
        seq_map[key] = 0
        return 0
    nxt = int(seq_map[key]) + 1
    if nxt > MAX_SEQ:
        raise SeqExhaustedError(
            f"SEQ exhausted for role={role} step={step} (max {MAX_SEQ})"
        )
    seq_map[key] = nxt
    return nxt


def peek_last_seq(seq_map: Mapping[str, int], *, role: int, step: int) -> Optional[int]:
    key = _seq_key(role, step)
    if key not in seq_map:
        return None
    return int(seq_map[key])


def allocate_client_id(
    *,
    direction: int | str,
    role: int,
    cycle_uid: int,
    step: int,
    seq_map: MutableMapping[str, int],
    reuse_seq: Optional[int] = None,
) -> int:
    """Allocate a V2 client id.

    If *reuse_seq* is set, re-encode the SAME logical order (retry path)
    without bumping SEQ. Otherwise bump SEQ via *seq_map*.
    """
    if reuse_seq is not None:
        seq = int(reuse_seq)
        if seq < 0 or seq > MAX_SEQ:
            raise ClientIdError(f"invalid reuse_seq {seq}")
        # ensure map reflects at least this seq
        key = _seq_key(role, step)
        prev = int(seq_map.get(key, -1))
        if seq > prev:
            seq_map[key] = seq
    else:
        seq = next_seq_for(seq_map, role=role, step=step)
    return encode_golden_fibo_client_id(
        direction=direction,
        role=role,
        cycle_uid=cycle_uid,
        step=step,
        seq=seq,
    )


def scan_highest_cycle_uid_from_client_ids(ids: Any) -> Optional[int]:
    """Return max decoded cycle_uid from an iterable of client ids, or None."""
    best: Optional[int] = None
    if ids is None:
        return None
    for raw in ids:
        try:
            dec = decode_golden_fibo_client_id(raw)
        except ClientIdError:
            continue
        if best is None or dec.cycle_uid > best:
            best = dec.cycle_uid
    return best


__all__ = [
    "MAGIC",
    "VERSION",
    "DIRECTION_BUY",
    "DIRECTION_SELL",
    "ROLE_STEP0",
    "ROLE_LADDER_ENTRY",
    "ROLE_SHARED_TP",
    "ROLE_EMERGENCY_CLOSE",
    "ROLE_REPAIR_ENTRY",
    "ROLE_NAMES",
    "ENGINE_ROLE_TO_V2",
    "MAX_STEP_NORMAL",
    "STEP_UNKNOWN",
    "MAX_SEQ",
    "MAX_CYCLE_UID",
    "LIGHTER_MAX_CLIENT_ORDER_INDEX",
    "GOLDEN_FIBO_EPOCH",
    "ClientIdError",
    "SeqExhaustedError",
    "DecodedClientId",
    "direction_from_str",
    "direction_to_str",
    "is_golden_fibo_v2_client_id",
    "encode_golden_fibo_client_id",
    "decode_golden_fibo_client_id",
    "epoch_minute_now",
    "allocate_cycle_uid",
    "next_seq_for",
    "peek_last_seq",
    "allocate_client_id",
    "scan_highest_cycle_uid_from_client_ids",
]
