"""Deterministic tests for the /trade Phase 1 architecture.

These tests verify the architectural invariants without making any
live network calls. The 12 required scenarios are:

1.  x_hyperliquid_agent.py is dynamically discovered as ``hyperliquid``.
2.  A fake ``x_example_agent.py`` is discoverable without any logic
    change to TradeDesk.
3.  Non-matching ``.py`` files in the agents directory are not treated
    as exchanges.
4.  The Telegram wizard's exchange screen obtains exchanges from
    TradeDesk.
5.  The account screen obtains accounts via TradeDesk -> exchange agent.
6.  The wizard does not inspect environment variables for accounts.
7.  The balance request uses the canonical request structure.
8.  Canonical balance formatting produces exactly 2 decimal places.
9.  The unit is preserved rather than converted.
10. Canonical errors render safely (no exception leakage).
11. No secrets appear in error output.
12. Existing non-/trade Telegram behavior is unaffected (the slash
    command dispatch only intercepts the ``trade`` command; the
    adapter's existing filters are untouched).

Run with::

    python3 -m pytest plugins/trade/tests/test_phase1.py -v
    # or, if pytest is unavailable:
    python3 plugins/trade/tests/test_phase1.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock


# ---------------------------------------------------------------------------
# Test environment
# ---------------------------------------------------------------------------

# Ensure the plugin directory is importable when run as a script.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent  # /usr/local/lib/hermes-agent

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Strip live HYPERLIQUID_* env vars so account-discovery tests
# against the real agent get a deterministic baseline, then restore on
# teardown.
_PRESERVED_ENV: Dict[str, str] = {}
for _k in list(os.environ.keys()):
    if _k.startswith("HYPERLIQUID_"):
        _PRESERVED_ENV[_k] = os.environ[_k]
        os.environ.pop(_k, None)


def _restore_env():
    for k in list(os.environ.keys()):
        if k.startswith("HYPERLIQUID_") and k not in _PRESERVED_ENV:
            os.environ.pop(k, None)
    for k, v in _PRESERVED_ENV.items():
        os.environ[k] = v


import atexit
atexit.register(_restore_env)


# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from plugins.trade import canonical, tradedesk, wizard  # noqa: E402
from plugins.trade.canonical import (  # noqa: E402
    GENERIC_ACTIONS,
    make_failure,
    make_success,
    normalize_balance,
    sanitize_error_message,
)
from plugins.trade.tradedesk import (  # noqa: E402
    TradeDesk,
    _iter_agent_files,
    _AGENT_FILENAME_PATTERN,
)
from plugins.trade.wizard import TradeWizard  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_agent_file(name: str, accounts: List[str], balance_value: str = "0.00") -> Path:
    """Create a fake x_<name>_agent.py file in a temp directory.

    Returns the path to the created agent file. The agent module
    exposes the canonical contract: name, list_accounts, capabilities,
    execute.
    """
    body = (
        'name = "' + name + '"\n'
        '\n'
        'def list_accounts():\n'
        '    return ' + repr(accounts) + '\n'
        '\n'
        'def capabilities():\n'
        '    return ["balance"]\n'
        '\n'
        'def execute(request):\n'
        '    from plugins.trade.canonical import make_success, make_failure, normalize_balance\n'
        '    if request.get("operation") == "balance":\n'
        '        return make_success(\n'
        '            operation="balance",\n'
        '            exchange="' + name + '",\n'
        '            account=request.get("account", ""),\n'
        '            balance=normalize_balance("' + balance_value + '", "USDC"),\n'
        '        )\n'
        '    return make_failure(\n'
        '        operation=request.get("operation", ""),\n'
        '        exchange="' + name + '",\n'
        '        account=request.get("account", ""),\n'
        '        code="NOT_IMPLEMENTED",\n'
        '        message="Not implemented yet.",\n'
        '    )\n'
    )
    tmpdir = Path(tempfile.mkdtemp(prefix="trade_test_"))
    file_path = tmpdir / ("x_" + name + "_agent.py")
    file_path.write_text(body)
    return file_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAgentDiscovery(unittest.TestCase):
    """Tests 1, 2, 3: dynamic agent discovery."""

    def test_1_hyperliquid_discovered(self):
        """x_hyperliquid_agent.py is dynamically discovered as 'hyperliquid'."""
        desk = TradeDesk()
        exchanges = desk.list_exchanges()
        self.assertIn("hyperliquid", exchanges)
        agent = desk._agents.get("hyperliquid")
        self.assertIsNotNone(agent)
        self.assertEqual(agent.name, "hyperliquid")
        for attr in ("name", "list_accounts", "capabilities", "execute"):
            self.assertTrue(hasattr(agent, attr), f"agent missing {attr}")

    def test_2_fake_agent_discoverable_without_logic_change(self):
        """A fake x_example_agent.py is discovered without any change
        to TradeDesk logic — proves the discovery is filename-driven."""
        with tempfile.TemporaryDirectory(prefix="trade_test_") as tmp:
            agents_dir = Path(tmp) / "agents"
            agents_dir.mkdir()
            fake_file = _make_fake_agent_file("example", ["alpha", "beta"], "42.5")
            (agents_dir / "x_example_agent.py").write_text(fake_file.read_text())
            desk = TradeDesk()
            with mock.patch.object(tradedesk, "_agents_dir", return_value=agents_dir):
                desk._agents = {}
                desk._loaded = False
                exchanges = desk.list_exchanges()
            self.assertIn("example", exchanges)
            self.assertEqual(
                sorted(desk.list_accounts("example")),
                ["alpha", "beta"],
            )

    def test_3_nonmatching_files_not_treated_as_exchanges(self):
        """Non-agent files in the agents directory are silently ignored."""
        with tempfile.TemporaryDirectory(prefix="trade_test_") as tmp:
            agents_dir = Path(tmp) / "agents"
            agents_dir.mkdir()
            (agents_dir / "__init__.py").write_text("")
            (agents_dir / "conftest.py").write_text("")
            (agents_dir / "utils.py").write_text("")
            (agents_dir / "test_x_hyperliquid_agent.py").write_text("")
            (agents_dir / "_private.py").write_text("")
            (agents_dir / "x_no_suffix.py").write_text("")
            (agents_dir / "x_hyperliquid.py").write_text("")
            (agents_dir / "x_provider_agent.txt").write_text("")
            (agents_dir / "x_42_invalid_agent.py").write_text("")

            for path in _iter_agent_files(agents_dir):
                self.assertRegex(
                    path.name,
                    _AGENT_FILENAME_PATTERN.pattern,
                    f"{path.name} passed _iter_agent_files but shouldn't have",
                )
            self.assertEqual(
                [p.name for p in _iter_agent_files(agents_dir)],
                [],
            )


class TestWizardExchangeScreen(unittest.TestCase):
    """Test 4: the exchange screen renders from TradeDesk."""

    def test_4_exchange_screen_uses_tradedesk(self):
        """The wizard's first screen obtains exchanges from TradeDesk."""
        with tempfile.TemporaryDirectory(prefix="trade_test_") as tmp:
            agents_dir = Path(tmp) / "agents"
            agents_dir.mkdir()
            fake_file = _make_fake_agent_file("example", ["alpha"], "0.00")
            (agents_dir / "x_example_agent.py").write_text(fake_file.read_text())
            desk = TradeDesk()
            with mock.patch.object(tradedesk, "_agents_dir", return_value=agents_dir):
                w = TradeWizard(tradedesk=desk)
                screen = w.open(("chat_test",))
                flat_buttons = [b for row in screen.buttons for b in row]
                texts = [b["text"] for b in flat_buttons]
                self.assertIn("example", texts)
                self.assertEqual(screen.text, "Trade\n\nSelect Exchange:")


