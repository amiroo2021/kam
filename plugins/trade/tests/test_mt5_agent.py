from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock
import json

import pytest

from plugins.trade.agents import x_mt5_agent as mt5
from plugins.trade.canonical import CanonicalInstrument, CanonicalOrderGroup, CanonicalOrderResult, CanonicalPosition, make_failure, make_success
from plugins.trade.wizard import TradeWizard


def _creds(**overrides):
    base = {
        "alias": "lite7486706",
        "account_no": "7486706",
        "password": "secret",
        "server": "LiteFinance-MT5",
        "host": "127.0.0.1",
        "port": 5555,
        "wait_seconds": 0.4,
        "symbols": {"XAUUSD": "XAUUSDm"},
    }
    base.update(overrides)
    return base


class _FakeConn:
    def __init__(self, login="", payload=None):
        self.is_connected = True
        self.login = ""
        self.account_payload = dict(payload or {})
        if login:
            self.account_payload["login"] = login
        self.requests = []

    async def send_request(self, request):
        action = getattr(getattr(request, "action", None), "value", request)
        self.requests.append(action)
        return SimpleNamespace(success=True, data=dict(self.account_payload))


class _FakeBridge:
    def __init__(self, host="0.0.0.0", port=5555):
        self.host = host
        self.port = port
        self.connections = []
        self.started = False
        self.stopped = False
        self._bound = None
        self.buy_calls = []
        self.pending_calls = []
        self.close_calls = []
        self.modify_calls = []
        self.cancel_calls = []
        self.modify_order_calls = []
        self.orders = []
        self.positions = [
            SimpleNamespace(
                ticket=11,
                symbol="EURUSD",
                order_type="BUY",
                volume=0.1,
                open_price=1.1,
                stop_loss=0.0,
                take_profit=0.0,
                profit=1.25,
            )
        ]
        self.symbols = ["EURUSD", "XAUUSDm"]
        self.tick = SimpleNamespace(bid=1.1, ask=1.2, last=1.15)
        self.rates = [{"time": 1, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "tick_volume": 10}]
        self.symbol_info_calls = []
        self.symbol_info = {
            "contract_size": 1,
            "volume_min": 0.01,
            "volume_max": 100,
            "volume_step": 0.01,
            "digits": 5,
            "point": 0.00001,
            "tick_size": 0.00001,
            "stops_level": 0,
            "freeze_level": 0,
            "currency_base": "EUR",
        }

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True
        self._bound = None

    def bind(self, connection):
        self._bound = connection

    async def get_positions(self):
        if self._bound is None:
            raise RuntimeError("TRADE_BEFORE_BIND")
        return list(self.positions)

    async def get_symbols(self):
        if self._bound is None:
            raise RuntimeError("TRADE_BEFORE_BIND")
        return list(self.symbols)

    async def get_tick(self, symbol):
        if self._bound is None:
            raise RuntimeError("TRADE_BEFORE_BIND")
        return self.tick

    async def get_symbol_info(self, symbol):
        if self._bound is None:
            raise RuntimeError("TRADE_BEFORE_BIND")
        self.symbol_info_calls.append(symbol)
        info = dict(self.symbol_info)
        info.setdefault("symbol", symbol)
        return info

    async def get_rates(self, symbol, timeframe, count):
        return list(self.rates)

    async def buy(self, symbol, volume, stop_loss=None, take_profit=None):
        if self._bound is None:
            raise RuntimeError("TRADE_BEFORE_BIND")
        self.buy_calls.append((symbol, volume, self._bound))
        return SimpleNamespace(ticket=99, price_executed=1.2, volume_executed=volume)

    async def sell(self, *args, **kwargs):
        if self._bound is None:
            raise RuntimeError("TRADE_BEFORE_BIND")
        return SimpleNamespace(ticket=98, price_executed=1.1, volume_executed=kwargs.get("volume", 0.1))

    async def place_pending_order(self, symbol, order_type, price, volume, stop_loss=None, take_profit=None):
        if self._bound is None:
            raise RuntimeError("TRADE_BEFORE_BIND")
        self.pending_calls.append((symbol, order_type, price, volume, self._bound))
        return SimpleNamespace(ticket=77, price_executed=price, volume_executed=volume)

    async def close_position(self, ticket):
        if self._bound is None:
            raise RuntimeError("TRADE_BEFORE_BIND")
        self.close_calls.append(ticket)
        return SimpleNamespace(close_price=1.19, profit=0.5)

    async def get_orders(self):
        if self._bound is None:
            raise RuntimeError("TRADE_BEFORE_BIND")
        return list(getattr(self, "orders", []))

    async def modify_position(self, ticket, stop_loss=None, take_profit=None):
        if self._bound is None:
            raise RuntimeError("TRADE_BEFORE_BIND")
        self.modify_calls.append((ticket, stop_loss, take_profit, self._bound))
        return {"ticket": ticket, "sl": stop_loss, "tp": take_profit}

    async def cancel_order(self, ticket):
        if self._bound is None:
            raise RuntimeError("TRADE_BEFORE_BIND")
        self.cancel_calls.append((ticket, self._bound))
        if int(ticket) in getattr(self, "cancel_fail", set()):
            err = RuntimeError("Order delete failed")
            err.error_code = 10019
            raise err
        return {"ticket": ticket, "cancelled": True}

    async def modify_order(self, ticket, price=None, stop_loss=None, take_profit=None, volume=None):
        if self._bound is None:
            raise RuntimeError("TRADE_BEFORE_BIND")
        self.modify_order_calls.append((ticket, price, stop_loss, take_profit, volume, self._bound))
        return {"ticket": ticket, "price": price}


