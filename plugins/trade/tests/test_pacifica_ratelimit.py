"""Offline Pacifica rate-limit header parsing and adaptive batch pacing."""
from __future__ import annotations

import json
import unittest
from decimal import Decimal
from email.message import Message
from unittest import mock
from urllib.error import HTTPError

from solders.keypair import Keypair
import base58

from plugins.trade.agents import x_pacifica_agent as pacifica


class _Response:
    def __init__(self, body, headers=None):
        self.body = json.dumps(body).encode()
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class TestParsePacificaRatelimitHeaders(unittest.TestCase):
    def test_divides_credit_fields_by_ten_but_not_time(self):
        parsed = pacifica.parse_pacifica_ratelimit_headers({
            "ratelimit": '"credits";r=990;t=22',
            "ratelimit-policy": '"credits";q=1000;w=60',
        })
        self.assertEqual(parsed["remaining"], Decimal("99.0"))
        self.assertEqual(parsed["quota"], Decimal("100.0"))
        self.assertEqual(parsed["reset_seconds"], 22.0)
        self.assertEqual(parsed["window_seconds"], 60.0)
        self.assertTrue(parsed["reliable"])

    def test_300_credit_quota_normalizes(self):
        parsed = pacifica.parse_pacifica_ratelimit_headers({
            "ratelimit": '"credits";r=3000;t=5',
            "ratelimit-policy": '"credits";q=3000;w=60',
        })
        self.assertEqual(parsed["remaining"], Decimal("300.0"))
        self.assertEqual(parsed["quota"], Decimal("300.0"))
        self.assertTrue(parsed["reliable"])

    def test_missing_or_malformed_headers_are_unreliable(self):
        self.assertFalse(pacifica.parse_pacifica_ratelimit_headers({})["reliable"])
        self.assertFalse(pacifica.parse_pacifica_ratelimit_headers({
            "ratelimit": "garbage",
        })["reliable"])
        self.assertFalse(pacifica.parse_pacifica_ratelimit_headers({
            "ratelimit": '"credits";r=-1;t=1',
            "ratelimit-policy": '"credits";q=1000;w=60',
        })["reliable"])

    def test_remaining_above_quota_is_unreliable(self):
        parsed = pacifica.parse_pacifica_ratelimit_headers({
            "ratelimit": '"credits";r=1200;t=5',
            "ratelimit-policy": '"credits";q=1000;w=60',
        })
        self.assertFalse(parsed["reliable"])
        self.assertEqual(parsed["remaining"], Decimal("120.0"))
        self.assertEqual(parsed["quota"], Decimal("100.0"))

    def test_message_headers_case_insensitive(self):
        msg = Message()
        msg["Ratelimit"] = '"credits";r=1200;t=32'
        msg["Ratelimit-Policy"] = '"credits";q=1250;w=60'
        parsed = pacifica.parse_pacifica_ratelimit_headers(msg)
        self.assertEqual(parsed["remaining"], Decimal("120.0"))
        self.assertEqual(parsed["quota"], Decimal("125.0"))
        self.assertTrue(parsed["reliable"])


