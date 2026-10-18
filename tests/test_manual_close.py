import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app
import round_history as journal


class ManualCloseTests(unittest.TestCase):
    def test_confirm_persists_and_rejects_stale_trade(self):
        start='2026-10-17T15:30:00-05:00'
        end='2026-10-17T15:45:00-05:00'
        purchase={'status':'filled','contracts':1,'target':83000,'attempted_at':'buy',
                  'btc_target_diff_dollars':12.5,
                  'close_order':{'status':'submitted','contracts':1,'attempted_at':'close',
                                 'limit_price_cents':64,'reason':'close_conditions'}}
        with tempfile.TemporaryDirectory() as folder, patch.object(journal,'ROOT',Path(folder)), patch.object(app.ARGS,'asset','BTC'), patch.object(app,'robinhood_report'):
            journal.upsert('BTC',start,end,purchase=purchase)
            client=app.app.test_client()
            payload=dict(asset='BTC',round_start=start,purchase_token='buy',close_token='close',price_cents=61.5)
            self.assertEqual(client.post('/api/purchase-closed',json=dict(payload,purchase_token='old')).status_code,409)
            self.assertEqual(journal.get('BTC',start)['purchase']['close_order']['status'],'submitted')
            for invalid in (None, True, -1, 101, '64'):
                self.assertEqual(client.post('/api/purchase-closed',json=dict(payload,price_cents=invalid)).status_code,400)
            self.assertEqual(client.post('/api/purchase-closed',json=payload).status_code,200)
            saved=journal.get('BTC',start)['purchase']
            self.assertEqual(saved['close_order']['status'],'filled')
            self.assertEqual(saved['close_order']['confirmed_by'],'user')
            self.assertEqual(saved['close_order']['limit_price_cents'],64)
            self.assertEqual(saved['close_order']['execution_price_cents'],61.5)
            self.assertIsNotNone(journal.trade_pnl(dict(saved,direction='UP / ABOVE',limit_price_cents=50),None))
            self.assertEqual(saved['btc_target_diff_dollars'],12.5)
            self.assertEqual(client.post('/api/purchase-closed',json=payload).status_code,200)
            self.assertEqual(client.post('/api/purchase-closed',json=dict(payload,asset='ETH')).status_code,409)
            self.assertEqual(client.post('/api/purchase-closed',json=[]).status_code,400)
            journal.upsert('BTC',start,end,purchase=dict(purchase,close_order=None))
            self.assertEqual(client.post('/api/purchase-closed',json=payload).status_code,409)
