"""Isolated BTC five-minute companion; exchange prices are not Predict quotes."""
from datetime import datetime, timezone, timedelta
import math
import threading
import time

import numpy as np
from flask import Blueprint, jsonify, request, render_template
import brti_engine as core
import crypto_website

MARKET_URL = 'https://web.crypto.com/explore/predict/crypto/btc/5-min'
blueprint = Blueprint('crypto_predict', __name__)


def round_bounds(now):
    start = datetime.fromtimestamp(int(now.timestamp()) // 300 * 300, timezone.utc)
    return start, start + timedelta(minutes=5)


def probability_above(candles, start, end, now, price, target):
    # All training observations precede this round, including candle close time.
    rows = sorted((c for c in candles if c['time'] + 60 <= start.timestamp()),
                  key=lambda c: c['time'])
    returns = [math.log(b['close'] / a['close']) for a, b in zip(rows, rows[1:])
               if b['time'] - a['time'] == 60
               and a['close'] > 0 and b['close'] > 0]
    if len(returns) < 30:
        raise ValueError('Not enough contiguous pre-round one-minute history.')
    minutes = max(0, (end - now).total_seconds()) / 60
    if minutes == 0:
        raise ValueError('Round has ended.')
    # A repeatable empirical bootstrap over only the remaining five-minute horizon.
    rng = np.random.default_rng(int(start.timestamp()))
    whole = int(minutes)
    draws = rng.choice(returns, size=(12000, whole + 1))
    paths = draws[:, :whole].sum(axis=1) + draws[:, whole] * math.sqrt(minutes - whole)
    return float(np.mean(price * np.exp(paths) >= target)), len(returns)


class CryptoMonitor:
    def __init__(self):
        self.lock = threading.RLock()
        self.target = None
        self.target_round = None
        self.target_source = None
        self.quote = None
        self.candles = []
        self.history_at = 0
        self.checked_at = 0
        self.error = None

    def refresh(self, now):
        if time.monotonic() - self.checked_at < 1:
            return
        self.checked_at = time.monotonic()
        try:
            data = core.http_get_json(core.CRYPTOCOM_TICKER,
                                     params={'instrument_name': 'BTC_USD'}, timeout=8)
            if data.get('code') != 0:
                raise ValueError('Crypto.com ticker API rejected the request.')
            row = next(r for r in data['result']['data'] if r['i'] == 'BTC_USD')
            stamp = datetime.fromtimestamp(int(row['t']) / 1000, timezone.utc)
            price = float(row['a'])
            if not math.isfinite(price) or price <= 0:
                raise ValueError('Invalid Crypto.com exchange price.')
            self.quote = {'price': price, 'time': stamp}
            if not self.candles or time.monotonic() - self.history_at > 60:
                candles = core.fetch_cryptocom_candles(now - timedelta(hours=5), now, 'BTC')
                if not candles:
                    raise ValueError('Crypto.com returned no minute history.')
                self.candles = candles
                self.history_at = time.monotonic()
            self.error = None
        except Exception as exc:
            self.error = str(exc)

    def snapshot(self):
        with self.lock:
            now = datetime.now(timezone.utc)
            self.refresh(now)
            now = datetime.now(timezone.utc)
            start, end = round_bounds(now)
            if self.target_round != start.isoformat():
                self.target = None
                self.target_round = None
                self.target_source = None
            market, browser_error = crypto_website.READER.read()
            if market and market['round_start'] == start.isoformat():
                self.target = market['target']
                self.target_round = market['round_start']
                self.target_source = 'Crypto.com Predict website'
            quote = self.quote
            fresh = bool(quote and -5 <= (now - quote['time']).total_seconds() <= 20)
            result = {
                'server_time': now.isoformat(), 'round_start': start.isoformat(),
                'round_end': end.isoformat(), 'market_url': MARKET_URL,
                'price': quote['price'] if quote else None,
                'price_time': quote['time'].isoformat() if quote else None,
                'price_fresh': fresh, 'target': self.target,
                'target_source': self.target_source,
                'contract_prices': market.get('contract_prices') if market and market['round_start'] == start.isoformat() and market.get('contract_prices') and 0 <= (now-datetime.fromisoformat(market['contract_prices']['read_at'])).total_seconds() <= 15 else None,
                'contract_error': market.get('contract_error') if market and market['round_start'] == start.isoformat() else 'Waiting for current round contract page.',
                'target_reader_status': browser_error or ('reading' if not market else 'connected'),
                'target_url': market['url'] if market and market['round_start'] == start.isoformat() else None,
                'price_source': 'Crypto.com Exchange BTC/USD (proxy, not Predict settlement)',
                'automation_status': 'target_reader_enabled_order_form_unavailable',
                'error': self.error, 'prediction': None,
            }
            if quote and not fresh:
                result['error'] = result['error'] or 'Exchange quote is stale; prediction paused.'
            if self.target is not None and fresh and not self.error:
                try:
                    p, samples = probability_above(self.candles, start, end, now,
                                                   quote['price'], self.target)
                    result['prediction'] = {'p_up': round(p * 100, 2),
                                            'p_down': round((1 - p) * 100, 2),
                                            'direction': 'UP' if p >= .5 else 'DOWN',
                                            'training_returns': samples}
                except ValueError as exc:
                    result['error'] = str(exc)
            return result


monitor = CryptoMonitor()


@blueprint.get('/crypto-com')
def home():
    return render_template('crypto_predict.html', market_url=MARKET_URL)


@blueprint.get('/api/crypto-com/state')
def state():
    return jsonify(monitor.snapshot())


@blueprint.post('/api/crypto-com/target')
def target():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error='Expected a JSON object.'), 400
    try:
        if isinstance(data.get('target'), bool):
            raise ValueError()
        value = float(data.get('target'))
        if not math.isfinite(value) or value <= 0:
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        return jsonify(error='Target must be a finite positive price.'), 400
    with monitor.lock:
        start, _ = round_bounds(datetime.now(timezone.utc))
        if data.get('round_start') != start.isoformat():
            return jsonify(error='Round changed. Refresh and enter the new round target.'), 409
        monitor.target, monitor.target_round = value, start.isoformat()
        monitor.target_source = 'manual'
    return jsonify(ok=True)
