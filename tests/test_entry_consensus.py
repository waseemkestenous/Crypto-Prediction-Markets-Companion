import copy
import unittest
from unittest.mock import Mock,patch
from datetime import datetime,timedelta
import app
import robinhood_target as rh
import order_settings


class EntryConsensusTests(unittest.TestCase):
    def prediction(self,direction='UP / ABOVE',votes=3):
        other='DOWN / BELOW' if direction=='UP / ABOVE' else 'UP / ABOVE'
        s=order_settings.validate(dict(order_settings.DEFAULTS,required_models=[],agreement_basis='both',require_final_live_agreement=True,min_model_agreement=3))
        return dict(direction=direction,live_direction=direction,live_snapshot={'direction':direction},
                    order_settings=s,model_directions=dict({'Final prediction':direction},**{name:direction if i<votes else other for i,name in enumerate(order_settings.MODEL_NAMES)}))

    def test_three_and_four_votes_both_sides_pass_but_split_votes_fail(self):
        for side in ('UP / ABOVE','DOWN / BELOW'):
            for votes in range(5):
                self.assertEqual(rh.entry_consensus_allows(self.prediction(side,votes),side),votes>=3)

    def test_missing_live_missing_model_or_direction_conflict_blocks(self):
        for field,value in [('live_direction','DOWN / BELOW'),('live_snapshot',None),('direction','DOWN / BELOW')]:
            p=self.prediction();p[field]=value
            self.assertFalse(rh.entry_consensus_allows(p,'UP / ABOVE'))
        p=self.prediction();del p['model_directions']['Trend']
        self.assertFalse(rh.entry_consensus_allows(p))
        self.assertTrue(rh.entry_consensus_allows({'order_settings':{}}))

    def test_final_rechecks_block_all_buy_routes_when_consensus_changes(self):
        expected=self.prediction()
        for options in ({},{'fallback_rule':{'min_distance_dollars':None}},{'force_buy':True}):
            p=copy.deepcopy(expected);p['live_direction']='DOWN / BELOW'
            self.assertFalse(rh.buy_prediction_matches(p,expected,**options))
            p=self.prediction(votes=2)
            self.assertFalse(rh.buy_prediction_matches(p,expected,**options))

    def test_primary_fallback_and_force_preparation_do_not_touch_browser_when_blocked(self):
        p=self.prediction(votes=2)
        p.update(target=83000,brti_price=83100,force_buy_request={'requested':True})
        p['order_settings']['force_buy']['enabled']=True
        p['order_settings']['fallbacks'][0].update(enabled=True,min_distance_dollars=0)
        start=datetime.now().astimezone();end=start+timedelta(minutes=15)
        for options in ({},{'fallback':True},{'force_buy':True}):
            page=Mock()
            with patch.object(rh,'buy_accuracy_guard_allows',return_value=True),patch.object(rh,'buy_choppiness_guard_allows',return_value=True):
                report=Mock()
                self.assertFalse(rh.prepare_contract_form(page,'BTC',start,end,lambda *a:p,report,**options))
                self.assertIn('Final and fresh Live',report.call_args.args[0])
                self.assertEqual(page.mock_calls,[])

    def test_backend_refuses_attempt_but_does_not_gate_fill_or_close_updates(self):
        start=datetime.now().astimezone();end=start+timedelta(minutes=15)
        settings=dict(app.ORDER_SETTINGS,require_final_live_agreement=True,min_model_agreement=3)
        with patch.dict(app.ORDER_SETTINGS,settings),patch.object(app,'robinhood_form_prediction',return_value=self.prediction(votes=2)),patch.object(app.round_history,'get') as get:
            self.assertFalse(app._record_robinhood_purchase('BTC',start,end,{'status':'attempted','direction':'UP / ABOVE'}))
            get.assert_not_called()

    def test_settings_validate_strict_types(self):
        for fields in ({'require_final_live_agreement':1},{'min_model_agreement':True},{'min_model_agreement':2.5},{'min_model_agreement':5}):
            with self.assertRaises(ValueError):order_settings.validate(dict(order_settings.DEFAULTS,**fields))

    def test_saving_from_old_page_preserves_enabled_consensus_gates(self):
        with patch.dict(app.ORDER_SETTINGS,require_final_live_agreement=True,min_model_agreement=3),patch.object(order_settings,'save'):
            response=app.app.test_client().post('/api/order-settings',json=dict(order_settings.DEFAULTS,require_final_live_agreement=True,min_model_agreement=3))
            self.assertEqual(response.status_code,200)
            old=dict(order_settings.DEFAULTS)
            del old['require_final_live_agreement'];del old['min_model_agreement']
            result=app.app.test_client().post('/api/order-settings',json=old)
            self.assertEqual(result.status_code,200)
            self.assertTrue(result.json['order_settings']['require_final_live_agreement'])
            self.assertEqual(result.json['order_settings']['min_model_agreement'],3)
