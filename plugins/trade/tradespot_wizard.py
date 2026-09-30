"""Telegram /tradespot wizard for SPOT trading.

This wizard is intentionally separate from the existing /trade wizard:

* separate SpotDesk discovery (``x_<exchange>_agent_spot.py`` only);
* separate callback namespace (``tradespot:``);
* separate in-memory state.

Phase 1 is navigation/read-only oriented. Mutating actions are not executed
from this wizard unless a later spot-agent integration explicitly implements
and wires the corresponding flow.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, cast

from .canonical import CanonicalResponse
from .spotdesk import SpotDesk, get_spotdesk
from .wizard import _account_option_parts, _button_row, _render_error_lines

logger = logging.getLogger(__name__)

BUTTON_CLOSE = ("❌ Close", "close")
BUTTON_BACK = ("⬅️ Back", "back")
BUTTON_REFRESH = ("🔄 Refresh", "refresh")
BUTTON_CHANGE_ACCOUNT = ("🔄 Change Account", "change_account")
BUTTON_CHANGE_EXCHANGE = ("🔄 Change Exchange", "change_exchange")

_SPOT_ACTIONS: tuple[tuple[str, str, str], ...] = (
    ("balance", "💰 Balance", "balance"),
    ("orders", "📋 Orders", "orders"),
    ("positions_orders", "📋 Orders", "orders"),
    ("new_order", "➕ New Order", "new_order"),
    ("ladder", "🪜 Ladder", "ladder"),
    ("cancel_orders", "❌ Cancel Orders", "cancel_orders"),
)
_READ_ONLY_ORDER_CAPS = {"orders", "positions_orders"}
_MUTATING_ACTIONS = {"new_order", "ladder", "cancel_orders"}


@dataclass(frozen=True)
class Screen:
    text: str
    buttons: List[List[Dict[str, str]]]
    state: str


@dataclass
class SpotWizardState:
    state: str = "select_exchange"
    exchange: Optional[str] = None
    account: Optional[str] = None


class TradeSpotWizard:
    """Small SPOT-specific Telegram state machine."""

    def __init__(self, spotdesk: Optional[SpotDesk] = None) -> None:
        self._desk = spotdesk or get_spotdesk()
        self._states: Dict[Tuple[Any, ...], SpotWizardState] = {}

    def _state_for(self, chat_key: Tuple[Any, ...]) -> SpotWizardState:
        state = self._states.get(chat_key)
        if state is None:
            state = SpotWizardState()
            self._states[chat_key] = state
        return state

    def reset(self, chat_key: Tuple[Any, ...]) -> None:
        self._states.pop(chat_key, None)

    def open(self, chat_key: Tuple[Any, ...]) -> Screen:
        self.reset(chat_key)
        return self._render_select_exchange(chat_key)

    def handle_text(self, chat_key: Tuple[Any, ...], text: str) -> Optional[Screen]:
        del chat_key, text
        return None

    def handle_callback(self, chat_key: Tuple[Any, ...], callback_suffix: str) -> Screen:
        suffix = (callback_suffix or "").strip()
        state = self._state_for(chat_key)
        if suffix in {"close", "exit"}:
            self.reset(chat_key)
            return Screen("Spot trading closed.", [], "closed")
        if state.state == "select_exchange":
            return self._handle_select_exchange(chat_key, suffix)
        if state.state == "select_account":
            return self._handle_select_account(chat_key, suffix)
        if state.state == "action":
            return self._handle_action(chat_key, suffix)
        if state.state in {"balance", "orders", "unsupported"}:
            return self._handle_result_screen(chat_key, suffix)
        return self.open(chat_key)

    def _render_select_exchange(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "select_exchange"
        state.exchange = None
        state.account = None
        exchanges = self._desk.list_exchanges()
        if not exchanges:
            return Screen(
                text=(
                    "🟦 Spot Trading\n\n"
                    "No spot exchanges are currently available.\n"
                    "Add a valid x_<exchange>_agent_spot.py module and try again."
                ),
                buttons=[[_button_row(*BUTTON_CLOSE)]],
                state="select_exchange",
            )
        rows = [[_button_row(ex, f"exchange:{ex}")] for ex in exchanges]
        rows.append([_button_row(*BUTTON_CLOSE)])
        return Screen("🟦 Spot Trading\n\nSelect Exchange:", rows, "select_exchange")

    def _handle_select_exchange(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if not suffix.startswith("exchange:"):
            return self._render_select_exchange(chat_key)
        exchange = suffix[len("exchange:") :].strip()
        if not exchange or exchange not in self._desk.list_exchanges():
            return self._render_select_exchange(chat_key)
        state = self._state_for(chat_key)
        state.exchange = exchange
        return self._render_select_account(chat_key)

    def _render_select_account(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "select_account"
        exchange = state.exchange or ""
        accounts = self._desk.list_accounts(exchange) if exchange else []
        if not accounts:
            return Screen(
                text=(
                    "🟦 Spot Trading\n"
                    f"Exchange: {exchange}\n\n"
                    "No accounts are configured for this spot exchange."
                ),
                buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]],
                state="select_account",
            )
        rows: List[List[Dict[str, str]]] = []
        for entry in accounts:
            alias, label = _account_option_parts(entry)
            if alias and label:
                rows.append([_button_row(label, f"account:{alias}")])
        rows.append([_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)])
        return Screen(
            text=(
                "🟦 Spot Trading\n"
                f"Exchange: {exchange}\n\n"
                "Select Account:"
            ),
            buttons=rows,
            state="select_account",
        )

    def _handle_select_account(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self._render_select_exchange(chat_key)
        if not suffix.startswith("account:"):
            return self._render_select_account(chat_key)
        alias = suffix[len("account:") :].strip()
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

    def _supported_action_buttons(self, exchange: str) -> List[tuple[str, str]]:
        caps = set(self._desk.capabilities(exchange) or [])
        rows: List[tuple[str, str]] = []
        seen_callbacks: set[str] = set()
        for cap, label, callback in _SPOT_ACTIONS:
            if cap in caps and callback not in seen_callbacks:
                rows.append((label, f"action:{callback}"))
                seen_callbacks.add(callback)
        return rows

    def _render_action(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "action"
        exchange = state.exchange or ""
        account = state.account or ""
        actions = self._supported_action_buttons(exchange)
        rows: List[List[Dict[str, str]]] = []
        for idx in range(0, len(actions), 2):
            row = [_button_row(*actions[idx])]
            if idx + 1 < len(actions):
                row.append(_button_row(*actions[idx + 1]))
            rows.append(row)
        rows.append([_button_row(*BUTTON_CHANGE_ACCOUNT)])
        rows.append([_button_row(*BUTTON_CHANGE_EXCHANGE)])
        rows.append([_button_row(*BUTTON_CLOSE)])
        body = (
            "🟦 Spot Trading\n"
            f"Exchange: {exchange}\n"
            f"Account: {account}\n"
        )
        if not actions:
            body += "\nNo supported spot actions are advertised by this agent."
        return Screen(body, rows, "action")

    def _handle_action(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back" or suffix == "change_account":
            state.account = None
            return self._render_select_account(chat_key)
        if suffix == "change_exchange":
            return self._render_select_exchange(chat_key)
        if not suffix.startswith("action:"):
            return self._render_action(chat_key)
        action = suffix[len("action:") :].strip()
        exchange = state.exchange or ""
        caps = set(self._desk.capabilities(exchange) or [])
        if action == "balance" and "balance" in caps:
            return self._render_balance(chat_key)
        if action == "orders" and caps & _READ_ONLY_ORDER_CAPS:
            return self._render_orders(chat_key)
        if action in _MUTATING_ACTIONS and action in caps:
            return self._render_mutating_not_enabled(chat_key, action)
        return self._render_action(chat_key)

    def _result_buttons(self) -> List[List[Dict[str, str]]]:
        return [[_button_row(*BUTTON_REFRESH)], [_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]]

    def _render_balance(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "balance"
        exchange = state.exchange or ""
        account = state.account or ""
        response = self._desk.execute({"operation": "balance", "exchange": exchange, "account": account})
        if response.success and response.balance is not None:
            lines = [
                "🟦 Spot Trading",
                "💰 Balance",
                "",
                f"Exchange: {exchange}",
                f"Account: {account}",
                "",
                f"Balance: {response.balance.value} {response.balance.unit}",
            ]
            data = getattr(response, "data", None)
            if isinstance(data, dict):
                assets = data.get("assets")
                if isinstance(assets, list) and assets:
                    lines.extend(["", "Assets"])
                    for item in assets[:20]:
                        if isinstance(item, dict):
                            symbol = str(item.get("asset") or item.get("symbol") or "").strip()
                            amount = str(item.get("amount") or item.get("balance") or "").strip()
                            if symbol and amount:
                                lines.append(f"• {symbol}: {amount}")
        else:
            lines = ["🟦 Spot Trading", "💰 Balance"]
            lines.extend(_render_error_lines(response.error, "Balance unavailable."))
        return Screen("\n".join(lines).rstrip(), self._result_buttons(), "balance")

    def _render_orders(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "orders"
        exchange = state.exchange or ""
        account = state.account or ""
        caps = set(self._desk.capabilities(exchange) or [])
        operation = "orders" if "orders" in caps else "positions_orders"
        response: CanonicalResponse = self._desk.execute({"operation": operation, "exchange": exchange, "account": account})
        lines = ["🟦 Spot Trading", "📋 Orders", "", f"Exchange: {exchange}", f"Account: {account}"]
        if response.success:
            groups = list(response.order_groups or [])
            count = response.open_order_count if response.open_order_count is not None else len(groups)
            lines.extend(["", f"Open orders: {count}"])
            for group in groups[:20]:
                symbol = str(getattr(group, "symbol", "") or "?")
                side = str(getattr(group, "side", "") or "?")
                order_count = getattr(group, "order_count", "?")
                total_size = getattr(group, "total_size", "")
                lines.append(f"• {symbol} {side}: {order_count} orders {total_size}".rstrip())
            data = getattr(response, "data", None)
            raw_orders = data.get("orders") if isinstance(data, dict) else None
            if isinstance(raw_orders, list) and not groups:
                for item in raw_orders[:20]:
                    lines.append(f"• {item}")
        else:
            lines.extend(_render_error_lines(response.error, "Orders unavailable."))
        return Screen("\n".join(lines).rstrip(), self._result_buttons(), "orders")

    def _render_mutating_not_enabled(self, chat_key: Tuple[Any, ...], action: str) -> Screen:
        state = self._state_for(chat_key)
        state.state = "unsupported"
        label = {
            "new_order": "New Order",
            "ladder": "Ladder",
            "cancel_orders": "Cancel Orders",
        }.get(action, action)
        return Screen(
            text=(
                "🟦 Spot Trading\n"
                f"{label}\n\n"
                "This spot action is advertised by the agent, but Telegram "
                "execution is not enabled in the /tradespot phase-1 wizard."
            ),
            buttons=[[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]],
            state="unsupported",
        )

    def _handle_result_screen(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self._render_action(chat_key)
        if suffix == "refresh" and state.state == "balance":
            return self._render_balance(chat_key)
        if suffix == "refresh" and state.state == "orders":
            return self._render_orders(chat_key)
        return self._render_action(chat_key)


_WIZARD = TradeSpotWizard()


def _chat_key_from_message(msg: Any) -> Tuple[Any, ...]:
    if msg is None:
        return ("unknown",)
    chat = getattr(msg, "chat", None)
    chat_id = getattr(chat, "id", None) if chat is not None else None
    thread_id = getattr(msg, "message_thread_id", None)
    if chat_id is None:
        return ("unknown",)
    return (str(chat_id), thread_id) if thread_id is not None else (str(chat_id),)


def _metadata_from_message(msg: Any) -> Optional[Dict[str, Any]]:
    if msg is None:
        return None
    thread_id = getattr(msg, "message_thread_id", None)
    if thread_id is None:
        return None
    return {"thread_id": thread_id}


def _chat_id_from_message(msg: Any) -> Optional[str]:
    chat = getattr(msg, "chat", None) if msg is not None else None
    chat_id = getattr(chat, "id", None) if chat is not None else None
    return str(chat_id) if chat_id is not None else None


async def _send_screen(adapter: Any, chat_id: str, screen: Screen, *, metadata: Optional[Dict[str, Any]] = None) -> None:
    send = getattr(adapter, "send_inline_keyboard", None)
    if callable(send):
        await cast(Any, send)(
            chat_id=chat_id,
            text=screen.text,
            buttons=screen.buttons,
            callback_prefix="tradespot",
            metadata=metadata,
        )
        return
    fallback = getattr(adapter, "send", None)
    if callable(fallback):
        await cast(Any, fallback)(chat_id, screen.text, metadata=metadata)


async def handle_tradespot_command(adapter: Any, msg: Any) -> bool:
    try:
        text = (getattr(msg, "text", "") or "").strip()
        if not text.startswith("/"):
            return False
        first = text.split(None, 1)[0]
        cmd_name = first.lstrip("/").split("@", 1)[0].lower()
        if cmd_name != "tradespot":
            return False
        chat_key = _chat_key_from_message(msg)
        chat_id = _chat_id_from_message(msg)
        if chat_id is None:
            logger.warning("tradespot wizard: cannot determine chat_id; skipping")
            return True
        screen = _WIZARD.open(chat_key)
        await _send_screen(adapter, chat_id, screen, metadata=_metadata_from_message(msg))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("tradespot wizard: command dispatch failed: %s", exc, exc_info=True)
        return False


async def handle_tradespot_callback(adapter: Any, query: Any, data: str) -> None:
    try:
        suffix = data[len("tradespot:") :] if data.startswith("tradespot:") else data
        query_message = getattr(query, "message", None)
        chat_key = _chat_key_from_message(query_message)
        try:
            await query.answer()
        except Exception:
            pass
        screen = await asyncio.to_thread(_WIZARD.handle_callback, chat_key, suffix)
        from plugins.platforms.telegram.adapter import (  # type: ignore[import-not-found]
            InlineKeyboardButton,
            InlineKeyboardMarkup,
        )

        rows = []
        for row in screen.buttons:
            button_row = []
            for btn in row:
                label = str(btn.get("text", ""))
                suffix_data = str(btn.get("callback_data", ""))
                if label and suffix_data:
                    button_row.append(InlineKeyboardButton(label, callback_data=f"tradespot:{suffix_data}"))
            if button_row:
                rows.append(button_row)
        keyboard = InlineKeyboardMarkup(rows) if rows else None
        try:
            await query.edit_message_text(text=screen.text, reply_markup=keyboard)
        except Exception:
            chat_id = _chat_id_from_message(query_message)
            if chat_id is not None:
                await _send_screen(adapter, chat_id, screen, metadata=_metadata_from_message(query_message))
    except Exception as exc:  # noqa: BLE001
        logger.error("tradespot wizard: callback dispatch failed: %s", exc, exc_info=True)
        try:
            await query.answer()
        except Exception:
            pass


async def handle_tradespot_text(adapter: Any, msg: Any) -> bool:
    try:
        screen = await asyncio.to_thread(_WIZARD.handle_text, _chat_key_from_message(msg), getattr(msg, "text", "") or "")
        if screen is None:
            return False
        chat_id = _chat_id_from_message(msg)
        if chat_id is None:
            return True
        await _send_screen(adapter, chat_id, screen, metadata=_metadata_from_message(msg))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("tradespot wizard: text dispatch failed: %s", exc, exc_info=True)
        return False


__all__ = [
    "TradeSpotWizard",
    "Screen",
    "SpotWizardState",
    "handle_tradespot_command",
    "handle_tradespot_callback",
    "handle_tradespot_text",
]
