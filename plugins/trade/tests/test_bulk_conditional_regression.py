"""Bulk conditional-order regression suite (permanent).

This file exists to prevent the class of defect that the 36 mocked checks in
``test_bulk_agent.py`` could not see.

WHY A NEW SUITE
---------------
The previous tests built their fixtures from the SAME assumption the
production code used, so they were circular: they confirmed the code did
what the code was written to do, never what the exchange accepts. The
exchange's own rules are stated below and are applied by an INDEPENDENT
model of the node, so a test can fail when the code disagrees with the
exchange rather than merely when the code changes.

THE EXCHANGE'S RULES (verified live on Bulk testnet, and in Bulk's docs)
------------------------------------------------------------------------
1. ``d`` on a conditional action is the TRIGGER DIRECTION:
   ``true`` fires at/above the threshold, ``false`` fires at/below it.
   The official SDK calls the field ``is_buy``, which is misleading for
   ``st``/``tp``: the exchange reads it as the trigger direction.

       take profit : fires ABOVE for a long, BELOW for a short
       stop loss   : fires BELOW for a long, ABOVE for a short

2. A conditional whose trigger is ALREADY satisfied fires the moment it is
   submitted and is then cancelled as IOC. So a protective conditional must
   sit on the side of the mark that has not been reached yet.

3. The executed order is always a reduce-only close against the position.
   The wire ``sz`` field is an UNSIGNED fixed-point number, so the closing
   side is not carried by a sign on the size: it is the exchange's reduce-
   only routing. The closing side is therefore a separate calculation from
   the trigger direction.

4. The resting size sign on the trigger book is the TRIGGER LEG, not the
   position side, and must never be used to tell a take profit from a stop
   loss.

5. A conditional whose kind is unknown must not be bucketed at all:
   guessing would let the agent cancel the wrong protection.

6. Mainnet verifies the VERSIONLESS transaction framing; testnet verifies
   the VERSION-12 framing. A signature failure is a transaction-level
   rejection that creates NO order history row.

7. ``minNotional`` is enforced on ordinary orders but NOT on reduce-only
   conditional orders.

8. A rejected order IS recorded in the order history; a transaction-level
   signature failure is NOT.

COVERED HERE
------------
  * all four long/short x TP/SL combinations, checked against the
    independent model of the node (not against the code's own assumption);
  * the operator's BTC short take-profit at 50000;
  * creation, updating, independent removal using price 0, and
    restoration after a failed update;
  * reason surfacing from the exchange;
  * honest restoration reporting (never claim lost protection that never
    existed);
  * both transaction-signing framings;
  * conditional identification from live rows.
"""
from __future__ import annotations

import importlib
import json
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

bulk = importlib.import_module("plugins.trade.agents.x_bulk_agent")

ACCOUNT_PUBKEY = "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7"


def _b58_seed() -> str:
    """A real base58 Ed25519 seed so the production signing path runs."""
    import base58
    return base58.b58encode(bytes(range(32))).decode()


CREDS = {
    "account": "main",
    "account_pubkey": ACCOUNT_PUBKEY,
    "agent_private_key": _b58_seed(),
    "base_url": bulk.DEFAULT_API_BASE,
    "signature_domain": "mainnet",
}

# The real BTC-USD metadata published in exchangeInfo.
BTC_MARKET = {
    "symbol": "BTC-USD",
    "sizeIncrement": 1e-08,
    "sizeDecimals": 8,
    "tickSize": 0.01,
    "minNotional": 1.0,
}

# ----------------------------------------------------------------------
# The independent model of the Bulk node.
#
# These are the exchange's rules, written out directly from Bulk's
# documentation and from live testnet observations - NOT from the agent's
# implementation. The tests compare the agent's output against them.
# ----------------------------------------------------------------------


def exchange_trigger_direction(kind: str, position_side: str) -> bool:
    """Rule 1: ``d`` is the trigger direction."""
    if kind == "tp":
        return position_side == "long"
    if kind == "st":
        return position_side == "short"
    raise AssertionError(f"unknown conditional kind {kind!r}")


def exchange_closing_side(position_side: str) -> str:
    """Rule 3: the order side that closes the position."""
    return "sell" if position_side == "long" else "buy"


def exchange_protected_side(kind: str, is_above: bool) -> str:
    """Which position a live conditional protects, per rule 1."""
    if kind == "tp":
        return "long" if is_above else "short"
    return "short" if is_above else "long"


def exchange_accepts_conditional(
    kind: str, position_side: str, trigger_above: bool, trigger_price: float, mark: float
) -> bool:
    """Rule 2: a satisfied trigger fires immediately, so it is refused."""
    if exchange_trigger_direction(kind, position_side) != trigger_above:
        return False
    if trigger_above:
        return trigger_price > mark
    return trigger_price < mark


