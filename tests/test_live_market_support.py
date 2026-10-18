import unittest
from datetime import datetime, timedelta, timezone
import live_market_support as support
import brti_engine as core


class MarketSupportTests(unittest.TestCase):
    def setUp(self):
        self.start=datetime(2026,10,16,tzinfo=timezone.utc)
        self.end=self.start+timedelta(minutes=15)
        self.now=self.start+timedelta(seconds=120)
        self.points=[{'observed_at':(self.start+timedelta(seconds=i)).isoformat(),
                      'price':83000+i*2} for i in range(60,121,5)]
        self.quotes=[{'observed_at':p['observed_at'],'target':83000,'yes_cents':50+i*1.25}
                     for i,p in enumerate(self.points)]
        self.rec={'updated_at':self.now,'p_up':.6,'p_down':.4,'confidence':.6,
                  'current_price':83240,'target_gap':240,'recommendation':'UP / ABOVE',
                  'strength':core.confidence_strength(.6)}

    def apply(self, **kwargs):
        return support.apply(kwargs.get('rec',self.rec),kwargs.get('points',self.points),
                             kwargs.get('quotes',self.quotes),self.start,self.end,core.confidence_strength)

    def test_alignment_is_bounded_and_does_not_mutate_baseline(self):
        result=self.apply()
        self.assertGreater(result['p_up'],.6)
        self.assertLessEqual(result['p_up'],.62)
        self.assertEqual(self.rec['p_up'],.6)
        self.assertAlmostEqual(result['market_support']['dollars_per_cent'],8)
        self.assertEqual(result['recommendation'],self.rec['recommendation'])

    def test_opposition_reduces_confidence_without_flipping(self):
        rec=dict(self.rec,p_up=.49,confidence=.51,recommendation='DOWN / BELOW')
        result=self.apply(rec=rec)
        self.assertGreater(result['p_up'],.49)
        self.assertLess(result['p_up'],.5)
        self.assertEqual(result['recommendation'],'DOWN / BELOW')

    def test_invalid_windows_do_not_change_probability(self):
        cases=[{'quotes':[]},{'quotes':self.quotes[:5]},
               {'quotes':[dict(q,target=84000) for q in self.quotes]},
               {'quotes':[dict(q,yes_cents=50) for q in self.quotes]},
               {'quotes':[dict(q,yes_cents=100-q['yes_cents']) for q in self.quotes]},
               {'points':self.points[:5]},
               {'quotes':[dict(q,observed_at=(self.now+timedelta(seconds=5)).isoformat()) for q in self.quotes]}]
        for case in cases:
            with self.subTest(case=case):
                result=self.apply(**case)
                self.assertEqual(result['p_up'],.6)
                self.assertFalse(result['market_support']['applied'])

    def test_confident_baseline_is_not_reduced_by_alignment(self):
        result=self.apply(rec=dict(self.rec,p_up=.99,confidence=.99))
        self.assertGreaterEqual(result['p_up'],.99)
