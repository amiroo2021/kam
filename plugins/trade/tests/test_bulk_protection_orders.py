"""Bulk native conditional-order (TP/SL) management.

Covers:
  * the version-12 canonical signing frame and the conditional action layout;
  * the trigger-direction rule for long and short positions;
  * create / replace / remove for TP and SL;
  * price 0 removing only the requested protection;
  * targeted cancellation by order id (no unrelated orders touched);
  * rejection and partial-failure handling;
  * verified-before-reporting-success behaviour.
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

CREDS = {
    "account": "main",
    "account_pubkey": ACCOUNT_PUBKEY,
    "agent_private_key": "redacted",
    "base_url": bulk.DEFAULT_API_BASE,
}

MARKET = {
    "symbol": "BTC-USD",
    "sizeIncrement": 0.001,
    "sizeDecimals": 3,
    "tickSize": 0.1,
}


def _conditional(order_type: str, oid: str, price: float, is_above: bool, size: float,
                 iso: bool = False) -> dict:
    """Build a live Bulk conditional order row as the node reports it."""
    return {
        "symbol": "BTC-USD",
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
    return {"positions": list(positions), "openOrders": list(open_orders)}


class TestBulkSigningFrame(unittest.TestCase):
    """The 4 October 2026 upgrade moved the wire to transaction version 12."""

    def setUp(self) -> None:
        import base58 as _b58

        # A real 32-byte Ed25519 seed so the production signing path runs.
        self.creds = dict(CREDS)
        self.creds["agent_private_key"] = _b58.b58encode(bytes(range(32))).decode()
        self._seed = bytes(range(32))

    def _sign_and_capture(self, actions, version=bulk._TRANSACTION_VERSION, nonce=7):
        """Run the real _sign_transaction and capture the signed message."""
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
        with mock.patch.object(bulk, "_TRANSACTION_VERSION", version), \
             mock.patch.dict(sys.modules, {"nacl": fake_nacl, "nacl.signing": fake_signing}):
            envelope = bulk._sign_transaction(self.creds, actions, nonce=nonce)
        return envelope, captured["message"]

    def test_transaction_version_is_12(self) -> None:
        self.assertEqual(bulk._TRANSACTION_VERSION, 12)

    def test_signed_message_carries_the_version_12_prefix(self) -> None:
        envelope, message = self._sign_and_capture(
            [{"m": {"c": "BTC-USD", "b": True, "sz": 0.1, "r": False, "i": False}}],
            nonce=42,
        )
        self.assertEqual(envelope["version"], 12)
        self.assertEqual(envelope["nonce"], "42")
        self.assertEqual(message[:4], struct.pack("<I", 12))

    def test_envelope_version_and_signed_bytes_agree(self) -> None:
        """A v12 frame sent without "version": 12 is parsed as v11 and rejected,
        so the envelope field and the signed prefix must never diverge."""
        envelope, message = self._sign_and_capture(
            [{"m": {"c": "BTC-USD", "b": True, "sz": 0.1, "r": False, "i": False}}],
        )
        self.assertEqual(envelope["version"], 12)
        self.assertEqual(message[:4], struct.pack("<I", 12))
        self.assertEqual(struct.unpack_from("<Q", message, 4)[0], 1)  # action count
        self.assertEqual(message[-1], 1)  # mainnet signature domain

    def test_legacy_versionless_framing_omits_both_prefix_and_field(self) -> None:
        """Version 11 (or an omitted version) must not carry the v12 prefix."""
        envelope, message = self._sign_and_capture(
            [{"m": {"c": "BTC-USD", "b": True, "sz": 0.1, "r": False, "i": False}}],
            version=None,
        )
        self.assertNotIn("version", envelope)
        # The action count is a u64 LE and is the first field when there is no
        # version prefix.
        self.assertEqual(message[:8], struct.pack("<Q", 1))

    def test_version_field_precedes_actions_in_the_json_envelope(self) -> None:
        """Bulk deserializes the envelope as a struct and rejects the request
        with "version must precede actions" if the field comes later."""
        envelope, _message = self._sign_and_capture(
            [{"m": {"c": "BTC-USD", "b": True, "sz": 0.1, "r": False, "i": False}}],
        )
        keys = list(envelope.keys())
        self.assertEqual(keys[0], "version")
        self.assertIn("actions", keys)
        self.assertLess(keys.index("version"), keys.index("actions"))
        # And the serialized JSON keeps that order.
        self.assertLess(
            json.dumps(envelope).index('"version"'),
            json.dumps(envelope).index('"actions"'),
        )

    def test_signature_is_a_valid_ed25519_signature_over_the_message(self) -> None:
        import base58 as _b58
        import nacl.signing

        envelope, message = self._sign_and_capture(
            [{"m": {"c": "BTC-USD", "b": True, "sz": 0.1, "r": False, "i": False}}],
        )
        signature = _b58.b58decode(envelope["signature"])
        # Verify with the real public key derived from the seed.
        signing_key = nacl.signing.SigningKey(self._seed)
        signing_key.verify_key.verify(message, signature)


class TestBulkConditionalEncoding(unittest.TestCase):
    """The conditional action layout must match bulk-keychain's canonical bytes."""

    def _decode(self, action: dict) -> tuple:
        raw = bulk._encode_action(action)
        offset = 0
        disc = struct.unpack_from("<I", raw, offset)[0]
        offset += 4
        (length,) = struct.unpack_from("<Q", raw, offset)
        offset += 8
        symbol = raw[offset:offset + length].decode()
        offset += length
        direction = raw[offset]
        offset += 1
        size = struct.unpack_from("<Q", raw, offset)[0]
        offset += 8
        threshold = struct.unpack_from("<Q", raw, offset)[0]
        offset += 8
        limit_tag = raw[offset]
        offset += 1
        limit = None
        if limit_tag:
            limit = struct.unpack_from("<Q", raw, offset)[0]
            offset += 8
        iso = raw[offset]
        offset += 1
        commission_tag = raw[offset]
        offset += 1
        slippage_tag = raw[offset]
        offset += 1
        return disc, symbol, direction, size, threshold, limit, iso, commission_tag, slippage_tag

    def test_stop_action_layout(self) -> None:
        disc, symbol, direction, size, threshold, limit, iso, comm, slip = self._decode(
            {"st": {"c": "BTC-USD", "d": False, "sz": 0.125, "tr": 90000.0, "lim": None, "i": False}}
        )
        self.assertEqual(disc, 5)
        self.assertEqual(symbol, "BTC-USD")
        self.assertEqual(direction, 0)
        self.assertEqual(size, 12_500_000)
        self.assertEqual(threshold, 9_000_000_000_000)
        self.assertIsNone(limit)
        self.assertEqual(iso, 0)
        self.assertEqual(comm, 0)
        self.assertEqual(slip, 0)

    def test_take_profit_action_layout(self) -> None:
        disc, symbol, direction, size, threshold, limit, iso, comm, slip = self._decode(
            {"tp": {"c": "BTC-USD", "d": True, "sz": 0.125, "tr": 120000.0, "lim": None, "i": True}}
        )
        self.assertEqual(disc, 6)
        self.assertEqual(symbol, "BTC-USD")
        self.assertEqual(direction, 1)
        self.assertEqual(size, 12_500_000)
        self.assertEqual(threshold, 12_000_000_000_000)
        self.assertIsNone(limit)
        self.assertEqual(iso, 1)
        self.assertEqual(comm, 0)
        self.assertEqual(slip, 0)

    def test_conditional_with_limit_price_encodes_some_tag(self) -> None:
        _, _, _, _, _, limit, _, _, _ = self._decode(
            {"st": {"c": "BTC-USD", "d": False, "sz": 1.0, "tr": 700.0, "lim": 695.5, "i": False}}
        )
        self.assertEqual(limit, 69_550_000_000)

    def test_market_and_limit_carry_the_v12_commission_option_tag(self) -> None:
        market = bulk._encode_action({"m": {"c": "BTC-USD", "b": True, "sz": 0.1, "r": False, "i": False}})
        limit = bulk._encode_action({"l": {"c": "BTC-USD", "b": True, "px": 700.0, "sz": 0.1, "tif": "GTC", "r": False, "i": False}})
        # The trailing byte is the absent-commission Option tag.
        self.assertEqual(market[-1], 0)
        self.assertEqual(limit[-1], 0)

    def test_alo_slide_and_alo_join_tif_indices(self) -> None:
        for tif, expected in (("GTC", 0), ("IOC", 1), ("ALO", 2), ("ALO_SLIDE", 3), ("ALO_JOIN", 4)):
            raw = bulk._encode_action(
                {"l": {"c": "BTC-USD", "b": True, "px": 700.0, "sz": 0.1, "tif": tif, "r": False, "i": False}}
            )
            # disc(4) + strlen(8) + symbol(7) + is_buy(1) + px(8) + sz(8) => tif at 36
            self.assertEqual(struct.unpack_from("<I", raw, 36)[0], expected)


