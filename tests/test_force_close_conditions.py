import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
import order_settings as settings
import robinhood_target as rh


class ForceCloseConditionsTests(unittest.TestCase):
    def setUp(self):
        self.start=datetime.now().astimezone()-timedelta(minutes=3)
        self.end=self.start+timedelta(minutes=15)
        self.rule=dict(settings.FORCE_CLOSE_DEFAULTS,enabled=True,min_price_cents=50,
                       minute_from=2,minute_to=5,max_brti_delta_dollars=0,time_in_force='good_till_date')
        self.p={'target':83000,'brti_price':82990,
                'purchase':{'status':'filled','target':83000,'direction':'UP / ABOVE','contracts':1,'attempted_at':'trade-token','force_close_token':'trade-token'},
                'order_settings':dict(settings.DEFAULTS,force_close=self.rule,stop_loss={'enabled':False})}

    def test_strict_price_directional_gap_and_window(self):
        now=self.start+timedelta(minutes=3)
        self.assertTrue(rh.force_close_matches(self.p,51,self.start,self.end,now))
        for price in (50,49,101,True,float('nan')):
            self.assertFalse(rh.force_close_matches(self.p,price,self.start,self.end,now))
        for minutes in (1.99,5,15):
            self.assertFalse(rh.force_close_matches(self.p,51,self.start,self.end,self.start+timedelta(minutes=minutes)))
        self.p['brti_price']=83000
        self.assertFalse(rh.force_close_matches(self.p,51,self.start,self.end,now))
        self.p['purchase']['direction']='DOWN / BELOW'
        self.p['brti_price']=83010
        self.assertTrue(rh.force_close_matches(self.p,51,self.start,self.end,now))

    def test_off_delta_missing_data_disabled_pending_close(self):
        self.rule['max_brti_delta_dollars']=None
        self.p['brti_price']=None
        self.assertTrue(rh.force_close_matches(self.p,51,self.start,self.end))
        self.rule['enabled']=False
        self.assertFalse(rh.force_close_matches(self.p,51,self.start,self.end))
        self.rule['enabled']=True
        self.p['purchase']['close_order']={'status':'submitted'}
        self.assertFalse(rh.force_close_matches(self.p,51,self.start,self.end))

    def test_validation_and_persistence(self):
        self.assertFalse(settings.validate(settings.DEFAULTS)['force_close']['enabled'])
        for update in ({'min_price_cents':100},{'min_price_cents':True},{'minute_from':5,'minute_to':5},
                       {'enabled':'yes'},{'max_brti_delta_dollars':float('nan')},{'time_in_force':'bad'}):
            with self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS,force_close=dict(self.rule,**update)))
        with tempfile.TemporaryDirectory() as folder,patch.object(settings,'PATH',Path(folder)/'settings.json'):
            settings.save(dict(settings.DEFAULTS,force_close=self.rule))
            self.assertEqual(settings.load()['force_close'],self.rule)

    def test_review_preserves_independent_tif_and_close_reason(self):
        self.p['order_settings']['force_close']=dict(self.rule)
        self.p['purchase'].update(attempted_at='trade-token',force_close_token='trade-token')
        page,submit,review,record=Mock(),Mock(),Mock(),Mock(return_value=True)
        review.inner_text.return_value='Order Summary Yes · $83,000 or above Contracts 1 Limit Price 60¢ Time in Force Good till date'
        with patch.object(rh,'close_review_controls',return_value=[(submit,review)]),patch.object(rh,'observe_close_receipt'):
            self.assertTrue(rh.submit_close_review(page,'BTC',self.start,self.end,self.p,Mock(),record,lambda *args:deepcopy(self.p),1,60))
        close=record.call_args_list[0].args[3]['close_order']
        self.assertEqual(close['reason'],'force_close')
        self.assertEqual(close['time_in_force'],'good_till_date')