class TestWizardAccountScreen(unittest.TestCase):
    """Test 5: the account screen pulls from TradeDesk -> agent."""

    def test_5_account_screen_pulls_via_tradedesk(self):
        """Accounts on the account screen come from the agent via TradeDesk."""
        with tempfile.TemporaryDirectory(prefix="trade_test_") as tmp:
            agents_dir = Path(tmp) / "agents"
            agents_dir.mkdir()
            fake_file = _make_fake_agent_file("example", ["alpha", "beta"], "100")
            (agents_dir / "x_example_agent.py").write_text(fake_file.read_text())
            desk = TradeDesk()
            with mock.patch.object(tradedesk, "_agents_dir", return_value=agents_dir):
                w = TradeWizard(tradedesk=desk)
                w.open(("chat_test",))
                screen = w.handle_callback(("chat_test",), "exchange:example")
                flat_buttons = [b for row in screen.buttons for b in row]
                texts = [b["text"] for b in flat_buttons]
                self.assertIn("alpha", texts)
                self.assertIn("beta", texts)
                self.assertIn("Exchange: example", screen.text)


class TestWizardNoEnvInspection(unittest.TestCase):
    """Test 6: wizard never inspects environment variables."""

    def test_6_wizard_does_not_inspect_env(self):
        """The wizard source contains no os.environ / os.getenv access."""
        wiz_path = Path(wizard.__file__)
        text = wiz_path.read_text(encoding="utf-8")
        self.assertNotIn("os.environ", text)
        self.assertNotIn("os.getenv", text)
        self.assertNotIn("HYPERLIQUID_", text)
        td_path = Path(tradedesk.__file__)
        td_text = td_path.read_text(encoding="utf-8")
        self.assertNotIn("HYPERLIQUID_", td_text)


class TestBalanceRequestStructure(unittest.TestCase):
    """Test 7: balance request uses the canonical structure."""

    def test_7_balance_request_shape(self):
        """A balance request is exactly the canonical structure."""
        captured: Dict[str, Any] = {}

        class CapturingAgent:
            name = "capture"

            def list_accounts(self):
                return ["x"]

            def capabilities(self):
                return ["balance"]

            def execute(self, request):
                captured["request"] = dict(request)
                return make_success(
                    operation="balance",
                    exchange="capture",
                    account=request.get("account", ""),
                    balance=normalize_balance("1.00", "USDC"),
                )

        desk = TradeDesk()
        desk._agents = {"capture": CapturingAgent()}
        desk._loaded = True
        w = TradeWizard(tradedesk=desk)
        w.open(("chat",))
        w.handle_callback(("chat",), "exchange:capture")
        w.handle_callback(("chat",), "account:x")
        w.handle_callback(("chat",), "action:balance")
        self.assertEqual(
            captured["request"],
            {
                "operation": "balance",
                "exchange": "capture",
                "account": "x",
            },
        )


class TestCanonicalBalanceFormatting(unittest.TestCase):
    """Tests 8, 9: canonical balance formatting rules."""

    def test_8_two_decimal_places(self):
        """normalize_balance always produces exactly 2 decimal places."""
        cases = [
            ("567.3335", "567.33"),
            ("43.324", "43.32"),
            ("100", "100.00"),
            ("0.005", "0.01"),
            ("0.004", "0.00"),
            (Decimal("16233.4"), "16233.40"),
            (16233.40, "16233.40"),
            ("0.0", "0.00"),
            ("-12.345", "-12.35"),
        ]
        for raw, expected in cases:
            b = normalize_balance(raw, "USDC")
            self.assertEqual(b.value, expected, f"raw={raw!r} got={b.value!r}")
            int_part, _, frac_part = b.value.partition(".")
            self.assertEqual(len(frac_part), 2, f"value={b.value!r} has != 2 decimals")

    def test_9_unit_preserved(self):
        """The unit is preserved verbatim — no implicit conversion."""
        for unit in ["USDC", "USDT", "USD", "USDG", "ETH", "BTC"]:
            b = normalize_balance("1.00", unit)
            self.assertEqual(b.unit, unit)
        rendered = (
            "Balance\n\n"
            "Exchange: x\n"
            "Account: y\n\n"
            f"Balance: {normalize_balance(43.324, 'USDT').value} "
            f"{normalize_balance(43.324, 'USDT').unit}\n"
        )
        self.assertIn("43.32 USDT", rendered)
        self.assertNotIn("USDC", rendered)


