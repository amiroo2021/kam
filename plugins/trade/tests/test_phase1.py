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
# Module-level env state preservation for PACIFICA_* keys.
# Pop PACIFICA_* env vars only at module import time, and restore
# them at module teardown. The atexit hook was insufficient because
# it never fires between tests inside one unittest process.
_MODULE_PRESERVED_PACIFICA_ENV: Dict[str, str] = {}
for _k in list(os.environ.keys()):
    if _k.startswith("PACIFICA_"):
        _MODULE_PRESERVED_PACIFICA_ENV[_k] = os.environ[_k]


def _restore_env() -> None:
    """Backward-compat no-op. Real restoration lives in tearDownModule."""
    pass


def setUpModule() -> None:
    for _k in list(os.environ.keys()):
        if _k.startswith("PACIFICA_") and _k not in _MODULE_PRESERVED_PACIFICA_ENV:
            _MODULE_PRESERVED_PACIFICA_ENV[_k] = os.environ[_k]


def tearDownModule() -> None:
    for _k in list(os.environ.keys()):
        if _k.startswith("PACIFICA_") and _k not in _MODULE_PRESERVED_PACIFICA_ENV:
            os.environ.pop(_k, None)
    for _k, _v in _MODULE_PRESERVED_PACIFICA_ENV.items():
        os.environ[_k] = _v




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
        """The plugin registers /trade as a Telegram-menu command."""
        from plugins.trade import register as trade_register, _handle_trade_slash

        class _StubCtx:
            def __init__(self) -> None:
                self.calls: List[Dict[str, Any]] = []

            def register_command(self, name, handler, description="", args_hint=""):
                self.calls.append(
                    {
                        "name": name,
                        "handler": handler,
                        "description": description,
                        "args_hint": args_hint,
                    }
                )

        ctx = _StubCtx()
        trade_register(ctx)
        names = {call["name"] for call in ctx.calls}
        self.assertIn("trade", names)
        trade_cmd = next(call for call in ctx.calls if call["name"] == "trade")
        self.assertEqual(trade_cmd["description"], "Open the trading wizard")
        self.assertIs(trade_cmd["handler"], _handle_trade_slash)

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

    def _setUp_env(self):
        """Set up an isolated APEX_<alias>_BITGET env config so the
        agent's account-discovery path has something to find. Idempotent:
        re-entrant calls reuse the same tmpdir-backed HERMES_HOME.
        """
        self._apex_env_backup = {k: v for k, v in os.environ.items() if k.startswith("APEX_")}
        for key in list(os.environ.keys()):
            if key.startswith("APEX_"):
                os.environ.pop(key, None)
        os.environ["APEX_BITGET_ACCOUNTID"] = "686607787470356535"
        os.environ["APEX_BITGET_APIKEY"] = "00000000-0000-0000-0000-000000000000"
        os.environ["APEX_BITGET_APIKEYSECRET"] = "placeholder-secret-replace-at-runtime"
        os.environ["APEX_BITGET_APIKEYPASSPHRASE"] = "placeholder-passphrase"
        os.environ["APEX_BITGET_SEEDS"] = ("0x" + "ab" * 32)
        os.environ["APEX_BITGET_L2KEY"] = ("0x" + "cd" * 32)

    def _tearDown_env(self):
        """Restore the live process environment after _setUp_env."""
        for key in list(os.environ.keys()):
            if key.startswith("APEX_"):
                os.environ.pop(key, None)
        os.environ.update(getattr(self, "_apex_env_backup", {}))
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
        # `set_leverage` is not implemented for Arcus (only Hyperliquid-style
        # agents support it). Pick an op that has no other validation path so
        # we hit the NOT_IMPLEMENTED fallback cleanly.
        response = arcus.execute({"operation": "set_leverage", "exchange": "arcus", "account": "amiroo"})
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "NOT_IMPLEMENTED")

    def test_arcus_capabilities_advertise_cancel_order_group(self):
        """Regression: the wizard dispatches the Cancel menu using
        ``cancel_order_group`` (singular). Arcus must advertise that operation
        in ``capabilities()`` and the dispatcher must accept it; otherwise the
        Cancel menu hits the ``NOT_IMPLEMENTED`` fallback. ``cancel_orders``
        (plural) remains a backwards-compat alias."""
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"
        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        caps = arcus.capabilities()
        self.assertIn("cancel_order_group", caps)
        self.assertIn("cancel_orders", caps)

        # Both operation spellings must route to the cancel handler, not the
        # NOT_IMPLEMENTED fallback. We mock _cancel_order_group to assert it
        # gets called for either name.
        import unittest.mock as mock
        called_with = {}
        def fake_cancel(req):
            called_with["req"] = req
            return arcus.make_success(
                operation="cancel_order_group", exchange="arcus",
                account="amiroo",
                cancel_group=arcus.CanonicalCancelGroupResult(
                    symbol="BTC-USD", side="buy",
                    targeted_order_count=0, cancelled_order_count=0,
                    confirmed_absent_count=0, remaining_target_count=0,
                    verified=True, partial=False, status="success",
                    batch_count=0,
                ),
            )
        with mock.patch.object(arcus, "_cancel_order_group", side_effect=fake_cancel):
            r1 = arcus.execute({
                "operation": "cancel_order_group",
                "exchange": "arcus", "account": "amiroo",
                "symbol": "BTC-USD", "side": "buy",
            })
            self.assertTrue(r1.success, f"cancel_order_group should work: {r1}")
            r2 = arcus.execute({
                "operation": "cancel_orders",
                "exchange": "arcus", "account": "amiroo",
                "symbol": "BTC-USD", "side": "buy",
            })
            self.assertTrue(r2.success, f"cancel_orders alias should work: {r2}")

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

        def fake_signed_post(credentials, path, payload, *, typed_payload=None):
            seen["path"] = path
            seen["payload"] = payload
            seen["typed_payload"] = typed_payload
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

        def fake_signed_post(credentials, path, payload, *, typed_payload=None):
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

        def fake_signed_post(credentials, path, payload, *, typed_payload=None):
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

        def fake_signed_post(credentials, path, payload, *, typed_payload=None):
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

    def test_arcus_typed_payload_place_has_exact_field_set_and_types(self):
        """Regression: /v1/placeOrder Scheme 1 typed payload must be exactly the
        set of integer/str fields below. If this test fails, a future change
        has drifted the signed bytes away from what Arcus's matching engine
        verifies against — the same root cause as the 2026-07-31 'invalid
        order signature' outage."""
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"
        os.environ["ARCUS_AMIROO_PRIVATE_KEY"] = "00" * 32

        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])

        creds = {
            "wallet": "0x742d35Cc6634C0532925a3b844Bc454e4438f44e",
            "api_signing_key": "any",
            "private_key_hex": "00" * 32,
            "account_index": 0,
            "base_url": "https://api.arcus.xyz",
        }
        typed = arcus._build_arcus_typed_payload_place(
            credentials=creds,
            market_id=1,
            price_ticks=74000,
            qty_quantums=70000,
            side="sell",
            time_in_force="gtt",
            good_til_time_us=1788986953972341,
            timestamp_ns=1785530953972340120,
            reduce_only=False,
        )

        self.assertEqual(
            list(typed.keys()),
            ["ad", "ai", "ct", "g", "m", "op", "p", "q", "r", "s", "t", "v"],
        )
        self.assertEqual(typed["ad"], "0x742d35cc6634c0532925a3b844bc454e4438f44e")
        self.assertIsInstance(typed["ad"], str)
        self.assertEqual(typed["ai"], 0)
        self.assertIsInstance(typed["ai"], int)
        self.assertEqual(typed["ct"], 1785530953972340120)
        self.assertIsInstance(typed["ct"], int)
        self.assertEqual(typed["g"], 1788986953972341 * 1000)
        self.assertIsInstance(typed["g"], int)
        self.assertEqual(typed["m"], 1)
        self.assertIsInstance(typed["m"], int)
        self.assertEqual(typed["op"], arcus._ARCUS_OP_PLACE)
        self.assertEqual(typed["op"], 1)
        self.assertIsInstance(typed["op"], int)
        self.assertEqual(typed["p"], 74000)
        self.assertIsInstance(typed["p"], int)
        self.assertEqual(typed["q"], 70000)
        self.assertIsInstance(typed["q"], int)
        self.assertEqual(typed["r"], 0)
        self.assertIsInstance(typed["r"], int)
        self.assertEqual(typed["s"], 1)  # SELL = 1 (verified live)
        self.assertIsInstance(typed["s"], int)
        self.assertEqual(typed["t"], 0)  # GTT = 0 (verified live)
        self.assertIsInstance(typed["t"], int)
        self.assertEqual(typed["v"], 1)
        self.assertIsInstance(typed["v"], int)

        signed_bytes = arcus._typed_payload_bytes(typed)
        expected = (
            '{"ad":"0x742d35cc6634c0532925a3b844bc454e4438f44e",'
            '"ai":0,'
            '"ct":1785530953972340120,'
            '"g":1788986953972341000,'
            '"m":1,'
            '"op":1,'
            '"p":74000,'
            '"q":70000,'
            '"r":0,'
            '"s":1,'
            '"t":0,'
            '"v":1}'
        )
        self.assertEqual(signed_bytes, expected)

        # BUY=0, SELL=1; reduce_only=True → r=1; other TIF codes.
        typed_buy = arcus._build_arcus_typed_payload_place(
            credentials=creds, market_id=1, price_ticks=1, qty_quantums=1,
            side="buy", time_in_force="gtt", good_til_time_us=1,
            timestamp_ns=1, reduce_only=True,
        )
        self.assertEqual(typed_buy["s"], 0)
        self.assertEqual(typed_buy["r"], 1)
        self.assertEqual(arcus._SIDE_TO_INT, {"buy": 0, "sell": 1})
        self.assertEqual(arcus._TIF_TO_INT, {"gtt": 0, "fok": 1, "ioc": 2, "alo": 3})

        # When client_id is provided, the typed payload MUST include `c` between
        # `ai` and `ct` (alphabetical canonical order). When empty, `c` is OMITTED
        # entirely. Including an empty `c` (or omitting a non-empty `c`) causes
        # Arcus's signature verifier to reject with HTTP 401 — see the live
        # debugging notes from 2026-07-31.
        typed_with_id = arcus._build_arcus_typed_payload_place(
            credentials=creds, market_id=1, price_ticks=74000, qty_quantums=70000,
            side="sell", time_in_force="gtt",
            good_til_time_us=1788986953972341, timestamp_ns=1785530953972340120,
            reduce_only=False, client_id="arcus-test-123",
        )
        self.assertEqual(
            list(typed_with_id.keys()),
            ["ad", "ai", "c", "ct", "g", "m", "op", "p", "q", "r", "s", "t", "v"],
        )
        self.assertEqual(typed_with_id["c"], "arcus-test-123")
        signed_with_id = arcus._typed_payload_bytes(typed_with_id)
        expected_with_id = (
            '{"ad":"0x742d35cc6634c0532925a3b844bc454e4438f44e",'
            '"ai":0,'
            '"c":"arcus-test-123",'
            '"ct":1785530953972340120,'
            '"g":1788986953972341000,'
            '"m":1,'
            '"op":1,'
            '"p":74000,'
            '"q":70000,'
            '"r":0,'
            '"s":1,'
            '"t":0,'
            '"v":1}'
        )
        self.assertEqual(signed_with_id, expected_with_id)

        # client_id="" → c is absent (not present as empty string).
        typed_empty_id = arcus._build_arcus_typed_payload_place(
            credentials=creds, market_id=1, price_ticks=74000, qty_quantums=70000,
            side="sell", time_in_force="gtt",
            good_til_time_us=1788986953972341, timestamp_ns=1785530953972340120,
            reduce_only=False, client_id="",
        )
        self.assertNotIn("c", typed_empty_id)
        self.assertEqual(
            list(typed_empty_id.keys()),
            ["ad", "ai", "ct", "g", "m", "op", "p", "q", "r", "s", "t", "v"],
        )

    def test_arcus_typed_payload_cancel_has_exact_field_set_and_types(self):
        """Regression: /v1/cancelOrder Scheme 1 typed payload is the exact 7-key
        shape below. g/p/q/s/t are intentionally absent — cancel doesn't carry
        them. Drift here = the same 'invalid signature' bug as for place."""
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"
        os.environ["ARCUS_AMIROO_PRIVATE_KEY"] = "00" * 32

        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])

        creds = {
            "wallet": "0x742d35Cc6634C0532925a3b844Bc454e4438f44e",
            "api_signing_key": "any",
            "private_key_hex": "00" * 32,
            "account_index": 0,
            "base_url": "https://api.arcus.xyz",
        }
        typed = arcus._build_arcus_typed_payload_cancel(
            credentials=creds,
            market_id=3,
            order_id="a07e3d675d154daa",
            timestamp_ns=1785530957476746009,
        )

        self.assertEqual(
            list(typed.keys()),
            ["ad", "ai", "ct", "id", "m", "op", "v"],
        )
        self.assertEqual(typed["ad"], "0x742d35cc6634c0532925a3b844bc454e4438f44e")
        self.assertEqual(typed["ai"], 0)
        self.assertEqual(typed["ct"], 1785530957476746009)
        self.assertEqual(typed["id"], "a07e3d675d154daa")
        self.assertEqual(typed["m"], 3)
        self.assertEqual(typed["op"], arcus._ARCUS_OP_CANCEL)
        self.assertEqual(typed["op"], 2)
        self.assertEqual(typed["v"], 1)

        signed_bytes = arcus._typed_payload_bytes(typed)
        expected = (
            '{"ad":"0x742d35cc6634c0532925a3b844bc454e4438f44e",'
            '"ai":0,'
            '"ct":1785530957476746009,'
            '"id":"a07e3d675d154daa",'
            '"m":3,'
            '"op":2,'
            '"v":1}'
        )
        self.assertEqual(signed_bytes, expected)

    def test_arcus_signed_post_passes_typed_payload_as_signed_bytes_and_body_as_data(self):
        """Regression: _signed_post must sign the typed payload, send the body
        as the request body, and use the typed payload's ct as X-Timestamp.
        This is what makes the live API accept signatures — body-signing here
        was the original 'invalid order signature' bug."""
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"
        os.environ["ARCUS_AMIROO_PRIVATE_KEY"] = "00" * 32

        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("00" * 32))
        pub = priv.public_key().public_bytes_raw().hex()
        creds = {
            "wallet": "0x742d35Cc6634C0532925a3b844Bc454e4438f44e",
            "api_signing_key": pub,
            "private_key_hex": "00" * 32,
            "account_index": 0,
            "base_url": "https://api.arcus.xyz",
        }

        captured = {}

        class _Resp:
            status_code = 200
            text = "{}"
            def json(self):
                return {"orderId": "deadbeef"}

        def fake_post(url, *, headers, data, timeout):
            captured["url"] = url
            captured["headers"] = dict(headers)
            captured["data"] = data
            return _Resp()

        typed = {
            "ad": "0x742d35cc6634c0532925a3b844bc454e4438f44e",
            "ai": 0,
            "ct": 1785530953972340120,
            "g": 1788986953972341000,
            "m": 1,
            "op": 1,
            "p": 74000,
            "q": 70000,
            "r": 0,
            "s": 1,
            "t": 0,
            "v": 1,
        }
        body = {
            "address": "0x742d35Cc6634C0532925a3b844Bc454e4438f44e",
            "marketId": 1,
            "accountIndex": 0,
            "orderSide": "SELL",
            "orderType": "LIMIT",
            "quantity": "0.07",
            "price": "74",
            "timeInForce": "GTT",
            "goodTilTime": "1788986953972341",
            "timestamp": 1785530953972340120,
        }

        with mock.patch.object(arcus.requests, "post", side_effect=fake_post):
            arcus._signed_post(creds, "/v1/placeOrder", body, typed_payload=typed)

            self.assertEqual(captured["headers"]["X-Timestamp"], "1785530953972340120")
            self.assertEqual(captured["headers"]["X-API-Key"], pub)
            self.assertEqual(captured["data"], arcus._canonical_json(body))
            self.assertNotIn('"op"', captured["data"])
            expected_signed = arcus._typed_payload_bytes(typed).encode("utf-8")
            self.assertEqual(
                captured["headers"]["X-Signature"],
                priv.sign(expected_signed).hex(),
            )

    # --- Ladder tests --------------------------------------------------

    def test_arcus_ladder_distribution_weights_uniform_returns_ones(self):
        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        weights = arcus._ladder_distribution_weights(5, "uniform")
        self.assertEqual(weights, [arcus.Decimal("1")] * 5)

    def test_arcus_ladder_distribution_weights_half_gaussian_orientation(self):
        """Half-Gaussian: index 0 = smallest weight, index N-1 = largest."""
        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        weights = arcus._ladder_distribution_weights(10, "half_gaussian")
        self.assertEqual(len(weights), 10)
        for i in range(len(weights) - 1):
            self.assertLess(weights[i], weights[i + 1],
                            f"weight[{i}] should be < weight[{i+1}]")
        self.assertAlmostEqual(float(weights[0]), arcus.math.exp(-4.5), places=6)
        self.assertAlmostEqual(float(weights[-1]), 1.0, places=6)

    def test_arcus_ladder_build_prices_monotonic_after_tick_quantization(self):
        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        D = arcus.Decimal
        prices = arcus._build_ladder_prices(D("70"), D("80"), 5, D("0.001"))
        self.assertEqual(len(prices), 5)
        self.assertEqual(prices[0], D("70"))
        for i in range(len(prices) - 1):
            self.assertLessEqual(prices[i], prices[i + 1])
        prices_buy = arcus._build_ladder_prices(D("80"), D("70"), 5, D("0.001"))
        for i in range(len(prices_buy) - 1):
            self.assertGreaterEqual(prices_buy[i], prices_buy[i + 1])

    def test_arcus_ladder_omits_sub_10usd_children_without_redistributing(self):
        """Children whose price × size < $10 are omitted; survivors keep original sizes."""
        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        D = arcus.Decimal
        creds = {"wallet": "0x" + "11" * 20, "api_signing_key": "x",
                 "private_key_hex": "00" * 32, "account_index": 0}
        children, kept_volume, omitted, kept_count = arcus._build_arcus_ladder_children(
            credentials=creds, market_id=1, price_increment=D("0.1"),
            size_increment=D("0.1"), side="sell", distribution="uniform",
            order_count=5, total_volume=D("100"), start_price=D("70"),
            end_price=D("80"), size_precision=1, price_precision=1,
            min_notional=D("10"), batch_id_prefix="t",
        )
        self.assertGreaterEqual(kept_count, 2)
        for c in children:
            self.assertGreaterEqual(c["price"] * c["size"], D("10"))
        self.assertEqual(omitted, 5 - kept_count)

    def test_arcus_ladder_execute_returns_failure_for_buy_with_inverted_prices(self):
        """BUY ladders need end_price < start_price; otherwise INVALID_LADDER_DIRECTION."""
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"
        os.environ["ARCUS_AMIROO_PRIVATE_KEY"] = "00" * 32
        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        response = arcus.execute({
            "operation": "ladder",
            "exchange": "arcus",
            "account": "amiroo",
            "symbol": "BTC-USD",
            "side": "buy",
            "distribution": "half_gaussian",
            "order_count": 10,
            "total_volume": "100",
            "start_price": "60000",
            "end_price": "65000",  # wrong direction for buy
        })
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "INVALID_LADDER_DIRECTION")

    def test_arcus_ladder_capabilities_advertise_ladder(self):
        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        self.assertIn("ladder", arcus.capabilities())

    # --- Position management tests -------------------------------------

    def test_arcus_capabilities_advertise_position_management(self):
        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        caps = arcus.capabilities()
        for op in ("positions_management", "set_tp", "set_sl", "close_position"):
            self.assertIn(op, caps, f"missing capability: {op}")

    def test_arcus_op_tpsl_is_op4(self):
        """Plain TPSL place uses op=4 (OpPlaceUntriggered) per Arcus auth spec.

        Distinct from op=1 (place) so signatures don't cross-replay between
        TPSL and plain placeOrder. This constant is the single source of
        truth for the typed payload's `op` field when placing TPSL.
        """
        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        self.assertEqual(arcus._ARCUS_OP_TPSL, 4)

    def test_arcus_typed_tpsl_payload_has_op4_and_positive_qty(self):
        """The TPSL typed payload uses op=4 and a positive q (the engine
        resizes to the full position at trigger time when isPositionTPSL=true;
        Arcus's signature verification rejects q=0)."""
        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        from decimal import Decimal
        creds = {"wallet": "0x" + "11" * 20, "api_signing_key": "x",
                 "private_key_hex": "00" * 32, "account_index": 0}
        # Build the typed payload the way set_tp would (side="sell" for a long
        # position; client_id non-empty so `c` is included; positive qty).
        typed = arcus._build_arcus_typed_payload_place(
            credentials=creds, market_id=1, price_ticks=74000, qty_quantums=57470338,
            side="sell", time_in_force="gtt", good_til_time_us=1788986953972341,
            timestamp_ns=1785530953972340120, reduce_only=True,
            client_id="arcus-tp-test",
        )
        typed["op"] = arcus._ARCUS_OP_TPSL  # what _execute_set_tp does
        self.assertEqual(typed["op"], 4)
        self.assertEqual(typed["q"], 57470338)
        self.assertGreater(typed["q"], 0, "typed payload q must be positive")
        # Same field set as op=1 except `op` is 4.
        self.assertIn("ad", typed)
        self.assertIn("ai", typed)
        self.assertIn("ct", typed)
        self.assertIn("g", typed)
        self.assertIn("m", typed)
        self.assertIn("p", typed)
        self.assertIn("r", typed)
        self.assertIn("s", typed)
        self.assertIn("t", typed)
        self.assertIn("v", typed)
        # `c` should be present when clientId is non-empty.
        self.assertEqual(typed["c"], "arcus-tp-test")

    def test_arcus_normalize_tpsl_side_inverts_position_side(self):
        """A TPSL closes the position when triggered: long→SELL, short→BUY."""
        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        self.assertEqual(arcus._arcus_normalize_tpsl_side("long"), "sell")
        self.assertEqual(arcus._arcus_normalize_tpsl_side("short"), "buy")

    def test_arcus_find_existing_tpsl_matches_take_profit_and_stop_loss(self):
        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        orders = [
            {"orderId": "a", "marketDisplayName": "BTC-USD", "type": "TAKE_PROFIT", "side": "SELL"},
            {"orderId": "b", "marketDisplayName": "BTC-USD", "type": "STOP_LOSS",  "side": "SELL"},
            {"orderId": "c", "marketDisplayName": "ETH-USD", "type": "TAKE_PROFIT", "side": "SELL"},
            {"orderId": "d", "marketDisplayName": "BTC-USD", "type": "LIMIT",      "side": "SELL"},
        ]
        tp = arcus._arcus_find_existing_tpsl(orders, "BTC-USD", "TP")
        self.assertEqual(tp["orderId"], "a")
        sl = arcus._arcus_find_existing_tpsl(orders, "BTC-USD", "SL")
        self.assertEqual(sl["orderId"], "b")
        # Wrong symbol — no rows match.
        self.assertIsNone(arcus._arcus_find_existing_tpsl(orders, "SOL-USD", "TP"))
        # Unknown class — fail loudly rather than silently returning a wrong
        # row (a previous version of this function mapped "INVALID" → "SL"
        # because the else branch always picked STOP_LOSS — bug).
        with self.assertRaises(ValueError):
            arcus._arcus_find_existing_tpsl(orders, "BTC-USD", "INVALID")

    def test_arcus_execute_set_tp_requires_symbol_and_price(self):
        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"
        os.environ["ARCUS_AMIROO_PRIVATE_KEY"] = "00" * 32
        r1 = arcus.execute({"operation": "set_tp", "exchange": "arcus", "account": "amiroo"})
        self.assertFalse(r1.success)
        self.assertEqual(r1.error.code, "MISSING_SYMBOL")
        r2 = arcus.execute({"operation": "set_tp", "exchange": "arcus",
                            "account": "amiroo", "symbol": "BTC-USD"})
        self.assertFalse(r2.success)
        self.assertEqual(r2.error.code, "INVALID_TP_PRICE")

    def test_arcus_execute_set_sl_requires_symbol_and_price(self):
        arcus = __import__("plugins.trade.agents.x_arcus_agent", fromlist=["*"])
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"
        os.environ["ARCUS_AMIROO_PRIVATE_KEY"] = "00" * 32
        r1 = arcus.execute({"operation": "set_sl", "exchange": "arcus", "account": "amiroo"})
        self.assertFalse(r1.success)
        self.assertEqual(r1.error.code, "MISSING_SYMBOL")
        r2 = arcus.execute({"operation": "set_sl", "exchange": "arcus",
                            "account": "amiroo", "symbol": "BTC-USD"})
        self.assertFalse(r2.success)
        self.assertEqual(r2.error.code, "INVALID_SL_PRICE")

    def test_apex_cancel_order_group_rejects_missing_symbol(self):
        """cancel_order_group must require symbol — otherwise the operator
        would mass-cancel without realising it."""
        import plugins.trade.agents.x_apex_agent as apex
        self._setUp_env()
        try:
            apex._lookup_credentials = lambda account: {
                "account": account,
                "account_id": "686607787470356535",
                "api_key": "k", "api_secret": "s", "passphrase": "p",
                "seeds": "0x" + "ab" * 32, "l2_private_key": "0x" + "cd" * 32,
            }
            try:
                resp = apex.execute({
                    "operation": "cancel_order_group", "exchange": "apex",
                    "account": "BITGET", "side": "buy",
                })
                self.assertFalse(resp.success)
                self.assertIsNotNone(resp.error)
                self.assertEqual(resp.error.code, "MISSING_SYMBOL")
            finally:
                apex._lookup_credentials = apex._lookup_credentials
        finally:
            self._tearDown_env()

    def test_apex_cancel_order_group_rejects_invalid_side(self):
        import plugins.trade.agents.x_apex_agent as apex
        self._setUp_env()
        try:
            apex._lookup_credentials = lambda account: {
                "account": account,
                "account_id": "686607787470356535",
                "api_key": "k", "api_secret": "s", "passphrase": "p",
                "seeds": "0x" + "ab" * 32, "l2_private_key": "0x" + "cd" * 32,
            }
            try:
                resp = apex.execute({
                    "operation": "cancel_order_group", "exchange": "apex",
                    "account": "BITGET", "symbol": "BTC-USDT", "side": "long",
                })
                self.assertFalse(resp.success)
                self.assertIsNotNone(resp.error)
                self.assertEqual(resp.error.code, "INVALID_SIDE")
            finally:
                apex._lookup_credentials = apex._lookup_credentials
        finally:
            self._tearDown_env()

    def test_apex_cancel_order_group_dispatch_reaches_handler(self):
        """Dispatching ``cancel_order_group`` must reach the real handler —
        the stubbed implementation returns MISSING_SYMBOL when symbol is
        absent, NOT NOT_IMPLEMENTED (which is reserved for unwired ops).
        """
        import plugins.trade.agents.x_apex_agent as apex
        self._setUp_env()
        try:
            apex._lookup_credentials = lambda account: {
                "account": account,
                "account_id": "686607787470356535",
                "api_key": "k", "api_secret": "s", "passphrase": "p",
                "seeds": "0x" + "ab" * 32, "l2_private_key": "0x" + "cd" * 32,
            }
            try:
                resp = apex.execute({
                    "operation": "cancel_order_group", "exchange": "apex",
                    "account": "BITGET",
                })
                self.assertFalse(resp.success)
                self.assertIsNotNone(resp.error)
                self.assertEqual(resp.error.code, "MISSING_SYMBOL")
            finally:
                apex._lookup_credentials = apex._lookup_credentials
        finally:
            self._tearDown_env()

    def test_apex_set_tp_rejects_missing_symbol(self):
        """set_tp must require symbol — otherwise the operator would
        configure TP on the wrong position."""
        import plugins.trade.agents.x_apex_agent as apex
        self._setUp_env()
        try:
            apex._lookup_credentials = lambda account: {
                "account": account,
                "account_id": "686607787470356535",
                "api_key": "k", "api_secret": "s", "passphrase": "p",
                "seeds": "0x" + "ab" * 32, "l2_private_key": "0x" + "cd" * 32,
            }
            try:
                resp = apex.execute({
                    "operation": "set_tp", "exchange": "apex",
                    "account": "BITGET", "price": "70000",
                })
                self.assertFalse(resp.success)
                self.assertIsNotNone(resp.error)
                self.assertEqual(resp.error.code, "MISSING_SYMBOL")
            finally:
                apex._lookup_credentials = apex._lookup_credentials
        finally:
            self._tearDown_env()

    def test_apex_set_tp_rejects_missing_price(self):
        import plugins.trade.agents.x_apex_agent as apex
        self._setUp_env()
        try:
            apex._lookup_credentials = lambda account: {
                "account": account,
                "account_id": "686607787470356535",
                "api_key": "k", "api_secret": "s", "passphrase": "p",
                "seeds": "0x" + "ab" * 32, "l2_private_key": "0x" + "cd" * 32,
            }
            try:
                resp = apex.execute({
                    "operation": "set_tp", "exchange": "apex",
                    "account": "BITGET", "symbol": "BTC-USDT",
                })
                self.assertFalse(resp.success)
                self.assertIsNotNone(resp.error)
                self.assertEqual(resp.error.code, "INVALID_TP_PRICE")
            finally:
                apex._lookup_credentials = apex._lookup_credentials
        finally:
            self._tearDown_env()

    def test_apex_set_tp_rejects_non_numeric_price(self):
        import plugins.trade.agents.x_apex_agent as apex
        self._setUp_env()
        try:
            apex._lookup_credentials = lambda account: {
                "account": account,
                "account_id": "686607787470356535",
                "api_key": "k", "api_secret": "s", "passphrase": "p",
                "seeds": "0x" + "ab" * 32, "l2_private_key": "0x" + "cd" * 32,
            }
            try:
                resp = apex.execute({
                    "operation": "set_tp", "exchange": "apex",
                    "account": "BITGET", "symbol": "BTC-USDT",
                    "price": "not-a-number",
                })
                self.assertFalse(resp.success)
                self.assertIsNotNone(resp.error)
                self.assertEqual(resp.error.code, "INVALID_TP_PRICE")
            finally:
                apex._lookup_credentials = apex._lookup_credentials
        finally:
            self._tearDown_env()

    def test_apex_set_sl_rejects_missing_symbol(self):
        import plugins.trade.agents.x_apex_agent as apex
        self._setUp_env()
        try:
            apex._lookup_credentials = lambda account: {
                "account": account,
                "account_id": "686607787470356535",
                "api_key": "k", "api_secret": "s", "passphrase": "p",
                "seeds": "0x" + "ab" * 32, "l2_private_key": "0x" + "cd" * 32,
            }
            try:
                resp = apex.execute({
                    "operation": "set_sl", "exchange": "apex",
                    "account": "BITGET", "price": "50000",
                })
                self.assertFalse(resp.success)
                self.assertIsNotNone(resp.error)
                self.assertEqual(resp.error.code, "MISSING_SYMBOL")
            finally:
                apex._lookup_credentials = apex._lookup_credentials
        finally:
            self._tearDown_env()

    def test_apex_set_sl_rejects_missing_price(self):
        import plugins.trade.agents.x_apex_agent as apex
        self._setUp_env()
        try:
            apex._lookup_credentials = lambda account: {
                "account": account,
                "account_id": "686607787470356535",
                "api_key": "k", "api_secret": "s", "passphrase": "p",
                "seeds": "0x" + "ab" * 32, "l2_private_key": "0x" + "cd" * 32,
            }
            try:
                resp = apex.execute({
                    "operation": "set_sl", "exchange": "apex",
                    "account": "BITGET", "symbol": "BTC-USDT",
                })
                self.assertFalse(resp.success)
                self.assertIsNotNone(resp.error)
                self.assertEqual(resp.error.code, "INVALID_SL_PRICE")
            finally:
                apex._lookup_credentials = apex._lookup_credentials
        finally:
            self._tearDown_env()

    def test_apex_close_position_rejects_missing_symbol(self):
        import plugins.trade.agents.x_apex_agent as apex
        self._setUp_env()
        try:
            apex._lookup_credentials = lambda account: {
                "account": account,
                "account_id": "686607787470356535",
                "api_key": "k", "api_secret": "s", "passphrase": "p",
                "seeds": "0x" + "ab" * 32, "l2_private_key": "0x" + "cd" * 32,
            }
            try:
                resp = apex.execute({
                    "operation": "close_position", "exchange": "apex",
                    "account": "BITGET",
                })
                self.assertFalse(resp.success)
                self.assertIsNotNone(resp.error)
                self.assertEqual(resp.error.code, "MISSING_SYMBOL")
            finally:
                apex._lookup_credentials = apex._lookup_credentials
        finally:
            self._tearDown_env()

    def test_apex_positions_management_dispatches_to_positions_orders(self):
        """positions_management must reach the real handler. With stubbed
        credentials it fails before the SDK call — the failure code is
        APEX_ERROR (network), NOT NOT_IMPLEMENTED."""
        import plugins.trade.agents.x_apex_agent as apex
        self._setUp_env()
        try:
            apex._lookup_credentials = lambda account: {
                "account": account,
                "account_id": "686607787470356535",
                "api_key": "k", "api_secret": "s", "passphrase": "p",
                "seeds": "0x" + "ab" * 32, "l2_private_key": "0x" + "cd" * 32,
            }
            try:
                resp = apex.execute({
                    "operation": "positions_management", "exchange": "apex",
                    "account": "BITGET",
                })
                # The crucial assertion is that we reached the real handler
                # rather than falling through to NOT_IMPLEMENTED. Depending on
                # the local Apex SDK fixture state this may fail or succeed.
                self.assertIsNotNone(resp)
                if not resp.success:
                    self.assertIsNotNone(resp.error)
                    self.assertNotEqual(resp.error.code, "NOT_IMPLEMENTED")
            finally:
                apex._lookup_credentials = apex._lookup_credentials
        finally:
            self._tearDown_env()

    def test_apex_set_tp_dispatches_to_handler(self):
        """set_tp must reach the real handler — with stub creds it fails
        with INVALID_TP_PRICE if missing, otherwise APEX_ERROR (network)."""
        import plugins.trade.agents.x_apex_agent as apex
        self._setUp_env()
        try:
            apex._lookup_credentials = lambda account: {
                "account": account,
                "account_id": "686607787470356535",
                "api_key": "k", "api_secret": "s", "passphrase": "p",
                "seeds": "0x" + "ab" * 32, "l2_private_key": "0x" + "cd" * 32,
            }
            try:
                # With both symbol and price present, the call gets past
                # validation. With stub creds the network call fails, so we
                # expect APEX_ERROR (not NOT_IMPLEMENTED).
                resp = apex.execute({
                    "operation": "set_tp", "exchange": "apex",
                    "account": "BITGET", "symbol": "BTC-USDT",
                    "price": "70000",
                })
                self.assertFalse(resp.success)
                self.assertIsNotNone(resp.error)
                self.assertNotEqual(resp.error.code, "NOT_IMPLEMENTED")
            finally:
                apex._lookup_credentials = apex._lookup_credentials
        finally:
            self._tearDown_env()

    def test_apex_set_sl_dispatches_to_handler(self):
        import plugins.trade.agents.x_apex_agent as apex
        self._setUp_env()
        try:
            apex._lookup_credentials = lambda account: {
                "account": account,
                "account_id": "686607787470356535",
                "api_key": "k", "api_secret": "s", "passphrase": "p",
                "seeds": "0x" + "ab" * 32, "l2_private_key": "0x" + "cd" * 32,
            }
            try:
                resp = apex.execute({
                    "operation": "set_sl", "exchange": "apex",
                    "account": "BITGET", "symbol": "BTC-USDT",
                    "price": "50000",
                })
                self.assertFalse(resp.success)
                self.assertIsNotNone(resp.error)
                self.assertNotEqual(resp.error.code, "NOT_IMPLEMENTED")
            finally:
                apex._lookup_credentials = apex._lookup_credentials
        finally:
            self._tearDown_env()

    def test_apex_close_position_dispatches_to_handler(self):
        import plugins.trade.agents.x_apex_agent as apex
        self._setUp_env()
        try:
            apex._lookup_credentials = lambda account: {
                "account": account,
                "account_id": "686607787470356535",
                "api_key": "k", "api_secret": "s", "passphrase": "p",
                "seeds": "0x" + "ab" * 32, "l2_private_key": "0x" + "cd" * 32,
            }
            try:
                resp = apex.execute({
                    "operation": "close_position", "exchange": "apex",
                    "account": "BITGET", "symbol": "BTC-USDT",
                })
                self.assertFalse(resp.success)
                self.assertIsNotNone(resp.error)
                self.assertNotEqual(resp.error.code, "NOT_IMPLEMENTED")
            finally:
                apex._lookup_credentials = apex._lookup_credentials
        finally:
            self._tearDown_env()

    def test_apex_capabilities_advertise_position_management(self):
        """The Apex capabilities list must include positions_management,
        set_tp, set_sl, close_position — they are all implemented."""
        import plugins.trade.agents.x_apex_agent as apex
        caps = apex.capabilities()
        for op in ("positions_management", "set_tp", "set_sl", "close_position"):
            self.assertIn(op, caps, f"missing capability: {op}")

    def test_apex_closing_side_inverts_position_side(self):
        """_apex_closing_side must return SELL for longs, BUY for shorts —
        otherwise the close order would open a new position instead of
        closing the existing one."""
        import plugins.trade.agents.x_apex_agent as apex
        self.assertEqual(apex._apex_closing_side("long"), "sell")
        self.assertEqual(apex._apex_closing_side("short"), "buy")

    def test_apex_position_side_reads_known_signals(self):
        """_apex_position_side must map Apex's side/posSide values to
        the canonical ``long``/``short`` strings the rest of the code
        uses."""
        import plugins.trade.agents.x_apex_agent as apex
        self.assertEqual(apex._apex_position_side({"side": "LONG"}), "long")
        self.assertEqual(apex._apex_position_side({"side": "SHORT"}), "short")
        self.assertEqual(apex._apex_position_side({"posSide": "LONG"}), "long")
        self.assertEqual(apex._apex_position_side({"posSide": "SHORT"}), "short")
        # Size sign fallback: positive = long, negative = short
        self.assertEqual(apex._apex_position_side({"size": "1.5"}), "long")
        self.assertEqual(apex._apex_position_side({"size": "-0.5"}), "short")
        self.assertEqual(apex._apex_position_side({}), "long")  # default

    def test_apex_position_size_returns_absolute_value(self):
        import plugins.trade.agents.x_apex_agent as apex
        from decimal import Decimal
        self.assertEqual(apex._apex_position_size({"size": "1.5"}), Decimal("1.5"))
        self.assertEqual(apex._apex_position_size({"size": "-0.5"}), Decimal("0.5"))
        self.assertEqual(apex._apex_position_size({}), Decimal("0"))

    def test_apex_normalize_meta_picks_required_fields(self):
        import plugins.trade.agents.x_apex_agent as apex
        from decimal import Decimal
        meta = apex._apex_normalize_meta({
            "symbol": "BTC-USDT",
            "tickSize": "0.1",
            "stepSize": "0.001",
            "lotSize": "0.0005",
            "minOrderSize": "0.001",
            "irrelevant_field": "ignored",
        })
        self.assertEqual(meta["symbol"], "BTC-USDT")
        self.assertEqual(meta["tick_size"], Decimal("0.1"))
        # stepSize wins over lotSize (lotSize is fallback)
        self.assertEqual(meta["step_size"], Decimal("0.001"))
        self.assertEqual(meta["min_order_size"], Decimal("0.001"))
        # Falls back to lotSize when stepSize is absent
        meta_no_step = apex._apex_normalize_meta({
            "symbol": "SOL-USDT", "lotSize": "0.1", "tickSize": "0.01",
        })
        self.assertEqual(meta_no_step["step_size"], Decimal("0.1"))

    def test_apex_ladder_3btc_50orders_not_rejected_by_unit_mismatch(self):
        """Regression: 3 BTC total volume across 50 orders between 60k-62k
        must NOT be rejected by the dispatcher-level preflight (the previous
        preflight incorrectly compared total_volume in instruments to a USD
        floor, so 3 BTC ~= $190k USD was wrongly rejected as 'too small').

        The dispatcher will fail later for OTHER reasons (no real SDK
        client is stubbed here) but the error code must NOT be
        INSUFFICIENT_VOLUME_FOR_ORDER_COUNT — that proves the preflight
        was removed and the correct per-child price * size check is left
        to the child-builder.
        """
        import plugins.trade.agents.x_apex_agent as apex
        self._setUp_env()
        try:
            apex._lookup_credentials = lambda account: {
                "account": account,
                "account_id": "686607787470356535",
                "api_key": "k", "api_secret": "s", "passphrase": "p",
                "seeds": "0x" + "ab" * 32, "l2_private_key": "0x" + "cd" * 32,
            }
            try:
                # No _client_for_credentials stub — the dispatcher will fail
                # somewhere AFTER the preflight (e.g. CONFIGURATION_ERROR or
                # bootstrap failure). The point is the preflight didn't fire.
                resp = apex.execute({
                    "operation": "ladder",
                    "exchange": "apex",
                    "account": "BITGET",
                    "symbol": "BTC",
                    "side": "buy",
                    "distribution": "half_gaussian",
                    "order_count": 50,
                    "total_volume": "3",
                    "start_price": "62000",
                    "end_price": "60000",
                })
                # We're not asserting success — there's no SDK client.
                # We only assert the preflight bad code did NOT surface.
                if not resp.success and resp.error is not None:
                    self.assertNotEqual(
                        resp.error.code, "INSUFFICIENT_VOLUME_FOR_ORDER_COUNT",
                        f"Dispatcher preflight unit-mismatch regression: "
                        f"3 BTC ladder wrongly rejected as {resp.error.code}",
                    )
            finally:
                apex._lookup_credentials = apex._lookup_credentials
        finally:
            self._tearDown_env()

    def test_apex_ladder_child_builder_keeps_instrument_units(self):
        """total_volume is in INSTRUMENTS (e.g. BTC), not USD. Verify the
        child builder distributes total_volume as base-asset sizes —
        not as USD-denominated sizes."""
        import plugins.trade.agents.x_apex_agent as apex
        # 3 BTC across 10 orders at $60k: each child ~0.3 BTC (notional ~$18k)
        children, kept_volume, omitted, kept_count = apex._apex_build_ladder_children(
            symbol="BTC-USDT",
            side="buy",
            distribution="half_gaussian",
            order_count=10,
            total_volume=Decimal("3"),
            start_price=Decimal("60000"),
            end_price=Decimal("62000"),
            size_increment=Decimal("0.001"),
            price_increment=Decimal("0.1"),
            min_order_size=Decimal("0.001"),
        )
        self.assertEqual(kept_count, 10)
        self.assertEqual(omitted, 0)
        # sum of sizes should equal total_volume (in BTC)
        total_size = sum(Decimal(c["size"]) for c in children)
        self.assertAlmostEqual(float(total_size), 3.0, places=2)
        # each child notional = price * size must be > $10 (the per-child floor)
        for c in children:
            size = Decimal(c["size"])
            notional = Decimal(c["price"]) * size
            self.assertGreater(notional, Decimal("10"))

    def test_apex_ladder_child_builder_drops_sub_floor_children(self):
        """Per-child notional floor (price * size < $10) must still be
        enforced when total_volume is too small to give every child a
        $10 USD notional. This proves the per-child USD check is intact.

        5 SOL across 50 orders at $90 = 0.1 SOL each = $9 each -> all sub-floor.
        """
        import plugins.trade.agents.x_apex_agent as apex
        children, kept_volume, omitted, kept_count = apex._apex_build_ladder_children(
            symbol="SOL-USDT",
            side="buy",
            distribution="uniform",
            order_count=50,
            total_volume=Decimal("5"),
            start_price=Decimal("90"),
            end_price=Decimal("91"),
            size_increment=Decimal("0.001"),
            price_increment=Decimal("0.01"),
            min_order_size=Decimal("0.001"),
        )
        # Per-child notional floor drops sub-floor children
        self.assertGreater(omitted, 0,
                           f"Per-child USD notional floor should drop sub-floor children; "
                           f"got kept_count={kept_count}, omitted={omitted}")

    def test_apex_ladder_sol_100sol_50orders_not_rejected(self):
        """Same regression for SOL: 100 SOL across 50 orders must not
        be rejected by the unit-mismatch preflight."""
        import plugins.trade.agents.x_apex_agent as apex
        self._setUp_env()
        try:
            apex._lookup_credentials = lambda account: {
                "account": account,
                "account_id": "686607787470356535",
                "api_key": "k", "api_secret": "s", "passphrase": "p",
                "seeds": "0x" + "ab" * 32, "l2_private_key": "0x" + "cd" * 32,
            }
            try:
                resp = apex.execute({
                    "operation": "ladder",
                    "exchange": "apex",
                    "account": "BITGET",
                    "symbol": "SOL",
                    "side": "buy",
                    "distribution": "uniform",
                    "order_count": 50,
                    "total_volume": "100",
                    "start_price": "150",
                    "end_price": "140",
                })
                if not resp.success and resp.error is not None:
                    self.assertNotEqual(
                        resp.error.code, "INSUFFICIENT_VOLUME_FOR_ORDER_COUNT",
                        f"Dispatcher preflight unit-mismatch regression: "
                        f"100 SOL ladder wrongly rejected as {resp.error.code}",
                    )
            finally:
                apex._lookup_credentials = apex._lookup_credentials
        finally:
            self._tearDown_env()

    def test_apex_sell_half_gaussian_sizes_increase_from_start_to_end(self):
        import plugins.trade.agents.x_apex_agent as apex
        children, kept_volume, omitted, kept_count = apex._apex_build_ladder_children(
            symbol="BTC-USDT",
            side="sell",
            distribution="half_gaussian",
            order_count=6,
            total_volume=Decimal("2.1"),
            start_price=Decimal("1000"),
            end_price=Decimal("1005"),
            size_increment=Decimal("0.01"),
            price_increment=Decimal("0.1"),
            min_order_size=Decimal("0"),
        )
        self.assertEqual(kept_count, 6)
        self.assertEqual(omitted, 0)
        prices = [Decimal(c["price"]) for c in children]
        sizes = [Decimal(c["size"]) for c in children]
        self.assertEqual(prices, sorted(prices))
        self.assertTrue(all(a <= b for a, b in zip(sizes, sizes[1:])), sizes)
        self.assertLess(sizes[0], sizes[-1])
        self.assertEqual(sum(sizes), kept_volume)
        self.assertEqual(sum(sizes), Decimal("2.1"))

    def test_apex_buy_half_gaussian_progression_remains_increasing_along_start_to_end(self):
        import plugins.trade.agents.x_apex_agent as apex
        children, kept_volume, omitted, kept_count = apex._apex_build_ladder_children(
            symbol="SOL-USDT",
            side="buy",
            distribution="half_gaussian",
            order_count=6,
            total_volume=Decimal("2.1"),
            start_price=Decimal("1005"),
            end_price=Decimal("1000"),
            size_increment=Decimal("0.01"),
            price_increment=Decimal("0.1"),
            min_order_size=Decimal("0"),
        )
        self.assertEqual(kept_count, 6)
        self.assertEqual(omitted, 0)
        prices = [Decimal(c["price"]) for c in children]
        sizes = [Decimal(c["size"]) for c in children]
        self.assertEqual(prices, sorted(prices, reverse=True))
        self.assertTrue(all(a <= b for a, b in zip(sizes, sizes[1:])), sizes)
        self.assertLess(sizes[0], sizes[-1])
        self.assertEqual(sum(sizes), kept_volume)
        self.assertEqual(sum(sizes), Decimal("2.1"))

    def test_apex_uniform_buy_and_sell_sizes_remain_equal_subject_to_rounding(self):
        import plugins.trade.agents.x_apex_agent as apex
        for side in ("buy", "sell"):
            children, kept_volume, omitted, kept_count = apex._apex_build_ladder_children(
                symbol="BTC-USDT",
                side=side,
                distribution="uniform",
                order_count=5,
                total_volume=Decimal("1.0"),
                start_price=(Decimal("1005") if side == "buy" else Decimal("1000")),
                end_price=(Decimal("1000") if side == "buy" else Decimal("1005")),
                size_increment=Decimal("0.01"),
                price_increment=Decimal("0.1"),
                min_order_size=Decimal("0"),
            )
            self.assertEqual(kept_count, 5)
            self.assertEqual(omitted, 0)
            sizes = [Decimal(c["size"]) for c in children]
            self.assertLessEqual(max(sizes) - min(sizes), Decimal("0.1"), (side, sizes))
            self.assertEqual(sum(sizes), kept_volume)
            self.assertEqual(sum(sizes), Decimal("1.0"))

    def test_apex_batch_models_preserve_ladder_builder_orientation(self):
        import plugins.trade.agents.x_apex_agent as apex
        children, _, _, _ = apex._apex_build_ladder_children(
            symbol="BTC-USDT",
            side="sell",
            distribution="half_gaussian",
            order_count=6,
            total_volume=Decimal("2.1"),
            start_price=Decimal("1000"),
            end_price=Decimal("1005"),
            size_increment=Decimal("0.01"),
            price_increment=Decimal("0.1"),
            min_order_size=Decimal("0"),
        )
        models = [
            apex._ApexLadderOrder(
                symbol=c["symbol"],
                side=c["side"],
                price=c["price"],
                size=c["size"],
                client_id=c["client_id"],
            )
            for c in children
        ]
        self.assertEqual(
            [(m.price, m.size) for m in models],
            [(c["price"], c["size"]) for c in children],
        )

    def test_arcus_ladder_3btc_50orders_not_rejected_by_unit_mismatch(self):
        """Same regression for Arcus: 3 BTC total volume across 50 orders
        must NOT be rejected by the dispatcher-level preflight (which had
        the same unit-mismatch bug)."""
        import plugins.trade.agents.x_arcus_agent as arcus
        os.environ["ARCUS_AMIROO_WALLET"] = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        os.environ["ARCUS_AMIROO_APISIGNINGKEY"] = "secret-a"
        os.environ["ARCUS_AMIROO_PRIVATE_KEY"] = "00" * 32
        # Stub the network-touching bits so the dispatcher fails AFTER
        # the preflight (any failure except INSUFFICIENT_VOLUME_FOR_ORDER_COUNT).
        original_resolve = getattr(arcus, "_resolve_market", None)
        original_signed = getattr(arcus, "_signed_post", None)
        arcus._resolve_market = lambda symbol: {"market_id": 1, "tick_size": "0.1",
                                                "step_size": "0.001",
                                                "size_precision": 3,
                                                "price_precision": 1,
                                                "min_notional": "10"}
        arcus._signed_post = lambda *a, **kw: {"status": "ok"}
        try:
            resp = arcus.execute({
                "operation": "ladder",
                "exchange": "arcus",
                "account": "amiroo",
                "symbol": "BTC-USD",
                "side": "buy",
                "distribution": "half_gaussian",
                "order_count": 50,
                "total_volume": "3",
                "start_price": "62000",
                "end_price": "60000",
            })
            if not resp.success and resp.error is not None:
                self.assertNotEqual(
                    resp.error.code, "INSUFFICIENT_VOLUME_FOR_ORDER_COUNT",
                    f"Arcus dispatcher preflight unit-mismatch regression: "
                    f"3 BTC ladder wrongly rejected as {resp.error.code}",
                )
        finally:
            if original_resolve is not None:
                arcus._resolve_market = original_resolve
            if original_signed is not None:
                arcus._signed_post = original_signed
            for key in ("ARCUS_AMIROO_WALLET", "ARCUS_AMIROO_APISIGNINGKEY",
                        "ARCUS_AMIROO_PRIVATE_KEY"):
                os.environ.pop(key, None)

    def test_arcus_ladder_child_builder_keeps_instrument_units(self):
        """Arcus analog: total_volume is in instruments; the child builder
        distributes as base-asset sizes, not USD."""
        import plugins.trade.agents.x_arcus_agent as arcus
        children, kept_volume, omitted_below_minimum, kept_count = \
            arcus._build_arcus_ladder_children(
                credentials={"wallet": "0xabc", "account_index": 0},
                market_id=1,
                price_increment=Decimal("0.1"),
                size_increment=Decimal("0.001"),
                side="buy",
                distribution="half_gaussian",
                order_count=10,
                total_volume=Decimal("3"),
                start_price=Decimal("60000"),
                end_price=Decimal("62000"),
                size_precision=3,
                price_precision=1,
                min_notional=Decimal("10"),
                batch_id_prefix="test",
            )
        self.assertEqual(kept_count, 10)
        # sum of sizes should equal total_volume (in BTC)
        total_size = sum(Decimal(c["size"]) for c in children)
        self.assertAlmostEqual(float(total_size), 3.0, places=2)
        for c in children:
            size = Decimal(c["size"])
            notional = Decimal(c["price"]) * size
            self.assertGreater(notional, Decimal("10"))

    def test_apex_order_tpsl_kind_uses_trigger_price_relation(self):
        """_apex_order_tpsl_kind must return 'TP' when trigger > price
        (closing the position above market), 'SL' when trigger < price.
        This is the only reliable signal for orders that don't carry
        an explicit tpslType."""
        import plugins.trade.agents.x_apex_agent as apex
        self.assertEqual(
            apex._apex_order_tpsl_kind({"triggerPrice": "70000", "price": "69999"}),
            "TP",
        )
        self.assertEqual(
            apex._apex_order_tpsl_kind({"triggerPrice": "50000", "price": "50001"}),
            "SL",
        )
        # Explicit tpslType wins
        self.assertEqual(
            apex._apex_order_tpsl_kind({"tpslType": "SL", "triggerPrice": "70000", "price": "69999"}),
            "SL",
        )
        # No triggerPrice → None (not a TP/SL)
        self.assertIsNone(apex._apex_order_tpsl_kind({"price": "100"}))
        self.assertIsNone(apex._apex_order_tpsl_kind({}))


def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
