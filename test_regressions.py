import asyncio
import copy
import math
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from eventkit import Event
from ib_insync import Contract, Order, OrderStatus, Position, Trade
from src.bot import RiskBot
from src.state import ManagedPosition, State

class TradingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'risk':{'tp_pct':1.0,'sl_pct':2.0,'trigger_pct':3.0,'trail_pct':1.0},
                    'bot':{'poll_interval':1,'order_timeout':1,'protection_check_interval':30}}
        self.ib = MagicMock()
        self.ib.orderStatusEvent = Event()
        self.ib.positionEvent = Event()
        self.ib.errorEvent = Event()
        self.ib.positions.return_value = []
        self.ib.openTrades.return_value = []
        self.ib.trades.return_value = []
        self.next_id = 100
        def place(contract, order):
            self.next_id += 1
            order.orderId = self.next_id
            return Trade(contract,order,OrderStatus(orderId=self.next_id,status='Submitted'))
        self.ib.placeOrder.side_effect = place
        self.bot = RiskBot(self.ib,self.cfg)
        self.mp = ManagedPosition(1,'AAPL','STK','SMART','USD',10,100)

    def trade(self, oid=1, kind='LMT', status='Submitted', oca='', conid=1):
        return Trade(Contract(conId=conid,symbol='AAPL'),
                     Order(orderId=oid,orderType=kind,action='SELL',ocaGroup=oca),
                     OrderStatus(orderId=oid,status=status))


    def test_invalid_risk_percentages_fail_startup(self):
        for value in (0,-1,float('nan'),float('inf'),100):
            cfg = copy.deepcopy(self.cfg)
            cfg['risk']['sl_pct'] = value
            with self.assertRaises(ValueError):
                RiskBot(self.ib,cfg)

    def test_invalid_timing_configuration_is_rejected(self):
        for name,value in (('poll_interval',0),('order_timeout',-1),('protection_check_interval',float('nan'))):
            cfg = copy.deepcopy(self.cfg)
            cfg['bot'][name] = value
            with self.assertRaises(ValueError):
                RiskBot(self.ib,cfg)

    def test_invalid_trailing_levels_are_rejected(self):
        for levels in ([{'trigger':3,'trailing':0}],
                       [{'trigger':3,'trailing':1},{'trigger':3,'trailing':2}],
                       [{'trigger':float('nan'),'trailing':1}]):
            cfg = copy.deepcopy(self.cfg)
            cfg['risk']['trailing_levels'] = levels
            with self.assertRaises(ValueError):
                RiskBot(self.ib,cfg)

    def test_invalid_market_prices_do_not_trigger(self):
        for price in (0,-1,float('inf'),float('nan'),None):
            self.bot._tickers[1] = SimpleNamespace(last=price,close=float('nan'))
            self.assertIsNone(self.bot._get_last_price(self.mp))
        self.bot._tickers[1] = SimpleNamespace(last=float('nan'),close=101)
        self.assertEqual(self.bot._get_last_price(self.mp),101)

    def test_futures_routing_uses_contract_exchange(self):
        self.mp.sec_type = 'FUT'
        self.mp.exchange = 'CME'
        self.assertEqual(self.bot._order_contract(self.mp).exchange,'CME')
        self.mp.sec_type = 'STK'
        self.assertEqual(self.bot._order_contract(self.mp).exchange,'SMART')

    def test_same_second_protection_groups_are_unique(self):
        groups = []
        with patch('src.bot.asyncio.sleep',new=AsyncMock()):
            for _ in range(2):
                self.mp.state = State.NEW
                asyncio.run(self.bot._place_tp_sl(self.mp))
                groups.append(self.mp.oca_group)
        self.assertNotEqual(*groups)
        self.assertTrue(all(g.startswith('OCA_1_') for g in groups))

    def test_recovered_take_profit_preserves_oca_link(self):
        self.mp.oca_group = 'OCA_existing'
        with patch('src.bot.asyncio.sleep',new=AsyncMock()):
            asyncio.run(self.bot._place_missing_tp(self.mp))
        order = self.ib.placeOrder.call_args.args[1]
        self.assertEqual((order.ocaGroup,order.ocaType),('OCA_existing',1))

    def test_failed_residual_cancel_prevents_new_trailing_stop(self):
        self.mp.tp_cancelled = self.mp.sl_cancelled = True
        self.ib.openTrades.return_value = [self.trade(kind='STP')]
        with patch.object(self.bot,'_wait_cancel',new=AsyncMock(return_value=False)):
            asyncio.run(self.bot._cancel_oca_and_trail(self.mp))
        self.ib.placeOrder.assert_not_called()
        self.assertEqual(self.mp.state,State.MONITORING)
