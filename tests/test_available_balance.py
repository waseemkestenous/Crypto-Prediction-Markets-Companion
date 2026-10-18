import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
import app
import robinhood_target as rh


class AvailableBalanceTests(unittest.TestCase):
    def test_reads_only_dollars_labelled_available(self):
        self.assertEqual(rh.parse_available_balance('Contracts 1 available Est. Cost $0.42 $100.98 available'),100.98)
        self.assertEqual(rh.parse_available_balance('$1,234.56\navailable'),1234.56)
        self.assertEqual(rh.parse_available_balance('$0.00 available'),0)
        self.assertEqual(rh.parse_available_balance('$101.03available'),101.03)
        for text in ('Contracts 1 available Est. Cost $0.42','$10 available $20 available','No balance'):
            self.assertIsNone(rh.parse_available_balance(text))

    def test_balance_expires_and_does_not_fall_back_to_calculation(self):
        now=datetime.now().astimezone()
        with patch.dict(app.STATE,available_balance=None),patch.object(app,'now_local',return_value=now):
            self.assertIsNone(app.current_available_balance())
            app.record_available_balance(100.98)
            self.assertEqual(app.current_available_balance()['amount'],100.98)
            app.record_available_balance(None)
            self.assertEqual(app.current_available_balance()['amount'],100.98)
            with patch.object(app,'now_local',return_value=now+timedelta(seconds=31)):
                self.assertIsNone(app.current_available_balance())
