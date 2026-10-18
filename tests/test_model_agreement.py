import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta
import robinhood_target as rh
import app

NAMES = ('Final prediction', 'Pattern', 'Recent', 'Bootstrap', 'Trend')

class AgreementTests(unittest.TestCase):
    def prediction(self, direction):
        return {'direction':direction,'target':83000,'model_directions':dict.fromkeys(NAMES,direction)}

    def test_all_up_and_all_down_accepted(self):
        for direction in ('UP / ABOVE','DOWN / BELOW'):
            self.assertTrue(rh.all_models_agree(self.prediction(direction)))

    def test_each_disagreeing_model_blocks_all_browser_interactions(self):
        for name in NAMES:
            prediction=self.prediction('UP / ABOVE')
            prediction['model_directions'][name]='DOWN / BELOW'
            page,report=Mock(),Mock()
            self.assertFalse(rh.prepare_contract_form(page,'BTC',None,None,lambda *args:prediction,report))
            self.assertEqual(page.mock_calls,[])
            self.assertIn('must all agree',report.call_args.args[0])

    def test_missing_models_and_unknown_direction_rejected(self):
        for name in NAMES:
            prediction=self.prediction('UP / ABOVE')
            del prediction['model_directions'][name]
            self.assertFalse(rh.all_models_agree(prediction))
        self.assertFalse(rh.all_models_agree({'direction':'UP / ABOVE'}))
        self.assertFalse(rh.all_models_agree(self.prediction('UNKNOWN')))

    def test_live_override_when_all_four_components_agree_with_live(self):
        for final,live in (('UP / ABOVE','DOWN / BELOW'),('DOWN / BELOW','UP / ABOVE')):
            p=self.prediction(final)
            p['live_direction']=live
            for name in NAMES[1:]: p['model_directions'][name]=live
            self.assertEqual(rh.purchase_direction(p),live)
            p['model_directions']['Trend']=final
            self.assertIsNone(rh.purchase_direction(p))

    def test_original_unanimous_gate_remains_available(self):
        p=self.prediction('UP / ABOVE')
        p['live_direction']='DOWN / BELOW'
        self.assertEqual(rh.purchase_direction(p),'UP / ABOVE')

    def test_selected_models_ignore_unselected_disagreement(self):
        p = self.prediction('UP / ABOVE')
        p['model_directions']['Trend'] = 'DOWN / BELOW'
        p['order_settings'] = dict(rh.order_settings.DEFAULTS, required_models=['Pattern'], agreement_basis='final')
        self.assertEqual(rh.purchase_direction(p), 'UP / ABOVE')
        p['model_directions']['Pattern'] = 'DOWN / BELOW'
        self.assertIsNone(rh.purchase_direction(p))

    def test_basis_restricts_direction_and_requires_live(self):
        p = self.prediction('UP / ABOVE')
        p['order_settings'] = dict(rh.order_settings.DEFAULTS, required_models=['Pattern'], agreement_basis='live')
        self.assertIsNone(rh.purchase_direction(p))
        p['live_direction'] = 'DOWN / BELOW'
        p['model_directions']['Pattern'] = 'DOWN / BELOW'
        self.assertEqual(rh.purchase_direction(p), 'DOWN / BELOW')
        p['order_settings']['agreement_basis'] = 'final'
        self.assertIsNone(rh.purchase_direction(p))
        p['order_settings']['agreement_basis'] = 'either'
        self.assertEqual(rh.purchase_direction(p), 'DOWN / BELOW')

    def test_no_model_filters_uses_selected_source(self):
        p = self.prediction('UP / ABOVE')
        p['model_directions']['Pattern'] = 'DOWN / BELOW'
        p['order_settings'] = dict(rh.order_settings.DEFAULTS, required_models=[])
        for basis in ('final', 'either'):
            p['order_settings']['agreement_basis'] = basis
            self.assertEqual(rh.purchase_direction(p), 'UP / ABOVE')
        p['order_settings']['agreement_basis'] = 'live'
        self.assertIsNone(rh.purchase_direction(p))
        p['live_direction'] = 'DOWN / BELOW'
        self.assertEqual(rh.purchase_direction(p), 'DOWN / BELOW')

    def test_both_requires_final_live_and_selected_models_to_agree(self):
        for direction in ('UP / ABOVE', 'DOWN / BELOW'):
            p = self.prediction(direction)
            p['order_settings'] = dict(rh.order_settings.DEFAULTS, agreement_basis='both')
            self.assertIsNone(rh.purchase_direction(p))
            p['live_direction'] = 'DOWN / BELOW' if direction == 'UP / ABOVE' else 'UP / ABOVE'
            self.assertIsNone(rh.purchase_direction(p))
            p['live_direction'] = direction
            self.assertEqual(rh.purchase_direction(p), direction)
            p['model_directions']['Pattern'] = 'UNKNOWN'
            self.assertIsNone(rh.purchase_direction(p))
            p['order_settings']['required_models'] = []
            self.assertEqual(rh.purchase_direction(p), direction)
            p['live_direction'] = None
            self.assertIsNone(rh.purchase_direction(p))

    def test_stale_live_recommendation_not_exposed(self):
        now=datetime.now().astimezone()
        start=now.replace(second=0,microsecond=0);end=start+timedelta(minutes=15)
        p={'start':start,'end':end,'prediction':'DOWN / BELOW','p_pattern':.7,'p_regime':.7,'p_bootstrap':.8,'p_trend':.6}
        rec={'updated_at':now-timedelta(seconds=app.core.LIVE_RECOMMENDATION_REFRESH_SECONDS*3),'recommendation':'UP / ABOVE'}
        with patch.object(app.ARGS,'asset','BTC'),patch.object(app,'now_local',return_value=now),patch.object(app,'current_round_key',return_value=(start,end)),patch.dict(app.STATE,{'round_start':start,'target':83000,'prediction':p,'live_recommendation':rec}):
            result=app.robinhood_form_prediction('BTC',start,end)
            self.assertIsNone(result['live_direction'])
            self.assertIsNone(rh.purchase_direction(result))
            rec['updated_at']=now
            result=app.robinhood_form_prediction('BTC',start,end)
            self.assertEqual(rh.purchase_direction(result),'UP / ABOVE')

    def test_live_snapshot_reports_actual_frozen_component_directions(self):
        start=datetime.now().astimezone().replace(second=0,microsecond=0)
        end=start+timedelta(minutes=15)
        prediction={'start':start,'end':end,'prediction':'UP / ABOVE',
                    'p_pattern':.7,'p_regime':.3,'p_bootstrap':.8,'p_trend':.6}
        with patch.object(app.ARGS,'asset','BTC'),patch.object(app,'current_round_key',return_value=(start,end)),patch.dict(app.STATE,{'round_start':start,'target':83000,'prediction':prediction}):
            result=app.robinhood_form_prediction('BTC',start,end)
            self.assertEqual(result['model_directions']['Recent'],'DOWN / BELOW')
            self.assertFalse(rh.all_models_agree(result))
