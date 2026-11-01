"""Universal MT5Linux get_contract_spec regression tests.

Covers broker-independent discovery, cache isolation, unit inference,
wizard wiring, and RPyC remote safety (Wine Python 3.8).
"""

from __future__ import annotations

import ast
import os
import re
import tempfile
import textwrap
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from plugins.trade.agents.x_mt5linux_agent import (
    ContractSpec,
    QuantityPlan,
    build_quantity_plan,
    clear_contract_spec_cache,
    convert_user_quantity,
    execute,
    infer_unit_label,
    _symbol_info_remote,
)


def _info(
    name: str,
    *,
    contract: float = 100.0,
    vmin: float = 0.01,
    vmax: float = 100.0,
    vstep: float = 0.01,
    path: str = "",
    description: str = "",
    calc_mode: int = 2,
    trade_mode: int = 4,
    base: str = "USD",
    profit: str = "USD",
    margin: str = "USD",
    volume_limit: float = 0.0,
) -> Dict[str, Any]:
    return {
        "name": name,
        "path": path,
        "description": description,
        "trade_contract_size": contract,
        "volume_min": vmin,
        "volume_max": vmax,
        "volume_step": vstep,
        "volume_limit": volume_limit,
        "trade_calc_mode": calc_mode,
        "trade_mode": trade_mode,
        "digits": 2,
        "currency_base": base,
        "currency_profit": profit,
        "currency_margin": margin,
        "point": 0.01,
        "trade_tick_size": 0.01,
        "trade_mode_full": True,
        "visible": True,
        "select": True,
        "spread": 1,
    }


class InferUnitLabelTests(unittest.TestCase):
    def test_crypto_any_ticker_not_allowlist(self) -> None:
        # Brand-new coin never seen by Hermes — path Cryptos is enough.
        spec = ContractSpec.from_mapping(
            "ZZZUSD",
            _info("ZZZUSD", path=r"Cryptos\ZZZUSD", description="Zzz coin", base="USD"),
        )
        assert spec is not None
        unit, mode = infer_unit_label(spec)
        self.assertEqual(mode, "underlying")
        self.assertEqual(unit, "ZZZ")

    def test_forex_eur(self) -> None:
        spec = ContractSpec.from_mapping(
            "EURUSD",
            _info(
                "EURUSD",
                contract=100000,
                path=r"Forex\EURUSD",
                description="Euro vs US Dollar",
                calc_mode=0,
                base="EUR",
            ),
        )
        assert spec is not None
        unit, mode = infer_unit_label(spec)
        self.assertEqual((mode, unit), ("underlying", "EUR"))

    def test_gold_oz(self) -> None:
        spec = ContractSpec.from_mapping(
            "XAUUSD",
            _info("XAUUSD", contract=100, path=r"Metals\XAUUSD", description="Gold", base="XAU"),
        )
        assert spec is not None
        unit, mode = infer_unit_label(spec)
        self.assertEqual(mode, "underlying")
        self.assertIn("oz", unit)

    def test_stock_shares(self) -> None:
        spec = ContractSpec.from_mapping(
            "AAPL",
            _info("AAPL", contract=1, path=r"Stocks\US\AAPL", description="Apple Inc", base="USD"),
        )
        assert spec is not None
        unit, mode = infer_unit_label(spec)
        self.assertEqual(mode, "underlying")
        self.assertIn("shares", unit)

    def test_index_lots(self) -> None:
        spec = ContractSpec.from_mapping(
            "US500",
            _info("US500", contract=1, path=r"Indices\US500", description="US 500", base="USD"),
        )
        assert spec is not None
        self.assertEqual(infer_unit_label(spec)[1], "lots")

    def test_ambiguous_cfd_lots(self) -> None:
        spec = ContractSpec.from_mapping(
            "FOO.m",
            _info("FOO.m", contract=10, path=r"CFD\FOO.m", description="Something", base="USD"),
        )
        assert spec is not None
        self.assertEqual(infer_unit_label(spec)[1], "lots")

    def test_broker_suffix_stripped_for_base(self) -> None:
        spec = ContractSpec.from_mapping(
            "BTCUSD.pc",
            _info("BTCUSD.pc", path=r"Cryptos\BTCUSD.pc", description="Bitcoin coin"),
        )
        assert spec is not None
        unit, mode = infer_unit_label(spec)
        self.assertEqual((mode, unit), ("underlying", "BTC"))


