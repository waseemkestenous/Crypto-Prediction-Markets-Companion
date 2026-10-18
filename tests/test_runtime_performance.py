"""Regression checks for bounded work, cache invalidation, and durable messages."""
import gzip
import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
import app
import order_logs
import price_history
import round_history as journal


class RuntimePerformanceTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name)
        self.start=datetime.now(timezone.utc)-timedelta(minutes=30)
        self.end=self.start+timedelta(minutes=15)
        for context in (patch.object(journal,'ROOT',self.root),patch.object(price_history,'ROOT',self.root/'prices'),
                        patch.object(order_logs,'PATH',self.root/'logs.sqlite3'),patch.dict(app.STATS,rounds=[]),patch.object(app.ARGS,'asset','BTC')):
            context.start();self.addCleanup(context.stop)

    def save(self,**fields):
        return journal.upsert('BTC',self.start.isoformat(),self.end.isoformat(),**fields)

    def test_cached_reads_are_isolated_and_external_replacements_are_detected(self):
        self.save(purchase={'contracts':1},contract_price_history=[{'yes_cents':50}])
        with patch.object(Path,'read_text',side_effect=AssertionError('Unexpected reread')):
            row=journal.get('BTC',self.start.isoformat())
            row['purchase']['contracts']=99
            self.assertEqual(journal.get('BTC',self.start.isoformat())['purchase']['contracts'],1)
            self.assertNotIn('contract_price_history',journal.get('BTC',self.start.isoformat(),include_quotes=False))
            self.assertEqual(journal.load('BTC')[0]['contract_price_history'][0]['yes_cents'],50)
        replacement=self.root/'replacement.json'
        row=journal.load('BTC')[0];row['purchase']['contracts']=3
        replacement.write_text(json.dumps([row]));replacement.replace(journal.path_for('BTC'))
        self.assertEqual(journal.get('BTC',self.start.isoformat())['purchase']['contracts'],3)

    def test_noop_write_skips_disk_and_failed_write_keeps_previous_snapshot(self):
        self.save(target=83000)
        path=journal.path_for('BTC');before=path.stat().st_mtime_ns
        self.save(target=83000)
        self.assertEqual(before,path.stat().st_mtime_ns)
        with patch.object(Path,'replace',side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):self.save(target=84000)
        self.assertEqual(journal.get('BTC',self.start.isoformat())['target'],83000)
        self.assertEqual(json.loads(path.read_text())[0]['target'],83000)
        self.assertEqual(list(self.root.glob('*.tmp')),[])

    def test_price_cache_preserves_validation_and_invalidates_after_append(self):
        point={'observed_at':self.start.isoformat(),'price':83000}
        price_history.append('BTC',self.start,point)
        first=price_history.load('BTC',self.start,self.end)
        first[0]['price']=1
        with patch.object(Path,'read_text',side_effect=AssertionError('Unexpected reread')):
            self.assertEqual(price_history.load('BTC',self.start,self.end)[0]['price'],83000)
        price_history.append('BTC',self.start,{'observed_at':(self.start+timedelta(seconds=1)).isoformat(),'price':83001})
        self.assertEqual(len(price_history.load('BTC',self.start,self.end)),2)

    def test_log_archive_retains_all_messages_and_serves_bounded_descending_pages(self):
        ids=[order_logs.append(self.start.isoformat(),f'Update {i}') for i in range(450)]
        first=order_logs.page();self.assertEqual(len(first['logs']),200)
        self.assertTrue(first['has_more']);self.assertEqual(first['logs'][0]['id'],ids[-1])
        second=order_logs.page(before=first['logs'][-1]['id'])
        third=order_logs.page(before=second['logs'][-1]['id'])
        self.assertEqual(len(second['logs']),200);self.assertEqual(len(third['logs']),50)
        self.assertFalse(third['has_more'])
        found=[entry['id'] for page in (first,second,third) for entry in page['logs']]
        self.assertEqual(found,list(reversed(ids)))
        client=app.app.test_client()
        self.assertEqual(len(client.get('/api/order-status-logs').get_json()['logs']),200)
        self.assertEqual(client.get('/api/order-status-logs?before=bad').status_code,400)

    def test_state_payload_does_not_grow_with_log_archive(self):
        with patch.dict(app.STATE),patch.object(app,'stats_json',return_value={}),patch.object(app,'current_contract_price_history',return_value=[]):
            for i in range(550):app.robinhood_report('Waiting '+str(i))
            self.assertEqual(len(app.STATE['order_status_logs']),500)
            payload=app.app.test_client().get('/api/state').get_json()
            self.assertNotIn('order_status_logs',payload)
            self.assertGreater(payload['order_status_log_id'],0)

    def test_gzip_is_negotiated_and_preserves_api_data(self):
        self.save(contract_price_history=[{'target':83000,'yes_cents':51,'no_cents':49}]*1500)
        client=app.app.test_client()
        compressed=client.get('/api/round-history',headers={'Accept-Encoding':'gzip'})
        self.assertEqual(compressed.headers['Content-Encoding'],'gzip')
        decoded=json.loads(gzip.decompress(compressed.data))
        self.assertEqual(len(decoded['rounds'][0]['contract_price_history']),1500)
        plain=client.get('/api/round-history',headers={'Accept-Encoding':'gzip;q=0'})
        self.assertNotIn('Content-Encoding',plain.headers)
        self.assertEqual(plain.get_json()['rounds'][0]['contract_price_history'],decoded['rounds'][0]['contract_price_history'])

    def test_sidebar_pagination_poll_guard_and_unchanged_chart(self):
        import re
        from urllib.parse import parse_qs,urlparse
        from playwright.sync_api import sync_playwright
        html=app.app.test_client().get('/').get_data(as_text=True)
        source=next(script for script in re.findall(r'<script[^>]*>(.*?)</script>',html,re.S)
                    if 'function loadOrderLogs' in script)
        messages=[{'id':i+1,'timestamp':self.start.isoformat(),'message':f'Waiting {i}'} for i in range(450)]
        def respond(route):
            query=parse_qs(urlparse(route.request.url).query)
            before=int(query['before'][0]) if 'before' in query else None
            rows=list(reversed([entry for entry in messages if before is None or entry['id']<before]))
            route.fulfill(json={'logs':rows[:200],'has_more':len(rows)>200})
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            page=browser.new_page(viewport={'width':1280,'height':800})
            page.route('**/api/order-status-logs*',respond)
            page.set_content(re.sub(r'<script\b[^>]*>.*?</script>','',html,flags=re.S))
            # Relative fetches need an origin, without loading any external surface.
            page.evaluate("()=>{const nativeFetch=window.fetch;window.fetch=(url,options)=>nativeFetch(new URL(url,'http://dashboard.test').href,options);}")
            page.add_script_tag(content=source[source.index('function orderStatusKind'):source.index('function syncOrderSettings')])
            page.get_by_role('button',name='More',exact=True).click()
            page.wait_for_function("document.querySelectorAll('#orderStatusLogs .order-message').length===200")
            self.assertIn('Waiting 449',page.locator('#orderStatusLogs .order-message').first.inner_text())
            page.get_by_role('button',name='Older logs',exact=True).click()
            page.wait_for_function("document.querySelector('#orderStatusLogs .order-message').textContent.includes('Waiting 249')")
            self.assertEqual(page.locator('#orderStatusLogs .order-message').count(),200)
            page.get_by_role('button',name='Older logs',exact=True).click()
            page.wait_for_function("document.querySelectorAll('#orderStatusLogs .order-message').length===50")
            self.assertTrue(page.get_by_role('button',name='Older logs',exact=True).is_disabled())
            page.get_by_role('button',name='Latest',exact=True).click()
            page.wait_for_function("document.querySelector('#orderStatusLogs .order-message').textContent.includes('Waiting 449')")
            page.locator('#orderLogsDialog').evaluate('(e)=>e.close()')
            poll_source=source[source.index('let statePollBusy=false;'):source.index('setInterval(poll,1000)')]
            page.add_script_tag(content='function render(){window.renderCalls=(window.renderCalls||0)+1;}'+poll_source)
            page.evaluate('()=>{window.fetch=()=>{window.requestCount=(window.requestCount||0)+1;return new Promise(resolve=>window.releaseFetch=resolve);};window.firstPoll=poll();window.secondPoll=poll();}')
            self.assertEqual(page.evaluate('window.requestCount'),1)
            page.evaluate('async()=>{window.releaseFetch({json:async()=>({})});await window.firstPoll;}')
            self.assertEqual(page.evaluate('window.renderCalls'),1)
            page.evaluate('()=>{window.thirdPoll=poll();}')
            self.assertEqual(page.evaluate('window.requestCount'),2)
            page.evaluate('async()=>{window.releaseFetch({json:async()=>({})});await window.thirdPoll;}')
            # A timestamp-only dashboard refresh must preserve the existing chart DOM.
            chart_source=source[source.index('function renderMarketCharts'):source.index('function volatilityChartIsHidden')]
            page.add_script_tag(content='''function escapeHistory(v){return String(v)};function fmtMoney(v){return '$'+Number(v).toFixed(2)};
function applyMarketLineVisibility(){};function underlyingPriceChart(){window.chartBuilds=(window.chartBuilds||0)+1;return '<svg data-unit="usd"><g class="contract-cursor" visibility="hidden"></g></svg>';}
'''+chart_source)
            data={'asset':'BTC','target':83000,'server_time':self.start.isoformat(),
                  'underlying_price_history':[{'price':83001,'observed_at':self.start.isoformat(),'minute':0}],
                  'contract_price_history':[]}
            page.evaluate('(s)=>renderMarketCharts(s)',data)
            page.evaluate("window.originalChart=document.querySelector('#underlyingPriceChart svg')")
            data['server_time']=(self.start+timedelta(seconds=1)).isoformat()
            page.evaluate('(s)=>renderMarketCharts(s)',data)
            self.assertEqual(page.evaluate('window.chartBuilds'),1)
            self.assertTrue(page.evaluate("window.originalChart===document.querySelector('#underlyingPriceChart svg')"))
            history_source=source[source.index('function updateRoundHistoryRows'):source.index('function changeRoundHistoryPage')]
            page.add_script_tag(content="""let roundHistoryBusy=false,roundHistoryPage=1,roundHistoryAsset=null;
function renderTrackedAccuracy(){};function historyDecision(){return ''};function purchaseLiveCell(){return ''};function settlementHistoryCell(){return ''};function purchaseHistoryCell(){return ''};
function roundTimingCell(){window.timingBuilds=(window.timingBuilds||0)+1;return 'Timing';}
function purchaseDetailsRows(){window.detailBuilds=(window.detailBuilds||0)+1;return [];}
"""+history_source)
            history={'asset':'BTC','server_time':self.end.isoformat(),'page':1,'pages':1,'total':1,'stats':{},
                     'rounds':[{'round_start':self.start.isoformat(),'round_end':self.end.isoformat(),'target':83000,'status':'settled'}]}
            page.evaluate('(data)=>{window.historyData=data;window.fetch=async()=>({ok:true,json:async()=>structuredClone(window.historyData)});}',history)
            page.evaluate('loadRoundHistory()')
            self.assertEqual(page.evaluate('window.timingBuilds'),1)
            page.evaluate("window.savedTimingRow=document.querySelectorAll('#roundHistoryBody tr')[1]")
            page.evaluate('loadRoundHistory()')
            self.assertEqual(page.evaluate('window.timingBuilds'),1)
            self.assertEqual(page.evaluate('window.detailBuilds'),1)
            self.assertTrue(page.evaluate("window.savedTimingRow===document.querySelectorAll('#roundHistoryBody tr')[1]"))
            page.evaluate('window.historyData.rounds[0].target=83001')
            page.evaluate('loadRoundHistory()')
            self.assertEqual(page.evaluate('window.timingBuilds'),2)
            browser.close()
