import unittest
from unittest.mock import patch,Mock
import app
import robinhood_target as rh

class ServerRuntimeTests(unittest.TestCase):
    def test_timeout_refreshes_page_and_other_errors_do_not(self):
        from playwright.sync_api import TimeoutError
        page, report = Mock(), Mock()
        self.assertTrue(rh.recover_browser_timeout(page, TimeoutError('Contracts input timeout'), report))
        page.reload.assert_called_once_with(wait_until='domcontentloaded', timeout=60000)
        page.reload.reset_mock()
        self.assertFalse(rh.recover_browser_timeout(page, ValueError('Round mismatch'), report))
        page.reload.assert_not_called()

    def test_refresh_error_is_reported_without_leaving_browser_loop(self):
        from playwright.sync_api import TimeoutError
        page, report = Mock(), Mock()
        page.reload.side_effect=TimeoutError('reload timeout')
        self.assertTrue(rh.recover_browser_timeout(page, TimeoutError('fill timeout'), report))
        self.assertIn('refresh failed', report.call_args.args[0])
    def test_headless_host_and_port_options(self):
        options=app.parse_runtime_options(['--headless','--host','127.0.0.1','--port','8100'])
        self.assertTrue(options.headless)
        self.assertEqual(options.host,'127.0.0.1')
        self.assertEqual(options.port,8100)
        self.assertFalse(app.parse_runtime_options([]).headless)

    def test_worker_passes_headless(self):
        with patch.object(app.ARGS,'browser_headless',True),patch.object(rh,'browser_loop') as loop:
            app.robinhood_worker()
            self.assertTrue(loop.call_args.kwargs['headless'])

    def test_browser_launch_uses_requested_mode(self):
        for headless in (True,False):
            with patch('playwright.sync_api.sync_playwright') as pw:
                context=pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
                page=Mock();context.pages=[page]
                stop=Mock();stop.is_set.return_value=True
                rh.browser_loop(stop,Mock(),Mock(),Mock(),Mock(),Mock(),headless=headless)
                launch=pw.return_value.__enter__.return_value.chromium.launch_persistent_context
                self.assertEqual(launch.call_args.kwargs['headless'],headless)
                self.assertEqual(launch.call_args.kwargs['no_viewport'],not headless)
                if headless:
                    self.assertEqual(launch.call_args.kwargs['viewport'],
                                     {'width':1920,'height':1080})
                else:
                    self.assertNotIn('viewport',launch.call_args.kwargs)
                context.close.assert_called_once()