class TwoBrokersDifferentContractSizes(unittest.TestCase):
    def test_same_symbol_different_contract_size(self) -> None:
        lite = ContractSpec.from_mapping(
            "ETHUSD",
            _info("ETHUSD", contract=1.0, path=r"Crypto\ETHUSD", description="Ethereum coin"),
        )
        pup = ContractSpec.from_mapping(
            "ETHUSD",
            _info("ETHUSD", contract=100.0, path=r"Cryptos\ETHUSD", description="Ethereum coin"),
        )
        assert lite and pup
        pl = build_quantity_plan(lite)
        pp = build_quantity_plan(pup)
        self.assertEqual(pl.mode, "underlying")
        self.assertEqual(pp.mode, "underlying")
        self.assertEqual(pl.min_underlying, Decimal("0.01"))  # 0.01 lot * 1
        self.assertEqual(pp.min_underlying, Decimal("1.00"))  # 0.01 * 100
        r_lite = convert_user_quantity("1", pl)
        r_pup = convert_user_quantity("1", pp)
        self.assertTrue(r_lite.ok and r_pup.ok)
        self.assertEqual(r_lite.lots, Decimal("1"))  # 1 ETH / 1
        self.assertEqual(r_pup.lots, Decimal("0.01"))  # 1 ETH / 100


class CacheIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_contract_spec_cache()

    def tearDown(self) -> None:
        clear_contract_spec_cache()

    def test_cache_key_includes_login_server_symbol(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        m._contract_cache_put(1, "ServerA", "HYPEUSD", {"native_symbol": "HYPEUSD", "v": "a"})
        m._contract_cache_put(2, "ServerA", "HYPEUSD", {"native_symbol": "HYPEUSD", "v": "b"})
        m._contract_cache_put(1, "ServerB", "HYPEUSD", {"native_symbol": "HYPEUSD", "v": "c"})
        self.assertEqual(m._contract_cache_get(1, "ServerA", "HYPEUSD")["v"], "a")
        self.assertEqual(m._contract_cache_get(2, "ServerA", "HYPEUSD")["v"], "b")
        self.assertEqual(m._contract_cache_get(1, "ServerB", "HYPEUSD")["v"], "c")
        self.assertIsNone(m._contract_cache_get(9, "ServerA", "HYPEUSD"))


class InvalidMetadataTests(unittest.TestCase):
    def test_zero_contract_size_rejected(self) -> None:
        self.assertIsNone(ContractSpec.from_mapping("X", _info("X", contract=0)))

    def test_missing_contract_size_rejected(self) -> None:
        info = _info("X")
        del info["trade_contract_size"]
        self.assertIsNone(ContractSpec.from_mapping("X", info))



class GetContractSpecExecuteTests(unittest.TestCase):
    """Mocked terminal path: new broker alias + suffix + missing symbol + cache."""

    def setUp(self) -> None:
        clear_contract_spec_cache()
        self._saved = {
            k: v
            for k, v in os.environ.items()
            if k.upper().startswith("MT5LINUX_") or k.startswith("mt5linux_") or k == "HERMES_HOME"
        }
        for key in list(os.environ):
            if key.upper().startswith("MT5LINUX_") or key.startswith("mt5linux_"):
                os.environ.pop(key, None)
        self.home = tempfile.mkdtemp(prefix="mt5_cs_")
        os.environ["HERMES_HOME"] = self.home
        Path(self.home, ".env").write_text("", encoding="utf-8")
        os.environ["MT5LINUX_LOCK_PATH"] = str(Path(self.home) / "terminal.lock")
        from plugins.trade.agents import x_mt5linux_agent as m

        m._CLIENT = None
        m._CLIENT_KEY = None

    def tearDown(self) -> None:
        clear_contract_spec_cache()
        for key in list(os.environ):
            if key.upper().startswith("MT5LINUX_") or key.startswith("mt5linux_"):
                os.environ.pop(key, None)
        os.environ.pop("HERMES_HOME", None)
        os.environ.update(self._saved)
        from plugins.trade.agents import x_mt5linux_agent as m

        m._CLIENT = None
        m._CLIENT_KEY = None

    def _patch_with_account(self, login: int, server: str, alias: str):
        from plugins.trade.agents import x_mt5linux_agent as m
        from unittest import mock

        client = mock.MagicMock()
        snap = {
            "ok": True,
            "account": {
                "login": login,
                "server": server,
                "balance": 1000.0,
                "equity": 1000.0,
                "currency": "USD",
                "trade_allowed": True,
                "trade_expert": True,
            },
            "terminal": {"connected": True, "trade_allowed": True, "tradeapi_disabled": False},
            "positions": [],
            "orders": [],
        }

        def fake_with_account(a, fn):
            accounts = m.discover_accounts()
            alias_u = m._canonicalize_alias(str(a).upper())
            meta = accounts[alias_u]
            return fn(client, snap, alias_u, meta["login"], meta["server"])

        return mock.patch.object(m, "_with_account", side_effect=fake_with_account), client

    def test_new_broker_alias_unknown_to_hermes(self) -> None:
        """Newly configured broker — no allowlist / no prior knowledge."""
        from plugins.trade.agents import x_mt5linux_agent as m
        from unittest import mock

        alias = "FRESHBROKER42"
        login, server = 424242, "Fresh-Live"
        os.environ[f"mt5linux_{alias}_ACCOUNT"] = str(login)
        os.environ[f"mt5linux_{alias}_SERVER"] = server
        os.environ[f"mt5linux_{alias}_PASSWORD"] = "pw"
        info = _info("SOLUSD", contract=100.0, path=r"Cryptos\SOLUSD", description="Solana coin")
        patcher, client = self._patch_with_account(login, server, alias)
        with patcher, mock.patch.object(
            m,
            "_symbol_info_remote",
            return_value={"ok": True, "info": info, "tick": {"bid": 1, "ask": 2, "last": 1.5, "time": 1}},
        ):
            resp = m.execute(
                {"operation": "get_contract_spec", "account": alias, "symbol": "SOLUSD"}
            )
        self.assertTrue(resp.success, msg=str(getattr(resp, "error", None)))
        assert resp.data is not None
        self.assertEqual(resp.data["native_symbol"], "SOLUSD")
        self.assertEqual(resp.data["plan"]["mode"], "underlying")
        self.assertEqual(resp.data["plan"]["unit_label"], "SOL")
        self.assertEqual(resp.data["spec"]["trade_contract_size"], "100")
        self.assertEqual(resp.data["login"], login)
        self.assertEqual(resp.data["server"], server)

    def test_broker_suffix_symbol(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m
        from unittest import mock

        alias = "NEWBROKER999"
        login, server = 999, "NewBroker-MT5-Demo"
        os.environ[f"mt5linux_{alias}_ACCOUNT"] = str(login)
        os.environ[f"mt5linux_{alias}_SERVER"] = server
        os.environ[f"mt5linux_{alias}_PASSWORD"] = "pw"
        info = _info("BTCUSD.pc", contract=1.0, path=r"Cryptos\BTCUSD.pc", description="Bitcoin coin")
        patcher, _ = self._patch_with_account(login, server, alias)
        with patcher, mock.patch.object(
            m,
            "_symbol_info_remote",
            return_value={"ok": True, "info": info, "tick": {"bid": 1, "ask": 2, "last": 1, "time": 1}},
        ):
            resp = m.execute(
                {"operation": "get_contract_spec", "account": alias, "symbol": "BTCUSD.pc"}
            )
        self.assertTrue(resp.success)
        assert resp.data is not None
        self.assertEqual(resp.data["plan"]["unit_label"], "BTC")

    def test_missing_symbol_structured_error(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m
        from unittest import mock

        alias = "NEWBROKER999"
        login, server = 999, "NewBroker-MT5-Demo"
        os.environ[f"mt5linux_{alias}_ACCOUNT"] = str(login)
        os.environ[f"mt5linux_{alias}_SERVER"] = server
        os.environ[f"mt5linux_{alias}_PASSWORD"] = "pw"
        patcher, _ = self._patch_with_account(login, server, alias)

        def remote_eval(cli, expr):
            if expr == "_sym_payload" or expr == "sym_payload":
                return {"ok": True, "info": None, "tick": None, "selected": False}
            if expr == "resolve_hits":
                return []
            return None

        with patcher, mock.patch.object(m, "_symbol_info_remote", return_value={"ok": True, "info": None}), mock.patch.object(
            m, "_remote_execute", return_value=None
        ), mock.patch.object(m, "_remote_eval", side_effect=remote_eval):
            resp = m.execute(
                {"operation": "get_contract_spec", "account": alias, "symbol": "DOESNOTEXIST"}
            )
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "CONTRACT_SPEC_UNAVAILABLE")
        self.assertIn("DOESNOTEXIST", resp.error.message)

    def test_cache_isolation_across_accounts(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m
        from unittest import mock

        for alias, login, server, contract in (
            ("BROKERA1", 1, "A", 1.0),
            ("BROKERB2", 2, "B", 100.0),
        ):
            os.environ[f"mt5linux_{alias}_ACCOUNT"] = str(login)
            os.environ[f"mt5linux_{alias}_SERVER"] = server
            os.environ[f"mt5linux_{alias}_PASSWORD"] = "pw"
            info = _info("ETHUSD", contract=contract, path=r"Cryptos\ETHUSD", description="Ethereum coin")
            patcher, _ = self._patch_with_account(login, server, alias)
            with patcher, mock.patch.object(
                m,
                "_symbol_info_remote",
                return_value={"ok": True, "info": info, "tick": {"bid": 1, "ask": 2, "last": 1, "time": 1}},
            ):
                resp = m.execute(
                    {"operation": "get_contract_spec", "account": alias, "symbol": "ETHUSD"}
                )
            self.assertTrue(resp.success)
            assert resp.data is not None
            from plugins.trade.agents.x_mt5linux_agent import _qty_fmt

            self.assertEqual(resp.data["spec"]["trade_contract_size"], _qty_fmt(Decimal(str(contract))))
        # Normalize check
        self.assertEqual(m._contract_cache_get(1, "A", "ETHUSD")["spec"]["trade_contract_size"], "1")
        self.assertEqual(m._contract_cache_get(2, "B", "ETHUSD")["spec"]["trade_contract_size"], "100")


class WizardUsesGetContractSpec(unittest.TestCase):
    def test_new_order_and_ladder_share_fetch(self) -> None:
        from plugins.trade.wizard import TradeWizard, WizardState
        from unittest import mock

        desk = mock.MagicMock()
        plan_payload = {
            "mode": "underlying",
            "unit_label": "HYPE",
            "contract_size": "100",
            "volume_min_lots": "0.01",
            "volume_max_lots": "100",
            "volume_step_lots": "0.01",
            "min_underlying": "1",
            "step_underlying": "1",
            "max_underlying": "10000",
            "native_symbol": "HYPEUSD",
            "diagnostic": None,
        }
        spec_payload = {
            "symbol": "HYPEUSD",
            "trade_contract_size": "100",
            "volume_min": "0.01",
            "volume_max": "100",
            "volume_step": "0.01",
            "path": r"Cryptos\HYPEUSD",
            "description": "Hyperliquid coin",
            "currency_base": "USD",
            "trade_calc_mode": 2,
        }
        desk.execute.return_value = SimpleNamespace(
            success=True,
            data={
                "native_symbol": "HYPEUSD",
                "spec": spec_payload,
                "plan": plan_payload,
                "info": _info("HYPEUSD", path=r"Cryptos\HYPEUSD", description="Hyperliquid coin"),
                "login": 1,
                "server": "S",
                "trade_contract_size": "100",
            },
            error=None,
            instrument=None,
        )
        wiz = TradeWizard(tradedesk=desk)
        state = WizardState(exchange="mt5linux", account="PUPRIME33849951", symbol="HYPEUSD")
        state.order["symbol"] = "HYPEUSD"
        state.ladder["symbol"] = "HYPEUSD"

        s1, p1 = wiz._mt5_volume_plan_for_state(state, ladder=False)
        s2, p2 = wiz._mt5_volume_plan_for_state(state, ladder=True)
        self.assertIsNotNone(s1)
        self.assertIsNotNone(p1)
        assert p1 is not None and p2 is not None
        self.assertEqual(p1.mode, "underlying")
        self.assertEqual(p1.unit_label, "HYPE")
        self.assertEqual(p2.unit_label, "HYPE")
        ops = []
        for c in desk.execute.call_args_list:
            arg = c.args[0] if c.args else c.kwargs
            ops.append(arg.get("operation"))
        self.assertTrue(ops)
        self.assertTrue(all(o == "get_contract_spec" for o in ops))

    def test_failure_surfaces_reason(self) -> None:
        from plugins.trade.wizard import TradeWizard, WizardState
        from unittest import mock

        desk = mock.MagicMock()
        desk.execute.return_value = SimpleNamespace(
            success=False,
            data=None,
            error=SimpleNamespace(
                code="CONTRACT_SPEC_UNAVAILABLE",
                message="symbol 'ZZ' not found on this account/server",
            ),
            instrument=None,
        )
        wiz = TradeWizard(tradedesk=desk)
        state = WizardState(exchange="mt5linux", account="X", symbol="ZZ")
        state.order["symbol"] = "ZZ"
        spec, plan = wiz._mt5_volume_plan_for_state(state, ladder=False)
        self.assertIsNone(spec)
        self.assertIsNone(plan)
        self.assertIn("not found", state.order.get("contract_spec_error", ""))


class RemoteSnippetSafety(unittest.TestCase):
    def test_symbol_info_remote_template_no_forbidden_imports(self) -> None:
        src = Path(__file__).resolve().parents[1] / "agents" / "x_mt5linux_agent.py"
        src_text = src.read_text(encoding="utf-8")
        m = re.search(r"def _symbol_info_remote[\s\S]*?code = f\"\"\"([\s\S]*?)\"\"\"", src_text)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertNotIn("import uuid", body)
        self.assertNotIn("import json", body)
        self.assertIn("symbol_select", body)
        self.assertIn("volume_limit", body)
        self.assertIn("trade_contract_size", body)

    def test_get_contract_spec_resolve_code_ast(self) -> None:
        """Catalog scan snippet must parse under 3.x and only import MetaTrader5."""
        # Build the same code pattern as _resolve_native_symbol
        req = "HYPEUSD"
        code = f"""
import MetaTrader5 as m
q = {req!r}
hits = []
for s in (m.symbols_get() or ()):
    name = str(s.name)
    up = name.upper().replace('.','').replace('-','').replace('_','')
    if up == q or name.upper() == q or up.startswith(q) or q in up:
        hits.append(name)
        if len(hits) >= 30:
            break
resolve_hits = hits
"""
        tree = ast.parse(code)
        imports = [n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)]
        froms = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        self.assertEqual(imports, ["MetaTrader5"])
        self.assertEqual(froms, [])


@unittest.skipUnless(os.environ.get("MT5LINUX_LIVE_CONTRACT_SPEC") == "1", "live opt-in")
class LiveContractSpecTests(unittest.TestCase):
    def test_puprime_hype_and_lite_sol(self) -> None:
        clear_contract_spec_cache()
        r1 = execute(
            {
                "operation": "get_contract_spec",
                "account": "PUPRIME33849951",
                "symbol": "HYPEUSD",
            }
        )
        self.assertTrue(r1.success, msg=str(getattr(r1, "error", None)))
        self.assertEqual(r1.data["native_symbol"], "HYPEUSD")
        self.assertEqual(r1.data["plan"]["mode"], "underlying")
        self.assertEqual(r1.data["plan"]["unit_label"], "HYPE")
        cs = Decimal(r1.data["spec"]["trade_contract_size"])
        self.assertGreater(cs, 0)

        r2 = execute(
            {
                "operation": "get_contract_spec",
                "account": "LITE7486706",
                "symbol": "SOLUSD",
            }
        )
        self.assertTrue(r2.success, msg=str(getattr(r2, "error", None)))
        self.assertEqual(r2.data["plan"]["unit_label"], "SOL")
        # Cache isolation: different login keys
        self.assertNotEqual(r1.data["login"], r2.data["login"])


if __name__ == "__main__":
    unittest.main()