class TestErrorSafety(unittest.TestCase):
    """Tests 10, 11: canonical errors render safely, no secrets leak."""

    def test_10_failure_envelope_shape(self):
        """A failure envelope has the canonical shape and a sanitized message."""
        response = make_failure(
            operation="balance",
            exchange="hyperliquid",
            account="fibo",
            code="BALANCE_UNAVAILABLE",
            message="Connection refused",
        )
        d = response.to_dict()
        self.assertEqual(
            d,
            {
                "success": False,
                "operation": "balance",
                "exchange": "hyperliquid",
                "account": "fibo",
                "error": {
                    "code": "BALANCE_UNAVAILABLE",
                    "message": "Connection refused",
                },
            },
        )

    def test_10b_failure_envelope_includes_exchange_reason(self):
        """A failure envelope can carry a sanitized exchange reason."""
        response = make_failure(
            operation="ladder",
            exchange="hyperliquid",
            account="fibo",
            code="EXCHANGE_REJECTED",
            message="Hyperliquid rejected the ladder.",
            exchange_reason="Insufficient margin for order placement.",
        )
        d = response.to_dict()
        self.assertEqual(d["error"]["exchange_reason"], "Insufficient margin for order placement.")
        self.assertIsNotNone(response.error)
        self.assertEqual(response.error.exchange_reason, "Insufficient margin for order placement.")

    def test_11_no_secrets_in_error_message(self):
        """sanitize_error_message redacts credential VALUES while preserving diagnostic context.

        Wallet addresses, orderIds, hashes, timestamps, and generic URLs are
        NOT secrets and must remain visible. Wholesale 'Error message suppressed'
        is NEVER triggered by these tokens. Only adjacent credential-shaped
        values are redacted.
        """
        # Cases that must REMAIN visible (wallet / URL / context words).
        keep_cases = [
            "HTTP 401 with wallet 0xd230724148476cf0f1e8bbcfa90e305a9ad670aa",
            "Address '0xd230724148476cf0f1e8bbcfa90e305a9ad670aa' not found",
            "INVALID_SIGNATURE: Invalid signature for account",
            "SIGNATURE_MISMATCH: signature verification failed",
            "ORDER_NOT_FOUND: orderId 0xdeadbeef not found",
            "signature_payload={\"ad\":\"0xd230724148476cf0f1e8bbcfa90e305a9ad670aa\",\"ai\":0,\"c\":\"client\",\"ct\":1785494168557347248}",
            "orderId 6cb5c8036da942a2",
            "0xdeadbeef",
            "timestamp 1785494168557347248",
            "I made a signature on the document.",
            "POST https://api.hyperliquid.xyz/info failed",
        ]
        for msg in keep_cases:
            sanitized = sanitize_error_message(msg)
            self.assertEqual(
                sanitized, msg, f"unexpectedly redacted: {msg!r} -> {sanitized!r}"
            )

        # The wholesale suppression boilerplate must NEVER appear.
        suppress_boilerplate = "Error message suppressed: potentially sensitive content."
        for msg in keep_cases:
            self.assertNotEqual(sanitize_error_message(msg), suppress_boilerplate)

        # Real credentials must still be redacted.
        secret_a_64 = "a" * 64
        secret_a_128 = "a" * 128
        secret_b_128 = "b" * 128
        secret_c_32 = "c" * 32
        redact_cases = [
            ("X-API-Key: " + secret_a_64, "X-API-Key: [REDACTED_API_KEY]"),
            ("X-Signature: " + secret_a_128, "X-Signature: [REDACTED_SIGNATURE]"),
            ('{"X-API-Key": "' + secret_a_64 + '", "X-Signature": "' + secret_b_128 + '"}',
             '{"X-API-Key": "[REDACTED_API_KEY]", "X-Signature": "[REDACTED_SIGNATURE]"}'),
            ("Authorization: Bearer " + secret_c_32, "Authorization: Bearer [REDACTED_BEARER]"),
            ("?token=" + secret_c_32 + "&symbol=SOL-USD", "?token=[REDACTED]&symbol=SOL-USD"),
            ("ARCUS_AMIROO_APISIGNINGKEY=" + secret_a_64, "ARCUS_AMIROO_APISIGNINGKEY=[REDACTED]"),
            ("ed25519:" + secret_c_32, "ed25519:[REDACTED_ED25519_SEED]"),
            ("signature: " + secret_a_64, "signature: [REDACTED_SIGNATURE]"),
            ("api_key: " + secret_a_64, "api_key: [REDACTED_API_KEY]"),
            ("private_key: " + secret_a_64, "private_key: [REDACTED_PRIVATE_KEY]"),
        ]
        for msg, expected in redact_cases:
            sanitized = sanitize_error_message(msg)
            self.assertEqual(
                sanitized, expected, f"bad redact: {msg!r} -> {sanitized!r}"
            )

        # Mixed: useful error text preserved, signature value redacted.
        mixed = (
            "Arcus returned 401: {\"code\":\"INVALID_SIGNATURE\",\"error\":\"signature verification failed for request signer: "
            + secret_a_64 + "\"}"
        )
        mixed_expected = (
            "Arcus returned 401: {\"code\":\"INVALID_SIGNATURE\",\"error\":\"signature verification failed for request signer: [REDACTED]\"}"
        )
        self.assertEqual(sanitize_error_message(mixed), mixed_expected)

        # Empty / None must normalize to the unknown indicator.
        self.assertEqual(sanitize_error_message(""), "Unknown error")
        self.assertEqual(sanitize_error_message(None), "Unknown error")  # type: ignore[arg-type]

    def test_11c_sanitize_error_message_is_idempotent_and_handles_multiple_credentials(self):
        """Idempotency + multiple credentials in one message.

        1. A message that already had its credential redacted must remain
           unchanged on a second pass (no further mutation).
        2. A single message containing multiple different credentials must
           have each credential value redacted exactly once.
        """
        # Idempotency: re-running on an already-redacted message is a no-op.
        already_redacted_cases = [
            "X-API-Key: [REDACTED_API_KEY]",
            "X-Signature: [REDACTED_SIGNATURE]",
            "Authorization: Bearer [REDACTED_BEARER]",
            "ed25519:[REDACTED_ED25519_SEED]",
            "signature: [REDACTED_SIGNATURE]",
            "api_key: [REDACTED_API_KEY]",
            "private_key: [REDACTED_PRIVATE_KEY]",
            "ARCUS_AMIROO_APISIGNINGKEY=[REDACTED]",
            "X-API-Key: [REDACTED_API_KEY], X-Signature: [REDACTED_SIGNATURE]",
            "?token=[REDACTED]&symbol=SOL-USD",
        ]
        for msg in already_redacted_cases:
            once = sanitize_error_message(msg)
            twice = sanitize_error_message(once)
            self.assertEqual(once, msg, f"first pass mutated: {msg!r} -> {once!r}")
            self.assertEqual(twice, msg, f"second pass mutated: {once!r} -> {twice!r}")

        # Multiple credentials in one message: each value redacted; surrounding
        # context preserved.
        secret_a_64 = "a" * 64
        secret_a_128 = "a" * 128
        secret_b_64 = "b" * 64
        secret_c_32 = "c" * 32
        multi = (
            "header dump: X-API-Key: " + secret_a_64 + " "
            "X-Signature: " + secret_a_128 + " "
            "Authorization: Bearer " + secret_c_32 + " "
            "url: ?token=" + secret_c_32 + "&symbol=SOL-USD "
            "ARCUS_AMIROO_APISIGNINGKEY=" + secret_b_64 + " "
            "ed25519:" + secret_c_32
        )
        multi_expected = (
            "header dump: X-API-Key: [REDACTED_API_KEY] "
            "X-Signature: [REDACTED_SIGNATURE] "
            "Authorization: Bearer [REDACTED_BEARER] "
            "url: ?token=[REDACTED]&symbol=SOL-USD "
            "ARCUS_AMIROO_APISIGNINGKEY=[REDACTED] "
            "ed25519:[REDACTED_ED25519_SEED]"
        )
        out = sanitize_error_message(multi)
        self.assertEqual(out, multi_expected)
        # And it must be idempotent on the redacted output.
        self.assertEqual(sanitize_error_message(out), out)

        # Sanity: a 64-char hex string that is NOT adjacent to a credential name
        # must remain visible (e.g. a bare tx hash).
        bare_tx = "fee tx-hash 0x" + "b" * 64
        self.assertEqual(sanitize_error_message(bare_tx), bare_tx)

        # URL query parameters: ?signature=, ?token=, ?apikey= etc. must be
        # redacted (with the surrounding context preserved).
        for msg, expected in [
            ("?signature=abc&symbol=SOL", "?signature=[REDACTED]&symbol=SOL"),
            ("?Signature=abc&symbol=SOL", "?Signature=[REDACTED]&symbol=SOL"),
            ("?SIGNATURE=abc&symbol=SOL", "?SIGNATURE=[REDACTED]&symbol=SOL"),
            ("?sig=abc&symbol=SOL", "?sig=[REDACTED]&symbol=SOL"),
            ("?token=abc&symbol=SOL-USD", "?token=[REDACTED]&symbol=SOL-USD"),
            ("?apikey=xxx&sig=yyy", "?apikey=[REDACTED]&sig=[REDACTED]"),
            ("?access_token=zzz&symbol=SOL", "?access_token=[REDACTED]&symbol=SOL"),
            # Non-credential query params must NOT be redacted.
            ("?account_id=0x1234&symbol=SOL", "?account_id=0x1234&symbol=SOL"),
            ("?symbol=SOL-USD", "?symbol=SOL-USD"),
        ]:
            sanitized = sanitize_error_message(msg)
            self.assertEqual(sanitized, expected, f"bad URL redact: {msg!r} -> {sanitized!r}")

    def test_11b_tradedesk_neutralizes_exchange_native_exceptions(self):
        """TradeDesk wraps raw exceptions in a canonical failure envelope.

        Diagnostic context (wallet, URL, error term) is preserved by the
        new value-redacting sanitizer. The wholesale suppression boilerplate
        must NOT appear.
        """
        class BoomAgent:
            name = "boom"

            def list_accounts(self):
                return []

            def capabilities(self):
                return []

            def execute(self, request):
                raise RuntimeError(
                    "GET https://api.hyperliquid.xyz/info failed: "
                    "wallet 0xdead1234dead1234dead1234dead1234dead1234"
                )

        desk = TradeDesk()
        desk._agents = {"boom": BoomAgent()}
        desk._loaded = True
        response = desk.execute({
            "operation": "balance",
            "exchange": "boom",
            "account": "any",
        })
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "AGENT_EXCEPTION")
        # The wholesale suppression boilerplate must NOT appear.
        self.assertNotEqual(
            response.error.message,
            "Error message suppressed: potentially sensitive content.",
        )
        # Diagnostic context is preserved (wallet address, URL, error term).
        self.assertIn("0xdead1234dead1234dead1234dead1234dead1234", response.error.message)
        self.assertIn("api.hyperliquid", response.error.message.lower())


