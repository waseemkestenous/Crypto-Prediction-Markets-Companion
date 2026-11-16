import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import brti_engine as core
import frozen_learning as learning


class FrozenLearningTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.rows = []
        for i in range(50):
            start = self.start+timedelta(minutes=15*i)
            self.rows.append({'asset':'BTC', 'round_start':start.isoformat(),
                'round_end':(start+timedelta(minutes=15)).isoformat(), 'status':'settled',
                'p_up':.6, 'target':100, 'settlement':99, 'actual':'DOWN / BELOW',
                'component_results':{label:{'p_up':.6} for _,label in learning.COMPONENTS}})
        self.prediction = dict(start=self.start+timedelta(minutes=15*50), p_up=.6, p_down=.4,
            prediction='UP / ABOVE', confidence=.6, strength=core.confidence_strength(.6),
            component_agreement=4, **{key:.6 for key,_ in learning.COMPONENTS})

    def apply(self, rows):
        return learning.apply(self.prediction, rows, 'BTC', core.confidence_strength)

    def test_holdout_activation_updates_fields_without_mutating_input(self):
        before = deepcopy(self.prediction)
        result = self.apply(self.rows)
        info = result['frozen_learning']
        self.assertEqual(info['status'], 'active')
        self.assertTrue(info['applied'])
        self.assertLess(info['candidate']['brier'], info['baseline']['brier'])
        self.assertLess(info['training_end'], info['validation_start'])
        self.assertEqual(result['prediction'], 'DOWN / BELOW')
        self.assertAlmostEqual(result['p_up']+result['p_down'], 1)
        self.assertEqual(result['component_agreement'], 0)
        self.assertEqual(self.prediction, before)

    def test_distribution_shift_stays_shadow(self):
        rows = deepcopy(self.rows)
        for row in rows[35:]:
            row.update(settlement=101, actual='UP / ABOVE')
        result = self.apply(rows)
        self.assertEqual(result['frozen_learning']['status'], 'shadow')
        self.assertFalse(result['frozen_learning']['applied'])
        self.assertEqual(result['p_up'], .6)

    def test_future_wrong_asset_invalid_and_duplicates_do_not_inflate_training(self):
        future = deepcopy(self.rows[-1])
        future.update(round_end=(self.prediction['start']+timedelta(minutes=15)).isoformat())
        invalid = deepcopy(self.rows[0]); invalid['settlement'] = 101
        wrong = deepcopy(self.rows[1]); wrong['asset'] = 'ETH'
        result = self.apply([*self.rows[:10], *self.rows[:10], future, invalid, wrong])
        self.assertEqual(result['frozen_learning']['usable_rounds'], 10)
        self.assertEqual(result['frozen_learning']['status'], 'insufficient')
        self.assertEqual(result['p_up'], .6)

    def test_original_probability_prevents_recursive_calibration(self):
        rows = deepcopy(self.rows)
        for row in rows:
            row['p_up'] = .01
            row['frozen_learning'] = {'base_p_up':.6}
        self.assertEqual(self.apply(rows)['p_up'], self.apply(self.rows)['p_up'])
