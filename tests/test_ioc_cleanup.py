import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import app
import robinhood_target as rh
import round_history as journal


class IocCleanupTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now().astimezone()
        self.start=self.now-timedelta(minutes=2)
        self.end=self.start+timedelta(minutes=15)
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        for context in (patch.object(journal,'ROOT',Path(self.folder.name)),
                        patch.object(app,'now_local',return_value=self.now),
                        patch.object(rh,'read_event_target',return_value=83000),
                        patch.object(rh,'observe_filled_order',return_value=False)):
            context.start();self.addCleanup(context.stop)
        self.clock=patch.object(rh,'datetime',wraps=datetime)
        clock=self.clock.start();self.addCleanup(self.clock.stop)
        clock.now.return_value=self.now
        self.page=Mock()
        self.page.locator.return_value.inner_text.return_value='Order canceled\nYes · $83,000 or above\nFilled quantity 0 of 2'
        self.report=Mock()

    def save(self,seconds=10,**fields):
        purchase=dict(status='submitted',target=83000,direction='UP / ABOVE',contracts=2,
                      conditions={'time_in_force':'ioc'},
                      attempted_at=(self.now-timedelta(seconds=seconds+1)).isoformat(),
                      submitted_at=(self.now-timedelta(seconds=seconds)).isoformat())
        purchase.update(fields)
        journal.upsert('BTC',self.start.isoformat(),self.end.isoformat(),target=83000,purchase=purchase)
        return purchase

    def prediction(self,*args):
        row=journal.get('BTC',self.start.isoformat()) or {}
        return {'target':83000,'purchase':row.get('purchase'),'purchase_exists':bool(row.get('purchase'))}

    def run_cleanup(self):
        return rh.clear_cancelled_ioc(self.page,'BTC',self.start,self.end,self.prediction,
                                      app.record_robinhood_purchase,self.report)

    def test_cancelled_zero_fill_is_removed_after_ten_seconds_and_archived(self):
        self.save()
        self.assertTrue(self.run_cleanup())
        row=journal.get('BTC',self.start.isoformat())
        self.assertIsNone(row['purchase'])
        self.assertTrue(row['purchase_removed'])
        self.assertEqual(row['purchase_removal_revision'],1)
        self.assertEqual(row['removed_purchase']['status'],'cancelled')
        self.assertEqual(row['removed_purchase']['filled_quantity'],0)
        self.assertFalse(self.prediction()['purchase_exists'])
        self.assertFalse(self.run_cleanup())
        self.page.reload.assert_called_once_with(wait_until='domcontentloaded',timeout=60000)

    def test_attempted_orders_can_also_be_reconciled(self):
        self.save(status='attempted')
        self.assertTrue(self.run_cleanup())

    def test_uncertain_partial_filled_gtd_and_test_orders_stay_blocked(self):
        for changes in ({'seconds':9},{'status':'filled'},{'conditions':{'time_in_force':'good_till_date'}},{'test_mode':True}):
            with self.subTest(changes=changes):
                self.save(**changes)
                self.assertFalse(self.run_cleanup())
                self.assertIsNotNone(self.prediction()['purchase'])
        self.page.reload.assert_not_called()

    def test_receipt_must_confirm_zero_fill_side_and_target(self):
        for text in ('Order submitted\nYes · $83,000 or above\nFilled quantity 0 of 2',
                     'Order canceled\nYes · $83,000 or above\nFilled quantity 1 of 2',
                     'Order canceled\nYes · $83,000 or above',
                     'Order canceled\nNo · $83,000 or above\nFilled quantity 0 of 2',
                     'Order canceled\nYes · $84,000 or above\nFilled quantity 0 of 2'):
            with self.subTest(text=text):
                self.save()
                self.page.locator.return_value.inner_text.return_value=text
                self.assertFalse(self.run_cleanup())
        self.page.reload.assert_not_called()

    def test_full_fill_observation_wins(self):
        self.save()
        with patch.object(rh,'observe_filled_order',return_value=True):
            self.assertFalse(self.run_cleanup())
        self.page.reload.assert_not_called()

    def test_refresh_failure_keeps_block_until_refresh_succeeds(self):
        self.save()
        self.page.reload.side_effect=[RuntimeError('Refresh failed'),None]
        with self.assertRaises(RuntimeError):self.run_cleanup()
        self.assertTrue(self.prediction()['purchase_exists'])
        self.assertTrue(self.run_cleanup())
        self.assertIsNone(self.prediction()['purchase'])

    def test_backend_rejects_stale_attempt_token(self):
        old=self.save()
        self.assertFalse(app.record_robinhood_purchase('BTC',self.start,self.end,{
            'status':'cancelled','target':83000,'cancellation_reason':'ioc_cancelled_unfilled',
            'expected_attempted_at':'other-attempt','expected_submitted_at':old['submitted_at'],
            'filled_quantity':0,'receipt_contracts':2,'confirmed_by':'visible_cancelled_ioc_receipt'}))
        self.assertEqual(self.prediction()['purchase']['status'],'submitted')
