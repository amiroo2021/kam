"""Tests for MT5Linux underlying↔lots quantity conversion."""
from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

from plugins.trade.agents.x_mt5linux_agent import (
    ContractSpec,
    build_quantity_plan,
    convert_user_quantity,
    format_volume_confirm,
)


def _spec(**kwargs):
    base = dict(
        symbol="SOLUSD",
        contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("100"),
        volume_step=Decimal("0.01"),
        path=r"ECN\Crypto\SOLUSD",
        description="Solana vs US Dollar",
    )
    base.update(kwargs)
    return ContractSpec(**base)


class Mt5QuantityMathTests(unittest.TestCase):
    def test_solusd_plan_min_one_token(self) -> None:
        plan = build_quantity_plan(_spec())
        self.assertEqual(plan.mode, "underlying")
        self.assertEqual(plan.unit_label, "SOL")
        self.assertEqual(plan.min_underlying, Decimal("1.00"))
        self.assertEqual(plan.step_underlying, Decimal("1.00"))
        self.assertIn("SOL tokens", plan.prompt_label)
        lines = plan.help_lines()
        self.assertTrue(any("100 SOL per lot" in x for x in lines))
        self.assertTrue(any("Minimum: 1 SOL = 0.01 MT5 lots" in x for x in lines))

    def test_ethusd_contract_one(self) -> None:
        plan = build_quantity_plan(
            _spec(
                symbol="ETHUSD",
                contract_size=Decimal("1"),
                path=r"ECN\Crypto\ETHUSD",
                description="Ethereum vs US Dollar",
            )
        )
        self.assertEqual(plan.unit_label, "ETH")
        self.assertEqual(plan.min_underlying, Decimal("0.01"))
        r = convert_user_quantity("0.05", plan)
        self.assertTrue(r.ok)
        self.assertEqual(r.lots, Decimal("0.05"))
        self.assertEqual(r.underlying, Decimal("0.05"))

    def test_hypeusd_contract_100(self) -> None:
        plan = build_quantity_plan(
            _spec(
                symbol="HYPEUSD",
                contract_size=Decimal("100"),
                path=r"ECN\Crypto\HYPEUSD",
                description="Hyperliquid vs US Dollar",
            )
        )
        self.assertEqual(plan.unit_label, "HYPE")
        r = convert_user_quantity("100", plan)
        self.assertTrue(r.ok)
        self.assertEqual(r.lots, Decimal("1"))

    def test_invalid_increment_not_silent(self) -> None:
        plan = build_quantity_plan(_spec())
        r = convert_user_quantity("1.5", plan)
        self.assertFalse(r.ok)
        self.assertIn("not a valid broker increment", r.message)
        self.assertIsNotNone(r.nearest_lots)

    def test_below_minimum(self) -> None:
        plan = build_quantity_plan(_spec())
        # 0 is invalid; use a step-aligned value below min if possible.
        # With min=0.01 lots and step=0.01, smallest positive step is also min.
        r = convert_user_quantity("0", plan)
        self.assertFalse(r.ok)
        self.assertIn("positive", r.message.lower())

    def test_confirm_lines(self) -> None:
        plan = build_quantity_plan(_spec())
        r = convert_user_quantity("100", plan)
        lines = format_volume_confirm(r, plan)
        self.assertEqual(lines[0], "Volume: 100 SOL")
        self.assertTrue(lines[1].startswith("MT5 volume:"))

    def test_forex_eur_underlying(self) -> None:
        plan = build_quantity_plan(
            ContractSpec(
                symbol="EURUSD",
                contract_size=Decimal("100000"),
                volume_min=Decimal("0.01"),
                volume_max=Decimal("100"),
                volume_step=Decimal("0.01"),
                currency_base="EUR",
                path=r"ECN\Forex Major\EURUSD",
                description="Euro vs US Dollar",
                trade_calc_mode=0,
            )
        )
        self.assertEqual(plan.mode, "underlying")
        self.assertEqual(plan.unit_label, "EUR")
        r = convert_user_quantity("1000", plan)  # 0.01 lot
        self.assertTrue(r.ok)
        self.assertEqual(r.lots, Decimal("0.01"))

    def test_unknown_index_uses_lots(self) -> None:
        plan = build_quantity_plan(
            ContractSpec(
                symbol="US500",
                contract_size=Decimal("1"),
                volume_min=Decimal("0.1"),
                volume_max=Decimal("50"),
                volume_step=Decimal("0.1"),
                path=r"ECN\Indices\US500",
                description="US 500",
                trade_calc_mode=2,
            )
        )
        self.assertEqual(plan.mode, "lots")
        r = convert_user_quantity("0.5", plan)
        self.assertTrue(r.ok)
        self.assertEqual(r.lots, Decimal("0.5"))



    def test_hypeusd_puprime_plan(self) -> None:
        plan = build_quantity_plan(
            _spec(
                symbol="HYPEUSD",
                contract_size=Decimal("100"),
                path=r"Cryptos\HYPEUSD",
                description="Hyperliquid coin",
            )
        )
        self.assertEqual(plan.mode, "underlying")
        self.assertEqual(plan.unit_label, "HYPE")
        self.assertEqual(plan.min_underlying, Decimal("1.00"))
        self.assertIn("HYPE tokens", plan.prompt_label)

    def test_remote_symbol_info_code_has_no_uuid_import(self) -> None:
        from pathlib import Path as P
        import re
        src = (P(__file__).resolve().parents[1] / "agents" / "x_mt5linux_agent.py").read_text(encoding="utf-8")
        # Extract _symbol_info_remote f-string body
        m = re.search(r"def _symbol_info_remote[\s\S]*?code = f\"\"\"([\s\S]*?)\"\"\"", src)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertNotIn("import uuid", body)
        self.assertNotIn("import json", body)
        self.assertIn("trade_contract_size", body)


