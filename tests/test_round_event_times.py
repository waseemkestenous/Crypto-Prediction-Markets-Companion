import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch
import app
import round_history as journal

class RoundEventTimeTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.start=datetime.now().astimezone();self.end=self.start+timedelta(minutes=15)
        for context in (patch.object(journal,'ROOT',Path(self.folder.name)),patch.object(app.ARGS,'asset','BTC'),patch.object(app,'current_round_key',return_value=(self.start,self.end)),patch.dict(app.STATE,round_start=self.start,round_end=self.end,target=83000,contract_prices=None),patch.dict(app.STATS,rounds=[])):
            context.start();self.addCleanup(context.stop)

    def row(self):
        return journal.load('BTC')[0]

    def test_only_direction_switch_updates_last_change(self):
        def recommend(direction,seconds):
            app.record_live_recommendation({'recommendation':direction,'updated_at':self.start+timedelta(seconds=seconds),'changed':True})
        recommend('UP / ABOVE',1)
        self.assertNotIn('last_live_change_at',self.row())
        recommend('UP / ABOVE',2)
        self.assertNotIn('last_live_change_at',self.row())
        recommend('DOWN / BELOW',3)
        self.assertEqual(self.row()['last_live_change_at'],app.dt_iso(self.start+timedelta(seconds=3)))
        recommend('DOWN / BELOW',4)
        self.assertEqual(self.row()['last_live_change_at'],app.dt_iso(self.start+timedelta(seconds=3)))
        recommend('UP / ABOVE',5)
        self.assertEqual(self.row()['last_live_change_at'],app.dt_iso(self.start+timedelta(seconds=5)))
        recommend('DOWN / BELOW',900)
        self.assertEqual(self.row()['last_live_direction'],'UP / ABOVE')
        self.assertEqual(self.row()['last_live_change_minute'],0.08)

    def test_last_above_threshold_observation_is_saved_per_side(self):
        def quote(yes,no,seconds):
            with patch.object(app,'now_local',return_value=self.start+timedelta(seconds=seconds)):
                app.record_contract_prices('BTC',self.start,self.end,83000,{'yes_cents':yes,'no_cents':no})
        quote(51,49,1);quote(60,40,2);quote(50,50,3);quote(49,51,4)
        row=self.row()
        self.assertEqual(row['last_up_above_50_minute'],0.03)
        self.assertEqual(row['last_down_above_50_minute'],0.07)
        self.assertEqual(row['last_up_above_50_at'],app.dt_iso(self.start+timedelta(seconds=2)))
        self.assertEqual(row['last_down_above_50_at'],app.dt_iso(self.start+timedelta(seconds=4)))
        quote(95,6,900)
        self.assertEqual(self.row()['last_up_above_50_at'],row['last_up_above_50_at'])
        result=app.app.test_client().get('/api/round-history').get_json()['rounds'][0]
        self.assertEqual(result['last_down_above_50_at'],row['last_down_above_50_at'])

    def test_round_change_does_not_carry_timestamps(self):
        app.record_live_recommendation({'recommendation':'UP / ABOVE','updated_at':self.start})
        app.record_live_recommendation({'recommendation':'DOWN / BELOW','updated_at':self.start+timedelta(seconds=1)})
        with patch.dict(app.STATE,round_start=self.end,round_end=self.end+timedelta(minutes=15)):
            app.record_live_recommendation({'recommendation':'UP / ABOVE','updated_at':self.end})
        self.assertNotIn('last_live_change_at',journal.load('BTC')[1])

    def test_existing_timestamps_gain_persisted_minute_numbers(self):
        journal.upsert('BTC',app.dt_iso(self.start),app.dt_iso(self.end),last_live_change_at=app.dt_iso(self.start+timedelta(minutes=7.5)),last_up_above_50_at=app.dt_iso(self.start),last_down_above_50_at=app.dt_iso(self.end))
        journal.merged('BTC',[])
        row=self.row()
        self.assertEqual(row['last_live_change_minute'],7.5)
        self.assertEqual(row['last_up_above_50_minute'],0)
        self.assertEqual(row['last_down_above_50_minute'],15)

    def test_since_start_values_frozen_at_last_direction_change(self):
        def recommend(direction,seconds,dollars,percent):
            app.record_live_recommendation({'recommendation':direction,'updated_at':self.start+timedelta(seconds=seconds),'change_dollars':dollars,'change_pct':percent})
        recommend('UP / ABOVE',0,1,.001)
        recommend('DOWN / BELOW',60,-2.38,-.0028)
        recommend('DOWN / BELOW',120,-5,-.006)
        row=self.row()
        self.assertEqual(row['last_live_change_dollars_at_change'],-2.38)
        self.assertEqual(row['last_live_change_pct_at_change'],-.0028)
        result=app.app.test_client().get('/api/round-history').get_json()['rounds'][0]
        self.assertEqual(result['last_live_change_dollars_at_change'],-2.38)
        recommend('UP / ABOVE',180,3,.004)
        self.assertEqual(self.row()['last_live_change_dollars_at_change'],3)
        self.assertEqual(self.row()['last_live_change_pct_at_change'],.004)

    def test_first_above_50_minute_is_not_overwritten(self):
        def quote(yes,no,minutes):
            with patch.object(app,'now_local',return_value=self.start+timedelta(minutes=minutes)):
                app.record_contract_prices('BTC',self.start,self.end,83000,{'yes_cents':yes,'no_cents':no})
        quote(50,50,1)
        self.assertNotIn('first_up_above_50_at', self.row())
        self.assertNotIn('first_down_above_50_at', self.row())
        self.assertEqual(self.row()['contract_price_history'][0]['yes_cents'], 50)
        quote(51,49,2.5)
        quote(90,11,3)
        quote(19,82,4)
        quote(10,91,5)
        quote(85,16,6)
        row=self.row()
        self.assertEqual(row['first_up_above_50_minute'],2.5)
        self.assertEqual(row['first_down_above_50_minute'],4)
        self.assertEqual(row['last_up_above_50_minute'],6)
        self.assertEqual(row['last_down_above_50_minute'],5)
        self.assertEqual(row['first_up_above_50_at'],app.dt_iso(self.start+timedelta(minutes=2.5)))

    def test_historical_50_cent_times_are_rebuilt_from_saved_quotes(self):
        start=self.start-timedelta(minutes=30);end=start+timedelta(minutes=15)
        samples=[{'observed_at':app.dt_iso(start+timedelta(minutes=m)), 'target':83000,
                  'yes_cents':yes,'no_cents':no} for m,yes,no in [(1,50,50),(2,55,45),(4,60,40),(6,49,51)]]
        journal.upsert('BTC',app.dt_iso(start),app.dt_iso(end),target=83000,contract_price_history=samples)
        row=journal.merged('BTC',[])[0]
        self.assertEqual(row['first_up_above_50_minute'],2)
        self.assertEqual(row['last_up_above_50_minute'],4)
        self.assertEqual(row['last_down_above_50_minute'],6)
        self.assertTrue(row['price_threshold_50_backfilled'])

    def test_last_crossings_do_not_advance_while_above_50(self):
        def quote(yes,no,seconds):
            with patch.object(app,'now_local',return_value=self.start+timedelta(seconds=seconds)):
                app.record_contract_prices('BTC',self.start,self.end,83000,{'yes_cents':yes,'no_cents':no})
        quote(50,50,0)
        self.assertNotIn('last_yes_cross_50_at',self.row())
        quote(51,49,60)
        quote(70,30,120)
        self.assertEqual(self.row()['last_yes_cross_50_minute'],1)
        quote(49,51,180)
        quote(30,70,240)
        self.assertEqual(self.row()['last_no_cross_50_minute'],3)
        quote(50,50,300)
        quote(51,49,360)
        quote(60,40,420)
        row=self.row()
        self.assertEqual(row['last_yes_cross_50_minute'],6)
        self.assertEqual(row['last_no_cross_50_minute'],3)
        quote(49,51,900)
        self.assertEqual(self.row()['last_no_cross_50_minute'],3)
        result=app.app.test_client().get('/api/round-history').get_json()['rounds'][0]
        self.assertEqual(result['last_yes_cross_50_minute'],6)
        self.assertEqual(result['last_no_cross_50_minute'],3)

    def test_historical_crossings_rebuilt_separately_from_last_above(self):
        start=self.start-timedelta(minutes=30)
        end=start+timedelta(minutes=15)
        samples=[{'observed_at':app.dt_iso(start+timedelta(minutes=m)), 'target':83000,
                  'yes_cents':yes,'no_cents':no} for m,yes,no in
                 [(1,51,49),(2,60,40),(3,50,50),(4,49,51),(5,40,60),(6,51,49),(7,70,30)]]
        fields=journal.last_50_crossings(list(reversed(samples)),start,end,83000)
        self.assertEqual(fields['last_yes_cross_50_minute'],6)
        self.assertEqual(fields['last_no_cross_50_minute'],4)
        journal.upsert('BTC',app.dt_iso(start),app.dt_iso(end),target=83000,contract_price_history=samples)
        result=next(row for row in journal.merged('BTC',[]) if row['round_start']==app.dt_iso(start))
        self.assertEqual(result['last_yes_cross_50_minute'],6)
        self.assertEqual(result['last_no_cross_50_minute'],4)
        self.assertEqual(result['last_up_above_50_minute'],7)
        self.assertTrue(result['price_crossings_50_backfilled'])
        self.assertEqual(journal.get('BTC',app.dt_iso(start))['last_yes_cross_50_minute'],6)

    def test_crossing_validation_and_first_available_quote(self):
        self.assertTrue(journal.crossed_above_50(None,51))
        self.assertTrue(journal.crossed_above_50(50,51))
        self.assertFalse(journal.crossed_above_50(51,60))
        for value in (None,True,float('nan'),float('inf'),101,-1,50):
            self.assertFalse(journal.crossed_above_50(49,value))
