"""Validate and persist the user-editable contract form settings."""
import json
import math
from pathlib import Path

PATH = Path(__file__).resolve().parent / 'order_settings.json'
FALLBACK_DEFAULTS = {'enabled': False, 'minute_from': 13.0, 'minute_to': 15.0, 'min_distance_dollars': 50.0, 'min_price_cents': 70.0, 'max_price_cents': 90.0, 'contracts': 1, 'required_models': []}
FORCE_BUY_DEFAULTS = dict(FALLBACK_DEFAULTS, enabled=False, minute_from=0.0, minute_to=15.0, min_distance_dollars=None, min_price_cents=0.0, max_price_cents=100.0, time_in_force='ioc')
MODEL_NAMES = ('Pattern', 'Recent', 'Bootstrap', 'Trend')
CLOSE_DEFAULTS = {'enabled': False, 'profit_per_ct_cents': 0.0, 'ct_price_above_cents': 0.0,
                  'minute_from': 5.0, 'minute_to': 10.0, 'max_brti_delta_dollars': None, 'time_in_force': 'ioc'}
FORCE_CLOSE_DEFAULTS = {'enabled':False,'min_price_cents':0.0,'max_brti_delta_dollars':None,'minute_from':0.0,'minute_to':15.0,'time_in_force':'ioc'}
LIVE_STRENGTHS = ('EXTREMELY WEAK', 'VERY WEAK', 'WEAK', 'MODERATE', 'STRONG')
STOP_LOSS_DEFAULTS = {'minute_from':0.0,'minute_to':15.0,'enabled': True, 'drop_cents': 20.0, 'max_price_cents':100.0, 'max_brti_delta_dollars': None, 'time_in_force': 'ioc'}
ACCURACY_BUY_GUARD_DEFAULTS = {'enabled': False, 'min_accuracy_pct': 60.0, 'window_hours': 24.0}
CHOPPINESS_BUY_GUARD_DEFAULTS = {'enabled': False, 'max_score': 3.0, 'rounds': 3, 'source': 'bitcoin', 'metric': 'midpoint_crossings', 'current_enabled': False, 'current_limit': 7, 'block_next_round': False}
ENTRY_STABILITY_DEFAULTS = {'enabled': False, 'seconds': 60.0}
DEFAULTS = {'entry_stability': dict(ENTRY_STABILITY_DEFAULTS), 'test_mode': False, 'choppiness_buy_guard': dict(CHOPPINESS_BUY_GUARD_DEFAULTS), 'accuracy_buy_guard': dict(ACCURACY_BUY_GUARD_DEFAULTS), 'primary_enabled':True, 'force_buy':dict(FORCE_BUY_DEFAULTS), 'force_close':dict(FORCE_CLOSE_DEFAULTS), 'min_live_strength': 'MODERATE', 'stop_loss': dict(STOP_LOSS_DEFAULTS), 'contract_trend_changes': 10, 'fallback': dict(FALLBACK_DEFAULTS), 'purchase_minute_from': 0.0, 'purchase_minute_to': 15.0, 'min_price_cents': 0.0, 'max_price_cents': 60.0, 'contracts': 1, 'initial_balance': 0.0, 'required_models': list(MODEL_NAMES), 'agreement_basis': 'either'}


DEFAULTS.update(require_final_live_agreement=False, min_model_agreement=0)

def optional_brti_delta(value, label):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f'{label} BRTI delta must be a finite number or blank.')
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f'{label} BRTI delta must be a finite number or blank.') from None
    if not math.isfinite(value):
        raise ValueError(f'{label} BRTI delta must be a finite number or blank.')
    return value


