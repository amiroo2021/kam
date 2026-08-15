"""OndoPerps Fibo adapter — cumulative-counter model (LOCKED v1).

Translates Fibo's ``ExchangeAdapter`` Protocol into calls to
``x_ondoperps_agent.execute(...)``. Zero exchange protocol details live
here. All HTTP, signing, credential lookup, account alias handling,
symbol resolution, price / quantity formatting, and order submission go
through ``x_ondoperps_agent`` — the SAME agent used by the /trade wizard.

POSITION-SCOPING LIMITATION (read carefully before live testing):

  OndoPerps maintains ONE TP + ONE SL per (account, instrument,
  direction) net position. Normal orders support ``clientOrderId`` on
  ``/v1/perps/orders``, but the protective ``/v1/perps/stop_order`` path
  still has NO client-order-id tagging. The
  cumulative Fibo position is implemented on top of this primitive.

  Consequences:
    - If the user (or /trade) ALREADY has a MANUAL position in the
      same (account, instrument, direction), the protective record we
      install protects the WHOLE NET POSITION (manual + Fibo) — not
      just the Fibo share.
    - If the user later opens / closes a manual position on the same
      side, the protective record's behaviour is unchanged, but the
      *ratio* of Fibo share to total exposure shifts.
    - Two Fibo registrations on the SAME (account, instrument,
      counterType) would race over the same protective record. The
      manager's ``start()`` rejects a duplicate key, so this is
      prevented at registration time.
    - Different accounts (e.g. ``amiroo`` vs ``bitget``) keep their
      protective records fully isolated.

  We cannot tag the protective record as Fibo-owned. The locked model
  accepts this limitation; the alternative would require OndoPerps
  upstream support that does not exist today.

CUMULATIVE-POSITION MODEL

  The adapter implements these cumulative-position operations against
  the OndoPerps ``/v1/perps/orders`` + ``/v1/perps/stop_order`` API:

    1. ``submit_volume_market_order``  → POST ``new_order`` (market,
       normal entry body with no reduce-only close flag).
    2. ``confirm_cumulative_position`` → read ``position_state`` and
       verify a row exists for (instrument, side) with size > 0.
    3. ``set_cumulative_sl``             → POST ``set_position_protections``
       with only the SL populated (TP cleared if present).
    4. ``verify_cumulative_sl``          → re-read ``position_state`` and
       confirm the SL matches the expected price.
    5. ``set_cumulative_tp``             → POST ``set_position_protections``
       with only the TP populated (SL cleared if present).
    6. ``verify_cumulative_tp``          → re-read ``position_state`` and
       confirm the TP matches the expected price.
    7. ``current_protection_state``      → read ``position_state`` and
       return ``(sl_price, tp_price)``.

  Steps 3-6 use the existing ``set_position_protections`` agent
  operation. That operation accepts both TP and SL atomically; we set
  only the leg we want to update and pass a sentinel value for the
  other leg so the agent doesn't disturb it. The agent's current
  ``_set_position_protections`` implementation requires both prices —
  we will route around this by using ``_set_position_trigger`` directly
  for single-leg updates (the underlying primitives already exist on
  the agent).

  For ``set_cumulative_tp`` we use ``_set_position_trigger(...,
  kind="takeProfit")`` and do NOT touch SL. For ``set_cumulative_sl`` we
  use ``_set_position_trigger(..., kind="stopLoss")`` and do NOT touch
  TP. This matches the locked model: "DO NOT touch unrelated manual TP
  orders."

  Verifications are done via ``position_state`` (cheap re-read of the
  per-(market, direction) snapshot).
"""

from __future__ import annotations

import re
import secrets
import string
import time
import zlib
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional, Tuple

from ..engine import ExchangeAdapter, RealOrderSide


_FIBO_SIDE_TO_ONDO = {
    RealOrderSide.BUY: "buy",
    RealOrderSide.SELL: "sell",
}
_RUN_ID_ALPHABET = string.ascii_uppercase + string.digits
_TYPE_SHORT = {
    "counterBUY": "CB",
    "counterSELL": "CS",
}


def _is_success(response: Any) -> bool:
    if response is None:
        return False
    if isinstance(response, dict):
        return bool(response.get("success"))
    return bool(getattr(response, "success", False))


def _error_code(response: Any) -> str:
    if response is None:
        return ""
    if isinstance(response, dict):
        err = response.get("error") or {}
        if isinstance(err, dict):
            return str(err.get("code") or "")
        return ""
    err = getattr(response, "error", None)
    if err is None:
        return ""
    return str(getattr(err, "code", "") or "")


