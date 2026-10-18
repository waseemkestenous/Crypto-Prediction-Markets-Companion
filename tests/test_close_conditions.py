import unittest
from datetime import datetime, timedelta
import order_settings as settings
import robinhood_target as rh
from unittest.mock import Mock, patch
from copy import deepcopy
from playwright.sync_api import sync_playwright


class CloseConditionsTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime.fromisoformat('2026-10-12T12:00:00-05:00')
        self.end = self.start + timedelta(minutes=15)
        self.prediction = {
            'target': 83000, 'direction': 'DOWN / BELOW',
            'purchase': {'status': 'filled', 'target': 83000, 'direction': 'UP / ABOVE', 'contracts':1, 'execution_price_cents':80},
            'order_settings': dict(settings.DEFAULTS, close_conditions=dict(settings.CLOSE_DEFAULTS, enabled=True,profit_per_ct_cents=1.12))}

    def matches(self, price=90, minute=5):
        return rh.close_conditions_match(self.prediction, price, self.start, self.end,
                                        self.start + timedelta(minutes=minute))

    def test_price_and_window_boundaries(self):
        self.assertTrue(self.matches())
        self.assertTrue(self.matches(price=95))
        for price in (85, 84, 0, 101, float('nan'), True, None):
            self.assertFalse(self.matches(price=price))
        for minute in (4.9, 10, 15):
            self.assertFalse(self.matches(minute=minute))
        self.assertTrue(self.matches(minute=9.99))

    def test_requires_confirmed_purchase_and_enabled_rule(self):
        for status in ('submitted', 'attempted', 'cancelled'):
            self.prediction['purchase']['status'] = status
            self.assertFalse(self.matches())
        self.prediction['purchase']['status'] = 'filled'
        self.prediction['purchase']['target'] = 82000
        self.assertFalse(self.matches())
        self.prediction['purchase']['target'] = 83000
        self.prediction['order_settings']['close_conditions']['enabled'] = False
        self.assertFalse(self.matches())

    def test_validation_and_disabled_legacy_default(self):
        self.assertEqual(settings.validate(settings.DEFAULTS)['close_conditions'], settings.CLOSE_DEFAULTS)
        for override in ({'minute_from': 10}, {'minute_to': 16},
                         {'profit_per_ct_cents': -1}, {'enabled': 'yes'}, {'time_in_force': 'invalid'}):
            with self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS, close_conditions=dict(settings.CLOSE_DEFAULTS, **override)))

    def test_signed_brti_delta_uses_held_direction_and_strict_threshold(self):
        close=self.prediction['order_settings']['close_conditions']
        for direction,sign in (('UP / ABOVE',1),('DOWN / BELOW',-1)):
            self.prediction['purchase']['direction']=direction
            for threshold in (-30,0,30):
                close['max_brti_delta_dollars']=threshold
                for offset,allowed in ((-0.01,True),(0,False),(0.01,False)):
                    self.prediction['brti_price']=83000+sign*(threshold+offset)
                    with self.subTest(direction=direction,threshold=threshold,offset=offset):
                        self.assertEqual(self.matches(),allowed)
                        self.assertFalse(self.matches(price=85))
                        self.assertFalse(self.matches(minute=10))

    def test_optional_brti_delta_and_missing_quotes(self):
        close=self.prediction['order_settings']['close_conditions']
        close['max_brti_delta_dollars']=0
        for quote in (None,True,float('nan'),float('inf')):
            self.prediction['brti_price']=quote
            self.assertFalse(self.matches())
        close['max_brti_delta_dollars']=None
        self.assertTrue(self.matches())

    def test_delta_validation_and_api_persistence(self):
        import app
        import tempfile
        from pathlib import Path
        for value in (True,'', 'invalid',float('nan'),float('inf')):
            with self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS,close_conditions={'max_brti_delta_dollars':value}))
        with tempfile.TemporaryDirectory() as folder,patch.object(settings,'PATH',Path(folder)/'settings.json'),patch.dict(app.ORDER_SETTINGS):
            for value in (-30,0,30,None):
                response=app.app.test_client().post('/api/order-settings',json=dict(settings.DEFAULTS,
                    close_conditions=dict(settings.CLOSE_DEFAULTS,max_brti_delta_dollars=value)))
                self.assertEqual(response.status_code,200)
                self.assertEqual(settings.load()['close_conditions']['max_brti_delta_dollars'],value)

    def test_close_only_delta_gets_fresh_source_and_rejects_stale_source(self):
        import app
        from types import SimpleNamespace
        now=self.start+timedelta(minutes=6)
        prediction={'start':self.start,'end':self.end,'prediction':'UP / ABOVE'}
        configured=settings.validate(dict(settings.DEFAULTS,fallbacks=[],stop_loss={'enabled':False},
            close_conditions=dict(settings.CLOSE_DEFAULTS,enabled=True,max_brti_delta_dollars=0)))
        source=SimpleNamespace(mode='cfbenchmarks',last_quote_at=now)
        with patch.dict(app.ORDER_SETTINGS,configured,clear=True),patch.object(app,'SOURCE',source),patch.object(app.ARGS,'asset','BTC'),patch.object(app,'now_local',return_value=now),patch.object(app,'current_round_key',return_value=(self.start,self.end)),patch.dict(app.STATE,round_start=self.start,target=83000,prediction=prediction,live_recommendation=None,live_updated_at=now,exchange_quotes={'CF Benchmarks BRTI':82990}):
            self.assertEqual(app.robinhood_form_prediction('BTC',self.start,self.end)['brti_price'],82990)
            source.last_quote_at=now-timedelta(seconds=21)
            self.assertIsNone(app.robinhood_form_prediction('BTC',self.start,self.end)['brti_price'])

    def test_close_time_in_force_is_independent_of_purchase_setting(self):
        validated=settings.validate(dict(settings.DEFAULTS, time_in_force='ioc',
            close_conditions=dict(settings.CLOSE_DEFAULTS,time_in_force='good_till_date')))
        self.assertEqual(validated['time_in_force'],'ioc')
        self.assertEqual(validated['close_conditions']['time_in_force'],'good_till_date')


