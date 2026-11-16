"""Read the public BTC five-minute market with Robinhood's Chrome approach."""
import atexit
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import threading
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

URL = 'https://web.crypto.com/explore/predict/crypto/btc/5-min'
PROFILE = Path(__file__).resolve().parent / '.crypto-com-browser'
ET = ZoneInfo('America/New_York')


def parse_current_market(text, links, now):
    """Require matching visible heading, dated market URL and live countdown."""
    start = datetime.fromtimestamp(int(now.timestamp()) // 300 * 300, timezone.utc)
    end = start + timedelta(minutes=5)
    local_end = end.astimezone(ET)
    headings = list(re.finditer(
        r'Bitcoin price Today at (\d{1,2}):(\d{2})\s*(am|pm) ET\s+LIVE\s+'
        r'(?:(\d+)m\s*)?(\d+)s\s+Above\s+\$([\d,]+(?:\.\d+)?)', text, re.I))
    if len(headings) != 1:
        raise ValueError('A unique LIVE Bitcoin five-minute target is not visible.')
    h = headings[0]
    hour, minute = int(h[1]), int(h[2])
    if not 1 <= hour <= 12 or minute > 59:
        raise ValueError('Invalid market heading time.')
    hour = hour % 12 + (12 if h[3].lower() == 'pm' else 0)
    if (hour, minute) != (local_end.hour, local_end.minute):
        raise ValueError('Visible Bitcoin market is not the current five-minute round.')
    remaining = int(h[4] or 0) * 60 + int(h[5])
    if not 0 < remaining <= 300 or abs(remaining - (end-now).total_seconds()) > 12:
        raise ValueError('Market countdown does not match the current round.')
    expected_slug = (f'bitcoin-price-on-{local_end.day}-'
                     f'{local_end.strftime("%b").lower()}-at-'
                     f'{local_end.hour % 12 or 12}{local_end.minute:02d}-'
                     f'{"am" if local_end.hour < 12 else "pm"}-et-')
    matching = set()
    for link in links:
        parsed = urlparse(link)
        if (parsed.scheme == 'https' and parsed.netloc == 'web.crypto.com'
                and parsed.path.startswith('/explore/predict/markets/' + expected_slug)):
            matching.add(link)
    if len(matching) != 1:
        raise ValueError('Current round date could not be verified from the market link.')
    target = float(h[6].replace(',', ''))
    if target <= 0:
        raise ValueError('Invalid Predict target.')
    return {'target': target, 'round_start': start.isoformat(),
            'round_end': end.isoformat(), 'url': matching.pop(),
            'read_at': now.isoformat()}


def parse_contract_quotes(text, url, market, now):
    if url != market['url'] or not (datetime.fromisoformat(market['round_start']) <= now < datetime.fromisoformat(market['round_end'])):
        raise ValueError('Contract quotes belong to an expired or different round.')
    # Recommendations and order-book panels contain other strike prices. Only
    # the primary Outcome block binds the Yes/No buttons to this contract.
    sections = re.findall(r'(?ms)^\s*Outcome\s*\n(.*?)^\s*Trade Yes\s*$', text)
    if len(sections) != 1:
        raise ValueError('Current contract Outcome section is not visible.')
    outcome = sections[0]
    targets = {float(v.replace(',', '')) for v in re.findall(r'Above\s+\$([\d,]+(?:\.\d+)?)', outcome)}
    if targets != {market['target']}:
        raise ValueError('Contract page target does not match the current market.')
    values = {}
    for side in ('Yes', 'No'):
        matches = {float(v) for v in re.findall(r'(?m)^\s*' + side + r'\s+(\d+(?:\.\d+)?)\s*¢\s*$', outcome)}
        if len(matches) != 1 or not 0 <= next(iter(matches)) <= 100:
            raise ValueError(f'Current {side} contract price is not visible.')
        values[side.lower() + '_cents'] = matches.pop()
    return dict(values, read_at=now.isoformat(), round_start=market['round_start'], url=url)


class PredictWebsiteReader:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.snapshot = None
        self.error = None
        self.headless = True

    def read(self):
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self.run, daemon=True,
                                               name='crypto-predict-website')
                self.thread.start()
            result = dict(self.snapshot) if self.snapshot else None
            error = self.error
        if result:
            age = (datetime.now(timezone.utc)-datetime.fromisoformat(result['read_at'])).total_seconds()
            if not 0 <= age <= 15:
                result = None
        return result, error

    def run(self):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    str(PROFILE), channel='chrome', headless=self.headless,
                    no_viewport=not self.headless,
                    **({'viewport': {'width': 1920, 'height': 1080}} if self.headless else {}))
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(URL, wait_until='domcontentloaded', timeout=60000)
                    detail = context.new_page()
                    detail_url = None
                    contract_failures = 0
                    failures = 0
                    while not self.stop.is_set():
                        try:
                            text = page.locator('body').inner_text(timeout=5000)
                            links = page.locator('a[href*="/markets/bitcoin-price-on-"]').evaluate_all(
                                '(elements) => elements.map(e => e.href)')
                            result = parse_current_market(text, links, datetime.now(timezone.utc))
                            with self.lock:
                                if self.snapshot and self.snapshot['round_start'] == result['round_start']:
                                    result['contract_prices'] = self.snapshot.get('contract_prices')
                                    result['contract_error'] = self.snapshot.get('contract_error')
                                self.snapshot, self.error = result, None
                            failures = 0
                            try:
                                if detail_url != result['url']:
                                    detail.goto(result['url'], wait_until='domcontentloaded', timeout=45000)
                                    detail_url = result['url']
                                    contract_failures = 0
                                detail.get_by_role('button', name=re.compile(r'^Yes \d+(?:\.\d+)?¢$')).wait_for(timeout=10000)
                                quotes = parse_contract_quotes(detail.locator('body').inner_text(timeout=5000), detail.url, result, datetime.now(timezone.utc))
                                result['contract_prices'] = quotes
                                result['contract_error'] = None
                                contract_failures = 0
                            except Exception as exc:
                                result['contract_prices'] = None
                                result['contract_error'] = str(exc)
                                contract_failures += 1
                                if contract_failures >= 5:
                                    # Navigate again next pass if the contract shell
                                    # remains stale or failed to hydrate.
                                    detail_url = None
                                    contract_failures = 0
                            with self.lock:
                                self.snapshot = result
                        except Exception as exc:
                            with self.lock:
                                self.snapshot, self.error = None, str(exc)
                            failures += 1
                            if failures >= 30:
                                page.reload(wait_until='domcontentloaded', timeout=45000)
                                failures = 0
                        self.stop.wait(1)
                finally:
                    context.close()
        except Exception as exc:
            with self.lock:
                self.snapshot, self.error = None, f'Crypto.com browser: {exc}'

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)


READER = PredictWebsiteReader()
atexit.register(READER.close)
