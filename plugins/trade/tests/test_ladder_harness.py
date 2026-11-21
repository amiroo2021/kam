"""Offline tests for the shared Lighter ladder harness primitives.

Covers the two defects found in the Phase 6 run:

  * evidence that must survive a crash (incremental persistence), and
  * cancellation chunk timings that must come from the ASYNC core the
    group runner actually awaits.

No live exchange calls. The cancel-timing test stubs the async core
and the group runner, so nothing is signed or sent.

"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.ladder_harness import (  # noqa: E402
    EvidenceLog,
    ladder_price_band,
    minimum_order_size,
    summarise_chunks,
    time_cancel_chunks,
)


def _lighter_agent():
    """Load the REPO's Lighter agent, by explicit file path.

    Two traps make a plain ``import`` unreliable here:

    1. ``plugins/trade/tests/conftest.py`` drops every
       ``plugins.trade.*`` entry from ``sys.modules`` between test
       modules, so a module-scope import goes stale.
    2. That conftest is a *pytest* conftest. Under
       ``unittest discover`` it never runs, so the editable-install
       finder still maps ``plugins.*`` to the INSTALLED tree under
       ``/usr/local/lib/hermes-agent`` -- an OLDER agent that predates
       the async cancel core and has no
       ``_submit_cancel_tx_batch_async`` at all.

    Loading the file directly from this repository's path sidesteps
    both: the module object always comes from the source under test.
    """
    import importlib.util

    agent_path = _REPO_ROOT / "plugins" / "trade" / "agents" / "x_lighter_agent.py"
    # The agent uses RELATIVE imports ("from ..canonical import ..."), so
    # the module must be loaded inside a package context for them to
    # resolve. Give it the repo's package name.
    module_name = "plugins.trade.agents.x_lighter_agent_repo_under_test"
    spec = importlib.util.spec_from_file_location(module_name, agent_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Make the parent package importable so "from ..canonical" works.
    module.__package__ = "plugins.trade.agents"
    spec.loader.exec_module(module)
    return module


class EvidenceLogTests(unittest.TestCase):
    """Incremental evidence persistence."""

    def test_record_appends_one_json_object_per_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.jsonl"
            with EvidenceLog(path) as log:
                log.record("batch", batch=1, accepted=50)
                log.record("batch", batch=2, accepted=50)
            rows = EvidenceLog.read(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["kind"] for r in rows], ["batch", "batch"])
        self.assertEqual([r["batch"] for r in rows], [1, 2])

    def test_records_are_on_disk_before_the_log_is_closed(self) -> None:
        """The whole point: a crash must not lose completed batches."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.jsonl"
            log = EvidenceLog(path)
            log.record("batch", batch=1)
            # Read the file while the log is still open -- this is what
            # a hard crash would leave behind.
            on_disk = EvidenceLog.read(path)
            log.close()
        self.assertEqual(len(on_disk), 1)
        self.assertEqual(on_disk[0]["batch"], 1)

    def test_header_is_written_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.jsonl"
            with EvidenceLog(path, header={"run": "phase6"}) as log:
                log.record("batch", batch=1)
            rows = EvidenceLog.read(path)
        self.assertEqual(rows[0]["kind"], "header")
        self.assertEqual(rows[0]["run"], "phase6")

    def test_torn_final_line_does_not_lose_earlier_records(self) -> None:
        """A hard kill mid-write leaves a partial line; keep the rest."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.jsonl"
            with EvidenceLog(path) as log:
                log.record("batch", batch=1)
                log.record("batch", batch=2)
            with path.open("a", encoding="utf-8") as fh:
                fh.write('{"kind": "batch", "batch": 3')  # torn write
            rows = EvidenceLog.read(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["batch"] for r in rows], [1, 2])

    def test_read_of_missing_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(EvidenceLog.read(Path(tmp) / "nope.jsonl"), [])


class CancelChunkTimingTests(unittest.TestCase):
    """Per-chunk timings must come from the ASYNC core."""

    def test_timings_are_captured_from_the_async_core(self) -> None:
        """The group runner must AWAIT the patched async core.

        This mirrors the production shape: ``_run_cancel_tx_group``
        awaits ``_submit_cancel_tx_batch_async`` directly. A patch on
        the sync wrapper never fires here, which is exactly the defect
        the Phase 6 run hit.

        The fake core is installed into the MODULE first, so the group
        runner always awaits a stub -- never the real network path.
        """
        import asyncio

        agent = _lighter_agent()

        async def fake_async_core(*, order_ids, **kwargs):
            return {
                "outcome": "success",
                "api_code": 200,
                "submitted_count": len(order_ids),
                "accepted_count": len(order_ids),
                "rejected_count": 0,
                "unknown_count": 0,
            }

        def fake_group_runner(*, credentials, market_id, target_ids):
            # The production runner awaits the async core on its own
            # event loop. Reproduce that shape exactly, against the
            # SAME module object the harness patches.
            async def _run():
                rows = []
                for start in range(0, len(target_ids), 15):
                    chunk = list(target_ids[start:start + 15])
                    outcome = await agent._submit_cancel_tx_batch_async(
                        credentials=credentials,
                        market_id=market_id,
                        order_ids=chunk,
                        signer=object(),
                    )
                    rows.append({"order_ids": chunk, "outcome": outcome.get("outcome")})
                return {"batches": rows}

            return asyncio.run(_run())

        # Install the stub so the group runner never reaches the network.
        saved_stub = agent._submit_cancel_tx_batch_async
        agent._submit_cancel_tx_batch_async = fake_async_core
        try:
            timings: List[Dict[str, Any]] = []
            result = time_cancel_chunks(
                fake_async_core,
                group_runner=fake_group_runner,
                group_kwargs={
                    "credentials": {"api_key_index": 4},
                    "market_id": 3,
                    "target_ids": list(range(250)),
                },
                timings=timings,
                module_hint=agent,
            )
        finally:
            agent._submit_cancel_tx_batch_async = saved_stub

        self.assertIn("batches", result)
        # 250 targets in chunks of 15 -> 17 chunks, each timed.
        self.assertEqual(len(timings), 17)
        self.assertEqual(timings[0]["chunk_size"], 15)
        self.assertEqual(timings[-1]["chunk_size"], 10)
        for t in timings:
            self.assertIsInstance(t["elapsed_ms"], float)
            self.assertGreaterEqual(t["elapsed_ms"], 0.0)
        self.assertEqual(timings[0]["outcome"], "success")

    def test_missing_timings_raise_instead_of_reporting_nothing(self) -> None:
        """A silent empty timing list is the Phase 6 defect itself.

        The harness must LOUDLY fail when the group ran chunks but no
        timing was captured -- that means the patch was on the wrong
        seam. Reporting zero chunks would read as "the cancel was
        instant", which is false.
        """
        import asyncio

        agent = _lighter_agent()

        async def fake_async_core(*, order_ids, **kwargs):
            return {"outcome": "success", "submitted_count": len(order_ids)}

        def fake_group_runner(*, target_ids):
            async def _run():
                return {"batches": [{"order_ids": list(target_ids)}]}

            return asyncio.run(_run())

        saved_stub = agent._submit_cancel_tx_batch_async
        agent._submit_cancel_tx_batch_async = fake_async_core
        try:
            # Patch the WRONG seam: the sync wrapper the group runner
            # never calls. No timings can be captured.
            with self.assertRaises(RuntimeError) as ctx:
                time_cancel_chunks(
                    fake_async_core,
                    group_runner=fake_group_runner,
                    group_kwargs={"target_ids": [1, 2, 3]},
                    timings=[],
                    module_hint=agent,
                )
        finally:
            agent._submit_cancel_tx_batch_async = saved_stub
        self.assertIn("chunk timings", str(ctx.exception))

    def test_patch_is_restored_even_when_the_group_raises(self) -> None:
        agent = _lighter_agent()

        async def fake_async_core(*, order_ids, **kwargs):
            return {"outcome": "success"}

        def exploding_group_runner(**kwargs):
            raise RuntimeError("boom")

        saved = agent._submit_cancel_tx_batch_async
        try:
            with self.assertRaises(RuntimeError):
                time_cancel_chunks(
                    fake_async_core,
                    group_runner=exploding_group_runner,
                    group_kwargs={},
                    timings=[],
                )
            self.assertIs(agent._submit_cancel_tx_batch_async, saved)
        finally:
            agent._submit_cancel_tx_batch_async = saved

    def test_summarise_chunks_reports_totals(self) -> None:
        timings = [
            {"chunk_size": 15, "elapsed_ms": 100.0},
            {"chunk_size": 15, "elapsed_ms": 250.0},
            {"chunk_size": 10, "elapsed_ms": 50.0},
        ]
        summary = summarise_chunks(timings)
        self.assertEqual(summary["chunks"], 3)
        self.assertEqual(summary["total_ms"], 400.0)
        self.assertEqual(summary["max_ms"], 250.0)
        self.assertEqual(summary["min_ms"], 50.0)
        self.assertEqual(summary["sizes"], [15, 15, 10])

    def test_summarise_chunks_of_nothing(self) -> None:
        self.assertEqual(summarise_chunks([])["chunks"], 0)


class PriceBandTests(unittest.TestCase):
    """Ladder price bands must be contiguous and land exactly."""

    def test_band_is_contiguous_and_descending(self) -> None:
        band = ladder_price_band(
            top="55.000", bottom="50.100", step="0.100", count=50
        )
        self.assertEqual(len(band), 50)
        self.assertEqual(band[0], "55.000")
        self.assertEqual(band[-1], "50.100")
        from decimal import Decimal
        for a, b in zip(band, band[1:]):
            self.assertEqual(Decimal(a) - Decimal(b), Decimal("0.100"))

    def test_band_that_does_not_fit_raises(self) -> None:
        with self.assertRaises(ValueError):
            ladder_price_band(top="55.000", bottom="50.100", step="0.100", count=51)

    def test_band_not_landing_on_bottom_raises(self) -> None:
        with self.assertRaises(ValueError):
            ladder_price_band(top="55.000", bottom="50.000", step="0.100", count=50)

    def test_non_positive_step_raises(self) -> None:
        with self.assertRaises(ValueError):
            ladder_price_band(top="55.000", bottom="50.100", step="0", count=10)

    def test_inverted_band_raises(self) -> None:
        with self.assertRaises(ValueError):
            ladder_price_band(top="50.000", bottom="55.000", step="0.100", count=10)


class MinimumOrderSizeTests(unittest.TestCase):
    """Size must clear the exchange minimums at the LOWEST band price."""

    def test_quote_minimum_binds_at_the_lowest_price(self) -> None:
        """The minimum size that clears $10 notional at $30.100.

        10 / 30.100 = 0.33223... -> 0.333 (rounded up to 3 decimals).
        The Phase 6 run used 0.400 -- the spec's choice, above this
        floor -- so this helper returns the MINIMUM, not the spec value.
        """
        size = minimum_order_size(
            min_base_amount="0.100",
            min_quote_amount="10.000000",
            lowest_price="30.100",
            size_decimals=3,
        )
        self.assertEqual(size, "0.333")
        from decimal import Decimal
        self.assertGreaterEqual(Decimal(size) * Decimal("30.100"), Decimal("10"))
        # ...and it must not overshoot into a size that clears the
        # minimum by more than one quantum.
        self.assertLess(Decimal(size) - Decimal("0.333"), Decimal("0.001"))

    def test_base_minimum_binds_when_quote_minimum_is_already_met(self) -> None:
        size = minimum_order_size(
            min_base_amount="0.500",
            min_quote_amount="1.000000",
            lowest_price="2.000",
            size_decimals=3,
        )
        self.assertEqual(size, "0.500")

    def test_size_is_rounded_up_to_the_size_decimals(self) -> None:
        size = minimum_order_size(
            min_base_amount="0.100",
            min_quote_amount="10.000000",
            lowest_price="33.000",
            size_decimals=3,
        )
        self.assertEqual(size, "0.304")  # 10 / 33 = 0.30303... -> 0.304
        from decimal import Decimal
        self.assertGreaterEqual(Decimal(size) * Decimal("33.000"), Decimal("10"))

    def test_non_positive_price_raises(self) -> None:
        with self.assertRaises(ValueError):
            minimum_order_size(
                min_base_amount="0.100",
                min_quote_amount="10",
                lowest_price="0",
                size_decimals=3,
            )


if __name__ == "__main__":
    unittest.main()
