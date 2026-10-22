"""Compact, read-only Telegram views for the /trade Summative Report.

This module has no Telegram or TradeDesk imports.  The wizard supplies the
TradeDesk and persists the report context; portfolio reads are delegated to
``portfolio_report`` which is restricted to balance and positions_orders.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import portfolio_report as portfolio

logger = logging.getLogger(__name__)

_PAGE_SIZE = 6
_NET_PAGE_SIZE = 6


@dataclass(frozen=True)
class ReportView:
    text: str
    buttons: List[List[Dict[str, str]]]


# ---------------------------------------------------------------------------
# Progress reporting
#
# The wizard is transport-agnostic; ``summative_report_ui`` is too.  Neither
# module imports Telegram or any transport client.  Progress is reported by
# pushing small payloads into ``state.report['progress']`` and emitting
# :func:`summative_report_ui.progress_text` strings that the transport can
# render via its existing message-edit helper.
#
# Three pieces:
#
# 1. ``ProgressSink`` — a callback interface.  Adapters (e.g. the Telegram
#    adapter) install one in ``state.report['progress_sink']`` so the
#    report layer can emit updates without knowing the transport.
# 2. ``ProgressCollector`` — a default in-memory sink that captures every
#    emitted frame; tests use this so progress is exercised end-to-end
#    without standing up a Telegram adapter.
# 3. ``push_progress`` / ``finalize_progress`` — the helpers called inside
#    ``portfolio_report.collect_portfolio_snapshot``.  They push through the
#    installed sink (if any) and always record the last frame on the state
#    so the next view can render the latest status.
# ---------------------------------------------------------------------------

ProgressFrame = Dict[str, Any]


class ProgressCollector:
    """Default sink — records every progress frame in order.

    Adapters that don't care about progress can simply do nothing; the
    wizard still completes.  Tests assert on the captured frames.
    """

    def __init__(self) -> None:
        self.frames: List[ProgressFrame] = []

    def __call__(self, frame: ProgressFrame) -> None:
        self.frames.append(dict(frame))


def _update_progress(
    state: Any,
    *,
    label: str,
    done: int,
    total: int,
    current: Optional[str],
) -> None:
    state.report["progress_total"] = max(
        int(state.report.get("progress_total") or 0), int(total)
    )
    push_progress(state, label=label, done=done, total=total, current=current)


def install_progress_sink(state: Any, sink: Any) -> ProgressCollector:
    """Install a progress sink on ``state.report`` for the next fetch.

    Returns the collector that ends up recording all frames.  Pass
    ``sink=None`` to install a fresh :class:`ProgressCollector` and let
    the caller fetch its frames later.
    """
    if sink is None:
        sink = ProgressCollector()
    state.report["progress_sink"] = sink
    state.report["progress_last"] = None
    state.report.setdefault("progress_history", [])
    return sink  # type: ignore[return-value]


def push_progress(
    state: Any,
    *,
    label: str,
    done: int,
    total: int,
    current: Optional[str] = None,
) -> Optional[ProgressFrame]:
    """Push a progress frame through the installed sink (if any).

    Always updates ``state.report['progress_last']`` so the next view
    render can surface the latest status even if no sink was installed.
    Returns the rendered frame (for adapters that want to render inline).
    """
    safe_total = max(0, int(total))
    safe_done = max(0, int(done))
    pct = 0.0 if safe_total <= 0 else min(100.0, (safe_done * 100.0) / safe_total)
    frame: ProgressFrame = {
        "label": label,
        "pct": pct,
        "done": safe_done,
        "total": safe_total,
        "current": current,
        "text": progress_text(label, pct, done=safe_done, total=safe_total, current=current),
    }
    state.report["progress_last"] = frame
    history = state.report.setdefault("progress_history", [])
    history.append(frame)
    sink = state.report.get("progress_sink")
    if sink is not None:
        try:
            sink(frame)
        except Exception:  # noqa: BLE001
            # A buggy sink must never break a fetch.
            logger.exception("summative progress sink raised")
    return frame


def begin_finalizing(state: Any) -> None:
    """Flip the installed sink to FINALIZING.  No further edits go out.

    Idempotent.  After this point the sink's ``__call__`` returns without
    calling ``editMessageText`` so a delayed progress edit cannot overwrite
    the dashboard render.
    """
    sink = state.report.get("progress_sink") if isinstance(state.report, dict) else None
    begin = getattr(sink, "begin_finalizing", None)
    if callable(begin):
        try:
            begin()
        except Exception:  # noqa: BLE001
            logger.exception("summative progress sink begin_finalizing raised")
    state.report["progress_finalized"] = True


def complete_progress(state: Any) -> None:
    """Mark the installed sink COMPLETED.  Idempotent."""
    sink = state.report.get("progress_sink") if isinstance(state.report, dict) else None
    complete = getattr(sink, "complete", None)
    if callable(complete):
        try:
            complete()
        except Exception:  # noqa: BLE001
            logger.exception("summative progress sink complete raised")
    state.report["progress_completed"] = True


def finalize_progress(state: Any, *, label: str = "Fetch complete") -> ProgressFrame:
    """Emit a final 100% progress frame and return it.

    Uses the most recent emitted frame's ``done``/``total`` when available;
    falls back to the ``total_targets`` hint when no frames were emitted
    (e.g. an exchange with zero accounts).
    """
    last = state.report.get("progress_last") or {}
    done = int(last.get("done") or 0)
    total = int(last.get("total") or state.report.get("progress_total") or done)
    if total <= 0:
        total = max(total, done)
    if total <= 0:
        total = 1
    frame = push_progress(state, label=label, done=total, total=total)
    if frame is None:
        frame = {
            "label": label,
            "pct": 100.0,
            "done": total,
            "total": total,
            "text": progress_text(label, 100.0, done=total, total=total),
        }
    return frame


def last_progress(state: Any) -> Optional[ProgressFrame]:
    return state.report.get("progress_last")


def _b(text: str, callback_data: str) -> Dict[str, str]:
    return {"text": text, "callback_data": callback_data}


def _report_snapshot(desk: Any, state: Any, *, refresh: bool = False) -> Any:
    snap = state.report.get("snapshot")
    if refresh or snap is None:
        # Install a default in-memory sink before the fetch so every
        # exchange/account emits a frame the next view can render.  Callers
        # (e.g. the Telegram adapter) can replace this with their own sink
        # by writing ``state.report['progress_sink']`` immediately before
        # invoking ``handle``.
        if state.report.get("progress_sink") is None:
            install_progress_sink(state, None)
        progress_state = state
        snap = portfolio.collect_portfolio_snapshot(
            desk,
            progress=lambda done, total, current=None: _update_progress(
                progress_state,
                label="Fetching positions",
                done=done,
                total=total,
                current=current,
            ),
        )
        state.report["snapshot"] = snap
        state.report["page"] = 0
        state.report["net_page"] = 0
        finalize_progress(state, label="Fetch complete")
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


def progress_bar(pct: float, *, width: int = 10) -> str:
    """Return a Unicode progress bar that fills ``0..100`` over ``width`` slots."""
    safe_width = max(1, int(width))
    clamped = max(0.0, min(100.0, float(pct)))
    filled = int(round((clamped / 100.0) * safe_width))
    filled = max(0, min(safe_width, filled))
    return "[" + ("█" * filled) + ("░" * (safe_width - filled)) + "]"


def progress_text(
    label: str,
    pct: float,
    *,
    done: int,
    total: int,
    current: Optional[str] = None,
) -> str:
    """Render a single-line progress message used for editMessageText updates."""
    line = f"🔄 {label}…\n{progress_bar(pct)} {pct:5.1f}%   ({done}/{total})"
    if current:
        line += f"\nCurrent: {current}"
    return line


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


def _standard_nav(*, back: str = "sr:m", show_home: bool = True) -> List[Dict[str, str]]:
    if show_home:
        return [_b("◀ Back", back), _b("🏠 Home", "sr:h"), _b("🔄 Refresh", "sr:refresh")]
    return [_b("◀ Back", back), _b("🔄 Refresh", "sr:refresh")]


def _footer(
    *,
    back: str = "sr:m",
    refresh: bool = True,
    extra: Optional[List[List[Dict[str, str]]]] = None,
) -> List[List[Dict[str, str]]]:
    rows: List[List[Dict[str, str]]] = []
    if extra:
        rows.extend(extra)
    if refresh:
        rows.append(_standard_nav(back=back))
    else:
        rows.append([_b("◀ Back", back), _b("🏠 Home", "sr:h")])
    return rows


def _view_settings(state: Any) -> Dict[str, Any]:
    settings = state.report.get("settings")
    if not isinstance(settings, dict):
        settings = {
            "net_filter": "all",   # all | long | short
            "net_sort": "gross",   # gross | net | pnl
            "page_size": _NET_PAGE_SIZE,
        }
        state.report["settings"] = settings
    return settings


def _format_pnl_units(units: Dict[str, Any]) -> str:
    if not units:
        return "—"
    pieces = [
        f"{portfolio.format_money(amount, signed=True)} {unit}"
        for unit, amount in sorted(units.items())
    ]
    return "; ".join(pieces)


def _format_signed(value: Any) -> str:
    if value is None:
        return "—"
    try:
        q = portfolio._q2(value)
    except Exception:  # noqa: BLE001
        return str(value)
    if q is None:
        return str(value)
    return f"{q:+.2f}" if q >= 0 else f"{q:.2f}"


def _format_abs(value: Any) -> str:
    if value is None:
        return "—"
    try:
        q = portfolio._q2(abs(value))
    except Exception:  # noqa: BLE001
        return str(value)
    if q is None:
        return str(value)
    return f"{q:.2f}"


def _menu(desk: Any, state: Any, *, refresh: bool = False) -> ReportView:
    snap = _report_snapshot(desk, state, refresh=refresh)
    state.report["view"] = "menu"
    scope = _scope(state, snap)
    selected = "All Exchanges"
    if state.report.get("filter_exchange"):
        selected = str(state.report["filter_exchange"])
        if state.report.get("filter_account"):
            selected += f" / {state.report['filter_account']}"

    equity = portfolio.total_equity(scope)
    pnl = portfolio.total_floating_pnl(scope)
    net_rows = portfolio.net_by_instrument(scope)
    risk = portfolio.risk_and_exposure(scope)

    net_rows_top = sorted(
        net_rows,
        key=lambda r: (-(r["gross"]), -(abs(r["net"])), r["instrument"]),
    )[:4]

    lines: List[str] = [
        "📊 Summative Report",
        "",
        _freshness(scope),
        f"Scope: {selected}",
    ]

    counts = (
        f"Open Positions: {len(scope.positions)}"
        f"   DEX: {len({a.exchange for a in scope.accounts})}"
        f"   Accounts: {scope.positions_accounts_ok}/{scope.positions_accounts_seen}"
    )
    lines.append(counts)

    lines += ["", "Equity (unconverted):"]
    if equity["equity_units"]:
        _native_lines(lines, "  Equity", equity["equity_units"], signed=False)
    else:
        lines.append("  Equity: unavailable")

    lines += ["", "uPnL (unconverted):"]
    if pnl["units"]:
        _native_lines(lines, "  PnL", pnl["units"], signed=True)
    else:
        lines.append("  uPnL: unavailable")

    if net_rows_top:
        lines += ["", "Top instruments (size, not USD notional):"]
        for row in net_rows_top:
            pnl_text = _format_pnl_units(row["pnl_units"])
            lines.append(
                f"• {row['display_symbol']}: "
                f"net {_format_signed(row['net'])} "
                f"gross {_format_abs(row['gross'])} "
                f"uPnL {pnl_text} "
                f"({row['accounts']} acct)"
            )
    else:
        lines += ["", "Top instruments: none"]

    lines += ["", "Closest liquidation: unavailable (canonical contract lacks liquidation price)."]
    if risk["notional_incomplete"]:
        lines.append("USD notional: N/A (canonical contract lacks multiplier/inverse metadata).")
    _coverage_lines(lines, scope)

    buttons: List[List[Dict[str, str]]] = [
        [_b("📈 Net Positions", "sr:n"), _b("🏛 By Exchange", "sr:e")],
        [_b("📊 By Symbol", "sr:p"), _b("⚠️ Risks", "sr:r")],
        [_b("⚡ Actions", "sr:a"), _b("🔍 Filter", "sr:f")],
        [_b("🔄 Refresh", "sr:refresh"), _b("⚙️ Settings", "sr:s")],
        [_b("◀ Back", "back"), _b("🏠 Home", "sr:h"), _b("✕ Exit", "exit")],
    ]
    return ReportView("\n".join(lines), buttons)


def _net_positions_view(desk: Any, state: Any, *, refresh: bool = False) -> ReportView:
    snapshot = _scope(state, _report_snapshot(desk, state, refresh=refresh))
    state.report["view"] = "net_positions"
    settings = _view_settings(state)
    rows = portfolio.net_by_instrument(snapshot)

    filter_kind = settings.get("net_filter", "all")
    if filter_kind == "long":
        rows = [r for r in rows if r["net"] > 0]
    elif filter_kind == "short":
        rows = [r for r in rows if r["net"] < 0]

    sort_kind = settings.get("net_sort", "gross")
    if sort_kind == "gross":
        rows.sort(key=lambda r: (-r["gross"], r["instrument"]))
    elif sort_kind == "net":
        rows.sort(key=lambda r: (abs(r["net"]), r["instrument"]))
    elif sort_kind == "pnl":
        def _pnl_key(r: Dict[str, Any]) -> Any:
            values = list(r["pnl_units"].values())
            return -max((abs(v) for v in values), default=portfolio.Decimal("0"))
        rows.sort(key=_pnl_key)

    state.report["net_rows"] = rows
    page = max(0, int(state.report.get("net_page", 0)))
    page_size = max(1, int(settings.get("page_size", _NET_PAGE_SIZE)))
    page_count = max(1, (len(rows) + page_size - 1) // page_size)
    page = min(page, page_count - 1)
    state.report["net_page"] = page
    begin = page * page_size
    visible = rows[begin : begin + page_size]

    lines = [
        "📈 Net Positions",
        "",
        _freshness(snapshot),
        f"Sort: {sort_kind}   Filter: {filter_kind}",
        "size (qty), not USD notional — multiplier/inverse contract metadata is not available.",
        "",
    ]
    if not visible:
        lines.append("No open positions.")
    for row in visible:
        pnl_text = _format_pnl_units(row["pnl_units"])
        lines.append(
            f"{row['display_symbol']}    "
            f"net {_format_signed(row['net'])}   "
            f"gross {_format_abs(row['gross'])}   "
            f"uPnL {pnl_text}   "
            f"({row['accounts']} acct)"
        )
    _coverage_lines(lines, snapshot)

    buttons: List[List[Dict[str, str]]] = [
        [_b("Long only", "sr:long"), _b("Short only", "sr:short")],
        [
            _b("Sort Net", "sr:snet"),
            _b("Sort Gross", "sr:sgross"),
            _b("Sort PnL", "sr:spnl"),
        ],
    ]
    for index, row in enumerate(visible, begin):
        buttons.append([_b(f"{row['display_symbol']} details", f"sr:nd:{index}")])
    if page_count > 1:
        nav: List[Dict[str, str]] = []
        if page > 0:
            nav.append(_b("◀ Prev", "sr:np"))
        nav.append(_b(f"{page + 1}/{page_count}", "sr:n"))
        if page + 1 < page_count:
            nav.append(_b("Next ▶", "sr:nn"))
        buttons.append(nav)
    buttons.append(_standard_nav(back="sr:m"))
    return ReportView("\n".join(lines), buttons)


def _net_position_detail(state: Any, index_text: str) -> ReportView:
    rows = state.report.get("net_rows") or []
    try:
        row = rows[int(index_text)]
    except (ValueError, IndexError, TypeError):
        return _net_positions_view(None, state)
    instrument = row["instrument"]
    pnl_text = _format_pnl_units(row["pnl_units"])

    lines = [
        f"📈 {row['display_symbol']}",
        "",
        f"net   {_format_signed(row['net'])}",
        f"gross {_format_abs(row['gross'])}   (size qty)",
        f"uPnL  {pnl_text}   ({row['accounts']} acct)",
        "",
    ]

    snapshot = state.report.get("snapshot")
    contributors: List[Any] = []
    if snapshot is not None:
        scoped = _scope(state, snapshot)
        for pos in scoped.positions:
            if pos.instrument_key != instrument:
                continue
            contributors.append(pos)
    if not contributors:
        lines.append("No contributing positions cached.")

    if contributors:
        lines.append("Open positions:")
        for pos in contributors[:12]:
            pnl_str = (
                f"{portfolio.format_money(pos.pnl, signed=True)} {pos.pnl_unit}"
                if pos.pnl is not None
                else "—"
            )
            size_str = format(pos.size, "f") if pos.size is not None else "—"
            lines.append(
                f"• {pos.exchange}/{pos.account_label}: {pos.symbol} {pos.side} "
                f"size {size_str} · PnL {pnl_str}"
            )
        if len(contributors) > 12:
            lines.append(f"• …and {len(contributors) - 12} more")

    lines += [
        "",
        "Future action buttons (read-only stub):",
    ]

    buttons: List[List[Dict[str, str]]] = [
        [
            _b("Set TP", "sr:atp"),
            _b("Set SL", "sr:asl"),
        ],
        [
            _b("Close 50%", "sr:c50"),
            _b("Close 100%", "sr:c100"),
        ],
    ]
    buttons.append(_standard_nav(back="sr:n"))
    return ReportView("\n".join(lines), buttons)


def _actions_menu(desk: Any, state: Any) -> ReportView:
    state.report["view"] = "actions"
    snapshot = _scope(state, _report_snapshot(desk, state))
    lines = [
        "⚡ Actions",
        "",
        _freshness(snapshot),
        "",
        "Bulk future operations — read-only stubs.",
        "These buttons do NOT submit, cancel, transfer, or modify accounts.",
        "",
        "When enabled, this menu will offer:",
        "• Close all positions on a symbol across all exchanges",
        "• Close all positions on an exchange",
        "• Cancel all open orders on an exchange",
        "",
        "Every action requires an explicit confirmation step before submitting.",
    ]
    buttons: List[List[Dict[str, str]]] = [
        [_b("Close symbol — choose", "sr:asym")],
        [_b("Close exchange — choose", "sr:aex")],
        [_b("Cancel all orders — confirm", "sr:acancel")],
    ]
    buttons.append(_standard_nav(back="sr:m"))
    return ReportView("\n".join(lines), buttons)


def _settings_menu(desk: Any, state: Any) -> ReportView:
    state.report["view"] = "settings"
    snapshot = _scope(state, _report_snapshot(desk, state))
    settings = _view_settings(state)
    lines = [
        "⚙️ Settings",
        "",
        _freshness(snapshot),
        "",
        f"Default Net sort: {settings.get('net_sort', 'gross')}",
        f"Default Net filter: {settings.get('net_filter', 'all')}",
        f"Page size: {settings.get('page_size', _NET_PAGE_SIZE)}",
        "",
        "Settings are read-only here; persist them via the wizard config.",
    ]
    buttons: List[List[Dict[str, str]]] = [
        [_b("Sort default: Gross", "sr:sgross")],
        [_b("Sort default: Net", "sr:snet")],
        [_b("Sort default: PnL", "sr:spnl")],
    ]
    buttons.append(_standard_nav(back="sr:m"))
    return ReportView("\n".join(lines), buttons)


def _home(desk: Any, state: Any) -> ReportView:
    return _menu(desk, state, refresh=False)


def _filter_exchanges(desk: Any, state: Any) -> ReportView:
    snapshot = _report_snapshot(desk, state)
    exchanges = portfolio.list_exchanges(snapshot)
    state.report["filter_exchanges"] = exchanges
    lines = ["🔎 Filter Exchange / Account", "", "All Exchanges → Exchange → Account → Open Positions"]
    buttons: List[List[Dict[str, str]]] = [[_b("All Exchanges", "sr:all")]]
    for index, exchange in enumerate(exchanges):
        buttons.append([_b(exchange, f"sr:fx:{index}")])
    buttons.append(_standard_nav(back="sr:m"))
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
    buttons.append(_standard_nav(back="sr:f"))
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
    buttons.append(_standard_nav(back="sr:e"))
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
    state.report["exchange_rows"] = rows
    buttons: List[List[Dict[str, str]]] = []
    page = max(0, int(state.report.get("exchange_page", 0)))
    page_size = _PAGE_SIZE
    page_count = max(1, (len(rows) + page_size - 1) // page_size)
    page = min(page, page_count - 1)
    state.report["exchange_page"] = page
    begin = page * page_size
    visible = rows[begin : begin + page_size]
    for index, row in enumerate(visible, begin):
        buttons.append([_b(f"{row['exchange']} accounts", f"sr:ex:{index}")])
    if page_count > 1:
        nav: List[Dict[str, str]] = []
        if page > 0:
            nav.append(_b("◀ Prev", "sr:ep"))
        nav.append(_b(f"{page + 1}/{page_count}", "sr:e"))
        if page + 1 < page_count:
            nav.append(_b("Next ▶", "sr:en"))
        buttons.append(nav)
    buttons.append(_standard_nav(back="sr:m"))
    return ReportView("\n".join(lines), buttons)


def _positions(desk: Any, state: Any) -> ReportView:
    snapshot = _scope(state, _report_snapshot(desk, state))
    state.report["view"] = "positions"
    groups = portfolio.positions_by_symbol(snapshot)
    state.report["position_groups"] = groups
    page = max(0, int(state.report.get("page", 0)))
    page_size = _PAGE_SIZE
    page_count = max(1, (len(groups) + page_size - 1) // page_size)
    page = min(page, page_count - 1)
    state.report["page"] = page
    begin = page * page_size
    rows = groups[begin : begin + page_size]
    lines = ["📊 Positions by Symbol", "", _freshness(snapshot)]
    if not rows:
        lines += ["", "No open positions."]
    for index, group in enumerate(rows, begin):
        pnl = _format_pnl_units(group.units) if group.pnl_any else "—"
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
    buttons.append(_standard_nav(back="sr:m"))
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


def _set_settings(state: Any, *, sort: Optional[str] = None, filter_kind: Optional[str] = None) -> ReportView:
    settings = _view_settings(state)
    if sort is not None:
        settings["net_sort"] = sort
    if filter_kind is not None:
        settings["net_filter"] = filter_kind
    state.report["net_page"] = 0
    return _net_positions_view(None, state)


def _action_stub(state: Any, action: str) -> ReportView:
    """Read-only stubs for the future Set TP / Set SL / Close 50% / Close 100% buttons."""
    lines = [
        f"🛑 {action}",
        "",
        "Read-only stub.",
        "This action is not enabled yet — it would submit, cancel, or close positions.",
        "When enabled, this screen will collect a confirm step before execution.",
        "",
        "No exchange writes were performed.",
    ]
    return ReportView("\n".join(lines), _standard_nav(back="sr:nd:0"))


def handle(desk: Any, state: Any, suffix: str) -> ReportView:
    """Route a ``sr:`` callback.  Menu and position navigation are live."""
    if suffix in {"summative_report", "sr:m", "sr:refresh"}:
        return _menu(desk, state, refresh=suffix == "sr:refresh")
    if suffix in {"sr:h", "sr:home"}:
        return _home(desk, state)
    if suffix == "sr:n":
        return _net_positions_view(desk, state)
    if suffix == "sr:a":
        return _actions_menu(desk, state)
    if suffix == "sr:s":
        return _settings_menu(desk, state)
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
    if suffix == "sr:ep":
        state.report["exchange_page"] = max(0, int(state.report.get("exchange_page", 0)) - 1)
        return _pnl_by_exchange(desk, state)
    if suffix == "sr:en":
        state.report["exchange_page"] = int(state.report.get("exchange_page", 0)) + 1
        return _pnl_by_exchange(desk, state)
    if suffix == "sr:np":
        state.report["net_page"] = max(0, int(state.report.get("net_page", 0)) - 1)
        return _net_positions_view(desk, state)
    if suffix == "sr:nn":
        state.report["net_page"] = int(state.report.get("net_page", 0)) + 1
        return _net_positions_view(desk, state)
    if suffix.startswith("sr:d:"):
        return _position_detail(state, suffix.rsplit(":", 1)[-1])
    if suffix.startswith("sr:nd:"):
        return _net_position_detail(state, suffix.rsplit(":", 1)[-1])
    if suffix == "sr:long":
        return _set_settings(state, filter_kind="long")
    if suffix == "sr:short":
        return _set_settings(state, filter_kind="short")
    if suffix == "sr:allf":
        return _set_settings(state, filter_kind="all")
    if suffix == "sr:snet":
        return _set_settings(state, sort="net")
    if suffix == "sr:sgross":
        return _set_settings(state, sort="gross")
    if suffix == "sr:spnl":
        return _set_settings(state, sort="pnl")
    if suffix == "sr:atp":
        return _action_stub(state, "Set TP")
    if suffix == "sr:asl":
        return _action_stub(state, "Set SL")
    if suffix == "sr:c50":
        return _action_stub(state, "Close 50%")
    if suffix == "sr:c100":
        return _action_stub(state, "Close 100%")
    if suffix == "sr:asym":
        return _action_stub(state, "Close symbol — choose")
    if suffix == "sr:aex":
        return _action_stub(state, "Close exchange — choose")
    if suffix == "sr:acancel":
        return _action_stub(state, "Cancel all orders — confirm")
    return _menu(desk, state)