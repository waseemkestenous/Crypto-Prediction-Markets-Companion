"""One shared read-only browser for the public, rendered BRTI summary."""
import atexit
import re
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

MAX_QUOTE_AGE_SECONDS = 20
SOURCE_POLL_INTERVAL_SECONDS = 0.5
HTML_FALLBACK_POLL_INTERVAL_SECONDS = 2
URL = 'https://www.cfbenchmarks.com/data/indices/BRTI'


def parse_rendered_summary(text):
    match = re.search(r'CME CF Bitcoin Real Time Index\s+BRTI\s+\$([\d,]+\.\d{2}).*?Last updated:\s*([^\n]+)', text, re.S)
    if not match:
        raise RuntimeError('Rendered BRTI price/time is not visible.')
    value = float(match[1].replace(',', ''))
    stamp = parsedate_to_datetime(match[2].strip())
    if value <= 0 or stamp.tzinfo is None:
        raise RuntimeError('Invalid rendered BRTI summary.')
    return {'value':value,'source_time':stamp,'fetched_at':datetime.now(timezone.utc)}


class LiveWebsiteReader:
    def __init__(self):
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.thread = None
        self.snapshot = None
        self.error = None

    def read(self):
        with self.lock:
            if self.thread is None:
                self.thread = threading.Thread(target=self.run,daemon=True,name='brti-website')
                self.thread.start()
        self.ready.wait(15)
        with self.lock:
            snapshot = dict(self.snapshot) if self.snapshot else None
            error = self.error
        if snapshot is None:
            raise RuntimeError(error or 'BRTI live website is loading.')
        age = (datetime.now(timezone.utc)-snapshot['source_time']).total_seconds()
        if not 0 <= age <= MAX_QUOTE_AGE_SECONDS:
            raise RuntimeError(f'BRTI rendered quote age is {age:.1f}s; maximum allowed is {MAX_QUOTE_AGE_SECONDS}s.')
        return snapshot

    def run(self):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='chrome',headless=True)
                try:
                    page = browser.new_page()
                    page.goto(URL,wait_until='domcontentloaded',timeout=30000)
                    page.get_by_text('Last updated:',exact=False).first.wait_for(timeout=15000)
                    failures = 0
                    stale_reads = 0
                    while not self.stop.is_set():
                        try:
                            snapshot = parse_rendered_summary(page.locator('body').inner_text(timeout=5000))
                            with self.lock:
                                if not self.snapshot or snapshot['source_time'] >= self.snapshot['source_time']:
                                    self.snapshot = snapshot
                                self.error = None
                            failures = 0
                            age = (datetime.now(timezone.utc)-snapshot['source_time']).total_seconds()
                            if 0 <= age <= MAX_QUOTE_AGE_SECONDS:
                                self.ready.set()
                            stale_reads = stale_reads+1 if age > 20 else 0
                            if stale_reads * SOURCE_POLL_INTERVAL_SECONDS >= 20:
                                page.reload(wait_until='domcontentloaded',timeout=15000)
                                stale_reads = 0
                        except Exception as exc:
                            with self.lock:
                                self.error = str(exc)
                            self.ready.set()
                            failures += 1
                            if failures >= 3:
                                page.reload(wait_until='domcontentloaded',timeout=15000)
                                failures = 0
                        self.stop.wait(SOURCE_POLL_INTERVAL_SECONDS)
                finally:
                    browser.close()
        except Exception as exc:
            with self.lock:
                self.error = f'BRTI live browser unavailable: {exc}'
                self.thread = None
            self.ready.set()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)


READER = LiveWebsiteReader()
atexit.register(READER.close)


def read_live_brti():
    return READER.read()