class CloseBrowserTests(unittest.TestCase):
    def test_trailing_peak_follows_observed_bids_without_lowering(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            page=browser.new_page()
            start=datetime.now().astimezone()-timedelta(minutes=1)
            end=start+timedelta(minutes=15)
            prediction={'target':83000,'purchase':{'status':'filled','target':83000,
                'direction':'UP / ABOVE','execution_price_cents':70,'contracts':1},
                'order_settings':dict(settings.DEFAULTS)}
            def record(asset,start,end,update):
                prediction['purchase'].update({k:v for k,v in update.items() if k!='target'})
                return True
            with patch.object(rh,'read_event_target',return_value=83000):
                for price in (50,90,85):
                    page.set_content(f'<section><h3>Yes · $83,000 or above</h3><div>Contracts 1 available</div><input><div>Bid {price}¢</div><button>Close all</button></section>')
                    self.assertFalse(rh.prepare_close_order(page,'BTC',start,end,lambda *args:deepcopy(prediction),Mock(),record))
                    self.assertEqual(prediction['purchase']['stop_loss_peak_bid_cents'],50 if price==50 else 90)
            browser.close()

    def test_order_placed_with_done_requires_full_filled_quantity(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            prediction = {'purchase': {'close_order': {'status': 'submitted', 'contracts': 1}}}
            record = Mock(return_value=True)
            page.set_content('''<h3>$83,531.59 or above order placed</h3>
                <p>Your order to sell 1 contract is placed.</p>
                <div>Filled quantity</div><div id="filled">0 of 1</div>
                <button onclick="document.body.dataset.done='clicked'">Done</button>''')
            with patch.object(rh, 'read_event_target', return_value=83531.59):
                self.assertFalse(rh.observe_close_receipt(page, 'BTC', None, None,
                    lambda *args: prediction, record, Mock()))
                record.assert_not_called()
                self.assertIsNone(page.locator('body').get_attribute('data-done'))
                page.locator('#filled').evaluate("el => el.textContent='1 of 1'")
                self.assertTrue(rh.observe_close_receipt(page, 'BTC', None, None,
                    lambda *args: prediction, record, Mock()))
                self.assertEqual(record.call_args.args[3]['close_order']['status'], 'filled')
                self.assertEqual(page.locator('body').get_attribute('data-done'), 'clicked')
            browser.close()

    def test_close_all_sale_is_confirmed_and_never_submitted_twice(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            start = datetime.now().astimezone() - timedelta(minutes=6)
            end = start + timedelta(minutes=15)
            for side, direction, stop, stop_tif in [('Yes', 'UP / ABOVE', False, 'ioc'), ('No', 'DOWN / BELOW', False, 'ioc'), ('Yes', 'UP / ABOVE', True, 'ioc'), ('No', 'DOWN / BELOW', True, 'ioc'), ('Yes', 'UP / ABOVE', True, 'good_till_date'), ('No', 'DOWN / BELOW', True, 'good_till_date')]:
                page.set_content(f'''<button id="outside-close">Close</button><section>
                  <h3>{side} · $83,000 or above</h3><button>Close</button>
                  <label for="qty">Contracts <span>3 available</span></label>
                  <input id="qty" type="{'number' if side == 'No' else 'text'}" value="0">
                  <label for="limit">Limit Price</label><div>Bid 90¢ · Ask 91¢</div>
                  <input id="limit" value="91">
                  <button role="combobox" aria-label="Time in force" id="tif"
                    onclick="gtd.hidden=false">Good till date</button>
                  <button role="option" id="gtd" hidden
                    onclick="tif.textContent='Immediate or cancel (IOC)';gtd.hidden=true">Immediate or cancel (IOC)</button>
                  <button onclick="document.body.dataset.reviewed='yes';review.hidden=false;this.parentElement.hidden=true">Close All</button>
                  </section><section id="review" hidden><h3>{side} · $83,000 or above</h3>
                  <div>Contracts 3</div><div>Limit Price Bid 90¢ · Ask 91¢ 90¢</div>
                  <div>Time in Force Immediate or cancel (IOC)</div><div>Order summary</div>
                  <button onclick="document.body.dataset.sold='yes';receipt.hidden=false;review.hidden=true">Close</button>
                  </section><div id="receipt" hidden><div>Filled quantity</div><div>3 of 3</div>
                  <button onclick="receipt.hidden=true">Done</button></div>''')
                if stop:
                    page.evaluate("""() => {
                        const option=document.getElementById('gtd');
                        document.body.appendChild(option);
                        option.setAttribute('role','menuitemradio');
                        option.innerHTML='<span>Immediate or cancel (IOC)</span><span>Any part of an IOC order that cannot be filled immediately will be canceled.</span>';
                        option.onclick=()=>{option.hidden=true;setTimeout(()=>document.getElementById('tif').textContent='Immediate or cancel (IOC)',100);};
                    }""")
                prediction = {'target': 83000, 'purchase': {'status': 'filled',
                    'target': 83000, 'direction': direction, 'contracts': 3, 'execution_price_cents': 50, 'stop_loss_peak_bid_cents':95 if stop else 90},
                    'order_settings': dict(settings.DEFAULTS, stop_loss={'enabled':stop,'drop_cents':5,'time_in_force':stop_tif}, close_conditions=dict(settings.CLOSE_DEFAULTS, enabled=not stop))}
                if stop and stop_tif=='good_till_date':
                    page.locator('#review').evaluate("node=>node.innerHTML=node.innerHTML.replace('Time in Force Immediate or cancel (IOC)','Time in Force Good till date')")
                if side == 'No':
                    # Resume an already-open review, without clicking the Close tab.
                    page.evaluate("document.querySelector('section').hidden=true;review.hidden=false;document.body.dataset.reviewed='yes'")
                def snapshot(*args):
                    return deepcopy(prediction)
                def record(asset, start, end, update):
                    if 'holding_quote' in update: return True
                    previous = prediction['purchase'].get('close_order') or {}
                    prediction['purchase']['close_order'] = dict(previous, **update['close_order'])
                    return True
                with patch.object(rh, 'read_event_target', return_value=83000):
                    self.assertTrue(rh.prepare_close_order(page, 'BTC', start, end, snapshot, Mock(), record))
                    self.assertEqual(prediction['purchase']['close_order']['status'], 'filled')
                    self.assertEqual(prediction['purchase']['close_order']['reason'], 'stop_loss' if stop else 'close_conditions')
                    self.assertEqual(prediction['purchase']['close_order']['limit_price_cents'], 90)
                    self.assertEqual(prediction['purchase']['close_order']['time_in_force'], stop_tif if stop else 'ioc')
                    if side == 'Yes':
                        self.assertEqual(page.locator('#tif').inner_text(), 'Good till date' if stop and stop_tif=='good_till_date' else 'Immediate or cancel (IOC)')
                    self.assertEqual(page.locator('body').get_attribute('data-sold'), 'yes')
                    self.assertEqual(page.locator('body').get_attribute('data-reviewed'), 'yes')
                    page.evaluate("delete document.body.dataset.sold")
                    self.assertFalse(rh.prepare_close_order(page, 'BTC', start, end, snapshot, Mock(), record))
                    self.assertIsNone(page.locator('body').get_attribute('data-sold'))

    def test_review_rechecks_delta_and_saves_close_conditions(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            try:
                page=browser.new_page()
                page.set_content('<section><h3>Yes · $83,000 or above</h3><div>Contracts 1</div>'
                    '<div>Limit Price 90¢</div><div>Time in Force Immediate or cancel (IOC)</div>'
                    '<div>Order summary</div><button onclick="document.body.dataset.sold=1">Close</button></section>')
                start=datetime.now().astimezone()-timedelta(minutes=6)
                end=start+timedelta(minutes=15)
                prediction={'target':83000,'brti_price':82990,'purchase':{'status':'filled',
                    'target':83000,'direction':'UP / ABOVE','contracts':1,'execution_price_cents':80},'order_settings':dict(settings.DEFAULTS,
                    stop_loss={'enabled':False},close_conditions=dict(settings.CLOSE_DEFAULTS,enabled=True,max_brti_delta_dollars=0))}
                for quote in (83000,None):
                    current=deepcopy(prediction);current['brti_price']=quote
                    record=Mock(return_value=True)
                    self.assertFalse(rh.submit_close_review(page,'BTC',start,end,prediction,Mock(),record,lambda *args:current,contracts=1,price=90))
                    record.assert_not_called()
                    self.assertIsNone(page.locator('body').get_attribute('data-sold'))
                record=Mock(return_value=True)
                self.assertTrue(rh.submit_close_review(page,'BTC',start,end,prediction,Mock(),record,lambda *args:deepcopy(prediction),contracts=1,price=90))
                closing=record.call_args_list[0].args[3]['close_order']
                self.assertEqual(closing['conditions']['max_brti_delta_dollars'],0)
                self.assertEqual(closing['reason'],'close_conditions')
            finally:
                browser.close()

    def test_wrong_side_price_and_unconfirmed_position_never_close(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            start = datetime.now().astimezone() - timedelta(minutes=6)
            end = start + timedelta(minutes=15)
            for side, price, status in [('No', 90, 'filled'), ('Yes', 80, 'filled'), ('Yes', 90, 'submitted')]:
                page.set_content(f'''<section><h3>{side} · $83,000 or above</h3>
                  <div>Contracts 1 available</div><input><div>Bid {price}¢ · Ask 91¢</div>
                  <button onclick="document.body.dataset.closed='yes'">Close all</button></section>''')
                prediction = {'target': 83000, 'purchase': {'status': status, 'target': 83000,
                    'direction': 'UP / ABOVE', 'contracts': 1,'execution_price_cents':80}, 'order_settings': dict(
                    settings.DEFAULTS, stop_loss={'enabled':False,'drop_cents':20}, close_conditions=dict(settings.CLOSE_DEFAULTS, enabled=True))}
                record = Mock()
                with patch.object(rh, 'read_event_target', return_value=83000):
                    self.assertFalse(rh.prepare_close_order(page, 'BTC', start, end,
                                     lambda *args: prediction, Mock(), record))
                self.assertFalse(any('close_order' in call.args[3] for call in record.call_args_list))
                self.assertIsNone(page.locator('body').get_attribute('data-closed'))