class TestAdaptiveCreditWait(unittest.TestCase):
    def tearDown(self):
        pacifica.reset_pacifica_rate_limit_state()

    def test_floor_is_preserved_when_adaptive_disabled(self):
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=False,
            remaining=Decimal("5"), reset_seconds=40.0, quota=Decimal("100"),
        )
        self.assertEqual(wait, 8.0)

    def test_headers_with_ample_credits_keep_floor(self):
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=Decimal("99"), reset_seconds=22.0, quota=Decimal("100"),
        )
        self.assertEqual(wait, 8.0)

    def test_low_remaining_waits_full_rolling_window_not_just_t(self):
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=Decimal("5"), reset_seconds=17.0, quota=Decimal("100"),
        )
        self.assertEqual(wait, 60.0)

    def test_zero_or_missing_t_with_low_remaining_does_not_keep_floor(self):
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=Decimal("0"), reset_seconds=0.0, quota=Decimal("100"),
        )
        self.assertEqual(wait, 60.0)
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=Decimal("5"), reset_seconds=None, quota=Decimal("100"),
        )
        self.assertEqual(wait, 60.0)

    def test_missing_headers_use_local_window_and_stay_conservative(self):
        pacifica.reset_pacifica_rate_limit_state()
        now = 1_000.0
        for i in range(9):
            pacifica.record_pacifica_credit_spend(10.0, now=now + i)
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=None, reset_seconds=None, quota=Decimal("100"),
            now=now + 9,
        )
        self.assertGreaterEqual(wait, 8.0)
        self.assertGreater(wait, 8.0)

    def test_heavy_verify_reserve_blocks_when_remaining_equals_creates_only(self):
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=Decimal("10"), reset_seconds=30.0, quota=Decimal("100"),
        )
        self.assertEqual(wait, 60.0)

    def test_reserve_covers_one_verify_get_and_envelope(self):
        need = pacifica.pacifica_batch_wait_need(10)
        self.assertEqual(need, Decimal("23"))
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=need - Decimal("1"), reset_seconds=4.0, quota=Decimal("100"),
            header_age_seconds=0.0,
        )
        self.assertGreater(wait, 8.0)
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=need, reset_seconds=4.0, quota=Decimal("100"),
            header_age_seconds=0.0,
        )
        self.assertEqual(wait, 8.0)

    def test_stale_headers_cannot_keep_the_floor(self):
        pacifica.reset_pacifica_rate_limit_state()
        pacifica.record_pacifica_ratelimit_headers({
            "ratelimit": '"credits";r=990;t=50',
            "ratelimit-policy": '"credits";q=1000;w=60',
        })
        captured = float(pacifica._pacifica_rate_limit["captured_at"])
        now = captured + 16.0
        pacifica.record_pacifica_credit_spend(90.0, now=now - 10.0)
        snap = pacifica.pacifica_effective_rate_limit(now=now)
        self.assertIsNone(snap["remaining"])
        self.assertFalse(snap["reliable"])
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=snap["remaining"], quota=Decimal("100"), now=now,
        )
        self.assertGreater(wait, 8.0)
        wait_if_stale_r = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=Decimal("99"), quota=Decimal("100"), now=now,
        )
        self.assertEqual(wait_if_stale_r, 8.0)

    def test_malformed_followup_invalidates_stale_remaining(self):
        pacifica.reset_pacifica_rate_limit_state()
        pacifica.record_pacifica_ratelimit_headers({
            "ratelimit": '"credits";r=990;t=50',
            "ratelimit-policy": '"credits";q=1000;w=60',
        })
        pacifica.record_pacifica_ratelimit_headers({"ratelimit": "garbage"})
        snap = pacifica.pacifica_effective_rate_limit()
        self.assertIsNone(snap["remaining"])
        self.assertFalse(snap["reliable"])

    def test_local_window_boundary_expires_only_after_60s(self):
        pacifica.reset_pacifica_rate_limit_state()
        now = 5_000.0
        pacifica.record_pacifica_credit_spend(90.0, now=now)
        wait_inside = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=None, quota=Decimal("100"), now=now + 1.0,
        )
        self.assertGreater(wait_inside, 8.0)
        wait_after = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=None, quota=Decimal("100"), now=now + 60.1,
        )
        self.assertEqual(wait_after, 8.0)

    def test_header_remaining_sees_other_process_spend(self):
        pacifica.reset_pacifica_rate_limit_state()
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=Decimal("12"), reset_seconds=9.0, quota=Decimal("100"),
        )
        self.assertGreater(wait, 8.0)

    def test_credit_need_is_commit_plus_one_verify_get_not_three(self):
        self.assertEqual(pacifica.pacifica_batch_commit_need(10), Decimal("11"))
        need = pacifica.pacifica_batch_wait_need(10)
        self.assertEqual(need, Decimal("23"))
        self.assertEqual(
            need,
            10 * pacifica.PACIFICA_CREDIT_PER_CREATE
            + pacifica.PACIFICA_CREDIT_BATCH_ENVELOPE
            + pacifica.PACIFICA_CREDIT_HEAVY_GET,
        )
        self.assertEqual(pacifica.pacifica_batch_wait_need(10, get_cost=Decimal("1")), Decimal("12"))
        self.assertNotEqual(need, Decimal("47"))

    def test_fresh_remaining_is_not_double_counted_against_local_ledger(self):
        pacifica.reset_pacifica_rate_limit_state()
        pacifica.record_pacifica_credit_spend(11.0, now=1_000.0)
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=Decimal("58"), reset_seconds=40.0, quota=Decimal("100"),
            now=1_000.0, header_age_seconds=0.0,
        )
        self.assertEqual(wait, 8.0)

    def test_header_age_below_cap_does_not_invent_sibling_rate(self):
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=Decimal("55"), reset_seconds=40.0, quota=Decimal("100"),
            header_age_seconds=10.0,
        )
        self.assertEqual(wait, 8.0)

    def test_stale_header_fallback_uses_implied_occupancy(self):
        pacifica.reset_pacifica_rate_limit_state()
        pacifica.record_pacifica_ratelimit_headers({
            "ratelimit": '"credits";r=100;t=50',
            "ratelimit-policy": '"credits";q=1000;w=60',
        })
        captured = float(pacifica._pacifica_rate_limit["captured_at"])
        snap = pacifica.pacifica_effective_rate_limit(now=captured + 16.0)
        self.assertIsNone(snap["remaining"])
        self.assertEqual(snap["implied_used"], Decimal("90.0"))
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=None, quota=Decimal("100"), now=captured + 16.0,
            implied_used=snap["implied_used"],
        )
        self.assertGreater(wait, 8.0)

    def test_local_expiry_avoids_full_window_when_our_credits_return(self):
        pacifica.reset_pacifica_rate_limit_state()
        now = 8_000.0
        pacifica.record_pacifica_credit_spend(20.0, now=now - 48.0)
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=Decimal("54"), reset_seconds=17.0, quota=Decimal("100"),
            now=now, header_age_seconds=0.0,
        )
        self.assertGreaterEqual(wait, 8.0)
        self.assertLess(wait, 60.0)

    def test_zero_remaining_without_local_cover_waits_window(self):
        pacifica.reset_pacifica_rate_limit_state()
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10, floor_seconds=8.0, adaptive=True,
            remaining=Decimal("0"), reset_seconds=3.0, quota=Decimal("100"),
            header_age_seconds=0.0,
        )
        self.assertEqual(wait, 60.0)

    def test_out_of_order_headers_are_ignored(self):
        pacifica.reset_pacifica_rate_limit_state()
        pacifica.record_pacifica_ratelimit_headers({
            "ratelimit": '"credits";r=200;t=10',
            "ratelimit-policy": '"credits";q=1000;w=60',
        }, now=100.0)
        ignored = pacifica.record_pacifica_ratelimit_headers({
            "ratelimit": '"credits";r=990;t=10',
            "ratelimit-policy": '"credits";q=1000;w=60',
        }, now=90.0)
        self.assertTrue(ignored.get("ignored_out_of_order"))
        self.assertEqual(pacifica._pacifica_rate_limit["remaining"], Decimal("20.0"))

    def test_header_delta_records_actual_get_not_reserve(self):
        pacifica.reset_pacifica_rate_limit_state()
        pacifica.record_pacifica_ratelimit_headers({
            "ratelimit": '"credits";r=1000;t=50',
            "ratelimit-policy": '"credits";q=1000;w=60',
        }, now=1.0, kind="get")
        parsed = pacifica.record_pacifica_ratelimit_headers({
            "ratelimit": '"credits";r=990;t=49',
            "ratelimit-policy": '"credits";q=1000;w=60',
        }, now=1.1, kind="get")
        self.assertEqual(parsed["observed_spend"], Decimal("1.0"))
        self.assertEqual(parsed["get_cost"], Decimal("1.0"))
        amount = pacifica._record_pacifica_spend_observed_or_estimate(Decimal("12"))
        self.assertEqual(amount, Decimal("1.0"))
        self.assertEqual(pacifica._pacifica_window_used(2.0), 1.0)