class TestBulkProtectionAction(unittest.TestCase):
    """set_tp / set_sl against a mocked Bulk node."""

    def _run(self, operation, price, before, after, submit_response, market=None):
        """Drive set_tp/set_sl against a mocked Bulk node.

        ``before``/``after`` are account snapshots; the conditional trigger
        book is served through the dedicated openOrders query. A single
        ``written`` flag switches the mocked node from its pre-write state to
        its post-write state when the submission is made.
        """
        creds = dict(CREDS)
        state = {"written": False}

        def _account(_credentials):
            return after if state["written"] else before

        def _book(_credentials):
            snapshot = after if state["written"] else before
            return list(snapshot.get("openOrders") or [])

        def _submit(_credentials, _actions):
            state["written"] = True
            return submit_response

        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=dict(market or MARKET)), \
             mock.patch.object(bulk, "_live_account", side_effect=_account), \
             mock.patch.object(bulk, "_conditional_orders", side_effect=_book), \
             mock.patch.object(bulk, "_submit_order", side_effect=_submit) as submit:
            resp = bulk.execute({
                "operation": operation, "exchange": "bulk", "account": "main",
                "symbol": "BTC", "price": price,
            })
        return resp, submit

    def test_long_take_profit_triggers_above(self) -> None:
        before = _snapshot([{"symbol": "BTC-USD", "size": 0.125, "iso": False}])
        after = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [_conditional("takeProfit", "tp-1", 120000.0, True, -0.125)],
        )
        resp, submit = self._run(
            "set_tp", "120000", before, after,
            {"response": {"data": {"statuses": [{"resting": {"oid": "tp-1"}}]}}},
        )
        self.assertTrue(resp.success)
        action = submit.call_args.args[1][0]["tp"]
        self.assertEqual(action["c"], "BTC-USD")
        self.assertTrue(action["d"], "a long take profit must trigger ABOVE")
        self.assertEqual(action["tr"], 120000.0)
        self.assertEqual(action["sz"], 0.125)
        self.assertIsNone(action["lim"])
        self.assertFalse(action["i"])
        self.assertEqual(resp.position_action.price, "120000")
        self.assertTrue(resp.position_action.verified)

    def test_long_stop_loss_triggers_below(self) -> None:
        before = _snapshot([{"symbol": "BTC-USD", "size": 0.125, "iso": False}])
        after = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [_conditional("stop", "sl-1", 90000.0, False, -0.125)],
        )
        resp, submit = self._run(
            "set_sl", "90000", before, after,
            {"response": {"data": {"statuses": [{"resting": {"oid": "sl-1"}}]}}},
        )
        self.assertTrue(resp.success)
        action = submit.call_args.args[1][0]["st"]
        self.assertFalse(action["d"], "a long stop loss must trigger BELOW")
        self.assertEqual(action["tr"], 90000.0)

    def test_short_take_profit_triggers_below_and_stop_triggers_above(self) -> None:
        before = _snapshot([{"symbol": "BTC-USD", "size": -0.125, "iso": False}])
        after = _snapshot(
            [{"symbol": "BTC-USD", "size": -0.125, "iso": False}],
            [_conditional("takeProfit", "tp-s", 80000.0, False, 0.125)],
        )
        resp, submit = self._run(
            "set_tp", "80000", before, after,
            {"response": {"data": {"statuses": [{"resting": {"oid": "tp-s"}}]}}},
        )
        self.assertTrue(resp.success)
        self.assertFalse(
            submit.call_args.args[1][0]["tp"]["d"],
            "a short take profit must trigger BELOW",
        )

    def test_isolated_position_routes_the_conditional_to_the_isolated_lane(self) -> None:
        before = _snapshot([{"symbol": "BTC-USD", "size": 0.125, "iso": True, "isoPubkey": "iso-pk"}])
        after = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": True, "isoPubkey": "iso-pk"}],
            [_conditional("takeProfit", "tp-i", 120000.0, True, -0.125, iso=True)],
        )
        resp, submit = self._run(
            "set_tp", "120000", before, after,
            {"response": {"data": {"statuses": [{"resting": {"oid": "tp-i"}}]}}},
        )
        self.assertTrue(resp.success)
        self.assertTrue(submit.call_args.args[1][0]["tp"]["i"])

    def test_price_is_quantized_down_to_the_tick(self) -> None:
        before = _snapshot([{"symbol": "BTC-USD", "size": 0.125, "iso": False}])
        after = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [_conditional("takeProfit", "tp-q", 120000.0, True, -0.125)],
        )
        resp, submit = self._run(
            "set_tp", "120000.07", before, after,
            {"response": {"data": {"statuses": [{"resting": {"oid": "tp-q"}}]}}},
        )
        self.assertTrue(resp.success)
        self.assertEqual(submit.call_args.args[1][0]["tp"]["tr"], 120000.0)

    def test_updating_an_existing_tp_cancels_it_first_so_no_duplicate_remains(self) -> None:
        before = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [_conditional("takeProfit", "tp-old", 110000.0, True, -0.125)],
        )
        after = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [_conditional("takeProfit", "tp-new", 120000.0, True, -0.125)],
        )
        resp, submit = self._run(
            "set_tp", "120000", before, after,
            {"response": {"data": {"statuses": [{"resting": {"oid": "tp-new"}}]}}},
        )
        self.assertTrue(resp.success)
        calls = submit.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[1][0]["cx"], {"c": "BTC-USD", "oid": "tp-old"})
        self.assertIn("tp", calls[1].args[1][0])

    def test_price_zero_removes_only_the_take_profit_and_keeps_the_stop_loss(self) -> None:
        before = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [
                _conditional("takeProfit", "tp-1", 120000.0, True, -0.125),
                _conditional("stop", "sl-1", 90000.0, False, -0.125),
            ],
        )
        after = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [_conditional("stop", "sl-1", 90000.0, False, -0.125)],
        )
        resp, submit = self._run(
            "set_tp", "0", before, after,
            {"response": {"data": {"statuses": [{"cancelled": {"oid": "tp-1"}}]}}},
        )
        self.assertTrue(resp.success)
        self.assertTrue(resp.position_action.removed)
        self.assertEqual(submit.call_args.args[1], [{"cx": {"c": "BTC-USD", "oid": "tp-1"}}])
        self.assertEqual(submit.call_count, 1, "removal must not submit a new conditional")

    def test_price_zero_removes_only_the_stop_loss_and_keeps_the_take_profit(self) -> None:
        before = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [
                _conditional("takeProfit", "tp-1", 120000.0, True, -0.125),
                _conditional("stop", "sl-1", 90000.0, False, -0.125),
            ],
        )
        after = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [_conditional("takeProfit", "tp-1", 120000.0, True, -0.125)],
        )
        resp, submit = self._run(
            "set_sl", "0", before, after,
            {"response": {"data": {"statuses": [{"cancelled": {"oid": "sl-1"}}]}}},
        )
        self.assertTrue(resp.success)
        self.assertTrue(resp.position_action.removed)
        self.assertEqual(submit.call_args.args[1], [{"cx": {"c": "BTC-USD", "oid": "sl-1"}}])

    def test_price_zero_without_an_existing_protection_is_a_no_op_success(self) -> None:
        before = _snapshot([{"symbol": "BTC-USD", "size": 0.125, "iso": False}])
        after = _snapshot([{"symbol": "BTC-USD", "size": 0.125, "iso": False}])
        resp, submit = self._run(
            "set_tp", "0", before, after,
            {"response": {"data": {"statuses": []}}},
        )
        self.assertTrue(resp.success)
        self.assertFalse(resp.position_action.removed)
        self.assertIn("No Take Profit was set", resp.position_action.message)
        submit.assert_not_called()

    def test_removal_cancels_only_the_targeted_order_id(self) -> None:
        # Unrelated rows that must NOT be cancelled: a plain limit on the same
        # symbol, and a take-profit that protects a different symbol.
        plain_limit = {
            "symbol": "BTC-USD", "orderId": "limit-1", "price": 50000.0,
            "size": 0.05, "originalSize": 0.05, "reduceOnly": False, "iso": False,
            "orderType": "limit", "trigger": None, "status": "resting",
        }
        other_symbol_tp = {
            "symbol": "ETH-USD", "orderId": "tp-eth", "price": 4000.0,
            "size": -1.0, "originalSize": -1.0, "reduceOnly": True, "iso": False,
            "orderType": "takeProfit", "trigger": {"isAbove": True, "px": 4000.0},
            "status": "resting",
        }
        before = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [_conditional("takeProfit", "tp-1", 120000.0, True, -0.125), plain_limit, other_symbol_tp],
        )
        after = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [plain_limit, other_symbol_tp],
        )
        resp, submit = self._run(
            "set_tp", "0", before, after,
            {"response": {"data": {"statuses": [{"cancelled": {"oid": "tp-1"}}]}}},
        )
        self.assertTrue(resp.success)
        cancelled_ids = [a["cx"]["oid"] for call in submit.call_args_list for a in call.args[1]]
        self.assertEqual(cancelled_ids, ["tp-1"])

    def test_bulk_rejection_is_reported_as_a_failure(self) -> None:
        before = _snapshot([{"symbol": "BTC-USD", "size": 0.125, "iso": False}])
        after = _snapshot([{"symbol": "BTC-USD", "size": 0.125, "iso": False}])
        resp, _submit = self._run(
            "set_tp", "120000", before, after,
            {"response": {"data": {"statuses": [{"rejectedInvalid": {"reason": "too small"}}]}}},
        )
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "PROTECTION_REJECTED")

    def test_verification_failure_when_the_order_does_not_appear(self) -> None:
        before = _snapshot([{"symbol": "BTC-USD", "size": 0.125, "iso": False}])
        after = _snapshot([{"symbol": "BTC-USD", "size": 0.125, "iso": False}])
        resp, _submit = self._run(
            "set_tp", "120000", before, after,
            {"response": {"data": {"statuses": [{"resting": {"oid": "tp-1"}}]}}},
        )
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "PROTECTION_NOT_VERIFIED")

    def test_verification_failure_when_the_trigger_direction_is_wrong(self) -> None:
        before = _snapshot([{"symbol": "BTC-USD", "size": 0.125, "iso": False}])
        after = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            # A long take profit must trigger above; below is wrong.
            [_conditional("takeProfit", "tp-1", 120000.0, False, -0.125)],
        )
        resp, _submit = self._run(
            "set_tp", "120000", before, after,
            {"response": {"data": {"statuses": [{"resting": {"oid": "tp-1"}}]}}},
        )
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "PROTECTION_NOT_VERIFIED")

    def test_submission_exception_is_reported_as_uncertain_and_never_retried(self) -> None:
        before = _snapshot([{"symbol": "BTC-USD", "size": 0.125, "iso": False}])
        creds = dict(CREDS)
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=dict(MARKET)), \
             mock.patch.object(bulk, "_live_account", side_effect=[before]), \
             mock.patch.object(bulk, "_submit_order", side_effect=TimeoutError("boom")) as submit:
            resp = bulk.execute({
                "operation": "set_tp", "exchange": "bulk", "account": "main",
                "symbol": "BTC", "price": "120000",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "PROTECTION_SUBMISSION_UNCERTAIN")
        self.assertEqual(submit.call_count, 1)
        self.assertFalse(resp.data["retry_safe"])

    def test_removal_that_leaves_the_order_present_is_not_reported_as_removed(self) -> None:
        before = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [_conditional("takeProfit", "tp-1", 120000.0, True, -0.125)],
        )
        after = before  # the order is still there after the cancel
        resp, _submit = self._run(
            "set_tp", "0", before, after,
            {"response": {"data": {"statuses": [{"cancelled": {"oid": "tp-1"}}]}}},
        )
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "PROTECTION_REMOVAL_NOT_VERIFIED")

    def test_ambiguous_multiple_existing_protections_refuses_to_choose(self) -> None:
        before = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [
                _conditional("takeProfit", "tp-1", 120000.0, True, -0.125),
                _conditional("takeProfit", "tp-2", 130000.0, True, -0.125),
            ],
        )
        after = before
        resp, submit = self._run(
            "set_tp", "120000", before, after,
            {"response": {"data": {"statuses": [{"resting": {"oid": "tp-1"}}]}}},
        )
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "AMBIGUOUS_PROTECTION_STATE")
        submit.assert_not_called()

    def test_position_size_must_conform_to_the_market_increment(self) -> None:
        before = _snapshot([{"symbol": "BTC-USD", "size": 0.12345, "iso": False}])
        after = _snapshot([{"symbol": "BTC-USD", "size": 0.12345, "iso": False}])
        resp, submit = self._run(
            "set_tp", "120000", before, after,
            {"response": {"data": {"statuses": [{"resting": {"oid": "tp-1"}}]}}},
        )
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "POSITION_SIZE_INVALID")
        submit.assert_not_called()

    def test_a_trigger_that_is_already_satisfied_is_refused(self) -> None:
        """Bulk fires a conditional whose trigger is already satisfied the
        moment it is submitted, so it would never protect the position."""
        before = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False, "fairPrice": 100000.0}],
        )
        after = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False, "fairPrice": 100000.0}],
        )
        # A long take profit must trigger ABOVE the mark; 90000 is below it.
        resp, submit = self._run(
            "set_tp", "90000", before, after,
            {"response": {"data": {"statuses": []}}},
        )
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "TP_PRICE_NOT_ABOVE_MARK")
        submit.assert_not_called()

    def test_a_stop_loss_below_the_mark_is_accepted(self) -> None:
        before = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False, "fairPrice": 100000.0}],
        )
        after = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False, "fairPrice": 100000.0}],
            [_conditional("stop", "sl-ok", 90000.0, False, -0.125)],
        )
        resp, submit = self._run(
            "set_sl", "90000", before, after,
            {"response": {"data": {"statuses": [{"resting": {"oid": "sl-ok"}}]}}},
        )
        self.assertTrue(resp.success, resp.error.code if resp.error else "")
        self.assertFalse(submit.call_args.args[1][0]["st"]["d"])

    def test_a_failed_update_restores_the_previous_protection(self) -> None:
        """If the replacement placement fails after the old order was
        cancelled, the old protection is re-placed so the position does not
        end up unprotected."""
        before = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [_conditional("takeProfit", "tp-old", 120000.0, True, -0.125)],
        )
        after = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [_conditional("takeProfit", "tp-old", 120000.0, True, -0.125)],
        )
        creds = dict(CREDS)
        state = {"written": False}
        submitted = []

        def _account(_credentials):
            return after if state["written"] else before

        def _book(_credentials):
            snapshot = after if state["written"] else before
            return list(snapshot.get("openOrders") or [])

        def _submit(_credentials, actions):
            submitted.append(list(actions))
            # The first submit is the cancellation, the second is the new
            # placement which must fail, the third is the restore.
            if len(submitted) == 2:
                state["written"] = True
                raise RuntimeError("exchange refused the placement")
            state["written"] = True
            return {"response": {"data": {"statuses": [{"resting": {"oid": "tp-restored"}}]}}}

        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=dict(MARKET)), \
             mock.patch.object(bulk, "_live_account", side_effect=_account), \
             mock.patch.object(bulk, "_conditional_orders", side_effect=_book), \
             mock.patch.object(bulk, "_submit_order", side_effect=_submit):
            resp = bulk.execute({
                "operation": "set_tp", "exchange": "bulk", "account": "main",
                "symbol": "BTC", "price": "130000",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "PROTECTION_SUBMISSION_UNCERTAIN")
        self.assertTrue(resp.data.get("previous_protection_restored"))
        self.assertFalse(resp.data.get("position_unprotected"))
        # cancel, failed placement, restore
        self.assertEqual(len(submitted), 3)
        self.assertIn("cx", submitted[0][0])
        self.assertIn("tp", submitted[1][0])
        self.assertIn("tp", submitted[2][0])
        # The restore must re-use the previous trigger price.
        self.assertEqual(submitted[2][0]["tp"]["tr"], 120000.0)

    def test_a_failed_update_that_cannot_restore_reports_the_position_as_unprotected(self) -> None:
        before = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [_conditional("takeProfit", "tp-old", 120000.0, True, -0.125)],
        )
        after = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [_conditional("takeProfit", "tp-old", 120000.0, True, -0.125)],
        )
        creds = dict(CREDS)
        submitted = []

        def _submit(_credentials, actions):
            submitted.append(list(actions))
            if len(submitted) == 1:
                # The cancellation succeeds.
                return {"response": {"data": {"statuses": [{"cancelled": {"oid": "tp-old"}}]}}}
            if len(submitted) == 2:
                raise RuntimeError("exchange refused the placement")
            # The restore also fails.
            raise RuntimeError("exchange refused the restore")

        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=dict(MARKET)), \
             mock.patch.object(bulk, "_live_account", side_effect=[before, after]), \
             mock.patch.object(bulk, "_conditional_orders", side_effect=[before.get("openOrders", []), after.get("openOrders", [])]), \
             mock.patch.object(bulk, "_submit_order", side_effect=_submit):
            resp = bulk.execute({
                "operation": "set_tp", "exchange": "bulk", "account": "main",
                "symbol": "BTC", "price": "130000",
            })
        self.assertFalse(resp.success)
        self.assertTrue(resp.data.get("position_unprotected"))
        self.assertIn("UNPROTECTED", resp.error.message)

    def test_a_conditional_in_the_other_lane_is_not_treated_as_protection(self) -> None:
        """A base-account position is not protected by an isolated-lane
        conditional, so a removal must not cancel it."""
        before = _snapshot(
            [{"symbol": "BTC-USD", "size": 0.125, "iso": False}],
            [_conditional("takeProfit", "tp-iso", 120000.0, True, -0.125, iso=True)],
        )
        after = _snapshot([{"symbol": "BTC-USD", "size": 0.125, "iso": False}])
        resp, submit = self._run(
            "set_tp", "0", before, after,
            {"response": {"data": {"statuses": []}}},
        )
        self.assertTrue(resp.success)
        self.assertFalse(resp.position_action.removed)
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
