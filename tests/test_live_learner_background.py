import threading
import unittest
from datetime import datetime
from unittest.mock import patch
import app
import live_learning


class BackgroundLearnerTests(unittest.TestCase):
    def test_training_does_not_block_baseline_and_runs_once(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def train(learner, *args):
            entered.set()
            release.wait(5)
            learner.info = {'status': 'shadow'}
            finished.set()
        with patch.object(app, 'LIVE_LEARNER', live_learning.LiveLearner()), \
                patch.object(app.round_history, 'load', return_value=[]), \
                patch.object(live_learning.LiveLearner, 'train', train):
            start = datetime.now().astimezone()
            try:
                learner = app.ensure_live_learner('BTC', start, None)
                self.assertTrue(entered.wait(1))
                self.assertFalse(finished.is_set())
                self.assertEqual(learner.info['status'], 'training')
                self.assertIsNone(learner.model)
                self.assertIs(app.ensure_live_learner('BTC', start, None), learner)
            finally:
                release.set()
                self.assertTrue(finished.wait(2))
                # Synchronize with the final atomic publication.
                for thread in threading.enumerate():
                    if thread.name.endswith('(train)'):
                        thread.join(2)
            self.assertEqual(app.LIVE_LEARNER.info['status'], 'shadow')
