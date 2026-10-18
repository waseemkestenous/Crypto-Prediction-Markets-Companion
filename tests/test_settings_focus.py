"""Polling must preserve settings controls while the user edits them."""
import re
import unittest
from playwright.sync_api import sync_playwright
import app
import order_settings


class SettingsFocusTests(unittest.TestCase):
    def test_polling_preserves_focus_and_unsaved_values(self):
        html = app.app.test_client().get('/').get_data(as_text=True)
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
        source = next(script for script in scripts if 'function syncOrderSettings' in script)
        helpers = source[source.index('let orderSettingsDirty='):source.index('async function saveOrderSettings')]
        sync = source[source.index('function syncOrderSettings'):source.index('function render(s)')]
        settings = order_settings.validate(dict(order_settings.DEFAULTS, fallbacks=[dict(
            order_settings.FALLBACK_DEFAULTS, enabled=True)]))
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            page.set_content(re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S))
            for script in scripts:
                page.evaluate('(source)=>new Function(source)', script)
            page.add_script_tag(content=helpers + sync)
            page.evaluate('(settings)=>syncOrderSettings({order_settings:settings})', settings)
            page.locator('#orderSettingsPanel').evaluate('(panel)=>{panel.open=true;}')
            self.assertEqual(page.locator('.fallback-details[open]').count(), 0)
            page.locator('#fallbackRules .fallback-details').first.evaluate('(panel)=>{panel.open=true;}')
            field = page.locator('#fallbackRules [data-field="min_distance_dollars"]')
            field.evaluate('(input)=>{window.originalInput=input;input.focus();}')
            # Poll before the first keystroke, including a changed server setting.
            changed = dict(settings, contracts=5, close_conditions=dict(settings['close_conditions'],max_brti_delta_dollars=-30))
            for _ in range(3):
                page.evaluate('(settings)=>syncOrderSettings({order_settings:settings})', changed)
            self.assertTrue(field.evaluate('(input)=>input===window.originalInput&&document.activeElement===input'))
            self.assertEqual(page.locator('#contractsInput').input_value(), '1')
            field.fill('-30')
            field.blur()
            page.evaluate('(settings)=>syncOrderSettings({order_settings:settings})', changed)
            self.assertEqual(field.input_value(), '-30')
            # A clean form receives changed settings after focus leaves the panel.
            page.evaluate('orderSettingsDirty=false')
            page.evaluate('(settings)=>syncOrderSettings({order_settings:settings})', changed)
            self.assertEqual(page.locator('#contractsInput').input_value(), '5')
            self.assertEqual(page.locator('#closeBrtiDeltaInput, #stopLossEnabled, #closeEnabled').count(), 0)
            field.evaluate('(input)=>window.originalInput=input')
            page.evaluate('(settings)=>syncOrderSettings({order_settings:settings})', changed)
            self.assertTrue(field.evaluate('(input)=>input===window.originalInput'))
            browser.close()
