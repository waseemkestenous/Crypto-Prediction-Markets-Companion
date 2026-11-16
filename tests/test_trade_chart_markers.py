import unittest
from playwright.sync_api import sync_playwright
import app


class TradeChartMarkerTests(unittest.TestCase):
    def test_filled_cycles_and_close_markers(self):
        html = app.app.test_client().get('/').get_data(as_text=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            page.set_content(html)
            result = page.evaluate('''()=>{
              const trade={status:'filled',direction:'DOWN / BELOW',test_mode:true,
                submitted_at:'2026-11-08T00:02:00Z',execution_price_cents:55,
                close_order:{status:'filled',filled_at:'2026-11-08T00:04:00Z',execution_price_cents:70}};
              const row={round_start:'2026-11-08T00:00:00Z',completed_purchases:[trade],purchase:trade};
              const events=chartTradeEvents(row);
              document.getElementById('underlyingPriceChart').innerHTML=underlyingPriceChart([],81000,[],[],row);
              const labels=[...document.querySelectorAll('.trade-chart-marker')].map(n=>n.getAttribute('aria-label'));
              const pending=chartTradeEvents({...row,completed_purchases:[],purchase:{...trade,close_order:{status:'submitted',submitted_at:'2026-11-08T00:04:00Z'}}});
              const unfilled=chartTradeEvents({...row,completed_purchases:[],purchase:{...trade,status:'attempted'}});
              return {events,labels,pending,unfilled};
            }''')
            self.assertEqual([e['kind'] for e in result['events']], ['Buy', 'Close'])
            self.assertEqual([e['minute'] for e in result['events']], [2, 4])
            self.assertEqual(len(result['labels']), 2)
            self.assertIn('TEST', result['labels'][0])
            self.assertIn('execution 70¢', result['labels'][1])
            self.assertEqual([e['kind'] for e in result['pending']], ['Buy'])
            self.assertEqual(result['unfilled'], [])
            settlement = page.evaluate("""()=>{
              const purchase={status:'filled',direction:'UP / ABOVE',submitted_at:'2026-11-08T00:02:00Z'};
              const row={round_start:'2026-11-08T00:00:00Z',round_end:'2026-11-08T00:15:00Z',actual:'UP / ABOVE',prediction:'DOWN / BELOW',p_up:.4,purchase};
              document.getElementById('underlyingPriceChart').innerHTML=underlyingPriceChart([],81000,[],[],row);
              const close=document.querySelector('.trade-chart-marker[data-kind="Close"]');
              const initial=document.querySelector('.initial-prediction-marker');
              const events=chartTradeEvents(row);
              const pending=chartTradeEvents({...row,actual:null});
              const explicit=chartTradeEvents({...row,purchase:{...purchase,close_order:{status:'filled',filled_at:'2026-11-08T00:10:00Z',execution_price_cents:60}}});
              return {events,pending,explicit,close:close.getAttribute('aria-label'),initial:initial.getAttribute('aria-label'),x:close.querySelector('line').getAttribute('x1')};
            }""")
            self.assertEqual(settlement['events'][-1]['minute'], 15)
            self.assertEqual(settlement['events'][-1]['price'], 100)
            self.assertIn('Settlement', settlement['close'])
            self.assertIn('payout 100¢', settlement['close'])
            self.assertIn('DOWN / BELOW', settlement['initial'])
            self.assertIn('UP 40.00%', settlement['initial'])
            self.assertEqual(float(settlement['x']), 932)
            self.assertEqual(len(settlement['pending']), 1)
            self.assertEqual([e['minute'] for e in settlement['explicit']], [2, 10])
            browser.close()
