import unittest
from datetime import datetime
from unittest.mock import Mock
from playwright.sync_api import sync_playwright
import robinhood_target as rh


class MarketListFallbackTests(unittest.TestCase):
    def test_verified_event_is_opened_and_other_rounds_are_rejected(self):
        start=datetime.fromisoformat('2026-11-16T00:15:00-05:00')
        end=datetime.fromisoformat('2026-11-16T00:30:00-05:00')
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='chrome',headless=True)
            context=browser.new_context()
            hits=[]
            mode=['valid']
            def serve(route):
                path=route.request.url
                hits.append(path)
                if '/prediction-markets?' in path:
                    body='''<section><h2>BTC 15 min · 12:00–12:15 AM EST</h2><a href="/events/expired">Open</a></section>
                    <section><a href="/events/eth">ETH 15 min</a></section>
                    <a href="https://example.com/events/wrong">BTC 15 min</a>'''
                    if mode[0]=='valid':
                        body+='''<section><h2>BTC 15 min · 12:15–12:30 AM EST</h2><a href="/events/current">Open</a></section>'''
                elif '/events/current' in path:
                    body='<h1>BTC 15 min · 12:15–12:30 AM EST</h1><p>$64,500.00 or above Event day November 16, 2026</p>'
                elif '/events/expired' in path:
                    body='<h1>BTC 15 min · 12:00–12:15 AM EST</h1><p>$64,400.00 or above Event day November 16, 2026</p>'
                else:
                    body='<h1>Original page</h1>'
                route.fulfill(body=body,content_type='text/html; charset=utf-8')
            context.route('**/*',serve)
            page=context.new_page()
            page.goto('https://robinhood.com/events/original')
            stop=Mock();stop.is_set.return_value=False
            report=Mock()
            self.assertTrue(rh.open_event_from_market_list(page,stop,report,'BTC',start,end),(report.call_args_list,hits))
            self.assertEqual(page.url,'https://robinhood.com/events/current')
            self.assertEqual(len(context.pages),1)
            self.assertEqual(rh.read_event_target(page,'BTC',start,end),64500)
            self.assertNotIn('https://robinhood.com/events/eth',hits)
            self.assertNotIn('https://example.com/events/wrong',hits)
            page.goto('https://robinhood.com/events/original')
            mode[0]='expired-only'
            self.assertFalse(rh.open_event_from_market_list(page,stop,report,'BTC',start,end))
            self.assertEqual(page.url,'https://robinhood.com/events/original')
            self.assertEqual(len(context.pages),1)
            browser.close()
