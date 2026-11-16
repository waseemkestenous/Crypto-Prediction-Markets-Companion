"""Read a visible Robinhood event in a dedicated, persistent Chrome profile."""
import math
import os
import sys
import re
import shutil
import subprocess
import unicodedata
import json
from urllib.parse import urlparse
import order_settings
import entry_stability
import round_history
from pathlib import Path
from datetime import datetime, timedelta, timezone

PROFILE = Path(__file__).resolve().parent / '.robinhood-browser'

EVENT_URL = 'https://robinhood.com/events/9eb401bf-7364-4ef4-a354-87639c2feab0?source=expired_settled_banner'
CURRENT_MARKETS_URL = 'https://robinhood.com/prediction-markets?category=6e58e4bd-23e4-4616-87a6-14e17f7e29aa'


def startup_event_url():
    """Resume the last verified BTC event instead of an obsolete seed event."""
    try:
        saved = json.loads((Path(__file__).resolve().parent / 'current_target.json').read_text())
        url = saved.get('startup_source_url') or saved.get('source_url', '')
        parsed = urlparse(url)
        if (saved.get('asset') == 'BTC' and parsed.scheme == 'https'
                and parsed.netloc == 'robinhood.com'
                and parsed.path.startswith('/events/') and parsed.path[len('/events/'):]
                and not parsed.username and not parsed.password):
            return url
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return EVENT_URL


def target_is_pending(text):
    return bool(re.search(r'target\s+price\s*:\s*TBD\b', text, re.I))


def open_current_event(page, stop, report):
    """Prefer the expired-event banner over refreshing an old event URL."""
    name = re.compile(r'^\s*View current event\s*$', re.I)
    candidates = page.get_by_role('button', name=name).or_(
        page.get_by_role('link', name=name))
    visible = [control for control in candidates.all() if control.is_visible()]
    if not visible:
        visible = [control for control in page.get_by_text(name).all() if control.is_visible()]
    if not visible:
        return False
    before = page.url
    report('Opening the current Robinhood event from the expired-event banner...')
    visible[0].click(timeout=10000)
    # Robinhood changes the event URL through client-side navigation.
    page.wait_for_url(lambda url: str(url) != before, timeout=15000,
                      wait_until='domcontentloaded')
    stop.wait(2)
    return True


def open_event_from_market_list(page, stop, report, asset, start, end):
    """Discover and verify a current event in a separate read-only tab."""
    report('Checking the Robinhood market list for the current '+asset+' 15-minute event...')
    probe = page.context.new_page()
    try:
        probe.goto(CURRENT_MARKETS_URL, wait_until='domcontentloaded', timeout=45000)
        links = probe.locator('a[href*="/events/"]')
        links.first.wait_for(state='attached', timeout=20000)
        candidates = links.evaluate_all('''elements => elements.filter(e => e.getClientRects().length).map(e => {
            let node=e, text=e.innerText || '';
            for(let i=0;i<5 && node;i++,node=node.parentElement){
                const value=node.innerText || '';
                if(value.length>1200)break;
                if(/\\b[A-Z]+\\s+15\\s*min\\b/i.test(value)){text=value;break;}
            }
            return {url:e.href,text};
        })''')
        seen = set()
        valid = []
        for candidate in candidates:
            url = candidate['url']
            parsed = urlparse(url)
            if (url in seen or parsed.scheme != 'https' or parsed.netloc != 'robinhood.com'
                    or not parsed.path.startswith('/events/') or not parsed.path[len('/events/'):]
                    or not re.search(rf'\b{re.escape(asset)}\s+15\s*min\b', normalize_event_text(candidate['text']), re.I)):
                continue
            seen.add(url)
            valid.append(url)
        for url in valid[:3]:
            if stop.is_set():
                return False
            probe.goto(url, wait_until='domcontentloaded', timeout=45000)
            try:
                read_event_target(probe, asset, start, end)
            except ValueError:
                continue
            page.goto(probe.url, wait_until='domcontentloaded', timeout=45000)
            report('Found the current '+asset+' round from the Robinhood market list; verifying its live quotes...')
            return True
        report('No verified current '+asset+' round was found on the Robinhood market list; retrying.')
        return False
    except Exception as exc:
        report(f'Robinhood market-list lookup unavailable: {exc}')
        return False
    finally:
        probe.close()


def refresh_or_open_current(page, stop, report, *, asset=None, start=None, end=None):
    if open_current_event(page, stop, report):
        return
    if asset and start is not None and end is not None:
        if open_event_from_market_list(page, stop, report, asset, start, end):
            return
    page.reload(wait_until='domcontentloaded', timeout=60000)


def refresh_pending_target(page, stop, report):
    if not target_is_pending(page.locator('body').inner_text()):
        return False
    report('Robinhood target is TBD; refreshing every 5 seconds until it is published.')
    if not stop.wait(5):
        refresh_or_open_current(page, stop, report)
    return True


def normalize_event_text(value):
    value = unicodedata.normalize('NFKC', value)
    value = ''.join(ch for ch in value if unicodedata.category(ch) != 'Cf')
    return ' '.join(value.split())


def login_required(text):
    text = normalize_event_text(text)
    return bool(re.search(r'\bLog in to Robinhood\b', text, re.I)
                and re.search(r'\bPassword\b', text, re.I))


def read_event_target(page, asset, start, end):
    if login_required(page.locator('body').inner_text(timeout=5000)):
        raise ValueError('Robinhood login required: sign in using the app browser profile to resume contract prices.')
    # Robinhood may render the market title as plain text rather than a heading.
    # Both paths still pass the full target, clock and calendar-date checks.
    headings = page.get_by_role('heading', name=re.compile(r'15[\s\u200b-\u200f]*min\b', re.I))
    market_title = page.get_by_text(re.compile(
        rf'^[\s\u200b-\u200f]*{re.escape(asset)}[\s\u200b-\u200f]+15[\s\u200b-\u200f]*min\b[^\n]*[·].*(?:EDT|EST)\s*$', re.I))
    heading = headings.or_(market_title).filter(visible=True).first
    try:
        heading.wait_for(state='visible', timeout=30000)
    except Exception:
        # Keep a local diagnostic of the actual automated tab for failed reads.
        try:
            diagnostic = Path(__file__).resolve().parent / 'outputs' / 'robinhood-reader-diagnostic.png'
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(diagnostic), timeout=5000)
        except Exception:
            pass
        raise
    title = heading.inner_text(timeout=5000)
    return parse_event(title, page.locator('body').inner_text(), asset, start, end)


def parse_event(title, text, asset, start, end):
    title = normalize_event_text(title)
    text = normalize_event_text(text)
    if target_is_pending(text):
        raise ValueError('Robinhood target is still TBD.')
    if not re.match(rf'^{re.escape(asset)}\s+15\s*min\b', title, re.I):
        raise ValueError(f'Expected {asset} 15-minute market; visible heading is {title!r}.')
    prices = {float(v.replace(',', '')) for v in re.findall(
        r'\$([\d,]+(?:\.\d+)?)\s+(?:target price|or above)', text, re.I)}
    if len(prices) != 1:
        raise ValueError('A unique Robinhood target price is not visible yet.')
    target = prices.pop()
    if not math.isfinite(target) or target <= 0:
        raise ValueError('Invalid Robinhood target.')
    times = re.search(r'·\s*(\d{1,2}:\d{2})(?:\s+([AP]M))?\s*[–—-]\s*'
                      r'(\d{1,2}:\d{2})\s+([AP]M)\s+(EDT|EST)', title, re.I)
    if not times:
        raise ValueError('Cannot verify the Robinhood round times yet.')
    first, meridiem, last, last_meridiem, zone = times.groups()
    event_zone = timezone(timedelta(hours=-4 if zone.upper() == 'EDT' else -5))
    local_start, local_end = start.astimezone(event_zone), end.astimezone(event_zone)
    # An omitted start AM/PM is ambiguous at noon/midnight.
    # Calendar dates are verified separately below; matching clock times alone is unsafe.
    def clock_matches(clock, periods, expected):
        for period in periods:
            parsed = datetime.strptime(clock + ' ' + period.upper(), '%I:%M %p')
            if (parsed.hour, parsed.minute) == (expected.hour, expected.minute):
                return True
        return False
    if not (clock_matches(first, [meridiem] if meridiem else ['AM', 'PM'], local_start)
            and clock_matches(last, [last_meridiem], local_end)):
        raise ValueError(f'Robinhood round time {times.group(0).lstrip("· ")} '
                         f'does not match app round {local_start:%H:%M} → {local_end:%H:%M} {zone.upper()}.')
    # About supplies explicit dates for both ends, including midnight crossings.
    date_pattern = r'([A-Za-z]+\s+\d{1,2},?\s+\d{4})'
    def parse_date(value):
        value = value.replace(',', '')
        for fmt in ('%B %d %Y', '%b %d %Y'):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                pass
        raise ValueError(f'Unrecognized Robinhood event date: {value!r}.')
    observed = []
    for clock, period, tz, day in re.findall(
            r'before\s+(\d{1,2}:\d{2})\s+([AP]M)\s+(EDT|EST)\s+on\s+' + date_pattern,
            text, re.I):
        date = parse_date(day)
        time = datetime.strptime(clock + ' ' + period.upper(), '%I:%M %p').time()
        observed.append(datetime.combine(date, time, timezone(timedelta(hours=-4 if tz.upper() == 'EDT' else -5))))
    if observed:
        if set(observed) != {start, end}:
            raise ValueError('Robinhood round dates do not match app round: '
                             + ', '.join(value.isoformat() for value in observed))
    else:
        days = {parse_date(day) for day in re.findall(r'Event day\s+' + date_pattern, text, re.I)}
        if not days:
            raise ValueError('Cannot verify the Robinhood round date yet.')
        if days != {local_start.date()}:
            raise ValueError(f'Robinhood event date does not match app round {local_start.date()}.')
    return target