def _credits(remaining, t=30, quota=100):
    scale = pacifica.PACIFICA_RATELIMIT_CREDIT_SCALE
    r = int(Decimal(str(remaining)) * scale)
    q = int(Decimal(str(quota)) * scale)
    return {
        "ratelimit": f'"credits";r={r};t={t}',
        "ratelimit-policy": f'"credits";q={q};w=60',
    }


def _record(remaining, t, now, kind):
    return pacifica.record_pacifica_ratelimit_headers(
        _credits(remaining, t=t), now=now, kind=kind,
    )


class TestGetCostLearning(unittest.TestCase):
    def setUp(self):
        pacifica.reset_pacifica_rate_limit_state()

    def tearDown(self):
        pacifica.reset_pacifica_rate_limit_state()

    def _wait_need(self):
        return pacifica.pacifica_batch_wait_need(
            10, get_cost=pacifica._pacifica_rate_limit.get("get_cost"),
        )

    def _wait_seconds(self, remaining):
        return pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10,
            floor_seconds=8.0,
            adaptive=True,
            remaining=Decimal(str(remaining)),
            reset_seconds=10.0,
            quota=Decimal("100"),
            header_age_seconds=0.0,
            get_cost=pacifica._pacifica_rate_limit.get("get_cost"),
        )

    def test_clean_get_to_get_96_95_94_learns_measured_cost(self):
        _record(96, 34, 1.0, "get")
        p1 = _record(95, 33, 1.3, "get")
        p2 = _record(94, 33, 1.6, "get")
        self.assertEqual(p1["observed_spend"], Decimal("1"))
        self.assertEqual(p2["observed_spend"], Decimal("1"))
        self.assertEqual(pacifica._pacifica_rate_limit["get_cost"], Decimal("1"))
        self.assertEqual(pacifica._pacifica_rate_limit["get_cost_source"], "observed")
        self.assertEqual(self._wait_need(), Decimal("12"))

    def test_remaining_increase_is_recovery_not_negative_spend(self):
        _record(94, 33, 1.0, "get")
        up = _record(96, 25, 9.0, "post")
        self.assertIsNone(up["observed_spend"])
        _record(85, 15, 19.0, "get")
        recovered = _record(93, 15, 19.3, "get")
        self.assertIsNone(recovered["observed_spend"])
        self.assertIsNone(pacifica._pacifica_rate_limit.get("get_cost"))
        self.assertEqual(self._wait_need(), Decimal("23"))

    def test_delayed_post_accounting_does_not_learn_get_cost(self):
        _record(96, 25, 1.0, "post")
        contaminated = _record(90, 24, 1.3, "get")
        self.assertEqual(contaminated["observed_spend"], Decimal("6"))
        self.assertIsNone(pacifica._pacifica_rate_limit.get("get_cost"))
        self.assertEqual(
            pacifica._pacifica_rate_limit.get("get_cost_source"), "documented_max",
        )
        self.assertEqual(self._wait_need(), Decimal("23"))
        wait = self._wait_seconds(20)
        self.assertGreater(wait, 8.0)

    def test_ambiguous_get_to_get_deltas_do_not_reduce_wait_need(self):
        _record(93, 15, 1.0, "get")
        d7 = _record(86, 14, 1.4, "get")
        d2 = _record(84, 13, 1.7, "get")
        d4 = _record(80, 13, 2.0, "get")
        self.assertEqual(d7["observed_spend"], Decimal("7"))
        self.assertEqual(d2["observed_spend"], Decimal("2"))
        self.assertEqual(d4["observed_spend"], Decimal("4"))
        self.assertIsNone(pacifica._pacifica_rate_limit.get("get_cost"))
        self.assertEqual(self._wait_need(), Decimal("23"))
        self.assertGreater(self._wait_seconds(20), 8.0)

    def test_established_low_get_cost_survives_contaminated_follow_up(self):
        _record(96, 34, 1.0, "get")
        _record(95, 33, 1.3, "get")
        _record(94, 33, 1.6, "get")
        self.assertEqual(pacifica._pacifica_rate_limit["get_cost"], Decimal("1"))
        _record(96, 25, 9.8, "post")
        _record(90, 24, 10.1, "get")
        _record(89, 24, 10.4, "get")
        _record(93, 15, 19.6, "get")
        _record(86, 14, 20.4, "get")
        _record(84, 13, 20.9, "get")
        _record(80, 13, 21.2, "get")
        self.assertEqual(pacifica._pacifica_rate_limit["get_cost"], Decimal("1"))
        self.assertEqual(pacifica._pacifica_rate_limit["get_cost_source"], "observed")
        self.assertEqual(self._wait_need(), Decimal("12"))

    def test_live_30_order_header_sequence_keeps_measured_get_cost(self):
        # Captured 2026-11-20 fibo adaptive 30: remaining credits after ×10 scale.
        sequence = [
            ("post", 96, 34),
            ("get", 95, 33),
            ("get", 94, 33),
            ("post", 96, 25),
            ("get", 90, 24),
            ("get", 89, 24),
            ("post", 87, 15),
            ("get", 85, 15),
            ("get", 93, 15),
            ("get", 86, 14),
            ("get", 84, 13),
            ("get", 80, 13),
        ]
        now = 1.0
        for kind, remaining, t in sequence:
            _record(remaining, t, now, kind)
            now += 0.4
        self.assertEqual(pacifica._pacifica_rate_limit["get_cost"], Decimal("1"))
        self.assertEqual(pacifica._pacifica_rate_limit["get_cost_source"], "observed")
        self.assertEqual(self._wait_need(), Decimal("12"))

    def test_reset_seconds_jump_up_does_not_learn_get_cost(self):
        _record(50, 20, 1.0, "get")
        parsed = _record(49, 40, 1.2, "get")
        self.assertEqual(parsed["observed_spend"], Decimal("1"))
        self.assertIsNone(pacifica._pacifica_rate_limit.get("get_cost"))
        self.assertEqual(self._wait_need(), Decimal("23"))

    def test_sibling_traffic_drop_does_not_learn_get_cost(self):
        _record(90, 30, 1.0, "get")
        parsed = _record(50, 28, 1.2, "get")
        self.assertEqual(parsed["observed_spend"], Decimal("40"))
        self.assertIsNone(pacifica._pacifica_rate_limit.get("get_cost"))
        self.assertEqual(self._wait_need(), Decimal("23"))

    def test_missing_headers_keep_established_get_cost_and_conservative_remaining(self):
        _record(96, 34, 1.0, "get")
        _record(95, 33, 1.3, "get")
        missing = pacifica.record_pacifica_ratelimit_headers({}, now=2.0, kind="get")
        self.assertFalse(missing["reliable"])
        self.assertEqual(pacifica._pacifica_rate_limit["get_cost"], Decimal("1"))
        self.assertIsNone(pacifica._pacifica_rate_limit.get("remaining"))
        self.assertEqual(self._wait_need(), Decimal("12"))
        wait = pacifica.pacifica_batch_credit_wait_seconds(
            action_count=10,
            floor_seconds=8.0,
            adaptive=True,
            remaining=None,
            quota=Decimal("100"),
            header_age_seconds=0.0,
            get_cost=pacifica._pacifica_rate_limit.get("get_cost"),
        )
        self.assertGreaterEqual(wait, 8.0)

    def test_out_of_order_response_does_not_learn_or_replace_get_cost(self):
        _record(96, 34, 10.0, "get")
        _record(95, 33, 10.3, "get")
        ignored = pacifica.record_pacifica_ratelimit_headers(
            _credits(80, t=13), now=9.0, kind="get",
        )
        self.assertTrue(ignored.get("ignored_out_of_order"))
        self.assertEqual(pacifica._pacifica_rate_limit["remaining"], Decimal("95"))
        self.assertEqual(pacifica._pacifica_rate_limit["get_cost"], Decimal("1"))

    def test_documented_max_stays_distinct_from_observed_get_cost(self):
        self.assertEqual(pacifica.PACIFICA_CREDIT_HEAVY_GET, Decimal("12"))
        self.assertEqual(pacifica.PACIFICA_CREDIT_MEASURED_GET, Decimal("1"))
        self.assertEqual(pacifica.pacifica_batch_wait_need(10), Decimal("23"))
        self.assertEqual(
            pacifica.pacifica_batch_wait_need(10, get_cost=pacifica.PACIFICA_CREDIT_MEASURED_GET),
            Decimal("12"),
        )
        self.assertEqual(
            pacifica.pacifica_batch_wait_need(10, get_cost=pacifica.PACIFICA_CREDIT_HEAVY_GET),
            Decimal("23"),
        )


