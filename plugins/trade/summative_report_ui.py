"""Compact, read-only Telegram views for the /trade Summative Report.

This module has no Telegram or TradeDesk imports.  The wizard supplies the
TradeDesk and persists the report context; portfolio reads are delegated to
``portfolio_report`` which is restricted to balance and positions_orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from . import portfolio_report as portfolio

_PAGE_SIZE = 8


@dataclass(frozen=True)
class ReportView:
    text: str
    buttons: List[List[Dict[str, str]]]


def _b(text: str, callback_data: str) -> Dict[str, str]:
    return {"text": text, "callback_data": callback_data}


def _report_snapshot(desk: Any, state: Any, *, refresh: bool = False) -> Any:
    snap = state.report.get("snapshot")
    if refresh or snap is None:
        snap = portfolio.collect_portfolio_snapshot(desk)
        state.report["snapshot"] = snap
        state.report["page"] = 0
    return snap


def _scope(state: Any, snapshot: Any) -> Any:
    return snapshot.filtered(
        state.report.get("filter_exchange"), state.report.get("filter_account")
    )


def _freshness(snapshot: Any) -> str:
    suffix = " · PARTIAL" if snapshot.positions_incomplete else ""
    return (
        f"Data fetched now · Positions: {snapshot.positions_accounts_ok}/"
        f"{snapshot.positions_accounts_seen} accounts{suffix}"
    )


def _native_lines(lines: List[str], label: str, units: Dict[str, Any], *, signed: bool = False) -> None:
    if not units:
        lines.append(f"{label}: unavailable")
        return
    for unit, amount in sorted(units.items()):
        lines.append(f"{label} ({unit}): {portfolio.format_money(amount, signed=signed)} {unit}")


def _coverage_lines(lines: List[str], snapshot: Any) -> None:
    if not snapshot.positions_incomplete:
        return
    lines.append("Positions coverage is PARTIAL; unavailable accounts are excluded, not zeroed.")
    lines.append("Excluded position accounts:")
    for account in snapshot.position_failures[:6]:
        reason = next((item.split(": ", 1)[-1] for item in account.errors if "positions" in item), "positions unavailable")
        lines.append(f"• {account.exchange}/{account.account_label}: {reason}")
    if len(snapshot.position_failures) > 6:
        lines.append(f"• …and {len(snapshot.position_failures) - 6} more")


def _footer(*, back: str = "sr:m", refresh: bool = True) -> List[List[Dict[str, str]]]:
    rows: List[List[Dict[str, str]]] = []
    if refresh:
        rows.append([_b("↻ Refresh", "sr:refresh")])
    rows.append([_b("◀️ Back", back), _b("✕ Exit", "exit")])
    return rows


def _menu(desk: Any, state: Any, *, refresh: bool = False) -> ReportView:
    snap = _report_snapshot(desk, state, refresh=refresh)
    state.report["view"] = "menu"
    scope = _scope(state, snap)
    selected = "All Exchanges"
    if state.report.get("filter_exchange"):
        selected = str(state.report["filter_exchange"])
        if state.report.get("filter_account"):
            selected += f" / {state.report['filter_account']}"
    text = "\n".join([
        "📊 Summative Report",
        "",
        _freshness(scope),
        f"Scope: {selected}",
        "",
        "Open positions only. Read-only; no orders or account changes.",
        "Choose a report:",
    ])
    return ReportView(text, [
        [_b("📊 Positions by Symbol", "sr:p")],
        [_b("🏦 PnL by Exchange", "sr:e")],
        [_b("💰 Total Floating PnL", "sr:t")],
        [_b("💼 Total Equity", "sr:q")],
        [_b("⚠️ Risk & Exposure", "sr:r")],
        [_b("🔎 Filter Exchange / Account", "sr:f")],
        [_b("🔙 Back", "back"), _b("✕ Exit", "exit")],
    ])


def _filter_exchanges(desk: Any, state: Any) -> ReportView:
    snapshot = _report_snapshot(desk, state)
    exchanges = portfolio.list_exchanges(snapshot)
    state.report["filter_exchanges"] = exchanges
    lines = ["🔎 Filter Exchange / Account", "", "All Exchanges → Exchange → Account → Open Positions"]
    buttons: List[List[Dict[str, str]]] = [[_b("All Exchanges", "sr:all")]]
    for index, exchange in enumerate(exchanges):
        buttons.append([_b(exchange, f"sr:fx:{index}")])
    buttons += _footer(back="sr:m", refresh=False)
    return ReportView("\n".join(lines), buttons)


def _filter_accounts(desk: Any, state: Any) -> ReportView:
    snapshot = _report_snapshot(desk, state)
    exchange = state.report.get("filter_exchange")
    accounts = portfolio.accounts_for_exchange(snapshot, exchange)
    state.report["filter_accounts"] = accounts
    lines = [f"🔎 {exchange}", "", "Select an account:"]
    buttons: List[List[Dict[str, str]]] = [[_b("All accounts", "sr:allx")]]
    for index, account in enumerate(accounts):
        buttons.append([_b(account.account_label, f"sr:fa:{index}")])
    buttons += _footer(back="sr:f", refresh=False)
    return ReportView("\n".join(lines), buttons)


def _account_detail(desk: Any, state: Any, account: Any) -> ReportView:
    snapshot = _report_snapshot(desk, state)
    state.report["filter_exchange"] = account.exchange
    state.report["filter_account"] = account.account
    scoped = _scope(state, snapshot)
    pnl = portfolio.total_floating_pnl(scoped)
    equity = portfolio.total_equity(scoped)
    lines = [f"💼 {account.exchange} / {account.account_label}", "", _freshness(scoped)]
    lines.append("Floating PnL (unconverted):")
    _native_lines(lines, "  PnL", pnl["units"], signed=True)
    for unit, value in sorted(equity["equity_units"].items()):
        lines.append(f"Equity ({unit}): {portfolio.format_money(value)} {unit}")
    for unit, value in sorted(equity["margin_units"].items()):
        lines.append(f"Margin used ({unit}): {portfolio.format_money(value)} {unit}")
    lines += ["", "Open positions:"]
    if not scoped.positions:
        lines.append("• None")
    for pos in scoped.positions[:8]:
        pnl_text = f"{portfolio.format_money(pos.pnl, signed=True)} {pos.pnl_unit}" if pos.pnl is not None else "—"
        lines.append(f"• {pos.symbol} {pos.side} size {format(pos.size, 'f') if pos.size is not None else '—'} · PnL {pnl_text}")
    if len(scoped.positions) > 8:
        lines.append(f"• …and {len(scoped.positions) - 8} more; open Positions by Symbol for pagination.")
    return ReportView("\n".join(lines), _footer(back="sr:f", refresh=True))


def _exchange_accounts(desk: Any, state: Any, exchange: str) -> ReportView:
    snapshot = _report_snapshot(desk, state)
    accounts = portfolio.accounts_for_exchange(snapshot, exchange)
    state.report["exchange_accounts"] = accounts
    lines = [f"🏦 {exchange} Accounts", "", _freshness(snapshot)]
    buttons: List[List[Dict[str, str]]] = []
    for index, account in enumerate(accounts):
        account_units: Dict[str, Any] = {}
        for position in account.positions:
            if position.pnl is not None:
                account_units[position.pnl_unit] = account_units.get(position.pnl_unit, 0) + position.pnl
        pnl = "; ".join(f"{portfolio.format_money(amount, signed=True)} {unit}" for unit, amount in sorted(account_units.items())) or "unavailable"
        lines.append(f"• {account.account_label}: {pnl}")
        buttons.append([_b(account.account_label, f"sr:ea:{index}")])
    if not accounts:
        lines.append("No accounts available.")
    buttons += _footer(back="sr:e")
    return ReportView("\n".join(lines), buttons)


def _risk(desk: Any, state: Any) -> ReportView:
    snapshot = _scope(state, _report_snapshot(desk, state))
    state.report["view"] = "risk"
    info = portfolio.risk_and_exposure(snapshot)
    lines = ["⚠️ Risk & Exposure", "", _freshness(snapshot), ""]
    lines.append("Gross Long Notional: N/A")
    lines.append("Gross Short Notional: N/A")
    if info["margin_units"]:
        for unit, amount in sorted(info["margin_units"].items()):
            lines.append(f"Margin in Use ({unit}): {portfolio.format_money(amount)} {unit}")
    else:
        lines.append("Margin in Use: unavailable")
    if info["leverage"] is not None:
        lines.append(f"Effective portfolio leverage: {info['leverage']:.2f}x")
    else:
        lines.append("Effective portfolio leverage: unavailable")
    if info["leverage_note"]:
        lines.append(f"({info['leverage_note']})")
    lines += ["", "Net exposure by instrument:"]
    for row in info["net_by_instrument"][:8]:
        value = "N/A" if not row["notional_any"] else portfolio.format_money(row["net_notional"], signed=True)
        lines.append(f"• {row['instrument']}: net size {format(row['net_size'], 'f')} · net notional {value}")
    if not info["net_by_instrument"]:
        lines.append("• No open positions.")
    if info["largest_losers"]:
        lines += ["", "Largest losing open positions:"]
        for pos in info["largest_losers"][:5]:
            lines.append(f"• {pos.exchange}/{pos.account_label} {pos.symbol} {pos.side}: {portfolio.format_money(pos.pnl, signed=True)} {pos.pnl_unit}")
    if info["elevated_margin"]:
        lines += ["", "Elevated margin utilization (≥70%):"]
        for row in info["elevated_margin"][:5]:
            lines.append(f"• {row['exchange']}/{row['account_label']}: {row['utilization']:.1%}")
    lines += ["", "Liquidation prices: unavailable in the current canonical agent contract.", info["notional_note"]]
    if info["incomplete"]:
        lines.append("Warning: unavailable accounts make this risk view incomplete.")
    return ReportView("\n".join(lines), _footer())


def _units(lines: List[str], label: str, units: Dict[str, Any]) -> None:
    if not units:
        lines.append(f"{label}: unavailable")
        return
    for unit, amount in sorted(units.items()):
        lines.append(f"{label} ({unit}): {portfolio.format_money(amount)} {unit}")


def _equity(desk: Any, state: Any) -> ReportView:
    snapshot = _scope(state, _report_snapshot(desk, state))
    state.report["view"] = "equity"
    info = portfolio.total_equity(snapshot)
    lines = ["💼 Total Equity", "", _freshness(snapshot), ""]
    _units(lines, "Total Equity", info["equity_units"])
    _units(lines, "Available Balance", info["available_units"])
    _units(lines, "Margin in Use", info["margin_units"])
    _native_lines(lines, "Floating PnL", info["floating_pnl_units"], signed=True)
    _coverage_lines(lines, snapshot)
    if info["missing"]:
        lines += ["", "Unavailable / fallback metrics:"]
        for item in info["missing"][:6]:
            lines.append(f"• {item}")
        if len(info["missing"]) > 6:
            lines.append(f"• …and {len(info['missing']) - 6} more")
    if info["incomplete"]:
        lines += ["", "Warning: equity is incomplete; units are not converted."]
    return ReportView("\n".join(lines), _footer())


def _total_pnl(desk: Any, state: Any) -> ReportView:
    snapshot = _scope(state, _report_snapshot(desk, state))
    state.report["view"] = "total_pnl"
    info = portfolio.total_floating_pnl(snapshot)
    lines = ["💰 Total Floating PnL", "", _freshness(snapshot), ""]
    _native_lines(lines, "Floating PnL", info["units"], signed=True)
    lines.append(f"Open Positions: {info['open_positions']}")
    lines.append(f"Exchanges: {info['exchanges']}")
    lines.append(f"Positions coverage: {info['accounts_ok']}/{info['accounts']} accounts" + (" · PARTIAL" if info["incomplete"] else ""))
    _native_lines(lines, "Long PnL", info["long_units"], signed=True)
    _native_lines(lines, "Short PnL", info["short_units"], signed=True)
    _coverage_lines(lines, snapshot)
    return ReportView("\n".join(lines), _footer())


def _pnl_by_exchange(desk: Any, state: Any) -> ReportView:
    snapshot = _scope(state, _report_snapshot(desk, state))
    state.report["view"] = "exchange_pnl"
    rows = portfolio.pnl_by_exchange(snapshot)
    lines = ["🏦 PnL by Exchange", "", _freshness(snapshot), ""]
    total = None
    if not rows:
        lines.append("No exchanges in the selected scope.")
    for row in rows:
        partial = " (PARTIAL)" if row["incomplete"] else ""
        if row["pnl_any"]:
            lines.append(f"{row['exchange']}:{partial}")
            _native_lines(lines, "  PnL", row["units"], signed=True)
        else:
            lines.append(f"{row['exchange']}: PnL unavailable{partial}")
    info = portfolio.total_floating_pnl(snapshot)
    lines += ["", "PORTFOLIO PnL (unconverted):"]
    _native_lines(lines, "  PnL", info["units"], signed=True)
    _coverage_lines(lines, snapshot)
    buttons: List[List[Dict[str, str]]] = []
    for index, row in enumerate(rows):
        buttons.append([_b(f"{row['exchange']} accounts", f"sr:ex:{index}")])
    buttons += _footer()
    state.report["exchange_rows"] = rows
    return ReportView("\n".join(lines), buttons)


def _positions(desk: Any, state: Any) -> ReportView:
    snapshot = _scope(state, _report_snapshot(desk, state))
    state.report["view"] = "positions"
    groups = portfolio.positions_by_symbol(snapshot)
    state.report["position_groups"] = groups
    page = max(0, int(state.report.get("page", 0)))
    page_count = max(1, (len(groups) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(page, page_count - 1)
    state.report["page"] = page
    begin = page * _PAGE_SIZE
    rows = groups[begin : begin + _PAGE_SIZE]
    lines = ["📊 Positions by Symbol", "", _freshness(snapshot)]
    if not rows:
        lines += ["", "No open positions."]
    for index, group in enumerate(rows, begin):
        pnl = "; ".join(f"{portfolio.format_money(amount, signed=True)} {unit}" for unit, amount in sorted(group.units.items())) if group.pnl_any else "—"
        lines.append(
            f"{group.display_symbol} {group.side} | Volume: {format(group.volume, 'f')} | PnL: {pnl}"
        )
    info = portfolio.total_floating_pnl(snapshot)
    lines += ["", "Overall floating PnL (unconverted):"]
    _native_lines(lines, "  PnL", info["units"], signed=True)
    _coverage_lines(lines, snapshot)
    buttons: List[List[Dict[str, str]]] = []
    for index, group in enumerate(rows, begin):
        buttons.append([_b(f"{group.display_symbol} {group.side}", f"sr:d:{index}")])
    if page_count > 1:
        nav: List[Dict[str, str]] = []
        if page > 0:
            nav.append(_b("◀ Prev", "sr:pp"))
        nav.append(_b(f"{page + 1}/{page_count}", "sr:p"))
        if page + 1 < page_count:
            nav.append(_b("Next ▶", "sr:pn"))
        buttons.append(nav)
    buttons += _footer()
    return ReportView("\n".join(lines), buttons)


def _position_detail(state: Any, index_text: str) -> ReportView:
    groups = state.report.get("position_groups") or []
    try:
        group = groups[int(index_text)]
    except (ValueError, IndexError, TypeError):
        return _positions(None, state)  # snapshot is cached; desk not used
    lines = [f"📊 {group.display_symbol} {group.side}", ""]
    lines.append(f"Aggregated volume: {format(group.volume, 'f')}")
    lines.append("Aggregated floating PnL (unconverted):")
    _native_lines(lines, "  PnL", group.units, signed=True)
    lines += ["", "Contributing open positions:"]
    for pos in group.contributors[:12]:
        pnl = f"{portfolio.format_money(pos.pnl, signed=True)} {pos.pnl_unit}" if pos.pnl is not None else "—"
        lines.append(
            f"• {pos.exchange}/{pos.account_label}: {pos.symbol} {pos.side} "
            f"size {format(pos.size, 'f') if pos.size is not None else '—'} · PnL {pnl}"
        )
    if len(group.contributors) > 12:
        lines.append(f"• …and {len(group.contributors) - 12} more")
    return ReportView("\n".join(lines), _footer(back="sr:p"))


def handle(desk: Any, state: Any, suffix: str) -> ReportView:
    """Route a ``sr:`` callback.  Menu and position navigation are live."""
    if suffix in {"summative_report", "sr:m", "sr:refresh"}:
        return _menu(desk, state, refresh=suffix == "sr:refresh")
    if suffix == "sr:p":
        return _positions(desk, state)
    if suffix == "sr:e":
        return _pnl_by_exchange(desk, state)
    if suffix == "sr:t":
        return _total_pnl(desk, state)
    if suffix == "sr:q":
        return _equity(desk, state)
    if suffix == "sr:r":
        return _risk(desk, state)
    if suffix == "sr:f":
        return _filter_exchanges(desk, state)
    if suffix == "sr:all":
        state.report.pop("filter_exchange", None)
        state.report.pop("filter_account", None)
        return _menu(desk, state)
    if suffix == "sr:allx":
        state.report.pop("filter_account", None)
        return _menu(desk, state)
    if suffix.startswith("sr:fx:"):
        try:
            exchange = (state.report.get("filter_exchanges") or [])[int(suffix.rsplit(":", 1)[-1])]
        except (ValueError, IndexError):
            return _filter_exchanges(desk, state)
        state.report["filter_exchange"] = exchange
        state.report.pop("filter_account", None)
        return _filter_accounts(desk, state)
    if suffix.startswith("sr:fa:"):
        try:
            account = (state.report.get("filter_accounts") or [])[int(suffix.rsplit(":", 1)[-1])]
        except (ValueError, IndexError):
            return _filter_accounts(desk, state)
        return _account_detail(desk, state, account)
    if suffix.startswith("sr:ex:"):
        try:
            exchange = (state.report.get("exchange_rows") or [])[int(suffix.rsplit(":", 1)[-1])]["exchange"]
        except (ValueError, IndexError, KeyError):
            return _pnl_by_exchange(desk, state)
        return _exchange_accounts(desk, state, exchange)
    if suffix.startswith("sr:ea:"):
        try:
            account = (state.report.get("exchange_accounts") or [])[int(suffix.rsplit(":", 1)[-1])]
        except (ValueError, IndexError):
            return _pnl_by_exchange(desk, state)
        return _account_detail(desk, state, account)
    if suffix == "sr:pp":
        state.report["page"] = max(0, int(state.report.get("page", 0)) - 1)
        return _positions(desk, state)
    if suffix == "sr:pn":
        state.report["page"] = int(state.report.get("page", 0)) + 1
        return _positions(desk, state)
    if suffix.startswith("sr:d:"):
        return _position_detail(state, suffix.rsplit(":", 1)[-1])
    return _menu(desk, state)
