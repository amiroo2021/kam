"""Hyperliquid per-dex failure classification and partial-coverage tests.

Regression coverage for the defect where ``_load_dex`` swallowed every
exception and returned bare empty lists, so a transient timeout / 429 was
reported as a *successful* fetch with zero positions.  The report then rendered
missing exposure as "no positions" instead of "unavailable".

Rules under test:
  * genuine empty account  -> success, zero positions, NOT partial
  * every dex failing      -> make_failure(POSITIONS_ORDERS_UNAVAILABLE)
  * some dexes failing     -> success WITH partial-coverage metadata, keeping
                              the positions that did arrive
  * a partial result is never full coverage
  * multi-dex aggregation and quantities are unchanged when all dexes succeed
"""

from __future__ import annotations

import unittest
import urllib.error
from typing import Any, Dict, List, Optional
from unittest import mock

from plugins.trade import portfolio_report as portfolio
from plugins.trade.agents import x_hyperliquid_agent as agent
from plugins.trade.canonical import CanonicalResponse


def _resp(positions: Optional[List[Any]] = None, data: Optional[Dict[str, Any]] = None) -> CanonicalResponse:
    from plugins.trade.canonical import make_success

    return make_success(
        operation="positions_orders",
        exchange="hyperliquid",
        account="TEST",
        positions=positions or [],
        data=data,
    )


class TestDexErrorClassification(unittest.TestCase):
    def test_rate_limit_is_classified(self) -> None:
        err = urllib.error.HTTPError("u", 429, "Too Many Requests", None, None)
        self.assertEqual(agent._classify_dex_error(err), "rate_limited")

    def test_timeout_is_classified(self) -> None:
        self.assertEqual(agent._classify_dex_error(TimeoutError("timed out")), "timeout")

    def test_other_http_status_is_classified(self) -> None:
        err = urllib.error.HTTPError("u", 503, "Service Unavailable", None, None)
        self.assertEqual(agent._classify_dex_error(err), "http_503")

    def test_network_error_is_classified(self) -> None:
        self.assertEqual(
            agent._classify_dex_error(urllib.error.URLError("refused")), "network"
        )

    def test_unknown_error_is_bucketed(self) -> None:
        self.assertEqual(agent._classify_dex_error(ValueError("weird")), "unknown")

    def test_failure_metadata_has_no_secret_material(self) -> None:
        meta = agent._dex_failure("", RuntimeError("boom"), partial=True)
        self.assertEqual(meta["dex"], "")
        self.assertIn("category", meta)
        self.assertTrue(meta["partial"])
        self.assertNotIn("wallet", meta)
        self.assertNotIn("secret", meta)


def _run(dex_names: List[str], chs_behavior: Dict[str, Any], oo_behavior: Any = None):
    """Drive _execute_positions_orders with stubbed transport."""
    def fake_chs(wallet, dex=""):
        spec = chs_behavior.get(dex, chs_behavior.get("*"))
        if isinstance(spec, Exception):
            raise spec
        return spec or {"assetPositions": []}

    def fake_oo(wallet, dex=""):
        if isinstance(oo_behavior, Exception):
            raise oo_behavior
        return []

    with mock.patch.object(agent, "_fetch_clearinghouse_state", side_effect=fake_chs), \
         mock.patch.object(agent, "_fetch_open_orders_for_dex", side_effect=fake_oo), \
         mock.patch.object(agent, "_discover_perp_dex_names", return_value=list(dex_names)), \
         mock.patch.object(agent, "_fetch_perp_market_candidates", return_value=[]):
        return agent._execute_positions_orders("ZERO", {"dex": dex_names[0] if len(dex_names) == 1 else ""})


def _pos(coin: str, szi: str) -> Dict[str, Any]:
    return {
        "position": {
            "coin": coin,
            "szi": szi,
            "entryPx": "100",
            "positionValue": "100",
            "unrealizedPnl": "0",
            "marginUsed": "50",
            "leverage": {"type": "cross", "value": 3},
            "liqPx": "0",
            "maxLeverage": 20,
            "maxTradeSzs": ["1", "1"],
        }
    }


class TestGenuineEmptyAccount(unittest.TestCase):
    def test_empty_account_is_success_not_partial(self) -> None:
        r = _run([""], {"*": {"assetPositions": []}})
        self.assertTrue(r.success)
        self.assertEqual(r.positions, [])
        self.assertIsNone(r.data, "a genuinely empty account is full coverage")

    def test_empty_account_marks_no_partial_flag(self) -> None:
        r = _run([""], {"*": {"assetPositions": []}})
        self.assertNotIn("partial_coverage", (r.data or {}))


class TestAllDexesFailing(unittest.TestCase):
    def test_single_dex_failure_is_a_failure_not_empty_success(self) -> None:
        r = _run([""], {"*": urllib.error.HTTPError("u", 429, "Too Many Requests", None, None)})
        self.assertFalse(r.success, "429 must not masquerade as an empty account")
        self.assertEqual(r.error.code, "POSITIONS_ORDERS_UNAVAILABLE")
        self.assertIn("unknown, not zero", r.error.message)

    def test_timeout_is_a_failure(self) -> None:
        r = _run([""], {"*": TimeoutError("timed out")})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "POSITIONS_ORDERS_UNAVAILABLE")

    def test_all_multiple_dexes_failing_is_a_failure(self) -> None:
        r = _run(
            ["", "dexA", "dexB"],
            {"*": RuntimeError("network down")},
        )
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "POSITIONS_ORDERS_UNAVAILABLE")
        self.assertEqual((r.data or {}).get("partial_coverage"), False)
        self.assertEqual(len((r.data or {}).get("dex_failures") or []), 3)