def parse_contract_prices(text, target):
    """Read only cent quotes immediately following the matching target label."""
    text = normalize_event_text(text)
    matches = re.findall(r'\$([\d,]+(?:\.\d+)?)\s+or above\s+(\d+(?:\.\d+)?)\s*¢', text, re.I)
    prices = [float(price) for label, price in matches if float(label.replace(',', '')) == target]
    if not prices:
        raise ValueError('Current Yes contract price is not visible.')
    # Select a contract is the actionable quote when the header also shows a quote.
    selected = re.split(r'Select a contract', text, flags=re.I)
    if len(selected) > 1:
        quotes = re.findall(r'\$([\d,]+(?:\.\d+)?)\s+or above\s+(\d+(?:\.\d+)?)\s*¢', selected[-1], re.I)
        preferred = [float(price) for label, price in quotes if float(label.replace(',', '')) == target]
        if preferred:
            prices = preferred
    if len(set(prices)) != 1 or not 0 <= prices[0] <= 100:
        raise ValueError('Current Yes contract price is invalid or ambiguous.')
    yes = prices[0]
    return {'yes_cents': yes, 'no_cents': round(100 - yes + 1, 4), 'no_is_estimate': True}


def open_login_browser():
    """Human login in ordinary Chrome, without an automation connection."""
    if sys.platform.startswith('linux'):
        if os.geteuid() == 0:
            raise RuntimeError('Chrome login cannot run as root. Use a regular Linux user and connect with ssh -X.')
        if not os.environ.get('DISPLAY'):
            raise RuntimeError('No display available. Connect with ssh -X from a computer with an X server, or use a remote desktop for login.')
    chrome = next((shutil.which(name) for name in
                   ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser')
                   if shutil.which(name)), None)
    if chrome is None:
        raise RuntimeError('Chrome is not installed.')
    if (PROFILE / 'SingletonLock').is_symlink() or (PROFILE / 'SingletonLock').exists():
        raise RuntimeError('Close the Robinhood browser and stop app.py first, then retry --login.')
    return subprocess.Popen([chrome, '--user-data-dir=' + str(PROFILE),
                             '--new-window', EVENT_URL])


def prediction_side(direction):
    return {"UP / ABOVE": "Buy Yes", "DOWN / BELOW": "Buy No"}.get(direction)


def limit_cents(raw):
    # The Contracts form displays Limit Price in cents. Do not infer dollars.
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:¢|c|cents)?\s*", raw, re.I)
    if not match:
        raise ValueError('Cannot read Limit Price in cents.')
    value = float(match.group(1))
    if not 0 < value <= 100:
        raise ValueError('Invalid Limit Price in cents.')
    return value


def all_models_agree(prediction):
    expected = prediction.get("direction")
    if expected not in ("UP / ABOVE", "DOWN / BELOW"):
        return False
    models = prediction.get("model_directions", {})
    return all(models.get(name) == expected for name in
               ("Final prediction", "Pattern", "Recent", "Bootstrap", "Trend"))


def purchase_direction(prediction):
    settings = order_settings.validate(prediction.get("order_settings", order_settings.DEFAULTS))
    final = prediction.get("direction")
    models = prediction.get("model_directions", {})
    valid = ("UP / ABOVE", "DOWN / BELOW")
    if final not in valid or models.get("Final prediction") != final:
        return None
    basis = settings["agreement_basis"]
    if basis == "both" and prediction.get("live_direction") != final:
        return None
    candidates = []
    if basis in ("final", "either", "both"):
        candidates.append(final)
    if basis in ("live", "either"):
        candidates.append(prediction.get("live_direction"))
    for direction in candidates:
        if direction in valid and all(models.get(name) == direction for name in settings["required_models"]):
            return direction
    return None


def in_purchase_window(settings, start, end):
    if start is None or end is None:
        return False
    now = datetime.now().astimezone()
    elapsed = (now - start).total_seconds() / 60
    return start <= now < end and settings['purchase_minute_from'] <= elapsed < settings['purchase_minute_to']


def prediction_matches(current, expected):
    if not current:
        return False
    def conditions(value):
        result = {k: v for k, v in value.items()
                  if k not in ('purchase_exists', 'purchase', 'live_snapshot', 'contract_price_history', 'contract_prices', 'buy_accuracy_guard', 'buy_choppiness_guard', 'entry_stability')}
        live = value.get('live_snapshot')
        # Updates to confidence/timestamps do not change the order's direction.
        # A missing snapshot still invalidates the draft (stale recommendation).
        result['live_snapshot'] = {'direction': live.get('direction')} if live else None
        return result
    return conditions(current) == conditions(expected)


def live_strength_matches(prediction):
    if not prediction:
        return False
    snapshot = prediction.get('live_snapshot') or {}
    confidence = snapshot.get('confidence')
    if confidence is None and isinstance(snapshot.get('p_up'), (int, float)):
        confidence = max(snapshot['p_up'], 1 - snapshot['p_up'])
    if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
            or not math.isfinite(confidence) or not .5 <= confidence <= 1):
        return False
    edge = confidence - .5
    rank = 0 if edge < .01 else 1 if edge < .02 else 2 if edge < .05 else 3 if edge < .10 else 4
    minimum = (prediction.get('order_settings') or {}).get('min_live_strength', 'MODERATE')
    return minimum in order_settings.LIVE_STRENGTHS and rank >= order_settings.LIVE_STRENGTHS.index(minimum)


def effective_test_mode(prediction):
    """Use virtual entries while the enabled accuracy guard pauses real buys."""
    settings = (prediction or {}).get('order_settings') or {}
    guard = (prediction or {}).get('buy_accuracy_guard') or {}
    return (settings.get('test_mode') is True
            or (settings.get('accuracy_buy_guard', {}).get('enabled') is True
                and guard.get('allowed') is False))


def buy_accuracy_guard_allows(prediction):
    if not prediction:
        return False
    if (prediction.get('order_settings') or {}).get('test_mode') is True:
        return True
    enabled = (prediction.get('order_settings') or {}).get('accuracy_buy_guard', {}).get('enabled', False)
    return not enabled or (prediction.get('buy_accuracy_guard') or {}).get('allowed') is True


def buy_accuracy_guard_reason(prediction):
    guard = prediction.get('buy_accuracy_guard') or {}
    accuracy = guard.get('accuracy')
    current = f"{accuracy:.2f}%" if accuracy is not None else 'no settled predictions'
    threshold = ((prediction.get('order_settings') or {}).get('accuracy_buy_guard') or {}).get('min_accuracy_pct', 60)
    hours = ((prediction.get('order_settings') or {}).get('accuracy_buy_guard') or {}).get('window_hours', 24)
    return f'Buy paused: Final prediction accuracy over the last {hours:g}h is {current}; required >= {threshold}%.'


def buy_choppiness_guard_allows(prediction):
    if not prediction:
        return False
    config = (prediction.get('order_settings') or {}).get('choppiness_buy_guard', {})
    enabled = any(config.get(key) for key in ('enabled', 'current_enabled', 'block_next_round'))
    return not enabled or (prediction.get('buy_choppiness_guard') or {}).get('allowed') is True


def buy_choppiness_guard_reason(prediction):
    guard = prediction.get('buy_choppiness_guard') or {}
    config = (prediction.get('order_settings') or {}).get('choppiness_buy_guard') or {}
    source = 'BTC' if config.get('source', 'bitcoin') == 'bitcoin' else 'Yes'
    rounds = config.get('rounds', 3)
    limit = config.get('current_limit', 7)
    if guard.get('current_blocked'):
        return f"Buy paused: current {source} round reached {guard['current_crossings']} midpoint crossings (limit >= {limit})."
    if guard.get('previous_blocked'):
        return (f"Buy paused: previous {source} round reached {guard['previous_crossings']} midpoint crossings "
                f"(limit >= {limit}); the following round is blocked until {guard.get('blocked_until', 'round end')}.")
    average = guard.get('average_score')
    if average is None:
        return (f"Buy paused: Midpoint crossings ({source}) needs {rounds} completed rounds with adequate quotes "
                f"({guard.get('ready_rounds', 0)}/{rounds} ready).")
    return (f"Buy paused: average Midpoint crossings ({source}) {average:.2f} over the last {rounds} completed rounds "
            f"exceeds {config.get('max_score', 3):g}.")


def buy_prediction_matches(current, expected, fallback_rule=None, *, force_buy=False):
    if effective_test_mode(current) != effective_test_mode(expected):
        return False
    if not effective_test_mode(current) and not buy_accuracy_guard_allows(current):
        return False
    if not buy_choppiness_guard_allows(current):
        return False
    if not force_buy and ((current or {}).get('order_settings') or {}).get('entry_stability', {}).get('enabled'):
        direction = current.get('live_direction') if fallback_rule is not None else purchase_direction(current)
        if not entry_stability.allows(current, direction, datetime.now().astimezone()):
            return False
    if fallback_rule is not None:
        if not current or current.get('purchase_exists'):
            return False
        brti, target = current.get('brti_price'), current.get('target')
        threshold = fallback_rule['min_distance_dollars']
        skip_gap = threshold is None and fallback_rule.get('_allow_missing_brti')
        if not isinstance(target, (int, float)) or not math.isfinite(target):
            return False
        direction = current.get('live_direction')
        if direction not in ('UP / ABOVE', 'DOWN / BELOW'):
            return False
        if not skip_gap:
            if not isinstance(brti, (int, float)) or isinstance(brti, bool) or not math.isfinite(brti):
                return False
            distance = (brti - target) * (1 if direction == 'UP / ABOVE' else -1)
            if threshold is not None and distance <= threshold:
                return False
        # A moving quote is valid while the fallback distance still qualifies.
        current = dict(current, brti_price=expected.get('brti_price'))
    return prediction_matches(current, expected) and live_strength_matches(current)


def buy_prediction_change_reason(current, expected, *, force_buy=False):
    if not current:
        return 'Buy waiting: current prediction became unavailable while preparing the form.'
    if not effective_test_mode(current) and not buy_accuracy_guard_allows(current):
        return buy_accuracy_guard_reason(current)
    if not buy_choppiness_guard_allows(current):
        return buy_choppiness_guard_reason(current)
    stability_enabled = not force_buy and (current.get('order_settings') or {}).get('entry_stability', {}).get('enabled')
    direction = purchase_direction(current) if stability_enabled else None
    if not force_buy and not entry_stability.allows(current, direction, datetime.now().astimezone()):
        return 'Buy waiting: BTC must remain on the buy side of the target for the configured stability duration, with fresh uninterrupted quotes.'
    if not live_strength_matches(current):
        return 'Buy waiting: Live recommendation became stale or fell below the minimum strength while preparing the form.'
    ignored = {'purchase_exists', 'purchase', 'live_snapshot', 'contract_price_history', 'contract_prices', 'buy_accuracy_guard', 'buy_choppiness_guard', 'entry_stability'}
    changed = sorted(key for key in (set(current) | set(expected)) - ignored
                     if current.get(key) != expected.get(key))
    current_live, expected_live = current.get('live_snapshot'), expected.get('live_snapshot')
    if (bool(current_live) != bool(expected_live)
            or (current_live or {}).get('direction') != (expected_live or {}).get('direction')):
        changed.append('Live recommendation direction/availability')
    detail = ', '.join(changed) or 'purchase conditions'
    return f'Buy waiting: prediction changed while preparing the form ({detail}); retrying.'


