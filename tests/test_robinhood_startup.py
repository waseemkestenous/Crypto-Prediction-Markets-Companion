import json
import unittest
from pathlib import Path
from unittest.mock import patch
import robinhood_target as rh


class StartupEventTests(unittest.TestCase):
    def test_explicit_starting_event_takes_priority_until_next_verified_target(self):
        url='https://robinhood.com/events/new-user-supplied'
        saved={'asset':'BTC','source_url':'https://robinhood.com/events/expired', 'startup_source_url':url}
        with patch.object(Path,'read_text',return_value=json.dumps(saved)):
            self.assertEqual(rh.startup_event_url(),url)

    def test_resume_last_verified_event(self):
        url='https://robinhood.com/events/recent-verified?source=expired_settled_banner'
        with patch.object(Path,'read_text',return_value=json.dumps({'asset':'BTC','source_url':url})):
            self.assertEqual(rh.startup_event_url(),url)

    def test_invalid_saved_url_or_asset_uses_seed(self):
        for asset,url in [('BTC','https://example.com/events/x'),('ETH','https://robinhood.com/events/x'),('BTC','http://robinhood.com/events/x'),('BTC','https://robinhood.com/events/'),('BTC','https://user@robinhood.com/events/x')]:
            with self.subTest(asset=asset,url=url),patch.object(Path,'read_text',return_value=json.dumps({'asset':asset,'source_url':url})):
                self.assertEqual(rh.startup_event_url(),rh.EVENT_URL)
        with patch.object(Path,'read_text',return_value='invalid json'):
            self.assertEqual(rh.startup_event_url(),rh.EVENT_URL)
