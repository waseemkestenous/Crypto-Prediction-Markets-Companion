"""Midpoint crossings on fixed, causal 15-second observations.

Score = strict side changes around Yes 50 cents or the BTC round target.
Exact touches and the first observed side are not crossings. Missing sampling
instants are skipped. Range is displayed separately and never gates buying.
"""
import math
from datetime import datetime, timedelta


SAMPLE_SECONDS = 15
MAX_QUOTE_AGE_SECONDS = 30
METRIC = 'midpoint_crossings'


def score(samples, start, end, target, asof, source='contract_yes'):
    """Score Bitcoin or Yes, excluding future/out-of-round observations.

    A completed 15-minute round has 60 sampling instants, 15..900 seconds.
    A quote must be at most 30 seconds old at its instant. Missing instants
    are skipped, including leading/trailing gaps; they are never filled
    with stale prices. Count side changes among the remaining sampled prices.
    Current-round scores need at least four instants and remain provisional.
    """
    if source not in ('bitcoin', 'contract_yes'):
        raise ValueError('Choppiness source must be bitcoin or contract_yes.')
    bitcoin = source == 'bitcoin'
    unit = 'dollars' if bitcoin else 'cents'
    complete = asof >= end
    until = min(asof, end)
    ticks = max(0, int((until - start).total_seconds() // SAMPLE_SECONDS))
    midpoint = target if bitcoin else 50.
    result = {'status': 'insufficient_data', 'score': None, 'source': source, 'range_cents': None,
              'metric': METRIC, 'midpoint': midpoint, 'crossings_up': None, 'crossings_down': None,
              'observed_crossings': None,
              'range_dollars': None, 'total_movement_dollars': None, 'net_movement_dollars': None,
              'total_movement_cents': None, 'net_movement_cents': None,
              'sample_count': 0, 'expected_samples': ticks, 'missing_samples': ticks,
              'sample_seconds': SAMPLE_SECONDS, 'complete': complete,
              'round_start': start.isoformat(), 'round_end': end.isoformat()}
    if (not isinstance(target, (int, float)) or isinstance(target, bool)
            or not math.isfinite(target) or (bitcoin and target <= 0)):
        return result
    observations = {}
    for sample in samples:
        try:
            stamp = datetime.fromisoformat(sample['observed_at'])
            price = sample['price' if bitcoin else 'yes_cents']
            if (start <= stamp < end and stamp <= until and (bitcoin or sample.get('target') == target)
                    and isinstance(price, (int, float)) and not isinstance(price, bool)
                    and math.isfinite(price) and (price > 0 if bitcoin else 0 <= price <= 100)):
                observations[stamp] = float(price)
        except (KeyError, TypeError, ValueError):
            continue
    observations = sorted(observations.items())
    values = []
    index = 0
    latest = None
    for tick in range(1, ticks + 1):
        instant = start + timedelta(seconds=tick * SAMPLE_SECONDS)
        while index < len(observations) and observations[index][0] <= instant:
            latest = observations[index]
            index += 1
        if latest is not None and (instant - latest[0]).total_seconds() <= MAX_QUOTE_AGE_SECONDS:
            values.append((tick, latest[1]))
    result['sample_count'] = len(values)
    result['missing_samples'] = ticks - len(values)
    prices = [value for _, value in values]
    previous_side = 0
    up = down = 0
    for price in prices:
        side = (price > midpoint) - (price < midpoint)
        if not side:
            continue
        if previous_side and side != previous_side:
            up += side > 0
            down += side < 0
        previous_side = side
    result.update(observed_crossings=up+down if values else None, crossings_up=up, crossings_down=down)
    if len(values) < 4:
        return result
    movement = math.fsum(abs(b-a) for a, b in zip(prices, prices[1:]))
    net = abs(prices[-1] - prices[0])
    result.update(status='ready', score=up+down, crossings_up=up, crossings_down=down)
    result.update({f'range_{unit}': max(prices)-min(prices),
                   f'total_movement_{unit}': movement, f'net_movement_{unit}': net})
    return result


def buy_guard(round_scores, config, current_round=None):
    """Require every requested completed round; never replace missing rounds."""
    required = config.get('rounds', 3)
    average_enabled = config.get('enabled', False)
    current_enabled = config.get('current_enabled', False)
    next_enabled = config.get('block_next_round', False)
    limit = config.get('current_limit', 7)
    enabled = average_enabled or current_enabled or next_enabled
    threshold = config.get('max_score', 3.)
    source = config.get('source', 'contract_yes')
    unit = 'dollars' if source == 'bitcoin' else 'cents'
    ready = [item for item in round_scores
             if item.get('status') == 'ready' and item.get('complete') and item.get('score') is not None
             and item.get('source', source) == source and item.get('metric') == METRIC]
    available = len(round_scores) == required and len(ready) == required
    average = math.fsum(item['score'] for item in ready) / required if available else None
    average_range = math.fsum(item[f'range_{unit}'] for item in ready) / required if available else None
    def observed(item):
        if not item or item.get('source', source) != source or item.get('metric') != METRIC:
            return None
        return item.get('observed_crossings', item.get('score'))
    current_count = observed(current_round)
    previous = round_scores[0] if round_scores else None
    previous_count = observed(previous) if previous and previous.get('complete') else None
    current_blocked = current_enabled and current_count is not None and current_count >= limit
    previous_blocked = next_enabled and previous_count is not None and previous_count >= limit
    average_blocked = average_enabled and (not available or average > threshold)
    return {'enabled': enabled, 'allowed': not (current_blocked or previous_blocked or average_blocked),
            'average_enabled': average_enabled, 'current_enabled': current_enabled, 'block_next_round': next_enabled,
            'current_limit': limit, 'current_crossings': current_count, 'previous_crossings': previous_count,
            'current_blocked': current_blocked, 'previous_blocked': previous_blocked, 'average_blocked': average_blocked,
            'max_score': threshold, 'required_rounds': required, 'ready_rounds': len(ready),
            'source': source, 'metric': METRIC, 'average_score': average,
            'average_range_cents': average_range if source == 'contract_yes' else None,
            'average_range_dollars': average_range if source == 'bitcoin' else None,
            'round_scores': round_scores, 'sample_seconds': SAMPLE_SECONDS}
