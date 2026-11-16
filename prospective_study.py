"""Append-only research snapshots. This module never makes trading decisions."""
import json
from pathlib import Path
from datetime import datetime, timedelta
import choppiness

ROOT = Path(__file__).resolve().parent / 'research_data' / 'entry_quality_v1'

def snapshot(start, end, now, target, underlying, contracts, prediction, recommendation, settings):
    points = []
    for p in underlying:
        stamp = datetime.fromisoformat(p['observed_at'])
        if start <= stamp <= now and isinstance(p.get('price'), (int, float)):
            points.append((stamp, p['price']))
    points.sort()
    recent = [(t,p) for t,p in points if (now-t).total_seconds() <= 120]
    spread = max((p for _,p in recent), default=0)-min((p for _,p in recent), default=0)
    latest = points[-1] if points else None
    delta = latest[1]-target if latest and isinstance(target,(int,float)) else None
    side = (delta>0)-(delta<0) if delta is not None else 0
    stable_since = None
    if side:
        for stamp,price in reversed(points):
            if (price>target)-(price<target) != side: break
            stable_since = stamp
    return dict(schema_version=1, observed_at=now.isoformat(), round_start=start.isoformat(),
        round_end=end.isoformat(), target=target, seconds_remaining=(end-now).total_seconds(),
        btc_price=latest[1] if latest else None,
        btc_quote_age_seconds=(now-latest[0]).total_seconds() if latest else None,
        target_delta_dollars=delta, range_120s_dollars=spread if recent else None,
        normalized_target_distance=delta/spread if delta is not None and spread>0 else None,
        observed_same_side_seconds=(now-stable_since).total_seconds() if stable_since else None,
        recent_sample_count=len(recent),
        crossings=choppiness.score(underlying,start,end,target,now,source='bitcoin'),
        crossings_120s=choppiness.score(underlying,max(start,now-timedelta(seconds=120)),end,target,now,source='bitcoin'),
        contract_quotes=contracts, model_snapshot={k:prediction.get(k) for k in ('prediction','p_pattern','p_regime','p_bootstrap','p_trend')},
        live_recommendation=recommendation, order_settings=settings,
        note='Observed persistence may span missing quotes; inspect quote coverage before evaluating. Snapshot is not a qualified buy signal.')

def append(asset, start, record):
    if not asset.isalnum(): raise ValueError('Invalid asset')
    path=ROOT/asset.lower()/f'{int(start.timestamp())}.jsonl'
    path.parent.mkdir(parents=True,exist_ok=True)
    # Serialize before opening so invalid values cannot leave partial records.
    line=json.dumps(record,default=lambda v:v.isoformat(),allow_nan=False,separators=(',',':'))
    with path.open('a') as stream: stream.write(line+'\n')
