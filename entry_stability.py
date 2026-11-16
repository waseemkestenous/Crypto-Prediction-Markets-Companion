"""Observed same-side BTC persistence; gaps never count as confirmed stability."""
import math
from datetime import datetime

MAX_GAP_SECONDS = 10
DIRECTIONS = ('UP / ABOVE', 'DOWN / BELOW')


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def measure(samples, start, end, now, target):
    result = {'direction': None, 'seconds': 0.0, 'observed_at': None, 'target': target}
    if not start or not end or not start <= now < end or not finite(target):
        return result
    first = last = None
    side = 0
    for point in samples:
        try:
            stamp = datetime.fromisoformat(point['observed_at'])
            price = point['price']
            if not start <= stamp <= now or stamp >= end or not finite(price) or price <= 0:
                continue
            if last is not None and stamp <= last:
                continue
            current_side = (price > target) - (price < target)
            if (first is None or current_side != side or not current_side
                    or (last is not None and (stamp-last).total_seconds() > MAX_GAP_SECONDS)):
                first = stamp
            side, last = current_side, stamp
        except (KeyError, ValueError, TypeError):
            continue
    if last is None or (now-last).total_seconds() > MAX_GAP_SECONDS or not side:
        return result
    return dict(result, direction=DIRECTIONS[0] if side > 0 else DIRECTIONS[1],
                seconds=(last-first).total_seconds(), observed_at=last.isoformat())


def allows(prediction, direction, now):
    config = (prediction.get('order_settings') or {}).get('entry_stability') or {}
    if not config.get('enabled'):
        return True
    value = prediction.get('entry_stability') or {}
    try:
        age = (now-datetime.fromisoformat(value['observed_at'])).total_seconds()
        gap = prediction['brti_price']-prediction['target']
        return (direction in DIRECTIONS and value.get('direction') == direction
                and value.get('target') == prediction['target']
                and finite(prediction['brti_price']) and finite(prediction['target'])
                and gap*(1 if direction == DIRECTIONS[0] else -1) > 0
                and 0 <= age <= MAX_GAP_SECONDS and finite(value.get('seconds'))
                and value['seconds'] >= config.get('seconds', 60))
    except (KeyError, ValueError, TypeError):
        return False
