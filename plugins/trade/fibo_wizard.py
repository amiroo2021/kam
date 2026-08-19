"""Telegram /fibo wizard — GoldenFibo v1.

Wizard flow:
  ▶️ Start Fibo
  → Exchange (same TradeDesk discovery as /trade)
  → Account (same TradeDesk account discovery as /trade)
  → GoldenFibo support check (UI-level; unsupported → clean message)
  → Instrument (selection or manual input)
  → BUY / SELL
  → Step0 Volume (default 0.01)
  → Percentage (default 0.01)
  → Review (showing V0..V20 + cumulative exposure)
  → START
  🔵 Running Fibo  → LIST / DETAIL
  🛑 STOP Fibo     → STOP a single registration

Discovery reuses TradeDesk (shared with /trade). Strategy math lives in
`plugins.trade.golden_fibo.config`. Runtime support is still gated by
`fibo_service.SUPPORTED_EXCHANGES` — discovery and execution are separate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Protocol, Tuple
from urllib.parse import quote, unquote

from .tradedesk import TradeDesk, get_tradedesk
from .fibo_service import (
    FiboServiceProtocol,
    SUPPORTED_EXCHANGES,
    get_fibo_service,
)
from .golden_fibo.config import (
    golden_fibo_cumulative_volume,
    golden_fibo_volume,
)
from .wizard import _account_option_parts

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Button labels
# ---------------------------------------------------------------------------
BUTTON_EXIT = ("✕ Exit", "exit")
BUTTON_BACK = ("◀️ Back", "back")
BUTTON_REFRESH = ("🔄 Refresh", "refresh")
BUTTON_START = ("▶️ Start Fibo", "menu:start")
BUTTON_RUNNING = ("🔵 Running Fibo", "menu:running")
BUTTON_STOP = ("🛑 STOP Fibo", "menu:stop")
BUTTON_CONFIRM_START = ("▶️ Start Fibo", "confirm_start")
BUTTON_CANCEL = ("Cancel", "back")
BUTTON_BUY = ("BUY", "direction:BUY")
BUTTON_SELL = ("SELL", "direction:SELL")
BUTTON_OTHER_SYMBOL = ("Other instrument...", "instrument:other")
BUTTON_DEFAULT_STEP0 = ("0.01", "step0:0.01")
BUTTON_DEFAULT_STEP0_BTC = ("0.0001", "step0:0.0001")

_DEFAULT_PERCENTAGE = "0.01"
_DEFAULT_STEP0_VOLUME = "0.01"
_DEFAULT_BTC_STEP0_VOLUME = "0.0001"

# Common instruments that the user can pick quickly. Lighter typically
# supports these; the wizard also allows manual input.
_QUICK_INSTRUMENTS = ("BTC", "ETH", "SOL", "HYPE", "ONDO")


# ---------------------------------------------------------------------------
# Screen + state
# ---------------------------------------------------------------------------
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
    direction: Optional[str] = None
    percentage: Optional[str] = None
    step0_volume: Optional[str] = None
    registration_key: Optional[str] = None


class TelegramAdapter(Protocol):
    async def send_inline_keyboard(self, *, chat_id, text, buttons, callback_prefix, metadata=None): ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _button(label: str, data: str) -> Dict[str, str]:
    return {"text": label, "callback_data": data}


def _percent_label(p: str) -> str:
    try:
        return f"{Decimal(p) * 100:.2f}%"
    except Exception:
        return p


def _vol_label(v: str) -> str:
    try:
        return f"{Decimal(v):.4f}".rstrip("0").rstrip(".")
    except Exception:
        return v


def _validate_percentage(s: str) -> Optional[str]:
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    if d <= 0:
        return None
    return str(d)


def _validate_step0_volume(s: str) -> Optional[str]:
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    if d <= 0:
        return None
    return str(d)


def _validate_direction(s: str) -> Optional[str]:
    s = s.strip().upper()
    if s in ("BUY", "SELL"):
        return s
    return None


def _account_aliases_for_exchange(tradedesk: TradeDesk, exchange: str) -> List[str]:
    """Return account aliases via the same TradeDesk path /trade uses.

    Does NOT parse .env / os.environ itself — TradeDesk → agent.list_accounts()
    is the single discovery source.
    """
    try:
        entries = tradedesk.list_accounts(exchange) if exchange else []
    except Exception:
        entries = []
    aliases: List[str] = []
    seen: set[str] = set()
    for entry in entries or []:
        alias, _label = _account_option_parts(entry)
        if not alias or alias in seen:
            continue
        seen.add(alias)
        aliases.append(alias)
    return aliases


def _account_entries_for_exchange(tradedesk: TradeDesk, exchange: str) -> List[Any]:
    """Raw account entries from TradeDesk (same source as /trade buttons)."""
    try:
        entries = tradedesk.list_accounts(exchange) if exchange else []
    except Exception:
        return []
    return list(entries or [])


def _discovered_exchanges(tradedesk: TradeDesk) -> List[str]:
    """Exchanges from TradeDesk agent discovery — identical source to /trade.

    Must NOT filter by GoldenFibo runtime support. Unsupported exchanges are
    still shown; the support check happens after account selection.
    """
    try:
        return list(tradedesk.list_exchanges() or [])
    except Exception:
        return []


def _golden_fibo_supported(exchange: str) -> bool:
    """True when fibo_service has a runtime adapter for this exchange."""
    return str(exchange or "").strip().lower() in {
        str(x).strip().lower() for x in SUPPORTED_EXCHANGES
    }


def _exchange_display_name(exchange: str) -> str:
    ex = str(exchange or "").strip()
    if not ex:
        return "this exchange"
    return ex[:1].upper() + ex[1:]


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------
class FiboWizard:
    CALLBACK_SEP = ":"

    def __init__(
        self,
        tradedesk: Optional[TradeDesk] = None,
        service: Optional[FiboServiceProtocol] = None,
    ) -> None:
        self._desk = tradedesk or get_tradedesk()
        self._service = service or get_fibo_service()
        self._states: Dict[Tuple[Any, ...], WizardState] = {}

    def reset(self, chat_key: Tuple[Any, ...]) -> None:
        self._states.pop(chat_key, None)

    def _state_for(self, chat_key: Tuple[Any, ...]) -> WizardState:
        s = self._states.get(chat_key)
        if s is None:
            s = WizardState()
            self._states[chat_key] = s
        return s

    def _set_state(self, s: WizardState, screen: Screen) -> Screen:
        """Update the wizard state to match the screen state.

        Every render/on-handler must call this so the wizard's
        WizardState stays in sync with the latest navigated screen.
        """
        s.state = screen.state
        return screen

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    def open(self, chat_key: Tuple[Any, ...]) -> Screen:
        s = self._state_for(chat_key)
        s.state = "main_menu"
        for k in (
            "exchange", "account", "instrument", "direction",
            "percentage", "step0_volume",
        ):
            setattr(s, k, None)
        return Screen(
            text="🧬 GoldenFibo Robot\n\nChoose an action:",
            buttons=[
                [_button(*BUTTON_START)],
                [_button(*BUTTON_RUNNING)],
                [_button(*BUTTON_STOP)],
                [_button(*BUTTON_EXIT)],
            ],
            state=s.state,
        )

    def handle_callback(self, chat_key: Tuple[Any, ...], data: str) -> Screen:
        s = self._state_for(chat_key)
        raw = str(data or "")
        if raw == "exit":
            self.reset(chat_key)
            return Screen(text="GoldenFibo closed.", buttons=[], state="closed")
        if raw == "back":
            return self._set_state(s, self._back(chat_key, s))
        if raw == "menu:start":
            return self._set_state(s, self._render_exchange(chat_key, s))
        if raw == "menu:running":
            return self._set_state(s, self._render_running(chat_key, s))
        if raw == "menu:stop":
            return self._set_state(s, self._render_stop_pick(chat_key, s))
        if raw.startswith("refresh") and s.state == "running_detail":
            return self._set_state(s, self._render_running_detail(chat_key, s, reg_key=s.registration_key))
        if raw.startswith("exchange:"):
            return self._set_state(s, self._on_exchange(chat_key, s, raw.split(":", 1)[1]))
        if raw.startswith("account:"):
            return self._set_state(s, self._on_account(chat_key, s, raw.split(":", 1)[1]))
        if raw.startswith("instrument:"):
            return self._set_state(s, self._on_instrument(chat_key, s, raw.split(":", 1)[1]))
        if raw.startswith("direction:"):
            return self._set_state(s, self._on_direction(chat_key, s, raw.split(":", 1)[1]))
        if raw.startswith("step0:"):
            return self._set_state(s, self._on_step0_pick(chat_key, s, raw.split(":", 1)[1]))
        if raw == "confirm_start":
            return self._set_state(s, self._on_confirm_start(chat_key, s))
        if raw.startswith("start_detail:"):
            reg = raw.split(":", 1)[1]
            return self._set_state(s, self._render_running_detail(chat_key, s, reg_key=reg))
        if raw.startswith("stop_pick:"):
            reg = raw.split(":", 1)[1]
            return self._set_state(s, self._render_stop_confirm(chat_key, s, reg_key=reg))
        if raw.startswith("confirm_stop:"):
            reg = raw.split(":", 1)[1]
            return self._set_state(s, self._on_stop(chat_key, s, reg_key=reg))
        return self.open(chat_key)

    def handle_text(self, chat_key: Tuple[Any, ...], text: str) -> Screen:
        s = self._state_for(chat_key)
        value = (text or "").strip()
        if s.state == "main_menu":
            return self._set_state(s, self.open(chat_key))
        if s.state == "exchange":
            return self._set_state(s, self._on_exchange(chat_key, s, value))
        if s.state == "account":
            return self._set_state(s, self._on_account(chat_key, s, value))
        if s.state == "instrument":
            if value.lower() == "other":
                return self._set_state(s, self._render_instrument_input(chat_key, s))
            return self._set_state(s, self._on_instrument(chat_key, s, value))
        if s.state == "instrument_input":
            return self._set_state(s, self._on_instrument(chat_key, s, value))
        if s.state == "direction":
            return self._set_state(s, self._on_direction(chat_key, s, value))
        if s.state == "percentage":
            return self._set_state(s, self._on_percentage(chat_key, s, value))
        if s.state == "step0_volume":
            return self._set_state(s, self._on_step0_volume(chat_key, s, value))
        if s.state == "review":
            return self._set_state(s, self._on_review_text(chat_key, s, value))
        return self.open(chat_key)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def _back(self, chat_key: Tuple[Any, ...], s: WizardState) -> Screen:
        if s.state == "review":
            return self._render_percentage(chat_key, s)
        if s.state == "percentage":
            return self._render_step0_volume(chat_key, s)
        if s.state == "step0_volume":
            return self._render_direction(chat_key, s)
        if s.state == "direction":
            return self._render_instrument(chat_key, s)
        if s.state == "instrument_input":
            return self._render_instrument(chat_key, s)
        if s.state == "instrument":
            return self._render_account(chat_key, s)
        if s.state == "unsupported_exchange":
            s.account = None
            return self._render_exchange(chat_key, s)
        if s.state == "account":
            s.account = None
            return self._render_exchange(chat_key, s)
        if s.state in ("running_detail", "stop_confirm"):
            return self._render_running(chat_key, s)
        return self.open(chat_key)

    # ------------------------------------------------------------------
    # Renderers
    # ------------------------------------------------------------------
    def _render_exchange(self, chat_key: Tuple[Any, ...], s: WizardState) -> Screen:
        options = _discovered_exchanges(self._desk)
        if not options:
            return Screen(
                text=(
                    "GoldenFibo\n\n"
                    "No exchanges are currently available.\n"
                    "Add an ``x_<exchange>_agent.py`` module to the trade "
                    "plugin's agents directory and try again."
                ),
                buttons=[[_button(*BUTTON_BACK)]],
                state="exchange",
            )
        buttons = [[_button(ex.upper(), f"exchange:{ex}")] for ex in options]
        buttons.append([_button(*BUTTON_BACK)])
        return Screen(
            text="Select exchange:",
            buttons=buttons,
            state="exchange",
        )

    def _on_exchange(self, chat_key: Tuple[Any, ...], s: WizardState, value: str) -> Screen:
        value = value.strip().lower()
        if value not in _discovered_exchanges(self._desk):
            return self._render_exchange(chat_key, s)
        s.exchange = value
        s.account = None
        return self._render_account(chat_key, s)

    def _render_account(self, chat_key: Tuple[Any, ...], s: WizardState) -> Screen:
        exchange = s.exchange or ""
        entries = _account_entries_for_exchange(self._desk, exchange)
        rows: List[List[Dict[str, str]]] = []
        for entry in entries:
            alias, label = _account_option_parts(entry)
            if not alias or not label:
                continue
            rows.append([_button(label, f"account:{alias}")])
        if not rows:
            # Match /trade: no free-text account entry when discovery is empty.
            return Screen(
                text=(
                    f"GoldenFibo\n\n"
                    f"Exchange: {exchange}\n\n"
                    "No accounts are configured for this exchange."
                ),
                buttons=[[_button(*BUTTON_BACK)]],
                state="account",
            )
        rows.append([_button(*BUTTON_BACK)])
        return Screen(
            text=(
                f"Exchange: {exchange.upper()}\n"
                "Select account:"
            ),
            buttons=rows,
            state="account",
        )

    def _on_account(self, chat_key: Tuple[Any, ...], s: WizardState, value: str) -> Screen:
        value = value.strip()
        exchange = s.exchange or ""
        valid = set(_account_aliases_for_exchange(self._desk, exchange))
        if not value or value not in valid:
            return self._render_account(chat_key, s)
        s.account = value
        # Support check AFTER account selection, BEFORE instrument/start.
        if not _golden_fibo_supported(exchange):
            return self._render_unsupported_exchange(chat_key, s)
        return self._render_instrument(chat_key, s)

    def _render_unsupported_exchange(
        self, chat_key: Tuple[Any, ...], s: WizardState
    ) -> Screen:
        """Graceful UI stop — no service/network/trading mutation."""
        name = _exchange_display_name(s.exchange or "")
        return Screen(
            text=(
                f"GoldenFibo is not yet available on {name}.\n\n"
                "Exchange discovery matches /trade, but GoldenFibo "
                "runtime support for this venue is not implemented yet.\n\n"
                "Choose Back to pick another exchange."
            ),
            buttons=[[_button(*BUTTON_BACK)]],
            state="unsupported_exchange",
        )

    def _render_instrument(self, chat_key: Tuple[Any, ...], s: WizardState) -> Screen:
        buttons = [[_button(sym, f"instrument:{sym}")] for sym in _QUICK_INSTRUMENTS]
        buttons.append([_button(*BUTTON_OTHER_SYMBOL)])
        buttons.append([_button(*BUTTON_BACK)])
        return Screen(
            text=(
                f"Exchange: {s.exchange.upper()}\n"
                f"Account: {s.account}\n"
                "Select instrument:"
            ),
            buttons=buttons,
            state="instrument",
        )

    def _render_instrument_input(self, chat_key: Tuple[Any, ...], s: WizardState) -> Screen:
        return Screen(
            text=(
                f"Exchange: {s.exchange.upper()}\n"
                f"Account: {s.account}\n"
                "Type the instrument symbol (e.g. BTC):"
            ),
            buttons=[[_button(*BUTTON_BACK)]],
            state="instrument_input",
        )

    def _on_instrument(self, chat_key: Tuple[Any, ...], s: WizardState, value: str) -> Screen:
        value = value.strip().upper()
        if not value:
            return self._render_instrument(chat_key, s)
        s.instrument = value
        return self._render_direction(chat_key, s)

    def _render_direction(self, chat_key: Tuple[Any, ...], s: WizardState) -> Screen:
        buttons = [
            [_button(*BUTTON_BUY)],
            [_button(*BUTTON_SELL)],
            [_button(*BUTTON_BACK)],
        ]
        return Screen(
            text=(
                f"Exchange: {s.exchange.upper()}\n"
                f"Account: {s.account}\n"
                f"Instrument: {s.instrument}\n"
                "Select direction:"
            ),
            buttons=buttons,
            state="direction",
        )

    def _on_direction(self, chat_key: Tuple[Any, ...], s: WizardState, value: str) -> Screen:
        v = _validate_direction(value)
        if v is None:
            return self._render_direction(chat_key, s)
        s.direction = v
        return self._render_step0_volume(chat_key, s)

    def _render_percentage(self, chat_key: Tuple[Any, ...], s: WizardState) -> Screen:
        return Screen(
            text=(
                f"Exchange: {s.exchange.upper()}\n"
                f"Account: {s.account}\n"
                f"Instrument: {s.instrument}\n"
                f"Direction: {s.direction}\n"
                f"Step0 volume: {_vol_label(s.step0_volume or '0')}\n"
                f"Enter percentage (e.g. 0.01 for 1%). Default: 0.01"
            ),
            buttons=[[_button(*BUTTON_BACK)]],
            state="percentage",
        )

    def _on_percentage(self, chat_key: Tuple[Any, ...], s: WizardState, value: str) -> Screen:
        v = _validate_percentage(value)
        if v is None:
            return self._render_percentage(chat_key, s)
        s.percentage = v
        return self._render_review(chat_key, s)

    def _render_step0_volume(self, chat_key: Tuple[Any, ...], s: WizardState) -> Screen:
        buttons = [
            [_button(*BUTTON_DEFAULT_STEP0)],
            [_button(*BUTTON_DEFAULT_STEP0_BTC)],
            [_button(*BUTTON_BACK)],
        ]
        return Screen(
            text=(
                f"Exchange: {s.exchange.upper()}\n"
                f"Account: {s.account}\n"
                f"Instrument: {s.instrument}\n"
                f"Direction: {s.direction}\n"
                "Enter Step0 volume (default 0.01 for SOL, 0.0001 for BTC-like):"
            ),
            buttons=buttons,
            state="step0_volume",
        )

    def _on_step0_pick(self, chat_key: Tuple[Any, ...], s: WizardState, value: str) -> Screen:
        v = _validate_step0_volume(value)
        if v is None:
            return self._render_step0_volume(chat_key, s)
        s.step0_volume = v
        return self._render_percentage(chat_key, s)

    def _on_step0_volume(self, chat_key: Tuple[Any, ...], s: WizardState, value: str) -> Screen:
        return self._on_step0_pick(chat_key, s, value)

    def _render_review(self, chat_key: Tuple[Any, ...], s: WizardState) -> Screen:
        try:
            step0 = Decimal(s.step0_volume or "0")
        except Exception:
            step0 = Decimal("0")
        ladder = []
        cum = Decimal("0")
        for n in range(21):
            v = golden_fibo_volume(step0, n)
            cum += v
            ladder.append(f"Step{n:<2} = {v}")
        cumulative = golden_fibo_cumulative_volume(step0, 20)
        text = (
            f"Review:\n"
            f"Exchange: {s.exchange}\n"
            f"Account: {s.account}\n"
            f"Instrument: {s.instrument}\n"
            f"Direction: {s.direction}\n"
            f"Percentage: {_percent_label(s.percentage or '0')}\n"
            f"Step0 volume: {_vol_label(s.step0_volume or '0')}\n\n"
            f"Ladder (V0..V20):\n" + "\n".join(ladder) + "\n\n"
            f"Cumulative through Step20: {cumulative}\n\n"
            "Press ▶️ Start Fibo to register."
        )
        buttons = [
            [_button(*BUTTON_CONFIRM_START)],
            [_button(*BUTTON_BACK)],
        ]
        return Screen(text=text, buttons=buttons, state="review")

    def _on_review_text(self, chat_key: Tuple[Any, ...], s: WizardState, value: str) -> Screen:
        if value.lower() in ("", "ok", "yes", "start", "go"):
            return self._on_confirm_start(chat_key, s)
        return self._render_review(chat_key, s)

    def _on_confirm_start(self, chat_key: Tuple[Any, ...], s: WizardState) -> Screen:
        cmd = {
            "op": "start",
            "exchange": s.exchange,
            "account": s.account,
            "instrument": s.instrument,
            "direction": s.direction,
            "percentage": s.percentage,
            "step0_volume": s.step0_volume,
        }
        resp = self._service.execute_command(cmd)
        if not resp.get("ok"):
            err = resp.get("error", "INTERNAL")
            text = f"❌ Start failed: {err}\n\n"
            if err == "OPPOSITE_DIRECTION_ACTIVE":
                existing = resp.get("existing_registration_key", "?")
                text += f"An opposite-direction registration is already active: {existing}"
            elif err == "DUPLICATE_REGISTRATION":
                text += f"Registration already exists: {resp.get('registration_key', '?')}"
            else:
                detail = resp.get("detail", "")
                if detail:
                    text += detail
            return Screen(
                text=text,
                buttons=[[_button(*BUTTON_START)], [_button(*BUTTON_EXIT)]],
                state="start_failed",
            )
        s.registration_key = resp.get("registration_key")
        text = (
            f"✅ Started GoldenFibo registration\n\n"
            f"{s.exchange}/{s.account}/{s.instrument}/{s.direction}\n\n"
            "Use 🔵 Running Fibo to view status."
        )
        return Screen(
            text=text,
            buttons=[[_button(*BUTTON_RUNNING)], [_button(*BUTTON_EXIT)]],
            state="started",
        )

    # ------------------------------------------------------------------
    # Running list / detail
    # ------------------------------------------------------------------
    def _render_running(self, chat_key: Tuple[Any, ...], s: WizardState) -> Screen:
        resp = self._service.execute_command({"op": "list"})
        if not resp.get("ok"):
            return Screen(text="Failed to list.", buttons=[[_button(*BUTTON_BACK)]], state="running_list")
        active = resp.get("registrations") or []
        quarantined = resp.get("quarantined") or []
        if not active:
            text = "No active GoldenFibo registrations."
            buttons = [[_button(*BUTTON_START)], [_button(*BUTTON_BACK)]]
        else:
            lines = ["Active GoldenFibo registrations:"]
            for r in active:
                key = r.get("registration_key", "?")
                lines.append(f"  • {key}")
            text = "\n".join(lines)
            buttons = [[_button(r.get("registration_key", "?"), f"start_detail:{r.get('registration_key', '?')}")] for r in active]
            buttons.append([_button(*BUTTON_REFRESH)])
            buttons.append([_button(*BUTTON_BACK)])
        if quarantined:
            text += "\n\nQuarantined old-strategy records:"
            for q in quarantined:
                text += f"\n  • {q.get('registration_key', '?')} (status={q.get('status')})"
        return Screen(text=text, buttons=buttons, state="running_list")

    def _render_running_detail(self, chat_key: Tuple[Any, ...], s: WizardState, reg_key: str) -> Screen:
        s.registration_key = reg_key
        resp = self._service.execute_command({"op": "detail", "registration_key": reg_key})
        if not resp.get("ok"):
            return Screen(text=f"Detail failed: {resp.get('error')}", buttons=[[_button(*BUTTON_BACK)]], state="running_detail")
        r = resp.get("registration") or {}
        text = (
            f"Registration: {r.get('registration_key')}\n"
            f"Exchange: {r.get('exchange')}\n"
            f"Account: {r.get('account')}\n"
            f"Instrument: {r.get('instrument')}\n"
            f"Direction: {r.get('direction')}\n"
            f"Cycle: {r.get('cycle_id')}\n"
            f"Highest filled step: {r.get('highest_filled_step')}\n"
            f"Expected cumulative size: {r.get('expected_cumulative_size')}\n"
            f"Current TP price: {r.get('current_tp_price')}\n"
            f"Next step: {r.get('next_step')}\n"
            f"Status: {r.get('status')}\n"
            f"Freeze reason: {r.get('freeze_reason')}\n"
        )
        buttons = [
            [_button("🛑 STOP", f"stop_pick:{reg_key}")],
            [_button(*BUTTON_REFRESH)],
            [_button(*BUTTON_BACK)],
        ]
        return Screen(text=text, buttons=buttons, state="running_detail")

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------
    def _render_stop_pick(self, chat_key: Tuple[Any, ...], s: WizardState) -> Screen:
        resp = self._service.execute_command({"op": "list"})
        if not resp.get("ok"):
            return Screen(text="Failed to list.", buttons=[[_button(*BUTTON_BACK)]], state="stop_pick")
        active = resp.get("registrations") or []
        if not active:
            return Screen(
                text="No active GoldenFibo registrations to stop.",
                buttons=[[_button(*BUTTON_START)], [_button(*BUTTON_BACK)]],
                state="stop_pick",
            )
        buttons = [[_button(r.get("registration_key", "?"), f"stop_pick:{r.get('registration_key', '?')}")] for r in active]
        buttons.append([_button(*BUTTON_BACK)])
        return Screen(
            text="Select a registration to STOP:",
            buttons=buttons,
            state="stop_pick",
        )

    def _render_stop_confirm(self, chat_key: Tuple[Any, ...], s: WizardState, reg_key: str) -> Screen:
        s.registration_key = reg_key
        return Screen(
            text=(
                f"STOP GoldenFibo for {reg_key}?\n\n"
                "This stops further robot operation for this registration.\n"
                "It does NOT auto-close the live position or any unrelated orders."
            ),
            buttons=[
                [_button("🛑 Confirm STOP", f"confirm_stop:{reg_key}")],
                [_button(*BUTTON_CANCEL)],
            ],
            state="stop_confirm",
        )

    def _on_stop(self, chat_key: Tuple[Any, ...], s: WizardState, reg_key: str) -> Screen:
        resp = self._service.execute_command({"op": "stop", "registration_key": reg_key})
        if not resp.get("ok"):
            err = resp.get("error", "INTERNAL")
            if err == "OLD_STRATEGY_REGISTRATION":
                text = (
                    f"❌ {reg_key} is an old-strategy quarantined record. "
                    "It cannot be stopped through /fibo. Decommission it manually."
                )
            else:
                text = f"❌ Stop failed: {err}"
            return Screen(
                text=text,
                buttons=[[_button(*BUTTON_BACK)], [_button(*BUTTON_EXIT)]],
                state="stop_done",
            )
        text = f"✅ Stopped GoldenFibo for {reg_key}.\n\nThe live position and any pending orders are untouched."
        return Screen(
            text=text,
            buttons=[[_button(*BUTTON_RUNNING)], [_button(*BUTTON_EXIT)]],
            state="stop_done",
        )


def get_fibo_wizard() -> FiboWizard:
    """Module-level singleton accessor."""
    return FiboWizard()



# ---------------------------------------------------------------------------
# Module-level dispatcher entry points used by the Telegram adapter.
#
# The adapter calls these by name (handle_fibo_command / _callback / _text).
# They delegate to the FiboWizard class singleton. The text dispatcher
# only handles wizard states that explicitly await free-text input;
# other states return False so the adapter falls through to normal
# message dispatch.
# ---------------------------------------------------------------------------
_TEXT_HANDLING_STATES = frozenset({
    "account",
    "instrument_input",
    "percentage",
    "step0_volume",
})


def _chat_key(msg_or_query: Any) -> Tuple[Any, ...]:
    """Derive a stable chat key from a Telegram message or callback query.

    Uses (chat_id, message_thread_id) when available so topic-aware
    Telegram conversations are isolated from one another.
    """
    chat_id = getattr(getattr(msg_or_query, "chat", None), "id", None)
    if chat_id is None:
        chat_id = getattr(getattr(getattr(msg_or_query, "message", None), "chat", None), "id", None)
    thread_id = getattr(msg_or_query, "message_thread_id", None)
    return (chat_id,) if thread_id is None else (chat_id, thread_id)


async def _send_screen(adapter: Any, msg_or_query: Any, screen: Screen) -> None:
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


async def handle_fibo_command(adapter: Any, msg: Any) -> bool:
    """Open the Fibo wizard for the chat that issued /fibo.

    Called by the Telegram adapter's command dispatch BEFORE generic
    command handling. Returns True if the message was a /fibo
    invocation and was consumed by the wizard, False otherwise (in
    which case the adapter should continue with normal dispatch).
    """
    text = str(getattr(msg, "text", "") or "").strip()
    if not text.startswith("/fibo"):
        return False
    parts = text.split(maxsplit=1)
    if not parts or parts[0] != "/fibo":
        return False
    wizard = get_fibo_wizard()
    chat_key = _chat_key(msg)
    screen = wizard.open(chat_key)
    await _send_screen(adapter, msg, screen)
    return True


async def handle_fibo_callback(adapter: Any, query: Any, data: str) -> bool:
    """Handle a ``fibo:`` prefixed callback query.

    The adapter has already routed the call here because
    ``data.startswith("fibo:")``. This function strips the ``fibo:``
    prefix, runs the wizard one step, edits the originating message
    in place to the next screen, and acknowledges the query.
    """
    raw = str(data or "")
    if not raw.startswith("fibo:"):
        return False
    payload = raw.split(":", 1)[1] if ":" in raw else ""
    wizard = get_fibo_wizard()
    chat_key = _chat_key(getattr(query, "message", query))
    screen = wizard.handle_callback(chat_key, payload)
    await _send_screen(adapter, query, screen)
    return True


async def handle_fibo_text(adapter: Any, msg: Any) -> bool:
    """Handle free-text input for wizard states that explicitly await it.

    Returns False if the current wizard state is not in
    ``_TEXT_HANDLING_STATES`` so the adapter falls through to normal
    message handling.
    """
    wizard = get_fibo_wizard()
    chat_key = _chat_key(msg)
    state = wizard._state_for(chat_key)  # noqa: SLF001
    if state.state not in _TEXT_HANDLING_STATES:
        return False
    screen = wizard.handle_text(chat_key, str(getattr(msg, "text", "") or ""))
    await _send_screen(adapter, msg, screen)
    return True


__all__ = [
    "FiboWizard",
    "Screen",
    "WizardState",
    "get_fibo_wizard",
    "handle_fibo_command",
    "handle_fibo_callback",
    "handle_fibo_text",
]