def contract_price_rising(prediction, direction, start, end, observed_prices=None):
    """Require a positive fitted price/time slope over the configured number of changes."""
    if not prediction or direction not in ('UP / ABOVE', 'DOWN / BELOW'):
        return False
    key = 'yes_cents' if direction == 'UP / ABOVE' else 'no_cents'
    values = []
    previous_time = None
    for sample in prediction.get('contract_price_history') or []:
        try:
            stamp = datetime.fromisoformat(sample['observed_at'])
            value = sample[key]
            if (sample.get('target') != prediction.get('target') or not start <= stamp < end
                    or (previous_time is not None and stamp < previous_time)
                    or not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value)):
                return False
        except (KeyError, TypeError, ValueError):
            return False
        previous_time = stamp
        if not values or value != values[-1][1]:
            values.append(((stamp - start).total_seconds(), value))
    quote = observed_prices if observed_prices is not None else prediction.get('contract_prices')
    if not quote:
        return False
    latest = quote.get(key)
    if not isinstance(latest, (int, float)) or isinstance(latest, bool) or not math.isfinite(latest):
        return False
    if not values or latest != values[-1][1]:
        values.append(((datetime.now().astimezone() - start).total_seconds(), latest))
    changes = (prediction.get('order_settings') or {}).get('contract_trend_changes', 10)
    if isinstance(changes, bool) or not isinstance(changes, int) or changes < 1:
        return False
    if len(values) < changes + 1:
        return False
    window = values[-(changes + 1):]
    mean_time = sum(t for t, _ in window) / len(window)
    mean_price = sum(price for _, price in window) / len(window)
    # The regression denominator is positive for distinct observation times;
    # the covariance therefore determines whether the overall trend rises.
    covariance = sum((t - mean_time) * (price - mean_price) for t, price in window)
    return covariance > 0


def review_buy_details(page, prediction, direction, contracts, price, time_in_force):
    side = 'Yes' if direction == 'UP / ABOVE' else 'No'
    matches = []
    for button in page.get_by_role('button', name='Buy', exact=True).all():
        if not button.is_visible() or not button.is_enabled():
            continue
        panel = button.locator("xpath=ancestor::*[contains(translate(string(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ ', 'abcdefghijklmnopqrstuvwxyz'), 'ordersummary')][1]")
        if panel.count() != 1:
            continue
        text = normalize_event_text(panel.inner_text())
        label = re.search(r'\b(Yes|No)\s*[·•]\s*\$([\d,]+(?:\.\d+)?)\s+or above', text, re.I)
        qty = re.search(r'\bContracts\s+(\d+)\b', text, re.I)
        clean = re.sub(r'Bid\s+(?:[\d.]+\s*¢|[–—-])\s*[·•]\s*Ask\s+(?:[\d.]+\s*¢|[–—-])', '', text, flags=re.I)
        limit = re.search(r'\bLimit Price\s+(\d+(?:\.\d+)?)\s*¢', clean, re.I)
        tif = r'(?:Immediate or cancel|IOC)' if time_in_force == 'ioc' else r'Good till date\b'
        if (label and label[1].lower() == side.lower() and float(label[2].replace(',', '')) == prediction['target']
                and qty and int(qty[1]) == int(contracts) and limit and float(limit[1]) == price
                and re.search(r'Time in Force\s+'+tif, text, re.I)):
            matches.append(button)
    return len(matches) == 1


def verify_contract_price_trend(page, prediction, direction, asset, start, end, report):
    if not prediction:
        return False
    try:
        if read_event_target(page, asset, start, end) != prediction.get('target'):
            return False
        text = page.locator('body').inner_text()
        try:
            prices = parse_contract_prices(text, prediction['target'])
        except ValueError:
            # The review replaces the target-adjacent quote with Bid / Ask.
            side = 'Yes' if direction == 'UP / ABOVE' else 'No'
            normalized = normalize_event_text(text)
            label = re.search(r'\b(Yes|No)\s*[·•]\s*\$([\d,]+(?:\.\d+)?)\s+or above', normalized, re.I)
            asks = re.findall(r'\bAsk\s+(\d+(?:\.\d+)?)\s*¢', normalized, re.I)
            if (not re.search(r'Order\s*Summary', normalized, re.I) or not label
                    or label[1].lower() != side.lower() or float(label[2].replace(',', '')) != prediction['target']
                    or len(set(asks)) != 1 or not 0 <= float(asks[0]) <= 100):
                raise ValueError('Cannot uniquely verify the review Ask quote.')
            ask = float(asks[0])
            prices = {'yes_cents':ask if side == 'Yes' else 101-ask,
                      'no_cents':ask if side == 'No' else 101-ask}
    except ValueError:
        report('Buy waiting: cannot verify the current contract quote for the rising-price condition.')
        return False
    allowed = contract_price_rising(prediction, direction, start, end, prices)
    if not allowed:
        report(f"Buy waiting: the overall price trend over the last {(prediction.get('order_settings') or {}).get('contract_trend_changes', 10)} contract changes must be upward.")
    return allowed


def clear_contract_quantity(quantity):
    # Review screens replace the input with static text. Do not wait for an
    # input that no longer exists when aborting a draft before submission.
    if quantity.count() == 1 and quantity.is_visible() and quantity.is_enabled():
        quantity.fill('', timeout=3000)


def apply_limit_price(limit, price):
    """IOC may lock this field; verify the form price instead of forcing it."""
    shown = limit.input_value(timeout=3000) or limit.get_attribute('placeholder', timeout=3000) or ''
    if limit_cents(shown) == price:
        return True
    if limit.is_enabled():
        limit.fill(f'{price:g}', timeout=3000)
    shown = limit.input_value(timeout=3000) or limit.get_attribute('placeholder', timeout=3000) or ''
    return limit_cents(shown) == price


def select_time_in_force(page, value, *, menu_page=None):
    from playwright.sync_api import expect, TimeoutError as PlaywrightTimeoutError
    selector = page.get_by_role('combobox', name=re.compile(r'^Time in force$', re.I))
    pattern = re.compile(r'Immediate\s+(?:or cancel|o\b)|\bIOC\b', re.I) if value == 'ioc' else re.compile(r'Good till dat(?:e)?\b', re.I)
    title = re.compile(r'^\s*Immediate\s+or\s+cancel(?:\s*\(IOC\))?\s*$', re.I) if value == 'ioc' else re.compile(r'^\s*Good till date\s*$', re.I)
    if selector.count() != 1 or not selector.is_visible():
        raise ValueError('Time in force selector is not visible; order not submitted.')
    if not pattern.search(selector.inner_text(timeout=3000)):
        selector.click(timeout=3000)
        # Dropdown menus may be portalled under body, outside the held-side panel.
        root = menu_page if menu_page is not None else page
        options = root.locator('[role="option"]:visible, [role="menuitem"]:visible, [role="menuitemradio"]:visible, [role="radio"]:visible').filter(has_text=pattern)
        try:
            options.first.wait_for(state='visible', timeout=1000)
        except PlaywrightTimeoutError:
            options = root.get_by_text(title)
            options.first.wait_for(state='visible', timeout=3000)
        visible = [option for option in options.all() if option.is_visible() and option.is_enabled()]
        if len(visible) != 1:
            raise ValueError('Cannot uniquely identify the requested Time in force option.')
        visible[0].click(timeout=3000)
    # Allow the selected value to update asynchronously before filling the sale.
    try:
        expect(selector).to_have_text(pattern, timeout=3000)
    except AssertionError:
        raise ValueError('Could not verify Time in force; order not submitted.') from None


def select_buy_in_contracts(page):
    from playwright.sync_api import expect, TimeoutError as PlaywrightTimeoutError
    selector = page.get_by_role('combobox', name='Buy In', exact=True)
    if selector.inner_text(timeout=3000).strip() == 'Contracts':
        return selector
    selector.click(timeout=3000)
    pattern = re.compile(r'^\s*Contracts\s*$', re.I)
    options = page.locator('[role="option"]:visible, [role="menuitem"]:visible, [role="menuitemradio"]:visible, [role="radio"]:visible').filter(has_text=pattern)
    try:
        options.first.wait_for(state='visible', timeout=1000)
    except PlaywrightTimeoutError:
        options = page.get_by_text('Contracts', exact=True)
        options.first.wait_for(state='visible', timeout=3000)
    visible = [option for option in options.all() if option.is_visible() and option.is_enabled()]
    if len(visible) != 1:
        raise ValueError('Cannot uniquely identify Contracts in the Buy In menu; order not submitted.')
    visible[0].click(timeout=3000)
    try:
        expect(selector).to_have_text(pattern, timeout=3000)
    except AssertionError:
        raise ValueError('Could not verify Buy In: Contracts; order not submitted.') from None
    return selector


