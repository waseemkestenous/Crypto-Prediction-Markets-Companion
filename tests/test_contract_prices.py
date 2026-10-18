import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
import app
import robinhood_target as rh
import round_history


class ContractPriceTests(unittest.TestCase):
    def test_round_price_changes_are_saved_and_isolated(self):
        start = datetime.now().astimezone()
        end = start + timedelta(minutes=15)
        with tempfile.TemporaryDirectory() as folder, patch.object(round_history, 'ROOT', Path(folder)), patch.object(app, 'current_round_key', return_value=(start, end)), patch.object(app.ARGS, 'asset', 'BTC'), patch.dict(app.STATE, target=83471.65, round_start=start, round_end=end, contract_prices=None):
            for seconds, yes in ((0, 40), (5, 40), (10, 42), (15, 40)):
                with patch.object(app, 'now_local', return_value=start + timedelta(seconds=seconds)):
                    app.record_contract_prices('BTC', start, end, 83471.65,
                                               {'yes_cents': yes, 'no_cents': 101 - yes})
            samples = round_history.load('BTC')[0]['contract_price_history']
            self.assertEqual([s['yes_cents'] for s in samples], [40, 42, 40])
            self.assertEqual([s['no_cents'] for s in samples], [61, 59, 61])
            self.assertEqual(samples[1]['observed_at'], (start + timedelta(seconds=10)).isoformat())
            with patch.object(app, 'now_local', return_value=start + timedelta(seconds=30)):
                result = app.app.test_client().get('/api/state').get_json()
                self.assertIsNone(result['contract_prices'])
                self.assertEqual(result['contract_price_history'], samples)
            self.assertEqual(app.current_contract_price_history(end), [])
            with patch.object(app, 'now_local', return_value=end):
                app.record_contract_prices('BTC', start, end, 83471.65,
                                           {'yes_cents': 50, 'no_cents': 51})
            self.assertEqual(round_history.load('BTC')[0]['contract_price_history'], samples)

    def test_header_decimal_and_no_formula(self):
        prices=rh.parse_contract_prices('$83,471.65 or above 0.7¢',83471.65)
        self.assertEqual(prices['yes_cents'],.7)
        self.assertEqual(prices['no_cents'],100.3)

    def test_selector_preferred_over_header(self):
        prices=rh.parse_contract_prices('$83,471.65 or above 0.7¢ Select a contract $83,471.65 or above\n0.1¢',83471.65)
        self.assertEqual(prices['yes_cents'],.1)
        self.assertEqual(prices['no_cents'],100.9)

    def test_missing_wrong_target_and_ambiguous_rejected(self):
        for text in ('$83,000 or above 20¢','$83,471.65 target price 20¢','$83,471.65 or above 20¢ $83,471.65 or above 21¢','$83,471.65 or above 101¢'):
            with self.subTest(text=text),self.assertRaises(ValueError):
                rh.parse_contract_prices(text,83471.65)

    def test_api_hides_stale_or_wrong_round_quotes(self):
        start=datetime.now().astimezone();end=start+timedelta(minutes=15)
        prices=rh.parse_contract_prices('$83,471.65 or above 40¢',83471.65)
        with tempfile.TemporaryDirectory() as folder,patch.object(round_history,'ROOT',Path(folder)),patch.object(app,'current_round_key',return_value=(start,end)),patch.object(app,'now_local',return_value=start),patch.object(app.ARGS,'asset','BTC'),patch.dict(app.STATE,target=83471.65,round_start=start,contract_prices=None):
            self.assertTrue(app.record_contract_prices('BTC',start,end,83471.65,prices))
            result=app.app.test_client().get('/api/state').get_json()['contract_prices']
            self.assertEqual(result['yes_cents'],40)
            self.assertEqual(result['no_cents'],61)
            with patch.object(app,'now_local',return_value=start+timedelta(seconds=11)):
                self.assertIsNone(app.current_contract_prices(start))
            self.assertIsNone(app.current_contract_prices(end))
            self.assertFalse(app.record_contract_prices('BTC',start,end,83000,prices))
            self.assertIsNone(app.current_contract_prices(start))
