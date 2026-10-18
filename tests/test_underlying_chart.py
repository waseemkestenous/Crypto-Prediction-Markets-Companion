import unittest
import tempfile
import threading
from pathlib import Path
import price_history
import round_history
from collections import deque
from datetime import datetime, timedelta
from unittest.mock import patch
import app


class UnderlyingChartTests(unittest.TestCase):
    def test_round_history_api_loads_saved_underlying_points(self):
        start=datetime.now().astimezone()-timedelta(hours=1)
        end=start+timedelta(minutes=15)
        point={'observed_at':(start+timedelta(seconds=3)).isoformat(),'minute':.05,'price':81500.0}
        with tempfile.TemporaryDirectory() as folder,patch.object(price_history,'ROOT',Path(folder)/'prices'),patch.object(round_history,'ROOT',Path(folder)),patch.dict(app.STATS,rounds=[]):
            price_history.append(app.ARGS.asset,start,point)
            round_history.upsert(app.ARGS.asset,start.isoformat(),end.isoformat(),target=81500,status='settled')
            rows=app.app.test_client().get('/api/round-history').get_json()['rounds']
            self.assertEqual(rows[0]['underlying_price_history'],[point])

    def test_worker_restores_current_round_at_startup(self):
        start=datetime.now().astimezone().replace(second=0,microsecond=0)
        end=start+timedelta(minutes=15)
        stopped=threading.Event()
        stopped.set()
        point={'observed_at':(start+timedelta(seconds=3)).isoformat(),'minute':.05,'price':81500.0}
        with tempfile.TemporaryDirectory() as folder,patch.object(price_history,'ROOT',Path(folder)),patch.dict(app.STATE,underlying_prices=deque(maxlen=2000)),patch.object(app,'STOP_EVENT',stopped),patch.object(app,'current_round_key',return_value=(start,end)),patch.object(app,'journal_round'):
            price_history.append(app.ARGS.asset,start,point)
            app.live_worker()
            self.assertEqual(list(app.STATE['underlying_prices']),[point])
            response=app.app.test_client().get('/api/state').get_json()
            self.assertEqual(response['underlying_price_history'],[point])

    def test_state_api_delivers_full_round_points(self):
        start=datetime.now().astimezone()
        end=start+timedelta(minutes=15)
        with tempfile.TemporaryDirectory() as folder,patch.object(price_history,'ROOT',Path(folder)),patch.dict(app.STATE,round_start=start,round_end=end,underlying_prices=deque(maxlen=2000)):
            for seconds in range(0,900):
                app.record_underlying_price(start,end,start+timedelta(seconds=seconds),81000+seconds)
            result=app.app.test_client().get('/api/state').get_json()
            self.assertEqual(len(result['underlying_price_history']),900)
            self.assertEqual(result['underlying_price_history'][0]['minute'],0)
            self.assertGreater(result['underlying_price_history'][-1]['minute'],14.9)
            self.assertIn('available_balance',result)
            restored=price_history.load(app.ARGS.asset,start,end)
            self.assertEqual(restored,result['underlying_price_history'])
            self.assertEqual(price_history.load('ETH' if app.ARGS.asset!='ETH' else 'BTC',start,end),[])
            self.assertEqual(price_history.load(app.ARGS.asset,end,end+timedelta(minutes=15)),[])
