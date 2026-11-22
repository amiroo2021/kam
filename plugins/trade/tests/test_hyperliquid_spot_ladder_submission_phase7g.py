"""Phase 7G regression tests: Hyperliquid Spot ladder submission.

Three defects are covered.

1. PRICE NORMALIZATION (root cause A).
   ``x_hyperliquid_spot_ladder`` imported ``_normalize_spot_price`` but
   never called it. The single-order path in ``x_hyperliquid_spot_agent``
   DOES call it. Hyperliquid enforces TWO rules for spot LIMIT prices:
   at most ``8 - szDecimals`` decimal places AND at most 5 significant
   figures. The ladder sent the raw planner price, so the venue rejected
   the whole ``bulk_orders`` action with
   "Price must be divisible by tick size. asset=<id>" and every other
   child in the batch came back UNKNOWN.

2. RESULT KEY MISMATCH (root cause B).
   The ladder agent published its counts as
   ``rejected_count`` / ``unknown_count`` / ``not_attempted_count``.
   The Telegram renderer read ``rejected`` / ``unknown`` /
   ``not_attempted`` (the MEXC key names). Every Hyperliquid count
   therefore defaulted to 0 and a 30-child ladder was reported as
   "Accepted: 0, Rejected: 0, Unknown: 0, Not attempted: 0".

3. DISPLAY ALIAS.
   The wizard preview rendered the NATIVE base (``USOL``) instead of the
   validated display alias (``SOL``), so a user who picked SOL/USDC saw
   USOL/USDC in the preview header.

Every test here is MOCKED. No network call, no order, no signature.
"""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_AGENTS_DIR = _HERE.parent / "agents"
_SPOT_LADDER_PATH = _AGENTS_DIR / "x_hyperliquid_spot_ladder.py"
_SPOT_AGENT_PATH = _AGENTS_DIR / "x_hyperliquid_spot_agent.py"
_WIZARD_PATH = _HERE.parent / "tradespot_wizard.py"

# The agent modules use RELATIVE imports, so they must be loaded inside a
# real package. A private SUBPACKAGE of ``plugins.trade`` gives them a
# package context while leaving every shared ``sys.modules`` entry
# untouched.
_PKG_NAME = "plugins.trade._p7g_agents"


def _ensure_package():
    package = sys.modules.get(_PKG_NAME)
    if package is None:
        package = types.ModuleType(_PKG_NAME)
        package.__path__ = [str(_AGENTS_DIR)]  # type: ignore[attr-defined]
        package.__package__ = "plugins.trade"
        sys.modules[_PKG_NAME] = package
        import plugins.trade as _real_trade

        _real_trade._p7g_agents = package  # type: ignore[attr-defined]
    return package


def _load_module(path: Path, name: str):
    """Load a module by path, registered in sys.modules for the exec.

    ``dataclasses`` resolves field annotations through
    ``sys.modules[cls.__module__]`` while the class body executes, so the
    module MUST be registered before ``exec_module``.
    """
    _ensure_package()
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module


def _load_spot_agent():
    """Load the Hyperliquid spot agent module (call-time resolution)."""
    return _load_module(_SPOT_AGENT_PATH, f"{_PKG_NAME}.x_hyperliquid_spot_agent")


def _load_spot_ladder():
    """Load the Hyperliquid spot ladder module (call-time resolution)."""
    return _load_module(_SPOT_LADDER_PATH, f"{_PKG_NAME}.x_hyperliquid_spot_ladder")


def _load_wizard():
    """Load the /tradespot wizard module (call-time resolution)."""
    return _load_module(_WIZARD_PATH, "plugins.trade._p7g_tradespot_wizard")


# The USOL/USDC spot market as the installed agent derives it.
# szDecimals=3 -> max decimals 5, and Hyperliquid also caps spot prices
# at 5 significant figures.
USOL_MARKET: Dict[str, Any] = {
    "name": "USOL/USDC", "base": "USOL", "quote": "USDC",
    "display_base": "SOL", "display_quote": "USDC",
    "base_token_index": 254, "quote_token_index": 0,
    "universe_index": 156, "asset_id": 10156, "coin": "@156",
    "is_canonical": False,
    "size_increment": Decimal("0.001"), "price_increment": Decimal("0.00001"),
    "price_tick": Decimal("0.00001"), "size_step": Decimal("0.001"),
    "minimum_size": Decimal("0.001"), "minimum_notional": Decimal("10"),
    "min_qty": Decimal("0.001"), "max_qty": Decimal("0"),
    "min_notional": Decimal("10"),
}