class WizardMt5VolumeTests(unittest.TestCase):
    def test_new_order_volume_screen_and_convert(self) -> None:
        from plugins.trade.wizard import TradeWizard, WizardState

        w = TradeWizard(tradedesk=mock.Mock())
        chat = ("tg", 1)
        st = w._state_for(chat)
        st.exchange = "mt5linux"
        st.account = "LITE7486706"
        st.symbol = "SOLUSD"
        st.order["symbol"] = "SOLUSD"
        st.order["side"] = "buy"

        fake_info = {
            "name": "SOLUSD",
            "trade_contract_size": "100",
            "volume_min": "0.01",
            "volume_max": "100",
            "volume_step": "0.01",
            "path": r"ECN\Crypto\SOLUSD",
            "description": "Solana vs US Dollar",
            "currency_base": "USD",
            "digits": 2,
        }

        class Resp:
            success = True
            data = {"info": fake_info, "contract_size": "100"}
            instrument = None
            market_price = None
            error = None

        w._desk.execute.return_value = Resp()
        screen = w._render_new_order_volume(chat)
        self.assertIn("SOL tokens", screen.text)
        self.assertIn("Minimum: 1 SOL = 0.01 MT5 lots", screen.text)
        self.assertIn("Contract size: 100 SOL per lot", screen.text)

        st.state = "awaiting_volume"
        screen2 = w._handle_new_order_text(chat, "100")
        self.assertEqual(st.order.get("volume_lots"), "1")
        self.assertEqual(st.order.get("volume"), "1")
        self.assertEqual(st.order.get("volume_underlying"), "100")
        self.assertIn("Volume:", screen2.text)
        self.assertIn("SOL", screen2.text)

        # invalid
        st.state = "awaiting_volume"
        bad = w._handle_new_order_text(chat, "1.5")
        self.assertEqual(bad.state, "awaiting_volume")
        self.assertIn("not a valid broker increment", bad.text)

    def test_non_mt5_unchanged(self) -> None:
        from plugins.trade.wizard import TradeWizard

        w = TradeWizard(tradedesk=mock.Mock())
        chat = ("tg", 2)
        st = w._state_for(chat)
        st.exchange = "hyperliquid"
        st.account = "MAIN"
        st.order["symbol"] = "SOL"
        st.order["side"] = "buy"
        screen = w._render_new_order_volume(chat)
        self.assertIn("Enter Volume:", screen.text)
        self.assertNotIn("MT5 lots", screen.text)
        st.state = "awaiting_volume"
        w._handle_new_order_text(chat, "1.25")
        self.assertEqual(st.order.get("volume"), "1.25")
        self.assertIsNone(st.order.get("volume_lots"))


if __name__ == "__main__":
    unittest.main()