class TestHermesIntegration(unittest.TestCase):
    """Test 12: existing non-/trade Telegram behavior is unaffected."""

    def test_12a_trade_in_command_registry(self):
        """The /trade command is registered in COMMAND_REGISTRY."""
        from hermes_cli.commands import COMMAND_REGISTRY
        names = {cmd.name for cmd in COMMAND_REGISTRY}
        self.assertIn("trade", names)
        # Locate the trade entry and check its fields.
        trade_cmd = next(c for c in COMMAND_REGISTRY if c.name == "trade")
        self.assertEqual(trade_cmd.category, "Trading")
        self.assertTrue(trade_cmd.gateway_only)

    def test_12b_trade_dispatch_in_handle_command(self):
        """The adapter's _handle_command path contains direct /trade
        dispatch and does not depend on a plugin-handler registry."""
        from plugins.platforms.telegram import adapter as tg_adapter
        src = Path(tg_adapter.__file__).read_text(encoding="utf-8")
        # /trade is dispatched via a direct import + call, not via a
        # plugin-handler dict lookup.
        self.assertIn("from plugins.trade.wizard import handle_trade_command", src)
        self.assertIn("await handle_trade_command(self, msg)", src)
        # No plugin-handler registry function should remain. We check
        # for actual code (not comments) by stripping comment lines.
        import re
        code_lines = "\n".join(
            line for line in src.splitlines()
            if not line.lstrip().startswith("#")
        )
        for removed in (
            "def register_inline_keyboard_plugin",
            "def register_plugin_slash_command",
            "def unregister_inline_keyboard_plugin",
            "def unregister_plugin_slash_command",
            "def _iter_plugin_slash_command_handlers",
            "def _get_inline_keyboard_handlers",
            "_plugin_hooks",
        ):
            self.assertNotIn(
                removed, code_lines,
                f"adapter still references {removed} in code",
            )

    def test_12c_trade_callback_in_handle_callback_query(self):
        """The adapter's _handle_callback_query path contains direct
        trade: prefix dispatch."""
        from plugins.platforms.telegram import adapter as tg_adapter
        src = Path(tg_adapter.__file__).read_text(encoding="utf-8")
        # The trade: prefix is checked directly.
        self.assertIn('data.startswith("trade:")', src)
        # The wizard callback handler is invoked directly.
        self.assertIn("from plugins.trade.wizard import handle_trade_callback", src)
        self.assertIn("await handle_trade_callback(self, query, data)", src)

    def test_12d_other_prefixes_preserved(self):
        """The adapter still has all the existing dispatch branches for
        other callback prefixes — the trade: branch is purely
        additive."""
        from plugins.platforms.telegram import adapter as tg_adapter
        src = Path(tg_adapter.__file__).read_text(encoding="utf-8")
        for prefix in ("mp:", "mpg:", "mpv:", "mm:", "mc:", "ea:", "gt:", "cp:", "sc:", "cl:"):
            present = (
                f'data.startswith("{prefix}' in src
                or f'"{prefix}"' in src
            )
            self.assertTrue(
                present,
                f"missing prefix dispatch: {prefix}",
            )

    def test_12e_other_commands_unaffected(self):
        """The wizard only matches /trade, not other commands.

        Also verifies that lookalike commands like /trader and /trades
        are NOT consumed (exact-match semantics, not prefix match)."""
        from plugins.trade.wizard import handle_trade_command
        import asyncio

        class FakeMsg:
            def __init__(self, text):
                self.text = text
                self.chat = type("C", (), {"id": 1})()
                self.message_thread_id = None
        class FakeAdapter:
            def __init__(self):
                self.sent = []
            async def send_inline_keyboard(self, **kwargs):
                self.sent.append(kwargs)
            async def send(self, *args, **kwargs):
                self.sent.append(("text", args))

        async def run():
            adapter = FakeAdapter()
            # /help, /restart, /status, /model, plain text:
            # should NOT be consumed by the wizard.
            not_consumed = [
                "/help me please",
                "/restart",
                "/status",
                "/model",
                "/commands",
                "hi there",
                "/trader",      # lookalike: must NOT match
                "/trades",      # lookalike: must NOT match
                "/traderjoe",   # lookalike: must NOT match
            ]
            for text in not_consumed:
                msg = FakeMsg(text)
                adapter.sent = []
                handled = await handle_trade_command(adapter, msg)
                self.assertFalse(
                    handled, f"{text!r} should not invoke the trade wizard",
                )
                self.assertEqual(
                    len(adapter.sent), 0,
                    f"{text!r} should not have triggered any send",
                )
            # /trade and /trade@botname and /trade args: SHOULD be
            # consumed.
            consumed = [
                "/trade",
                "/TRADE",  # case-insensitive
                "/trade@mybot",
                "/trade abc def",
            ]
            for text in consumed:
                msg = FakeMsg(text)
                adapter.sent = []
                handled = await handle_trade_command(adapter, msg)
                self.assertTrue(
                    handled, f"{text!r} should invoke the trade wizard",
                )
                self.assertGreater(
                    len(adapter.sent), 0,
                    f"{text!r} should have triggered a send",
                )

        asyncio.run(run())

    def test_12f_wizard_remains_exchange_agnostic(self):
        """The wizard source contains no exchange-specific logic."""
        from plugins.trade import wizard
        src = Path(wizard.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "if exchange == ",
            'exchange == "hyperliquid"',
            "HYPERLIQUID_",
            "0x",
        ):
            self.assertNotIn(forbidden, src, f"wizard contains '{forbidden}'")

    def test_12g_tradedesk_remains_exchange_agnostic(self):
        """The TradeDesk source contains no exchange-specific logic."""
        from plugins.trade import tradedesk
        src = Path(tradedesk.__file__).read_text(encoding="utf-8")
        for forbidden in (
            'exchange == "hyperliquid"',
            "HYPERLIQUID_",
        ):
            self.assertNotIn(forbidden, src, f"TradeDesk contains '{forbidden}'")


