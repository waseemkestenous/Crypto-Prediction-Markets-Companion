import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, Mock
import robinhood_target as rh
import app
import round_history as journal

class RemovePurchaseTests(unittest.TestCase):
    def test_full_close_allows_next_purchase_and_preserves_completed_cycle(self):
        start=datetime.now().astimezone()-timedelta(minutes=3);end=start+timedelta(minutes=15)
        old={'status':'filled','target':83000,'direction':'UP / ABOVE','contracts':1,
             'close_order':{'status':'filled','filled_at':'receipt-time'}}
        with tempfile.TemporaryDirectory() as folder,patch.object(journal,'ROOT',Path(folder)):
            journal.upsert('BTC',app.dt_iso(start),app.dt_iso(end),purchase=old)
            page=Mock();prediction={'purchase_exists':False,'purchase':old}
            token=rh.refresh_removed_purchase(page,prediction,('BTC',start),None,Mock())
            page.reload.assert_called_once()
            self.assertEqual(rh.refresh_removed_purchase(page,prediction,('BTC',start),token,Mock()),token)
            self.assertEqual(page.reload.call_count,1)
            new={'status':'attempted','target':83000,'direction':'DOWN / BELOW','contracts':2}
            with patch.dict(app.ORDER_SETTINGS, allow_multi_buy=False):
                self.assertFalse(app.record_robinhood_purchase('BTC',start,end,new))
                self.assertEqual(journal.load('BTC')[0]['purchase'],old)
            self.assertTrue(app.record_robinhood_purchase('BTC',start,end,new))
            row=journal.load('BTC')[0]
            self.assertEqual(row['completed_purchases'],[old])
            self.assertEqual(row['purchase'],new)
            self.assertNotIn('close_order',row['purchase'])
            self.assertTrue(app.record_robinhood_purchase('BTC',start,end,{'status':'submitted','target':83000}))
            self.assertEqual(len(journal.load('BTC')[0]['completed_purchases']),1)
    def test_each_removal_refreshes_once_before_another_order(self):
        page, report = Mock(), Mock()
        key = ('BTC', 'round')
        prediction = {'purchase_exists': False, 'purchase_removal_revision': 1}
        token = rh.refresh_removed_purchase(page, prediction, key, None, report)
        page.reload.assert_called_once_with(wait_until='domcontentloaded', timeout=60000)
        self.assertEqual(rh.refresh_removed_purchase(page, prediction, key, token, report), token)
        self.assertEqual(page.reload.call_count, 1)
        prediction['purchase_removal_revision'] = 2
        self.assertNotEqual(rh.refresh_removed_purchase(page, prediction, key, token, report), token)
        self.assertEqual(page.reload.call_count, 2)
    def test_new_attempt_can_replace_removed_purchase_and_confirm_fill(self):
        start=datetime.now().astimezone()-timedelta(minutes=1);end=start+timedelta(minutes=15)
        old={'status':'submitted','target':83000,'contracts':1,'limit_price_cents':40}
        with tempfile.TemporaryDirectory() as folder,patch.object(journal,'ROOT',Path(folder)):
            journal.upsert('BTC',app.dt_iso(start),app.dt_iso(end),purchase=None,
                           purchase_removed=True,removed_purchase=old)
            new={'status':'attempted','direction':'UP / ABOVE','target':83000,'contracts':2,'limit_price_cents':45}
            self.assertTrue(app.record_robinhood_purchase('BTC',start,end,new))
            row=journal.load('BTC')[0]
            self.assertFalse(row['purchase_removed'])
            self.assertEqual(row['purchase'],new)
            self.assertEqual(row['removed_purchase'],old)
            self.assertTrue(app.record_robinhood_purchase('BTC',start,end,{'status':'submitted','target':83000}))
            self.assertTrue(app.record_robinhood_purchase('BTC',start,end,{
                'status':'filled','target':83000,'confirmed_by':'visible_order_receipt',
                'filled_quantity':2,'receipt_contracts':2}))
            self.assertEqual(journal.load('BTC')[0]['purchase']['status'],'filled')

    def test_remove_excludes_purchase_and_restore_preserves_details(self):
        start=datetime.now().astimezone()-timedelta(hours=1);end=start+timedelta(minutes=15)
        purchase={'status':'filled','direction':'UP / ABOVE','target':83000,'contracts':10,'limit_price_cents':92.1,'conditions':{'agreement_basis':'both'},'live_recommendation':{'direction':'UP / ABOVE'}}
        with tempfile.TemporaryDirectory() as folder,patch.object(journal,'ROOT',Path(folder)),patch.object(app.ARGS,'asset','BTC'),patch.dict(app.STATS,rounds=[]):
            journal.upsert('BTC',app.dt_iso(start),app.dt_iso(end),prediction='UP / ABOVE',actual='UP / ABOVE',target=83000,settlement=83100,status='settled',purchase=purchase)
            client=app.app.test_client();body={'asset':'BTC','round_start':app.dt_iso(start)}
            self.assertEqual(client.post('/api/purchase-remove',json=body).status_code,200)
            row=journal.load('BTC')[0]
            self.assertIsNone(row['purchase'])
            self.assertEqual(row['actual'],'UP / ABOVE')
            self.assertIsNone(journal.purchase_accuracy([row])['accuracy'])
            self.assertIsNone(journal.purchase_pnl(row))
            self.assertEqual(client.get('/api/round-history?purchases=only').get_json()['total'],0)
            self.assertTrue(app.record_robinhood_purchase('BTC',start,end,{'status':'filled','target':83000,'confirmed_by':'visible_order_receipt'}))
            self.assertIsNone(journal.load('BTC')[0]['purchase'])
            self.assertEqual(client.post('/api/purchase-filled',json=body).status_code,200)
            restored=journal.load('BTC')[0]
            self.assertFalse(restored['purchase_removed'])
            self.assertEqual(restored['purchase']['conditions'],purchase['conditions'])
            self.assertEqual(restored['purchase']['contracts'],10)
            self.assertEqual(restored['purchase']['live_recommendation'],purchase['live_recommendation'])

    def test_invalid_requests_do_not_create_rounds(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(journal,'ROOT',Path(folder)),patch.object(app.ARGS,'asset','BTC'),patch.dict(app.STATS,rounds=[]):
            client=app.app.test_client()
            self.assertEqual(client.post('/api/purchase-remove',json=[]).status_code,400)
            self.assertEqual(client.post('/api/purchase-remove',json={'asset':'ETH'}).status_code,409)
            self.assertEqual(client.post('/api/purchase-remove',json={'asset':'BTC','round_start':'missing'}).status_code,404)
            self.assertEqual(journal.load('BTC'),[])
