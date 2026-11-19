from datetime import datetime, timedelta
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import app
import order_settings as settings
import round_history as journal
import robinhood_target as rh


class SettingsTests(unittest.TestCase):
    def test_trend_changes_validate_and_persist(self):
        self.assertEqual(settings.validate(settings.DEFAULTS)['contract_trend_changes'],10)
        for value in (0,-1,1.5,True,None,float('nan')):
            with self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS,contract_trend_changes=value))
        with tempfile.TemporaryDirectory() as folder,patch.object(settings,'PATH',Path(folder)/'settings.json'),patch.dict(app.ORDER_SETTINGS):
            response=app.app.test_client().post('/api/order-settings',json=dict(settings.DEFAULTS,contract_trend_changes=5))
            self.assertEqual(response.status_code,200)
            self.assertEqual(settings.load()['contract_trend_changes'],5)

    def test_time_in_force_validates_and_persists(self):
        self.assertEqual(settings.validate(settings.DEFAULTS)['time_in_force'], 'ioc')
        with self.assertRaises(ValueError):
            settings.validate(dict(settings.DEFAULTS, time_in_force='invalid'))
        with tempfile.TemporaryDirectory() as folder, patch.object(settings, 'PATH', Path(folder)/'settings.json'), patch.dict(app.ORDER_SETTINGS):
            response=app.app.test_client().post('/api/order-settings',json=dict(settings.DEFAULTS,time_in_force='good_till_date'))
            self.assertEqual(response.status_code,200)
            self.assertEqual(settings.load()['time_in_force'],'good_till_date')
    def test_invalid_prices_and_quantities(self):
        for price, quantity in ((0,1),(101,1),(float('nan'),1),(60,0),(60,1.5),(True,1),(60,True)):
            with self.subTest(price=price,quantity=quantity), self.assertRaises(ValueError):
                settings.validate({'max_price_cents':price,'contracts':quantity})

    def test_initial_balance_validates_and_supports_old_settings(self):
        self.assertEqual(settings.validate({'max_price_cents':60,'contracts':1})['initial_balance'],0)
        self.assertEqual(settings.validate({'max_price_cents':60,'contracts':1,'initial_balance':123.45})['initial_balance'],123.45)
        for value in (-1, float('nan'), float('inf'), True, 'invalid'):
            with self.assertRaises(ValueError):
                settings.validate({'max_price_cents':60,'contracts':1,'initial_balance':value})

    def test_save_endpoint_persists_and_reports_settings(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(settings,'PATH',Path(folder)/'settings.json'),patch.object(journal,'ROOT',Path(folder)),patch.dict(app.ORDER_SETTINGS,settings.DEFAULTS):
            client=app.app.test_client()
            response=client.post('/api/order-settings',json={'max_price_cents':75,'contracts':3})
            self.assertEqual(response.status_code,200)
            self.assertEqual(settings.load(),settings.validate(dict(settings.DEFAULTS,max_price_cents=75.0,contracts=3)))
            self.assertEqual(client.get('/api/state').get_json()['order_settings'],settings.load())
            self.assertEqual(client.post('/api/order-settings',json={'max_price_cents':101,'contracts':0}).status_code,400)
            self.assertEqual(settings.load()['contracts'],3)

    def test_purchase_conditions_validate_and_persist(self):
        for models in (['Unknown'], ['Pattern', 'Pattern'], 'Pattern', [None]):
            with self.subTest(models=models), self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS, required_models=models))
        with self.assertRaises(ValueError):
            settings.validate(dict(settings.DEFAULTS, agreement_basis='unknown'))
        with tempfile.TemporaryDirectory() as folder, patch.object(settings, 'PATH', Path(folder)/'settings.json'), patch.dict(app.ORDER_SETTINGS):
            response = app.app.test_client().post('/api/order-settings', json=dict(settings.DEFAULTS, required_models=['Trend', 'Pattern'], agreement_basis='live'))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(settings.load()['required_models'], ['Pattern', 'Trend'])
            self.assertEqual(settings.load()['agreement_basis'], 'live')

    def form(self,price,threshold,quantity,record=None,minimum=0,prices=None,trend=None,observed_quotes=None):
        prediction={'direction':'UP / ABOVE','target':83000,'model_directions':dict.fromkeys(
            ('Final prediction','Pattern','Recent','Bootstrap','Trend'),'UP / ABOVE'),
            'order_settings':{'min_price_cents':minimum,'max_price_cents':threshold,'contracts':quantity},
            'live_snapshot':{'direction':'UP / ABOVE','p_up':.7}}
        start=datetime.now().astimezone()-timedelta(minutes=1)
        end=start+timedelta(minutes=15)
        prediction['contract_price_history']=[{'target':83000,'observed_at':(start+timedelta(seconds=i)).isoformat(),'yes_cents':round(price-.1+i*.01,4),'no_cents':round(101-price+.1-i*.01,4)} for i in range(11)]
        prediction['contract_prices']={'yes_cents':price,'no_cents':101-price}
        if trend is not None:
            prediction.update(trend)
        page,qty,limit,buyin=Mock(),Mock(),Mock(),Mock()
        page.url='http://fixture.test/event'
        buyin.inner_text.return_value='Contracts'
        qty.input_value.return_value=str(quantity)
        limit.input_value.side_effect=[str(value) for value in prices]+[str(prices[-1])]*4 if prices else [str(price)]*6
        done = Mock()
        done.all.return_value = [done]
        done.is_visible.return_value = True
        other_button = Mock()
        other_button.all.return_value = []
        page.locator.return_value.inner_text.return_value = f'Filled quantity {quantity} of {quantity}'
        qty.or_.return_value=qty
        qty.count.return_value=1
        page.get_by_role.side_effect=lambda role,**args: (buyin if role=='combobox' else
            qty if 'Contracts' in str(args.get('name')) else limit if role=='textbox' else
            done if args.get('name')=='Done' else other_button)
        with patch.object(rh,'read_event_target',return_value=83000), patch.object(rh,'review_buy_details',return_value=True), patch.object(rh,'select_time_in_force') as tif, patch.object(rh,'parse_contract_prices',side_effect=observed_quotes or [prediction['contract_prices']]*2):
            result=rh.prepare_contract_form(page,'BTC',start,end,lambda *args:prediction,Mock(),record)
        if result:
            tif.assert_called_once()
        return result,qty,limit

    def test_form_uses_configurable_threshold_and_quantity_with_mock_controls(self):
        result,qty,limit=self.form(74,75,3)
        self.assertTrue(result)
        qty.fill.assert_called_once_with('3',timeout=3000)
        limit.fill.assert_not_called()  # The verified form already contains this price.

    def test_successful_buy_records_filled_with_saved_quantity_and_price(self):
        record=Mock(return_value=True)
        result,_,_=self.form(38,60,2,record)
        self.assertTrue(result)
        attempt=record.call_args_list[0].args[3]
        self.assertEqual(record.call_args_list[1].args[3]['status'], 'submitted')
        filled=record.call_args_list[2].args[3]
        self.assertEqual(attempt['contracts'],2)
        self.assertEqual(attempt['limit_price_cents'],38)
        self.assertEqual(filled['status'],'filled')
        self.assertEqual(filled['target'],83000)
        self.assertEqual(filled['confirmed_by'],'visible_order_receipt')
        self.assertEqual(filled['filled_quantity'], 2)

    def test_threshold_is_strict(self):
        result,qty,limit=self.form(75,75,3)
        self.assertFalse(result)
        qty.fill.assert_not_called()
        limit.fill.assert_not_called()

    def test_minimum_validation_and_legacy_default(self):
        self.assertEqual(settings.validate({'max_price_cents':60,'contracts':1})['min_price_cents'],0)
        for minimum in (-1,60,61,True,None,'bad',float('nan'),float('inf')):
            with self.subTest(minimum=minimum), self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS,min_price_cents=minimum))

    def test_minimum_is_strict_and_inside_range_allowed(self):
        for price in (19,20,60):
            result,qty,limit=self.form(price,60,1,minimum=20)
            self.assertFalse(result)
            qty.fill.assert_not_called()
            limit.fill.assert_not_called()
        self.assertTrue(self.form(21,60,1,minimum=20)[0])

    def test_price_falling_to_minimum_clears_quantity(self):
        for prices in ([30,20],[30,30,20]):
            result,qty,_=self.form(30,60,1,minimum=20,prices=prices)
            self.assertFalse(result)
            self.assertEqual(qty.fill.call_args.args[0],'')

    def test_minimum_persists_through_endpoint(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(settings,'PATH',Path(folder)/'settings.json'),patch.dict(app.ORDER_SETTINGS):
            client=app.app.test_client()
            response=client.post('/api/order-settings',json=dict(settings.DEFAULTS,min_price_cents=20))
            self.assertEqual(response.status_code,200)
            self.assertEqual(settings.load()['min_price_cents'],20)
            self.assertEqual(client.get('/api/state').get_json()['order_settings']['min_price_cents'],20)

    def test_empty_models_persist_without_restoring_defaults(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(settings,'PATH',Path(folder)/'settings.json'),patch.dict(app.ORDER_SETTINGS):
            client=app.app.test_client()
            response=client.post('/api/order-settings',json=dict(settings.DEFAULTS,required_models=[]))
            self.assertEqual(response.status_code,200)
            self.assertEqual(settings.load()['required_models'],[])
            self.assertEqual(client.get('/api/state').get_json()['order_settings']['required_models'],[])

    def test_both_agreement_persists(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(settings,'PATH',Path(folder)/'settings.json'),patch.dict(app.ORDER_SETTINGS):
            client=app.app.test_client()
            response=client.post('/api/order-settings',json=dict(settings.DEFAULTS,agreement_basis='both',required_models=[]))
            self.assertEqual(response.status_code,200)
            self.assertEqual(settings.load()['agreement_basis'],'both')
            self.assertEqual(client.get('/api/state').get_json()['order_settings']['agreement_basis'],'both')

    def test_purchase_conditions_saved_before_buy_and_preserved_on_fill(self):
        from datetime import datetime, timedelta
        start=datetime.now().astimezone(); end=start+timedelta(minutes=15)
        with tempfile.TemporaryDirectory() as folder, patch.object(journal,'ROOT',Path(folder)):
            journal.upsert('BTC',app.dt_iso(start),app.dt_iso(end),prediction='UP / ABOVE')
            def record(asset,unused_start,unused_end,purchase):
                return app.record_robinhood_purchase(asset,start,end,purchase)
            result,_,_=self.form(38,60,2,record,minimum=20)
            self.assertTrue(result)
            purchase=journal.load('BTC')[0]['purchase']
            self.assertEqual(purchase['status'],'filled')
            self.assertEqual(purchase['conditions'],{
            'require_final_live_agreement':False,'min_model_agreement':0,
                'min_price_cents':20,'max_price_cents':60,'contracts':2,
                'required_models':list(settings.MODEL_NAMES),'agreement_basis':'either',
                'entry_stability':dict(settings.ENTRY_STABILITY_DEFAULTS),
                'purchase_minute_from':0,'purchase_minute_to':15,'contract_trend_changes':10,'contract_trend_method':'linear_regression','min_live_strength':'MODERATE','rule':'primary','fallback':None,'fallback_rule_number':None,'time_in_force':'ioc'})
            with patch.dict(app.ORDER_SETTINGS,min_price_cents=40,required_models=[]):
                self.assertEqual(journal.load('BTC')[0]['purchase']['conditions']['min_price_cents'],20)