def _live_row(kind: str, oid: str, price: float, is_above: bool, size: float,
              symbol: str = "BTC-USD", iso: bool = False) -> dict:
    """Build a live conditional row in the shape the node reports."""
    order_type = "takeProfit" if kind == "tp" else "stop"
    return {
        "symbol": symbol,
        "orderId": oid,
        "price": price,
        "originalSize": size,
        "size": size,
        "filledSize": 0.0,
        "reduceOnly": True,
        "iso": iso,
        "orderType": order_type,
        "trigger": {"isAbove": is_above, "px": price},
        "tif": "gtc",
        "status": "resting",
    }


def _snapshot(positions, open_orders=()) -> dict:
    return {
        "user": ACCOUNT_PUBKEY,
        "positions": list(positions),
        "openOrders": list(open_orders),
    }


def _resting_response(oid: str) -> dict:
    return {"response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}


def _rejected_response(reason: str) -> dict:
    """An order-level rejection: Bulk states the reason inside the payload."""
    return {
        "response": {
            "data": {
                "statuses": [
                    {"rejectedInvalid": {"oid": "x", "reason": reason}}
                ]
            }
        }
    }


def _signature_error_response() -> dict:
    """A transaction-level refusal: no order row is ever created."""
    return {
        "status": "error",
        "response": {
            "type": "order",
            "data": {"statuses": [{"error": {"message": "bad signature"}}]},
        },
    }


class _BulkNode:
    """A small model of the Bulk node used by the tests.

    It applies the exchange rules above to decide whether a submitted
    action is accepted, and it records the rows it would show on the
    trigger book. It is deliberately independent of the agent's code.
    """

    def __init__(self, mark: float, position_side: str, position_size: float):
        self.mark = mark
        self.position_side = position_side
        self.position_size = position_size
        self.rows: list[dict] = []
        self.rejects: list[tuple[str, str]] = []   # (kind, reason)
        self.signature_error = False
        self.fail_submit = False
        self.oid_counter = 0
        # When set, every conditional submission is rejected with this
        # reason - used to exercise the restore-after-a-failed-update path
        # without tripping the agent's own pre-flight guards.
        self.reject_with: str | None = None

    def _next_oid(self) -> str:
        self.oid_counter += 1
        return f"oid-{self.oid_counter}"

    def submit(self, actions):
        """Apply the exchange rules to the submitted actions."""
        if self.signature_error:
            return _signature_error_response()
        if self.fail_submit:
            raise RuntimeError("simulated transport failure")

        new_rows: list[dict] = []
        for action in actions:
            if "cx" in action:
                oid = str(action["cx"].get("oid") or "")
                self.rows = [r for r in self.rows if r["orderId"] != oid]
                continue
            if "tp" in action or "st" in action:
                kind = "tp" if "tp" in action else "st"
                if self.reject_with is not None:
                    self.rejects.append((kind, self.reject_with))
                    return _rejected_response(self.reject_with)
                row = action[kind]
                trigger_above = bool(row.get("d"))
                price = float(row.get("tr"))
                size = float(row.get("sz"))
                if not exchange_accepts_conditional(
                    kind, self.position_side, trigger_above, price, self.mark
                ):
                    reason = (
                        "trigger direction does not protect the position"
                        if exchange_trigger_direction(kind, self.position_side) != trigger_above
                        else "trigger price is already satisfied"
                    )
                    self.rejects.append((kind, reason))
                    return _rejected_response(reason)
                # The resting size sign is the TRIGGER LEG, not the position
                # side (rule 4).
                leg_sign = 1.0 if trigger_above else -1.0
                new_rows.append(_live_row(kind, self._next_oid(), price,
                                          trigger_above, leg_sign * size))
                continue
            return _rejected_response("unsupported action in this model")

        self.rows.extend(new_rows)
        return _resting_response(new_rows[0]["orderId"] if new_rows else "none")


class _Harness:
    """Drives the real agent against a :class:`_BulkNode`."""

    def __init__(self, node: _BulkNode, before_rows=None):
        self.node = node
        self.before_rows = list(before_rows or [])

    def _book(self, _credentials):
        return list(self.node.rows)

    def _account(self, _credentials):
        return _snapshot(
            [{"symbol": "BTC-USD", "size": self.node.position_size,
              "iso": False, "fairPrice": self.node.mark}],
            list(self.node.rows),
        )

    def _submit(self, _credentials, actions):
        return self.node.submit(actions)

    def run(self, operation: str, price: str):
        creds = dict(CREDS)
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=dict(BTC_MARKET)), \
             mock.patch.object(bulk, "_live_account", side_effect=self._account), \
             mock.patch.object(bulk, "_conditional_orders", side_effect=self._book), \
             mock.patch.object(bulk, "_submit_order", side_effect=self._submit) as submit:
            resp = bulk.execute({
                "operation": operation, "exchange": "bulk", "account": "main",
                "symbol": "BTC", "price": price,
            })
        return resp, submit


