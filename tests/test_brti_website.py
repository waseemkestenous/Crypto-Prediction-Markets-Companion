import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import brti_website as website
import brti_engine as core


class BrtiWebsiteTests(unittest.TestCase):
    def test_rendered_price_is_separate_from_percent_and_chart(self):
        text='CME CF Bitcoin Real Time Index\nBRTI\n$81,636.960.16%\nLast updated: Thu, 15 Oct 2026 00:11:31 GMT\nValue $100,000.00'
        result=website.parse_rendered_summary(text)
        self.assertEqual(result['value'],81636.96)
        self.assertEqual(result['source_time'],datetime(2026,10,15,0,11,31,tzinfo=timezone.utc))
        with self.assertRaises(RuntimeError):
            website.parse_rendered_summary('Reference Rate BRR $81,636.96')

    def test_browser_data_preferred_over_html(self):
        now=datetime.now(timezone.utc)
        snapshot={'value':81636.96,'source_time':now,'fetched_at':now}
        with patch.object(core,'read_live_brti',return_value=snapshot),patch.object(core.SESSION,'get') as get:
            source=core.BenchmarkSource(asset='BTC')
            self.assertEqual(source.latest(),81636.96)
            self.assertEqual(source.last_transport,'Live rendered website')
            get.assert_not_called()

    def test_cached_browser_snapshot_must_still_be_fresh(self):
        reader=website.LiveWebsiteReader()
        reader.thread=Mock()
        reader.ready.set()
        reader.snapshot={'value':81636.96,'source_time':datetime.now(timezone.utc)-timedelta(seconds=30)}
        with self.assertRaisesRegex(RuntimeError,'quote age'):
            reader.read()

    def test_older_browser_and_html_cannot_replace_newer_pair(self):
        now=datetime.now(timezone.utc)
        source=core.BenchmarkSource(asset='BTC')
        source._accept_brti_snapshot(81000,now,now,'Live rendered website')
        for transport in ('Live rendered website','HTML snapshot'):
            self.assertEqual(source._accept_brti_snapshot(80000,now-timedelta(seconds=30),now,transport),81000)
            self.assertEqual(source.last_quote_at,now)
            self.assertEqual(source.last_brti_snapshot['value'],81000)
        # Equal timestamps cannot silently change the price either.
        self.assertEqual(source._accept_brti_snapshot(79000,now,now,'HTML snapshot'),81000)

    def test_rejected_old_update_does_not_renew_an_expired_pair(self):
        now=datetime.now(timezone.utc)
        source=core.BenchmarkSource(asset='BTC')
        source._accept_brti_snapshot(81000,now,now,'Live rendered website')
        with patch.object(core,'datetime') as clock:
            clock.now.return_value=now+timedelta(seconds=21)
            with self.assertRaises(RuntimeError):
                source._accept_brti_snapshot(80000,now-timedelta(seconds=30),now,'HTML snapshot')
        self.assertEqual(source.last_quote_at,now)
        self.assertEqual(source.last_quote_value,81000)

    def test_twenty_second_boundary(self):
        stamp=datetime.now(timezone.utc)
        for age,allowed in ((15,True),(20,True),(20.01,False)):
            source=core.BenchmarkSource(asset='BTC')
            with patch.object(core,'datetime') as clock:
                clock.now.return_value=stamp+timedelta(seconds=age)
                if allowed:
                    self.assertEqual(source._accept_brti_snapshot(81000,stamp,clock.now.return_value,'website'),81000)
                else:
                    with self.assertRaises(RuntimeError):
                        source._accept_brti_snapshot(81000,stamp,clock.now.return_value,'website')