def _error_message(response: Any) -> str:
    if response is None:
        return ""
    if isinstance(response, dict):
        err = response.get("error") or {}
        if isinstance(err, dict):
            return str(err.get("message") or "")
        return ""
    err = getattr(response, "error", None)
    if err is None:
        return ""
    return str(getattr(err, "message", "") or "")


def _positions_list(response: Any) -> list:
    if response is None:
        return []
    if isinstance(response, dict):
        return list(response.get("positions") or [])
    return list(getattr(response, "positions", None) or [])


def _order_payload(response: Any) -> Any:
    if response is None:
        return None
    if isinstance(response, dict):
        return response.get("order")
    return getattr(response, "order", None)


def _decimal_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "0"
    try:
        decimal_value = Decimal(text)
    except Exception:  # noqa: BLE001
        return text
    rendered = format(decimal_value, "f")
    if rendered in {"-0", "-0.0"}:
        return "0"
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".") or "0"
    return rendered


def _position_action_payload(response: Any) -> Any:
    if response is None:
        return None
    if isinstance(response, dict):
        return response.get("position_action")
    return getattr(response, "position_action", None)


def _position_action_verified(response: Any) -> bool:
    """Whether the canonical position_action was verified."""
    pa = _position_action_payload(response)
    if pa is None:
        return False
    if isinstance(pa, dict):
        return bool(pa.get("verified"))
    return bool(getattr(pa, "verified", False))


def _instrument_payload(response: Any) -> Any:
    if response is None:
        return None
    if isinstance(response, dict):
        return response.get("instrument")
    return getattr(response, "instrument", None)


def _price_increment_decimal(response: Any) -> Optional[Decimal]:
    instrument = _instrument_payload(response)
    if instrument is None:
        return None
    if isinstance(instrument, dict):
        raw = instrument.get("price_increment")
    else:
        raw = getattr(instrument, "price_increment", None)
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        inc = Decimal(text)
    except Exception:  # noqa: BLE001
        return None
    return inc if inc > 0 else None