def validate(data):
    if not isinstance(data, dict):
        raise ValueError('Settings must be an object.')
    stability = data.get('entry_stability', ENTRY_STABILITY_DEFAULTS)
    if not isinstance(stability, dict) or not isinstance(stability.get('enabled', False), bool):
        raise ValueError('Entry stability enabled must be true or false.')
    seconds = stability.get('seconds', 60)
    if isinstance(seconds, bool):
        raise ValueError('Entry stability seconds must be greater than 0 and at most 900.')
    try:
        seconds = float(seconds)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Entry stability seconds must be greater than 0 and at most 900.') from None
    if not math.isfinite(seconds) or not 0 < seconds <= 900:
        raise ValueError('Entry stability seconds must be greater than 0 and at most 900.')
    stability = {'enabled': stability.get('enabled', False), 'seconds': seconds}
    primary_enabled = data.get('primary_enabled', True)
    if not isinstance(primary_enabled, bool):
        raise ValueError('Enable Primary must be true or false.')
    min_live_strength = data.get('min_live_strength', 'MODERATE')
    if min_live_strength not in LIVE_STRENGTHS:
        raise ValueError('Select a valid minimum Strength.')
    price = data.get('max_price_cents')
    minimum = data.get('min_price_cents', 0)
    if isinstance(minimum, bool):
        raise ValueError('Minimum price must be a nonnegative number below the maximum price.')
    try:
        minimum = float(minimum)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Minimum price must be a nonnegative number below the maximum price.') from None
    quantity = data.get('contracts')
    if isinstance(price, bool) or isinstance(quantity, bool):
        raise ValueError('Enter numeric price and contract quantity.')
    try:
        price = float(price)
        numeric_quantity = float(quantity)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Enter numeric price and contract quantity.') from None
    if not math.isfinite(price) or not 0 < price <= 100:
        raise ValueError('Maximum price must be greater than 0 and at most 100 cents.')
    if not math.isfinite(minimum) or not 0 <= minimum < price:
        raise ValueError('Minimum price must be nonnegative and below the maximum price.')
    if not math.isfinite(numeric_quantity) or numeric_quantity < 1 or numeric_quantity > 9007199254740991 or not numeric_quantity.is_integer():
        raise ValueError('Contracts must be a positive whole number.')
    changes = data.get('contract_trend_changes', 10)
    if isinstance(changes, bool):
        raise ValueError('Changes must be a positive whole number.')
    try:
        changes = float(changes)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Changes must be a positive whole number.') from None
    if not math.isfinite(changes) or not 1 <= changes <= 9007199254740991 or not changes.is_integer():
        raise ValueError('Changes must be a positive whole number.')
    balance = data.get('initial_balance', 0)
    if isinstance(balance, bool):
        raise ValueError('Initial balance must be a nonnegative number.')
    try:
        balance = float(balance)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Initial balance must be a nonnegative number.') from None
    if not math.isfinite(balance) or balance < 0:
        raise ValueError('Initial balance must be a nonnegative number.')
    minute_from = data.get('purchase_minute_from', 0)
    minute_to = data.get('purchase_minute_to', 15)
    if isinstance(minute_from, bool) or isinstance(minute_to, bool):
        raise ValueError('Purchase minutes must satisfy 0 <= From < To <= 15.')
    try:
        minute_from, minute_to = float(minute_from), float(minute_to)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Purchase minutes must satisfy 0 <= From < To <= 15.') from None
    if not (math.isfinite(minute_from) and math.isfinite(minute_to) and 0 <= minute_from < minute_to <= 15):
        raise ValueError('Purchase minutes must satisfy 0 <= From < To <= 15.')
    models = data.get('required_models', list(MODEL_NAMES))
    if (not isinstance(models, list)
            or any(not isinstance(name, str) or name not in MODEL_NAMES for name in models)
            or len(set(models)) != len(models)):
        raise ValueError('Select valid models without duplicates, or leave all unchecked.')
    require_final_live_agreement = data.get('require_final_live_agreement', False)
    min_model_agreement = data.get('min_model_agreement', 0)
    if not isinstance(require_final_live_agreement, bool):
        raise ValueError('Final/Live agreement must be true or false.')
    if isinstance(min_model_agreement, bool) or not isinstance(min_model_agreement, int) or not 0 <= min_model_agreement <= 4:
        raise ValueError('Minimum model agreement must be a whole number from 0 to 4.')
    basis = data.get('agreement_basis', 'either')
    if basis not in ('final', 'live', 'either', 'both'):
        raise ValueError('Agreement must use Final prediction, Live, either, or both.')
    fallback = data.get('fallback', FALLBACK_DEFAULTS)
    fallback = validate_fallback(dict({'contract_trend_changes':int(changes), 'min_live_strength':min_live_strength}, **fallback) if isinstance(fallback, dict) else fallback)
    fallbacks = data.get('fallbacks', [fallback])
    if not isinstance(fallbacks, list):
        raise ValueError('Fallback rules must be a list.')
    fallbacks = [validate_fallback(dict({'contract_trend_changes':int(changes), 'min_live_strength':min_live_strength}, **rule) if isinstance(rule, dict) else rule) for rule in fallbacks]
    stop = data.get('stop_loss', STOP_LOSS_DEFAULTS)
    if not isinstance(stop, dict) or not isinstance(stop.get('enabled', True), bool):
        raise ValueError('Stop loss must have a valid enabled flag.')
    drop = stop.get('drop_cents', 20)
    if isinstance(drop, bool):
        raise ValueError('Enter a numeric stop loss drop.')
    try:
        drop = float(drop)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Enter a numeric stop loss drop.') from None
    if not math.isfinite(drop) or not 0 < drop <= 100:
        raise ValueError('Stop loss drop must be greater than 0 and at most 100 cents.')
    maximum = stop.get('max_price_cents', 100)
    if isinstance(maximum, bool):
        raise ValueError('Stop loss maximum price must be greater than 0 and at most 100 cents.')
    try:
        maximum = float(maximum)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Enter a numeric stop loss maximum price.') from None
    if not math.isfinite(maximum) or not 0 < maximum <= 100:
        raise ValueError('Stop loss maximum price must be greater than 0 and at most 100 cents.')
    delta = optional_brti_delta(stop.get('max_brti_delta_dollars'), 'Stop loss')
    first, last = stop.get('minute_from', 0), stop.get('minute_to', 15)
    if isinstance(first, bool) or isinstance(last, bool):
        raise ValueError('Stop loss minutes must satisfy 0 <= From < To <= 15.')
    try:
        first, last = float(first), float(last)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Stop loss minutes must satisfy 0 <= From < To <= 15.') from None
    if not (math.isfinite(first) and math.isfinite(last) and 0 <= first < last <= 15):
        raise ValueError('Stop loss minutes must satisfy 0 <= From < To <= 15.')
    stop_tif = stop.get('time_in_force', 'ioc')
    if stop_tif not in ('ioc', 'good_till_date'):
        raise ValueError('Stop loss Time in force must be IOC or Good till date.')
    stop = {'minute_from':first,'minute_to':last,'enabled': stop.get('enabled', True), 'drop_cents': drop, 'max_price_cents':maximum, 'max_brti_delta_dollars':delta, 'time_in_force': stop_tif}
    close = data.get('close_conditions', CLOSE_DEFAULTS)
    if not isinstance(close, dict):
        raise ValueError('Close conditions must be an object.')
    # Preserve the entered number when adopting the requested cents-per-CT unit.
    if 'profit_per_ct_cents' not in close and 'profit_dollars' in close:
        close = dict(close, profit_per_ct_cents=close['profit_dollars'])
    checked = validate_fallback({**CLOSE_DEFAULTS, **close, 'contracts':1, 'min_distance_dollars':0, 'min_price_cents':0, 'max_price_cents':100})
    close = {key: checked[key] for key in CLOSE_DEFAULTS}
    profit = close['profit_per_ct_cents']
    if isinstance(profit, bool):
        raise ValueError('Profit per CT must be a nonnegative number of cents.')
    try:
        profit = float(profit)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Profit per CT must be a nonnegative number of cents.') from None
    if not math.isfinite(profit) or profit < 0:
        raise ValueError('Profit per CT must be a nonnegative number of cents.')
    close['profit_per_ct_cents'] = profit
    ct_price = close['ct_price_above_cents']
    if isinstance(ct_price, bool):
        raise ValueError('Close all CT price must be at least 0 and below 100 cents.')
    try:
        ct_price = float(ct_price)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Close all CT price must be at least 0 and below 100 cents.') from None
    if not math.isfinite(ct_price) or not 0 <= ct_price < 100:
        raise ValueError('Close all CT price must be at least 0 and below 100 cents.')
    close['ct_price_above_cents'] = ct_price
    close['max_brti_delta_dollars'] = optional_brti_delta(close['max_brti_delta_dollars'], 'Close')
    if close['time_in_force'] not in ('ioc', 'good_till_date'):
        raise ValueError('Close Time in force must be IOC or Good till date.')
    # Migrate legacy shared exits into independent fallback settings.
    for rule in fallbacks:
        nested = validate(dict(DEFAULTS, fallbacks=[],
            close_conditions=rule.get('close_conditions', close),
            stop_loss=rule.get('stop_loss', stop)))
        rule['close_conditions'] = nested['close_conditions']
        rule['stop_loss'] = nested['stop_loss']
    force_buy = data.get('force_buy', FORCE_BUY_DEFAULTS)
    if not isinstance(force_buy, dict):
        raise ValueError('Force Buy must be an object.')
    force_buy = validate_fallback(dict(FORCE_BUY_DEFAULTS, **force_buy))
    if force_buy['time_in_force'] not in ('ioc', 'good_till_date'):
        raise ValueError('Force Buy Time in force must be IOC or Good till date.')
    manual = data.get('force_close', data.get('force_stop', FORCE_CLOSE_DEFAULTS))
    if not isinstance(manual, dict):
        raise ValueError('Force Close must be an object.')
    checked_manual = validate_fallback(dict(FORCE_CLOSE_DEFAULTS, **manual, max_price_cents=100,
                                           contracts=1, min_distance_dollars=0))
    manual = {key:checked_manual[key] for key in FORCE_CLOSE_DEFAULTS}
    manual['max_brti_delta_dollars'] = optional_brti_delta(manual['max_brti_delta_dollars'], 'Force Close')
    if manual['time_in_force'] not in ('ioc', 'good_till_date'):
        raise ValueError('Force Close Time in force must be IOC or Good till date.')
    guard = data.get('accuracy_buy_guard', ACCURACY_BUY_GUARD_DEFAULTS)
    if not isinstance(guard, dict) or not isinstance(guard.get('enabled', False), bool):
        raise ValueError('Accuracy buy guard must contain a true/false enabled flag.')
    threshold = guard.get('min_accuracy_pct', 60.0)
    if isinstance(threshold, bool):
        raise ValueError('Minimum accuracy must be between 0 and 100%.')
    try:
        threshold = float(threshold)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Minimum accuracy must be between 0 and 100%.') from None
    if not math.isfinite(threshold) or not 0 <= threshold <= 100:
        raise ValueError('Minimum accuracy must be between 0 and 100%.')
    window_hours = guard.get('window_hours', 24.0)
    if isinstance(window_hours, bool):
        raise ValueError('Accuracy window must be greater than 0 and at most 8760 hours.')
    try:
        window_hours = float(window_hours)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Accuracy window must be greater than 0 and at most 8760 hours.') from None
    if not math.isfinite(window_hours) or not 0 < window_hours <= 8760:
        raise ValueError('Accuracy window must be greater than 0 and at most 8760 hours.')
    guard = {'enabled': guard.get('enabled', False), 'min_accuracy_pct': threshold, 'window_hours': window_hours}
    chop = data.get('choppiness_buy_guard', CHOPPINESS_BUY_GUARD_DEFAULTS)
    if not isinstance(chop, dict) or not isinstance(chop.get('enabled', False), bool):
        raise ValueError('Choppiness buy guard must contain a true/false enabled flag.')
    if chop.get('metric') != 'midpoint_crossings':
        raise ValueError('The score now counts midpoint crossings. Refresh the page before saving its settings.')
    max_score, rounds = chop.get('max_score', 3.0), chop.get('rounds', 3)
    if isinstance(max_score, bool) or isinstance(rounds, bool):
        raise ValueError('Maximum average crossings must be non-negative and rounds a whole number from 1 to 96.')
    try:
        max_score, rounds = float(max_score), float(rounds)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Maximum average crossings must be non-negative and rounds a whole number from 1 to 96.') from None
    if (not math.isfinite(max_score) or max_score < 0
            or not math.isfinite(rounds) or not rounds.is_integer() or not 1 <= rounds <= 96):
        raise ValueError('Maximum average crossings must be non-negative and rounds a whole number from 1 to 96.')
    chop_source = chop.get('source', 'bitcoin')
    if chop_source not in ('bitcoin', 'contract_yes'):
        raise ValueError('Choppiness source must be Bitcoin or Contract Yes.')
    current_enabled, block_next = chop.get('current_enabled', False), chop.get('block_next_round', False)
    if not isinstance(current_enabled, bool) or not isinstance(block_next, bool):
        raise ValueError('Current-round and next-round crossing filters must be true or false.')
    limit = chop.get('current_limit', 7)
    if isinstance(limit, bool):
        raise ValueError('Current-round crossing limit must be a whole number from 1 to 59.')
    try:
        limit = float(limit)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Current-round crossing limit must be a whole number from 1 to 59.') from None
    if not math.isfinite(limit) or not limit.is_integer() or not 1 <= limit <= 59:
        raise ValueError('Current-round crossing limit must be a whole number from 1 to 59.')
    chop = {'enabled': chop.get('enabled', False), 'max_score': max_score, 'rounds': int(rounds), 'source': chop_source, 'metric': 'midpoint_crossings', 'current_enabled': current_enabled, 'current_limit': int(limit), 'block_next_round': block_next}
    test_mode = data.get('test_mode', False)
    if not isinstance(test_mode, bool):
        raise ValueError('Test mode must be true or false.')
    allow_multi_buy = data.get('allow_multi_buy', True)
    if not isinstance(allow_multi_buy, bool):
        raise ValueError('Allow multiple buys must be true or false.')
    time_in_force = data.get('time_in_force', 'ioc')
    if time_in_force not in ('ioc', 'good_till_date'):
        raise ValueError('Time in force must be IOC or Good till date.')
    return {'entry_stability':stability, 'test_mode':test_mode, 'choppiness_buy_guard':chop, 'accuracy_buy_guard':guard, 'primary_enabled':primary_enabled, 'force_buy':force_buy, 'force_close':manual, 'min_live_strength': min_live_strength, 'stop_loss': stop, 'contract_trend_changes': int(changes), 'time_in_force': time_in_force, 'allow_multi_buy': allow_multi_buy, 'close_conditions': close, 'fallback': fallbacks[0] if fallbacks else dict(FALLBACK_DEFAULTS), 'fallbacks': fallbacks, 'purchase_minute_from': minute_from, 'purchase_minute_to': minute_to, 'min_price_cents': minimum, 'max_price_cents': price, 'contracts': int(numeric_quantity), 'initial_balance': balance,
            'required_models': [name for name in MODEL_NAMES if name in models], 'agreement_basis': basis,
            'require_final_live_agreement': require_final_live_agreement, 'min_model_agreement': min_model_agreement}


