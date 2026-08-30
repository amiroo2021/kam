"""Deterministic Telegram wizard state machine.

This module is exchange-agnostic. It knows generic concepts (``exchange``,
``account``, ``balance``, ``orders``, etc.) and exchanges Telegram user
navigation into canonical TradeDesk requests. It NEVER inspects
environment variables, credential names, or exchange-specific response
shapes.

States (encoded as strings, stored per chat_id):

- ``"select_exchange"`` — first screen; user picks an exchange.
- ``"select_account"``  — user has picked an exchange; picks an account.
- ``"action"``          — user has picked an account; picks an action.
- ``"balance"``         — balance view; refresh returns to itself.
- ``"summative_report"`` — cross-exchange balances + positions summary.
- ``"positions_orders"`` — read-only positions & orders view.
- ``"new_order"``        — new-order symbol chooser.
- ``"ladder"``          — ladder flow root.
- ``"awaiting_symbol"``  — the wizard is waiting for free text.

Navigation is keyed by the chat_id (or a tuple of chat_id+thread_id)
passed in by the caller. The wizard keeps state in a small in-memory
dict — sufficient for the current trade flow.

The module exposes two top-level functions for direct dispatch from
the Telegram adapter:

    handle_trade_command(adapter, msg)
    handle_trade_callback(adapter, query, data)

These wrap the TradeWizard class. The adapter calls them directly from
its own _handle_command and _handle_callback_query paths; the adapter
does not use a plugin-handler registry for /trade.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

from .canonical import (
    GENERIC_ACTIONS,
    GENERIC_ACTION_LABELS,
    PHASE1_IMPLEMENTED_ACTIONS,
    CanonicalResponse,
)
from .tradedesk import TradeDesk, get_tradedesk

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Button layout
# ---------------------------------------------------------------------------

# Tuple list: (label, callback_data suffix). The wizard emits
# callback_data suffixes; the adapter's send_inline_keyboard helper
# prefixes them with the namespace token (``trade:``) on the wire.
BUTTON_EXIT = ("\u2715 Exit", "exit")
BUTTON_BACK = ("\u25c0\ufe0f Back", "back")
BUTTON_REFRESH = ("\u21bb Refresh", "refresh")
BUTTON_SUMMATIVE_REPORT = ("\U0001f4ca Summative Report", "summative_report")
BUTTON_CONTINUE = ("Continue", "continue")
BUTTON_SYMBOL_BTC = ("BTC", "symbol:BTC")
BUTTON_SYMBOL_ETH = ("ETH", "symbol:ETH")
BUTTON_SYMBOL_HYPE = ("HYPE", "symbol:HYPE")
BUTTON_SYMBOL_SOL = ("SOL", "symbol:SOL")
BUTTON_OTHER_SYMBOL = ("Other...", "symbol:other")
BUTTON_NEW_ORDER_BUY = ("🔵 Buy", "side:buy")
BUTTON_NEW_ORDER_SELL = ("🔴 Sell", "side:sell")
BUTTON_CONFIRM_ORDER = ("✅ Confirm Order", "confirm")
BUTTON_LADDER_HALF_GAUSSIAN = ("Half Gaussian", "distribution:half_gaussian")
BUTTON_LADDER_UNIFORM = ("Uniform", "distribution:uniform")
BUTTON_LADDER_BUY = ("Buy", "side:buy")
BUTTON_LADDER_SELL = ("Sell", "side:sell")
BUTTON_LADDER_CONFIRM = ("✅ Confirm", "confirm")
# Trade 2.0 — instrument resolution confirm / failure actions.
BUTTON_RESOLVE_AGREE = ("Agree", "resolve:agree")
BUTTON_RESOLVE_OTHER = ("Other...", "resolve:other")
BUTTON_RESOLVE_RETRY = ("Retry", "resolve:retry")
# Max priced instrument buttons on the picker (plus Other...).
_INSTRUMENT_PICK_MAX = 5


def _button_row(label: str, callback_suffix: str) -> Dict[str, str]:
    """Build a single button dict in the canonical button shape."""
    return {"text": label, "callback_data": callback_suffix}


def _render_error_lines(error: Any, fallback_message: str) -> List[str]:
    if error is None:
        return ["", fallback_message]
    lines = ["", f"Error: {error.message}"]
    exchange_reason = getattr(error, "exchange_reason", None)
    if isinstance(exchange_reason, str) and exchange_reason.strip():
        lines.append(f"Reason: {exchange_reason}")
    lines.append(f"({error.code})")
    return lines


def _comma_format(value: Any) -> str:
    """Format a numeric string with thousands separators without rounding."""
    if value is None:
        return "—"
    text = str(value).strip()
    if not text:
        return "—"
    try:
        decimal_value = Decimal(text)
    except Exception:  # noqa: BLE001
        return text
    rendered = format(decimal_value, "f")
    sign = ""
    if rendered.startswith(("+", "-")):
        sign, rendered = rendered[0], rendered[1:]
    integer, dot, fraction = rendered.partition(".")
    try:
        integer = f"{int(integer):,}"
    except Exception:  # noqa: BLE001
        pass
    return f"{sign}{integer}{dot}{fraction}"


def _pnl_format(value: Any) -> str:
    """Format PnL with an explicit sign and two decimals."""
    if value is None:
        return "—"
    try:
        decimal_value = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return "—"
    quantized = decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "+" if quantized >= 0 else "-"
    magnitude = abs(quantized)
    return f"{sign}{format(magnitude, 'f')}"


def _direction_emoji(side: Any) -> str:
    text = str(side or "").strip().lower()
    if text in {"long", "buy", "b"}:
        return "🔵"
    if text in {"short", "sell", "s"}:
        return "🔴"
    return "⚪"


def _display_or_dash(value: Any) -> str:
    return _comma_format(value)


def _wizard_decimal(value: Any) -> Optional[Decimal]:
    """Parse a numeric display value; return None if missing/invalid."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"—", "-", "None", "null"}:
        return None
    try:
        return Decimal(text)
    except Exception:  # noqa: BLE001
        return None


def _format_money(value: Decimal, places: str = "0.01") -> str:
    try:
        quantized = value.quantize(Decimal(places), rounding=ROUND_HALF_UP)
    except Exception:  # noqa: BLE001
        quantized = value
    return _comma_format(format(quantized, "f"))


def _display_protection(value: Any, count: Any = None) -> str:
    text = _display_or_dash(value)
    try:
        count_value = int(count) if count is not None else None
    except Exception:
        count_value = None
    if text == "—":
        return text
    if count_value is not None and count_value > 1:
        return f"{text} (multiple)"
    return text


def _account_option_parts(entry: Any) -> Tuple[Optional[str], Optional[str]]:
    if isinstance(entry, str):
        alias = entry.strip()
        return (alias or None, alias or None)
    if isinstance(entry, dict):
        alias = str(entry.get("account", "")).strip()
        if not alias:
            return (None, None)
        label = str(entry.get("label", alias)).strip() or alias
        return (alias, label)
    return (None, None)


def _rounded_money_display(value: Any) -> str:
    """Render price-like values to at most two decimals for compact menus."""
    try:
        number = Decimal(str(value))
        rounded = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{rounded:,.2f}"
    except Exception:
        return _display_or_dash(value)


def _order_group_summary_line(group: Any) -> str:
    symbol = str(getattr(group, "symbol", "")).strip() or "Symbol"
    side = str(getattr(group, "side", "")).strip().lower() or "side"
    count = int(getattr(group, "order_count", 0) or 0)
    total_size = _display_or_dash(getattr(group, "total_size", ""))
    vwap = _rounded_money_display(getattr(group, "vwap", ""))
    min_price = _display_or_dash(getattr(group, "min_price", ""))
    max_price = _display_or_dash(getattr(group, "max_price", ""))
    count_label = "order" if count == 1 else "orders"
    if min_price == max_price:
        price_fragment = f"VWAP {vwap} · @ {min_price}"
    else:
        price_fragment = f"VWAP {vwap} · range {min_price}-{max_price}"
    return "\n".join(
        [
            f"{_direction_emoji(side)} {symbol} {side}",
            f"{count} {count_label} · total size {total_size}",
            price_fragment,
        ]
    )


def _order_group_button_text(group: Any) -> str:
    side = str(getattr(group, "side", "")).strip().title() or "Side"
    symbol = str(getattr(group, "symbol", "")).strip() or "Symbol"
    count = getattr(group, "order_count", 0)
    return f"{_direction_emoji(getattr(group, 'side', ''))} {symbol} {side} -- {count} orders"


# ---------------------------------------------------------------------------
# Screen rendering
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Screen:
    """A single rendered wizard screen.

    Attributes:
        text: Human-readable text body (Markdown-safe; the Telegram
            adapter is responsible for any final Markdown escaping).
        buttons: List of button rows. Each row is a list of button
            dicts with ``text`` and ``callback_data``. The wizard
            emits callback suffixes only; the caller assembles the
            transport-specific prefix.
        state: Next wizard state to persist for this chat.
    """

    text: str
    buttons: List[List[Dict[str, str]]]
    state: str


@dataclass
class WizardState:
    """Per-chat wizard state.

    Holds the selected exchange and account (when applicable), and the
    current screen position. The telegram adapter is responsible for
    passing in a stable chat key (e.g. ``(chat_id, thread_id)``).
    """

    state: str = "select_exchange"
    exchange: Optional[str] = None
    account: Optional[str] = None
    symbol: Optional[str] = None
    requested_symbol: Optional[str] = None
    resolved_instrument: Optional[Dict[str, Any]] = None
    # Ranked venue instruments for the picker (symbol + optional price).
    instrument_candidates: List[Dict[str, Any]] = field(default_factory=list)
    order: Dict[str, Any] = field(default_factory=dict)
    flow: Optional[str] = None
    ladder: Dict[str, Any] = field(default_factory=dict)
    cancel: Dict[str, Any] = field(default_factory=dict)
    positions: List[Dict[str, Any]] = field(default_factory=list)
    position: Dict[str, Any] = field(default_factory=dict)
    position_action: Dict[str, Any] = field(default_factory=dict)


