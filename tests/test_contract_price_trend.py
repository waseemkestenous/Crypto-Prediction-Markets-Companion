import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock

import robinhood_target as rh
import test_order_settings


class ContractTrendTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime.now().astimezone() - timedelta(minutes=1)
        self.end = self.start + timedelta(minutes=15)

    def prediction(self, values):
        return {'target': 83000, 'contract_price_history': [
            {'target': 83000, 'observed_at': (self.start + timedelta(seconds=i)).isoformat(),
             'yes_cents': value, 'no_cents': 101-value}
            for i, value in enumerate(values)],
            'contract_prices': {'yes_cents': values[-1], 'no_cents': 101-values[-1]}}

    def allowed(self, prediction, direction='UP / ABOVE'):
        return rh.contract_price_rising(prediction, direction, self.start, self.end)

    def test_requires_ten_changes_not_ten_samples(self):
        self.assertFalse(self.allowed(self.prediction(list(range(30,40)))))
        self.assertTrue(self.allowed(self.prediction(list(range(30,41)))))

    def test_configurable_trend_window(self):
        p=self.prediction([40,39,38,37,36,35,34,33,32,33,34])
        self.assertFalse(self.allowed(p))
        p['order_settings']={'contract_trend_changes':2}
        self.assertTrue(self.allowed(p))
        p['order_settings']['contract_trend_changes']=11
        self.assertFalse(self.allowed(p))

    def test_selected_side_and_dips(self):
        rising=self.prediction(list(range(30,41)))
        self.assertTrue(self.allowed(rising))
        self.assertFalse(self.allowed(rising, 'DOWN / BELOW'))
        falling=self.prediction(list(range(40,29,-1)))
        self.assertTrue(self.allowed(falling, 'DOWN / BELOW'))
        self.assertTrue(self.allowed(self.prediction([30,31,32,31,34,35,36,37,38,39,40])))
        self.assertTrue(self.allowed(self.prediction([40,30]+list(range(31,41)))))

    def test_repeats_do_not_count_and_missing_quotes_block(self):
        self.assertFalse(self.allowed(self.prediction([30]*10+[31])))
        p=self.prediction(list(range(30,41)))
        p['contract_prices']=None
        self.assertFalse(self.allowed(p))
        self.assertFalse(self.allowed({}))

    def test_flat_overall_trend_and_mixed_downtrend_block(self):
        self.assertFalse(self.allowed(self.prediction([30,31,30,31,30,31,30,31,30,31,30])))
        self.assertFalse(self.allowed(self.prediction([40,39,40,37,38,35,36,33,34,31,32])))

    def test_small_last_dip_keeps_positive_overall_trend(self):
        self.assertTrue(self.allowed(self.prediction([30,31,32,33,34,35,36,37,38,39,38])))

    def test_wrong_round_target_and_invalid_quote_block(self):
        for field,value in (('target',82000),('observed_at',self.end.isoformat()),('yes_cents',float('nan'))):
            p=self.prediction(list(range(30,41)))
            p['contract_price_history'][-1][field]=value
            self.assertFalse(self.allowed(p))

    def test_missing_trend_blocks_primary_and_fallback_before_browser(self):
        from order_settings import DEFAULTS, FALLBACK_DEFAULTS
        p={'direction':'UP / ABOVE','target':83000,'model_directions':dict.fromkeys(('Final prediction','Pattern','Recent','Bootstrap','Trend'),'UP / ABOVE'),
           'live_direction':'UP / ABOVE','live_snapshot':{'direction':'UP / ABOVE'},'brti_price':83100,
           'order_settings':dict(DEFAULTS,fallback=dict(FALLBACK_DEFAULTS,enabled=True,minute_from=0))}
        for fallback in (False,True):
            page=Mock()
            self.assertFalse(rh.prepare_contract_form(page,'BTC',self.start,self.end,lambda *args:p,Mock(),fallback=fallback))
            self.assertEqual(page.mock_calls,[])

    def test_drop_before_buy_prevents_submission(self):
        quotes=[{'yes_cents':40,'no_cents':61},{'yes_cents':39,'no_cents':62}]
        record=Mock(return_value=True)
        result,_,_=test_order_settings.SettingsTests().form(40,60,1,record,observed_quotes=quotes)
        self.assertFalse(result)
        record.assert_not_called()

    def test_price_updates_do_not_invalidate_prediction_identity(self):
        p=self.prediction(list(range(30,41)))
        self.assertTrue(rh.prediction_matches(dict(p,contract_prices={'yes_cents':41}),p))