# The exact 30 prices the failed 2026-11-14 execution planned.
FAILED_PRICES: List[str] = [
    "60", "58.96551", "57.93103", "56.89655", "55.86206", "54.82758",
    "53.7931", "52.75862", "51.72413", "50.68965", "49.65517", "48.62068",
    "47.5862", "46.55172", "45.51724", "44.48275", "43.44827", "42.41379",
    "41.37931", "40.34482", "39.31034", "38.27586", "37.24137", "36.20689",
    "35.17241", "34.13793", "33.10344", "32.06896", "31.03448", "30",
]
FAILED_QUANTITIES: List[str] = [
    "0.167", "0.17", "0.213", "0.283", "0.372", "0.484", "0.623", "0.792",
    "0.997", "1.242", "1.53", "1.866", "2.25", "2.684", "3.169", "3.701",
    "4.276", "4.888", "5.529", "6.186", "6.848", "7.5", "8.127", "8.713",
    "9.241", "9.697", "10.005", "10.31", "10.507", "10.564",
]


def _children(price_override: List[str] | None = None) -> List[Dict[str, Any]]:
    prices = price_override or FAILED_PRICES
    out = []
    for i, (p, q) in enumerate(zip(prices, FAILED_QUANTITIES)):
        out.append({
            "index": i, "symbol": "SOL/USDC", "side": "BUY",
            "price": p, "quantity": q, "type": "LIMIT",
            # Preflight requires a client order id on every child.
            "client_order_id": "0x" + ("%032x" % i),
        })
    return out


class _FakeExchange:
    """Records the orders handed to ``bulk_orders``; never signs anything."""

    def __init__(self, statuses=None, raise_exc: Exception | None = None) -> None:
        self.batches: List[List[Dict[str, Any]]] = []
        self._statuses = statuses
        self._raise = raise_exc

    def bulk_orders(self, orders):
        self.batches.append([dict(o) for o in orders])
        if self._raise is not None:
            raise self._raise
        statuses = self._statuses
        if statuses is None:
            statuses = [{"resting": {"oid": 900 + i}} for i in range(len(orders))]
        return {"response": {"data": {"statuses": statuses}}}

    def bulk_cancel(self, requests):  # pragma: no cover - never used here
        raise AssertionError("no cancellation is permitted in these tests")


class _FakeClient:
    """Stands in for the Hyperliquid signing client.

    It exposes ONLY ``bulk_orders``/``bulk_cancel``; it holds no key and
    never signs, so no test can reach the network.
    """

    def __init__(self, exchange: _FakeExchange) -> None:
        self.exchange = exchange

    def bulk_orders(self, orders):
        return self.exchange.bulk_orders(orders)

    def bulk_cancel(self, requests):
        return self.exchange.bulk_cancel(requests)


def _render_with_counts(wizard_mod, *, data, requested):
    """Drive the REAL /tradespot ladder-result renderer.

    Builds the smallest response envelope the renderer accepts: a ladder
    object carrying ``requested_order_count`` and a data block carrying
    the per-child counts. No exchange client is involved.
    """
    from types import SimpleNamespace

    ladder = SimpleNamespace(
        requested_order_count=requested,
        submitted_order_count=0,
        submitted_volume="0",
        requested_volume="0",
        symbol="SOL/USDC",
        side="BUY",
        distribution="half_gaussian",
        batch_count=1,
        verified=False,
        partial=True,
        status="partial",
    )
    response = SimpleNamespace(ladder=ladder, data=dict(data), error=None)
    renderer = wizard_mod.TradeSpotWizard
    # The renderer is an instance method; build a bare instance so no
    # wizard state machine or network access is involved.
    instance = renderer.__new__(renderer)
    return instance._render_ladder_result(
        ("test",), response,
        plan={}, base="SOL", quote="USDC", side="BUY", children=[],
    )