class TestAdaptiveBatchExecute(unittest.TestCase):
    def setUp(self):
        pacifica.reset_pacifica_rate_limit_state()
        self.key = Keypair()
        self.creds = {
            "account": "offline", "address": str(self.key.pubkey()),
            "agent_wallet": str(self.key.pubkey()),
            "agent_private_key": base58.b58encode(bytes(self.key)).decode(),
        }
        self.posts = []
        self.accepted = set()

    def tearDown(self):
        pacifica.reset_pacifica_rate_limit_state()

    def _read(self, url, params=None):
        if url.endswith("/orders/history"):
            return {"data": [], "has_more": False}
        return {"data": [{"client_order_id": cid} for cid in self.accepted]}

    def _post(self, req, timeout=None):
        body = json.loads(req.data)
        self.posts.append(body)
        results = []
        for action in body["actions"]:
            cid = action["data"]["client_order_id"]
            self.accepted.add(cid)
            results.append({"success": True, "order_id": len(self.accepted), "error": None})
        return _Response(
            {"success": True, "data": {"results": results}},
            headers={"ratelimit": '"credits";r=990;t=50', "ratelimit-policy": '"credits";q=1000;w=60'},
        )

    def _run(self, count=20, response=None, **options):
        with mock.patch.object(pacifica, "_lookup_credentials", return_value=self.creds), \
             mock.patch.object(pacifica, "_get_market_info", return_value={"tick_size": "0.01", "lot_size": "0.01"}), \
             mock.patch.object(pacifica, "_http_get_json", side_effect=self._read), \
             mock.patch.object(pacifica.urllib.request, "urlopen", side_effect=response or self._post), \
             mock.patch.object(pacifica.time, "sleep") as sleep:
            result = pacifica.execute({
                "operation": "ladder", "account": "offline", "symbol": "SOL",
                "side": "sell", "distribution": "uniform", "order_count": count,
                "total_volume": str(count), "start_price": "100", "end_price": "101",
                **options,
            })
        return result, sleep

    def test_sequential_remains_default_without_batch_opt_in(self):
        sent = []
        def single(_creds, path, operation, payload):
            sent.append(path)
            self.accepted.add(payload["client_order_id"])
            return {"i": len(sent)}
        with mock.patch.object(pacifica, "_post_signed", side_effect=single):
            result, _ = self._run(count=2, response=AssertionError("batch"), submission_mode=None)
        self.assertEqual(sent, ["/orders/create", "/orders/create"])
        self.assertTrue(result.success)

    def test_adaptive_is_opt_in_and_does_not_undercut_floor(self):
        result, sleep = self._run(count=20, submission_mode="batch", batch_size=10, adaptive_pacing=True)
        self.assertEqual(len(self.posts), 2)
        self.assertTrue(result.success)
        waits = [call.args[0] for call in sleep.call_args_list if call.args]
        self.assertTrue(waits)
        self.assertGreaterEqual(min(waits), pacifica.PACIFICA_LADDER_MIN_SECONDS_PER_ACTION * 10)

    def test_first_429_stops_without_retry_or_later_batches(self):
        def rate_limit(req, timeout=None):
            self.posts.append(json.loads(req.data))
            raise HTTPError(req.full_url, 429, "rate limited", {}, None)
        result, _ = self._run(count=40, response=rate_limit, submission_mode="batch")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(result.ladder.uncertain_child_count, 10)
        self.assertEqual(result.ladder.not_attempted_child_count, 30)
        self.assertTrue(result.ladder.rate_limited)
        self.assertEqual(len({a["data"]["client_order_id"] for a in self.posts[0]["actions"]}), 10)

    def test_incomplete_batch_reconciliation_blocks_next_batch(self):
        def post(req, timeout=None):
            body = json.loads(req.data)
            self.posts.append(body)
            results = []
            for i, action in enumerate(body["actions"]):
                cid = action["data"]["client_order_id"]
                if i == 0:
                    self.accepted.add(cid)
                    results.append({"success": True, "order_id": 1, "error": None})
                else:
                    results.append({"success": True, "order_id": None, "error": None})
            return _Response({"success": True, "data": {"results": results}})
        result, _ = self._run(count=20, response=post, submission_mode="batch", batch_size=10)
        self.assertEqual(len(self.posts), 1)
        self.assertGreaterEqual(result.ladder.uncertain_child_count, 1)
        self.assertEqual(result.ladder.not_attempted_child_count, 10)

    def test_rejected_child_blocks_next_batch(self):
        def post(req, timeout=None):
            body = json.loads(req.data)
            self.posts.append(body)
            results = []
            for i, action in enumerate(body["actions"]):
                cid = action["data"]["client_order_id"]
                if i == 0:
                    results.append({"success": False, "order_id": None, "error": "margin unavailable"})
                else:
                    self.accepted.add(cid)
                    results.append({"success": True, "order_id": i, "error": None})
            return _Response({"success": True, "data": {"results": results}})
        result, _ = self._run(count=20, response=post, submission_mode="batch", batch_size=10)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(result.ladder.rejected_child_count, 1)
        self.assertEqual(result.ladder.not_attempted_child_count, 10)

    def test_cancel_path_unchanged_by_adaptive_helpers(self):
        self.assertTrue(hasattr(pacifica, "_signed_cancel_order"))
        self.assertTrue(hasattr(pacifica, "_execute_cancel_order_group"))

    def test_three_verification_gets_are_recorded_in_local_ledger(self):
        calls = {"n": 0}

        def getter(url, params=None):
            calls["n"] += 1
            return {"data": []}

        with mock.patch.object(pacifica, "_http_get_json", side_effect=getter), \
             mock.patch.object(pacifica.time, "sleep"):
            pacifica._ladder_verify_children({"address": "x"}, ["cid-1"])
        self.assertEqual(calls["n"], 3)
        used = pacifica._pacifica_window_used(__import__("time").monotonic())
        self.assertEqual(used, float(3 * pacifica.PACIFICA_CREDIT_HEAVY_GET))

    def test_batch_post_records_per_create_plus_envelope_not_one_http(self):
        with mock.patch.object(pacifica, "_ladder_verify_children", return_value=(set(self.accepted), [])), \
             mock.patch.object(pacifica, "_ladder_history_by_client_id", return_value={}):
            self._run(count=10, submission_mode="batch", batch_size=10)
        used = pacifica._pacifica_window_used(__import__("time").monotonic())
        self.assertEqual(
            used,
            float(10 * pacifica.PACIFICA_CREDIT_PER_CREATE + pacifica.PACIFICA_CREDIT_BATCH_ENVELOPE),
        )

    def test_simulated_250_pacing_estimates(self):
        estimates = pacifica.simulate_pacifica_ladder_pacing(
            order_count=250, batch_size=10, post_seconds=0.55, verify_seconds=0.01,
        )
        self.assertEqual(estimates["fixed_12s"]["posts"], 25)
        self.assertAlmostEqual(estimates["fixed_12s"]["wall_s"], 24 * 12 + 25 * 0.56, places=1)
        self.assertAlmostEqual(estimates["fixed_8s"]["wall_s"], 24 * 8 + 25 * 0.56, places=1)
        self.assertGreaterEqual(estimates["adaptive"]["wall_s"], estimates["fixed_8s"]["wall_s"])
        self.assertLessEqual(estimates["adaptive"]["wall_s"], estimates["fixed_12s"]["wall_s"])
        self.assertLessEqual(estimates["adaptive"]["peak_create_credits_per_60s"], 80)

    def test_simulated_250_scenarios(self):
        normal = pacifica.simulate_pacifica_ladder_pacing(order_count=250, scenario="normal")
        heavy = pacifica.simulate_pacifica_ladder_pacing(order_count=250, scenario="heavy_get")
        missing = pacifica.simulate_pacifica_ladder_pacing(order_count=250, scenario="missing_header")
        bursty = pacifica.simulate_pacifica_ladder_pacing(order_count=250, scenario="bursty_sibling")
        floor = normal["fixed_8s"]["wall_s"]
        self.assertGreaterEqual(normal["adaptive"]["wall_s"], floor)
        self.assertLessEqual(normal["adaptive"]["wall_s"], normal["fixed_12s"]["wall_s"])
        self.assertGreaterEqual(heavy["adaptive"]["wall_s"], floor)
        self.assertGreaterEqual(missing["adaptive"]["wall_s"], floor)
        self.assertGreaterEqual(bursty["adaptive"]["wall_s"], floor)
        self.assertGreaterEqual(heavy["adaptive"]["wall_s"], normal["adaptive"]["wall_s"])
        self.assertGreaterEqual(bursty["adaptive"]["wall_s"], normal["adaptive"]["wall_s"])


if __name__ == "__main__":
    unittest.main()
