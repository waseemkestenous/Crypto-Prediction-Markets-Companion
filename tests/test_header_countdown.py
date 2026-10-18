import re
import unittest
from playwright.sync_api import sync_playwright
import app


class HeaderCountdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html=app.app.test_client().get('/').get_data(as_text=True)
        source=next(s for s in re.findall(r'<script[^>]*>(.*?)</script>',cls.html,re.S)
                    if 'function syncHeaderCountdown' in s)
        cls.helpers=source[source.index('function secondsText'):source.index('let orderSettingsDirty=')]
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch(channel='chrome',headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()

    def setUp(self):
        self.page=self.browser.new_page(viewport={'width':1400,'height':900})
        self.addCleanup(self.page.close)
        self.page.set_content(re.sub(r'<script\b[^>]*>.*?</script>','',self.html,flags=re.S))
        self.page.evaluate("()=>{window.testClock=0;Object.defineProperty(performance,'now',{value:()=>window.testClock});}")
        self.page.add_script_tag(content=self.helpers)

    def sync(self,seconds):
        self.page.evaluate('''seconds=>{
          const server=Date.parse('2026-10-16T12:00:00Z');
          syncHeaderCountdown({server_time:new Date(server).toISOString(),round:{end:new Date(server+seconds*1000).toISOString()}});
        }''',seconds)

    def test_all_color_boundaries_and_compact_bold_text(self):
        for seconds,text,color in [(900,'15:00','rgb(48, 210, 124)'),(601,'10:01','rgb(48, 210, 124)'),
                (600,'10:00','rgb(102, 156, 255)'),(361,'06:01','rgb(102, 156, 255)'),
                (360,'06:00','rgb(237, 243, 251)'),(181,'03:01','rgb(237, 243, 251)'),
                (180,'03:00','rgb(245, 196, 81)'),(61,'01:01','rgb(245, 196, 81)'),
                (60,'01:00','rgb(255, 92, 108)'),(0,'00:00','rgb(255, 92, 108)')]:
            self.sync(seconds)
            timer=self.page.locator('#headerRoundTimer')
            self.assertEqual(timer.inner_text(),text)
            self.assertEqual(timer.evaluate('(e)=>getComputedStyle(e).color'),color)
        self.assertEqual(timer.evaluate('(e)=>parseFloat(getComputedStyle(e).fontSize)'),32)
        self.assertEqual(timer.evaluate('(e)=>getComputedStyle(e).fontWeight'),'900')

    def test_ticks_without_server_polls_clamps_zero_and_resets_next_round(self):
        self.sync(63)
        self.page.evaluate('window.testClock=4000;tickHeaderCountdown()')
        self.assertEqual(self.page.locator('#headerRoundTimer').inner_text(),'00:59')
        self.page.evaluate('window.testClock=100000;tickHeaderCountdown()')
        self.assertEqual(self.page.locator('#headerRoundTimer').inner_text(),'00:00')
        self.sync(900)
        self.assertEqual(self.page.locator('#headerRoundTimer').inner_text(),'15:00')
        self.page.evaluate('syncHeaderCountdown({})')
        self.assertEqual(self.page.locator('#headerRoundTimer').inner_text(),'--:--')

    def test_centered_desktop_and_mobile_header(self):
        self.sync(420)
        for width in (1400,1024,375):
            self.page.set_viewport_size({'width':width,'height':900})
            header=self.page.locator('.header').bounding_box()
            timer=self.page.locator('#headerRoundTimer').bounding_box()
            self.assertAlmostEqual(timer['x']+timer['width']/2,header['x']+header['width']/2,delta=1)
            self.assertTrue(self.page.locator('.header').evaluate('(e)=>e.scrollWidth<=e.clientWidth'))
        self.page.set_viewport_size({'width':1400,'height':900})
        height_with_timer=self.page.locator('.header').bounding_box()['height']
        self.page.locator('#headerRoundTimer').evaluate('(e)=>e.hidden=true')
        self.assertEqual(self.page.locator('.header').bounding_box()['height'],height_with_timer)
        self.page.locator('#headerRoundTimer').evaluate('(e)=>e.hidden=false')
        self.page.locator('.header').screenshot(path='/tmp/targetpulse-header-countdown.png')
