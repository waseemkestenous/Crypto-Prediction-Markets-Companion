import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import app
import round_history as journal
import robinhood_target as rh

class PurchaseAccuracyTests(unittest.TestCase):
    def test_accuracy_includes_archived_closes_and_scores_actual_trade_result(self):
        win={'status':'filled','direction':'UP / ABOVE','contracts':1,'execution_price_cents':40,
             'close_order':{'status':'filled','contracts':1,'execution_price_cents':60}}
        loss=dict(win,close_order={'status':'filled','contracts':1,'execution_price_cents':30})
        pending=dict(win,close_order={'status':'submitted','contracts':1,'limit_price_cents':60})
        row={'actual':'DOWN / BELOW','completed_purchases':[win,loss,pending],
             'purchase':{'status':'filled','direction':'DOWN / BELOW'}}
        stats=journal.purchase_accuracy([row])
        self.assertEqual((stats['hits'],stats['misses'],stats['total']),(2,2,4))
        self.assertEqual(stats['accuracy'],50.0)
        self.assertEqual(stats['awaiting_result'],0)
        self.assertEqual(stats['filled_trades'],4)
    def test_round_pnl_and_balance_include_all_closed_and_settled_trades(self):
        closed = {'status': 'filled', 'direction': 'UP / ABOVE', 'contracts': 2,
                  'execution_price_cents': 40, 'fees_dollars': .04,
                  'close_order': {'status': 'filled', 'contracts': 2,
                                  'execution_price_cents': 60, 'fees_dollars': .05}}
        final = {'status': 'filled', 'direction': 'DOWN / BELOW', 'contracts': 1,
                 'execution_price_cents': 40, 'fees_dollars': .02}
        row = {'actual': 'UP / ABOVE', 'completed_purchases': [closed, closed], 'purchase': final}
        self.assertAlmostEqual(journal.purchase_pnl(row), .20)
        summary = journal.balance_summary([row], 10)
        self.assertAlmostEqual(summary['net_pnl'], .20)
        self.assertEqual(summary['counted_purchases'], 3)
        row['actual'] = None
        self.assertAlmostEqual(journal.purchase_pnl(row), .62)
        self.assertEqual(journal.balance_summary([row], 10)['pending_or_missing'], 1)

    def test_closed_trade_uses_limits_only_as_estimated_price_and_both_fees(self):
        row = {'purchase': {'status': 'filled', 'direction': 'UP / ABOVE',
               'contracts': 1, 'limit_price_cents': 50,
               'close_order': {'status': 'filled', 'contracts': 1, 'limit_price_cents': 61}}}
        self.assertAlmostEqual(journal.purchase_pnl(row), .0712)
        row['purchase']['close_order']['status'] = 'submitted'
        self.assertIsNone(journal.purchase_pnl(row))
    def test_own_attempt_and_live_timestamp_updates_do_not_cancel_buy(self):
        expected = {'target': 83000, 'direction': 'UP / ABOVE', 'purchase': {},
                    'purchase_exists': False, 'live_direction': 'UP / ABOVE',
                    'live_snapshot': {'direction': 'UP / ABOVE', 'p_up': .7, 'updated_at': 'old'}}
        current = dict(expected, purchase={'status': 'attempted'}, purchase_exists=True,
                       live_snapshot={'direction': 'UP / ABOVE', 'p_up': .72, 'updated_at': 'new'})
        self.assertTrue(rh.prediction_matches(current, expected))
        self.assertFalse(rh.prediction_matches(dict(current, live_snapshot=None), expected))
        self.assertFalse(rh.prediction_matches(dict(current, live_direction='DOWN / BELOW'), expected))
        self.assertFalse(rh.prediction_matches(dict(current, target=83001), expected))

    def test_buy_continues_after_its_attempt_is_saved(self):
        from copy import deepcopy
        from datetime import datetime, timedelta
        start = datetime.now().astimezone() - timedelta(minutes=1)
        end = start + timedelta(minutes=15)
        prediction = {'direction': 'UP / ABOVE', 'target': 83000,
                      'model_directions': dict.fromkeys(('Final prediction', 'Pattern', 'Recent', 'Bootstrap', 'Trend'), 'UP / ABOVE'),
                      'purchase': {}, 'purchase_exists': False,
                      'live_snapshot': {'direction': 'UP / ABOVE', 'p_up': .7}}
        page, quantity, limit, buy_in, buy, other = (Mock() for _ in range(6))
        buy_in.inner_text.return_value = 'Contracts'
        quantity.input_value.return_value = '1'
        limit.input_value.return_value = '40'
        other.all.return_value = []
        quantity.or_.return_value=quantity
        quantity.count.return_value=1
        page.get_by_role.side_effect = lambda role, **args: (
            buy_in if role == 'combobox' else quantity if 'Contracts' in str(args.get('name'))
            else limit if role == 'textbox' else buy if args.get('name') == 'Buy' else other)
        def record(asset, start, end, purchase):
            prediction['purchase'] = dict(prediction['purchase'], **purchase)
            prediction['purchase_exists'] = True
            return True
        with patch.object(rh, 'read_event_target', return_value=83000), \
             patch.object(rh,'review_buy_details',return_value=True), patch.object(rh, 'select_time_in_force'), \
             patch.object(rh, 'in_purchase_window', return_value=True), patch.object(rh,'contract_price_rising',return_value=True), patch.object(rh,'verify_contract_price_trend',return_value=True):
            self.assertTrue(rh.prepare_contract_form(page, 'BTC', start, end,
                lambda *args: deepcopy(prediction), Mock(), record))
        buy.click.assert_called_once_with(timeout=3000)
        self.assertEqual(prediction['purchase']['status'], 'submitted')
    def test_unfilled_close_settles_without_sale_fee(self):
        for status in ('attempted','submitted'):
            purchase={'status':'filled','direction':'UP / ABOVE','contracts':1,
                      'execution_price_cents':70,'fees_dollars':.0194,
                      'close_order':{'status':status,'contracts':1,'limit_price_cents':50,'fees_dollars':.0194}}
            self.assertAlmostEqual(journal.purchase_pnl({'purchase':purchase,'actual':'UP / ABOVE'}),.2806)
            self.assertAlmostEqual(journal.purchase_pnl({'purchase':purchase,'actual':'DOWN / BELOW'}),-.7194)
            self.assertIsNone(journal.purchase_pnl({'purchase':purchase}))

    def test_pnl_uses_purchase_direction_quantity_price_and_per_contract_fee(self):
        win={'actual':'DOWN / BELOW','prediction':'UP / ABOVE',
             'purchase':{'status':'filled','direction':'DOWN / BELOW','contracts':2,'limit_price_cents':49}}
        lose={'actual':'UP / ABOVE','purchase':dict(win['purchase'])}
        pending={'actual':None,'purchase':dict(win['purchase'])}
        missing={'actual':'DOWN / BELOW','purchase':{'status':'filled','direction':'DOWN / BELOW'}}
        self.assertAlmostEqual(journal.purchase_pnl(win),.9812)
        self.assertAlmostEqual(journal.purchase_pnl(lose),-1.0188)
        self.assertIsNone(journal.purchase_pnl(pending))
        self.assertIsNone(journal.purchase_pnl(missing))
        result=journal.balance_summary([win,lose,pending,missing],100)
        self.assertAlmostEqual(result['net_pnl'],-.0376)
        self.assertAlmostEqual(result['current_balance'],99.9624)
        self.assertEqual(result['counted_purchases'],2)
        self.assertEqual(result['pending_or_missing'],2)

    def test_only_filled_and_settled_orders_count(self):
        rows=[{'actual':'UP / ABOVE','purchase':{'status':'filled','direction':'UP / ABOVE'}},
              {'actual':'DOWN / BELOW','purchase':{'status':'filled','direction':'UP / ABOVE'}},
              {'actual':'UP / ABOVE','purchase':{'status':'submitted','direction':'UP / ABOVE'}},
              {'actual':'UP / ABOVE','hit':True},
              {'actual':None,'purchase':{'status':'filled','direction':'DOWN / BELOW'}}]
        result=journal.purchase_accuracy(rows)
        self.assertEqual(result['total'],2)
        self.assertEqual(result['accuracy'],50)
        self.assertEqual(result['awaiting_result'],1)
        self.assertEqual(result['unconfirmed_orders'],1)

    def test_pnl_prefers_observed_execution_price_over_limit(self):
        row = {'actual': 'UP / ABOVE', 'purchase': {
            'status': 'filled', 'direction': 'UP / ABOVE', 'contracts': 1,
            'limit_price_cents': 60, 'execution_price_cents': 56}}
        self.assertAlmostEqual(journal.purchase_pnl(row), .4206)

    def test_existing_filled_order_gets_execution_price_from_position(self):
        from datetime import datetime
        start = datetime.fromisoformat('2026-10-10T21:00:00-05:00')
        end = datetime.fromisoformat('2026-10-10T21:15:00-05:00')
        with tempfile.TemporaryDirectory() as folder, patch.object(journal, 'ROOT', Path(folder)):
            journal.upsert('BTC', start.isoformat(), end.isoformat(), purchase={
                'status': 'filled', 'direction': 'UP / ABOVE', 'target': 83000,
                'contracts': 1, 'limit_price_cents': 59})
            page = Mock()
            page.locator.return_value.inner_text.return_value = (
                'Your positions\n$83,000 or above\n1 Yes BTC 15 min 10:00–10:15 PM EDT @ 56¢')
            with patch.object(rh, 'read_event_target', return_value=83000):
                self.assertTrue(rh.observe_positions_section(
                    page, 'BTC', start, end, app.record_robinhood_purchase))
            purchase = journal.load('BTC')[0]['purchase']
            self.assertEqual(purchase['limit_price_cents'], 59)
            self.assertEqual(purchase['execution_price_cents'], 56)
            self.assertEqual(purchase['execution_price_source'], 'your_positions_display')

    def test_no_fills_has_no_accuracy_even_with_legacy_hits(self):
        self.assertIsNone(journal.purchase_accuracy([{'actual':'UP / ABOVE','hit':True}])['accuracy'])

    def test_fill_confirmation_persists_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(journal,'ROOT',Path(folder)),patch.object(app.ARGS,'asset','BTC'),patch.dict(app.STATS,{'rounds':[]}):
            start='2026-10-10T21:00:00-05:00';end='2026-10-10T21:15:00-05:00'
            journal.upsert('BTC',start,end,actual='UP / ABOVE',prediction='UP / ABOVE',status='settled',
                           purchase={'status':'submitted','direction':'UP / ABOVE','target':83000})
            client=app.app.test_client()
            for _ in range(2):
                r=client.post('/api/purchase-filled',json={'asset':'BTC','round_start':start})
                self.assertEqual(r.status_code,200)
            self.assertEqual(journal.load('BTC')[0]['purchase']['status'],'filled')
            self.assertEqual(client.get('/api/state').get_json()['stats']['total'],1)
            self.assertEqual(client.get('/api/state').get_json()['stats']['accuracy'],100)

    def test_purchase_live_snapshot_is_preserved_when_fill_is_confirmed(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as folder,patch.object(journal,'ROOT',Path(folder)):
            start=datetime.fromisoformat('2026-10-10T21:00:00-05:00')
            end=datetime.fromisoformat('2026-10-10T21:15:00-05:00')
            journal.upsert('BTC',start.isoformat(),end.isoformat())
            live={'direction':'DOWN / BELOW','p_up':.3,'confidence':.7,'minute':8,'updated_at':'2026-10-10T21:07:00-05:00'}
            self.assertTrue(app.record_robinhood_purchase('BTC',start,end,
                            {'status':'submitted','direction':'DOWN / BELOW','target':83000,'live_recommendation':live}))
            with patch.dict(app.STATE,{'live_recommendation':{'recommendation':'UP / ABOVE'}}):
                self.assertTrue(app.record_robinhood_purchase('BTC',start,end,{'status':'filled','target':83000}))
            saved=journal.load('BTC')[0]['purchase']
            self.assertEqual(saved['live_recommendation'],live)
            self.assertEqual(saved['status'],'filled')

    def test_order_waits_for_live_snapshot_before_touching_form(self):
        prediction={'direction':'UP / ABOVE','model_directions':dict.fromkeys(
            ('Final prediction','Pattern','Recent','Bootstrap','Trend'),'UP / ABOVE'),
            'live_snapshot':None}
        page,record=Mock(),Mock()
        self.assertFalse(rh.prepare_contract_form(page,'BTC',None,None,
                         lambda *args:prediction,Mock(),record))
        page.get_by_role.assert_not_called()
        record.assert_not_called()

    def test_failed_submission_keeps_pre_submission_live_snapshot(self):
        prediction={'direction':'UP / ABOVE','target':83000,
                    'model_directions':dict.fromkeys(
                        ('Final prediction','Pattern','Recent','Bootstrap','Trend'),'UP / ABOVE'),
                    'live_snapshot':{'direction':'UP / ABOVE','p_up':.7}}
        page,qty,limit,buyin,buy=Mock(),Mock(),Mock(),Mock(),Mock()
        buyin.inner_text.return_value='Contracts'
        qty.input_value.return_value='1'
        limit.input_value.return_value='40'
        buy.click.side_effect=RuntimeError('submission timeout')
        qty.or_.return_value=qty
        qty.count.return_value=1
        page.get_by_role.side_effect=lambda role,**args: (buyin if role=='combobox' else
            qty if 'Contracts' in str(args.get('name')) else limit if role=='textbox' else
            buy if args.get('name')=='Buy' else Mock())
        record=Mock(return_value=True)
        with patch.object(rh,'read_event_target',return_value=83000), patch.object(rh,'review_buy_details',return_value=True), patch.object(rh,'select_time_in_force'), patch.object(rh,'in_purchase_window',return_value=True), patch.object(rh,'contract_price_rising',return_value=True), patch.object(rh,'verify_contract_price_trend',return_value=True):
            with self.assertRaisesRegex(RuntimeError,'submission timeout'):
                rh.prepare_contract_form(page,'BTC',None,None,lambda *args:prediction,Mock(),record)
        saved=record.call_args.args[3]
        self.assertEqual(saved['status'],'attempted')
        self.assertEqual(saved['live_recommendation'],prediction['live_snapshot'])

    def test_submitted_receipt_cannot_mark_filled(self):
        page,record=Mock(),Mock()
        page.get_by_role.return_value.all.return_value=[]
        self.assertFalse(rh.observe_filled_order(page,'BTC',None,None,record))
        record.assert_not_called()

    def test_explicit_fill_receipt_records_after_event_validation(self):
        page,record,marker=Mock(),Mock(),Mock()
        marker.is_visible.return_value=True
        page.get_by_role.return_value.all.return_value=[marker]
        page.locator.return_value.inner_text.return_value='Filled quantity\n1 of 1'
        record.return_value=True
        with patch.object(rh,'read_event_target',return_value=83000):
            self.assertTrue(rh.observe_filled_order(page,'BTC',None,None,record))
        self.assertEqual(record.call_args.args[3]['status'],'filled')

    def test_done_without_complete_quantity_cannot_mark_filled(self):
        for text in ('Order filled', 'Filled quantity 0 of 1',
                     'Filled quantity 1 of 2', 'Filled quantity 0 of 0'):
            page, record, marker = Mock(), Mock(), Mock()
            marker.is_visible.return_value = True
            page.get_by_role.return_value.all.return_value = [marker]
            page.locator.return_value.inner_text.return_value = text
            self.assertFalse(rh.observe_filled_order(page, 'BTC', None, None, record))
            record.assert_not_called()

    def test_receipt_quantity_must_match_saved_order(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as folder, patch.object(journal, 'ROOT', Path(folder)):
            start = datetime.fromisoformat('2026-10-10T21:00:00-05:00')
            end = datetime.fromisoformat('2026-10-10T21:15:00-05:00')
            journal.upsert('BTC', start.isoformat(), end.isoformat(),
                           purchase={'status': 'submitted', 'target': 83000, 'contracts': 5})
            receipt = {'status': 'filled', 'target': 83000,
                       'confirmed_by': 'visible_order_receipt',
                       'filled_quantity': 1, 'receipt_contracts': 1}
            self.assertFalse(app.record_robinhood_purchase('BTC', start, end, receipt))
            self.assertEqual(journal.load('BTC')[0]['purchase']['status'], 'submitted')
            receipt.update(filled_quantity=5, receipt_contracts=5)
            self.assertTrue(app.record_robinhood_purchase('BTC', start, end, receipt))