# ----------------------------------------------------------------------
# 1. All four long/short x TP/SL combinations
# ----------------------------------------------------------------------

class TestAllFourProtectionCombinations(unittest.TestCase):
    """Each combination is checked against the independent model.

    The four cases are:
        long  + take profit -> d = true  (fires above), closes with a sell
        long  + stop loss   -> d = false (fires below), closes with a sell
        short + take profit -> d = false (fires below), closes with a buy
        short + stop loss   -> d = true  (fires above), closes with a buy
    """

    CASES = (
        # operation, position_side, position_size, kind, expected_d, mark, price
        ("set_tp", "long", 0.125, "tp", True, 100000.0, "120000"),
        ("set_sl", "long", 0.125, "st", False, 100000.0, "90000"),
        ("set_tp", "short", -0.125, "tp", False, 100000.0, "80000"),
        ("set_sl", "short", -0.125, "st", True, 100000.0, "120000"),
    )

    def test_each_combination_uses_the_exchange_trigger_direction(self) -> None:
        for operation, side, size, kind, expected_d, mark, price in self.CASES:
            with self.subTest(operation=operation, side=side):
                node = _BulkNode(mark=mark, position_side=side, position_size=size)
                harness = _Harness(node)
                resp, submit = harness.run(operation, price)
                self.assertTrue(
                    resp.success,
                    f"{operation} on a {side} failed: {getattr(resp, 'message', resp)}",
                )
                action = submit.call_args.args[1][0][kind]
                self.assertEqual(
                    action["d"], expected_d,
                    f"{kind} on a {side} must send d={expected_d} "
                    f"(exchange rule: trigger direction)",
                )
                self.assertEqual(
                    action["d"], exchange_trigger_direction(kind, side),
                    "the agent disagrees with the exchange's own rule",
                )
                self.assertEqual(action["sz"], abs(size))
                self.assertIsNone(action["lim"])
                self.assertFalse(action["i"])

    def test_each_combination_lands_on_the_trigger_book(self) -> None:
        for operation, side, size, kind, expected_d, mark, price in self.CASES:
            with self.subTest(operation=operation, side=side):
                node = _BulkNode(mark=mark, position_side=side, position_size=size)
                harness = _Harness(node)
                resp, _submit = harness.run(operation, price)
                self.assertTrue(resp.success)
                self.assertEqual(len(node.rows), 1)
                row = node.rows[0]
                self.assertEqual(row["trigger"]["isAbove"], expected_d)
                self.assertTrue(row["reduceOnly"])
                self.assertEqual(row["orderType"], "takeProfit" if kind == "tp" else "stop")

    def test_closing_side_is_kept_separate_from_the_trigger_direction(self) -> None:
        """The two answers differ for a long take profit, so they must be
        computed independently - a long take profit has d=true but closes
        with a sell."""
        self.assertTrue(bulk._protection_trigger_direction("tp", "long"))
        self.assertEqual(bulk._protection_closing_side("long"), "sell")
        self.assertFalse(bulk._protection_trigger_direction("tp", "short"))
        self.assertEqual(bulk._protection_closing_side("short"), "buy")
        self.assertFalse(bulk._protection_trigger_direction("st", "long"))
        self.assertEqual(bulk._protection_closing_side("long"), "sell")
        self.assertTrue(bulk._protection_trigger_direction("st", "short"))
        self.assertEqual(bulk._protection_closing_side("short"), "buy")

    def test_the_success_message_names_the_trigger_and_the_closing_side(self) -> None:
        node = _BulkNode(mark=100000.0, position_side="long", position_size=0.125)
        harness = _Harness(node)
        resp, _submit = harness.run("set_tp", "120000")
        self.assertTrue(resp.success)
        message = resp.position_action.message
        self.assertIn("at or above", message)
        self.assertIn("sell", message)
        self.assertIn("reduce-only", message)