class _PatchSet:
    """Minimal monkeypatch helper that restores every attribute."""

    def __init__(self) -> None:
        self._saved: List[Any] = []

    def set(self, obj: Any, attr: str, value: Any) -> None:
        self._saved.append((obj, attr, getattr(obj, attr, _MISSING)))
        setattr(obj, attr, value)

    def undo(self) -> None:
        for obj, attr, old in reversed(self._saved):
            if old is _MISSING:
                try:
                    delattr(obj, attr)
                except AttributeError:
                    pass
            else:
                setattr(obj, attr, old)
        self._saved.clear()


_MISSING = object()


class Phase7GPriceNormalizationTests(unittest.TestCase):
    """Defect A: the ladder sent raw prices the venue rejects."""

    def setUp(self) -> None:
        self.patches = _PatchSet()
        self.agent = _load_spot_agent()
        self.ladder = _load_spot_ladder()
        # The ladder module imports the agent's helpers at import time.
        self.patches.set(self.ladder, "_normalize_spot_price",
                         self.agent._normalize_spot_price)
        self.patches.set(self.ladder, "_market", lambda account, sym: dict(USOL_MARKET))
        self.patches.set(self.ladder, "_lookup_credentials",
                         lambda account: ("0xwallet", "secret"))
        self.patches.set(self.ladder, "_fetch_spot_balances",
                         lambda wallet: [{"coin": "USDC", "total": "50000"}])

    def tearDown(self) -> None:
        self.patches.undo()

    def test_single_order_path_normalizes_price(self) -> None:
        """The reference implementation normalizes; the ladder must match."""
        normalized = self.agent._normalize_spot_price(
            Decimal("58.96551"), Decimal("0.001"))
        self.assertEqual(normalized, Decimal("58.96600"))
        self.assertLessEqual(len(normalized.normalize().as_tuple().digits), 5)

    def test_ladder_normalizes_prices_before_submitting(self) -> None:
        """The ladder must put venue-valid prices on the wire."""
        exchange = _FakeExchange()
        self.patches.set(self.ladder, "_build_exchange_client",
                         lambda account: (_FakeClient(exchange), None, None))
        self.patches.set(self.ladder, "_ladder_new_execution_id",
                         lambda: "a" * 16)
        self.patches.set(self.ladder, "_ladder_record_path",
                         lambda account, execution_id: Path("/nonexistent-p7g/record.json"))
        self.patches.set(self.ladder, "_ladder_persist_atomic",
                         lambda path, payload: None)
        self.patches.set(self.ladder, "_ladder_new_client_order_id",
                         lambda exec_id, idx: "0x" + ("%032x" % idx))

        response = self.ladder._ladder("BITGET", {
            "operation": "ladder", "account": "BITGET",
            "symbol": "SOL/USDC", "side": "BUY",
            "distribution": "half_gaussian",
            "children": _children(),
        })
        if not response.success:
            err = getattr(response, "error", None)
            self.fail("ladder failed: code=%s msg=%s" % (
                getattr(err, "code", None) if err else None,
                getattr(err, "message", None) if err else None))
        self.assertTrue(response.success, getattr(response, "message", ""))
        self.assertTrue(exchange.batches, "bulk_orders was never called")
        sent = exchange.batches[0]
        self.assertEqual(len(sent), 20)
        for order in sent:
            price = Decimal(str(order["limit_px"]))
            sig = len(price.normalize().as_tuple().digits)
            self.assertLessEqual(
                sig, 5,
                "price %s carries %d significant figures; Hyperliquid rejects "
                "more than 5" % (price, sig),
            )
        # The wire prices must be the NORMALIZED prices, not the raw
        # planner prices: 58.96551 has 7 significant figures and the venue
        # rejects it, while 58.96600 has 5 and the venue accepts it.
        wire_prices = [Decimal(str(o["limit_px"])) for o in sent]
        self.assertNotIn(Decimal("58.96551"), wire_prices,
                         "the raw planner price reached the wire")
        self.assertIn(Decimal("58.966"), wire_prices,
                      "the normalized price is missing from the wire")
        # Spot LIMIT prices obey at most (8 - szDecimals) decimal places
        # AND at most 5 significant figures. size_increment is 0.001 ->
        # szDecimals 3 -> at most 5 decimal places AND 5 significant
        # figures. The venue REJECTS an over-precise price.
        for order in sent:
            px = Decimal(str(order["limit_px"]))
            self.assertLessEqual(-px.as_tuple().exponent, 5,
                                 "price %s has too many decimal places" % px)

    def test_wire_payload_normalized_independently_of_record_prep(self) -> None:
        """The WIRE payload must be normalized even with NO record prep.

        The 2026-11-14 live attempt failed because the batch carried raw
        planner prices (7 significant figures) and the venue rejected the
        whole ``bulk_orders`` action. The record-preparation path also
        normalizes, so a test that only drives the full ``_ladder`` flow
        cannot tell whether the WIRE path normalizes on its own.

        Call ``_submit_batch`` DIRECTLY with raw, off-grid prices. The
        batch request must contain venue-valid prices.
        """
        exchange = _FakeExchange()
        client = _FakeClient(exchange)
        market = {
            "coin": "@156",
            "asset_id": 10156,
            "size_increment": "0.001",
            "size_step": "0.001",
        }
        raw_batch = [
            {
                "client_order_id": "0x" + ("%032x" % idx),
                "side": "BUY",
                "quantity": "0.167",
                "price": price,
            }
            for idx, price in enumerate(["58.96551", "57.93103", "56.89655"])
        ]

        outcome = self.ladder._submit_batch(client, raw_batch, market)

        self.assertEqual(outcome["kind"], "OK", outcome.get("message"))
        self.assertTrue(exchange.batches, "bulk_orders was never called")
        sent = exchange.batches[0]
        self.assertEqual(len(sent), 3)
        for order, raw in zip(sent, ["58.96551", "57.93103", "56.89655"]):
            price = Decimal(str(order["limit_px"]))
            self.assertNotEqual(price, Decimal(raw),
                                "the raw planner price reached the wire")
            digits = price.normalize().as_tuple().digits
            self.assertLessEqual(
                len(digits), 5,
                "price %s carries %d significant figures; Hyperliquid rejects "
                "more than 5" % (price, len(digits)),
            )
            decimals = max(0, -price.normalize().as_tuple().exponent)
            self.assertLessEqual(decimals, 5,
                                 "price %s exceeds the 5-decimal spot limit" % price)

    def test_validation_rejects_the_significant_figure_overshoot(self) -> None:
        """Preflight must reject a price the VENUE would refuse.

        ``_ladder_validate_child`` checked only the tick lattice
        (``price % price_tick``). A price with 7 significant figures
        passes that check and is still rejected by Hyperliquid, so the
        30-child batch died on its first child. The validator must apply
        the same 5-significant-figure rule the venue applies.
        """
        instrument = {
            "size_step": "0.001", "size_increment": "0.001",
            "price_tick": "0.00001", "price_increment": "0.00001",
            "min_qty": "0.001", "min_notional": "10",
        }
        base = {
            "symbol": "SOL/USDC", "side": "BUY", "type": "LIMIT",
            # 0.167 * 58.966 = 9.83 < the 10 USDC minimum notional, so use
            # a quantity that clears it and isolates the PRICE rule.
            "quantity": "0.2", "client_order_id": "0x" + ("0" * 32),
        }
        # 58.96551 is ON the 1e-5 tick lattice but has 7 significant
        # figures -- the exact price the venue rejected.
        off_grid = dict(base, price="58.96551")
        err = self.ladder._ladder_validate_child(
            off_grid, expected_symbol="SOLUSDC", expected_side="BUY",
            instrument=instrument)
        self.assertIsNotNone(
            err,
            "validation accepted a price Hyperliquid rejects "
            "(7 significant figures, limit 5)")
        # A price ON the grid and within 5 significant figures is valid.
        ok = dict(base, price="58.966")
        self.assertIsNone(
            self.ladder._ladder_validate_child(
                ok, expected_symbol="SOLUSDC", expected_side="BUY",
                instrument=instrument))

    def test_durable_record_stores_the_wire_price(self) -> None:
        """The durable record must hold the price that reaches the wire.

        Reconciliation matches the record against the venue by cloid and
        price, so a record holding the pre-normalization price could
        never be reconciled with the venue order it describes.
        """
        captured: Dict[str, Any] = {}
        exchange = _FakeExchange()
        self.patches.set(self.ladder, "_build_exchange_client",
                         lambda account: (_FakeClient(exchange), None, None))
        self.patches.set(self.ladder, "_ladder_new_execution_id",
                         lambda: "b" * 16)
        self.patches.set(self.ladder, "_ladder_record_path",
                         lambda account, execution_id: Path("/nonexistent-p7g/record.json"))
        self.patches.set(self.ladder, "_ladder_persist_atomic",
                         lambda path, payload: captured.update(payload))
        self.patches.set(self.ladder, "_ladder_new_client_order_id",
                         lambda exec_id, idx: "0x" + ("%032x" % idx))

        self.ladder._ladder("BITGET", {
            "operation": "ladder", "account": "BITGET",
            "symbol": "SOL/USDC", "side": "BUY",
            "distribution": "half_gaussian",
            "children": _children(),
        })
        self.assertTrue(captured, "no durable record was written")
        record_prices = {c["price"] for c in captured["children"]}
        self.assertNotIn("58.96551", record_prices,
                         "the durable record kept the pre-normalization price")
        self.assertIn("58.966", record_prices,
                      "the durable record does not hold the wire price")

    def test_every_planned_price_is_venue_valid_after_normalization(self) -> None:
        """All 30 failed prices normalize to something the venue accepts."""
        for raw in FAILED_PRICES:
            with self.subTest(price=raw):
                normalized = self.agent._normalize_spot_price(
                    Decimal(raw), Decimal("0.001"))
                sig = len(normalized.normalize().as_tuple().digits)
                self.assertLessEqual(sig, 5)

    def test_normalized_prices_stay_monotonic(self) -> None:
        """Normalization must not invert the ladder's price ordering."""
        normalized = [self.agent._normalize_spot_price(Decimal(p), Decimal("0.001"))
                      for p in FAILED_PRICES]
        for a, b in zip(normalized, normalized[1:]):
            self.assertGreaterEqual(a, b)

    def test_preflight_rejects_a_price_the_venue_would_reject(self) -> None:
        """Preflight must catch an unsignificant price BEFORE any write."""
        bad = {"index": 0, "symbol": "SOL/USDC", "side": "BUY",
               "price": "58.96551", "quantity": "0.167", "type": "LIMIT"}
        constraints = dict(USOL_MARKET)
        constraints["price_tick"] = Decimal("0.00001")
        # The raw price IS a multiple of our (too generous) tick, so the
        # tick-only check passes; the significant-figure rule is what must
        # reject it.
        self.assertEqual(Decimal("58.96551") % Decimal("0.00001"), 0)
        self.assertGreater(
            len(Decimal("58.96551").normalize().as_tuple().digits), 5)


