import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright
import app
import order_settings


class PrimaryExitTests(unittest.TestCase):
    def test_primary_exits_save_independently_of_fallbacks(self):
        html=app.app.test_client().get('/').get_data(as_text=True)
        source=next(s for s in re.findall(r'<script[^>]*>(.*?)</script>',html,re.S)
                    if 'function syncOrderSettings' in s)
        helpers=source[source.index('let orderSettingsDirty='):source.index('async function saveBalance')]
        sync=source[source.index('function syncOrderSettings'):source.index('function render(s)')]
        configured=order_settings.validate(dict(order_settings.DEFAULTS,
            close_conditions=dict(order_settings.CLOSE_DEFAULTS,enabled=True),
            fallbacks=[{'stop_loss':{'drop_cents':12}}]))
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            page=browser.new_page(viewport={'width':1400,'height':1000})
            page.set_content(re.sub(r'<script\b[^>]*>.*?</script>','',html,flags=re.S))
            page.add_script_tag(content=helpers+sync)
            page.evaluate('(settings)=>syncOrderSettings({order_settings:settings})',configured)
            self.assertFalse(page.locator('.primary-details').evaluate('(el)=>el.open'))
            page.locator('#orderSettingsPanel').evaluate('(el)=>el.open=true')
            page.locator('.primary-details>summary').click()
            primary=page.locator('#primaryExitRules')
            primary.locator('[data-exit=stop_loss][data-key=drop_cents]').fill('30')
            primary.locator('[data-exit=close_conditions][data-key=profit_per_ct_cents]').fill('20')
            primary.locator('[data-exit=close_conditions][data-key=ct_price_above_cents]').fill('75')
            self.assertEqual(primary.locator('[data-exit=close_conditions][data-key=min_price_cents]').count(),0)
            primary.locator('[data-exit=close_conditions][data-key=time_in_force]').select_option('good_till_date')
            page.evaluate('''()=>{window.fetch=async(url,options)=>{window.saved=JSON.parse(options.body);return {ok:true,json:async()=>({ok:true})}}}''')
            page.evaluate('saveOrderSettings()')
            saved=page.evaluate('window.saved')
            self.assertEqual(saved['stop_loss']['drop_cents'],30)
            self.assertEqual(saved['close_conditions']['profit_per_ct_cents'],20)
            self.assertEqual(saved['close_conditions']['ct_price_above_cents'],75)
            self.assertNotIn('max_price_cents',saved['close_conditions'])
            self.assertEqual(saved['close_conditions']['time_in_force'],'good_till_date')
            self.assertTrue(saved['close_conditions']['enabled'])
            self.assertEqual(saved['fallbacks'][0]['stop_loss']['drop_cents'],12)
            with tempfile.TemporaryDirectory() as folder,patch.object(order_settings,'PATH',Path(folder)/'settings.json'),patch.dict(app.ORDER_SETTINGS):
                response=app.app.test_client().post('/api/order-settings',json=saved)
                self.assertEqual(response.status_code,200)
                loaded=order_settings.load()
                self.assertEqual(loaded['stop_loss']['drop_cents'],30)
                self.assertEqual(loaded['close_conditions']['ct_price_above_cents'],75)
                self.assertEqual(loaded['fallbacks'][0]['stop_loss']['drop_cents'],12)
            browser.close()
