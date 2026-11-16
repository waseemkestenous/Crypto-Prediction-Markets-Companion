import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import entry_stability as guard
import order_settings
import robinhood_target as rh


class EntryStabilityTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 11, 9, tzinfo=timezone.utc)
        self.end = self.start + timedelta(minutes=15)
        self.now = self.start + timedelta(seconds=90)

    def points(self, seconds, price=101):
        return [{'observed_at': (self.start+timedelta(seconds=s)).isoformat(), 'price': price} for s in seconds]

    def measure(self, points, now=None):
        return guard.measure(points, self.start, self.end, now or self.now, 100)

    def prediction(self, points):
        return {'target':100, 'brti_price':101, 'direction':'UP / ABOVE', 'live_direction':'UP / ABOVE',
                'model_directions':dict.fromkeys(('Final prediction',*order_settings.MODEL_NAMES),'UP / ABOVE'),
                'entry_stability':self.measure(points), 'live_snapshot':{'direction':'UP / ABOVE','confidence':.7},
                'order_settings':dict(order_settings.DEFAULTS,entry_stability={'enabled':True,'seconds':60})}

    def test_threshold_and_freshness(self):
        p=self.prediction(self.points(range(30,91,5)))
        self.assertTrue(guard.allows(p,'UP / ABOVE',self.now))
        self.assertFalse(guard.allows(p,'DOWN / BELOW',self.now))
        self.assertFalse(guard.allows(p,'UP / ABOVE',self.now+timedelta(seconds=11)))
        p=self.prediction(self.points(range(35,91,5)))
        self.assertFalse(guard.allows(p,'UP / ABOVE',self.now))
        p['order_settings']['entry_stability']['enabled']=False
        self.assertTrue(guard.allows(p,'UP / ABOVE',self.now))

    def test_touch_cross_and_gaps_reset(self):
        points=self.points(range(0,91,5))
        for price in (100,99):
            changed=[dict(p) for p in points];changed[-3]['price']=price
            self.assertEqual(self.measure(changed)['seconds'],5)
        self.assertEqual(self.measure(self.points([0,5,10,80,85,90]))['seconds'],10)
        self.assertIsNone(self.measure(self.points(range(0,71,5)))['direction'])
        self.assertEqual(self.measure(points+self.points([95],99))['seconds'],90) # future ignored
        self.assertEqual(self.measure(points, self.end)['seconds'],0)

    def test_down_and_missing_or_wrong_target(self):
        points=self.points(range(30,91,5),99)
        p=self.prediction(points);p['brti_price']=99
        self.assertTrue(guard.allows(p,'DOWN / BELOW',self.now))
        p['target']=102
        self.assertFalse(guard.allows(p,'DOWN / BELOW',self.now))
        p.pop('entry_stability')
        self.assertFalse(guard.allows(p,'DOWN / BELOW',self.now))

    def test_draft_recheck_blocks_reset_but_accepts_growing_timer(self):
        p=self.prediction(self.points(range(30,91,5)))
        with patch.object(rh,'datetime') as clock:
            clock.now.return_value=self.now
            current=dict(p,entry_stability=dict(p['entry_stability'],seconds=65))
            self.assertTrue(rh.buy_prediction_matches(current,p))
            current['entry_stability']['seconds']=5
            self.assertFalse(rh.buy_prediction_matches(current,p))
            rule={'min_distance_dollars':None,'_allow_missing_brti':True}
            self.assertFalse(rh.buy_prediction_matches(current,p,rule))
            self.assertTrue(rh.buy_prediction_matches(current,p,rule,force_buy=True))

    def test_primary_and_fallback_block_before_browser_interaction(self):
        p=self.prediction(self.points(range(35,91,5)))
        p['force_buy_request']={'token':'requested'}
        p['order_settings']['force_buy']=dict(order_settings.FORCE_BUY_DEFAULTS,enabled=True)
        p['order_settings']['fallback']=dict(order_settings.FALLBACK_DEFAULTS,enabled=True,min_distance_dollars=None)
        now=datetime.now().astimezone();start=now-timedelta(minutes=2);end=start+timedelta(minutes=15)
        for flags in ({},{'fallback':True}):
            page=Mock()
            self.assertFalse(rh.prepare_contract_form(page,'BTC',start,end,lambda *a:p,Mock(),**flags))
            self.assertEqual(page.mock_calls,[])

    def test_settings_validation(self):
        self.assertEqual(order_settings.validate(order_settings.DEFAULTS)['entry_stability'], {'enabled':False,'seconds':60})
        for value in (0,-1,901,True,float('nan'),'invalid'):
            with self.assertRaises(ValueError):
                order_settings.validate(dict(order_settings.DEFAULTS,entry_stability={'enabled':True,'seconds':value}))
        with self.assertRaises(ValueError):
            order_settings.validate(dict(order_settings.DEFAULTS,entry_stability={'enabled':'yes','seconds':60}))

    def test_settings_endpoint_and_browser_round_trip(self):
        import tempfile
        from pathlib import Path
        import app
        from playwright.sync_api import sync_playwright
        settings = order_settings.validate(dict(order_settings.DEFAULTS, entry_stability={'enabled':True,'seconds':60}))
        with tempfile.TemporaryDirectory() as folder, patch.object(order_settings,'PATH',Path(folder)/'settings.json'), patch.dict(app.ORDER_SETTINGS):
            client=app.app.test_client()
            response=client.post('/api/order-settings',json=settings)
            self.assertEqual(response.status_code,200)
            self.assertEqual(order_settings.load()['entry_stability'],{'enabled':True,'seconds':60})
            html=client.get('/').get_data(as_text=True)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            page=browser.new_page()
            page.set_content(html)
            page.evaluate('s=>syncOrderSettings({order_settings:s})',settings)
            self.assertTrue(page.locator('#entryStabilityEnabled').is_checked())
            self.assertEqual(page.locator('#entryStabilitySeconds').input_value(),'60')
            page.evaluate("()=>{window.fetch=async(url,options)=>{if(options?.body)window.savedSettings=JSON.parse(options.body);return {ok:true,json:async()=>({ok:true})}}}")
            page.evaluate('saveOrderSettings()')
            self.assertEqual(page.evaluate('window.savedSettings.entry_stability'),{'enabled':True,'seconds':60})
            browser.close()