class TestBtcShortTakeProfitAt50000(unittest.TestCase):
    """The operator's exact case: a BTC SHORT with a take profit at 50000.

    The short take profit must trigger BELOW the mark (``d = false``). This
    is the case that failed on 2026-11-27 - but it failed at the
    transaction level ("bad signature"), not because of the trigger
    direction.
    """

    def test_short_take_profit_at_50000_sends_d_false(self) -> None:
        node = _BulkNode(mark=95300.0, position_side="short", position_size=-0.0001)
        harness = _Harness(node)
        resp, submit = harness.run("set_tp", "50000")
        self.assertTrue(
            resp.success,
            f"the short TP at 50000 must be accepted: {getattr(resp, 'message', resp)}",
        )
        action = submit.call_args.args[1][0]["tp"]
        self.assertEqual(action["c"], "BTC-USD")
        self.assertFalse(
            action["d"],
            "a short take profit triggers BELOW the mark, so d must be false",
        )
        self.assertEqual(action["tr"], 50000.0)
        self.assertEqual(action["sz"], 0.0001)

    def test_the_node_accepts_the_short_take_profit_at_50000(self) -> None:
        """The independent model agrees with the exchange's live behaviour."""
        self.assertTrue(
            exchange_accepts_conditional("tp", "short", False, 50000.0, 95300.0)
        )
        self.assertFalse(
            exchange_accepts_conditional("tp", "short", True, 50000.0, 95300.0),
            "a short take profit with d=true would protect the wrong side",
        )

    def test_a_short_take_profit_above_the_mark_is_refused(self) -> None:
        """A trigger already satisfied fires instantly, so it is refused."""
        node = _BulkNode(mark=95300.0, position_side="short", position_size=-0.0001)
        harness = _Harness(node)
        resp, _submit = harness.run("set_tp", "96000")
        self.assertFalse(resp.success)
        self.assertIn("MARK", resp.error.code)


# ----------------------------------------------------------------------
# 2. Creation, update, independent removal with 0, restoration
# ----------------------------------------------------------------------

class TestProtectionLifecycle(unittest.TestCase):
    def test_creating_a_take_profit_leaves_the_stop_loss_alone(self) -> None:
        node = _BulkNode(mark=100000.0, position_side="long", position_size=0.125)
        node.rows.append(_live_row("st", "sl-1", 90000.0, False, -0.125))
        harness = _Harness(node)
        resp, _submit = harness.run("set_tp", "120000")
        self.assertTrue(resp.success)
        self.assertEqual(len(node.rows), 2)
        self.assertIn("sl-1", [r["orderId"] for r in node.rows])

    def test_removing_the_take_profit_with_zero_keeps_the_stop_loss(self) -> None:
        node = _BulkNode(mark=100000.0, position_side="long", position_size=0.125)
        node.rows.append(_live_row("tp", "tp-1", 120000.0, True, 0.125))
        node.rows.append(_live_row("st", "sl-1", 90000.0, False, -0.125))
        harness = _Harness(node)
        resp, submit = harness.run("set_tp", "0")
        self.assertTrue(resp.success)
        self.assertTrue(resp.position_action.removed)
        remaining = [r["orderId"] for r in node.rows]
        self.assertNotIn("tp-1", remaining)
        self.assertIn("sl-1", remaining)
        self.assertIn("Stop Loss", resp.position_action.message)
        # Exactly one cancellation, for the take profit only.
        cancels = [
            a for a in submit.call_args.args[1]
            if "cx" in a
        ]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0]["cx"]["oid"], "tp-1")

    def test_removing_the_stop_loss_with_zero_keeps_the_take_profit(self) -> None:
        node = _BulkNode(mark=100000.0, position_side="long", position_size=0.125)
        node.rows.append(_live_row("tp", "tp-1", 120000.0, True, 0.125))
        node.rows.append(_live_row("st", "sl-1", 90000.0, False, -0.125))
        harness = _Harness(node)
        resp, _submit = harness.run("set_sl", "0")
        self.assertTrue(resp.success)
        remaining = [r["orderId"] for r in node.rows]
        self.assertIn("tp-1", remaining)
        self.assertNotIn("sl-1", remaining)

    def test_removing_a_protection_that_was_never_set_is_a_no_op(self) -> None:
        node = _BulkNode(mark=100000.0, position_side="long", position_size=0.125)
        harness = _Harness(node)
        resp, _submit = harness.run("set_tp", "0")
        self.assertTrue(resp.success)
        self.assertFalse(resp.position_action.removed)
        self.assertIn("No Take Profit was set", resp.position_action.message)

    def test_updating_replaces_the_previous_protection(self) -> None:
        node = _BulkNode(mark=100000.0, position_side="long", position_size=0.125)
        node.rows.append(_live_row("tp", "tp-old", 120000.0, True, 0.125))
        harness = _Harness(node)
        resp, _submit = harness.run("set_tp", "130000")
        self.assertTrue(resp.success)
        self.assertEqual(len(node.rows), 1)
        self.assertEqual(node.rows[0]["trigger"]["px"], 130000.0)
        self.assertNotEqual(node.rows[0]["orderId"], "tp-old")


