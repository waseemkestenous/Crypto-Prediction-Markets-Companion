import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from flask import Flask
import crypto_predict as crypto


class CryptoPredictTests(unittest.TestCase):
    def setUp(self):
        reader = patch.object(crypto.crypto_website.READER, 'read', return_value=(None, None))
        reader.start()
        self.addCleanup(reader.stop)
        self.app = Flask(__name__, template_folder='../templates')
        self.app.register_blueprint(crypto.blueprint)
        self.client = self.app.test_client()

    def test_five_minute_boundary_rolls_over(self):
        now = datetime(2026, 11, 14, 6, 9, 59, tzinfo=timezone.utc)
        start, end = crypto.round_bounds(now)
        self.assertEqual(start.minute, 5)
        self.assertEqual((end-start).total_seconds(), 300)
        self.assertEqual(crypto.round_bounds(end)[0], end)

    def test_target_validation_and_round_binding(self):
        start, _ = crypto.round_bounds(datetime.now(timezone.utc))
        with patch.object(crypto, 'monitor', crypto.CryptoMonitor()):
            for value in (None, True, 0, -1, 'nan', 'inf'):
                self.assertEqual(self.client.post('/api/crypto-com/target', json={
                    'target': value, 'round_start': start.isoformat()}).status_code, 400)
            self.assertEqual(self.client.post('/api/crypto-com/target', json=[]).status_code, 400)
            self.assertEqual(self.client.post('/api/crypto-com/target', json={
                'target': 80000, 'round_start': 'old'}).status_code, 409)
            self.assertEqual(self.client.post('/api/crypto-com/target', json={
                'target': 80000, 'round_start': start.isoformat()}).status_code, 200)

    def test_old_target_and_stale_quote_cannot_predict(self):
        m = crypto.CryptoMonitor()
        now = datetime.now(timezone.utc)
        start, _ = crypto.round_bounds(now)
        m.target = 80000
        m.target_round = (start-timedelta(minutes=5)).isoformat()
        m.quote = {'price': 81000, 'time': now-timedelta(seconds=30)}
        with patch.object(m, 'refresh'):
            result = m.snapshot()
        self.assertIsNone(result['target'])
        self.assertIsNone(result['prediction'])
        self.assertFalse(result['price_fresh'])

    def test_bootstrap_excludes_current_round_and_future_candles(self):
        start = datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc)
        end = start+timedelta(minutes=5)
        rows = [{'time': int(start.timestamp())-60*i, 'close': 80000.0}
                for i in range(1, 65)]
        rows += [{'time': int(start.timestamp())+60*i, 'close': 1.0} for i in range(5)]
        p, count = crypto.probability_above(rows, start, end, start, 80000, 81000)
        self.assertEqual(count, 63)
        self.assertEqual(p, 0)
        p, _ = crypto.probability_above(rows, start, end, start, 82000, 81000)
        self.assertEqual(p, 1)
        with self.assertRaisesRegex(ValueError, 'ended'):
            crypto.probability_above(rows, start, end, end, 82000, 81000)

    def test_page_shows_automation_limitation(self):
        response = self.client.get('/crypto-com')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Order preparation is not enabled', response.get_data(as_text=True))
        self.assertIn(crypto.MARKET_URL, response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
