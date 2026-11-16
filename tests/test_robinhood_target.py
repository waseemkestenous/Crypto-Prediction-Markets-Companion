import unittest
from datetime import datetime
from unittest.mock import patch, Mock
import robinhood_target as rh


class EventTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime.fromisoformat('2026-10-10T23:30:00-05:00')
        self.end = datetime.fromisoformat('2026-10-10T23:45:00-05:00')
        self.title = 'BTC 15 min · 12:30–12:45 AM EDT'
        self.text = ('Will the price of Bitcoin be at or above the $82,824.63 target price? '
                     "If the simple average before 12:45 AM EDT on Oct 11, 2026 is at least "
                     "the average before 12:30 AM EDT on October 11, 2026, then Yes.")

    def test_visible_contract_target_and_timeline_dates(self):
        text = '$82,824.63 or above 45¢ Event day October 11, 2026'
        self.assertEqual(rh.parse_event(self.title, text, 'BTC', self.start, self.end), 82824.63)

    def test_wrapped_about_description(self):
        self.assertEqual(rh.parse_event(self.title, self.text.replace(' ', '\n'),
                                      'BTC', self.start, self.end), 82824.63)

    def test_missing_date_rejected_even_with_matching_heading_times(self):
        with self.assertRaisesRegex(ValueError, 'round date'):
            rh.parse_event(self.title, '$82,824.63 or above', 'BTC', self.start, self.end)

    def test_missing_heading_times_rejected(self):
        with self.assertRaisesRegex(ValueError, 'round times'):
            rh.parse_event('BTC 15 min', self.text, 'BTC', self.start, self.end)

    def test_wrong_heading_time_rejected(self):
        with self.assertRaisesRegex(ValueError, 'does not match app round'):
            rh.parse_event(self.title.replace('12:30', '12:15'), self.text,
                           'BTC', self.start, self.end)

    def test_noon_and_midnight_with_omitted_start_meridiem(self):
        for start, end, title in (
            ('2026-10-14T23:45:00-04:00', '2026-10-15T00:00:00-04:00', 'BTC 15 min · 11:45–12:00 AM EDT'),
            ('2026-10-14T11:45:00-04:00', '2026-10-14T12:00:00-04:00', 'BTC 15 min · 11:45–12:00 PM EDT'),
            ('2026-12-14T22:45:00-06:00', '2026-12-14T23:00:00-06:00', 'BTC 15 min · 11:45–12:00 AM EST'),
        ):
            with self.subTest(title=title):
                self.assertEqual(rh.parse_event(title, '$82,824.63 or above Event day ' + datetime.fromisoformat(start).astimezone(datetime.fromisoformat(end).tzinfo).strftime('%B %d, %Y'), 'BTC',
                                               datetime.fromisoformat(start), datetime.fromisoformat(end)), 82824.63)

    def test_login_page_is_reported_without_waiting_for_market_title(self):
        page=Mock()
        page.locator.return_value.inner_text.return_value='Log in to Robinhood Email Password Log In'
        with self.assertRaisesRegex(ValueError, 'Robinhood login required'):
            rh.read_event_target(page,'BTC',self.start,self.end)
        page.get_by_role.assert_not_called()
        self.assertFalse(rh.login_required(self.text))

    def test_visible_heading_preferred_over_stale_tab_title(self):
        page = Mock()
        page.title.return_value = 'Home | Robinhood'
        heading = page.get_by_role.return_value.or_.return_value.filter.return_value.first
        heading.inner_text.return_value = self.title
        page.locator.return_value.inner_text.return_value = self.text
        self.assertEqual(rh.read_event_target(page, 'BTC', self.start, self.end), 82824.63)
        page.title.assert_not_called()
        heading.wait_for.assert_called_once_with(state='visible', timeout=30000)

    def test_unicode_format_characters_and_spacing_in_heading(self):
        title = '\u200eBTC\u200b 15\xa0min · 12:30–12:45 AM EDT'
        self.assertEqual(rh.parse_event(title, self.text, 'BTC', self.start, self.end), 82824.63)

    def test_tbd_rejected_even_if_old_numeric_target_remains(self):
        with self.assertRaisesRegex(ValueError, 'still TBD'):
            rh.parse_event(self.title, 'Target price: TBD or above ' + self.text,
                           'BTC', self.start, self.end)

    def test_tbd_refresh_then_numeric_target_available(self):
        page, stop, report = Mock(), Mock(), Mock()
        page.get_by_role.return_value.or_.return_value.all.return_value = []
        page.get_by_text.return_value.all.return_value = []
        stop.wait.return_value = False
        body = page.locator.return_value
        body.inner_text.return_value = 'Target price: TBD or above'
        page.reload.side_effect = lambda **kwargs: setattr(body.inner_text, 'return_value', self.text)
        self.assertTrue(rh.refresh_pending_target(page, stop, report))
        stop.wait.assert_called_once_with(5)
        page.reload.assert_called_once_with(wait_until='domcontentloaded', timeout=60000)
        self.assertFalse(rh.refresh_pending_target(page, stop, report))
        self.assertEqual(rh.parse_event(self.title, body.inner_text(), 'BTC', self.start, self.end), 82824.63)
        self.assertEqual(page.reload.call_count, 1)

    def test_no_refresh_for_valid_target_or_when_stopping(self):
        page, stop, report = Mock(), Mock(), Mock()
        page.get_by_role.return_value.or_.return_value.all.return_value = []
        page.get_by_text.return_value.all.return_value = []
        page.locator.return_value.inner_text.return_value = self.text
        self.assertFalse(rh.refresh_pending_target(page, stop, report))
        page.reload.assert_not_called()
        page.locator.return_value.inner_text.return_value = 'Target price: TBD or above'
        stop.wait.return_value = True
        self.assertTrue(rh.refresh_pending_target(page, stop, report))
        page.reload.assert_not_called()

    def test_login_uses_ordinary_chrome_and_shared_profile(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(rh, 'PROFILE', Path(folder)), \
                 patch.object(rh.shutil, 'which', return_value='/usr/bin/google-chrome'), \
                 patch.object(rh.subprocess, 'Popen') as launch:
                rh.open_login_browser()
                argv = launch.call_args.args[0]
                self.assertIn('--user-data-dir=' + folder, argv)
                self.assertIn(rh.EVENT_URL, argv)
                self.assertFalse(any('remote-debugging' in v or 'automation' in v for v in argv))

    def test_midnight_timezone_match(self):
        self.assertEqual(rh.parse_event(self.title, self.text, 'BTC', self.start, self.end), 82824.63)

    def test_old_or_conflicting_date_rejected_when_clock_times_match(self):
        for text in (self.text.replace('Oct 11', 'Oct 10'), self.text.replace('Oct 11', 'Oct 10').replace('October 11', 'October 10'), '$82,824.63 or above Event day October 10, 2026'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'date'):
                rh.parse_event(self.title, text, 'BTC', self.start, self.end)

    def test_observed_old_event_rejected_and_current_event_accepted(self):
        start = datetime.fromisoformat('2026-10-15T20:45:00-05:00')
        end = datetime.fromisoformat('2026-10-15T21:00:00-05:00')
        title = 'BTC 15 min · 9:45–10:00 PM EDT'
        old = ('$82,936.94 or above 99.9¢ before 10:00 PM EDT on Oct 10, 2026 '
               'before 9:45 PM EDT on October 10, 2026 Event day October 10, 2026')
        with self.assertRaisesRegex(ValueError, 'round dates'):
            rh.parse_event(title, old, 'BTC', start, end)
        current = old.replace('82,936.94', '82,070.89').replace('Oct 10', 'Oct 15').replace('October 10', 'October 15')
        self.assertEqual(rh.parse_event(title, current, 'BTC', start, end), 82070.89)

    def test_explicit_midnight_dates_and_wrong_year(self):
        start = datetime.fromisoformat('2026-10-14T23:45:00-04:00')
        end = datetime.fromisoformat('2026-10-15T00:00:00-04:00')
        title = 'BTC 15 min · 11:45–12:00 AM EDT'
        text = ('$82,000 or above before 12:00 AM EDT on Oct 15, 2026 '
                'before 11:45 PM EDT on October 14, 2026')
        self.assertEqual(rh.parse_event(title, text, 'BTC', start, end), 82000)
        with self.assertRaisesRegex(ValueError, 'round dates'):
            rh.parse_event(title, text.replace('2026', '2025'), 'BTC', start, end)

    def test_wrong_asset_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Expected ETH'):
            rh.parse_event(self.title, self.text, 'ETH', self.start, self.end)

    def test_conflicting_targets_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unique'):
            rh.parse_event(self.title, self.text + ' $83,000.00 target price', 'BTC', self.start, self.end)

    def test_boundary_or_asset_change_during_fetch_rejected(self):
        import app
        with patch.object(app, 'current_round_key', return_value=(self.end, self.end)):
            self.assertFalse(app.accept_robinhood_target(82824.63, 'BTC', self.start, self.end, rh.EVENT_URL))


class RoundTimingTests(unittest.TestCase):
    def setUp(self):
        import app
        import tempfile
        from pathlib import Path
        self.app = app
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.start = datetime.fromisoformat('2026-10-10T23:30:00-05:00')
        self.end = datetime.fromisoformat('2026-10-10T23:45:00-05:00')
        from datetime import timedelta
        self.next_end = self.end + timedelta(minutes=15)
        self.state = app.STATE.copy()
        self.addCleanup(self.restore)
        app.STATE.update(round_start=self.start, round_end=self.end, target=83000, prediction={"old": True})
        self.patches = [patch.object(app.ARGS, 'asset', 'BTC'),
                        patch.object(app, 'current_round_key', return_value=(self.start, self.end)),
                        patch.object(app, 'now_local', return_value=self.end - timedelta(seconds=60)),
                        patch.object(app, '__file__', str(Path(self.folder.name) / 'app.py')),
                        patch.object(app.round_history, 'ROOT', Path(self.folder.name))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def restore(self):
        self.app.STATE.clear()
        self.app.STATE.update(self.state)

    def test_accepted_current_target_is_immediately_visible_in_dashboard_api(self):
        from pathlib import Path
        import json
        self.app.STATE.update(target=None, prediction=None)
        with patch.object(self.app.threading, 'Thread'):
            self.assertTrue(self.app.accept_robinhood_target(82824.63, 'BTC',
                            self.start, self.end, rh.EVENT_URL))
        response = self.app.app.test_client().get('/api/state').get_json()
        self.assertEqual(response['target'], 82824.63)
        self.assertEqual(response['analysis_status'], 'analyzing')
        saved = json.loads((Path(self.folder.name) / 'current_target.json').read_text())
        self.assertEqual(saved['target'], response['target'])
        self.assertEqual(saved['round_start'], response['round']['start'])

    def test_last_minute_stays_on_current_round(self):
        from datetime import timedelta
        for seconds in (61, 60, 1):
            with patch.object(self.app, 'now_local', return_value=self.end - timedelta(seconds=seconds)):
                snapshot = self.app.robinhood_snapshot()
                self.assertEqual(snapshot[1:3], (self.start, self.end))
                self.assertFalse(snapshot[4])

    def test_new_round_selected_only_at_boundary(self):
        with patch.object(self.app, 'current_round_key', return_value=(self.end, self.next_end)):
            snapshot = self.app.robinhood_snapshot()
            self.assertEqual(snapshot[1:3], (self.end, self.next_end))
            self.assertFalse(snapshot[3])
            self.assertFalse(snapshot[4])

    def test_next_round_target_rejected_even_in_final_minute(self):
        self.assertFalse(self.app.accept_robinhood_target(82824.63, 'BTC',
                         self.end, self.next_end, rh.EVENT_URL))
        self.assertEqual(self.app.STATE['target'], 83000)

    def test_early_next_round_and_wrong_asset_rejected(self):
        from datetime import timedelta
        with patch.object(self.app, 'now_local', return_value=self.end - timedelta(seconds=61)):
            self.assertFalse(self.app.accept_robinhood_target(82824.63, 'BTC', self.end, self.next_end, rh.EVENT_URL))
        self.assertFalse(self.app.accept_robinhood_target(82824.63, 'ETH', self.end, self.next_end, rh.EVENT_URL))


if __name__ == '__main__':
    unittest.main()