class TestPartialCoverage(unittest.TestCase):
    def test_one_dex_failing_keeps_other_positions(self) -> None:
        r = _run(
            ["", "dexA"],
            {"": {"assetPositions": [_pos("HYPE", "-100")]},
             "dexA": urllib.error.HTTPError("u", 429, "Too Many Requests", None, None)},
        )
        self.assertTrue(r.success, "partial data is still a successful read")
        data = r.data or {}
        self.assertTrue(data["partial_coverage"])
        self.assertEqual(data["dexes_failed"], 1)
        self.assertEqual(data["dexes_queried"], ["", "dexA"])
        coins = [p.symbol for p in r.positions]
        self.assertIn("HYPE", coins, "positions from the healthy dex must survive")

    def test_partial_metadata_is_not_full_coverage(self) -> None:
        r = _run(
            ["", "dexA"],
            {"": {"assetPositions": [_pos("HYPE", "-100")]},
             "dexA": TimeoutError("timed out")},
        )
        data = r.data or {}
        self.assertTrue(data["partial_coverage"])
        cats = {f["category"] for f in data["dex_failures"]}
        self.assertEqual(cats, {"timeout"})
        dexes = {f["dex"] for f in data["dex_failures"]}
        self.assertEqual(dexes, {"dexA"})

    def test_open_orders_failure_keeps_positions_and_flags_partial(self) -> None:
        r = _run(
            [""],
            {"*": {"assetPositions": [_pos("BTC", "1")]}},
            oo_behavior=RuntimeError("orders endpoint down"),
        )
        self.assertTrue(r.success)
        self.assertTrue((r.data or {}).get("partial_coverage"))
        self.assertEqual([p.symbol for p in r.positions], ["BTC"])

    def test_all_healthy_dexes_report_no_partial_flag(self) -> None:
        r = _run(
            ["", "dexA"],
            {"": {"assetPositions": [_pos("HYPE", "-100")]},
             "dexA": {"assetPositions": [_pos("BTC", "2")]}},
        )
        self.assertTrue(r.success)
        self.assertIsNone(r.data, "no failures means full coverage")


class TestMultiDexAggregationUnchanged(unittest.TestCase):
    def test_all_healthy_dexes_aggregate_positions(self) -> None:
        r = _run(
            ["", "dexA"],
            {"": {"assetPositions": [_pos("HYPE", "-100")]},
             "dexA": {"assetPositions": [_pos("BTC", "2")]}},
        )
        coins = sorted(p.symbol for p in r.positions)
        self.assertEqual(coins, ["BTC", "HYPE"])

    def test_quantities_unchanged_when_all_succeed(self) -> None:
        r = _run([""], {"*": {"assetPositions": [_pos("HYPE", "-135.73")]}})
        self.assertEqual(len(r.positions), 1)
        self.assertEqual(str(r.positions[0].size).lstrip("-"), "135.73")
        self.assertEqual(r.positions[0].symbol, "HYPE")


class TestPartialPropagatesIntoReport(unittest.TestCase):
    def _account(self, data: Optional[Dict[str, Any]]) -> portfolio.AccountSnapshot:
        return portfolio.AccountSnapshot(
            exchange="hyperliquid",
            account="ZERO",
            account_label="ZERO",
            identity="hyperliquid|ZERO",
            ok=True,
            balance=None,
            balance_unit=None,
            equity=None,
            available=None,
            balance_ok=True,
            positions_ok=True,
            positions_partial=bool(data and data.get("partial_coverage")),
            partial_detail=(
                ["hyperliquid/ZERO: 1 of 2 perp dex(es) unavailable (rate_limited)"]
                if data and data.get("partial_coverage")
                else []
            ),
        )

    def test_partial_account_marks_scope_incomplete(self) -> None:
        acct = self._account({"partial_coverage": True, "dexes_failed": 1})
        snap = portfolio.PortfolioSnapshot(
            fetched_at=0.0, accounts=[acct], positions=[],
            accounts_seen=1, accounts_ok=1, incomplete=acct.positions_partial,
        )
        self.assertTrue(snap.incomplete)

    def test_full_coverage_account_is_not_incomplete(self) -> None:
        acct = self._account(None)
        snap = portfolio.PortfolioSnapshot(
            fetched_at=0.0, accounts=[acct], positions=[],
            accounts_seen=1, accounts_ok=1, incomplete=acct.positions_partial,
        )
        self.assertFalse(snap.incomplete)

    def test_to_dict_exposes_partial_fields(self) -> None:
        d = self._account({"partial_coverage": True}).to_dict()
        self.assertTrue(d["positions_partial"])
        self.assertEqual(len(d["partial_detail"]), 1)


class TestBackwardsCompatibility(unittest.TestCase):
    def test_canonical_success_still_defaults_to_no_data(self) -> None:
        from plugins.trade.canonical import make_success

        r = make_success(
            operation="positions_orders", exchange="x", account="y", positions=[]
        )
        self.assertIsNone(r.data)
        self.assertNotIn("data", r.to_dict())

    def test_canonical_failure_still_defaults_to_no_data(self) -> None:
        from plugins.trade.canonical import make_failure

        r = make_failure(
            operation="positions_orders", exchange="x", account="y",
            code="E", message="m",
        )
        self.assertIsNone(r.data)
        self.assertFalse(r.success)

    def test_failure_data_round_trips(self) -> None:
        from plugins.trade.canonical import make_failure

        r = make_failure(
            operation="positions_orders", exchange="x", account="y",
            code="E", message="m", data={"partial_coverage": False},
        )
        self.assertEqual(r.to_dict()["data"]["partial_coverage"], False)


if __name__ == "__main__":
    unittest.main()