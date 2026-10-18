import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import round_history as journal
import app


class JournalTests(unittest.TestCase):
    def test_date_filter_is_inclusive_and_applied_before_pagination(self):
        with patch.object(app.ARGS,'asset','BTC'),patch.dict(app.STATS,rounds=[]):
            for day in (10,11,12):
                journal.upsert('BTC',f'2026-10-{day}T21:00:00-05:00',f'2026-10-{day}T21:15:00-05:00')
            client=app.app.test_client()
            result=client.get('/api/round-history?date_from=2026-10-11&date_to=2026-10-11').get_json()
            self.assertEqual(result['total'],1)
            self.assertTrue(result['rounds'][0]['round_start'].startswith('2026-10-11'))
            self.assertEqual(client.get('/api/round-history?date_from=2026-10-11').get_json()['total'],2)
            self.assertEqual(client.get('/api/round-history?date_from=2026-10-12&date_to=2026-10-10').status_code,400)
            self.assertEqual(client.get('/api/round-history?date_from=invalid').status_code,400)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch = patch.object(journal, 'ROOT', Path(self.tmp.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.start = '2026-10-10T21:00:00-05:00'
        self.end = '2026-10-10T21:15:00-05:00'

    def test_upsert_persists_prediction_and_settlement_without_duplicate(self):
        journal.upsert('BTC', self.start, self.end, status='predicted', target=83000,
                       component_results={'Similar-pattern': {'predicted':'UP','p_up':0.6,'hit':None}})
        journal.upsert('BTC', self.start, self.end, status='settled', settlement=83100, hit=True)
        rows = json.loads(journal.path_for('BTC').read_text())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['component_results']['Similar-pattern']['p_up'], .6)
        self.assertEqual(rows[0]['settlement'], 83100)
        self.assertEqual(journal.load('ETH'), [])

    def test_end_of_round_cancels_only_unconfirmed_local_records(self):
        from datetime import datetime, timedelta
        end = datetime.fromisoformat(self.end)
        for status in ('attempted', 'submitted', 'filled'):
            with self.subTest(status=status):
                purchase = {'status': status, 'contracts': 1, 'target': 83000,
                            'live_recommendation': {'direction': 'UP / ABOVE'}}
                journal.upsert('BTC', self.start, self.end, purchase=purchase)
                journal.cancel_unconfirmed_purchases('BTC', end - timedelta(seconds=1))
                self.assertEqual(journal.load('BTC')[0]['purchase']['status'], status)
                journal.cancel_unconfirmed_purchases('BTC', end)
                saved = journal.load('BTC')[0]['purchase']
                self.assertEqual(saved['status'], 'filled' if status == 'filled' else 'cancelled')
                self.assertEqual(saved['live_recommendation'], purchase['live_recommendation'])
                if status != 'filled':
                    self.assertEqual(saved['cancellation_scope'], 'app_record')
                    self.assertEqual(saved['previous_status'], status)
                with patch.object(journal, 'upsert') as save:
                    journal.cancel_unconfirmed_purchases('BTC', end + timedelta(seconds=1))
                    save.assert_not_called()

    def test_merges_old_results_and_missing_prediction_with_pagination(self):
        legacy=[{'round_start':self.start,'round_end':self.end,'target':83000,
                 'settlement':83100,'hit':True,'component_results':{'Trend/regime':{'p_up':.8,'predicted':'UP','hit':True}}}]
        journal.upsert('BTC', self.start, self.end, status='waiting_target')
        rows=journal.merged('BTC',legacy)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['status'],'settled')
        self.assertTrue(rows[0]['component_results']['Trend/regime']['hit'])
        with patch.dict(app.STATS, {'rounds':legacy}), patch.object(app.ARGS,'asset','BTC'):
            r=app.app.test_client().get('/api/round-history?page=999').get_json()
            self.assertEqual(r['page'],1)
            self.assertEqual(r['total'],1)
            self.assertEqual(r['rounds'][0]['target'],83000)

    def test_purchase_filter_applies_before_pagination(self):
        from datetime import datetime, timedelta
        first=datetime.fromisoformat(self.start)
        for i in range(30):
            start=first+timedelta(minutes=15*i)
            fields={'purchase':{'status':'filled'}} if i%2==0 else {}
            journal.upsert('BTC',start.isoformat(),(start+timedelta(minutes=15)).isoformat(),**fields)
        with patch.dict(app.STATS,{'rounds':[]}),patch.object(app.ARGS,'asset','BTC'):
            client=app.app.test_client()
            all_rows=client.get('/api/round-history').get_json()
            only=client.get('/api/round-history?purchases=only').get_json()
            hidden=client.get('/api/round-history?purchases=hide').get_json()
            self.assertEqual(all_rows['total'],30)
            self.assertEqual(only['total'],15)
            self.assertEqual(hidden['total'],15)
            self.assertEqual(only['pages'],1)
            self.assertTrue(all(r['purchase']['status']=='filled' for r in only['rounds']))
            self.assertTrue(all(not r.get('purchase') for r in hidden['rounds']))
            self.assertEqual(client.get('/api/round-history?purchases=invalid').status_code,400)

    def test_direction_and_result_filters_can_be_combined(self):
        journal.upsert('BTC',self.start,self.end,prediction='UP / ABOVE',actual='DOWN / BELOW',
                       settlement=82900,hit=False,
                       component_results={'Similar-pattern':{'predicted':'DOWN / BELOW TARGET','hit':True}},
                       purchase={'status':'filled','live_recommendation':{'direction':'UP / ABOVE'}})
        with patch.dict(app.STATS,{'rounds':[]}),patch.object(app.ARGS,'asset','BTC'):
            client=app.app.test_client()
            self.assertEqual(client.get('/api/round-history?final=miss&pattern=hit&live=miss&actual=down&purchases=only').get_json()['total'],1)
            self.assertEqual(client.get('/api/round-history?final=up&pattern=down').get_json()['total'],1)
            self.assertEqual(client.get('/api/round-history?final=hit').get_json()['total'],0)
            self.assertEqual(client.get('/api/round-history?actual=hit').status_code,400)

    def test_filtered_accuracy_excludes_current_round_and_counts_all_pages(self):
        from datetime import datetime, timedelta
        first=datetime.fromisoformat(self.start)
        for i in range(30):
            start=first+timedelta(minutes=15*i)
            journal.upsert('BTC',start.isoformat(),(start+timedelta(minutes=15)).isoformat(),
                           prediction='UP / ABOVE',actual='DOWN / BELOW',settlement=82900,hit=False,
                           purchase={'status':'filled','direction':'UP / ABOVE'})
        current=first+timedelta(minutes=15*30)
        journal.upsert('BTC',current.isoformat(),(current+timedelta(minutes=15)).isoformat(),
                       prediction='UP / ABOVE',actual='UP / ABOVE',settlement=83100,hit=True,
                       purchase={'status':'filled','direction':'UP / ABOVE'})
        with patch.dict(app.STATS,{'rounds':[]}),patch.object(app.ARGS,'asset','BTC'), \
             patch.object(app,'now_local',return_value=current+timedelta(minutes=1)):
            client=app.app.test_client()
            result=client.get('/api/round-history?final=up').get_json()
            self.assertEqual(len(result['rounds']),25)
            self.assertEqual(result['stats']['total'],30)
            self.assertEqual(result['stats']['accuracy'],0)
            self.assertEqual(result['stats']['final_prediction']['total'],30)
            self.assertIsNone(client.get('/api/round-history?final=hit').get_json()['stats']['accuracy'])
            self.assertEqual(client.get('/api/round-history?purchases=hide').get_json()['stats']['total'],0)

    def test_expired_round_without_prediction_has_no_false_miss(self):
        journal.upsert('BTC', self.start, self.end)
        row=journal.merged('BTC',[])[0]
        self.assertEqual(row['status'],'no_prediction')
        self.assertIsNone(row['hit'])

    def test_prediction_is_saved_before_settlement_with_all_models(self):
        from datetime import datetime
        start=datetime.fromisoformat(self.start)
        end=datetime.fromisoformat(self.end)
        prediction={'start':start,'end':end,'target':83000,'prediction':'UP / ABOVE',
                    'p_up':.6,'p_down':.4,'p_pattern':.7,'p_regime':.3,
                    'p_bootstrap':.8,'p_trend':.4,
                    'adaptive_weights':{'pattern':{'weight':.4},'regime':{'weight':.3},
                                        'bootstrap':{'weight':.2},'trend':{'weight':.1}}}
        with patch.dict(app.STATE,{'round_start':start,'round_end':end}), \
             patch.dict(app.HISTORY,{'error':None,'index':object(),'records':[]}), \
             patch.object(app,'ANALYSIS_TOKEN',42), \
             patch.object(app.core,'current_round',return_value=(start,end)), \
             patch.object(app.core,'build_round_prediction',return_value=prediction), \
             patch.object(app.HISTORY_READY,'wait',return_value=True):
            app.analyze_target(83000,start,end,42)
        rows=journal.load('BTC')
        self.assertEqual(rows[0]['status'],'predicted')
        self.assertEqual(len(rows[0]['component_results']),4)
        self.assertEqual(rows[0]['component_results']['Recent 3h regime']['predicted'],'DOWN / BELOW TARGET')
        self.assertIsNone(rows[0]['component_results']['Recent 3h regime']['hit'])
        self.assertEqual(rows[0]['adaptive_weights'],
                         {'pattern':.4,'regime':.3,'bootstrap':.2,'trend':.1})

    def test_next_target_recovers_missing_settlement_and_persists_scores(self):
        journal.upsert('BTC', self.start, self.end, target=83000, prediction='UP / ABOVE',
                       component_results={'Trend/regime':{'p_up':.3,'hit':None}},
                       purchase={'status':'filled','direction':'DOWN / BELOW'})
        journal.upsert('BTC', self.end, '2026-10-10T21:30:00-05:00', target=82900)
        rows=journal.merged('BTC',[])
        row=next(r for r in rows if r['round_start']==self.start)
        self.assertEqual(row['settlement'],82900)
        self.assertEqual(row['actual'],'DOWN / BELOW')
        self.assertFalse(row['hit'])
        self.assertTrue(row['component_results']['Trend/regime']['hit'])
        self.assertEqual(row['settlement_source'],'next_round_target')
        self.assertEqual(journal.purchase_accuracy(rows)['accuracy'],100)
        self.assertEqual(journal.final_prediction_accuracy(rows)['accuracy'],0)
        self.assertEqual(journal.load('BTC')[0]['settlement'],82900)

    def test_next_target_replaces_existing_settlement_but_does_not_bridge_gap(self):
        journal.upsert('BTC',self.start,self.end,target=83000,settlement=83100)
        journal.upsert('BTC',self.end,'2026-10-10T21:30:00-05:00',target=82900)
        journal.merged('BTC',[])
        self.assertEqual(journal.load('BTC')[0]['settlement'],82900)
        journal.upsert('BTC',self.start,self.end,settlement=None)
        path=journal.path_for('BTC')
        rows=journal.load('BTC');rows[1]['round_start']='2026-10-10T21:16:00-05:00'
        path.write_text(json.dumps(rows))
        self.assertIsNone(next(r for r in journal.merged('BTC',[]) if r['round_start']==self.start)['settlement'])

    def test_boundary_settlement_is_not_rewritten_by_repeated_legacy_reads(self):
        legacy = [{'round_start': self.start, 'round_end': self.end,
                   'target': 83000, 'settlement': 83100, 'actual': 'UP / ABOVE',
                   'prediction': 'UP / ABOVE', 'hit': True}]
        journal.upsert('BTC', self.start, self.end, **{
            k: v for k, v in legacy[0].items() if k not in ('round_start', 'round_end')})
        journal.upsert('BTC', self.end, '2026-10-10T21:30:00-05:00', target=82900)
        journal.merged('BTC', legacy)
        with patch.object(journal, 'upsert', wraps=journal.upsert) as save:
            for _ in range(3):
                rows = journal.merged('BTC', legacy)
                row = next(r for r in rows if r['round_start'] == self.start)
                self.assertEqual(row['settlement'], 82900)
                self.assertFalse(row['hit'])
            save.assert_not_called()

    def test_final_accuracy_includes_unbought_rounds_and_uses_final_direction(self):
        rows = [
            {'prediction':'UP / ABOVE', 'actual':'UP / ABOVE', 'settlement':83001},
            {'prediction':'DOWN / BELOW', 'actual':'UP / ABOVE', 'settlement':83002,
             'purchase':{'status':'filled', 'direction':'UP / ABOVE'}},
            {'prediction':'UP / ABOVE', 'actual':None, 'settlement':None},
            {'prediction':None, 'actual':'UP / ABOVE', 'settlement':83003},
        ]
        self.assertEqual(journal.final_prediction_accuracy(rows),
                         {'total':2, 'hits':1, 'misses':1, 'accuracy':50.0})
        self.assertEqual(journal.purchase_accuracy(rows)['accuracy'], 100.0)
        self.assertIsNone(journal.final_prediction_accuracy([])['accuracy'])

    def test_stats_archive_keeps_more_than_1000_rounds(self):
        import brti_engine as core
        rows=[{'round_start':str(i)} for i in range(1001)]
        with patch.object(core,'stats_file_for_asset',return_value=Path(self.tmp.name)/'stats.json'):
            core.save_stats({'_asset':'BTC','rounds':rows})
        self.assertEqual(len(json.loads((Path(self.tmp.name)/'stats.json').read_text())['rounds']),1001)

    def test_dashboard_renders_all_four_model_predictions(self):
        from werkzeug.serving import make_server
        from playwright.sync_api import sync_playwright
        models={name:{'predicted':'UP / ABOVE TARGET','p_up':.65,'hit':True}
                for name in ('Similar-pattern','Recent 3h regime','Historical bootstrap','Trend/regime')}
        journal.upsert('BTC',self.start,self.end,status='settled',target=83000,
                       prediction='UP / ABOVE',p_up=.7,settlement=83100,actual='UP / ABOVE',hit=True,
                       component_results=models,
                       adaptive_weights={"pattern":.4,"regime":.3,"bootstrap":.2,"trend":.1},
                       purchase={'status':'filled','direction':'UP / ABOVE',
                                 'contracts':2,'limit_price_cents':38,'confirmed_by':'buy_button_click',
                                 'live_recommendation':{'direction':'UP / ABOVE','p_up':.612,
                                                        'updated_at':'2026-10-10T21:08:00-05:00'}})
        with patch.dict(app.STATS, {'rounds':[]}), patch.object(app.ARGS,'asset','BTC'):
            server=make_server('127.0.0.1',0,app.app)
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(channel='chrome',headless=True)
                    page=browser.new_page(viewport={'width':1400,'height':1000})
                    errors=[]
                    page.on('pageerror',lambda exc:errors.append(str(exc)))
                    page.goto(f'http://127.0.0.1:{server.server_port}/')
                    page.get_by_role('button',name='Clear dates',exact=True).click()
                    page.locator('#roundHistoryBody [title="Settled"]').wait_for()
                    self.assertEqual(page.locator('#roundHistoryBody').inner_text().count('65.0%'),4)
                    self.assertIn('$83,000.00',page.locator('#roundHistoryBody').inner_text())
                    self.assertIn('61.2%',page.locator('#roundHistoryBody').inner_text())
                    for weight in ('40.00%', '30.00%', '20.00%', '10.00%'):
                        self.assertIn(weight,page.locator('#roundHistoryBody').inner_text())
                    self.assertIn('CTs: 2',page.locator('#roundHistoryBody').inner_text())
                    self.assertIn('P/L ≈ $+1.2012',page.locator('#roundHistoryBody tr').first.locator('td').nth(0).inner_text())
                    self.assertNotIn('P/L',page.locator('#roundHistoryBody tr').first.locator('td').nth(9).inner_text())
                    self.assertIn('price: 38¢',page.locator('#roundHistoryBody').inner_text())
                    self.assertEqual(page.locator('.round-log th').count(),10)
                    self.assertEqual(errors,[])
                    page.locator('#roundHistoryBody').scroll_into_view_if_needed()
                    page.screenshot(path='/tmp/cpmc-round-history.png')
                    browser.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)