class TestRestorationAfterAFailedUpdate(unittest.TestCase):
    """A failed update must restore the protection it cancelled - and must
    never claim a protection was lost when none existed.

    The node rejects every conditional with a fixed reason, so the
    replacement AND the restore both fail. That is the only state in which
    a protection is genuinely lost.
    """

    def test_a_failed_update_restores_the_previous_protection(self) -> None:
        """The replacement fails but the restore succeeds -> nothing lost."""
        node = _BulkNode(mark=100000.0, position_side="long", position_size=0.125)
        node.rows.append(_live_row("tp", "tp-old", 120000.0, True, 0.125))
        harness = _Harness(node)
        # The calls are: 1 = cancel the old TP, 2 = the replacement (fails),
        # 3 = the restore (succeeds).
        calls = {"n": 0}

        def _submit(_credentials, actions):
            calls["n"] += 1
            if calls["n"] == 2 and any("tp" in a for a in actions):
                return _rejected_response("size must be positive")
            return harness.node.submit(actions)

        creds = dict(CREDS)
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=dict(BTC_MARKET)), \
             mock.patch.object(bulk, "_live_account", side_effect=harness._account), \
             mock.patch.object(bulk, "_conditional_orders", side_effect=harness._book), \
             mock.patch.object(bulk, "_submit_order", side_effect=_submit):
            resp = bulk.execute({
                "operation": "set_tp", "exchange": "bulk", "account": "main",
                "symbol": "BTC", "price": "130000",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "PROTECTION_REJECTED")
        # The previous protection was cancelled and then put back.
        self.assertEqual(len(node.rows), 1)
        self.assertEqual(node.rows[0]["trigger"]["px"], 120000.0)
        self.assertFalse(
            resp.data.get("position_unprotected"),
            "the protection was restored, so the position is not unprotected",
        )
        self.assertNotIn("UNPROTECTED", resp.error.message)

    def test_a_failed_update_without_a_previous_protection_never_claims_a_loss(self) -> None:
        """No previous protection existed -> nothing was cancelled -> nothing
        can be lost. This is the misleading-message bug."""
        node = _BulkNode(mark=100000.0, position_side="long", position_size=0.125)
        node.reject_with = "size must be positive"
        harness = _Harness(node)
        resp, _submit = harness.run("set_tp", "130000")
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "PROTECTION_REJECTED")
        self.assertNotIn("UNPROTECTED", resp.error.message)
        self.assertNotIn("could NOT be restored", resp.error.message)
        self.assertFalse(resp.data.get("position_unprotected"))
        self.assertIsNone(resp.data.get("previous_protection_restored"))

    def test_a_lost_protection_is_reported_and_names_which_one(self) -> None:
        """When a previous protection existed and could NOT be restored, the
        report must name the lost protection (TP vs SL)."""
        node = _BulkNode(mark=100000.0, position_side="long", position_size=0.125)
        node.rows.append(_live_row("tp", "tp-old", 120000.0, True, 0.125))
        node.reject_with = "size must be positive"
        harness = _Harness(node)
        creds = dict(CREDS)
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=dict(BTC_MARKET)), \
             mock.patch.object(bulk, "_live_account", side_effect=harness._account), \
             mock.patch.object(bulk, "_conditional_orders", side_effect=harness._book), \
             mock.patch.object(bulk, "_submit_order", side_effect=harness._submit):
            resp = bulk.execute({
                "operation": "set_tp", "exchange": "bulk", "account": "main",
                "symbol": "BTC", "price": "130000",
            })
        self.assertFalse(resp.success)
        self.assertIn("UNPROTECTED", resp.error.message)
        self.assertIn("Take Profit", resp.error.message)
        self.assertTrue(resp.data.get("position_unprotected"))
        self.assertIs(resp.data.get("previous_protection_restored"), False)

    def test_a_lost_stop_loss_is_reported_as_a_stop_loss(self) -> None:
        node = _BulkNode(mark=100000.0, position_side="long", position_size=0.125)
        node.rows.append(_live_row("st", "sl-old", 90000.0, False, -0.125))
        node.reject_with = "size must be positive"
        harness = _Harness(node)
        creds = dict(CREDS)
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=dict(BTC_MARKET)), \
             mock.patch.object(bulk, "_live_account", side_effect=harness._account), \
             mock.patch.object(bulk, "_conditional_orders", side_effect=harness._book), \
             mock.patch.object(bulk, "_submit_order", side_effect=harness._submit):
            resp = bulk.execute({
                "operation": "set_sl", "exchange": "bulk", "account": "main",
                "symbol": "BTC", "price": "80000",
            })
        self.assertFalse(resp.success)
        self.assertIn("UNPROTECTED", resp.error.message)
        self.assertIn("Stop Loss", resp.error.message)