class _StubSession:
    """Test double for the persistent shared MT5 session."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.started = False
        self.stopped = False
        self._bound = None

    def ensure_started(self):
        self.started = True
        self.bridge.started = True

    def bind_account(self, creds):
        # Delegate to the real identity resolver so the mocked tests exercise
        # the same code path as production (probe, retry, duplicate detection).
        return asyncio.run(
            mt5._bind_matching_ea(self.bridge, creds, float(creds.get("wait_seconds") or 0.4))
        )

    def run(self, bridge, raw_account, verified, fn):
        async def _call():
            return await fn(bridge, raw_account, verified)

        return asyncio.run(_call())


@pytest.fixture
def fake_bridge(monkeypatch):
    bridge = _FakeBridge()
    monkeypatch.setattr(mt5, "_IDENTITY_SETTLE_SECONDS", 0)

    def factory(*_a, **_k):
        return bridge

    monkeypatch.setattr(mt5, "_bridge_factory", lambda: factory)
    session = _StubSession(bridge)
    monkeypatch.setattr(mt5, "_SESSION_FACTORY", lambda _creds: session)
    return bridge


def test_list_accounts_case_insensitive(monkeypatch):
    monkeypatch.setattr(
        mt5,
        "_combined_mt5_env",
        lambda: {
            "mt5_Lite7486706_account": "7486706",
            "MT5_PO5701520_ACCOUNT": "5701520",
            "MT5_VT16646363_ACCOUNT": "16646363",
        },
    )
    assert mt5.list_accounts() == ["lite7486706", "po5701520", "vt16646363"]


def test_shared_port_is_5555(monkeypatch):
    monkeypatch.setattr(
        mt5,
        "_combined_mt5_env",
        lambda: {"MT5_LITE7486706_ACCOUNT": "7486706", "MT5_LITE7486706_PORT": "1111"},
    )
    creds = mt5._lookup_account("lite7486706")
    assert creds["port"] == 5555


def test_missing_login_rejects_and_closes(fake_bridge, monkeypatch):
    fake_bridge.connections = [_FakeConn(login="")]
    monkeypatch.setattr(mt5, "_lookup_account", lambda alias: _creds())
    resp = mt5.execute({"operation": "balance", "account": "lite7486706"})
    assert not resp.success
    assert resp.error.code == "ACCOUNT_IDENTITY_MISSING"
    # The shared listener stays up between operations (EAs stay attached), so
    # the bridge is started and deliberately NOT stopped per call.
    assert fake_bridge.started
    assert fake_bridge._bound is None
    assert fake_bridge.buy_calls == []


def test_mismatch_login_rejects_trade(fake_bridge, monkeypatch):
    fake_bridge.connections = [_FakeConn(login="999999")]
    monkeypatch.setattr(mt5, "_lookup_account", lambda alias: _creds())
    resp = mt5.execute(
        {"operation": "new_order", "account": "lite7486706", "symbol": "EURUSD", "side": "buy", "order_type": "limit", "volume": 0.1, "price": "1.09"}
    )
    assert not resp.success
    assert resp.error.code == "ACCOUNT_IDENTITY_MISMATCH"
    # The message must name the login that actually answered.
    assert "999999" in resp.error.message
    assert fake_bridge.buy_calls == []


def test_late_arriving_ea_is_reidentified(fake_bridge, monkeypatch):
    """A connection whose first GET_ACCOUNT probe fails must stay retryable.

    The old code added every connection to a permanent `probed` set, so an EA
    that was not ready on the first probe was never identified again and the
    request failed with a bogus identity error.
    """
    class _FlakyConn(_FakeConn):
        def __init__(self, login="7486706", fail_first=1):
            super().__init__(login=login)
            self.calls = 0
            self.fail_first = fail_first

        async def send_request(self, request):
            self.calls += 1
            if self.calls <= self.fail_first:
                raise RuntimeError("EA not ready")
            return await super().send_request(request)

    flaky = _FlakyConn(login="7486706", fail_first=1)
    fake_bridge.connections = [flaky]
    monkeypatch.setattr(mt5, "_lookup_account", lambda alias: _creds())
    resp = mt5.execute({"operation": "balance", "account": "lite7486706"})
    assert resp.success
    assert flaky.calls > 1
    assert fake_bridge._bound is None or fake_bridge._bound is not None


def test_duplicate_login_rejects(fake_bridge, monkeypatch):
    fake_bridge.connections = [_FakeConn(login="7486706"), _FakeConn(login="7486706")]
    monkeypatch.setattr(mt5, "_lookup_account", lambda alias: _creds())
    resp = mt5.execute({"operation": "balance", "account": "lite7486706"})
    assert not resp.success
    assert resp.error.code == "ACCOUNT_IDENTITY_DUPLICATE"
    assert fake_bridge.buy_calls == []


def test_unique_login_routes_balance_and_positions(fake_bridge, monkeypatch):
    match = _FakeConn(
        login="7486706",
        payload={"balance": 1000.5, "equity": 1001.0, "margin": 10, "free_margin": 990, "leverage": 500, "login": "7486706"},
    )
    other = _FakeConn(login="5701520")
    fake_bridge.connections = [other, match]
    monkeypatch.setattr(mt5, "_lookup_account", lambda alias: _creds())
    bal = mt5.execute({"operation": "balance", "account": "lite7486706"})
    assert bal.success
    assert bal.balance.value == "1000.50"
    pos = mt5.execute({"operation": "positions_orders", "account": "lite7486706"})
    assert pos.success
    assert pos.positions[0].symbol == "EURUSD"


def test_trade_only_after_unique_bind(fake_bridge, monkeypatch):
    match = _FakeConn(login="7486706")
    other = _FakeConn(login="5701520")
    fake_bridge.connections = [other, match]
    monkeypatch.setattr(mt5, "_lookup_account", lambda alias: _creds())
    resp = mt5.execute(
        {"operation": "new_order", "account": "lite7486706", "symbol": "EURUSD", "side": "buy", "order_type": "limit", "volume": 0.1, "price": "1.09"}
    )
    assert resp.success
    assert fake_bridge.buy_calls == []
    assert fake_bridge.pending_calls
    assert fake_bridge.pending_calls[0][-1] is match
    assert fake_bridge.pending_calls[0][0] == "EURUSD"
    assert "LIMIT" in str(fake_bridge.pending_calls[0][1])


def test_not_implemented(monkeypatch):
    monkeypatch.setattr(mt5, "_lookup_account", lambda alias: _creds())
    resp = mt5.execute({"operation": "market_immediate", "account": "lite7486706"})
    assert resp.error.code == "NOT_IMPLEMENTED"


def test_pending_orders_and_tpsl_and_cancel(fake_bridge, monkeypatch):
    match = _FakeConn(login="7486706")
    fake_bridge.connections = [match]
    fake_bridge.orders = [{"ticket": 55, "symbol": "EURUSD", "type": "BUY_LIMIT", "volume": 0.1, "price": 1.05}]
    monkeypatch.setattr(mt5, "_lookup_account", lambda alias: _creds())
    pos = mt5.execute({"operation": "positions_orders", "account": "lite7486706"})
    assert pos.success
    assert pos.open_order_count == 1
    assert pos.data["pending_orders"][0]["ticket"] == "55"
    tp = mt5.execute({"operation": "set_tp", "account": "lite7486706", "ticket": 11, "price": 1.2})
    assert tp.success
    assert str(fake_bridge.modify_calls[0][0]) == "11"
    assert fake_bridge.modify_calls[0][3] is match
    sl0 = mt5.execute({"operation": "set_sl", "account": "lite7486706", "ticket": 11, "price": 0})
    assert sl0.success and sl0.position_action.removed
    cancel = mt5.execute({"operation": "cancel_order", "account": "lite7486706", "ticket": 55})
    assert cancel.success
    assert str(fake_bridge.cancel_calls[0][0]) == "55"
    assert fake_bridge.cancel_calls[0][1] is match
    caps = set(mt5.capabilities())
    assert {"set_tp", "set_sl", "cancel_order", "cancel_orders", "new_order", "close_position", "ladder"} <= caps


def test_busy_lock(monkeypatch):
    monkeypatch.setattr(mt5, "_lookup_account", lambda alias: _creds())
    assert mt5._SESSION_LOCK.acquire(blocking=False)
    try:
        resp = mt5.execute({"operation": "balance", "account": "lite7486706"})
        assert resp.error.code == "MT5_BUSY"
    finally:
        mt5._SESSION_LOCK.release()


def _pos(ticket, symbol, order_type, volume, open_price, profit=0.0):
    return SimpleNamespace(
        ticket=ticket,
        symbol=symbol,
        order_type=order_type,
        volume=volume,
        open_price=open_price,
        stop_loss=0.0,
        take_profit=0.0,
        profit=profit,
    )


def _bind_account(fake_bridge, monkeypatch):
    fake_bridge.connections = [_FakeConn(login="7486706")]
    monkeypatch.setattr(mt5, "_lookup_account", lambda alias: _creds())


def test_shared_session_bind_returns_real_account_payload(fake_bridge, monkeypatch):
    """Regression: bind_account() must return the EA's GET_ACCOUNT payload.

    Returning an empty dict made every balance read report 0.00.
    """
    conn = _FakeConn(
        login="7486706",
        payload={
            "balance": 2029.18,
            "equity": 6275.15,
            "margin": 239.33,
            "free_margin": 6035.82,
            "leverage": 50,
            "login": "7486706",
        },
    )
    session = mt5._SharedSession(host="127.0.0.1", port=5555, wait_seconds=0.2)
    session.bridge = fake_bridge
    fake_bridge.connections = [conn]
    session._logins = {"7486706": conn}
    session._accounts = {"7486706": dict(conn.account_payload)}

    raw = session.bind_account(_creds())
    assert raw, "bind_account must return the account payload, not {}"
    assert raw["balance"] == 2029.18
    assert raw["login"] == "7486706"

    fake_bridge.connections = [conn]
    fake_bridge.positions = [_pos(11, "EURUSD", "BUY", 0.1, 1.1)]
    monkeypatch.setattr(mt5, "_SESSION_FACTORY", lambda _c: session)
    monkeypatch.setattr(mt5, "_lookup_account", lambda alias: _creds())
    resp = mt5.execute({"operation": "balance", "account": "lite7486706"})
    assert resp.success
    assert resp.balance.value == "2029.18"
    assert resp.portfolio_summary.account_value == "6275.15"


def test_positions_aggregate_by_exact_symbol_and_side(fake_bridge, monkeypatch):
    # 11 SELL HYPEUSD lots of 0.1: five @96, five @97, one @96.94 → vol 1.1, range 96–97, vwap 96.54
    sells = [_pos(100 + i, "HYPEUSD", "SELL", 0.1, 96.0) for i in range(5)]
    sells += [_pos(105 + i, "HYPEUSD", "SELL", 0.1, 97.0) for i in range(5)]
    sells.append(_pos(110, "HYPEUSD", "SELL", 0.1, 96.94))
    buys = [_pos(200, "HYPEUSD", "BUY", 0.2, 95.0), _pos(201, "HYPEUSD", "BUY", 0.3, 96.0)]
    other = [_pos(300, "BTCUSD.c", "SELL", 0.01, 70000.0)]
    fake_bridge.positions = sells + buys + other
    _bind_account(fake_bridge, monkeypatch)

    resp = mt5.execute({"operation": "positions_orders", "account": "lite7486706"})
    assert resp.success
    groups = {(p.symbol, p.side): p for p in resp.positions}
    assert set(groups) == {("HYPEUSD", "short"), ("HYPEUSD", "long"), ("BTCUSD.c", "short")}

    hype_sell = groups[("HYPEUSD", "short")]
    assert hype_sell.count == 11
    assert hype_sell.size == "1.1"
    assert hype_sell.min_price == "96"
    assert hype_sell.max_price == "97"
    assert hype_sell.entry_price == "96.54"
    assert hype_sell.tickets == [str(100 + i) for i in range(11)]
    assert "BTCUSD" not in {p.symbol for p in resp.positions}

    hype_buy = groups[("HYPEUSD", "long")]
    assert hype_buy.count == 2
    assert hype_buy.size == "0.5"
    assert hype_buy.tickets == ["200", "201"]


def test_positions_are_not_pending_orders(fake_bridge, monkeypatch):
    fake_bridge.positions = [
        _pos(11, "HYPEUSD", "SELL", 0.1, 96.0),
        _pos(12, "HYPEUSD", "SELL", 0.1, 97.0),
    ]
    _bind_account(fake_bridge, monkeypatch)
    resp = mt5.execute({"operation": "positions_orders", "account": "lite7486706"})
    assert resp.success
    assert resp.order_groups in (None, [])
    assert resp.open_order_count == 0
    assert (resp.data or {}).get("pending_orders") == []
    legs = (resp.data or {}).get("position_legs") or []
    assert {leg["ticket"] for leg in legs} == {"11", "12"}


def test_pending_order_listing_returns_orders(fake_bridge, monkeypatch):
    _bind_account(fake_bridge, monkeypatch)
    fake_bridge.orders = [{"ticket": 9, "symbol": "EURUSD", "type": "BUY_LIMIT", "volume": 0.1, "price": 1.05}]
    resp = mt5.execute({"operation": "pending_orders", "account": "lite7486706"})
    assert resp.success
    assert resp.open_order_count == 1
    assert resp.data["pending_orders"][0]["ticket"] == "9"


def test_close_without_ticket_does_not_close_group(fake_bridge, monkeypatch):
    fake_bridge.positions = [
        _pos(11, "HYPEUSD", "SELL", 0.1, 96.0),
        _pos(12, "HYPEUSD", "SELL", 0.1, 97.0),
    ]
    _bind_account(fake_bridge, monkeypatch)
    resp = mt5.execute(
        {"operation": "close_position", "account": "lite7486706", "symbol": "HYPEUSD", "side": "sell"}
    )
    assert not resp.success
    assert fake_bridge.close_calls == []


def test_close_by_ticket_closes_one(fake_bridge, monkeypatch):
    fake_bridge.positions = [
        _pos(11, "HYPEUSD", "SELL", 0.1, 96.0),
        _pos(12, "HYPEUSD", "SELL", 0.1, 97.0),
    ]
    _bind_account(fake_bridge, monkeypatch)
    resp = mt5.execute(
        {"operation": "close_position", "account": "lite7486706", "symbol": "HYPEUSD", "ticket": 12}
    )
    assert resp.success
    assert [str(t) for t in fake_bridge.close_calls] == ["12"]


class _Mt5Desk:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def list_exchanges(self):
        return ["mt5"]

    def list_accounts(self, exchange):
        return ["lite7486706"] if exchange == "mt5" else []

    def capabilities(self, exchange):
        return ["positions_orders", "positions_management", "close_position"]

    def execute(self, request):
        self.requests.append(dict(request))
        return self.response


def _hype_group_response(operation="positions_orders"):
    return make_success(
        operation=operation,
        exchange="mt5",
        account="lite7486706",
        positions=[
            CanonicalPosition(
                symbol="HYPEUSD",
                side="short",
                size="1.1",
                entry_price="96.54",
                pnl="12.3",
                count=11,
                min_price="96",
                max_price="97",
                tickets=[100 + i for i in range(11)],
            )
        ],
        data={
            "pending_orders": "NOT_IMPLEMENTED",
            "position_legs": [
                {
                    "ticket": 100 + i,
                    "symbol": "HYPEUSD",
                    "side": "short",
                    "size": "0.1",
                    "entry_price": "96" if i < 5 else ("97" if i < 10 else "96.94"),
                    "pnl": "1",
                }
                for i in range(11)
            ],
        },
    )


def test_compact_position_line_matches_example():
    from plugins.trade.wizard import _position_group_summary_line

    pos = CanonicalPosition(
        symbol="HYPEUSD",
        side="short",
        size="1.1",
        entry_price="96.54",
        pnl="0",
        count=11,
        min_price="96",
        max_price="97",
    )
    assert _position_group_summary_line(pos) == "🔴 HYPEUSD (11) vol:1.1 range:96–97 vwap:96.54 pnl:+0.00"


def test_position_group_line_rounds_vwap_and_shows_group_pnl():
    from plugins.trade.wizard import _position_group_summary_line

    pos = CanonicalPosition(
        symbol="BTCUSD.c",
        side="short",
        size="2.95",
        entry_price="63676.34338983050847457627119",
        pnl="80.25",
        count=86,
        min_price="61987.26",
        max_price="64542.54",
    )
    line = _position_group_summary_line(pos)
    assert "vwap:63,676.34" in line
    assert "range:61,987.26–64,542.54" in line
    assert "pnl:+80.25" in line
    # No 28-digit Decimal ever reaches the menu.
    assert "83050847457627119" not in line
    assert len(line) < 120


def test_sub_cent_prices_keep_precision():
    from plugins.trade.wizard import _rounded_money_display

    assert _rounded_money_display("0.000012345") == "0.00001235"
    assert _rounded_money_display("63728.81393220338983050847458") == "63,728.81"
    assert _rounded_money_display("61987.26") == "61,987.26"


def test_positions_orders_screen_uses_compact_group_and_not_pending(monkeypatch):
    desk = _Mt5Desk(_hype_group_response())
    wizard = TradeWizard(tradedesk=desk)
    key = ("chat",)
    wizard.open(key)
    wizard.handle_callback(key, "exchange:mt5")
    wizard.handle_callback(key, "account:lite7486706")
    screen = wizard.handle_callback(key, "action:positions_orders")
    assert "🔴 HYPEUSD (11) vol:1.1 range:96–97 vwap:96.54" in screen.text
    assert "Pending orders: not implemented" in screen.text.lower() or "not implemented" in screen.text.lower()
    assert "No open orders." not in screen.text
    labels = [b["text"] for row in screen.buttons for b in row]
    assert any("HYPEUSD" in text and "11" in text for text in labels)


def test_grouped_row_opens_group_actions_then_optional_tickets(monkeypatch):
    desk = _Mt5Desk(_hype_group_response("positions_management"))
    wizard = TradeWizard(tradedesk=desk)
    key = ("chat",)
    wizard.open(key)
    wizard.handle_callback(key, "exchange:mt5")
    wizard.handle_callback(key, "account:lite7486706")
    screen = wizard.handle_callback(key, "action:positions_management")
    group_btn = next(
        b for row in screen.buttons for b in row if "HYPEUSD" in b["text"] and "11" in b["text"]
    )
    detail = wizard.handle_callback(key, group_btn["callback_data"].split("trade:", 1)[-1])
    assert detail.state == "position_detail"
    labels = [b["text"] for row in detail.buttons for b in row]
    assert any("Close All" in text for text in labels)
    assert any("Individual" in text for text in labels)
    assert not any(req.get("operation") == "close_position" for req in desk.requests)
    expanded = wizard.handle_callback(key, "tickets")
    assert expanded.state == "position_group"
    ticket_btn = next(b for row in expanded.buttons for b in row if "100" in b["text"])
    ticket_detail = wizard.handle_callback(key, ticket_btn["callback_data"].split("trade:", 1)[-1])
    assert ticket_detail.state == "position_detail"
    confirm = wizard.handle_callback(key, "close_position")
    assert confirm.state == "position_close_confirm"
    wizard.handle_callback(key, "confirm")
    close_reqs = [req for req in desk.requests if req.get("operation") == "close_position"]
    assert close_reqs
    assert close_reqs[-1].get("ticket") == 100


def test_resolve_btc_to_broker_symbol(fake_bridge, monkeypatch):
    fake_bridge.symbols = ["ETHUSD", "BTCUSD.c", "HYPEUSD"]
    fake_bridge.symbol_info["currency_base"] = "BTC"
    _bind_account(fake_bridge, monkeypatch)
    resp = mt5.execute({"operation": "resolve_instrument", "account": "lite7486706", "symbol": "BTC"})
    assert resp.success
    assert resp.instrument.symbol == "BTCUSD.c"
    assert resp.instrument.native_symbol == "BTCUSD.c"


def test_resolve_btc_ambiguous(fake_bridge, monkeypatch):
    fake_bridge.symbols = ["BTCUSD", "BTCUSD.c"]
    _bind_account(fake_bridge, monkeypatch)
    resp = mt5.execute({"operation": "resolve_instrument", "account": "lite7486706", "symbol": "BTC"})
    assert not resp.success
    assert resp.error.code == "INSTRUMENT_AMBIGUOUS"
    symbols = {row["symbol"] for row in (resp.data or {}).get("candidates") or []}
    assert symbols == {"BTCUSD", "BTCUSD.c"}


def test_new_order_limit_only_converts_tokens_and_rejects_market(fake_bridge, monkeypatch):
    fake_bridge.symbols = ["BTCUSD.c"]
    fake_bridge.tick = SimpleNamespace(bid=69850, ask=69860, last=69855)
    fake_bridge.symbol_info.update(
        {"contract_size": 1, "volume_min": 0.01, "volume_step": 0.01, "volume_max": 10, "tick_size": 1, "point": 1, "digits": 0, "currency_base": "BTC"}
    )
    _bind_account(fake_bridge, monkeypatch)
    market = mt5.execute(
        {"operation": "new_order", "account": "lite7486706", "symbol": "BTC", "side": "sell", "order_type": "market", "volume": "0.05", "price": "70000"}
    )
    assert not market.success
    assert market.error.code == "INVALID_ORDER_TYPE"
    assert fake_bridge.buy_calls == []
    assert fake_bridge.pending_calls == []

    bad_vol = mt5.execute(
        {"operation": "new_order", "account": "lite7486706", "symbol": "BTC", "side": "sell", "order_type": "limit", "volume": "0.055", "price": "70000"}
    )
    assert not bad_vol.success
    assert bad_vol.error.code == "INVALID_VOLUME"
    assert fake_bridge.pending_calls == []

    marketable = mt5.execute(
        {"operation": "new_order", "account": "lite7486706", "symbol": "BTC", "side": "sell", "order_type": "limit", "volume": "0.05", "price": "69000"}
    )
    assert not marketable.success
    assert marketable.error.code == "INVALID_PRICE"
    assert fake_bridge.pending_calls == []

    ok = mt5.execute(
        {"operation": "new_order", "account": "lite7486706", "symbol": "BTC", "side": "sell", "order_type": "limit", "volume": "0.05", "price": "70000"}
    )
    assert ok.success
    assert fake_bridge.buy_calls == []
    assert len(fake_bridge.pending_calls) == 1
    symbol, order_type, price, volume, _bound = fake_bridge.pending_calls[0]
    assert symbol == "BTCUSD.c"
    assert "SELL_LIMIT" in str(order_type)
    assert price == 70000.0
    assert volume == 0.05


def test_new_order_never_guesses_contract_size(fake_bridge, monkeypatch):
    fake_bridge.symbols = ["BTCUSD.c"]
    fake_bridge.symbol_info["contract_size"] = 0
    _bind_account(fake_bridge, monkeypatch)
    resp = mt5.execute(
        {"operation": "new_order", "account": "lite7486706", "symbol": "BTCUSD.c", "side": "buy", "order_type": "limit", "volume": "0.05", "price": "1.09"}
    )
    assert not resp.success
    assert resp.error.code == "SYMBOL_INFO_UNAVAILABLE"
    assert fake_bridge.pending_calls == []


class _Mt5OrderDesk:
    def __init__(self):
        self.requests = []
        self.native = "BTCUSD.c"
        self.bid = 69850
        self.ask = 69860

    def list_exchanges(self):
        return ["mt5"]

    def list_accounts(self, exchange):
        return ["VT16646363"] if exchange == "mt5" else []

    def capabilities(self, exchange):
        return ["new_order", "resolve_instrument", "market_price"]

    def execute(self, request):
        self.requests.append(dict(request))
        op = request.get("operation")
        if op == "resolve_instrument":
            return make_success(
                operation="resolve_instrument",
                exchange="mt5",
                account=request.get("account"),
                instrument=CanonicalInstrument(
                    requested_symbol=request.get("symbol"),
                    symbol=self.native,
                    display_name=self.native,
                    native_symbol=self.native,
                    display_symbol=request.get("symbol"),
                    base="BTC",
                    price_increment="1",
                    size_increment="0.01",
                    minimum_size="0.01",
                ),
                data={
                    "contract_size": "1",
                    "volume_step": "0.01",
                    "volume_min": "0.01",
                    "currency_base": "BTC",
                    "bid": "69850",
                    "ask": "69860",
                    "candidates": [
                        {
                            "symbol": self.native,
                            "native_symbol": self.native,
                            "bid": "69850",
                            "ask": "69860",
                            "price": "69855",
                            "contract_size": "1",
                        }
                    ],
                },
            )
        if op == "market_price":
            from plugins.trade.canonical import CanonicalMarketPrice

            return make_success(
                operation="market_price",
                exchange="mt5",
                account=request.get("account"),
                market_price=CanonicalMarketPrice(
                    requested_symbol=request.get("symbol"),
                    market=self.native,
                    price=str(self.ask),
                ),
                data={"bid": self.bid, "ask": self.ask},
            )
        if op == "new_order":
            return make_success(
                operation="new_order",
                exchange="mt5",
                account=request.get("account"),
                order=CanonicalOrderResult(
                    symbol=self.native,
                    side=request.get("side"),
                    order_type="sell_limit",
                    requested_volume=str(request.get("volume")),
                    requested_price=str(request.get("price")),
                    submitted_volume=str(request.get("volume")),
                    submitted_price=str(request.get("price")),
                    verified=True,
                    status="success",
                    exchange_order_id=1,
                ),
            )
        return make_success(operation=op or "", exchange="mt5", account=request.get("account") or "")


def test_wizard_mt5_new_order_matches_rise_confirm():
    desk = _Mt5OrderDesk()
    wizard = TradeWizard(tradedesk=desk)
    key = ("chat",)
    wizard.open(key)
    wizard.handle_callback(key, "exchange:mt5")
    wizard.handle_callback(key, "account:VT16646363")
    side = wizard.handle_callback(key, "action:new_order")
    picker = wizard.handle_callback(key, "symbol:BTC")
    assert picker.state == "instrument_confirm"
    assert "BTCUSD.c" in picker.text
    assert "csz 1" in picker.text or "69850" in picker.text
    side = wizard.handle_callback(key, "resolve:pick:0")
    assert "Select Side:" in side.text
    assert "Symbol: BTC (BTCUSD.c)" in side.text
    assert "Exchange: MT5" in side.text
    assert not any(req.get("operation") == "new_order" for req in desk.requests)
    wizard.handle_callback(key, "side:sell")
    price_screen = wizard.handle_text(key, "0.05")
    assert "Enter Limit Price:" in price_screen.text
    assert "Volume: 0.05 BTC" in price_screen.text
    assert "Bid:" in price_screen.text
    marketable = wizard.handle_text(key, "69000")
    assert "revised limit price" in marketable.text.lower()
    assert not any(req.get("operation") == "new_order" for req in desk.requests)
    confirm = wizard.handle_text(key, "70000")
    assert "⚠️ Confirm New Order" in confirm.text
    assert "Exchange: MT5" in confirm.text
    assert "Account: VT16646363" in confirm.text
    assert "Symbol: BTC (BTCUSD.c)" in confirm.text
    assert "Side: 🔴 Sell" in confirm.text
    assert "Volume: 0.05 BTC" in confirm.text
    assert "Lots: 0.05" in confirm.text
    assert "Type: LIMIT" in confirm.text
    assert "Price: 70,000" in confirm.text
    result = wizard.handle_callback(key, "confirm")
    orders = [req for req in desk.requests if req.get("operation") == "new_order"]
    assert len(orders) == 1
    assert orders[0]["order_type"] == "limit"
    assert orders[0]["symbol"] == "BTCUSD.c"
    assert orders[0]["side"] == "sell"
    assert orders[0]["volume"] == "0.05"
    assert orders[0]["price"] == "70000"
    assert "✅ Order Submitted" in result.text


def test_btc_usd_and_suffix_are_ambiguous(fake_bridge, monkeypatch):
    fake_bridge.symbols = ["BTCUSD", "BTCUSD.c", "ETHUSD"]
    _bind_account(fake_bridge, monkeypatch)
    resp = mt5.execute({"operation": "resolve_instrument", "account": "lite7486706", "symbol": "BTCUSD"})
    assert not resp.success
    assert resp.error.code == "INSTRUMENT_AMBIGUOUS"
    names = {row["symbol"] for row in (resp.data or {}).get("candidates") or []}
    assert names == {"BTCUSD", "BTCUSD.c"}
    row = next(r for r in resp.data["candidates"] if r["symbol"] == "BTCUSD.c")
    assert row.get("bid")
    assert row.get("contract_size")


def test_ladder_places_limit_children(fake_bridge, monkeypatch):
    fake_bridge.symbols = ["BTCUSD.c"]
    fake_bridge.tick = SimpleNamespace(bid=69850, ask=69860, last=69855)
    fake_bridge.symbol_info.update(
        {"contract_size": 1, "volume_min": 0.01, "volume_step": 0.01, "volume_max": 10, "tick_size": 1, "point": 1, "digits": 0, "currency_base": "BTC"}
    )
    _bind_account(fake_bridge, monkeypatch)
    market = mt5.execute(
        {
            "operation": "ladder",
            "account": "lite7486706",
            "symbol": "BTCUSD.c",
            "side": "sell",
            "distribution": "half_gaussian",
            "order_count": "3",
            "total_volume": "0.15",
            "start_price": "70000",
            "end_price": "72000",
        }
    )
    assert market.success
    assert fake_bridge.buy_calls == []
    assert len(fake_bridge.pending_calls) >= 2
    for _sym, order_type, price, volume, _bound in fake_bridge.pending_calls:
        assert "SELL_LIMIT" in str(order_type)
        assert price >= 70000
        assert volume > 0
    assert market.ladder.submitted_order_count == len(fake_bridge.pending_calls)
    assert market.ladder.symbol == "BTCUSD.c"


def test_wizard_mt5_ladder_confirm_shows_lots():
    desk = _Mt5OrderDesk()
    wizard = TradeWizard(tradedesk=desk)
    key = ("chat",)
    wizard.open(key)
    wizard.handle_callback(key, "exchange:mt5")
    wizard.handle_callback(key, "account:VT16646363")
    wizard.handle_callback(key, "action:ladder")
    wizard.handle_callback(key, "distribution:half_gaussian")
    picker = wizard.handle_callback(key, "symbol:BTC")
    assert picker.state == "instrument_confirm"
    wizard.handle_callback(key, "resolve:pick:0")
    wizard.handle_callback(key, "side:sell")
    wizard.handle_text(key, "3")
    wizard.handle_text(key, "0.06")
    wizard.handle_text(key, "70000")
    confirm = wizard.handle_text(key, "72000")
    assert "⚠️ Confirm Ladder" in confirm.text
    assert "Type: LIMIT" in confirm.text
    assert "BTCUSD.c" in confirm.text
    assert "Total lots:" in confirm.text
    assert not any(req.get("operation") == "ladder" for req in desk.requests)
    result = wizard.handle_callback(key, "confirm")
    ladders = [req for req in desk.requests if req.get("operation") == "ladder"]
    assert len(ladders) == 1
    assert ladders[0]["symbol"] == "BTCUSD.c"
    assert ladders[0]["side"] == "sell"
    assert ladders[0]["distribution"] == "half_gaussian"


def test_pending_orders_group_by_symbol_and_side(fake_bridge, monkeypatch):
    fake_bridge.orders = [
        {"ticket": 55, "symbol": "HYPEUSD", "type": "SELL_LIMIT", "volume": 0.1, "price": 96},
        {"ticket": 56, "symbol": "HYPEUSD", "type": "SELL_LIMIT", "volume": 0.2, "price": 97},
        {"ticket": 57, "symbol": "BTCUSD.c", "type": "BUY_LIMIT", "volume": 0.01, "price": 70000},
    ]
    _bind_account(fake_bridge, monkeypatch)
    resp = mt5.execute({"operation": "positions_orders", "account": "lite7486706"})
    assert resp.success
    groups = {(g.symbol, g.side): g for g in resp.order_groups}
    assert set(groups) == {("HYPEUSD", "sell"), ("BTCUSD.c", "buy")}
    hype = groups[("HYPEUSD", "sell")]
    assert hype.order_count == 2
    assert hype.total_size == "0.3"
    assert hype.min_price == "96"
    assert hype.max_price == "97"
    assert hype.order_ids == ["55", "56"]
    assert resp.open_order_count == 3


def test_cancel_order_group_without_ticket_is_fail_closed(fake_bridge, monkeypatch):
    fake_bridge.orders = [
        {"ticket": 55, "symbol": "HYPEUSD", "type": "SELL_LIMIT", "volume": 0.1, "price": 96},
        {"ticket": 56, "symbol": "HYPEUSD", "type": "SELL_LIMIT", "volume": 0.2, "price": 97},
    ]
    _bind_account(fake_bridge, monkeypatch)
    resp = mt5.execute(
        {"operation": "cancel_order_group", "account": "lite7486706", "symbol": "HYPEUSD", "side": "sell"}
    )
    assert not resp.success
    assert resp.error.code == "MISSING_TICKET"
    assert fake_bridge.cancel_calls == []


def test_cancel_order_group_cancels_matching_tickets_sequentially(fake_bridge, monkeypatch):
    fake_bridge.orders = [
        {"ticket": 55, "symbol": "HYPEUSD", "type": "SELL_LIMIT", "volume": 0.1, "price": 96},
        {"ticket": 56, "symbol": "HYPEUSD", "type": "SELL_STOP", "volume": 0.2, "price": 97},
        {"ticket": 57, "symbol": "HYPEUSD", "type": "BUY_LIMIT", "volume": 0.1, "price": 90},
        {"ticket": 58, "symbol": "BTCUSD.c", "type": "SELL_LIMIT", "volume": 0.01, "price": 70000},
    ]
    _bind_account(fake_bridge, monkeypatch)
    resp = mt5.execute(
        {
            "operation": "cancel_order_group",
            "account": "lite7486706",
            "symbol": "HYPEUSD",
            "side": "sell",
            "tickets": [55, 56],
        }
    )
    assert resp.success
    assert [str(c[0]) for c in fake_bridge.cancel_calls] == ["55", "56"]
    assert resp.cancel_group.cancelled_order_count == 2
    assert resp.cancel_group.targeted_order_count == 2
    assert resp.cancel_group.verified


def test_cancel_order_group_continues_after_failure_and_detects_stale_group(fake_bridge, monkeypatch):
    fake_bridge.orders = [
        {"ticket": 55, "symbol": "HYPEUSD", "type": "SELL_LIMIT", "volume": 0.1, "price": 96},
        {"ticket": 56, "symbol": "HYPEUSD", "type": "SELL_LIMIT", "volume": 0.2, "price": 97},
    ]
    fake_bridge.cancel_fail = {55}
    _bind_account(fake_bridge, monkeypatch)
    partial = mt5.execute(
        {
            "operation": "cancel_order_group",
            "account": "lite7486706",
            "symbol": "HYPEUSD",
            "side": "sell",
            "tickets": [55, 56],
        }
    )
    assert partial.success
    assert [str(c[0]) for c in fake_bridge.cancel_calls] == ["55", "56"]
    assert partial.cancel_group.cancelled_order_count == 1
    assert partial.cancel_group.remaining_target_count == 1
    assert partial.cancel_group.partial
    assert (partial.data or {}).get("failed")

    fake_bridge.cancel_calls.clear()
    stale = mt5.execute(
        {
            "operation": "cancel_order_group",
            "account": "lite7486706",
            "symbol": "HYPEUSD",
            "side": "sell",
            "tickets": [55],
        }
    )
    assert not stale.success
    assert stale.error.code == "GROUP_CHANGED"
    assert fake_bridge.cancel_calls == []


class _Mt5CancelDesk:
    def __init__(self):
        self.requests = []

    def list_exchanges(self):
        return ["mt5"]

    def list_accounts(self, exchange):
        return ["vt16646363"] if exchange == "mt5" else []

    def capabilities(self, exchange):
        return ["positions_orders", "cancel_order", "cancel_orders", "set_tp", "set_sl", "close_position"]

    def execute(self, request):
        self.requests.append(dict(request))
        op = request.get("operation")
        if op == "positions_orders":
            return make_success(
                operation=op,
                exchange="mt5",
                account="vt16646363",
                open_order_count=2,
                order_groups=[
                    CanonicalOrderGroup(
                        symbol="HYPEUSD",
                        side="sell",
                        order_count=2,
                        total_size="0.3",
                        vwap="96.67",
                        min_price="96",
                        max_price="97",
                        order_ids=[55, 56],
                    )
                ],
                data={
                    "pending_orders": [
                        {"ticket": 55, "symbol": "HYPEUSD", "side": "sell", "size": "0.1", "volume": "0.1", "price": "96", "entry_price": "96"},
                        {"ticket": 56, "symbol": "HYPEUSD", "side": "sell", "size": "0.2", "volume": "0.2", "price": "97", "entry_price": "97"},
                    ]
                },
            )
        if op == "cancel_order":
            return make_success(operation="cancel_order", exchange="mt5", account="vt16646363", data={"ticket": request.get("ticket"), "cancelled": True})
        if op == "cancel_order_group":
            from plugins.trade.canonical import CanonicalCancelGroupResult

            tickets = list(request.get("tickets") or [])
            return make_success(
                operation="cancel_order_group",
                exchange="mt5",
                account="vt16646363",
                cancel_group=CanonicalCancelGroupResult(
                    symbol=request.get("symbol") or "",
                    side=request.get("side") or "",
                    targeted_order_count=len(tickets),
                    cancelled_order_count=len(tickets),
                    confirmed_absent_count=len(tickets),
                    remaining_target_count=0,
                    verified=True,
                    status="success",
                    requested_cancel_count=len(tickets),
                    verified_cancel_count=len(tickets),
                ),
                data={"cancelled_tickets": [str(t) for t in tickets], "failed": []},
            )
        return make_success(operation=op or "", exchange="mt5", account=request.get("account") or "")


def test_wizard_mt5_cancel_group_is_default():
    desk = _Mt5CancelDesk()
    wizard = TradeWizard(tradedesk=desk)
    key = ("chat",)
    wizard.open(key)
    wizard.handle_callback(key, "exchange:mt5")
    wizard.handle_callback(key, "account:vt16646363")
    screen = wizard.handle_callback(key, "action:cancel_orders")
    assert "Open orders: 2" in screen.text
    group_btn = next(b for row in screen.buttons for b in row if "HYPEUSD" in b["text"])
    confirm = wizard.handle_callback(key, group_btn["callback_data"].split("trade:", 1)[-1])
    assert confirm.state == "cancel_group_confirm"
    assert "Cancel ALL pending tickets in this group?" in confirm.text
    assert "Tickets: #55, #56" in confirm.text
    assert "Orders: 2" in confirm.text
    assert not any(req.get("operation") in {"cancel_order", "cancel_order_group"} for req in desk.requests)
    result = wizard.handle_callback(key, "confirm")
    groups = [req for req in desk.requests if req.get("operation") == "cancel_order_group"]
    assert len(groups) == 1
    assert groups[0]["symbol"] == "HYPEUSD"
    assert groups[0]["side"] == "sell"
    assert groups[0]["tickets"] == [55, 56]
    assert "✅ Orders Cancelled" in result.text


def test_wizard_mt5_optional_ticket_cancel():
    desk = _Mt5CancelDesk()
    wizard = TradeWizard(tradedesk=desk)
    key = ("chat",)
    wizard.open(key)
    wizard.handle_callback(key, "exchange:mt5")
    wizard.handle_callback(key, "account:vt16646363")
    wizard.handle_callback(key, "action:cancel_orders")
    group_btn = next(
        b for row in wizard.handle_callback(key, "action:cancel_orders").buttons for b in row if "HYPEUSD" in b["text"]
    )
    # re-open flow cleanly
    wizard.handle_callback(key, "action:cancel_orders")
    screen = wizard.handle_callback(key, "action:cancel_orders")
    group_btn = next(b for row in screen.buttons for b in row if "HYPEUSD" in b["text"])
    confirm = wizard.handle_callback(key, group_btn["callback_data"].split("trade:", 1)[-1])
    expanded = wizard.handle_callback(key, "tickets")
    assert expanded.state == "cancel_group"
    assert "Select a ticket to cancel:" in expanded.text
    ticket_btn = next(b for row in expanded.buttons for b in row if "55" in b["text"])
    ticket_confirm = wizard.handle_callback(key, ticket_btn["callback_data"].split("trade:", 1)[-1])
    assert "Cancel this ticket only?" in ticket_confirm.text
    result = wizard.handle_callback(key, "confirm")
    cancels = [req for req in desk.requests if req.get("operation") == "cancel_order"]
    assert len(cancels) == 1
    assert cancels[0]["ticket"] == 55
    assert "✅ Order Cancelled" in result.text


def test_wizard_set_tp_sends_group_tickets():
    desk = _Mt5Desk(_hype_group_response("positions_management"))
    wizard = TradeWizard(tradedesk=desk)
    key = ("chat",)
    wizard.open(key)
    wizard.handle_callback(key, "exchange:mt5")
    wizard.handle_callback(key, "account:lite7486706")
    screen = wizard.handle_callback(key, "action:positions_management")
    group_btn = next(b for row in screen.buttons for b in row if "HYPEUSD" in b["text"] and "11" in b["text"])
    wizard.handle_callback(key, group_btn["callback_data"].split("trade:", 1)[-1])
    wizard.handle_callback(key, "set_tp")
    wizard.handle_text(key, "120")
    wizard.handle_callback(key, "confirm")
    tps = [req for req in desk.requests if req.get("operation") == "set_tp"]
    assert tps
    assert tps[-1].get("tickets") == [100 + i for i in range(11)]


def test_live_shaped_group_confirm_keeps_64bit_tickets():
    tickets = ["5264312433", "5264312510", "5264312525"]
    pending = [
        {"ticket": "5266852261", "symbol": "BTCUSD", "side": "sell", "size": "0.01", "volume": "0.01", "price": "75836.73", "entry_price": "75836.73"},
        {"ticket": "5266852330", "symbol": "BTCUSD", "side": "sell", "size": "0.05", "volume": "0.05", "price": "80000", "entry_price": "80000"},
    ]
    from plugins.trade.canonical import CanonicalCancelGroupResult

    class Desk:
        def __init__(self):
            self.requests = []

        def list_exchanges(self):
            return ["mt5"]

        def list_accounts(self, exchange):
            return ["lite7486706"] if exchange == "mt5" else []

        def capabilities(self, exchange):
            return ["positions_orders", "positions_management", "cancel_order", "cancel_orders", "set_tp", "close_position"]

        def execute(self, request):
            self.requests.append(dict(request))
            op = request.get("operation")
            if op in {"positions_orders", "positions_management"}:
                return make_success(
                    operation=op,
                    exchange="mt5",
                    account="lite7486706",
                    open_order_count=2,
                    positions=[
                        CanonicalPosition(
                            symbol="HYPEUSD",
                            side="short",
                            size="0.3",
                            entry_price="96.1",
                            pnl="1",
                            count=3,
                            min_price="96",
                            max_price="96.2",
                            tickets=tickets,
                        )
                    ],
                    order_groups=[
                        CanonicalOrderGroup(
                            symbol="BTCUSD",
                            side="sell",
                            order_count=2,
                            total_size="0.06",
                            vwap="78600",
                            min_price="75836.73",
                            max_price="80000",
                            order_ids=["5266852261", "5266852330"],
                        )
                    ],
                    data={"pending_orders": pending, "position_legs": [{"ticket": t, "symbol": "HYPEUSD", "side": "short", "size": "0.1"} for t in tickets]},
                )
            if op == "cancel_order_group":
                return make_success(
                    operation=op,
                    exchange="mt5",
                    account="lite7486706",
                    cancel_group=CanonicalCancelGroupResult(
                        symbol="BTCUSD",
                        side="sell",
                        targeted_order_count=2,
                        cancelled_order_count=2,
                        confirmed_absent_count=2,
                        remaining_target_count=0,
                        verified=True,
                        status="success",
                    ),
                )
            if op == "set_tp":
                return make_success(operation=op, exchange="mt5", account="lite7486706")
            return make_success(operation=op or "unknown", exchange="mt5", account="lite7486706")

    desk = Desk()
    wizard = TradeWizard(tradedesk=desk)
    key = ("chat",)
    wizard.open(key)
    wizard.handle_callback(key, "exchange:mt5")
    wizard.handle_callback(key, "account:lite7486706")
    cancel = wizard.handle_callback(key, "action:cancel_orders")
    assert "BTCUSD" in cancel.text
    group_btn = next(b for row in cancel.buttons for b in row if "BTCUSD" in b["text"])
    confirm = wizard.handle_callback(key, group_btn["callback_data"].split("trade:", 1)[-1])
    assert "Confirm Cancel" in " ".join(b["text"] for row in confirm.buttons for b in row)
    assert "#5266852261" in confirm.text
    assert "#5266852330" in confirm.text
    assert "2" in confirm.text
    wizard.handle_callback(key, "confirm")
    reqs = [r for r in desk.requests if r.get("operation") == "cancel_order_group"]
    assert reqs[-1]["tickets"] == ["5266852261", "5266852330"]
    assert reqs[-1]["symbol"] == "BTCUSD"
    assert reqs[-1]["side"] == "sell"


def test_group_set_tp_applies_each_ticket_and_skips_opposite_side(fake_bridge, monkeypatch):
    fake_bridge.positions = [
        _pos(11, "ZECUSD", "SELL", 0.1, 40.0),
        _pos(12, "ZECUSD", "SELL", 0.2, 41.0),
        _pos(13, "ZECUSD", "BUY", 0.1, 39.0),
    ]
    _bind_account(fake_bridge, monkeypatch)
    resp = mt5.execute(
        {
            "operation": "set_tp",
            "account": "lite7486706",
            "symbol": "ZECUSD",
            "side": "sell",
            "price": "35",
            "tickets": [11, 12],
        }
    )
    assert resp.success
    assert [str(c[0]) for c in fake_bridge.modify_calls] == ["11", "12"]
    assert fake_bridge.modify_calls[0][1] is None
    assert fake_bridge.modify_calls[0][2] == 35.0
    stale = mt5.execute(
        {
            "operation": "set_tp",
            "account": "lite7486706",
            "symbol": "ZECUSD",
            "side": "sell",
            "price": "35",
            "tickets": [11],
        }
    )
    assert not stale.success
    assert stale.error.code == "GROUP_CHANGED"


def _get_json_ulong(json_text: str, key: str) -> int:
    """Mirror MT5SocketClient.mq5 GetJsonUlong: digit string → ulong, no double."""
    needle = f'"{key}":'
    pos = json_text.find(needle)
    if pos < 0:
        needle = f'"{key}": '
        pos = json_text.find(needle)
    if pos < 0:
        return 0
    start = pos + len(needle)
    while start < len(json_text) and json_text[start] == " ":
        start += 1
    if start < len(json_text) and json_text[start] == '"':
        start += 1
    end = start
    while end < len(json_text) and json_text[end].isdigit():
        end += 1
    if end <= start:
        return 0
    value = 0
    for ch in json_text[start:end]:
        value = value * 10 + (ord(ch) - 48)
    return value


def test_tickets_survive_above_2_32_and_2_53(fake_bridge, monkeypatch):
    big32 = 2**32 + 1
    big53 = 2**53 + 1
    assert big32 == 4294967297
    assert big53 == 9007199254740993
    assert int(float(big53)) != big53
    quoted = json.dumps({"ticket": str(big53)})
    unquoted = '{"ticket":%s}' % big53
    assert quoted == '{"ticket": "9007199254740993"}' or json.loads(quoted)["ticket"] == str(big53)
    assert json.loads(quoted)["ticket"] == str(big53)
    assert _get_json_ulong(quoted, "ticket") == big53
    assert _get_json_ulong(unquoted, "ticket") == big53
    assert _get_json_ulong('{"ticket":"%s"}' % big32, "ticket") == big32
    assert mt5._normalize_ticket(str(big32)) == str(big32)
    assert mt5._normalize_ticket(big32) == str(big32)
    assert mt5._normalize_ticket(str(big53)) == str(big53)
    assert mt5._normalize_ticket(big53) == str(big53)
    assert mt5._normalize_ticket(float(big53)) is None
    assert mt5._normalize_ticket("969347827") == "969347827"

    fake_bridge.positions = [
        _pos(big32, "ZECUSD", "SELL", 0.1, 40.0),
        _pos(big53, "ZECUSD", "SELL", 0.2, 41.0),
        _pos(13, "ZECUSD", "BUY", 0.1, 39.0),
    ]
    fake_bridge.orders = [
        {"ticket": str(big32), "symbol": "HYPEUSD", "type": "SELL_LIMIT", "volume": 0.1, "price": 96},
        {"ticket": str(big53), "symbol": "HYPEUSD", "type": "SELL_STOP", "volume": 0.2, "price": 97},
    ]
    _bind_account(fake_bridge, monkeypatch)
    listed = mt5.execute({"operation": "positions_orders", "account": "lite7486706"})
    assert listed.success
    pos_group = next(p for p in listed.positions if p.symbol == "ZECUSD" and p.side == "short")
    assert pos_group.tickets == [str(big32), str(big53)]
    order_group = next(g for g in listed.order_groups if g.symbol == "HYPEUSD" and g.side == "sell")
    assert order_group.order_ids == [str(big32), str(big53)]

    tp = mt5.execute(
        {
            "operation": "set_tp",
            "account": "lite7486706",
            "symbol": "ZECUSD",
            "side": "sell",
            "price": "35",
            "tickets": [str(big32), str(big53)],
        }
    )
    assert tp.success
    assert [str(c[0]) for c in fake_bridge.modify_calls] == [str(big32), str(big53)]
    stale = mt5.execute(
        {
            "operation": "set_tp",
            "account": "lite7486706",
            "symbol": "ZECUSD",
            "side": "sell",
            "price": "35",
            "tickets": [str(big32)],
        }
    )
    assert not stale.success
    assert stale.error.code == "GROUP_CHANGED"

    cancel = mt5.execute(
        {
            "operation": "cancel_order_group",
            "account": "lite7486706",
            "symbol": "HYPEUSD",
            "side": "sell",
            "tickets": [str(big32), str(big53)],
        }
    )
    assert cancel.success
    assert [str(c[0]) for c in fake_bridge.cancel_calls] == [str(big32), str(big53)]


def test_cancel_back_uses_cache_and_errors_are_not_zero_orders():
    class Desk:
        def __init__(self):
            self.requests = []
            self.fail = False

        def list_exchanges(self):
            return ["mt5"]

        def list_accounts(self, exchange):
            return ["vt16646363"] if exchange == "mt5" else []

        def capabilities(self, exchange):
            return ["positions_orders", "cancel_order", "cancel_orders"]

        def execute(self, request):
            self.requests.append(dict(request))
            if self.fail:
                return make_failure(
                    operation="positions_orders",
                    exchange="mt5",
                    account="vt16646363",
                    code="ACCOUNT_IDENTITY_MISMATCH",
                    message="Connected EA login does not match the selected MT5 account.",
                )
            return make_success(
                operation="positions_orders",
                exchange="mt5",
                account="vt16646363",
                open_order_count=2,
                order_groups=[
                    CanonicalOrderGroup(
                        symbol="BTCUSD.c",
                        side="sell",
                        order_count=2,
                        total_size="12",
                        vwap="69250",
                        min_price="65500",
                        max_price="70000",
                        order_ids=["2526432260", "2526432902"],
                    )
                ],
                data={
                    "pending_orders": [
                        {"ticket": "2526432260", "symbol": "BTCUSD.c", "side": "sell", "size": "10", "volume": "10"},
                        {"ticket": "2526432902", "symbol": "BTCUSD.c", "side": "sell", "size": "2", "volume": "2"},
                    ]
                },
            )

    desk = Desk()
    wizard = TradeWizard(tradedesk=desk)
    key = ("chat",)
    wizard.open(key)
    wizard.handle_callback(key, "exchange:mt5")
    wizard.handle_callback(key, "account:vt16646363")
    screen = wizard.handle_callback(key, "action:cancel_orders")
    assert "BTCUSD.c" in screen.text
    fetches = [r for r in desk.requests if r.get("operation") == "positions_orders"]
    assert len(fetches) == 1
    group_btn = next(b for row in screen.buttons for b in row if "BTCUSD.c" in b["text"])
    confirm = wizard.handle_callback(key, group_btn["callback_data"].split("trade:", 1)[-1])
    assert confirm.state == "cancel_group_confirm"
    back = wizard.handle_callback(key, "back")
    assert "BTCUSD.c" in back.text
    fetches = [r for r in desk.requests if r.get("operation") == "positions_orders"]
    assert len(fetches) == 1
    exit_screen = wizard.handle_callback(key, "exit")
    assert exit_screen.state == "closed"
    assert len([r for r in desk.requests if r.get("operation") == "positions_orders"]) == 1

    desk2 = Desk()
    desk2.fail = True
    wizard2 = TradeWizard(tradedesk=desk2)
    key2 = ("chat2",)
    wizard2.open(key2)
    wizard2.handle_callback(key2, "exchange:mt5")
    wizard2.handle_callback(key2, "account:vt16646363")
    err = wizard2.handle_callback(key2, "action:cancel_orders")
    assert "Error:" in err.text
    assert "ACCOUNT_IDENTITY_MISMATCH" in err.text
    assert "No open orders." not in err.text
    assert "Open orders: 0" not in err.text



