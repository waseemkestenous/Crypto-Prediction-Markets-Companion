import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock,patch
import app
import order_settings as settings
import robinhood_target as rh
import round_history


class ForceBuyTests(unittest.TestCase):
    def form(self,requested=True,enabled=True,brti=None,threshold=None,price=50,minute=3,direction='DOWN / BELOW'):
        now=datetime.now().astimezone();start=now-timedelta(minutes=minute);end=start+timedelta(minutes=15)
        rule=dict(settings.FORCE_BUY_DEFAULTS,enabled=enabled,contracts=2,min_price_cents=40,max_price_cents=60,
                  min_distance_dollars=threshold,time_in_force='good_till_date')
        p={'direction':'UP / ABOVE','target':83000,'live_direction':direction,
           'live_snapshot':{'direction':direction,'confidence':.7},'brti_price':brti,
           'purchase_exists':False,'force_buy_request':'request' if requested else None,
           'order_settings':dict(settings.DEFAULTS,force_buy=rule)}
        key='yes_cents' if direction=='UP / ABOVE' else 'no_cents';other='no_cents' if key=='yes_cents' else 'yes_cents'
        p['contract_price_history']=[{'target':83000,'observed_at':(start+timedelta(seconds=i)).isoformat(),key:price-.1+i*.01,other:101-price+.1-i*.01} for i in range(11)]
        p['contract_prices']={key:price,other:101-price}
        page,qty,limit,buyin,done=Mock(),Mock(),Mock(),Mock(),Mock();page.url='http://fixture.test'
        qty.input_value.return_value='2';limit.input_value.return_value=str(price);buyin.inner_text.return_value='Contracts'
        done.all.return_value=[];qty.or_.return_value=qty;qty.count.return_value=1
        page.get_by_role.side_effect=lambda role,**args:buyin if role=='combobox' else qty if 'Contracts' in str(args.get('name')) else limit if role=='textbox' else done if args.get('name')=='Done' else Mock()
        record,report=Mock(return_value=True),Mock()
        with patch.object(rh,'read_event_target',return_value=83000),patch.object(rh,'review_buy_details',return_value=True),patch.object(rh,'select_time_in_force') as tif,patch.object(rh,'parse_contract_prices',return_value=p['contract_prices']):
            result=rh.prepare_contract_form(page,'BTC',start,end,lambda *args:p,report,record,force_buy=True)
        return result,record,tif,page

    def test_manual_buy_uses_live_side_quantity_and_independent_tif(self):
        result,record,tif,_=self.form()
        self.assertTrue(result)
        purchase=record.call_args_list[0].args[3]
        self.assertEqual(purchase['purchase_rule'],'force_buy')
        self.assertEqual(purchase['direction'],'DOWN / BELOW')
        self.assertEqual(purchase['contracts'],2)
        self.assertEqual(purchase['conditions']['time_in_force'],'good_till_date')
        tif.assert_called_once_with(unittest.mock.ANY,'good_till_date')

    def test_no_auto_buy_and_strict_bounds(self):
        for options in ({'requested':False},{'enabled':False},{'price':40},{'price':60},
                        {'threshold':10,'brti':None},{'threshold':10,'brti':82990},
                        {'minute':15}):
            with self.subTest(options=options):
                result,record,_,_=self.form(**options)
                self.assertFalse(result);record.assert_not_called()
        self.assertTrue(self.form(threshold=10,brti=82989)[0])

    def test_validation_and_request_lifecycle(self):
        now=datetime.now().astimezone();start=now-timedelta(minutes=2);end=start+timedelta(minutes=15)
        config=settings.validate(dict(settings.DEFAULTS,force_buy=dict(settings.FORCE_BUY_DEFAULTS,enabled=True)))
        self.assertFalse(settings.validate(settings.DEFAULTS)['force_buy']['enabled'])
        for update in ({'contracts':1.5},{'min_price_cents':100},{'time_in_force':'bad'}):
            with self.assertRaises(ValueError):settings.validate(dict(settings.DEFAULTS,force_buy=dict(config['force_buy'],**update)))
        with tempfile.TemporaryDirectory() as folder,patch.object(round_history,'ROOT',Path(folder)),patch.dict(app.ORDER_SETTINGS,config,clear=True),patch.object(app.ARGS,'asset','BTC'),patch.object(app,'current_round_key',return_value=(start,end)),patch.dict(app.STATE,round_start=start,target=83000,force_buy_request=None),patch.object(app,'robinhood_report'):
            client=app.app.test_client();payload={'asset':'BTC','round_start':start.isoformat()}
            self.assertEqual(client.post('/api/force-buy',json=payload).status_code,200)
            token=app.current_force_buy_request('BTC',start,end)
            self.assertTrue(token)
            self.assertEqual(client.post('/api/force-buy',json=payload).status_code,200)
            self.assertEqual(app.current_force_buy_request('BTC',start,end),token)
            with patch.object(app,'_record_robinhood_purchase',return_value=True):
                self.assertTrue(app.record_robinhood_purchase('BTC',start,end,{'status':'attempted'}))
            self.assertIsNone(app.STATE['force_buy_request'])
            round_history.upsert('BTC',start.isoformat(),end.isoformat(),purchase={'status':'filled'})
            self.assertEqual(client.post('/api/force-buy',json=payload).status_code,409)
