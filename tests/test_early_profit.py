import unittest
from copy import deepcopy
from datetime import datetime,timedelta
import order_settings as settings
import robinhood_target as rh


class EarlyProfitTests(unittest.TestCase):
    def setUp(self):
        self.start=datetime.now().astimezone()-timedelta(minutes=6)
        self.end=self.start+timedelta(minutes=15)
        self.prediction={'target':83000,'purchase':{'status':'filled','target':83000,
            'direction':'UP / ABOVE','contracts':2,'execution_price_cents':50},
            'order_settings':dict(settings.DEFAULTS,close_conditions=dict(settings.CLOSE_DEFAULTS,
                enabled=True,profit_per_ct_cents=6.12))}

    def matches(self,price):
        return rh.close_conditions_match(self.prediction,price,self.start,self.end)

    def test_strict_net_profit_per_ct_in_cents_with_fees(self):
        self.assertAlmostEqual(rh.estimated_close_profit(self.prediction['purchase'],60),.1224)
        self.assertEqual(rh.estimated_close_profit_per_ct_cents(self.prediction['purchase'],60),6.12)
        self.assertFalse(self.matches(60))
        self.assertTrue(self.matches(60.01))
        self.assertTrue(self.matches(99))
        self.assertFalse(self.matches(51))
        self.prediction['purchase']['contracts']=1
        self.assertTrue(self.matches(60.01))
        self.prediction['purchase'].update(contracts=2,fees_dollars=.1)
        self.assertFalse(self.matches(60.01))

    def test_execution_price_precedes_limit_and_no_price_never_closes(self):
        purchase=self.prediction['purchase']
        purchase.update(limit_price_cents=90)
        self.assertTrue(self.matches(70))
        del purchase['execution_price_cents']
        self.assertFalse(self.matches(70))
        del purchase['limit_price_cents']
        self.assertFalse(self.matches(99))
        for price in (None,True,0,101,float('nan'),float('inf')):
            self.assertIsNone(rh.estimated_close_profit(purchase,price))

    def test_down_contract_and_fallback_threshold_are_independent(self):
        self.prediction['purchase']['direction']='DOWN / BELOW'
        fallback=settings.validate(dict(settings.DEFAULTS,fallbacks=[{
            'close_conditions':dict(settings.CLOSE_DEFAULTS,enabled=True,profit_per_ct_cents=15)}]))['fallbacks'][0]
        self.assertTrue(self.matches(60.01))
        self.prediction['purchase'].update(purchase_rule='fallback',conditions={'rule':'fallback','fallback':deepcopy(fallback)})
        self.assertFalse(self.matches(60.01))
        self.assertTrue(self.matches(70))

    def test_validation_and_migration_remove_price_range(self):
        for value in (-1,True,None,'',float('nan'),float('inf')):
            with self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS,close_conditions={'profit_per_ct_cents':value}))
            with self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS,fallbacks=[{'close_conditions':{'profit_per_ct_cents':value}}]))
        old={'enabled':True,'min_price_cents':85,'max_price_cents':95}
        migrated=settings.validate(dict(settings.DEFAULTS,close_conditions=old,fallbacks=[{'close_conditions':old}]))
        self.assertNotIn('min_price_cents',migrated['close_conditions'])
        self.assertNotIn('max_price_cents',migrated['fallbacks'][0]['close_conditions'])
        self.assertEqual(migrated['close_conditions']['profit_per_ct_cents'],0)

    def test_ct_price_and_profit_must_both_strictly_exceed_thresholds(self):
        close=self.prediction['order_settings']['close_conditions']
        close.update(profit_per_ct_cents=0,ct_price_above_cents=60)
        self.assertFalse(self.matches(60))
        self.assertFalse(self.matches(59.99))
        self.assertTrue(self.matches(60.01))
        close['profit_per_ct_cents']=50
        self.assertFalse(self.matches(60.01))
        for direction in ('UP / ABOVE','DOWN / BELOW'):
            self.prediction['purchase']['direction']=direction
            close['profit_per_ct_cents']=0
            self.assertTrue(self.matches(60.01))

    def test_ct_price_validation_for_primary_and_fallback(self):
        for value in (-1,100,True,None,'',float('nan'),float('inf')):
            for location in ('primary','fallback'):
                data=dict(settings.DEFAULTS)
                rule={'ct_price_above_cents':value}
                if location=='primary':data['close_conditions']=rule
                else:data['fallbacks']=[{'close_conditions':rule}]
                with self.assertRaises(ValueError):settings.validate(data)
        checked=settings.validate(dict(settings.DEFAULTS,close_conditions={'ct_price_above_cents':75},
            fallbacks=[{'close_conditions':{'ct_price_above_cents':85}}]))
        self.assertEqual(checked['close_conditions']['ct_price_above_cents'],75)
        self.assertEqual(checked['fallbacks'][0]['close_conditions']['ct_price_above_cents'],85)

    def test_same_per_ct_threshold_for_any_position_size(self):
        close=self.prediction['order_settings']['close_conditions']
        close['profit_per_ct_cents']=20
        for quantity in (1,2,3,10,100):
            self.prediction['purchase']['contracts']=quantity
            self.assertFalse(self.matches(73.88))
            self.assertTrue(self.matches(73.89))
            self.assertEqual(rh.estimated_close_profit_per_ct_cents(self.prediction['purchase'],73.88),20)
        self.prediction['purchase'].update(contracts=2,fees_dollars=.08)
        self.assertEqual(rh.estimated_close_profit_per_ct_cents(self.prediction['purchase'],80),24.06)

    def test_existing_entered_threshold_keeps_number_in_cents(self):
        checked=settings.validate(dict(settings.DEFAULTS,
            close_conditions={'profit_dollars':20},
            fallbacks=[{'close_conditions':{'profit_dollars':15}}]))
        self.assertEqual(checked['close_conditions']['profit_per_ct_cents'],20)
        self.assertEqual(checked['fallbacks'][0]['close_conditions']['profit_per_ct_cents'],15)
        self.assertNotIn('profit_dollars',checked['close_conditions'])
