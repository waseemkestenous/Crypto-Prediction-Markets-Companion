import unittest
import tempfile
import json
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import prospective_study as study

class ResearchTest(unittest.TestCase):
    def test_future_excluded_and_snapshot_persisted(self):
        start=datetime(2026,11,6,tzinfo=timezone.utc)
        now=start+timedelta(seconds=90)
        points=[{'observed_at':(start+timedelta(seconds=s)).isoformat(),'price':p} for s,p in [(0,99),(30,101),(60,102),(120,999)]]
        row=study.snapshot(start,start+timedelta(minutes=15),now,100,points,None,{}, {},{})
        self.assertEqual(row['btc_price'],102)
        self.assertEqual(row['target_delta_dollars'],2)
        self.assertEqual(row['observed_same_side_seconds'],60)
        self.assertEqual(row['btc_quote_age_seconds'],30)
        with tempfile.TemporaryDirectory() as tmp,patch.object(study,'ROOT',Path(tmp)):
            study.append('BTC',start,row)
            saved=json.loads(next(Path(tmp).rglob('*.jsonl')).read_text())
            self.assertEqual(saved['btc_price'],102)
    def test_missing_quotes_not_zero_distance(self):
        start=datetime(2026,11,6,tzinfo=timezone.utc)
        row=study.snapshot(start,start+timedelta(minutes=15),start,100,[],None,{}, {},{})
        self.assertIsNone(row['target_delta_dollars'])
        self.assertIsNone(row['normalized_target_distance'])
