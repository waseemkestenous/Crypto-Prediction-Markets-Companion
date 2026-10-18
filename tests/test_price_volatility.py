import json
import tempfile
import unittest
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import app
import price_history


class PriceVolatilityTests(unittest.TestCase):
    def setUp(self):
        self.start=datetime(2026,10,15,12,tzinfo=timezone.utc)
        self.end=self.start+timedelta(minutes=15)

    def points(self, values):
        return [{'observed_at':(self.start+timedelta(seconds=t)).isoformat(),
                 'minute':t/60,'price':price} for t,price in values]

    def test_rolling_range_excludes_older_observations(self):
        points=self.points([(0,90000),(10,80000),(20,80010),(30,79990),(40,80030),(50,80005),(60,80000),(70,80020)])
        v=price_history.volatility_60s(points,self.start+timedelta(seconds=70))
        self.assertEqual(v['status'],'ready')
        self.assertEqual(v['range_dollars'],40)
        self.assertAlmostEqual(v['range_pct'],40/79990*100)
        self.assertEqual(v['samples'],7)
        self.assertEqual(v['coverage_seconds'],60)

    def test_direction_reversals_ignore_flat_prices_and_old_direction(self):
        cases = [([100,101,102,103,104,105,106],0,6,0),
                 ([106,105,104,103,102,101,100],0,0,6),
                 ([100,101,100,101,100,101,100],5,3,3),
                 ([100,101,101,100,100,101,101],2,2,1),
                 ([100]*7,0,0,0)]
        for prices,reversals,up,down in cases:
            with self.subTest(prices=prices):
                points=self.points(list(zip(range(0,61,10),prices)))
                v=price_history.volatility_60s(points,self.start+timedelta(seconds=60))
                self.assertEqual((v['direction_reversals'],v['up_moves'],v['down_moves']), (reversals,up,down))
        points=self.points([(0,110)]+[(t,100+t) for t in range(10,71,10)])
        self.assertEqual(price_history.volatility_60s(points,self.start+timedelta(seconds=70))['direction_reversals'],0)

    def test_warmup_gaps_and_flat_market(self):
        for values in ([(0,80000)],[(0,80000),(10,80010),(20,80020)],
                       [(0,80000),(10,80010),(40,80020),(60,80030)]):
            points=self.points(values)
            v=price_history.volatility_60s(points,datetime.fromisoformat(points[-1]['observed_at']))
            self.assertEqual(v['status'],'insufficient_data')
            self.assertIsNone(v['range_dollars'])
            self.assertIsNone(v['direction_reversals'])
        flat=self.points([(t,80000) for t in range(0,61,10)])
        self.assertEqual(price_history.volatility_60s(flat,self.start+timedelta(seconds=60))['range_dollars'],0)

    def test_records_restores_and_exposes_metric_without_duplicate_source_points(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(price_history,'ROOT',Path(folder)),patch.dict(app.STATE,round_start=self.start,round_end=self.end,underlying_prices=deque(maxlen=2000)):
            for t in range(0,71,10):
                app.record_underlying_price(self.start,self.end,self.start+timedelta(seconds=t),80000+t)
            saved=price_history.load(app.ARGS.asset,self.start,self.end)
            self.assertEqual(saved,list(app.STATE['underlying_prices']))
            self.assertEqual(saved[-1]['volatility_60s']['range_dollars'],60)
            raw=[json.loads(line) for line in price_history.path_for(app.ARGS.asset,self.start).read_text().splitlines()]
            self.assertEqual(raw[-1]['volatility_60s']['range_dollars'],60)
            self.assertEqual(raw[-1]['volatility_60s']['direction_reversals'],0)
            app.record_underlying_price(self.start,self.end,self.start+timedelta(seconds=70),90000)
            self.assertEqual(len(app.STATE['underlying_prices']),8)
            data=app.app.test_client().get('/api/state').get_json()
            self.assertEqual(data['underlying_price_history'][-1]['volatility_60s']['range_dollars'],60)
            app.STATE['underlying_prices']=deque(saved,maxlen=2000)
            app.record_underlying_price(self.start,self.end,self.start+timedelta(seconds=80),80080)
            self.assertEqual(app.STATE['underlying_prices'][-1]['volatility_60s']['range_dollars'],60)

    def test_browser_renders_live_history_and_inspection(self):
        import re
        from playwright.sync_api import sync_playwright
        html=app.app.test_client().get('/').get_data(as_text=True)
        scripts=re.findall(r'<script[^>]*>(.*?)</script>',html,re.S)
        source=next(s for s in scripts if 'function volatilityHistoryChart' in s)
        chart=source[source.index('function renderMarketCharts'):source.index('function roundTimingCell')]
        helpers='\n'.join(line for line in source.splitlines() if line.startswith(('const fmtMoney=', 'const escapeHistory=')))
        points=self.points([(t,80000+t) for t in range(0,71,10)])
        for i,point in enumerate(points):
            point['volatility_60s']=price_history.volatility_60s(points[:i+1],datetime.fromisoformat(point['observed_at']))
        state={'asset':'BTC','target':80000,'underlying_price_history':points,'contract_price_history':[],
               'server_time':(self.start+timedelta(seconds=70)).isoformat()}
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            try:
                page=browser.new_page()
                page.route('http://charts.test/**',lambda route:route.fulfill(body='<html></html>',content_type='text/html'))
                page.goto('http://charts.test/')
                page.set_content('<div id="underlyingChartTitle"></div><div id="bitcoinVolatility"></div><div id="underlyingPriceChart"></div>')
                for script in scripts:
                    page.evaluate('(source)=>{new Function(source);return true}',script)
                page.add_script_tag(content=helpers+'\nfunction applyMarketLineVisibility(){}\n'+chart)
                page.evaluate('(s)=>renderMarketCharts(s)',state)
                self.assertIn('$60.00',page.locator('#bitcoinVolatility').inner_text())
                self.assertIn('Reversals: 0 (↑ 6 / ↓ 0)',page.locator('#bitcoinVolatility').inner_text())
                self.assertEqual(page.get_by_role('img',name='Recorded rolling 60 second direction reversals').count(),1)
                self.assertEqual(page.get_by_role('img',name='Recorded rolling 60 second price range').count(),1)
                page.evaluate("showContractChartPoint(document.querySelector('svg[data-unit=usd]'),7)")
                self.assertIn('60s range $60.00',page.locator('.contract-cursor-price').text_content())
                self.assertIn('Reversals: 0 (↑ 6 / ↓ 0)',page.locator('.contract-cursor-price').text_content())
                for index in (0,7):
                    page.evaluate("(index)=>showContractChartPoint(document.querySelector('svg[data-unit=usd]'),index)",index)
                    bounds=page.locator('.contract-cursor text').evaluate_all('(lines)=>lines.filter(line=>line.textContent).map(line=>{const b=line.getBBox();return {left:b.x,right:b.x+b.width};})')
                    self.assertTrue(all(b['left']>=7 and b['right']<=993 for b in bounds),bounds)
                selected_text=page.locator('.contract-cursor-price').text_content()
                page.locator('svg[data-unit="usd"]').focus()
                for _ in range(3):
                    page.evaluate('(s)=>renderMarketCharts(s)',state)
                    self.assertEqual(page.locator('.contract-cursor').get_attribute('visibility'),'visible')
                    self.assertEqual(page.locator('.contract-cursor-price').text_content(),selected_text)
                    self.assertTrue(page.locator('svg[data-unit="usd"]').evaluate('(svg)=>document.activeElement===svg'))
                # An appended source quote must not move selection to the latest sample.
                extra=self.points([(80,80100)])[0]
                extra['volatility_60s']=price_history.volatility_60s(points+[extra],datetime.fromisoformat(extra['observed_at']))
                state['underlying_price_history']=points+[extra]
                page.evaluate('(s)=>renderMarketCharts(s)',state)
                self.assertEqual(page.locator('.contract-cursor-price').text_content(),selected_text)
                page.locator('svg[data-unit="usd"]').dispatch_event('pointerleave')
                page.evaluate('(s)=>renderMarketCharts(s)',state)
                self.assertEqual(page.locator('.contract-cursor').get_attribute('visibility'),'hidden')
                state['underlying_price_history']=points
                page.evaluate("showContractChartPoint(document.querySelector('svg[data-unit=usd]'),0)")
                self.assertNotIn('Reversals:',page.locator('.contract-cursor-price').text_content())
                state['server_time']=(self.start+timedelta(seconds=100)).isoformat()
                page.evaluate('(s)=>renderMarketCharts(s)',state)
                self.assertIn('Stale observation',page.locator('#bitcoinVolatility').inner_text())
                range_panel=page.locator('[data-volatility-chart="range_dollars"]')
                reversal_panel=page.locator('[data-volatility-chart="direction_reversals"]')
                range_panel.locator('summary').click()
                page.wait_for_function("localStorage.getItem('dashboard.volatility.range_dollars.hidden')==='true'")
                self.assertFalse(range_panel.locator('svg').is_visible())
                self.assertTrue(reversal_panel.locator('svg').is_visible())
                page.evaluate('(s)=>renderMarketCharts(s)',state)
                self.assertFalse(range_panel.locator('svg').is_visible())
                reversal_panel.locator('summary').click()
                page.wait_for_function("localStorage.getItem('dashboard.volatility.direction_reversals.hidden')==='true'")
                # A fresh page session restores both choices from browser storage.
                page.reload()
                page.set_content('<div id="underlyingChartTitle"></div><div id="bitcoinVolatility"></div><div id="underlyingPriceChart"></div>')
                page.add_script_tag(content=helpers+'\nfunction applyMarketLineVisibility(){}\n'+chart)
                page.evaluate('(s)=>renderMarketCharts(s)',state)
                self.assertFalse(range_panel.locator('svg').is_visible())
                self.assertFalse(reversal_panel.locator('svg').is_visible())
                range_panel.locator('summary').click()
                page.wait_for_function("localStorage.getItem('dashboard.volatility.range_dollars.hidden')==='false'")
                self.assertTrue(range_panel.locator('svg').is_visible())
                self.assertFalse(reversal_panel.locator('svg').is_visible())
                state['underlying_price_history']=points[:2]
                page.evaluate('(s)=>renderMarketCharts(s)',state)
                self.assertIn('insufficient samples',page.locator('#bitcoinVolatility').inner_text())
                page.evaluate("showContractChartPoint(document.querySelector('svg[data-unit=usd]'),0)")
                state['round']={'start':(self.start+timedelta(minutes=15)).isoformat()}
                page.evaluate('(s)=>renderMarketCharts(s)',state)
                self.assertEqual(page.locator('.contract-cursor').get_attribute('visibility'),'hidden')
            finally:
                browser.close()
