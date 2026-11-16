from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

import app
import order_settings
import robinhood_target as rh
import round_history


class AccuracyBuyGuardTests(TestCase):
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)

    def settings(self, enabled=True, threshold=60, hours=24):
        return dict(order_settings.DEFAULTS, accuracy_buy_guard={
            'enabled': enabled, 'min_accuracy_pct': threshold, 'window_hours': hours})

    def row(self, hours=1, hit=True, **fields):
        return dict(round_end=(self.now-timedelta(hours=hours)).isoformat(),
                    prediction='UP / ABOVE', actual='UP / ABOVE' if hit else 'DOWN / BELOW',
                    settlement=100, **fields)

    def test_rolling_window_and_settled_unbought_rounds(self):
        rows = [self.row(), self.row(24), self.row(25), self.row(-1),
                dict(self.row(), settlement=None), dict(self.row(), prediction=None),
                {'round_end': 'invalid'}, dict(self.row(0), settlement=None, result_confirmed_by='user')]
        result = round_history.accuracy_buy_guard(rows, self.settings(), self.now)
        self.assertEqual((result['hits'], result['total'], result['accuracy']), (2, 2, 100))
        self.assertTrue(result['allowed'])

    def test_threshold_equality_resume_and_unrounded_comparison(self):
        rows = [self.row(hit=i < 3) for i in range(5)]
        self.assertTrue(round_history.accuracy_buy_guard(rows, self.settings(), self.now)['allowed'])
        self.assertFalse(round_history.accuracy_buy_guard(rows, self.settings(threshold=60.001), self.now)['allowed'])
        rows.append(self.row(hit=False))
        self.assertFalse(round_history.accuracy_buy_guard(rows, self.settings(), self.now)['allowed'])
        rows.extend([self.row(), self.row()])
        self.assertTrue(round_history.accuracy_buy_guard(rows, self.settings(), self.now)['allowed'])

    def test_custom_window_changes_the_buy_decision(self):
        rows = [self.row(hours=1, hit=False), self.row(hours=3), self.row(hours=5)]
        short = round_history.accuracy_buy_guard(rows, self.settings(hours=2), self.now)
        long = round_history.accuracy_buy_guard(rows, self.settings(hours=6), self.now)
        self.assertEqual(short['total'], 1)
        self.assertFalse(short['allowed'])
        self.assertEqual(long['total'], 3)
        self.assertTrue(long['allowed'])
        fractional = round_history.accuracy_buy_guard([self.row(hours=.25), self.row(hours=.5)],
                                                     self.settings(hours=.5), self.now)
        self.assertEqual(fractional['total'], 1)
        for hours in (0, -1, 8761, True, None, float('nan'), float('inf'), 'bad'):
            with self.subTest(hours=hours), self.assertRaises(ValueError):
                order_settings.validate(self.settings(hours=hours))
        legacy = dict(order_settings.DEFAULTS, accuracy_buy_guard={'enabled': True, 'min_accuracy_pct': 65})
        self.assertEqual(order_settings.validate(legacy)['accuracy_buy_guard']['window_hours'], 24)

    def test_empty_history_pauses_only_when_enabled(self):
        self.assertFalse(round_history.accuracy_buy_guard([], self.settings(), self.now)['allowed'])
        self.assertTrue(round_history.accuracy_buy_guard([], self.settings(False), self.now)['allowed'])

    def test_validation_and_api_persistence(self):
        self.assertFalse(order_settings.validate(order_settings.DEFAULTS)['accuracy_buy_guard']['enabled'])
        for value in (-1, 101, True, None, float('nan'), float('inf'), 'invalid'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                order_settings.validate(self.settings(threshold=value))
        with self.assertRaises(ValueError):
            order_settings.validate(dict(order_settings.DEFAULTS, accuracy_buy_guard={'enabled': 'yes'}))
        with TemporaryDirectory() as folder, patch.object(order_settings, 'PATH', Path(folder)/'settings.json'), patch.dict(app.ORDER_SETTINGS):
            result = app.app.test_client().post('/api/order-settings', json=self.settings(threshold=65, hours=6))
            self.assertEqual(result.status_code, 200)
            self.assertEqual(order_settings.load()['accuracy_buy_guard'], {'enabled': True, 'min_accuracy_pct': 65, 'window_hours': 6})

    def test_accuracy_pause_routes_to_virtual_mode_before_browser_actions(self):
        prediction = {'order_settings': self.settings(), 'buy_accuracy_guard': {'allowed': False, 'accuracy': 50}}
        for options in ({}, {'fallback': True}, {'force_buy': True}):
            page, report = Mock(), Mock()
            self.assertFalse(rh.prepare_contract_form(page, 'BTC', self.now, self.now+timedelta(minutes=15),
                                                     lambda *args: prediction, report, **options))
            self.assertEqual(page.mock_calls, [])
            self.assertTrue(rh.effective_test_mode(prediction))
            self.assertNotIn('Buy paused: Final', report.call_args.args[0])

    def test_live_rechecks_ignore_counts_but_obey_guard(self):
        prediction = {'order_settings': self.settings(), 'live_snapshot': {'direction': 'UP / ABOVE', 'confidence': .57},
                      'buy_accuracy_guard': {'allowed': True, 'total': 50}}
        updated = dict(prediction, buy_accuracy_guard={'allowed': True, 'total': 51})
        self.assertTrue(rh.buy_prediction_matches(updated, prediction))
        updated['buy_accuracy_guard']['allowed'] = False
        self.assertFalse(rh.buy_prediction_matches(updated, prediction))
        self.assertFalse(rh.buy_accuracy_guard_allows({'order_settings': self.settings()}))
        self.assertTrue(rh.buy_accuracy_guard_allows({'order_settings': self.settings(False)}))

    def test_last_buy_record_check_does_not_block_close_updates(self):
        with patch.object(app, 'current_buy_accuracy_guard', return_value={'allowed': False}) as guard, \
             patch.object(round_history, 'get', return_value=None) as get:
            self.assertFalse(app._record_robinhood_purchase('BTC', self.now, self.now, {'status': 'attempted'}))
            get.assert_not_called()
            guard.reset_mock()
            app._record_robinhood_purchase('BTC', self.now, self.now, {'close_order': {'status': 'attempted'}})
            guard.assert_not_called()
            get.assert_called_once()
