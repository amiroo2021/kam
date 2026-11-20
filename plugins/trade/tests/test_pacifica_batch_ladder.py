"""Offline Pacifica batch ladder contract; no network or real credentials."""
from __future__ import annotations

import json
import unittest
from email.message import Message
from unittest import mock
from urllib.error import HTTPError

from solders.keypair import Keypair
import base58

from plugins.trade.agents import x_pacifica_agent as pacifica


class _Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode()
        self.headers = Message()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class TestPacificaBatchLadder(unittest.TestCase):
    def setUp(self):
        self.key = Keypair()
        self.creds = {
            "account": "offline", "address": str(self.key.pubkey()),
            "agent_wallet": str(self.key.pubkey()),
            "agent_private_key": base58.b58encode(bytes(self.key)).decode(),
        }
        self.posts = []
        self.accepted = set()
        self.history = []

    def _read(self, url, params=None):
        if url.endswith("/orders/history"):
            return {"data": self.history, "has_more": False}
        return {"data": [{"client_order_id": cid} for cid in self.accepted]}

    def _run(self, count=250, response=None, **options):
        with mock.patch.object(pacifica, "_lookup_credentials", return_value=self.creds), \
             mock.patch.object(pacifica, "_get_market_info", return_value={"tick_size": "0.01", "lot_size": "0.01"}), \
             mock.patch.object(pacifica, "_http_get_json", side_effect=self._read), \
             mock.patch.object(pacifica.urllib.request, "urlopen", side_effect=response), \
             mock.patch.object(pacifica.time, "sleep") as sleep:
            result = pacifica.execute({
                "operation": "ladder", "account": "offline", "symbol": "SOL",
                "side": "sell", "distribution": "uniform", "order_count": count,
                "total_volume": str(count), "start_price": "100", "end_price": "101",
                "submission_mode": "batch",  # batch tests opt in; production default is sequential
                **options,
            })
        return result, sleep

    def _post(self, req, timeout=None):
        body = json.loads(req.data)
        self.posts.append(body)
        results = []
        for action in body["actions"]:
            cid = action["data"]["client_order_id"]
            self.accepted.add(cid)
            results.append({"success": True, "order_id": len(self.accepted), "error": None})
        return _Response({"success": True, "data": {"results": results}})

    def test_250_batch_actions_are_signed_bounded_and_paced(self):
        result, sleep = self._run(response=self._post)
        self.assertEqual(len(self.posts), 25)
        self.assertTrue(all(1 <= len(p["actions"]) <= 10 for p in self.posts))
        self.assertEqual(result.ladder.batch_count, 25)
        self.assertEqual((result.ladder.accepted_child_count, result.ladder.rejected_child_count,
                          result.ladder.uncertain_child_count), (250, 0, 0))
        self.assertEqual(result.ladder.status, "success")
        self.assertEqual(len(self.accepted), 250)
        self.assertGreaterEqual(sleep.call_count, 24)
        for action in self.posts[0]["actions"]:
            self.assertEqual(action["type"], "Create")
            data = action["data"]
            self.assertEqual(data["account"], self.creds["address"])
            self.assertEqual(data["agent_wallet"], self.creds["agent_wallet"])
            self.assertEqual((data["symbol"], data["side"], data["tif"], data["reduce_only"]),
                             ("SOL", "ask", "GTC", False))
            self.assertEqual(data["amount"], "1.00")
            self.assertEqual(data["expiry_window"], 5000)
            payload = {k: data[k] for k in ("symbol", "price", "reduce_only", "amount",
                                            "side", "tif", "client_order_id")}
            msg = pacifica._prepare_signature_message({"type": "create_order", "timestamp": data["timestamp"],
                                                        "expiry_window": data["expiry_window"]}, payload)
            from solders.signature import Signature
            self.assertTrue(Signature.from_bytes(base58.b58decode(data["signature"]))
                            .verify(self.key.pubkey(), msg.encode()))

    def test_partial_results_and_unacknowledged_action_are_not_counted_accepted(self):
        def partial(req, timeout=None):
            self.posts.append(json.loads(req.data))
            actions = self.posts[-1]["actions"]
            self.accepted.add(actions[0]["data"]["client_order_id"])
            return _Response({"success": True, "data": {"results": [
                {"success": True, "order_id": 51, "error": None},
                {"success": False, "order_id": None, "error": "insufficient margin"},
                {"success": True, "order_id": None, "error": None},
            ]}})
        result, _ = self._run(count=3, response=partial, batch_size=3)
        self.assertEqual((result.ladder.accepted_child_count, result.ladder.rejected_child_count,
                          result.ladder.uncertain_child_count), (1, 1, 1))
        self.assertEqual(result.ladder.status, "partial_success")
        self.assertIn("insufficient margin", result.ladder.exchange_reason)
        self.assertEqual(len(self.posts), 1)

    def test_timeout_reconciles_positive_ids_and_never_replays_batch(self):
        def timeout(req, timeout=None):
            body = json.loads(req.data)
            self.posts.append(body)
            cid = body["actions"][0]["data"]["client_order_id"]
            self.accepted.add(cid)
            self.history.append({"client_order_id": body["actions"][1]["data"]["client_order_id"],
                                 "order_status": "filled", "order_id": 92})
            raise TimeoutError("read timed out")
        result, _ = self._run(count=3, response=timeout, batch_size=3)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual((result.ladder.accepted_child_count, result.ladder.uncertain_child_count), (2, 1))
        self.assertEqual(len({a["data"]["client_order_id"] for a in self.posts[0]["actions"]}), 3)

    def test_429_is_bounded_and_never_replays_sent_ids(self):
        def rate_limit(req, timeout=None):
            self.posts.append(json.loads(req.data))
            raise HTTPError(req.full_url, 429, "rate limited", {}, None)
        result, sleep = self._run(count=40, response=rate_limit)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len({a["data"]["client_order_id"] for p in self.posts for a in p["actions"]}),
                         sum(len(p["actions"]) for p in self.posts))
        self.assertEqual(result.ladder.accepted_child_count, 0)
        self.assertEqual(result.ladder.rejected_child_count, 0)
        self.assertEqual(result.ladder.uncertain_child_count, 10)
        self.assertEqual(result.ladder.not_attempted_child_count, 30)
        self.assertEqual(result.ladder.accepted_child_count + result.ladder.rejected_child_count +
                         result.ladder.uncertain_child_count + result.ladder.not_attempted_child_count, 40)
        self.assertFalse(result.ladder.verified)
        self.assertTrue(result.ladder.rate_limited)
        self.assertGreater(sleep.call_count, 0)

    def test_sequential_mode_does_not_use_batch_endpoint(self):
        sent = []
        def single(_creds, path, operation, payload):
            sent.append((path, operation, payload))
            self.accepted.add(payload["client_order_id"])
            return {"i": len(sent)}
        with mock.patch.object(pacifica, "_post_signed", side_effect=single):
            result, sleep = self._run(count=3, response=AssertionError("batch was called"),
                                      submission_mode="sequential")
        self.assertEqual(len(sent), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(sleep.call_args.args, (pacifica.PACIFICA_LADDER_MIN_SECONDS_PER_ACTION,))
        self.assertEqual(result.ladder.status, "success")

    def test_configured_batch_size_is_capped_or_invalid_fails_before_post(self):
        result, _ = self._run(count=7, response=self._post, batch_size=3)
        self.assertEqual([len(p["actions"]) for p in self.posts], [3, 3, 1])
        self.posts.clear()
        result, _ = self._run(count=3, response=self._post, batch_size=11)
        self.assertFalse(result.success)
        self.assertEqual(self.posts, [])
    def test_network_failure_and_ambiguous_envelope_do_not_retry(self):
        def broken(req, timeout=None):
            self.posts.append(json.loads(req.data))
            if len(self.posts) == 1:
                raise ConnectionResetError("connection reset after write")
            return _Response({"success": True, "data": {"results": [
                {"success": True, "order_id": None, "error": None},
            ]}})
        result, _ = self._run(count=2, response=broken, batch_size=1)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(result.ladder.uncertain_child_count, 1)
        self.assertEqual(result.ladder.not_attempted_child_count, 1)
        self.assertEqual(result.ladder.accepted_child_count, 0)
        self.assertEqual(result.ladder.rejected_child_count, 0)

    def test_batch_preserves_explicit_client_id_tif_reduce_only_and_price(self):
        child = {"symbol": "BTC", "side": "buy", "price": "70001.50", "size": "0.123",
                 "client_order_id": "93a1c17e-a52b-4e2c-b975-693a114ebd13",
                 "reduce_only": True, "tif": "ALO"}
        with mock.patch.object(pacifica, "_post_ladder_batch", return_value={
            "success": True, "data": {"results": [{"success": False, "error": "reduce only"}]}
        }) as post:
            rows, limited, preflight = pacifica._ladder_submit_batch(self.creds, [child], 4)
        data = post.call_args.args[0][0]["data"]
        self.assertEqual({k: data[k] for k in ("client_order_id", "tif", "reduce_only", "price", "amount", "side")},
                         {"client_order_id": child["client_order_id"], "tif": "ALO", "reduce_only": True,
                          "price": "70001.50", "amount": "0.123", "side": "bid"})
        self.assertEqual((rows[0]["index"], rows[0]["outcome"], limited, preflight), (4, "rejected", False, False))

    def test_250_mixed_actions_preserve_positional_errors_and_unique_ids(self):
        seen = 0
        def mixed(req, timeout=None):
            nonlocal seen
            body = json.loads(req.data)
            self.posts.append(body)
            results = []
            for action in body["actions"]:
                seen += 1
                if seen % 2 == 0 or seen > 242:
                    results.append({"success": False, "order_id": None, "error": "margin unavailable"})
                else:
                    self.accepted.add(action["data"]["client_order_id"])
                    results.append({"success": True, "order_id": seen, "error": None})
            return _Response({"success": True, "data": {"results": results}})
        result, _ = self._run(response=mixed)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual((result.ladder.accepted_child_count, result.ladder.rejected_child_count,
                          result.ladder.not_attempted_child_count), (5, 5, 240))
        self.assertEqual(result.ladder.status, "partial_success")
        self.assertEqual(len({a["data"]["client_order_id"] for p in self.posts for a in p["actions"]}), 10)
        self.assertIn("margin unavailable", result.ladder.exchange_reason)

    def test_env_fallback_mode_and_unsafe_pacing_validation(self):
        with mock.patch.dict(pacifica.os.environ, {"PACIFICA_LADDER_SUBMISSION_MODE": "sequential"}):
            sent = []
            def single(_creds, path, operation, payload):
                sent.append(payload)
                self.accepted.add(payload["client_order_id"])
                return {"i": len(sent)}
            with mock.patch.object(pacifica, "_post_signed", side_effect=single):
                response, _ = self._run(count=2, response=AssertionError("batch called"),
                                        submission_mode=None)
        self.assertEqual(response.ladder.batch_count, 2)
        self.assertEqual(len(sent), 2)
        response, _ = self._run(count=2, response=self._post, batch_size=2,
                                batch_interval_seconds=0)
        self.assertFalse(response.success)
        self.assertEqual(self.posts, [])
    def test_duplicate_client_ids_or_unsupported_order_type_fail_before_post(self):
        child = {"symbol": "BTC", "side": "buy", "price": "70001", "size": "0.123",
                 "client_order_id": "93a1c17e-a52b-4e2c-b975-693a114ebd13"}
        with mock.patch.object(pacifica, "_post_ladder_batch") as post:
            rows, limited, preflight = pacifica._ladder_submit_batch(self.creds, [child, child], 0)
            self.assertTrue(preflight)
            self.assertEqual([row["submitted"] for row in rows], [0, 0])
            self.assertIn("duplicate client_order_id", rows[-1]["reason"])
            rows, _, preflight = pacifica._ladder_submit_batch(
                self.creds, [{**child, "order_type": "market"}], 0,
            )
            self.assertTrue(preflight)
            self.assertIn("only supports limit", rows[0]["reason"])
            post.assert_not_called()
    def test_short_positional_results_reconcile_all_without_guessing_index(self):
        def short(req, timeout=None):
            body = json.loads(req.data)
            self.posts.append(body)
            self.accepted.add(body["actions"][2]["data"]["client_order_id"])
            return _Response({"success": True, "data": {"results": [
                {"success": True, "order_id": 1},
                {"success": False, "error": "margin unavailable"},
            ]}})
        result, _ = self._run(count=3, response=short, batch_size=3)
        self.assertEqual((result.ladder.accepted_child_count, result.ladder.rejected_child_count,
                          result.ladder.uncertain_child_count), (1, 0, 2))
        self.assertEqual(len(self.posts), 1)

    def test_conflicting_action_result_is_uncertain(self):
        with mock.patch.object(pacifica, "_post_ladder_batch", return_value={
            "success": True, "data": {"results": [{"success": False, "order_id": 999,
                                                     "error": "rejected"}]},
        }):
            rows, _, _ = pacifica._ladder_submit_batch(self.creds, [
                {"symbol": "BTC", "side": "buy", "price": "70001", "size": "0.123"},
            ], 0)
        self.assertEqual(rows[0]["outcome"], "uncertain")
    def test_duplicate_telegram_confirm_while_pacifica_ladder_runs_is_ignored(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from plugins.trade.wizard import TradeWizard

        started, release = Event(), Event()
        desk = mock.Mock()
        desk.list_exchanges.return_value = ["pacifica"]
        response = pacifica.make_failure(operation="ladder", exchange="pacifica",
                                         account="offline", code="LADDER_SUBMISSION_UNCERTAIN",
                                         message="Check client IDs before any retry")
        def execute(_request):
            started.set()
            release.wait(timeout=2)
            return response
        desk.execute.side_effect = execute
        wizard = TradeWizard(tradedesk=desk)
        key = ("offline",)
        state = wizard._state_for(key)
        state.state, state.exchange, state.account = "ladder_confirm", "pacifica", "offline"
        state.ladder.update({"symbol": "SOL", "side": "sell", "distribution": "uniform",
                             "order_count": "3", "total_volume": "3",
                             "start_price": "100", "end_price": "101"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(wizard.handle_callback, key, "confirm")
            self.assertTrue(started.wait(timeout=2))
            second = wizard.handle_callback(key, "confirm")
            release.set()
            first.result(timeout=2)
        self.assertIn("already", second.text.lower())
        desk.execute.assert_called_once()
    def test_all_uncertain_wizard_shows_reconciliation_warning(self):
        from plugins.trade.wizard import TradeWizard
        def timeout(req, timeout=None):
            self.posts.append(json.loads(req.data))
            raise TimeoutError("ambiguous POST")
        result, _ = self._run(count=2, response=timeout, batch_size=2)
        self.assertEqual(result.ladder.uncertain_child_count, 2)
        self.assertFalse(result.ladder.verified)
        desk = mock.Mock()
        desk.execute.return_value = result
        wizard = TradeWizard(tradedesk=desk)
        key = ("offline",)
        state = wizard._state_for(key)
        state.state, state.exchange, state.account = "ladder_confirm", "pacifica", "offline"
        state.ladder.update({"symbol": "SOL", "side": "sell", "distribution": "uniform",
                             "order_count": "2", "total_volume": "2",
                             "start_price": "100", "end_price": "101"})
        screen = wizard.handle_callback(key, "confirm")
        self.assertIn("Uncertain: 2", screen.text)
        self.assertIn("check history before any retry", screen.text)
    def test_signing_failure_and_filtered_children_are_not_exchange_rejections(self):
        with mock.patch.object(pacifica, "_build_signed_body", side_effect=ValueError("bad signature")):
            result, _ = self._run(count=3, response=AssertionError("must not POST"))
        self.assertEqual(len(self.posts), 0)
        self.assertEqual(result.ladder.batch_count, 0)
        self.assertEqual((result.ladder.rejected_child_count,
                          result.ladder.not_attempted_child_count), (0, 3))
        self.assertEqual(result.ladder.to_dict()["not_attempted_child_count"], 3)
        self.assertTrue(all(row["outcome"] == "not_attempted" for row in result.ladder.batches))

        original = pacifica._ladder_build_children
        def omit_one(**kw):
            children, _, _, _ = original(**kw)
            return children[:2], pacifica.Decimal("2"), 1, 2
        with mock.patch.object(pacifica, "_ladder_build_children", side_effect=omit_one):
            result, _ = self._run(count=3, response=self._post)
        self.assertEqual((result.ladder.accepted_child_count, result.ladder.rejected_child_count,
                          result.ladder.not_attempted_child_count), (2, 0, 1))
        self.assertEqual(result.ladder.status, "partial_success")
    def test_default_mode_is_sequential_and_batch_requires_explicit_opt_in(self):
        sent = []
        def single(_creds, path, operation, payload):
            sent.append(payload)
            self.accepted.add(payload["client_order_id"])
            return {"i": len(sent)}
        with mock.patch.object(pacifica, "_combined_pacifica_env", return_value={}), \
             mock.patch.object(pacifica, "_post_signed", side_effect=single):
            result, sleep = self._run(count=3, response=AssertionError("batch called"),
                                      submission_mode=None)
        self.assertEqual(result.ladder.status, "success")
        self.assertEqual((result.ladder.batch_count, len(sent), sleep.call_count), (3, 3, 2))
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()
