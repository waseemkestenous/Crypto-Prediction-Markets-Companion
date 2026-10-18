import re
import unittest
from playwright.sync_api import sync_playwright
import app


class ContractSensitivityTests(unittest.TestCase):
    def test_rolling_dollars_per_cent_and_data_quality(self):
        html=app.app.test_client().get('/').get_data(as_text=True)
        source=next(s for s in re.findall(r'<script[^>]*>(.*?)</script>',html,re.S)
                    if 'function bitcoinDollarsPerCent60s' in s)
        helper=source[source.index('function bitcoinDollarsPerCent60s'):source.index('function underlyingPriceChart')]
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            page=browser.new_page()
            page.add_script_tag(content=helper)
            points=[{'minute':sec/60,'price':83000+sec*2} for sec in range(0,121,5)]
            quotes=[{'minute':sec/60,'yes_cents':50+sec/4} for sec in range(0,121,5)]
            run=lambda ps,qs:page.evaluate('([p,q])=>bitcoinDollarsPerCent60s(p,q)',[ps,qs])
            result=run(points,quotes)
            self.assertIsNone(result[0])
            self.assertAlmostEqual(result[-1]['dollars'],8)
            self.assertAlmostEqual(result[-1]['r2'],1)
            # Future observations cannot alter an earlier estimate.
            self.assertEqual(result[12],run(points,quotes+[{'minute':3,'yes_cents':0}])[12])
            self.assertIsNone(run(points,[dict(q,yes_cents=50) for q in quotes])[-1])
            self.assertIsNone(run(points,[dict(q,yes_cents=100-q['yes_cents']) for q in quotes])[-1])
            self.assertIsNone(run(points,quotes[:5])[-1])
            # Compare changing rolling windows against a direct least-squares reference.
            import math
            noisy_points=[{'minute':sec/60,'price':83000+sec*1.7+math.sin(sec/8)} for sec in range(0,301,5)]
            noisy_quotes=[{'minute':sec/60,'yes_cents':35+sec*.09+math.sin(sec/11)*.2} for sec in range(0,301,5)]
            actual=run(noisy_points,noisy_quotes)
            for index in range(12,len(noisy_points)):
                t=noisy_points[index]['minute']*60
                pairs=[(q['yes_cents'],p['price']) for p,q in zip(noisy_points,noisy_quotes) if t-60<=p['minute']*60<=t]
                mx=sum(x for x,y in pairs)/len(pairs);my=sum(y for x,y in pairs)/len(pairs)
                xx=sum((x-mx)**2 for x,y in pairs);yy=sum((y-my)**2 for x,y in pairs)
                xy=sum((x-mx)*(y-my) for x,y in pairs)
                self.assertIsNotNone(actual[index])
                self.assertAlmostEqual(actual[index]['dollars'],xy/xx,places=7)
                self.assertAlmostEqual(actual[index]['r2'],xy*xy/(xx*yy),places=7)
            browser.close()