class TestEndToEndRendering(unittest.TestCase):
    """A non-live e2e: walk the wizard through every screen with a fake
    agent and confirm everything renders canonically without any
    network IO."""

    def test_e2e_walk_happy_path(self):
        with tempfile.TemporaryDirectory(prefix="trade_test_") as tmp:
            agents_dir = Path(tmp) / "agents"
            agents_dir.mkdir()
            fake_file = _make_fake_agent_file("example", ["alpha", "beta"], "100.50")
            (agents_dir / "x_example_agent.py").write_text(fake_file.read_text())
            desk = TradeDesk()
            with mock.patch.object(tradedesk, "_agents_dir", return_value=agents_dir):
                w = TradeWizard(tradedesk=desk)
                key = ("chat",)
                s = w.open(key)
                self.assertEqual(s.state, "select_exchange")
                s = w.handle_callback(key, "exchange:example")
                self.assertEqual(s.state, "select_account")
                self.assertIn("Exchange: example", s.text)
                s = w.handle_callback(key, "account:beta")
                self.assertEqual(s.state, "action")
                self.assertIn("Account: beta", s.text)
                action_buttons = [
                    b for row in s.buttons for b in row
                    if b["callback_data"].startswith("action:")
                ]
                self.assertEqual(len(action_buttons), 6)
                callback_names = {b["callback_data"] for b in action_buttons}
                self.assertEqual(
                    callback_names,
                    {
                        "action:balance",
                        "action:positions_orders",
                        "action:new_order",
                        "action:ladder",
                        "action:cancel_orders",
                        "action:positions_management",
                    },
                )
                s = w.handle_callback(key, "action:balance")
                self.assertEqual(s.state, "balance")
                self.assertIn("Balance: 100.50 USDC", s.text)
                s = w.handle_callback(key, "refresh")
                self.assertEqual(s.state, "balance")
                self.assertIn("Balance: 100.50 USDC", s.text)
                s = w.handle_callback(key, "back")
                self.assertEqual(s.state, "select_account")
                s = w.handle_callback(key, "exit")
                self.assertEqual(s.state, "closed")
                self.assertEqual(s.buttons, [])


