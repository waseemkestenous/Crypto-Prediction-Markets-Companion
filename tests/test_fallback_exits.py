import unittest
from datetime import datetime, timedelta
from copy import deepcopy
import order_settings as settings
import robinhood_target as rh


class FallbackExitTests(unittest.TestCase):
    def test_migration_and_validation(self):
        configured = settings.validate(dict(settings.DEFAULTS, fallbacks=[{}, {}],
            close_conditions=dict(settings.CLOSE_DEFAULTS, enabled=True)))
        self.assertTrue(configured['fallbacks'][0]['close_conditions']['enabled'])
        configured['fallbacks'][0]['stop_loss']['drop_cents'] = 5
        self.assertEqual(configured['fallbacks'][1]['stop_loss']['drop_cents'], 20)
        for key, value in [('stop_loss', {'drop_cents': -1}),
                           ('close_conditions', {'time_in_force': 'bad'})]:
            with self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS, fallbacks=[{key: value}]))

    def test_purchase_exits_survive_reordering_and_removal(self):
        configured = settings.validate(dict(settings.DEFAULTS, fallbacks=[{
            'close_conditions': dict(settings.CLOSE_DEFAULTS, enabled=True),
            'stop_loss': dict(settings.STOP_LOSS_DEFAULTS, drop_cents=5)}]))
        captured = deepcopy(configured['fallbacks'][0])
        start = datetime.now().astimezone()
        prediction = {'target': 83000, 'order_settings': settings.validate(settings.DEFAULTS),
            'purchase': {'status': 'filled', 'target': 83000, 'direction': 'UP / ABOVE',
                'stop_loss_peak_bid_cents': 95, 'purchase_rule': 'fallback','contracts':1,'execution_price_cents':50,
                'conditions': {'rule': 'fallback', 'fallback': captured}}}
        self.assertTrue(rh.close_conditions_match(prediction, 90, start,
            start+timedelta(minutes=15), start+timedelta(minutes=6)))
        self.assertTrue(rh.stop_loss_matches(prediction, 90, start,
            start+timedelta(minutes=15), start+timedelta(minutes=6)))
        prediction['purchase']['conditions']['fallback']['stop_loss']['enabled'] = False
        self.assertFalse(rh.stop_loss_matches(prediction, 90, start,
            start+timedelta(minutes=15), start+timedelta(minutes=6)))

    def test_editor_preserves_independent_exits_when_moving_rules(self):
        import re
        import app
        from playwright.sync_api import sync_playwright
        html = app.app.test_client().get('/').get_data(as_text=True)
        source = next(script for script in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
                      if 'function readFallbackRules' in script)
        helpers = source[source.index('let orderSettingsDirty='):source.index('async function saveOrderSettings')]
        rules = settings.validate(dict(settings.DEFAULTS, fallbacks=[
            {'stop_loss': {'drop_cents': 5}}, {'stop_loss': {'drop_cents': 12}}]))['fallbacks']
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            page.set_content(re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S))
            page.add_script_tag(content=helpers)
            page.evaluate('(rules)=>renderFallbackRules(rules)', rules)
            self.assertEqual(page.locator('#fallbackRules .fallback-rule').count(), 2)
            self.assertEqual(page.locator('#closeEnabled, #stopLossEnabled').count(), 0)
            self.assertEqual(page.locator('#fallbackRules [data-exit=close_conditions][data-key=enabled]').count(), 2)
            self.assertEqual(page.locator('#fallbackRules [data-exit=stop_loss][data-key=enabled]').count(), 2)
            page.evaluate('moveFallbackRule(0,1)')
            actual = page.evaluate('readFallbackRules()')
            self.assertEqual([rule['stop_loss']['drop_cents'] for rule in actual], [12, 5])
            self.assertEqual(settings.validate(dict(settings.DEFAULTS, fallbacks=actual))['fallbacks'],
                             [rules[1], rules[0]])
            self.assertEqual(page.locator('#orderSettingsPanel #forceCloseEnabled').count(), 1)
            self.assertEqual(page.locator('#orderSettingsPanel #saveOrderSettingsBtn').count(), 1)
            browser.close()
