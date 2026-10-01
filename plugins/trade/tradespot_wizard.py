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
import secrets
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Dict, List, Mapping, Optional, Tuple, cast

from .canonical import CanonicalResponse
from .spotdesk import SpotDesk, get_spotdesk
from .wizard import _account_option_parts, _button_row, _render_error_lines

logger = logging.getLogger(__name__)

BUTTON_CLOSE = ("❌ Close", "close")
BUTTON_BACK = ("⬅️ Back", "back")
BUTTON_BACK_RETURN = ("↩️ Back", "back")
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
_MUTATING_ACTIONS = {"ladder", "cancel_orders"}
_QUICK_PICK_BASE_ASSETS = ("SOL", "ETH", "HYPE", "SUI")
_PAIR_PAGE_SIZE = 8
_MEXC_QUOTE_ASSETS = ("USDT", "USDC")
_QUOTE_CENTS = Decimal("0.01")
_OPEN_LIMIT_STATUSES = {"", "NEW", "PARTIALLY_FILLED", "LIVE", "PENDING"}
_LIMIT_ORDER_TYPES = {"", "LIMIT", "LIMIT_MAKER"}
_CLOSED_STATUSES = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}


def _balance_assets(response: CanonicalResponse) -> List[Any]:
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        assets = data.get("assets")
        if isinstance(assets, list):
            return assets
    return []


def _asset_symbol(item: Mapping[str, Any]) -> str:
    return str(item.get("asset") or item.get("symbol") or "").strip().upper()


def _to_amount(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    text = str(value).strip()
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _asset_total_decimal(item: Mapping[str, Any]) -> Decimal:
    total = item.get("total")
    if total is not None and str(total).strip() != "":
        return _to_amount(total)
    free = item.get("free")
    locked = item.get("locked")
    if (free is not None and str(free).strip() != "") or (locked is not None and str(locked).strip() != ""):
        return _to_amount(free) + _to_amount(locked)
    for key in ("amount", "balance"):
        raw = item.get(key)
        if raw is not None and str(raw).strip() != "":
            return _to_amount(raw)
    return Decimal("0")


def _thousands(text: str) -> str:
    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]
    if "." in text:
        whole, frac = text.split(".", 1)
        return f"{sign}{int(whole or '0'):,}.{frac}"
    return f"{sign}{int(text or '0'):,}"


def _format_inventory_amount(value: Decimal, *, quote: bool) -> str:
    if quote:
        quantized = value.quantize(_QUOTE_CENTS, rounding=ROUND_HALF_UP)
        return _thousands(format(quantized, "f"))
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if not text or text == "-":
        text = "0"
    return _thousands(text)


def _mexc_inventory_lines(assets: List[Any]) -> List[str]:
    totals: Dict[str, Decimal] = {}
    for item in assets:
        if not isinstance(item, Mapping):
            continue
        symbol = _asset_symbol(item)
        if not symbol:
            continue
        totals[symbol] = totals.get(symbol, Decimal("0")) + _asset_total_decimal(item)
    lines = [
        f"{symbol}: {_format_inventory_amount(totals.get(symbol, Decimal('0')), quote=True)}"
        for symbol in _MEXC_QUOTE_ASSETS
    ]
    others = sorted(
        (symbol, amount)
        for symbol, amount in totals.items()
        if symbol not in _MEXC_QUOTE_ASSETS and amount > 0
    )
    lines.extend(
        f"{symbol}: {_format_inventory_amount(amount, quote=False)}"
        for symbol, amount in others
    )
    return lines


