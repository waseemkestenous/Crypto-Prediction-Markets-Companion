from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
import json
import re

import app
import choppiness
import order_settings
import price_history
import robinhood_target as rh
import round_history as journal


START = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
END = START + timedelta(minutes=15)


def quotes(prices, start=START, target=80000):
    return [{'observed_at': (start+timedelta(seconds=15*i+14)).isoformat(),
             'target': target, 'yes_cents': price, 'no_cents': 100-price}
            for i, price in enumerate(prices)]


def ready_score(value=50, complete=True):
    return {'status': 'ready', 'score': value, 'range_cents': 20, 'complete': complete,
            'metric': 'midpoint_crossings',
            'round_start': START.isoformat(), 'round_end': END.isoformat()}


class ChoppinessScoreTests(TestCase):
    def test_current_threshold_and_exactly_one_following_round(self):
        config={'enabled':False,'source':'contract_yes','current_enabled':True,
                'current_limit':7,'block_next_round':True,'rounds':3}
        low=[ready_score(0)]*3
        self.assertTrue(choppiness.buy_guard(low,config,ready_score(6,False))['allowed'])
        hit=choppiness.buy_guard(low,config,ready_score(7,False))
        self.assertFalse(hit['allowed'])
        self.assertTrue(hit['current_blocked'])
        next_round=choppiness.buy_guard([ready_score(7)],config,ready_score(0,False))
        self.assertTrue(next_round['previous_blocked'])
        self.assertFalse(next_round['allowed'])
        # Missing older average data is irrelevant when the average filter is off.
        reset=choppiness.buy_guard([ready_score(0)],config,ready_score(0,False))
        self.assertTrue(reset['allowed'])
        self.assertFalse(reset['previous_blocked'])
        self.assertTrue(choppiness.buy_guard([ready_score(0),ready_score(7),ready_score(9)],config,ready_score(0,False))['allowed'])
        disabled_next=dict(config,block_next_round=False)
        self.assertTrue(choppiness.buy_guard([ready_score(7)],disabled_next,ready_score(0,False))['allowed'])

    def test_early_current_gate_uses_observed_crossings_before_score_warmup(self):
        current=choppiness.score(quotes([40,60]),START,END,80000,
                                START+timedelta(seconds=30))
        self.assertIsNone(current['score'])
        self.assertEqual(current['observed_crossings'],1)
        config={'enabled':False,'current_enabled':True,'current_limit':1}
        self.assertFalse(choppiness.buy_guard([],config,current)['allowed'])

    def test_crossings_ignore_initial_side_and_exact_touches(self):
        for values, expected in [([49,50,49,49],0), ([49,50,51,51],1),
                                 ([51,50,49,51],2), ([50,50,50,50],0),
                                 ([10,30,20,40],0), ([90,70,80,60],0)]:
            for source in ('contract_yes','bitcoin'):
                with self.subTest(values=values,source=source):
                    samples=quotes(values+[values[-1]]*56)
                    if source=='bitcoin':
                        samples=[{'observed_at':s['observed_at'],'price':80000+s['yes_cents']-50} for s in samples]
                    result=choppiness.score(samples,START,END,80000,END,source=source)
                    self.assertEqual(result['score'],expected)
                    self.assertEqual(result['crossings_up']+result['crossings_down'],expected)

    def test_fixed_grid_scores_trend_flat_and_choppy_paths(self):
        for prices, expected in [([30+i for i in range(60)], 1), ([50]*60, 0),
                                 ([50, 70, 40, 60]+[60]*56, 2)]:
            with self.subTest(expected=expected):
                result = choppiness.score(quotes(prices), START, END, 80000, END)
                self.assertAlmostEqual(result['score'], expected)
                self.assertEqual(result['sample_count'], 60)
                self.assertEqual(result['range_cents'], max(prices)-min(prices))

    def test_causal_sampling_and_current_score_is_provisional(self):
        samples = quotes([50, 70, 40, 60])
        stamp = START+timedelta(seconds=60)
        before = choppiness.score(samples, START, END, 80000, stamp)
        # Neither a future valid quote nor a quote just AFTER an earlier tick
        # may be used to change that tick's value.
        later = samples+[{'observed_at': (stamp+timedelta(seconds=1)).isoformat(),
                          'target': 80000, 'yes_cents': 0}]
        self.assertEqual(choppiness.score(later, START, END, 80000, stamp), before)
        after_first_tick = dict(samples[0], observed_at=(START+timedelta(seconds=16)).isoformat(), yes_cents=99)
        # The second tick has its own newer observation, so the extra quote
        # must not affect any sampled value.
        self.assertEqual(choppiness.score(samples+[after_first_tick], START, END, 80000, stamp), before)
        self.assertEqual(before['status'], 'ready')
        self.assertFalse(before['complete'])
        self.assertIsNone(choppiness.score(samples, START, END, 80000, stamp-timedelta(seconds=1))['score'])

    def test_quote_cadence_invalid_data_and_next_round_do_not_change_score(self):
        samples = quotes([50, 70, 40, 60]+[60]*56)
        expected = choppiness.score(samples, START, END, 80000, END)
        extra = [dict(s, observed_at=(datetime.fromisoformat(s['observed_at'])-timedelta(seconds=1)).isoformat()) for s in samples]
        extra += [dict(samples[0], target=90000, yes_cents=1),
                  dict(samples[0], observed_at=END.isoformat(), yes_cents=100)]
        for value in (True, None, '50', float('nan'), -1, 101):
            extra.append(dict(samples[0], yes_cents=value))
        extra.append(dict(samples[0], observed_at='invalid'))
        self.assertEqual(choppiness.score(list(reversed(samples+extra)), START, END, 80000, END), expected)

    def test_gaps_are_skipped_but_empty_or_single_quote_stays_unavailable(self):
        samples = quotes([50]*60)
        for data in ([], samples[:1]):
            with self.subTest(size=len(data)):
                self.assertIsNone(choppiness.score(data, START, END, 80000, END)['score'])
        for data in (samples[:40], samples[12:], samples[:20]+samples[30:]):
            with self.subTest(size=len(data)):
                result = choppiness.score(data, START, END, 80000, END)
                self.assertEqual(result['score'], 0)
                self.assertGreater(result['missing_samples'], 0)
        self.assertEqual(choppiness.score(samples, START, END, 80000, END)['score'], 0)

    def test_internal_gaps_do_not_fill_stale_prices_or_reject_available_path(self):
        samples = quotes([50,70])+quotes([40,60], START+timedelta(minutes=10))
        for source in ('contract_yes','bitcoin'):
            data = samples if source=='contract_yes' else [
                {'observed_at':s['observed_at'],'price':80000+s['yes_cents']} for s in samples]
            result = choppiness.score(data, START, END, 80050 if source=='bitcoin' else 80000, END, source=source)
            self.assertEqual(result['score'], 2)
            self.assertEqual(result['sample_count'], 6)
            self.assertEqual(result['missing_samples'], 54)

    def test_guard_threshold_is_unrounded_and_requires_every_completed_round(self):
        config = {'enabled': True, 'max_score': 2, 'rounds': 3}
        scores = [ready_score(1), ready_score(2), ready_score(3)]
        self.assertTrue(choppiness.buy_guard(scores, config)['allowed'])
        self.assertFalse(choppiness.buy_guard(scores, dict(config,max_score=1.999))['allowed'])
        for data in (scores[:2], scores+[ready_score()], [ready_score(), ready_score(), ready_score(0, False)]):
            self.assertIsNone(choppiness.buy_guard(data, config)['average_score'])
            self.assertFalse(choppiness.buy_guard(data, config)['allowed'])
            self.assertTrue(choppiness.buy_guard(data, dict(config, enabled=False))['allowed'])

    def test_bitcoin_crossings_use_the_round_target_and_dollar_units(self):
        values = [50,70,40,60]+[60]*56
        samples = [{'observed_at': s['observed_at'], 'price': 80000+s['yes_cents']} for s in quotes(values)]
        result = choppiness.score(samples, START, END, 80050, END, source='bitcoin')
        self.assertEqual(result['score'], 2)
        self.assertEqual(result['source'], 'bitcoin')
        self.assertEqual(result['range_dollars'], 30)
        self.assertIsNone(result['range_cents'])
        self.assertEqual(choppiness.score(samples, START, END, 90000, END, source='bitcoin')['score'],0)
        invalid = [dict(samples[0], price=p) for p in (0,-1,True,None,'80000',float('nan'),float('inf'))]
        invalid += [dict(samples[0], observed_at=END.isoformat(), price=1)]
        self.assertEqual(result, choppiness.score(samples+invalid, START, END, 80050, END, source='bitcoin'))
        self.assertIsNone(choppiness.score([], START, END, None, END, source='bitcoin')['score'])

    def test_average_never_combines_different_sources(self):
        contract = dict(ready_score(), source='contract_yes')
        bitcoin = dict(ready_score(80), source='bitcoin', range_cents=None, range_dollars=30)
        config = {'enabled': True, 'source': 'bitcoin', 'rounds': 2}
        self.assertFalse(choppiness.buy_guard([contract,bitcoin], config)['allowed'])
        guard = choppiness.buy_guard([bitcoin,bitcoin], dict(config,max_score=80))
        self.assertTrue(guard['allowed'])
        self.assertEqual(guard['average_range_dollars'], 30)
        self.assertIsNone(guard['average_range_cents'])


