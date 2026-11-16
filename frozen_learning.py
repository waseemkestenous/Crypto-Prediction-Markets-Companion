"""Calibrate the next frozen decision using only previously settled rounds."""
import math
from datetime import datetime
import numpy as np
from live_learning import finite, fit, predict, scores, MIN_ROUNDS, MAX_ROUNDS, BLEND

COMPONENTS = (('p_pattern', 'Similar-pattern'), ('p_regime', 'Recent 3h regime'),
              ('p_bootstrap', 'Historical bootstrap'), ('p_trend', 'Trend/regime'))


def vector(base, probabilities):
    if not all(finite(p) and 0 <= p <= 1 for p in [base, *probabilities]):
        raise ValueError('Invalid recorded probability')
    clipped = float(np.clip(base, .02, .98))
    return np.asarray([math.log(clipped/(1-clipped)), *probabilities])


def apply(prediction, rows, asset, strength):
    """Return a new decision once, before publication; never mutate a frozen round."""
    result = dict(prediction)
    examples = {}
    excluded = 0
    for row in rows:
        try:
            start, end = (datetime.fromisoformat(row[key]) for key in ('round_start', 'round_end'))
            if (row.get('asset', asset) != asset or row.get('status') != 'settled'
                    or not start < end <= prediction['start']):
                continue
            target, settlement = row.get('target'), row.get('settlement')
            actual = row.get('actual')
            if (not finite(target) or target <= 0 or not finite(settlement) or settlement <= 0
                    or actual not in ('UP / ABOVE', 'DOWN / BELOW')
                    or (settlement >= target) != (actual == 'UP / ABOVE')):
                raise ValueError('Invalid settlement')
            # Train against the original model, avoiding repeated calibration feedback.
            base = (row.get('frozen_learning') or {}).get('base_p_up', row.get('p_up'))
            probabilities = [row['component_results'][label]['p_up'] for _, label in COMPONENTS]
            examples[start] = {'round': start.isoformat(), 'base': base,
                               'x': vector(base, probabilities), 'y': int(settlement >= target)}
        except (KeyError, TypeError, ValueError, OverflowError):
            excluded += 1
    samples = [examples[key] for key in sorted(examples)][-MAX_ROUNDS:]
    info = {'status': 'insufficient', 'applied': False, 'usable_rounds': len(samples),
            'excluded_rounds': excluded, 'minimum_rounds': MIN_ROUNDS,
            'base_p_up': prediction['p_up'], 'blend': BLEND}
    result['frozen_learning'] = info
    if len(samples) < MIN_ROUNDS:
        return result
    cut = int(len(samples)*.7)
    train, holdout = samples[:cut], samples[cut:]
    model = fit(train)
    baseline = scores(holdout, [e['base'] for e in holdout])
    candidate = scores(holdout, [predict(model, e['x'], e['base']) for e in holdout])
    active = (candidate['rounds'] >= 10 and candidate['brier'] < baseline['brier']-.002
              and candidate['log_loss'] < baseline['log_loss'] and candidate['accuracy'] >= baseline['accuracy'])
    probability = predict(model, vector(prediction['p_up'], [prediction[key] for key, _ in COMPONENTS]), prediction['p_up'])
    info.update(status='active' if active else 'shadow', baseline=baseline, candidate=candidate,
                candidate_p_up=probability, training_rounds=cut, validation_rounds=len(holdout),
                training_end=train[-1]['round'], validation_start=holdout[0]['round'])
    if active:
        direction = 'UP / ABOVE' if probability >= .5 else 'DOWN / BELOW'
        result.update(p_up=probability, p_down=1-probability, confidence=max(probability, 1-probability),
                      prediction=direction, strength=strength(probability),
                      component_agreement=sum((prediction[key] >= .5) == (probability >= .5) for key, _ in COMPONENTS))
        info['applied'] = True
    return result
