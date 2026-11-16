import unittest
from datetime import datetime,timedelta
from unittest.mock import Mock,patch
import order_settings as settings
import robinhood_target as rh

class FallbackTests(unittest.TestCase):
    def form(self,direction='UP / ABOVE',brti=83100,price=80,minute=14,purchased=False,min_distance=50, changes=10, strength="MODERATE", confidence=.7):
        now=datetime.now().astimezone();start=now-timedelta(minutes=minute);end=start+timedelta(minutes=15)
        p={'direction':'UP / ABOVE','target':83000,'live_direction':direction,'live_snapshot':{'direction':direction,'confidence':confidence},'brti_price':brti,'purchase_exists':purchased,'order_settings':dict(settings.DEFAULTS,fallback=dict(settings.FALLBACK_DEFAULTS,enabled=True,contracts=3,min_distance_dollars=min_distance,contract_trend_changes=changes,min_live_strength=strength))}
        key='yes_cents' if direction=='UP / ABOVE' else 'no_cents'
        other='no_cents' if key=='yes_cents' else 'yes_cents'
        p['contract_price_history']=[{'target':83000,'observed_at':(start+timedelta(seconds=i)).isoformat(),key:round(price-.1+i*.01,4),other:round(101-price+.1-i*.01,4)} for i in range(11)]
        p['contract_prices']={key:price,other:101-price}
        page,qty,limit,buyin=Mock(),Mock(),Mock(),Mock();page.url='http://fixture.test'
        qty.input_value.return_value='3';limit.input_value.return_value=str(price);buyin.inner_text.return_value='Contracts'
        done=Mock();done.all.return_value=[]
        qty.or_.return_value=qty
        qty.count.return_value=1
        page.get_by_role.side_effect=lambda role,**args:buyin if role=='combobox' else qty if 'Contracts' in str(args.get('name')) else limit if role=='textbox' else done if args.get('name')=='Done' else Mock()
        record=Mock(return_value=True)
        with patch.object(rh,'read_event_target',return_value=83000), patch.object(rh,'review_buy_details',return_value=True), patch.object(rh,'select_time_in_force'), patch.object(rh,'parse_contract_prices',return_value=p['contract_prices']):
            result=rh.prepare_contract_form(page,'BTC',start,end,lambda *args:p,Mock(),record,fallback=True)
        return result,page,record

    def test_independent_changes_and_strength_are_enforced_and_recorded(self):
        for changes, strength, allowed in ((2, 'WEAK', True), (11, 'WEAK', False), (2, 'STRONG', False)):
            with self.subTest(changes=changes, strength=strength):
                result, _, record = self.form(changes=changes, strength=strength, confidence=.57)
                self.assertEqual(result, allowed)
                if allowed:
                    conditions = record.call_args_list[0].args[3]['conditions']
                    self.assertEqual(conditions['contract_trend_changes'], changes)
                    self.assertEqual(conditions['min_live_strength'], strength)
                else:
                    record.assert_not_called()

    def test_fallback_filter_migration_and_validation(self):
        migrated = settings.validate(dict(settings.DEFAULTS, contract_trend_changes=4, min_live_strength='WEAK'))
        self.assertEqual(migrated['fallbacks'][0]['contract_trend_changes'], 4)
        self.assertEqual(migrated['fallbacks'][0]['min_live_strength'], 'WEAK')
        for value in (0, 1.5, True, None, float('inf')):
            with self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS, fallbacks=[{'contract_trend_changes':value}]))
        with self.assertRaises(ValueError):
            settings.validate(dict(settings.DEFAULTS, fallbacks=[{'min_live_strength':'unknown'}]))

    def test_up_down_use_current_prediction_and_fallback_quantity(self):
        for direction,brti in (('UP / ABOVE',83100),('DOWN / BELOW',82900)):
            result,page,record=self.form(direction,brti)
            self.assertTrue(result)
            purchase=record.call_args_list[0].args[3]
            self.assertEqual(purchase['contracts'],3)
            self.assertEqual(purchase['direction'],direction)
            self.assertEqual(purchase['purchase_rule'],'fallback')
            self.assertEqual(purchase['conditions']['fallback']['min_distance_dollars'],50)

    def test_distance_window_price_and_existing_purchase_block(self):
        for args in ({'brti':83050},{'brti':82900},{'brti':None},{'minute':12},{'minute':15},{'price':70},{'price':90},{'purchased':True}):
            with self.subTest(args=args):
                result,_,record=self.form(**args)
                self.assertFalse(result);record.assert_not_called()

    def test_fallback_reports_direction_and_distance_separately(self):
        for direction, gap, expected in (
            ('DOWN / BELOW', 35.46, 'opposite to Live DOWN / BELOW'),
            (None, 35.46, 'fresh Live direction is not available'),
            ('UP / ABOVE', 10, 'requires more than 10 dollars'),
        ):
            with self.subTest(direction=direction, gap=gap):
                prediction = {'target': 83000, 'brti_price': 83000 + gap,
                              'live_direction': direction,
                              'order_settings': dict(settings.DEFAULTS, fallback=dict(
                                  settings.FALLBACK_DEFAULTS, enabled=True, min_distance_dollars=10))}
                page, report = Mock(), Mock()
                self.assertFalse(rh.prepare_contract_form(
                    page, 'BTC', None, None, lambda *args: prediction, report, fallback=True))
                self.assertIn(expected, report.call_args.args[0])
                page.get_by_role.assert_not_called()

    def test_selected_fallback_models_block_disagreement_before_browser_actions(self):
        for model in settings.MODEL_NAMES:
            prediction = {'target':83000, 'brti_price':83100, 'live_direction':'UP / ABOVE',
                          'model_directions':{model:'DOWN / BELOW'},
                          'order_settings':dict(settings.DEFAULTS, fallback=dict(
                              settings.FALLBACK_DEFAULTS, enabled=True, required_models=[model]))}
            page, report = Mock(), Mock()
            self.assertFalse(rh.prepare_contract_form(
                page, 'BTC', None, None, lambda *args:prediction, report, fallback=True))
            self.assertIn('selected fallback models', report.call_args.args[0])
            self.assertEqual(page.mock_calls, [])

    def test_fallback_models_validation_and_defaults(self):
        self.assertEqual(settings.validate_fallback({})['required_models'], [])
        self.assertEqual(settings.validate_fallback({'required_models':['Trend','Pattern']})['required_models'],
                         ['Pattern','Trend'])
        for value in ('Pattern', None, ['Unknown'], [True]):
            with self.assertRaises(ValueError):
                settings.validate_fallback({'required_models':value})

    def test_moving_brti_rechecks_distance_instead_of_exact_price(self):
        for direction, sign in (('UP / ABOVE', 1), ('DOWN / BELOW', -1)):
            expected = {'target':83000, 'brti_price':83000+sign*100,
                        'live_direction':direction,
                        'live_snapshot':{'direction':direction,'confidence':.7}}
            rule = dict(settings.FALLBACK_DEFAULTS, min_distance_dollars=50)
            for gap, allowed in ((101,True),(51,True),(50,False),(49,False)):
                current = dict(expected, brti_price=83000+sign*gap)
                self.assertEqual(rh.buy_prediction_matches(current, expected, rule), allowed)
            self.assertFalse(rh.buy_prediction_matches(dict(expected,brti_price=None),expected,rule))
            self.assertFalse(rh.buy_prediction_matches(dict(expected,target=83001),expected,rule))
            self.assertFalse(rh.buy_prediction_matches(dict(expected,purchase_exists=True),expected,rule))
            self.assertFalse(rh.buy_prediction_matches(dict(expected,live_snapshot=None),expected,rule))

    def test_fallback_validation(self):
        for values in ({'minute_from':15},{'contracts':1.5},{'min_price_cents':95},{'enabled':'yes'}):
            with self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS,fallback=dict(settings.FALLBACK_DEFAULTS,**values)))

    def test_signed_distance_allows_only_prices_above_directional_threshold(self):
        for direction, sign in (('UP / ABOVE', 1), ('DOWN / BELOW', -1)):
            for threshold, gap, allowed in ((-30, -29, True), (-30, -30, False),
                                            (-30, -31, False), (-30, 0, True),
                                            (-30, 10, True), (0, 0, False),
                                            (0, 1, True), (0, -1, False)):
                with self.subTest(direction=direction, threshold=threshold, gap=gap):
                    result, _, record = self.form(direction=direction, brti=83000+sign*gap,
                                                  min_distance=threshold)
                    self.assertEqual(result, allowed)
                    if not allowed:
                        record.assert_not_called()

    def test_signed_distance_persists_and_nonfinite_values_are_rejected(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder, patch.object(settings, 'PATH', Path(folder)/'settings.json'):
            saved = settings.save(dict(settings.DEFAULTS, fallback=dict(
                settings.FALLBACK_DEFAULTS, min_distance_dollars=-30)))
            self.assertEqual(settings.load()['fallbacks'][0]['min_distance_dollars'], -30)
        for value in (float('nan'), float('inf'), float('-inf')):
            with self.assertRaises(ValueError):
                settings.validate_fallback(dict(settings.FALLBACK_DEFAULTS, min_distance_dollars=value))

    def test_blank_distance_disables_directional_gap_gate(self):
        for direction in ('UP / ABOVE', 'DOWN / BELOW'):
            for brti in (82900, 83000, 83100):
                with self.subTest(direction=direction, brti=brti):
                    result, _, record = self.form(direction=direction, brti=brti, min_distance=None)
                    self.assertTrue(result)
                    self.assertIsNone(record.call_args_list[0].args[3]['conditions']['fallback']['min_distance_dollars'])
        result, _, record = self.form(brti=None, min_distance=None)
        self.assertFalse(result)
        record.assert_not_called()

    def test_blank_distance_persists_through_settings_api(self):
        import app
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder, patch.object(settings, 'PATH', Path(folder)/'settings.json'), patch.dict(app.ORDER_SETTINGS):
            rule = dict(settings.FALLBACK_DEFAULTS, enabled=True, min_distance_dollars=None)
            response = app.app.test_client().post('/api/order-settings', json=dict(settings.DEFAULTS, fallbacks=[rule]))
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(settings.load()['fallbacks'][0]['min_distance_dollars'])
            self.assertIsNone(app.app.test_client().get('/api/state').get_json()['order_settings']['fallbacks'][0]['min_distance_dollars'])

    def test_multiple_rules_validate_and_legacy_rule_is_preserved(self):
        first = dict(settings.FALLBACK_DEFAULTS, enabled=True, contracts=2)
        second = dict(first, minute_from=10, min_distance_dollars=100, contracts=5)
        self.assertEqual(settings.validate(dict(settings.DEFAULTS, fallback=first))['fallbacks'],
                         [dict(first, contract_trend_changes=10, min_live_strength="MODERATE", close_conditions=settings.CLOSE_DEFAULTS, stop_loss=settings.STOP_LOSS_DEFAULTS)])
        result = settings.validate(dict(settings.DEFAULTS, fallbacks=[first, second]))
        self.assertEqual([r['contracts'] for r in result['fallbacks']], [2, 5])
        self.assertEqual(settings.validate(dict(settings.DEFAULTS, fallbacks=[]))['fallbacks'], [])
        for invalid in (None, {}, [dict(second, contracts=0)]):
            with self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS, fallbacks=invalid))

    def test_fallback_priority_skips_disabled_and_stops_after_submission(self):
        rules = [dict(settings.FALLBACK_DEFAULTS, enabled=enabled) for enabled in (False, True, True, True)]
        prediction = {'order_settings': dict(settings.DEFAULTS, fallbacks=rules)}
        with patch.object(rh, 'prepare_contract_form', side_effect=[False, True]) as prepare:
            self.assertTrue(rh.prepare_fallback_forms(Mock(), 'BTC', None, None,
                            lambda *args: prediction, Mock(), Mock()))
        self.assertEqual([call.kwargs['fallback_index'] for call in prepare.call_args_list], [1, 2])
