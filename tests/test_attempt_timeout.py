import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import app
import robinhood_target as rh
import round_history as journal


class AttemptTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now().astimezone()
        self.start=self.now-timedelta(minutes=1)
        self.end=self.start+timedelta(minutes=15)

    def run_timeout(self, seconds=31, filled=False, status='attempted', fail_reload=False):
        with tempfile.TemporaryDirectory() as folder,patch.object(journal,'ROOT',Path(folder)):
            journal.upsert('BTC',self.start.isoformat(),self.end.isoformat(),purchase={
                'status':status,'target':83000,'direction':'UP / ABOVE','contracts':1,
                'attempted_at':(self.now-timedelta(seconds=seconds)).isoformat()})
            def snapshot(*args):
                return {'target':83000,'purchase':journal.load('BTC')[0]['purchase']}
            page=Mock()
            if fail_reload:
                page.reload.side_effect=[RuntimeError('reload failed'),None]
            with patch.object(rh,'observe_filled_order',return_value=filled),patch.object(app,'now_local',return_value=self.now):
                if fail_reload:
                    with self.assertRaisesRegex(RuntimeError,'reload failed'):
                        rh.expire_unconfirmed_attempt(page,'BTC',self.start,self.end,snapshot,app.record_robinhood_purchase,Mock())
                result=rh.expire_unconfirmed_attempt(page,'BTC',self.start,self.end,snapshot,app.record_robinhood_purchase,Mock())
                saved=journal.load('BTC')[0]
                row=saved.get('purchase') or saved.get('removed_purchase')
                if result:
                    self.assertIsNone(saved['purchase'])
                    self.assertTrue(saved['purchase_removed'])
                    self.assertEqual(saved['purchase_removal_revision'],1)
                self.assertFalse(rh.expire_unconfirmed_attempt(page,'BTC',self.start,self.end,snapshot,app.record_robinhood_purchase,Mock()))
            return result,page,row

    def test_expire_and_refresh_once(self):
        result,page,row=self.run_timeout()
        self.assertTrue(result)
        self.assertEqual(row['status'],'cancelled')
        self.assertFalse(row['timeout_refresh_pending'])
        page.reload.assert_called_once_with(wait_until='domcontentloaded',timeout=60000)

    def test_wait_and_confirmed_fill_take_precedence(self):
        for options in ({'seconds':10},{'filled':True},{'status':'filled'},{'status':'submitted'}):
            result,page,row=self.run_timeout(**options)
            self.assertFalse(result)
            page.reload.assert_not_called()
            self.assertNotEqual(row['status'],'cancelled')

    def test_failed_refresh_is_retried_without_recancelling(self):
        result,page,row=self.run_timeout(fail_reload=True)
        self.assertTrue(result)
        self.assertEqual(page.reload.call_count,2)
        self.assertFalse(row['timeout_refresh_pending'])
