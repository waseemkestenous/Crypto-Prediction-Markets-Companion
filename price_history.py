"""Append-only, per-round underlying observations for restart-safe charts."""
import json
from collections import OrderedDict
from copy import deepcopy
import math
import threading
import choppiness
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent / 'price_history'
LOCK = threading.RLock()
_READ_CACHE = OrderedDict()
# A 25-round page uses chart, live-change and scoring entries per round.
# Keep the whole working set so sequential polling cannot evict every entry.
READ_CACHE_LIMIT = 128
_CHOPPINESS_CACHE = OrderedDict()
CHOPPINESS_CACHE_LIMIT = 256


def path_for(asset,start):
    if not asset.isalnum():
        raise ValueError('Invalid asset')
    return ROOT / asset.lower() / f'{int(start.timestamp())}.jsonl'


def append(asset,start,point):
    with LOCK:
        path=path_for(asset,start)
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('a') as stream:
            stream.write(json.dumps(point,separators=(',',':'))+'\n')


def append_live(asset, start, recommendation):
    """Keep baseline/candidate probabilities for future prospective evaluation."""
    record = dict(recommendation, updated_at=recommendation['updated_at'].isoformat())
    with LOCK:
        path = path_for(asset,start).with_suffix('.live.jsonl')
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('a') as stream:
            stream.write(json.dumps(record,separators=(',',':'),allow_nan=False)+'\n')


def live_changes(asset, start, end):
    """Read actual direction transitions from the saved recommendation stream."""
    if start is None or end is None:
        return []
    with LOCK:
        path = path_for(asset, start).with_suffix('.live.jsonl')
        try:
            stat = path.stat()
        except FileNotFoundError:
            return []
        key = (str(path.resolve()), start.isoformat(), end.isoformat(), 'live_changes')
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        cached = _READ_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            _READ_CACHE.move_to_end(key)
            return deepcopy(cached[1])
        changes, previous, last_stamp = [], None, None
        for line in path.read_text().splitlines():
            try:
                record = json.loads(line)
                stamp = datetime.fromisoformat(record['updated_at'])
                direction = record['recommendation']
                if (not start <= stamp < end or direction not in ('UP / ABOVE', 'DOWN / BELOW')
                        or (last_stamp is not None and stamp <= last_stamp)):
                    continue
                if previous is not None and previous != direction:
                    changes.append({'observed_at': stamp.isoformat(),
                                    'minute': (stamp-start).total_seconds()/60,
                                    'direction': direction, 'previous_direction': previous})
                previous, last_stamp = direction, stamp
            except (KeyError, ValueError, TypeError):
                continue
        _READ_CACHE[key] = (signature, changes)
        _READ_CACHE.move_to_end(key)
        while len(_READ_CACHE) > READ_CACHE_LIMIT:
            _READ_CACHE.popitem(last=False)
        return deepcopy(changes)


def volatility_60s(points, stamp):
    """Observed high-low range, not a forecast or an annualized volatility."""
    window = []
    for point in reversed(points):
        observed = datetime.fromisoformat(point['observed_at'])
        age = (stamp-observed).total_seconds()
        if age > 60:
            break
        if age >= 0:
            window.append((observed, point['price']))
    window.reverse()
    coverage = (window[-1][0]-window[0][0]).total_seconds() if window else 0
    gaps = [(b[0]-a[0]).total_seconds() for a,b in zip(window, window[1:])]
    history_seconds = (stamp-datetime.fromisoformat(points[0]['observed_at'])).total_seconds() if points else 0
    complete = history_seconds >= 60 and len(window) >= 4 and coverage >= 50 and max(gaps, default=0) <= 20
    high = max((p for _,p in window), default=None)
    low = min((p for _,p in window), default=None)
    spread = high-low if complete else None
    up_moves = down_moves = reversals = 0
    previous_direction = 0
    for (_,before),(_,after) in zip(window,window[1:]):
        direction = (after > before) - (after < before)
        if not direction:
            continue
        up_moves += direction > 0
        down_moves += direction < 0
        if previous_direction and direction != previous_direction:
            reversals += 1
        previous_direction = direction
    return {'direction_reversals': reversals if complete else None,
            'up_moves': up_moves if complete else None, 'down_moves': down_moves if complete else None,
            'range_dollars': spread, 'range_pct': spread/low*100 if complete else None,
            'high': high, 'low': low, 'samples': len(window),
            'coverage_seconds': coverage, 'status': 'ready' if complete else 'insufficient_data'}


def load(asset,start,end,include_volatility=True):
    """Load validated source observations; scoring can omit rolling chart metrics."""
    with LOCK:
        path=path_for(asset,start)
        try:
            stat=path.stat()
        except FileNotFoundError:
            return []
        cache_key=(str(path.resolve()),start.isoformat(),end.isoformat(),include_volatility)
        signature=(stat.st_mtime_ns,stat.st_size,stat.st_ino)
        cached=_READ_CACHE.get(cache_key)
        if cached is not None and cached[0]==signature:
            _READ_CACHE.move_to_end(cache_key)
            return deepcopy(cached[1])
        points=[]
        for line in path.read_text().splitlines():
            try:
                point=json.loads(line)
                stamp=datetime.fromisoformat(point['observed_at'])
                price=point['price']
                if (not start<=stamp<end or not isinstance(price,(int,float)) or isinstance(price,bool)
                        or not math.isfinite(price) or price<=0):
                    continue
                if points and stamp<=datetime.fromisoformat(points[-1]['observed_at']):
                    continue
                restored={'observed_at':stamp.isoformat(),'minute':(stamp-start).total_seconds()/60,'price':float(price)}
                if include_volatility and 'volatility_60s' in point:
                    # Recalculate from saved source observations; ignore untrusted derived fields.
                    restored['volatility_60s']=volatility_60s(points+[restored],stamp)
                points.append(restored)
            except (KeyError,ValueError,TypeError):
                # Ignore a truncated final line after an interrupted write.
                continue
        result=points[-2000:]
        _READ_CACHE[cache_key]=(signature,result)
        _READ_CACHE.move_to_end(cache_key)
        while len(_READ_CACHE)>READ_CACHE_LIMIT:
            _READ_CACHE.popitem(last=False)
        return deepcopy(result)


def round_choppiness(start, end, asof, target=None):
    """Bitcoin score cached by the source file revision and sampling instant."""
    with LOCK:
        path = path_for('BTC', start)
        try:
            stat = path.stat()
            signature = stat.st_mtime_ns, stat.st_size, stat.st_ino
        except FileNotFoundError:
            signature = None
        key = str(path.resolve()), start.isoformat(), end.isoformat(), target
        tick = max(0, int((min(asof, end)-start).total_seconds() // choppiness.SAMPLE_SECONDS))
        cached = _CHOPPINESS_CACHE.get(key)
        if cached is None or cached['signature'] != signature or cached['tick'] != tick:
            samples = load('BTC', start, end, include_volatility=False)
            cached = {'signature': signature, 'tick': tick,
                      'result': choppiness.score(samples, start, end, target, asof, source='bitcoin')}
            _CHOPPINESS_CACHE[key] = cached
        _CHOPPINESS_CACHE.move_to_end(key)
        while len(_CHOPPINESS_CACHE) > CHOPPINESS_CACHE_LIMIT:
            _CHOPPINESS_CACHE.popitem(last=False)
        return deepcopy(cached['result'])