def _quantize_price_text(value: Any, increment: Optional[Decimal]) -> str:
    decimal_value = Decimal(str(value))
    if increment is not None and increment > 0:
        decimal_value = (decimal_value / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * increment
    return _decimal_text(decimal_value)


def _position_action_removed(response: Any) -> bool:
    pa = _position_action_payload(response)
    if pa is None:
        return False
    if isinstance(pa, dict):
        return bool(pa.get("removed"))
    return bool(getattr(pa, "removed", False))


def _position_action_current_side(response: Any) -> str:
    pa = _position_action_payload(response)
    if pa is None:
        return ""
    if isinstance(pa, dict):
        return str(pa.get("current_side") or "")
    return str(getattr(pa, "current_side", "") or "")


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if hasattr(value, "to_dict") and callable(getattr(value, "to_dict")):
        try:
            return _plain(value.to_dict())
        except Exception:  # noqa: BLE001
            return repr(value)
    if hasattr(value, "__dict__"):
        try:
            return _plain(vars(value))
        except Exception:  # noqa: BLE001
            return repr(value)
    return repr(value)


class OndoPerpsFiboAdapter:
    """Concrete ``ExchangeAdapter`` for OndoPerps — cumulative model."""

    def __init__(
        self,
        exchange_name: str,
        account_alias: str,
        agent: Any,
    ) -> None:
        self.exchange_name = str(exchange_name)
        self.account_alias = str(account_alias)
        self._agent = agent
        self._run_id_by_instance: dict[str, str] = {}
        self._cycle_by_instance: dict[str, int] = {}
        self._last_step_by_instance: dict[str, int] = {}
        self._last_submit_by_instance: dict[str, dict[str, Any]] = {}
        self._last_confirm_by_instance: dict[str, dict[str, Any]] = {}
        self._last_protection_by_instance: dict[str, dict[str, Any]] = {}
        self._confirm_attempts: int = 5
        self._confirm_delay_seconds: float = 0.5
        self._sleep_fn = time.sleep
        self._event_sink = None

    def set_event_sink(self, sink: Any) -> None:
        self._event_sink = sink

    def _emit(self, event: str, **fields: Any) -> None:
        if self._event_sink is None:
            return
        payload = {"event": event}
        payload.update({k: _plain(v) for k, v in fields.items()})
        try:
            self._event_sink(payload)
        except Exception:  # noqa: BLE001
            return

    @staticmethod
    def _new_run_id(length: int = 4) -> str:
        return "".join(secrets.choice(_RUN_ID_ALPHABET) for _ in range(length))

    def on_registration_started(self, instance_key: str) -> None:
        key = str(instance_key)
        self._run_id_by_instance[key] = self._new_run_id()
        self._cycle_by_instance[key] = 1
        self._last_step_by_instance.pop(key, None)

    def on_registration_stopped(self, instance_key: str) -> None:
        key = str(instance_key)
        self._run_id_by_instance.pop(key, None)
        self._cycle_by_instance.pop(key, None)
        self._last_step_by_instance.pop(key, None)
        self._last_submit_by_instance.pop(key, None)
        self._last_confirm_by_instance.pop(key, None)
        self._last_protection_by_instance.pop(key, None)

    @staticmethod
    def _safe_token(value: Any) -> str:
        text = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "").strip())
        return text.strip("_") or "X"

    def _shorten_client_order_id(
        self,
        *,
        account_token: str,
        instrument_token: str,
        type_short: str,
        run_id: str,
        cycle: int,
        counter_step: int,
        instance_key: str,
        limit: int = 64,
    ) -> str:
        raw = (
            f"FIBO_{account_token}_{instrument_token}_{type_short}_{run_id}"
            f"_Y{int(cycle)}_C{int(counter_step)}"
        )
        if len(raw) <= limit:
            return raw

        reg_hash = f"{zlib.crc32(str(instance_key).encode('utf-8')) & 0xffffffff:08X}"
        suffix = f"_{type_short}_{reg_hash}_{run_id}_Y{int(cycle)}_C{int(counter_step)}"
        prefix = "FIBO_"
        budget = limit - len(prefix) - len(suffix) - 1
        if budget < 2:
            raise ValueError("Client order ID budget exhausted")

        # Split remaining readable budget across account/instrument and keep at
        # least one character from each, with deterministic shortening.
        account_budget = max(1, budget // 2)
        instrument_budget = max(1, budget - account_budget)
        account_short = account_token[:account_budget]
        instrument_short = instrument_token[:instrument_budget]
        shortened = f"{prefix}{account_short}_{instrument_short}{suffix}"
        if len(shortened) > limit:
            overflow = len(shortened) - limit
            if len(instrument_short) > len(account_short) and len(instrument_short) > 1:
                instrument_short = instrument_short[:-overflow]
            else:
                account_short = account_short[:-overflow]
            shortened = f"{prefix}{account_short}_{instrument_short}{suffix}"
        return shortened

    def _next_cycle_number(self, instance_key: str, counter_step: int) -> int:
        key = str(instance_key)
        step = int(counter_step)
        cycle = self._cycle_by_instance.get(key)
        last_step = self._last_step_by_instance.get(key)
        if cycle is None:
            cycle = 1
        elif last_step is not None and step <= last_step:
            cycle += 1
        self._cycle_by_instance[key] = cycle
        self._last_step_by_instance[key] = step
        return cycle

    def _client_order_id_for(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
        counter_step: int,
    ) -> str:
        parts = str(instance_key).split(":")
        counter_type = parts[-1] if parts and str(parts[-1]).startswith("counter") else (
            "counterBUY" if side == RealOrderSide.BUY else "counterSELL"
        )
        type_short = _TYPE_SHORT.get(counter_type, self._safe_token(counter_type))
        cycle = self._next_cycle_number(instance_key, counter_step)
        run_id = self._run_id_by_instance.get(str(instance_key))
        if run_id is None:
            self.on_registration_started(str(instance_key))
            run_id = self._run_id_by_instance[str(instance_key)]
        return self._shorten_client_order_id(
            account_token=self._safe_token(self.account_alias),
            instrument_token=self._safe_token(instrument),
            type_short=type_short,
            run_id=run_id,
            cycle=cycle,
            counter_step=counter_step,
            instance_key=instance_key,
        )

    def last_submission_context(self, instance_key: str) -> dict[str, Any]:
        return dict(self._last_submit_by_instance.get(str(instance_key)) or {})

    def last_confirmation_diagnostic(self, instance_key: str) -> dict[str, Any]:
        return dict(self._last_confirm_by_instance.get(str(instance_key)) or {})

    def _protection_expectation(
        self,
        *,
        instance_key: str,
        instrument: str,
        kind: str,
        raw_price: float,
        response: Any = None,
    ) -> dict[str, Any]:
        key = str(instance_key)
        raw_target = _decimal_text(raw_price)
        response_increment = _price_increment_decimal(response)
        quantized_text = ""
        if response is not None:
            action = _position_action_payload(response)
            if isinstance(action, dict):
                quantized_text = str(action.get("price") or "").strip()
            else:
                quantized_text = str(getattr(action, "price", "") or "").strip() if action is not None else ""
        stored = self._last_protection_by_instance.get(key) or {}
        if not quantized_text and stored.get("kind") == kind and stored.get("raw_target") == raw_target:
            quantized_text = str(stored.get("expected_quantized") or "").strip()
        if not quantized_text:
            increment = response_increment if response_increment is not None else stored.get("price_increment")
            if isinstance(increment, str):
                try:
                    increment = Decimal(increment)
                except Exception:  # noqa: BLE001
                    increment = None
            try:
                quantized_text = _quantize_price_text(raw_target, increment)
            except Exception:  # noqa: BLE001
                quantized_text = raw_target
        expectation = {
            "registration_key": key,
            "instrument": str(instrument).upper(),
            "kind": kind,
            "raw_target": raw_target,
            "expected_quantized": quantized_text or raw_target,
            "price_increment": _decimal_text(response_increment) if response_increment is not None else stored.get("price_increment"),
            "response": _plain(response) if response is not None else stored.get("response"),
        }
        self._last_protection_by_instance[key] = expectation
        return dict(expectation)

    def _verify_protection(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
        raw_price: float,
        kind: str,
    ) -> bool:
        key = str(instance_key)
        expected_side = "long" if side == RealOrderSide.BUY else "short"
        previous_ctx = dict(self._last_protection_by_instance.get(key) or {})
        previous_verified_value = previous_ctx.get("last_verified_raw")
        diagnostic = self._protection_expectation(
            instance_key=instance_key,
            instrument=instrument,
            kind=kind,
            raw_price=raw_price,
            response=None,
        )
        attempts = max(1, int(self._confirm_attempts))
        diagnostic.update({
            "verification_attempts": 0,
            "attempts": [],
            "reason_code": f"{kind.upper()}_VERIFY_FAILED",
            "overall_result": False,
            "previous_verified": _decimal_text(previous_verified_value) if previous_verified_value is not None else None,
            "expected_position_side": expected_side,
        })
        event_name = "sl_verification_attempt" if kind == "sl" else "tp_verification_attempt"
        field_name = "sl" if kind == "sl" else "tp"
        expected_quantized = str(diagnostic.get("expected_quantized") or "")
        expected_decimal = Decimal(expected_quantized)
        for attempt in range(1, attempts + 1):
            response = self._agent.execute({
                "operation": "position_state",
                "exchange": self.exchange_name,
                "account": self.account_alias,
                "symbol": instrument,
            })
            response_success = _is_success(response)
            actual_exchange_value = None
            actual_decimal = None
            comparison_result = False
            matched_symbol = False
            matched_side = False
            increment = _price_increment_decimal(response)
            if increment is not None:
                diagnostic["price_increment"] = _decimal_text(increment)
                if (not expected_quantized) or expected_quantized == str(diagnostic.get("raw_target") or ""):
                    expected_quantized = _quantize_price_text(raw_price, increment)
                    expected_decimal = Decimal(expected_quantized)
                    diagnostic["expected_quantized"] = expected_quantized
                    self._last_protection_by_instance[key] = dict(diagnostic)
            if response_success:
                for pos in _positions_list(response):
                    if isinstance(pos, dict):
                        pos_symbol = str(pos.get("symbol") or "").upper()
                        pos_side = str(pos.get("side") or "").lower()
                        actual_text = str(pos.get(field_name) or "").strip()
                    else:
                        pos_symbol = str(getattr(pos, "symbol", "") or "").upper()
                        pos_side = str(getattr(pos, "side", "") or "").lower()
                        actual_text = str(getattr(pos, field_name, "") or "").strip()
                    if pos_symbol != instrument.upper():
                        continue
                    matched_symbol = True
                    if pos_side != expected_side:
                        continue
                    matched_side = True
                    actual_exchange_value = actual_text or None
                    if actual_exchange_value:
                        try:
                            actual_decimal = Decimal(actual_exchange_value)
                            comparison_result = (actual_decimal == expected_decimal)
                        except Exception:  # noqa: BLE001
                            comparison_result = actual_exchange_value == expected_quantized
                    break
            attempt_payload = {
                "attempt": attempt,
                "response_success": response_success,
                "actual_exchange_%s" % field_name: actual_exchange_value,
                "raw_target_%s" % field_name: diagnostic.get("raw_target"),
                "expected_quantized_%s" % field_name: diagnostic.get("expected_quantized"),
                "previous_verified_%s" % field_name: diagnostic.get("previous_verified"),
                "comparison_result": comparison_result,
                "symbol_matches": matched_symbol,
                "side_matches": matched_side,
                "error": _error_message(response),
            }
            self._emit(
                event_name,
                registration_key=key,
                instrument=str(instrument).upper(),
                requested_order_side=_FIBO_SIDE_TO_ONDO.get(side, str(side).lower()),
                exchange_quote_increment=diagnostic.get("price_increment"),
                **attempt_payload,
            )
            diagnostic["attempts"].append(attempt_payload)
            diagnostic["verification_attempts"] = attempt
            if comparison_result:
                diagnostic["reason_code"] = "OK"
                diagnostic["overall_result"] = True
                diagnostic["last_verified_raw"] = diagnostic.get("raw_target")
                diagnostic["last_verified_quantized"] = actual_exchange_value
                self._last_protection_by_instance[key] = dict(diagnostic)
                return True
            if attempt < attempts:
                self._sleep_fn(self._confirm_delay_seconds)
        self._last_protection_by_instance[key] = dict(diagnostic)
        return False

    # ------------------------------------------------------------------
    # ExchangeAdapter Protocol — cumulative-position surface
    # ------------------------------------------------------------------

    def submit_volume_market_order(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
        counter_step: int,
        volume: float,
    ) -> str:
        """Submit a non-reduce-only MARKET counter volume."""
        ondo_side = _FIBO_SIDE_TO_ONDO.get(side, str(side).lower())
        client_order_id = self._client_order_id_for(
            instance_key=instance_key,
            instrument=instrument,
            side=side,
            counter_step=counter_step,
        )
        self._emit(
            "client_order_id_prepared",
            registration_key=str(instance_key),
            instrument=str(instrument).upper(),
            client_order_id=client_order_id,
            requested_order_side=ondo_side,
            requested_volume=_decimal_text(volume),
            counter_step=int(counter_step),
        )
        response = self._agent.execute({
            "operation": "new_order",
            "exchange": self.exchange_name,
            "account": self.account_alias,
            "symbol": instrument,
            "side": ondo_side,
            "order_type": "market",
            "reduce_only": False,
            "client_order_id": client_order_id,
            "volume": _decimal_text(volume),
        })
        self._emit(
            "order_create_response",
            registration_key=str(instance_key),
            instrument=str(instrument).upper(),
            client_order_id=client_order_id,
            response_success=_is_success(response),
            response=_plain(response),
        )
        if not _is_success(response):
            raise RuntimeError(
                f"{_error_code(response) or 'ONDOPERPS_NEW_ORDER_FAILED'}: "
                f"{_error_message(response)}".strip(": ")
            )
        self._emit(
            "post_submit_success",
            registration_key=str(instance_key),
            instrument=str(instrument).upper(),
            client_order_id=client_order_id,
            response=_plain(response),
        )
        order = _order_payload(response)
        if order is None:
            raise RuntimeError("ONDOPERPS_NEW_ORDER_NO_ORDER_OBJECT")
        if isinstance(order, dict):
            oid = str(order.get("exchange_order_id") or "").strip()
        else:
            oid = str(getattr(order, "exchange_order_id", "") or "").strip()
        if oid:
            self._emit(
                "inline_order_id_present",
                registration_key=str(instance_key),
                instrument=str(instrument).upper(),
                client_order_id=client_order_id,
                exchange_order_id=oid,
            )
        else:
            self._emit(
                "inline_order_id_absent",
                registration_key=str(instance_key),
                instrument=str(instrument).upper(),
                client_order_id=client_order_id,
            )

        verify_attempts = max(1, int(self._confirm_attempts))
        verify_error = ""
        verify_response = None
        for attempt in range(1, verify_attempts + 1):
            verify_error = ""
            try:
                verify_response = self._agent.execute({
                    "operation": "get_exact_order",
                    "exchange": self.exchange_name,
                    "account": self.account_alias,
                    "symbol": instrument,
                    "side": ondo_side,
                    "volume": _decimal_text(volume),
                    "client_order_id": client_order_id,
                })
            except Exception as exc:  # noqa: BLE001
                verify_response = None
                verify_error = f"{type(exc).__name__}: {exc}"

            verify_success = _is_success(verify_response)
            verified_order = _order_payload(verify_response)
            if verify_success and verified_order is not None:
                if isinstance(verified_order, dict):
                    looked_up_oid = str(verified_order.get("exchange_order_id") or "").strip()
                else:
                    looked_up_oid = str(getattr(verified_order, "exchange_order_id", "") or "").strip()
                if looked_up_oid:
                    oid = looked_up_oid
            self._emit(
                "client_lookup_attempt",
                registration_key=str(instance_key),
                instrument=str(instrument).upper(),
                client_order_id=client_order_id,
                attempt=attempt,
                response_success=verify_success,
                error=verify_error or _error_message(verify_response),
                exchange_order_id=oid or None,
                response=_plain(verify_response),
            )
            if verify_success and oid:
                break
            if attempt < verify_attempts:
                self._sleep_fn(self._confirm_delay_seconds)
        else:
            self._emit(
                "order_verify_failed",
                registration_key=str(instance_key),
                instrument=str(instrument).upper(),
                client_order_id=client_order_id,
                error=verify_error or _error_message(verify_response),
                response=_plain(verify_response),
            )
            raise RuntimeError(
                f"ORDER_VERIFY_FAILED: {(verify_error or _error_message(verify_response) or 'exact clientOrderId lookup timed out') }"
            )

        self._last_submit_by_instance[str(instance_key)] = {
            "registration_key": str(instance_key),
            "instrument": str(instrument).upper(),
            "client_order_id": client_order_id,
            "exchange_order_id": oid,
            "requested_order_side": ondo_side,
            "expected_position_side": "long" if ondo_side == "buy" else "short",
            "requested_volume": _decimal_text(volume),
        }
        self._emit(
            "exact_order_verified",
            registration_key=str(instance_key),
            instrument=str(instrument).upper(),
            client_order_id=client_order_id,
            exchange_order_id=oid,
            response=_plain(response),
        )
        return oid

    def confirm_cumulative_position(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
        expected_size: Optional[float] = None,
    ) -> bool:
        """True iff a row exists for ``(instrument, fibo_side_to_canonical_side(side))``
        with size > 0.
        """
        key = str(instance_key)
        ondo_side = _FIBO_SIDE_TO_ONDO.get(side, str(side).lower())
        expected_canonical_side = "long" if ondo_side == "buy" else "short"
        expected_size_decimal = None
        if expected_size is not None:
            expected_size_decimal = Decimal(str(expected_size))
        attempts = max(1, int(self._confirm_attempts))
        diagnostic: dict[str, Any] = {
            "registration_key": key,
            "instrument": str(instrument).upper(),
            "requested_order_side": ondo_side,
            "expected_position_side": expected_canonical_side,
            "expected_size": _decimal_text(expected_size_decimal) if expected_size_decimal is not None else None,
            "verification_attempts": 0,
            "attempts": [],
            "reason_code": "POSITION_CONFIRM_FAILED",
            "overall_result": False,
        }

        for attempt in range(1, attempts + 1):
            response = None
            error_text = ""
            try:
                response = self._agent.execute({
                    "operation": "position_state",
                    "exchange": self.exchange_name,
                    "account": self.account_alias,
                    "symbol": instrument,
                })
            except Exception as exc:  # noqa: BLE001
                error_text = f"{type(exc).__name__}: {exc}"

            attempt_rows = []
            row_symbol_match = False
            row_side_match = False
            row_size_match = False
            overall_result = False
            response_success = _is_success(response)
            native_market = None
            native_symbol = None
            native_direction = None
            instrument_payload = None
            if isinstance(response, dict):
                instrument_payload = response.get("instrument")
            elif response is not None:
                instrument_payload = getattr(response, "instrument", None)
            if isinstance(instrument_payload, dict):
                native_market = instrument_payload.get("display_name")
                native_symbol = instrument_payload.get("symbol")
            elif instrument_payload is not None:
                native_market = getattr(instrument_payload, "display_name", None)
                native_symbol = getattr(instrument_payload, "symbol", None)

            if response_success:
                for pos in _positions_list(response):
                    if isinstance(pos, dict):
                        pos_symbol = str(pos.get("symbol") or "").upper()
                        pos_side = str(pos.get("side") or "").lower()
                        size_text = str(pos.get("size") or "0")
                        entry_price = pos.get("entry_price")
                        pnl = pos.get("pnl")
                        tp = pos.get("tp")
                        sl = pos.get("sl")
                    else:
                        pos_symbol = str(getattr(pos, "symbol", "") or "").upper()
                        pos_side = str(getattr(pos, "side", "") or "").lower()
                        size_text = str(getattr(pos, "size", "0") or "0")
                        entry_price = getattr(pos, "entry_price", None)
                        pnl = getattr(pos, "pnl", None)
                        tp = getattr(pos, "tp", None)
                        sl = getattr(pos, "sl", None)
                    try:
                        size = Decimal(size_text or "0")
                    except Exception:  # noqa: BLE001
                        size = Decimal("0")
                    symbol_matches = pos_symbol == str(instrument).upper()
                    side_matches = pos_side == expected_canonical_side
                    size_matches = size > 0 and (
                        expected_size_decimal is None or size >= expected_size_decimal
                    )
                    overall = symbol_matches and side_matches and size_matches
                    row_symbol_match = row_symbol_match or symbol_matches
                    row_side_match = row_side_match or (symbol_matches and side_matches)
                    row_size_match = row_size_match or (symbol_matches and side_matches and size_matches)
                    overall_result = overall_result or overall
                    attempt_rows.append({
                        "symbol": pos_symbol,
                        "native_market": native_market,
                        "canonical_side": pos_side,
                        "raw_direction": native_direction,
                        "size": _decimal_text(size),
                        "entry_price": entry_price,
                        "pnl": pnl,
                        "tp": tp,
                        "sl": sl,
                        "symbol_matches": symbol_matches,
                        "side_matches": side_matches,
                        "size_matches": size_matches,
                        "overall_result": overall,
                    })

            attempt_payload = {
                "attempt": attempt,
                "response_success": response_success,
                "error": error_text or _error_message(response),
                "instrument_payload_symbol": native_symbol,
                "instrument_payload_market": native_market,
                "position_exists": bool(attempt_rows),
                "symbol_matches": row_symbol_match,
                "side_matches": row_side_match,
                "size_matches": row_size_match,
                "overall_result": overall_result,
                "positions": attempt_rows,
            }
            self._emit(
                "position_confirmation_attempt",
                registration_key=key,
                instrument=str(instrument).upper(),
                requested_order_side=ondo_side,
                expected_position_side=expected_canonical_side,
                expected_size=_decimal_text(expected_size_decimal) if expected_size_decimal is not None else None,
                **attempt_payload,
            )
            diagnostic["attempts"].append(attempt_payload)
            diagnostic["verification_attempts"] = attempt
            if overall_result:
                diagnostic["reason_code"] = "OK"
                diagnostic["overall_result"] = True
                self._last_confirm_by_instance[key] = diagnostic
                return True
            if attempt < attempts:
                self._sleep_fn(self._confirm_delay_seconds)

        if diagnostic["attempts"] and not any(a.get("response_success") for a in diagnostic["attempts"]):
            diagnostic["reason_code"] = "POSITION_STATE_READ_FAILED"
        self._last_confirm_by_instance[key] = diagnostic
        return False

    def set_cumulative_sl(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
        sl_price: float,
    ) -> bool:
        """Install/replace the SL. Uses the agent's ``set_sl`` op which
        leaves the existing TP intact.
        """
        response = self._agent.execute({
            "operation": "set_sl",
            "exchange": self.exchange_name,
            "account": self.account_alias,
            "symbol": instrument,
            "price": _decimal_text(sl_price),
        })
        self._emit(
            "sl_set_response",
            registration_key=str(instance_key),
            instrument=str(instrument).upper(),
            requested_order_side=_FIBO_SIDE_TO_ONDO.get(side, str(side).lower()),
            sl_raw=_decimal_text(sl_price),
            response_success=_is_success(response),
            response=_plain(response),
        )
        self._protection_expectation(
            instance_key=instance_key,
            instrument=instrument,
            kind="sl",
            raw_price=sl_price,
            response=response,
        )
        return _is_success(response)

    def verify_cumulative_sl(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
        sl_price: float,
    ) -> bool:
        """Re-read ``position_state`` and confirm the SL matches the expected quantized price."""
        return self._verify_protection(
            instance_key=instance_key,
            instrument=instrument,
            side=side,
            raw_price=sl_price,
            kind="sl",
        )

    def set_cumulative_tp(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
        tp_price: float,
    ) -> bool:
        """Install/replace the TP. Uses the agent's ``set_tp`` op which
        leaves the existing SL intact.
        """
        response = self._agent.execute({
            "operation": "set_tp",
            "exchange": self.exchange_name,
            "account": self.account_alias,
            "symbol": instrument,
            "price": _decimal_text(tp_price),
        })
        self._emit(
            "tp_set_response",
            registration_key=str(instance_key),
            instrument=str(instrument).upper(),
            requested_order_side=_FIBO_SIDE_TO_ONDO.get(side, str(side).lower()),
            tp_raw=_decimal_text(tp_price),
            response_success=_is_success(response),
            response=_plain(response),
        )
        self._protection_expectation(
            instance_key=instance_key,
            instrument=instrument,
            kind="tp",
            raw_price=tp_price,
            response=response,
        )
        return _is_success(response)

    def verify_cumulative_tp(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
        tp_price: float,
    ) -> bool:
        """Re-read ``position_state`` and confirm the TP matches the expected quantized price."""
        return self._verify_protection(
            instance_key=instance_key,
            instrument=instrument,
            side=side,
            raw_price=tp_price,
            kind="tp",
        )

    def current_protection_state(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
    ) -> Tuple[Optional[float], Optional[float]]:
        """Return ``(sl_price, tp_price)`` currently installed on the
        position. ``None`` for legs that are absent.
        """
        response = self._agent.execute({
            "operation": "position_state",
            "exchange": self.exchange_name,
            "account": self.account_alias,
            "symbol": instrument,
        })
        if not _is_success(response):
            return (None, None)
        for pos in _positions_list(response):
            if isinstance(pos, dict):
                pos_symbol = str(pos.get("symbol") or "").upper()
                sl_text = str(pos.get("sl") or "").strip()
                tp_text = str(pos.get("tp") or "").strip()
            else:
                pos_symbol = str(getattr(pos, "symbol", "")).upper()
                sl_text = str(getattr(pos, "sl", "") or "").strip()
                tp_text = str(getattr(pos, "tp", "") or "").strip()
            if pos_symbol != instrument.upper():
                continue
            sl_price: Optional[float] = None
            tp_price: Optional[float] = None
            if sl_text:
                try:
                    sl_price = float(Decimal(sl_text))
                except Exception:  # noqa: BLE001
                    sl_price = None
            if tp_text:
                try:
                    tp_price = float(Decimal(tp_text))
                except Exception:  # noqa: BLE001
                    tp_price = None
            return (sl_price, tp_price)
        return (None, None)

    # ------------------------------------------------------------------
    # Optional cleanup hooks
    # ------------------------------------------------------------------

    def remove_cumulative_tp(
        self,
        *,
        instance_key: str,
        instrument: str,
        side: RealOrderSide,
    ) -> bool:
        """Remove ONLY the TP. Default no-op — Ondo has no client-tagged
        TP removal that we can safely distinguish from a manual TP.

        See ``POSITION-SCOPING LIMITATION`` in the module docstring.
        Override in a future revision when OndoPerps upstream supports
        client-order-id tagging for protective records.
        """
        return False

    def cleanup_counters(
        self,
        *,
        instance_key: str,
        instrument: str,
    ) -> None:
        """Strategy-cycle cleanup for the dedicated Fibo lane.

        This is NOT user STOP. It is invoked only on strategy recovery / kill
        when a NEW cycle is about to begin. The selected
        (exchange, account, instrument) lane is treated as Fibo-dedicated.
        """
        close_resp = self._agent.execute({
            "operation": "close_position",
            "exchange": self.exchange_name,
            "account": self.account_alias,
            "symbol": instrument,
        })
        if not (_is_success(close_resp) or _error_code(close_resp) == "POSITION_NOT_FOUND"):
            raise RuntimeError(
                f"{_error_code(close_resp) or 'ONDOPERPS_CLOSE_FAILED'}: "
                f"{_error_message(close_resp)}".strip(": ")
            )

        tp_resp = self._agent.execute({
            "operation": "set_tp",
            "exchange": self.exchange_name,
            "account": self.account_alias,
            "symbol": instrument,
            "price": "0",
        })
        if not (
            _is_success(tp_resp)
            or _position_action_removed(tp_resp)
            or _error_code(tp_resp) == "POSITION_NOT_FOUND"
        ):
            raise RuntimeError(
                f"{_error_code(tp_resp) or 'ONDOPERPS_CLEAR_TP_FAILED'}: "
                f"{_error_message(tp_resp)}".strip(": ")
            )

        sl_resp = self._agent.execute({
            "operation": "set_sl",
            "exchange": self.exchange_name,
            "account": self.account_alias,
            "symbol": instrument,
            "price": "0",
        })
        if not (
            _is_success(sl_resp)
            or _position_action_removed(sl_resp)
            or _error_code(sl_resp) == "POSITION_NOT_FOUND"
        ):
            raise RuntimeError(
                f"{_error_code(sl_resp) or 'ONDOPERPS_CLEAR_SL_FAILED'}: "
                f"{_error_message(sl_resp)}".strip(": ")
            )

        state = self._agent.execute({
            "operation": "position_state",
            "exchange": self.exchange_name,
            "account": self.account_alias,
            "symbol": instrument,
        })
        if not _is_success(state):
            raise RuntimeError(
                f"{_error_code(state) or 'ONDOPERPS_STATE_VERIFY_FAILED'}: "
                f"{_error_message(state)}".strip(": ")
            )
        if _positions_list(state):
            raise RuntimeError("ONDOPERPS_STATE_VERIFY_NOT_FLAT")
        return None


__all__ = ["OndoPerpsFiboAdapter"]