class TestPhase1ImplementedActions(unittest.TestCase):
    """Phase 1 only implements balance. Other actions reply 'not implemented'."""

    def test_phase1_only_balance(self):
        self.assertIn("balance", canonical.PHASE1_IMPLEMENTED_ACTIONS)
        self.assertIn("positions_orders", canonical.PHASE1_IMPLEMENTED_ACTIONS)
        for action in ("new_order", "ladder", "cancel_orders",
                       "positions_management"):
            self.assertNotIn(action, canonical.PHASE1_IMPLEMENTED_ACTIONS)
        self.assertEqual(
            set(GENERIC_ACTIONS),
            {"balance", "positions_orders", "new_order", "ladder",
             "cancel_orders", "positions_management"},
        )


class TestRaydiumLadderOmitBelowMinimum(unittest.TestCase):
    """Regression for the Raydium ladder omit-below-minimum-notional behavior.

    When a child order would fall below the exchange's min_quantity or
    min_notional, the agent must omit that child and continue submitting the
    remaining children rather than aborting the whole batch.
    """

    def setUp(self):
        self._raydium_env_backup = {k: v for k, v in os.environ.items() if k.startswith("RAYDIUM_")}
        for key in list(os.environ.keys()):
            if key.startswith("RAYDIUM_"):
                os.environ.pop(key, None)
        self._hermes_home_backup = os.environ.get("HERMES_HOME")
        self._raydium_tmpdir = tempfile.TemporaryDirectory(prefix="raydium_test_home_")
        os.environ["HERMES_HOME"] = self._raydium_tmpdir.name

    def tearDown(self):
        for key in list(os.environ.keys()):
            if key.startswith("RAYDIUM_"):
                os.environ.pop(key, None)
        os.environ.update(self._raydium_env_backup)
        if self._hermes_home_backup is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._hermes_home_backup
        self._raydium_tmpdir.cleanup()
        _restore_env()

    def test_raydium_ladder_omits_below_min_notional_children(self):
        os.environ["RAYDIUM_PHANTOM_ACCOUNT_ID"] = "acc-phantom"
        os.environ["RAYDIUM_PHANTOM_API_KEY"] = "pub-phantom"
        os.environ["RAYDIUM_PHANTOM_SECRET_KEY"] = "3MNQE1X"

        raydium = __import__("plugins.trade.agents.x_raydium_agent", fromlist=["*"])

        # Raydium BTC: base_tick=1e-05, quote_tick=0.1, min_notional=10.
        metadata = {
            "market_id": 1,
            "symbol": "PERP_BTC_USDC",
            "display_symbol": "BTC",
            "price_precision": 1,
            "size_precision": 5,
            "min_quantity": "0.00001",
            "min_notional": "10",
            "mark_price": "63500",
        }

        children, kept_volume, omitted = raydium._build_raydium_ladder_children(
            distribution="half_gaussian",
            order_count=10,
            total_volume=raydium._decimal_or_zero("0.001"),
            start_price=raydium._decimal_or_zero("63500"),
            end_price=raydium._decimal_or_zero("61000"),
            size_decimals=5,
            price_decimals=1,
            min_quantity=raydium._decimal_or_zero("0.00001"),
            min_notional=raydium._decimal_or_zero("10"),
        )

        # Tiny total volume must force most children below min_notional=10
        # while still leaving at least some children valid.
        self.assertGreater(omitted, 0, "expected some children to be omitted as below minimum")
        for child in children:
            notional = child["price"] * child["size"]
            self.assertGreaterEqual(notional, raydium._decimal_or_zero("10"))
            self.assertGreaterEqual(child["size"], raydium._decimal_or_zero("0.00001"))