class TestFiredConditionalExecution(unittest.TestCase):
    """What the exchange does when a protective conditional FIRES.

    MEASURED on Bulk testnet (the exchange's own order-history rows for
    the fired order):

        long  position, take profit (d=true)  -> fired order side = "sell"
        long  position, stop loss   (d=false) -> fired order side = "sell"
        short position, take profit (d=false) -> fired order side = "buy"
        short position, stop loss   (d=true)  -> fired order side = "buy"

    The exchange derives the closing side ITSELF from the position, in
    every combination, INDEPENDENT of the trigger direction. So:

      * the agent's only job on the wire is the trigger direction (``d``),
        which selects WHEN the protection fires;
      * the closing side is the exchange's job, and the agent must not try
        to encode it (the wire ``sz`` field is unsigned);
      * a fired conditional is submitted reduce-only and IOC, so it can
        never increase exposure - at worst it is cancelled as
        ``cancelledIoc`` when it cannot fill.

    These are the exchange's recorded values, kept as a permanent record
    of what was measured.
    """

    MEASURED = (
        # (position_side, kind, trigger_above, fired_order_side)
        ("long", "tp", True, "sell"),
        ("long", "st", False, "sell"),
        ("short", "tp", False, "buy"),
        ("short", "st", True, "buy"),
    )

    def test_the_exchange_picks_the_closing_side_from_the_position(self) -> None:
        for position_side, kind, trigger_above, fired_side in self.MEASURED:
            with self.subTest(position_side=position_side, kind=kind):
                # The agent's closing-side calculation agrees with what the
                # exchange recorded.
                self.assertEqual(
                    bulk._protection_closing_side(position_side), fired_side
                )

    def test_the_closing_side_does_not_depend_on_the_trigger_direction(self) -> None:
        """The same position side always closes the same way, whatever the
        trigger direction is - which is why the two are separate."""
        for position_side in ("long", "short"):
            tp_sides = {fired for side, kind, _d, fired in self.MEASURED
                        if side == position_side and kind == "tp"}
            sl_sides = {fired for side, kind, _d, fired in self.MEASURED
                        if side == position_side and kind == "st"}
            self.assertEqual(tp_sides, sl_sides)
            self.assertEqual(len(tp_sides), 1)

    def test_the_agent_sends_an_absolute_size_not_a_signed_one(self) -> None:
        """The wire ``sz`` is unsigned, so the agent must send the absolute
        quantity and must never try to encode the closing side in it."""
        action = bulk._bulk_conditional_action(
            kind="tp", symbol="BTC-USD", size=bulk.Decimal("-0.125"),
            threshold=bulk.Decimal("120000"), trigger_above=False, isolated=False,
        )["tp"]
        self.assertEqual(action["sz"], 0.125)
        self.assertFalse(action["d"])


# ----------------------------------------------------------------------
# 3. Rejection reason surfacing
# ----------------------------------------------------------------------

class TestRejectionReasonSurfacing(unittest.TestCase):
    def test_the_exchange_reason_is_included_in_the_failure_message(self) -> None:
        node = _BulkNode(mark=100000.0, position_side="long", position_size=0.125)
        harness = _Harness(node)
        with mock.patch.object(harness, "_submit", side_effect=[
            _rejected_response("isolated account not set up for execution under parent"),
        ]):
            creds = dict(CREDS)
            with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
                 mock.patch.object(bulk, "_find_market", return_value=dict(BTC_MARKET)), \
                 mock.patch.object(bulk, "_live_account", side_effect=harness._account), \
                 mock.patch.object(bulk, "_conditional_orders", side_effect=harness._book), \
                 mock.patch.object(bulk, "_submit_order", side_effect=harness._submit):
                resp = bulk.execute({
                    "operation": "set_tp", "exchange": "bulk", "account": "main",
                    "symbol": "BTC", "price": "120000",
                })
        self.assertFalse(resp.success)
        self.assertIn("isolated account not set up for execution under parent", resp.error.message)
        self.assertIn("isolated account not set up for execution under parent",
                      resp.data.get("bulk_reasons", []))

    def test_a_transaction_level_signature_error_is_surfaced_verbatim(self) -> None:
        """A signature failure is not an order rejection; the node's message
        must reach the operator."""
        node = _BulkNode(mark=100000.0, position_side="long", position_size=0.125)
        node.signature_error = True
        harness = _Harness(node)
        resp, _submit = harness.run("set_tp", "120000")
        self.assertFalse(resp.success)
        self.assertIn("bad signature", resp.error.message)

    def test_reasons_are_parsed_from_both_payload_shapes(self) -> None:
        order_level = bulk._position_action_status(
            _rejected_response("could not find market: NOPE-USD")
        )
        self.assertEqual(order_level["classification"], "rejected")
        self.assertIn("could not find market: NOPE-USD", order_level["reasons"])
        transaction_level = bulk._position_action_status(_signature_error_response())
        self.assertIn("bad signature", transaction_level["reasons"])


