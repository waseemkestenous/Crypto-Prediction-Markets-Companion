import tempfile
import unittest
from copy import deepcopy
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
import app
import order_settings as settings
import robinhood_target as rh
import round_history


class TradeCloseOverrideTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now().astimezone();self.start=self.now-timedelta(minutes=3)
        self.end=self.start+timedelta(minutes=15)
        self.purchase={'status':'filled','target':83000,'direction':'UP / ABOVE','contracts':1,
                       'attempted_at':'trade-token','stop_loss_peak_bid_cents':90,'execution_price_cents':40}
        self.p={'target':83000,'purchase':self.purchase,'order_settings':dict(settings.DEFAULTS,
                close_conditions=dict(settings.CLOSE_DEFAULTS,enabled=True,profit_per_ct_cents=10,minute_from=0,minute_to=15),
                force_close=dict(settings.FORCE_CLOSE_DEFAULTS,enabled=True))}

    def test_independent_ignores_and_manual_close_remains_available(self):
        self.assertTrue(rh.stop_loss_matches(self.p,60,self.start,self.end))
        self.assertTrue(rh.close_conditions_match(self.p,60,self.start,self.end))
        self.purchase['ignore_stop_loss']=True
        self.assertFalse(rh.stop_loss_matches(self.p,60,self.start,self.end))
        self.assertTrue(rh.close_conditions_match(self.p,60,self.start,self.end))
        self.purchase['ignore_early_profit']=True
        self.assertFalse(rh.sale_conditions_match(self.p,60,self.start,self.end))
        self.purchase['force_close_token']='trade-token'
        self.assertTrue(rh.sale_conditions_match(self.p,60,self.start,self.end))
        self.purchase['ignore_stop_loss']=False
        self.assertTrue(rh.stop_loss_matches(self.p,60,self.start,self.end))

    def test_force_buy_disables_all_automatic_exits(self):
        for origin in ({'purchase_rule':'force_buy'}, {'conditions':{'rule':'force_buy'}}):
            with self.subTest(origin=origin):
                self.p['purchase'] = dict(self.purchase, **origin)
                self.assertFalse(rh.close_conditions_match(self.p,60,self.start,self.end))
                self.assertFalse(rh.stop_loss_matches(self.p,60,self.start,self.end))
                self.assertFalse(rh.sale_conditions_match(self.p,60,self.start,self.end))
                self.p['purchase']['force_close_token'] = 'trade-token'
                self.assertTrue(rh.sale_conditions_match(self.p,60,self.start,self.end))
                with tempfile.TemporaryDirectory() as folder,patch.object(round_history,'ROOT',Path(folder)):
                    round_history.upsert('BTC',self.start.isoformat(),self.end.isoformat(),purchase=self.p['purchase'])
                    for reason in ('close_conditions', 'stop_loss'):
                        self.assertFalse(app.record_robinhood_purchase('BTC',self.start,self.end,
                            {'target':83000,'close_order':{'status':'attempted','reason':reason}}))
                    self.assertNotIn('close_order',round_history.load('BTC')[0]['purchase'])

    def test_endpoint_is_trade_scoped_and_persists(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(round_history,'ROOT',Path(folder)),patch.object(app.ARGS,'asset','BTC'),patch.object(app,'current_round_key',return_value=(self.start,self.end)):
            round_history.upsert('BTC',self.start.isoformat(),self.end.isoformat(),purchase=self.purchase)
            client=app.app.test_client()
            payload={'asset':'BTC','round_start':self.start.isoformat(),'purchase_token':'trade-token','field':'ignore_stop_loss','value':True}
            self.assertEqual(client.post('/api/trade-close-overrides',json=payload).status_code,200)
            saved=round_history.load('BTC')[0]['purchase']
            self.assertTrue(saved['ignore_stop_loss'])
            self.assertNotIn('ignore_early_profit',saved)
            payload['value']=False
            self.assertEqual(client.post('/api/trade-close-overrides',json=payload).status_code,200)
            self.assertFalse(round_history.load('BTC')[0]['purchase']['ignore_stop_loss'])
            for changes,code in [({'purchase_token':'stale'},409),({'round_start':'old'},409),({'value':'yes'},400),({'field':'other'},400)]:
                self.assertEqual(client.post('/api/trade-close-overrides',json=dict(payload,**changes)).status_code,code)
            saved['close_order']={'status':'submitted'}
            round_history.upsert('BTC',self.start.isoformat(),self.end.isoformat(),purchase=saved)
            self.assertEqual(client.post('/api/trade-close-overrides',json=payload).status_code,409)

    def test_close_intent_rechecks_override_before_recording(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(round_history,'ROOT',Path(folder)):
            purchase=dict(self.purchase,ignore_stop_loss=True,ignore_early_profit=True)
            round_history.upsert('BTC',self.start.isoformat(),self.end.isoformat(),purchase=purchase)
            for reason in ('stop_loss','close_conditions'):
                self.assertFalse(app.record_robinhood_purchase('BTC',self.start,self.end,
                    {'target':83000,'close_order':{'status':'attempted','reason':reason}}))
            self.assertNotIn('close_order',round_history.load('BTC')[0]['purchase'])