def _format_spot_number(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if not text or text == "-":
        text = "0"
    return _thousands(text)


def _display_pair_from_symbol(symbol: str) -> str:
    sym = str(symbol or "").upper().replace("/", "")
    for quote in ("USDT", "USDC", "FDUSD", "BTC", "ETH", "MX", "USD"):
        if sym.endswith(quote) and len(sym) > len(quote):
            return f"{sym[:-len(quote)]}/{quote}"
    return str(symbol or "").upper()


def _order_remaining(row: Mapping[str, Any]) -> Decimal:
    remaining = row.get("remaining_qty")
    if remaining is not None and str(remaining).strip() != "":
        return _to_amount(remaining)
    orig = row.get("orig_qty", row.get("origQty"))
    executed = row.get("executed_qty", row.get("executedQty"))
    return _to_amount(orig) - _to_amount(executed)


def _group_open_limit_orders(rows: List[Any]) -> List[Dict[str, Any]]:
    buckets: Dict[Tuple[str, str], List[Mapping[str, Any]]] = {}
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "").upper()
        if status in _CLOSED_STATUSES or (status and status not in _OPEN_LIMIT_STATUSES):
            continue
        order_type = str(item.get("type") or item.get("order_type") or "").upper()
        if order_type and order_type not in _LIMIT_ORDER_TYPES:
            continue
        side = str(item.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            continue
        remaining = _order_remaining(item)
        if remaining <= 0:
            continue
        pair = str(item.get("pair") or "").strip().upper()
        compact = str(item.get("symbol") or "").strip().upper().replace("/", "")
        if not pair:
            pair = _display_pair_from_symbol(compact or str(item.get("symbol") or ""))
        if not compact:
            compact = pair.replace("/", "")
        if not pair or not compact:
            continue
        buckets.setdefault((side, pair), []).append(item)
    groups: List[Dict[str, Any]] = []
    for side, pair in sorted(buckets, key=lambda key: (key[1], 0 if key[0] == "BUY" else 1)):
        items = buckets[(side, pair)]
        qty = sum((_order_remaining(row) for row in items), Decimal("0"))
        prices = [_to_amount(row.get("price")) for row in items]
        live_prices = [p for p in prices if p > 0]
        notional = sum((_order_remaining(row) * _to_amount(row.get("price")) for row in items), Decimal("0"))
        vwap = (notional / qty) if qty > 0 else Decimal("0")
        compact = str(items[0].get("symbol") or pair).upper().replace("/", "")
        quote = pair.split("/")[-1] if "/" in pair else ""
        base = pair.split("/")[0] if "/" in pair else pair
        groups.append(
            {
                "side": side,
                "pair": pair,
                "symbol": compact,
                "base": base,
                "quote": quote,
                "order_count": len(items),
                "total_qty": qty,
                "min_price": min(live_prices) if live_prices else Decimal("0"),
                "max_price": max(live_prices) if live_prices else Decimal("0"),
                "vwap": vwap,
                "order_ids": [str(row.get("order_id") or row.get("orderId") or "") for row in items if str(row.get("order_id") or row.get("orderId") or "")],
            }
        )
    return groups


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
    selected_base: Optional[str] = None
    pair_candidates: List[Dict[str, Any]] = field(default_factory=list)
    pair_page: int = 0
    selected_instrument: Optional[Dict[str, Any]] = None
    order_side: Optional[str] = None
    order_quantity: Optional[str] = None
    order_limit_price: Optional[str] = None
    confirm_token: Optional[str] = None
    confirm_consumed: bool = False
    last_submit_screen: Optional["Screen"] = None
    cancel_side: Optional[str] = None
    cancel_symbol: Optional[str] = None
    cancel_pair: Optional[str] = None
    # Ladder state
    ladder_side: Optional[str] = None
    ladder_total_qty: Optional[str] = None
    ladder_start_price: Optional[str] = None
    ladder_end_price: Optional[str] = None
    ladder_order_count: Optional[int] = None
    ladder_distribution: Optional[str] = None
    ladder_plan: Optional[Dict[str, Any]] = None
    ladder_confirm_token: Optional[str] = None
    ladder_execution_id: Optional[str] = None
    ladder_confirm_consumed: bool = False


class TradeSpotWizard:
    """Small SPOT-specific Telegram state machine."""

    # Test-only escape hatch: when True, the wizard renders the Ladder action
    # button even if the agent does not advertise the `ladder` capability.
    # Production keeps the agent cap off, so the button is hidden by default.
    _ladder_preview_only: bool = False

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
        state = self._state_for(chat_key)
        value = (text or "").strip()
        if state.state == "new_order_other":
            return self._handle_other_symbol_text(chat_key, value)
        if state.state == "new_order_qty":
            return self._handle_quantity_text(chat_key, value)
        if state.state == "new_order_price":
            return self._handle_limit_price_text(chat_key, value)
        if state.state.startswith("ladder_"):
            return self._handle_ladder_text(chat_key, value)
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
        if state.state == "new_order_asset":
            return self._handle_new_order_asset(chat_key, suffix)
        if state.state == "new_order_other":
            return self._handle_new_order_other_callback(chat_key, suffix)
        if state.state == "new_order_pairs":
            return self._handle_pair_selection(chat_key, suffix)
        if state.state == "new_order_side":
            return self._handle_side_selection(chat_key, suffix)
        if state.state == "new_order_qty":
            return self._handle_quantity_callback(chat_key, suffix)
        if state.state == "new_order_price":
            return self._handle_limit_price_callback(chat_key, suffix)
        if state.state == "new_order_preview":
            return self._handle_preview_callback(chat_key, suffix)
        if state.state == "order_result":
            return self._handle_order_result_callback(chat_key, suffix)
        if state.state == "cancel_orders":
            return self._handle_cancel_list_callback(chat_key, suffix)
        if state.state == "cancel_confirm":
            return self._handle_cancel_confirm_callback(chat_key, suffix)
        if state.state == "cancel_result":
            return self._handle_cancel_result_callback(chat_key, suffix)
        # Ladder states
        if state.state == "ladder_asset":
            screen = self._handle_ladder_asset(chat_key, suffix)
            if screen.state == "new_order_pairs":
                state.state = "ladder_pairs"
            return screen
        if state.state == "ladder_pairs":
            screen = self._handle_ladder_pair_selection(chat_key, suffix)
            if screen.state == "new_order_side":
                state.state = "ladder_side"
                return self._render_ladder_side(chat_key)
            return screen
        if state.state == "ladder_side":
            return self._handle_ladder_side_selection(chat_key, suffix)
        if state.state == "ladder_total_qty":
            return self._render_ladder_total_qty(chat_key)
        if state.state == "ladder_start_price":
            return self._render_ladder_start_price(chat_key)
        if state.state == "ladder_end_price":
            return self._render_ladder_end_price(chat_key)
        if state.state == "ladder_order_count":
            return self._render_ladder_order_count(chat_key)
        if state.state == "ladder_distribution":
            if suffix in {"distribution:uniform", "distribution:half_gaussian"}:
                state.ladder_distribution = suffix[len("distribution:") :].strip()
                return self._render_ladder_preview(chat_key)
            return self._render_ladder_distribution(chat_key)
        if state.state == "ladder_preview":
            if suffix.startswith("ladder_confirm:"):
                return self._handle_ladder_confirm_callback(chat_key, suffix)
            if suffix.startswith("ladder_edit:"):
                return self._handle_ladder_edit_callback(chat_key, suffix)
            if suffix == "back":
                return self._render_ladder_edit_screen(chat_key)
            return self._render_ladder_preview(chat_key)
        if state.state == "ladder_result":
            if suffix == "back":
                return self._render_ladder_edit_screen(chat_key)
            if suffix.startswith("ladder_reconcile:"):
                return self._handle_ladder_reconcile(chat_key, suffix)
            if suffix.startswith("ladder_ack:"):
                return self._handle_ladder_ack(chat_key, suffix)
            return self._render_action(chat_key)
        if state.state == "ladder_edit":
            if suffix.startswith("ladder_edit:"):
                return self._handle_ladder_edit_callback(chat_key, suffix)
            if suffix == "back":
                return self._render_action(chat_key)
            return self._render_ladder_edit_screen(chat_key)
        if state.state in {"balance", "orders", "unsupported"}:
            return self._handle_result_screen(chat_key, suffix)
        return self.open(chat_key)

    def _render_select_exchange(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "select_exchange"
        state.exchange = None
        state.account = None
        self._clear_order_state(state)
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
        self._clear_order_state(state)
        return self._render_action(chat_key)

    def _supports_new_order_picker(self, exchange: str) -> bool:
        caps = set(self._desk.capabilities(exchange) or [])
        return "new_order" in caps or {"list_instruments", "resolve_instrument", "market_price"}.issubset(caps)

    def _supported_action_buttons(self, exchange: str) -> List[tuple[str, str]]:
        caps = set(self._desk.capabilities(exchange) or [])
        rows: List[tuple[str, str]] = []
        seen_callbacks: set[str] = set()
        for cap, label, callback in _SPOT_ACTIONS:
            if cap == "ladder":
                # Phase 1 source-only: ladder button only renders when the
                # agent explicitly advertises the `ladder` capability OR the
                # wizard is in test-only preview mode.
                if "ladder" in caps or self._ladder_preview_only:
                    rows.append((label, f"action:{callback}"))
                    seen_callbacks.add(callback)
                continue
            if cap == "new_order" and self._supports_new_order_picker(exchange):
                rows.append((label, f"action:{callback}"))
                seen_callbacks.add(callback)
                continue
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
        if action == "new_order" and self._supports_new_order_picker(exchange):
            return self._render_new_order_assets(chat_key)
        if action == "cancel_orders" and "cancel_orders" in caps:
            return self._render_cancel_orders(chat_key)
        if action == "ladder" and (
            "ladder" in caps or self._ladder_preview_only
        ):
            return self._render_ladder_assets(chat_key)
        if action in _MUTATING_ACTIONS and action in caps:
            return self._render_mutating_not_enabled(chat_key, action)
        return self._render_action(chat_key)

    def _clear_order_state(self, state: SpotWizardState) -> None:
        state.selected_base = None
        state.pair_candidates = []
        state.pair_page = 0
        state.selected_instrument = None
        state.order_side = None
        state.order_quantity = None
        state.order_limit_price = None
        state.confirm_token = None
        state.confirm_consumed = False
        state.last_submit_screen = None
        state.cancel_side = None
        state.cancel_symbol = None
        state.cancel_pair = None
        # Clear ladder fields so a fresh wizard session does not inherit them.
        state.ladder_side = None
        state.ladder_total_qty = None
        state.ladder_start_price = None
        state.ladder_end_price = None
        state.ladder_order_count = None
        state.ladder_distribution = None
        state.ladder_plan = None
        state.ladder_confirm_token = None

    # ------------------------------------------------------------------
    # Ladder preview flow (Phase 1, source-only)
    # ------------------------------------------------------------------

    def _render_ladder_assets(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "ladder_asset"
        self._clear_order_state(state)
        exchange = state.exchange or ""
        title = f"🟦 {exchange.upper()} Spot — Ladder" if exchange else "🟦 Spot — Ladder"
        rows = [[_button_row(asset, f"asset:{asset}")] for asset in _QUICK_PICK_BASE_ASSETS]
        rows.append([_button_row("Other", "asset:other")])
        rows.append([_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)])
        return Screen(f"{title}\n\nSelect Asset:", rows, "ladder_asset")

    def _render_ladder_side(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "ladder_side"
        pair = self._selected_pair_name(state)
        rows = [
            [_button_row("🔵 BUY", "side:buy")],
            [_button_row("🔴 SELL", "side:sell")],
            [_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)],
        ]
        return Screen(f"🟦 {pair} — Ladder\n\nSelect Side:", rows, "ladder_side")

    def _render_ladder_total_qty(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "ladder_total_qty"
        pair = self._selected_pair_name(state)
        base = ((state.selected_instrument or {}).get("base")
                or (state.selected_instrument or {}).get("baseAsset")
                or "BASE").upper()
        return Screen(
            f"🟦 {pair} — Ladder\n\nEnter total quantity in {base}:",
            [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]],
            "ladder_total_qty",
        )

    def _render_ladder_start_price(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "ladder_start_price"
        pair = self._selected_pair_name(state)
        quote = ((state.selected_instrument or {}).get("quote")
                 or (state.selected_instrument or {}).get("quoteAsset")
                 or "QUOTE").upper()
        return Screen(
            f"🟦 {pair} — Ladder\n\nEnter START price in {quote}:",
            [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]],
            "ladder_start_price",
        )

    def _render_ladder_end_price(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "ladder_end_price"
        pair = self._selected_pair_name(state)
        quote = ((state.selected_instrument or {}).get("quote")
                 or (state.selected_instrument or {}).get("quoteAsset")
                 or "QUOTE").upper()
        side = (state.ladder_side or "").upper()
        direction = "lower than START (BUY)" if side == "BUY" else "higher than START (SELL)"
        return Screen(
            f"🟦 {pair} — Ladder\n\nEnter END price in {quote}:\nMust be {direction}.",
            [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]],
            "ladder_end_price",
        )

    def _render_ladder_order_count(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "ladder_order_count"
        pair = self._selected_pair_name(state)
        return Screen(
            f"🟦 {pair} — Ladder\n\nEnter number of orders (1-500):",
            [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]],
            "ladder_order_count",
        )

    def _render_ladder_distribution(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "ladder_distribution"
        pair = self._selected_pair_name(state)
        rows = [
            [_button_row("Uniform", "distribution:uniform")],
            [_button_row("Half-Gaussian", "distribution:half_gaussian")],
            [_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)],
        ]
        return Screen(f"🟦 {pair} — Ladder\n\nSelect distribution:", rows, "ladder_distribution")

    def _compute_ladder_plan(self, state: SpotWizardState) -> Optional[Dict[str, Any]]:
        from plugins.trade.spot_ladder import compute_ladder, plan_as_dict

        item = state.selected_instrument or {}
        try:
            total = _to_amount(state.ladder_total_qty)
            start = _to_amount(state.ladder_start_price)
            end = _to_amount(state.ladder_end_price)
            count = int(state.ladder_order_count or 0)
        except (InvalidOperation, ValueError, TypeError) as exc:
            return {"_error": f"Invalid ladder inputs: {exc}"}
        if total <= 0:
            return {"_error": "Total quantity must be greater than zero."}
        if start <= 0 or end <= 0:
            return {"_error": "START and END prices must be greater than zero."}
        if count <= 0:
            return {"_error": "Order count must be greater than zero."}
        try:
            plan = compute_ladder(
                side=state.ladder_side or "BUY",
                distribution=state.ladder_distribution or "uniform",
                total_volume=total,
                start_price=start,
                end_price=end,
                order_count=count,
                instrument=item,
            )
        except ValueError as exc:
            return {"_error": str(exc)}
        return plan_as_dict(plan)

    def _ladder_request_values(self, state: SpotWizardState) -> tuple[Decimal, int] | None:
        try:
            total = _to_amount(state.ladder_total_qty)
            count = int(state.ladder_order_count or 0)
        except (InvalidOperation, ValueError, TypeError):
            return None
        return total, count

    def _ladder_plan_error(self, state: SpotWizardState, plan: Optional[Dict[str, Any]]) -> Optional[str]:
        requested = self._ladder_request_values(state)
        if requested is None:
            return "Invalid ladder inputs. Re-open the ladder preview and try again."
        requested_total, requested_count = requested
        if requested_count <= 0:
            return "Order count must be greater than zero."
        if requested_total <= 0:
            return "Total quantity must be greater than zero."
        if not isinstance(plan, dict):
            return "Planner did not return a ladder plan."
        if plan.get("_error"):
            return str(plan.get("_error"))
        children = plan.get("children") or []
        if not children:
            return "Planner returned no children."
        if len(children) != requested_count:
            return f"Planner returned {len(children)} children for requested {requested_count} orders."
        for index, child in enumerate(children, start=1):
            try:
                qty = _to_amount(child.get("size"))
                price = _to_amount(child.get("price"))
            except (InvalidOperation, ValueError, TypeError):
                return f"Child {index} has invalid quantity or price."
            if qty <= 0 or price <= 0:
                return f"Child {index} has non-positive quantity or price."
        return None

    def _render_ladder_plan_error(self, chat_key: Tuple[Any, ...], message: str) -> Screen:
        state = self._state_for(chat_key)
        state.ladder_confirm_token = None
        state.ladder_execution_id = None
        state.state = "ladder_preview"
        pair = self._selected_pair_name(state)
        body = (
            f"🟦 {pair} — LIMIT Ladder Preview\n\n"
            f"Unable to build ladder.\n\n{message}\n\n"
            "No orders were submitted. Adjust total quantity, order count, or price range and try again."
        )
        return Screen(body, [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]], "ladder_preview")

    def _render_ladder_submit_error(self, chat_key: Tuple[Any, ...], message: str) -> Screen:
        state = self._state_for(chat_key)
        state.ladder_confirm_token = None
        state.state = "ladder_result"
        body = (
            "🟦 Unable to submit ladder\n\n"
            f"{message}\n\n"
            "No orders were submitted. Re-open the ladder preview before trying again."
        )
        return Screen(body, [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]], "ladder_result")

    def _render_ladder_preview(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        item = state.selected_instrument or {}
        plan = self._compute_ladder_plan(state)
        plan_error = self._ladder_plan_error(state, plan)
        if plan_error:
            return self._render_ladder_plan_error(chat_key, plan_error)
        plan = plan or {}
        base = ((item.get("base") or item.get("baseAsset") or "BASE")).upper()
        quote = ((item.get("quote") or item.get("quoteAsset") or "QUOTE")).upper()
        side = (state.ladder_side or "").upper()
        distribution_label = (
            "Half-Gaussian" if plan.get("distribution") == "half_gaussian" else "Uniform"
        )
        if side == "BUY":
            required_asset = quote
            available = self._holding_for(state, required_asset)
            required = _to_amount(plan.get("total_notional"))
            after = available - required
        else:
            required_asset = base
            available = self._holding_for(state, required_asset)
            required = _to_amount(plan.get("total_size"))
            after = available - required

        children = plan.get("children") or []
        total_children = len(children)
        show_first = min(3, total_children)
        show_last = min(3, max(0, total_children - show_first))
        child_rows: List[str] = []
        for c in children[:show_first]:
            child_rows.append(
                f"  {c['price']} {quote} × {c['size']} {base} = {c['notional']} {quote}"
            )
        if total_children > show_first + show_last:
            child_rows.append("  …")
        if show_last > 0:
            for c in children[-show_last:]:
                child_rows.append(
                    f"  {c['price']} {quote} × {c['size']} {base} = {c['notional']} {quote}"
                )

        notes = plan.get("notes") or []
        notes_block = ("\n\n" + "\n".join(notes)) if notes else ""

        insufficient = after < 0
        body = (
            f"🟦 {base}/{quote} — LIMIT Ladder Preview\n"
            f"\nSide: {side}\n"
            f"Distribution: {distribution_label}\n"
            f"Orders: {plan.get('max_valid_children', total_children)}\n"
            f"Total Quantity: {plan.get('total_size','0')} {base}\n"
            f"Price Range: {state.ladder_end_price or ''} → {state.ladder_start_price or ''} {quote}\n"
            f"VWAP: {plan.get('vwap','0')} {quote}\n"
            f"\nRequired: {plan.get('total_notional','0')} {quote}\n"
            f"Available: {available.normalize():f} {quote}\n"
            f"After ladder: {after.normalize():f} {quote}"
            f"{notes_block}\n\nChildren:\n"
            + ("\n".join(child_rows) if child_rows else "  (none)")
        )

        if insufficient:
            body += (
                f"\n\nInsufficient {required_asset}: need {required.normalize():f}, "
                f"have {available.normalize():f}. No orders were submitted."
            )
            rows = [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]]
            state.state = "ladder_result"
            return Screen(body, rows, "ladder_result")

        if not state.ladder_confirm_token:
            import secrets
            state.ladder_confirm_token = f"lad:{secrets.token_hex(8)}"
        if not state.ladder_execution_id:
            import secrets as _secrets
            # Deterministic per-preview execution ID; all child
            # newClientOrderId values are derived from it so the second
            # Confirm (which is rejected by single-use token below)
            # would map to the same exchange orders.
            state.ladder_execution_id = _secrets.token_hex(8)
        rows: List[List[Dict[str, str]]] = [
            [_button_row("✅ Confirm & Place Ladder", f"ladder_confirm:{state.ladder_confirm_token}")],
            [_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)],
        ]
        state.state = "ladder_preview"
        return Screen(body, rows, "ladder_preview")

    def _handle_ladder_confirm_callback(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        token = suffix[len("ladder_confirm:") :].strip() if suffix.startswith("ladder_confirm:") else ""
        if not token or token != (state.ladder_confirm_token or ""):
            # Invalid token; re-render the preview untouched.
            return self._render_ladder_preview(chat_key)
        if state.ladder_confirm_consumed:
            # Single-use: a second Confirm tap must NOT submit again.
            # Show the cached result or a clear "already submitted" line.
            rows = [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]]
            state.state = "ladder_result"
            return Screen(
                "This ladder has already been submitted. No further orders were placed.",
                rows,
                "ladder_result",
            )
        # Mark the token consumed BEFORE any network write.
        state.ladder_confirm_consumed = True

        item = state.selected_instrument or {}
        # Re-resolve instrument so a stale preview never reaches the wire.
        fresh = self._re_resolve_instrument(state)
        if fresh is None or "size_step" not in (fresh or {}):
            rows = [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]]
            state.state = "ladder_result"
            return Screen(
                "Instrument constraints are unavailable on the live exchange. Re-open the ladder preview before placing.",
                rows,
                "ladder_result",
            )
        # Compare stable fields; if anything material changed, refuse to submit.
        if fresh.get("size_step") != item.get("size_step") or fresh.get("price_tick") != item.get("price_tick"):
            rows = [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]]
            state.state = "ladder_result"
            return Screen(
                "Exchange constraints changed since this preview was approved. "
                "Re-open the ladder to regenerate the plan.",
                rows,
                "ladder_result",
            )
        # Re-fetch balance and verify still sufficient.
        side = (state.ladder_side or "").upper()
        base = (fresh.get("base") or fresh.get("baseAsset") or "BASE").upper()
        quote = (fresh.get("quote") or fresh.get("quoteAsset") or "QUOTE").upper()
        # Reuse the existing balance extraction helper.
        balance_resp = self._desk.execute(
            {"operation": "balance", "exchange": state.exchange, "account": state.account}
        )
        totals: Dict[str, Decimal] = {}
        for row in _balance_assets(balance_resp):
            if not isinstance(row, Mapping):
                continue
            symbol = _asset_symbol(row)
            if not symbol:
                continue
            totals[symbol] = totals.get(symbol, Decimal("0")) + _asset_total_decimal(row)
        plan = self._compute_ladder_plan(state)
        plan_error = self._ladder_plan_error(state, plan)
        if plan_error:
            return self._render_ladder_submit_error(chat_key, plan_error)
        plan = plan or {}
        if side == "BUY":
            required = _to_amount(plan.get("total_notional"))
            have = totals.get(quote, Decimal("0"))
            if have < required:
                rows = [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]]
                state.state = "ladder_result"
                return Screen(
                    f"Balance insufficient at submit. Need {required.normalize():f} {quote}, have {have.normalize():f} {quote}. No orders were submitted.",
                    rows,
                    "ladder_result",
                )
        else:
            required = _to_amount(plan.get("total_size"))
            have = totals.get(base, Decimal("0"))
            if have < required:
                rows = [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]]
                state.state = "ladder_result"
                return Screen(
                    f"Balance insufficient at submit. Need {required.normalize():f} {base}, have {have.normalize():f} {base}. No orders were submitted.",
                    rows,
                    "ladder_result",
                )

        # Build the FINAL precomputed children for the agent.
        children_payload = []
        execution_id = state.ladder_execution_id or "lad00000"
        for idx, child in enumerate(plan.get("children") or []):
            children_payload.append({
                "instrument": fresh,
                "symbol": str(fresh.get("symbol") or ""),
                "side": side,
                "quantity": str(child.get("size") or "0"),
                "price": str(child.get("price") or "0"),
                "client_order_id": f"ts_{execution_id}_{idx:03d}"[:32],
            })

        response = self._desk.execute({
            "operation": "ladder",
            "exchange": state.exchange,
            "account": state.account,
            "children": children_payload,
            "distribution": plan.get("distribution") or "",
            # Forward the wizard's execution_id so the agent's durable
            # record uses the SAME exec_id the wizard's confirm token
            # was minted for. This is what lets the wizard's reconcile
            # button later find the exact persisted record by id.
            "execution_id": state.ladder_execution_id,
        })
        # Persist the durable execution_id on the wizard state so the
        # reconcile button can find the persisted record after restart.
        if response.success and isinstance(getattr(response, "data", None), dict):
            agent_exec_id = response.data.get("execution_id")
            if isinstance(agent_exec_id, str) and agent_exec_id:
                state.ladder_execution_id = agent_exec_id
        # Burn the token so a second tap cannot submit.
        state.ladder_confirm_token = None
        state.state = "ladder_result"
        return self._render_ladder_result(
            chat_key,
            response,
            plan=plan,
            base=base,
            quote=quote,
            side=side,
            children=children_payload,
        )

    def _render_ladder_result(
        self,
        chat_key: Tuple[Any, ...],
        response: Any,
        *,
        plan: Dict[str, Any],
        base: str,
        quote: str,
        side: str,
        children: List[Dict[str, Any]],
    ) -> Screen:
        """Render the post-submission result. Per the user spec, never
        label a partial ladder 'failed' — show accepted / rejected /
        unknown / not_attempted counts explicitly."""
        rows = [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]]
        ladder = getattr(response, "ladder", None) if response is not None else None
        data = getattr(response, "data", None) or {}
        if ladder is None:
            err = getattr(response, "error", None)
            code = (err.code if err else None) or "UNKNOWN"
            msg = (err.message if err else "unknown") or "unknown"
            return Screen(
                f"🟦 Ladder submission failed\n\n{base}/{quote} {side}\n\nCode: {code}\n{msg}\n\nNo orders were placed.",
                rows,
                "ladder_result",
            )
        requested = getattr(ladder, "requested_order_count", 0) or 0
        accepted = getattr(ladder, "submitted_order_count", 0) or 0
        submitted_volume = getattr(ladder, "submitted_volume", "0") or "0"
        accepted_vwap_raw = (data.get("accepted_vwap") if isinstance(data, dict) else None)
        planned_vwap_raw = (data.get("planned_vwap") if isinstance(data, dict) else None)

        # When no children were accepted, accepted_vwap is meaningless.
        # Substitute "—" so we never render "Accepted VWAP:  USDC" with an
        # empty numeric followed by the unit.
        accepted_vwap_disp: Optional[str] = None
        if isinstance(accepted_vwap_raw, str) and accepted_vwap_raw.strip() and accepted_vwap_raw.lower() != "none":
            accepted_vwap_disp = accepted_vwap_raw
        elif isinstance(accepted_vwap_raw, (int, float)):
            accepted_vwap_disp = str(accepted_vwap_raw)
        if accepted_vwap_disp in (None, "", "None"):
            accepted_vwap_disp = "—"

        planned_vwap_disp: Optional[str] = None
        if isinstance(planned_vwap_raw, str) and planned_vwap_raw.strip() and planned_vwap_raw.lower() != "none":
            planned_vwap_disp = planned_vwap_raw
        elif isinstance(planned_vwap_raw, (int, float)):
            planned_vwap_disp = str(planned_vwap_raw)
        if planned_vwap_disp in (None, "", "None"):
            planned_vwap_disp = "—"

        rejected = (data.get("rejected") if isinstance(data, dict) else 0) or 0
        unknown = (data.get("unknown") if isinstance(data, dict) else 0) or 0
        not_attempted = (data.get("not_attempted") if isinstance(data, dict) else 0) or 0
        warn = ""
        if unknown or not_attempted:
            warn = "\n\n⚠️ Submission status is uncertain for some children.\nDo not retry the ladder until open orders are reconciled."
        body = (
            f"🟦 Ladder submission result\n\n"
            f"{base}/{quote} {side}\n"
            f"Requested: {requested}\n\n"
            f"Accepted: {accepted}\n"
            f"Rejected: {rejected}\n"
            f"Unknown: {unknown}\n"
            f"Not attempted: {not_attempted}\n\n"
            f"Accepted volume: {submitted_volume} {base}\n"
            f"Accepted VWAP: {accepted_vwap_disp} {quote}\n"
            f"Planned VWAP: {planned_vwap_disp} {quote}"
            f"{warn}"
        )
        if unknown or not_attempted:
            # Read-only reconciliation controls. Prefer the durable
            # 🔎 Reconcile Ladder button (uses the persisted execution_id
            # and the exact original client_order_ids) so a gateway
            # restart can still recover the same exact children.
            # NO ladder_confirm / batchOrders / order / cancel buttons.
            rows.append([
                _button_row("🔎 Reconcile Ladder", "ladder_reconcile:by_execution_id"),
            ])
            rows.append([
                _button_row("📂 Reconcile Open Orders", "ladder_reconcile:open_orders"),
                _button_row("📜 Reconcile All Orders", "ladder_reconcile:all_orders"),
            ])
            rows.append([
                _button_row("✅ Acknowledge & Close", "ladder_ack:close"),
            ])
        return Screen(body, rows, "ladder_result")

    def _render_ladder_edit_screen(self, chat_key: Tuple[Any, ...]) -> Screen:
        """Compact "Edit Ladder" screen that preserves exchange, account,
        pair, side, distribution, quantity, order count, START, and END.

        Reachable from ``Back`` on both ladder preview and ladder-result
        screens. Does NOT create a new Confirm token. Does NOT call the
        agent. Edits route through the existing field prompts and the next
        preview re-render mints a fresh confirm token + execution id.
        """
        state = self._state_for(chat_key)
        state.state = "ladder_edit"
        # Invalidate any prior single-use confirm token / execution id so a
        # stale token cannot leak into a new preview.
        state.ladder_confirm_token = None
        state.ladder_execution_id = None
        item = state.selected_instrument or {}
        base = str(item.get("base") or item.get("baseAsset") or "BASE").upper()
        quote = str(item.get("quote") or item.get("quoteAsset") or "QUOTE").upper()
        pair = self._selected_pair_name(state) or (f"{base}/{quote}" if base and quote else "")
        side_disp = (state.ladder_side or "").upper()
        distribution_disp = (
            "Half-Gaussian" if (state.ladder_distribution or "") == "half_gaussian" else "Uniform"
        )
        body = (
            f"🟦 {pair} — Edit LIMIT Ladder\n\n"
            f"Side: {side_disp}\n"
            f"Distribution: {distribution_disp}\n"
            f"Total Quantity: {state.ladder_total_qty or '—'} {base}\n"
            f"Orders: {state.ladder_order_count or '—'}\n"
            f"START: {state.ladder_start_price or '—'} {quote}\n"
            f"END: {state.ladder_end_price or '—'} {quote}\n"
        )
        rows: List[List[Dict[str, str]]] = []
        rows.append([_button_row(
            f"Quantity: {state.ladder_total_qty or '—'} {base}",
            "ladder_edit:qty",
        )])
        rows.append([_button_row(
            f"Orders: {state.ladder_order_count or '—'}",
            "ladder_edit:orders",
        )])
        rows.append([_button_row(
            f"START: {state.ladder_start_price or '—'} {quote}",
            "ladder_edit:start",
        )])
        rows.append([_button_row(
            f"END: {state.ladder_end_price or '—'} {quote}",
            "ladder_edit:end",
        )])
        rows.append([_button_row(
            f"Distribution: {distribution_disp}",
            "ladder_edit:distribution",
        )])
        rows.append([_button_row("Preview", "ladder_edit:preview")])
        rows.append([_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)])
        return Screen(body, rows, "ladder_edit")

    def _handle_ladder_reconcile(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        """Read-only reconciliation against the live exchange.

        GET /api/v3/openOrders or /api/v3/allOrders filtered by the ladder's
        symbol and execution id (when available). NEVER submits or cancels.
        """
        state = self._state_for(chat_key)
        item = state.selected_instrument or {}
        symbol = (
            item.get("symbol")
            or f"{item.get('base', '')}{item.get('quote', '')}".upper()
            or ""
        )
        which = suffix[len("ladder_reconcile:") :].strip()
        desk = getattr(self, "_spotdesk", None) or getattr(self, "spotdesk", None)
        try:
            if which == "open_orders":
                rows = self._reconcile_open_orders(symbol)
                body = self._format_reconcile_open_orders(symbol, rows)
            elif which == "all_orders":
                rows = self._reconcile_all_orders(symbol)
                body = self._format_reconcile_all_orders(symbol, rows, state)
            elif which == "by_execution_id":
                body = self._reconcile_by_execution_id(state, symbol)
            else:
                body = f"🟦 Reconcile {symbol}\n\nUnknown reconcile action."
        except Exception as exc:
            body = f"🟦 Reconcile {symbol}\n\nRead failed: {exc}"
        # Always stay in ladder_result and re-render the original result text
        # above the reconcile block, with safe Back/Close buttons.
        return Screen(
            body,
            [
                [_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)],
            ],
            "ladder_result",
        )

    def _reconcile_by_execution_id(self, state: Any, symbol: str) -> str:
        """Reconcile the ladder using the persisted execution_id and the
        EXACT original client_order_ids. Reads the durable record from disk
        so a gateway restart can still recover the same expected children.

        The agent's reconcile_batch() does the GETs. This wizard handler
        is just the read-only bridge. NEVER POSTs / DELETEs / retries.
        """
        execution_id = getattr(state, "ladder_execution_id", None) or ""
        if not execution_id:
            return (
                "🟦 Reconcile Ladder\n\n"
                "No execution_id is stored for this ladder. "
                "Use 📂 Reconcile Open Orders or 📜 Reconcile All Orders."
            )
        # Read the durable record directly so we know the EXACT original
        # expected client_order_ids (not derived from the current state).
        try:
            from plugins.trade.agents.x_mexc_agent_spot import _ladder_load_record
        except Exception as exc:  # noqa: BLE001
            return f"🟦 Reconcile Ladder\n\nagent not importable: {exc}"
        rec = None
        load_err = None
        try:
            rec = _ladder_load_record(state.account, execution_id)
        except Exception as exc:  # noqa: BLE001
            load_err = str(exc)
        if rec is None:
            return (
                f"🟦 Reconcile Ladder\n\n"
                f"No durable record for execution_id={execution_id} "
                f"({load_err or 'missing'}).\n\n"
                f"Use 📂 Reconcile Open Orders or 📜 Reconcile All Orders."
            )
        expected = [
            str(c.get("client_order_id") or "")
            for c in (rec.get("children") or [])
            if isinstance(c, dict)
        ]
        if not expected:
            return (
                f"🟦 Reconcile Ladder\n\n"
                f"Execution {execution_id} has no children on record."
            )
        # Call the read-only bridge execute() op to keep the dispatcher
        # path identical to all other reads. NO POST/DELETE.
        try:
            resp = self._desk.execute({
                "operation": "ladder_reconcile",
                "exchange": state.exchange,
                "account": state.account,
                "symbol": symbol,
                "execution_id": execution_id,
                "expected_client_order_ids": expected,
            })
        except Exception as exc:  # noqa: BLE001
            return f"🟦 Reconcile Ladder\n\nreconcile failed: {exc}"
        if not getattr(resp, "success", False):
            return (
                f"🟦 Reconcile Ladder\n\n"
                f"reconcile failed: {getattr(resp, 'error', None) or resp}"
            )
        summary = (resp.data or {}).get("summary") or {}
        head = (
            f"🟦 {symbol} {rec.get('side') or ''}\n"
            f"Execution: {execution_id}\n\n"
            f"Expected: {len(expected)}\n"
            f"Open: {summary.get('FOUND_OPEN', 0)}\n"
            f"Filled: {summary.get('FOUND_FILLED', 0)}\n"
            f"Canceled: {summary.get('FOUND_CANCELED', 0)}\n"
            f"Other terminal: {summary.get('FOUND_OTHER_TERMINAL', 0)}\n"
            f"Not found: {summary.get('NOT_FOUND', 0)}\n"
            f"Query unknown: {summary.get('QUERY_UNKNOWN', 0)}\n"
        )
        if summary.get("NOT_FOUND", 0) > 0 and summary.get("QUERY_UNKNOWN", 0) == 0:
            head += (
                "\n⚠️ Submission remains UNKNOWN for the Not found children.\n"
                "Do not retry until reconciled."
            )
        return head

    def _reconcile_open_orders(self, symbol: str) -> List[Dict[str, Any]]:
        """GET /api/v3/openOrders?symbol=<sym> via the spot agent's read path.
        Returns a list of dicts (possibly empty). No POST/DELETE ever issued.
        """
        agent = self._spot_agent_instance()
        if agent is None:
            return []
        return agent.open_orders(symbol=symbol)

    def _reconcile_all_orders(self, symbol: str) -> List[Dict[str, Any]]:
        """GET /api/v3/allOrders?symbol=<sym> via the spot agent's read path."""
        agent = self._spot_agent_instance()
        if agent is None:
            return []
        return agent.all_orders(symbol=symbol)

    def _spot_agent_instance(self):
        """Best-effort lookup of the MEXC spot agent bound to this wizard."""
        desk = getattr(self, "_spotdesk", None) or getattr(self, "spotdesk", None)
        if desk is None:
            return None
        agent = getattr(desk, "x_mexc_agent_spot", None)
        if agent is None and hasattr(desk, "agents"):
            try:
                agent = desk.agents.get("x_mexc_agent_spot")  # type: ignore[attr-defined]
            except Exception:
                agent = None
        return agent

    def _format_reconcile_open_orders(self, symbol: str, rows: List[Dict[str, Any]]) -> str:
        if not rows:
            return (
                f"🟦 Reconcile Open Orders\n\n{symbol}: no live open orders.\n\n"
                f"Use Reconcile All Orders to see recently-cancelled or filled children."
            )
        head = f"🟦 Reconcile Open Orders\n\n{symbol}: {len(rows)} live open orders\n\n"
        body_lines: List[str] = []
        for r in rows[:10]:
            cid = r.get("clientOrderId") or r.get("origClientOrderId") or "—"
            price = r.get("price") or "—"
            qty = r.get("origQty") or r.get("quantity") or "—"
            side = r.get("side") or "—"
            status = r.get("status") or "—"
            body_lines.append(
                f"  {side} {qty}@{price} status={status} clientOrderId={cid}"
            )
        if len(rows) > 10:
            body_lines.append(f"  … +{len(rows) - 10} more")
        return head + "\n".join(body_lines)

    def _format_reconcile_all_orders(
        self,
        symbol: str,
        rows: List[Dict[str, Any]],
        state: Any,
    ) -> str:
        execution_id = getattr(state, "ladder_execution_id", None) or ""
        if not rows:
            return (
                f"🟦 Reconcile All Orders\n\n{symbol}: no orders found in\n"
                f"/api/v3/allOrders filtered by this symbol.\n\n"
                f"execution_id={execution_id or '—'}\n"
                f"NOT_FOUND in allOrders is NOT proof of pre-send failure.\n"
                f"Open Orders above is the live truth."
            )
        head = f"🟦 Reconcile All Orders\n\n{symbol}: {len(rows)} historical orders\n\nexecution_id={execution_id or '—'}\n\n"
        body_lines: List[str] = []
        for r in rows[:10]:
            cid = r.get("clientOrderId") or "—"
            price = r.get("price") or "—"
            qty = r.get("origQty") or "—"
            status = r.get("status") or "—"
            body_lines.append(
                f"  status={status} {qty}@{price} clientOrderId={cid}"
            )
        if len(rows) > 10:
            body_lines.append(f"  … +{len(rows) - 10} more")
        return head + "\n".join(body_lines)

    def _handle_ladder_ack(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        """User acknowledged the UNKNOWN outcome. Return to action screen.

        This DOES NOT clear the ladder inputs (so the user can still reconcile
        by re-opening Edit Ladder via /tradespot → action:ladder), but it does
        invalidate the consumed Confirm token.
        """
        state = self._state_for(chat_key)
        which = suffix[len("ladder_ack:") :].strip()
        if which == "close":
            return self._render_action(chat_key)
        return self._render_ladder_result(chat_key)

    def _handle_ladder_edit_callback(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        """Route a single field of the Edit Ladder screen to its prompt.

        Invalidates the prior confirm token so the next preview mints a
        fresh one; this keeps Confirm tokens single-use relative to the
        plan that produced them, even after edits.
        """
        state = self._state_for(chat_key)
        if state.state not in {"ladder_preview", "ladder_result", "ladder_edit"}:
            return self._render_ladder_edit_screen(chat_key)
        state.ladder_confirm_token = None
        state.ladder_execution_id = None
        field = suffix[len("ladder_edit:") :].strip() if suffix.startswith("ladder_edit:") else ""
        if field == "qty":
            return self._render_ladder_total_qty(chat_key)
        if field == "orders":
            return self._render_ladder_order_count(chat_key)
        if field == "start":
            return self._render_ladder_start_price(chat_key)
        if field == "end":
            return self._render_ladder_end_price(chat_key)
        if field == "distribution":
            return self._render_ladder_distribution(chat_key)
        if field == "preview":
            return self._render_ladder_preview(chat_key)
        return self._render_ladder_edit_screen(chat_key)

    def _handle_ladder_text(self, chat_key: Tuple[Any, ...], text: str) -> Optional[Screen]:
        state = self._state_for(chat_key)
        if state.state == "ladder_total_qty":
            value = (text or "").strip()
            try:
                qty = _to_amount(value)
                if qty <= 0:
                    raise InvalidOperation
            except InvalidOperation:
                return Screen(
                    f"🟦 {self._selected_pair_name(state)} — Ladder\n\nInvalid total quantity.\nEnter a positive decimal in {((state.selected_instrument or {}).get('base') or 'BASE')}:",
                    [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]],
                    "ladder_total_qty",
                )
            state.ladder_total_qty = format(qty.normalize(), "f")
            return self._render_ladder_start_price(chat_key)
        if state.state == "ladder_start_price":
            try:
                value = _to_amount(text)
                if value <= 0:
                    raise InvalidOperation
            except InvalidOperation:
                return Screen(
                    f"🟦 {self._selected_pair_name(state)} — Ladder\n\nInvalid START price.\nEnter a positive price in {((state.selected_instrument or {}).get('quote') or 'QUOTE')}:",
                    [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]],
                    "ladder_start_price",
                )
            state.ladder_start_price = format(value.normalize(), "f")
            return self._render_ladder_end_price(chat_key)
        if state.state == "ladder_end_price":
            side = (state.ladder_side or "BUY").upper()
            try:
                value = _to_amount(text)
                start_v = _to_amount(state.ladder_start_price)
                if value <= 0:
                    raise InvalidOperation
                if side == "BUY" and value >= start_v:
                    raise InvalidOperation
                if side == "SELL" and value <= start_v:
                    raise InvalidOperation
            except InvalidOperation:
                direction = "lower than START" if side == "BUY" else "higher than START"
                return Screen(
                    f"🟦 {self._selected_pair_name(state)} — Ladder\n\nEND price must be {direction}.\nTry again:",
                    [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]],
                    "ladder_end_price",
                )
            state.ladder_end_price = format(value.normalize(), "f")
            return self._render_ladder_order_count(chat_key)
        if state.state == "ladder_order_count":
            try:
                count = int((text or "").strip())
                if count <= 0 or count > 500:
                    raise ValueError
            except ValueError:
                return Screen(
                    f"🟦 {self._selected_pair_name(state)} — Ladder\n\nEnter an integer between 1 and 500:",
                    [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]],
                    "ladder_order_count",
                )
            state.ladder_order_count = count
            return self._render_ladder_distribution(chat_key)
        return None

    def _handle_ladder_asset(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        # Reuse the new_order asset handlers so ladder pairs flow identically.
        return self._handle_new_order_asset(chat_key, suffix)

    def _handle_ladder_pair_selection(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        return self._handle_pair_selection(chat_key, suffix)

    def _handle_ladder_side_selection(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        # The new_order side handler sets `state.order_side` and renders the qty
        # prompt; for ladder we want the ladder total-qty prompt next.
        self._handle_side_selection(chat_key, suffix)
        state = self._state_for(chat_key)
        state.ladder_side = state.order_side
        return self._render_ladder_total_qty(chat_key)

    def _render_new_order_assets(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "new_order_asset"
        self._clear_order_state(state)
        exchange = state.exchange or ""
        title = f"🟦 {exchange.upper()} Spot — New Order" if exchange else "🟦 Spot — New Order"
        rows = [[_button_row(asset, f"asset:{asset}")] for asset in _QUICK_PICK_BASE_ASSETS]
        rows.append([_button_row("Other", "asset:other")])
        rows.append([_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)])
        return Screen(f"{title}\n\nSelect Asset:", rows, "new_order_asset")

    def _handle_new_order_asset(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_action(chat_key)
        if not suffix.startswith("asset:"):
            return self._render_new_order_assets(chat_key)
        asset = suffix[len("asset:") :].strip().upper()
        if asset == "OTHER":
            return self._render_other_prompt(chat_key)
        if asset not in _QUICK_PICK_BASE_ASSETS:
            return self._render_new_order_assets(chat_key)
        return self._render_pair_picker_for_base(chat_key, asset)

    def _render_other_prompt(self, chat_key: Tuple[Any, ...], message: str = "") -> Screen:
        state = self._state_for(chat_key)
        state.state = "new_order_other"
        body = (
            "🟦 MEXC Spot — New Order\n\n"
            "Enter a spot symbol or pair.\n\n"
            "Examples:\n"
            "BTC\n"
            "BTC/USDC\n"
            "BTCUSDC"
        )
        if message:
            body = f"{body}\n\n{message}"
        return Screen(body, [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]], "new_order_other")

    def _handle_new_order_other_callback(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_new_order_assets(chat_key)
        return self._render_other_prompt(chat_key)

    def _handle_other_symbol_text(self, chat_key: Tuple[Any, ...], text: str) -> Screen:
        query = self._normalize_symbol_query(text)
        if not query:
            return self._render_other_prompt(chat_key, "Please enter a base asset or spot pair.")
        pairs = self._find_pairs(chat_key, query)
        if not pairs:
            return self._render_other_prompt(chat_key, f"No API-enabled tradable MEXC spot pair found for {query}.")
        state = self._state_for(chat_key)
        state.selected_base = str(pairs[0].get("baseAsset") or query).upper()
        state.pair_candidates = pairs
        state.pair_page = 0
        return self._render_pair_picker(chat_key)

    def _normalize_symbol_query(self, value: str) -> str:
        return (value or "").strip().upper().replace(" ", "").replace("-", "/")

    def _find_pairs(self, chat_key: Tuple[Any, ...], query: str) -> List[Dict[str, Any]]:
        normalized = query.replace("/", "")
        instruments = self._list_instruments(chat_key, normalized)
        matches: List[Dict[str, Any]] = []
        for item in instruments:
            symbol = str(item.get("symbol") or "").upper()
            base = str(item.get("baseAsset") or "").upper()
            quote = str(item.get("quoteAsset") or "").upper()
            if "/" in query:
                is_match = symbol == normalized
            elif symbol == normalized and base and quote:
                is_match = True
            else:
                is_match = base == normalized
            if is_match and self._is_selectable_instrument(item):
                matches.append(self._with_market_price(chat_key, item))
        return matches

    def _render_pair_picker_for_base(self, chat_key: Tuple[Any, ...], base: str) -> Screen:
        state = self._state_for(chat_key)
        state.selected_base = base.upper()
        state.pair_candidates = self._find_pairs(chat_key, base)
        state.pair_page = 0
        if not state.pair_candidates:
            return Screen(
                f"🟦 MEXC Spot — {base.upper()}\n\nNo API-enabled tradable spot pairs found.",
                [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]],
                "new_order_pairs",
            )
        return self._render_pair_picker(chat_key)

    def _list_instruments(self, chat_key: Tuple[Any, ...], query: str = "") -> List[Dict[str, Any]]:
        state = self._state_for(chat_key)
        response = self._desk.execute({
            "operation": "list_instruments",
            "exchange": state.exchange or "",
            "account": state.account or "",
            "query": query,
            "limit": 5000,
        })
        data = getattr(response, "data", None)
        rows = data.get("instruments") if response.success and isinstance(data, dict) else []
        return [dict(row) for row in rows if isinstance(row, Mapping)]

    def _is_selectable_instrument(self, item: Mapping[str, Any]) -> bool:
        symbol = str(item.get("symbol") or "").strip()
        base = str(item.get("baseAsset") or "").strip()
        quote = str(item.get("quoteAsset") or "").strip()
        status = str(item.get("status") or "").upper()
        api_enabled = item.get("api_enabled_for_key", True)
        api_eligible = item.get("api_eligible", True)
        spot_allowed = bool(item.get("isSpotTradingAllowed"))
        return bool(symbol and base and quote and api_enabled and api_eligible and spot_allowed and status in {"1", "ENABLED", "TRADING"})

    def _with_market_price(self, chat_key: Tuple[Any, ...], item: Mapping[str, Any]) -> Dict[str, Any]:
        out = dict(item)
        state = self._state_for(chat_key)
        symbol = str(out.get("symbol") or "").upper()
        try:
            response = self._desk.execute({
                "operation": "market_price",
                "exchange": state.exchange or "",
                "account": state.account or "",
                "symbol": symbol,
            })
            data = getattr(response, "data", None)
            price = None
            if response.success and response.market_price is not None:
                price = str(response.market_price.price or response.market_price.mark_price or "")
            if not price and isinstance(data, dict):
                price = str(data.get("price") or "")
            if price:
                out["market_price"] = self._format_price(price, out)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tradespot price lookup failed for %s: %s", symbol, exc)
        return out

    def _format_price(self, price: Any, item: Mapping[str, Any]) -> str:
        raw = str(price or "").strip()
        try:
            dec = Decimal(raw)
        except (InvalidOperation, ValueError):
            return raw
        quote_precision = item.get("quotePrecision")
        try:
            places = int(quote_precision)
        except (TypeError, ValueError):
            places = 8 if dec < Decimal("1") else 4
        places = max(0, min(10, places))
        text = f"{dec:.{places}f}"
        if places > 4 and "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"

    def _pair_label(self, item: Mapping[str, Any]) -> str:
        base = str(item.get("baseAsset") or "").upper()
        quote = str(item.get("quoteAsset") or "").upper()
        price = str(item.get("market_price") or "").strip()
        label = f"{base}/{quote}" if base and quote else str(item.get("symbol") or "")
        return f"{label}   {price}" if price else f"{label}   price unavailable"

    def _render_pair_picker(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "new_order_pairs"
        base = state.selected_base or "Asset"
        total = len(state.pair_candidates)
        pages = max(1, (total + _PAIR_PAGE_SIZE - 1) // _PAIR_PAGE_SIZE)
        state.pair_page = max(0, min(state.pair_page, pages - 1))
        start = state.pair_page * _PAIR_PAGE_SIZE
        page_items = state.pair_candidates[start : start + _PAIR_PAGE_SIZE]
        rows = [[_button_row(self._pair_label(item), f"pair:{start + idx}")] for idx, item in enumerate(page_items)]
        if pages > 1:
            nav = []
            if state.pair_page > 0:
                nav.append(_button_row("◀️", "page:prev"))
            nav.append(_button_row(f"{state.pair_page + 1}/{pages}", "noop"))
            if state.pair_page + 1 < pages:
                nav.append(_button_row("▶️", "page:next"))
            rows.append(nav)
        rows.append([_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)])
        return Screen(f"🟦 MEXC Spot — {base}\n\nSelect Pair:", rows, "new_order_pairs")

    def _handle_pair_selection(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self._render_new_order_assets(chat_key)
        if suffix == "page:prev":
            state.pair_page -= 1
            return self._render_pair_picker(chat_key)
        if suffix == "page:next":
            state.pair_page += 1
            return self._render_pair_picker(chat_key)
        if not suffix.startswith("pair:"):
            return self._render_pair_picker(chat_key)
        try:
            idx = int(suffix[len("pair:") :])
        except ValueError:
            return self._render_pair_picker(chat_key)
        if idx < 0 or idx >= len(state.pair_candidates):
            return self._render_pair_picker(chat_key)
        state.selected_instrument = dict(state.pair_candidates[idx])
        state.order_side = None
        state.order_quantity = None
        state.order_limit_price = None
        return self._render_side_picker(chat_key)

    def _selected_pair_name(self, state: SpotWizardState) -> str:
        item = state.selected_instrument or {}
        base = str(item.get("baseAsset") or "").upper()
        quote = str(item.get("quoteAsset") or "").upper()
        return f"{base}/{quote}" if base and quote else str(item.get("symbol") or "")

    def _render_side_picker(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "new_order_side"
        item = state.selected_instrument or {}
        pair = self._selected_pair_name(state)
        quote = str(item.get("quoteAsset") or "").upper()
        price = str(item.get("market_price") or "price unavailable")
        rows = [[_button_row("🟢 BUY", "side:buy"), _button_row("🔴 SELL", "side:sell")], [_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]]
        return Screen(f"🟦 MEXC Spot — {pair}\nMarket: {price} {quote}\n\nSelect Side:", rows, "new_order_side")

    def _handle_side_selection(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_pair_picker(chat_key)
        if suffix not in {"side:buy", "side:sell"}:
            return self._render_side_picker(chat_key)
        state = self._state_for(chat_key)
        state.order_side = suffix.split(":", 1)[1]
        state.order_quantity = None
        state.order_limit_price = None
        return self._render_quantity_prompt(chat_key)

    def _render_quantity_prompt(self, chat_key: Tuple[Any, ...], message: str = "") -> Screen:
        state = self._state_for(chat_key)
        state.state = "new_order_qty"
        item = state.selected_instrument or {}
        base = str(item.get("baseAsset") or "BASE").upper()
        pair = self._selected_pair_name(state)
        side = str(state.order_side or "").upper()
        body = f"🟦 {pair} — {side}\n\nEnter LIMIT quantity in {base}:"
        if message:
            body += f"\n\n{message}"
        return Screen(body, [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]], "new_order_qty")

    def _handle_quantity_callback(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_side_picker(chat_key)
        return self._render_quantity_prompt(chat_key)

    def _valid_decimal_text(self, value: str) -> bool:
        try:
            return Decimal(value) > 0
        except (InvalidOperation, ValueError):
            return False

    def _handle_quantity_text(self, chat_key: Tuple[Any, ...], text: str) -> Screen:
        value = text.strip()
        if not self._valid_decimal_text(value):
            return self._render_quantity_prompt(chat_key, "Enter a positive numeric quantity.")
        state = self._state_for(chat_key)
        state.order_quantity = value
        return self._render_limit_price_prompt(chat_key)

    def _render_limit_price_prompt(self, chat_key: Tuple[Any, ...], message: str = "") -> Screen:
        state = self._state_for(chat_key)
        state.state = "new_order_price"
        item = state.selected_instrument or {}
        pair = self._selected_pair_name(state)
        side = str(state.order_side or "").upper()
        base = str(item.get("baseAsset") or "BASE").upper()
        quote = str(item.get("quoteAsset") or "QUOTE").upper()
        market = str(item.get("market_price") or "price unavailable")
        body = (
            f"🟦 {pair} — {side}\n\n"
            f"Quantity: {state.order_quantity} {base}\n"
            f"Market: {market} {quote}\n\n"
            f"Enter LIMIT price in {quote}:"
        )
        if message:
            body += f"\n\n{message}"
        return Screen(body, [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]], "new_order_price")

    def _handle_limit_price_callback(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_quantity_prompt(chat_key)
        return self._render_limit_price_prompt(chat_key)

    def _handle_limit_price_text(self, chat_key: Tuple[Any, ...], text: str) -> Screen:
        value = text.strip()
        if not self._valid_decimal_text(value):
            return self._render_limit_price_prompt(chat_key, "Enter a positive numeric limit price.")
        state = self._state_for(chat_key)
        state.order_limit_price = value
        return self._render_order_preview(chat_key)

    def _format_decimal(self, value: Decimal) -> str:
        text = format(value.normalize(), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    def _decimal_step(self, value: Any) -> Decimal:
        text = str(value or "").strip()
        if not text:
            return Decimal("0")
        try:
            inc = Decimal(text)
        except (InvalidOperation, ValueError):
            return Decimal("0")
        return inc if inc > 0 else Decimal("0")

    def _quantize_down(self, value: Decimal, increment: Decimal) -> Decimal:
        if increment <= 0:
            return value
        steps = (value / increment).to_integral_value(rounding=ROUND_DOWN)
        return steps * increment

    def _size_increment(self, item: Mapping[str, Any]) -> Decimal:
        for key in ("size_step", "size_increment"):
            inc = self._decimal_step(item.get(key))
            if inc > 0:
                return inc
        return Decimal("0")

    def _price_increment(self, item: Mapping[str, Any]) -> Decimal:
        for key in ("price_tick", "price_increment"):
            inc = self._decimal_step(item.get(key))
            if inc > 0:
                return inc
        return Decimal("0")

    def _constraint_error(
        self,
        item: Mapping[str, Any],
        qty: Decimal,
        price: Decimal,
        side: str,
        base: str,
        quote: str,
    ) -> Optional[str]:
        if self._size_increment(item) <= 0 or self._price_increment(item) <= 0:
            return "Instrument trading constraints are unavailable."
        min_qty = self._decimal_step(item.get("min_qty") or item.get("minimum_size"))
        max_qty = self._decimal_step(item.get("max_qty"))
        min_notional = self._decimal_step(item.get("min_notional"))
        if qty <= 0 or (min_qty > 0 and qty < min_qty):
            if min_qty > 0:
                return f"Minimum quantity: {self._format_decimal(min_qty)} {base}"
            return "Invalid quantity or price."
        if max_qty > 0 and qty > max_qty:
            return f"Maximum quantity: {self._format_decimal(max_qty)} {base}"
        if price <= 0:
            return "Invalid quantity or price."
        if side == "BUY" and min_notional > 0 and (qty * price) < min_notional:
            return f"Minimum order value: {self._format_decimal(min_notional)} {quote}"
        return None

    def _re_resolve_instrument(self, state: SpotWizardState) -> Optional[Dict[str, Any]]:
        item = dict(state.selected_instrument or {})
        symbol = str(item.get("symbol") or "").upper()
        response = self._desk.execute(
            {
                "operation": "resolve_instrument",
                "exchange": state.exchange or "",
                "account": state.account or "",
                "symbol": symbol,
            }
        )
        data = getattr(response, "data", None)
        resolved = None
        if response.success:
            if getattr(response, "instrument", None) is not None:
                resolved = dict(response.instrument.to_dict())  # type: ignore[union-attr]
            if isinstance(data, dict):
                inst = data.get("instrument")
                if isinstance(inst, dict):
                    resolved = {**(resolved or {}), **inst}
        if resolved:
            item.update(resolved)
            state.selected_instrument = item
            return item
        return item if item else None

    def _holding_for(self, state: SpotWizardState, asset: str) -> Decimal:
        response = self._desk.execute(
            {
                "operation": "balance",
                "exchange": state.exchange or "",
                "account": state.account or "",
            }
        )
        totals: Dict[str, Decimal] = {}
        for row in _balance_assets(response):
            if not isinstance(row, Mapping):
                continue
            symbol = _asset_symbol(row)
            if not symbol:
                continue
            totals[symbol] = totals.get(symbol, Decimal("0")) + _asset_total_decimal(row)
        return totals.get(str(asset).upper(), Decimal("0"))

    def _instrument_tradable(self, item: Mapping[str, Any]) -> Optional[str]:
        if item.get("api_enabled_for_key") is False or item.get("api_eligible") is False:
            return "Symbol is not API-enabled for this key."
        if item.get("isSpotTradingAllowed") is False:
            return "Symbol is not enabled for spot trading."
        order_types = {str(x).upper() for x in (item.get("orderTypes") or [])}
        if order_types and "LIMIT" not in order_types:
            return "LIMIT is not an allowed order type for this symbol."
        return None

    def _render_order_preview(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "new_order_preview"
        state.confirm_consumed = False
        state.last_submit_screen = None
        item = self._re_resolve_instrument(state) or {}
        item = dict(item)
        size_step = self._size_increment(item)
        price_step = self._price_increment(item)
        pair = self._selected_pair_name(state)
        side = str(state.order_side or "").upper()
        base = str(item.get("base") or item.get("baseAsset") or "BASE").upper()
        quote = str(item.get("quote") or item.get("quoteAsset") or "QUOTE").upper()
        try:
            qty = Decimal(str(state.order_quantity or "0"))
            price = Decimal(str(state.order_limit_price or "0"))
        except (InvalidOperation, ValueError):
            qty = Decimal("0")
            price = Decimal("0")
        qty = self._quantize_down(qty, size_step)
        price = self._quantize_down(price, price_step)
        state.order_quantity = self._format_decimal(qty)
        state.order_limit_price = self._format_decimal(price)
        quote_like = quote in _MEXC_QUOTE_ASSETS
        required = (qty * price) if side == "BUY" else qty
        required_asset = quote if side == "BUY" else base
        available = self._holding_for(state, required_asset)
        after = available - required
        tradable_error = self._instrument_tradable(item)
        constraint_error = self._constraint_error(item, qty, price, side, base, quote)
        lines = [
            f"🟦 MEXC Spot — {pair}",
            "LIMIT order preview",
            "",
            f"Side: {side}",
            f"Quantity: {state.order_quantity} {base}",
            f"Limit price: {state.order_limit_price} {quote}",
            "",
        ]
        if side == "BUY":
            lines.extend(
                [
                    f"Required: {_format_inventory_amount(required, quote=quote_like)} {quote}",
                    f"Available: {_format_inventory_amount(available, quote=quote_like)} {quote}",
                    f"After order: {_format_inventory_amount(after, quote=quote_like)} {quote}",
                ]
            )
        else:
            lines.extend(
                [
                    f"Required: {_format_inventory_amount(required, quote=False)} {base}",
                    f"Available: {_format_inventory_amount(available, quote=False)} {base}",
                    f"After order: {_format_inventory_amount(after, quote=False)} {base}",
                ]
            )
        buttons: List[List[Dict[str, str]]]
        if constraint_error:
            lines.extend(["", constraint_error])
            state.confirm_token = None
            buttons = [[_button_row(*BUTTON_BACK), _button_row("Cancel", "cancel")]]
        elif tradable_error:
            lines.extend(["", tradable_error])
            state.confirm_token = None
            buttons = [[_button_row(*BUTTON_BACK), _button_row("Cancel", "cancel")]]
        elif after < 0:
            lines.extend(["", f"Insufficient {required_asset}."])
            state.confirm_token = None
            buttons = [[_button_row(*BUTTON_BACK), _button_row("Cancel", "cancel")]]
        else:
            token = secrets.token_hex(8)
            state.confirm_token = token
            buttons = [
                [_button_row("Confirm & Place Order", f"place:{token}")],
                [_button_row(*BUTTON_BACK), _button_row("Cancel", "cancel")],
            ]
        return Screen("\n".join(lines), buttons, "new_order_preview")

    def _handle_preview_callback(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self._render_limit_price_prompt(chat_key)
        if suffix in {"cancel", "close"}:
            self._clear_order_state(state)
            return self._render_action(chat_key)
        if suffix == "confirm_disabled":
            return self._render_order_preview(chat_key)
        if suffix.startswith("place:"):
            return self._submit_limit_order(chat_key, suffix[len("place:") :])
        return self._render_order_preview(chat_key)

    def _submit_limit_order(self, chat_key: Tuple[Any, ...], token: str) -> Screen:
        state = self._state_for(chat_key)
        if not token or token != (state.confirm_token or ""):
            return self._render_order_preview(chat_key)
        if state.confirm_consumed:
            if state.last_submit_screen is not None:
                return state.last_submit_screen
            return Screen(
                "🟦 MEXC Spot\n\nThis confirmation was already used. No additional order was submitted.",
                [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]],
                "order_result",
            )
        state.confirm_consumed = True
        item = self._re_resolve_instrument(state) or {}
        item = dict(item)
        size_step = self._size_increment(item)
        price_step = self._price_increment(item)
        tradable_error = self._instrument_tradable(item)
        if tradable_error:
            screen = Screen(
                f"🟦 MEXC Spot\n\n{tradable_error}\nNo order was placed.",
                [[_button_row(*BUTTON_BACK), _button_row("Cancel", "cancel")]],
                "order_result",
            )
            state.last_submit_screen = screen
            state.state = "order_result"
            return screen
        side = str(state.order_side or "").upper()
        base = str(item.get("base") or item.get("baseAsset") or "").upper()
        quote = str(item.get("quote") or item.get("quoteAsset") or "").upper()
        try:
            qty = Decimal(str(state.order_quantity or "0"))
            price = Decimal(str(state.order_limit_price or "0"))
        except (InvalidOperation, ValueError):
            qty = Decimal("0")
            price = Decimal("0")
        qty = self._quantize_down(qty, size_step)
        price = self._quantize_down(price, price_step)
        if size_step <= 0 or price_step <= 0:
            screen = Screen(
                "🟦 MEXC Spot\n\nInstrument trading constraints are unavailable.\nNo order was placed.",
                [[_button_row(*BUTTON_BACK), _button_row("Cancel", "cancel")]],
                "order_result",
            )
            state.last_submit_screen = screen
            state.state = "order_result"
            return screen
        if qty <= 0 or price <= 0:
            screen = Screen(
                "🟦 MEXC Spot\n\nInvalid quantity or price.\nNo order was placed.",
                [[_button_row(*BUTTON_BACK), _button_row("Cancel", "cancel")]],
                "order_result",
            )
            state.last_submit_screen = screen
            state.state = "order_result"
            return screen
        required_asset = quote if side == "BUY" else base
        required = (qty * price) if side == "BUY" else qty
        available = self._holding_for(state, required_asset)
        if available < required:
            screen = Screen(
                f"🟦 MEXC Spot\n\nInsufficient {required_asset}.\nNo order was placed.",
                [[_button_row(*BUTTON_BACK), _button_row("Cancel", "cancel")]],
                "order_result",
            )
            state.last_submit_screen = screen
            state.state = "order_result"
            return screen
        pair = self._selected_pair_name(state)
        response = self._desk.execute(
            {
                "operation": "new_order",
                "exchange": state.exchange or "",
                "account": state.account or "",
                "symbol": str(item.get("symbol") or ""),
                "side": side,
                "order_type": "LIMIT",
                "quantity": self._format_decimal(qty),
                "price": self._format_decimal(price),
                "client_order_id": token,
            }
        )
        if not response.success:
            err = getattr(response, "error", None)
            code = getattr(err, "code", "") if err is not None else ""
            message = getattr(err, "message", "Order was not placed.") if err is not None else "Order was not placed."
            title = "⚠️ Order status unknown" if code == "ORDER_STATUS_UNKNOWN" else "❌ LIMIT order failed"
            extra = "\nNot retried." if code == "ORDER_STATUS_UNKNOWN" else "\nNo order was placed."
            screen = Screen(
                f"{title}\n\n{message}{extra}",
                [[_button_row(*BUTTON_BACK), _button_row("Cancel", "cancel")]],
                "order_result",
            )
            state.last_submit_screen = screen
            state.state = "order_result"
            return screen
        order = getattr(response, "order", None)
        order_id = getattr(order, "exchange_order_id", None) if order is not None else None
        if order_id is None:
            data = getattr(response, "data", None)
            if isinstance(data, dict):
                order_id = data.get("orderId")
        status = getattr(order, "status", None) if order is not None else None
        if not status:
            data = getattr(response, "data", None)
            if isinstance(data, dict):
                status = data.get("status")
        notional = qty * price
        quote_like = quote in _MEXC_QUOTE_ASSETS
        screen = Screen(
            "\n".join(
                [
                    "✅ LIMIT order submitted",
                    "",
                    f"MEXC — {state.account or ''}",
                    f"{side} {self._format_decimal(qty)} {pair}",
                    f"Price: {self._format_decimal(price)} {quote}",
                    f"Estimated value: {_format_inventory_amount(notional, quote=quote_like)} {quote}",
                    "",
                    f"Order ID: {order_id if order_id is not None else '—'}",
                    f"Status: {status or 'NEW'}",
                ]
            ),
            [
                [_button_row("New Order", "new_order")],
                [_button_row("Open Orders", "orders")],
                [_button_row(*BUTTON_BACK)],
            ],
            "order_result",
        )
        state.last_submit_screen = screen
        state.state = "order_result"
        return screen

    def _handle_order_result_callback(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix == "back":
            return self._render_action(chat_key)
        if suffix in {"cancel", "close"}:
            self._clear_order_state(state)
            return self._render_action(chat_key)
        if suffix == "new_order":
            return self._render_new_order_assets(chat_key)
        if suffix == "orders":
            return self._render_orders(chat_key)
        if state.last_submit_screen is not None:
            return state.last_submit_screen
        return self._render_action(chat_key)

    def _result_buttons(self) -> List[List[Dict[str, str]]]:
        return [[_button_row(*BUTTON_REFRESH)], [_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]]

    def _render_balance(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "balance"
        exchange = state.exchange or ""
        account = state.account or ""
        response = self._desk.execute({"operation": "balance", "exchange": exchange, "account": account})
        if response.success and (response.balance is not None or _balance_assets(response)):
            lines = [
                "🟦 Spot Trading",
                "💰 Balance",
                "",
                f"Exchange: {exchange}",
                f"Account: {account}",
                "",
            ]
            assets = _balance_assets(response)
            if str(exchange).lower() == "mexc":
                lines.extend(["Assets", *_mexc_inventory_lines(assets)])
            else:
                if response.balance is not None:
                    lines.append(f"Balance: {response.balance.value} {response.balance.unit}")
                extra_lines = []
                for item in assets[:20]:
                    if not isinstance(item, Mapping):
                        continue
                    symbol = _asset_symbol(item)
                    amount = _asset_total_decimal(item)
                    if symbol and amount > 0:
                        extra_lines.append(f"• {symbol}: {_format_inventory_amount(amount, quote=False)}")
                if extra_lines:
                    lines.extend(["", "Assets", *extra_lines])
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

    def _fetch_open_order_rows(self, state: SpotWizardState) -> List[Any]:
        exchange = state.exchange or ""
        account = state.account or ""
        caps = set(self._desk.capabilities(exchange) or [])
        operation = "open_orders" if "open_orders" in caps else "orders"
        response = self._desk.execute({"operation": operation, "exchange": exchange, "account": account})
        if not response.success:
            return []
        data = getattr(response, "data", None)
        rows = data.get("orders") if isinstance(data, dict) else None
        return list(rows) if isinstance(rows, list) else []

    def _side_dot(self, side: str) -> str:
        return "🔵" if str(side).upper() == "BUY" else "🔴"

    def _group_body_lines(self, group: Mapping[str, Any], *, confirm: bool) -> List[str]:
        side = str(group.get("side") or "")
        pair = str(group.get("pair") or "")
        count = int(group.get("order_count") or 0)
        base = str(group.get("base") or "")
        quote = str(group.get("quote") or "")
        qty = group.get("total_qty") if isinstance(group.get("total_qty"), Decimal) else _to_amount(group.get("total_qty"))
        min_price = group.get("min_price") if isinstance(group.get("min_price"), Decimal) else _to_amount(group.get("min_price"))
        max_price = group.get("max_price") if isinstance(group.get("max_price"), Decimal) else _to_amount(group.get("max_price"))
        vwap = group.get("vwap") if isinstance(group.get("vwap"), Decimal) else _to_amount(group.get("vwap"))
        title = f"{self._side_dot(side)} {side} {pair}" if confirm else f"{self._side_dot(side)} {pair}"
        count_line = f"Orders: {count}" if confirm or count == 1 else f"{count} Orders"
        lines = [title, count_line, f"Total Volume: {_format_spot_number(qty)} {base}"]
        if count == 1:
            lines.append(f"Price: {_format_spot_number(min_price or max_price)} {quote}")
        else:
            lines.append(f"Price Range: {_format_spot_number(min_price)} → {_format_spot_number(max_price)} {quote}")
        lines.append(f"VWAP: {_format_spot_number(vwap)} {quote}")
        return lines

    def _find_group(self, groups: List[Dict[str, Any]], side: str, symbol: str) -> Optional[Dict[str, Any]]:
        side_u = str(side or "").upper()
        compact = str(symbol or "").upper().replace("/", "")
        for group in groups:
            if str(group.get("side") or "").upper() == side_u and str(group.get("symbol") or "").upper().replace("/", "") == compact:
                return group
        return None

    def _render_cancel_orders(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "cancel_orders"
        state.confirm_token = None
        state.confirm_consumed = False
        exchange = state.exchange or ""
        account = state.account or ""
        groups = _group_open_limit_orders(self._fetch_open_order_rows(state))
        lines = [
            "🟦 Spot Trading",
            "❌ Cancel Orders",
            "",
            f"Exchange: {exchange}",
            f"Account: {account}",
            "",
        ]
        buttons: List[List[Dict[str, str]]] = []
        if not groups:
            lines.append("No open LIMIT orders.")
        else:
            blocks = []
            for group in groups:
                blocks.append("\n".join(self._group_body_lines(group, confirm=False)))
                label = f"{self._side_dot(group['side'])} {group['pair']} · {group['order_count']}"
                buttons.append([_button_row(label, f"cg:{group['side']}:{group['symbol']}")])
            lines.append("\n\n".join(blocks))
        buttons.append([_button_row(*BUTTON_BACK_RETURN)])
        return Screen("\n".join(lines).rstrip(), buttons, "cancel_orders")

    def _handle_cancel_list_callback(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_action(chat_key)
        if suffix.startswith("cg:"):
            parts = suffix.split(":")
            if len(parts) >= 3:
                return self._render_cancel_confirm(chat_key, parts[1], parts[2])
        return self._render_cancel_orders(chat_key)

    def _render_cancel_confirm(self, chat_key: Tuple[Any, ...], side: str, symbol: str) -> Screen:
        state = self._state_for(chat_key)
        state.state = "cancel_confirm"
        side_u = str(side or "").upper()
        compact = str(symbol or "").upper().replace("/", "")
        groups = _group_open_limit_orders(self._fetch_open_order_rows(state))
        group = self._find_group(groups, side_u, compact)
        if group is None:
            state.confirm_token = None
            return Screen(
                "🟦 MEXC Spot — Cancel Orders\n\nNo matching open LIMIT orders remain.",
                [[_button_row(*BUTTON_BACK_RETURN)]],
                "cancel_confirm",
            )
        state.cancel_side = side_u
        state.cancel_symbol = str(group["symbol"])
        state.cancel_pair = str(group["pair"])
        token = secrets.token_hex(8)
        state.confirm_token = token
        state.confirm_consumed = False
        count = int(group["order_count"])
        lines = [
            "🟦 MEXC Spot — Cancel Orders",
            "",
            *self._group_body_lines(group, confirm=True),
            "",
            f"This will cancel ALL currently open {side_u} {group['pair']} limit orders.",
        ]
        buttons = [
            [_button_row(f"❌ Cancel {count} Orders", f"cx:{token}")],
            [_button_row(*BUTTON_BACK_RETURN)],
        ]
        return Screen("\n".join(lines), buttons, "cancel_confirm")

    def _handle_cancel_confirm_callback(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_cancel_orders(chat_key)
        if suffix.startswith("cx:"):
            return self._submit_cancel_orders(chat_key, suffix[len("cx:") :])
        state = self._state_for(chat_key)
        return self._render_cancel_confirm(chat_key, state.cancel_side or "", state.cancel_symbol or "")

    def _submit_cancel_orders(self, chat_key: Tuple[Any, ...], token: str) -> Screen:
        state = self._state_for(chat_key)
        if not token or token != (state.confirm_token or ""):
            return self._render_cancel_confirm(chat_key, state.cancel_side or "", state.cancel_symbol or "")
        if state.confirm_consumed:
            if state.last_submit_screen is not None:
                return state.last_submit_screen
            return Screen(
                "🟦 MEXC Spot — Cancel Orders\n\nThis confirmation was already used. No additional cancellation was sent.",
                [[_button_row(*BUTTON_BACK_RETURN)]],
                "cancel_result",
            )
        state.confirm_consumed = True
        side = str(state.cancel_side or "").upper()
        symbol = str(state.cancel_symbol or "").upper().replace("/", "")
        pair = str(state.cancel_pair or _display_pair_from_symbol(symbol))
        groups = _group_open_limit_orders(self._fetch_open_order_rows(state))
        group = self._find_group(groups, side, symbol)
        order_ids = list(group.get("order_ids") or []) if group else []
        if not order_ids:
            screen = self._cancel_result_screen(state, pair, side, requested=0, cancelled=0, remaining=0, unknown=False)
            state.last_submit_screen = screen
            state.state = "cancel_result"
            return screen
        response = self._desk.execute(
            {
                "operation": "cancel_orders",
                "exchange": state.exchange or "",
                "account": state.account or "",
                "symbol": symbol,
                "side": side,
                "order_ids": order_ids,
            }
        )
        err = getattr(response, "error", None)
        code = getattr(err, "code", "") if err is not None else ""
        if code == "CANCEL_STATUS_UNKNOWN" or (not response.success and code == "CANCEL_STATUS_UNKNOWN"):
            screen = self._cancel_unknown_screen(state, pair, side, len(order_ids))
            state.last_submit_screen = screen
            state.state = "cancel_result"
            return screen
        refreshed = _group_open_limit_orders(self._fetch_open_order_rows(state))
        remaining_group = self._find_group(refreshed, side, symbol)
        remaining = int(remaining_group["order_count"]) if remaining_group else 0
        still = set(remaining_group.get("order_ids") or []) if remaining_group else set()
        cancelled = len(set(order_ids) - still)
        data = getattr(response, "data", None) if response.success else None
        if isinstance(data, dict):
            if data.get("cancelled") is not None:
                cancelled = int(data.get("cancelled") or cancelled)
            if data.get("remaining") is not None:
                remaining = int(data.get("remaining") or remaining)
        screen = self._cancel_result_screen(
            state,
            pair,
            side,
            requested=len(order_ids),
            cancelled=cancelled,
            remaining=remaining,
            unknown=False,
        )
        state.last_submit_screen = screen
        state.state = "cancel_result"
        return screen

    def _cancel_unknown_screen(self, state: SpotWizardState, pair: str, side: str, requested: int) -> Screen:
        dot = self._side_dot(side)
        text = "\n".join(
            [
                "🟦 Spot Trading",
                "⚠️ Cancellation status unknown",
                "",
                f"Exchange: {state.exchange or ''}",
                f"Account: {state.account or ''}",
                "",
                f"{dot} {pair}",
                "",
                f"Requested: {requested}",
                "Not retried.",
            ]
        )
        return Screen(text, self._cancel_result_buttons(), "cancel_result")

    def _cancel_result_screen(
        self,
        state: SpotWizardState,
        pair: str,
        side: str,
        *,
        requested: int,
        cancelled: int,
        remaining: int,
        unknown: bool,
    ) -> Screen:
        complete = remaining == 0 and cancelled == requested and requested > 0
        title = "✅ Orders Cancelled" if complete else "⚠️ Cancellation incomplete"
        dot = self._side_dot(side)
        text = "\n".join(
            [
                "🟦 Spot Trading",
                title,
                "",
                f"Exchange: {state.exchange or ''}",
                f"Account: {state.account or ''}",
                "",
                f"{dot} {pair}",
                "",
                f"Requested: {requested}",
                f"Cancelled: {cancelled}",
                f"Remaining: {remaining}",
            ]
        )
        return Screen(text, self._cancel_result_buttons(), "cancel_result")

    def _cancel_result_buttons(self) -> List[List[Dict[str, str]]]:
        return [
            [_button_row("Cancel Orders", "cancel_orders")],
            [_button_row("Orders", "orders")],
            [_button_row("Main Menu", "main")],
        ]

    def _handle_cancel_result_callback(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        state = self._state_for(chat_key)
        if suffix in {"back", "main"}:
            return self._render_action(chat_key)
        if suffix in {"cancel_orders", "action:cancel_orders"}:
            return self._render_cancel_orders(chat_key)
        if suffix == "orders":
            return self._render_orders(chat_key)
        if state.last_submit_screen is not None:
            return state.last_submit_screen
        return self._render_action(chat_key)

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
