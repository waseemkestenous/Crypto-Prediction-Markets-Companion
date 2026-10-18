import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
import app


class OpenPnlTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now().astimezone()
        self.row={'asset':'BTC','round_start':(self.now-timedelta(minutes=1)).isoformat(),
                  'round_end':(self.now+timedelta(minutes=14)).isoformat(),'target':83000,
                  'purchase':{'status':'filled','target':83000,'direction':'UP / ABOVE',
                              'contracts':1,'execution_price_cents':70,'attempted_at':'test'}}
        self.quote={'asset':'BTC','round_start':self.row['round_start'],'target':83000,
                    'direction':'UP / ABOVE','purchase_token':'test','bid_cents':80,
                    'updated_at':self.now.isoformat()}

    def test_winning_and_fee_adjusted_loss(self):
        with patch.dict(app.STATE,holding_quote=self.quote):
            result=app.open_purchase_pnl(self.row,self.now)
            self.assertEqual(result['status'],'winning')
            self.assertAlmostEqual(result['net_pnl'],.0612)
            self.quote['bid_cents']=72
            self.assertEqual(app.open_purchase_pnl(self.row,self.now)['status'],'losing')

    def test_stale_wrong_cycle_and_closed_are_not_labeled_winning(self):
        with patch.dict(app.STATE,holding_quote=self.quote):
            self.assertEqual(app.open_purchase_pnl(self.row,self.now+timedelta(seconds=11))['status'],'unavailable')
            self.quote['purchase_token']='older'
            self.assertEqual(app.open_purchase_pnl(self.row,self.now)['status'],'unavailable')
            self.row['purchase']['close_order']={'status':'filled'}
            self.assertIsNone(app.open_purchase_pnl(self.row,self.now))
