import unittest
from datetime import datetime, timezone
from crypto_website import parse_current_market


class CryptoWebsiteTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 14, 6, 20, 13, tzinfo=timezone.utc)
        self.text = 'BTC\n5 min\nBitcoin price Today at 1:25 am ET\nLIVE\n4m 47s\n\nAbove $67,250.57\n3.23x'
        self.url = 'https://web.crypto.com/explore/predict/markets/bitcoin-price-on-14-nov-at-125-am-et-wsill6'

    def test_observed_page_target_and_end(self):
        s = parse_current_market(self.text, [self.url], self.now)
        self.assertEqual(s['target'], 67250.57)
        self.assertEqual(s['round_end'], '2026-11-14T06:25:00+00:00')
        self.assertEqual(s['round_start'], '2026-11-14T06:20:00+00:00')

    def test_old_round_target_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_current_market(self.text.replace('1:25', '1:20'), [self.url], self.now)

    def test_wrong_date_or_host_is_rejected(self):
        for url in (self.url.replace('14-nov', '13-nov'), self.url.replace('web.crypto.com', 'example.com')):
            with self.assertRaises(ValueError):
                parse_current_market(self.text, [url], self.now)

    def test_stale_countdown_and_ambiguous_targets_are_rejected(self):
        for text in (self.text.replace('4m 47s', '2m 0s'), self.text+'\n'+self.text):
            with self.assertRaises(ValueError):
                parse_current_market(text, [self.url], self.now)

    def test_et_midnight_uses_contract_date(self):
        now = datetime(2026,11,15,4,59,30,tzinfo=timezone.utc)
        text = 'Bitcoin price Today at 12:00 am ET\nLIVE\n30s\nAbove $67,250.57'
        url = self.url.replace('14-nov-at-125-am', '15-nov-at-1200-am')
        self.assertEqual(parse_current_market(text,[url],now)['round_end'], '2026-11-15T05:00:00+00:00')


class ContractQuoteTests(unittest.TestCase):
    def test_real_visible_quotes_and_round_guards(self):
        from crypto_website import parse_contract_quotes
        now=datetime(2026,11,14,6,26,tzinfo=timezone.utc)
        market={'target':67313.47,'url':'https://web.crypto.com/round',
                'round_start':'2026-11-14T06:25:00+00:00',
                'round_end':'2026-11-14T06:30:00+00:00'}
        text='Outcome\nAbove $67,313.47\n29%\nYes 29¢\nNo 72¢\nTrade Yes\nTrade No'
        q=parse_contract_quotes(text,market['url'],market,now)
        self.assertEqual(q['yes_cents'],29)
        self.assertEqual(q['no_cents'],72)
        recommendations = '\nPicks for you\nAbove $68,000.00\nYes 50¢\nNo 51¢'
        scoped = parse_contract_quotes(text+recommendations,market['url'],market,now)
        self.assertEqual(scoped['yes_cents'],29)
        self.assertEqual(scoped['no_cents'],72)
        with self.assertRaises(ValueError):parse_contract_quotes(text.replace('Outcome','Other'),market['url'],market,now)
        for bad in (text.replace('67,313.47','67,250.57'),text.replace('Yes 29¢','Yes 101¢'),text.replace('No 72¢','72%')):
            with self.assertRaises(ValueError):parse_contract_quotes(bad,market['url'],market,now)
        with self.assertRaises(ValueError):parse_contract_quotes(text,'wrong',market,now)
        with self.assertRaises(ValueError):parse_contract_quotes(text,market['url'],market,datetime(2026,11,14,6,30,tzinfo=timezone.utc))
