import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
import app
import order_settings as settings
import robinhood_target as rh

class PurchaseWindowTests(unittest.TestCase):
    def test_invalid_ranges(self):
        for first,last in ((-1,15),(0,16),(5,5),(10,2),(True,15),(0,None),(float('nan'),15)):
            with self.subTest(first=first,last=last),self.assertRaises(ValueError):
                settings.validate(dict(settings.DEFAULTS,purchase_minute_from=first,purchase_minute_to=last))

    def test_boundaries_and_round_end(self):
        start=datetime.now().astimezone();end=start+timedelta(minutes=15)
        config=dict(settings.DEFAULTS,purchase_minute_from=5,purchase_minute_to=12)
        for minutes,allowed in ((-1,False),(4.99,False),(5,True),(11.99,True),(12,False),(15,False)):
            with patch.object(rh,'datetime') as clock:
                clock.now.return_value=start+timedelta(minutes=minutes)
                self.assertEqual(rh.in_purchase_window(config,start,end),allowed)
        with patch.object(rh,'datetime') as clock:
            clock.now.return_value=end
            self.assertFalse(rh.in_purchase_window(settings.DEFAULTS,start,end))

    def test_outside_window_never_touches_browser(self):
        p={'direction':'UP / ABOVE','model_directions':dict.fromkeys(('Final prediction',*settings.MODEL_NAMES),'UP / ABOVE'),'live_snapshot':{'direction':'UP / ABOVE'},'order_settings':dict(settings.DEFAULTS,purchase_minute_from=10)}
        now=datetime.now().astimezone();page=Mock()
        self.assertFalse(rh.prepare_contract_form(page,'BTC',now-timedelta(minutes=1),now+timedelta(minutes=14),lambda *args:p,Mock()))
        self.assertEqual(page.mock_calls,[])

    def test_expired_before_buy_prevents_submission(self):
        from test_order_settings import SettingsTests
        with patch.object(rh,'in_purchase_window',side_effect=[True]*5+[False]):
            record=Mock(return_value=True)
            self.assertFalse(SettingsTests().form(30,60,1,record)[0])
            record.assert_not_called()

    def test_window_persists(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(settings,'PATH',Path(folder)/'settings.json'),patch.dict(app.ORDER_SETTINGS):
            response=app.app.test_client().post('/api/order-settings',json=dict(settings.DEFAULTS,purchase_minute_from=5,purchase_minute_to=12))
            self.assertEqual(response.status_code,200)
            self.assertEqual(settings.load()['purchase_minute_from'],5)
            self.assertEqual(settings.load()['purchase_minute_to'],12)
