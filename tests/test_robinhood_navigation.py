import unittest
import re
from unittest.mock import Mock, patch
from playwright.sync_api import sync_playwright
import robinhood_target as rh


class NavigationTests(unittest.TestCase):
    def test_clearing_quantity_does_not_wait_for_missing_review_input(self):
        self.page.set_content('<div>Contracts 1</div><button>Buy</button>')
        quantity = self.page.get_by_role('textbox', name=re.compile(r'^Contracts\b'))
        rh.clear_contract_quantity(quantity)
        self.page.set_content('<input aria-label="Contracts" disabled value="1">')
        rh.clear_contract_quantity(quantity)
        self.assertEqual(quantity.input_value(), '1')
    def test_disabled_ioc_limit_verifies_placeholder_without_writing(self):
        self.page.set_content('<input aria-label="Limit Price" disabled placeholder="87¢">')
        limit = self.page.get_by_role('textbox', name='Limit Price')
        self.assertTrue(rh.apply_limit_price(limit, 87))
        self.assertEqual(limit.input_value(), '')
        self.assertFalse(rh.apply_limit_price(limit, 86))

    def test_editable_limit_is_filled_and_verified(self):
        self.page.set_content('<input aria-label="Limit Price" placeholder="87¢">')
        limit = self.page.get_by_role('textbox', name='Limit Price')
        self.assertTrue(rh.apply_limit_price(limit, 85))
        self.assertEqual(limit.input_value(), '85')
    def test_time_in_force_is_selected_and_verified(self):
        for value, label in [('ioc', 'Immediate or cancel (IOC)'), ('good_till_date', 'Good till date')]:
            self.page.set_content('''<button role="combobox" aria-label="Time in Force" id="tif"
                onclick="options.hidden=false">Choose</button>
                <div id="options" hidden>
                <button role="option" onclick="tif.textContent=this.textContent;options.hidden=true">Immediate or cancel (IOC)</button>
                <button role="option" onclick="tif.textContent=this.textContent;options.hidden=true">Good till date</button></div>''')
            rh.select_time_in_force(self.page, value)
            self.assertEqual(self.page.locator('#tif').inner_text(), label)
            rh.select_time_in_force(self.page, value)
            self.assertFalse(self.page.locator('#options').is_visible())

    def test_missing_time_in_force_prevents_submission(self):
        self.page.set_content('<button>Buy</button>')
        with self.assertRaisesRegex(ValueError, 'Time in force selector'):
            rh.select_time_in_force(self.page, 'ioc')

    def test_buy_in_contracts_portal_option_and_legacy_list(self):
        for role in ('menuitemradio','option','listitem'):
            self.page.set_content(f'''<section><button role="combobox" aria-label="Buy In" id="buyin"
                onclick="document.getElementById('buy-menu').hidden=false">Dollars</button></section>
                <div role="list" id="buy-menu" hidden><div role="{role}"
                onclick="document.getElementById('buy-menu').hidden=true;setTimeout(()=>buyin.textContent='Contracts',100)">Contracts</div>
                <div role="{role}">Dollars</div></div>''')
            selector=rh.select_buy_in_contracts(self.page)
            self.assertEqual(selector.inner_text(),'Contracts')
            self.assertFalse(self.page.locator('#buy-menu').is_visible())
            rh.select_buy_in_contracts(self.page)
            self.assertFalse(self.page.locator('#buy-menu').is_visible())

    def test_ioc_menu_outside_close_panel_with_description(self):
        self.page.set_content('''<section id="close-panel">
            <button role="combobox" aria-label="Time in force" id="tif"
              onclick="document.getElementById('portal').hidden=false">Good till dat</button></section>
            <div id="portal" hidden><div role="menuitemradio" tabindex="0"
              onclick="document.getElementById('portal').hidden=true;setTimeout(()=>tif.textContent='Immediate or cancel (IOC)',100)">
              <div>Immediate or cancel (IOC)</div><p>Any part of an IOC order that cannot be filled immediately will be canceled.</p>
            </div><div role="menuitemradio">Good till date<p>Expires tomorrow</p></div></div>''')
        rh.select_time_in_force(self.page.locator('#close-panel'),'ioc',menu_page=self.page)
        self.assertEqual(self.page.locator('#tif').inner_text(),'Immediate or cancel (IOC)')
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.old_hits = 0
        self.content = ''
        def serve(route):
            if route.request.url.endswith('/old'):
                self.old_hits += 1
                route.fulfill(body=self.content, content_type='text/html; charset=utf-8')
            else:
                route.fulfill(body='<h1>Current event</h1>', content_type='text/html; charset=utf-8')
        self.page.route('http://events.test/**', serve)
        self.stop, self.report = Mock(), Mock()
        self.stop.wait.return_value = False

    def test_link_button_and_text_navigate_without_refreshing_old_event(self):
        for html in ('<a href="/new">View current event</a>',
                     '<button onclick="location.href=\'/new\'">View current event</button>',
                     '<span onclick="location.href=\'/new\'">View current event</span>'):
            self.content = html
            self.page.goto('http://events.test/old')
            visits = self.old_hits
            rh.refresh_or_open_current(self.page, self.stop, self.report)
            self.assertTrue(self.page.url.endswith('/new'))
            self.assertEqual(self.old_hits, visits)

    def test_tbd_with_expired_banner_opens_current_event(self):
        self.content = '<p>Target price: TBD or above</p><a href="/new">View current event</a>'
        self.page.goto('http://events.test/old')
        visits = self.old_hits
        self.assertTrue(rh.refresh_pending_target(self.page, self.stop, self.report))
        self.assertTrue(self.page.url.endswith('/new'))
        self.assertEqual(self.old_hits, visits)

    def test_waits_for_delayed_market_heading_among_other_h1_elements(self):
        from datetime import datetime
        self.content = """<title>Home | Robinhood</title><h1>Robinhood</h1>
        <div id='event'></div><script>
        setTimeout(() => {document.getElementById('event').innerHTML =
          '<h1>BTC 15 min · 2:00–2:15 AM EDT</h1>' +
          '<p>$82,963.62 or above Event day October 11, 2026</p>';}, 250);
        </script>"""
        self.page.goto('http://events.test/old')
        target = rh.read_event_target(self.page, 'BTC',
                 datetime.fromisoformat('2026-10-11T01:00:00-05:00'),
                 datetime.fromisoformat('2026-10-11T01:15:00-05:00'))
        self.assertEqual(target, 82963.62)

    def test_plain_text_market_title_and_actionable_quote(self):
        from datetime import datetime
        self.page.set_content("""<h1>Robinhood</h1>
          <div>BTC 15 min · 9:15–9:30 PM EST</div>
          <div>$73,346.95 or above 44¢</div>
          <section>Select a contract <div>$73,346.95 or above 46¢</div></section>
          <p>Event day November 10, 2026</p>""")
        start=datetime.fromisoformat('2026-11-10T20:15:00-06:00')
        end=datetime.fromisoformat('2026-11-10T20:30:00-06:00')
        target=rh.read_event_target(self.page,'BTC',start,end)
        self.assertEqual(target,73346.95)
        self.assertEqual(rh.parse_contract_prices(self.page.locator('body').inner_text(),target)['yes_cents'],46)
        with self.assertRaisesRegex(ValueError,'does not match app round'):
            rh.read_event_target(self.page,'BTC',start.replace(minute=0),end.replace(minute=15))

    def test_no_banner_refreshes_same_event(self):
        self.content = '<p>Target price: TBD or above</p>'
        self.page.goto('http://events.test/old')
        visits = self.old_hits
        rh.refresh_or_open_current(self.page, self.stop, self.report)
        self.assertEqual(self.old_hits, visits + 1)
        self.assertTrue(self.page.url.endswith('/old'))

    def test_fill_requires_visible_done_and_complete_quantity(self):
        for receipt, expected in (
            ('<button>Done</button><div>Filled quantity</div><div>1 of 1</div>', True),
            ('<button>Done</button><div>Filled quantity</div><div>5 of 5</div>', True),
            ('<button>Done</button><div>Filled quantity</div><div>1 of 5</div>', False),
            ('<button>Done</button><div>Order filled</div>', False),
            ('<button hidden>Done</button><div>Filled quantity 1 of 1</div>', False),
            ('<div>Filled quantity 1 of 1</div>', False),
        ):
            with self.subTest(receipt=receipt):
                self.page.set_content(receipt)
                record = Mock(return_value=True)
                with patch.object(rh, 'read_event_target', return_value=83000):
                    self.assertEqual(rh.observe_filled_order(
                        self.page, 'BTC', None, None, record), expected)
                self.assertEqual(record.call_count, int(expected))

    def test_owned_contracts_are_read_without_clicking_close_all(self):
        for quantity, expected in (('1 available', True), ('5 available', True),
                                   ('0 available', False), ('$97.48 available', False)):
            with self.subTest(quantity=quantity):
                self.page.set_content('<div>Contracts</div><div>' + quantity +
                    '</div><button onclick="document.body.dataset.closed=\'yes\'">Close all</button>')
                record = Mock(return_value=True)
                with patch.object(rh, 'read_event_target', return_value=83000):
                    self.assertEqual(rh.observe_owned_contracts(
                        self.page, 'BTC', None, None, record), expected)
                self.assertEqual(record.call_count, int(expected))
                self.assertIsNone(self.page.locator('body').get_attribute('data-closed'))

    def test_your_positions_matches_target_asset_quantity_and_side(self):
        for label, asset, side, quantity, expected in (
            ('83,000', 'BTC', 'Yes', 1, True),
            ('83,000', 'BTC', 'No', 5, True),
            ('82,000', 'BTC', 'Yes', 1, False),
            ('83,000', 'ETH', 'Yes', 1, False),
            ('83,000', 'BTC', 'Yes', 0, False),
        ):
            with self.subTest(label=label, asset=asset, side=side, quantity=quantity):
                self.page.set_content(f'<h2>Your positions</h2><div>${label} or above</div>'
                    f'<div>{quantity} {side} · {asset} 15 min 9:15–9:30 PM EDT @ 56¢</div>')
                record = Mock(return_value=True)
                with patch.object(rh, 'read_event_target', return_value=83000):
                    self.assertEqual(rh.observe_positions_section(
                        self.page, 'BTC', None, None, record), expected)
                self.assertEqual(record.call_count, int(expected))
                if expected:
                    receipt = record.call_args.args[3]
                    self.assertEqual(receipt['available_contracts'], quantity)
                    self.assertEqual(receipt['execution_price_cents'], 56)
                    self.assertEqual(receipt['position_direction'],
                                     'UP / ABOVE' if side == 'Yes' else 'DOWN / BELOW')
