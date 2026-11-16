import json
import math
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
import numpy as np
import brti_engine as core
import live_learning as learning
import price_history


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.start=datetime(2026,10,1,tzinfo=timezone.utc)
        self.end=self.start+timedelta(minutes=15)

    def points(self,start,up=False):
        return [{'observed_at':(start+timedelta(seconds=s)).isoformat(),'minute':s/60,
                 'price':100000+(1 if up else -1)+.1*math.sin(s/10)} for s in range(0,900,10)]

    def rows(self,count=50,shift=False):
        rows=[]
        for i in range(count):
            start=self.start+timedelta(minutes=15*i);end=start+timedelta(minutes=15)
            up=shift and i>=35
            rows.append({'round_start':start.isoformat(),'round_end':end.isoformat(),
                         'status':'settled','target':100000,'settlement':100001 if up else 99999,
                         'p_up':.6,'prediction':'UP / ABOVE','actual':'UP / ABOVE' if up else 'DOWN / BELOW'})
        return rows

    def train(self,rows):
        learner=learning.LiveLearner()
        def load(asset,start,end):
            up=next(r['actual']=='UP / ABOVE' for r in rows if r['round_start']==start.isoformat())
            return self.points(start,up)
        with patch.object(price_history,'load',side_effect=load):
            learner.train('BTC',self.start+timedelta(minutes=15*50),rows,lambda info,value,stamp:.6)
        return learner

    def test_temporal_holdout_gate_and_distribution_shift(self):
        rows=self.rows();learner=self.train(rows)
        self.assertEqual(learner.info['status'],'active')
        self.assertLess(learner.info['candidate']['brier'],learner.info['baseline']['brier'])
        self.assertLess(learner.info['training_end'],learner.info['validation_start'])
        shifted=self.train(self.rows(shift=True))
        self.assertEqual(shifted.info['status'],'shadow')

    def test_future_current_and_invalid_settlements_never_train(self):
        rows=self.rows()
        future=dict(rows[-1],round_start=(self.start+timedelta(days=20)).isoformat(),round_end=(self.start+timedelta(days=20,minutes=15)).isoformat())
        corrupt=dict(rows[0],settlement=100900)
        calls=[]
        def baseline(info,value,stamp):
            calls.append(info['start']);return .6
        learner=learning.LiveLearner()
        with patch.object(price_history,'load',side_effect=lambda asset,start,end:self.points(start)):
            learner.train('BTC',self.start+timedelta(minutes=15*50),[corrupt,*rows[1:],future],baseline)
        self.assertEqual(learner.info['excluded_rounds'],1)
        self.assertTrue(all(self.start<stamp<self.start+timedelta(minutes=15*50) for stamp in calls))

    def test_features_ignore_future_points_and_require_fresh_complete_windows(self):
        points=self.points(self.start)
        stamp=self.start+timedelta(minutes=5)
        a=learning.features(points,100000,stamp,self.start,self.end,.6)
        b=learning.features([p for p in points if datetime.fromisoformat(p['observed_at'])<=stamp],100000,stamp,self.start,self.end,.6)
        np.testing.assert_array_equal(a[0],b[0])
        self.assertIsNone(learning.features(points[:2],100000,stamp,self.start,self.end,.6))

    def test_apply_shadow_preserves_baseline_active_updates_all_fields(self):
        learner=self.train(self.rows())
        now=self.start+timedelta(minutes=5)
        rec={'p_up':.6,'p_down':.4,'confidence':.6,'strength':core.confidence_strength(.6),
             'current_price':99999,'target_gap':-1,'updated_at':now,'recommendation':'UP / ABOVE',
             'original_prediction':'UP / ABOVE','changed':False,'status':'STILL THE SAME'}
        changed=learner.apply(rec,self.points(self.start),self.start,self.end,core.confidence_strength)
        self.assertTrue(changed['learning']['applied'])
        self.assertLess(changed['p_up'],.5)
        self.assertEqual(changed['recommendation'],'DOWN / BELOW')
        self.assertTrue(changed['changed'])
        self.assertAlmostEqual(changed['confidence'],1-changed['p_up'])
        self.assertEqual(rec['p_up'],.6)
        learner.info['status']='shadow'
        shadow=learner.apply(rec,self.points(self.start),self.start,self.end,core.confidence_strength)
        self.assertEqual(shadow['p_up'],rec['p_up'])
        self.assertFalse(shadow['learning']['applied'])
        learner.info['status']='active'
        stale=learner.apply(rec,self.points(self.start)[:2],self.start,self.end,core.confidence_strength)
        self.assertFalse(stale['learning']['applied'])

    def test_insufficient_data_and_audit_log(self):
        learner=self.train(self.rows(10))
        self.assertEqual(learner.info['status'],'insufficient')
        self.assertIsNone(learner.model)
        with tempfile.TemporaryDirectory() as folder,patch.object(price_history,'ROOT',Path(folder)):
            record={'updated_at':self.start,'p_up':.6,'learning':{'base_p_up':.6,'candidate_p_up':.55,'applied':False}}
            price_history.append_live('BTC',self.start,record)
            saved=json.loads(price_history.path_for('BTC',self.start).with_suffix('.live.jsonl').read_text())
            self.assertEqual(saved['learning'],record['learning'])

    def test_recorded_baselines_use_original_probability_and_exclude_future_or_wrong_target(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(price_history, 'ROOT', Path(folder)):
            stamp = self.start+timedelta(minutes=5)
            row = {'updated_at':stamp, 'p_up':.8, 'current_price':100001, 'target_gap':1,
                   'learning':{'base_p_up':.55}}
            price_history.append_live('BTC', self.start, row)
            price_history.append_live('BTC', self.start, dict(row, updated_at=self.end))
            price_history.append_live('BTC', self.start, dict(row, target_gap=100))
            self.assertEqual(learning.recorded_baselines('BTC', self.start, self.end, 100000), [(stamp,.55)])

    def test_training_prefers_recorded_probabilities_to_replay(self):
        rows = self.rows()
        def recorded(asset, start, end, target):
            return [(start+timedelta(minutes=m), .6) for m in (2,5,8,11,14)]
        with patch.object(learning, 'recorded_baselines', side_effect=recorded), \
                patch.object(price_history, 'load', side_effect=lambda asset,start,end:self.points(start)):
            learner=learning.LiveLearner()
            learner.train('BTC', self.start+timedelta(minutes=15*50), rows,
                          lambda *args: self.fail('Recorded baseline should be used'))
        self.assertEqual(learner.info['recorded_samples'], 250)
        self.assertEqual(learner.info['replayed_samples'], 0)
