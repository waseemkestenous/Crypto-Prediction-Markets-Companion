import unittest
from unittest.mock import Mock
from datetime import datetime, timedelta
import robinhood_target as rh
import order_settings as settings


class LiveStrengthTests(unittest.TestCase):
    def test_moderate_or_strong_and_missing_values(self):
        for confidence,allowed in ((.54,False),(.55,True),(.65,True),(None,False),(float('nan'),False),(True,False)):
            self.assertEqual(rh.live_strength_matches({'live_snapshot':{'confidence':confidence}}),allowed)

    def test_custom_minimum_and_final_recheck(self):
        p={'live_snapshot':{'confidence':.57},'order_settings':{'min_live_strength':'STRONG'}}
        self.assertFalse(rh.live_strength_matches(p))
        p['live_snapshot']['confidence']=.7
        self.assertTrue(rh.live_strength_matches(p))
        current=dict(p,live_snapshot={'confidence':.54})
        self.assertFalse(rh.buy_prediction_matches(current,p))

    def test_weak_live_never_touches_browser(self):
        now=datetime.now().astimezone()
        p={'direction':'UP / ABOVE','model_directions':dict.fromkeys(('Final prediction',*settings.MODEL_NAMES),'UP / ABOVE'),
           'live_snapshot':{'direction':'UP / ABOVE','confidence':.53}}
        page=Mock()
        self.assertFalse(rh.prepare_contract_form(page,'BTC',now-timedelta(minutes=1),now+timedelta(minutes=14),lambda *args:p,Mock()))
        self.assertEqual(page.mock_calls,[])

    def test_validates_and_defaults(self):
        self.assertEqual(settings.validate(settings.DEFAULTS)['min_live_strength'],'MODERATE')
        with self.assertRaises(ValueError):
            settings.validate(dict(settings.DEFAULTS,min_live_strength='invalid'))


class BuyChangeReasonTests(unittest.TestCase):
    def test_changed_field_is_reported(self):
        with unittest.mock.patch.object(rh, 'live_strength_matches', return_value=True):
            reason = rh.buy_prediction_change_reason({'brti_price':83001}, {'brti_price':83000})
        self.assertIn('brti_price', reason)
        self.assertIn('retrying', reason)

    def test_unavailable_prediction_is_reported(self):
        self.assertIn('became unavailable', rh.buy_prediction_change_reason(None, {}))
