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
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Mapping, Optional, Tuple, cast

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
_MUTATING_ACTIONS = {"ladder", "cancel_orders"}
_QUICK_PICK_BASE_ASSETS = ("SOL", "ETH", "HYPE", "SUI")
_PAIR_PAGE_SIZE = 8
_MEXC_QUOTE_ASSETS = ("USDT", "USDC")
_QUOTE_CENTS = Decimal("0.01")


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
        state = self._state_for(chat_key)
        value = (text or "").strip()
        if state.state == "new_order_other":
            return self._handle_other_symbol_text(chat_key, value)
        if state.state == "new_order_qty":
            return self._handle_quantity_text(chat_key, value)
        if state.state == "new_order_price":
            return self._handle_limit_price_text(chat_key, value)
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

    def _render_order_preview(self, chat_key: Tuple[Any, ...]) -> Screen:
        state = self._state_for(chat_key)
        state.state = "new_order_preview"
        item = state.selected_instrument or {}
        pair = self._selected_pair_name(state)
        side = str(state.order_side or "").upper()
        base = str(item.get("baseAsset") or "BASE").upper()
        quote = str(item.get("quoteAsset") or "QUOTE").upper()
        qty = Decimal(str(state.order_quantity or "0"))
        price = Decimal(str(state.order_limit_price or "0"))
        notional = qty * price
        if side == "BUY":
            balance_line = f"Required balance: {quote}"
            estimate_line = f"Estimated cost: {self._format_decimal(notional)} {quote}"
        else:
            balance_line = f"Required balance: {base}"
            estimate_line = f"Estimated proceeds: {self._format_decimal(notional)} {quote}"
        lines = [
            f"🟦 MEXC Spot — {pair}",
            "LIMIT order preview",
            "",
            f"Side: {side}",
            f"Quantity: {state.order_quantity} {base}",
            f"Limit price: {state.order_limit_price} {quote}",
            balance_line,
            estimate_line,
            "",
            "Live submission is disabled in this phase.",
        ]
        return Screen("\n".join(lines), [[_button_row("Confirm", "confirm_disabled")], [_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]], "new_order_preview")

    def _format_decimal(self, value: Decimal) -> str:
        text = format(value.normalize(), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    def _handle_preview_callback(self, chat_key: Tuple[Any, ...], suffix: str) -> Screen:
        if suffix == "back":
            return self._render_limit_price_prompt(chat_key)
        if suffix == "confirm_disabled":
            return Screen(
                "🟦 MEXC Spot\n\nLive order submission is disabled. No order was placed.",
                [[_button_row(*BUTTON_BACK), _button_row(*BUTTON_CLOSE)]],
                "new_order_preview",
            )
        return self._render_order_preview(chat_key)

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