def prepare_contract_form(page, asset, start, end, get_prediction, report, record_purchase=None, fallback=False, fallback_index=0, force_buy=False):
    """Submit under the configured conditions and verify the fill receipt."""
    prediction = get_prediction(asset, start, end)
    if not prediction:
        report('Buy waiting: prediction for the current asset and round is not ready.')
        return False
    if not effective_test_mode(prediction) and not buy_accuracy_guard_allows(prediction):
        report(buy_accuracy_guard_reason(prediction))
        return False
    if not buy_choppiness_guard_allows(prediction):
        report(buy_choppiness_guard_reason(prediction))
        return False
    if prediction.get('purchase_exists'):
        report('Buy skipped: this round already has a purchase or recorded attempt.')
        return False
    settings = order_settings.validate(prediction.get("order_settings", order_settings.DEFAULTS))
    if fallback and fallback_index < len(settings['fallbacks']):
        rule = settings['fallbacks'][fallback_index]
        overrides = {key: rule[key] for key in ('contract_trend_changes', 'min_live_strength')}
        original_get_prediction = get_prediction

        def get_prediction(*args):
            current = original_get_prediction(*args)
            if not current:
                return current
            current_settings = current.get('order_settings') or {}
            current_rules = current_settings.get('fallbacks', [current_settings.get('fallback', {})])
            selected = current_rules[fallback_index] if fallback_index < len(current_rules) else {}
            effective = {key: selected.get(key, current_settings.get(key, order_settings.DEFAULTS[key]))
                         for key in overrides}
            return dict(current, order_settings=dict(current_settings, **effective))

        prediction = get_prediction(asset, start, end)
        if not prediction:
            return False
        settings = dict(settings, **overrides)
    if not force_buy and not fallback and not settings["primary_enabled"]:
        report("Primary disabled.")
        return False
    if force_buy:
        fb = dict(settings['force_buy'], _allow_missing_brti=True)
        if not prediction.get('force_buy_request') or not fb['enabled']:
            report('Force Buy waiting: enable the option and press Force Buy first.')
            return False
        direction = prediction.get('live_direction')
        if direction not in ('UP / ABOVE', 'DOWN / BELOW'):
            report('Force Buy waiting: a fresh Live direction is required.')
            return False
        threshold = fb['min_distance_dollars']
        brti, target = prediction.get('brti_price'), prediction.get('target')
        if threshold is not None:
            if not isinstance(brti, (int, float)) or isinstance(brti, bool) or not math.isfinite(brti):
                report('Force Buy waiting: fresh BRTI price is required for the distance condition.')
                return False
            gap = (brti-target)*(1 if direction == 'UP / ABOVE' else -1)
            if gap <= threshold:
                report(f'Force Buy waiting: directional BRTI gap {gap:.2f} dollars must be above {threshold:g} dollars.')
                return False
        settings = dict(settings, min_price_cents=fb['min_price_cents'],max_price_cents=fb['max_price_cents'],
                        contracts=fb['contracts'],purchase_minute_from=fb['minute_from'],purchase_minute_to=fb['minute_to'],
                        required_models=[],agreement_basis='live',time_in_force=fb['time_in_force'],
                        entry_stability=dict(settings['entry_stability'], enabled=False))
    elif fallback:
        rules = settings['fallbacks']
        if fallback_index >= len(rules):
            report('Buy skipped: this fallback rule was removed from settings.')
            return False
        fb = rules[fallback_index]
        settings = dict(settings, fallback=fb)
        brti, target = prediction.get('brti_price'), prediction.get('target')
        direction = prediction.get('live_direction')
        if not fb['enabled'] or asset != 'BTC' or not isinstance(brti, (int, float)) or not math.isfinite(brti) or not isinstance(target, (int, float)):
            report('Buy waiting: fallback needs an enabled BTC rule and fresh BRTI/target data.')
            return False
        distance = brti - target
        if direction not in ('UP / ABOVE', 'DOWN / BELOW'):
            report('Buy waiting: a fresh Live direction is not available for the fallback rule.')
            return False
        directional_distance = distance if direction == 'UP / ABOVE' else -distance
        if fb['min_distance_dollars'] is not None and directional_distance < 0 and fb['min_distance_dollars'] >= 0:
            report(f"Buy waiting: BRTI gap {distance:+.2f} dollars is opposite to Live {direction}; fallback requires more than {fb['min_distance_dollars']:g} dollars in that direction.")
            return False
        if fb['min_distance_dollars'] is not None and directional_distance <= fb['min_distance_dollars']:
            report(f"Buy waiting: BRTI gap in Live {direction} direction is {directional_distance:.2f} dollars; fallback requires more than {fb['min_distance_dollars']:g} dollars.")
            return False
        settings = dict(settings, min_price_cents=fb['min_price_cents'], max_price_cents=fb['max_price_cents'],
                        contracts=fb['contracts'], purchase_minute_from=fb['minute_from'], purchase_minute_to=fb['minute_to'],
                        required_models=fb['required_models'], agreement_basis='live')
        if any(prediction.get('model_directions', {}).get(name) != direction
               for name in fb['required_models']):
            report('Order skipped: selected fallback models must all agree with the fresh Live recommendation.')
            return False
    else:
        direction = purchase_direction(prediction)
    if direction is None:
        report('Order skipped: selected models must all agree with the selected Final prediction / fresh Live recommendation basis.')
        return False
    if not force_buy and not entry_stability.allows(prediction, direction, datetime.now().astimezone()):
        value = prediction.get('entry_stability') or {}
        report(f"Buy waiting: BTC stability {value.get('seconds', 0):.1f}s; requires {settings['entry_stability']['seconds']:g}s on the {direction} side of the target with fresh quotes.")
        return False
    if not prediction.get("live_snapshot"):
        report("Waiting for a fresh Live recommendation before preparing the order.")
        return False
    if not live_strength_matches(prediction):
        report(f"Buy waiting: Live minute recommendation strength must be at least {settings['min_live_strength']}.")
        return False
    if not in_purchase_window(settings, start, end):
        report(f"Waiting for purchase window: minute {settings['purchase_minute_from']:g} to {settings['purchase_minute_to']:g} after round start.")
        return False
    if not contract_price_rising(prediction, direction, start, end):
        report(f"Buy waiting: need an upward overall trend over {settings['contract_trend_changes']} contract changes and a fresh quote.")
        return False
    min_price = settings["min_price_cents"]
    max_price = settings["max_price_cents"]
    contracts = str(settings["contracts"])
    side = prediction_side(direction)
    if not side:
        report('Buy skipped: no valid contract side for the current direction.')
        return False
    if effective_test_mode(prediction):
        price = (prediction.get('contract_prices') or {}).get(
            'yes_cents' if direction == 'UP / ABOVE' else 'no_cents')
        if not isinstance(price, (int, float)) or isinstance(price, bool) or not min_price < price < max_price:
            report('Test buy waiting: current quote must be inside the configured price range.')
            return False
        current = get_prediction(asset, start, end)
        if not buy_prediction_matches(current, prediction, fb if fallback or force_buy else None, force_buy=force_buy):
            return False
        stamp = datetime.now().astimezone()
        purchase = {
            'status': 'attempted', 'test_mode': True, 'direction': direction,
            'test_mode_reason': 'manual' if settings['test_mode'] else 'accuracy_pause',
            'target': prediction['target'], 'contracts': int(contracts),
            'limit_price_cents': price, 'execution_price_cents': price,
            'attempted_at': stamp.isoformat(), 'submitted_at': stamp.isoformat(),
            'purchase_minute': round((stamp-start).total_seconds()/60, 2),
            'purchase_rule': 'force_buy' if force_buy else 'fallback' if fallback else 'primary',
            'fallback_rule_number': fallback_index+1 if fallback else None,
            'conditions': dict(settings, rule='force_buy' if force_buy else 'fallback' if fallback else 'primary'),
            'live_recommendation': prediction['live_snapshot'],
            'simulation_price_source': 'displayed_contract_quote',
            'btc_price': prediction.get('brti_price'),
        }
        if not record_purchase or record_purchase(asset, start, end, purchase) is False:
            return False
        if record_purchase(asset, start, end, {
                'target': prediction['target'], 'status': 'filled', 'test_mode': True,
                'confirmed_by': 'simulation', 'filled_at': stamp.isoformat(),
                'filled_quantity': int(contracts), 'receipt_contracts': int(contracts)}) is False:
            return False
        report(f'Test filled: {side}, {contracts} virtual contracts at {price:g}¢.')
        return True
    target = read_event_target(page, asset, start, end)
    if target != prediction["target"]:
        raise ValueError('Browser target does not match the frozen prediction.')
    selected = page.get_by_role('button', name=side, exact=True)
    if not selected.is_enabled():
        report(f'Buy waiting: Robinhood {side} button is disabled.')
        return False
    selected.click(timeout=3000)
    buy_in = select_buy_in_contracts(page)
    quantity_name = re.compile(r'^Contracts\b', re.I)
    quantity = page.get_by_role('textbox', name=quantity_name).or_(
        page.get_by_role('spinbutton', name=quantity_name))
    if quantity.count() != 1 or not quantity.is_visible() or not quantity.is_enabled():
        report('Buy waiting: the Contracts input is missing, ambiguous or disabled; the buy form may not be open.')
        return False
    limit = page.get_by_role('textbox', name=re.compile(r'^Limit Price'))
    price = limit_cents(limit.input_value(timeout=3000) or limit.get_attribute('placeholder', timeout=3000) or '')
    if not min_price < price < max_price:
        report(f'{side}: Limit Price {price:g}¢; waiting for a price above {min_price:g}¢ and below {max_price:g}¢.')
        return False
    if not in_purchase_window(settings, start, end):
        report('Buy waiting: purchase window closed while preparing the form.')
        return False
    current_prediction = get_prediction(asset, start, end)
    if not buy_prediction_matches(current_prediction, prediction, fb if fallback or force_buy else None, force_buy=force_buy):
        report(buy_prediction_change_reason(current_prediction, prediction, force_buy=force_buy))
        return False
    select_time_in_force(page, settings['time_in_force'])
    if quantity.count() != 1 or not quantity.is_visible() or not quantity.is_enabled():
        report('Buy waiting: Contracts input changed or became unavailable after selecting Time in force.')
        return False
    quantity.fill(contracts, timeout=3000)
    price = limit_cents(limit.input_value(timeout=3000) or limit.get_attribute('placeholder', timeout=3000) or '')
    if not in_purchase_window(settings, start, end) or not min_price < price < max_price or not buy_prediction_matches(get_prediction(asset, start, end), prediction, fb if fallback or force_buy else None, force_buy=force_buy):
        clear_contract_quantity(quantity)
        report('Draft cleared because the price or round changed.')
        return False
    price_verified = apply_limit_price(limit, price)
    current_prediction = get_prediction(asset, start, end)
    reason = None
    if not in_purchase_window(settings, start, end):
        reason = 'Buy not submitted: purchase window closed while preparing the form.'
    elif not price_verified:
        reason = 'Buy not submitted: form limit price could not be verified.'
    elif not buy_prediction_matches(current_prediction, prediction, fb if fallback or force_buy else None, force_buy=force_buy):
        reason = buy_prediction_change_reason(current_prediction, prediction, force_buy=force_buy)
    if reason:
        clear_contract_quantity(quantity)
        report(reason)
        return False
    if quantity.input_value(timeout=3000) != contracts or buy_in.inner_text().strip() != 'Contracts':
        raise ValueError('Could not verify the contract quantity.')
    if not in_purchase_window(settings, start, end):
        clear_contract_quantity(quantity)
        report('Draft cleared: purchase window closed before order review.')
        return False
    if not verify_contract_price_trend(page, get_prediction(asset, start, end), direction, asset, start, end, report):
        clear_contract_quantity(quantity)
        return False
    page.get_by_role(
    'button', name='Review order', exact=True).click(timeout=3000)
    page.get_by_role('button', name='Buy', exact=True).wait_for(state='visible', timeout=3000)
    if not review_buy_details(page, prediction, direction, contracts, price, settings['time_in_force']):
        report('Buy not submitted: could not verify review side, target, quantity, limit and Time in force.')
        page.reload(wait_until='domcontentloaded', timeout=60000)
        return False
    purchase = {"status": "attempted", "direction": direction,
                "direction_source": "live_recommendation" if direction != prediction["direction"] else "final_prediction",
                "target": target, "contracts": int(contracts), "limit_price_cents": price,
                "purchase_rule": "force_buy" if force_buy else "fallback" if fallback else "primary",
                "fallback_rule_number": fallback_index + 1 if fallback else None,
                "fallback_brti_price": prediction.get("brti_price") if fallback else None,
                "conditions": dict({key: settings[key] for key in
                               ("min_price_cents", "max_price_cents", "contracts", "required_models", "agreement_basis", "purchase_minute_from", "purchase_minute_to", "time_in_force", "min_live_strength", "entry_stability")}, contract_trend_changes=settings["contract_trend_changes"], contract_trend_method="linear_regression", rule="force_buy" if force_buy else "fallback" if fallback else "primary", fallback=settings["fallback"] if fallback else None, fallback_rule_number=fallback_index + 1 if fallback else None),
                "source_url": page.url,
                "attempted_at": datetime.now().astimezone().isoformat(),
                "live_recommendation": prediction["live_snapshot"]}
    if force_buy:
        purchase["conditions"]["force_buy"] = dict(settings["force_buy"])
    if not in_purchase_window(settings, start, end) or not buy_prediction_matches(get_prediction(asset, start, end), prediction, fb if fallback or force_buy else None, force_buy=force_buy):
        clear_contract_quantity(quantity)
        report('Buy review cancelled: purchase window, Live strength, direction or settings changed before submission.')
        page.reload(wait_until='domcontentloaded', timeout=60000)
        return False
    submitted_at = datetime.now().astimezone()
    if not verify_contract_price_trend(page, get_prediction(asset, start, end), direction, asset, start, end, report):
        clear_contract_quantity(quantity)
        page.reload(wait_until='domcontentloaded', timeout=60000)
        return False
    # Only journal an attempt once all review checks passed, just before Buy.
    snapshot = get_prediction(asset, start, end) or {}
    if not buy_prediction_matches(snapshot, prediction, fb if fallback or force_buy else None, force_buy=force_buy):
        clear_contract_quantity(quantity)
        report(buy_prediction_change_reason(snapshot, prediction, force_buy=force_buy))
        page.reload(wait_until='domcontentloaded', timeout=60000)
        return False
    btc = snapshot.get('brti_price')
    valid_btc = (asset == 'BTC' and snapshot.get('target') == target
                 and isinstance(btc, (int, float)) and not isinstance(btc, bool)
                 and math.isfinite(btc) and btc > 0)
    purchase['btc_price'] = btc if valid_btc else None
    purchase['btc_target_diff_dollars'] = btc - target if valid_btc else None
    purchase['attempted_at'] = datetime.now().astimezone().isoformat()
    if record_purchase and record_purchase(asset, start, end, purchase) is False:
        raise ValueError("Could not save the purchase conditions and Live snapshot; order not submitted.")
    page.get_by_role(
        'button', name='Buy', exact=True
    ).click(timeout=3000)
    if record_purchase:
        record_purchase(asset, start, end, {"status": "submitted", "target": target,
                                           "submitted_at": submitted_at.isoformat(),
                                           "purchase_minute": round((submitted_at - start).total_seconds() / 60, 2)})
        if observe_filled_order(page, asset, start, end, record_purchase):
            report(f'Filled: {side}, Contracts {contracts}, Limit Price {price:g}¢.')
            return True
    report(f'Submitted: {side}, Contracts {contracts}; waiting for Done and full Filled quantity.')
    return True


