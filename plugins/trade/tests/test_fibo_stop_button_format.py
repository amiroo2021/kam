"""Phase 2.13.x — focused tests for the compact Stop Fibo button label.

Verifies:

  * Emoji mapping for all four (variant, side) combinations:
      - (NORMALFIB, SELL)  → 🔴
      - (FASTFIB,   SELL)  → 🔴🔴
      - (NORMALFIB, BUY)   → 🔵
      - (FASTFIB,   BUY)   → 🔵🔵
      - unknown variant/side → ⚪ (defensive)

  * USD-stripping for ordinary MT4 source symbols:
      SOLUSD → SOL
      ETHUSD → ETH
      BTCUSD → BTC
      XAUUSD → XAU
      Non-USD symbols unchanged: SOL → SOL, BTC → BTC.
      Symbols ending in USD-like substrings that don't match
      the suffix set are unchanged.

  * The label does NOT modify the underlying registration
    object or its registration_key/source_symbol.

  * The label format is ``<emoji> <symbol> / <Exchange> /
    <Account>`` with the first letter of Exchange/Account
    capitalized.

  * The picker screen produces buttons with this label AND
    unchanged callback_data (``fibo:stop:p:<idx>``).

The tests do NOT make exchange calls. They use a stub
FiboRegistration with the relevant fields set.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from unittest import mock

# Make sure both /usr/local/lib/hermes-agent and /root/kam are
# importable so we can request the fibo_wizard from the deployed
# runtime (since the wizard module lives in the Hermes tree).
sys.path.insert(0, "/usr/local/lib/hermes-agent")
sys.path.insert(0, "/root/kam")


def _make_reg(
    source_symbol: str = "SOLUSD",
    exchange: str = "hyperliquid",
    account: str = "BASED",
    variant: str = "NORMALFIB",
    side: str = "SELL",
    registration_key: str | None = None,
    exchange_instrument: str = "SOL",
):
    """Build a stub object exposing the attributes that
    ``_stop_button_label`` reads.

    We don't need a full FiboRegistration; the helper only
    accesses ``source_symbol``, ``symbol``, ``variant``,
    ``side``, ``exchange``, ``account``. We use ``types.SimpleNamespace``
    so the test is independent of the production store dataclass.
    """
    from types import SimpleNamespace

    if registration_key is None:
        registration_key = (
            f"{exchange}/{account}/{source_symbol}/{variant}/{side}"
        )
    return SimpleNamespace(
        source_symbol=source_symbol,
        symbol=source_symbol,
        exchange=exchange,
        account=account,
        variant=variant,
        side=side,
        exchange_instrument=exchange_instrument,
        registration_key=registration_key,
    )


def _load_wizard_module():
    """Load the fibo_wizard module freshly so we pick up the
    newly-saved helpers (in case another test cached an older
    import).
    """
    if "plugins.trade.fibo_wizard" in sys.modules:
        del sys.modules["plugins.trade.fibo_wizard"]
    return importlib.import_module("plugins.trade.fibo_wizard")


class StopButtonLabelEmojiTests(unittest.TestCase):
    """All four (variant, side) emoji mappings."""

    def setUp(self) -> None:
        self.wiz = _load_wizard_module()

    def test_normalfib_sell_red(self) -> None:
        reg = _make_reg(variant="NORMALFIB", side="SELL")
        label = self.wiz._stop_button_label(reg)
        self.assertTrue(label.startswith("🔴 "),
                        f"expected red emoji prefix, got {label!r}")

    def test_fastfib_sell_double_red(self) -> None:
        reg = _make_reg(variant="FASTFIB", side="SELL")
        label = self.wiz._stop_button_label(reg)
        self.assertTrue(label.startswith("🔴🔴 "),
                        f"expected double-red emoji prefix, got {label!r}")

    def test_normalfib_buy_blue(self) -> None:
        reg = _make_reg(variant="NORMALFIB", side="BUY")
        label = self.wiz._stop_button_label(reg)
        self.assertTrue(label.startswith("🔵 "),
                        f"expected blue emoji prefix, got {label!r}")

    def test_fastfib_buy_double_blue(self) -> None:
        reg = _make_reg(variant="FASTFIB", side="BUY")
        label = self.wiz._stop_button_label(reg)
        self.assertTrue(label.startswith("🔵🔵 "),
                        f"expected double-blue emoji prefix, got {label!r}")

    def test_unknown_variant_side_uses_defensive_white(self) -> None:
        reg = _make_reg(variant="UNKNOWN", side="SELL")
        label = self.wiz._stop_button_label(reg)
        self.assertTrue(label.startswith("⚪ "),
                        f"expected defensive ⚪ for unknown variant, got {label!r}")

    def test_lowercase_variant_and_side_normalized(self) -> None:
        """The label should treat 'normalfib' and 'sell' (lowercase)
        equivalently to 'NORMALFIB' and 'SELL'."""
        reg = _make_reg(variant="normalfib", side="sell")
        label = self.wiz._stop_button_label(reg)
        self.assertTrue(label.startswith("🔴 "),
                        f"expected normalized red emoji, got {label!r}")


class StopButtonLabelUSDStrippingTests(unittest.TestCase):
    """USD-stripping for display-only purposes."""

    def setUp(self) -> None:
        self.wiz = _load_wizard_module()

    def test_solusd_strips_to_sol(self) -> None:
        self.assertEqual(self.wiz._strip_usd_for_display("SOLUSD"), "SOL")

    def test_ethusd_strips_to_eth(self) -> None:
        self.assertEqual(self.wiz._strip_usd_for_display("ETHUSD"), "ETH")

    def test_btcusd_strips_to_btc(self) -> None:
        self.assertEqual(self.wiz._strip_usd_for_display("BTCUSD"), "BTC")

    def test_xauusd_strips_to_xau(self) -> None:
        self.assertEqual(self.wiz._strip_usd_for_display("XAUUSD"), "XAU")

    def test_non_usd_symbols_unchanged(self) -> None:
        for s in ("SOL", "BTC", "ETH", "XAU", "EURUSDx", "USDEUR"):
            if s.endswith("USD") and len(s) > 3:
                # Like EURUSDx doesn't end with USD; USDEUR ends with
                # EUR not USD; those should not be stripped.
                continue
            self.assertEqual(
                self.wiz._strip_usd_for_display(s), s,
                f"unexpectedly stripped {s!r}",
            )

    def test_empty_string_returns_empty(self) -> None:
        self.assertEqual(self.wiz._strip_usd_for_display(""), "")

    def test_usd_only_returns_usd_unchanged(self) -> None:
        """A symbol equal to 'USD' should NOT be stripped (the
        function guards against stripping the entire symbol,
        which would leave an empty display string).
        """
        self.assertEqual(self.wiz._strip_usd_for_display("USD"), "USD")


class StopButtonLabelFormatTests(unittest.TestCase):
    """Full label format <emoji> <symbol> / <Exchange> / <Account>."""

    def setUp(self) -> None:
        self.wiz = _load_wizard_module()

    def test_full_label_sol_normalfib_sell_hyperliquid(self) -> None:
        """Example from the spec:
        SOLUSD + NORMALFIB + SELL + hyperliquid/BASED
        → 🔴 SOL / Hyperliquid / Based
        """
        reg = _make_reg(
            source_symbol="SOLUSD",
            exchange="hyperliquid",
            account="BASED",
            variant="NORMALFIB",
            side="SELL",
        )
        self.assertEqual(
            self.wiz._stop_button_label(reg),
            "🔴 SOL / Hyperliquid / Based",
        )

    def test_full_label_sol_fastfib_sell_hyperliquid(self) -> None:
        reg = _make_reg(
            source_symbol="SOLUSD",
            exchange="hyperliquid",
            account="BASED",
            variant="FASTFIB",
            side="SELL",
        )
        self.assertEqual(
            self.wiz._stop_button_label(reg),
            "🔴🔴 SOL / Hyperliquid / Based",
        )

    def test_full_label_eth_normalfib_buy_ondoperps(self) -> None:
        reg = _make_reg(
            source_symbol="ETHUSD",
            exchange="ondoperps",
            account="BITGET",
            variant="NORMALFIB",
            side="BUY",
        )
        self.assertEqual(
            self.wiz._stop_button_label(reg),
            "🔵 ETH / OndoPerps / Bitget",
        )

    def test_full_label_eth_fastfib_buy_ondoperps(self) -> None:
        reg = _make_reg(
            source_symbol="ETHUSD",
            exchange="ondoperps",
            account="BITGET",
            variant="FASTFIB",
            side="BUY",
        )
        self.assertEqual(
            self.wiz._stop_button_label(reg),
            "🔵🔵 ETH / OndoPerps / Bitget",
        )

    def test_label_does_not_modify_source_symbol(self) -> None:
        """The label renderer must NOT mutate the registration's
        source_symbol or registration_key.
        """
        reg = _make_reg(
            source_symbol="ETHUSD",
            exchange="ondoperps",
            account="BITGET",
            variant="NORMALFIB",
            side="BUY",
            registration_key="ondoperps/BITGET/ETH-USD.P/NORMALFIB/BUY",
        )
        before = (
            reg.source_symbol,
            reg.symbol,
            reg.registration_key,
            reg.exchange_instrument,
        )
        self.wiz._stop_button_label(reg)
        after = (
            reg.source_symbol,
            reg.symbol,
            reg.registration_key,
            reg.exchange_instrument,
        )
        self.assertEqual(before, after,
                         f"label renderer mutated registration: before={before} after={after}")


class StopPickerScreenButtonTests(unittest.TestCase):
    """The picker screen buttons must use the compact label and
    keep callback_data as ``fibo:stop:p:<idx>`` so clicking stops
    the correct registration by index (the index is keyed by
    registration_key in the underlying store).
    """

    def setUp(self) -> None:
        self.wiz = _load_wizard_module()

    def _stub_active_registrations(self, regs):
        """Patch ``_stop_active_registrations`` to return ``regs``."""
        return mock.patch.object(
            self.wiz, "_stop_active_registrations", return_value=regs,
        )

    def test_picker_screen_button_label_and_callback(self) -> None:
        regs = [
            _make_reg(
                source_symbol="SOLUSD",
                exchange="hyperliquid", account="BASED",
                variant="NORMALFIB", side="SELL",
                registration_key="hyperliquid/BASED/SOL/NORMALFIB/SELL",
            ),
            _make_reg(
                source_symbol="ETHUSD",
                exchange="ondoperps", account="BITGET",
                variant="NORMALFIB", side="BUY",
                registration_key="ondoperps/BITGET/ETH-USD.P/NORMALFIB/BUY",
            ),
        ]
        with self._stub_active_registrations(regs):
            screen = self.wiz._build_stop_picker_screen()
        # The screen has buttons for each registration.
        reg_buttons = []
        for row in screen["buttons"]:
            for btn in row:
                if btn.get("callback_data", "").startswith("fibo:stop:p:"):
                    reg_buttons.append(btn)
        self.assertEqual(len(reg_buttons), 2)
        # First registration: 🔴 SOL / Hyperliquid / Based
        self.assertEqual(reg_buttons[0]["text"], "🔴 SOL / Hyperliquid / Based")
        self.assertEqual(reg_buttons[0]["callback_data"], "fibo:stop:p:0")
        # Second registration: 🔵 ETH / OndoPerps / Bitget
        self.assertEqual(
            reg_buttons[1]["text"], "🔵 ETH / OndoPerps / Bitget",
        )
        self.assertEqual(reg_buttons[1]["callback_data"], "fibo:stop:p:1")

    def test_callback_data_uses_index_not_registration_key(self) -> None:
        """The callback references the active-list INDEX (not the
        registration_key itself). The index is later resolved by
        ``_stop_active_registrations`` (which sorts by
        registration_key) and looked up by position.
        """
        regs = [
            _make_reg(
                source_symbol="ETHUSD",
                exchange="ondoperps", account="BITGET",
                variant="NORMALFIB", side="BUY",
            ),
        ]
        with self._stub_active_registrations(regs):
            screen = self.wiz._build_stop_picker_screen()
        reg_buttons = []
        for row in screen["buttons"]:
            for btn in row:
                if btn.get("callback_data", "").startswith("fibo:stop:p:"):
                    reg_buttons.append(btn)
        self.assertEqual(len(reg_buttons), 1)
        # Callback is ``fibo:stop:p:0`` — index-based, not
        # registration-key-based. This preserves the existing
        # callback contract (no breaking change).
        self.assertEqual(reg_buttons[0]["callback_data"], "fibo:stop:p:0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
