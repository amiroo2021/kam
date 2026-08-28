"""Phase 2.13.18 — Cycle-aware Fibo convergence: persistent
runtime state for cycle ownership.

Each active Fibo registration is associated with the MT4
cycle_id under which its exchange exposure was last
synchronized. The MT4 cycle_id is the ownership boundary:
when the current MT4 cycle differs from the synchronized
cycle, the existing exchange position belongs to a previous
cycle and must be closed before opening the new cycle.

State is persisted to ``${HERMES_HOME}/fibo/cycle_state.json``
under the existing Fibo singleton lock. Updates use atomic
file replacement (write-temp + rename).

Schema (version 1)::

    {
        "version": 1,
        "registrations": {
            "<registration_key>": {
                "source":                "...",
                "exchange":              "...",
                "account":               "...",
                "exchange_instrument":   "...",
                "variant":               "...",
                "side":                  "BUY" | "SELL",
                "synchronized_cycle_id": int,
                "transition":            None | str
            }
        }
    }

``transition`` is None in STEADY. During a transition it
records the latest sub-step to support crash-safe resume:

    "CLOSE_SENT"   — we sent a close for the old-cycle exposure
    "CLOSE_VERIFIED"— we verified the instrument is flat
    "OPEN_SENT"    — we sent the new-cycle open
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
from typing import Any, Dict, Optional


SCHEMA_VERSION = 1

# Transition sub-steps.
TRANSITION_NONE = None
TRANSITION_CLOSE_SENT = "CLOSE_SENT"
TRANSITION_CLOSE_VERIFIED = "CLOSE_VERIFIED"
TRANSITION_OPEN_SENT = "OPEN_SENT"

_VALID_TRANSITIONS = {
    TRANSITION_NONE,
    TRANSITION_CLOSE_SENT,
    TRANSITION_CLOSE_VERIFIED,
    TRANSITION_OPEN_SENT,
}


def _default_path() -> pathlib.Path:
    """Resolve the default path of the cycle-state file.

    Honours ``HERMES_HOME`` so tests and production share the
    same convention."""
    base = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return pathlib.Path(base) / "fibo" / "cycle_state.json"


class CycleStateStore:
    """Persistent state store for cycle ownership.

    Thread/process-safe: callers should hold the existing Fibo
    singleton lock (``/root/.hermes/fibo/converge.lock``) for
    read-modify-write. Read-only methods are lock-free.
    """

    def __init__(self, path: Optional[pathlib.Path] = None) -> None:
        self.path = pathlib.Path(path) if path is not None else _default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Ensure file exists so concurrent readers see a valid
        # empty state.
        if not self.path.exists():
            self._atomic_write({"version": SCHEMA_VERSION, "registrations": {}})

    def _read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"version": SCHEMA_VERSION, "registrations": {}}
        try:
            raw = self.path.read_text()
        except OSError:
            return {"version": SCHEMA_VERSION, "registrations": {}}
        if not raw.strip():
            return {"version": SCHEMA_VERSION, "registrations": {}}
        try:
            data = json.loads(raw)
        except (OSError, ValueError):
            return {"version": SCHEMA_VERSION, "registrations": {}}
        if not isinstance(data, dict):
            return {"version": SCHEMA_VERSION, "registrations": {}}
        if "registrations" not in data or not isinstance(data["registrations"], dict):
            data["registrations"] = {}
        return data

    def _atomic_write(self, data: Dict[str, Any]) -> None:
        """Write ``data`` atomically: write-temp + rename."""
        # Ensure parent dir exists and is 0700.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        fd, tmp_path = tempfile.mkstemp(
            prefix=".cycle_state.", suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp_path, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _write(self, data: Dict[str, Any]) -> None:
        self._atomic_write(data)

    # --- public read API -------------------------------------------------

    def get_state(
        self, registration_key: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the persisted state for ``registration_key`` or
        ``None`` if no state is recorded."""
        data = self._read()
        return data.get("registrations", {}).get(registration_key)

    def get_synchronized_cycle_id(self, registration_key: str) -> Optional[int]:
        """Return the persisted synchronized cycle_id, or None if
        no state is recorded yet (legacy / first adoption)."""
        st = self.get_state(registration_key)
        if st is None:
            return None
        return st.get("synchronized_cycle_id")

    def get_transition(self, registration_key: str) -> Optional[str]:
        """Return the current transition sub-step, or None if in
        STEADY state."""
        st = self.get_state(registration_key)
        if st is None:
            return None
        return st.get("transition")

    # --- public write API (must be called under Fibo singleton
    #     lock for read-modify-write atomicity) -----------------------

    def adopt_first_cycle(
        self,
        registration_key: str,
        *,
        source: str,
        exchange: str,
        account: str,
        exchange_instrument: str,
        variant: str,
        side: str,
        cycle_id: int,
    ) -> None:
        """Initialize state for a fresh registration whose
        exchange position is already known to be flat.

        Sets ``synchronized_cycle_id = cycle_id`` and
        ``transition = None``."""
        data = self._read()
        data["registrations"][registration_key] = {
            "source": source,
            "exchange": exchange,
            "account": account,
            "exchange_instrument": exchange_instrument,
            "variant": variant,
            "side": side,
            "synchronized_cycle_id": int(cycle_id),
            "transition": TRANSITION_NONE,
        }
        self._write(data)

    def begin_transition_close_sent(
        self,
        registration_key: str,
        old_cycle_id: int,
    ) -> None:
        """Persist: a CLOSE order has been sent for the old cycle."""
        data = self._read()
        st = data["registrations"].setdefault(
            registration_key,
            {},
        )
        st["synchronized_cycle_id"] = int(old_cycle_id)
        st["transition"] = TRANSITION_CLOSE_SENT
        self._write(data)

    def advance_transition_close_verified(
        self,
        registration_key: str,
        old_cycle_id: int,
    ) -> None:
        """Persist: exchange is verified FLAT after the CLOSE."""
        data = self._read()
        st = data["registrations"].setdefault(
            registration_key,
            {},
        )
        st["synchronized_cycle_id"] = int(old_cycle_id)
        st["transition"] = TRANSITION_CLOSE_VERIFIED
        self._write(data)

    def advance_transition_open_sent(
        self,
        registration_key: str,
        new_cycle_id: int,
    ) -> None:
        """Persist: an OPEN order has been sent for the new cycle.
        ``synchronized_cycle_id`` is updated to the new cycle
        only after the resulting exposure is verified.
        """
        data = self._read()
        st = data["registrations"].setdefault(
            registration_key,
            {},
        )
        # NOTE: do NOT update synchronized_cycle_id yet. We are
        # not in STEADY until the open fills and is verified.
        st["transition"] = TRANSITION_OPEN_SENT
        self._write(data)

    def finalize_transition(
        self,
        registration_key: str,
        new_cycle_id: int,
    ) -> None:
        """Persist: the new cycle's open is verified. Enter
        STEADY."""
        data = self._read()
        st = data["registrations"].setdefault(
            registration_key,
            {},
        )
        st["synchronized_cycle_id"] = int(new_cycle_id)
        st["transition"] = TRANSITION_NONE
        self._write(data)

    def finalize_inactive(
        self,
        registration_key: str,
    ) -> None:
        """Persist: target is zero, exchange is flat. Enter
        STEADY with cycle_id = 0."""
        data = self._read()
        st = data["registrations"].setdefault(
            registration_key,
            {},
        )
        st["synchronized_cycle_id"] = 0
        st["transition"] = TRANSITION_NONE
        self._write(data)

    def clear(self, registration_key: str) -> None:
        """Remove persisted state for a registration (used when
        a registration is fully stopped/removed)."""
        data = self._read()
        if registration_key in data.get("registrations", {}):
            del data["registrations"][registration_key]
            self._write(data)


def cycle_state_path_for(registration_key: str) -> pathlib.Path:
    """Convenience: return the path used for the cycle state
    file (only one file shared across all registrations)."""
    return _default_path()
