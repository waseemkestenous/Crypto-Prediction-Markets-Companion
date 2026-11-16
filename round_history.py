"""Durable round journal; separate from model accuracy statistics."""
import json
from copy import deepcopy
from collections import OrderedDict
import math
import threading
import tempfile
import choppiness
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent
LOCK = threading.RLock()


def crossed_above_50(previous, current):
    valid = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 100
    return valid(current) and current > 50 and (previous is None or valid(previous) and previous <= 50)


def last_50_crossings(samples, start, end, target):
    """Rebuild upward threshold crossings from the available, ordered quotes."""
    ordered = []
    for sample in samples:
        try:
            stamp = datetime.fromisoformat(sample['observed_at'])
            if start <= stamp < end and sample.get('target') == target:
                ordered.append((stamp, sample))
        except (KeyError, TypeError, ValueError):
            continue
    previous = {}
    fields = {}
    for stamp, sample in sorted(ordered, key=lambda item: item[0]):
        for side in ('yes', 'no'):
            value = sample.get(side + '_cents')
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 100:
                continue
            if crossed_above_50(previous.get(side), value):
                fields[f'last_{side}_cross_50_at'] = stamp.isoformat()
                fields[f'last_{side}_cross_50_minute'] = round((stamp-start).total_seconds()/60, 2)
            previous[side] = value
    return fields


def path_for(asset):
    return ROOT / f'round_history_{asset.lower()}.json'


_CACHE = OrderedDict()
CACHE_LIMIT = 8
_CHOPPINESS_CACHE = OrderedDict()
CHOPPINESS_CACHE_LIMIT = 256


def _signature(path):
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size, stat.st_ino
    except FileNotFoundError:
        return None


def _read(asset):
    """Internal immutable snapshot, refreshed if another writer replaces the file."""
    path = path_for(asset).resolve()
    signature = _signature(path)
    cached = _CACHE.get(path)
    if cached is None or cached['signature'] != signature:
        records = json.loads(path.read_text()) if signature is not None else []
        cached = {'signature':signature, 'records':records,
                  'index':{row['round_start']:row for row in records}}
        _CACHE[path] = cached
    _CACHE.move_to_end(path)
    while len(_CACHE) > CACHE_LIMIT:
        _CACHE.popitem(last=False)
    return cached


def _copy_record(row, include_quotes):
    return _copy_json(row if include_quotes else {key:value for key,value in row.items()
                    if key not in ('contract_price_history','underlying_price_history')})