def observe_filled_order(page, asset, start, end, record_purchase):
    done = page.get_by_role('button', name='Done', exact=True)
    if not any(control.is_visible() for control in done.all()):
        return False
    text = normalize_event_text(page.locator('body').inner_text())
    quantities = re.findall(r'\bFilled quantity\s*:?\s*(\d+)\s+of\s+(\d+)\b', text, re.I)
    if len(quantities) != 1:
        return False
    filled, requested = map(int, quantities[0])
    if filled <= 0 or filled != requested:
        return False
    target = read_event_target(page, asset, start, end)
    saved = record_purchase(asset, start, end, {"status": "filled", "target": target,
                            "confirmed_by": "visible_order_receipt",
                            "filled_quantity": filled, "receipt_contracts": requested})
    if saved:
        done.first.click(timeout=3000)
        observe_owned_contracts(page, asset, start, end, record_purchase)
    return bool(saved)


def observe_owned_contracts(page, asset, start, end, record_purchase):
    """Read the position after Done; never click Close or Close all."""
    if observe_positions_section(page, asset, start, end, record_purchase):
        return True
    close_all = page.get_by_role('button', name=re.compile(r'^Close all$', re.I))
    if not any(control.is_visible() for control in close_all.all()):
        return False
    # Keep line boundaries to distinguish contract quantity from cash available.
    text = page.locator('body').inner_text()
    quantities = re.findall(r'^\s*(\d+)\s+available\s*$', text, re.I | re.M)
    if len(quantities) != 1 or int(quantities[0]) <= 0:
        return False
    target = read_event_target(page, asset, start, end)
    return bool(record_purchase(asset, start, end, {
        'target': target, 'position_confirmed': True,
        'available_contracts': int(quantities[0]),
        'position_confirmed_by': 'visible_close_all_and_available_quantity'}))


def observe_positions_section(page, asset, start, end, record_purchase):
    text = normalize_event_text(page.locator('body').inner_text())
    if 'Your positions' not in text:
        return False
    positions = text.split('Your positions', 1)[1]
    rows = re.findall(
        r'\$([\d,]+(?:\.\d+)?)\s+or above\s+(\d+)\s+(Yes|No)\s*[·•]?\s*'
        + re.escape(asset) + r'\s+15\s*min\b[^$@]*?@\s*(\d+(?:\.\d+)?)\s*¢', positions, re.I)
    if not rows:
        return False
    target = read_event_target(page, asset, start, end)
    for label, quantity, side, price in rows:
        if (float(label.replace(',', '')) != target or int(quantity) <= 0
                or not 0 < float(price) <= 100):
            continue
        if record_purchase(asset, start, end, {
                'target': target, 'position_confirmed': True,
                'available_contracts': int(quantity),
                'position_direction': 'UP / ABOVE' if side.lower() == 'yes' else 'DOWN / BELOW',
                'execution_price_cents': float(price),
                'execution_price_source': 'your_positions_display',
                'position_confirmed_by': 'visible_your_positions'}):
            return True
    return False


def brti_delta_below(prediction, threshold):
    """Compare a fresh snapshot's gap in the held direction; None disables it."""
    if threshold is None:
        return True
    brti, target = prediction.get('brti_price'), prediction.get('target')
    direction = (prediction.get('purchase') or {}).get('direction')
    valid = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if not valid(brti) or not valid(target) or direction not in ('UP / ABOVE', 'DOWN / BELOW'):
        return False
    gap = brti-target if direction == 'UP / ABOVE' else target-brti
    return gap < threshold


def trade_exit_settings(prediction):
    """Use the exits captured for this purchase, independent of fallback order."""
    settings = order_settings.validate(prediction['order_settings'])
    purchase = prediction.get('purchase') or {}
    conditions = purchase.get('conditions') or {}
    fallback = conditions.get('fallback') or {}
    if conditions.get('rule') == 'fallback' or purchase.get('purchase_rule') == 'fallback':
        for key in ('close_conditions', 'stop_loss'):
            if key in fallback:
                settings[key] = fallback[key]
        # Normalize captured legacy price-range exits to the current profit schema.
        settings = order_settings.validate(dict(settings, fallbacks=[]))
    return settings


def estimated_close_profit(purchase, price_cents):
    """Use the same fee-adjusted whole-trade P/L as the open-position display."""
    if (not isinstance(price_cents, (int, float)) or isinstance(price_cents, bool)
            or not math.isfinite(price_cents) or not 0 < price_cents <= 100):
        return None
    return round_history.trade_pnl(dict(purchase, close_order={
        'status': 'filled', 'contracts': purchase.get('contracts'),
        'execution_price_cents': price_cents}), None)


def estimated_close_profit_per_ct_cents(purchase, price_cents):
    from decimal import Decimal
    net = estimated_close_profit(purchase, price_cents)
    if net is None:
        return None
    return float(Decimal(str(net)) * 100 / Decimal(str(purchase['contracts'])))


def close_conditions_match(prediction, price_cents, start, end, now=None):
    """Match only confirmed holdings, their direction and the configured window."""
    if not prediction or start is None or end is None:
        return False
    purchase = prediction.get('purchase') or {}
    if (purchase.get('ignore_early_profit')
            or purchase.get('purchase_rule') == 'force_buy'
            or (purchase.get('conditions') or {}).get('rule') == 'force_buy'):
        return False
    if purchase.get('status') != 'filled' or purchase.get('target') != prediction.get('target'):
        return False
    if purchase.get('direction') not in ('UP / ABOVE', 'DOWN / BELOW'):
        return False
    close = trade_exit_settings(prediction)['close_conditions']
    now = now or datetime.now().astimezone()
    minute = (now - start).total_seconds() / 60
    profit = estimated_close_profit_per_ct_cents(purchase, price_cents)
    return (close['enabled'] and start <= now < end
            and close['minute_from'] <= minute < close['minute_to']
            and isinstance(price_cents, (int, float)) and not isinstance(price_cents, bool)
            and math.isfinite(price_cents)
            and not purchase.get('close_order')
            and profit is not None and profit > close.get('profit_per_ct_cents', 0)
            and price_cents > close.get('ct_price_above_cents', 0)
            and brti_delta_below(prediction, close['max_brti_delta_dollars']))


