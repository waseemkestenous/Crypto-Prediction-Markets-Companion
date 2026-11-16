import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import price_history


class LiveChartChangesTests(unittest.TestCase):
    def test_only_transitions_are_loaded_and_cache_updates(self):
        start = datetime(2026, 11, 7, tzinfo=timezone.utc)
        end = start + timedelta(minutes=15)
        with tempfile.TemporaryDirectory() as folder, patch.object(price_history, 'ROOT', Path(folder)):
            for minute, direction in [(1, 'UP / ABOVE'), (2, 'UP / ABOVE'), (3, 'DOWN / BELOW')]:
                price_history.append_live('BTC', start, {'updated_at': start+timedelta(minutes=minute), 'recommendation': direction})
            changes = price_history.live_changes('BTC', start, end)
            self.assertEqual([p['minute'] for p in changes], [3])
            self.assertEqual(changes[0]['previous_direction'], 'UP / ABOVE')
            price_history.append_live('BTC', start, {'updated_at': start+timedelta(minutes=4), 'recommendation': 'UP / ABOVE'})
            with price_history.path_for('BTC', start).with_suffix('.live.jsonl').open('a') as stream:
                stream.write('{truncated')
            self.assertEqual([p['minute'] for p in price_history.live_changes('BTC', start, end)], [3, 4])
            self.assertEqual(price_history.live_changes('ETH', start, end), [])
