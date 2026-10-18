import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import brti_engine as core


class BrtiFreshnessTests(unittest.TestCase):
    def test_html_source_check_every_two_seconds(self):
        stamp=datetime.now(timezone.utc)
        source=core.BenchmarkSource(asset='BTC')
        response=Mock(text=self.html(int(stamp.timestamp()*1000)))
        with patch.object(core,'read_live_brti',side_effect=RuntimeError('no browser')),patch.object(core.SESSION,'get',return_value=response) as get,patch.object(core.time,'monotonic',side_effect=[100,101,102]):
            source._brti_quote()
            source._brti_quote()
            self.assertEqual(get.call_count,1)
            source._brti_quote()
            self.assertEqual(get.call_count,2)

    def html(self,stamp):
        page={'props':{'pageProps':{'externalIndexId':'BRTI','indexSummary':{'value':'81570.54','lastUpdated':stamp}}}}
        return '<script id="__NEXT_DATA__">'+json.dumps(page)+'</script>'

    def test_source_timestamp_is_preserved(self):
        stamp=datetime.now(timezone.utc)-timedelta(seconds=3)
        response=Mock(text=self.html(int(stamp.timestamp()*1000)))
        source=core.BenchmarkSource(asset='BTC')
        with patch.object(core,'read_live_brti',side_effect=RuntimeError('no browser')),patch.object(core.SESSION,'get',return_value=response) as get:
            self.assertEqual(source.latest(),81570.54)
            self.assertAlmostEqual(source.last_quote_at.timestamp(),stamp.timestamp(),places=2)
            self.assertGreater(source.last_fetched_at,source.last_quote_at)
            self.assertEqual(get.call_args.args[0],core.CF_BRTI_URL)

    def test_old_future_and_missing_timestamps_do_not_become_fresh_quotes(self):
        now=datetime.now(timezone.utc)
        for stamp in (int((now-timedelta(seconds=25)).timestamp()*1000),int((now+timedelta(seconds=60)).timestamp()*1000),None):
            source=core.BenchmarkSource(asset='BTC')
            with patch.object(core,'read_live_brti',side_effect=RuntimeError('no browser')),patch.object(core.SESSION,'get',return_value=Mock(text=self.html(stamp))):
                with self.assertRaises(RuntimeError):
                    source.latest()
                self.assertEqual(source.last_quotes,{})
                self.assertIn('CF Benchmarks BRTI',source.last_errors)