def stop_loss_matches(prediction, price, start, end, now=None):
    if not prediction or start is None or end is None:
        return False
    purchase = prediction.get('purchase') or {}
    if (purchase.get('ignore_stop_loss')
            or purchase.get('purchase_rule') == 'force_buy'
            or (purchase.get('conditions') or {}).get('rule') == 'force_buy'):
        return False
    now = now or datetime.now().astimezone()
    if (purchase.get('status') != 'filled' or purchase.get('target') != prediction.get('target')
            or purchase.get('direction') not in ('UP / ABOVE', 'DOWN / BELOW')
            or not start <= now < end):
        return False
    stop = trade_exit_settings(prediction)['stop_loss']
    peak = purchase.get('stop_loss_peak_bid_cents')
    valid = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if not brti_delta_below(prediction, stop['max_brti_delta_dollars']):
        return False
    minute = (now-start).total_seconds()/60
    return (stop['enabled'] and stop['minute_from'] <= minute < stop['minute_to'] and valid(peak) and 0 < peak <= 100 and valid(price) and 0 < price <= 100
            and price <= peak - stop['drop_cents'] and price < stop['max_price_cents'])


def force_exit_matches(prediction, price, start, end, now, key):
    if not prediction or start is None or end is None:
        return False
    purchase = prediction.get('purchase') or {}
    rule = order_settings.validate(prediction['order_settings'])[key]
    now = now or datetime.now().astimezone()
    minute = (now-start).total_seconds()/60
    return (rule['enabled'] and purchase.get('status') == 'filled'
            and not purchase.get('close_order') and purchase.get('target') == prediction.get('target')
            and purchase.get('direction') in ('UP / ABOVE', 'DOWN / BELOW')
            and start <= now < end and rule['minute_from'] <= minute < rule['minute_to']
            and isinstance(price, (int, float)) and not isinstance(price, bool)
            and math.isfinite(price) and rule['min_price_cents'] < price <= 100
            and brti_delta_below(prediction, rule['max_brti_delta_dollars']))


def force_close_matches(prediction, price, start, end, now=None):
    purchase = (prediction or {}).get('purchase') or {}
    token = purchase.get('attempted_at') or purchase.get('submitted_at')
    return bool(token and purchase.get('force_close_token') == token
                and force_exit_matches(prediction, price, start, end, now, 'force_close'))


def sale_conditions_match(prediction, price, start, end, now=None):
    return force_close_matches(prediction, price, start, end, now) or stop_loss_matches(prediction, price, start, end, now) or close_conditions_match(prediction, price, start, end, now)


def prepare_fallback_forms(page, asset, start, end, get_prediction, report, record_purchase):
    prediction = get_prediction(asset, start, end)
    if not prediction:
        return False
    settings = order_settings.validate(prediction['order_settings'])
    reasons = []
    for index, rule in enumerate(settings['fallbacks']):
        if not rule['enabled']:
            continue
        messages = []
        if prepare_contract_form(page, asset, start, end, get_prediction, messages.append,
                record_purchase, fallback=True, fallback_index=index):
            if messages:
                report(f'Fallback {index+1}: {messages[-1]}')
            return True
        reasons.append(f"Fallback {index+1}: {messages[-1] if messages else 'No qualifying entry.'}")
    if reasons:
        report('\n'.join(reasons))
    return False


def observe_close_receipt(page, asset, start, end, get_prediction, record_purchase, report):
    prediction = get_prediction(asset, start, end)
    closing = (prediction or {}).get('purchase', {}).get('close_order') or {}
    if closing.get('status') not in ('attempted', 'submitted'):
        return False
    done = page.get_by_role('button', name='Done', exact=True)
    if not any(control.is_visible() for control in done.all()):
        return False
    quantities = re.findall(r'\bFilled quantity\s*:?\s*(\d+)\s+of\s+(\d+)\b',
                            normalize_event_text(page.locator('body').inner_text()), re.I)
    if len(quantities) != 1:
        return False
    filled, total = map(int, quantities[0])
    if filled <= 0 or filled != total or total != closing.get('contracts'):
        return False
    target = read_event_target(page, asset, start, end)
    if not record_purchase(asset, start, end, {'target': target, 'close_order': {
            'status': 'filled', 'filled_quantity': filled, 'receipt_contracts': total,
            'confirmed_by': 'visible_done_and_filled_quantity',
            'filled_at': datetime.now().astimezone().isoformat()}}):
        return False
    done.first.click(timeout=3000)
    report(f'Close confirmed: {filled} contracts filled.')
    return True


def close_review_controls(page):
    """Distinguish the Close tab from the final Close button in a review."""
    controls = []
    for button in page.get_by_role('button', name=re.compile(r'^Close$', re.I)).all():
        if not button.is_visible():
            continue
        review = button.locator("xpath=ancestor::*[contains(translate(string(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ ', 'abcdefghijklmnopqrstuvwxyz'), 'ordersummary')][1]")
        if review.count() == 1 and re.search(r'Order\s*Summary', review.inner_text(), re.I):
            # A Close tab outside the review may have the whole page as its
            # matching ancestor. Only the review's own single Close qualifies.
            local_buttons = review.get_by_role('button', name=re.compile(r'^Close$', re.I))
            if sum(control.is_visible() for control in local_buttons.all()) != 1:
                continue
            controls.append((button, review))
    return controls


def submit_close_review(page, asset, start, end, prediction, report, record_purchase, get_prediction, contracts=None, price=None):
    purchase = prediction.get('purchase') or {}
    if purchase.get('test_mode') or (prediction.get('order_settings') or {}).get('test_mode'):
        return False
    target = prediction['target']
    side = 'Yes' if purchase['direction'] == 'UP / ABOVE' else 'No'
    settings = trade_exit_settings(prediction)['close_conditions']
    controls = close_review_controls(page)
    if len(controls) != 1:
        report(f'Close not submitted: found {len(controls)} final Close buttons in the review; expected one.')
        return False
    if not controls[0][0].is_enabled():
        report('Close waiting: final Close button is disabled by Robinhood.')
        return False
    submit, review = controls[0]
    review_text = normalize_event_text(review.inner_text())
    review_text = re.sub(r'Bid\s+(?:[\d.]+\s*¢|[–—-])\s*[·•]\s*Ask\s+(?:[\d.]+\s*¢|[–—-])', '', review_text, flags=re.I)
    label = re.search(r'\b(Yes|No)\s*[·•]\s*\$([\d,]+(?:\.\d+)?)\s+or above', review_text, re.I)
    reviewed_quantity = re.search(r'\bContracts\s+(\d+)\b', review_text, re.I)
    reviewed_price = re.search(r'\bLimit Price\s+(\d+(?:\.\d+)?)\s*¢', review_text, re.I)
    if reviewed_quantity and contracts is None:
        contracts = purchase.get('contracts')
    if reviewed_price and price is None:
        price = float(reviewed_price[1])
    current = get_prediction(asset, start, end)
    forced = force_close_matches(current, price, start, end)
    stop_triggered = not forced and stop_loss_matches(current, price, start, end)
    if forced:
        settings = dict(order_settings.validate(current['order_settings'])['force_close'])
    if stop_triggered:
        settings = dict(settings, time_in_force=trade_exit_settings(current)['stop_loss']['time_in_force'])
    if (not label or label[1].lower() != side.lower()
            or float(label[2].replace(',', '')) != target
            or not reviewed_quantity or int(reviewed_quantity[1]) != contracts
            or not reviewed_price or float(reviewed_price[1]) != price
            or not re.search(r'Time in Force\s+' + (
                r'(?:Immediate or cancel|IOC)' if settings['time_in_force'] == 'ioc'
                else r'Good till date\b'), review_text, re.I)
            or not sale_conditions_match(current, price, start, end)
            or (current.get('order_settings') or {}).get('test_mode')
            or current.get('purchase') != purchase):
        report('Close not submitted: review details or conditions changed.')
        return False
    closing = {'status': 'attempted', 'contracts': contracts, 'direction': purchase['direction'],
               'limit_price_cents': price, 'time_in_force': settings['time_in_force'], 'conditions': settings,
               'reason': 'force_close' if forced else 'stop_loss' if stop_triggered else 'close_conditions',
               'stop_loss': dict(trade_exit_settings(current)['stop_loss'],
                                 peak_bid_cents=purchase.get('stop_loss_peak_bid_cents')) if stop_triggered else None,
               'attempted_at': datetime.now().astimezone().isoformat()}
    btc = current.get('brti_price')
    valid_btc = (asset == 'BTC' and current.get('target') == target
                 and isinstance(btc, (int, float)) and not isinstance(btc, bool)
                 and math.isfinite(btc) and btc > 0)
    closing['btc_price'] = btc if valid_btc else None
    closing['btc_target_diff_dollars'] = btc - target if valid_btc else None
    if not record_purchase(asset, start, end, {'target': target, 'close_order': closing}):
        return False
    submit.click(timeout=3000)
    record_purchase(asset, start, end, {'target': target, 'close_order': {
        'status': 'submitted', 'submitted_at': datetime.now().astimezone().isoformat()}})
    report(f'Close submitted: {"Stop loss · " if stop_triggered else ""}all {contracts} {side} contracts at {price:g}¢; awaiting fill confirmation.')
    observe_close_receipt(page, asset, start, end, get_prediction, record_purchase, report)
    return True


def prepare_virtual_close(asset, start, end, prediction, get_prediction, report, record_purchase):
    """Simulate exits using displayed quotes, without touching broker controls."""
    purchase = prediction['purchase']
    if purchase.get('status') != 'filled' or purchase.get('close_order'):
        return False
    price = (prediction.get('contract_prices') or {}).get(
        'yes_cents' if purchase['direction'] == 'UP / ABOVE' else 'no_cents')
    if (not isinstance(price, (int, float)) or isinstance(price, bool)
            or not math.isfinite(price) or not 0 < price <= 100):
        return False
    target = purchase['target']
    peak = purchase.get('stop_loss_peak_bid_cents')
    if peak is None or price > peak:
        if record_purchase(asset, start, end, {'target': target,
                'stop_loss_peak_bid_cents': price,
                'stop_loss_peak_bid_at': datetime.now().astimezone().isoformat()}) is False:
            return False
        prediction = get_prediction(asset, start, end)
        if not prediction:
            return False
    if not sale_conditions_match(prediction, price, start, end):
        report(f'Test holding: virtual contract quote {price:g}¢; waiting for exit conditions.')
        return False
    stamp = datetime.now().astimezone().isoformat()
    contracts = purchase['contracts']
    closing = {'status': 'attempted', 'test_mode': True, 'contracts': contracts,
               'direction': purchase['direction'], 'limit_price_cents': price,
               'execution_price_cents': price, 'attempted_at': stamp,
               'reason': 'force_close' if force_close_matches(prediction, price, start, end)
                         else 'stop_loss' if stop_loss_matches(prediction, price, start, end)
                         else 'close_conditions', 'simulation_price_source': 'displayed_contract_quote'}
    if record_purchase(asset, start, end, {'target': target, 'close_order': closing}) is False:
        return False
    if record_purchase(asset, start, end, {'target': target, 'close_order': {
            'status': 'filled', 'filled_at': stamp, 'confirmed_by': 'simulation',
            'filled_quantity': contracts, 'receipt_contracts': contracts}}) is False:
        return False
    report(f'Test closed: {contracts} virtual contracts at {price:g}¢.')
    return True