class TestArcusAgentPhase1(unittest.TestCase):
    def setUp(self):
        self._arcus_env_backup = {k: v for k, v in os.environ.items() if k.startswith("ARCUS_")}
        for key in list(os.environ.keys()):
            if key.startswith("ARCUS_"):
                os.environ.pop(key, None)
        self._hermes_home_backup = os.environ.get("HERMES_HOME")
        self._arcus_tmpdir = tempfile.TemporaryDirectory(prefix="arcus_test_home_")
        os.environ["HERMES_HOME"] = self._arcus_tmpdir.name

    def tearDown(self):
        for key in list(os.environ.keys()):
            if key.startswith("ARCUS_"):
                os.environ.pop(key, None)
        os.environ.update(self._arcus_env_backup)
        if self._hermes_home_backup is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._hermes_home_backup
        self._arcus_tmpdir.cleanup()
        _restore_env()

    def test_arcus_list_accounts_discovers_only_complete_blocks_from_env_and_dotenv(self):
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"

        env_path = Path(os.environ["HERMES_HOME"]) / ".env"
        env_path.write_text(
            "ARCUS_BEAM_WALLET=0x1111111111111111111111111111111111111111\n"
            "ARCUS_BEAM_APISIGNINGKEY=secret-b\n"
            "ARCUS_INCOMPLETE_WALLET=0x2222222222222222222222222222222222222222\n"
        )

        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        self.assertEqual(arcus.list_accounts(), ["amiroo", "beam"])

    def test_arcus_balance_uses_public_account_endpoint_and_maps_portfolio(self):
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"

        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        seen = {}

        def fake_public_get(credentials, path):
            seen.setdefault("paths", []).append(path)
            seen["wallet"] = credentials["wallet"]
            if path == "/v1/account":
                return {
                    "accountIndex": 0,
                    "address": "0x742d35cc6634c0532925a3b844bc454e4438f44e",
                    "netQuoteBalance": "1000.25",
                    "equity": "1234.56",
                    "freeCollateral": "789.01",
                    "positions": {
                        "1": {
                            "marketDisplayName": "BTC-USD",
                            "side": "LONG",
                            "size": "0.010",
                            "averageEntryPrice": "63000.5",
                            "markPx": "64000.0",
                            "unrealizedPnl": "12.34",
                        }
                    },
                }
            raise AssertionError(path)

        with mock.patch.object(arcus, "_public_get", side_effect=fake_public_get):
            response = arcus.execute({"operation": "balance", "exchange": "arcus", "account": "amiroo"})

        self.assertTrue(response.success)
        self.assertEqual(seen["paths"], ["/v1/account"])
        self.assertEqual(response.balance.value, "1234.56")
        self.assertEqual(response.balance.unit, "USD")
        self.assertEqual(response.portfolio_summary.account_value, "1234.56")
        self.assertEqual(response.portfolio_summary.withdrawable, "789.01")
        self.assertEqual(response.portfolio_summary.margin_used, "445.55")
        self.assertEqual(response.portfolio_summary.total_position_value, "234.31")
        self.assertEqual(len(response.positions), 1)
        self.assertEqual(response.positions[0].symbol, "BTC-USD")
        self.assertEqual(response.positions[0].side, "long")

    def test_arcus_positions_orders_uses_public_reads_and_aggregates_orders(self):
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"

        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])

        def fake_public_get(credentials, path):
            if path == "/v1/account":
                return {
                    "positions": {
                        "1": {
                            "marketDisplayName": "BTC-USD",
                            "side": "LONG",
                            "size": "0.010",
                            "averageEntryPrice": "63000.5",
                            "markPx": "64000.0",
                            "unrealizedPnl": "12.34",
                        }
                    }
                }
            if path == "/v1/openOrders":
                return {
                    "orders": [
                        {"marketDisplayName": "BTC-USD", "side": "BUY", "price": "62000", "remainingSize": "0.005"},
                        {"marketDisplayName": "BTC-USD", "side": "BUY", "price": "61000", "remainingSize": "0.015"},
                        {"marketDisplayName": "ETH-USD", "side": "SELL", "price": "3500", "remainingSize": "1.25"},
                    ]
                }
            raise AssertionError(path)

        with mock.patch.object(arcus, "_public_get", side_effect=fake_public_get):
            response = arcus.execute({"operation": "positions_orders", "exchange": "arcus", "account": "amiroo"})

        self.assertTrue(response.success)
        self.assertEqual(len(response.positions), 1)
        self.assertEqual(response.open_order_count, 3)
        self.assertEqual(len(response.order_groups), 2)
        btc_group = next(group for group in response.order_groups if group.symbol == "BTC-USD")
        self.assertEqual(btc_group.side, "buy")
        self.assertEqual(btc_group.order_count, 2)
        self.assertEqual(btc_group.total_size, "0.020")
        self.assertEqual(btc_group.vwap, "61250")
        self.assertEqual(btc_group.min_price, "61000")
        self.assertEqual(btc_group.max_price, "62000")

    def test_arcus_not_implemented_operations_fail_cleanly(self):
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"
        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        response = arcus.execute({"operation": "ladder", "exchange": "arcus", "account": "amiroo"})
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "NOT_IMPLEMENTED")

    def test_arcus_signed_post_serializes_object_body_and_signs_with_ed25519(self):
        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        priv = Ed25519PrivateKey.generate()
        api_signing_key = priv.public_key().public_bytes_raw().hex()
        credentials = {
            "account": "amiroo",
            "wallet": "0x742d35Cc6634C0532925a3b844Bc454e4438f44e",
            "api_signing_key": api_signing_key,
            "private_key_hex": priv.private_bytes_raw().hex(),
            "account_index": 0,
            "base_url": "https://api.arcus.xyz",
        }
        captured = {}

        class FakeResponse:
            status_code = 200
            headers = {}
            text = "{}"

            def json(self):
                return {"orderId": "ord-1", "status": "OPEN"}

        def fake_post(url, *, headers, data, timeout):
            captured["url"] = url
            captured["headers"] = dict(headers)
            captured["data"] = data
            captured["timeout"] = timeout
            return FakeResponse()

        with mock.patch.object(arcus.requests, "post", side_effect=fake_post):
            payload = {"address": credentials["wallet"], "marketId": 1, "quantity": "0.01", "price": "100"}
            response = arcus._signed_post(credentials, "/v1/placeOrder", payload)

        self.assertEqual(response, {"orderId": "ord-1", "status": "OPEN"})
        self.assertEqual(captured["url"], "https://api.arcus.xyz/v1/placeOrder")
        self.assertEqual(captured["headers"]["Content-Type"], "application/json")
        self.assertEqual(captured["headers"]["X-API-Key"], priv.public_key().public_bytes_raw().hex())
        self.assertEqual(len(captured["headers"]["X-Signature"]), 128)
        self.assertEqual(captured["data"], arcus._canonical_json(payload))

    def test_arcus_new_order_quantizes_uses_live_market_and_submits_place_order(self):
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"

        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        seen = {}

        market = {
            "market_id": 1,
            "display_symbol": "BTC-USD",
            "tick_size": arcus._decimal_or_zero("0.1"),
            "step_size": arcus._decimal_or_zero("0.00000001"),
            "price_precision": 1,
            "size_precision": 8,
            "min_notional": arcus._decimal_or_zero("5"),
        }

        def fake_resolve_market(symbol):
            return market

        def fake_signed_post(credentials, path, payload):
            seen["path"] = path
            seen["payload"] = payload
            seen["client_id_prefix"] = payload["clientId"][:5]
            return {"orderId": "deadbeef", "status": "OPEN"}

        with mock.patch.object(arcus, "_resolve_market", side_effect=fake_resolve_market), \
             mock.patch.object(arcus, "_signed_post", side_effect=fake_signed_post):
            response = arcus.execute({
                "operation": "new_order",
                "exchange": "arcus",
                "account": "amiroo",
                "symbol": "btc-usd",
                "side": "buy",
                "order_type": "limit",
                "volume": "0.05",
                "price": "100.07",
            })

        self.assertTrue(response.success)
        self.assertEqual(seen["path"], "/v1/placeOrder")
        self.assertEqual(seen["payload"]["marketId"], 1)
        self.assertEqual(seen["payload"]["address"], "0x742d35Cc6634C0532925a3b844Bc454e4438f44e")
        self.assertEqual(seen["payload"]["price"], "100.0")
        self.assertEqual(seen["payload"]["quantity"], "0.05000000")
        self.assertEqual(seen["client_id_prefix"], "arcus")
        self.assertTrue(seen["payload"]["reduceOnly"] is False)
        self.assertEqual(response.order.exchange_order_id, int("deadbeef", 16))

    def test_arcus_new_order_rejects_below_min_notional_before_submit(self):
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"

        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        market = {
            "market_id": 1,
            "display_symbol": "BTC-USD",
            "tick_size": arcus._decimal_or_zero("0.1"),
            "step_size": arcus._decimal_or_zero("0.00000001"),
            "price_precision": 1,
            "size_precision": 8,
            "min_notional": arcus._decimal_or_zero("5"),
        }

        with mock.patch.object(arcus, "_resolve_market", return_value=market):
            response = arcus.execute({
                "operation": "new_order",
                "exchange": "arcus",
                "account": "amiroo",
                "symbol": "BTC-USD",
                "side": "buy",
                "order_type": "limit",
                "volume": "0.0001",
                "price": "100.0",
            })

        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "NOTIONAL_BELOW_MINIMUM")

    def test_arcus_cancel_orders_partial_when_one_target_unconfirmed(self):
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"
        os.environ["ARCUS_AMIROO_PRIVATE_KEY"] = "00" * 32

        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        market = {"market_id": 1, "display_symbol": "BTC-USD", "tick_size": arcus._decimal_or_zero("0.1"), "step_size": arcus._decimal_or_zero("0.00000001"), "price_precision": 1, "size_precision": 8, "min_notional": arcus._decimal_or_zero("5")}

        calls = []

        def fake_resolve_market(symbol):
            return market

        def fake_signed_post(credentials, path, payload):
            calls.append((path, payload.get("orderId")))
            if payload.get("orderId") == "ord-2":
                raise RuntimeError("HTTP 500: error")
            return {"status": "CANCELED"}

        open_orders_before = [
            {"orderId": "ord-1", "marketDisplayName": "BTC-USD", "side": "BUY"},
            {"orderId": "ord-2", "marketDisplayName": "BTC-USD", "side": "BUY"},
            {"orderId": "ord-3", "marketDisplayName": "ETH-USD", "side": "BUY"},
        ]
        open_orders_after = [
            {"orderId": "ord-1", "marketDisplayName": "BTC-USD", "side": "BUY"},
            {"orderId": "ord-3", "marketDisplayName": "ETH-USD", "side": "BUY"},
        ]
        fetch_state = {"count": 0}

        def fake_fetch_open_orders(credentials):
            if fetch_state["count"] == 0:
                fetch_state["count"] += 1
                return open_orders_before
            return open_orders_after

        with mock.patch.object(arcus, "_resolve_market", side_effect=fake_resolve_market), \
             mock.patch.object(arcus, "_signed_post", side_effect=fake_signed_post), \
             mock.patch.object(arcus, "_fetch_open_orders_for_account", side_effect=fake_fetch_open_orders):
            response = arcus.execute({
                "operation": "cancel_orders",
                "exchange": "arcus",
                "account": "amiroo",
                "symbol": "BTC-USD",
                "side": "buy",
            })

        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "VERIFICATION_FAILED")
        self.assertEqual([path for path, _ in calls], ["/v1/cancelOrder", "/v1/cancelOrder"])
        self.assertEqual(response.cancel_group.cancelled_order_count, 2)
        self.assertEqual(response.cancel_group.remaining_target_count, 1)

    def test_arcus_cancel_orders_full_success_when_after_state_matches_target(self):
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"
        os.environ["ARCUS_AMIROO_PRIVATE_KEY"] = "00" * 32

        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        market = {"market_id": 1, "display_symbol": "BTC-USD", "tick_size": arcus._decimal_or_zero("0.1"), "step_size": arcus._decimal_or_zero("0.00000001"), "price_precision": 1, "size_precision": 8, "min_notional": arcus._decimal_or_zero("5")}

        def fake_signed_post(credentials, path, payload):
            return {"status": "CANCELED"}

        open_orders_before = [
            {"orderId": "ord-1", "marketDisplayName": "BTC-USD", "side": "BUY"},
            {"orderId": "ord-2", "marketDisplayName": "BTC-USD", "side": "BUY"},
            {"orderId": "ord-3", "marketDisplayName": "ETH-USD", "side": "BUY"},
        ]
        fetch_state = {"count": 0}

        def fake_fetch_open_orders(credentials):
            if fetch_state["count"] == 0:
                fetch_state["count"] += 1
                return open_orders_before
            return []

        with mock.patch.object(arcus, "_resolve_market", return_value=market), \
             mock.patch.object(arcus, "_signed_post", side_effect=fake_signed_post), \
             mock.patch.object(arcus, "_fetch_open_orders_for_account", side_effect=fake_fetch_open_orders):
            response = arcus.execute({
                "operation": "cancel_orders",
                "exchange": "arcus",
                "account": "amiroo",
                "symbol": "BTC-USD",
                "side": "buy",
            })

        self.assertTrue(response.success)
        self.assertEqual(response.cancel_group.cancelled_order_count, 2)
        self.assertEqual(response.cancel_group.targeted_order_count, 2)
        self.assertEqual(response.cancel_group.confirmed_absent_count, 2)
        self.assertTrue(response.cancel_group.verified)

    def test_arcus_new_order_resolves_short_base_symbol(self):
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"

        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])

        def fake_public_get(credentials, path):
            return {
                "markets": [
                    {"marketId": 1, "marketDisplayName": "BTC-USD", "baseAsset": "BTC", "tickSize": "0.1", "stepSize": "0.00000001", "minOrderNotional": "5"},
                    {"marketId": 2, "marketDisplayName": "ETH-USD", "baseAsset": "ETH", "tickSize": "0.01", "stepSize": "0.001", "minOrderNotional": "5"},
                ]
            }

        def fake_signed_post(credentials, path, payload):
            return {"orderId": "deadbeef", "status": "OPEN"}

        with mock.patch.object(arcus, "_public_get", side_effect=fake_public_get), \
             mock.patch.object(arcus, "_signed_post", side_effect=fake_signed_post):
            response = arcus.execute({
                "operation": "new_order",
                "exchange": "arcus",
                "account": "amiroo",
                "symbol": "ETH",
                "side": "sell",
                "order_type": "limit",
                "volume": "0.1",
                "price": "2500",
            })

        self.assertTrue(response.success)
        self.assertEqual(response.order.symbol, "ETH-USD")

    def test_arcus_cancel_orders_uses_market_lookup_before_targeting(self):
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"

        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])

        def fake_resolve_market(symbol):
            raise ValueError("SYMBOL_NOT_FOUND")

        with mock.patch.object(arcus, "_resolve_market", side_effect=fake_resolve_market):
            response = arcus.execute({
                "operation": "cancel_orders",
                "exchange": "arcus",
                "account": "amiroo",
                "symbol": "NOPE-USD",
                "side": "buy",
            })

        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "SYMBOL_NOT_FOUND")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
