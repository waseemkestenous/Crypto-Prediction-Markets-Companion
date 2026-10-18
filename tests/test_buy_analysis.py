import unittest
from datetime import datetime, timedelta, timezone

import buy_analysis as analysis


class BuyAnalysisTests(unittest.TestCase):
    def rows(self, count=10):
        start=datetime(2026,1,1,tzinfo=timezone.utc)
        rows=[]
        for i in range(count):
            stamp=start+timedelta(minutes=15*i)
            rows.append({'round_start':stamp.isoformat(),'round_end':(stamp+timedelta(minutes=15)).isoformat(),
                         'target':83000,'prediction':'UP / ABOVE','actual':'UP / ABOVE',
                         'component_results':{},'contract_price_history':[
                             {'observed_at':(stamp+timedelta(minutes=j)).isoformat(),
                              'target':83000,'yes_cents':30+j,'no_cents':71-j}
                             for j in range(15)]})
        return rows

    def test_training_selection_does_not_use_validation_outcomes(self):
        rows=self.rows()
        result=analysis.analyze(rows)
        self.assertEqual(result['training_rounds'],7)
        self.assertEqual(result['validation_rounds'],3)
        for row in rows[7:]:
            row['actual']='DOWN / BELOW'
        changed=analysis.analyze(rows)
        self.assertEqual(result['recommendation'],changed['recommendation'])
        self.assertGreater(result['validation']['net_pnl'],0)
        self.assertLess(changed['validation']['net_pnl'],0)
        self.assertFalse(changed['supported'])

    def test_first_eligible_quote_one_entry_and_settlement_fee(self):
        row=analysis.prepare_round(self.rows(1)[0])
        config={'required_models':[],'purchase_minute_from':5,'purchase_minute_to':10,
                'min_price_cents':30,'max_price_cents':60,'contract_trend_changes':3}
        result=analysis.evaluate([row],config)
        self.assertEqual(result['trades'],1)
        self.assertEqual(result['entries'][0]['minute'],5)
        self.assertAlmostEqual(result['net_pnl'],1-.35-.0194)

    def test_missing_quotes_pending_and_wrong_target_excluded(self):
        rows=self.rows(3)
        rows[0]['contract_price_history']=[]
        rows[1]['actual']=None
        for point in rows[2]['contract_price_history']:
            point['target']=1
        result=analysis.analyze(rows)
        self.assertEqual(result['completed_rounds'],2)
        self.assertEqual(result['quote_rounds'],0)
        self.assertIsNone(result['recommendation'])

    def test_no_entry_before_trend_observed(self):
        row=analysis.prepare_round(self.rows(1)[0])
        config={'required_models':[],'purchase_minute_from':0,'purchase_minute_to':3,
                'min_price_cents':0,'max_price_cents':100,'contract_trend_changes':3}
        self.assertEqual(analysis.evaluate([row],config)['trades'],0)
