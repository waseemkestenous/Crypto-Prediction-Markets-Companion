"""Exercise actual browser form interactions against a local fixture, never an exchange."""
import unittest
from datetime import datetime
from playwright.sync_api import sync_playwright
import robinhood_target as rh


class FormTests(unittest.TestCase):
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
        self.start = datetime.fromisoformat('2026-10-10T23:30:00-05:00')
        self.end = datetime.fromisoformat('2026-10-10T23:45:00-05:00')
        self.messages = []

    def fixture(self, yes, no):
        self.page.set_content('''<title>BTC 15 min · 12:30–12:45 AM EDT</title>
          <p>$82,824.63 target price before 12:45 AM EDT on Oct 11, 2026
          before 12:30 AM EDT on October 11, 2026</p>
          <button onclick="document.body.dataset.side='yes';limit.value='';limit.placeholder='%s¢'">Buy Yes</button>
          <button onclick="document.body.dataset.side='no';limit.value='';limit.placeholder='%s¢'">Buy No</button>
          <button role="combobox" aria-label="Buy In" id="buy" onclick="options.hidden=false">Dollars</button>
          <ul aria-label="Buy In" id="options" hidden><li onclick="buy.textContent='Contracts';options.hidden=true">Contracts</li></ul>
          <label for="qty">Contracts</label><input id="qty">
          <div id="limit-label">Limit Price Bid 68¢ · Ask 69¢ bid and ask limit price detail</div>
          <input id="limit" aria-labelledby="limit-label" placeholder="69¢">
          <button onclick="document.body.dataset.review='clicked'">Review order</button>
          <button onclick="document.body.dataset.submit='clicked'">Submit order</button>
        ''' % (yes, no))

    def prepare(self, direction, callback=None):
        return rh.prepare_contract_form(self.page, 'BTC', self.start, self.end,
             callback or (lambda *args: {'direction': direction, 'target': 82824.63}), self.messages.append)

    def assert_unsubmitted(self):
        self.assertIsNone(self.page.locator('body').get_attribute('data-review'))
        self.assertIsNone(self.page.locator('body').get_attribute('data-submit'))

    def test_up_uses_yes_and_fixes_placeholder_price(self):
        self.fixture(59, 80)
        self.assertTrue(self.prepare('UP / ABOVE'))
        self.assertEqual(self.page.locator('body').get_attribute('data-side'), 'yes')
        self.assertEqual(self.page.locator('#qty').input_value(), '1')
        self.assertEqual(self.page.locator('#limit').input_value(), '59')
        self.assert_unsubmitted()

    def test_down_checks_no_price_even_when_yes_expensive(self):
        self.fixture(81, 19)
        self.assertTrue(self.prepare('DOWN / BELOW'))
        self.assertEqual(self.page.locator('body').get_attribute('data-side'), 'no')
        self.assertEqual(self.page.locator('#limit').input_value(), '19')
        self.assert_unsubmitted()

    def test_sixty_and_above_do_not_fill_quantity(self):
        for price in (60, 69):
            self.fixture(price, 10)
            self.assertFalse(self.prepare('UP / ABOVE'))
            self.assertEqual(self.page.locator('#qty').input_value(), '')
            self.assert_unsubmitted()

    def test_prediction_change_during_fill_clears_quantity(self):
        self.fixture(59, 41)
        original = {'direction': 'UP / ABOVE', 'target': 82824.63}
        replies = iter([original, original, None])
        self.assertFalse(self.prepare('UP / ABOVE', lambda *args: next(replies)))
        self.assertEqual(self.page.locator('#qty').input_value(), '')
        self.assert_unsubmitted()

    def test_target_mismatch_does_not_touch_side(self):
        self.fixture(59, 41)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.prepare('UP / ABOVE', lambda *args: {'direction':'UP / ABOVE','target':1})
        self.assertIsNone(self.page.locator('body').get_attribute('data-side'))
        self.assert_unsubmitted()