class ChoppinessIntegrationTests(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        contexts = [patch.object(journal, 'ROOT', self.root), patch.dict(app.ORDER_SETTINGS),
                    patch.object(price_history, 'ROOT', self.root/'prices'),
                    patch.object(order_settings, 'PATH', self.root/'settings.json'),
                    patch.object(app.ARGS, 'asset', 'BTC')]
        for context in contexts:
            context.start()
            self.addCleanup(context.stop)

    def config(self, **fields):
        config = dict(order_settings.CHOPPINESS_BUY_GUARD_DEFAULTS, enabled=True, source='contract_yes')
        config.update(fields)
        return dict(order_settings.DEFAULTS, choppiness_buy_guard=config)

    def test_validation_defaults_and_api_persistence(self):
        self.assertFalse(order_settings.validate({'max_price_cents': 60, 'contracts': 1})['choppiness_buy_guard']['enabled'])
        self.assertEqual(order_settings.validate(order_settings.DEFAULTS)['choppiness_buy_guard']['source'], 'bitcoin')
        for key, values in [('max_score', [-1, True, None, float('nan'), float('inf')]),
                            ('rounds', [0, 97, 1.5, True, None, float('nan'), 'bad'])]:
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    order_settings.validate(self.config(**{key: value}))
        for value in (None, [], {'enabled': 'yes'}):
            with self.assertRaises(ValueError):
                order_settings.validate(dict(order_settings.DEFAULTS, choppiness_buy_guard=value))
        for source in ('invalid',None,[],True):
            with self.assertRaises(ValueError):
                order_settings.validate(self.config(source=source))
        response = app.app.test_client().post('/api/order-settings', json=self.config(max_score=65, rounds=4))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(order_settings.load()['choppiness_buy_guard'], {'enabled': True, 'max_score': 65, 'rounds': 4, 'source': 'contract_yes', 'metric':'midpoint_crossings','current_enabled':False,'current_limit':7,'block_next_round':False})
        self.assertEqual(app.ORDER_SETTINGS['choppiness_buy_guard']['rounds'], 4)

    def test_old_path_score_settings_migrate_without_reusing_old_units(self):
        legacy=dict(order_settings.DEFAULTS,choppiness_buy_guard={
            'enabled':True,'max_score':70,'rounds':5,'source':'contract_yes'})
        order_settings.PATH.write_text(json.dumps(legacy))
        loaded=order_settings.load()['choppiness_buy_guard']
        self.assertEqual(loaded,{'enabled':False,'max_score':3,'rounds':5,
                                 'source':'contract_yes','metric':'midpoint_crossings','current_enabled':False,'current_limit':7,'block_next_round':False})
        response=app.app.test_client().post('/api/order-settings',json=legacy)
        self.assertEqual(response.status_code,400)
        response=app.app.test_client().post('/api/order-settings',json=self.config(max_score=2.5))
        self.assertEqual(response.status_code,200)
        self.assertEqual(order_settings.load()['choppiness_buy_guard']['max_score'],2.5)
        self.assertTrue(order_settings.load()['choppiness_buy_guard']['enabled'])

    def test_new_gates_validate_persist_and_survive_an_older_open_settings_page(self):
        for value in (0,60,1.5,True,None,float('nan')):
            with self.assertRaises(ValueError):
                order_settings.validate(self.config(current_limit=value))
        for key in ('current_enabled','block_next_round'):
            with self.assertRaises(ValueError):
                order_settings.validate(self.config(**{key:'yes'}))
        config=self.config(enabled=False,current_enabled=True,current_limit=7,block_next_round=True)
        response=app.app.test_client().post('/api/order-settings',json=config)
        self.assertEqual(response.status_code,200)
        saved=order_settings.load()['choppiness_buy_guard']
        self.assertTrue(saved['current_enabled'])
        self.assertTrue(saved['block_next_round'])
        legacy=dict(config,choppiness_buy_guard={k:v for k,v in config['choppiness_buy_guard'].items()
                    if k not in ('current_enabled','current_limit','block_next_round')})
        response=app.app.test_client().post('/api/order-settings',json=legacy)
        self.assertEqual(response.status_code,200)
        self.assertEqual(order_settings.load()['choppiness_buy_guard'],saved)

    def test_current_and_next_gates_reset_by_round_and_survive_restart(self):
        app.ORDER_SETTINGS.update(self.config(enabled=False,source='bitcoin',current_enabled=True,
                                             block_next_round=True,current_limit=7))
        for offset in (0,1,2):
            start=START+timedelta(minutes=15*offset)
            end=start+timedelta(minutes=15)
            journal.upsert('BTC',start.isoformat(),end.isoformat(),target=80000)
            prices=[40,60]*4+[60]*52 if offset==0 else [40]*60
            for sample in quotes(prices,start):
                price_history.append('BTC',start,{'observed_at':sample['observed_at'],
                                                 'price':80000+sample['yes_cents']-50})
        for stamp,allowed,current_blocked,previous_blocked in [
                (START+timedelta(seconds=105),True,False,False),
                (START+timedelta(seconds=120),False,True,False),
                (END,False,False,True),
                (END+timedelta(minutes=5),False,False,True),
                (END+timedelta(minutes=15),True,False,False)]:
            price_history._CHOPPINESS_CACHE.clear()
            journal._CHOPPINESS_CACHE.clear()
            with patch.object(app,'now_local',return_value=stamp):
                result=app.current_buy_choppiness_guard()
            self.assertEqual((result['allowed'],result['current_blocked'],result['previous_blocked']),
                             (allowed,current_blocked,previous_blocked),stamp)
            if not allowed:self.assertIsNotNone(result['blocked_until'])

    def test_current_only_guard_blocks_every_buy_route_when_average_is_off(self):
        prediction={'order_settings':self.config(enabled=False,current_enabled=True),
                    'buy_choppiness_guard':{'allowed':False,'current_blocked':True,'current_crossings':7}}
        for options in ({},{'fallback':True},{'force_buy':True}):
            page,report=Mock(),Mock()
            self.assertFalse(rh.prepare_contract_form(page,'BTC',START,END,lambda *args:prediction,report,**options))
            self.assertEqual(page.mock_calls,[])
            self.assertIn('current',report.call_args.args[0])
        self.assertFalse(rh.buy_prediction_matches(prediction,prediction))

    def test_exact_previous_rounds_include_unbought_and_exclude_current(self):
        app.ORDER_SETTINGS.update(self.config())
        now = START+timedelta(minutes=5)
        for lag, score in [(1, 80), (2, 80), (3, 80)]:
            previous = START-timedelta(minutes=15*lag)
            journal.upsert('BTC', previous.isoformat(), (previous+timedelta(minutes=15)).isoformat(),
                           target=80000, contract_price_history=quotes([50]*60, previous))
        # Current round has a high score but never participates in the average.
        journal.upsert('BTC', START.isoformat(), END.isoformat(), target=80000,
                       contract_price_history=quotes([40,60]*30))
        with patch.object(app, 'now_local', return_value=now):
            guard = app.current_buy_choppiness_guard(include_current=True)
        self.assertEqual(guard['average_score'], 0)
        self.assertTrue(guard['allowed'])
        self.assertEqual(guard['ready_rounds'], 3)
        self.assertEqual(guard['current_round']['score'], 19)
        self.assertEqual([r['round_start'] for r in guard['round_scores']],
                         [(START-timedelta(minutes=15*i)).isoformat() for i in (1,2,3)])
        # A missing immediate predecessor cannot be replaced by an older one.
        app.ORDER_SETTINGS.update(self.config(rounds=4))
        with patch.object(app, 'now_local', return_value=now):
            self.assertFalse(app.current_buy_choppiness_guard()['allowed'])

    def test_cache_reuses_unchanged_quotes_but_handles_restart_and_external_changes(self):
        journal.upsert('BTC', START.isoformat(), END.isoformat(), target=80000,
                       contract_price_history=quotes([50]*60))
        with patch.object(choppiness, 'score', wraps=choppiness.score) as compute:
            result = journal.contract_round_choppiness('BTC', START, END, END)
            result['score'] = 99
            journal.upsert('BTC', START.isoformat(), END.isoformat(), actual='UP / ABOVE')
            self.assertEqual(journal.contract_round_choppiness('BTC', START, END, END)['score'], 0)
            self.assertEqual(compute.call_count, 1)
            row = journal.get('BTC', START.isoformat())
            row['contract_price_history'] = quotes([40,60]*30)
            replacement = self.root/'replacement.json'
            replacement.write_text(json.dumps([row]))
            replacement.replace(journal.path_for('BTC'))
            self.assertEqual(journal.contract_round_choppiness('BTC', START, END, END)['score'], 59)
            self.assertEqual(compute.call_count, 2)
            journal._CHOPPINESS_CACHE.clear()
            self.assertEqual(journal.contract_round_choppiness('BTC', START, END, END)['score'], 59)
            self.assertEqual(compute.call_count, 3)

    def test_flat_quotes_get_heartbeats_without_recording_every_poll(self):
        with patch.dict(app.STATE, target=80000), patch.object(app, 'current_round_key', return_value=(START, END)):
            for seconds in (0, 10, 15, 20, 30):
                with patch.object(app, 'now_local', return_value=START+timedelta(seconds=seconds)):
                    app.record_contract_prices('BTC', START, END, 80000, {'yes_cents': 50, 'no_cents': 50})
        saved = journal.get('BTC', START.isoformat())['contract_price_history']
        self.assertEqual(len(saved), 3)
        self.assertEqual([s['observed_at'] for s in saved], [(START+timedelta(seconds=i)).isoformat() for i in (0,15,30)])

    def test_bitcoin_cache_refreshes_for_appends_and_external_replacements(self):
        samples = [{'observed_at': s['observed_at'], 'price': 80000+s['yes_cents']}
                   for s in quotes([50,70,40,60]+[60]*56)]
        for sample in samples[:4]:
            price_history.append('BTC', START, sample)
        stamp = START+timedelta(seconds=60)
        with patch.object(choppiness, 'score', wraps=choppiness.score) as compute:
            result = price_history.round_choppiness(START, END, stamp, 80050)
            self.assertEqual(result['score'], 2)
            result['score'] = 0
            self.assertEqual(price_history.round_choppiness(START, END, stamp, 80050)['score'], 2)
            self.assertEqual(compute.call_count, 1)
            price_history.append('BTC', START, samples[4])
            # An appended future quote changes the file, not the historical score.
            self.assertEqual(price_history.round_choppiness(START, END, stamp, 80050)['score'], 2)
            self.assertEqual(compute.call_count, 2)
            path = price_history.path_for('BTC', START)
            replacement = path.with_suffix('.replacement')
            replacement.write_text(''.join(json.dumps(dict(s,price=80000))+'\n' for s in samples))
            replacement.replace(path)
            self.assertEqual(price_history.round_choppiness(START, END, END, 80050)['score'], 0)
            self.assertEqual(compute.call_count, 3)
            price_history._CHOPPINESS_CACHE.clear()
            self.assertEqual(price_history.round_choppiness(START, END, END, 80050)['score'], 0)

    def test_bitcoin_target_correction_invalidates_cached_score(self):
        for sample in quotes([40,60]*30):
            price_history.append('BTC',START,{'observed_at':sample['observed_at'],
                                             'price':80000+sample['yes_cents']-50})
        self.assertEqual(price_history.round_choppiness(START,END,END,80000)['score'],59)
        self.assertEqual(price_history.round_choppiness(START,END,END,90000)['score'],0)
        self.assertIsNone(price_history.round_choppiness(START,END,END,None)['score'])

    def test_same_saved_source_drives_history_and_buy_guard_without_fallback(self):
        for lag in (1,2,3):
            start = START-timedelta(minutes=15*lag)
            end = start+timedelta(minutes=15)
            journal.upsert('BTC',start.isoformat(),end.isoformat(),target=80000,
                           contract_price_history=quotes([50]*60,start))
            for sample in quotes([50,70,40,60]+[60]*56,start):
                price_history.append('BTC',start,{'observed_at':sample['observed_at'],'price':80000+sample['yes_cents']-50})
        app.ORDER_SETTINGS.update(self.config(source='bitcoin', max_score=1))
        with patch.object(app,'now_local',return_value=START+timedelta(minutes=5)), patch.dict(app.STATS,rounds=[]):
            bitcoin = app.current_buy_choppiness_guard()
            history = app.app.test_client().get('/api/round-history').get_json()['rounds']
            self.assertFalse(bitcoin['allowed'])
            self.assertEqual(bitcoin['average_score'],2)
            self.assertTrue(all(row['choppiness']['source']=='bitcoin' for row in history))
            self.assertTrue(all(row['choppiness']['score']==2 for row in history))
            app.ORDER_SETTINGS.update(self.config(source='contract_yes'))
            self.assertTrue(app.current_buy_choppiness_guard()['allowed'])
            history = app.app.test_client().get('/api/round-history').get_json()['rounds']
            self.assertTrue(all(row['choppiness']['source']=='contract_yes' for row in history))
            self.assertTrue(all(row['choppiness']['score']==0 for row in history))
            app.ORDER_SETTINGS.update(self.config(source='bitcoin',rounds=4))
            self.assertFalse(app.current_buy_choppiness_guard()['allowed'])

    def test_all_buy_routes_and_final_rechecks_obey_both_guards(self):
        prediction = {'order_settings': self.config(), 'buy_choppiness_guard': {'allowed': False, 'average_score': 80},
                      'live_snapshot': {'direction': 'UP / ABOVE', 'confidence': .57}}
        for options in ({}, {'fallback': True}, {'force_buy': True}):
            page, report = Mock(), Mock()
            self.assertFalse(rh.prepare_contract_form(page, 'BTC', START, END, lambda *args: prediction, report, **options))
            self.assertEqual(page.mock_calls, [])
            self.assertIn('Midpoint crossings', report.call_args.args[0])
        self.assertFalse(rh.buy_prediction_matches(prediction, prediction))
        self.assertIn('Midpoint crossings', rh.buy_prediction_change_reason(prediction, prediction))
        allowed = dict(prediction, buy_choppiness_guard={'allowed': True, 'average_score': 50})
        self.assertTrue(rh.buy_prediction_matches(allowed, prediction))
        self.assertFalse(rh.buy_choppiness_guard_allows({'order_settings': self.config()}))
        combined = dict(allowed, order_settings=dict(self.config(), accuracy_buy_guard={'enabled': True}),
                        buy_accuracy_guard={'allowed': False})
        self.assertFalse(rh.buy_prediction_matches(combined, combined))

    def test_final_record_blocks_new_buys_and_does_not_block_close_or_fill_records(self):
        with patch.object(app, 'current_buy_accuracy_guard', return_value={'allowed': True}), \
             patch.object(app, 'current_buy_choppiness_guard', return_value={'allowed': False}) as guard, \
             patch.object(journal, 'get', return_value=None) as get:
            self.assertFalse(app._record_robinhood_purchase('BTC', START, END, {'status': 'attempted'}))
            get.assert_not_called()
            guard.reset_mock()
            for update in ({'close_order': {'status': 'attempted'}}, {'status': 'filled'}, {'status': 'submitted'}):
                app._record_robinhood_purchase('BTC', START, END, update)
            guard.assert_not_called()


class ChoppinessBrowserTests(TestCase):
    def test_controls_save_values_and_preserve_unsaved_edits_and_show_status(self):
        from playwright.sync_api import sync_playwright
        html = app.app.test_client().get('/').get_data(as_text=True)
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
        source = next(s for s in scripts if 'function syncOrderSettings' in s)
        helpers = source[source.index('let orderSettingsDirty='):source.index('async function saveOrderSettings')]
        save = source[source.index('async function saveOrderSettings'):source.index('async function saveBalance')]
        sync = source[source.index('function syncOrderSettings'):source.index('function render(s)')]
        settings = order_settings.validate(order_settings.DEFAULTS)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width': 900, 'height': 1000})
            page.set_content(re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S))
            for script in scripts:
                page.evaluate('(s)=>new Function(s)', script)
            page.add_script_tag(content=helpers+save+sync)
            page.evaluate('(s)=>syncOrderSettings({order_settings:s})', settings)
            page.locator('#orderSettingsPanel').evaluate('(p)=>p.open=true')
            page.locator('#choppinessBuyGuardMaxInput').fill('65')
            page.locator('#choppinessBuyGuardRoundsInput').fill('4')
            self.assertEqual(page.locator('#choppinessBuyGuardSourceInput').input_value(),'bitcoin')
            page.locator('#choppinessBuyGuardSourceInput').select_option('contract_yes')
            page.locator('#choppinessBuyGuardEnabled').check()
            page.locator('#choppinessCurrentEnabled').check()
            page.locator('#choppinessCurrentLimitInput').fill('9')
            page.locator('#choppinessBlockNextInput').check()
            page.locator('#choppinessBuyGuardEnabled').blur()
            page.evaluate('(s)=>syncOrderSettings({order_settings:s})', settings)
            self.assertEqual(page.locator('#choppinessBuyGuardMaxInput').input_value(), '65')
            self.assertTrue(page.locator('#choppinessBuyGuardEnabled').is_checked())
            self.assertTrue(page.locator('#choppinessCurrentEnabled').is_checked())
            self.assertTrue(page.locator('#choppinessBlockNextInput').is_checked())
            self.assertEqual(page.locator('#choppinessCurrentLimitInput').input_value(),'9')
            self.assertEqual(page.locator('#choppinessBuyGuardSourceInput').input_value(),'contract_yes')
            page.evaluate("()=>{window.fetch=async(url,options)=>{window.savedSettings=JSON.parse(options.body);return {ok:true,json:async()=>({ok:true})};};}")
            page.evaluate('saveOrderSettings()')
            self.assertEqual(page.evaluate('savedSettings.choppiness_buy_guard'), {'enabled': True, 'max_score': 65, 'rounds': 4, 'source':'contract_yes','metric':'midpoint_crossings','current_enabled':True,'current_limit':9,'block_next_round':True})
            guard = choppiness.buy_guard([ready_score(8)]*3, {'enabled': True, 'rounds': 3, 'max_score': 3})
            guard['current_round'] = ready_score(2, False)
            page.evaluate('(g)=>renderChoppinessGuard(g)', guard)
            text = page.locator('#choppinessBuyGuardStatus').inner_text()
            self.assertIn('Current crossings (partial): 2', text)
            self.assertIn('Last 3 avg: 8.00', text)
            self.assertIn('Buying paused', text)
            btc = dict(ready_score(80),source='bitcoin',range_cents=None,range_dollars=30)
            btc_guard = choppiness.buy_guard([btc]*3,{'enabled':True,'rounds':3,'source':'bitcoin'})
            btc_guard['current_round'] = btc
            page.evaluate('(g)=>renderChoppinessGuard(g)',btc_guard)
            text = page.locator('#choppinessBuyGuardStatus').inner_text()
            self.assertIn('Bitcoin',text)
            self.assertIn('Range: $30.00',text)
            self.assertNotIn('¢',text)
            live=choppiness.buy_guard([ready_score(0)],{'enabled':False,'current_enabled':True,'current_limit':7},ready_score(7,False))
            live['current_round']=ready_score(7,False)
            page.evaluate('(g)=>renderChoppinessGuard(g)',live)
            text=page.locator('#choppinessBuyGuardStatus').inner_text()
            self.assertIn('Average filter: off',text)
            self.assertIn('current round ≥ 7',text)
            following=choppiness.buy_guard([ready_score(7)],{'enabled':False,'block_next_round':True,'current_limit':7},ready_score(0,False))
            following['current_round']=ready_score(0,False)
            page.evaluate('(g)=>renderChoppinessGuard(g)',following)
            self.assertIn('previous round reached 7',page.locator('#choppinessBuyGuardStatus').inner_text())
            page.evaluate('(g)=>renderChoppinessGuard(g)', choppiness.buy_guard([], {'enabled': True, 'rounds': 3}))
            self.assertIn('insufficient data', page.locator('#choppinessBuyGuardStatus').inner_text())
            browser.close()
