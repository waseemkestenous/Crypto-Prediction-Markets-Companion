import unittest
import tempfile
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
import app
import round_history as journal

class PurchasePriceTests(unittest.TestCase):
    def test_price_edit_persists_preserves_snapshot_and_recalculates_pnl(self):
        start=datetime.now().astimezone()-timedelta(hours=1);end=start+timedelta(minutes=15)
        purchase={'status':'filled','direction':'UP / ABOVE','contracts':5,'limit_price_cents':87,'conditions':{'rule':'fallback'},'purchase_minute':13.4}
        with tempfile.TemporaryDirectory() as folder,patch.object(journal,'ROOT',Path(folder)),patch.object(app.ARGS,'asset','BTC'),patch.dict(app.STATS,rounds=[]):
            journal.upsert('BTC',app.dt_iso(start),app.dt_iso(end),purchase=purchase,actual='UP / ABOVE',target=83000,settlement=83100)
            client=app.app.test_client();body={'asset':'BTC','round_start':app.dt_iso(start),'price_cents':85.5}
            self.assertEqual(client.post('/api/purchase-price',json=body).status_code,200)
            row=journal.load('BTC')[0];p=row['purchase']
            self.assertEqual(p['limit_price_cents'],85.5)
            self.assertEqual(p['original_limit_price_cents'],87)
            self.assertEqual(p['conditions'],purchase['conditions'])
            self.assertEqual(p['purchase_minute'],13.4)
            self.assertAlmostEqual(journal.purchase_pnl(row),5*(1-.855-.0194))
            body['price_cents']=86
            self.assertEqual(client.post('/api/purchase-price',json=body).status_code,200)
            self.assertEqual(journal.load('BTC')[0]['purchase']['original_limit_price_cents'],87)
            for invalid in (None,True,0,-1,101,'bad',float('nan')):
                body['price_cents']=invalid
                self.assertEqual(client.post('/api/purchase-price',json=body).status_code,400)
            self.assertEqual(journal.load('BTC')[0]['purchase']['limit_price_cents'],86)
            body.update(price_cents=80,asset='ETH')
            self.assertEqual(client.post('/api/purchase-price',json=body).status_code,409)
            body.update(asset='BTC',round_start='missing')
            self.assertEqual(client.post('/api/purchase-price',json=body).status_code,404)