class TradeWizard:
    """Telegram wizard state machine.

    Stateless except for an in-memory ``_states`` dict keyed by chat
    key. The trade plugin owns the instance; no global state.
    """

    # Callback suffix -> wizard action. The wizard never inspects
    # exchange names; only generic actions like "select_exchange" or
    # "pick_account_<alias>".
    CALLBACK_SEP = ":"

    def __init__(self, tradedesk: Optional[TradeDesk] = None) -> None:
        self._desk = tradedesk or get_tradedesk()
        self._states: Dict[Tuple[Any, ...], WizardState] = {}

    # -- state helpers ---------------------------------------------------

    def _state_for(self, chat_key: Tuple[Any, ...]) -> WizardState:
        state = self._states.get(chat_key)
        if state is None:
            state = WizardState()
            self._states[chat_key] = state
        return state

    def reset(self, chat_key: Tuple[Any, ...]) -> None:
        self._states.pop(chat_key, None)

    # -- public entry points --------------------------------------------

    def open(self, chat_key: Tuple[Any, ...]) -> Screen:
        """Open the wizard at the first screen."""
        self._states.pop(chat_key, None)
        return self._render_select_exchange(chat_key)

    def handle_text(self, chat_key: Tuple[Any, ...], text: str) -> Optional[Screen]:
        """Consume free-text input only when the wizard is explicitly waiting.

        Returns a Screen when the text was consumed, or None when the
        wizard is not in a free-text state and the caller should continue
        normal Hermes dispatch.
        """
        state = self._state_for(chat_key)
        if state.state == "awaiting_symbol":
            return self._handle_awaiting_symbol(chat_key, text)
        if state.state == "awaiting_native_symbol":
            return self._handle_awaiting_native_symbol(chat_key, text)
        if state.state in {
            "awaiting_volume",
            "awaiting_price",
            "awaiting_tp_price",
            "awaiting_sl_price",
            "awaiting_ladder_order_count",
            "awaiting_ladder_total_volume",
            "awaiting_ladder_start_price",
            "awaiting_ladder_end_price",
        }:
            return self._handle_awaiting_text(chat_key, text)
        return None

    def handle_callback(
        self,
        chat_key: Tuple[Any, ...],
        callback_suffix: str,
    ) -> Screen:
        """Advance the wizard based on a callback suffix from a button.

        The direct-dispatch helper passes the already-stripped
        suffix (everything after ``trade:``). The wizard never
        inspects exchange names.
        """
        suffix = (callback_suffix or "").strip()
        state = self._state_for(chat_key)

        if suffix == "exit":
            self.reset(chat_key)
            return Screen(
                text="Trade closed.",
                buttons=[],
                state="closed",
            )

        if state.state == "select_exchange":
            return self._handle_select_exchange(chat_key, suffix)
        if state.state == "select_account":
            return self._handle_select_account(chat_key, suffix)
        if state.state == "action":
            return self._handle_action(chat_key, suffix)
        if state.state == "balance":
            return self._handle_balance(chat_key, suffix)
        if state.state == "summative_report":
            return self._handle_summative_report(chat_key, suffix)
        if state.state == "positions_orders":
            return self._handle_positions_orders(chat_key, suffix)
        if state.state == "positions_management":
            return self._handle_positions_management(chat_key, suffix)
        if state.state == "position_detail":
            return self._handle_position_detail(chat_key, suffix)
        if state.state == "awaiting_tp_price":
            return self._handle_position_tp_input(chat_key, suffix)
        if state.state == "position_tp_confirm":
            return self._handle_position_tp_confirm(chat_key, suffix)
        if state.state == "awaiting_sl_price":
            return self._handle_position_sl_input(chat_key, suffix)
        if state.state == "position_sl_confirm":
            return self._handle_position_sl_confirm(chat_key, suffix)
        if state.state == "position_close_confirm":
            return self._handle_position_close_confirm(chat_key, suffix)
        if state.state == "position_action_result":
            return self._handle_position_action_result(chat_key, suffix)
        if state.state == "new_order":
            return self._handle_new_order(chat_key, suffix)
        if state.state == "new_order_side":
            return self._handle_new_order_side(chat_key, suffix)
        if state.state == "new_order_confirm":
            return self._handle_new_order_confirm(chat_key, suffix)
        if state.state == "new_order_result":
            return self._handle_new_order_result(chat_key, suffix)
        if state.state == "awaiting_volume":
            return self._handle_new_order_text_callback(chat_key, suffix, "volume")
        if state.state == "awaiting_price":
            return self._handle_new_order_text_callback(chat_key, suffix, "price")
        if state.state == "ladder":
            return self._handle_ladder(chat_key, suffix)
        if state.state == "ladder_symbol":
            return self._handle_ladder_symbol(chat_key, suffix)
        if state.state == "ladder_side":
            return self._handle_ladder_side(chat_key, suffix)
        if state.state == "ladder_confirm":
            return self._handle_ladder_confirm(chat_key, suffix)
        if state.state == "cancel_orders":
            return self._handle_cancel_orders(chat_key, suffix)
        if state.state == "cancel_group_confirm":
            return self._handle_cancel_group_confirm(chat_key, suffix)
        if state.state == "cancel_result":
            return self._handle_cancel_result(chat_key, suffix)
        if state.state == "awaiting_symbol":
            return self._handle_awaiting_symbol_callback(chat_key, suffix)
        if state.state == "instrument_confirm":
            return self._handle_instrument_confirm(chat_key, suffix)
        if state.state == "instrument_unresolved":
            return self._handle_instrument_unresolved(chat_key, suffix)
        if state.state == "awaiting_native_symbol":
            return self._handle_awaiting_native_symbol_callback(chat_key, suffix)
        if state.state == "awaiting_ladder_order_count":
            return self._handle_ladder_text_callback(chat_key, suffix, "order_count")
        if state.state == "awaiting_ladder_total_volume":
            return self._handle_ladder_text_callback(chat_key, suffix, "total_volume")
        if state.state == "awaiting_ladder_start_price":
            return self._handle_ladder_text_callback(chat_key, suffix, "start_price")
        if state.state == "awaiting_ladder_end_price":
            return self._handle_ladder_text_callback(chat_key, suffix, "end_price")

        # Unknown state — restart.
        return self.open(chat_key)

    # -- first screen ----------------------------------------------------

    def _render_select_exchange(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "select_exchange"
        exchanges = self._desk.list_exchanges()
        if not exchanges:
            return Screen(
                text=(
                    "Trade\n\n"
                    "No exchanges are currently available.\n"
                    "Add an ``x_<exchange>_agent.py`` module to the trade "
                    "plugin's agents directory and try again."
                ),
                buttons=[[_button_row(*BUTTON_EXIT)]],
                state="select_exchange",
            )

        rows: List[List[Dict[str, str]]] = []
        for ex in exchanges:
            rows.append([_button_row(ex, f"exchange:{ex}")])
        rows.append([_button_row(*BUTTON_SUMMATIVE_REPORT)])
        rows.append([_button_row(*BUTTON_EXIT)])
        return Screen(
            text=(
                "Trade\n\n"
                "Select Exchange:"
            ),
            buttons=rows,
            state="select_exchange",
        )

    def _handle_select_exchange(
        self,
        chat_key: Tuple[Any, ...],
        suffix: str,
    ) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self.open(chat_key)
        if suffix == "summative_report":
            return self._render_summative_report(chat_key, refresh=False)
        if not suffix.startswith("exchange:"):
            return self._render_select_exchange(chat_key)
        exchange = suffix[len("exchange:"):].strip()
        if not exchange or exchange not in self._desk.list_exchanges():
            return self._render_select_exchange(chat_key)
        state.exchange = exchange
        return self._render_select_account(chat_key)

    # -- account screen --------------------------------------------------

    def _render_select_account(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "select_account"
        exchange = state.exchange or ""
        accounts = self._desk.list_accounts(exchange) if exchange else []
        if not accounts:
            state.exchange = None
            return Screen(
                text=(
                    f"Trade\n\n"
                    f"Exchange: {exchange}\n\n"
                    "No accounts are configured for this exchange."
                ),
                buttons=[
                    [
                        _button_row(*BUTTON_BACK),
                        _button_row(*BUTTON_EXIT),
                    ]
                ],
                state="select_account",
            )

        rows: List[List[Dict[str, str]]] = []
        for entry in accounts:
            alias, label = _account_option_parts(entry)
            if not alias or not label:
                continue
            rows.append([_button_row(label, f"account:{alias}")])
        rows.append([_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)])
        return Screen(
            text=(
                "Trade\n\n"
                f"Exchange: {exchange}\n\n"
                "Select Account:"
            ),
            buttons=rows,
            state="select_account",
        )

    def _handle_select_account(
        self,
        chat_key: Tuple[Any, ...],
        suffix: str,
    ) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            state.exchange = None
            return self._render_select_exchange(chat_key)
        if not suffix.startswith("account:"):
            return self._render_select_account(chat_key)
        alias = suffix[len("account:"):].strip()
        exchange = state.exchange or ""
        valid_aliases = {
            parsed_alias
            for parsed_alias, _label in (_account_option_parts(entry) for entry in self._desk.list_accounts(exchange))
            if parsed_alias
        }
        if alias not in valid_aliases:
            return self._render_select_account(chat_key)
        state.account = alias
        return self._render_action(chat_key)

    # -- action screen ---------------------------------------------------

    def _render_action(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "action"
        rows: List[List[Dict[str, str]]] = []
        # Two columns of action buttons.
        actions = list(GENERIC_ACTIONS)
        for i in range(0, len(actions), 2):
            row = [
                _button_row(
                    GENERIC_ACTION_LABELS[actions[i]],
                    f"action:{actions[i]}",
                )
            ]
            if i + 1 < len(actions):
                row.append(
                    _button_row(
                        GENERIC_ACTION_LABELS[actions[i + 1]],
                        f"action:{actions[i + 1]}",
                    )
                )
            rows.append(row)
        rows.append([_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)])
        return Screen(
            text=(
                "Trade\n\n"
                f"Exchange: {state.exchange}\n"
                f"Account: {state.account}\n"
            ),
            buttons=rows,
            state="action",
        )

    def _handle_action(
        self,
        chat_key: Tuple[Any, ...],
        suffix: str,
    ) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            state.account = None
            return self._render_select_account(chat_key)
        if not suffix.startswith("action:"):
            return self._render_action(chat_key)
        action = suffix[len("action:"):].strip()
        if action not in GENERIC_ACTIONS:
            return self._render_action(chat_key)

        if action == "balance":
            return self._render_balance(chat_key, refresh=False)
        if action == "positions_orders":
            return self._render_positions_orders(chat_key, refresh=False)
        if action == "positions_management":
            return self._render_positions_management(chat_key, refresh=False)
        if action == "new_order":
            return self._render_new_order(chat_key)
        if action == "cancel_orders":
            return self._render_cancel_orders(chat_key, refresh=False)
        if action == "ladder":
            return self._render_ladder(chat_key)

        # Generic empty-state screen for unimplemented actions.
        return Screen(
            text=(
                "Trade\n\n"
                f"Exchange: {state.exchange}\n"
                f"Account: {state.account}\n\n"
                "Not implemented yet."
            ),
            buttons=[
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]
            ],
            state="action",
        )

    # -- Trade 2.0: instrument resolution + priced picker (once) --------

    def _agent_capabilities(self, exchange: Optional[str]) -> List[str]:
        """Return desk capabilities for ``exchange``, or ``[]``."""
        if not exchange:
            return []
        caps_fn = getattr(self._desk, "capabilities", None)
        if not callable(caps_fn):
            return []
        try:
            caps = caps_fn(exchange) or []
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(caps, list):
            return []
        return [c for c in caps if isinstance(c, str)]

    def _agent_supports_resolve(self, exchange: Optional[str]) -> bool:
        """True when the exchange agent advertises ``resolve_instrument``."""
        return "resolve_instrument" in self._agent_capabilities(exchange)

    def _agent_supports_list_instruments(self, exchange: Optional[str]) -> bool:
        return "list_instruments" in self._agent_capabilities(exchange)

    def _agent_supports_market_price(self, exchange: Optional[str]) -> bool:
        return "market_price" in self._agent_capabilities(exchange)

    def _agent_supports_instrument_lookup(self, exchange: Optional[str]) -> bool:
        """True when resolve and/or catalog listing is available."""
        caps = self._agent_capabilities(exchange)
        return "resolve_instrument" in caps or "list_instruments" in caps

    def _call_resolve_instrument(
        self,
        exchange: str,
        account: str,
        symbol: str,
    ) -> CanonicalResponse:
        """Dispatch once through TradeDesk to the agent's existing resolver."""
        return self._desk.execute(
            {
                "operation": "resolve_instrument",
                "exchange": exchange,
                "account": account,
                "symbol": symbol,
            }
        )

    def _call_list_instruments(
        self, exchange: str, account: str
    ) -> List[Dict[str, Any]]:
        """Read venue catalog via ``list_instruments`` (empty on failure)."""
        try:
            response = self._desk.execute(
                {
                    "operation": "list_instruments",
                    "exchange": exchange,
                    "account": account,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_instruments failed: %s", exc)
            return []
        if not getattr(response, "success", False):
            return []
        data = getattr(response, "data", None)
        if not isinstance(data, dict):
            return []
        records = data.get("instruments")
        if not isinstance(records, list):
            return []
        out: List[Dict[str, Any]] = []
        for item in records:
            if isinstance(item, dict):
                out.append(item)
        return out

    def _call_market_price(
        self, exchange: str, account: str, symbol: str
    ) -> Optional[str]:
        """Best-effort mark/last price string; ``None`` when unavailable."""
        if not self._agent_supports_market_price(exchange):
            return None
        try:
            response = self._desk.execute(
                {
                    "operation": "market_price",
                    "exchange": exchange,
                    "account": account,
                    "symbol": symbol,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("market_price(%s) failed: %s", symbol, exc)
            return None
        if not getattr(response, "success", False):
            return None
        candidates: List[Any] = []
        mp_obj = getattr(response, "market_price", None)
        if mp_obj is not None:
            if isinstance(mp_obj, dict):
                candidates.extend(
                    [
                        mp_obj.get("mark_price"),
                        mp_obj.get("price"),
                        mp_obj.get("last_external_price"),
                    ]
                )
            else:
                candidates.extend(
                    [
                        getattr(mp_obj, "mark_price", None),
                        getattr(mp_obj, "price", None),
                        getattr(mp_obj, "last_external_price", None),
                    ]
                )
        data = getattr(response, "data", None)
        if isinstance(data, dict):
            candidates.extend(
                [data.get("mark_price"), data.get("price"), data.get("mark")]
            )
        for raw in candidates:
            if raw is None:
                continue
            text = str(raw).strip()
            if not text:
                continue
            try:
                dec = Decimal(text)
            except Exception:  # noqa: BLE001
                continue
            if dec.is_finite():
                return format(dec.normalize(), "f")
        return None

    def _candidate_symbol(self, item: Any) -> str:
        if isinstance(item, dict):
            return str(
                item.get("symbol")
                or item.get("instrument")
                or item.get("market")
                or ""
            ).strip()
        return str(item or "").strip()

    # Concept groups expand free-text (OIL, GOLD, …) into catalog tokens.
    # Matching still requires the venue catalog to contain the market —
    # these never invent an instrument id.
    _CONCEPT_GROUPS: Tuple[frozenset, ...] = (
        frozenset({"GOLD", "XAU", "XAUUSD", "XAUUSDT"}),
        frozenset({"SILVER", "XAG", "XAGUSD", "XAGUSDT"}),
        frozenset({"OIL", "WTI", "BRENT", "CRUDE", "CL", "OILUSD", "CRUDEOIL"}),
        frozenset({"NATGAS", "NG", "GAS", "HENRY", "NATGASUSD"}),
        frozenset({"BTC", "BITCOIN", "XBT", "BTCUSD", "BTCUSDT"}),
        frozenset({"ETH", "ETHEREUM", "ETHER", "ETHUSD", "ETHUSDT"}),
        frozenset({"SOL", "SOLANA", "SOLUSD", "SOLUSDT"}),
    )
    _QUOTE_NOISE = frozenset(
        {"USD", "USDT", "USDC", "PERP", "P", "USDTM", "USDCM"}
    )

    def _tokenize_instrument(self, text: str) -> List[str]:
        raw = str(text or "").strip().upper()
        if not raw:
            return []
        parts = [p for p in re.split(r"[^A-Z0-9]+", raw) if p]
        out: List[str] = []
        for p in parts:
            if p in self._QUOTE_NOISE:
                continue
            out.append(p)
            # Peel trailing quote suffixes stuck to a base (WTIUSD → WTI).
            for suffix in ("USDT", "USDC", "USD", "PERP"):
                if p.endswith(suffix) and len(p) > len(suffix):
                    base = p[: -len(suffix)]
                    if base and base not in out:
                        out.append(base)
        return out

    def _symbol_search_hints(self, requested: str) -> List[str]:
        """Expand user text into catalog search tokens (never venue ids)."""
        raw = str(requested or "").strip().upper()
        if not raw:
            return []
        hints: List[str] = [raw]
        hints.extend(self._tokenize_instrument(raw))
        for suffix in ("USDT", "USDC", "USD", "PERP"):
            if raw.endswith(suffix) and len(raw) > len(suffix):
                prefix = raw[: -len(suffix)]
                if prefix and prefix not in hints:
                    hints.append(prefix)
        # Expand concept groups (OIL → WTI/BRENT/CRUDE, GOLD → XAU, …).
        expanded: List[str] = []
        for seed in list(hints):
            for group in self._CONCEPT_GROUPS:
                if seed in group:
                    for token in group:
                        if token not in hints and token not in expanded:
                            expanded.append(token)
        hints.extend(expanded)
        # De-dupe preserve order.
        seen: set[str] = set()
        out: List[str] = []
        for h in hints:
            if h and h not in seen:
                seen.add(h)
                out.append(h)
        return out

    def _rank_catalog_candidates(
        self, catalog: List[Dict[str, Any]], requested: str
    ) -> List[Dict[str, Any]]:
        """Rank venue catalog rows against free-text ``requested``.

        Strategy (exchange-agnostic):
        1. Prefer fibo ranker when the fibo package is installed.
        2. Else score each catalog row with exact / concept-token /
           field-substring / SequenceMatcher signals.
        3. If nothing scores, still return the closest fuzzy top-N so the
           user can pick (never invent symbols outside the catalog).
        """
        if not catalog:
            return []
        try:
            from plugins.trade.fibo.candidates import rank_candidates

            ranked = rank_candidates(catalog, requested)
            out: List[Dict[str, Any]] = []
            for cand in ranked:
                if int(getattr(cand, "score", 0) or 0) <= 0:
                    continue
                sym = str(getattr(cand, "instrument", "") or "").strip()
                if not sym:
                    continue
                price = getattr(cand, "price", None)
                entry: Dict[str, Any] = {
                    "symbol": sym,
                    "display_name": str(getattr(cand, "display_name", "") or "").strip(),
                    "score": int(getattr(cand, "score", 0) or 0),
                }
                if price is not None:
                    entry["price"] = (
                        format(price.normalize(), "f")
                        if hasattr(price, "normalize")
                        else str(price)
                    )
                out.append(entry)
            if out:
                # Apply the same strength filter as the builtin ranker.
                best = max(int(e.get("score") or 0) for e in out)
                floor = 40 if best >= 40 else max(15, int(best * 0.5))
                strong = [
                    e for e in out
                    if int(e.get("score") or 0) >= floor
                ]
                return strong or out[: min(3, len(out))]
        except Exception as exc:  # noqa: BLE001
            logger.warning("rank_candidates fallback: %s", exc)

        req = str(requested or "").strip().upper()
        hints = self._symbol_search_hints(req)
        hint_set = {h.upper() for h in hints}
        scored: List[Tuple[int, float, str, Dict[str, Any]]] = []

        for raw in catalog:
            if not isinstance(raw, dict):
                continue
            sym = self._candidate_symbol(raw)
            if not sym:
                continue
            up = sym.upper()
            base = str(raw.get("base") or "").strip().upper()
            display = str(
                raw.get("display_name") or raw.get("displayName") or ""
            ).strip().upper()
            desc = str(
                raw.get("description")
                or raw.get("long_name")
                or raw.get("longName")
                or ""
            ).strip().upper()
            tokens = set(self._tokenize_instrument(up))
            if base:
                tokens.update(self._tokenize_instrument(base))
            if display:
                tokens.update(self._tokenize_instrument(display))
            if desc:
                tokens.update(self._tokenize_instrument(desc))

            score = 0
            if up == req:
                score += 100
            if base and base == req:
                score += 80
            # Concept / hint token hits on instrument tokens.
            overlap = tokens & hint_set
            if overlap:
                score += 90 if any(t == req or t in hint_set for t in overlap) else 60
                # Prefer more specific token hits.
                if any(len(t) >= 3 and t in tokens for t in hint_set):
                    score += 10
            # Substring presence in fields.
            blob = " ".join(x for x in (up, base, display, desc) if x)
            for h in hint_set:
                if len(h) >= 2 and h in blob:
                    score += 25
                    break
            # Fuzzy similarity against symbol / base / tokens.
            best_ratio = 0.0
            for cand_txt in (up, base, display, *tokens):
                if not cand_txt:
                    continue
                r = SequenceMatcher(None, req, cand_txt).ratio()
                if r > best_ratio:
                    best_ratio = r
                for h in hint_set:
                    if h == req:
                        continue
                    r2 = SequenceMatcher(None, h, cand_txt).ratio()
                    if r2 > best_ratio:
                        best_ratio = r2
            if best_ratio >= 0.75:
                score += 40
            elif best_ratio >= 0.55:
                score += 20
            elif best_ratio >= 0.4:
                score += 8

            entry = {
                "symbol": sym,
                "display_name": str(
                    raw.get("display_name") or raw.get("displayName") or ""
                ).strip(),
                "score": score,
                "fuzzy": round(best_ratio, 3),
            }
            if raw.get("price") is not None:
                entry["price"] = str(raw.get("price"))
            scored.append((score, best_ratio, up, entry))

        # Prefer positive semantic scores; drop weak fuzzy noise.
        positive = [t for t in scored if t[0] > 0]
        positive.sort(key=lambda t: (-t[0], -t[1], t[2]))
        if positive:
            best = positive[0][0]
            # Keep strong hits (exact/concept/substring). Require either a
            # solid score floor or proximity to the best match so weak
            # SequenceMatcher noise (OIL≈ETH) does not appear as a pick.
            floor = 40 if best >= 40 else max(15, int(best * 0.5))
            keep = [
                t
                for t in positive
                if t[0] >= floor or (t[0] >= 25 and t[1] >= 0.7)
            ]
            if not keep:
                keep = positive[: min(3, len(positive))]
            return [t[3] for t in keep]

        # No semantic hit — still offer closest catalog rows by fuzzy ratio.
        fuzzy = [t for t in scored if t[1] >= 0.45]
        fuzzy.sort(key=lambda t: (-t[1], t[2]))
        out_fuzzy: List[Dict[str, Any]] = []
        for _score, ratio, _up, entry in fuzzy[:_INSTRUMENT_PICK_MAX]:
            row = dict(entry)
            row["score"] = max(1, int(ratio * 100))
            out_fuzzy.append(row)
        return out_fuzzy

    def _enrich_candidate_prices(
        self,
        exchange: str,
        account: str,
        candidates: List[Dict[str, Any]],
        *,
        limit: int = _INSTRUMENT_PICK_MAX,
    ) -> List[Dict[str, Any]]:
        """Attach market prices to the top ``limit`` candidates (best-effort)."""
        out: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in candidates:
            sym = self._candidate_symbol(item)
            if not sym:
                continue
            key = sym.upper()
            if key in seen:
                continue
            seen.add(key)
            entry = dict(item) if isinstance(item, dict) else {"symbol": sym}
            entry["symbol"] = sym
            if not entry.get("price") and self._agent_supports_market_price(exchange):
                price = self._call_market_price(exchange, account, sym)
                if price:
                    entry["price"] = price
            out.append(entry)
            if len(out) >= limit:
                break
        return out

    def _build_priced_candidates(
        self,
        exchange: str,
        account: str,
        requested: str,
        *,
        primary_native: Optional[str] = None,
        agent_candidates: Optional[List[Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Merge resolver primary + similar catalog hits, then attach prices."""
        merged: List[Dict[str, Any]] = []
        seen: set[str] = set()

        def _push(sym: str, **extra: Any) -> None:
            key = sym.upper()
            if not sym or key in seen:
                return
            seen.add(key)
            row = {"symbol": sym}
            row.update({k: v for k, v in extra.items() if v is not None and v != ""})
            merged.append(row)

        if primary_native:
            _push(str(primary_native).strip(), score=1000, primary=True)

        for item in agent_candidates or []:
            sym = self._candidate_symbol(item)
            if not sym:
                continue
            extra: Dict[str, Any] = {}
            if isinstance(item, dict):
                if item.get("price") is not None:
                    extra["price"] = item.get("price")
                if item.get("display_name"):
                    extra["display_name"] = item.get("display_name")
            _push(sym, score=900, **extra)

        if self._agent_supports_list_instruments(exchange):
            catalog = self._call_list_instruments(exchange, account)
            for ranked in self._rank_catalog_candidates(catalog, requested):
                _push(
                    str(ranked.get("symbol") or "").strip(),
                    score=ranked.get("score"),
                    display_name=ranked.get("display_name"),
                    price=ranked.get("price"),
                )

        if not merged and primary_native:
            _push(str(primary_native).strip(), score=1000, primary=True)

        return self._enrich_candidate_prices(
            exchange, account, merged, limit=_INSTRUMENT_PICK_MAX
        )

    def _instrument_button_label(self, entry: Dict[str, Any]) -> str:
        sym = self._candidate_symbol(entry) or "?"
        price = entry.get("price")
        if price is None or str(price).strip() == "":
            return sym
        return f"{sym} · {_comma_format(price)}"

    def _commit_native_symbol(self, chat_key: Tuple[Any, ...], native: str) -> None:
        """Store the exchange-native instrument for all downstream /trade ops."""
        state = self._state_for(chat_key)
        native_sym = str(native or "").strip()
        state.symbol = native_sym
        if state.flow == "ladder":
            state.ladder["symbol"] = native_sym
        else:
            state.order["symbol"] = native_sym

    def _continue_after_symbol(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        if state.flow == "ladder":
            return self._render_ladder_side(chat_key)
        return self._render_new_order_side(chat_key)

    def _symbol_chooser_screen(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        if state.flow == "ladder":
            return self._render_ladder_symbol(chat_key)
        return self._render_new_order(chat_key)

    def _flow_title(self, chat_key: Tuple[Any, ...]) -> str:
        return "Ladder" if self._state_for(chat_key).flow == "ladder" else "New Order"

    def _extract_resolved_native(self, response: CanonicalResponse) -> Optional[str]:
        inst = getattr(response, "instrument", None)
        if inst is None:
            return None
        if isinstance(inst, dict):
            sym = str(inst.get("symbol") or "").strip()
        else:
            sym = str(getattr(inst, "symbol", "") or "").strip()
        return sym or None

    def _instrument_payload(self, response: CanonicalResponse) -> Dict[str, Any]:
        inst = getattr(response, "instrument", None)
        if inst is None:
            return {}
        if hasattr(inst, "to_dict") and callable(inst.to_dict):
            try:
                data = inst.to_dict()
                if isinstance(data, dict):
                    return dict(data)
            except Exception:  # noqa: BLE001
                pass
        if isinstance(inst, dict):
            return dict(inst)
        return {
            "symbol": str(getattr(inst, "symbol", "") or "").strip(),
            "requested_symbol": str(getattr(inst, "requested_symbol", "") or "").strip(),
            "display_name": str(getattr(inst, "display_name", "") or "").strip(),
        }

    def _apply_entered_instrument(
        self,
        chat_key: Tuple[Any, ...],
        entered: str,
        *,
        manual_native: bool = False,
    ) -> Screen:
        """Resolve once at the earliest common point (exchange+account+symbol).

        - No resolve/list capability → legacy pass-through.
        - ``manual_native`` (Other path) → store typed value as-is, no resolve.
        - Resolver and/or catalog → priced instrument buttons + Other...
        - User must pick a button (never auto-guess on ambiguity).
        - After pick, continue New Order / Ladder params; confirm still
          dispatches ``new_order`` / ``ladder`` via the exchange agent API.
        """
        state = self._state_for(chat_key)
        raw = str(entered or "").strip()
        if not raw:
            title = self._flow_title(chat_key)
            state.state = "awaiting_symbol"
            return Screen(
                text=(
                    f"{title}\n\n"
                    "Enter another symbol:\n\n"
                    "Instrument not found."
                ),
                buttons=[
                    [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
                ],
                state="awaiting_symbol",
            )

        requested = raw.upper()
        state.requested_symbol = requested
        state.resolved_instrument = None
        state.instrument_candidates = []

        if manual_native:
            self._commit_native_symbol(chat_key, requested)
            return self._continue_after_symbol(chat_key)

        exchange = str(state.exchange or "").strip()
        account = str(state.account or "").strip()
        if (
            not exchange
            or not account
            or not self._agent_supports_instrument_lookup(exchange)
        ):
            # No resolver/catalog — keep legacy /trade pass-through.
            self._commit_native_symbol(chat_key, requested)
            return self._continue_after_symbol(chat_key)

        primary_native: Optional[str] = None
        payload: Dict[str, Any] = {"requested_symbol": requested}
        agent_error_candidates: List[Any] = []
        resolve_error_code: Optional[str] = None
        resolve_error_message: Optional[str] = None

        if self._agent_supports_resolve(exchange):
            try:
                response = self._call_resolve_instrument(exchange, account, requested)
            except Exception as exc:  # noqa: BLE001
                logger.warning("resolve_instrument failed: %s", exc)
                resolve_error_code = "AGENT_EXCEPTION"
                resolve_error_message = str(exc)
                response = None
            if response is not None:
                if getattr(response, "success", False):
                    primary_native = self._extract_resolved_native(response)
                    if primary_native:
                        payload = self._instrument_payload(response)
                        payload["requested_symbol"] = requested
                        payload["symbol"] = primary_native
                    else:
                        resolve_error_code = "INSTRUMENT_NOT_FOUND"
                        resolve_error_message = "Instrument not found."
                else:
                    err = getattr(response, "error", None)
                    resolve_error_code = str(
                        getattr(err, "code", "") or "INSTRUMENT_NOT_FOUND"
                    )
                    resolve_error_message = str(
                        getattr(err, "message", "") or "Instrument not found."
                    )
                    data = getattr(response, "data", None)
                    if isinstance(data, dict):
                        raw_cands = data.get("candidates")
                        if isinstance(raw_cands, list):
                            agent_error_candidates = list(raw_cands)

        candidates = self._build_priced_candidates(
            exchange,
            account,
            requested,
            primary_native=primary_native,
            agent_candidates=agent_error_candidates,
        )
        state.instrument_candidates = list(candidates)

        if candidates:
            if primary_native:
                payload["symbol"] = primary_native
            else:
                # Catalog-only hit — stage first candidate as default Agree target.
                payload["symbol"] = self._candidate_symbol(candidates[0])
            payload["candidates"] = candidates
            state.resolved_instrument = payload
            return self._render_instrument_confirm(chat_key)

        state.resolved_instrument = {
            "requested_symbol": requested,
            "error_code": resolve_error_code or "INSTRUMENT_NOT_FOUND",
            "error_message": resolve_error_message or "Instrument not found.",
            "candidates": [],
        }
        return self._render_instrument_unresolved(chat_key)

    def _render_instrument_confirm(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "instrument_confirm"
        info = state.resolved_instrument or {}
        source = str(state.requested_symbol or info.get("requested_symbol") or "").strip()
        candidates = list(state.instrument_candidates or info.get("candidates") or [])
        if candidates and not state.instrument_candidates:
            state.instrument_candidates = list(candidates)
        title = self._flow_title(chat_key)
        lines = [
            f"{title}",
            "",
            "Select Instrument",
            f"Source: {source}",
        ]
        if candidates:
            lines.append("")
            for idx, entry in enumerate(candidates[:_INSTRUMENT_PICK_MAX], start=1):
                if not isinstance(entry, dict):
                    entry = {"symbol": self._candidate_symbol(entry)}
                sym = self._candidate_symbol(entry)
                price = entry.get("price")
                if price is not None and str(price).strip() != "":
                    lines.append(f"{idx}. {sym}  ·  {_comma_format(price)}")
                else:
                    lines.append(f"{idx}. {sym}")
        else:
            resolved = str(info.get("symbol") or "").strip()
            if resolved:
                lines.extend(["", f"Resolved: {resolved}"])

        buttons: List[List[Dict[str, str]]] = []
        if candidates:
            for idx, entry in enumerate(candidates[:_INSTRUMENT_PICK_MAX]):
                if not isinstance(entry, dict):
                    entry = {"symbol": self._candidate_symbol(entry)}
                buttons.append(
                    [
                        _button_row(
                            self._instrument_button_label(entry),
                            f"resolve:pick:{idx}",
                        )
                    ]
                )
        else:
            # Fallback: Agree on staged resolved id (legacy single-match).
            buttons.append(
                [_button_row(*BUTTON_RESOLVE_AGREE), _button_row(*BUTTON_RESOLVE_OTHER)]
            )
        if candidates:
            buttons.append([_button_row(*BUTTON_RESOLVE_OTHER)])
        buttons.append([_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)])
        return Screen(
            text="\n".join(lines),
            buttons=buttons,
            state="instrument_confirm",
        )

    def _render_instrument_unresolved(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "instrument_unresolved"
        info = state.resolved_instrument or {}
        source = str(state.requested_symbol or info.get("requested_symbol") or "").strip()
        code = str(info.get("error_code") or "INSTRUMENT_NOT_FOUND")
        message = str(info.get("error_message") or "Instrument not found.")
        candidates = list(state.instrument_candidates or info.get("candidates") or [])
        title = self._flow_title(chat_key)
        lines = [
            f"{title}",
            "",
            "Instrument Resolution",
            f"Source: {source}",
            "Resolved: (not found)",
            "",
            message,
            f"({code})",
        ]
        buttons: List[List[Dict[str, str]]] = []
        # Prefer clickable similar instruments when we have them.
        clickable = [c for c in candidates if self._candidate_symbol(c)]
        if clickable:
            state.instrument_candidates = [
                c if isinstance(c, dict) else {"symbol": self._candidate_symbol(c)}
                for c in clickable[:_INSTRUMENT_PICK_MAX]
            ]
            lines.append("")
            lines.append("Similar instruments:")
            for idx, entry in enumerate(state.instrument_candidates, start=1):
                label = self._instrument_button_label(entry)
                lines.append(f"{idx}. {self._candidate_symbol(entry)}")
                buttons.append(
                    [_button_row(label, f"resolve:pick:{idx - 1}")]
                )
            buttons.append(
                [
                    _button_row(*BUTTON_RESOLVE_RETRY),
                    _button_row(*BUTTON_RESOLVE_OTHER),
                ]
            )
        else:
            if isinstance(candidates, list) and candidates:
                lines.append("")
                lines.append("Candidates:")
                for item in candidates[:8]:
                    label = self._candidate_symbol(item)
                    if label:
                        lines.append(f"• {label}")
            buttons.append(
                [
                    _button_row(*BUTTON_RESOLVE_RETRY),
                    _button_row(*BUTTON_RESOLVE_OTHER),
                ]
            )
        buttons.append([_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)])
        return Screen(
            text="\n".join(lines),
            buttons=buttons,
            state="instrument_unresolved",
        )

    def _render_awaiting_native_symbol(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "awaiting_native_symbol"
        title = self._flow_title(chat_key)
        source = str(state.requested_symbol or "").strip()
        extra = f"\nSource: {source}" if source else ""
        return Screen(
            text=(
                f"{title}\n\n"
                "Enter the exchange-native instrument:"
                f"{extra}"
            ),
            buttons=[
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="awaiting_native_symbol",
        )

    def _pick_instrument_candidate(
        self, chat_key: Tuple[Any, ...], index: int
    ) -> Screen:
        state = self._state_for(chat_key)
        candidates = list(state.instrument_candidates or [])
        if index < 0 or index >= len(candidates):
            if state.resolved_instrument and state.resolved_instrument.get("symbol"):
                return self._render_instrument_confirm(chat_key)
            return self._render_instrument_unresolved(chat_key)
        entry = candidates[index]
        native = self._candidate_symbol(entry)
        if not native:
            return self._render_instrument_unresolved(chat_key)
        info = dict(state.resolved_instrument or {})
        info["symbol"] = native
        if isinstance(entry, dict) and entry.get("price") is not None:
            info["price"] = entry.get("price")
        state.resolved_instrument = info
        self._commit_native_symbol(chat_key, native)
        return self._continue_after_symbol(chat_key)

    def _handle_instrument_confirm(
        self, chat_key: Tuple[Any, ...], suffix: str
    ) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix == "back":
            state.resolved_instrument = None
            state.instrument_candidates = []
            state.symbol = None
            return self._symbol_chooser_screen(chat_key)
        if suffix.startswith("resolve:pick:"):
            raw_idx = suffix[len("resolve:pick:") :].strip()
            try:
                idx = int(raw_idx)
            except ValueError:
                return self._render_instrument_confirm(chat_key)
            return self._pick_instrument_candidate(chat_key, idx)
        if suffix == "resolve:agree":
            # Legacy Agree: first candidate, else staged resolved symbol.
            if state.instrument_candidates:
                return self._pick_instrument_candidate(chat_key, 0)
            info = state.resolved_instrument or {}
            native = str(info.get("symbol") or "").strip()
            if not native:
                return self._render_instrument_unresolved(chat_key)
            self._commit_native_symbol(chat_key, native)
            return self._continue_after_symbol(chat_key)
        if suffix == "resolve:other":
            return self._render_awaiting_native_symbol(chat_key)
        return self._render_instrument_confirm(chat_key)

    def _handle_instrument_unresolved(
        self, chat_key: Tuple[Any, ...], suffix: str
    ) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix == "back":
            state.resolved_instrument = None
            state.instrument_candidates = []
            state.symbol = None
            return self._symbol_chooser_screen(chat_key)
        if suffix.startswith("resolve:pick:"):
            raw_idx = suffix[len("resolve:pick:") :].strip()
            try:
                idx = int(raw_idx)
            except ValueError:
                return self._render_instrument_unresolved(chat_key)
            return self._pick_instrument_candidate(chat_key, idx)
        if suffix == "resolve:retry":
            requested = str(state.requested_symbol or "").strip()
            if not requested:
                return self._symbol_chooser_screen(chat_key)
            return self._apply_entered_instrument(chat_key, requested)
        if suffix == "resolve:other":
            return self._render_awaiting_native_symbol(chat_key)
        return self._render_instrument_unresolved(chat_key)

    def _handle_awaiting_native_symbol(
        self, chat_key: Tuple[Any, ...], text: str
    ) -> Screen:
        native = (text or "").strip()
        if not native:
            return self._render_awaiting_native_symbol(chat_key)
        state = self._state_for(chat_key)
        exchange = str(state.exchange or "").strip()
        # Prefer agent resolve/catalog when available so aliases like
        # GOLD → XAU still surface a pickable venue id. Only fall back
        # to a raw as-is commit when the exchange has no lookup ops.
        if self._agent_supports_instrument_lookup(exchange):
            return self._apply_entered_instrument(
                chat_key, native, manual_native=False
            )
        # Legacy / no-resolve desks: operator-supplied value as-is.
        return self._apply_entered_instrument(
            chat_key, native, manual_native=True
        )

    def _handle_awaiting_native_symbol_callback(
        self, chat_key: Tuple[Any, ...], suffix: str
    ) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix == "back":
            info = state.resolved_instrument or {}
            if state.instrument_candidates or info.get("symbol"):
                return self._render_instrument_confirm(chat_key)
            if info.get("error_code"):
                return self._render_instrument_unresolved(chat_key)
            return self._symbol_chooser_screen(chat_key)
        return self._render_awaiting_native_symbol(chat_key)

    # -- new order symbol selection -------------------------------------

    def _render_new_order(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "new_order"
        state.flow = "new_order"
        state.symbol = None
        state.requested_symbol = None
        state.resolved_instrument = None
        state.instrument_candidates = []
        state.order.clear()
        state.ladder.clear()
        return Screen(
            text=(
                "New Order\n\n"
                "Select Symbol:"
            ),
            buttons=[
                [_button_row(*BUTTON_SYMBOL_BTC), _button_row(*BUTTON_SYMBOL_ETH)],
                [_button_row(*BUTTON_SYMBOL_HYPE), _button_row(*BUTTON_SYMBOL_SOL)],
                [_button_row(*BUTTON_OTHER_SYMBOL)],
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="new_order",
        )

    def _render_new_order_awaiting_symbol(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "awaiting_symbol"
        state.flow = "new_order"
        state.symbol = None
        state.resolved_instrument = None
        return Screen(
            text=(
                "New Order\n\n"
                "Enter Symbol:"
            ),
            buttons=[
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="awaiting_symbol",
        )

    def _render_new_order_side(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "new_order_side"
        state.flow = "new_order"
        symbol = str(state.symbol or state.order.get("symbol") or "").strip()
        exchange = state.exchange or ""
        account = state.account or ""
        return Screen(
            text=(
                "New Order\n\n"
                f"Exchange: {exchange[:1].upper() + exchange[1:] if exchange else exchange}\n"
                f"Account: {account}\n"
                f"Symbol: {symbol}\n\n"
                "Select Side:"
            ),
            buttons=[
                [_button_row(*BUTTON_NEW_ORDER_BUY), _button_row(*BUTTON_NEW_ORDER_SELL)],
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="new_order_side",
        )

    def _handle_new_order(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self._render_action(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix == "other":
            return self._render_new_order_awaiting_symbol(chat_key)
        if suffix.startswith("symbol:"):
            symbol = suffix[len("symbol:"):].strip()
            if symbol.lower() == "other":
                return self._render_new_order_awaiting_symbol(chat_key)
            if not symbol:
                return self._render_new_order(chat_key)
            return self._apply_entered_instrument(chat_key, symbol)
        return self._render_new_order(chat_key)

    def _handle_awaiting_symbol(self, chat_key: Tuple[Any, ...], text: str) -> Screen:
        return self._apply_entered_instrument(chat_key, text)

    def _handle_awaiting_symbol_callback(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            if self._state_for(chat_key).flow == "ladder":
                return self._render_ladder_symbol(chat_key)
            return self._render_new_order(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        return self._render_new_order_awaiting_symbol(chat_key)

    def _decimal_text(self, value: Any) -> Optional[str]:
        try:
            text = str(value).strip().replace(",", "")
            if not text:
                return None
            decimal_value = Decimal(text)
        except Exception:  # noqa: BLE001
            return None
        if decimal_value <= 0:
            return None
        rendered = format(decimal_value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered or "0"

    def _render_new_order_volume(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "awaiting_volume"
        symbol = str(state.order.get("symbol") or state.symbol or "").strip()
        side = str(state.order.get("side") or "").strip().lower()
        side_label = "🔵 Buy" if side == "buy" else "🔴 Sell" if side == "sell" else side.title()
        return Screen(
            text=(
                "New Order\n\n"
                f"Exchange: {state.exchange[:1].upper() + state.exchange[1:] if state.exchange else state.exchange}\n"
                f"Account: {state.account}\n"
                f"Symbol: {symbol}\n"
                f"Side: {side_label}\n\n"
                "Enter Volume:"
            ),
            buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]],
            state="awaiting_volume",
        )

    def _render_new_order_price(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "awaiting_price"
        symbol = str(state.order.get("symbol") or state.symbol or "").strip()
        side = str(state.order.get("side") or "").strip().lower()
        side_label = "🔵 Buy" if side == "buy" else "🔴 Sell" if side == "sell" else side.title()
        volume = _comma_format(state.order.get("volume") or "")
        return Screen(
            text=(
                "New Order\n\n"
                f"Exchange: {state.exchange[:1].upper() + state.exchange[1:] if state.exchange else state.exchange}\n"
                f"Account: {state.account}\n"
                f"Symbol: {symbol}\n"
                f"Side: {side_label}\n"
                f"Volume: {volume}\n\n"
                "Enter Price:"
            ),
            buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]],
            state="awaiting_price",
        )

    def _render_new_order_confirm(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        symbol = str(state.order.get("symbol") or state.symbol or "").strip()
        side = str(state.order.get("side") or "").strip().lower()
        side_label = "🔵 Buy" if side == "buy" else "🔴 Sell" if side == "sell" else side.title()
        volume = _comma_format(state.order.get("volume") or "")
        price = _comma_format(state.order.get("price") or "")
        lines = [
            "⚠️ Confirm New Order",
            "",
            f"Exchange: {state.exchange[:1].upper() + state.exchange[1:] if state.exchange else state.exchange}",
            f"Account: {state.account}",
            "",
            f"Symbol: {symbol}",
            f"Side: {side_label}",
            f"Volume: {volume}",
            f"Price: {price}",
        ]
        state.state = "new_order_confirm"
        return Screen(
            text="\n".join(lines).rstrip(),
            buttons=[
                [_button_row(*BUTTON_CONFIRM_ORDER)],
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="new_order_confirm",
        )

    def _render_new_order_result(self, chat_key: Tuple[Any, ...], response: CanonicalResponse) -> Screen:
        state = self._state_for(chat_key)
        state.state = "new_order_result"
        symbol = str(state.order.get("symbol") or state.symbol or "").strip()
        side = str(state.order.get("side") or "").strip().lower()
        side_label = "🔵 Buy" if side == "buy" else "🔴 Sell" if side == "sell" else side.title()
        lines = ["New Order"]
        order = response.order if response.success and getattr(response, "order", None) is not None else None
        if order is not None:
            lines.extend([
                "",
                "✅ Order Submitted",
                f"Exchange: {state.exchange}",
                f"Account: {state.account}",
                "",
                f"{side_label} {symbol}",
                f"Verified on exchange: {'Yes' if order.verified else 'No'}",
            ])
        else:
            error = response.error
            lines.extend(_render_error_lines(error, "Order submission failed."))
        return Screen(
            text="\n".join(lines).rstrip(),
            buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]],
            state="new_order_result",
        )

    def _render_new_order_text_error(self, field: str, message: str) -> Screen:
        label = "Enter Volume:" if field == "volume" else "Enter Price:"
        return Screen(
            text=f"New Order\n\n{message}\n\n{label}",
            buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]],
            state="awaiting_volume" if field == "volume" else "awaiting_price",
        )

    def _handle_new_order_side(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self._render_new_order(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix == "side:buy":
            state.order["side"] = "buy"
            return self._render_new_order_volume(chat_key)
        if suffix == "side:sell":
            state.order["side"] = "sell"
            return self._render_new_order_volume(chat_key)
        return self._render_new_order_side(chat_key)

    def _handle_new_order_text(self, chat_key: Tuple[Any, ...], text: str) -> Screen:
        state = self._state_for(chat_key)
        value = self._decimal_text(text)
        if value is None:
            field = "volume" if state.state == "awaiting_volume" else "price"
            return self._render_new_order_text_error(field, "Please enter a positive numeric value.")
        if state.state == "awaiting_volume":
            state.order["volume"] = value
            return self._render_new_order_price(chat_key)
        if state.state == "awaiting_price":
            state.order["price"] = value
            return self._render_new_order_confirm(chat_key)
        return self._render_new_order(chat_key)

    def _handle_position_price_text(self, chat_key: Tuple[Any, ...], text: str) -> Screen:
        state = self._state_for(chat_key)
        raw = str(text or "").strip().replace(",", "")
        try:
            value = Decimal(raw)
        except Exception:  # noqa: BLE001
            return self._render_position_tp_input(chat_key) if state.state == "awaiting_tp_price" else self._render_position_sl_input(chat_key)
        if value < 0:
            return self._render_position_tp_input(chat_key) if state.state == "awaiting_tp_price" else self._render_position_sl_input(chat_key)
        rendered = format(value.normalize(), "f") if value != 0 else "0"
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".") or "0"
        if state.state == "awaiting_tp_price":
            state.position["tp_price_input"] = rendered
            return self._render_position_tp_confirm(chat_key, rendered)
        if state.state == "awaiting_sl_price":
            state.position["sl_price_input"] = rendered
            return self._render_position_sl_confirm(chat_key, rendered)
        return self._render_position_detail(chat_key, state.position)

    def _handle_new_order_confirm(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self._render_new_order_price(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix != "confirm":
            return self._render_new_order_confirm(chat_key)
        request = {
            "operation": "new_order",
            "exchange": state.exchange or "",
            "account": state.account or "",
            "symbol": state.order.get("symbol") or state.symbol or "",
            "side": state.order.get("side") or "",
            "order_type": "limit",
            "volume": state.order.get("volume") or "",
            "price": state.order.get("price") or "",
        }
        response: CanonicalResponse = self._desk.execute(request)
        return self._render_new_order_result(chat_key, response)

    def _handle_awaiting_text(self, chat_key: Tuple[Any, ...], text: str) -> Screen:
        state = self._state_for(chat_key)
        if state.state in {
            "awaiting_ladder_order_count",
            "awaiting_ladder_total_volume",
            "awaiting_ladder_start_price",
            "awaiting_ladder_end_price",
        }:
            return self._handle_awaiting_ladder_text(chat_key, text)
        if state.state in {"awaiting_volume", "awaiting_price"}:
            return self._handle_new_order_text(chat_key, text)
        if state.state in {"awaiting_tp_price", "awaiting_sl_price"}:
            return self._handle_position_price_text(chat_key, text)
        return self._render_new_order(chat_key)

    def _render_ladder(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "ladder"
        state.flow = "ladder"
        state.symbol = None
        state.requested_symbol = None
        state.resolved_instrument = None
        state.ladder.clear()
        return Screen(
            text=(
                "Ladder\n\n"
                "Select Distribution:"
            ),
            buttons=[
                [_button_row(*BUTTON_LADDER_HALF_GAUSSIAN)],
                [_button_row(*BUTTON_LADDER_UNIFORM)],
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="ladder",
        )

    def _handle_new_order_text_callback(self, chat_key: Tuple[Any, ...], suffix: str, field: str) -> Screen:
        if suffix == "back":
            if field == "volume":
                return self._render_new_order_side(chat_key)
            return self._render_new_order_volume(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        return self._render_new_order_volume(chat_key) if field == "volume" else self._render_new_order_price(chat_key)

    def _handle_new_order_result(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_action(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        return self._render_action(chat_key)

    def _handle_ladder(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self._render_action(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix == "distribution:half_gaussian":
            state.ladder["distribution"] = "half_gaussian"
            return self._render_ladder_symbol(chat_key)
        if suffix == "distribution:uniform":
            state.ladder["distribution"] = "uniform"
            return self._render_ladder_symbol(chat_key)
        return self._render_ladder(chat_key)

    def _render_ladder_symbol(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "ladder_symbol"
        state.flow = "ladder"
        state.symbol = None
        state.resolved_instrument = None
        return Screen(
            text=(
                "Ladder\n\n"
                "Select Symbol:"
            ),
            buttons=[
                [_button_row(*BUTTON_SYMBOL_BTC), _button_row(*BUTTON_SYMBOL_ETH)],
                [_button_row(*BUTTON_SYMBOL_HYPE), _button_row(*BUTTON_SYMBOL_SOL)],
                [_button_row(*BUTTON_OTHER_SYMBOL)],
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="ladder_symbol",
        )

    def _render_ladder_awaiting_symbol(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "awaiting_symbol"
        state.flow = "ladder"
        state.symbol = None
        state.resolved_instrument = None
        return Screen(
            text=(
                "Ladder\n\n"
                "Enter Symbol:"
            ),
            buttons=[
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="awaiting_symbol",
        )

    def _handle_ladder_symbol(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self._render_ladder(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix == "other":
            return self._render_ladder_awaiting_symbol(chat_key)
        if suffix.startswith("symbol:"):
            symbol = suffix[len("symbol:"):].strip()
            if symbol.lower() == "other":
                return self._render_ladder_awaiting_symbol(chat_key)
            if not symbol:
                return self._render_ladder_symbol(chat_key)
            return self._apply_entered_instrument(chat_key, symbol)
        return self._render_ladder_symbol(chat_key)

    def _render_ladder_side(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "ladder_side"
        state.flow = "ladder"
        return Screen(
            text=(
                "Ladder\n\n"
                f"Symbol: {state.symbol or ''}\n"
                "Select Side:"
            ),
            buttons=[
                [_button_row(*BUTTON_LADDER_BUY), _button_row(*BUTTON_LADDER_SELL)],
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="ladder_side",
        )

    def _handle_ladder_side(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self._render_ladder_symbol(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix == "side:buy":
            state.ladder["side"] = "buy"
            return self._render_ladder_order_count(chat_key)
        if suffix == "side:sell":
            state.ladder["side"] = "sell"
            return self._render_ladder_order_count(chat_key)
        return self._render_ladder_side(chat_key)

    def _render_ladder_order_count(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "awaiting_ladder_order_count"
        state.flow = "ladder"
        return Screen(
            text="Ladder\n\nEnter Number of Orders:",
            buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]],
            state="awaiting_ladder_order_count",
        )

    def _render_ladder_total_volume(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "awaiting_ladder_total_volume"
        state.flow = "ladder"
        return Screen(
            text="Ladder\n\nEnter Total Volume:",
            buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]],
            state="awaiting_ladder_total_volume",
        )

    def _render_ladder_start_price(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "awaiting_ladder_start_price"
        state.flow = "ladder"
        return Screen(
            text="Ladder\n\nEnter Start Price:",
            buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]],
            state="awaiting_ladder_start_price",
        )

    def _render_ladder_end_price(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "awaiting_ladder_end_price"
        state.flow = "ladder"
        return Screen(
            text="Ladder\n\nEnter End Price:",
            buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]],
            state="awaiting_ladder_end_price",
        )

    def _render_ladder_confirm(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        ladder = state.ladder
        symbol = str(ladder.get("symbol") or state.symbol or "").strip()
        distribution = str(ladder.get("distribution") or "").strip()
        side = str(ladder.get("side") or "").strip().lower()
        side_label = "🔵 Buy" if side == "buy" else "🔴 Sell" if side == "sell" else side.title()
        requested_count = ladder.get("order_count", "")
        total_volume = ladder.get("total_volume", "")
        start_price = ladder.get("start_price", "")
        end_price = ladder.get("end_price", "")
        lines = [
            "⚠️ Confirm Ladder",
            "",
            f"Exchange: {state.exchange}",
            f"Account: {state.account}",
            f"Symbol: {symbol}",
            f"Side: {side_label}",
            f"Distribution: {'Half Gaussian' if distribution == 'half_gaussian' else 'Uniform' if distribution == 'uniform' else distribution}",
            f"Orders: {requested_count}",
            f"Total Volume: {_comma_format(total_volume)}",
            f"Start Price: {_comma_format(start_price)}",
            f"End Price: {_comma_format(end_price)}",
            "",
            "Smallest size near Start",
            "Largest size near End",
        ]
        state.state = "ladder_confirm"
        return Screen(
            text="\n".join(lines).rstrip(),
            buttons=[
                [_button_row(*BUTTON_LADDER_CONFIRM)],
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="ladder_confirm",
        )

    def _handle_awaiting_ladder_text(self, chat_key: Tuple[Any, ...], text: str) -> Screen:
        state = self._state_for(chat_key)
        value = (text or "").strip()
        if not value:
            return self._render_ladder_text_error(chat_key, state.state, "Please enter a value.")
        if state.state == "awaiting_ladder_order_count":
            try:
                count = int(value)
            except Exception:  # noqa: BLE001
                return self._render_ladder_text_error(chat_key, state.state, "Order count must be a whole number.")
            if count <= 0:
                return self._render_ladder_text_error(chat_key, state.state, "Order count must be greater than zero.")
            state.ladder["order_count"] = str(count)
            return self._render_ladder_total_volume(chat_key)
        if state.state == "awaiting_ladder_total_volume":
            state.ladder["total_volume"] = value
            return self._render_ladder_start_price(chat_key)
        if state.state == "awaiting_ladder_start_price":
            state.ladder["start_price"] = value
            return self._render_ladder_end_price(chat_key)
        if state.state == "awaiting_ladder_end_price":
            start = self._decimal_for_ladder(state.ladder.get("start_price"))
            end = self._decimal_for_ladder(value)
            if start is None or end is None:
                return self._render_ladder_text_error(chat_key, state.state, "Prices must be valid numbers.")
            side = str(state.ladder.get("side") or "").strip().lower()
            if side == "buy" and not (end < start):
                return self._render_ladder_text_error(chat_key, state.state, "For a BUY ladder, End Price must be lower than Start Price.")
            if side == "sell" and not (end > start):
                return self._render_ladder_text_error(chat_key, state.state, "For a SELL ladder, End Price must be higher than Start Price.")
            state.ladder["end_price"] = value
            return self._render_ladder_confirm(chat_key)
        return self._render_ladder(chat_key)

    def _render_ladder_text_error(self, chat_key: Tuple[Any, ...], state_name: str, message: str) -> Screen:
        state = self._state_for(chat_key)
        title = "Ladder"
        if state_name == "awaiting_ladder_order_count":
            buttons = [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]]
            return Screen(text=f"{title}\n\nEnter Number of Orders:\n\n{message}", buttons=buttons, state=state_name)
        if state_name == "awaiting_ladder_total_volume":
            buttons = [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]]
            return Screen(text=f"{title}\n\nEnter Total Volume:\n\n{message}", buttons=buttons, state=state_name)
        if state_name == "awaiting_ladder_start_price":
            buttons = [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]]
            return Screen(text=f"{title}\n\nEnter Start Price:\n\n{message}", buttons=buttons, state=state_name)
        buttons = [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]]
        return Screen(text=f"{title}\n\nEnter End Price:\n\n{message}", buttons=buttons, state=state_name)

    def _handle_ladder_text_callback(self, chat_key: Tuple[Any, ...], suffix: str, field: str) -> Screen:
        if suffix == "back":
            if field == "order_count":
                return self._render_ladder_side(chat_key)
            if field == "total_volume":
                return self._render_ladder_order_count(chat_key)
            if field == "start_price":
                return self._render_ladder_total_volume(chat_key)
            if field == "end_price":
                return self._render_ladder_start_price(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        return self._render_ladder_text_for_field(chat_key, field)

    def _render_ladder_text_for_field(self, chat_key: Tuple[Any, ...], field: str) -> Screen:
        if field == "order_count":
            return self._render_ladder_order_count(chat_key)
        if field == "total_volume":
            return self._render_ladder_total_volume(chat_key)
        if field == "start_price":
            return self._render_ladder_start_price(chat_key)
        return self._render_ladder_end_price(chat_key)

    def _decimal_for_ladder(self, value: Any) -> Optional[Decimal]:
        try:
            text = str(value).strip()
            if not text:
                return None
            return Decimal(text)
        except Exception:  # noqa: BLE001
            return None

    def _handle_ladder_confirm(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_ladder_end_price(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix != "confirm":
            return self._render_ladder_confirm(chat_key)
        state = self._state_for(chat_key)
        request = {
            "operation": "ladder",
            "exchange": state.exchange or "",
            "account": state.account or "",
            "symbol": state.ladder.get("symbol") or state.symbol or "",
            "side": state.ladder.get("side") or "",
            "distribution": state.ladder.get("distribution") or "",
            "order_count": state.ladder.get("order_count") or "",
            "total_volume": state.ladder.get("total_volume") or "",
            "start_price": state.ladder.get("start_price") or "",
            "end_price": state.ladder.get("end_price") or "",
        }
        response: CanonicalResponse = self._desk.execute(request)
        state.state = "ladder_result"
        lines = ["Ladder"]
        if response.success and response.ladder is not None:
            ladder = response.ladder
            lines.extend([
                "",
                "Submitted.",
                f"Status: {ladder.status}",
                f"Requested orders: {ladder.requested_order_count}",
                f"Submitted orders: {ladder.submitted_order_count}",
                f"Requested volume: {ladder.requested_volume}",
                f"Submitted volume: {ladder.submitted_volume}",
                f"Batches: {ladder.batch_count}",
                f"Verified: {'Yes' if ladder.verified else 'No'}",
            ])
        else:
            error = response.error
            lines.extend(_render_error_lines(error, "Ladder submission failed."))
        return Screen(
            text="\n".join(lines).rstrip(),
            buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]],
            state="ladder_result",
        )

    def _render_cancel_orders(self, chat_key: Tuple[Any, ...], refresh: bool) -> Screen:
        state = self._state_for(chat_key)
        state.state = "cancel_orders"
        state.flow = "cancel_orders"
        state.cancel.clear()
        exchange = state.exchange or ""
        account = state.account or ""
        request = {
            "operation": "positions_orders",
            "exchange": exchange,
            "account": account,
        }
        response: CanonicalResponse = self._desk.execute(request)
        header = f"❌ Cancel Orders -- {exchange} / {account}"
        lines: List[str] = [header, "", "Select orders to cancel:", ""]
        buttons: List[List[Dict[str, str]]] = []
        if response.success:
            order_groups = response.order_groups or []
            lines.append(f"Open orders: {response.open_order_count or 0}")
            if order_groups:
                lines.append("")
                for group in order_groups:
                    lines.append(_order_group_summary_line(group))
                    lines.append("")
                    buttons.append([
                        _button_row(
                            _order_group_button_text(group),
                            f"cancel_group:{quote(str(group.symbol), safe='')}:{group.side}",
                        )
                    ])
                if lines and not lines[-1]:
                    lines.pop()
            else:
                lines.append("No open orders.")
        else:
            err_code = response.error.code if response.error else "POSITIONS_ORDERS_UNAVAILABLE"
            err_msg = response.error.message if response.error else "Open orders unavailable."
            reason_line = None
            if response.error is not None and isinstance(getattr(response.error, "exchange_reason", None), str):
                reason_text = getattr(response.error, "exchange_reason", None)
                if reason_text and str(reason_text).strip():
                    reason_line = f"Reason: {reason_text}"
            extra_lines = [f"Error: {err_msg}"]
            if reason_line is not None:
                extra_lines.append(reason_line)
            extra_lines.append(f"({err_code})")
            lines.extend(extra_lines)
        if refresh:
            state.cancel.pop("refreshed", None)
        buttons.append([_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)])
        return Screen(
            text="\n".join(lines).rstrip(),
            buttons=buttons,
            state="cancel_orders",
        )

    def _cancel_group_details(self, chat_key: Tuple[Any, ...]) -> Optional[Any]:
        state = self._state_for(chat_key)
        exchange = state.exchange or ""
        account = state.account or ""
        symbol = str(state.cancel.get("symbol") or "").strip()
        side = str(state.cancel.get("side") or "").strip().lower()
        if not exchange or not account or not symbol or side not in {"buy", "sell"}:
            return None
        response = self._desk.execute(
            {
                "operation": "positions_orders",
                "exchange": exchange,
                "account": account,
            }
        )
        if not getattr(response, "success", False):
            return None
        for group in response.order_groups or []:
            if str(getattr(group, "symbol", "")).strip() == symbol and str(getattr(group, "side", "")).strip().lower() == side:
                return group
        return None

    def _render_cancel_confirm(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        group = self._cancel_group_details(chat_key)
        symbol = str(state.cancel.get("symbol") or "").strip()
        side = str(state.cancel.get("side") or "").strip().lower()
        side_label = "Buy" if side == "buy" else "Sell" if side == "sell" else side.title()
        lines = [
            "⚠️ Confirm Cancellation",
            "",
            f"Exchange: {state.exchange}",
            f"Account: {state.account}",
            "",
            f"{_direction_emoji(side)} {symbol} {side}",
        ]
        if group is not None:
            lines.extend([
                "",
                f"Orders: {getattr(group, 'order_count', 0)}",
                f"Total size: {_display_or_dash(getattr(group, 'total_size', ''))}",
                f"VWAP: {_display_or_dash(getattr(group, 'vwap', ''))}",
            ])
            min_price = _display_or_dash(getattr(group, 'min_price', ''))
            max_price = _display_or_dash(getattr(group, 'max_price', ''))
            if min_price == max_price:
                lines.append(f"Range: @ {min_price}")
            else:
                lines.append(f"Range: {min_price}-{max_price}")
        lines.extend([
            "",
            f"Cancel this {symbol} {side_label} group?",
        ])
        state.state = "cancel_group_confirm"
        return Screen(
            text="\n".join(lines).rstrip(),
            buttons=[
                [_button_row("✅ Confirm Cancel", "confirm")],
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="cancel_group_confirm",
        )

    def _handle_cancel_orders(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "refresh":
            return self._render_cancel_orders(chat_key, refresh=True)
        if suffix == "back":
            state.account = None
            return self._render_select_account(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix.startswith("cancel_group:"):
            parts = suffix.split(":", 2)
            if len(parts) != 3:
                return self._render_cancel_orders(chat_key, refresh=False)
            _, symbol, side = parts
            state.cancel = {"symbol": unquote(symbol), "side": side}
            return self._render_cancel_confirm(chat_key)
        return self._render_cancel_orders(chat_key, refresh=False)

    def _handle_cancel_group_confirm(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self._render_cancel_orders(chat_key, refresh=True)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix != "confirm":
            return self._render_cancel_confirm(chat_key)
        request = {
            "operation": "cancel_order_group",
            "exchange": state.exchange or "",
            "account": state.account or "",
            "symbol": state.cancel.get("symbol") or "",
            "side": state.cancel.get("side") or "",
        }
        response: CanonicalResponse = self._desk.execute(request)
        state.state = "cancel_result"
        lines = ["Cancel Orders"]
        if response.success and getattr(response, "cancel_group", None) is not None:
            result = response.cancel_group
            lines.extend([
                "",
                "✅ Orders Cancelled",
                f"Exchange: {state.exchange}",
                f"Account: {state.account}",
                "",
                f"{_direction_emoji(result.side)} {result.symbol} {result.side}",
                f"Cancelled: {result.cancelled_order_count} orders",
                f"Verification: {'Passed' if result.verified else 'Failed'}",
            ])
        else:
            error = response.error
            lines.extend(_render_error_lines(error, "Cancellation failed."))
        return Screen(
            text="\n".join(lines).rstrip(),
            buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]],
            state="cancel_result",
        )

    def _handle_cancel_result(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_cancel_orders(chat_key, refresh=True)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        return self._render_cancel_orders(chat_key, refresh=True)

    # -- positions & orders screen --------------------------------------

    def _render_positions_orders(
        self,
        chat_key: Tuple[Any, ...],
        refresh: bool,
    ) -> Screen:
        state = self._state_for(chat_key)
        state.state = "positions_orders"
        exchange = state.exchange or ""
        account = state.account or ""
        request = {
            "operation": "positions_orders",
            "exchange": exchange,
            "account": account,
        }
        response: CanonicalResponse = self._desk.execute(request)
        header = f"📋 Open Orders & 💼 Positions — {exchange} / {account}"
        if response.success:
            positions = response.positions or []
            order_groups = response.order_groups or []
            lines: List[str] = [header, "", "Current Positions", ""]

            if positions:
                for position in positions:
                    lines.append(
                        " ".join(
                            [
                                f"{_direction_emoji(position.side)} {position.symbol}",
                            ]
                        )
                    )
                    lines.append(
                        " ".join(
                            [
                                f"Size: {_display_or_dash(position.size)}",
                                f"Entry: {_display_or_dash(position.entry_price)}",
                                f"PnL: {_pnl_format(position.pnl)}",
                                f"TP: {_display_protection(position.tp, getattr(position, 'tp_count', None))}",
                                f"SL: {_display_protection(position.sl, getattr(position, 'sl_count', None))}",
                            ]
                        )
                    )
                    lines.append("")
            else:
                lines.append("No open positions.")
                lines.append("")

            lines.append(f"Open orders: {response.open_order_count or 0}")
            if order_groups:
                for group in order_groups:
                    lines.append(_order_group_summary_line(group))
            else:
                lines.append("No open orders.")

            text = "\n".join(lines).rstrip()
        else:
            err_code = response.error.code if response.error else "POSITIONS_ORDERS_UNAVAILABLE"
            err_msg = response.error.message if response.error else "Positions and orders unavailable."
            reason_line = None
            if response.error is not None:
                reason_text = getattr(response.error, "exchange_reason", None)
                if isinstance(reason_text, str) and reason_text.strip():
                    reason_line = f"Reason: {reason_text}"
            lines = [header, "", f"Error: {err_msg}"]
            if reason_line is not None:
                lines.append(reason_line)
            lines.append(f"({err_code})")
            text = "\n".join(lines)
        return Screen(
            text=text,
            buttons=[
                [_button_row(*BUTTON_REFRESH)],
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="positions_orders",
        )

    def _handle_positions_orders(
        self,
        chat_key: Tuple[Any, ...],
        suffix: str,
    ) -> Screen:
        if suffix == "refresh":
            return self._render_positions_orders(chat_key, refresh=True)
        if suffix == "back":
            state = self._state_for(chat_key)
            state.account = None
            return self._render_select_account(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        return self._render_positions_orders(chat_key, refresh=False)

    def _render_positions_management(self, chat_key: Tuple[Any, ...], refresh: bool) -> Screen:
        state = self._state_for(chat_key)
        state.state = "positions_management"
        state.flow = "positions_management"
        state.position.clear()
        state.order.clear()
        exchange = state.exchange or ""
        account = state.account or ""
        response: CanonicalResponse = self._desk.execute(
            {
                "operation": "positions_management",
                "exchange": exchange,
                "account": account,
            }
        )
        header = f"💼 Positions Management — {exchange} / {account}"
        if response.success:
            positions = list(response.positions or [])
            state.positions = [pos.to_dict() if hasattr(pos, "to_dict") else dict(pos) for pos in positions]  # type: ignore[arg-type]
            lines: List[str] = [header, "", "Current Positions", ""]
            buttons: List[List[Dict[str, str]]] = []
            if positions:
                for position in positions:
                    side = str(getattr(position, "side", "")).strip().lower()
                    symbol = str(getattr(position, "symbol", "")).strip()
                    lines.extend(
                        [
                            f"{_direction_emoji(side)} {symbol}",
                            (
                                f"Size: {_display_or_dash(getattr(position, 'size', None))}"
                                f"  Entry: {_display_or_dash(getattr(position, 'entry_price', None))}"
                                f"  PnL: {_pnl_format(getattr(position, 'pnl', None))}"
                            ),
                            f"TP: {_display_protection(getattr(position, 'tp', None), getattr(position, 'tp_count', None))}",
                            f"SL: {_display_protection(getattr(position, 'sl', None), getattr(position, 'sl_count', None))}",
                            "",
                        ]
                    )
                    buttons.append([
                        _button_row(f"{_direction_emoji(side)} {symbol} {side.title()}", f"position:{symbol}:{side}"),
                    ])
            else:
                lines.append("No open positions.")
                lines.append("")
            buttons.append([_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)])
            return Screen(text="\n".join(lines).rstrip(), buttons=buttons, state="positions_management")

        err_code = response.error.code if response.error else "POSITIONS_UNAVAILABLE"
        err_msg = response.error.message if response.error else "Positions unavailable."
        reason_line = None
        if response.error is not None:
            reason_text = getattr(response.error, "exchange_reason", None)
            if isinstance(reason_text, str) and reason_text.strip():
                reason_line = f"Reason: {reason_text}"
        lines = [header, "", f"Error: {err_msg}"]
        if reason_line is not None:
            lines.append(reason_line)
        lines.append(f"({err_code})")
        return Screen(
            text="\n".join(lines),
            buttons=[
                [_button_row(*BUTTON_REFRESH)],
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="positions_management",
        )

    def _selected_position(self, chat_key: Tuple[Any, ...], suffix: str = "") -> Optional[Dict[str, Any]]:
        state = self._state_for(chat_key)
        if suffix.startswith("position:"):
            parts = suffix.split(":", 2)
            if len(parts) == 3:
                symbol = parts[1].strip().upper()
                side = parts[2].strip().lower()
                for position in state.positions:
                    if str(position.get("symbol", "")).strip().upper() == symbol and str(position.get("side", "")).strip().lower() == side:
                        return dict(position)
        current = state.position if state.position else None
        return dict(current) if current else None

    def _render_position_detail(self, chat_key: Tuple[Any, ...], position: Dict[str, Any]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "position_detail"
        state.flow = "positions_management"
        state.position = dict(position)
        symbol = str(position.get("symbol", "")).strip()
        side = str(position.get("side", "")).strip().lower()
        side_label = "Long" if side == "long" else "Short" if side == "short" else side.title()
        header = f"💼 {symbol} Position — {state.exchange} / {state.account}"
        lines = [
            header,
            "",
            f"{_direction_emoji(side)} {side_label}",
            f"Size: {_display_or_dash(position.get('size'))}",
            f"Entry: {_display_or_dash(position.get('entry_price'))}",
            f"PnL: {_pnl_format(position.get('pnl'))}",
            f"TP: {_display_protection(position.get('tp'), position.get('tp_count'))}",
            f"SL: {_display_protection(position.get('sl'), position.get('sl_count'))}",
            "",
            "What would you like to do?",
        ]
        return Screen(
            text="\n".join(lines),
            buttons=[
                [_button_row("Set TP", "set_tp"), _button_row("Set SL", "set_sl")],
                [_button_row("Close Position", "close_position")],
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="position_detail",
        )

    def _render_position_tp_input(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "awaiting_tp_price"
        position = state.position
        symbol = str(position.get("symbol", "")).strip()
        side = str(position.get("side", "")).strip().lower()
        side_label = "Long" if side == "long" else "Short" if side == "short" else side.title()
        return Screen(
            text=(
                "Set Take Profit\n\n"
                f"{_direction_emoji(side)} {symbol} {side_label}\n"
                f"Current TP: {_display_protection(position.get('tp'), position.get('tp_count'))}\n\n"
                "Enter TP Price:\n"
                "Enter 0 to remove TP."
            ),
            buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]],
            state="awaiting_tp_price",
        )

    def _render_position_sl_input(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "awaiting_sl_price"
        position = state.position
        symbol = str(position.get("symbol", "")).strip()
        side = str(position.get("side", "")).strip().lower()
        side_label = "Long" if side == "long" else "Short" if side == "short" else side.title()
        return Screen(
            text=(
                "Set Stop Loss\n\n"
                f"{_direction_emoji(side)} {symbol} {side_label}\n"
                f"Current SL: {_display_protection(position.get('sl'), position.get('sl_count'))}\n\n"
                "Enter SL Price:\n"
                "Enter 0 to remove SL."
            ),
            buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]],
            state="awaiting_sl_price",
        )

    def _render_position_tp_confirm(self, chat_key: Tuple[Any, ...], price: str) -> Screen:
        state = self._state_for(chat_key)
        state.state = "position_tp_confirm"
        position = state.position
        symbol = str(position.get("symbol", "")).strip()
        side = str(position.get("side", "")).strip().lower()
        side_label = "Long" if side == "long" else "Short" if side == "short" else side.title()
        current_tp = _display_protection(position.get("tp"), position.get("tp_count"))
        if price == "0":
            if current_tp == "—":
                return Screen(
                    text=(
                        "Set Take Profit\n\n"
                        f"{_direction_emoji(side)} {symbol} {side_label}\n"
                        f"Current TP: {current_tp}\n\n"
                        "No Take Profit was set."
                    ),
                    buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]],
                    state="position_tp_confirm",
                )
            confirm_label = "Confirm Remove"
            title = "⚠️ Confirm Remove Take Profit"
            new_line = f"Current TP: {current_tp}"
        else:
            confirm_label = "Confirm"
            title = "⚠️ Confirm Take Profit"
            new_line = f"New TP: {price}"
        return Screen(
            text="\n".join(
                [
                    title,
                    "",
                    f"Exchange: {state.exchange}",
                    f"Account: {state.account}",
                    f"Position: {_direction_emoji(side)} {symbol} {side_label}",
                    f"Current Size: {_display_or_dash(position.get('size'))}",
                    f"Current TP: {current_tp}",
                    new_line,
                ]
            ),
            buttons=[
                [_button_row(confirm_label, "confirm")],
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="position_tp_confirm",
        )

    def _render_position_sl_confirm(self, chat_key: Tuple[Any, ...], price: str) -> Screen:
        state = self._state_for(chat_key)
        state.state = "position_sl_confirm"
        position = state.position
        symbol = str(position.get("symbol", "")).strip()
        side = str(position.get("side", "")).strip().lower()
        side_label = "Long" if side == "long" else "Short" if side == "short" else side.title()
        current_sl = _display_protection(position.get("sl"), position.get("sl_count"))
        if price == "0":
            if current_sl == "—":
                return Screen(
                    text=(
                        "Set Stop Loss\n\n"
                        f"{_direction_emoji(side)} {symbol} {side_label}\n"
                        f"Current SL: {current_sl}\n\n"
                        "No Stop Loss was set."
                    ),
                    buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]],
                    state="position_sl_confirm",
                )
            confirm_label = "Confirm Remove"
            title = "⚠️ Confirm Remove Stop Loss"
            new_line = f"Current SL: {current_sl}"
        else:
            confirm_label = "Confirm"
            title = "⚠️ Confirm Stop Loss"
            new_line = f"New SL: {price}"
        return Screen(
            text="\n".join(
                [
                    title,
                    "",
                    f"Exchange: {state.exchange}",
                    f"Account: {state.account}",
                    f"Position: {_direction_emoji(side)} {symbol} {side_label}",
                    f"Current Size: {_display_or_dash(position.get('size'))}",
                    f"Current SL: {current_sl}",
                    new_line,
                ]
            ),
            buttons=[
                [_button_row(confirm_label, "confirm")],
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="position_sl_confirm",
        )

    def _render_position_close_confirm(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "position_close_confirm"
        position = state.position
        symbol = str(position.get("symbol", "")).strip()
        side = str(position.get("side", "")).strip().lower()
        side_label = "Long" if side == "long" else "Short" if side == "short" else side.title()
        return Screen(
            text="\n".join(
                [
                    "⚠️ Confirm Close Position",
                    "",
                    f"Exchange: {state.exchange}",
                    f"Account: {state.account}",
                    "",
                    f"{_direction_emoji(side)} {symbol} {side_label}",
                    f"Displayed Size: {_display_or_dash(position.get('size'))}",
                    "",
                    "This will close the entire current position.",
                ]
            ),
            buttons=[
                [_button_row("Confirm Close", "confirm")],
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="position_close_confirm",
        )

    def _render_position_action_result(self, chat_key: Tuple[Any, ...], response: CanonicalResponse, operation: str) -> Screen:
        state = self._state_for(chat_key)
        action = getattr(response, "position_action", None)
        action_dict = action.to_dict() if action is not None and hasattr(action, "to_dict") else (dict(action) if isinstance(action, dict) else {})
        action_dict["operation"] = operation
        action_dict["return_target"] = "positions_management" if operation == "close_position" else "position_detail"
        state.position_action = action_dict
        state.state = "position_action_result"

        symbol = str(action_dict.get("symbol") or state.position.get("symbol") or "").strip()
        lines: List[str] = []
        if response.success and action_dict:
            if operation == "set_tp":
                lines.append("✅ Take Profit Updated" if not action_dict.get("removed") else "✅ Take Profit Removed")
            elif operation == "set_sl":
                lines.append("✅ Stop Loss Updated" if not action_dict.get("removed") else "✅ Stop Loss Removed")
            else:
                lines.append("✅ Position Closed")
            lines.extend([
                "",
                f"Exchange: {state.exchange}",
                f"Account: {state.account}",
                f"Symbol: {symbol}",
            ])
            if action_dict.get("price") is not None:
                label = "TP" if operation == "set_tp" else "SL"
                lines.append(f"{label}: {_display_or_dash(action_dict.get('price'))}")
            if action_dict.get("current_side") is not None:
                lines.append(f"Side: {str(action_dict.get('current_side')).title()}")
            if action_dict.get("current_size") is not None:
                lines.append(f"Size: {_display_or_dash(action_dict.get('current_size'))}")
            if action_dict.get("message"):
                lines.append(str(action_dict.get("message")))
            lines.append(f"Verified: {'Passed' if action_dict.get('verified') else 'Failed'}")
        else:
            title = "Position Action Failed"
            if operation == "set_tp":
                title = "Take Profit Failed"
            elif operation == "set_sl":
                title = "Stop Loss Failed"
            elif operation == "close_position":
                title = "Close Position Failed"
            lines.extend([title, ""])
            error = response.error
            lines.extend(_render_error_lines(error, "Position action failed."))

        return Screen(
            text="\n".join(lines).rstrip(),
            buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)]],
            state="position_action_result",
        )

    def _handle_positions_management(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            state.account = None
            return self._render_select_account(chat_key)
        if suffix == "refresh":
            return self._render_positions_management(chat_key, refresh=True)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix.startswith("position:"):
            position = self._selected_position(chat_key, suffix)
            if position is not None:
                return self._render_position_detail(chat_key, position)
        return self._render_positions_management(chat_key, refresh=False)

    def _handle_position_detail(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self._render_positions_management(chat_key, refresh=False)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix == "set_tp":
            return self._render_position_tp_input(chat_key)
        if suffix == "set_sl":
            return self._render_position_sl_input(chat_key)
        if suffix == "close_position":
            return self._render_position_close_confirm(chat_key)
        if suffix == "confirm":
            return self._render_position_detail(chat_key, state.position)
        return self._render_position_detail(chat_key, state.position)

    def _handle_position_tp_input(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_position_detail(chat_key, self._selected_position(chat_key) or self._state_for(chat_key).position)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        return self._render_position_tp_input(chat_key)

    def _handle_position_tp_confirm(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_position_detail(chat_key, self._selected_position(chat_key) or self._state_for(chat_key).position)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix != "confirm":
            return self._render_position_tp_confirm(chat_key, str(self._state_for(chat_key).position.get("tp_price_input") or ""))
        state = self._state_for(chat_key)
        request = {
            "operation": "set_tp",
            "exchange": state.exchange or "",
            "account": state.account or "",
            "symbol": state.position.get("symbol") or "",
            "price": state.position.get("tp_price_input") or "",
        }
        response: CanonicalResponse = self._desk.execute(request)
        return self._render_position_action_result(chat_key, response, "set_tp")

    def _handle_position_sl_input(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_position_detail(chat_key, self._selected_position(chat_key) or self._state_for(chat_key).position)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        return self._render_position_sl_input(chat_key)

    def _handle_position_sl_confirm(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_position_detail(chat_key, self._selected_position(chat_key) or self._state_for(chat_key).position)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix != "confirm":
            return self._render_position_sl_confirm(chat_key, str(self._state_for(chat_key).position.get("sl_price_input") or ""))
        state = self._state_for(chat_key)
        request = {
            "operation": "set_sl",
            "exchange": state.exchange or "",
            "account": state.account or "",
            "symbol": state.position.get("symbol") or "",
            "price": state.position.get("sl_price_input") or "",
        }
        response: CanonicalResponse = self._desk.execute(request)
        return self._render_position_action_result(chat_key, response, "set_sl")

    def _handle_position_close_confirm(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_position_detail(chat_key, self._selected_position(chat_key) or self._state_for(chat_key).position)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        if suffix != "confirm":
            return self._render_position_close_confirm(chat_key)
        state = self._state_for(chat_key)
        request = {
            "operation": "close_position",
            "exchange": state.exchange or "",
            "account": state.account or "",
            "symbol": state.position.get("symbol") or "",
        }
        response: CanonicalResponse = self._desk.execute(request)
        return self._render_position_action_result(chat_key, response, "close_position")

    def _handle_position_action_result(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            if str(state.position_action.get("return_target") or "") == "positions_management":
                return self._render_positions_management(chat_key, refresh=True)
            return self._render_position_detail(chat_key, self._selected_position(chat_key) or state.position)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        return self._render_positions_management(chat_key, refresh=True)

    # -- balance screen --------------------------------------------------

    def _render_balance(
        self,
        chat_key: Tuple[Any, ...],
        refresh: bool,
    ) -> Screen:
        state = self._state_for(chat_key)
        state.state = "balance"
        exchange = state.exchange or ""
        account = state.account or ""
        request = {
            "operation": "balance",
            "exchange": exchange,
            "account": account,
        }
        response: CanonicalResponse = self._desk.execute(request)
        if response.success and response.balance is not None:
            value = response.balance.value
            unit = response.balance.unit
            lines = [
                "Balance",
                "",
                f"Exchange: {exchange}",
                f"Account: {account}",
                "",
                f"Balance: {value} {unit}",
            ]
            summary = getattr(response, "portfolio_summary", None)
            if summary is not None:
                summary_unit = str(getattr(summary, "unit", unit) or unit)
                lines.extend(
                    [
                        "",
                        f"Account Value: {_display_or_dash(getattr(summary, 'account_value', None))} {summary_unit}",
                        f"Withdrawable: {_display_or_dash(getattr(summary, 'withdrawable', None))} {summary_unit}",
                        f"Margin Used: {_display_or_dash(getattr(summary, 'margin_used', None))} {summary_unit}",
                        f"Total Position Value: {_display_or_dash(getattr(summary, 'total_position_value', None))} {summary_unit}",
                    ]
                )
            positions = response.positions or []
            if positions:
                lines.extend(["", "Open Positions", ""])
                for position in positions:
                    lines.append(f"{_direction_emoji(position.side)} {position.symbol}")
                    lines.append(
                        " ".join(
                            [
                                f"Size: {_display_or_dash(position.size)}",
                                f"Entry: {_display_or_dash(position.entry_price)}",
                                f"PnL: {_pnl_format(position.pnl)}",
                                f"TP: {_display_protection(position.tp, getattr(position, 'tp_count', None))}",
                                f"SL: {_display_protection(position.sl, getattr(position, 'sl_count', None))}",
                            ]
                        )
                    )
                    lines.append("")
            text = "\n".join(lines).rstrip() + "\n"
        else:
            err_code = response.error.code if response.error else "BALANCE_UNAVAILABLE"
            err_msg = response.error.message if response.error else "Balance unavailable."
            reason_line = None
            if response.error is not None:
                reason_text = getattr(response.error, "exchange_reason", None)
                if isinstance(reason_text, str) and reason_text.strip():
                    reason_line = f"Reason: {reason_text}"
            lines = [
                "Balance",
                "",
                f"Exchange: {response.exchange}",
                f"Account: {response.account}",
                "",
                f"Error: {err_msg}",
            ]
            if reason_line is not None:
                lines.append(reason_line)
            lines.append(f"({err_code})")
            text = "\n".join(lines)
        return Screen(
            text=text,
            buttons=[
                [_button_row(*BUTTON_REFRESH)],
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="balance",
        )

    def _handle_balance(
        self,
        chat_key: Tuple[Any, ...],
        suffix: str,
    ) -> Screen:
        if suffix == "refresh":
            return self._render_balance(chat_key, refresh=True)
        if suffix == "back":
            state = self._state_for(chat_key)
            state.account = None
            return self._render_select_account(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        return self._render_balance(chat_key, refresh=False)

    # -- summative report (all exchanges / accounts) --------------------

    def _render_summative_report(
        self,
        chat_key: Tuple[Any, ...],
        refresh: bool,
    ) -> Screen:
        """Cross-exchange snapshot: summed balances + all open positions.

        Read-only. Walks every discovered exchange/account via the desk's
        canonical ``balance`` and ``positions_orders`` ops. Failures on a
        single account are listed without aborting the rest.
        """
        del refresh  # always live-fetch; kept for Refresh button parity
        state = self._state_for(chat_key)
        state.state = "summative_report"
        # Report is venue-agnostic; clear single-venue selection so Back
        # returns cleanly to the exchange picker.
        state.exchange = None
        state.account = None

        balance_by_unit: Dict[str, Decimal] = {}
        account_value_by_unit: Dict[str, Decimal] = {}
        per_account_lines: List[str] = []
        position_lines: List[str] = []
        position_net: Dict[Tuple[str, str], Decimal] = {}  # (symbol, side) -> size
        pnl_total = Decimal("0")
        pnl_any = False
        errors: List[str] = []
        accounts_ok = 0
        accounts_seen = 0

        exchanges = list(self._desk.list_exchanges() or [])
        for exchange in exchanges:
            try:
                accounts = list(self._desk.list_accounts(exchange) or [])
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{exchange}: list_accounts failed ({exc})")
                continue
            for entry in accounts:
                alias, label = _account_option_parts(entry)
                if not alias:
                    continue
                accounts_seen += 1
                display_name = label or alias
                bal_value: Optional[Decimal] = None
                bal_unit = "USDT"
                acct_value: Optional[Decimal] = None

                try:
                    bal_resp: CanonicalResponse = self._desk.execute(
                        {
                            "operation": "balance",
                            "exchange": exchange,
                            "account": alias,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{exchange}/{display_name}: balance error ({exc})")
                    bal_resp = None  # type: ignore[assignment]

                if bal_resp is not None and bal_resp.success and bal_resp.balance is not None:
                    bal_value = _wizard_decimal(bal_resp.balance.value)
                    bal_unit = str(bal_resp.balance.unit or "USDT").strip() or "USDT"
                    if bal_value is not None:
                        balance_by_unit[bal_unit] = balance_by_unit.get(bal_unit, Decimal("0")) + bal_value
                    summary = getattr(bal_resp, "portfolio_summary", None)
                    if summary is not None:
                        acct_value = _wizard_decimal(getattr(summary, "account_value", None))
                        sum_unit = str(getattr(summary, "unit", None) or bal_unit).strip() or bal_unit
                        if acct_value is not None:
                            account_value_by_unit[sum_unit] = (
                                account_value_by_unit.get(sum_unit, Decimal("0")) + acct_value
                            )
                    accounts_ok += 1
                elif bal_resp is not None and not bal_resp.success:
                    err = bal_resp.error
                    msg = getattr(err, "message", None) or "balance unavailable"
                    errors.append(f"{exchange}/{display_name}: {msg}")

                try:
                    pos_resp: CanonicalResponse = self._desk.execute(
                        {
                            "operation": "positions_orders",
                            "exchange": exchange,
                            "account": alias,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{exchange}/{display_name}: positions error ({exc})")
                    pos_resp = None  # type: ignore[assignment]

                positions = []
                if pos_resp is not None and pos_resp.success:
                    positions = list(pos_resp.positions or [])
                elif pos_resp is not None and not pos_resp.success:
                    err = pos_resp.error
                    msg = getattr(err, "message", None) or "positions unavailable"
                    # Avoid duplicate noise if balance already failed hard.
                    errors.append(f"{exchange}/{display_name}: positions {msg}")

                bal_txt = (
                    f"{_format_money(bal_value)} {bal_unit}"
                    if bal_value is not None
                    else "—"
                )
                if acct_value is not None:
                    bal_txt += f" (AV {_format_money(acct_value)})"
                per_account_lines.append(f"• {exchange} / {display_name}: {bal_txt}")

                for position in positions:
                    symbol = str(getattr(position, "symbol", "") or "").strip() or "?"
                    side = str(getattr(position, "side", "") or "").strip().lower() or "?"
                    size_d = _wizard_decimal(getattr(position, "size", None))
                    pnl_d = _wizard_decimal(getattr(position, "pnl", None))
                    if size_d is not None and size_d != 0:
                        key = (symbol.upper(), side)
                        position_net[key] = position_net.get(key, Decimal("0")) + size_d
                    if pnl_d is not None:
                        pnl_total += pnl_d
                        pnl_any = True
                    position_lines.append(
                        " ".join(
                            [
                                f"{_direction_emoji(side)} {exchange}/{display_name}",
                                symbol,
                                f"sz {_display_or_dash(getattr(position, 'size', None))}",
                                f"@ {_display_or_dash(getattr(position, 'entry_price', None))}",
                                f"PnL {_pnl_format(getattr(position, 'pnl', None))}",
                            ]
                        )
                    )

        lines: List[str] = [
            "📊 Summative Report",
            "",
            f"Accounts scanned: {accounts_ok}/{accounts_seen}"
            if accounts_seen
            else "Accounts scanned: 0",
            "",
            "Balances (sum by unit)",
        ]
        if balance_by_unit:
            for unit in sorted(balance_by_unit.keys()):
                lines.append(f"• {unit}: {_format_money(balance_by_unit[unit])}")
        else:
            lines.append("• No balances available.")

        if account_value_by_unit:
            lines.append("")
            lines.append("Account value (sum by unit)")
            for unit in sorted(account_value_by_unit.keys()):
                lines.append(f"• {unit}: {_format_money(account_value_by_unit[unit])}")

        lines.extend(["", "Per account"])
        if per_account_lines:
            lines.extend(per_account_lines)
        else:
            lines.append("• No configured accounts.")

        lines.extend(["", "Open positions (all venues)"])
        if position_lines:
            lines.extend(position_lines)
            if position_net:
                lines.extend(["", "Position size net by symbol/side"])
                for (symbol, side), size in sorted(position_net.items()):
                    lines.append(
                        f"{_direction_emoji(side)} {symbol} {side}: {_display_or_dash(format(size, 'f'))}"
                    )
            if pnl_any:
                lines.append("")
                lines.append(f"Total unrealized PnL (reported): {_pnl_format(pnl_total)}")
        else:
            lines.append("No open positions.")

        if errors:
            lines.extend(["", "Issues"])
            # Cap error noise for Telegram length.
            for err_line in errors[:12]:
                lines.append(f"• {err_line}")
            if len(errors) > 12:
                lines.append(f"• …and {len(errors) - 12} more")

        text = "\n".join(lines).rstrip()
        # Telegram hard limit ~4096; keep a safety margin for markdown/wrapping.
        if len(text) > 3800:
            text = text[:3750].rstrip() + "\n\n…truncated. Refresh or open per-exchange views for detail."

        return Screen(
            text=text,
            buttons=[
                [_button_row(*BUTTON_REFRESH)],
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_EXIT)],
            ],
            state="summative_report",
        )

    def _handle_summative_report(
        self,
        chat_key: Tuple[Any, ...],
        suffix: str,
    ) -> Screen:
        if suffix == "refresh":
            return self._render_summative_report(chat_key, refresh=True)
        if suffix == "back":
            return self._render_select_exchange(chat_key)
        if suffix == "exit":
            self.reset(chat_key)
            return Screen(text="Trade closed.", buttons=[], state="closed")
        return self._render_summative_report(chat_key, refresh=False)


# ---------------------------------------------------------------------------
# Direct-dispatch helpers — invoked by the Telegram adapter
# ---------------------------------------------------------------------------
#
# The adapter imports these two functions and calls them directly
# from its own _handle_command and _handle_callback_query paths.
# The adapter does not use a plugin-handler registry for /trade;
# the trade: callback prefix is recognized explicitly by the adapter.
#
# These helpers translate the adapter's "live object" view of a
# message / query into the wizard's chat-key abstraction, and
# dispatch the result back through the adapter's send_inline_keyboard
# helper.

_WIZARD = TradeWizard()


def _chat_key_from_message(msg: Any) -> Tuple[Any, ...]:
    """Build a stable chat key from a python-telegram-bot Message.

    Includes the thread_id so forum-topic and DM-topic users get
    separate wizard state.
    """
    if msg is None:
        return ("unknown",)
    chat = getattr(msg, "chat", None)
    chat_id = getattr(chat, "id", None) if chat is not None else None
    thread_id = getattr(msg, "message_thread_id", None)
    if chat_id is None:
        return ("unknown",)
    return (str(chat_id), thread_id) if thread_id is not None else (str(chat_id),)


def _metadata_from_message(msg: Any) -> Optional[Dict[str, Any]]:
    """Build an adapter metadata dict from a Message (for thread routing)."""
    if msg is None:
        return None
    thread_id = getattr(msg, "message_thread_id", None)
    if thread_id is None:
        return None
    return {"thread_id": thread_id}


async def handle_trade_command(adapter: Any, msg: Any) -> bool:
    """Open the trade wizard for the chat that issued /trade.

    Called by the Telegram adapter's _handle_command path BEFORE
    generic command dispatch. Returns True if the message was
    consumed by the wizard, False if it wasn't a /trade invocation
    (in which case the adapter should continue with normal
    dispatch). Wraps the wizard logic in try/except so a wizard
    bug does not break the entire adapter.
    """
    try:
        text = (getattr(msg, "text", "") or "").strip()
        if not text.startswith("/"):
            return False
        first = text.split(None, 1)[0]
        cmd_name = first.lstrip("/").split("@", 1)[0].lower()
        if cmd_name != "trade":
            return False
        chat_key = _chat_key_from_message(msg)
        metadata = _metadata_from_message(msg)
        screen = _WIZARD.open(chat_key)
        chat_id = _chat_id_from_message(msg)
        if chat_id is None:
            logger.warning("trade wizard: cannot determine chat_id; skipping")
            return True  # consumed (don't fall through), but didn't render
        await _send_screen(adapter, chat_id, screen, metadata=metadata)
        logger.info(
            "trade wizard: /trade opened chat_key=%s",
            chat_key,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("trade wizard: /trade dispatch failed: %s", exc, exc_info=True)
        return False


async def handle_trade_callback(adapter: Any, query: Any, data: str) -> None:
    """Handle a ``trade:`` prefixed callback query.

    The adapter has already routed the call here because
    ``data.startswith("trade:")``. This function strips the
    ``trade:`` prefix, runs the wizard one step, edits the
    originating message in place to the next screen, and
    acknowledges the query.

    On any failure, the query is still acknowledged (so the user
    doesn't see a stuck "loading\u2026" indicator) and the failure
    is logged. The wizard's state is left untouched.
    """
    try:
        # data still carries the "trade:" prefix here — the adapter
        # passes the raw query.data and we strip it ourselves so
        # the contract is explicit at the boundary.
        suffix = data
        if suffix.startswith("trade:"):
            suffix = suffix[len("trade:"):]
        query_message = getattr(query, "message", None)
        chat_key = _chat_key_from_message(query_message)
        screen = _WIZARD.handle_callback(chat_key, suffix)
        # Build the inline keyboard for the new screen.
        from plugins.platforms.telegram.adapter import (
            InlineKeyboardButton,
            InlineKeyboardMarkup,
        )
        rows = []
        for row in screen.buttons:
            button_row = []
            for btn in row:
                if not isinstance(btn, dict):
                    continue
                label = str(btn.get("text", ""))
                suffix_data = str(btn.get("callback_data", ""))
                if not label or not suffix_data:
                    continue
                button_row.append(
                    InlineKeyboardButton(
                        label,
                        callback_data=f"trade:{suffix_data}",
                    )
                )
            if button_row:
                rows.append(button_row)
        keyboard = InlineKeyboardMarkup(rows) if rows else None
        try:
            await query.edit_message_text(
                text=screen.text,
                reply_markup=keyboard,
            )
        except Exception:
            # Edit failed (e.g. message too old). Fall back to a
            # fresh send so the user still sees the next screen.
            chat_id = _chat_id_from_message(query_message)
            if chat_id is not None:
                metadata = _metadata_from_message(query_message)
                await _send_screen(adapter, str(chat_id), screen, metadata=metadata)
        try:
            await query.answer()
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "trade wizard: callback dispatch failed: %s", exc, exc_info=True,
        )
        try:
            await query.answer()
        except Exception:
            pass


async def handle_trade_text(adapter: Any, msg: Any) -> bool:
    """Handle free-text input for wizard states that explicitly await it."""
    try:
        chat_key = _chat_key_from_message(msg)
        text = getattr(msg, "text", "") or ""
        screen = _WIZARD.handle_text(chat_key, text)
        if screen is None:
            return False
        chat_id = _chat_id_from_message(msg)
        if chat_id is None:
            logger.warning("trade wizard: cannot determine chat_id for text input")
            return True
        metadata = _metadata_from_message(msg)
        await _send_screen(adapter, str(chat_id), screen, metadata=metadata)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("trade wizard: text dispatch failed: %s", exc, exc_info=True)
        return False


def _chat_id_from_message(msg: Any) -> Optional[str]:
    """Extract a string chat_id from a python-telegram-bot Message."""
    if msg is None:
        return None
    chat = getattr(msg, "chat", None)
    if chat is None:
        return None
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return None
    return str(chat_id)


async def _send_screen(
    adapter: Any,
    chat_id: str,
    screen: Any,
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Send a wizard screen via the supplied telegram adapter.

    Uses the adapter's plugin-facing ``send_inline_keyboard`` helper.
    The helper does the transport; the wizard owns the screen
    layout and the ``trade:`` prefix.
    """
    send = getattr(adapter, "send_inline_keyboard", None)
    if not callable(send):
        # Fall back to plain text. The wizard still completes; the
        # user sees the screen text but no buttons. This is a
        # degraded path for adapters that don't yet support plugin
        # inline keyboards.
        fallback = getattr(adapter, "send", None)
        if callable(fallback):
            try:
                await fallback(chat_id, screen.text, metadata=metadata)
            except Exception as send_err:  # noqa: BLE001
                logger.warning("Plain-text fallback send failed: %s", send_err)
        return
    try:
        await send(
            chat_id=chat_id,
            text=screen.text,
            buttons=screen.buttons,
            callback_prefix="trade",
            metadata=metadata,
        )
    except Exception as send_err:  # noqa: BLE001
        logger.warning("send_inline_keyboard failed: %s", send_err)


__all__ = [
    "TradeWizard",
    "Screen",
    "WizardState",
    "BUTTON_BACK",
    "BUTTON_EXIT",
    "BUTTON_REFRESH",
    "handle_trade_command",
    "handle_trade_callback",
    "handle_trade_text",
]
