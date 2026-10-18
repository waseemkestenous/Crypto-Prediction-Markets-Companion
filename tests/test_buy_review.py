import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from copy import deepcopy
from playwright.sync_api import sync_playwright
import robinhood_target as rh
import order_settings as settings


class BuyReviewTests(unittest.TestCase):
    def test_review_layout_submits_and_rejects_mismatches(self):
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='chrome',headless=True)
            page=browser.new_page()
            for side,direction,ask in [('Yes','UP / ABOVE',39),('No','DOWN / BELOW',60)]:
                start=datetime.now().astimezone()-timedelta(minutes=1)
                end=start+timedelta(minutes=15)
                config=settings.validate(dict(settings.DEFAULTS,time_in_force='good_till_date',max_price_cents=80))
                pred={'target':83000,'brti_price':83012.5 if side=='Yes' else 82990,'direction':direction,'model_directions':dict.fromkeys(('Final prediction',*settings.MODEL_NAMES),direction),
                      'live_snapshot':{'direction':direction,'confidence':.7},'order_settings':config,
                      'contract_prices':{'yes_cents':39,'no_cents':60},
                      'contract_price_history':[{'target':83000,'observed_at':(start+timedelta(seconds=i)).isoformat(),
                           'yes_cents':round(ask-.1+i*.01,4) if side=='Yes' else round(101-ask+.1-i*.01,4),
                           'no_cents':round(ask-.1+i*.01,4) if side=='No' else round(101-ask+.1-i*.01,4)} for i in range(11)]}
                page.set_content(f'''<section id="form"><div>$83,000 or above 39¢</div><button>Buy {side}</button>
                    <button role="combobox" aria-label="Buy In">Contracts</button>
                    <label for="qty">Contracts</label><input id="qty">
                    <label for="limit">Limit Price</label><input id="limit" value="{ask+1}">
                    <button role="combobox" aria-label="Time in force">Good till date</button>
                    <button onclick="document.getElementById('form').hidden=true;document.getElementById('review').hidden=false">Review order</button></section>
                    <section id="review" hidden><h3>{side} · $83,000 or above</h3><div>Buy {side}</div>
                    <div>Contracts 1</div><div>Limit Price Bid {ask-1}¢ · Ask {ask}¢ {ask+1}¢</div>
                    <div>Time in Force Good till date</div><div>Order Summary</div>
                    <button onclick="document.body.dataset.bought='yes'">Buy</button><button>Edit</button></section>''')
                records=[]
                def record(asset,start,end,update):
                    records.append(update)
                    pred['purchase']=dict(pred.get('purchase',{}),**update)
                    pred['purchase_exists']=True
                    return True
                with patch.object(rh,'read_event_target',return_value=83000):
                    self.assertTrue(rh.prepare_contract_form(page,'BTC',start,end,lambda *args:deepcopy(pred),lambda message:None,record))
                self.assertEqual(page.locator('body').get_attribute('data-bought'),'yes')
                self.assertEqual(records[-1]['status'],'submitted')
                self.assertEqual(records[0]['btc_price'], pred['brti_price'])
                self.assertEqual(records[0]['btc_target_diff_dollars'], 12.5 if side=='Yes' else -10)
                self.assertFalse(rh.review_buy_details(page,pred,direction,2,ask+1,'good_till_date'))
                self.assertFalse(rh.review_buy_details(page,pred,direction,1,ask+2,'good_till_date'))
                self.assertFalse(rh.review_buy_details(page,pred,direction,1,ask+1,'ioc'))
            browser.close()
