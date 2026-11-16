import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
import app
import order_settings
import robinhood_target as rh
import round_history
from tests import test_force_buy


class TestModeTests(unittest.TestCase):
    def test_setting_is_opt_in_and_boolean(self):
        self.assertFalse(order_settings.validate(order_settings.DEFAULTS)['test_mode'])
        for value in ('true', 1, None):
            with self.assertRaises(ValueError):
                order_settings.validate(dict(order_settings.DEFAULTS, test_mode=value))

    def test_saved_mode_survives_reload(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(order_settings, 'PATH', Path(folder)/'settings.json'):
            order_settings.save(dict(order_settings.DEFAULTS, test_mode=True))
            self.assertTrue(order_settings.load()['test_mode'])

    def test_entry_never_touches_order_controls(self):
        with patch.dict(order_settings.DEFAULTS, test_mode=True):
            result, record, tif, page = test_force_buy.ForceBuyTests().form()
        self.assertTrue(result)
        page.get_by_role.assert_not_called()
        tif.assert_not_called()
        self.assertTrue(record.call_args_list[0].args[3]['test_mode'])
        self.assertEqual(record.call_args_list[-1].args[3]['status'], 'filled')

    def test_accuracy_pause_allows_virtual_entry_but_still_blocks_real_entry(self):
        paused = {'order_settings': dict(order_settings.DEFAULTS,
                  accuracy_buy_guard={'enabled': True}), 'buy_accuracy_guard': {'allowed': False}}
        self.assertFalse(rh.buy_accuracy_guard_allows(paused))
        paused['order_settings']['test_mode'] = True
        self.assertTrue(rh.buy_accuracy_guard_allows(paused))
        with patch.dict(order_settings.DEFAULTS, test_mode=True,
                        accuracy_buy_guard={'enabled': True}), patch.object(
                            rh, 'buy_accuracy_guard_allows', wraps=rh.buy_accuracy_guard_allows):
            result, record, _, page = test_force_buy.ForceBuyTests().form()
        self.assertTrue(result)
        page.get_by_role.assert_not_called()
        self.assertTrue(record.call_args_list[0].args[3]['test_mode'])

    def test_accuracy_pause_journals_only_virtual_attempts_when_test_mode_enabled(self):
        now = datetime.now().astimezone()
        start, end = now-timedelta(minutes=3), now+timedelta(minutes=12)
        with tempfile.TemporaryDirectory() as folder, patch.object(round_history, 'ROOT', Path(folder)), \
                patch.dict(app.ORDER_SETTINGS, test_mode=True, accuracy_buy_guard={'enabled': False}), \
                patch.object(app, 'current_buy_accuracy_guard', return_value={'allowed': False}), \
                patch.object(app, 'current_buy_choppiness_guard', return_value={'allowed': True}):
            round_history.upsert('BTC', start.isoformat(), end.isoformat(), target=83000)
            attempt = {'status': 'attempted', 'target': 83000, 'test_mode': True}
            self.assertTrue(app._record_robinhood_purchase('BTC', start, end, attempt))
            self.assertFalse(app._record_robinhood_purchase('BTC', start, end, dict(attempt, test_mode=False)))
            with patch.dict(app.ORDER_SETTINGS, test_mode=False):
                self.assertFalse(app._record_robinhood_purchase('BTC', start, end, attempt))

    def test_automatic_mode_follows_accuracy_and_manual_switch(self):
        config = dict(order_settings.DEFAULTS, test_mode=False,
                      accuracy_buy_guard={'enabled': True})
        prediction = {'order_settings': config, 'buy_accuracy_guard': {'allowed': False}}
        self.assertTrue(rh.effective_test_mode(prediction))
        prediction['buy_accuracy_guard']['allowed'] = True
        self.assertFalse(rh.effective_test_mode(prediction))
        config['test_mode'] = True
        self.assertTrue(rh.effective_test_mode(prediction))
        config.update(test_mode=False, accuracy_buy_guard={'enabled': False})
        prediction['buy_accuracy_guard']['allowed'] = False
        self.assertFalse(rh.effective_test_mode(prediction))

    def test_mode_transition_invalidates_order_in_both_directions(self):
        settings = dict(order_settings.DEFAULTS, test_mode=False, accuracy_buy_guard={'enabled': True})
        live = {'order_settings': settings, 'buy_accuracy_guard': {'allowed': True}}
        virtual = dict(live, buy_accuracy_guard={'allowed': False})
        self.assertFalse(rh.buy_prediction_matches(live, virtual))
        self.assertFalse(rh.buy_prediction_matches(virtual, live))

    def test_automatic_virtual_buys_when_manual_switch_off(self):
        from tests import test_fallback_purchase
        original = rh.prepare_contract_form
        def paused_entry(*args, **kwargs):
            args = list(args)
            get_prediction = args[4]
            def paused(*query):
                prediction = get_prediction(*query)
                return dict(prediction, buy_accuracy_guard={'allowed': False},
                            order_settings=dict(prediction['order_settings'], test_mode=False,
                                                accuracy_buy_guard={'enabled': True}))
            args[4] = paused
            return original(*args, **kwargs)
        with patch.object(rh, 'prepare_contract_form', side_effect=paused_entry):
            result, record, _, page = test_force_buy.ForceBuyTests().form()
            self.assertTrue(result)
            page.get_by_role.assert_not_called()
            self.assertEqual(record.call_args_list[0].args[3]['test_mode_reason'], 'accuracy_pause')
            result, page, record = test_fallback_purchase.FallbackTests().form()
            self.assertTrue(result)
            page.get_by_role.assert_not_called()
            self.assertEqual(record.call_args_list[0].args[3]['test_mode_reason'], 'accuracy_pause')

    def test_automatic_mode_can_journal_virtual_but_rejects_live_attempt(self):
        now = datetime.now().astimezone()
        with tempfile.TemporaryDirectory() as folder, patch.object(round_history, 'ROOT', Path(folder)), \
                patch.dict(app.ORDER_SETTINGS, test_mode=False, accuracy_buy_guard={'enabled': True}), \
                patch.object(app, 'current_buy_accuracy_guard', return_value={'allowed': False}), \
                patch.object(app, 'current_buy_choppiness_guard', return_value={'allowed': True}):
            round_history.upsert('BTC', now.isoformat(), now.isoformat(), target=83000)
            self.assertTrue(app._record_robinhood_purchase('BTC', now, now,
                {'status': 'attempted', 'test_mode': True, 'target': 83000}))
            self.assertFalse(app._record_robinhood_purchase('BTC', now, now,
                {'status': 'attempted', 'target': 83000}))

    def test_virtual_lifecycle_persists_and_closes_after_mode_disabled(self):
        now = datetime.now().astimezone()
        start, end = now-timedelta(minutes=3), now+timedelta(minutes=12)
        config = order_settings.validate(dict(order_settings.DEFAULTS, test_mode=False))
        purchase = {'status':'filled','test_mode':True,'target':83000,'contracts':2,
                    'direction':'UP / ABOVE','attempted_at':now.isoformat(),
                    'execution_price_cents':40,'limit_price_cents':40}
        with tempfile.TemporaryDirectory() as folder, patch.object(round_history,'ROOT',Path(folder)), patch.dict(app.ORDER_SETTINGS,config,clear=True):
            round_history.upsert('BTC',start.isoformat(),end.isoformat(),purchase=purchase)
            def prediction(*args):
                return {'target':83000,'purchase':round_history.get('BTC',start.isoformat())['purchase'],
                        'order_settings':config,'contract_prices':{'yes_cents':60}}
            page = Mock()
            with patch.object(rh,'sale_conditions_match',return_value=True), patch.object(rh,'force_close_matches',return_value=False), patch.object(rh,'stop_loss_matches',return_value=False):
                self.assertTrue(rh.prepare_close_order(page,'BTC',start,end,prediction,Mock(),app.record_robinhood_purchase))
            page.get_by_role.assert_not_called()
            saved=round_history.get('BTC',start.isoformat())['purchase']
            self.assertTrue(saved['test_mode'])
            self.assertEqual(saved['close_order']['status'],'filled')
            self.assertAlmostEqual(round_history.trade_pnl(saved,None),.3224)

    def test_test_mode_does_not_close_live_position(self):
        page=Mock()
        prediction={'purchase':{'status':'filled'},'order_settings':{'test_mode':True}}
        self.assertFalse(rh.prepare_close_order(page,'BTC',None,None,lambda *args:prediction,Mock(),Mock()))
        page.get_by_role.assert_not_called()
