"""Bounded live confidence adjustment from synchronized BTC/Yes observations."""
import math
from datetime import datetime


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def apply(recommendation, points, quotes, start, end, strength):
    result = dict(recommendation)
    base = result['p_up']
    info = {'applied': False, 'reason': 'insufficient_data', 'base_p_up': base,
            'max_adjustment': .02, 'method': 'experimental_60s_market_confirmation'}
    result['market_support'] = info
    now = result['updated_at']
    if not start <= now < end or (now-start).total_seconds() < 60:
        return result
    prices = []
    for point in points:
        try:
            stamp = datetime.fromisoformat(point['observed_at'])
            if start <= stamp <= now and finite(point['price']):
                prices.append((stamp, point['price']))
        except (KeyError, TypeError, ValueError):
            continue
    prices.sort()
    if not prices or (now-prices[-1][0]).total_seconds() > 10:
        info['reason'] = 'stale_btc'
        return result
    pairs = {}
    for quote in quotes:
        try:
            stamp = datetime.fromisoformat(quote['observed_at'])
            if not 0 <= (now-stamp).total_seconds() <= 60 or not start <= stamp < end:
                continue
            target = quote.get('target')
            if not finite(target) or not math.isclose(target, result['current_price']-result['target_gap'], rel_tol=0, abs_tol=.005):
                continue
            cents = quote['yes_cents']
            if not finite(cents) or not 0 <= cents <= 100:
                continue
            btc = next((p for p in reversed(prices) if p[0] <= stamp), None)
            if btc and (stamp-btc[0]).total_seconds() <= 5:
                pairs[stamp] = (cents, btc[1])
        except (KeyError, ValueError, TypeError):
            continue
    paired = sorted(pairs.items())
    if len(paired) < 5 or (paired[-1][0]-paired[0][0]).total_seconds() < 45:
        return result
    if (now-paired[-1][0]).total_seconds() > 10:
        info['reason'] = 'stale_contract'
        return result
    values = [value for _, value in paired]
    mx = sum(x for x, _ in values)/len(values)
    my = sum(y for _, y in values)/len(values)
    xx = sum((x-mx)**2 for x, _ in values)
    yy = sum((y-my)**2 for _, y in values)
    xy = sum((x-mx)*(y-my) for x, y in values)
    if xx <= 0 or yy <= 0 or xy <= 0:
        info['reason'] = 'weak_relationship'
        return result
    r2 = xy*xy/(xx*yy)
    info.update(r2=r2, dollars_per_cent=xy/xx, samples=len(values))
    if r2 < .8 or max(x for x, _ in values)-min(x for x, _ in values) < 1:
        info['reason'] = 'weak_relationship'
        return result
    cents_move = values[-1][0]-values[0][0]
    btc_move = values[-1][1]-values[0][1]
    if abs(cents_move) < 1 or cents_move*btc_move <= 0:
        info['reason'] = 'no_clear_move'
        return result
    sign = 1 if base >= .5 else -1
    aligned = cents_move*sign > 0
    # At most two percentage points; never reverse the underlying recommendation.
    adjustment = min(.02, .02*r2*min(1, abs(cents_move)/3))
    confidence = max(base, 1-base)
    confidence = min(1.0, confidence+adjustment) if aligned else max(.500001, confidence-adjustment)
    probability = confidence if sign == 1 else 1-confidence
    info.update(applied=True, reason='aligned' if aligned else 'opposed',
                adjustment=probability-base, candidate_p_up=probability)
    result.update(p_up=probability, p_down=1-probability, confidence=confidence,
                  strength=strength(probability))
    return result
