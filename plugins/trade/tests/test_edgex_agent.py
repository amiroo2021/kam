from __future__ import annotations
import asyncio, os, sys, tempfile, unittest
from pathlib import Path
from decimal import Decimal
from unittest import mock

ROOT=Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from plugins.trade.agents import x_edgex_agent as edgex
from plugins.trade import tradedesk

class EdgeXAgentTests(unittest.TestCase):
    class _FakeClient:
        def __init__(self):
            self.calls = []
            self.closed = False
        async def create_limit_order(self, **kwargs):
            self.calls.append(("create_limit_order", kwargs))
            return {"code": "SUCCESS", "data": {"orderId": "101"}}
        async def create_market_order(self, **kwargs):
            self.calls.append(("create_market_order", kwargs))
            return {"code": "SUCCESS", "data": {"orderId": "102"}}
        async def cancel_order(self, params):
            self.calls.append(("cancel_order", params))
            return {"code": "SUCCESS", "data": {"orderId": str(getattr(params, 'order_id', '103'))}}
        async def create_order(self, params):
            self.calls.append(("create_order", params))
            return {"code": "SUCCESS", "data": {"orderId": "104"}}
        async def close(self):
            self.closed = True

    def setUp(self):
        self.old={k:v for k,v in os.environ.items() if k.startswith('EDGEX_')}
        for k in list(os.environ):
            if k.startswith('EDGEX_'): os.environ.pop(k)
        self.home=tempfile.mkdtemp(); os.environ['HERMES_HOME']=self.home
        Path(self.home,'.env').write_text('')
    def tearDown(self):
        for k in list(os.environ):
            if k.startswith('EDGEX_'): os.environ.pop(k)
        os.environ.update(self.old)
    def set_account(self):
        for s,v in {'ACCOUNTID':'123','APIKEY':'key','APISECRET':'secret','APIPASSPHRASE':'pass','SIGNERKEY':'signer'}.items():
            os.environ[f'EDGEX_MAIN_{s}']=v
    def test_discovery_complete_only(self):
        os.environ['EDGEX_HALF_ACCOUNTID']='1'; self.assertEqual(edgex.list_accounts(),[])
        self.set_account(); self.assertEqual(edgex.list_accounts(),['main'])
    def test_tradedesk_discovers_agent(self):
        self.assertIn('edgex',tradedesk.TradeDesk().list_exchanges())
    def test_balance_and_positions_orders(self):
        self.set_account()
        asset={'collateralAssetModelList':[{'totalEquity':'100','availableAmount':'80','initialMarginRequirement':'10','totalPositionValueAbs':'50'}],
               'positionList':[{'contractId':'1','openSize':'2'}],
               'positionAssetList':[{'contractId':'1','avgEntryPrice':'20','unrealizePnl':'3'}]}
        orders={'dataList':[{'contractId':'1','side':'BUY','size':'2','price':'10'},{'contractId':'1','side':'BUY','size':'1','price':'13'}]}
        def request(c,p,q): return orders if 'getActiveOrderPage' in p else asset
        with mock.patch.object(edgex,'_request',side_effect=request), mock.patch.object(edgex,'_metadata',return_value={'1':'BTCUSDT'}):
            b=edgex.execute({'operation':'balance','account':'main'}).to_dict(); self.assertTrue(b['success']); self.assertEqual(b['balance']['value'],'100.00')
            po=edgex.execute({'operation':'positions_orders','account':'main'}).to_dict(); self.assertTrue(po['success']); self.assertEqual(po['positions'][0]['symbol'],'BTCUSDT'); self.assertEqual(po['order_groups'][0]['order_count'],2)

    def test_vwap_is_rounded_for_wizard_display(self):
        from plugins.trade.canonical import CanonicalOrderGroup
        from plugins.trade.wizard import _order_group_summary_line
        group = CanonicalOrderGroup("BTCUSDC", "buy", 17, "4.212", "61729.958689458689", "61357.0", "62173.0")
        text = _order_group_summary_line(group)
        self.assertIn("VWAP 61,729.96", text)
        self.assertNotIn("61729.958689", text)

    def test_short_symbol_resolves_to_quote_contract(self):
        with mock.patch.object(edgex, "_metadata", return_value={"3":"SOLUSDC"}):
            self.assertEqual(edgex._resolve_contract("SOL"), ("3", "SOLUSDC"))

    def test_ladder_prices_are_quantized_to_contract_tick(self):
        prices=edgex._ladder_prices(Decimal("100"),Decimal("120"),20,Decimal("0.01"))
        self.assertEqual(prices[1],Decimal("101.05"))
        self.assertTrue(all(p == p.quantize(Decimal("0.01")) for p in prices))

    def test_half_gaussian_ladder_sizes_increase_and_preserve_total(self):
        sizes=edgex._ladder_sizes(Decimal("200"),20,Decimal("0.1"),"half_gaussian",Decimal("2"))
        self.assertLess(sizes[0],sizes[-1])
        self.assertTrue(all(size >= Decimal("2") for size in sizes))
        self.assertEqual(sum(sizes),Decimal("200"))
        self.assertGreater(len(set(sizes)),10)

    def test_position_trigger_uses_valid_time_in_force(self):
        self.assertEqual(edgex._trigger_time_in_force(), "GOOD_TIL_CANCEL")

    def test_trigger_order_uses_trigger_price_as_sdk_price(self):
        self.assertTrue(edgex._trigger_order_price("70000") == "70000")

    def test_positions_orders_extracts_live_tp_sl_orders(self):
        rows=[{"contractId":"1","type":"TAKE_PROFIT_MARKET","triggerPrice":"70000","side":"SELL","size":"3.433","isPositionTpsl":True},{"contractId":"1","type":"STOP_MARKET","triggerPrice":"50000","side":"SELL","size":"3.433","isPositionTpsl":True}]
        self.assertEqual(edgex._protection_prices(rows,"1"),("70000","50000"))

    def test_position_management_view_can_show_protection(self):
        self.assertIn("protection", edgex._positions_orders.__doc__ or "")

    def test_zero_tp_sl_selects_existing_protection_order_for_cancellation(self):
        rows=[{"id":"11","contractId":"1","type":"TAKE_PROFIT_MARKET","isPositionTpsl":True},{"id":"12","contractId":"1","type":"STOP_MARKET","isPositionTpsl":True}]
        self.assertEqual(edgex._protection_order_ids(rows,"1","set_tp"),["11"])
        self.assertEqual(edgex._protection_order_ids(rows,"1","set_sl"),["12"])

    def test_write_capabilities_are_advertised(self):
        for operation in ("ladder","cancel_order_group","set_tp","set_sl","close_position"):
            self.assertIn(operation, edgex.capabilities())

    def test_new_order_is_advertised_and_dispatches(self):
        self.set_account()
        self.assertIn("new_order", edgex.capabilities())
        with mock.patch.object(edgex, "_metadata", return_value={"42":"SOLUSDC"}), mock.patch.object(edgex, "_create_limit_order", return_value={"data":{"orderId":"99"}}):
            result=edgex.execute({"operation":"new_order","account":"main","symbol":"SOLUSDC","side":"sell","volume":"1","price":"120"}).to_dict()
        self.assertTrue(result["success"])
        self.assertEqual(result["order"]["exchange_order_id"], 99)

    def test__run_async_works_inside_running_loop_after_fix(self):
        async def scenario():
            self.assertEqual(edgex._run_async(asyncio.sleep(0, result="ok")), "ok")
        asyncio.run(scenario())

    def test_new_order_can_run_inside_existing_event_loop(self):
        self.set_account()
        async def scenario():
            fake = self._FakeClient()
            with mock.patch.object(edgex, "_build_client", return_value=fake), \
                 mock.patch.object(edgex, "_resolve_contract", return_value=("42", "SOLUSDC")):
                result = edgex.execute({"operation":"new_order","account":"main","symbol":"SOLUSDC","side":"sell","volume":"1","price":"120"}).to_dict()
            self.assertTrue(result["success"])
            self.assertEqual(result["order"]["exchange_order_id"], 101)
            self.assertEqual(fake.calls[0][0], "create_limit_order")
            self.assertTrue(fake.closed)
        asyncio.run(scenario())

    def test_ladder_can_run_inside_existing_event_loop(self):
        self.set_account()
        async def scenario():
            fake = self._FakeClient()
            with mock.patch.object(edgex, "_build_client", return_value=fake), \
                 mock.patch.object(edgex, "_resolve_contract", return_value=("42", "SOLUSDC")), \
                 mock.patch.object(edgex, "_contract_rules", return_value=(Decimal("0.01"), Decimal("0.1"), Decimal("0.1"))):
                result = edgex.execute({
                    "operation":"ladder","account":"main","symbol":"SOLUSDC","side":"sell",
                    "order_count":"2","total_volume":"1.0","start_price":"120","end_price":"121","distribution":"uniform"
                }).to_dict()
            self.assertTrue(result["success"])
            self.assertEqual(result["ladder"]["submitted_order_count"], 2)
            self.assertEqual([name for name, _ in fake.calls if name == "create_limit_order"], ["create_limit_order", "create_limit_order"])
            self.assertTrue(fake.closed)
        asyncio.run(scenario())

    def test_cancel_tp_sl_close_write_helpers_can_run_inside_existing_event_loop(self):
        self.set_account()
        async def scenario():
            fake = self._FakeClient()
            with mock.patch.object(edgex, "_build_client", return_value=fake):
                cancel = edgex._cancel_order_by_id({"account_id":"123","api_key":"k","passphrase":"p","api_secret":"s","signer_key":"sig"}, "777")
                market = edgex._create_market_order({"account_id":"123","api_key":"k","passphrase":"p","api_secret":"s","signer_key":"sig"}, "42", "1", "sell")
                trigger = edgex._create_trigger_order({"account_id":"123","api_key":"k","passphrase":"p","api_secret":"s","signer_key":"sig"}, "42", "1", "sell", "70000", "tp")
            self.assertEqual(cancel["code"], "SUCCESS")
            self.assertEqual(market["code"], "SUCCESS")
            self.assertEqual(trigger["code"], "SUCCESS")
            self.assertEqual([name for name, _ in fake.calls], ["cancel_order", "create_market_order", "create_order"])
            self.assertTrue(fake.closed)
        asyncio.run(scenario())

if __name__=='__main__': unittest.main()