# ----------------------------------------------------------------------
# 4. Both transaction-signing framings
# ----------------------------------------------------------------------

class TestTransactionFramingSelection(unittest.TestCase):
    def test_mainnet_uses_the_versionless_framing_and_testnet_uses_v12(self) -> None:
        self.assertIsNone(bulk._default_transaction_version({"base_url": bulk.DEFAULT_API_BASE}))
        self.assertEqual(
            bulk._default_transaction_version(
                {"base_url": "https://exchange-api.bulk.trade/api/v1"}), 12
        )

    def test_the_submitted_envelope_matches_the_endpoint_default(self) -> None:
        """The signed bytes and the envelope field must never diverge."""
        import base58 as _b58
        import nacl.signing as real_signing

        captured = {}

        class _RecordingSK:
            def __init__(self, seed):
                self._inner = real_signing.SigningKey(seed)

            @property
            def verify_key(self):
                return self._inner.verify_key

            def sign(self, message):
                captured["message"] = message
                return self._inner.sign(message)

        fake_signing = type("s", (), {"SigningKey": _RecordingSK})
        fake_nacl = type("m", (), {"signing": fake_signing})
        action = [{"m": {"c": "BTC-USD", "b": True, "sz": 0.1, "r": False, "i": False}}]

        for base_url, expect_prefix, expect_field in (
            (bulk.DEFAULT_API_BASE, False, None),
            ("https://exchange-api.bulk.trade/api/v1", True, 12),
        ):
            with self.subTest(base_url=base_url):
                creds = dict(CREDS)
                creds["base_url"] = base_url
                creds["agent_private_key"] = _b58.b58encode(bytes(range(32))).decode()
                with mock.patch.dict(sys.modules, {"nacl": fake_nacl, "nacl.signing": fake_signing}):
                    envelope = bulk._sign_transaction(creds, action, nonce=7)
                message = captured["message"]
                if expect_prefix:
                    self.assertEqual(envelope["version"], expect_field)
                    self.assertEqual(message[:4], struct.pack("<I", 12))
                    self.assertEqual(list(envelope.keys())[0], "version")
                else:
                    self.assertNotIn("version", envelope)
                    self.assertEqual(message[:8], struct.pack("<Q", 1))

    def test_a_signature_error_is_not_retried_with_the_other_framing(self) -> None:
        """Automatic framing fallback is disabled: the node's exact reason is
        surfaced instead of a second attempt."""
        calls = []

        def _post(_base_url, _path, payload, headers=None):
            calls.append(payload)
            return _signature_error_response()

        creds = dict(CREDS)
        with mock.patch.object(bulk, "_post_json", side_effect=_post):
            result = bulk._submit_order(creds, [{"m": {
                "c": "BTC-USD", "b": True, "sz": 0.1, "r": False, "i": False}}])
        self.assertEqual(len(calls), 1, "the agent must not retry the transaction")
        self.assertEqual(result["status"], "error")
        self.assertIn("bad signature", json.dumps(result))

    def test_the_per_account_override_reaches_the_submitted_envelope(self) -> None:
        import base58 as _b58

        creds = dict(CREDS)
        creds["base_url"] = "https://exchange-api.bulk.trade/api/v1"
        creds["transaction_version"] = "11"
        creds["agent_private_key"] = _b58.b58encode(bytes(range(32))).decode()
        envelope = bulk._sign_transaction(
            creds, [{"m": {"c": "BTC-USD", "b": True, "sz": 0.1, "r": False, "i": False}}],
            nonce=7,
        )
        self.assertNotIn("version", envelope)


# ----------------------------------------------------------------------
# 5. Conditional identification from live rows
# ----------------------------------------------------------------------