def prepare_close_order(page, asset, start, end, get_prediction, report, record_purchase):
    prediction = get_prediction(asset, start, end)
    if not prediction:
        return False
    purchase = prediction.get('purchase') or {}
    if purchase.get('test_mode'):
        return prepare_virtual_close(asset, start, end, prediction, get_prediction, report, record_purchase)
    if (prediction.get('order_settings') or {}).get('test_mode'):
        return False
    if purchase.get('close_order'):
        if purchase['close_order'].get('status') == 'filled':
            return False
        if not observe_close_receipt(page, asset, start, end, get_prediction, record_purchase, report):
            closing = purchase['close_order']
            report(f"Close {closing.get('status', 'unconfirmed')}: waiting for Done and Filled quantity {closing.get('contracts')} of {closing.get('contracts')}; new buys are blocked until the full close is confirmed.")
        return True
    settings = trade_exit_settings(prediction)['close_conditions']
    stop = trade_exit_settings(prediction)['stop_loss']
    holding_monitoring = (purchase.get('status') == 'filled'
                       and purchase.get('target') == prediction.get('target')
                       and purchase.get('direction') in ('UP / ABOVE', 'DOWN / BELOW')
                       and start <= datetime.now().astimezone() < end)
    stop_monitoring = stop['enabled'] and not purchase.get('ignore_stop_loss') and holding_monitoring
    if not holding_monitoring:
        if settings['enabled'] and purchase.get('status') == 'filled':
            report(f"Close waiting: allowed minutes {settings['minute_from']:g}–{settings['minute_to']:g} after round start; current minute {(datetime.now().astimezone() - start).total_seconds() / 60:.2f}.")
        return False
    target = read_event_target(page, asset, start, end)
    side = 'Yes' if purchase['direction'] == 'UP / ABOVE' else 'No'
    if close_review_controls(page):
        return submit_close_review(page, asset, start, end, prediction, report,
                                   record_purchase, get_prediction)
    if not any(button.is_visible() for button in page.get_by_role('button', name=re.compile(r'^Close all$', re.I)).all()):
        for tab in page.get_by_text('Close', exact=True).all():
            if tab.is_visible() and tab.is_enabled():
                panel = tab.locator("xpath=ancestor::*[.//input and contains(string(.), 'or above')][1]")
                if panel.count() != 1:
                    report('Close waiting: Close tab is visible but its order form was not found.')
                    continue
                label = re.search(r'\b(Yes|No)\s*[·•]\s*\$([\d,]+(?:\.\d+)?)\s+or above',
                                  normalize_event_text(panel.inner_text()), re.I)
                if label and label[1].lower() == side.lower() and float(label[2].replace(',', '')) == target:
                    tab.click(timeout=3000)
                    report(f'Close: opened the {side} holding Close tab; checking available contracts and bid next.')
                    return False
                report(f'Close waiting: visible form does not match the held {side} contract at ${target:,.2f}.')
    # Require the held side and target in the same visible order panel.
    buttons = page.get_by_role('button', name=re.compile(r'^Close all$', re.I))
    panels = []
    for button in buttons.all():
        if button.is_visible():
            panel = button.locator("xpath=ancestor::*[.//input and contains(string(.), 'or above')][1]")
            if panel.count() != 1:
                continue
            text = normalize_event_text(panel.inner_text())
            label = re.search(r'\b(Yes|No)\s*[·•]\s*\$([\d,]+(?:\.\d+)?)\s+or above', text, re.I)
            if label and label[1].lower() == side.lower() and float(label[2].replace(',', '')) == target:
                panels.append((button, panel))
    if len(panels) != 1:
        report(f'Close waiting: the {side} holding Close panel is not visible.')
        return False
    close_all, panel = panels[0]
    text = normalize_event_text(panel.inner_text())
    quantity = re.search(r'\bContracts\s+(\d+)\s+available\b', text, re.I)
    bid = re.search(r'\bBid\s+(\d+(?:\.\d+)?)\s*¢', text, re.I)
    if not quantity or int(quantity[1]) <= 0 or not bid:
        report('Close waiting: available contracts or current bid are not visible.')
        return False
    contracts, price = int(quantity[1]), float(bid[1])
    if purchase.get('force_close_token') and contracts != purchase.get('contracts'):
        report('Force close waiting: available contracts do not match the recorded position.')
        return False
    if holding_monitoring:
        record_purchase(asset, start, end, {'target':target,'holding_quote':{
            'bid_cents':price,'direction':purchase['direction'],'available_contracts':contracts,
            'purchase_token':purchase.get('attempted_at') or purchase.get('submitted_at')}})
    if stop_monitoring and 0 < price <= 100:
        peak = purchase.get('stop_loss_peak_bid_cents')
        if not isinstance(peak, (int, float)) or isinstance(peak, bool) or not math.isfinite(peak) or price > peak:
            if not record_purchase(asset, start, end, {'target':target,
                    'stop_loss_peak_bid_cents':price, 'stop_loss_peak_bid_at':datetime.now().astimezone().isoformat()}):
                report('Close waiting: could not save the trailing stop peak.')
                return False
            prediction = get_prediction(asset, start, end)
            if not prediction:
                return False
            purchase = prediction.get('purchase') or {}
    if not sale_conditions_match(prediction, price, start, end):
        if stop_monitoring:
            peak = purchase.get('stop_loss_peak_bid_cents')
            threshold = peak - stop['drop_cents'] if isinstance(peak, (int, float)) and not isinstance(peak, bool) and math.isfinite(peak) else None
            report(f"Close waiting: {side} bid {price:g}¢; stop loss requires bid at {threshold:g}¢ or lower AND strictly below {stop['max_price_cents']:g}¢, within minutes {stop['minute_from']:g}–{stop['minute_to']:g}." if threshold is not None else 'Close waiting: trailing stop loss needs an observed peak bid.')
        else:
            profit = estimated_close_profit_per_ct_cents(purchase, price)
            shown_profit = f'{profit:.4f}¢/CT' if profit is not None else 'unavailable'
            report(f"Close waiting: {side} bid {price:g}¢; estimated net profit {shown_profit}, required strictly above {settings.get('profit_per_ct_cents', 0):g}¢/CT AND bid strictly above {settings.get('ct_price_above_cents', 0):g}¢ within minutes {settings['minute_from']:g}–{settings['minute_to']:g}.")
        return False
    quantity_name = re.compile(r'^Contracts\b', re.I)
    quantity_input = panel.get_by_role('textbox', name=quantity_name).or_(
        panel.get_by_role('spinbutton', name=quantity_name))
    limit_name = re.compile(r'^Limit Price', re.I)
    limit = panel.get_by_role('textbox', name=limit_name).or_(
        panel.get_by_role('spinbutton', name=limit_name))
    if quantity_input.count() != 1 or limit.count() != 1:
        report('Close waiting: could not uniquely identify Contracts and Limit Price inputs.')
        return False
    stop_triggered = stop_loss_matches(prediction, price, start, end)
    forced = force_close_matches(prediction, price, start, end)
    tif = order_settings.validate(prediction['order_settings'])['force_close']['time_in_force'] if forced else stop['time_in_force'] if stop_triggered else settings['time_in_force']
    select_time_in_force(panel, tif, menu_page=page)
    quantity_input.fill(str(contracts), timeout=3000)
    if quantity_input.input_value(timeout=3000) != str(contracts):
        report('Close skipped: could not verify all available contracts.')
        return False
    price_verified = apply_limit_price(limit, price)
    current = get_prediction(asset, start, end)
    if (not sale_conditions_match(current, price, start, end)
            or current.get('purchase') != purchase
            or not price_verified):
        clear_contract_quantity(quantity_input)
        return False
    close_all.click(timeout=3000)
    page.get_by_text(re.compile(r'^Order\s*Summary$', re.I)).first.wait_for(state='visible', timeout=3000)
    return submit_close_review(page, asset, start, end, prediction, report,
                               record_purchase, get_prediction, contracts, price)


