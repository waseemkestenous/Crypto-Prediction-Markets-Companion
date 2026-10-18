import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import app
import order_settings as settings
import robinhood_target as rh
from tests import test_fallback_purchase, test_force_buy


class PrimaryEnabledTests(unittest.TestCase):
    def test_defaults_validation_and_disabled_primary_never_touches_browser(self):
        self.assertTrue(settings.validate(settings.DEFAULTS)['primary_enabled'])
        for value in ('false',0,None):
            with self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS,primary_enabled=value))
        page,report=Mock(),Mock()
        prediction={'order_settings':dict(settings.DEFAULTS,primary_enabled=False)}
        self.assertFalse(rh.prepare_contract_form(page,'BTC',None,None,lambda *args:prediction,report))
        report.assert_called_once_with('Primary disabled.')
        self.assertEqual(page.mock_calls,[])

    def test_fallback_and_force_buy_still_work_when_primary_disabled(self):
        with patch.dict(settings.DEFAULTS,primary_enabled=False):
            self.assertTrue(test_fallback_purchase.FallbackTests().form()[0])
            self.assertTrue(test_force_buy.ForceBuyTests().form()[0])

    def test_enabled_state_persists_through_settings_endpoint(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(settings,'PATH',Path(folder)/'settings.json'),patch.dict(app.ORDER_SETTINGS),patch.dict(app.STATE):
            client=app.app.test_client()
            for enabled in (False,True):
                response=client.post('/api/order-settings',json=dict(settings.DEFAULTS,primary_enabled=enabled))
                self.assertEqual(response.status_code,200)
                self.assertEqual(settings.load()['primary_enabled'],enabled)
                self.assertEqual(app.ORDER_SETTINGS['primary_enabled'],enabled)