class TestConditionalIdentification(unittest.TestCase):
    def test_a_take_profit_and_a_stop_loss_are_bucketed_by_their_kind(self) -> None:
        rows = [
            _live_row("tp", "tp-1", 120000.0, True, 0.125),
            _live_row("st", "sl-1", 90000.0, False, -0.125),
        ]
        buckets = bulk._bucket_protection_rows(rows, "BTC-USD")
        self.assertEqual([r["orderId"] for r in buckets["tp"]], ["tp-1"])
        self.assertEqual([r["orderId"] for r in buckets["sl"]], ["sl-1"])

    def test_the_resting_size_sign_does_not_decide_the_kind(self) -> None:
        """Rule 4: the size sign is the trigger leg, not the position side.

        Both rows below have the SAME size sign but different kinds; the
        kind must come from ``orderType``.
        """
        tp = _live_row("tp", "tp-1", 120000.0, True, 0.125)
        sl = _live_row("st", "sl-1", 90000.0, False, 0.125)
        self.assertEqual(tp["size"], sl["size"])
        buckets = bulk._bucket_protection_rows([tp, sl], "BTC-USD")
        self.assertEqual([r["orderId"] for r in buckets["tp"]], ["tp-1"])
        self.assertEqual([r["orderId"] for r in buckets["sl"]], ["sl-1"])

    def test_the_protected_side_is_derived_from_the_kind_and_the_trigger(self) -> None:
        self.assertEqual(
            bulk._conditional_protected_side(_live_row("tp", "a", 120000.0, True, 0.125)), "long"
        )
        self.assertEqual(
            bulk._conditional_protected_side(_live_row("tp", "b", 80000.0, False, 0.125)), "short"
        )
        self.assertEqual(
            bulk._conditional_protected_side(_live_row("st", "c", 90000.0, False, 0.125)), "long"
        )
        self.assertEqual(
            bulk._conditional_protected_side(_live_row("st", "d", 120000.0, True, 0.125)), "short"
        )
        # The independent model agrees.
        for kind, is_above in (("tp", True), ("tp", False), ("st", False), ("st", True)):
            row = _live_row(kind, "x", 1.0, is_above, 0.1)
            self.assertEqual(
                bulk._conditional_protected_side(row), exchange_protected_side(kind, is_above)
            )

    def test_a_row_of_unknown_kind_is_not_bucketed(self) -> None:
        """Rule 5: guessing would let the agent cancel the wrong protection."""
        unknown = {
            "symbol": "BTC-USD", "orderId": "x-1", "price": 1.0, "size": 1.0,
            "reduceOnly": True, "orderType": "somethingElse",
            "trigger": {"isAbove": True, "px": 1.0},
        }
        buckets = bulk._bucket_protection_rows([unknown], "BTC-USD")
        self.assertEqual(buckets["tp"], [])
        self.assertEqual(buckets["sl"], [])

    def test_a_conditional_for_the_other_side_does_not_protect_this_position(self) -> None:
        """A take profit BELOW the mark protects a SHORT, not a long."""
        rows = [_live_row("tp", "tp-short", 80000.0, False, 0.125)]
        with mock.patch.object(bulk, "_conditional_orders", return_value=rows):
            buckets = bulk._protection_rows_for_position(
                {}, {}, "BTC-USD", False, position_side="long"
            )
        self.assertEqual(buckets["tp"], [])
        with mock.patch.object(bulk, "_conditional_orders", return_value=rows):
            buckets = bulk._protection_rows_for_position(
                {}, {}, "BTC-USD", False, position_side="short"
            )
        self.assertEqual([r["orderId"] for r in buckets["tp"]], ["tp-short"])


# ----------------------------------------------------------------------
# 6. Minimum notional
# ----------------------------------------------------------------------

class TestMinimumNotionalExemption(unittest.TestCase):
    def test_a_conditional_below_the_minimum_notional_is_still_submitted(self) -> None:
        """Rule 7: minNotional is not enforced on reduce-only conditionals.

        A 0.001 BTC position at 50000 has a notional of 50 - below the 100
        minimum set here.
        """
        market = dict(BTC_MARKET)
        market["minNotional"] = 100.0
        note = bulk._protection_min_notional_note(market, bulk.Decimal("0.001"), bulk.Decimal("50000"), "Take Profit")
        self.assertIsNotNone(note)
        self.assertIn("minimum notional", note)
        self.assertIn("50", note)

    def test_a_conditional_above_the_minimum_notional_is_not_flagged(self) -> None:
        note = bulk._protection_min_notional_note(
            BTC_MARKET, bulk.Decimal("0.125"), bulk.Decimal("120000"), "Take Profit"
        )
        self.assertIsNone(note)

    def test_the_minimum_notional_note_does_not_block_the_submission(self) -> None:
        """The note is advisory: the order is still submitted."""
        node = _BulkNode(mark=100000.0, position_side="long", position_size=0.001)
        market = dict(BTC_MARKET)
        market["minNotional"] = 1000.0     # 0.001 x 120000 = 120 < 1000
        creds = dict(CREDS)
        harness = _Harness(node)
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=market), \
             mock.patch.object(bulk, "_live_account", side_effect=harness._account), \
             mock.patch.object(bulk, "_conditional_orders", side_effect=harness._book), \
             mock.patch.object(bulk, "_submit_order", side_effect=harness._submit):
            resp = bulk.execute({
                "operation": "set_tp", "exchange": "bulk", "account": "main",
                "symbol": "BTC", "price": "120000",
            })
        self.assertTrue(resp.success)
        self.assertIn("minimum notional", resp.position_action.message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
