"""Exploratory, chronological replay of observed contract quotes, never orders."""
import math
from datetime import datetime, timezone
from itertools import product

MODEL_KEYS = {'Pattern': 'Similar-pattern', 'Recent': 'Recent 3h regime',
              'Bootstrap': 'Historical bootstrap', 'Trend': 'Trend/regime'}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def prepare_round(row):
    direction = row.get('prediction')
    if direction not in ('UP / ABOVE', 'DOWN / BELOW'):
        return None
    key = 'yes_cents' if direction == 'UP / ABOVE' else 'no_cents'
    try:
        start, end = datetime.fromisoformat(row['round_start']), datetime.fromisoformat(row['round_end'])
    except (KeyError, TypeError, ValueError):
        return None
    points = []
    for sample in row.get('contract_price_history') or []:
        try:
            stamp = datetime.fromisoformat(sample['observed_at'])
            minute = (stamp-start).total_seconds()/60
            price = sample[key]
            if (not start <= stamp < end or sample.get('target') != row.get('target')
                    or not finite(price) or not 0 < price <= 100):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        points.append((minute, price))
    points.sort()
    changes = []
    observations = []
    for minute, price in points:
        if not changes or changes[-1][1] != price:
            changes.append((minute, price))
        trends = []
        for count in (3, 5, 10, 20):
            window = changes[-(count+1):]
            if len(window) != count+1:
                continue
            mt = sum(t for t, _ in window)/len(window)
            mp = sum(p for _, p in window)/len(window)
            if sum((t-mt)*(p-mp) for t, p in window) > 0:
                trends.append(count)
        observations.append((minute, price, trends))
    if not observations:
        return None
    agreed = []
    for label, stored_key in MODEL_KEYS.items():
        model = row.get('component_results', {}).get(stored_key, {})
        probability = model.get('p_up')
        if finite(probability) and ('UP / ABOVE' if probability >= .5 else 'DOWN / BELOW') == direction:
            agreed.append(label)
    return {'observations': observations, 'agreed': agreed, 'hit': row['actual'] == direction,
            'round_start': row['round_start']}


def evaluate(rounds, config):
    trades = []
    for row in rounds:
        if not set(config['required_models']).issubset(row['agreed']):
            continue
        for minute, price, trends in row['observations']:
            if (config['purchase_minute_from'] <= minute < config['purchase_minute_to']
                    and config['min_price_cents'] < price < config['max_price_cents']
                    and config['contract_trend_changes'] in trends):
                # One contract, first eligible observation; hold through settlement.
                pnl = (1 if row['hit'] else 0) - price/100 - .0194
                trades.append({'round_start':row['round_start'], 'minute':round(minute,2),
                               'price_cents':price, 'hit':row['hit'], 'pnl':round(pnl,6)})
                break
    total = sum(t['pnl'] for t in trades)
    hits = sum(t['hit'] for t in trades)
    return {'trades':len(trades), 'hits':hits, 'win_rate':round(hits/len(trades)*100,2) if trades else None,
            'net_pnl':round(total,4), 'average_pnl':round(total/len(trades),4) if trades else None,
            'eligible_rounds':len(rounds), 'entries':trades}


def analyze(rows, now=None):
    now = now or datetime.now(timezone.utc)
    completed = []
    for row in rows:
        try:
            ended = datetime.fromisoformat(row['round_end']) <= now
        except (KeyError, TypeError, ValueError):
            continue
        if ended and row.get('actual') in ('UP / ABOVE','DOWN / BELOW'):
            completed.append(row)
    completed.sort(key=lambda row: datetime.fromisoformat(row['round_start']))
    rounds = [prepared for row in completed if (prepared := prepare_round(row))]
    result = {'completed_rounds':len(completed), 'quote_rounds':len(rounds),
              'excluded_rounds':len(completed)-len(rounds), 'updated_at':now.isoformat(),
              'status':'insufficient', 'recommendation':None,
              'method':'First eligible observed quote per round, 1 contract, Final prediction, hold to settlement. Buy fee estimate 1.94¢; no settlement sell fee. Chronological 70% training / 30% validation.',
              'limitations':['Quotes are observations, not executable order-form prices; No is estimated.',
                             'Replay assumes frozen predictions/models were available at each observed quote.',
                             'Live strength/direction history and historical sell bids are incomplete: Live, fallback distance, stop loss and early close are not replayed.',
                             'No automatic settings changes. This is an exploratory comparison, not guaranteed profit.']}
    if len(rounds) < 6:
        return result
    split = int(len(rounds)*.7)
    training, validation = rounds[:split], rounds[split:]
    windows = [(0,15),(0,5),(3,10),(5,10),(5,15),(8,13),(10,15),(12,15),(13,15)]
    prices = [(0,40),(0,60),(10,50),(20,60),(30,70),(40,80),(50,90),(60,95),(70,95)]
    models = [[], *[[name] for name in MODEL_KEYS], list(MODEL_KEYS)]
    best = None
    tested = 0
    for (first,last),(minimum,maximum),count,required in product(windows,prices,(3,5,10,20),models):
        config = {'purchase_minute_from':first,'purchase_minute_to':last,
                  'min_price_cents':minimum,'max_price_cents':maximum,
                  'contract_trend_changes':count,'required_models':required,
                  'agreement_basis':'final','contracts':1}
        stats = evaluate(training, config)
        tested += 1
        if stats['trades'] < 3:
            continue
        score = (stats['net_pnl'], stats['trades'])
        if best is None or score > best[0]:
            best = (score,config,stats)
    result.update(training_rounds=len(training),validation_rounds=len(validation),candidates_tested=tested)
    if best is None:
        return result
    _, config, stats = best
    holdout = evaluate(validation,config)
    result.update(status='exploratory' if len(rounds)<30 else 'historical',
                  recommendation=config,training=stats,validation=holdout,
                  combined=evaluate(rounds,config),
                  supported=holdout['trades']>=3 and holdout['net_pnl']>0,
                  training_end=training[-1]['round_start'],validation_start=validation[0]['round_start'])
    return result
