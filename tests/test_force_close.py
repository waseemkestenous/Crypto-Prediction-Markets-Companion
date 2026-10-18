import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
import app
import round_history
import robinhood_target as rh
import order_settings


class ForceCloseTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now().astimezone()
        self.start=self.now-timedelta(minutes=2)
        self.end=self.start+timedelta(minutes=15)
        self.token=self.start.isoformat()
        self.purchase={'status':'filled','target':83000,'direction':'UP / ABOVE',
                       'contracts':2,'attempted_at':self.token,'force_close_token':self.token}
        self.prediction={'target':83000,'purchase':self.purchase,
                         'order_settings':dict(order_settings.DEFAULTS,stop_loss={'enabled':False},force_close=dict(order_settings.FORCE_CLOSE_DEFAULTS,enabled=True))}

    def test_manual_request_keeps_position_checks(self):
        self.assertTrue(rh.sale_conditions_match(self.prediction,10,self.start,self.end,self.now))
        for changes in ({'force_close_token':'old'},{'status':'submitted'},
                        {'target':84000},{'close_order':{'status':'submitted'}}):
            p=dict(self.prediction,purchase=dict(self.purchase,**changes))
            self.assertFalse(rh.force_close_matches(p,10,self.start,self.end,self.now))
        for price in (0,True,float('nan'),101):
            self.assertFalse(rh.force_close_matches(self.prediction,price,self.start,self.end,self.now))
        self.assertFalse(rh.force_close_matches(self.prediction,10,self.start,self.end,self.end))

    def test_enabling_checkbox_never_closes_without_button_request(self):
        p=deepcopy(self.prediction)
        p['purchase'].pop('force_close_token')
        self.assertFalse(rh.force_close_matches(p,10,self.start,self.end,self.now))
        self.assertFalse(rh.sale_conditions_match(p,10,self.start,self.end,self.now))
        p['purchase']['force_close_token']=self.token
        p['order_settings']['force_close']['min_price_cents']=50
        self.assertFalse(rh.force_close_matches(p,10,self.start,self.end,self.now))
        self.assertTrue(rh.force_close_matches(p,51,self.start,self.end,self.now))
        p['order_settings']['force_close']['enabled']=False
        self.assertFalse(rh.force_close_matches(p,51,self.start,self.end,self.now))

    def test_endpoint_persists_request_and_rejects_stale_button(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(round_history,'ROOT',Path(folder)),patch.object(app.ARGS,'asset','BTC'),patch.object(app,'current_round_key',return_value=(self.start,self.end)),patch.dict(app.STATE,target=83000),patch.object(app,'robinhood_report'),patch.dict(app.ORDER_SETTINGS,force_close={'enabled':True}):
            purchase=dict(self.purchase);purchase.pop('force_close_token')
            round_history.upsert('BTC',self.start.isoformat(),self.end.isoformat(),purchase=purchase)
            client=app.app.test_client()
            payload={'asset':'BTC','round_start':self.start.isoformat(),'purchase_token':'old'}
            self.assertEqual(client.post('/api/force-close',json=payload).status_code,409)
            payload['purchase_token']=self.token
            self.assertEqual(client.post('/api/force-close',json=payload).status_code,200)
            saved=round_history.load('BTC')[0]['purchase']
            self.assertEqual(saved['force_close_token'],self.token)
            self.assertEqual(client.post('/api/force-close',json=payload).status_code,200)
            with patch.dict(app.ORDER_SETTINGS,force_close={'enabled':False}):
                self.assertEqual(client.post('/api/force-close',json=payload).status_code,409)
            payload['round_start']='old'
            self.assertEqual(client.post('/api/force-close',json=payload).status_code,409)

    def test_forced_review_uses_ioc_and_records_reason_before_submit(self):
        submit,review,page,record=Mock(),Mock(),Mock(),Mock(return_value=True)
        review.inner_text.return_value='Order Summary Yes · $83,000 or above Contracts 2 Limit Price 10¢ Time in Force Immediate or cancel'
        with patch.object(rh,'close_review_controls',return_value=[(submit,review)]),patch.object(rh,'observe_close_receipt'):
            self.assertTrue(rh.submit_close_review(page,'BTC',self.start,self.end,self.prediction,Mock(),record,lambda *args:deepcopy(self.prediction),2,10))
        closing=record.call_args_list[0].args[3]['close_order']
        self.assertEqual(closing['reason'],'force_close')
        self.assertEqual(closing['time_in_force'],'ioc')
        submit.click.assert_called_once()

    def test_validates_own_settings_and_drops_legacy_key(self):
        p=deepcopy(self.prediction)
        p['order_settings']['force_stop']=dict(order_settings.FORCE_CLOSE_DEFAULTS,
            enabled=False, min_price_cents=90)
        self.assertTrue(rh.force_close_matches(p,10,self.start,self.end,self.now))
        p['order_settings']['force_close']['min_price_cents']=50
        self.assertFalse(rh.force_close_matches(p,10,self.start,self.end,self.now))
        self.assertTrue(rh.force_close_matches(p,51,self.start,self.end,self.now))
        for update in ({'minute_from':15},{'min_price_cents':100},
                       {'time_in_force':'bad'},{'max_brti_delta_dollars':True}):
            with self.assertRaises(ValueError):
                order_settings.validate(dict(order_settings.DEFAULTS,force_close=update))
        with tempfile.TemporaryDirectory() as folder,patch.object(order_settings,'PATH',Path(folder)/'settings.json'):
            order_settings.save(p['order_settings'])
            self.assertEqual(order_settings.load()['force_close']['min_price_cents'],50)
            self.assertNotIn('force_stop',order_settings.load())

    def test_independent_delta_window_and_time_in_force(self):
        p=deepcopy(self.prediction)
        p['order_settings']['force_close'].update(minute_from=1,minute_to=4,
            max_brti_delta_dollars=0,time_in_force='good_till_date')
        p['brti_price']=83000
        self.assertFalse(rh.force_close_matches(p,10,self.start,self.end,self.now))
        p['brti_price']=82999
        self.assertTrue(rh.force_close_matches(p,10,self.start,self.end,self.now))
        self.assertFalse(rh.force_close_matches(p,10,self.start,self.end,self.start+timedelta(minutes=4)))
        submit,review,page,record=Mock(),Mock(),Mock(),Mock(return_value=True)
        review.inner_text.return_value='Order Summary Yes · $83,000 or above Contracts 2 Limit Price 10¢ Time in Force Good till date'
        with patch.object(rh,'close_review_controls',return_value=[(submit,review)]),patch.object(rh,'observe_close_receipt'):
            self.assertTrue(rh.submit_close_review(page,'BTC',self.start,self.end,p,Mock(),record,lambda *args:deepcopy(p),2,10))
        self.assertEqual(record.call_args_list[0].args[3]['close_order']['time_in_force'],'good_till_date')

    def test_editor_saves_force_close_separately(self):
        import re
        from playwright.sync_api import sync_playwright
        html=app.app.test_client().get('/').get_data(as_text=True)
        source=next(script for script in re.findall(r'<script[^>]*>(.*?)</script>',html,re.S)
                    if 'function syncOrderSettings' in script)
        helpers=source[source.index('let orderSettingsDirty='):source.index('async function saveBalance')]
        sync=source[source.index('function syncOrderSettings'):source.index('function render(s)')]
        configured=order_settings.validate(dict(order_settings.DEFAULTS,
            force_stop=dict(order_settings.FORCE_CLOSE_DEFAULTS,min_price_cents=80),
            force_close=dict(order_settings.FORCE_CLOSE_DEFAULTS,enabled=True,min_price_cents=20)))
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            page=browser.new_page()
            page.set_content(re.sub(r'<script\b[^>]*>.*?</script>','',html,flags=re.S))
            page.add_script_tag(content=helpers+sync)
            page.evaluate('(settings)=>syncOrderSettings({order_settings:settings})',configured)
            self.assertEqual(page.locator('#forceCloseMinPriceInput').input_value(),'20')
            self.assertEqual(page.locator('#forceStopEnabled').count(),0)
            page.evaluate('''()=>{window.fetch=async(url,options)=>{window.savedSettings=JSON.parse(options.body);return {ok:true,json:async()=>({ok:true})}}}''')
            page.evaluate('saveOrderSettings()')
            saved=page.evaluate('window.savedSettings')
            self.assertEqual(saved['force_close']['min_price_cents'],20)
            self.assertTrue(saved['force_close']['enabled'])
            self.assertNotIn('force_stop',saved)
            browser.close()

    def test_legacy_force_stop_migrates_to_single_force_close(self):
        legacy=dict(order_settings.DEFAULTS)
        legacy.pop('force_close')
        legacy['force_stop']=dict(order_settings.FORCE_CLOSE_DEFAULTS,enabled=True,min_price_cents=42)
        checked=order_settings.validate(legacy)
        self.assertTrue(checked['force_close']['enabled'])
        self.assertEqual(checked['force_close']['min_price_cents'],42)
        self.assertNotIn('force_stop',checked)