def validate_fallback(fallback):
    if not isinstance(fallback, dict):
        raise ValueError('Fallback settings must be an object.')
    fallback = dict(FALLBACK_DEFAULTS, **fallback)
    if 'min_live_strength' in fallback and fallback['min_live_strength'] not in LIVE_STRENGTHS:
        raise ValueError('Select a valid fallback Strength.')
    if 'contract_trend_changes' in fallback:
        changes = fallback['contract_trend_changes']
        try:
            numeric = float(changes)
        except (TypeError, ValueError, OverflowError):
            raise ValueError('Fallback Changes must be a positive whole number.') from None
        if isinstance(changes, bool) or not math.isfinite(numeric) or not 1 <= numeric <= 9007199254740991 or not numeric.is_integer():
            raise ValueError('Fallback Changes must be a positive whole number.')
        fallback['contract_trend_changes'] = int(numeric)
    if not isinstance(fallback['enabled'], bool):
        raise ValueError('Fallback enabled must be true or false.')
    for key in FALLBACK_DEFAULTS:
        if key in ('enabled', 'required_models'):
            continue
        value = fallback[key]
        if key == 'min_distance_dollars' and value is None:
            continue
        if isinstance(value, bool):
            raise ValueError('Fallback values must be numeric.')
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError('Fallback values must be numeric.') from None
        if not math.isfinite(value):
            raise ValueError('Fallback values must be finite.')
        fallback[key] = value
    if not 0 <= fallback['minute_from'] < fallback['minute_to'] <= 15:
        raise ValueError('Fallback minutes must satisfy 0 <= From < To <= 15.')
    if not 0 <= fallback['min_price_cents'] < fallback['max_price_cents'] <= 100:
        raise ValueError('Fallback prices must satisfy 0 <= Min < Max <= 100.')
    if not 1 <= fallback['contracts'] <= 9007199254740991 or not fallback['contracts'].is_integer():
        raise ValueError('Fallback CTs must be a positive whole number.')
    models = fallback['required_models']
    if not isinstance(models, list) or any(name not in MODEL_NAMES for name in models):
        raise ValueError('Fallback required models must be a list of Pattern, Recent, Bootstrap, Trend.')
    fallback['required_models'] = [name for name in MODEL_NAMES if name in models]
    fallback['contracts'] = int(fallback['contracts'])
    return fallback


def load():
    if not PATH.exists():
        return validate(DEFAULTS)
    data = json.loads(PATH.read_text())
    guard = data.get('choppiness_buy_guard') if isinstance(data, dict) else None
    if isinstance(guard, dict) and guard.get('metric') != 'midpoint_crossings':
        # The old 0–100 path score has incompatible units. Preserve source and
        # lookback, but do not activate the new gate with a legacy threshold.
        data['choppiness_buy_guard'] = dict(guard, enabled=False, max_score=3.0, metric='midpoint_crossings')
    return validate(data)


def save(settings):
    validated = validate(settings)
    temp = PATH.with_suffix('.tmp')
    temp.write_text(json.dumps(validated, indent=2) + '\n')
    temp.replace(PATH)
    return validated