def clear_cancelled_ioc(page, asset, start, end, get_prediction, record_purchase, report):
    """Release only a verified zero-fill IOC after the confirmation window."""
    prediction = get_prediction(asset, start, end) or {}
    purchase = prediction.get('purchase') or {}
    pending = (purchase.get('status') == 'cancelled'
               and purchase.get('cancellation_reason') == 'ioc_cancelled_unfilled'
               and purchase.get('ioc_cleanup_pending'))
    if not pending:
        if (purchase.get('status') not in ('attempted', 'submitted')
                or purchase.get('test_mode')
                or purchase.get('direction') not in ('UP / ABOVE', 'DOWN / BELOW')
                or (purchase.get('conditions') or {}).get('time_in_force') != 'ioc'):
            return False
        try:
            stamps = [datetime.fromisoformat(purchase[key]) for key in ('attempted_at', 'submitted_at') if purchase.get(key)]
            if not stamps or (datetime.now().astimezone()-max(stamps)).total_seconds() < 10:
                return False
        except (ValueError, TypeError):
            return False
        if observe_filled_order(page, asset, start, end, record_purchase):
            return False
        text = page.locator('body').inner_text(timeout=5000)
        terminal = re.search(r'(?im)^\s*(?:Order(?: status)?\s*:?\s*)?(?:cancelled|canceled|expired|rejected)[.!]?\s*$', text)
        quantities = re.findall(r'\bFilled quantity\s*:?\s*(\d+)\s+of\s+(\d+)\b', text, re.I)
        side = 'Yes' if purchase.get('direction') == 'UP / ABOVE' else 'No'
        labels = re.findall(r'\b(Yes|No)\s*[·•]\s*\$([\d,]+(?:\.\d+)?)\s+or above', normalize_event_text(text), re.I)
        expected = (side.lower(), purchase.get('target'))
        if (not terminal or quantities != [('0', str(purchase.get('contracts')))]
                or {(s.lower(), float(t.replace(',', ''))) for s, t in labels} != {expected}
                or read_event_target(page, asset, start, end) != purchase.get('target')):
            return False
        if not record_purchase(asset, start, end, {
                'status': 'cancelled', 'target': purchase['target'],
                'cancellation_reason': 'ioc_cancelled_unfilled',
                'cancellation_scope': 'exchange_confirmed',
                'cancelled_at': datetime.now().astimezone().isoformat(),
                'expected_attempted_at': purchase.get('attempted_at'),
                'expected_submitted_at': purchase.get('submitted_at'),
                'filled_quantity': 0, 'receipt_contracts': purchase['contracts'],
                'confirmed_by': 'visible_cancelled_ioc_receipt', 'ioc_cleanup_pending': True}):
            return False
    # Refresh before releasing the record: an old receipt must not be reused.
    page.reload(wait_until='domcontentloaded', timeout=60000)
    if not record_purchase(asset, start, end, {
            'target': purchase['target'], 'expected_attempted_at': purchase.get('attempted_at'),
            'expected_submitted_at': purchase.get('submitted_at'),
            'ioc_cleanup_refreshed_at': datetime.now().astimezone().isoformat()}):
        return False
    report('IOC cancelled with zero filled contracts; cleared the app purchase record after 10 seconds. New buys can qualify again.')
    return True


def expire_unconfirmed_attempt(page, asset, start, end, get_prediction, record_purchase, report):
    prediction = get_prediction(asset, start, end)
    purchase = (prediction or {}).get('purchase') or {}
    pending_refresh = (purchase.get('status') == 'cancelled'
                       and purchase.get('cancellation_reason') == 'attempt_confirmation_timeout'
                       and purchase.get('timeout_refresh_pending'))
    if not pending_refresh:
        if purchase.get('status') != 'attempted':
            return False
        try:
            elapsed = (datetime.now().astimezone() - datetime.fromisoformat(purchase['attempted_at'])).total_seconds()
        except (KeyError, TypeError, ValueError):
            return False
        if elapsed < 30:
            return False
        # A visible full fill takes precedence over expiration.
        if observe_filled_order(page, asset, start, end, record_purchase):
            return False
        current = get_prediction(asset, start, end)
        latest = (current or {}).get('purchase') or {}
        if latest.get('status') != 'attempted' or latest.get('attempted_at') != purchase.get('attempted_at'):
            return False
        if not record_purchase(asset, start, end, {'target':prediction['target'],
                'status':'cancelled','cancellation_reason':'attempt_confirmation_timeout',
                'cancellation_scope':'app_record','cancelled_at':datetime.now().astimezone().isoformat(),
                'expected_attempted_at':purchase['attempted_at'],'timeout_refresh_pending':True}):
            return False
    report('Unconfirmed attempt timed out after 30 seconds; refreshing Robinhood and removing its purchase record.')
    page.reload(wait_until='domcontentloaded', timeout=60000)
    record_purchase(asset, start, end, {'target':prediction['target'],
        'timeout_refresh_pending':False,'timeout_refreshed_at':datetime.now().astimezone().isoformat()})
    return True


def refresh_removed_purchase(page, prediction, key, previous, report):
    revision = (prediction or {}).get('purchase_removal_revision', 0)
    closing = ((prediction or {}).get('purchase') or {}).get('close_order') or {}
    closed = closing.get('status') == 'filled'
    token = (*key, revision, closing.get('filled_at') if closed else None)
    if (revision or closed) and not prediction.get('purchase_exists') and token != previous:
        report('Refreshing Robinhood after removal or confirmed close before preparing another order...')
        page.reload(wait_until='domcontentloaded', timeout=60000)
        return token
    return previous


def recover_browser_timeout(page, exc, report):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    if not isinstance(exc, PlaywrightTimeoutError):
        return False
    report(f'Robinhood timeout: {exc}. Refreshing the page before rechecking order state...')
    try:
        page.reload(wait_until='domcontentloaded', timeout=60000)
    except Exception as reload_error:
        report(f'Robinhood refresh failed after timeout: {reload_error}')
    return True


def parse_available_balance(text):
    """Read the dollar amount labelled available, never contract quantity/cost."""
    normalized = normalize_event_text(text)
    amounts = re.findall(r'\$\s*([\d,]+(?:\.\d{1,2})?)\s*available\b', normalized, re.I)
    values = {float(value.replace(',', '')) for value in amounts}
    if len(values) != 1:
        return None
    amount = values.pop()
    return amount if math.isfinite(amount) and amount >= 0 else None


def read_available_balance(page):
    # Robinhood places available buying power in the footer above Predict.
    amounts = set()
    for footer in page.get_by_text('Predict', exact=True).all():
        if not footer.is_visible():
            continue
        panel = footer.locator("xpath=ancestor::*[contains(translate(string(.), 'AVAILABLE', 'available'), 'available')][1]")
        if panel.count() == 1:
            amount = parse_available_balance(panel.inner_text())
            if amount is not None:
                amounts.add(amount)
    if len(amounts) == 1:
        return amounts.pop()
    if amounts:
        return None
    pattern = re.compile(r'^\s*\$\s*[\d,]+(?:\.\d{1,2})?\s*available\s*$', re.I)
    for label in page.get_by_text(pattern).all():
        if label.is_visible():
            amount = parse_available_balance(label.inner_text())
            if amount is not None:
                amounts.add(amount)
    return amounts.pop() if len(amounts) == 1 else None


def browser_loop(stop, snapshot, accept, report, get_prediction, record_purchase, record_prices=None, *, headless=False, record_balance=None):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE), channel='chrome', headless=headless,
            no_viewport=not headless,
            **({'viewport': {'width': 1920, 'height': 1080}} if headless else {}))
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(startup_event_url(), wait_until='domcontentloaded', timeout=60000)
        attempted = None
        prepared = None
        refreshed_removal = None
        while not stop.is_set():
            asset, start, end, has_target, upcoming = snapshot()
            try:
                if page.is_closed():
                    report('Robinhood browser was closed. Restart the app to reopen it.')
                    break
                if login_required(page.locator('body').inner_text(timeout=5000)):
                    report('Robinhood login required: the app browser is signed out. Sign in using the app browser profile to resume contract prices.')
                    stop.wait(5)
                    continue
                if record_balance:
                    record_balance(read_available_balance(page))
                key = (asset, start)
                if open_current_event(page, stop, report):
                    attempted = key
                    continue
                if attempted != key:
                    refresh_or_open_current(page, stop, report)
                    attempted = key
                    continue
                if refresh_pending_target(page, stop, report):
                    continue
                if record_prices:
                    try:
                        quote_target = read_event_target(page, asset, start, end)
                        prices = parse_contract_prices(page.locator('body').inner_text(), quote_target)
                        record_prices(asset, start, end, quote_target, prices)
                    except ValueError:
                        record_prices(asset, start, end, None, None)
                if has_target:
                    current = get_prediction(asset, start, end)
                    if clear_cancelled_ioc(page, asset, start, end, get_prediction, record_purchase, report):
                        prepared = None
                        refreshed = get_prediction(asset, start, end) or {}
                        refreshed_removal = (*key, refreshed.get('purchase_removal_revision', 0), None)
                        continue
                    removal = refresh_removed_purchase(page, current, key, refreshed_removal, report)
                    if removal != refreshed_removal:
                        refreshed_removal = removal
                        prepared = None
                        continue
                    if current and not current.get('purchase_exists'):
                        prepared = None
                    if prepare_close_order(page, asset, start, end, get_prediction, report, record_purchase):
                        stop.wait(0.5)
                        continue
                    if current and current.get('purchase_exists') and not (current.get('purchase') or {}).get('test_mode') and observe_filled_order(page, asset, start, end, record_purchase):
                        prepared = key
                    if current and current.get('purchase_exists') and not (current.get('purchase') or {}).get('test_mode'):
                        observe_owned_contracts(page, asset, start, end, record_purchase)
                    if current and current.get('purchase_exists'):
                        # Keep the close diagnostic visible instead of replacing
                        # it with a redundant "Buy skipped" message every loop.
                        stop.wait(0.5)
                        continue
                    if not upcoming and current and current.get('force_buy_request'):
                        messages = []
                        if prepare_contract_form(page, asset, start, end, get_prediction, messages.append,
                                                 record_purchase, force_buy=True):
                            prepared = key
                        if messages: report('Force Buy: '+messages[-1])
                        stop.wait(0.5)
                        continue
                    if not upcoming and prepared != (asset, start):
                        messages = []
                        if prepare_contract_form(page, asset, start, end, get_prediction, messages.append, record_purchase):
                            prepared = key
                            if messages: report(messages[-1])
                        else:
                            primary = messages[-1] if messages else 'No qualifying entry.'
                            messages = []
                            if prepare_fallback_forms(page, asset, start, end, get_prediction, messages.append, record_purchase):
                                prepared = key
                                if messages: report(messages[-1])
                            else:
                                report('Primary: '+primary+('\n'+messages[-1] if messages else ''))
                    stop.wait(0.5)
                    continue
                try:
                    target = read_event_target(page, asset, start, end)
                except ValueError as exc:
                    report(f'Target not recorded: {exc} Refreshing/checking current event in 5 seconds.')
                    if not stop.wait(5):
                        refresh_or_open_current(page, stop, report, asset=asset, start=start, end=end)
                    continue
                if not accept(target, asset, start, end, page.url):
                    report(f'Read target {target:,.2f}, but round/asset changed or target was already set; retrying.')
            except Exception as exc:
                if recover_browser_timeout(page, exc, report):
                    prepared = None
                else:
                    report(f'Robinhood: {exc}')
            stop.wait(0.5)
        context.close()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Set up Robinhood login in ordinary Chrome.')
    parser.add_argument('--login', action='store_true', help='Open Chrome for manual login before starting app.py')
    options = parser.parse_args()
    if options.login:
        try:
            open_login_browser()
        except RuntimeError as exc:
            parser.exit(1, str(exc) + '\n')
        print('Sign in manually, then close this Chrome window and start app.py.')
    else:
        parser.print_help()
