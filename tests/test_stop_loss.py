import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import app
import order_settings as settings
import robinhood_target as rh
import round_history as journal


class StopLossTests(unittest.TestCase):
    def test_stop_loss_minute_window_boundaries(self):
        self.p['order_settings']['stop_loss'].update(minute_from=5,minute_to=12)
        for minute,allowed in ((4.99,False),(5,True),(11.99,True),(12,False),(15,False)):
            self.assertEqual(rh.stop_loss_matches(self.p,49,self.start,self.end,
                self.start+timedelta(minutes=minute)),allowed)

    def test_invalid_stop_loss_minutes(self):
        for first,last in ((-1,15),(0,16),(5,5),(10,5),(True,15),(0,None)):
            with self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS,stop_loss={'minute_from':first,'minute_to':last}))

    def setUp(self):
        self.start=datetime.now().astimezone()-timedelta(minutes=1)
        self.end=self.start+timedelta(minutes=15)
        self.p={'target':83000,'purchase':{'target':83000,'status':'filled',
                'direction':'UP / ABOVE','execution_price_cents':70,'limit_price_cents':80,'stop_loss_peak_bid_cents':70},
                'order_settings':dict(settings.DEFAULTS,stop_loss={'enabled':True,'drop_cents':20})}

    def test_exact_threshold_and_both_sides_ignore_normal_close_conditions(self):
        for direction in ('UP / ABOVE','DOWN / BELOW'):
            self.p['purchase']['direction']=direction
            self.assertTrue(rh.sale_conditions_match(self.p,50,self.start,self.end))
            self.assertTrue(rh.sale_conditions_match(self.p,49,self.start,self.end))
            self.assertFalse(rh.sale_conditions_match(self.p,50.01,self.start,self.end))

    def test_peak_not_buy_price_and_invalid_holdings(self):
        self.p['purchase']['stop_loss_peak_bid_cents']=90
        self.assertTrue(rh.stop_loss_matches(self.p,70,self.start,self.end))
        self.assertFalse(rh.stop_loss_matches(self.p,71,self.start,self.end))
        del self.p['purchase']['stop_loss_peak_bid_cents']
        self.assertFalse(rh.stop_loss_matches(self.p,50,self.start,self.end))

        self.p['purchase']['stop_loss_peak_bid_cents']=70
        self.p['purchase']['status']='submitted'
        self.assertFalse(rh.stop_loss_matches(self.p,50,self.start,self.end))
        self.p['purchase']['status']='filled'
        self.assertFalse(rh.stop_loss_matches(self.p,50,self.start,self.end,self.end))
        self.p['order_settings']['stop_loss']['enabled']=False
        self.assertFalse(rh.stop_loss_matches(self.p,50,self.start,self.end))


    def test_requires_both_trailing_drop_and_strict_price_ceiling(self):
        self.p['purchase']['stop_loss_peak_bid_cents']=90
        self.p['order_settings']['stop_loss']['max_price_cents']=60
        self.assertFalse(rh.stop_loss_matches(self.p,65,self.start,self.end))
        self.assertFalse(rh.stop_loss_matches(self.p,60,self.start,self.end))
        self.assertTrue(rh.stop_loss_matches(self.p,59.99,self.start,self.end))
        self.p['purchase']['stop_loss_peak_bid_cents']=65
        self.assertFalse(rh.stop_loss_matches(self.p,50,self.start,self.end))

    def test_signed_brti_delta_is_strict_and_combined_with_drop(self):
        for direction, sign in (('UP / ABOVE', 1), ('DOWN / BELOW', -1)):
            self.p['purchase']['direction'] = direction
            for threshold in (-30, 0, 30):
                self.p['order_settings']['stop_loss']['max_brti_delta_dollars'] = threshold
                for offset, allowed in ((-0.01, True), (0, False), (0.01, False)):
                    self.p['brti_price'] = 83000 + sign*(threshold+offset)
                    with self.subTest(direction=direction, threshold=threshold, offset=offset):
                        self.assertEqual(rh.stop_loss_matches(self.p, 49, self.start, self.end), allowed)
                        self.assertFalse(rh.stop_loss_matches(self.p, 60, self.start, self.end))

    def test_brti_delta_requires_valid_quote_and_is_optional(self):
        self.p['order_settings']['stop_loss']['max_brti_delta_dollars'] = 0
        for quote in (None, True, float('nan'), float('inf')):
            self.p['brti_price'] = quote
            self.assertFalse(rh.stop_loss_matches(self.p, 49, self.start, self.end))
        self.p['order_settings']['stop_loss']['max_brti_delta_dollars'] = None
        self.assertTrue(rh.stop_loss_matches(self.p, 49, self.start, self.end))

    def test_brti_delta_validation_and_api_persistence(self):
        for value in (True, '', 'invalid', float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS, stop_loss={'max_brti_delta_dollars': value}))
        with tempfile.TemporaryDirectory() as folder, patch.object(settings, 'PATH', Path(folder)/'settings.json'), patch.dict(app.ORDER_SETTINGS):
            for value in (-30, 0, 30, None):
                response = app.app.test_client().post('/api/order-settings', json=dict(
                    settings.DEFAULTS, stop_loss={'max_brti_delta_dollars': value}))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(settings.load()['stop_loss']['max_brti_delta_dollars'], value)

    def test_invalid_price_ceiling(self):
        for value in (0,-1,101,True,None,float('nan')):
            with self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS,stop_loss={'max_price_cents':value}))
    def test_validation_and_save(self):
        for drop in (0,-1,101,True,None,float('nan')):
            with self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS,stop_loss={'enabled':True,'drop_cents':drop}))
        with tempfile.TemporaryDirectory() as folder,patch.object(settings,'PATH',Path(folder)/'settings.json'),patch.dict(app.ORDER_SETTINGS):
            result=app.app.test_client().post('/api/order-settings',json=dict(settings.DEFAULTS,stop_loss={'enabled':True,'drop_cents':12.5,'max_price_cents':65.5,'minute_from':2,'minute_to':13,'time_in_force':'good_till_date'}))
            self.assertEqual(result.status_code,200)
            self.assertEqual(settings.load()['stop_loss']['drop_cents'],12.5)
            self.assertEqual(settings.load()['stop_loss']['max_price_cents'],65.5)
            self.assertEqual(settings.load()['stop_loss']['minute_from'],2)
            self.assertEqual(settings.load()['stop_loss']['minute_to'],13)
            self.assertEqual(settings.load()['stop_loss']['time_in_force'],'good_till_date')

    def test_peak_is_persisted_and_never_lowered(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(journal,'ROOT',Path(folder)):
            journal.upsert('BTC',self.start.isoformat(),self.end.isoformat(),purchase=self.p['purchase'])
            for peak in (85,90,80):
                self.assertTrue(app.record_robinhood_purchase('BTC',self.start,self.end,
                    {'target':83000,'stop_loss_peak_bid_cents':peak,'stop_loss_peak_bid_at':self.start.isoformat()}))
            purchase=journal.load('BTC')[0]['purchase']
            self.assertEqual(purchase['stop_loss_peak_bid_cents'],90)
            self.assertEqual(purchase['execution_price_cents'],70)
            self.p['purchase']=purchase
            self.assertTrue(rh.stop_loss_matches(self.p,70,self.start,self.end))