def _copy_json(value):
    """Copy JSON containers without deepcopy's per-scalar dispatch overhead."""
    if isinstance(value, dict):
        return {key: _copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return deepcopy(value)


def load(asset, include_quotes=True):
    with LOCK:
        return [_copy_record(row,include_quotes) for row in _read(asset)['records']]


def get(asset, start, include_quotes=True):
    """Copy just one round rather than parsing/copying the entire journal."""
    with LOCK:
        row = _read(asset)['index'].get(start)
        return _copy_record(row,include_quotes) if row is not None else None


def contract_round_choppiness(asset, start, end, asof):
    """Score one immutable quote snapshot, with bounded caching and no writes."""
    with LOCK:
        row = _read(asset)['index'].get(start.isoformat()) or {}
        samples = row.get('contract_price_history') or ()
        target = row.get('target')
        key = (path_for(asset).resolve(), start.isoformat(), end.isoformat())
        tick = max(0, int((min(asof, end)-start).total_seconds() // choppiness.SAMPLE_SECONDS))
        cached = _CHOPPINESS_CACHE.get(key)
        if (cached is None or cached['samples'] is not samples
                or cached['target'] != target or cached['tick'] != tick):
            cached = {'samples': samples, 'target': target, 'tick': tick,
                      'result': choppiness.score(samples, start, end, target, asof)}
            _CHOPPINESS_CACHE[key] = cached
        _CHOPPINESS_CACHE.move_to_end(key)
        while len(_CHOPPINESS_CACHE) > CHOPPINESS_CACHE_LIMIT:
            _CHOPPINESS_CACHE.popitem(last=False)
        return deepcopy(cached['result'])


def upsert(asset, start, end, **fields):
    with LOCK:
        cached = _read(asset)
        previous = cached['index'].get(start)
        if previous is not None and all(previous.get(key)==value for key,value in fields.items()):
            return deepcopy(previous)
        record = dict(previous) if previous is not None else {
            'asset':asset,'round_start':start,'round_end':end,'target':None,'prediction':None,
            'component_results':{},'settlement':None,'actual':None,'hit':None,'status':'waiting_target'}
        record.update(deepcopy(fields))
        records = [record if row['round_start']==start else row for row in cached['records']]
        if previous is None:
            records.append(record)
        path = path_for(asset)
        # Historical rows are immutable snapshots. Reuse their JSON encodings
        # instead of encoding the entire archive for each live quote update.
        encoded = dict(cached.get('encoded') or {
            row['round_start']: json.dumps(row, separators=(',', ':'))
            for row in cached['records']})
        encoded[start] = json.dumps(record, separators=(',', ':'))
        # Keep the same atomic JSON format; compact encoding avoids rewriting indentation.
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix=path.stem+'.', suffix='.tmp', delete=False) as stream:
            temp = Path(stream.name)
            try:
                stream.write('[' + ','.join(encoded[row['round_start']] for row in records) + ']\n')
            except BaseException:
                temp.unlink(missing_ok=True)
                raise
        try:
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
        resolved = path.resolve()
        _CACHE[resolved] = {'signature':_signature(path),'records':records,
                            'encoded': encoded,
                            'index':{row['round_start']:row for row in records}}
        _CACHE.move_to_end(resolved)
        while len(_CACHE)>CACHE_LIMIT:
            _CACHE.popitem(last=False)
        return deepcopy(record)


def cancel_unconfirmed_purchases(asset, now=None):
    """Expire local records only; this does not cancel exchange orders."""
    now = now or datetime.now(timezone.utc)
    with LOCK:
        for row in list(_read(asset)['records']):
            purchase = row.get('purchase') or {}
            if (purchase.get('status') not in ('attempted', 'submitted')
                    or datetime.fromisoformat(row['round_end']) > now):
                continue
            upsert(asset, row['round_start'], row['round_end'], purchase=dict(
                purchase, status='cancelled', cancelled_at=now.isoformat(),
                cancellation_scope='app_record',
                cancellation_reason='round_ended_without_fill_confirmation',
                previous_status=purchase['status']))


def merged(asset, legacy, include_quotes=True):
    with LOCK:
        cancel_unconfirmed_purchases(asset)
        records = {r['round_start']: dict(r, asset=asset, status='settled') for r in legacy}
        for row in load(asset,include_quotes=include_quotes):
            key = row['round_start']
            # A legacy settlement remains authoritative if journal data is older.
            if key in records and records[key].get('settlement') is not None:
                records[key] = dict(row, **records[key])
                # Boundary settlements have already replaced the legacy sample.
                # Do not restore that sample on every dashboard read, then
                # rewrite the entire journal to replace it again.
                if row.get('settlement_source') == 'next_round_target':
                    for field in ('settlement', 'actual', 'hit', 'component_results',
                                  'status', 'error', 'settlement_source',
                                  'settlement_source_round'):
                        if field in row:
                            records[key][field] = row[field]
            else:
                records[key] = row
        now = datetime.now(timezone.utc)
        # The next-round target is authoritative at the shared boundary.
        by_start = {datetime.fromisoformat(r['round_start']): r for r in records.values()}
        for row in records.values():
            boundary = datetime.fromisoformat(row['round_end'])
            following = by_start.get(boundary)
            if boundary > now or not following:
                continue
            target, settlement = row.get('target'), following.get('target')
            if (not isinstance(target, (int, float)) or not isinstance(settlement, (int, float))
                    or not math.isfinite(target) or not math.isfinite(settlement)
                    or target <= 0 or settlement <= 0):
                continue
            actual = 'UP / ABOVE' if settlement >= target else 'DOWN / BELOW'
            components = {name: dict(item, hit=(item['p_up'] >= .5) == (actual == 'UP / ABOVE'))
                          if item.get('p_up') is not None else dict(item)
                          for name, item in row.get('component_results', {}).items()}
            fields = {'settlement': settlement, 'actual': actual,
                      'hit': row['prediction'] == actual if row.get('prediction') else None,
                      'component_results': components, 'status': 'settled', 'error': None,
                      'settlement_source': 'next_round_target',
                      'settlement_source_round': following['round_start']}
            changed = any(row.get(key) != value for key, value in fields.items())
            row.update(fields)
            if changed:
                upsert(asset, row['round_start'], row['round_end'], **fields)
        for row in records.values():
            minute_fields = {}
            if not row.get('price_threshold_50_backfilled'):
                start_time, end_time = datetime.fromisoformat(row['round_start']), datetime.fromisoformat(row['round_end'])
                stored = _read(asset)['index'].get(row['round_start'],{})
                for sample in stored.get('contract_price_history',row.get('contract_price_history')) or []:
                    try:
                        stamp = datetime.fromisoformat(sample['observed_at'])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not start_time <= stamp < end_time or sample.get('target') != row.get('target'):
                        continue
                    for side, key in (('up','yes_cents'), ('down','no_cents')):
                        value = sample.get(key)
                        if not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value) or not 50 < value <= 100:
                            continue
                        for kind in ('first','last'):
                            event = f'{kind}_{side}_above_50'
                            old = minute_fields.get(event+'_at', row.get(event+'_at'))
                            try:
                                previous = datetime.fromisoformat(old) if old else None
                            except (TypeError, ValueError):
                                previous = None
                            if previous is None or (stamp < previous if kind=='first' else stamp > previous):
                                minute_fields[event+'_at'] = stamp.isoformat()
                                minute_fields[event+'_minute'] = round((stamp-start_time).total_seconds()/60,2)
                if end_time <= now:
                    minute_fields['price_threshold_50_backfilled'] = True
            if not row.get('price_crossings_50_backfilled'):
                start_time, end_time = datetime.fromisoformat(row['round_start']), datetime.fromisoformat(row['round_end'])
                stored = _read(asset)['index'].get(row['round_start'], {})
                minute_fields.update(last_50_crossings(
                    stored.get('contract_price_history', row.get('contract_price_history')) or [],
                    start_time, end_time, row.get('target')))
                if end_time <= now:
                    minute_fields['price_crossings_50_backfilled'] = True
            row.update(minute_fields)
            for event in ('last_up_above_50', 'last_down_above_50', 'first_up_above_50', 'first_down_above_50', 'last_live_change', 'last_up_above_80', 'last_down_above_80', 'first_up_above_80', 'first_down_above_80'):
                if row.get(event + '_at') and row.get(event + '_minute') is None:
                    try:
                        elapsed = (datetime.fromisoformat(row[event + '_at']) - datetime.fromisoformat(row['round_start'])).total_seconds() / 60
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(elapsed) and 0 <= elapsed <= 15:
                        minute_fields[event + '_minute'] = round(elapsed, 2)
            if minute_fields:
                row.update(minute_fields)
                upsert(asset, row['round_start'], row['round_end'], **minute_fields)
            if row.get('actual') and row.get('result_confirmed_by') == 'user':
                row['status'] = 'settled'
            elif row.get('status') in ('waiting_target', 'analyzing', 'predicted') and datetime.fromisoformat(row['round_end']) <= now:
                row['status'] = 'unsettled' if row.get('prediction') else 'no_prediction'
        return sorted(records.values(), key=lambda r: datetime.fromisoformat(r['round_start']), reverse=True)


def purchase_accuracy(records):
    trades = [(trade, row.get('actual')) for row in records for trade in round_trades(row)]
    filled = [(trade, actual) for trade, actual in trades if trade.get('status') == 'filled']
    hits = misses = awaiting = breakevens = 0
    for trade, actual in filled:
        closing = trade.get('close_order') or {}
        if closing:
            pnl = trade_pnl(trade, actual)
            if pnl is None:
                awaiting += 1
            elif pnl > 0:
                hits += 1
            elif pnl < 0:
                misses += 1
            else:
                breakevens += 1
        elif actual in ('UP / ABOVE', 'DOWN / BELOW') and trade.get('direction') in ('UP / ABOVE', 'DOWN / BELOW'):
            if trade['direction'] == actual:
                hits += 1
            else:
                misses += 1
        else:
            awaiting += 1
    total = hits + misses
    return {'total': total, 'hits': hits, 'misses': misses,
            'accuracy': round(hits / total * 100, 2) if total else None,
            'filled_rounds': len(filled), 'filled_trades': len(filled),
            'awaiting_result': awaiting, 'breakevens': breakevens,
            'unconfirmed_orders': sum(trade.get('status') == 'submitted' for trade, _ in trades)}


def final_prediction_accuracy(records):
    """Accuracy of frozen final predictions across settled rounds, including unbought rounds."""
    directions = ('UP / ABOVE', 'DOWN / BELOW')
    completed = [row for row in records
                 if (row.get('settlement') is not None or row.get('result_confirmed_by') == 'user')
                 and row.get('prediction') in directions
                 and row.get('actual') in directions]
    hits = sum(row['prediction'] == row['actual'] for row in completed)
    total = len(completed)
    return {'total': total, 'hits': hits, 'misses': total - hits,
            'accuracy': round(hits / total * 100, 2) if total else None}


def accuracy_buy_guard(records, settings, now):
    """Gate buys using settled final predictions within the configured rolling window."""
    config = settings.get('accuracy_buy_guard') or {}
    enabled = config.get('enabled', False)
    threshold = config.get('min_accuracy_pct', 60.0)
    window_hours = config.get('window_hours', 24.0)
    cutoff = now - timedelta(hours=window_hours)
    recent = []
    for row in records:
        try:
            ended = datetime.fromisoformat(row['round_end'])
            if cutoff < ended <= now:
                recent.append(row)
        except (KeyError, TypeError, ValueError):
            continue
    stats = final_prediction_accuracy(recent)
    allowed = not enabled or (stats['total'] > 0 and stats['hits'] * 100 / stats['total'] >= threshold)
    return dict(stats, enabled=enabled, min_accuracy_pct=threshold, window_hours=window_hours, allowed=allowed)


def round_trades(row):
    trades = list(row.get('completed_purchases') or [])
    if row.get('purchase'):
        trades.append(row['purchase'])
    return trades


def trade_pnl(purchase, actual):
    """Modeled net P/L; recorded execution prices take precedence over limits."""
    from decimal import Decimal, InvalidOperation
    if purchase.get('status') != 'filled' or purchase.get('direction') not in ('UP / ABOVE', 'DOWN / BELOW'):
        return None
    closing = purchase.get('close_order') or {}
    if closing and closing.get('status') != 'filled':
        # An unfilled sale does not replace the end-of-round payout.
        if actual not in ('UP / ABOVE', 'DOWN / BELOW'):
            return None
        closing = {}
    if not closing and actual not in ('UP / ABOVE', 'DOWN / BELOW'):
        return None
    try:
        quantity = Decimal(str(purchase.get('contracts')))
        price = Decimal(str(purchase.get('execution_price_cents', purchase.get('limit_price_cents')))) / 100
        if not quantity.is_finite() or not price.is_finite() or quantity <= 0 or quantity != quantity.to_integral_value() or not 0 <= price <= 1:
            return None
        buy_fee = Decimal(str(purchase.get('fees_dollars', quantity * Decimal('0.0194'))))
        if closing:
            sold = Decimal(str(closing.get('contracts')))
            sale_price = Decimal(str(closing.get('execution_price_cents', closing.get('limit_price_cents')))) / 100
            close_fee = Decimal(str(closing.get('fees_dollars', quantity * Decimal('0.0194'))))
            if sold != quantity or not sale_price.is_finite() or not 0 <= sale_price <= 1:
                return None
            proceeds = quantity * sale_price
        else:
            close_fee = Decimal(0)
            proceeds = quantity if purchase['direction'] == actual else Decimal(0)
        if not buy_fee.is_finite() or not close_fee.is_finite() or buy_fee < 0 or close_fee < 0:
            return None
        return float(proceeds - quantity * price - buy_fee - close_fee)
    except (InvalidOperation, ValueError, TypeError):
        return None


def purchase_pnl(row):
    from decimal import Decimal
    profits = [trade_pnl(trade, row.get('actual')) for trade in round_trades(row)]
    values = [Decimal(str(value)) for value in profits if value is not None]
    return float(sum(values, Decimal(0))) if values else None


def balance_summary(records, initial_balance):
    from decimal import Decimal
    trades = [(trade, row.get('actual')) for row in records for trade in round_trades(row)]
    profits = [trade_pnl(trade, actual) for trade, actual in trades]
    total = sum((Decimal(str(value)) for value in profits if value is not None), Decimal(0))
    missing = sum(trade.get('status') == 'filled' and value is None
                  for (trade, _), value in zip(trades, profits))
    return {'initial_balance': initial_balance, 'net_pnl': float(total),
            'current_balance': float(Decimal(str(initial_balance)) + total),
            'counted_purchases': sum(value is not None for value in profits),
            'pending_or_missing': missing}