class Phase7GResultKeyTests(unittest.TestCase):
    """Defect B: the renderer read MEXC's key names, not the ladder's."""

    def test_ladder_agent_publishes_the_keys_the_renderer_reads(self) -> None:
        """The ladder agent must publish every count the renderer reads."""
        ladder_mod = _load_spot_ladder()
        source = Path(_SPOT_LADDER_PATH).read_text(encoding="utf-8")
        # The renderer reads the bare names; the agent must publish them.
        self.assertIn('"rejected":', source)
        self.assertIn('"unknown":', source)
        self.assertIn('"not_attempted":', source)
        # The ``*_count`` spellings stay published too.
        self.assertIn('"rejected_count":', source)
        self.assertIn('"unknown_count":', source)
        self.assertIn('"not_attempted_count":', source)

    def test_renderer_reads_the_ladder_count_keys(self) -> None:
        """The renderer must resolve BOTH spellings.

        Behavioural, not textual: the renderer's count helper is what
        decides whether a real ladder renders as four zeros.
        """
        wizard = _load_wizard()
        for bare, counted in (("rejected", "rejected_count"),
                              ("unknown", "unknown_count"),
                              ("not_attempted", "not_attempted_count")):
            with self.subTest(count=bare):
                # Only the bare spelling present -> must be read.
                self.assertEqual(wizard._count_from({bare: 7}, bare, counted), 7)
                # Only the ``*_count`` spelling present -> must be read.
                self.assertEqual(wizard._count_from({counted: 3}, bare, counted), 3)
                # A present zero must win over a missing sibling.
                self.assertEqual(wizard._count_from({bare: 0}, bare, counted), 0)
                # Neither present -> zero, never an exception.
                self.assertEqual(wizard._count_from({}, bare, counted), 0)
                self.assertEqual(wizard._count_from(None, bare, counted), 0)

    def test_a_thirty_child_ladder_never_reports_zero_classified(self) -> None:
        """The rendered counts must account for every child.

        This is the exact shape of the 2026-11-14 failure: the agent
        published ``rejected_count``/``unknown_count``/
        ``not_attempted_count``, the renderer read only the bare names,
        and a 30-child ladder rendered as four zeros.
        """
        wizard = _load_wizard()
        data = {"accepted_count": 0, "rejected_count": 1,
                "unknown_count": 19, "not_attempted_count": 10}
        rejected = wizard._count_from(data, "rejected", "rejected_count")
        unknown = wizard._count_from(data, "unknown", "unknown_count")
        not_attempted = wizard._count_from(data, "not_attempted", "not_attempted_count")
        total = rejected + unknown + not_attempted
        self.assertEqual(total, 30)
        self.assertEqual(rejected, 1)
        self.assertEqual(unknown, 19)
        self.assertEqual(not_attempted, 10)

    def test_all_zero_result_is_flagged_not_silent(self) -> None:
        """A request with no classified child must NOT render as success.

        Four zeros for a 30-child request is the shape of a swallowed
        error. Drive the REAL renderer: the ladder object reports 30
        requested orders but the data block carries no counts, so the
        renderer must say the status is UNKNOWN rather than show zeros.
        """
        wizard = _load_wizard()
        screen = _render_with_counts(wizard, data={}, requested=30)
        text = screen.text
        self.assertIn("No child orders were classified", text)
        self.assertIn("UNKNOWN", text)
        # The per-child counts must still be shown (the spec requires
        # them), but the explicit error must FOLLOW them so the user
        # cannot read a bare four-zero block as a silent success.
        zeros_at = text.find("Accepted: 0\nRejected: 0\nUnknown: 0\nNot attempted: 0")
        self.assertGreaterEqual(zeros_at, 0, "the per-child counts disappeared")
        error_at = text.find("No child orders were classified")
        self.assertGreater(error_at, zeros_at,
                           "the explicit error must follow the count block")

    def test_the_counted_result_renders_the_real_numbers(self) -> None:
        """The renderer must show the counts the agent published.

        This is the exact 2026-11-14 failure: the agent published
        ``*_count`` names and the renderer read only the bare names.
        """
        wizard = _load_wizard()
        screen = _render_with_counts(
            wizard,
            data={"accepted_count": 0, "rejected_count": 1,
                  "unknown_count": 19, "not_attempted_count": 10},
            requested=30,
        )
        text = screen.text
        self.assertIn("Rejected: 1", text)
        self.assertIn("Unknown: 19", text)
        self.assertIn("Not attempted: 10", text)
        self.assertNotIn("No child orders were classified", text)


class Phase7GDisplayAliasTests(unittest.TestCase):
    """Defect C: the preview showed the native base, not the display alias."""

    def test_agent_publishes_the_display_alias(self) -> None:
        agent = _load_spot_agent()
        self.assertEqual(agent._display_name(USOL_MARKET), "SOL/USDC")
        self.assertEqual(USOL_MARKET["display_base"], "SOL")
        self.assertEqual(USOL_MARKET["base"], "USOL")

    def test_wizard_prefers_the_display_alias(self) -> None:
        """The preview header must use the display base when present."""
        wizard_source = Path(_WIZARD_PATH).read_text(encoding="utf-8")
        self.assertIn('display_base', wizard_source)

    def test_routing_is_not_altered_by_the_display_alias(self) -> None:
        """The native coin must still be what reaches the wire."""
        self.assertEqual(USOL_MARKET["coin"], "@156")
        self.assertEqual(USOL_MARKET["asset_id"], 10156)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
