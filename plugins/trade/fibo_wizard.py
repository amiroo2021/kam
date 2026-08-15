"""Telegram /fibo wizard/controller.

This module is UI only. It sends standardized commands to the persistent
Fibo service and renders responses. It does not discover Linux processes,
launch shell commands, or implement strategy/exchange logic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple
from urllib.parse import quote, unquote

from .tradedesk import TradeDesk, get_tradedesk
from .fibo_service import FiboServiceProtocol, get_fibo_service

logger = logging.getLogger(__name__)

BUTTON_EXIT = ("✕ Exit", "exit")
BUTTON_BACK = ("◀️ Back", "back")
BUTTON_REFRESH = ("🔄 Refresh", "refresh")
BUTTON_START = ("▶️ Start Fibo", "menu:start")
BUTTON_RUNNING = ("🔵 Running Fibo", "menu:running")
BUTTON_STOP = ("🛑 STOP Fibo", "menu:stop")
BUTTON_CONFIRM_START = ("▶️ Start Fibo", "confirm_start")
BUTTON_CONFIRM_STOP_CLOSE = ("🛑 STOP & CLOSE", "confirm_stop_close")
BUTTON_CANCEL = ("Cancel", "back")
BUTTON_SYMBOL_BTC = ("BTC", "symbol:BTC")
BUTTON_SYMBOL_ETH = ("ETH", "symbol:ETH")
BUTTON_SYMBOL_XAU = ("XAU", "symbol:XAU")
BUTTON_SYMBOL_ONDO = ("ONDO", "symbol:ONDO")
BUTTON_OTHER_SYMBOL = ("Other...", "symbol:other")
BUTTON_COUNTER_BUY = ("Counter BUY", "counter_side:counterBUY")
BUTTON_COUNTER_SELL = ("Counter SELL", "counter_side:counterSELL")

_DEFAULT_DIVIDE_PERCENT = 100.0
_DEFAULT_COUNTER1 = 1.3
_DEFAULT_COUNTER2 = 0.8
_DEFAULT_COUNTER3 = 0.5
_DEFAULT_COUNTER4 = 0.3
_DEFAULT_POLL_SECONDS = 2.0


@dataclass(frozen=True)
class Screen:
    text: str
    buttons: List[List[Dict[str, str]]]
    state: str


@dataclass
class WizardState:
    state: str = "main_menu"
    exchange: Optional[str] = None
    account: Optional[str] = None
    instrument: Optional[str] = None
    counter_side: Optional[str] = None
    registration_key: Optional[str] = None
    start_params: Dict[str, Any] = field(default_factory=dict)


class TelegramAdapter(Protocol):
    async def send_inline_keyboard(self, *, chat_id, text, buttons, callback_prefix, metadata=None): ...


class FiboWizard:
    CALLBACK_SEP = ":"

    def __init__(self, tradedesk: Optional[TradeDesk] = None, service: Optional[FiboServiceProtocol] = None) -> None:
        self._desk = tradedesk or get_tradedesk()
        self._service = service or get_fibo_service()
        self._states: Dict[Tuple[Any, ...], WizardState] = {}

    def reset(self, chat_key: Tuple[Any, ...]) -> None:
        self._states.pop(chat_key, None)

    def open(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "main_menu"
        return Screen(
            text="🧬 Fibo Robot",
            buttons=[
                [_button(*BUTTON_START)],
                [_button(*BUTTON_RUNNING)],
                [_button(*BUTTON_STOP)],
                [_button(*BUTTON_EXIT)],
            ],
            state=state.state,
        )

    def handle_callback(self, chat_key: Tuple[Any, ...], data: str) -> Screen:
        state = self._state_for(chat_key)
        raw = str(data or "")
        if raw == "exit":
            self.reset(chat_key)
            return Screen(text="Fibo closed.", buttons=[], state="closed")
        if raw == "refresh" and state.state == "running_detail":
            return self._render_running_detail(chat_key)
        if state.state == "main_menu":
            return self._handle_main_menu(chat_key, raw)
        if state.state == "select_exchange":
            return self._handle_select_exchange(chat_key, raw)
        if state.state == "select_account":
            return self._handle_select_account(chat_key, raw)
        if state.state == "select_instrument":
            return self._handle_select_instrument(chat_key, raw)
        if state.state == "awaiting_instrument":
            return self._render_message("Type Instrument", state.state)
        if state.state == "select_counter_side":
            return self._handle_select_counter_side(chat_key, raw)
        if state.state in {
            "awaiting_divide_percent",
            "awaiting_counter1",
            "awaiting_counter2",
            "awaiting_counter3",
            "awaiting_counter4",
        }:
            return self._handle_numeric_callback(chat_key, raw)
        if state.state == "review_start":
            return self._handle_review(chat_key, raw)
        if state.state == "running_list":
            return self._handle_running_list(chat_key, raw)
        if state.state == "running_detail":
            if raw == "back":
                return self._render_running_list(chat_key)
            return self._render_running_detail(chat_key)
        if state.state == "stop_list":
            return self._handle_stop_list(chat_key, raw)
        if state.state == "stop_confirm":
            return self._handle_stop_confirm(chat_key, raw)
        return self.open(chat_key)

    def handle_text(self, chat_key: Tuple[Any, ...], text: str) -> Screen:
        state = self._state_for(chat_key)
        cleaned = str(text or "").strip()
        if state.state == "awaiting_instrument":
            if not cleaned:
                return self._render_message("Invalid instrument.", state.state)
            state.instrument = cleaned.upper()
            return self._render_counter_side(chat_key)
        if state.state in {
            "awaiting_divide_percent",
            "awaiting_counter1",
            "awaiting_counter2",
            "awaiting_counter3",
            "awaiting_counter4",
        }:
            try:
                value = float(cleaned)
            except ValueError:
                return self._render_message("Invalid number. Please enter a numeric value.", state.state)
            if value < 0:
                return self._render_message("Value must be >= 0.", state.state)
            field_name = {
                "awaiting_divide_percent": "divide_percent",
                "awaiting_counter1": "counter1",
                "awaiting_counter2": "counter2",
                "awaiting_counter3": "counter3",
                "awaiting_counter4": "counter4",
            }[state.state]
            state.start_params[field_name] = value
            return self._advance_after_numeric(chat_key, field_name)
        return self.open(chat_key)

    def _handle_main_menu(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "menu:start":
            return self._render_exchange_select(chat_key)
        if suffix == "menu:running":
            return self._render_running_list(chat_key)
        if suffix == "menu:stop":
            return self._render_stop_list(chat_key)
        return self.open(chat_key)

    def _render_exchange_select(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "select_exchange"
        rows = [[_button(self._pretty_exchange(ex), f"exchange:{ex}")] for ex in self._desk.list_exchanges()]
        rows.append([_button(*BUTTON_BACK), _button(*BUTTON_EXIT)])
        return Screen(text="🧬 Start Fibo\n\nSelect Exchange", buttons=rows, state=state.state)

    def _handle_select_exchange(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self.open(chat_key)
        prefix, value = _split_callback(suffix)
        if prefix != "exchange" or not value:
            return self._render_exchange_select(chat_key)
        state = self._state_for(chat_key)
        state.exchange = value
        state.state = "select_account"
        rows = [[_button(str(acc), f"account:{acc}")] for acc in self._desk.list_accounts(value)]
        rows.append([_button(*BUTTON_BACK), _button(*BUTTON_EXIT)])
        return Screen(
            text=f"🧬 Start Fibo\n\nExchange: {self._pretty_exchange(value)}\n\nSelect Account",
            buttons=rows,
            state=state.state,
        )

    def _handle_select_account(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self._render_exchange_select(chat_key)
        prefix, value = _split_callback(suffix)
        if prefix != "account" or not value:
            return self._handle_select_exchange(chat_key, f"exchange:{state.exchange or ''}")
        state.account = value
        return self._render_instrument_select(chat_key)

    def _render_instrument_select(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "select_instrument"
        return Screen(
            text="🧬 Start Fibo\n\nSelect / Enter Instrument",
            buttons=[
                [_button(*BUTTON_SYMBOL_BTC), _button(*BUTTON_SYMBOL_ETH)],
                [_button(*BUTTON_SYMBOL_XAU), _button(*BUTTON_SYMBOL_ONDO)],
                [_button(*BUTTON_OTHER_SYMBOL)],
                [_button(*BUTTON_BACK), _button(*BUTTON_EXIT)],
            ],
            state=state.state,
        )

    def _handle_select_instrument(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self._handle_select_exchange(chat_key, f"exchange:{state.exchange or ''}")
        if suffix == "symbol:other":
            state.state = "awaiting_instrument"
            return Screen(
                text="🧬 Start Fibo\n\nType Instrument",
                buttons=[[_button(*BUTTON_BACK), _button(*BUTTON_EXIT)]],
                state=state.state,
            )
        prefix, value = _split_callback(suffix)
        if prefix != "symbol" or not value:
            return self._render_instrument_select(chat_key)
        state.instrument = value.upper()
        return self._render_counter_side(chat_key)

    def _render_counter_side(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "select_counter_side"
        return Screen(
            text="🧬 Start Fibo\n\nSelect Counter Type",
            buttons=[
                [_button(*BUTTON_COUNTER_BUY)],
                [_button(*BUTTON_COUNTER_SELL)],
                [_button(*BUTTON_BACK), _button(*BUTTON_EXIT)],
            ],
            state=state.state,
        )

    def _handle_select_counter_side(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self._render_instrument_select(chat_key)
        prefix, value = _split_callback(suffix)
        if prefix != "counter_side" or value not in {"counterBUY", "counterSELL"}:
            return self._render_counter_side(chat_key)
        state.counter_side = value
        return self._render_numeric_prompt(chat_key, "divide_percent")

    def _handle_numeric_callback(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._numeric_back(chat_key)
        prefix, value = _split_callback(suffix)
        if prefix != "default":
            return self._render_message("Please enter a value or choose the default button.", self._state_for(chat_key).state)
        defaults = {
            "divide_percent": _DEFAULT_DIVIDE_PERCENT,
            "counter1": _DEFAULT_COUNTER1,
            "counter2": _DEFAULT_COUNTER2,
            "counter3": _DEFAULT_COUNTER3,
            "counter4": _DEFAULT_COUNTER4,
        }
        if value not in defaults:
            return self._render_message("Unknown default.", self._state_for(chat_key).state)
        state = self._state_for(chat_key)
        state.start_params[value] = defaults[value]
        return self._advance_after_numeric(chat_key, value)

    def _advance_after_numeric(self, chat_key: Tuple[Any, ...], field_name: str) -> Screen:
        order = ["divide_percent", "counter1", "counter2", "counter3", "counter4"]
        idx = order.index(field_name)
        if idx == len(order) - 1:
            return self._render_start_review(chat_key)
        return self._render_numeric_prompt(chat_key, order[idx + 1])

    def _numeric_back(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        order = ["awaiting_divide_percent", "awaiting_counter1", "awaiting_counter2", "awaiting_counter3", "awaiting_counter4"]
        current = state.state
        idx = order.index(current)
        if idx == 0:
            return self._render_counter_side(chat_key)
        field = ["divide_percent", "counter1", "counter2", "counter3", "counter4"][idx - 1]
        return self._render_numeric_prompt(chat_key, field)

    def _render_numeric_prompt(self, chat_key: Tuple[Any, ...], field_name: str) -> Screen:
        state = self._state_for(chat_key)
        prompts = {
            "divide_percent": ("awaiting_divide_percent", "Divide Percent?", "Default: 100"),
            "counter1": ("awaiting_counter1", "Counter 1 Volume?", "Default: 1.3"),
            "counter2": ("awaiting_counter2", "Counter 2 Volume?", "Default: 0.8"),
            "counter3": ("awaiting_counter3", "Counter 3 Volume?", "Default: 0.5"),
            "counter4": ("awaiting_counter4", "Counter 4 Volume?", "Default: 0.3"),
        }
        state_name, prompt, default_label = prompts[field_name]
        state.state = state_name
        return Screen(
            text=f"🧬 Start Fibo\n\n{prompt}",
            buttons=[
                [_button(default_label, f"default:{field_name}")],
                [_button(*BUTTON_BACK), _button(*BUTTON_EXIT)],
            ],
            state=state.state,
        )

    def _render_start_review(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "review_start"
        p = state.start_params
        text = (
            "🧬 Start Fibo\n\n"
            f"Exchange: {self._pretty_exchange(state.exchange)}\n"
            f"Account: {state.account}\n"
            f"Instrument: {state.instrument}\n"
            f"Counter Type: {state.counter_side}\n\n"
            f"Divide Percent: {self._format_num(p.get('divide_percent'))}\n\n"
            f"C1: {self._format_num(p.get('counter1'))}\n"
            f"C2: {self._format_num(p.get('counter2'))}\n"
            f"C3: {self._format_num(p.get('counter3'))}\n"
            f"C4: {self._format_num(p.get('counter4'))}"
        )
        return Screen(
            text=text,
            buttons=[
                [_button(*BUTTON_CONFIRM_START)],
                [_button(*BUTTON_CANCEL), _button(*BUTTON_EXIT)],
            ],
            state=state.state,
        )

    def _handle_review(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_numeric_prompt(chat_key, "counter4")
        if suffix != "confirm_start":
            return self._render_start_review(chat_key)
        state = self._state_for(chat_key)
        command = {
            "op": "start",
            "exchange": state.exchange,
            "account": state.account,
            "instrument": state.instrument,
            "counter_side": state.counter_side,
            "divide_percent": float(state.start_params["divide_percent"]),
            "counter1": float(state.start_params["counter1"]),
            "counter2": float(state.start_params["counter2"]),
            "counter3": float(state.start_params["counter3"]),
            "counter4": float(state.start_params["counter4"]),
            "poll_seconds": _DEFAULT_POLL_SECONDS,
        }
        result = self._service.execute_command(command)
        if not result.get("ok"):
            message = str(result.get("message") or result.get("error") or "Failed to start Fibo")
            return Screen(
                text=f"Start failed.\n\n{message}",
                buttons=[[_button(*BUTTON_START)], [_button(*BUTTON_EXIT)]],
                state="main_menu",
            )
        state.registration_key = str(result.get("registration_key") or "")
        return Screen(
            text=f"Fibo started.\n\nRegistration: {state.registration_key}",
            buttons=[[_button(*BUTTON_RUNNING)], [_button(*BUTTON_EXIT)]],
            state="main_menu",
        )

    def _render_running_list(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "running_list"
        response = self._service.execute_command({"op": "list"})
        regs = list(response.get("registrations") or [])
        lines = ["🔵 Running Fibo", ""]
        rows: List[List[Dict[str, str]]] = []
        if not regs:
            lines.append("No running registrations.")
        for reg in regs:
            label = self._registration_label(reg)
            lines.append(label)
            rows.append([_button(label, f"registration:{quote(str(reg['registration_key']), safe='')}")])
        rows.append([_button(*BUTTON_BACK), _button(*BUTTON_EXIT)])
        return Screen(text="\n".join(lines), buttons=rows, state=state.state)

    def _handle_running_list(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self.open(chat_key)
        prefix, value = _split_callback(suffix)
        if prefix != "registration" or not value:
            return self._render_running_list(chat_key)
        state = self._state_for(chat_key)
        state.registration_key = unquote(value)
        return self._render_running_detail(chat_key)

    def _render_running_detail(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "running_detail"
        detail_response = self._service.execute_command({"op": "detail", "registration_key": state.registration_key})
        detail = dict(detail_response.get("detail") or {})
        text = (
            f"Status: {detail.get('status')}\n"
            f"Exchange: {self._pretty_exchange(detail.get('exchange'))}\n"
            f"Account: {detail.get('account')}\n"
            f"Instrument: {detail.get('instrument')}\n"
            f"Counter Side: {self._pretty_counter_side(detail.get('counter_side'))}\n"
            f"Current Mark Price: {self._format_num(detail.get('current_mark_price'))}\n"
            f"Step0: {self._format_num(detail.get('step0'))}\n"
            f"Step0TP: {self._format_num(detail.get('step0tp'))}\n"
            f"Step1: {self._format_num(detail.get('step1'))}\n"
            f"Step2: {self._format_num(detail.get('step2'))}\n"
            f"Step3: {self._format_num(detail.get('step3'))}\n"
            f"Step4: {self._format_num(detail.get('step4'))}\n"
            f"Step5: {self._format_num(detail.get('step5'))}\n"
            f"Highest Activated Level: {detail.get('highest_activated_level')}\n"
            f"Activated Levels: {detail.get('activated_levels')}\n"
            f"Configured C1-C4: {self._format_num(detail.get('configured_c1'))}, {self._format_num(detail.get('configured_c2'))}, {self._format_num(detail.get('configured_c3'))}, {self._format_num(detail.get('configured_c4'))}\n"
            f"Cumulative Real Volume: {self._format_num(detail.get('cumulative_real_volume'))}\n"
            f"Current Position Side/Size: {detail.get('position_side')} / {self._format_num(detail.get('position_size'))}\n"
            f"Average Entry Price: {self._format_num(detail.get('average_entry_price'))}\n"
            f"Current Strategy Raw SL: {self._format_num(detail.get('current_strategy_raw_sl'))}\n"
            f"Actual Exchange SL: {self._format_num(detail.get('actual_exchange_sl'))}\n"
            f"Current Strategy Raw TP: {self._format_num(detail.get('current_strategy_raw_tp'))}\n"
            f"Actual Exchange TP: {self._format_num(detail.get('actual_exchange_tp'))}\n"
            f"Frozen Reason/Error: {detail.get('frozen_reason') or '—'}\n"
            f"Completed Fibo Cycles: {self._format_num(detail.get('completed_cycles'))}\n"
            f"Profitable Cycles: {self._format_num(detail.get('profitable_cycles'))}\n"
            f"Losing Cycles: {self._format_num(detail.get('losing_cycles'))}\n"
            f"Total Realized P&L: {self._format_num(detail.get('total_realized_pnl'))}\n"
            f"Fees: {self._format_num(detail.get('fees'))}"
        )
        return Screen(
            text=text,
            buttons=[
                [_button(*BUTTON_REFRESH)],
                [_button(*BUTTON_BACK), _button(*BUTTON_EXIT)],
            ],
            state=state.state,
        )

    def _render_stop_list(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "stop_list"
        response = self._service.execute_command({"op": "list"})
        regs = list(response.get("registrations") or [])
        lines = ["🛑 STOP Fibo", ""]
        rows: List[List[Dict[str, str]]] = []
        if not regs:
            lines.append("No running registrations.")
        for reg in regs:
            label = self._registration_label(reg)
            lines.append(label)
            rows.append([_button(label, f"registration:{quote(str(reg['registration_key']), safe='')}")])
        rows.append([_button(*BUTTON_BACK), _button(*BUTTON_EXIT)])
        return Screen(text="\n".join(lines), buttons=rows, state=state.state)

    def _handle_stop_list(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self.open(chat_key)
        prefix, value = _split_callback(suffix)
        if prefix != "registration" or not value:
            return self._render_stop_list(chat_key)
        state = self._state_for(chat_key)
        state.registration_key = unquote(value)
        state.state = "stop_confirm"
        label = self._registration_label_from_key(state.registration_key)
        return Screen(
            text=(
                "STOP & CLOSE will:\n\n"
                f"{label}\n\n"
                "- stop that Fibo registration\n"
                "- stop further quote/cascade processing\n"
                "- prevent new Fibo orders/protection updates\n"
                "- close the current position for that account/instrument\n"
                "- remove its TP\n"
                "- remove its SL\n"
                "- verify the lane is flat and clean"
            ),
            buttons=[
                [_button(*BUTTON_CONFIRM_STOP_CLOSE)],
                [_button(*BUTTON_CANCEL), _button(*BUTTON_EXIT)],
            ],
            state=state.state,
        )

    def _handle_stop_confirm(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_stop_list(chat_key)
        if suffix != "confirm_stop_close":
            return self._render_stop_list(chat_key)
        state = self._state_for(chat_key)
        result = self._service.execute_command({"op": "stop_close", "registration_key": state.registration_key})
        if not result.get("ok"):
            message = str(result.get("message") or result.get("error") or "STOP & CLOSE failed")
            return Screen(
                text=f"STOP & CLOSE failed.\n\n{message}",
                buttons=[[_button(*BUTTON_STOP)], [_button(*BUTTON_EXIT)]],
                state="main_menu",
            )
        return Screen(
            text=(
                f"STOP & CLOSE completed for {state.registration_key}.\n\n"
                f"Verified clean: {bool(result.get('verified_clean'))}"
            ),
            buttons=[[_button(*BUTTON_RUNNING)], [_button(*BUTTON_EXIT)]],
            state="main_menu",
        )

    def _state_for(self, chat_key: Tuple[Any, ...]) -> WizardState:
        state = self._states.get(chat_key)
        if state is None:
            state = WizardState()
            self._states[chat_key] = state
        return state

    @staticmethod
    def _pretty_exchange(value: Any) -> str:
        text = str(value or "")
        return "Ondoperps" if text.lower() == "ondoperps" else text

    @staticmethod
    def _pretty_counter_side(value: Any) -> str:
        text = str(value or "")
        return {
            "counterBUY": "Counter BUY",
            "counterSELL": "Counter SELL",
        }.get(text, text)

    def _registration_label(self, detail: Dict[str, Any]) -> str:
        return (
            f"{self._pretty_exchange(detail.get('exchange'))} / {detail.get('account')} / "
            f"{detail.get('instrument')} / {self._pretty_counter_side(detail.get('counter_side'))}"
        )

    def _registration_label_from_key(self, key: Optional[str]) -> str:
        if not key:
            return "Unknown registration"
        parts = str(key).split(":", 3)
        if len(parts) != 4:
            return key
        exchange, account, instrument, counter = parts
        return f"{self._pretty_exchange(exchange)} / {account} / {instrument} / {self._pretty_counter_side(counter)}"

    @staticmethod
    def _format_num(value: Any) -> str:
        if value is None:
            return "—"
        text = str(value)
        if text.endswith(".0"):
            return text[:-2]
        return text

    @staticmethod
    def _render_message(text: str, state: str) -> Screen:
        return Screen(text=text, buttons=[[_button(*BUTTON_BACK), _button(*BUTTON_EXIT)]], state=state)


def _split_callback(data: str) -> Tuple[str, str]:
    text = str(data or "")
    if ":" not in text:
        return text, text
    prefix, _, suffix = text.partition(":")
    return prefix, suffix


def _button(text: str, callback_data: str) -> Dict[str, str]:
    return {"text": str(text), "callback_data": str(callback_data)}


_WIZARD: Optional[FiboWizard] = None


def get_fibo_wizard() -> FiboWizard:
    global _WIZARD
    if _WIZARD is None:
        _WIZARD = FiboWizard()
    return _WIZARD


async def _send_screen(adapter: TelegramAdapter, msg_or_query: Any, screen: Screen) -> None:
    chat_id = getattr(getattr(msg_or_query, "chat", None), "id", None)
    if chat_id is None:
        chat_id = getattr(getattr(getattr(msg_or_query, "message", None), "chat", None), "id", None)
    await adapter.send_inline_keyboard(
        chat_id=chat_id,
        text=screen.text,
        buttons=screen.buttons,
        callback_prefix="fibo",
        metadata={"state": screen.state},
    )


async def handle_fibo_command(adapter: TelegramAdapter, msg: Any) -> bool:
    text = str(getattr(msg, "text", "") or "").strip()
    if not text.startswith("/fibo"):
        return False
    parts = text.split(maxsplit=1)
    if parts[0] != "/fibo":
        return False
    wizard = get_fibo_wizard()
    chat_key = _chat_key(msg)
    screen = wizard.open(chat_key)
    await _send_screen(adapter, msg, screen)
    return True


async def handle_fibo_callback(adapter: TelegramAdapter, query: Any, data: str) -> bool:
    if not str(data or "").startswith("fibo:"):
        return False
    payload = str(data).split(":", 1)[1] if ":" in str(data) else ""
    wizard = get_fibo_wizard()
    chat_key = _chat_key(getattr(query, "message", query))
    screen = wizard.handle_callback(chat_key, payload)
    await _send_screen(adapter, query, screen)
    return True


async def handle_fibo_text(adapter: TelegramAdapter, msg: Any) -> bool:
    wizard = get_fibo_wizard()
    chat_key = _chat_key(msg)
    state = wizard._state_for(chat_key)  # noqa: SLF001
    if state.state not in {
        "awaiting_instrument",
        "awaiting_divide_percent",
        "awaiting_counter1",
        "awaiting_counter2",
        "awaiting_counter3",
        "awaiting_counter4",
    }:
        return False
    screen = wizard.handle_text(chat_key, str(getattr(msg, "text", "") or ""))
    await _send_screen(adapter, msg, screen)
    return True


def _chat_key(msg: Any) -> Tuple[Any, ...]:
    chat_id = getattr(getattr(msg, "chat", None), "id", None)
    thread_id = getattr(msg, "message_thread_id", None)
    return (chat_id,) if thread_id is None else (chat_id, thread_id)


__all__ = [
    "FiboWizard",
    "Screen",
    "WizardState",
    "get_fibo_wizard",
    "handle_fibo_command",
    "handle_fibo_callback",
    "handle_fibo_text",
]
