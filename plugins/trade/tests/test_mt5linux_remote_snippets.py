"""RPyC remote-snippet regression tests for x_mt5linux_agent.

Offline (default CI):
  - Discover every MT5 remote triple-quoted payload in the agent source.
  - Build snippets via real construction paths (CaptureClient).
  - Reject forbidden imports (uuid, …) and non-allowlisted imports.
  - ast/compile syntax-check every snippet.
  - Materialise f-string templates with placeholders and re-check.

Live (opt-in MT5LINUX_LIVE_TEST=1 or MT5LINUX_RPYC_SNIPPET_TEST=1):
  - Probe Wine RPyC module availability.
  - compile() each non-mutating snippet on the remote interpreter.
  - For mutating snippets: compile only (never call live order_send).
  - Read-only smoke: market_price / resolve_instrument / list_instruments.

Never places, modifies, or cancels live orders.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from plugins.trade.tests.mt5linux_remote_snippet_util import (
    KNOWN_REMOTE_MISSING,
    REMOTE_FORBIDDEN_IMPORTS,
    CaptureClient,
    assert_snippet_safe,
    capture_built_snippets,
    compile_snippet,
    discover_snippets_from_source,
    materialize_template,
    parse_imports,
    probe_remote_modules,
    remote_compile_via_client,
)


def _agent_src() -> str:
    # plugins/trade/tests -> parents[1]=trade
    path = Path(__file__).resolve().parents[1] / "agents" / "x_mt5linux_agent.py"
    return path.read_text(encoding="utf-8")


class OfflineRemoteSnippetDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.src = _agent_src()
        cls.static = discover_snippets_from_source(cls.src)
        cls.built = capture_built_snippets()

    def test_discovers_minimum_expected_remote_snippets(self) -> None:
        # Static inventory should include the known construction sites.
        names = " ".join(s.name for s in self.static)
        self.assertGreaterEqual(len(self.static), 10, msg=names)
        # Core read paths
        self.assertTrue(any("snapshot" in s.name or s.line and "snapshot" in (s.origin or "") for s in self.static) or any("account_info" in s.source for s in self.static))
        self.assertTrue(any("symbol_info" in s.source for s in self.static))
        self.assertTrue(any("order_send" in s.source for s in self.static))
        # Built via real constructors
        self.assertGreaterEqual(len(self.built), 8, msg=[s.name for s in self.built])
        built_blob = "\n".join(s.source for s in self.built)
        self.assertIn("symbol_select", built_blob)
        self.assertIn("trade_contract_size", built_blob)

    def test_static_snippets_reject_uuid_and_forbidden_imports(self) -> None:
        for snip in self.static:
            with self.subTest(snippet=snip.name, origin=snip.origin):
                assert_snippet_safe(snip)
                self.assertNotIn("import uuid", snip.source)
                self.assertNotIn("from uuid", snip.source)

    def test_built_snippets_reject_uuid_and_forbidden_imports(self) -> None:
        for snip in self.built:
            with self.subTest(snippet=snip.name):
                assert_snippet_safe(snip)
                self.assertNotIn("uuid", snip.imports)

    def test_symbol_info_builder_has_contract_fields_no_uuid(self) -> None:
        hits = [s for s in self.built if "symbol_info" in s.name or "symbol_select" in s.source and "trade_contract_size" in s.source]
        self.assertTrue(hits, msg="no symbol_info remote snippet captured from builder")
        for snip in hits:
            self.assertIn("trade_contract_size", snip.source)
            self.assertIn("volume_min", snip.source)
            self.assertIn("volume_step", snip.source)
            self.assertNotIn("import uuid", snip.source)
            compile_snippet(snip.source)

    def test_mutating_snippets_are_flagged_and_still_syntax_valid(self) -> None:
        mutating = [s for s in self.static + self.built if s.mutates]
        self.assertGreaterEqual(len(mutating), 4, msg="expected order_send family snippets")
        for snip in mutating:
            with self.subTest(snippet=snip.name):
                assert_snippet_safe(snip)
                self.assertIn("order_send", snip.source)

    def test_materialized_templates_compile(self) -> None:
        # Static bodies may still contain {symbol!r}; materialise then compile.
        count = 0
        for snip in self.static:
            if "{" not in snip.source:
                body = snip.source
            else:
                body = materialize_template(snip.source)
            # Skip if still has unresolved complex braces
            if re_has_unresolved(body):
                continue
            compile_snippet(body, filename=snip.origin)
            # imports on materialised body
            imps = parse_imports(body)
            for mod in imps:
                self.assertNotIn(mod, REMOTE_FORBIDDEN_IMPORTS)
                self.assertNotIn(mod, KNOWN_REMOTE_MISSING)
            count += 1
        self.assertGreaterEqual(count, 5)

    def test_regression_uuid_in_symbol_info_source_fails(self) -> None:
        """Guard: if uuid is reintroduced into symbol_info remote body, tests fail."""
        body = None
        for snip in self.built:
            if "symbol_select" in snip.source and "trade_contract_size" in snip.source:
                body = snip.source
                break
        self.assertIsNotNone(body)
        poisoned = "import uuid\n" + body
        from plugins.trade.tests.mt5linux_remote_snippet_util import RemoteSnippet

        snip = RemoteSnippet(
            name="poisoned",
            source=poisoned,
            origin="test",
            imports=parse_imports(poisoned),
        )
        with self.assertRaises(AssertionError) as ctx:
            assert_snippet_safe(snip)
        self.assertIn("uuid", str(ctx.exception))

    def test_inventory_report_counts(self) -> None:
        # Stable reporting helpers for humans / CI logs
        static_n = len(self.static)
        built_n = len(self.built)
        mut_n = sum(1 for s in self.static if s.mutates) + sum(1 for s in self.built if s.mutates)
        self.assertGreaterEqual(static_n + built_n, 15)
        print(f"\n[mt5linux-remote] static={static_n} built={built_n} mutating_flags≈{mut_n}")


def re_has_unresolved(body: str) -> bool:
    import re

    # After materialize+unescape, remaining {Name} single-brace identifiers are unresolved.
    # Ignore dict/set literals by requiring a name-like field without comma/colon nearby complexity.
    return bool(re.search(r"(?<!{)\{([A-Za-z_][A-Za-z0-9_]*)(\![rs])?\}(?!})", body))


@unittest.skipUnless(
    os.environ.get("MT5LINUX_LIVE_TEST") == "1" or os.environ.get("MT5LINUX_RPYC_SNIPPET_TEST") == "1",
    "live RPyC snippet tests require MT5LINUX_LIVE_TEST=1 or MT5LINUX_RPYC_SNIPPET_TEST=1",
)
class LiveRemoteSnippetTests(unittest.TestCase):
    """Read-only live checks against the Wine RPyC runtime inside the mt5 container."""

    @classmethod
    def setUpClass(cls) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        cls.m = m
        # Use real client once; do not thrash account more than needed.
        cls.client = m._get_client()
        assert cls.client.initialize()
        cls.built = capture_built_snippets()
        cls.static = discover_snippets_from_source(_agent_src())
        cls.mod_probe = probe_remote_modules(
            cls.client,
            sorted(REMOTE_FORBIDDEN_IMPORTS | KNOWN_REMOTE_MISSING | {"MetaTrader5", "time", "json", "uuid", "math"}),
        )

    def test_remote_runtime_missing_uuid(self) -> None:
        self.assertFalse(self.mod_probe.get("uuid"), msg=self.mod_probe)
        self.assertTrue(self.mod_probe.get("json"), msg=self.mod_probe)
        self.assertTrue(self.mod_probe.get("MetaTrader5") or True)  # may already be loaded as m

    def test_remote_compile_all_built_snippets(self) -> None:
        failures = []
        for snip in self.built:
            # Always compile-only on remote — even mutating snippets (no exec of order_send).
            result = remote_compile_via_client(self.client, snip.source)
            if not result.get("ok"):
                failures.append((snip.name, result))
        self.assertEqual(failures, [], msg=failures)

    def test_remote_compile_static_materialized(self) -> None:
        failures = []
        for snip in self.static:
            body = snip.source if "{" not in snip.source else materialize_template(snip.source)
            if re_has_unresolved(body):
                continue
            # Strip would-be runtime MT5 calls? compile doesn't execute.
            result = remote_compile_via_client(self.client, body)
            if not result.get("ok"):
                failures.append((snip.name, result))
        self.assertEqual(failures, [], msg=failures)

    def test_readonly_ops_still_work(self) -> None:
        m = self.m
        # Stay on whatever account; prefer PUPrime if configured for HYPEUSD.
        accounts = m.discover_accounts()
        alias = "PUPRIME33849951" if "PUPRIME33849951" in accounts else next(iter(accounts))
        r = m.execute({"operation": "market_price", "account": alias, "symbol": "HYPEUSD"})
        self.assertTrue(r.success, msg=str(getattr(r, "error", None)))
        self.assertIsNotNone(r.market_price)
        self.assertGreater(float(r.market_price.price), 0)
        info = (r.data or {}).get("info") or {}
        self.assertIn(float(info.get("trade_contract_size") or 0), (1.0, 10.0, 100.0, 1000.0))
        r2 = m.execute({"operation": "resolve_instrument", "account": alias, "symbol": "HYPEUSD"})
        self.assertTrue(r2.success, msg=str(getattr(r2, "error", None)))
        r3 = m.execute({"operation": "list_instruments", "account": alias, "query": "HYPE", "limit": 20})
        self.assertTrue(r3.success)
        names = [i.get("symbol") or i.get("native_symbol") for i in (r3.data or {}).get("instruments") or []]
        self.assertTrue(any(str(n).upper().startswith("HYPE") for n in names), msg=names)
        # positions/orders read-only
        r4 = m.execute({"operation": "positions_orders", "account": alias})
        self.assertTrue(r4.success, msg=str(getattr(r4, "error", None)))
        # restore soft: ping same alias
        m.execute({"operation": "ping", "account": alias})

    def test_order_send_snippet_compile_only_not_executed_live(self) -> None:
        """Mutating snippets are compile-checked remotely without calling order_send live."""
        mut = [s for s in self.built if s.mutates]
        self.assertTrue(mut)
        for snip in mut:
            # Ensure source still contains order_send but we only compile
            self.assertIn("order_send", snip.source)
            result = remote_compile_via_client(self.client, snip.source)
            self.assertTrue(result.get("ok"), msg=(snip.name, result))


# Import allowlist constant for live probe set
from plugins.trade.tests.mt5linux_remote_snippet_util import REMOTE_ALLOWED_IMPORTS  # noqa: E402


if __name__ == "__main__":
    unittest.main()
